# AGENTS.md — harness_eval_gateway

Reference document for the agent working on this repo. It captures the product
vision, agreed architecture, domain model, and working conventions. Keep it
updated as decisions land.

## Project overview

A platform for comparatively evaluating **agent harnesses** (Claude Code, Codex
CLI, Aider, custom minimal loops, ...) on coding tasks, with a **custom AI
gateway** as the observability layer.

A user submits a task (e.g. "build a replica of 2048") plus a run
configuration. The system spawns isolated Docker sandboxes across a
**config × model matrix** — each harness configuration runs on every selected
participant model (a per-config `model` overrides the run-level model list;
PR14 reversed the original harnesses-only stance). Results compare harness
capability and model quality side by side. An independent LLM evaluator
scores each produced artifact against a human-approved rubric. All model
traffic and agent activity are captured as structured events powering a live
+ persisted execution timeline.

**MVP shape:** 1 model (Ollama, small local model) × 2 harness configs → 2
concurrent sandboxes. Evaluator runs afterwards in its own sandbox. Since
**miniloop** (our custom harness) is the first adapter, the MVP comparison
axis is harness *configuration* — e.g. different skill pack sets or system
prompts across the two sandboxes. Multi-harness comparisons land as CLI
adapters are added.

## Core concepts

- **Run** — one submitted evaluation. Spec = task prompt + model(s) + set of
  harnesses + skill packs + evaluator model. `model_name` is the default
  participant model; `models` (normalized into `config.models`) selects the
  full participant set.
- **Task preset (PR22)** — a reusable evaluation task. Vendored presets are
  committed Markdown under `task_presets/<slug>.md` (frontmatter metadata +
  body = the prompt); user presets are `task_presets` table rows. One shared
  slug namespace; submitting `preset_ref` resolves the preset, replaces the
  task prompt with the preset body verbatim, and snapshots
  `runs.preset_ref` + `preset_revision`. Freeform prompts stay supported —
  `task_prompt` is always the authoritative run text.
- **Sandbox** — a Docker container running one harness instance on the task,
  with an identical clean initial repo/workspace as its siblings.
- **Harness adapter** — per-harness plugin: knows how to install, configure,
  launch, observe, and terminate a given agent CLI inside a sandbox.
- **Prompt cell** — a no-sandbox matrix cell (`adapter: "prompt"`): one
  gateway-metered chat call from the orchestrator process, no container.
  Same sandbox row / cred / artifact / evaluation / scoring pipeline.
- **AI gateway** — lightweight custom proxy; the only egress route for model
  traffic. Metering, event capture, cost accounting, budget enforcement.
- **Event** — structured, timestamped record (LLM request/response, tool call,
  shell command, file op, test execution, checkpoint, done signal, ...).
  Powers the live execution timeline and post-run analysis.
- **Rubric** — scoring spec generated from the task prompt, reviewed/edited by
  the user before launch, then **frozen** and shared across all sandboxes.
- **Artifact** — final workspace snapshot (tarball / git bundle) + full
  transcript. Stored on local disk, metadata indexed in Postgres.
- **Evaluator** — post-run LLM judge running in its own sandbox (same infra,
  traffic via gateway), scoring each artifact per rubric dimension.

## Architecture

```
┌──────────┐  submit   ┌─────────────┐  spawn N  ┌──────────────────┐
│ Next.js  │──────────▶│ Control API │──────────▶│ Agent Sandboxes  │
│ web UI   │           │ Orchestrator│           │ (Docker, 1 per   │
│          │◀──────────│             │           │  harness)        │
└────┬─────┘  results  └──────┬──────┘           └────────┬─────────┘
     │                        │                           │ LLM traffic
     │  live timeline         │                           ▼
     │  (SSE)           ┌──────┴──────┐           ┌──────────────────┐   ┌───────────┐
     └─────────────────▶│  Postgres   │◀──────────│   AI Gateway     │──▶│ Providers │
                        │ runs/events │  events   │ custom proxy,    │   │ OpenRouter│
                        │ scores/     │           │ per-run creds,   │   │ Ollama /  │
                        │ rubric/meta │           │ budgets          │   │ direct    │
                        └──────▲──────┘           └──────────────────┘   └───────────┘
                               │ scores
                        ┌──────┴──────┐           ┌──────────────────┐
                        │ Evaluator   │◀──────────│ Artifact store   │
                        │ (sandboxed  │ artifacts │ (local disk:     │
                        │  LLM judge) │           │ repos,transcripts│
                        └─────────────┘           └──────────────────┘
```

### Components

1. **Web UI (Next.js)** — submission form (task prompt, model, harnesses,
   skill packs, evaluator model), rubric review/edit screen, run status,
   live execution timeline (SSE), persisted timeline replay, results matrix,
   flag when evaluator model == any participant model.
2. **Control API + Orchestrator (FastAPI, async)** — validates submissions,
   expands run matrix, generates rubric via gateway, drives run state machine
   (`submitted → rubric_review → [queued →] provisioning → running →
   evaluating → done | failed | cancelled`), manages sandbox lifecycle,
   enforces termination policy, paces run/sandbox admission (below).
3. **AI gateway (Python, custom)** — exposes an OpenAI-compatible
   (`/v1/chat/completions`) endpoint, translates to upstream providers
   (**Ollama is the default upstream for MVP** — small local models, e.g.
   `qwen2.5-coder:3b`). Anthropic-compatible (`/v1/messages`) endpoint added
   when a CLI adapter requires it. Per-run credentials map traffic to
   run+sandbox. Records: model, tokens in/out, cache read/write, latency,
   per-call speed (`ttft_ms` first-byte, `total_ms`, `tokens_per_sec` —
   PR30), cost, failures. Emits events to Postgres. Enforces token/cost
   budgets.
4. **Sandboxes (Docker)** — per-harness image + installed skill packs + clean
   workspace + injected `done` mechanism. Env points harness at gateway with
   run-scoped creds. Egress locked to gateway — no telemetry holes.
   Adapter tails harness output and pushes normalized events.
5. **Postgres** — run specs, sandbox instances, frozen rubric, append-only
   event stream, artifact metadata, evaluator scores.
6. **Evaluator** — runs in its own sandbox post-run; fetches artifact + rubric,
   scores via gateway, returns score + rationale per dimension. Objective
   signals (PR6+PR7+PR8): the `scoring` collector registry measures the
   workspace before judging — parse-only static analysis (real cyclomatic
   complexity, size, language mix) plus, when enabled, exec scoring:
   build/tests/coverage run inside a locked `heg-exec` container (zero
   egress, resource-capped, artifact read-only). PR8 adds a third signal,
   `generated_tests`: the evaluator itself calls the model (via gateway,
   eval cred) to WRITE a pytest suite for the submission, persists it at
   `<artifact>/generated_tests/`, and runs it against the untouched
   workspace in the same locked container — pass rate folds into the same
   report. All feed the judge `ANALYSIS.md`/`analysis.json`.
   Judge-output hardening (small local judges): the scores parser repairs
   single-quoted Python-literal pseudo-JSON (`ast.literal_eval`), retries
   feed the prior `eval_scores_failed` detail back into `EVAL_PROMPT.md`
   (`prior_error`), and a control-plane direct-completion fallback
   (`evaluator/judge_direct.py` — one chat call on the eval cred,
   `response_format=json_object`; `EVAL_DIRECT_FALLBACK=0` disables) lands
   scores when the agentic judge's output is unusable. Testgen retries the
   generation call once at raised caps on empty/timed-out/zero-file output
   (`TESTGEN_MAX_TOKENS_RETRY`, up to 2× `TESTGEN_TIMEOUT_SECONDS` clamped
   to the eval-attempt deadline); a scores doc
   missing `overall` gets it synthesized as the dimension mean + a warning.
7. **Artifact store** — `./data/artifacts/<run_id>/<sandbox_id>/` — repo
   tarballs, transcripts, event exports.

## Run lifecycle (maps to product flow)

1. **Submit** — form → run spec (`POST /api/runs`).
2. **Rubric draft** — orchestrator calls LLM via gateway → draft rubric →
   UI displays; user edits → approves → **frozen**, shared across sandboxes.
   **Pacing (PR15):** runs serialize — one active run at a time; an approval
   landing while a run executes (or is already queued) parks in `queued`
   and promotes FIFO by approval when the slot frees (lifecycle end or the
   next startup sweep). `started_at` = approval/queue-entry time.
   Drafting itself is pre-queue: a process-wide semaphore
   (`RUBRIC_GEN_CONCURRENCY`, default 1) serializes the gateway call across
   parallel submits — a run parked on it stays `submitted` and emits a
   `rubric_queued` checkpoint while it waits.
3. **Provision** — prepare identical initial repo; build/launch N sandboxes.
   **Task inputs (PR24):** `run.config.task_inputs` (`repo_url`+`ref` public
   https clone, or `archive_id` staged via `POST /api/uploads`) is
   materialized once per run under `data/seeds/<run_id>/content/`
   (`sandbox/seeds.py::prepare_run_seed`, tmp-dir + atomic rename,
   idempotent) and `copy_seed`ed into every cell's workspace before
   `docker run` — identical bytes per cell, proven by
   `harness_config.seed_sha` (the workspace copy's `hash_tree`) plus a
   `stage:"seed"` checkpoint per sandbox. Archive members are jail-checked
   twice (upload `check_archive` + `extract_archive` — never `extractall`;
   traversal/absolute/links/specials rejected, decompression capped) and an
   escapee-symlink sweep runs post-materialization. Prompt cells (PR25)
   have no workspace — seeds don't apply. `spawn_one` additionally gates
   each `docker run` on host memory headroom
   (`SPAWN_MIN_FREE_MEM_MB`, probed via `/proc/meminfo` → `sysconf` →
   `docker info`+`stats`; unreadable probe = gate off). The wait is capped
   by `SPAWN_MEM_WAIT_SECONDS` — expiry fails that one sandbox and makes
   the remaining configs fail fast (one probe each), so sustained pressure
   can't hold the run's slot for N×cap.
   **Local-run sandbox pacing (F30):** when EVERY participant model
   resolves to a local provider (ollama — `pacing.provider_for_model`
   mirrors the gateway's `PROVIDER_ROUTES`/`UPSTREAM_PROVIDER` routing),
   at most `SANDBOX_MAX_CONCURRENT_LOCAL` (default 2, 0 disables) cells'
   containers are active at once. Cells past the cap insert as
   `Sandbox.status="queued"` BEFORE `docker run` (`sandbox_queued`
   checkpoint, FIFO `ahead` position) and promote `queued → provisioning`
   via `pacing.claim_queued_sandbox` as siblings go terminal —
   `lifecycle._watch_cells_paced` runs the claim between `asyncio.wait`
   completions so order is deterministic. The launch then runs the same
   pipeline (pack staging, mem gate, cred, `docker run`) — the cap is a
   container-slot count, not a call-rate limit: a small local model can't
   honestly serve N parallel agents anyway. Prompt cells hold no slot.
4. **Execute** — agent loops; all model calls via gateway; adapter streams
   normalized events; UI shows live timeline.
5. **Terminate** — each sandbox reaches a terminal state (below); workspace
   snapshotted to artifact store.
6. **Evaluate** — evaluator sandbox scores each artifact vs rubric.
7. **Results** — matrix comparison, per-sandbox timeline replay, downloads.

## Termination semantics

Terminal states per sandbox:

| State | Meaning |
|---|---|
| `agent_done` | Agent invoked the injected `done` mechanism (skill/command) |
| `harness_complete` | Harness loop finished on its own |
| `timeout` | Wall-clock cap hit |
| `budget_exceeded` | Token/cost budget hit (enforced at gateway) |
| `stuck_loop` | Repeated identical actions / retry cap hit |
| `error` | Crash / unrecoverable failure / upstream provider outage |

`budget_exceeded` keys on the gateway's own budget-gate event
(`stage="budget"` + `reason="budget_exceeded"`) — a bare 429 from the
*upstream provider* is NOT budget exhaustion: ≥3 `stage="upstream"` error
events terminate the sandbox `error` instead. Three limits share the
verdict (PR28) — the terminate checkpoint's `payload.limit` says which:
`run_tokens` (shared ceiling, cred `max_tokens`), `sandbox_tokens`
(per-cell ceiling, cred `sandbox_max_tokens` metered against the cell's
own usage + monitor backstop), `cost` (`max_cost_usd` vs the cell's
estimated spend at the layered price table — priced models only).

**Budget resilience (F30):** caps are live-widenable, not one-shot.
`POST /api/sandboxes/{id}/extend_budget` appends a `budget_extended`
checkpoint; the gateway's `_budget_gate` and the monitor's poll sum those
rows into the cred's effective ceilings (token grants widen BOTH the run
and per-sandbox limits the cred passes through; cost grants widen
`max_cost_usd`) — a control-plane bump takes effect on the next
call/poll, no restart. When spend crosses ~80% of the effective cap,
`budget_warning` checkpoints emit once per (sandbox, limit kind) — the
gateway on the request path, the monitor poll as backstop — and surface
in the UI as a "near cap" badge with an extend affordance.
`budgets.auto_extend` (submit checkbox) makes the monitor grant +50%
once on the first warning (`via="auto"`).

Provisional defaults (confirm with user): 60 min wall clock, $2 spend cap,
stuck-loop = 3 consecutive identical tool calls.

Every sandbox gets a `done` mechanism baked in (a `done` CLI on PATH and/or a
done skill pack) that signals the orchestrator — `agent_done` is distinct from
`harness_complete`.

## Event model

`events`: `id, run_id, sandbox_id, ts, seq, source, type, payload JSONB`

- **sources**: `gateway`, `harness`, `sandbox`, `evaluator`, `orchestrator`
- **types**: `llm_request`, `llm_response` (tokens in/out, cache r/w, latency,
  ttft_ms/total_ms/tokens_per_sec speed fields, cost, model), `tool_call`,
  `tool_result` (hook-sourced tool outcomes),
  `shell_command`, `file_op`, `test_execution`, `checkpoint`,
  `skill_invocation`, `done_signal`, `error`, `run_started`,
  `terminal_state`, `context_compacted`, `miniloop_result` (authoritative
  list: `api/schemas.py::EventType` — a contract test AST-scans emitters)

Gateway events are captured passively from the request/response stream.
Agent-level events (tool calls, shell, file ops) are produced by the harness
adapter translating native output (e.g. Claude Code `--output-format
stream-json`, Aider logs) or harness hooks where available.

**Hook events (PR17):** harnesses with a native hook/plugin surface register
the shared receiver `python3 -m adapters.common.hooks <adapter>` — payload
JSON arrives on stdin (Claude Code `settings.json` hooks, pi's generated
`heg-events.ts` extension) or argv (Codex `notify`), the adapter's
`translate_hook` normalizes it, and events POST to `/internal/events` with
`via: "hook"` stamped into the payload. The receiver always exits 0 and
mirrors JSONL to stderr only (Claude injects SessionStart/UserPromptSubmit
hook stdout into agent context). Hooked today: `claude_code` (PostToolUse →
`tool_result`; session lifecycle → `checkpoint`), `codex`
(agent-turn-complete → `checkpoint` incl. `last-assistant-message`), `pi`
(tool_execution_end → `tool_result`). `deepseek` (no stable observer/plugin
API — cordis loader internals only) and `miniloop` (our own agent, already
first-class) stay on the translated path; new adapters opt in by adding
`translate_hook` + a `MANIFEST["hooks"]` line (contract-tested pairing).

## Harness adapter contract

Each adapter provides:

- `image_spec()` — Dockerfile/image definition for the sandbox
- `install_skillpacks(packs)` — how skill packs are provisioned
- `env(gateway_url, run_creds)` — env vars wiring the harness to the gateway
- `launch(task_prompt)` — start the agent loop (non-interactive mode)
- `translate_events(stream)` → normalized `Event[]`
- `completion_signal()` — how `done` / loop-completion is detected
- *(optional)* `translate_hook` in `adapters/<name>/translate.py` + a
  `MANIFEST["hooks"]` descriptor — wires the harness's native hook/plugin
  surface into `adapters.common.hooks` (see Event model § hook events)

**MVP adapter:** `miniloop` — our custom minimal coding agent (tool-call
loop: shell exec, file read/write, `done`). Shipped CLI adapters (F10 +
PR13): Claude Code, Codex, Pi, DeepSeek `dsh`, Aider — each runs against
the same model via the gateway (paid accounts only needed if pointing at
hosted providers; Ollama keeps everything free). Miniloop retries a
tool-less reply with a corrective `agent_nudge` message
(`MINILOOP_NUDGE_RETRIES`, default 2) before it counts toward the
text-only `stuck_loop` streak — small models answer in prose unless told
prose is invalid. Its per model-call timeout is
`MINILOOP_LLM_TIMEOUT_SECONDS` (default 960) and must exceed the
gateway's worst-case silent hold on one call — (1 +
`UPSTREAM_RETRY_ATTEMPTS`) × `UPSTREAM_TIMEOUT_SECONDS` + total retry
backoff (~915s under the shipped defaults) — an agent that gives up
first abandons a call the gateway is still paying for upstream.

**Pseudo-adapter (PR25):** `prompt` is selectable (manifest +
`selectable_adapter_names()`) but has NO adapter module — it is absent
from `adapter_names()` and `get_adapter("prompt")` raises a no-sandbox
`KeyError`. Matrix cells with `adapter == "prompt"` skip `spawn_one`
entirely and route to `orchestrator/prompt_cell.py::run_prompt_cell`,
which runs the ordinary sandbox row through a single
`{GATEWAY_URL}/v1/chat/completions` call on a sandbox-scoped cred (one
retry on upstream 5xx/timeout; 4xx fails immediately), writes
`output.txt` + `meta.json` + `transcript.log` into the standard
artifact dir, emits `run_started` + `terminal_state` +
`sandbox_finalized`, and finishes `done`/`harness_complete`. The run's
`wall_clock_seconds` bounds the cell like a container cap — a monotonic
deadline cancels an in-flight call and skips the remaining retry
(terminal `timeout`, reason `wall_clock`). Prompt and
container cells interleave in one run; evaluation receives
`prompt_output_path` so `output.txt` stages as the submission.
`system_prompt_extra` becomes a `system` message. Preview/thumbnail
endpoints 404 early for prompt cells. Budgets still apply: the cred
carries `max_tokens`/`sandbox_max_tokens`, and `_budget_cap_detail`
enforces the run ceiling, the per-cell token overshoot, and
`max_cost_usd` inline post-call (the monitor's poll never sees a
no-sandbox cell).

**Default skill packs** (named presets, vendored under `skillpacks/`):
`superpowers`, `mattpocock-skills`, `spec-kit`.

**Providers:** Ollama default (local, free); OpenRouter + direct
Anthropic/OpenAI landed (env-switched, `PROVIDER_ROUTES` per-model).

## Rubric

MVP dimensions: functional requirements, acceptance criteria, code
implementation quality, cyclomatic complexity. Since PR23 (F26) those four
are only the **code-task default** — `RubricContent.dimensions` accepts
1–12 arbitrary dimensions (names soft-normalize to snake_case + a small
canonical-alias map; rejected only on names normalizing to empty or past
128 chars, reserved score-namespace names, or duplicates after
normalization), so non-coding rubrics (extraction accuracy, tone,
compliance,
…) generate, freeze, score, and render with no code axes at all.
Generation can be steered: `RunCreate.rubric_hint` (fallback: preset
category) feeds a domain line into the gen prompt, and a **rubric
template** (`RunCreate.rubric_template` — vendored JSON seeds under
`rubric_templates/` + user `rubric_templates` rows, `GET
/api/rubric-templates`) replaces the freeform task text as the generation
basis; a preset's `suggested_rubric` (`task_presets.suggested_rubric`
JSONB / vendored frontmatter) fills in when no explicit template is sent.
Collectors still report every measurable signal; the judge brief names
only the dimensions the rubric carries and marks non-applicable dims
(e.g. `cyclomatic_complexity` on a non-code artifact) mid-scale with a
"not applicable" rationale rather than dropping the row.

Since PR6/PR7 the judge
scores from **measured** signals where available — the `scoring` package's
static-analysis collector (lizard, parse-only) computes real cyclomatic
complexity/size/language mix, and the `exec` collector runs the
submission's build/tests/coverage inside a locked `heg-exec` container
(`--network none`, nobody user, resource caps, read-only artifact mount);
the report is staged as `ANALYSIS.md`/`analysis.json` for the judge,
persisted at `<artifact>/analysis.json`, and surfaced on the results API
(flat `exec` summary included). Disable exec via `SCORING_EXEC=0`;
requires the `heg-exec:latest` image (`docker build -f
sandbox/images/exec.Dockerfile -t heg-exec:latest .`).

PR8 adds **generated-test scoring** (`evaluator/testgen.py`): before
staging, the evaluator calls the model through the gateway (metered on the
per-eval cred — generation consumes the same `EVAL_TOKEN_BUDGET` allowance
as the judge) with the rubric + bounded source listing → parses the output
into `test_*.py` files (deterministic containment: each chunk must
byte-compile AND define a `test_*` — anything else is dropped and a fully
unusable generation degrades to `skipped`) → persists the suite to
`<artifact>/generated_tests/` + `_manifest.json` (provenance, never the
candidate's `workspace/`) → executes it via
`ExecRunner.execute(workspace, test_target=…)` — the suite mounts
read-only at `/srv/test_target` inside `heg-exec`, copied to a writable
dir, pytest pointed at it; coverage stays `--source=/work` (the candidate
code) and the build phase never compiles generated files. Pass rate,
counts, failing names, and coverage land in the `generated_tests`
`SignalReport` — same `analysis.json` envelope, `ANALYSIS.md` section,
`eval_analysis`/`test_execution` (`phase=eval_testgen`) events, flat
`generated_tests` summary on the results API, and a `generated_tests/`
copy staged for the judge. Flags: `SCORING_TESTGEN=0` disables (conftest
pins it); `TESTGEN_MODEL`, `TESTGEN_SOURCE_BUDGET_BYTES` (24k),
`TESTGEN_MAX_FILES` (8), `TESTGEN_MAX_TOKENS` (4096),
`TESTGEN_TIMEOUT_SECONDS` (960). `EXEC_TEST_TARGET` is the
ExecRunner→runner env contract for the suite mount.

PR26 (F25) adds a fourth measured signal, `suite`: runs fanned out from a
vendored **benchmark suite** (`benchmark_suites/<slug>/suite.yaml` — tasks
are preset refs or inline prompts, each optionally pinned to an adapter)
get their declared **deterministic checkers** executed post-artifact and
pre-judge by `scoring/checkers.py`. Output checkers (`exact`, `contains`,
`regex`, `json_schema`, `numeric_tol`, `output_grep`) read bounded,
jailed files under the artifact (prompt cells: `output.txt` at the
artifact root); workspace checkers (`file_exists`, `grep`) resolve
workspace-first; `py_test` runs the suite's vendored tests against the
candidate workspace inside the locked `heg-exec` container via
`ExecRunner` — candidate test code never executes on the host. Each
outcome persists as an append-only `checker_results` row
(pass/fail/skipped/error + detail — `skipped` = infra absent, `error` =
checker misconfig/crash), plus `suite_checks.json` in the artifact, a
`suite_checks` checkpoint/`test_execution` event, the `suite` signal in
`analysis.json`/`ANALYSIS.md`, and a `suite_pass` Score row (pass rate
×10 — the one deliberate fold into the score table). Results surfaces a
flat `suite` summary + `checker_results` per sandbox (newest-wins per
check spec); `GET /api/suite-runs/{id}` aggregates a batch task × cell
with `judge_overall` beside checker roll-ups. Rescores re-read the
current suite.yaml; checker outcomes stay complementary to — never
merged into — judge rubric dimensions.

Flow: auto-generated from task spec → HITL review/edit in UI → frozen →
applied identically to all sandboxes. Generation is fault-tolerant —
`RUBRIC_GEN_RETRIES` (default 2) covers transient upstream errors and
validation failures (the error is fed back into the retry prompt), and
lenient normalization coerces scalar `generated_by` + numeric-string
score/weight fields before validation; structural problems still fail.

## Stack

- **Backend**: Python 3.12, FastAPI, asyncio; SQLAlchemy + Alembic
- **Frontend**: Next.js (TypeScript), SSE for live timeline
- **DB**: Postgres 16 (docker-compose)
- **Inference**: Ollama (docker-compose or host), small models for testing
- **Sandboxes**: Docker, per-harness image, host docker socket for spawning
- **Evaluator**: sandboxed agent (same infra)
- **Artifacts**: local disk `./data/artifacts/`
- **Auth**: none — local single-user tool for MVP

## Repo layout (proposed)

```
gateway/       # LLM proxy: providers, metering, budgets, event emission,
               # opt-in response cache (PR18 replay)
orchestrator/  # run state machine, matrix expansion, rubric gen, lifecycle
api/           # control-plane HTTP API (FastAPI app)
adapters/      # harness adapters (miniloop + shipped CLI adapters)
sandbox/       # image defs, workspace prep, done mechanism, event collector
evaluator/     # rubric scoring agent
scoring/       # objective signal collectors (static analysis + exec/coverage)
skillpacks/    # vendored default packs: superpowers, mattpocock-skills, spec-kit
skillpack_import/  # F11 GitHub import: codeload fetch, SKILL.md scan, pack store
task_presets/  # vendored task presets (<slug>.md — frontmatter + prompt body)
api/presets.py # preset store: vendored discovery + merged vendored/user resolve
benchmark_suites/ # vendored benchmark suites (<slug>/suite.yaml + tests/)
api/suites.py  # suite store: yaml parse, slug-safe resolve, jailed check paths
scoring/checkers.py # deterministic suite checkers (output + workspace + py_test)
web/           # Next.js app
db/            # schema + Alembic migrations
tests/         # unit / integration / e2e
data/          # artifacts, imported skill packs (skillpacks/), sandbox state,
               # task-input uploads (uploads/) + materialized seeds (seeds/)
               # — all gitignored
docs/
docker-compose.yml
```

## MVP feature map

Feature IDs, code locations, and verification steps are maintained in
**[FEATURE_MAP.md](./FEATURE_MAP.md)** — update it whenever a feature's paths
or tests change.

| # | Feature |
|---|---------|
| F0 | Dev environment & scaffold |
| F1 | Submission form → run spec |
| F2 | Orchestrator: matrix, rubric gen, lifecycle |
| F3 | AI gateway: OpenAI-compatible proxy → upstream providers (Ollama default), telemetry, budgets |
| F4 | Sandbox runtime: spawn, egress lockdown, snapshots, done mechanism |
| F5 | Miniloop harness + adapter + default skill packs |
| F6 | Event store + live timeline (SSE) + replay |
| F7 | Evaluator sandbox: rubric scoring + rationale |
| F8 | Results UI: matrix, scores, timeline, artifacts |
| F9 | Run analytics: per-sandbox metrics + estimated cost |
| F10 | Multi-harness adapters (Codex, Claude Code, Pi, DeepSeek, Aider) |
| F11 | Skillpack import from GitHub (discover → select → import) |
| F12 | Artifact preview (static html + desktop GUI via noVNC + python-server web apps) |
| F13 | Graphical analysis page + cross-run trends (recharts) |
| F14 | Eval iteration: re-score runs + versioned rubric edits |
| F15 | Submit UX: live model picker + install, named harness configs |
| F16 | Model-vs-model matrix: multi-model runs + per-model analytics |
| F17 | Artifact thumbnails (headless screenshots via the preview stack) |
| F18 | Prompt-only matrix cells (no-sandbox `prompt` adapter) |
| F19 | Artifact file browser + side-by-side diff |
| F20 | Trials per cell + per-config model options |
| F21 | Budgets & pricing accuracy (`model_prices` glob rate table) |
| F22 | Quality × cost/speed scatter + deterministic verdict card |
| F23 | Task inputs (seeded workspace: repo_url or uploaded archive) |
| F24 | Task preset library (vendored + user presets, preset_ref provenance) |
| F25 | Benchmark suites + deterministic checkers (suite.yaml fan-out, objective checkers beside judge scores) |
| F31 | Timeline compare-mode clock (absolute|relative toggle, per-cell t+0 re-basing) |
| F27 | Cross-run compare + leaderboards (global model/config-family rankings, run-vs-run diff) |
| F28 | Run export + share links (`/export?fmt=json|csv` bundle; `/share/{token}` read-only view) |
| F29 | Dashboard home (/dashboard summary aggregate — KPIs, weekly spend/runs, queue lane, top-5 boards) |

## Post-MVP roadmap

- Additional harness adapters beyond the shipped six (miniloop, codex,
  claude_code, pi, deepseek, aider)
- Anthropic-compatible gateway endpoint (`/v1/messages`)
- ~~Model-vs-model comparisons (multi-model matrices)~~ — **landed as F16**
- ~~Objective scoring: build/test execution, coverage~~ — **landed PR6
  (static analysis) + PR7 (exec+coverage)** via `scoring/` signal
  collectors
- ~~Automated test-suite generation as eval target~~ — **landed PR8**:
  `generated_tests` signal (LLM-written suite, exec via `test_target`,
  pass rate into the judge brief)
- ~~Benchmark suites + deterministic checkers~~ — **landed PR26 (F25)**:
  vendored `suite.yaml` fan-out + objective `checker_results` beside
  judge scores (see Rubric §)
- Higher-fidelity instrumentation via harness hooks/plugins
- Gateway response caching / deterministic replay
- Multi-host / K8s sandbox scaling
- Auth, saved run templates, share links — **share links landed as
  PR35/F28** (`runs.share_token` + read-only `/share/{token}`); auth +
  templates remain PR19 territory
- ~~Dynamic model picker~~ + ~~named harness configs~~ — **landed as F15**
- ~~Cross-run analytics & cost dashboards~~ — **landed (F13 trends +
  PR36/F29 `/dashboard`)**: weekly spend/runs, terminal mix, queue lane,
  top-5 boards, recent runs
- Timeline-side analytics — **landed (PR9)**: per-sandbox metrics strip on
  `web/app/runs/[id]/timeline/` (turns/tokens/latency/speed/cost/errors
  chips + judge line; chips double as sandbox-filter selectors; 15s
  refresh while non-terminal). Leftover idea: on-event-hover cumulative
  stats.
- Split-view trace comparison — **landed (PR10)**: `merged | split` toggle
  on `web/app/runs/[id]/timeline/`. Split renders two sandbox lanes on ONE
  merged `(ts,id)` ordering inside a single scroll container — synchronized
  scroll + time alignment are structural (no scroll mirroring); sticky lane
  pickers (labels/status/count, swap-on-collision, `?view=split&left=&right=`
  seeds), run-level + non-lane-sandbox events as full-width shared rows,
  PR9 metrics chips assign lanes in split mode. Transform + tests:
  `web/lib/splitTimeline.ts(.test.ts)`; shared row markup:
  `timeline/EventRow.tsx`.
- ~~Lightweight artifact thumbnails~~ — **landed as PR4/F17**: results-page
  `📷 snapshot` POSTs a bounded headless-chromium capture through the
  artifact's own preview stack; `<artifact>/thumbnail.png` heads the
  preview cell and clicks through to the full live preview.
- ~~Desktop/GUI artifact previews — noVNC-style streaming for pygame etc.~~
  — landed (F12; extended to pyglet/arcade/tkinter + python-server in PR3)
- "Add next prompt" — continue a finished sandbox with a follow-up prompt
  on the same workspace (multi-turn runs: new phase in the event stream +
  artifact re-snapshotted; resume-in-place vs respawn-from-snapshot TBD)
- ~~More desktop preview types~~ + ~~python-server previews~~ — **landed
  as PR3/F12 extension**: desktop covers pygame/pyglet/arcade/tkinter
  (deps baked into `heg-preview-desktop`, incl. a wheels stage for
  pymunk's missing aarch64 wheel); `python-server` executes web apps via
  `heg-preview-server` + `server-preview-runner.py` (detects the app's
  listen socket in `/proc/net/tcp`, relays :8000 → actual port; `manage.py`
  gets the django runserver contract).
- dsh native helpers (spawn-helper, node-pty) — npm ≥11 blocks their
  postinstall scripts; CLI works without them, but revisit `--allow-scripts`
  for the dsh package set if evals hit TTY-dependent tasks

## Decisions log

- ~~Compare **harnesses, not models**~~ — **reversed by PR14 (F16)**: the
  matrix is `configs × models`. `model_name` remains the default participant
  model; `RunCreate.models` selects the full set (defaults to `[model_name]`)
  and a per-config `model` overrides the run list for that config. Same-model
  runs remain the default; `same_model_eval` now checks the evaluator against
  *every* participant model.
- Custom lightweight gateway (not LiteLLM) — observability is the core value.
- Two-layer telemetry: passive gateway capture + adapter-translated harness
  events for agent-level detail.
- HITL rubric approval; frozen pre-launch; shared across sandboxes.
- Evaluator in own sandbox; UI flags evaluator == any participant model.
- LLM-judge scoring, fed by measured signals: PR6 added parse-only static
  analysis (real cyclomatic complexity + static metrics via lizard);
  PR7 added exec-derived checks — build/test/coverage measured in a
  locked, zero-egress `heg-exec` container (read-only artifact, nobody
  user, resource-capped) — as judge evidence + a `test_execution` event.
- Artifacts local; metadata in Postgres.
- Evaluator model is user-selected at submission (independence is opt-in).
- **Ollama is the default inference** for MVP — small local models; cost ≈ 0
  but still metered uniformly.
- **Miniloop-first**: our custom harness is the initial adapter; MVP
  comparisons vary harness config (skill packs, prompts), not CLIs.
- Default skill packs shipped as named presets: superpowers,
  mattpocock-skills, spec-kit.
- Termination caps confirmed: 60 min wall clock, $2 spend cap, stuck-loop =
  3 consecutive identical tool calls.
- No auth — local single-user tool; docker-compose topology; sandboxes
  spawned via host Docker socket.
- **Phase 2.5 — pluggable CLI-harness adapters**: `adapters/common` shim
  (stdlib-only) writes each CLI's native config, launches it in JSONL mode,
  translates records → normalized events, prints `HEG_RESULT`. Registry in
  `orchestrator/adapters.py` covers miniloop, codex, claude_code, pi,
  deepseek, aider; `GET /api/harnesses` serves manifests to the submit
  form. Aider has no JSONL mode — the shim's `translate_text` hook
  (PR13) maps its plain-text status lines heuristically instead.
- **Gateway protocol fan-out**: `/v1/messages` (+`count_tokens`) for
  Anthropic-SDK CLIs, `/v1/responses` for Codex, `/v1/models` for probes —
  pass-throughs that preserve `stream` with a metering tee
  (`usage_from_sse`). `/v1/chat/completions` now honors `stream: true` the
  same way (pi streams unconditionally; the old force-`stream:false`
  returned a bare JSON body its SSE reader dropped as an empty response).
  Every metered call leaves a terminal event: a client that disconnects
  mid-stream still gets `llm_response` (partial capture or
  `usage_estimated`) plus an `error` `stage="client_disconnected"`, and
  one that disconnects while the gateway awaits upstream headers/retries
  gets the same `client_disconnected` error — all emitted under a
  shielded anyio cancel scope, since Starlette delivers the disconnect by
  cancelling the stream task and unshielded emit awaits would re-raise
  `Cancelled` (live-observed: a dsh lane left `llm_request` + zero
  `llm_response`). Upstream timeouts are `UPSTREAM_TIMEOUT_SECONDS`
  (default 300) on the gateway side.
- **CLI image pattern**: node:slim base + `python3` for the shim + pinned
  npm CLI + `adapters/` under `/opt/harness` + `done` at `/usr/local/bin`.
  npm install happens before COPY so dependency layers cache. DeepSeek's
  `dsh` needs node >=24.9 → `node:24-slim` (others stay on 22). Aider is a
  Python CLI → `python:3.12-slim` + `pip install aider-chat` (PR13).
- **PR15 pacing is DB-driven, not in-memory**: `queued` is a run status, so
  the submission queue survives api restarts (startup sweep re-promotes).
  Queue order = `started_at` (approval time). Spawn admission is a bounded
  wait *inside* `spawn_one` (per-sandbox failure on timeout) rather than a
  scheduler loop — sequential provisioning already makes spawns the unit of
  queuing. `run.status` stays `String` — no migration needed for `queued`.
- **Rescoring holds the same single slot as a run**: `rescore_run` parks on
  `_await_rescore_slot` (emitting `rescore_queued`) while any run is
  active/queued, and `runner.start_rescore_task` wraps the pass in the same
  promote-next-queued `finally` a lifecycle gets — an eval can never overlap
  another run, and a crashed rescore still frees the lane.
- **Cancel/delete**: `cancelled` is a terminal run status —
  `DELETE /api/runs/{id}` cancels `submitted`/`rubric_review`/`queued`
  runs (status flip + `cancelled` checkpoint + `finished_at`; a pending
  rubric-draft task is cancelled, and a freed `queued` position promotes
  the next parked run via `runner.release_run_slot`). Terminal runs
  (`done`/`failed`/`cancelled`) hard-delete instead — bulk row removal
  for scores/artifacts/events/rubrics/sandboxes then the run, plus
  best-effort `data/artifacts/<run_id>/` removal (the append-only-events
  rule is explicitly waived for junk-run removal). In-flight runs
  (`provisioning`/`running`/`evaluating`) 409 — sandbox teardown is out
  of scope. Lifecycle/provisioning bail on a vanished or `cancelled` run
  mid-flight. A cancelled run can never be rescored — nothing executed.
- **Submit validation (`api/modelcheck.py`)**: `POST /api/runs` strips +
  rejects blank task prompts/models (422), then resolves each selected
  model against the provider catalog the gateway would route it to (60s
  per-model cache). Absent models land as advisory `warnings` on the 201
  (the name may be pullable); a catalog fetch failure skips validation
  entirely (offline tolerance). The one hard 422 is a provider-reported
  `tools`-capability mismatch for a tool-call harness — checked for
  participant AND evaluator models (the evaluator drives miniloop tool
  calls), incl. per-config model pins.
- **Workspace ZIP downloads are filtered**: `GET /api/artifacts/{id}/
  download` on a directory drops `.git`, `__pycache__`, `*.pyc`,
  `node_modules`, `.venv` at any depth (symlinks were already skipped);
  the on-disk artifact is untouched.
- **`done` invocation**: bare `done` is a shell keyword in sh/bash — prompts
  instruct harnesses to run `/usr/local/bin/done` (or `command done`).
- **PR17 — hook/plugin telemetry lane**: `adapters/common/hooks.py` is the
  shared receiver (`python3 -m adapters.common.hooks <adapter> [json]`) for
  native hooks — claude_code `settings.json` hooks (stdin), codex `notify`
  (argv), pi's generated extension forwarder (stdin). Normalization stays in
  Python via each adapter's `translate_hook` (contract-tested); events carry
  `via: "hook"`. New `tool_result` EventType reports tool *outcomes* — the
  detail translated streams drop to avoid double-counting tool calls. Hook
  choice per CLI follows documented surfaces only (dsh/miniloop untranslated
  paths unchanged).
- **Imported skill packs (F11) live under `data/skillpacks/`**, not the
  vendored `skillpacks/` tree — vendored packs are committed and baked into
  the api image (ephemeral FS in compose), while `data/` is gitignored and
  bind-mounted at `/data`. Imported + vendored share ONE name namespace so
  `skill_packs` values resolve unambiguously across both roots; staging
  searches vendored first. Import `mode`s: `pack` = one new pack (taken name
  → 409 + `detail.suggested_name` — the UI's confirm-and-rename flow; never
  silently merge), `merge` = append into an existing imported pack (vendored
  targets rejected), `split` = one pack per skill, auto-renamed
  `x-2`/`x-3` on collision.
- **GitHub import fetches codeload tarballs** (one request per repo — no
  `api.github.com` rate-limit dependency); `/tree/<branch>/<path>` refs are
  ambiguous (branch names may contain slashes) so candidates probe
  longest-first. `commit_sha` resolves best-effort via `git/ref`/​`commits`
  for provenance + import pinning. `GITHUB_TOKEN` (env) is sent as a Bearer
  token when set — private repos work best-effort; proper token UX is a
  follow-up.
- **Artifact preview (F12) ships static + desktop + python-server** —
  `PreviewPlan.type` + `PreviewOut.type` carry `"static"` (serve files),
  `"desktop"` (execute GUI under Xvnc/noVNC), `"python-server"` (execute
  a web app, published on localhost). `PREVIEW_TYPES` is the single
  validation set; `PreviewManager.spawn` dispatches per type.
- Preview containers get their **own dedicated bridge `heg-preview`**, not
  `heg-sandbox` and not `--internal` — Docker silently drops published ports
  on internal networks (verified), so "isolated + host-reachable" is
  impossible; port binds `127.0.0.1` only, masquerade disabled (Linux),
  container hardened (nobody/read-only/cap-drop/no-new-priv).
- **Executed-code previews isolate the runner, not the serving hop**:
  desktop + python-server runners share `--internal` `heg-desktop` (zero
  egress — api-side proxying was rejected: the api would join the
  runner's net, giving candidate code a path to the control plane) with
  `--cpus`/`--memory`/`--pids-limit` bounds (`PREVIEW_DESKTOP_*`). A
  pinned `alpine/socat` sidecar (`heg.preview_role=proxy`, dumb TCP pipe —
  nothing for the artifact to exploit) straddles both nets and publishes
  the runner's port on localhost — `:6080` websockify/noVNC for desktop,
  `:8000` for python-server (the runner's `server-preview-runner.py`
  bridges whatever port the app binds). GUI stacks (pygame-ce, pyglet,
  arcade, tkinter) and web frameworks (flask/fastapi/django/aiohttp/
  bottle/tornado/gunicorn) are baked into the images — preview nets have
  zero egress so deps can NEVER pip-install at spawn.
- **Detection produces a typed candidate list, not a hidden pick** —
  `scan_preview` returns every runnable entry (`.html` → static, evidenced
  `.py` → desktop GUI or python-server) ordered default-first;
  `plan`/`candidates[0]` is only the dropdown's default. Executed `.py`
  candidates share `_py_candidates`: file imports a stack (pygame/
  pyglet/arcade/tkinter → desktop; flask/fastapi/django/http.server/… →
  python-server — line-anchored, comments can't match), OR entry-shaped +
  a non-comment requirements.txt line naming it (bare `tk` in
  requirements is NOT tkinter evidence — unrelated package, tkinter is
  stdlib). Scanned at root + ≤3 nested levels with hidden/
  `_DESKTOP_SKIP_DIRS`/`templates`/symlinks skipped. `entry=` POST picks
  any workspace-relative file — `.py` bypasses evidence (the pick IS the
  disambiguation; `manage.py` alone maps to python-server — only it
  publishes the app port). Zero candidates → UI shows disabled "not
  supported". Deps beyond the baked stacks can't install (no egress) —
  the app fails at import and the runner exits visibly. Static defaults
  when both match. `type=` POST
  overrides the pick: alone it selects the first detected candidate of
  that type (a both-matching workspace defaults to static —
  `{type:"desktop"}` reaches the game); with `entry=` it forces the plan —
  `static` serves any jailed file (the entry is only the URL opened —
  view a `.py`'s source instead of executing it), `desktop`/
  `python-server` still require a `.py` entry. A live preview is returned
  as-is unless an
  explicit different plan is requested, then it's stopped and respawned.
  The results-GET probe is memoized by `sandbox.preview.ScanCache`
  (`PREVIEW_SCAN_TTL_SECONDS`, default 30s) keyed on
  (jail, run, sandbox, artifact_path) — workspaces only change on
  re-snapshot.
- **Gateway response caching / deterministic replay (PR18)**: opt-in
  `GATEWAY_CACHE_ENABLED` — OFF by default so real runs hit the live
  upstream. Metered generation calls replay from a sqlite store
  (`gateway/cache.py`, `GATEWAY_CACHE_PATH`) keyed on
  `namespace + path + provider + outgoing-body` (post-transform, so
  stream/buffered variants and PROVIDER_ROUTES targets can't collide;
  bump `GATEWAY_CACHE_NAMESPACE` to invalidate). Semantics: hits still emit
  `llm_request`/`llm_response` with `payload.cached: true`, usage is
  re-metered from the stored body so cached spend counts toward the run
  budget, and the budget gate runs before the lookup — a replayed run
  accrues tokens and 429s identically to the live one. Only complete 2xx
  bodies are stored; unmetered probes (`count_tokens`, `/v1/models`) bypass
  the cache; cache failures degrade to a miss, never a proxy 5xx (same
  doctrine as event emission).
- **Preview registry lives on docker labels**, not in memory or Postgres —
  restart-safe discovery + the TTL reaper (default 15 min) works
  out-of-process; runner+proxy share `heg.preview_id`, stop removes all.
- **Eval judge retries** — a judge that produces zero Score rows
  (missing/invalid `scores.json`) is re-spawned up to `EVAL_SCORE_RETRIES`
  (default 1) with a fresh cred; `eval_retry` checkpoints make it visible in
  the timeline. Keyed on persisted Score rows, not the evaluator's return —
  infra exceptions are deterministic and not retried. Motivation: small local
  judge models flake ~1-in-3 evals (observed modes: flat dimension-map JSON,
  paged-read stuck loops, grep-loop wander, unescaped quotes in rationale) —
  `EVAL_PROMPT.md` carries targeted counter-hints (paging, no inner `"`),
  and retries additionally pass the last `eval_scores_failed` detail as
  `prior_error` so the respawned judge sees *why* its output was rejected
  (queried from the events table per attempt — blind respawns flake
  identically). Two more output-side safety nets sit under the retry loop:
  `parse_scores_json` repairs single-quoted Python-literal dicts via
  `ast.literal_eval`, and a direct-completion fallback
  (`evaluator/judge_direct.py` — one bounded control-plane chat call on the
  eval cred with `response_format=json_object`, retrying once without the
  field on a 4xx) runs before an attempt is abandoned; its scores write
  back over `scores.json` and land via the normal row-insert path with
  `eval_scored` `mode="direct_fallback"`, while a fallback failure adds
  `fallback_detail` to the `eval_scores_failed` event. When all attempts produce no Score rows the orchestrator emits a final
  `error` event (`stage="evaluation"`, `phase="eval_failed"`) for the
  sandbox; the results API surfaces it as per-sandbox `eval_error` so the UI
  shows "eval failed" rather than an empty score matrix — a sandbox can be
  `done` with failed eval, so check `eval_error`, not score-null alone.
- One live preview per sandbox (POST is idempotent — re-clicking returns the
  existing preview); dead containers are cleared on respawn.
- **Artifact thumbnails (F17) reuse the preview stack, not a parallel one** —
  capture spawns the artifact's own `PreviewManager` topology with
  `heg.preview_purpose=thumb` (interactive endpoints filter on `preview`, so
  a mid-flight capture never surfaces as the live preview; the reaper/stop
  see both), then a one-shot `heg-preview-thumb` container (debian chromium
  on `python:3.12-slim`, nobody/read-only/tmpfs) joins `heg-preview` and
  screenshots the in-network URL — `<runner>:8000` for static,
  `<runner>-proxy:{8000|6080}` for the executed types (Xvnc is
  loopback-only, so noVNC is desktop's only window → bigger settle +
  virtual-time budget). Trigger is explicit-only (`POST .../thumbnail`,
  bounded ~2min; results GET never spawns — it only jailed-probes
  `thumbnail.png` + the `thumbnail.json` meta sidecar). Failures → 502 and
  "no thumbnail", never a broken results read.
- **Charts: recharts** (F13) — picked over visx/chart.js: smallest fit for
  grouped/stacked bars + a composed bar-line trend under React 19 / Next 16;
  themed via hexes mirroring globals.css tokens (no CSS-var reads). The
  cross-run trends endpoint (`GET /api/analytics/runs`) prices each point's
  cells at their own model's rate (unlisted → unpriced, null cost)
  unless `?price_model=` forces uniform
  repricing — same "estimate, not billing" framing as F9.
- **Rubric versions are immutable** (F14): post-run edits INSERT a new
  `rubrics` row (`parent_id` → edited version, `version = max+1`,
  previous approved → `superseded`); the frozen row is never mutated.
  `Score.rubric_id` + denormalized `Score.rubric_version` keep per-pass
  provenance so old/new score sets stay distinguishable — re-score only
  ever adds Score rows, never rewrites them.
- **Re-score is a dedicated path, not a lifecycle replay**: terminal runs
  (`done|failed`) transition to `evaluating` and back via
  `orchestrator.lifecycle.rescore_run` over stored artifact snapshots —
  participant sandboxes are never respawned. Registered as the `rescore`
  task kind so a concurrent trigger 409s; the startup sweep rescues an
  interrupted re-score (marked by a `rescore_requested`/`rescore_started`
  checkpoint) to `done` when ≥1 Score row exists — a first-pass eval with
  no marker still fails.
- **Model catalog/install ride the gateway, not Ollama direct (F15)** — the
  api mints a sandbox-null `heg.*` service cred to call `GET /v1/models`,
  which keeps the
  provider abstraction (Ollama today, OpenRouter next) behind the gateway;
  the gateway stamps `X-HEG-Provider` so the UI only offers install when the
  upstream can `ollama pull` (`POST /v1/models/pull` → `/api/pull`, 501
  otherwise). Pull is **control-plane only**: creds carrying a `sandbox_id`
  (every participant/evaluator cred, and `heg-dev`) get 403 — only
  run-scoped service creds (`sandbox_id=None`) may pull; the body is
  allowlisted to `name`/`model` (+ forced `stream`, `insecure` dropped) and
  the upstream client is pinned to Ollama (`get_ollama_client`) so pull can
  never follow per-model routing. Pull progress streams end-to-end as
  NDJSON — never buffered.
- **Sandbox names are persisted, not derived (F15)** — `HarnessConfigIn.name`
  (≤64, whitespace-normalized) lands on `sandboxes.name`; `spawn_one` fills
  blanks with `<adapter>-<shortid>` once the UUID exists. All UI surfaces
  read the column — no per-component re-derivation to drift.
- **Unlisted models are unpriced, not fallback-priced (PR21)** —
  `resolve_price_model` returns `None` for models absent from
  `PRICE_TABLE`; analytics emit `price_model`/`estimated_cost_usd`/
  `evaluator_cost_usd` = null (never the first table entry's rate) and the
  UI renders "unpriced". Explicit `?price_model=` still reprices uniformly;
  unknown keys stay 422. Same audit batch: run-detail controls mirror
  DELETE semantics (cancel only `submitted`/`rubric_review`/`queued`;
  terminal runs hard-delete), the runs list paginates via `?offset`
  ("Load more"), landing copy describes the config × model matrix, and the
  results `total` row is the weighted scale-normalised aggregate
  (`web/lib/scoreMath.ts`), not a raw Σ.
- **Price truth lives in the DB, static table is the fallback (PR28)** —
  `model_prices` rows keyed by case-insensitive fnmatch `model_glob`
  (`created_at, id` declaration order, first match wins) layer OVER
  `api.pricing.PRICE_TABLE` via `PriceBook`; a bare (no `/`) glob never
  matches `vendor/model` ids. `PUT /api/prices` upserts user rows
  (never clobbered by sync) — `local` marks free inference, so PUT 422s
  it beside any nonzero rate rather than billing under a "local — $0"
  label; `POST /api/prices/sync` maps OpenRouter
  `/models` `pricing.*` USD-per-token strings to per-Mtok rates through
  the gateway's `x-heg-provider` override — entries with no parseable
  rate are skipped rather than priced-free. Per-cell budgets:
  `sandbox_token_budget` mints a `sandbox_max_tokens` cred claim
  (gateway gate + monitor backstop → `limit="sandbox_tokens"`;
  `prompt_cell._budget_cap_detail` gives the same overshoot verdict
  inline post-call for no-sandbox cells, run ceiling
  `token_budget` → `limit="run_tokens"` included);
  `max_cost_usd` is monitor-polled for containers and checked inline
  post-call for prompt cells (`prompt_cell._budget_cap_detail`) against
  the cell's layered-price estimate → `limit="cost"`, and it can't
  trip an unpriced model (no honest rate to bill against). Cache-read tokens bill at
  `cache_read_per_mtok_usd` when declared, else the input rate.
  Price pickers (results + analysis) grow rates inline via "+ add rate".
- **Config × model heatmap + winner card (PR31)** — the results page's
  score card has a `matrix | grid` toggle; the grid
  (`web/app/runs/[id]/results/GridView.tsx` + pure transform
  `web/lib/gridMatrix.ts`, `node --test`-covered) folds sandboxes into
  config rows × model cols. Row identity = `harness_config` minus
  per-cell `model`/trial keys — the sandbox `name` is unique per sandbox
  and must NOT be the group key. Cells show mean judge `overall` ±sd
  (same-(config, model) repeats are the cell's trials — PR27 needs no
  special-casing) on a fixed 0–10 red→amber→green scale; the `w` sub-line
  is the PR21 weighted total (same `specMap`/`dimensionKeys` the totals
  row uses — no second weighting). The winner card ranks cells by mean
  weighted total only (a judge `overall` alone can't win), with runner-up
  Δ, est. cost/wall clock (F9 analytics — "unpriced"/"—", never $0) and
  mean pass-rate (`generated_tests.pass_rate` → `exec` tests).
- **Artifact file browsing is rooted per-sandbox, jailed like the download
  (PR34/F19)** — `GET /api/sandboxes/{id}/files[/{path}]` reuses
  `sandbox/preview.py`'s `resolve_workspace_dir`/`resolve_artifact_dir`
  rather than `Artifact` rows: `workspace/` wins when the snapshot exists,
  else the artifact dir itself browses (prompt cells surface `output.txt`/
  `meta.json`/`transcript.log`; `FileListOut.root` reports which root
  answered). Listings are depth(8)/entry(5000)-bounded with the workspace
  ZIP's exclusions (`.git`/`node_modules`/`__pycache__`/`.venv`/`*.pyc`)
  and never follow symlinks; text reads cap at 256 KiB and NUL-carrying
  content returns `binary: true` + the `?download=1` FileResponse. `?diff=
  <other>` is `difflib` server-side (a/ + b/ labels, 1 MiB per side, 10k
  lines out, missing-side → diff vs empty) — the UI renders the unified
  format in a `<pre>` classified by `web/lib/fileBrowser.ts`, whose
  `mergeFileLists` reuses the PR10 merged-ordering split idiom for the
  two-lane file list (no diff dependency added). Traversal/absolute-path/
  sibling-workspace escapes 404 identically to the artifact download.
- **PR27 — trials + per-config model options**: `RunCreate.trials` (1–8,
  default 1) replicates every effective config × model cell N times;
  expansion stays model-major then cell-major so a cell's `t1..tN` replicas
  stay adjacent, each stamped `harness_config.trial_index` (the key is
  ABSENT at trials=1 — single-shot runs stay bit-identical to pre-PR27).
  `trial_index` lives inside the existing `harness_config` JSONB — no new
  sandbox column; `runs.trials` is a real int column (migration
  `d7e9f1a3b5c7`, the column wins over `config["trials"]` on old specs).
  Sandbox names append ` · tN` after the model suffix
  (`cfg · model · t3`), truncating the label side to fit String(255).
  `harness_configs[].model_options` (`temperature`/`seed`/`max_tokens`/
  `reasoning`, `extra="forbid"`) is filtered at submit against the
  adapter's `MANIFEST["model_options"]` — unsupported keys are dropped
  from the persisted cell and reported as advisory `warnings` (never a
  422); miniloop + `prompt` honor all four, codex takes `reasoning` only
  (→ `model_reasoning_effort`), other CLIs none. Miniloop maps them to
  `MINILOOP_TEMPERATURE`/`MINILOOP_SEED`/`MAX_TOKENS`/
  `MINILOOP_REASONING_EFFORT` env knobs → request-body fields.
  Results/analytics group trials by `cell_key` (harness_config minus
  `trial_index`, serialized sorted): the results matrix shows one column
  per cell (`mean ± sd (n=N)`, sample sd n−1) with expandable per-trial
  rows; analytics adds a `cells` rollup (`overall_mean`/`overall_sd`/
  `overall_n` counting only scored trials) plus per-sandbox `cell`/
  `trial_index`, and a by-cell view in the results analytics table.
- **Speed is a first-class metric (PR30)** — every metered
  `llm_response` carries `ttft_ms` (first response byte: first content
  chunk for streams, first body byte for buffered — the buffered path
  reads upstream through a `_FirstByteTimedStream` shim so
  `Response.aread` still owns body assembly), `total_ms` (same value as
  the established `latency_ms`, kept for readers), and derived
  `tokens_per_sec` = completion tokens ÷ call wall seconds. Cache
  replays emit the timing but a null rate — they measure the store, not
  the model — and are excluded from every analytics timing stat
  (latency included). Analytics rolls them up as `ttft_ms` stats
  (avg/p95/max, pooled) + `avg_tokens_per_sec` per sandbox, cell, and
  model;
  events predating the fields contribute no sample. The analysis page's
  latency chart renders ttft avg/p95 bars next to avg/p95/max (hidden
  when a backend emits no ttft) plus an output-speed card (mean avg
  tok/s per cell), the timeline metrics strip gains a `spd` chip, and
  the results Analytics table adds `ttft ms` + `tok/s` rows.
- **Quality × cost/speed scatter + deterministic verdict (PR33/F22)** —
  the analysis page's "Quality × cost | wall clock" card plots one dot
  per config × model cell (the same `buildScoreGrid` fold as the PR31
  grid — trials mean in), y = mean weighted total, x = mean est. cost or
  wall-clock, color/shape = model, size = mean avg tok/s. Accent rings +
  a dashed line mark the Pareto frontier (nothing is both cheaper/
  faster AND better). Cells missing the active axis are EXCLUDED and
  named in the legend — unpriced never plots at $0 — and the summary
  "verdict" card under the chart is a pure template from
  `web/lib/scatterMatrix.ts` (`paretoFrontier`, `buildVerdict`,
  `verdictLines`), `node --test`-covered like `gridMatrix.ts`.
- **Task inputs seed the workspace once per run (PR24/F23)** — the seed is
  materialized per RUN (`data/seeds/<run_id>/`, atomic rename), not per
  sandbox: N cells cloning the same repo independently would waste the
  wall clock and could race remote ref movement — one fetch pins
  `resolved_sha`, then every workspace copies the same local bytes, and
  the identical-bytes proof is the recorded `harness_config.seed_sha` (the
  copy's own content hash, not the provenance hash). Archives upload via
  `POST /api/uploads` BEFORE the run submits (upload ids are uuids;
  `RunCreate.task_inputs.archive_id` 422s on a dangling id — a run can
  never reference an archive staged after submit). Repo seeds are
  `https://`-only public clones (`init`+`fetch --depth 1`+`checkout
  FETCH_HEAD` — one code path for branch/tag/sha); `git` runs with
  prompting disabled under `SEED_GIT_TIMEOUT_SECONDS`. Archive extraction
  never calls `extractall` — members are name-checked and byte-capped,
  and a post-materialization sweep drops symlinks resolving outside the
  seed root. **Preset contract (PR22/F24):** preset frontmatter
  may carry `seed: {repo_url: <url>, ref: <sha>} | {upload: <upload_id>}`
  — it maps onto `task_inputs` when `preset_ref` resolves (`upload`
  translates to `archive_id`). Prompt cells have no workspace — a
  `task_inputs` seed simply doesn't reach them.
- **Task presets (PR22/F24) mirror the skillpack resource model** — vendored
  presets are committed `task_presets/<slug>.md` files (same stdlib
  frontmatter parser as skillpacks — flow-style values like
  `tags: [a, b]`, no YAML block lists), user presets are Postgres rows;
  both share ONE slug namespace and create-collisions return the same
  structured 409 `name_conflict` + `suggested_name`. Submit sends
  `preset_ref` — the API resolves it, REPLACES `task_prompt` with the
  preset body verbatim, and snapshots `runs.preset_ref` +
  `preset_revision` (provenance + grouping axis; `task_prompt` stays the
  authoritative run text — a preset edit never rewrites history; resubmit
  re-resolves the CURRENT revision). Vendored presets are read-only at
  runtime (PATCH/DELETE 409); user PATCH bumps `revision`. Slugs validate
  before any filesystem touch — traversal (`../x`) is a 422, never a
  path. `/presets` + `/presets/[slug]` expose browse/detail with run
  history, per-model/per-config leaderboards, and a score trend — the
  grouping axis later suites/leaderboard pages hang off.
- **Benchmark suites (PR26/F25) are vendored files, not preset fields** —
  the roadmap sketched `preset.suite.cases`; the shipped model is a
  `benchmark_suites/<slug>/suite.yaml` task list (preset_ref XOR inline
  prompt, optional `adapter` pin, `checks:` per task) because a suite
  groups heterogeneous tasks, not parameterizations of one prompt.
  `POST /api/suites/{slug}/run` fans out ONE normal run per task under a
  shared `suite_runs` batch row (`runs.suite_ref`/`suite_index`/
  `suite_run_id`) — the whole existing lifecycle (approval → provision →
  monitor → snapshot → eval) stays authoritative; no second runner.
  **Deterministic checkers are complementary evidence, never judge
  input**: `scoring/checkers.py` measures pass/fail/skipped/error
  post-artifact into append-only `checker_results` (autoincrement `id`
  orders newest-wins merges; rescores append, never update). The only
  deliberate fold into the score table is the explicit `suite_pass` row
  (pass rate ×10) the card asked for. Candidate code still never
  executes on the host — `py_test` runs inside locked `heg-exec`; a
  missing Docker/image is `skipped`, not a failure. Paths are
  suite-jailed and byte-capped; slugs validate before filesystem touch.
- **Timeline compare-mode clock (F31)**: timeline + run-console headers
  carry an `absolute | relative` `.seg` toggle (`?clock=` seeds the
  timeline). Relative re-bases each cell to its own earliest
  `run_started` (else first event) so `+2m14s` compares progress across
  paced/queued start skew; the pure transform lives in
  `web/lib/timelineModes.ts` (`laneStartMs`/`relMs`/`formatRelativeMs`,
  node-tested). Pre-`run_started` provisioning events keep NEGATIVE
  offsets rather than clamping — the seed/spawn prelude is real signal.
  Run-level (`sandbox_id: null`) events share the `""` baseline — the
  console's mixed system lane keys everything to it so its clock stays
  monotonic — and day markers drop out in relative mode. Console lanes
  gained an `.evc-ev__t` time column + a footer stat (absolute:
  `start HH:MM:SS`; relative: elapsed to the lane's latest event —
  `Date.now()` is barred by the render-purity lint).
- **Leaderboards rank sandbox CELLS, not runs (PR32/F27)** —
  `GET /api/analytics/leaderboard` pools the newest-wins `overall`
  Score per sandbox (mean ± sample sd over n scored cells + distinct
  runs) into two boards: participant model and config FAMILY
  (`family:<adapter>|<sorted packs>` — model pins, names, and trial
  markers stripped so one family aggregates across submissions; `name`/
  `label` surface only when every member agrees). Filters are run-axis:
  `preset` (runs.preset_ref), `since`/`until` (inclusive created_at
  bounds — naive inputs parse as UTC), `min_trials` (runs.trials ≥ N,
  the submitted repeat count — not raw score count). Unscored cells
  are excluded, never zeroed; `sandboxes` vs `n` keeps coverage honest.
  `/compare?runs=idA,idB` fetches detail+results+analytics per run
  client-side (no new endpoint): spec-diff rows, the same
  `buildScoreGrid` score matrices as the results page, participant-vs-
  evaluator-split analytics deltas, and the terminal-reason mix.
  `web/lib/compareRuns.ts` holds the pure transforms; the shared
  `LeaderboardBoards` renders on /leaderboard and embedded, preset-
  filtered, on /presets/[slug].
- **Run export is a projection, not a transcript (PR35/F28)** —
  `GET /api/runs/{id}/export?fmt=json` ships the `heg-run-export` v1
  bundle: spec + every rubric version + the verbatim `RunResults`/
  `RunAnalytics` payloads (the endpoint CALLS the same handlers the UI
  uses — never re-derive) + events-lite rows: event payloads projected
  to top-level scalars (strings capped) plus the `usage` dict; nested
  content dropped on purpose. `fmt=csv` flattens to one row per
  sandbox×dimension (judge `overall` = last dim row per sandbox;
  unscored sandboxes still get one empty row; per-sandbox + analytics
  columns repeat; UTF-8 BOM for Excel). The bundle models
  (`RunExport`/`EventLiteOut`/`RunSpecOut`) live in
  `api/routers/results.py` per the Phase-2 ownership split.
- **Share links are the PR19 extension point (PR35/F28)** —
  `runs.share_token` (nullable unique, migration `c5f9a2b8d4e7` chained
  on `e4f5a6b7c8d9`) minted idempotently by `POST /api/runs/{id}/share`,
  revoked by DELETE. `GET /api/share/{token}` serves a read-only bundle
  (spec + matrix + bounded events-lite) to `/share/{token}` — the app
  is unauthenticated, so the token is capability-URL prep for PR19
  auth, NOT a security boundary today; when auth lands, the
  control-plane routes lock down while `/share/*` stays open on the
  token.
- **Dashboard summary is one server-side aggregate (PR36/F29)** —
  `GET /api/analytics/summary` returns everything `/dashboard` needs
  that the leaderboard doesn't already serve (KPI totals, `?weeks=`
  ISO-Monday buckets of runs/turns/spend ending at the current week,
  terminal-reason counts + a separate pending count, the queue lane —
  active runs + `queued` stamped with promotion-order position from
  `started_at`, awaiting-review count, and `?recent=` newest runs with
  newest-wins mean `overall`). One events scan prices each metered
  call exactly like per-run analytics: sandbox events after the first
  evaluator-source event bill at the run's `evaluator_model`;
  run-level calls (`sandbox_id` null — rubric drafting) bill at the
  payload `model`. Unpriced calls null their week's total + the grand
  total (never zero-filled; `unpriced_llm_turns` names the gap),
  `?price_model=` reprices all traffic like the trends endpoint.
  `/dashboard` reuses the PR32 leaderboard for its top-5 boards and
  polls only while the lane is busy. `web/lib/dashboard.ts` holds the
  pure chart transforms (node-tested).
- **F30 — local-run sandbox pacing reuses the run-queue pattern, one
  level down**: `"queued"` is a real SANDBOX status (rows persist —
  sweeps/UI see parked cells; `ACTIVE_SANDBOX_STATES` =
  provisioning/running/terminating explicitly excludes it), applied ONLY
  when every participant model resolves local (`pacing.LOCAL_PROVIDERS`
  = ollama — provider routing is mirrored in `pacing.provider_for_model`
  rather than imported, since gateway settings differ from api
  settings). The cap is a concurrent-CONTAINER count driven by
  `lifecycle._watch_cells_paced` — claims run on the loop thread between
  `asyncio.wait` completions so FIFO order is deterministic, and a
  failed spawn never eats a slot (only active states count). A queued
  cell's launch re-runs the full spawn pipeline (`provision._launch` —
  staging/mem gate/cred/docker) — pacing wraps, it doesn't shortcut.
  `queue_sandbox`/`claim_queued_sandbox`/`spawn_queued` mirror
  `place_run`/`claim_next_queued` semantics.
- **F30 — budget extensions are events, not columns**: no migration —
  `extend_budget`/`auto_extend` grants are append-only `budget_extended`
  checkpoints summed per read (`metering.budget_state`/`budget_states`),
  so a control-plane bump is live on the gateway's next request and the
  monitor's next poll without restarting the cell, and the timeline
  shows the grant. `budget_warning` checkpoints (once per sandbox+limit,
  ~80% of the EFFECTIVE cap) dedup the same way — read-then-emit on both
  the gateway request path and the monitor backstop. The default run
  budget moved 200k → 500k (`BudgetsIn`/`DEFAULT_RUN_TOKEN_BUDGET`
  aligned) — budgets stop runaways, not honest work.

- **Rubric dimensions are open, not canonical-locked (PR23/F26)** — the
  exact-four validator was the last thing keeping eval coding-only.
  `RubricContent` now takes 1–12 arbitrary dims; the canonical names
  remain as (a) the `code-default` template's set, (b) alias-fold
  targets for common LLM re-phrasings, and (c) the results table's
  sort anchor (canonical-first, then alpha). Structural rejections:
  names that normalize to empty or past 128 chars, reserved
  score-namespace names (`overall`, `suite_pass`, `scores`, `summary`,
  `total`), and duplicates after normalization — a 3-dim rubric is
  valid, not an error. Generation steering resolves strictly: explicit
  `rubric_template` → preset `suggested_rubric` → freeform task text,
  with `rubric_hint` → preset `category` → none for the domain line;
  all three land on `run.config` so `draft_rubric` resolves them at
  draft time. Rubric templates reuse the preset merged-store pattern
  (vendored `rubric_templates/*.json` + DB rows). The judge brief —
  not the collector set — decides signal applicability: collectors
  always report what they can measure, and `evaluator/prompts.py`
  phrases the measured-signals rule around the rubric's actual dims,
  marking non-applicable dims mid-scale + "not applicable" instead of
  fabricating or dropping rows. Frontend editing is the shared
  `RubricEditor` (structured form, raw JSON behind a toggle); raw-JSON
  textareas are gone from both edit sites.

## Working conventions

- Python: type hints, pydantic models at boundaries, asyncio for I/O.
- Events are append-only — never update/delete; corrections are new events.
- Keep changes small and verifiable; note new commands/config here as they
  land.
- **Update `FEATURE_MAP.md`** whenever a feature's code paths, UI surface, or
  tests change — it's part of the definition of done.
- Dev environment: `docker compose up -d` (api :8001, postgres :5433,
  gateway :8081); `uv run pytest`; `uv run ruff check .`; web dev
  `cd web && npm run dev` (use `-p 3002` if :3000 is taken);
  e2e `cd web && E2E_PORT=3100 npm run test:e2e`.
- `GET /api/runs/{id}/analytics?price_model=` (F9) serves per-sandbox metrics
  + estimated cost; price resolution runs through the layered
  `api/pricing.py::PriceBook` (`model_prices` glob rows over the static
  `PRICE_TABLE`, F21 — managed via `GET/PUT/DELETE /api/prices`,
  `POST /api/prices/sync` for OpenRouter).
  `tool_calls` merges every agent-activity signal without double counting —
  `tool_call` events, hook-sourced `tool_result` outcomes (per-name max),
  and `shell_command`/`file_op`/`test_execution` as proxies when a lane has
  no finer signal — and carries `empty_workspace` when the monitor flagged
  a done-ish exit on a file-less workspace.
- `UPSTREAM_PROVIDER=ollama|openrouter|openai|anthropic` (gitignored `.env`
  keys `OPENROUTER_API_KEY`/`OPENAI_API_KEY`/`ANTHROPIC_API_KEY`; compose
  `${VAR}` passthrough) picks the gateway's default upstream — every provider
  a request may resolve to needs its key or the gateway refuses to boot.
  `PROVIDER_ROUTES` (JSON `{"glob": "provider"}`, first match wins,
  case-insensitive) overrides per request model — e.g.
  `{"claude-*": "anthropic"}` serves claude_code from Anthropic while the
  rest stays on Ollama. Namespace rule: a glob with no `/` is a local-name
  pattern and never matches a `vendor/model` id (`qwen*` can't hijack
  OpenRouter's `qwen/qwen3-8b:free`) — write `qwen/*` or an exact id to
  route namespaced models. Coverage differs: `openrouter` has chat/completions +
  models only; `openai` adds `/v1/responses` but no `/v1/messages`
  (claude_code/dsh can't use it); `anthropic` has `/v1/messages`
  (+count_tokens) + models but no chat/completions/responses (miniloop,
  rubric drafting, codex can't use it) — unsupported paths return the
  upstream's native 404. Client bases normalize to the API root: httpx
  concatenates base+request paths and request paths carry `/v1/…`. Callers'
  Ollama-only body params (`think`, `num_ctx`, `keep_alive`, `options`) are
  stripped for non-ollama providers; note mandatory-reasoning models (e.g.
  ernie-5.5) 400 on miniloop's `reasoning_effort:"none"` — pick tolerant free
  ids (particle-1, ling-3.2-flash, dots-3 verified). Anthropic auth is
  `x-api-key` + `anthropic-version` (`ANTHROPIC_VERSION`, default
  2023-06-01; caller `anthropic-beta`/`anthropic-version` headers are
  forwarded when the resolved provider is anthropic). An `x-heg-provider`
  request header forces the provider outright on service creds
  (`sandbox_id=None`; unknown/unkeyed values 400 — silently ignored on
  sandbox-scoped creds so agent code can't pick paid upstreams). Emitted
  `llm_request`/`llm_response`/`error` events record the resolved provider.
  `GET /api/models?provider=<name>` forwards that header on its minted
  service cred so the submit form's per-row provider picker browses each
  upstream's catalog (row provider is picker metadata only — `RunCreate`
  carries no per-model provider; routing still resolves per model name via
  PROVIDER_ROUTES). Tests pin `UPSTREAM_PROVIDER=ollama` in
  `tests/conftest.py`.
- `GET /api/skillpacks` (F11) lists vendored + imported packs (the submit
  form's source of truth); `POST /api/skillpacks/discover` (GitHub URL →
  skills), `POST /api/skillpacks/import` (selection → `data/skillpacks/`;
  `mode` = `pack`|`merge`|`split`, collision → 409 `detail.suggested_name`),
  `DELETE /api/skillpacks/{name}` (imported only).
- `GET /api/presets` (PR22) lists the merged task-preset library (vendored
  `task_presets/` + user rows) with browse aggregates (`runs_count`,
  `last_run_mean`); `GET /api/presets/{slug}` (+`/analytics` → runs,
  per-model/per-config leaderboards, trend), `POST /api/presets/{slug}`
  (create user preset — slug collision → 409 `name_conflict` +
  `suggested_name`, same contract as skillpack import), `PATCH`/`DELETE`
  (user presets only — vendored are filesystem resources; PATCH bumps
  `revision`). `RunCreate.preset_ref` resolves the preset and REPLACES
  `task_prompt` with its body verbatim; `runs.preset_ref` +
  `preset_revision` snapshot the provenance, and `GET /api/runs?preset=`
  filters. Slugs validate `^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$` BEFORE
  any filesystem touch — vendored resolution is traversal-safe.
- **Known issues (Phase 3 candidates):** event ingest has no retry/buffering —
  a sandbox emitting during an api restart loses early events; gateway emits
  best-effort. `heg-dev` static cred exists for dev only. Unauthenticated
  GitHub ref/sha resolution (F11) is best-effort under the ~60/hr
  api.github.com cap — imports still work via codeload tarball when it
  degrades.
