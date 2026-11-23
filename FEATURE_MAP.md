# FEATURE_MAP.md

Living index of product features: **where each lives in the codebase and how to
verify it**.

**Maintenance rule:** update this file whenever a feature's code paths, UI
surface, or tests change. Treat it as part of the definition of done.

Status legend: 🔲 planned · 🚧 in progress · ✅ working

## Overview

| ID | Feature | Status | Primary code | Primary tests |
|----|---------|--------|--------------|---------------|
| F0 | Dev environment & scaffold | ✅ | `docker-compose.yml`, `db/`, `.github/workflows/ci.yml`, `web/e2e/` | `tests/test_smoke.py` |
| F1 | Submission form → run spec | ✅ | `web/app/submit/`, `api/routers/runs.py` | `tests/unit/api/test_runs.py` |
| F2 | Orchestrator (matrix, rubric, lifecycle) | ✅ | `orchestrator/` | `tests/unit/orchestrator/` |
| F3 | AI gateway (proxy, metering, budgets) | ✅ | `gateway/` | `tests/unit/gateway/` |
| F4 | Sandbox runtime (spawn, egress, snapshot, done) | ✅ | `sandbox/`, `data/artifacts/` | `tests/integration/test_sandbox.py` |
| F5 | Miniloop harness + adapter + skill packs | ✅ | `adapters/miniloop/`, `skillpacks/` | `tests/unit/adapters/miniloop/` |
| F6 | Event store + live timeline | ✅ | `api/routers/events.py`, `web/app/runs/[id]/timeline/` | `tests/unit/api/test_events.py` |
| F7 | Evaluator sandbox (rubric scoring + objective signals) | ✅ | `evaluator/`, `scoring/` | `tests/unit/evaluator/`, `tests/unit/scoring/` |
| F8 | Results UI (matrix, scores, artifacts) | ✅ | `web/app/runs/`, `api/routers/results.py` | `tests/unit/api/test_results.py` |
| F9 | Run analytics (per-sandbox metrics + cost) | ✅ | `api/routers/analytics.py`, `api/pricing.py`, `web/app/runs/[id]/results/AnalyticsSection.tsx` | `tests/unit/api/test_analytics.py` |
| F10 | Multi-harness adapters (Codex, Claude Code, Pi, DeepSeek, Aider) | ✅ | `adapters/common/`, `adapters/{codex,claude_code,pi,deepseek,aider}/`, `orchestrator/adapters.py`, `sandbox/images/*.Dockerfile` | `tests/unit/adapters/`, `tests/integration/test_adapter_images.py` |
| F11 | Skillpack import from GitHub | ✅ | `skillpack_import/`, `api/routers/skillpacks.py`, `web/app/submit/ImportSkillPackDialog.tsx` | `tests/unit/api/test_skillpacks.py`, `test_skillpack_import.py` |
| F12 | Artifact preview (static html + desktop GUI via noVNC + python-server web apps, locked-down containers; crashed apps surface exit_code/detail/log tail and an early spawn crash → 502) | ✅ | `sandbox/preview.py`, `api/routers/preview.py`, `sandbox/images/desktop-preview.Dockerfile`, `sandbox/images/server-preview.Dockerfile`, `web/app/runs/[id]/results/PreviewControls.tsx` | `tests/unit/sandbox/test_preview.py`, `tests/unit/api/test_preview.py`, `tests/integration/test_preview.py` |
| F13 | Graphical analysis page + cross-run trends (recharts) | ✅ | `web/app/runs/[id]/analysis/`, `api/routers/analytics.py` (`GET /api/analytics/runs`), `web/app/runs/[id]/results/ResultsClient.tsx` (totals row) | `tests/unit/api/test_analytics.py` (trends section) |
| F14 | Eval iteration (re-score + versioned rubrics) | ✅ | `api/routers/results.py`, `orchestrator/lifecycle.py` (`rescore_run`), `orchestrator/rubric.py` (`create_rubric_version`), `web/app/runs/[id]/results/RescoreControls.tsx` | `tests/unit/orchestrator/test_rescore.py`, `tests/unit/api/test_results.py`, `tests/unit/evaluator/test_evaluate.py` |
| F15 | Submit UX: live model picker + install, named harness configs | ✅ | `api/routers/models.py`, `gateway/main.py` (`/v1/models/pull`), `web/app/submit/page.tsx`, `db/migrations/versions/a2b3c4d5e6f7_sandbox_name.py`, `orchestrator/provision.py` | `tests/unit/api/test_models.py`, `tests/unit/gateway/test_proxy.py` (pull), `tests/unit/db/test_migrations.py`, `tests/unit/orchestrator/test_provision.py` (name) |
| F16 | Model-vs-model matrix (config × model) + per-model analytics | ✅ | `orchestrator/matrix.py`, `orchestrator/lifecycle.py`, `orchestrator/provision.py`, `api/schemas.py`, `api/routers/runs.py`, `results.py`, `analytics.py`, `web/app/submit/page.tsx`, `web/lib/api.ts`, `web/app/runs/[id]/` (detail, results, analysis) | `tests/unit/orchestrator/test_matrix.py`, `test_lifecycle.py`, `test_provision.py`, `tests/unit/api/test_runs.py`, `test_results.py`, `test_analytics.py` |
| F17 | Artifact thumbnails (headless screenshots via the preview stack; desktop captures wait on real VNC framebuffer content, dead-app fast-fail with exit reason) | ✅ | `sandbox/thumbnails.py`, `sandbox/images/thumbnail-capture.{Dockerfile,sh}`, `sandbox/images/thumbnail-vnc.py`, `api/routers/preview.py` (`.../thumbnail`), `api/routers/results.py` (`SandboxResult.thumbnail`), `web/app/runs/[id]/results/PreviewControls.tsx` | `tests/unit/sandbox/test_thumbnails.py`, `tests/unit/api/test_thumbnails.py`, `tests/integration/test_thumbnail.py` |
| F18 | Prompt-only matrix cells (no-sandbox `prompt` adapter: one metered LLM call per cell, `output.txt` artifact, full results/eval pipeline) | ✅ | `orchestrator/prompt_cell.py`, `orchestrator/adapters.py` (pseudo-manifest), `orchestrator/lifecycle.py`, `evaluator/evaluate.py` (`prompt_output_path`), `web/app/runs/[id]/results/` (inline output view) | `tests/unit/orchestrator/test_prompt_cell.py`, `test_lifecycle.py` (prompt cases), `tests/unit/api/test_results.py` (prompt-cell cases) |
| F19 | Artifact file browser + side-by-side diff (jailed listing/read endpoints; prompt cells browse the artifact root so `output.txt` stays reachable; server-side unified diffs) | ✅ | `api/routers/results.py` (`/api/sandboxes/{id}/files[/{path}]`), `web/app/runs/[id]/results/FileBrowser.tsx`, `web/lib/fileBrowser.ts`, `web/lib/api.ts` | `tests/unit/api/test_results.py` (PR34 section), `web/lib/fileBrowser.test.ts` |
| F20 | Trials per cell + per-config model options (config × model × trials expansion, `mean ± sample sd (n=N)` grouped results + cell rollups, adapter-filtered `model_options`) | ✅ | `api/schemas.py`, `api/routers/{runs,results,analytics}.py`, `orchestrator/{matrix,lifecycle,provision,prompt_cell,adapters}.py`, `adapters/common/contract.py`, `db/migrations/versions/d7e9f1a3b5c7_runs_trials.py`, `web/lib/{api,trialGroups,resubmit}.ts`, `web/app/submit/page.tsx`, `web/app/runs/[id]/results/`, `web/app/runs/[id]/analysis/` | `tests/unit/orchestrator/test_matrix.py`, `test_provision.py`, `tests/unit/api/test_runs.py`, `test_analytics.py`, `tests/unit/db/test_migrations.py` |
| F21 | Budgets & pricing accuracy (per-cell `sandbox_max_tokens`/`max_cost_usd` caps; `model_prices` glob rate table layered over the static `PRICE_TABLE` via `GET/PUT/DELETE /api/prices` + OpenRouter sync) | ✅ | `api/pricing.py` (`PriceBook`), `api/routers/prices.py`, `db/models.py::ModelPriceRow`, `gateway/creds.py`, `gateway/metering.py`, `orchestrator/monitor.py`, `web/components/AddRate.tsx`, `web/lib/api.ts` | `tests/unit/api/test_pricing.py`, `test_prices.py`, `tests/unit/gateway/` (creds/metering/proxy), `tests/unit/orchestrator/` (monitor/provision), `tests/unit/db/test_migrations.py` |

## Feature details

### F0 — Dev environment & scaffold ✅

- **What:** repo layout, docker-compose (Postgres 16 on host port **5433**,
  api on **8001** — offset to avoid conflicts with other local projects),
  Alembic base schema (`runs`, `sandboxes`, `events`, `rubrics`, `scores`,
  `artifacts`), pytest + ruff scaffold, Next.js 16 app, GitHub Actions CI
  (backend lint+pytest w/ Postgres service, web lint+build, Playwright e2e).
- **Where:** `docker-compose.yml`, `Dockerfile`, `db/migrations/`,
  `pyproject.toml`, `.github/workflows/ci.yml`, `web/playwright.config.ts`,
  `web/e2e/smoke.spec.ts`, `tests/`
- **Theme:** `web/app/globals.css` — dark "instrument panel" tokens
  (near-black surfaces, hairline borders, signal-lime `--accent`, mono
  kickers, square badges, per-event-type colors via `.tl-type[data-type]`).
  The `/` landing page is a marketing-style long scroll:
  `web/app/landing.css` + `web/components/landing/*` (scripted run-console
  replay in `web/lib/sim.ts`), plain CSS, no framework.
- **Verify:**
  - `docker compose up -d` → `curl localhost:8001/health` + `/health/db` ok
  - `DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5433/harness_eval alembic upgrade head` → all tables present
  - `uv run pytest` + `uv run ruff check .` green
  - `cd web && npm run lint && npm run build` green
  - `cd web && E2E_PORT=3100 npm run test:e2e` → smoke passes (3100 avoids
    local port-3000 conflict; CI uses 3000)
  - CI: push triggers `.github/workflows/ci.yml` — 3 jobs must go green
- **Depends on:** —

### F1 — Submission form → run spec

- **What:** form captures task prompt, model, harness configs (adapter +
  skill pack selections — six adapters available, F10), evaluator model;
  `POST /api/runs` persists spec with status `submitted`. The adapter
  dropdown is populated live from `GET /api/harnesses` (static fallback list
  when the API is unreachable); model inputs use the ComboInput suggestion
  dropdown. Submit validation rejects blank task prompts/models (422) and
  resolves each selected model against the provider catalog the gateway
  would route it to (`api/modelcheck.py`, 60s per-model cache) — absent
  models land as advisory `warnings` on the 201, a failed catalog fetch
  skips validation entirely (offline tolerance), and a provider-reported
  `tools`-capability mismatch under a tool-call harness is a hard 422
  (participant + evaluator models, incl. per-config pins).
- **Where:** `web/app/submit/page.tsx` + form components,
  `web/components/ComboInput.tsx` (model suggestion dropdown),
  `web/app/globals.css` (`.combo*`),
  `web/lib/api.ts` (`listHarnesses`, `HarnessManifest`, `ADAPTERS`),
  `api/routers/runs.py`, `api/routers/harnesses.py`, `api/schemas.py`,
  `api/modelcheck.py`, db `runs` table
- **Verify:**
  - UI: fill form → submit → run appears with status `submitted`
  - API: `POST /api/runs` → 201 + run id; invalid spec → 422; unknown
    model → 201 + `warnings`, unreachable catalog → 201, no-`tools`
    model under a tool harness → 422
  - Tests: `tests/unit/api/test_runs.py` (incl. `test_create_run_multi_adapter`),
    `tests/unit/api/test_harnesses.py`, `tests/unit/api/test_modelcheck.py`
- **Depends on:** F0

### F2 — Orchestrator ✅

- **What:** run state machine (`submitted → rubric_review → [queued →]
  provisioning → running → evaluating → done | failed | cancelled`);
  matrix expansion
  (harness configs × model); rubric draft generated via gateway → frozen on
  user approval; termination policy watcher (timeout, budget, stuck-loop,
  done signal). `DELETE /api/runs/{id}` cancels pre-spawn runs
  (`submitted`/`rubric_review`/`queued` → `cancelled`; a pending rubric
  task is cancelled and a freed `queued` position promotes the next
  parked run), hard-deletes terminal runs (bulk row removal + best-effort
  artifact-dir cleanup), and 409s in-flight runs (sandbox teardown out of
  scope); lifecycle/provisioning bail on a vanished/cancelled run.
  Rubric drafting is fault-tolerant — bounded retries
  (`RUBRIC_GEN_RETRIES`, default 2) cover transient upstream errors +
  validation failures (error fed back into the prompt), and lenient
  normalization coerces scalar `generated_by` + numeric-string fields.
  Drafting is serialized process-wide (`RUBRIC_GEN_CONCURRENCY`, default
  1) so parallel submits can't thrash a single-model provider; a run
  parked on the semaphore stays `submitted` (pre-queue) and emits a
  `rubric_queued` checkpoint. The draft call's per-attempt HTTP timeout
  (`RUBRIC_GEN_TIMEOUT_SECONDS`, default 960) out-waits the gateway's
  worst-case upstream hold (~915s) so a congested draft can't abandon a
  call its retry would then overlap.
- **Per-cell budgets (PR28):** the monitor enforces the run spec's
  optional `sandbox_token_budget` (sandbox's own metered total) and
  `max_cost_usd` (its estimated spend at the layered price table —
  priced models only) per poll alongside the run ceiling — a prompt
  cell finishes before the poll could run, so `prompt_cell` applies the
  same checks inline post-call; each trips
  `budget_exceeded` with a terminate-checkpoint `limit` of
  `sandbox_tokens`/`cost` (`run_tokens` for the shared ceiling).
- **Pacing (PR15):** one active run at a time — approvals while a run
  executes (or is queued) park in `queued` and promote FIFO by approval
  (`pacing.place_run` / `claim_next_queued`; promotion is wired into
  `runner._lifecycle_then_promote` + re-kicked by `startup_sweep`). Spawn
  memory gate: `spawn_one` waits for `SPAWN_MIN_FREE_MEM_MB` host headroom
  before `docker run` (probed via `/proc/meminfo` → `sysconf` → `docker
  info`+`stats`), capped by `SPAWN_MEM_WAIT_SECONDS`, expiry fails that
  sandbox — remaining configs then fail fast (one probe each) so pressure
  can't hold the slot for N×cap. Pacing emits `checkpoint` events keyed by
  `payload.stage`: `run_queued`/`run_dequeued` (run scope),
  `spawn_mem_wait`/`spawn_mem_release`/`spawn_mem_timeout` (sandbox scope).
- **Where:** `orchestrator/` (`state_machine.py`, `lifecycle.py`,
  `provision.py`, `monitor.py`, `rubric.py`, `runner.py`, `pacing.py`,
  `events.py`, `adapters.py`), db `runs`/`sandboxes`/`rubrics`,
  `api/routers/runs.py` (`DELETE /api/runs/{id}`)
- **Verify:**
  - Unit: `tests/unit/orchestrator/` (`test_state_machine.py`,
    `test_lifecycle.py`, `test_monitor.py`, `test_provision.py`,
    `test_rubric.py`, `test_pacing.py`), queue E2E + cancel/delete in
    `tests/unit/api/test_runs.py` (`test_approve_queues_behind_active_run`,
    `test_delete_queued_run_releases_slot`, `test_delete_inflight_run_409`,
    `test_delete_done_run_hard_deletes`)
  - Manual: submit → edit/approve rubric → sandboxes spawn → terminal states
    recorded correctly; approve a second run mid-flight → `queued` badge →
    promotes when the first finishes
- **Depends on:** F3, F4, F5

### F3 — AI gateway ✅

- **What:** OpenAI-compatible `/v1/chat/completions` endpoint → configurable
  upstream (Ollama default) — non-streaming by default, metered SSE pass-through when the
  caller sets `stream: true` (pi and other OpenAI-compatible CLIs stream
  unconditionally); Anthropic Messages `/v1/messages` +
  `/v1/messages/count_tokens` (Claude Code, dsh) and OpenAI Responses
  `/v1/responses` (Codex) pass-throughs, streaming preserved with a metering
  tee; `GET /v1/models` for CLI probes; per-run/sandbox HMAC credentials
  (`heg.<b64>.<sig>`); records tokens in/out, latency, failures; token
  budgets (429 + `error` event) — the cred's `max_tokens` run ceiling plus,
  since PR30, every metered `llm_response` also carries per-call speed:
  `ttft_ms` (first response byte — first content chunk for streams, first
  body byte for buffered reads), `total_ms` (same value as `latency_ms`),
  and derived `tokens_per_sec` (completion ÷ call wall seconds; null on
  cache replays — a replay times the store, not the model),
  since PR28, a `sandbox_max_tokens` per-cell claim metered against only
  that sandbox's own usage; emits `llm_request`/`llm_response`/`error`
  events carrying the resolved upstream provider — every metered call is
  terminally accounted, so a client disconnect mid-stream or while the
  upstream request is in flight still lands `llm_response` (partial /
  `usage_estimated`) plus `error` `stage="client_disconnected"` under a
  shielded cancel scope; upstream calls bound by
  `UPSTREAM_TIMEOUT_SECONDS` (default 300). `/internal/events`
  (harness-event proxy, cred or ingest-token auth) + `/internal/done`
  (done_signal) for sandbox egress.
- **Upstreams:** `UPSTREAM_PROVIDER` picks the default provider —
  `ollama` (default), `openrouter`, `openai`, `anthropic` — and
  `PROVIDER_ROUTES` (JSON `{"model-glob": provider}`) overrides it per
  request model (first match wins, case-insensitive); an `x-heg-provider`
  request header forces the provider outright on service creds
  (`sandbox_id=None`; unknown/unkeyed values 400, ignored on
  sandbox-scoped creds). Each
  provider gets its own key/base-URL/headers (Anthropic sends `x-api-key` +
  `anthropic-version`; caller `anthropic-beta`/`anthropic-version` headers
  forward to the anthropic provider); endpoints a provider doesn't
  implement return its native 404; Ollama-only body params are stripped
  for hosted providers.
- **Response cache / deterministic replay (PR18):** opt-in via
  `GATEWAY_CACHE_ENABLED` (OFF by default — real runs hit the live
  upstream). Metered generation calls (`/v1/chat/completions` streamed +
  buffered, `/v1/messages`, `/v1/responses`) replay from a sqlite store
  (`GATEWAY_CACHE_PATH`, default `./data/gateway_cache.sqlite3`,
  `GATEWAY_CACHE_MAX_ENTRIES` LRU bound) keyed on
  `namespace + path + provider + outgoing-body` (post-transform — stream vs
  buffered and per-provider routes can't collide; bump
  `GATEWAY_CACHE_NAMESPACE` to invalidate). Hits skip the upstream, return
  an `x-heg-cache: hit` header, and still emit `llm_request`/`llm_response`
  with `payload.cached: true` + the stored body re-metered for usage —
  cached spend counts toward the run budget and the budget gate runs before
  the lookup, so a replay 429s at the same point the live run did. Only
  complete 2xx responses are stored; `count_tokens`/`/v1/models` bypass the
  cache; cache failures degrade to a miss (never a proxy 5xx).
- **Where:** `gateway/` (`main.py`, `cache.py`, `creds.py`, `metering.py`,
  `deps.py`, `config.py`, `mint_cred.py` CLI), compose service `gateway`
  (:8081→8080)
- **Verify:**
  - Unit: `tests/unit/gateway/` (creds, metering, proxy — hermetic; protocol
    pass-throughs + SSE usage normalization covered; stream retry + client-
    abandonment terminal events + upstream timeout wiring covered),
    `test_upstream_provider.py` (provider selection, key validation,
    PROVIDER_ROUTES resolution, `x-heg-provider` override, per-provider
    client base/headers, ollama-param stripping, anthropic protocol-header
    forwarding, provider attribution on emitted events),
    `test_cache.py` (key stability/provider+body+path keying, LRU bound,
    file persistence, namespace invalidation, miss→hit replay for buffered +
    SSE + /v1/messages + /v1/responses, cached-event flagging + usage
    metering, budget gate before lookup, non-2xx and unmetered bypass,
    lookup/store failure degradation),
    `test_speed_metrics.py` (PR30 ttft_ms/total_ms/tokens_per_sec on
    buffered + streamed llm_response payloads, first-byte shim, replay →
    null rate)
  - Manual: mint cred (`uv run python -m gateway.mint_cred`) →
    `curl localhost:8081/v1/chat/completions` → completion + event rows
  - M2 e2e proven: sandbox→gateway→Ollama→events (see run
    `a58dd3de-1b71-4c4e-9a06-ad5d2ce5210e` in dev DB); P2.5 live e2e: real
    `/v1/responses` (Codex), `/v1/messages` (Claude), streamed
    `/v1/chat/completions` (Pi) through the gateway to Ollama
- **Depends on:** F0 + F6 ingestion endpoint

### F4 — Sandbox runtime ✅

- **What:** spawn one Docker container per sandbox on internal network
  `heg-sandbox` (`--internal`, egress locked — proven: external DNS fails);
  gateway attached with alias `gateway` → `http://gateway:8080` is the only
  egress; `./skillpacks` bind-mounted ro at `/opt/skillpacks`; `done` CLI on
  PATH → `POST /internal/done`; workspace bind-mount; snapshot →
  `data/artifacts/<run>/<sandbox>/{workspace/,transcript.log,meta.json}`.
- **Where:** `sandbox/` (`runtime.py`, `spec.py`, `images/miniloop.Dockerfile`,
  `images/done`, `images/testapp/`), `data/artifacts/`, `data/sandboxes/`
- **Verify:**
  - Integration: `tests/integration/test_sandbox.py` (`@pytest.mark.docker`,
    6 pass 1 skip) — incl. egress-lockdown proof
  - M2 e2e proven: real miniloop sandbox completed task + artifact
- **Depends on:** F0; integrates with F3, F5

### F5 — Miniloop harness + adapter + skill packs ✅

- **What:** custom minimal coding agent — tool-call loop over the gateway
  (tools: `shell`, `read_file`, `write_file`, `use_skill`, `done`); default
  model `qwen3:8b` via Ollama; JSONL harness events on stdout + HTTP ingest;
  `MINILOOP_RESULT` terminator line; skill packs vendored under `skillpacks/`
  (51 SKILL.md: superpowers 14, mattpocock-skills 28, spec-kit 9 generated);
  progressive-disclosure catalog in system prompt. Tool-less assistant
  replies get `MINILOOP_NUDGE_RETRIES` corrective nudges (default 2,
  `stage="agent_nudge"` checkpoints) before counting toward the text-only
  `stuck_loop` streak. Per model-call timeout
  `MINILOOP_LLM_TIMEOUT_SECONDS` (default 960 — must exceed the gateway's
  worst-case call hold, (1 + `UPSTREAM_RETRY_ATTEMPTS`) ×
  `UPSTREAM_TIMEOUT_SECONDS` + retry backoff, so calls aren't abandoned
  mid-spend).
- **Where:** `adapters/miniloop/` (`agent.py`, `tools.py`, `events.py`,
  `skills.py`, `prompts.py`, `adapter.py`, `__main__.py`), `skillpacks/`
- **Verify:**
  - Unit: `tests/unit/adapters/miniloop/` (64 tests — loop, tools, events,
    skills, adapter)
  - Live: `python -m adapters.miniloop` vs host Ollama → agent_done ✓
  - M2 e2e proven: in-sandbox run, 51 skills discovered, done_signal emitted
- **Depends on:** F3, F4

### F6 — Event store + live timeline ✅

- **What:** append-only `events` table + ingestion API + query API + SSE
  stream; UI timeline page — live during run, replayable after with
  timestamps. API side (`api/routers/events.py`: ingest batch/single,
  query w/ filters+pagination, SSE tail) + timeline UI both landed.
  **Split view (PR10):** a `merged | split` toggle in the toolbar switches
  the stream to two sandbox lanes — side-by-side, time-aligned event
  streams for comparison. Design: ONE scroll container renders a merged
  `(ts, id)` ordering of both lanes' events as two-column rows (an event
  fills only its lane's cell; the other stays empty), so synchronized
  scroll + time alignment are structural — no scroll mirroring. A sticky
  lane header holds per-lane sandbox pickers (labels from analytics,
  status badge, per-lane event count); picking the id already in the
  other lane swaps them. Run-level (`sandbox_id` null) and non-lane
  sandbox events render as full-width shared rows. Lanes seed from
  `?view=split&left=&right=` URL params, else auto-default to the first
  two discovered sandboxes (never overwriting an explicit pick); the
  sandbox filter dropdown is merged-only — in split mode the PR9 metrics
  chips assign lanes instead (occupant clears, new id fills first empty
  lane else replaces right). Row rendering is shared via `EventRow.tsx`
  (same `.tl-*` markup + per-type colors as merged).
- **Where:** `api/routers/events.py`, `api/schemas.py` (EventSource/EventType
  enums), db `events`, `web/app/runs/[id]/timeline/` (`TimelineClient.tsx`,
  `SandboxMetricsBar.tsx` — PR9 strip, `SplitTimeline.tsx` + `EventRow.tsx`
  — PR10 split view, see F9), `web/lib/splitTimeline.ts` (`buildSplitRows`
  merged-ordering transform, `pickLane`/`toggleLane`/`laneEventCounts`)
  + `splitTimeline.test.ts`
- **Run-detail console:** `web/components/RunConsoleLive.tsx` — per-sandbox
  event lanes on the run detail page (backlog + SSE + `after_id` poll, same
  merge as the timeline; evaluator/orchestrator traffic routed to a bottom
  lane). Shares the `evc-*` stylesheet in `globals.css` with the landing
  replay console.
- **Verify:**
  - Unit: `tests/unit/api/test_events.py` + `test_event_types.py` (13+ tests);
    `cd web && npm test` — `splitTimeline.test.ts` covers the merged
    `(ts,id)` ordering (incl. the mixed fractional-seconds ts edge),
    day markers, shared rows, lane counts, and all lane-selection rules
  - Live: SSE verified via curl; M2 events persisted
  - UI: run → timeline page shows live events; `split` toggle → two lanes
    scroll in lockstep on one time axis, lane pickers swap on collision,
    run-level rows span both columns; run detail shows live lanes
- **Depends on:** F0 (ingestion slice unblocks F3)

### F7 — Evaluator sandbox ✅

- **What:** post-run sandboxed agent (same miniloop image as judge); loads
  staged artifact copy + frozen rubric; judges via gateway (metered,
  per-sandbox cred); writes score + rationale per rubric dimension
  (functional requirements, acceptance criteria, code quality, cyclomatic
  complexity); `scores.json` parsed into `Score` rows with salvage-mode
  parsing (incl. flat `{dimension: score}` normalization); `EVAL_TRANSCRIPT.log`
  written for evidence. Bounded retry: a judge run that produces zero Score
  rows is re-spawned up to `EVAL_SCORE_RETRIES` times (default 1) with a fresh
  cred, emitting an `eval_retry` checkpoint — covers small-model flake modes
  (malformed JSON, stuck-loop wander); infra exceptions are not retried.
  Exhausted attempts leave a final `error` event (`stage="evaluation"`,
  `phase="eval_failed"`) which the results API surfaces as per-sandbox
  `eval_error` — distinguishable from "not yet scored" (F8). Judge per-request
  output cap is `EVAL_JUDGE_MAX_TOKENS` (default 8192; thinking judges).
  Judge-output reliability: retries feed the last `eval_scores_failed`
  detail back via `prior_error` in EVAL_PROMPT.md; `parse_scores_json`
  repairs single-quoted Python-literal pseudo-JSON (`ast.literal_eval`); a
  control-plane direct-completion fallback (`evaluator/judge_direct.py`,
  `response_format=json_object`, `EVAL_DIRECT_MAX_TOKENS` 4096,
  `EVAL_DIRECT_FALLBACK=0` disables) writes scores back and lands through
  the normal path with `eval_scored` `mode="direct_fallback"` (failures add
  `fallback_detail` to `eval_scores_failed`).
  **Objective signals (PR6+PR7):** before staging, the candidate workspace
  is measured by the `scoring` package's signal collectors —
  `static_analysis` runs lizard (multi-language, parse-only, no code
  execution) for real cyclomatic complexity, nloc, function counts,
  nesting depth, and language mix; `exec` runs the workspace's
  build/tests/coverage inside a locked `heg-exec` container
  (`--network none`, cap-drop ALL + cap-add SETUID/SETGID — the in-image
  runner is uid 0 only to demote every candidate phase to nobody, keeping
  the `/out` result channel and container stdout unforgeable —
  no-new-privileges, cpu/memory/pids bounds, tmpfs-capped scratch,
  artifact mounted read-only at `/srv/src` and copied to container-local
  `/work` — the artifact is never written).
  The report is staged into the eval workspace as `ANALYSIS.md`
  (judge-facing brief incl. the executed-checks block; judge is told to
  score `cyclomatic_complexity` from measured values and to treat
  build/test/coverage as ground truth for functional/acceptance dims) +
  `analysis.json`, persisted at `<artifact>/analysis.json`, summarized in
  an `eval_analysis` checkpoint event (plus a `test_execution` event when
  candidate tests ran), and re-validated + embedded as `analysis` + flat
  `exec` ExecSummary on `GET /api/runs/{id}/results` sandboxes.
  Candidate-authored `ANALYSIS.md`/`analysis.json` files are dropped at
  staging (reserved names — fake measured evidence can't reach the judge).
  Collection never blocks evaluation: collector failures degrade to
  `status="error"`/`skipped` signal entries / `status="unavailable"`
  event. Collector registry (`scoring/collectors.py::COLLECTORS`) is the
  extension point for further signal types; `SCORING_EXEC=0` disables the
  exec collector (conftest pins it for unit tests).
  **Generated test suite (PR8):** a third `generated_tests` signal is
  appended by the evaluator itself (`evaluator/testgen.py`, not a
  registry collector — it needs gateway creds + the artifact dir).
  Before staging, the evaluator calls the model through the gateway on
  the per-eval cred (metered/budgeted like the judge; draws on the same
  `EVAL_TOKEN_BUDGET` allowance) with the rubric + budget-capped source
  listing (`build_testgen_prompt` in `prompts.py`) → parses output into
  `test_*.py` files via `### FILE:` markers (deterministic containment:
  every file must `compile()` AND define a `test_*`; rejected chunks are
  reported, a fully unusable generation → `status="skipped"`) → persists
  the suite + `_manifest.json` to `<artifact>/generated_tests/`
  (provenance — never the candidate's `workspace/`) → runs it via
  `ExecRunner.execute(workspace, test_target=…)` — PR8's
  `EXEC_TEST_TARGET` contract: the dir mounts read-only at
  `/srv/test_target`, exec_runner copies it to `/tmp/gen_tests`, pytest
  runs there, coverage stays `--source=/work` and the build phase never
  compiles generated files (a broken generated test can't be attributed
  to the candidate's build). JUnit parsing now also captures failing
  test names (`failed_tests`). The signal lands in the same envelope —
  `ANALYSIS.md` "Generated test suite" section + `analysis.json` +
  `eval_analysis` summary + `test_execution` `phase=eval_testgen` event
  + flat `generated_tests` summary on the results API — and the suite is
  staged into the eval workspace for the judge (a candidate-authored
  `generated_tests/` is a reserved name, evicted at staging).
  `SCORING_TESTGEN=0` disables (conftest pins it); knobs:
  `TESTGEN_MODEL`, `TESTGEN_SOURCE_BUDGET_BYTES` (24k),
  `TESTGEN_MAX_FILES` (8), `TESTGEN_MAX_TOKENS` (4096),
  `TESTGEN_TIMEOUT_SECONDS` (960, the one raised-cap retry gets up to 2×,
  clamped to the eval-attempt deadline),
  `TESTGEN_MAX_TOKENS_RETRY` (8192) — retries cover empty content,
  zero-runnable-file output, and timeouts. A scores doc missing
  `overall` gets it synthesized as the mean of dimension scores +
  warning (`parse_scores_json`).
- **Where:** `evaluator/` (`evaluate.py`, `sandbox_spec.py`, `scores.py`,
  `prompts.py`, `judge_direct.py` — direct-completion fallback,
  `testgen.py` — PR8 suite generation + exec orchestration),
  `scoring/` (`models.py` — analysis.json/API pydantic
  contract, `collectors.py` — SignalCollector protocol + registry,
  `static.py` — lizard collector, `exec.py` — exec collector,
  `render.py` — ANALYSIS.md), `sandbox/exec.py` (ExecRunner — docker
  lifecycle + result parsing + `test_target` mount) +
  `sandbox/images/exec.Dockerfile` + `exec_runner.py` (in-image
  build/test/coverage phases + `EXEC_TEST_TARGET` mode), db `scores`,
  `api/routers/results.py` (`SandboxResult.analysis` + `.exec` +
  `.generated_tests`), `api/schemas.py` (AnalysisReport re-export)
- **Verify:**
  - Unit: `tests/unit/evaluator/` (`test_evaluate.py`, `test_scores.py`,
    `test_prompts.py`) — mocked runtime; staging, timeout salvage, malformed
    scores covered; `tests/unit/evaluator/test_judge_direct.py` — fake
    gateway client: request contract, 4xx response_format retry, bounded
    prompt gather, error mapping; `tests/unit/evaluator/test_testgen.py` — fake gateway
    client + fake exec runner: skip/error semantics, deterministic
    containment of bad output (fences/markers/syntax/no-test rejection),
    provenance layout, pass-rate metrics, cap knobs;
    `tests/unit/scoring/` (`test_static.py`,
    `test_collectors.py`, `test_render.py`, `test_exec.py`) — workspaces
    with known CC values, skip/cap/symlink rules, collector isolation,
    markdown render, exec metric mapping/status semantics;
    `tests/unit/sandbox/test_exec.py` — exec run-argv isolation posture
    (incl. `test_target` mount/env), result file/sentinel parsing,
    timeout/missing-image paths; `tests/unit/sandbox/test_exec_runner.py`
    — in-image helpers (junit failed names, target staging, discovery)
  - Exec integration: `tests/integration/test_exec_scoring.py`
    (`@pytest.mark.docker`) — real `heg-exec` container executes a trivial
    artifact: build ok, 2/2 tests pass, coverage measured, container
    force-removed, workspace untouched; PR8 cases run an external
    `generated_tests` dir via `test_target` (pass + red suite w/ failing
    names, mount stays byte-identical)
  - Results API: `tests/unit/api/test_results.py` — analysis embedded when
    `<artifact>/analysis.json` exists+valid, null otherwise
  - Manual: scores + rationale visible on results page; rationale for
    cyclomatic_complexity cites measured CC values; build/test failures
    visible in analysis + eval events
- **Depends on:** F2, F4, artifacts from a completed run

### F8 — Results UI ✅

- **What:** run list; per-run matrix (sandbox × rubric dimension); score +
  rationale view; link to timeline replay; artifact download; flag when
  evaluator model == any participant model. The run-detail rubric table also
  renders per-sandbox score columns + an `overall` row once results exist
  (`RunDetailClient.tsx` `RubricView` + `getRunResults` poll while
  `evaluating`). Sandboxes whose eval failed surface an `eval failed` badge
  (from `SandboxResult.eval_error`) instead of blank score cells, and a
  file-less workspace lights an `empty workspace` badge
  (`SandboxResult.empty_workspace`, sourced from the stored
  `sandbox_finalized`/`empty_workspace` checkpoint payloads — no FS
  re-probe). Directory
  artifact downloads ship as ZIPs that filter `.git`, `__pycache__`,
  `*.pyc`, `node_modules`, `.venv` at any depth (download-side only — the
  raw workspace on disk is untouched). **PR21:** the runs list paginates
  via `?offset`/`?limit` (50-row pages + "Load more", poll refetches the
  loaded head window), and run-detail action controls mirror DELETE
  semantics — "Cancel run" renders only for `submitted`/`rubric_review`/
  `queued`, terminal runs get "Delete run" (hard delete), in-flight
  statuses show neither.
- **Where:** `web/app/runs/` (list, detail, `results/`), results components,
  `api/routers/results.py` (results + scores + artifact-download endpoints)
- **Verify:**
  - UI: complete run → compare scores side-by-side → open timeline →
    download artifact
  - Unit: `tests/unit/api/test_results.py` (matrix, scores, download jail,
    `test_download_directory_zip_filters_noise`);
    e2e: `tests/e2e/` (Playwright, post-MVP)
- **Depends on:** F1, F6, F7

### F9 — Run analytics ✅

- **What:** `GET /api/runs/{id}/analytics?price_model=` — per-sandbox metrics
  aggregated from the `events` stream: llm turns, token split
  (in/out/cache/total), latency (avg/p95/max), PR30 speed — `ttft_ms`
  (avg/p95/max first-byte stats) + `avg_tokens_per_sec` (mean per-call
  output rate; events without the gateway's speed fields contribute no
  sample, and every timing stat skips `cached` replays — their timings
  measure the store's serve time, not the model) — tool-call +
  skill-invocation
  breakdowns, shell/file/test/error counts, wall clock, estimated cost at a
  selectable price model (`api/pricing.py` `PRICE_TABLE`; local models
  flagged `local` → $0). The tool-call mix merges `tool_call` events,
  hook-sourced `tool_result` outcomes (per-name max — a call producing
  both records counts once), and `shell_command`/`file_op`/`test_execution`
  as proxies when a lane has no finer signal, so CLI adapters report a
  non-zero mix; `empty_workspace` surfaces the monitor's file-less
  workspace flag on the sandbox row. Judge (evaluator) traffic shares the
  participant
  sandbox_id via its cred, so it's split out at the `eval_started` checkpoint
  and reported under `evaluator` rather than polluting participant numbers.
  Results page gains an "Analytics" section — side-by-side comparison table +
  price-model dropdown that recomputes cost client-side (no refetch).
  Timeline page (PR9) hosts the same numbers in-context: a per-sandbox
  metrics strip between the filter toolbar and the event stream — compact
  chips (turns · tokens in→out · avg latency · tok/s speed · est. cost ·
  errors, plus a judge-traffic line once the evaluator runs; the `spd`
  chip carries ttft detail on hover — PR30). Chips double as sandbox
  selectors: merged mode toggles the sandbox filter (same state as the
  toolbar dropdown, whose options gain adapter labels); split mode (PR10)
  assigns the sandbox to a comparison lane instead. The strip refreshes
  every 15s while the run is non-terminal. **PR21 cost honesty:** models
  absent from `PRICE_TABLE` are *unpriced* — `price_model`/
  `estimated_cost_usd`/`evaluator_cost_usd` come back null rather than
  billed at the first table entry's rate; an explicit `?price_model=`
  still reprices uniformly (unknown key → 422). Every UI cost surface
  renders "unpriced", never $0 or a stand-in model name. **PR28:** price
  resolution now runs through the layered `PriceBook` — `model_prices`
  glob rows (declared via `PUT /api/prices` or OpenRouter sync) match
  first and shadow the static table; a glob is itself a valid
  `?price_model=` pick, and cache-read tokens bill at a declared cache
  rate instead of the input rate. The picker's "+ add rate" control PUTs
  a row inline and folds it into the picker table on the spot (F21).
- **Where:** `api/pricing.py`, `api/routers/analytics.py`, response schemas
  in `api/schemas.py`, `web/app/runs/[id]/results/AnalyticsSection.tsx`,
  `web/lib/api.ts` (`getRunAnalytics`, `estimateCostUsd`),
  `web/lib/timelineAnalytics.ts` (`sandboxStrips` transform) +
  `timelineAnalytics.test.ts`, `web/lib/format.ts` (`formatNum`/`formatUsd`),
  `web/app/runs/[id]/timeline/SandboxMetricsBar.tsx` + `.tl-metric*` in
  `web/app/globals.css`
- **Verify:**
  - Unit: `tests/unit/api/test_analytics.py` (aggregation math, eval-phase
    split, cost at known price model, defaults, 404/422, zero-event run);
    `cd web && npm test` (node --test — `sandboxStrips` chip transform)
  - API: `curl localhost:8001/api/runs/<id>/analytics?price_model=gpt-4o`
  - UI: results page → "Analytics" card → switch price model → cost updates;
    timeline page → strip shows per-sandbox stats live → click a chip →
    stream filters to that sandbox (merged) / chip assigns a split lane
- **Depends on:** F6 (events), F8 (results page)

### F10 — Multi-harness adapters ✅

- **What:** pluggable adapter registry so runs can compare real harness CLIs,
  not just miniloop configs. Registered adapters: `miniloop`, `codex`
  (@openai/codex), `claude_code` (@anthropic-ai/claude-code), `pi`
  (@mariozechner/pi-coding-agent), `deepseek` (@deepseek-ai/dsh), `aider`
  (aider-chat, PR13). A shared stdlib-only shim (`adapters/common/`) runs
  inside each image: writes the harness's native config pointing at the
  gateway, launches the CLI in machine-readable JSONL mode (aider emits
  plain text only — the shim's `translate_text` hook maps its status lines
  heuristically instead), translates records → normalized events, posts
  them to `/internal/events`, emits `terminal_state` + prints
  `HEG_RESULT {…}`. Per-adapter Docker images pin the CLI version;
  `orchestrator/provision.py` resolves each config's image from
  `image_spec()["image"]` (miniloop keeps the `SANDBOX_IMAGE` fallback).
  `GET /api/harnesses` serves adapter manifests (display name, LLM protocol,
  CLI, image) for the submit form; `HarnessConfigIn.adapter` validates
  against the registry. Design doc: `docs/p25-harness-adapters.md`.
- **Hook/plugin telemetry (PR17):** harnesses with a documented native hook
  surface report higher-fidelity events through the shared receiver
  `adapters/common/hooks.py` (`python3 -m adapters.common.hooks <adapter>
  [json]` — stdin payload for Claude Code hooks and pi's extension
  forwarder, argv for Codex `notify`). Each hooked adapter ships `translate_hook`
  beside `translate_obj` in `translate.py` + a `MANIFEST["hooks"]` line;
  emitted payloads are stamped `via: "hook"`. Coverage: `claude_code`
  settings.json hooks (`PostToolUse` → `tool_result` — outcome detail the
  stream translation drops; `SessionStart/End`, `Stop`, `SubagentStop`,
  `Notification`, `PreCompact` → `checkpoint`), `codex` `notify`
  (`agent-turn-complete` → `checkpoint` with `last-assistant-message`), `pi`
  generated `extensions/heg-events.ts` (`tool_execution_end` →
  `tool_result`). `deepseek`/`aider`/`miniloop` stay translated-only. The
  receiver always exits 0 and mirrors to stderr (stdout can feed back into
  agent context for some claude hook events). Pi tool errors carry detail —
  stderr/message/result text when reported, else a named-tool + exit-code
  fallback — and `tool_result`/coarse events feed the F9 tool-call mix so
  CLI lanes aren't all-zero.
- **Where:** `adapters/common/` (`runner.py`, `contract.py`, `hooks.py`),
  `adapters/{codex,claude_code,pi,deepseek,aider}/` (`adapter.py`, `config.py`,
  `translate.py`, `__main__.py`; aider also `launcher.py` — the patched
  entrypoint that lifts aider's explicit-yes gate on suggested shell
  commands), `orchestrator/adapters.py`,
  `orchestrator/provision.py`, `sandbox/images/{codex,claude_code,pi,
  deepseek,aider}.Dockerfile`, `api/routers/harnesses.py`
- **Verify:**
  - Unit: `tests/unit/adapters/test_adapter_contract.py` (contract across all
    adapters: manifest, image_spec, env, launch, translation → valid
    `EventType`s, `HEG_RESULT` parsing, hook-manifest ↔ `translate_hook`
    pairing) + per-adapter tests under `tests/unit/adapters/<name>/` +
    `tests/unit/adapters/common/test_hooks.py` (receiver: payload parsing,
    via-stamping, ingest POST, exit-0 guarantee)
  - API: `tests/unit/api/test_harnesses.py`, multi-adapter submit in
    `test_runs.py::test_create_run_multi_adapter`
  - Docker: `tests/integration/test_adapter_images.py` (`-m docker`) — CLI
    binary runs + shim always prints `HEG_RESULT`
  - Live e2e proven (isolated gateway+sandbox topology): Codex via
    `/v1/responses` and Claude Code via `/v1/messages` each created the task
    file and exited `harness_complete`; Pi streamed real
    `/v1/chat/completions` traffic through the gateway
- **Depends on:** F3 (protocol endpoints), F4 (sandbox runtime), F5 (pattern)

### F11 — Skillpack import from GitHub ✅

- **What:** users paste a GitHub link — repo root
  (`github.com/owner/repo`), subdirectory (`…/tree/<branch>/<path>`), or a
  direct `SKILL.md` (`…/blob/<branch>/<path>/SKILL.md`, also
  `raw.githubusercontent.com`) — the backend scans a codeload tarball for
  `SKILL.md` files (frontmatter name/description parsed), the user picks a
  subset, and imports via one of three `mode`s: `pack` (all skills into one
  NEW named pack — a taken name returns **409 + structured
  `detail.suggested_name`** for the confirm-and-rename flow), `merge`
  (skills appended into an existing imported pack; vendored targets
  rejected), or `split` (each selected skill becomes its own pack named
  after the skill dir, auto-renamed `-2`/`-3` on collision and reported in
  the response). Imported packs live under
  **`data/skillpacks/<pack>/`** — the data tree, NOT the vendored
  `skillpacks/` (which is committed + baked into the api image; `data/` is
  gitignored and bind-mounted at `/data` in compose). They merge into one
  name namespace with vendored packs everywhere: `GET /api/skillpacks`,
  submit-time `skill_packs` validation, and provisioning staging (selected
  packs copy into the per-sandbox dir mounted at `/opt/skillpacks`).
- **Where:**
  - `skillpack_import/github.py` — `parse_github_url` (accepted shapes),
    `fetch_repo` (codeload tarball; ambiguous slash-refs probed
    longest-first — GitHub's own longest-match; `HEAD` resolves default
    branch), `scan_skills` / `extract_skill_files` (size-capped), sha
    resolution via `api.github.com` `git/ref` + `commits` (best-effort —
    `commit_sha` lands in `PACK.json` provenance and lets imports pin the
    exact tree discovered).
  - `skillpack_import/store.py` — `imported_root(settings)` (=
    `ARTIFACT_ROOT.parent/"skillpacks"`, overridable via
    `IMPORTED_SKILLPACKS_DIR`), `list_packs` (counts via the same
    `SkillCatalog` the sandbox loader uses), `write_pack` (sanitize names,
    per-dir dedup `-2`/`-3`, `PACK.json` manifest, all-or-nothing rollback),
    `delete_pack`.
  - `api/routers/skillpacks.py` — `GET /api/skillpacks`,
    `POST …/discover`, `POST …/import` (201; modes pack/merge/split,
    collision → `{"kind": "name_conflict", "name", "suggested_name",
    "source"}`), `DELETE …/{name}` (imported only; vendored → 409).
    `store.suggest_pack_name` finds the first free `name-2`/`name-3`.
    Fetcher is an injectable FastAPI dep (`get_fetcher`) — tests never
    touch the network.
  - `api/config.py` — `GITHUB_TOKEN` (optional PAT → Bearer on GitHub
    calls; private repos best-effort), `IMPORTED_SKILLPACKS_DIR`.
  - `orchestrator/provision.py` — `known_skillpacks(*roots)` merges roots;
    `stage_skillpacks(..., extra_roots=)` resolves packs vendored-first
    then imported.
  - Web: `web/lib/api.ts` (`listSkillPacks`, `discoverSkillPack`,
    `importSkillPack`, `deleteSkillPack`), submit page fetches the pack
    list dynamically (vendored const is just the fallback), per-config
    "+ import from GitHub" button → `ImportSkillPackDialog` (URL →
    discover → checkbox select → mode radio: new pack / existing imported
    pack / one-pack-per-skill; name collision shows a rename notice
    prefilled with the api's `suggested_name`), modal styles in
    `globals.css`.
- **Verify:**
  - Unit: `tests/unit/api/test_skillpack_import.py` (URL shapes, ref
    probing w/ FakeFetcher, tarball scan/extract, store dedup/merge/
    traversal, multi-root provision) + `tests/unit/api/test_skillpacks.py`
    (endpoints: list/discover/import/delete, error statuses, dedup, and
    imported pack passing `POST /api/runs` validation).
  - API: `POST /api/skillpacks/discover {"url": "https://github.com/anthropics/skills"}`
    → skills; `POST /api/skillpacks/import` → files under `data/skillpacks/<pack>/`.
  - UI: submit → "+ import from GitHub" → paste link → pick skills →
    import → pack shows with an "imported" badge, selectable per config.
- **Depends on:** F5 (pack layout/loader), F4 (staging/mount path)

### F12 — Artifact preview ✅

- **What:** "Preview" button per sandbox on the results page → serves or
  *runs* the snapshot workspace in a throwaway container so the user can
  see/play the artifact in a new browser tab. Types: `"static"` (html),
  `"desktop"` (GUI executed under Xvnc, streamed via noVNC — pygame,
  pyglet, arcade, tkinter), and `"python-server"` (a web app executed by
  a relay runner and served on localhost — flask/fastapi/django/etc.).
  - `GET /api/runs/{run}/results` → per-sandbox `previewable` +
    `preview_type` (cheap FS probe of the jailed workspace).
  - `POST /api/runs/{run}/sandboxes/{sandbox}/preview` → `201
    {preview_id, url, type, status, entry, expires_at}`; idempotent (a live
    preview is returned as-is — unless an explicit `entry`/`type` requests
    a different plan, which stops it and respawns); `404`
    run/sandbox/workspace missing, `422` zero candidates / bad `entry` /
    unsupported type, `502` docker failure. Optional body `{"entry":
    "rel/path"}` picks an entry — jailed to the workspace, must exist and
    be non-symlinked; `.html`/`.htm` → static, `.py` → desktop,
    `manage.py` → python-server (explicit `.py` bypasses framework
    evidence — the pick IS the disambiguation; suffix alone can't tell a
    GUI app from a web server, so a bare `.py` defaults to desktop and
    `type=python-server` runs it as a web app).
    `{"type": "static"|"desktop"|"python-server"}` overrides the pick: alone it selects the
    first detected candidate of that type (a both-matching workspace
    defaults to static — `{"type": "desktop"}` reaches the game); with
    `entry` it forces the plan — `static` serves any jailed file (view a
    `.py`'s source — the entry is only the URL opened), `desktop` and
    `python-server` still require a `.py` entry. No body → the scan's
    default (`candidates[0]`).
  - `GET .../preview` → current state (any status) or `404`; `DELETE
    .../preview` → `204` stop.
- **Detection** (`sandbox/preview.py::scan_preview` → `PreviewScan{plan,
  candidates}`): `candidates` = EVERY runnable entry as typed plans,
  ordered default-first — `plan`/`candidates[0]` is just the dropdown's
  default selection, never a hidden decision. Static entries first
  (root `index.html`/`.htm` → other root `*.html` sorted → `*/index.html`
  one level down sorted — `templates/` skipped: it's the flask/django
  render dir, not a site entry), then desktop `.py` entries, then
  python-server `.py` entries (root + nested ≤3
  levels; hidden dirs, symlinks, `_DESKTOP_SKIP_DIRS` —
  node_modules/__pycache__/tests/venv/dist/build/docs/templates/... —
  skipped; ordered shallower → named-rank → path). Both executed
  detectors share `_py_candidates`: a `.py` is a candidate iff the file
  itself has evidence — a line-anchored import (`pygame`/`pyglet`/
  `arcade`/`tkinter` → desktop; `flask`/`fastapi`/`django`/`aiohttp`/
  `http.server`/… → python-server; comments can't match) — OR it's
  entry-shaped (named entry, or a dir containing exactly one `.py`) AND
  a scanned `requirements.txt` names the stack on a non-comment line
  (desktop req evidence: pygame(-ce)/pyglet/arcade — bare `tk` excluded,
  it's the unrelated Tcl package and tkinter is stdlib; server req
  evidence: the framework names). So a `static+pygame` workspace
  defaults to static yet still offers the desktop entry; helper .py
  files with real imports are offered too, under whichever type(s)
  their evidence supports.
- **UI**: `preview_candidates` (typed `{entry,type}`) rides
  `SandboxResult` → the dropdown lists each candidate plus type-override
  variants (`index.html (site)` / `game/main.py (desktop)` /
  `app.py (server)` / `app.py (view source)` — any executed `.py` can be
  served instead of run) + Preview button POSTing `{entry, type}`; one
  option → plain button; zero → disabled button + "not supported"
  (artifact still downloadable — preview is an add-on).
- **Static posture** (`_spawn_static`): `python:3.12-slim` running
  `python -m http.server` over the workspace bind-mounted `:ro`; nobody
  user, `--read-only` rootfs, `--cap-drop ALL`, `no-new-privileges`.
  Dedicated bridge `heg-preview` (NOT `--internal` — Docker silently drops
  published ports on internal networks, verified) with
  `enable_ip_masquerade=false` (egress block on Linux; inert on Docker
  Desktop). Port publishes on `127.0.0.1` ephemeral only — no LAN exposure.
- **Executed-code posture** (`_spawn_desktop`/`_spawn_server` → shared
  `_spawn_isolated` — candidate code *executes*, so stricter than
  static): the runner sits on `--internal` `heg-desktop` — the isolation
  boundary for ALL executed previews (zero egress, zero published ports;
  peers = its own socat proxy plus other ephemeral preview runners, none
  of which can egress) — with
  `--cpus`/`--memory`/`--pids-limit` bounds (`PREVIEW_DESKTOP_*` defaults
  1 / 512m / 256, shared by both types), cap-drop ALL, no-new-privileges,
  nobody. A pinned `alpine/socat:1.8.1.3` sidecar
  (`heg.preview_role=proxy`) straddles `heg-desktop` + `heg-preview` — a
  dumb TCP pipe publishing `127.0.0.1` → the runner's port.
  - **desktop**: port = websockify `:6080` (noVNC UI at `/vnc.html`);
    runner image `heg-preview-desktop`
    (`sandbox/images/desktop-preview.Dockerfile`, repo-root build
    context, multi-stage: wheels built in a gcc stage then installed
    `--no-index` — pymunk has no aarch64 wheel) bundles TigerVNC Xvnc +
    novnc + websockify + pygame-ce + pyglet + arcade + tkinter (system
    `tk` libs + mesa GL for pyglet/arcade software rendering). Artifact
    mounted `:ro`, staged to writable `/work`.
  - **python-server**: port = `:8000`, where `server-preview-runner.py`
    (baked as the image ENTRYPOINT) makes the candidate app reachable
    whatever it binds — watches `/proc/net/tcp{,6}` for the app's first
    LISTEN socket, then either lets a non-loopback `:8000` bind serve
    directly or runs an asyncio TCP relay `:8000 → <detected>` (binding
    the eth0 IP when the app squats `127.0.0.1:8000`); `manage.py` gets
    `runserver 0.0.0.0:8000 --noreload`. Runner image
    `sandbox/images/server-preview.Dockerfile` bakes the common web
    frameworks (flask, fastapi/uvicorn, django, aiohttp, bottle, tornado,
    gunicorn) — candidate deps beyond them can't install (no egress) —
    the app fails at import and the runner exits visibly. URL = app root.
  - Deps are baked/wheeled at image build — NEVER pip-installed at spawn
    (preview nets have zero egress).
- **Registry = docker labels** (`heg.preview*`) — `list()`/`get` rebuild
  state via `docker inspect`, so api restarts lose nothing; expiry rides a
  label so the reaper works out-of-process. Runner + proxy share
  `heg.preview_id`; `heg.preview_role` distinguishes them and `stop()`
  removes every labeled container. TTL `PREVIEW_TTL_SECONDS` (default 900)
  enforced by an asyncio reaper (`PREVIEW_REAP_SECONDS`, default 30s).
- **Jail:** `resolve_workspace_dir` reuses the download-jail rule —
  `artifact_path` or `<ARTIFACT_ROOT>/<run>/<sandbox>` must resolve inside
  `ARTIFACT_ROOT`, and `workspace` must not be a symlink escaping it.
- **Where:** `sandbox/preview.py` (detection + `plan_for_entry`/
  `plan_for_type` overrides, `ScanCache`, `PreviewManager` —
  `_spawn_isolated` shared by `_spawn_desktop`/`_spawn_server`,
  `_PROXIED_PORTS` maps type → sidecar port for port resolution),
  `api/routers/preview.py` (+ reaper),
  `api/config.py` (`PREVIEW_*` incl. `PREVIEW_SCAN_TTL_SECONDS`,
  `PREVIEW_SERVER_IMAGE`),
  `api/routers/results.py` (cached previewable probe via `_scan_cache`),
  `sandbox/images/desktop-preview.Dockerfile` +
  `desktop-preview-entrypoint.sh`,
  `sandbox/images/server-preview.Dockerfile` +
  `server-preview-runner.py`,
  `web/lib/api.ts` (`getPreview`/`startPreview`/`stopPreview`, `PreviewOut`),
  `web/app/runs/[id]/results/PreviewControls.tsx`
- **Verify:**
  - Unit: `tests/unit/sandbox/test_preview.py` (static + desktop +
    python-server detection — nested depth bound, skip-dirs incl.
    templates/, candidate ordering/default-first, non-named `.py`
    evidence, comment-stripped requirements, GUI stacks
    pyglet/arcade/tkinter, `plan_for_entry` jail/validation + `type=`
    override incl. manage.py→python-server, `plan_for_type`,
    `ScanCache` hit/expiry/LRU/clear; manager via stubbed docker —
    runner/proxy args for both executed types, role labels, stop-all) +
    `tests/unit/api/test_preview.py` (lifecycle, `entry=`/`type=`
    overrides, live-preview replace-on-different-plan, default-spawn
    semantics, typed `preview_candidates`, cached results probe)
  - Docker: `tests/integration/test_preview.py` (`@pytest.mark.docker` —
    static spawn → HTTP 200, localhost-only binding, reaper; desktop spawn
    builds `heg-preview-desktop` → real pygame executes
    (PYGAME_OK in logs), tkinter/pyglet/arcade apps execute under Xvnc
    (TK_OK/PYGLET_OK/ARCADE_OK markers), runner on internal net with
    limits, sidecar publishes localhost:6080, vnc.html 200, both
    containers cleaned; python-server spawn builds `heg-preview-server`
    → flask app on 127.0.0.1:5000 served through the runner's relay +
    stdlib app on 0.0.0.0:8000 served directly, HTTP 200 through the
    sidecar, same internal-net/limits assertions, both containers
    cleaned)
  - Manual: results page → "Preview" on a pygame artifact → noVNC desktop
    opens in new tab; on a flask artifact → the app's URL opens;
    "■ stop". Build images first:
    `docker build -f sandbox/images/desktop-preview.Dockerfile -t
    heg-preview-desktop:latest .` and
    `docker build -f sandbox/images/server-preview.Dockerfile -t
    heg-preview-server:latest .`
  - Note: the `previewable`/`preview_type` results-GET probe rides
    `sandbox.preview.ScanCache` — memoized per (jail, run, sandbox,
    artifact_path) for `PREVIEW_SCAN_TTL_SECONDS` (default 30s; 0
    disables); the workspace only changes on re-snapshot, so staleness is
    bounded. `reset_scan_cache()` is the test hook.
- **Depends on:** F4 (runtime plumbing/host-path translation), F8 (results
  page + artifact store layout)

### F13 — Graphical analysis page + cross-run trends ✅

- **What:** `/runs/[id]/analysis` — a charts dashboard over the F9 analytics
  + F8 scores, using **recharts** (v3.10.x — React-19-compatible, lightweight
  enough for the plain-CSS theme; palette hexes mirror the globals.css
  tokens in `analysis/charts.tsx`). Cards: score-by-rubric-dimension
  (grouped horizontal bars + "weighted total leader" line), tool-call mix
  (stacked, top-7 tools + `other`), tokens in/out (stacked), LLM latency /
  TTFT (avg/p95/max plus ttft avg/p95 bars when the backend records them —
  PR30), output speed per sandbox (mean avg tok/s — PR30), estimated cost
  per sandbox (price-model dropdown →
  client-side recompute, same trick as AnalyticsSection — local models show
  a note instead of empty bars), participant-vs-evaluator spend split
  (stacked; judge priced at the evaluator model's rate when listed), and a
  cross-run trends ComposedChart (stacked participant+evaluator token bars
  + mean-overall-score line on a right 0–10 axis; newest 20 runs, current
  run accent-highlighted, hover shows run meta). The results score matrix
  also gains a **totals row** (`total`) — distinct from the judge-reported
  `overall` — with the max cell flagged `leader`. PR21 makes the total
  honest: `web/lib/scoreMath.ts` computes a weighted, scale-normalised
  aggregate — Σ ((score − min)/(max − min)) × weight over Σ weight,
  rescaled to 0–10 — honouring each rubric dimension's `weight` +
  `min_score`/`max_score` instead of a raw Σ; the analysis page's leader
  line uses the same helper.
- **API:** `GET /api/analytics/runs?limit=&price_model=` →
  `RunTrends{price_model|null, price_table, points[]}` — per-run roll-up of
  the same participant/evaluator event split (eval boundary per sandbox),
  latest-`overall`-score-per-sandbox mean, participant + judge tokens,
  errors, cost. No `price_model` → each point's participant cells costed
  at their own model's listed rate (PR21: any unlisted cell model makes
  the whole point unpriced — `estimated_cost_usd:null`, since a partial
  total would read as complete;
  `RunTrends.price_model:null` "auto" — a point's own `price_model` is the
  single applied key, or null when cells priced at mixed rates); with it →
  uniform repricing.
  Newest-first; `limit` 1–100 default 20.
- **Where:** `api/routers/analytics.py` (`get_run_trends` +
  `_eval_boundaries`/`_is_eval_event` helpers, also used by the per-run
  endpoint), `api/schemas.py` (`RunTrendPoint`, `RunTrends`),
  `web/app/runs/[id]/analysis/` (`page.tsx`, `AnalysisClient.tsx`,
  `charts.tsx`), `web/lib/analysisRows.ts` + `analysisRows.test.ts`
  (chart-row shapers — an absent metric stays absent, no phantom-zero
  bar), `web/app/runs/[id]/results/ResultsClient.tsx` (`row-total`
  + `dimTotals`/`maxTotal` memos), `web/lib/scoreMath.ts` (weighted-total
  helper) + `web/lib/scoreMath.test.ts`, `web/lib/api.ts` (`getRunTrends`,
  `RunTrendPoint`, `RunTrends`), `web/lib/format.ts` (`formatNum`,
  `formatUsd`), `web/app/globals.css` (`.analysis-grid`, `.chart-tip`,
  `.chart-legend`, `.row-total`/`.score-cell--leader`), nav links in
  `RunDetailClient`/`ResultsClient`/`TimelineClient`.
- **Verify:**
  - Unit: `tests/unit/api/test_analytics.py` F13 section — shape +
    newest-first ordering, per-point aggregation (tokens/turns/errors/
    sandbox_count/mean overall — non-overall scores excluded), auto vs
    uniform repricing, judge priced at evaluator model, prompt truncation
    (120 chars), `limit` bounds (1/0/101), 422 unknown price_model, null
    mean for unscored runs.
  - API: `curl localhost:8001/api/analytics/runs?limit=20`
  - UI: results page → matrix `total` row shows the weighted 0–10 aggregate
    and flags the leader; run detail → "Analysis →" → all cards render on a
    scored run; cost dropdown reprices the cost + split charts client-side.
- **Depends on:** F6 (events), F7 (scores), F8 (results page), F9
  (analytics aggregation)

### F14 — Eval iteration: re-score + versioned rubrics ✅

- **What:** iterate on evaluation after a run finishes — without touching
  participant sandboxes:
  - **Post-run rubric edits** (`POST /api/runs/{id}/rubrics`): edits to a
    completed run's rubric are persisted as a NEW `Rubric` row
    (`version = max+1`, `parent_id` → the edited row, `status="approved"`);
    the previous approved version is marked `superseded`, never mutated.
    Old `Score` rows keep pointing at their original rubric version.
  - **Re-score** (`POST /api/runs/{id}/rescore`): re-runs the evaluator on
    stored artifact snapshots for terminal (`done`/`failed`) runs —
    `done|failed → evaluating → done|failed` via the dedicated
    `orchestrator.lifecycle.rescore_run` task (registered as `rescore` in
    the runner task registry; a concurrent trigger → 409). Optional
    `rubric_id` pin, `sandbox_ids` subset, and judge `model` override.
    `POST …/rubrics` with `rescore=true` creates the version and queues
    the re-score in one request. `_rescore_run` itself refuses non-
    terminal runs (defense-in-depth — `evaluating` is admitted only with
    a rescore marker, i.e. the API pre-transition; `cancelled` is always
    rejected — nothing ever executed), and the empty-pass
    retry checks the score-count DELTA over the pre-pass baseline so
    stale rows don't satisfy it; `rescore_finished` is always emitted
    (error included on crash). A rescore holds the same serialized pacing
    slot as a run — `rescore_run` waits on `_await_rescore_slot`
    (`rescore_queued` checkpoint) while another run is active/queued, and
    `runner.start_rescore_task` wraps the pass in the same
    promote-next-queued `finally` a lifecycle gets, so a rescore never
    overlaps another run and always frees the lane on exit.
  - **Score provenance:** every `Score` carries `rubric_id` +
    denormalized `rubric_version` (stamped at write time, survives rubric
    lookups). `GET /api/runs/{id}/results?rubric_version=N` filters the
    matrix to one version's scores; the default view is per-sandbox
    newest-wins (created_at, seq insertion-order tiebreak). `GET
    /api/runs/{id}/rubrics`
    lists versions + score counts; `GET /api/rubrics/{id}` returns one
    version incl. content (prefills the editor).
  - **Events stay append-only:** `rescore_requested` /
    `rescore_started` / `rescore_finished` / `rubric_version_created`
    checkpoints; eval `started`/`scored` payloads carry
    `rubric_id`+`rubric_version`.
  - **UI:** results page gains a "Re-score & rubric versions" card —
    version selector (latest = newest-wins), re-score button, JSON rubric
    editor that saves as the next version (+ optional re-score); per-
    sandbox "judged: rubric vN" provenance in the matrix header.
  - **Crash semantics:** an api restart mid-re-score keeps the startup-
    sweep rule — an `evaluating` run carrying a `rescore_requested`/
    `rescore_started` checkpoint with ≥1 Score row recovers to `done`
    (a first-pass eval has no marker and still fails), so an
    interrupted re-score doesn't demote a previously-done run.
- **Where:** `db/models.py` (`Rubric.parent_id`, `Score.rubric_version`,
  `uq_rubrics_run_version` unique constraint) +
  migration `b3e7a1c9d2f4`; `orchestrator/rubric.py`
  (`create_rubric_version`), `orchestrator/lifecycle.py` (`rescore_run`,
  `_evaluate_all` refactor), `orchestrator/runner.py`
  (`start_rescore_task`, `_track` coroutine-factory dedup),
  `orchestrator/state_machine.py` (`done|failed → evaluating`);
  `evaluator/evaluate.py` (`rubric_version` kwarg → Score rows + event
  payloads); `api/routers/results.py` (rubrics/rescore endpoints, version
  filter); `web/lib/api.ts`,
  `web/app/runs/[id]/results/RescoreControls.tsx` + `ResultsClient.tsx`.
- **Verify:**
  - Unit: `tests/unit/orchestrator/test_rescore.py` (rescore_run paths,
    task dedup, sweep rescue), `tests/unit/api/test_results.py`
    (endpoints, guards, version filter), `tests/unit/evaluator/
    test_evaluate.py` (provenance stamping), `test_state_machine.py`.
  - API: `POST /api/runs/<id>/rescore` on a finished run → 202 → new
    Score rows with the pinned `rubric_version`.
  - UI: results page → pick an older version → matrix shows that pass's
    scores; edit rubric → save & re-score → run returns to `evaluating`.
- **Depends on:** F7 (evaluator), F8 (results page), F4 (artifact store)

### F15 — Submit UX: live model picker + install, named harness configs ✅

- **What:** three submit-form upgrades shipped together.
  - **Live model catalog** — `GET /api/models` proxies the gateway's
    existing `GET /v1/models` (api mints a run-null `heg.*` cred with
    `GATEWAY_CRED_SECRET`), normalizes `{object:"list",data:[...]}` →
    `{models:[{id,...}], provider, pull_supported}`. The gateway stamps
    `X-HEG-Provider` on the response so `pull_supported` is true only for
    the `ollama` upstream. `GET /api/models?provider=<name>` forwards the
    name as the gateway's `x-heg-provider` service-cred override (unknown
    names 400 at the api via `require_provider`; valid-but-unkeyed 400s at
    the gateway) so each submit-form row can browse the upstream its
    provider select names — the reported `provider` is always the
    gateway's RESOLVED one. Submit page replaces the offline
    `MODEL_SUGGESTIONS` fallback with the live ids once the catalog is
    non-empty, for the model ComboInputs.
  - **Model install** — `POST /api/models/pull` → gateway
    `POST /v1/models/pull` → upstream `POST /api/pull` (ollama-only —
    `501` for other providers), NDJSON progress streamed end-to-end
    (httpx `send(stream=True)` → `StreamingResponse`, pull body forced to
    `{"name","stream":true}`). Submit page: a `name:tag` input + pull
    button with a single status line — while streaming it shows the last
    ollama status plus a `completed`/`total` percent, then the outcome
    (errors styled as field errors); on success the model catalog
    refreshes and the new id enters the suggestions. `pull_supported=false`
    hides the control entirely.
  - **Named harness configs** — optional `name` (≤64 chars) per
    `HarnessConfigIn`; persisted on `sandboxes.name` (migration
    `a2b3c4d5e6f7`), default `<adapter>-<sandbox-id[:8]>` assigned in
    `spawn_one` after the UUID exists. Surfaced as `name` on
    `SandboxOut`/`SandboxResult`/`SandboxAnalytics` and rendered —
    with short-id fallback/tooltips — in the submit rows, run detail
    (config summary + sandboxes table + rubric breakdown headers),
    timeline sandbox filter + row labels, results matrix + artifacts
    table, analytics column heads, analysis-page legend chips, and the
    live run-console lane heads.
- **Where:** `api/routers/models.py` (`ModelsOut`/`ModelPullIn`/
  `ModelInfo`, + `api/main.py` include), `gateway/main.py` (`pull_model`,
  provider header), `api/schemas.py` (`HarnessConfigIn.name`,
  `SandboxOut.name`, `SandboxResult.name`, `SandboxAnalytics.name`),
  `db/models.py` + `db/migrations/versions/a2b3c4d5e6f7_sandbox_name.py`,
  `orchestrator/provision.py` (`_sandbox_name` default),
  `api/routers/results.py` + `analytics.py` (`name` fields),
  `web/lib/api.ts` (`getModels(provider?)`, `PROVIDERS`, `pullModel`
  streaming), `web/lib/modelRows.ts` (+ `.test.ts` — row→payload mapping),
  `web/app/submit/page.tsx`, `web/app/runs/[id]/` (`RunDetailClient`,
  `timeline/TimelineClient`, `results/ResultsClient`,
  `results/AnalyticsSection`, `analysis/AnalysisClient`),
  `web/components/RunConsoleLive.tsx`
- **Verify:**
  - Unit: `tests/unit/api/test_models.py` (normalize, provider flag,
    `?provider=` forwarding + validation, gateway error/unreachable, pull
    streaming + validation), `web/lib/modelRows.test.ts` (rows→
    `model_name`/`models` mapping, dedupe),
    `tests/unit/gateway/test_proxy.py` (pull passthrough/stream-forced/
    501 non-ollama/auth), `tests/unit/db/test_migrations.py`
    (upgrade/downgrade on sqlite, single head, type match),
    `tests/unit/orchestrator/test_provision.py` (custom name persisted,
    default/whitespace → `adapter-shortid`),
    `tests/unit/api/test_runs.py` (name round-trip, 64-char bound),
    `test_results.py`/`test_analytics.py` (`name` in responses)
  - Manual: `curl localhost:8001/api/models` → catalog + `pull_supported`;
    `curl -N -X POST localhost:8001/api/models/pull -d '{"name":"qwen3:8b"}'`
    → ndjson progress lines; submit form → model dropdown lists live ids,
    install streams progress; run detail/timeline/results/analytics show
    the configured names (or `adapter-shortid` defaults).
- **Depends on:** F1 (submit form), F3 (gateway `/v1/models` + creds)

### F16 — Model-vs-model matrix (config × model) + per-model analytics ✅

- **What:** reverses the original "compare harnesses, not models" decision —
  a run can now vary the participant model as well as the harness config.
  - **Spec** — `RunCreate.models` (optional list, deduped/normalized against
    `model_name`; omitted/empty/blank → `[model_name]`;
    over-`MAX_MATRIX_CELLS` expanded cells rejected).
    `HarnessConfigIn.model` optionally pins one config to a single
    model, overriding the run list for that config. Normalized set persisted
    as `run.config.models`; `RunSummary` exposes `models` (`RunOut` carries
    it under `config`).
  - **Expansion** — `orchestrator/matrix.py`: `participant_models`,
    `expanded_configs` (config × models minus explicit overrides, deduped),
    `sandbox_model` (per-sandbox resolution). `lifecycle.py` provisions one
    sandbox per expanded cell; `provision.py` persists the resolved model in
    `Sandbox.harness_config.model`, folds it into the sandbox name/label,
    and passes it to the adapter env as `MODEL_NAME`. Per-cell spawn failures
    stay isolated.
  - **API** — `same_model_eval` now compares the evaluator model against
    *every* participant model; results carry `SandboxResult.model` +
    `RunResults.models`; analytics resolves pricing per sandbox's own model
    (`price_model` param still forces a uniform reprice, now optional) and
    adds a `models` rollup (`ModelAnalytics`: tokens/cost aggregates
    per participant model).
  - **UI** — submit form picks participants as a row list: each row is a
    provider select + model ComboInput (`?provider=` catalog per row),
    row 1 is the primary (`model_name`), "+ Add Another Model" appends
    (capped by `models` max 8 + the matrix cap); provider is picker
    metadata only — `RunCreate` carries no per-model provider, routing
    resolves per model name via PROVIDER_ROUTES. Evaluator model gets the
    same single-row picker; Ollama pull preserved (shown while any row is
    on ollama and the upstream reports pull support). Run detail/list show
    the model set; results
    matrix + timeline + live console label each cell with its model;
    AnalyticsSection gains a sandbox/model segmented view with per-model
    aggregation and per-model cost; the analysis page prices each sandbox at
    its own model's rate in "auto" mode.
  - **PR31 — config × model heatmap + winner card:** the results page's
    score card gains a `matrix | grid` segmented toggle. **Grid** folds the
    per-sandbox columns back into the comparison matrix — rows = submitted
    harness configs (identity = `harness_config` minus the injected
    `model`/trial keys; the sandbox `name` is unique per sandbox and can't
    group), cols = `RunResults.models` plus observed extras — each cell
    showing the mean judge `overall` across its members (trials included —
    same-(config, model) repeats roll up `±sd · ×n` with a scored-count
    note under partial coverage), the PR21 weighted total underneath, on
    the fixed 0–10 red→amber→green heat scale (readable at 32 cells).
    The **winner card** above the grid ranks cells by mean WEIGHTED total
    (never the judge's self-reported `overall`; a cell with only an
    `overall` can't win), and shows the pick's weighted total, judge
    overall, +Δ vs the runner-up cell (named), mean estimated cost per
    sandbox + wall clock (from the F9 analytics payload — "unpriced"/"—"
    honestly, never a $0 stand-in), and mean test pass-rate
    (`generated_tests.pass_rate` → `exec` tests fallback). The winning
    cell gets an outline + ★ in the grid. The matrix view's `total` row
    already computed the PR21 weighted-normalised aggregate
    (`web/lib/scoreMath.ts::weightedDimTotal`); the grid reuses the same
    `dimensionKeys`/`specMap` rather than a second weighting.
- **Where:** `orchestrator/matrix.py` (new), `orchestrator/lifecycle.py`,
  `orchestrator/provision.py`, `api/schemas.py` (`RunCreate.models`,
  `HarnessConfigIn.model`, `MAX_MATRIX_CELLS`, `models` on run/results/
  analytics schemas), `api/routers/runs.py`, `api/routers/results.py`,
  `api/routers/analytics.py`, `web/lib/api.ts`, `web/lib/modelRows.ts`
  (participant-row → `model_name`/`models` payload mapping),
  `web/app/submit/page.tsx`,
  `web/app/runs/page.tsx`, `web/app/runs/[id]/RunDetailClient.tsx`,
  `web/app/runs/[id]/results/{ResultsClient,AnalyticsSection,GridView}.tsx`,
  `web/app/runs/[id]/analysis/AnalysisClient.tsx`,
  `web/app/runs/[id]/timeline/TimelineClient.tsx`,
  `web/lib/timelineAnalytics.ts` (per-sandbox price basis in strips),
  `web/lib/gridMatrix.ts` (PR31 grid transform) + `gridMatrix.test.ts`,
  `web/components/RunConsoleLive.tsx`, `web/app/globals.css`
- **Verify:**
  - Unit: `tests/unit/orchestrator/test_matrix.py` (defaults, overrides,
    dedupe, limits), `test_lifecycle.py` (matrix expansion + spawn isolation),
    `test_provision.py` (model persisted in harness_config + env),
    `tests/unit/api/test_runs.py` (validation, normalization, same-model
    eval across the set), `test_results.py` (`model`/`models` fields),
    `test_analytics.py` (per-model pricing + rollup),
    `web/lib/modelRows.test.ts` (participant-row payload mapping)
  - Manual: submit with 2 configs × 2 models → 4 sandboxes named/labeled
    per cell; results matrix + analytics "model" view group by model; cost
    auto-mode prices each cell at its own model. PR31: results page →
    "grid" toggle → config × model heat table renders (8×4 = 32 cells),
    winner card shows the top weighted cell + runner-up Δ + cost/time/
    pass-rate; `cd web && npm test` covers the transform (cell assembly,
    sparse cells, mean±sd rollups, heat stops, winner pick, pass-rate).
- **Depends on:** F15 (PR1 — named configs + live model picker this stacks
  on); best after multi-provider support so >1 model is usable.

### F17 — Artifact thumbnails ✅

- **What:** results-page `📷 snapshot` button → a synchronous, bounded
  headless screenshot of the artifact, served exactly as an interactive
  preview would serve it. The PNG lands at `<artifact>/thumbnail.png`
  (+ `thumbnail.json` meta sidecar), rides the results payload as
  `SandboxResult.thumbnail{url,captured_at,type,entry}`, and renders as a
  small shot at the head of the preview cell. Clicking the shot opens the
  FULL live preview (spawning one when none is running). `↻ recapture`
  re-shoots; DELETE removes it.
- **Trigger = explicit-only.** Generation is never lazy on `GET
  .../results` — that endpoint is polled every few seconds and capture
  costs a container spawn + browser run. The results GET only FS-probes
  for an existing `thumbnail.png` (cheap, jailed, meta-decorated).
- **API** (`api/routers/preview.py`):
  - `POST .../thumbnail` → `201 {url, captured_at, type, entry}`;
    optional `{entry,type}` body picks the captured candidate exactly like
    `POST .../preview` (default = scan's default plan). `502` on capture
    failure — "no thumbnail" is a fine outcome, never a broken results
    read. `422` unpreviewable workspace.
  - `GET .../thumbnail` → `200 image/png` (jailed like artifact downloads;
    a planted symlink can't escape `ARTIFACT_ROOT`) or `404`.
  - `DELETE .../thumbnail` → `204`, removes PNG + sidecar.
- **Capture pipeline** (`sandbox/thumbnails.py::Thumbnailer.capture`):
  1. `PreviewManager.spawn(purpose=PURPOSE_THUMB)` — the artifact's own
     preview stack, same isolation/topology (static → hardened
     http.server; desktop/python-server → runner+socat on the zero-egress
     `heg-desktop` net), short TTL (`THUMB_PREVIEW_TTL_SECONDS`, 300s).
  2. `_wait_ready` confirms the stack came up — a dead runner fails
     fast. Serving readiness is polled inside the capture container
     (≤ `THUMB_READY_SECONDS`, 30s): it shares `heg-preview`, and the
     published port binds the HOST's loopback, unreachable from a
     containerized api (compose).
  3. Type-scaled settle (`THUMB_SETTLE_SECONDS` 0.5s;
     `THUMB_DESKTOP_SETTLE_SECONDS` 8s — Xvnc is loopback-only inside the
     runner, so the noVNC page is the only window onto the framebuffer).
  4. One-shot `docker run --rm` of `heg-preview-thumb` (repo-built:
     debian chromium on `python:3.12-slim` —
     `sandbox/images/thumbnail-capture.Dockerfile` + `…-capture.sh`) on
     the `heg-preview` bridge, loading the stack's in-network URL by
     container name (`<runner>:8000` for static,
     `<runner>-proxy:{8000|6080}` for the executed types). nobody +
     read-only rootfs + cap-drop ALL + no-new-privileges + tmpfs /tmp;
     `timeout` inside, subprocess timeout (ready wait +
     `THUMB_CAPTURE_SECONDS`, 45s, + slack) outside.
  5. PNG-magic validation → atomic `os.replace` to `thumbnail.png` +
     `thumbnail.json` (`{version,captured_at,type,entry,generator,
     width,height,bytes}`). Scratch `.thumb-*` dir + stack always cleaned.
- **Purpose separation:** capture stacks carry `heg.preview_purpose=thumb`
  — the interactive endpoints filter on `preview`, so a mid-flight
  capture is never returned as "the preview" and never satisfies
  start_preview's reuse check; the reaper/`stop()` see both (leaks stay
  TTL-bounded). Unlabeled legacy previews read as `preview`.
- **Where:** `sandbox/thumbnails.py` (`Thumbnailer`, `ThumbResult`,
  `ThumbnailError`), `sandbox/preview.py` (`LABEL_PURPOSE`/`PURPOSE_*`,
  `Preview.purpose`, purpose-aware `get_for_sandbox`, `resolve_artifact_dir`,
  `_entry_url_path`, `_PROXIED_PORTS`), `api/routers/preview.py`
  (`capture_thumbnail`/`get_thumbnail`/`delete_thumbnail`,
  `get_thumbnailer`, purpose filtering in `start_preview`),
  `api/routers/results.py` (`ThumbnailRef`, `_load_thumbnail`),
  `api/config.py` (`PREVIEW_THUMB_IMAGE`, `THUMB_*` bounds),
  `sandbox/images/thumbnail-capture.Dockerfile` +
  `thumbnail-capture.sh`, `web/lib/api.ts` (`ThumbRef`, `ThumbOut`,
  `thumbnailUrl`, `captureThumbnail`, `deleteThumbnail`),
  `web/app/runs/[id]/results/PreviewControls.tsx` (`.thumb-*` styles in
  `globals.css`)
- **Verify:**
  - Unit: `tests/unit/sandbox/test_thumbnails.py` (capture flow via a
    stubbed docker CLI + fake manager — spawn args/labels, ready-poll,
    dead-runner fast fail, capture URL per type incl. entry-path quoting,
    PNG validation, meta write, scratch/stack cleanup, timeout→`rm -f`) +
    `tests/unit/api/test_thumbnails.py` (POST/GET/DELETE lifecycle, 502
    mapping, jailed GET incl. symlink escape → 404, results payload
    embeds `thumbnail`) + purpose-filtering cases in
    `tests/unit/{sandbox,api}/test_preview.py`
  - Docker: `tests/integration/test_thumbnail.py`
    (`@pytest.mark.docker` — builds `heg-preview-thumb`, real static-site
    capture end-to-end → PNG + meta, label-scoped cleanup only)
  - Manual: build `docker build -f
    sandbox/images/thumbnail-capture.Dockerfile -t
    heg-preview-thumb:latest .`, results page → `📷 snapshot` → shot
    appears → click → live preview opens.
- **Depends on:** F12 (preview stack, purpose label plumbing, jailed
  workspace resolution), F8 (results page)

### F18 — Prompt-only matrix cells (no-sandbox adapter) ✅

**What:** a pseudo-adapter `prompt` that makes a single LLM call per
matrix cell with zero Docker containers — widening the matrix beyond
coding agents to business-task evals (extraction, summarisation, Q&A,
classification) while reusing the same creds, metering, artifacts,
timeline, evaluation, and results pipeline.

**Why:** the matrix already expands `configs × models`; prompt cells add
a row type that answers "what does the model itself do with this prompt"
— baseline-vs-harness comparisons and non-coding tasks that have no
workspace to produce.

- `prompt` appears in `/api/harnesses` + the submit picker under the
  **No sandbox** group (manifest `no_sandbox: true`, no image/cli), but
  is NOT in `adapter_names()` — `get_adapter("prompt")` raises a
  no-sandbox `KeyError` and spawn-side lookups can't smuggle a
  container-less cell into `spawn_one`.
- Cell flow (`orchestrator/prompt_cell.py::run_prompt_cell`): create the
  ordinary `Sandbox` row → mint the normal sandbox-scoped gateway cred
  → `emit run_started` → POST `{GATEWAY_URL}/v1/chat/completions` with
  `Bearer <cred>` — one retry on upstream 5xx/timeout only; 4xx fails
  immediately. Gateway emits `llm_request`/`llm_response` + metering as
  usual. Assistant text → `output.txt` (+ `meta.json` +
  `transcript.log`) under the standard artifact dir; `sandbox_finalized`
  + `terminal_state` events + `done`/`harness_complete` close the cell.
- `harness_configs[].system_prompt_extra` lands as a `system` message
  before the task prompt.
- Mixed runs interleave: prompt cells resolve inline, container cells
  spawn+monitor in the same loop; evaluation runs once all cells are
  terminal, with `prompt_output_path` pointing the evaluator at
  `output.txt`.
- Evaluator: `evaluate_sandbox(prompt_output_path=...)` stages
  `output.txt` as the submission (no workspace required); EVAL_PROMPT
  frames it as a prompt output. Missing output stages empty — judged,
  not infra-failed. Static/exec/generated-test collectors degrade to
  `skipped` on `.txt`-only submissions.
- Results/preview: the `output` artifact kind downloads via the existing
  endpoint; results rows report `previewable: false` and the preview +
  thumbnail endpoints 404 early for prompt cells (before any workspace
  scan). The web results page shows output text inline + download,
  suppressing preview affordances.
- Files: `orchestrator/prompt_cell.py`, `orchestrator/adapters.py`
  (pseudo-manifest + `no_sandbox_adapter_names()`),
  `orchestrator/lifecycle.py` (prompt branch + all-cells-fail early
  exit), `evaluator/evaluate.py` + `evaluator/prompts.py`
  (`prompt_output_path`), `api/routers/runs.py` (validation),
  `api/routers/preview.py` (guards), `web/lib/api.ts`,
  `web/app/submit/page.tsx` (group), `web/app/runs/[id]/results/*`
  (output view + preview suppression).
- Tests:
  - `tests/unit/orchestrator/test_prompt_cell.py` — request shape, cred
    scope, single retry on 5xx/timeout, artifact/meta/events/finalize,
    failure paths.
  - `tests/unit/orchestrator/test_lifecycle.py::test_lifecycle_prompt_only_run`
    + `test_lifecycle_mixed_prompt_and_container` — zero spawns, mixed
    routing, all-fail → run failed.
  - `tests/unit/adapters/test_adapter_contract.py` — selectable-but-not-
    spawnable + manifest keys.
  - `tests/unit/api/test_harnesses.py`, `test_runs.py`, `test_results.py`,
    `test_preview.py` — manifest surface, submit validation, output
    artifact/download + previewable:false, preview/thumbnail 404s.
  - `tests/unit/evaluator/test_evaluate.py` — output.txt staging,
    missing-output empty stage, analysis persistence.
- Verify: `uv run pytest tests/unit/orchestrator/test_prompt_cell.py
  tests/unit/orchestrator/test_lifecycle.py -k prompt -q`; manual:
  submit a `prompt × 3 models` run → no containers started (docker ps),
  three `output.txt` artifacts, scores land, cost/tokens metered;
  results page shows output inline with preview absent.
- **Depends on:** F16 (config × model matrix expansion), F3 (gateway
  creds/metering), F7 (evaluator staging), F8/F12 (results + preview).

### F19 — Artifact file browser + side-by-side diff ✅

- **What:** the results matrix's artifacts cell gets a `📂 browse` chip
  opening a two-pane modal — jailed file tree on the left, size-capped
  text viewer on the right — plus a `diff vs` peer picker that diffs one
  path across two sandboxes' artifacts server-side and renders the
  unified diff in a colored `<pre>`. Users inspect and compare produced
  workspaces without downloading tarballs; prompt cells' `output.txt`
  surfaces through the same browser.
- **Browse root:** `workspace/` wins when the snapshot exists; a
  workspace-less artifact (prompt cells, pre-snapshot failures) browses
  the artifact dir itself (`output.txt`, `transcript.log`, `meta.json`,
  `analysis.json`, `generated_tests/`…). `FileListOut.root` reports
  which root answered (`"workspace"` | `"artifact"`).
- **Endpoints** (`api/routers/results.py`, all jailed under
  `ARTIFACT_ROOT` exactly like the artifact-download endpoint):
  - `GET /api/sandboxes/{id}/files` — depth-bounded (`8` levels),
    entry-bounded (`5000`, `truncated` flag) walk; `node_modules`/`.git`/
    `__pycache__`/`.venv`/`*.pyc` pruned like workspace ZIPs; symlinks
    never followed or listed.
  - `GET /api/sandboxes/{id}/files/{path}` — text read capped at 256 KiB
    (`truncated` flag); NUL-carrying reads → `binary: true, text: null`
    (UI offers `?download=1` FileResponse instead). Traversal, absolute
    paths, symlink escapes → 404; `..` that stays inside the root
    resolves normally.
  - `GET .../files/{path}?diff=<other_sandbox_id>` — unified diff
    (`difflib`, `a/`+`b/` labels) of the same path in both browse roots.
    A path absent on one side diffs against empty (`a_missing`/
    `b_missing`); absent on both → 404. Per-side input cap 1 MiB, output
    cap 10k lines; binary sides → `binary: true`, empty `diff`.
    `response_model=None` on the route (union of Response/FileContentOut/
    FileDiffOut).
- **UI** (`web/app/runs/[id]/results/FileBrowser.tsx` + `web/lib/
  fileBrowser.ts`): browse mode renders the flat listing as a
  collapsible tree (`buildFileTree` synthesizes parents a truncated
  listing missed). Diff mode applies the PR10 split idiom
  (`mergeFileLists` — ONE merged ordering of both sides' paths, each row
  carrying its left/right cell, single-sided files facing an empty lane)
  with a `only-left / only-right / shared` summary header; the viewer
  renders `parseDiffLines`-classified rows (meta/hunk/add/del/context).
  No diff library added — the server produces the format.
- **Files:** `api/routers/results.py`, `tests/unit/api/test_results.py`,
  `web/lib/api.ts`, `web/lib/fileBrowser.ts`,
  `web/lib/fileBrowser.test.ts`,
  `web/app/runs/[id]/results/FileBrowser.tsx`,
  `web/app/runs/[id]/results/ResultsClient.tsx`, `web/app/globals.css`.
- **Tests:** `tests/unit/api/test_results.py` PR34 section — fixture
  workspace listing (paths, exclusions, `root:"workspace"`), text read +
  256 KiB cap, `?download=1` attachment, `?diff=` valid unified format +
  missing-side/binary/identical cases, `../` (incl. sibling-workspace
  escape) + absolute-path + dir-as-file → 404, prompt-cell
  `output.txt`/`meta.json` listing with `root:"artifact"`.
  `web/lib/fileBrowser.test.ts` —
  tree nesting/sorting, merged-lane ordering + single-sided rows,
  summary counts, diff-line classification.
- **Verify:** browse a 2048 artifact → `index.html` opens in the viewer;
  diff two sandboxes' `index.html` → unified diff; percent-encoded
  `..%2f` traversal and absolute paths → 404.
- **Depends on:** F8 (results matrix + artifact jail), F12 (`sandbox/
  preview.py` `resolve_artifact_dir`/`resolve_workspace_dir` helpers),
  F18 (prompt-cell artifact root as a browse target).

### F20 — Trials per cell + per-config model options (PR27) ✅

**What:** every effective config × model matrix cell repeats `trials`
times (1–8, default 1) so results report mean ± sample sd with `n`
instead of a single sample; each harness config may carry
`model_options` (`temperature`, `seed`, `max_tokens`, `reasoning`) that
adapters honor where they expose the equivalent control.

**Why:** a one-shot sample per cell can't rank harness/model combos
defensibly — small models are stochastic. Repeated trials give the
score distribution; model options make the comparison axis itself
configurable per cell (e.g. temperature sweeps).

- `RunCreate.trials` (int, 1–8, default 1) persists on `runs.trials`
  (migration `d7e9f1a3b5c7`) and `config["trials"]`; the 32-sandbox
  matrix cap applies to the post-trial count
  (`configs × models × trials`).
- Expansion (`orchestrator/matrix.py::expanded_configs`) runs
  model-major first, then stamps a 1-based `trial_index` on each replica
  cell-major — a cell's trials stay adjacent. `trials=1` emits no key
  (pre-PR27 shape); `trial_index`/`cell_key` helpers read the stamp and
  group identity (config minus `trial_index`, serialized sorted).
  Lifecycle reads `run.trials` (column wins; `config["trials"]` covers
  pre-column specs). Prompt pseudo-cells replicate identically.
- Sandbox names append ` · tN` after the model suffix
  (`cfg · model · t3`) — provision truncates the label side, never the
  tail (`sandboxes.name` is String(255)).
- `HarnessConfigIn.model_options` is a closed schema (`extra="forbid"`,
  per-key bounds; `reasoning` normalizes to lowercase and maps to the
  request-level `reasoning_effort`). Submit filters keys against
  `orchestrator.adapters.supported_model_options` (each adapter's
  `MANIFEST["model_options"]`, surfaced on `GET /api/harnesses`):
  unsupported keys are dropped from the persisted cell + advisory
  `warnings` on the response — never a 422. Support today: `miniloop` +
  `prompt` all four; `codex` `reasoning` only (→
  `model_reasoning_effort` config); `claude_code`/`pi`/`deepseek`/
  `aider` none.
- Miniloop maps options to env knobs `MINILOOP_TEMPERATURE`,
  `MINILOOP_SEED`, `MAX_TOKENS`, `MINILOOP_REASONING_EFFORT`
  (`AgentConfig.from_env` → request-body fields; the explicit
  `max_tokens` kwarg outranks `model_options.max_tokens`). `prompt`
  cells fold supported options into the chat-completions body. `cli_env`
  takes `model_options` across the CLI contract.
- Results API returns `trials` on the run + `trial_index` per sandbox;
  the results matrix groups trials under one config × model header
  showing `mean ± sd (n=N)` (sample sd, n−1 — null under 2 scored
  trials; unscored trials drop out of `n`) with expandable per-trial
  rows keeping rationales/artifacts. The by-model rollup reads
  `mean overall (±sd, n=…)`.
- Analytics adds per-sandbox `cell`/`trial_index` and a `cells` rollup
  (`overall_mean`/`overall_sd`/`overall_n`, pooled metrics across the
  cell's trials) plus a by-cell table view; the analysis page gains a
  "Score variance by cell" table. Web helpers in
  `web/lib/trialGroups.ts` (`cellKey`/`groupByCell`/`meanSd`/
  `fmtMeanSd`/`stripTrialSuffix`).
- Submit UI: a `trials` stepper (1–8) plus advanced per-config
  model-options controls annotated with each adapter's supported set;
  submit warnings surface dropped keys. Reasoning-effort options are
  adapter-scoped — codex offers `minimal|low|medium|high|xhigh` (its
  `model_reasoning_effort` vocabulary; `max`/`none` fail codex's config
  parse), pass-through adapters keep the broader list. `resubmit` carries
  both fields.
- Files: `api/schemas.py` (`ModelOptions`, `RunCreate.trials`,
  `CellAnalytics`), `api/routers/runs.py` (persist + filter + warnings),
  `api/routers/analytics.py` + `results.py` (`trial_index`/`cell`/
  `cells`), `orchestrator/{matrix,lifecycle,provision,prompt_cell,
  adapters}.py`, `adapters/common/contract.py` (`cli_env` signature),
  `adapters/{miniloop,codex,claude_code,pi,deepseek,aider}`,
  `db/models.py` + `db/migrations/versions/d7e9f1a3b5c7_runs_trials.py`,
  `web/lib/{api,trialGroups,resubmit}.ts`, `web/app/submit/page.tsx`,
  `web/app/runs/[id]/results/{ResultsClient,AnalyticsSection}.tsx`,
  `web/app/runs/[id]/analysis/AnalysisClient.tsx`, `web/app/globals.css`.
- Tests: `test_matrix.py` (N-count, 1-based cell-major stamp,
  `cell_key`/`trial_index`), `test_provision.py` (` · tN` naming +
  truncation, model_options → miniloop env), `test_analytics.py`
  (hand-worked mean ± sd = 7.667 ± 1.528 on scores 6/8/9, scored-only
  `n`), `test_runs.py` (trials persist/bounds/cap, options kept-
  supported/dropped-unsupported/422-unknown, whole-map removal),
  `tests/unit/adapters/miniloop/test_adapter.py` (env knobs, kwarg
  precedence), `tests/unit/db/test_migrations.py` (head +
  up/down).
- Verify: `uv run pytest tests/unit -q`; manual — submit a 2-config
  run at trials=3 → six ` · tN` sandboxes; results matrix shows one
  column per cell with `mean ± sd (n=3)` + expandable trials; a codex
  config with `temperature` submits 201 with a drop warning.
- **Depends on:** F16 (config × model matrix), F8/F9 (results +
  analytics), F18 (prompt cells replicate through the same path).

### F21 — Budgets & pricing accuracy (PR28) ✅

**What:** budgets that can't be starved + prices that aren't guesses.

- **Per-cell token cap** — `BudgetsIn.sandbox_token_budget` (optional)
  mints into each participant cred as the `sandbox_max_tokens` claim;
  the gateway `_budget_gate` meters it against ONLY that sandbox's own
  `llm_response` total (a runaway cell starves itself, not siblings —
  service/eval creds carry no claim and stay exempt), and the monitor's
  poll is the backstop that terminates the cell `budget_exceeded` with
  `payload.limit="sandbox_tokens"` once its own total crosses —
  `prompt_cell._budget_cap_detail` gives a no-sandbox cell the same
  post-call overshoot verdict inline — the run ceiling
  (`token_budget` → `limit="run_tokens"`) included.
- **Per-cell cost cap** — `BudgetsIn.max_cost_usd` (optional): the
  monitor prices the sandbox's own participant traffic each poll at its
  own model through the layered price book (prompt/completion/cache-read
  split) and terminates `budget_exceeded` with `payload.limit="cost"`.
  A prompt cell finishes before the monitor's poll could run, so
  `prompt_cell._budget_cap_detail` runs the same metered estimate inline
  post-call — a clean completion that breached the cap flips to
  `budget_exceeded` (error/timeout verdicts stand). Priced models only
  — an unpriced cell resolves `None` and can't trip
  the cap (PR21 honesty: never a guessed rate). Terminate checkpoints
  now carry `limit` + the numbers that fired (`used_tokens`/`budget`,
  `estimated_cost_usd`/`budget_usd`) for all three limits.
- **`model_prices` table + `GET/PUT/DELETE /api/prices`** —
  user-editable rates keyed by case-insensitive fnmatch `model_glob`
  (`gpt-4*`), first match in declaration order wins, layered OVER the
  static `PRICE_TABLE`: a glob hit is the price key analytics resolves
  (and a valid `?price_model=` pick); a glob equal to a static key
  shadows it. `cache_read_per_mtok_usd` NULL bills cached input at the
  input rate; `local` marks free inference — PUT 422s `local` beside any
  nonzero rate (the pickers render "local — $0"), so free rows can't
  silently bill. A bare (no `/`) glob never
  matches `vendor/model` ids — write `openai/*` deliberately.
- **`POST /api/prices/sync`** — pulls the OpenRouter `/models` catalog
  THROUGH the gateway (`x-heg-provider: openrouter` service-cred
  override; the api never talks to providers). `pricing.*` USD-per-token
  strings are stored as per-Mtok rates (`input_cache_read` →
  `cache_read_per_mtok_usd`); entries with no parseable prompt or
  completion rate are skipped rather than priced-free; `source="user"`
  rows are never clobbered. 400s when openrouter isn't a configured
  provider.
- **UI** — both cost pickers (results Analytics card + analysis page)
  gain "+ add rate" (PUT /api/prices inline; the new glob folds into the
  picker table immediately and is selected); submit Budgets gains the
  two optional per-cell fields (empty = unset). `Budgets.sandbox_token_budget`
  / `max_cost_usd` are nullable in `BudgetsIn`; the run config stores the
  full dump (nulls = unset) so pre-PR28 runs behave identically.
- **Files:** `api/pricing.py` (`PriceBook`/`load_book`/`glob_matches`/
  cache-aware `estimate_cost_usd`), `api/routers/prices.py`,
  `db/models.py::ModelPriceRow` + migrations `c4e8d1a7f3b5`,
  `d6e2b8f4a1c9` (case-insensitive `lower(model_glob)` unique index — the
  DB enforces the identity the case-folding lookups assume),
  `gateway/creds.py` (`sandbox_max_tokens` claim), `gateway/metering.py`
  (`llm_usage_totals` prompt/completion/cache split + sandbox scoping),
  `gateway/main.py::_budget_gate`, `gateway/mint_cred.py`,
  `orchestrator/provision.py` (`budgets_for`, `mint_sandbox_cred`),
  `orchestrator/monitor.py` (per-cell poll checks + terminate detail),
  `orchestrator/prompt_cell.py::_budget_cap_detail` (inline per-cell caps),
  `api/routers/analytics.py` (book-resolved keys), `web/lib/api.ts`
  (prices fns, `globMatch`/`matchPriceKey`, cache-aware `estimateCostUsd`),
  `web/components/AddRate.tsx`, `web/app/submit/page.tsx`,
  `web/app/runs/[id]/results/AnalyticsSection.tsx`,
  `web/app/runs/[id]/analysis/AnalysisClient.tsx`.
- **Tests:** `tests/unit/api/test_pricing.py` (glob rules, first-match
  precedence, layering, unpriced→None, cache-rate split),
  `tests/unit/api/test_prices.py` (GET/PUT/DELETE round-trip, e2e
  repricing, OpenRouter sync via mocked gateway client — mapping,
  user-row protection, unpriced skip, update-vs-add, lost-insert-race
  convergence),
  `tests/unit/gateway/test_creds.py` + `test_metering.py` +
  `test_proxy.py` (claim roundtrip/validation, sandbox-scoped sums,
  per-cell 429 `limit=sandbox_tokens`, sibling isolation, service-cred
  exemption), `tests/unit/orchestrator/test_monitor.py` +
  `test_provision.py` (token cap → `limit=sandbox_tokens`, cost cap →
  `limit="cost"` priced model + model_prices row, unpriced can't trip),
  `tests/unit/db/test_migrations.py` (head = `d6e2b8f4a1c9`, table
  up/down + ORM roundtrip + lower-glob uniqueness).
- **Verify:** `uv run pytest tests/unit/api/test_pricing.py
  tests/unit/api/test_prices.py tests/unit/gateway
  tests/unit/orchestrator -q`; manual: PUT
  `{"model_glob":"gpt-4o","input_per_mtok_usd":2.5,"output_per_mtok_usd":10}`
  → analytics cost appears; submit a run with `max_cost_usd: 0.05` →
  sandbox terminates `budget_exceeded` (checkpoint `limit: "cost"`);
  unlisted model still shows `unpriced`.
- **Depends on:** PR21 (unpriced honesty this makes permanent), F3
  (creds/metering), F9 (analytics cost surface).

### F22 — Quality × cost/speed scatter + auto summary (PR33) ✅

- **What:** the analysis page gains a "Quality × cost | wall clock"
  scatter card (full-width, after the score cards): one dot per
  config × model matrix cell — the same `buildScoreGrid` fold as the
  PR31 results grid, so trials mean into their cell. y = mean PR21
  **weighted** total (never the judge's self-reported `overall`); x =
  mean per-sandbox estimated cost (PR28 honest prices) or mean
  wall-clock on the segmented toggle; color + marker shape = model
  (first-seen order is stable); dot size = mean avg output tok/s
  (smallest = unmeasured speed). Accent rings + a dashed connector mark
  the **Pareto frontier** — the cells nothing beats on both axes at
  once. Under the chart a deterministic **verdict** card (no LLM call)
  names the best cell with the runner-up delta, the value pick (max
  quality-per-$ or per-minute over the frontier), and the cheap/fast
  alternative ("88% cheaper for −4").
- **Honesty/sparse handling:** cells missing the active axis are
  EXCLUDED and named in the legend — an unpriced cell never plots at
  $0 (PR21 rule) — while scored-but-unclocked cells drop only on the
  wall-clock toggle. Cells with no weighted score never become points;
  `unscoredCells`/`excluded`/`partialCostCells` counts are disclosed in
  the legend (a cell priced over only some members still plots, flagged).
- **Where:** `web/lib/scatterMatrix.ts` — pure transform mirroring
  `gridMatrix.ts` (`buildScatterPoints` folds `cellExtras` + tok/s
  means, `axisValue`/`withAxis` resolve x, `paretoFrontier` =
  non-dominated set x-asc, `buildVerdict` + `verdictLines` = the
  template, `buildScatterData` bundles it all) +
  `web/lib/scatterMatrix.test.ts` (hand-computed Pareto oracles, sparse
  handling, determinism under shuffle); `web/app/runs/[id]/analysis/
  QualityScatter.tsx` (recharts `ScatterChart` + custom tip/ring) wired
  into `AnalysisClient.tsx` (shared specMap/dimensionKeys); `.verdict`
  styles in `web/app/globals.css`.
- **Verify:** `cd web && node --test lib/scatterMatrix.test.ts` (22
  tests); manual: analysis page on a scored run → dots per cell, toggle
  cost↔wall clock changes the frontier + verdict wording
  (cheaper↔faster, pts/$↔pts/min); a run with an unpriced model lists
  that cell under "excluded" rather than plotting it at $0.
- **Depends on:** F16 (PR31 grid fold + weighted totals), F21 (PR28
  honest prices), F9/F13 (analytics payload + analysis page), PR30
  speed fields.

## Implementation sequence

**Phase 0 — foundation (sequential):** F0 scaffold + base schema.

**Phase 1 — the spine (3 parallel lanes):**

- **Lane A (backend):** F6 ingestion endpoint → F3 gateway → event flow lands
  in Postgres. *Milestone M1: "hello model" — curl through gateway produces an
  event row.*
- **Lane B (agent):** F5 miniloop harness program (runs standalone against any
  OpenAI-compatible endpoint; no docker needed to iterate).
- **Lane C (infra):** F4 sandbox runtime mechanics (spawn, network, env,
  snapshot, `done`) using a trivial command first.
- *Milestone M2: "agent in a box" — miniloop inside a sandbox completes a
  trivial task end-to-end with events + artifact.*

**Phase 2 — the loop:**

- F2 orchestrator (rubric gen + lifecycle) — Lane A
- F7 evaluator — parallel with F2 once artifacts + rubric exist
- F1 form + F6 timeline UI + F8 results — Lane C frontend, built against API
  contracts/mocks, integrated as endpoints land
- *Milestone M3: full MVP run — form → rubric approval → 2 sandboxes →
  evaluation → results page.*

**Phase 3 — hardening:** e2e test of a real run, docs, polish.

Parallelizable: F3 ∥ F4 ∥ F5 ∥ frontend scaffold; F2 ∥ F7; UI work
throughout against mocks.
