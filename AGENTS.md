# AGENTS.md — harness_eval_gateway

Reference document for the agent working on this repo. It captures the product
vision, agreed architecture, domain model, and working conventions. Keep it
updated as decisions land.

## Project overview

A platform for comparatively evaluating **agent harnesses** (Claude Code, Codex
CLI, Aider, custom minimal loops, ...) on coding tasks, with a **custom AI
gateway** as the observability layer.

A user submits a task (e.g. "build a replica of 2048") plus a run
configuration. The system spawns isolated Docker sandboxes — one per harness —
all running the **same underlying model**, so results compare *harness
capability*, not raw model quality. An independent LLM evaluator scores each
produced artifact against a human-approved rubric. All model traffic and agent
activity are captured as structured events powering a live + persisted
execution timeline.

**MVP shape:** 1 model (Ollama, small local model) × 2 harness configs → 2
concurrent sandboxes. Evaluator runs afterwards in its own sandbox. Since
**miniloop** (our custom harness) is the first adapter, the MVP comparison
axis is harness *configuration* — e.g. different skill pack sets or system
prompts across the two sandboxes. Multi-harness comparisons land as CLI
adapters are added.

## Core concepts

- **Run** — one submitted evaluation. Spec = task prompt + model + set of
  harnesses + skill packs + evaluator model.
- **Sandbox** — a Docker container running one harness instance on the task,
  with an identical clean initial repo/workspace as its siblings.
- **Harness adapter** — per-harness plugin: knows how to install, configure,
  launch, observe, and terminate a given agent CLI inside a sandbox.
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
   flag when evaluator model == participant model.
2. **Control API + Orchestrator (FastAPI, async)** — validates submissions,
   expands run matrix, generates rubric via gateway, drives run state machine
   (`submitted → rubric_review → provisioning → running → evaluating →
   done | failed`), manages sandbox lifecycle, enforces termination policy.
3. **AI gateway (Python, custom)** — exposes an OpenAI-compatible
   (`/v1/chat/completions`) endpoint, translates to upstream providers
   (**Ollama is the default upstream for MVP** — small local models, e.g.
   `qwen2.5-coder:3b`). Anthropic-compatible (`/v1/messages`) endpoint added
   when a CLI adapter requires it. Per-run credentials map traffic to
   run+sandbox. Records: model, tokens in/out, cache read/write, latency,
   cost, failures. Emits events to Postgres. Enforces token/cost budgets.
4. **Sandboxes (Docker)** — per-harness image + installed skill packs + clean
   workspace + injected `done` mechanism. Env points harness at gateway with
   run-scoped creds. Egress locked to gateway — no telemetry holes.
   Adapter tails harness output and pushes normalized events.
5. **Postgres** — run specs, sandbox instances, frozen rubric, append-only
   event stream, artifact metadata, evaluator scores.
6. **Evaluator** — runs in its own sandbox post-run; fetches artifact + rubric,
   scores via gateway, returns score + rationale per dimension. Objective
   signals (PR6): the `scoring` collector registry measures the workspace
   before judging (parse-only static analysis — real cyclomatic complexity,
   size, language mix) and feeds the judge `ANALYSIS.md`/`analysis.json`.
7. **Artifact store** — `./data/artifacts/<run_id>/<sandbox_id>/` — repo
   tarballs, transcripts, event exports.

## Run lifecycle (maps to product flow)

1. **Submit** — form → run spec (`POST /api/runs`).
2. **Rubric draft** — orchestrator calls LLM via gateway → draft rubric →
   UI displays; user edits → approves → **frozen**, shared across sandboxes.
3. **Provision** — prepare identical initial repo; build/launch N sandboxes.
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
| `error` | Crash / unrecoverable failure |

Provisional defaults (confirm with user): 60 min wall clock, $2 spend cap,
stuck-loop = 3 consecutive identical tool calls.

Every sandbox gets a `done` mechanism baked in (a `done` CLI on PATH and/or a
done skill pack) that signals the orchestrator — `agent_done` is distinct from
`harness_complete`.

## Event model

`events`: `id, run_id, sandbox_id, ts, seq, source, type, payload JSONB`

- **sources**: `gateway`, `harness`, `sandbox`, `evaluator`, `orchestrator`
- **types**: `llm_request`, `llm_response` (tokens in/out, cache r/w, latency,
  cost, model), `tool_call`, `tool_result` (hook-sourced tool outcomes),
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
loop: shell exec, file read/write, `done`). Post-MVP adapters: Claude Code
CLI, Codex CLI, Aider (note: these need paid accounts/API keys — not free).

**Default skill packs** (named presets, vendored under `skillpacks/`):
`superpowers`, `mattpocock-skills`, `spec-kit`.

**Providers:** Ollama default (local, free); OpenRouter / direct
Anthropic/OpenAI post-MVP.

## Rubric

MVP dimensions: functional requirements, acceptance criteria, code
implementation quality, cyclomatic complexity. Since PR6 the judge scores
from **measured** signals where available — the `scoring` package's
static-analysis collector (lizard, parse-only) computes real cyclomatic
complexity/size/language mix before evaluation; the report is staged as
`ANALYSIS.md`/`analysis.json` for the judge, persisted at
`<artifact>/analysis.json`, and surfaced on the results API. Remaining
objective signals (build/test exec, coverage) are post-MVP.

Flow: auto-generated from task spec → HITL review/edit in UI → frozen →
applied identically to all sandboxes.

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
gateway/       # LLM proxy: providers, metering, budgets, event emission
orchestrator/  # run state machine, matrix expansion, rubric gen, lifecycle
api/           # control-plane HTTP API (FastAPI app)
adapters/      # harness adapters (miniloop first; cli adapters post-MVP)
sandbox/       # image defs, workspace prep, done mechanism, event collector
evaluator/     # rubric scoring agent
scoring/       # objective signal collectors (static analysis; PR7 adds exec/coverage)
skillpacks/    # vendored default packs: superpowers, mattpocock-skills, spec-kit
skillpack_import/  # F11 GitHub import: codeload fetch, SKILL.md scan, pack store
web/           # Next.js app
db/            # schema + Alembic migrations
tests/         # unit / integration / e2e
data/          # artifacts, imported skill packs (skillpacks/), sandbox state (gitignored)
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
| F3 | AI gateway: OpenAI-compatible proxy → Ollama, telemetry, budgets |
| F4 | Sandbox runtime: spawn, egress lockdown, snapshots, done mechanism |
| F5 | Miniloop harness + adapter + default skill packs |
| F6 | Event store + live timeline (SSE) + replay |
| F7 | Evaluator sandbox: rubric scoring + rationale |
| F8 | Results UI: matrix, scores, timeline, artifacts |
| F9 | Run analytics: per-sandbox metrics + estimated cost |
| F10 | Multi-harness adapters (Codex, Claude Code, Pi, DeepSeek) |
| F11 | Skillpack import from GitHub (discover → select → import) |
| F12 | Artifact preview (static html + desktop/pygame via noVNC) |
| F13 | Graphical analysis page + cross-run trends (recharts) |

## Post-MVP roadmap

- Additional harness adapters: Claude Code CLI, Codex CLI, Aider
- Additional providers: OpenRouter, direct Anthropic/OpenAI
- Anthropic-compatible gateway endpoint (`/v1/messages`)
- Model-vs-model comparisons (multi-model matrices)
- Objective scoring: build/test execution, coverage as score dimensions
  (PR7 — static analysis landed in PR6 via `scoring/` signal collectors)
- Automated test-suite generation as eval target
- Gateway response caching / deterministic replay
- Multi-host / K8s sandbox scaling
- Memory-aware spawn scheduling — gate `spawn_one` on host memory pressure
  (docker info / cgroup stats) and queue pending sandboxes until headroom
  exists, instead of launching all N configs at once (matters at 8x)
- Run submission queue — pace/serialize whole runs so a new submission waits
  behind the active run instead of overlapping; combined with memory-aware
  spawn scheduling this bounds concurrent sandboxes when >2 would exceed
  headroom for the loaded model
- Auth, saved run templates, share links
- Dynamic model picker — populate the submit form's model suggestions from
  `ollama list` (gateway `/v1/models` pass-through already exists) instead of
  the hardcoded `MODEL_SUGGESTIONS`, plus an install option (api endpoint →
  `ollama pull`, stream pull progress into the UI)
- Named harness configs — optional per-config label set at submit, persisted
  onto the sandbox and surfaced in the timeline filter, results matrix, and
  analytics so same-adapter sandboxes are distinguishable; default to
  `<adapter>-<sandbox short id>` when left blank
- Cross-run analytics & cost dashboards
- Timeline-side analytics — per-sandbox metrics rendered alongside the
  execution timeline or on event hover (cost/tokens/latency in context)
- Split-view trace comparison — side-by-side, time-aligned event streams for
  two sandboxes on the traces/timeline page (today it's one merged stream
  with a single-sandbox filter dropdown); scrolling/diff-alignment UX TBD
- Lightweight artifact thumbnails — capture screenshots of produced artifacts
  for at-a-glance review before spawning a full live preview sandbox
- Desktop/GUI artifact previews — noVNC-style streaming for pygame etc.
  (executes candidate code → needs hardened isolation; approved direction)
- Re-score runs — re-run the evaluator on stored artifacts
- Post-run rubric edits — modify the frozen rubric after a run and re-score
  with the updated rubric (keep score provenance per rubric version)
- "Add next prompt" — continue a finished sandbox with a follow-up prompt
  on the same workspace (multi-turn runs: new phase in the event stream +
  artifact re-snapshotted; resume-in-place vs respawn-from-snapshot TBD)
- More desktop preview types (pyglet/arcade/tkinter — needs baked deps or
  a wheelhouse mount — no egress in preview nets), python-server previews
- dsh native helpers (spawn-helper, node-pty) — npm ≥11 blocks their
  postinstall scripts; CLI works without them, but revisit `--allow-scripts`
  for the dsh package set if evals hit TTY-dependent tasks

## Decisions log

- Compare **harnesses, not models**: same model across sandboxes in a run.
- Custom lightweight gateway (not LiteLLM) — observability is the core value.
- Two-layer telemetry: passive gateway capture + adapter-translated harness
  events for agent-level detail.
- HITL rubric approval; frozen pre-launch; shared across sandboxes.
- Evaluator in own sandbox; UI flags evaluator == participant model.
- LLM-judge scoring, fed by measured signals: PR6 added parse-only static
  analysis (real cyclomatic complexity + static metrics via lizard) as
  judge evidence — exec-derived checks (build/test/coverage) remain
  post-MVP (PR7).
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
  deepseek; `GET /api/harnesses` serves manifests to the submit form.
- **Gateway protocol fan-out**: `/v1/messages` (+`count_tokens`) for
  Anthropic-SDK CLIs, `/v1/responses` for Codex, `/v1/models` for probes —
  pass-throughs that preserve `stream` with a metering tee
  (`usage_from_sse`). `/v1/chat/completions` now honors `stream: true` the
  same way (pi streams unconditionally; the old force-`stream:false`
  returned a bare JSON body its SSE reader dropped as an empty response).
- **CLI image pattern**: node:slim base + `python3` for the shim + pinned
  npm CLI + `adapters/` under `/opt/harness` + `done` at `/usr/local/bin`.
  npm install happens before COPY so dependency layers cache. DeepSeek's
  `dsh` needs node >=24.9 → `node:24-slim` (others stay on 22).
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
- **Artifact preview (F12) ships static + desktop** — `PreviewPlan.type` +
  `PreviewOut.type` leave room for `python-server` next. Static = serve
  files; desktop = execute pygame under Xvnc/noVNC.
- Preview containers get their **own dedicated bridge `heg-preview`**, not
  `heg-sandbox` and not `--internal` — Docker silently drops published ports
  on internal networks (verified), so "isolated + host-reachable" is
  impossible; port binds `127.0.0.1` only, masquerade disabled (Linux),
  container hardened (nobody/read-only/cap-drop/no-new-priv).
- **Desktop previews isolate the runner, not the serving hop**: the
  executed artifact runs alone on `--internal` `heg-desktop` (zero egress —
  api-side proxying was rejected: the api would join the runner's net,
  giving candidate code a path to the control plane) with
  `--cpus`/`--memory`/`--pids-limit` bounds. A pinned `alpine/socat` sidecar
  (`heg.preview_role=proxy`, dumb TCP pipe — nothing for the artifact to
  exploit) straddles both nets and publishes the noVNC port on localhost.
- **Detection produces a typed candidate list, not a hidden pick** —
  `scan_preview` returns every runnable entry (`.html` → static, evidenced
  `.py` → desktop) ordered default-first; `plan`/`candidates[0]` is only
  the dropdown's default. `.py` candidates need pygame evidence (file
  imports pygame, OR entry-shaped + a non-comment requirements.txt line);
  scanned at root + ≤3 nested levels with hidden/`_DESKTOP_SKIP_DIRS`/
  symlinks skipped. `entry=` POST picks any workspace-relative file —
  `.py` overrides evidence (the pick IS the disambiguation). Zero
  candidates → UI shows disabled "not supported". Deps beyond baked-in
  pygame-ce can't install (no egress) — the app fails at import and the
  runner exits visibly. Static defaults when both match. `type=` POST
  overrides the pick: alone it selects the first detected candidate of
  that type (a both-matching workspace defaults to static —
  `{type:"desktop"}` reaches the game); with `entry=` it forces the plan —
  `static` serves any jailed file (the entry is only the URL opened —
  view a `.py`'s source instead of executing it), `desktop` still
  requires a `.py` entry. A live preview is returned as-is unless an
  explicit different plan is requested, then it's stopped and respawned.
  The results-GET probe is memoized by `sandbox.preview.ScanCache`
  (`PREVIEW_SCAN_TTL_SECONDS`, default 30s) keyed on
  (jail, run, sandbox, artifact_path) — workspaces only change on
  re-snapshot.
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
  `EVAL_PROMPT.md` carries targeted counter-hints (paging, no inner `"`).
- One live preview per sandbox (POST is idempotent — re-clicking returns the
  existing preview); dead containers are cleared on respawn.
- **Charts: recharts** (F13) — picked over visx/chart.js: smallest fit for
  grouped/stacked bars + a composed bar-line trend under React 19 / Next 16;
  themed via hexes mirroring globals.css tokens (no CSS-var reads). The
  cross-run trends endpoint (`GET /api/analytics/runs`) prices each point at
  its own run's resolved price unless `?price_model=` forces uniform
  repricing — same "estimate, not billing" framing as F9.

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
  + estimated cost; price table lives in `api/pricing.py` (`PRICE_TABLE`).
- `UPSTREAM_PROVIDER=openrouter` + `OPENROUTER_API_KEY` (gitignored `.env`;
  compose `${VAR}` passthrough) switches the gateway upstream to OpenRouter
  for free-model testing without local Ollama. Only `/v1/chat/completions` +
  `/v1/models` are supported there — `/v1/messages`/`/v1/responses` will 404,
  so claude_code/codex adapters still need `ollama`. Client base is the API
  root (`/api/`): httpx concatenates base+request paths and request paths
  carry `/v1/…`. Callers' Ollama-only body params (`think`, `num_ctx`,
  `keep_alive`, `options`) are stripped for non-ollama providers; note
  mandatory-reasoning models (e.g. ernie-5.5) 400 on miniloop's
  `reasoning_effort:"none"` — pick tolerant free ids (particle-1,
  ling-3.2-flash, dots-3 verified). Tests pin `UPSTREAM_PROVIDER=ollama`
  in `tests/conftest.py`.
- `GET /api/skillpacks` (F11) lists vendored + imported packs (the submit
  form's source of truth); `POST /api/skillpacks/discover` (GitHub URL →
  skills), `POST /api/skillpacks/import` (selection → `data/skillpacks/`;
  `mode` = `pack`|`merge`|`split`, collision → 409 `detail.suggested_name`),
  `DELETE /api/skillpacks/{name}` (imported only).
- **Known issues (Phase 3 candidates):** event ingest has no retry/buffering —
  a sandbox emitting during an api restart loses early events; gateway emits
  best-effort. `heg-dev` static cred exists for dev only. Unauthenticated
  GitHub ref/sha resolution (F11) is best-effort under the ~60/hr
  api.github.com cap — imports still work via codeload tarball when it
  degrades.
