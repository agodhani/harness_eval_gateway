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
| F7 | Evaluator sandbox (rubric scoring) | ✅ | `evaluator/` | `tests/unit/evaluator/` |
| F8 | Results UI (matrix, scores, artifacts) | ✅ | `web/app/runs/`, `api/routers/results.py` | `tests/unit/api/test_results.py` |
| F9 | Run analytics (per-sandbox metrics + cost) | ✅ | `api/routers/analytics.py`, `api/pricing.py`, `web/app/runs/[id]/results/AnalyticsSection.tsx` | `tests/unit/api/test_analytics.py` |

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

- **What:** form captures task prompt, model, harness configs (miniloop +
  skill pack selections), evaluator model; `POST /api/runs` persists spec with
  status `submitted`.
- **Where:** `web/app/submit/page.tsx` + form components,
  `api/routers/runs.py`, `api/schemas.py`, db `runs` table
- **Verify:**
  - UI: fill form → submit → run appears with status `submitted`
  - API: `POST /api/runs` → 201 + run id; invalid spec → 422
  - Tests: `tests/unit/api/test_runs.py`
- **Depends on:** F0

### F2 — Orchestrator ✅

- **What:** run state machine (`submitted → rubric_review → provisioning →
  running → evaluating → done | failed`); matrix expansion (harness configs ×
  model); rubric draft generated via gateway → frozen on user approval;
  termination policy watcher (timeout, budget, stuck-loop, done signal).
- **Where:** `orchestrator/` (`state_machine.py`, `lifecycle.py`,
  `provision.py`, `monitor.py`, `rubric.py`, `runner.py`, `events.py`,
  `adapters.py`), db `runs`/`sandboxes`/`rubrics`
- **Verify:**
  - Unit: `tests/unit/orchestrator/` (`test_state_machine.py`,
    `test_lifecycle.py`, `test_monitor.py`, `test_provision.py`,
    `test_rubric.py`)
  - Manual: submit → edit/approve rubric → sandboxes spawn → terminal states
    recorded correctly
- **Depends on:** F3, F4, F5

### F3 — AI gateway ✅

- **What:** OpenAI-compatible `/v1/chat/completions` endpoint → Ollama
  upstream; per-run/sandbox HMAC credentials (`heg.<b64>.<sig>`); records
  tokens in/out, latency, failures; token budgets (429 + `error` event);
  emits `llm_request`/`llm_response`/`error` events. `/internal/events`
  (harness-event proxy, cred or ingest-token auth) + `/internal/done`
  (done_signal) for sandbox egress. Anthropic-compatible endpoint post-MVP.
- **Where:** `gateway/` (`main.py`, `creds.py`, `metering.py`, `deps.py`,
  `config.py`, `mint_cred.py` CLI), compose service `gateway` (:8081→8080)
- **Verify:**
  - Unit: `tests/unit/gateway/` (creds, metering, proxy — hermetic)
  - Manual: mint cred (`uv run python -m gateway.mint_cred`) →
    `curl localhost:8081/v1/chat/completions` → completion + event rows
  - M2 e2e proven: sandbox→gateway→Ollama→events (see run
    `a58dd3de-1b71-4c4e-9a06-ad5d2ce5210e` in dev DB)
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
  progressive-disclosure catalog in system prompt.
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
- **Where:** `api/routers/events.py`, `api/schemas.py` (EventSource/EventType
  enums), db `events`, `web/app/runs/[id]/timeline/`
- **Verify:**
  - Unit: `tests/unit/api/test_events.py` + `test_event_types.py` (13+ tests)
  - Live: SSE verified via curl; M2 events persisted
  - UI: run → timeline page shows live events
- **Depends on:** F0 (ingestion slice unblocks F3)

### F7 — Evaluator sandbox ✅

- **What:** post-run sandboxed agent (same miniloop image as judge); loads
  staged artifact copy + frozen rubric; judges via gateway (metered,
  per-sandbox cred); writes score + rationale per rubric dimension
  (functional requirements, acceptance criteria, code quality, cyclomatic
  complexity); `scores.json` parsed into `Score` rows with salvage-mode
  parsing; `EVAL_TRANSCRIPT.log` written for evidence.
- **Where:** `evaluator/` (`evaluate.py`, `sandbox_spec.py`, `scores.py`,
  `prompts.py`), db `scores`, `api/routers/results.py`
- **Verify:**
  - Unit: `tests/unit/evaluator/` (`test_evaluate.py`, `test_scores.py`,
    `test_prompts.py`) — mocked runtime; staging, timeout salvage, malformed
    scores covered
  - Manual: scores + rationale visible on results page
- **Depends on:** F2, F4, artifacts from a completed run

### F8 — Results UI ✅

- **What:** run list; per-run matrix (sandbox × rubric dimension); score +
  rationale view; link to timeline replay; artifact download; flag when
  evaluator model == participant model.
- **Where:** `web/app/runs/` (list, detail, `results/`), results components,
  `api/routers/results.py` (results + scores + artifact-download endpoints)
- **Verify:**
  - UI: complete run → compare scores side-by-side → open timeline →
    download artifact
  - Unit: `tests/unit/api/test_results.py` (matrix, scores, download jail);
    e2e: `tests/e2e/` (Playwright, post-MVP)
- **Depends on:** F1, F6, F7

### F9 — Run analytics ✅

- **What:** `GET /api/runs/{id}/analytics?price_model=` — per-sandbox metrics
  aggregated from the `events` stream: llm turns, token split
  (in/out/cache/total), latency (avg/p95/max), tool-call + skill-invocation
  breakdowns, shell/file/test/error counts, wall clock, estimated cost at a
  selectable price model (`api/pricing.py` `PRICE_TABLE`; local models
  flagged `local` → $0). Judge (evaluator) traffic shares the participant
  sandbox_id via its cred, so it's split out at the `eval_started` checkpoint
  and reported under `evaluator` rather than polluting participant numbers.
  Results page gains an "Analytics" section — side-by-side comparison table +
  price-model dropdown that recomputes cost client-side (no refetch).
- **Where:** `api/pricing.py`, `api/routers/analytics.py`, response schemas
  in `api/schemas.py`, `web/app/runs/[id]/results/AnalyticsSection.tsx`,
  `web/lib/api.ts` (`getRunAnalytics`, `estimateCostUsd`)
- **Verify:**
  - Unit: `tests/unit/api/test_analytics.py` (aggregation math, eval-phase
    split, cost at known price model, defaults, 404/422, zero-event run)
  - API: `curl localhost:8001/api/runs/<id>/analytics?price_model=gpt-4o`
  - UI: results page → "Analytics" card → switch price model → cost updates
- **Depends on:** F6 (events), F8 (results page)

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
