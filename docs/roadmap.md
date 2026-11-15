# Post-MVP roadmap — PR plan

Handoff doc for implementation agents. Read `AGENTS.md` (architecture,
conventions, decisions log) and `FEATURE_MAP.md` (per-feature code paths +
verify steps) first. Repo conventions: Python 3.12 type hints, pydantic at
boundaries, append-only events, `uv run pytest` + `uv run ruff check .` must
stay green, update `FEATURE_MAP.md` + `AGENTS.md` as part of done. Work each
PR on its own branch/worktree — shared files listed under "conflicts" are
append-mostly; rebase on main before merge.

## Already landed (do not redo)

- F0–F12 (see FEATURE_MAP.md): orchestrator, gateway w/ metering+budgets,
  sandbox runtime + egress lockdown, miniloop + 5 CLI adapters (codex,
  claude_code, pi, deepseek, aider), evaluator w/ judge retries, results UI,
  analytics (F9), skillpack import (F11), artifact preview static+desktop
  (F12).
- **PR2 preview fixes**: `type=` POST-body override on the artifact preview
  (`plan_for_type` picks the first candidate of a type; `entry`+`type` forces
  the plan — `static` serves any jailed file, `desktop` requires `.py`) and a
  `ScanCache`'d results-GET probe (`PREVIEW_SCAN_TTL_SECONDS`, default 30s).
  Details: `FEATURE_MAP.md` F12.
- **PR3 preview types**: `python-server` (web apps via `heg-preview-server`
  + listen-socket relay runner) and desktop GUI stacks beyond pygame
  (pyglet/arcade/tkinter — baked deps, zero-egress preview nets). Shared
  `_spawn_isolated` runner+sidecar topology; `_PROXIED_PORTS` per-type port
  resolution. Details: `FEATURE_MAP.md` F12.
- **PR9 timeline-side analytics**: per-sandbox metrics strip on
  `web/app/runs/[id]/timeline/` (turns/tokens/latency/cost/errors chips +
  judge line; chips double as sandbox-filter selectors; `sandboxStrips`
  transform in `web/lib/timelineAnalytics.ts` + `node --test` unit tests).
  Leftover idea: on-event-hover cumulative stats.
- **PR10 split-view trace comparison**: `merged | split` toggle on the
  timeline page — two sandbox lanes on ONE merged `(ts,id)` ordering inside
  a single scroll container (synchronized scroll + time alignment by
  construction, no scroll mirroring); sticky lane pickers with
  swap-on-collision, run-level/other-sandbox events as full-width shared
  rows, metrics chips assign lanes in split mode; transform + tests in
  `web/lib/splitTimeline.ts(.test.ts)`, shared row markup in
  `timeline/EventRow.tsx`.
- **OpenRouter provider** (commit 8014338): `UPSTREAM_PROVIDER=ollama|openrouter`
  in `gateway/config.py`; client base normalizes to `/api/` (httpx
  concatenates base+request paths); Ollama-only body params
  (`think`/`num_ctx`/`keep_alive`/`options`) stripped for hosted providers;
  tests pin `UPSTREAM_PROVIDER=ollama` in `tests/conftest.py`. OpenRouter
  only serves `/v1/chat/completions` + `/v1/models` — `/v1/messages` and
  `/v1/responses` 404 there (claude_code/codex needed ollama until PR12
  routing). Verified free
  ids: `particle-ai/particle-1:free`, `inclusionai/ling-3.2-flash:free`,
  `dots-studio/dots-3-note-preview:free`. Mandatory-reasoning models (ernie,
  lfm) 400 on miniloop's `reasoning_effort:"none"`.
- **PR6 — static-analysis signals** (`scoring/` package, see below).
- **PR7 — exec + coverage signals** (`exec` collector in `scoring/` +
  `sandbox/exec.py` ExecRunner + `heg-exec` image, see below).
- **PR8 — Automated test-suite generation**: `evaluator/testgen.py` writes
  a pytest suite via the gateway (eval cred, metered), persists it to
  `<artifact>/generated_tests/`, runs it via `ExecRunner` `test_target`
  (`EXEC_TEST_TARGET` env → `/srv/test_target` ro mount in `heg-exec`),
  and folds pass rate into the `generated_tests` `SignalReport` →
  `ANALYSIS.md`/judge brief/results API flat summary. `SCORING_TESTGEN=0`
  opts out; `TESTGEN_*` knobs bound prompt budget, files, tokens, timeout.
- **PR11 — Graphical analysis + cross-run dashboards** (F13): recharts,
  `GET /api/analytics/runs`, results-matrix totals row.
- **OpenAI + Anthropic providers + per-model routing** (PR12):
  `UPSTREAM_PROVIDER` gains `openai` (chat/completions + responses + models;
  no /v1/messages) and `anthropic` (/v1/messages + count_tokens + models;
  no chat-completions) — Anthropic auth is `x-api-key` + `anthropic-version`.
  `PROVIDER_ROUTES` (JSON `{model-glob: provider}`, first match wins)
  overrides the process-wide provider per request model, so one gateway can
  e.g. route `claude-*` to Anthropic while the rest stays on Ollama. Every
  provider a request may resolve to must have its key (boot-time check).
  An `x-heg-provider` request header forces the provider outright on
  service creds (`sandbox_id=None`; unknown or unkeyed values 400) and is
  silently ignored on sandbox-scoped creds — agent code can't retarget
  onto paid upstreams; caller `anthropic-beta`/`anthropic-version` headers
  forward to the anthropic upstream. Emitted `llm_*`/`error` events record
  the resolved provider.
- **Gateway response cache / deterministic replay** (PR18): opt-in
  `GATEWAY_CACHE_ENABLED` (off by default); sqlite store
  (`gateway/cache.py`, `GATEWAY_CACHE_PATH`) keyed on
  `namespace+path+provider+outgoing-body` — hits replay byte-verbatim with
  `x-heg-cache: hit`, emit `llm_*` events marked `payload.cached: true`,
  and still count toward the run budget (gate runs before lookup). Only
  complete 2xx bodies stored; `count_tokens`/`/v1/models` bypass;
  `GATEWAY_CACHE_NAMESPACE` bump invalidates; `GATEWAY_CACHE_MAX_ENTRIES`
  LRU bound.
- **PR5 — Eval iteration** (F14): re-score on stored artifacts
  (`POST /api/runs/{id}/rescore` → `lifecycle.rescore_run`), versioned
  post-run rubric edits (`parent_id` chain, `Score.rubric_version`/`seq`
  provenance, `?rubric_version=` results filter). See FEATURE_MAP.md F14.
- **PR17 — hook/plugin telemetry lane**: shared receiver
  `adapters/common/hooks.py` (`python3 -m adapters.common.hooks <adapter>`)
  wired to claude_code `settings.json` hooks (stdin), codex `notify`
  (argv), and pi's generated `heg-events.ts` extension (stdin); per-adapter
  `translate_hook` normalizes payloads stamped `via: "hook"`; new
  `tool_result` EventType carries tool outcomes. Details: FEATURE_MAP.md
  F10 + docs/p25-harness-adapters.md §PR17.
- **PR15 — Run pacing** (`queued` run status + `orchestrator/pacing.py`):
  `place_run`/`claim_next_queued` FIFO promotion via
  `runner._lifecycle_then_promote` + `startup_sweep` re-kick; `spawn_one`
  waits on `SPAWN_MIN_FREE_MEM_MB` headroom capped by
  `SPAWN_MEM_WAIT_SECONDS` (timeout fails that sandbox, siblings fail
  fast). Details: `FEATURE_MAP.md` F2.

## Dependency DAG

Hard edge (──▶) = must merge first (shared files or required infra).
Soft edge (╌▶) = works better after / benefits from.

```
PR1  Submit UX ────────▶ PR14 Model-vs-model ◀╌╌╌╌ PR12 Providers ──▶ PR18 Cache/replay
   │                                                         │
   ├╌ labels ╌▶ PR9  Timeline-analytics ──▶ PR10 Split-view   ├╌╌╌▶ PR13 Aider ╌╌╌▶ PR17 Hooks
   └╌ labels ╌▶ PR10

PR2  Preview fixes ────▶ PR3  Preview types ──▶ PR4  Thumbnails

PR6  Static analysis ──▶ PR7  Exec+coverage ──▶ PR8  Test-suite-gen

PR15 Queue/mem-aware ──▶ PR16 Add-next-prompt ╌╌▶ PR20 K8s
PR19 Auth/templates/share — independent
```

## Topological levels

- **Depth 0 (start immediately):** PR1, PR3,
  PR15, PR19 — plus PR17 (soft ←13 satisfied — PR13 landed).
- **Depth 1:** PR4 (←3), PR10 (←9), PR14 (←1), PR16 (←15),
  PR18 (←12).
- **Floating:** PR20 (soft ←15).

Critical path depth = 1 remaining (`3→4`; was 3 via `6→7→8` before
PR2, PR6, PR7, and PR8 landed). Max safe concurrency ≈ 9 lanes.

## Shared-file hotspots (merge-conflict risk)

- `api/schemas.py`, `web/lib/api.ts` — most PRs append here.
- `AGENTS.md`, `FEATURE_MAP.md` — every PR updates docs; rebase.
- `db/migrations/` — PR1 adds a migration; renumber revisions at merge.
- `orchestrator/` — PR15 vs PR16 both touch the state machine;
  coordinate.
- `sandbox/preview.py` + `api/routers/preview.py` + `PreviewControls.tsx` —
  all of lane B; strictly sequential.

---

## PR cards

### PR1 — Submit-form UX
- **Scope:** dynamic model picker (replace hardcoded `MODEL_SUGGESTIONS`
  with live list from gateway `/v1/models` — pass-through exists; add an
  api proxy route e.g. `GET /api/models` → `GATEWAY_URL`); model install
  option (api endpoint → `ollama pull` on the ollama provider, stream pull
  progress); named harness configs (optional label per config, persisted
  onto the sandbox row — alembic migration — surfaced in timeline filter,
  results matrix, analytics; default `<adapter>-<sandbox short id>`).
- **Touch:** `web/app/submit/`, `web/components/ComboInput.tsx`,
  `web/lib/api.ts`, `api/schemas.py` (`HarnessConfigIn`), `api/routers/runs.py`,
  `api/routers/harnesses.py`, db `sandboxes` (new `name` col migration),
  `web/app/runs/[id]/timeline/` + results labels.
- **Verify:** unit tests for schema + migration; UI shows live model list
  + names in filters.

### PR2 — Preview fixes ✅ landed
`type=` POST-body override + `ScanCache`'d results-GET probe — see "Already
landed" and `FEATURE_MAP.md` F12.

### PR3 — Preview types — ✅ landed
- **Scope:** `python-server` preview type (run candidate code like desktop —
  needs the runner+socat-proxy topology, zero-egress internal net);
  more desktop types: pyglet/arcade/tkinter (baked deps or wheelhouse mount —
  no egress in preview nets).
- **Landed:** `python-server` spawns `heg-preview-server` (baked flask/
  fastapi/django/aiohttp/bottle/tornado/gunicorn) + `server-preview-runner.py`
  which detects the app's first LISTEN socket via `/proc/net/tcp{,6}` and
  relays `:8000` → actual port (direct when the app already binds non-loopback
  `:8000`; `manage.py` → `runserver 0.0.0.0:8000 --noreload`). Desktop image
  gained pyglet/arcade/tkinter (multi-stage wheels build — pymunk has no
  aarch64 wheel; system `tk` + mesa GL libs). `_spawn_desktop`/`_spawn_server`
  share `_spawn_isolated`; `_PROXIED_PORTS` resolves sidecar ports per type.
  `PREVIEW_TYPES` = {static, desktop, python-server}; `templates/` skipped in
  scans (flask render dir). Details: `FEATURE_MAP.md` F12.

### PR4 — Artifact thumbnails
- **Scope:** lightweight screenshots of produced artifacts for at-a-glance
  review before spawning a full preview sandbox (reuses preview spawn infra
  + a headless capture step).
- **Touch:** `sandbox/preview.py` or new `sandbox/thumbnails.py`, artifact
  store layout (`data/artifacts/<run>/<sandbox>/`), results API + UI.

### PR5 — Eval iteration: re-score + rubric edits — ✅ landed
Landed as F14: `POST /api/runs/{id}/rubrics` appends immutable rubric
versions (`parent_id` chain, previous approved → `superseded`,
`uq_rubrics_run_version`), `POST /api/runs/{id}/rescore` re-runs the
evaluator on stored artifacts via `orchestrator.lifecycle.rescore_run`
(terminal runs only, `rescore` task-kind dedup), and Score rows carry
`rubric_version`/`seq` provenance for the newest-wins matrix merge.
Paths + tests: FEATURE_MAP F14.

### PR6 — Objective scoring I: static analysis — ✅ landed
Landed as the `scoring/` collector registry (lizard, parse-only): the
evaluator stages `ANALYSIS.md`/`analysis.json` judge evidence, persists
`<artifact>/analysis.json`, emits an `eval_analysis` checkpoint, and the
results API embeds `SandboxResult.analysis`. Paths + tests: FEATURE_MAP F7.

### PR7 — Objective scoring II: exec + coverage — ✅ landed
Landed as the `exec` signal collector: candidate build/tests/coverage run
inside a locked `heg-exec` container (`--network none`, uid-split demotion
of candidate phases to nobody, resource + tmpfs bounds, sentinel-first
result channel). `analysis.json` gains the exec signal, `ANALYSIS.md` an
"Executed checks" block, a `test_execution` event is emitted when tests
ran, and `GET /api/runs/{id}/results` exposes a flat `exec` ExecSummary.
`SCORING_EXEC=0` disables. Paths + tests: FEATURE_MAP F7.

### PR8 — Automated test-suite generation — ✅ landed
See "Already landed" — `evaluator/testgen.py` + `ExecRunner` `test_target`
(`EXEC_TEST_TARGET`), `generated_tests` SignalReport → judge brief,
`SCORING_TESTGEN`/`TESTGEN_*` flags.

### PR9 — Timeline-side analytics — ✅ landed
- **Scope:** per-sandbox metrics (cost/tokens/latency) rendered alongside
  the timeline or on event hover — F9 already computes everything; mostly
  UI placement.
- **Touch:** `web/app/runs/[id]/timeline/`, `web/lib/api.ts`
  (`getRunAnalytics` exists).
- **Verify:** visual + a unit test on any new transform.
- **Landed:** metrics strip between toolbar and stream (`SandboxMetricsBar`
  + `sandboxStrips`); see "Already landed" + `FEATURE_MAP.md` F9.

### PR10 — Split-view trace comparison — ✅ landed
- **Scope:** side-by-side, time-aligned event streams for two sandboxes on
  the timeline page (today: one merged stream + a single-sandbox filter
  dropdown); scrolling/diff-alignment UX TBD.
- **Touch:** `web/app/runs/[id]/timeline/` (new split component),
  `web/lib/api.ts` if new query params needed.
- **Depends on:** PR9 (same page — land it first).
- **Landed:** `merged | split` toolbar toggle (`?view=split&left=&right=`
  seeds lanes, auto-defaults to first two sandboxes until touched). Design
  picked over scroll-mirroring: ONE scroll container renders a merged
  `(ts,id)` ordering as two-column rows — each event fills only its lane's
  cell, so sync scroll + time alignment are structural and a quiet lane
  reads as gaps (diff-tool idiom). `buildSplitRows`/`pickLane`/`toggleLane`/
  `laneEventCounts` in `web/lib/splitTimeline.ts` (+
  node:test); `SplitTimeline.tsx` sticky lane heads (picker swap on
  collision, status badge, per-lane counts); `EventRow.tsx` extraction
  shares row markup/summaries/colors across both views; run-level +
  non-lane-sandbox events span both columns; PR9 metrics chips assign lanes
  in split mode (`selectedIds` prop); sandbox filter dropdown is
  merged-only. `ts` ordering uses `Date.parse` (mixed fractional-seconds
  ISO strings don't sort lexicographically).

### PR11 — Graphical analysis page + cross-run dashboards — ✅ landed
- **Scope:** charts over F9 analytics + scores: per-dimension score
  comparison across sandboxes, token/cost/latency per sandbox, tool-call
  mix, eval-vs-participant spend split, cross-run trends; **also a totals
  row / aggregate column on the results score matrix** (per-sandbox winner
  at a glance). Charting lib: recharts.
- **Touch:** new page (e.g. `web/app/runs/[id]/analysis/`), results matrix
  component, `api/routers/analytics.py` if new aggregates needed.
- **Verify:** renders on real run data; totals match matrix sums.

### PR12 — Providers (remainder) ✅ landed
- **Scope:** remaining upstreams — direct Anthropic + OpenAI adapters
  (OpenRouter already landed); per-request/per-model provider routing if
  wanted (today's switch is process-wide).
- **Landed:** `openai` + `anthropic` providers; `PROVIDER_ROUTES` JSON
  model-glob → provider overrides resolved per request `model`;
  `x-heg-provider` request header override (service creds only); caller
  `anthropic-beta`/`anthropic-version` headers forward to the anthropic
  upstream; provider recorded on emitted `llm_*`/`error` events.
- **Touch:** `gateway/config.py`, `gateway/deps.py`,
  `gateway/main.py` (body transforms if APIs differ), `api/config.py`
  (key plumbing), `.env.example`.
- **Verify:** `tests/unit/gateway/test_upstream_provider.py` pattern.

### PR13 — Aider adapter (+ dsh helpers if needed)

> **Status: landed** — `adapters/aider/` shipped; dsh native helpers
> skipped (no TTY-dependent evals — see `docs/p25-harness-adapters.md`).

- **Scope:** Aider CLI adapter following the F10 shim pattern; dsh native
  helpers (spawn-helper, node-pty via `--allow-scripts`) ONLY if evals hit
  TTY-dependent tasks — otherwise skip.
- **Touch:** `adapters/aider/` (adapter.py, config.py, translate.py,
  `__main__.py`), `adapters/common/`, `orchestrator/adapters.py` registry,
  `sandbox/images/aider.Dockerfile`, `api/schemas.py` adapter enum.
- **Verify:** `tests/unit/adapters/test_adapter_contract.py` covers it
  automatically + per-adapter tests; `test_adapter_images.py` docker test.

### PR14 — Model-vs-model comparisons
- **Scope:** multi-model matrices — vary model as well as harness config
  (matrix expansion is `configs × model` already; needs spec/UI support +
  compare-by-model analytics). Reverses the early "harnesses not models"
  decision — flag in docs.
- **Touch:** `api/schemas.py` run spec, `orchestrator/` matrix expansion,
  submit form, results/analytics UI.
- **Depends on:** PR1 (HarnessConfigIn schema churn settles); best after
  PR12 (needs >1 usable provider to be interesting).

### PR15 — Run pacing: submission queue + memory-aware scheduling — ✅ landed
`queued` run status + `orchestrator/pacing.py` — see "Already landed" and
`FEATURE_MAP.md` F2.

### PR16 — "Add next prompt" (multi-turn runs)
- **Scope:** continue a finished sandbox with a follow-up prompt on the
  same workspace — new phase in the event stream + artifact re-snapshot;
  resume-in-place vs respawn-from-snapshot TBD (decide at kickoff).
- **Touch:** `orchestrator/` state machine + lifecycle, `api/routers/runs.py`,
  sandbox runtime (resume semantics), run detail UI.
- **Depends on:** PR15 (same files; pacing settles first).

### PR17 — Higher-fidelity instrumentation via hooks/plugins — ✅ landed
Landed as the hook/plugin telemetry lane: `adapters/common/hooks.py` shared
receiver (right-to-left argv dict-JSON scan → select-gated bounded stdin,
always exit 0, JSONL mirror to stderr only) + per-adapter `translate_hook`
paired with `MANIFEST["hooks"]` (contract-tested). claude_code
`settings.json` hooks (`PostToolUse` → `tool_result`; lifecycle /
notification → `checkpoint`), codex `notify` (`agent-turn-complete` →
`checkpoint` + `last-assistant-message`), pi generated
`extensions/heg-events.ts` (`tool_execution_end` → `tool_result`);
deepseek/miniloop stay translated-only. New `tool_result` EventType carries
tool outcomes; `via: "hook"` stamped on every hook payload. Details:
FEATURE_MAP F10, docs/p25-harness-adapters.md §PR17.

### PR18 — Gateway response caching / deterministic replay ✅ landed
- **Scope:** cache upstream responses keyed on request for deterministic
  replay (dev/CI aid — cheap reruns).
- **Landed:** `gateway/cache.py` sqlite store keyed on
  `namespace + path + provider + outgoing-body`; opt-in
  `GATEWAY_CACHE_*` flags (off by default); hits replay byte-verbatim
  (`x-heg-cache: hit`), still emit `llm_*` events with `payload.cached:
  true`, re-meter stored usage against the run budget, and the budget gate
  runs before lookup — replays accrue + 429 identically to live runs. Only
  complete 2xx bodies stored; unmetered probes bypass; failures degrade to
  miss. Details: `FEATURE_MAP.md` F3.
- **Touch:** `gateway/` (cache layer in proxy path, config flags).
- **Depends on:** PR12 (gateway churn settles).

### PR19 — Auth + saved run templates + share links
- **Scope:** auth (explicitly out of MVP scope — local single-user tool);
  saved run templates (small — could pull forward into PR1 if wanted);
  share links (needs auth-adjacent infra).
- **Touch:** api middleware, `web/` auth surfaces, db for templates.

### PR20 — Multi-host / K8s sandbox scaling
- **Scope:** sandbox spawning beyond the single host docker socket.
- **Depends on:** soft — PR15 (pacing semantics to reuse).
- **Last:** premature for a local tool.
