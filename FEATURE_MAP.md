# FEATURE_MAP.md

Living index of product features: **where each lives in the codebase and how to
verify it**.

**Maintenance rule:** update this file whenever a feature's code paths, UI
surface, or tests change. Treat it as part of the definition of done.

Status legend: 🔲 planned · 🚧 in progress · ✅ working

## Overview

| ID | Feature | Status | Primary code | Primary tests |
|----|---------|--------|--------------|---------------|
| F0 | Dev environment & scaffold | 🔲 | `docker-compose.yml`, `db/`, repo root | `tests/test_smoke.py` |
| F1 | Submission form → run spec | 🔲 | `web/app/submit/`, `api/routers/runs.py` | `tests/unit/api/test_runs.py` |
| F2 | Orchestrator (matrix, rubric, lifecycle) | 🔲 | `orchestrator/` | `tests/unit/orchestrator/` |
| F3 | AI gateway (proxy, metering, budgets) | 🔲 | `gateway/` | `tests/unit/gateway/` |
| F4 | Sandbox runtime (spawn, egress, snapshot, done) | 🔲 | `sandbox/`, `data/artifacts/` | `tests/integration/test_sandbox.py` |
| F5 | Miniloop harness + adapter + skill packs | 🔲 | `adapters/miniloop/`, `skillpacks/` | `tests/unit/adapters/miniloop/` |
| F6 | Event store + live timeline | 🔲 | `api/routers/events.py`, `web/app/runs/[id]/timeline/` | `tests/unit/api/test_events.py` |
| F7 | Evaluator sandbox (rubric scoring) | 🔲 | `evaluator/` | `tests/integration/test_evaluator.py` |
| F8 | Results UI (matrix, scores, artifacts) | 🔲 | `web/app/runs/`, `api/routers/results.py` | `tests/e2e/` (later) |

## Feature details

### F0 — Dev environment & scaffold

- **What:** repo layout, docker-compose (Postgres 16, Ollama), Alembic base
  schema (`runs`, `sandboxes`, `events`, `rubrics`, `scores`, `artifacts`),
  pytest + lint scaffold.
- **Where:** `docker-compose.yml`, `db/migrations/`, `pyproject.toml`,
  `tests/`
- **Verify:**
  - `docker compose up -d` → Postgres healthy, `curl localhost:11434/api/tags`
    responds (Ollama)
  - `ollama pull qwen2.5-coder:3b` succeeds (default small model)
  - `alembic upgrade head` → all tables present
  - `pytest tests/test_smoke.py` green
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

### F2 — Orchestrator

- **What:** run state machine (`submitted → rubric_review → provisioning →
  running → evaluating → done | failed`); matrix expansion (harness configs ×
  model); rubric draft generated via gateway → frozen on user approval;
  termination policy watcher (timeout, budget, stuck-loop, done signal).
- **Where:** `orchestrator/` (`state_machine.py`, `lifecycle.py`,
  `rubric.py`, `termination.py`), db `runs`/`sandboxes`/`rubrics`
- **Verify:**
  - Unit: `tests/unit/orchestrator/test_state_machine.py`,
    `test_termination.py`, `test_rubric.py`
  - Manual: submit → edit/approve rubric → sandboxes spawn → terminal states
    recorded correctly
- **Depends on:** F3, F4, F5

### F3 — AI gateway

- **What:** OpenAI-compatible `/v1/chat/completions` endpoint → Ollama
  upstream; per-run/sandbox credentials map traffic to a run; records tokens
  in/out, latency, failures (cost ≈ 0 local, still recorded); budget caps;
  emits `llm_request`/`llm_response`/`error` events. Anthropic-compatible
  endpoint added later if a CLI adapter requires it.
- **Where:** `gateway/` (`app.py`, `providers/ollama.py`, `metering.py`,
  `auth.py`), event emission → F6 ingestion
- **Verify:**
  - Unit: `tests/unit/gateway/test_proxy.py`, `test_metering.py`,
    `test_budgets.py`
  - Manual: `curl` chat completion with run creds → response streams back +
    event rows in Postgres
  - Budget: exceed cap → 429 + `budget_exceeded` event
- **Depends on:** F0 + F6 ingestion endpoint (build that slice first)

### F4 — Sandbox runtime

- **What:** spawn one Docker container per sandbox on an internal network
  (egress only to gateway); identical initial workspace; injected `done`
  command on PATH; env wiring (gateway URL, run creds, task prompt); workspace
  snapshot → `data/artifacts/` on exit.
- **Where:** `sandbox/` (`runtime.py`, `images/`, `done` script),
  `data/artifacts/`
- **Verify:**
  - Integration: `tests/integration/test_sandbox.py`
    (`@pytest.mark.docker`): spawn → exec → snapshot exists
  - Manual: `curl` to external host fails inside sandbox; gateway reachable
- **Depends on:** F0; integrates with F3, F5

### F5 — Miniloop harness + adapter + skill packs

- **What:** our custom minimal coding agent — tool-call loop over the gateway
  (tools: shell exec, file read/write, `done`); skill packs vendored under
  `skillpacks/` (`superpowers`, `mattpocock-skills`, `spec-kit`), catalog
  injected into the system prompt; adapter implements the harness contract
  (`image_spec`, `env`, `launch`, `translate_events`, `completion_signal`).
- **Where:** `adapters/miniloop/` (agent program + adapter), `skillpacks/`
- **Verify:**
  - Unit: `tests/unit/adapters/miniloop/test_loop.py`, `test_tools.py`,
    `test_events.py`
  - Integration: sandbox run of "create hello.txt containing X" →
    `agent_done`, file present in artifact, harness events emitted
- **Depends on:** F3, F4

### F6 — Event store + live timeline

- **What:** append-only `events` table + ingestion API + query API + SSE
  stream; UI timeline page — live during run, replayable after with
  timestamps.
- **Where:** `api/routers/events.py` (ingest, query, SSE), db `events`,
  `web/app/runs/[id]/timeline/`
- **Verify:**
  - Unit: `tests/unit/api/test_events.py` (ingest ordering, filters)
  - UI: timeline updates live without refresh; reload preserves order
- **Depends on:** F0 (ingestion slice unblocks F3)

### F7 — Evaluator sandbox

- **What:** post-run sandboxed agent; loads artifact + frozen rubric; judges
  via gateway (metered); writes score + rationale per rubric dimension
  (functional requirements, acceptance criteria, code quality, cyclomatic
  complexity).
- **Where:** `evaluator/`, db `scores`, `api/routers/results.py`
- **Verify:**
  - Integration: `tests/integration/test_evaluator.py` — fixture artifact →
    score rows cover all rubric dimensions
  - Manual: scores + rationale visible on results page
- **Depends on:** F2, F4, artifacts from a completed run

### F8 — Results UI

- **What:** run list; per-run matrix (sandbox × rubric dimension); score +
  rationale view; link to timeline replay; artifact download; flag when
  evaluator model == participant model.
- **Where:** `web/app/runs/`, results components, `api/routers/results.py`
- **Verify:**
  - UI: complete run → compare scores side-by-side → open timeline →
    download artifact
  - Tests: `tests/e2e/` (Playwright, post-MVP)
- **Depends on:** F1, F6, F7

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
