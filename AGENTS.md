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

**MVP shape:** 1 model × 2 harnesses → 2 concurrent sandboxes. Evaluator runs
afterwards in its own sandbox.

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
3. **AI gateway (Python, custom)** — exposes both OpenAI-compatible
   (`/v1/chat/completions`) and Anthropic-compatible (`/v1/messages`)
   endpoints, translates to upstream providers (OpenRouter primary; Ollama /
   direct as configured). Per-run credentials map traffic to run+sandbox.
   Records: model, tokens in/out, cache read/write, latency, cost, failures.
   Emits events to Postgres. Enforces token/cost budgets.
4. **Sandboxes (Docker)** — per-harness image + installed skill packs + clean
   workspace + injected `done` mechanism. Env points harness at gateway with
   run-scoped creds. Egress locked to gateway — no telemetry holes.
   Adapter tails harness output and pushes normalized events.
5. **Postgres** — run specs, sandbox instances, frozen rubric, append-only
   event stream, artifact metadata, evaluator scores.
6. **Evaluator** — runs in its own sandbox post-run; fetches artifact + rubric,
   scores via gateway, returns score + rationale per dimension.
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
  cost, model), `tool_call`, `shell_command`, `file_op`, `test_execution`,
  `checkpoint`, `skill_invocation`, `done_signal`, `error`

Gateway events are captured passively from the request/response stream.
Agent-level events (tool calls, shell, file ops) are produced by the harness
adapter translating native output (e.g. Claude Code `--output-format
stream-json`, Aider logs) or harness hooks where available.

## Harness adapter contract

Each adapter provides:

- `image_spec()` — Dockerfile/image definition for the sandbox
- `install_skillpacks(packs)` — how skill packs are provisioned
- `env(gateway_url, run_creds)` — env vars wiring the harness to the gateway
- `launch(task_prompt)` — start the agent loop (non-interactive mode)
- `translate_events(stream)` → normalized `Event[]`
- `completion_signal()` — how `done` / loop-completion is detected

**MVP adapter candidates** (free/open access): Claude Code CLI, Codex CLI,
Aider (scripted minimal loop), plus a custom minimal loop harness. Providers:
OpenRouter primary; Ollama for local models; direct Anthropic/OpenAI if keys
exist.

## Rubric

MVP dimensions: functional requirements, acceptance criteria, code
implementation quality, cyclomatic complexity (LLM-assessed for MVP — cheap
static analysis is a post-MVP objective signal).

Flow: auto-generated from task spec → HITL review/edit in UI → frozen →
applied identically to all sandboxes.

## Stack

- **Backend**: Python 3.12, FastAPI, asyncio; SQLAlchemy + Alembic
- **Frontend**: Next.js (TypeScript), SSE for live timeline
- **DB**: Postgres 16 (docker-compose)
- **Sandboxes**: Docker, per-harness image, host docker socket for spawning
- **Evaluator**: sandboxed agent (same infra)
- **Artifacts**: local disk `./data/artifacts/`

## Repo layout (proposed)

```
gateway/       # LLM proxy: adapters, metering, budgets, event emission
orchestrator/  # run state machine, matrix expansion, rubric gen, lifecycle
api/           # control-plane HTTP API (FastAPI app)
adapters/      # harness adapters (claude_code, codex, aider, miniloop, ...)
sandbox/       # image defs, workspace prep, done mechanism, event collector
evaluator/     # rubric scoring agent
web/           # Next.js app
db/            # schema + Alembic migrations
data/          # artifacts (gitignored)
docs/
docker-compose.yml
```

## MVP feature map

| # | Feature | Status |
|---|---------|--------|
| F1 | Submission form → run spec | not started |
| F2 | Orchestrator: matrix, rubric gen, lifecycle | not started |
| F3 | Gateway: OpenAI+Anthropic endpoints, telemetry, budgets | not started |
| F4 | Sandbox runtime: images, skill packs, egress lockdown | not started |
| F5 | Event store + live timeline (SSE) + replay | not started |
| F6 | Evaluator sandbox: rubric scoring + rationale | not started |
| F7 | Results UI: matrix, scores, timeline, artifacts | not started |

## Post-MVP roadmap

- Model-vs-model comparisons (multi-model matrices)
- Objective scoring: build/test execution, coverage, static analysis
- Automated test-suite generation as eval target
- Higher-fidelity instrumentation via harness hooks/plugins
- Gateway response caching / deterministic replay
- Multi-host / K8s sandbox scaling
- Auth, saved run templates, share links
- Cross-run analytics & cost dashboards

## Decisions log

- Compare **harnesses, not models**: same model across sandboxes in a run.
- Custom lightweight gateway (not LiteLLM) — observability is the core value.
- Two-layer telemetry: passive gateway capture + adapter-translated harness
  events for agent-level detail.
- HITL rubric approval; frozen pre-launch; shared across sandboxes.
- Evaluator in own sandbox; UI flags evaluator == participant model.
- LLM-judge scoring only for MVP; objective checks post-MVP.
- Artifacts local; metadata in Postgres.
- Evaluator model is user-selected at submission (independence is opt-in).

## Working conventions

- Python: type hints, pydantic models at boundaries, asyncio for I/O.
- Events are append-only — never update/delete; corrections are new events.
- Keep changes small and verifiable; note new commands/config here as they
  land.
- Dev environment commands: TBD — fill in once scaffolded.
