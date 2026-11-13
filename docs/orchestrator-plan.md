# Orchestrator Plan — Phase 2 (F2 + run API surface)

Implementation spec for the F2 orchestrator and the `/api/runs*` endpoints.
Read `AGENTS.md`, `FEATURE_MAP.md`, and the Phase-1 code first; this doc assumes
them. Everything below maps to existing code.

## 0. Facts that constrain the design (verified in Phase-1 code)

- **`terminal_state` never reaches the DB.** miniloop emits
  `run_started`/`context_compacted`/`terminal_state` via `EventEmitter`, but
  `api.schemas.EventType` (`api/schemas.py:27`) allows only 10 types →
  `/internal/events` 422s them silently. **Terminal detection must use
  `docker logs` (`MINILOOP_RESULT` line) + container exit codes +
  `done_signal`/gateway `error` events.** Also add the 3 missing types to the
  enum (cheap fix; `Event.type` is `String(64)`, no migration).
- **Creds:** `gateway/creds.py:mint_cred(run_id, sandbox_id|None, secret,
  ttl_seconds, max_tokens)`. `sandbox_id=None` gives run-level creds (rubric
  gen, evaluator). Gateway budget is **per-run**: `metering.tokens_used`
  (`gateway/metering.py:43`) sums `llm_response` tokens across the run, and a
  cred's `max_tokens` claim sets the ceiling → participant creds get
  `max_tokens=token_budget`; rubric/evaluator creds get
  `max_tokens = tokens_used(run) + EVAL_TOKEN_BUDGET` — an additive allowance
  so they can't 429 once participants burn the budget (their tokens still
  count toward the run total; negligible).
- **Skillpacks mount is all-or-nothing** (`sandbox/runtime.py` mounts all of
  `./skillpacks`). Per-config subsets need a staged dir + a new
  `SandboxSpec.skillpacks_host_path` field. Symlinks don't survive bind mounts
  → **real copies** (packs are small markdown).
- **`system_prompt_extra`** is a comparison axis (AGENTS.md) but miniloop has
  no hook → add `SYSTEM_PROMPT_EXTRA` env support (~4 lines in `agent.py`).
- **api process needs Docker access** to spawn sandboxes: no docker CLI in the
  image, no socket mount in compose — changes listed in §7.
- **DB needs no migration** — `runs.config`/`rubrics.content`/`scores` fit.
  `Sandbox` has no `created_at`; order by `id`/`started_at`.
- `SandboxRuntime` is **synchronous** (`subprocess.run`, timeouts to 300 s) →
  every call from async code goes through `asyncio.to_thread`. DB helpers are
  the exception: they run directly on the event-loop thread (tiny queries;
  sharing one sqlite `:memory:`/StaticPool connection across `to_thread`
  workers segfaults — see `db/session.py`).

## 1. Run spec + endpoints — `api/routers/runs.py`

### Schemas (add to `api/schemas.py`)

```python
class HarnessConfigIn(BaseModel):
    adapter: str = "miniloop"                      # must be in ADAPTERS registry
    skill_packs: list[str] = Field(default_factory=list, max_length=16)
    system_prompt_extra: str | None = Field(default=None, max_length=8000)

class BudgetsIn(BaseModel):
    max_iters: int = Field(default=50, ge=1, le=1000)
    token_budget: int = Field(default=200_000, ge=1_000)   # PER-RUN (see §6)
    wall_clock_seconds: int = Field(default=3600, ge=60, le=86_400)

class RunCreate(BaseModel):
    task_prompt: str = Field(min_length=1, max_length=100_000)
    model_name: str = Field(min_length=1, max_length=255)
    harness_configs: list[HarnessConfigIn] = Field(min_length=1, max_length=8)
    evaluator_model: str | None = Field(default=None, max_length=255)
    budgets: BudgetsIn = Field(default_factory=BudgetsIn)
```

Responses: `SandboxOut` (id, harness_config, status, terminal_reason,
artifact_path, started_at, finished_at), `RubricOut` (id, run_id, version,
status, content, created_at), `RunOut` (id, status, task_prompt, model_name,
config, `same_model_eval`, timestamps, `sandboxes: list`, `rubric | None`),
`RunSummary` (id, status, task_prompt[:200], model_name, created_at,
finished_at, sandbox_count). `same_model_eval` =
`(evaluator_model or model_name) == model_name`, computed at submit, stored in
`config`, echoed in responses.

`Run.config` stored shape:

```json
{"harness_configs": [...], "evaluator_model": "...", "budgets": {...},
 "same_model_eval": true}
```

### Endpoints

| Method/Path | Behavior |
|---|---|
| `POST /api/runs` | Validate (below) → insert `Run(status="submitted")` → `runner.start_rubric_task(run_id)` → **201** `RunOut` (rubric=None). |
| `GET /api/runs` | `?status=&limit=(50,≤200)&offset=` → `list[RunSummary]`, `created_at DESC`. |
| `GET /api/runs/{id}` | `RunOut` incl. sandboxes + latest rubric. 404. |
| `GET /api/runs/{id}/rubric` | Latest `Rubric` by version. 404 if none. |
| `PUT /api/runs/{id}/rubric` | Body `{"content": RubricContent}` — only when `run.status=="rubric_review"` **and** `rubric.status=="draft"`; replaces `content` in place (version stays 1). Else 409. 422 on schema violation. |
| `POST /api/runs/{id}/approve` | Same preconditions as PUT; sets `rubric.status="approved"`, transitions run → `provisioning`, sets `started_at`, emits orchestrator event, `runner.start_lifecycle_task(run_id)`. → `RunOut`. |

Submit-time validation → 422: `adapter ∈ ADAPTERS`, every `skill_packs[]` entry
is a dir under `./skillpacks/`, non-empty `task_prompt`/`model_name`,
`evaluator_model` defaults to `model_name` when omitted.

Statuses (db comment `models.py:46`): run =
`submitted → rubric_review → provisioning → running → evaluating → done | failed`;
sandbox = `provisioning | running | terminating | done | failed`
(+ `terminal_reason`).

## 2. Rubric generation — `orchestrator/rubric.py`

Triggered by the background task started in `POST /api/runs` (not in-request —
LLM call can take minutes on a 3B model).

- `async def draft_rubric(run_id) -> None`: load run → mint run cred
  (`sandbox_id=None`, `ttl=3600`, `max_tokens = tokens_used + EVAL_TOKEN_BUDGET`)
  → POST
  `{settings.GATEWAY_URL}/v1/chat/completions` (httpx, `timeout=180`,
  `temperature=0.2`, `max_tokens=2048`, `stream=False`) with
  `model = settings.RUBRIC_MODEL or run.model_name` → parse → validate →
  **single transaction**: insert `Rubric(version=1, status="draft")` +
  `run.status="rubric_review"` + orchestrator `checkpoint` event. Any failure →
  `run.status="failed"` + `error` event (resubmit is the recovery; no retry
  endpoint in MVP).
- `parse_rubric_json(text) -> RubricContent`: strip ```` ``` ```` fences /
  leading prose, `json.loads`, validate. On failure, **one** retry with a
  "respond with ONLY the JSON object" nudge appended.
- `RubricContent` (pydantic, `extra="allow"`):
  `summary: str`, `dimensions: list[{name, title, description, weight=1.0,
  min_score=0.0, max_score=10.0}]`, `criteria: list[{id, text, dimension,
  weight=1.0}]`, `generated_by: dict`.
  **Validator:** `dimensions` must contain exactly the 4 canonical names —
  `functional_requirements`, `acceptance_criteria`, `code_quality`,
  `cyclomatic_complexity` (AGENTS.md MVP set; F7 contract depends on them).
  LLM variants are normalized by name-match; missing → validation error.
- Prompt sketch (system): *"You design evaluation rubrics for coding-agent
  benchmarks. Given a task, output ONLY a JSON rubric: the 4 fixed dimensions
  below (each with description + 0–10 scale) plus a task-specific `criteria`
  list decomposing the prompt's requirements."* + dimension names inline.
  User msg = `task_prompt` + "Return JSON only."

## 3. Provisioning — `orchestrator/provision.py` + `lifecycle.py`

`run_lifecycle(run_id)` (the approve-triggered task) does, sequentially:

1. `run.status="provisioning"` (already set at approve; emit `checkpoint`).
2. **Per `harness_config`** (N ≤ 8, sequential `to_thread` calls — docker run
   ≈1 s each):
   - `sid = uuid4()`; insert `Sandbox(id=sid, run_id, harness_config=cfg,
     status="provisioning")` and **commit before spawn** — sandbox rows must
     exist before events referencing them ingest.
   - Stage skillpacks: `shutil.copytree` each selected pack dir into
     `{rt.state_dir}/{sid}/skillpacks/` (empty list → empty dir). This dir is
     bind-mounted, so it must live under the host-visible state dir.
   - Mint cred: `ttl_seconds = wall_clock + 3600`, `max_tokens = token_budget`.
   - `env = adapter.env(gateway_url="http://gateway:8080",
     cred, task_prompt, model_name, events_ingest_url=
     "http://gateway:8080/internal/events", max_iters=...)`; add
     `SYSTEM_PROMPT_EXTRA` when set. (`INGEST_TOKEN` not needed —
     `EventEmitter` falls back to `GATEWAY_CRED`, and `/internal/events`
     accepts creds.)
   - `SandboxSpec(sandbox_id=str(sid), run_id=str(run.id),
     image=settings.SANDBOX_IMAGE, env, workspace_host_path=
     {rt.state_dir}/{sid}/workspace, skillpacks_host_path=staged_dir,
     cmd=adapter.launch({...})["cmd"])`.
   - `await to_thread(rt.spawn, spec)` → set `docker_container_id`,
     `status="running"`, `started_at=now`. Spawn exception → sandbox
     `status="failed"`, `terminal_reason="error"`, `finished_at`, error event;
     **continue with remaining configs**.
3. If ≥1 sandbox running → `run.status="running"`; else `run.status="failed"`
   (all spawns failed) + `finished_at`.
4. `await asyncio.gather(*(watch_sandbox(...) for running sandboxes))` — one
   monitor task per sandbox (§4). Spawn-failed sandboxes skip monitoring (no
   container) but still count as terminal for run completion.
5. All terminal → `run.status="evaluating"` → evaluator pass (§5) → `done` or
   `failed` + `finished_at`.

**Runtime/spec additions required:**
- `sandbox/spec.py`: `skillpacks_host_path: Path | None = None`.
- `sandbox/runtime.py:spawn`: mount `spec.skillpacks_host_path or
  _SKILLPACKS_HOST_DIR`; add `logs(sandbox_id, tail=200) -> str`
  (`docker logs --tail N`, stdout+stderr merged).
- `adapters/miniloop/agent.py:main`: append `SYSTEM_PROMPT_EXTRA` env to the
  system prompt when set.
- `adapters/miniloop/adapter.py`: add `parse_terminal_state(logs: str) ->
  str | None` — scans for the last `MINILOOP_RESULT ` line, returns its
  `terminal_state` (reuses `translate_events`).

## 4. Monitoring / termination — `orchestrator/monitor.py`

`watch_sandbox(run, sandbox, rt, budgets, session_factory)` — poll loop,
`MONITOR_POLL_SECONDS=5`:

Each iteration (DB reads via short-lived sessions on the event-loop thread):

1. **Deadline:** `now > sandbox.started_at + wall_clock_seconds` → terminate
   `timeout`.
2. **Budget:** any `error` event for this sandbox with
   `payload.reason=="budget_exceeded"` or `http_status==429` → terminate
   `budget_exceeded`. Also `metering.tokens_used(run_id) >= token_budget` →
   terminate `budget_exceeded` (run-wide; all monitors trip ~together — the
   gateway will 429 stragglers anyway).
3. **Container state** (`to_thread(rt.status, sid)`):
   - `running` → check `done_signal` event seen; if older than
     `DONE_GRACE_SECONDS=30` → terminate `agent_done` (safety net).
   - `exited`/`dead` → `logs = to_thread(rt.logs, sid, tail=500)` →
     `adapter.parse_terminal_state(logs)` → verdict; **fallback** by
     `exit_code`: `0`→`harness_complete` (or `agent_done` if `done_signal`
     seen), `2`/other nonzero→`error`.
   - `missing` → `error`.
4. **Terminate path** (timeout/budget/manual): sandbox `status="terminating"`
   → `to_thread(rt.stop, sid, remove=False)` → proceed to finalize.

**Verdict → finalize** (`finalize_sandbox`, always runs once per sandbox):
`to_thread(rt.snapshot, sid, run.id)` → insert `Artifact` rows
(`workspace/`→kind `workspace`, `transcript.log`→`transcript`, `meta.json`→
`meta`; `size_bytes` via file walk) → `sandbox.artifact_path`,
`status = "done"` for `agent_done|harness_complete|stuck_loop`, `"failed"` for
`timeout|budget_exceeded|error`; `terminal_reason`, `finished_at`; orchestrator
`checkpoint` event. Then `to_thread(rt.remove, sid)` — keep snapshot first.

Mapping table (first match wins; `MINILOOP_RESULT` carries `terminal_state`):

| Signal | terminal_reason | sandbox.status |
|---|---|---|
| result=`agent_done`, or `done_signal`+exit 0 | `agent_done` | done |
| result=`harness_complete`, or exit 0 w/o done | `harness_complete` | done |
| result=`stuck_loop` | `stuck_loop` | done |
| result=`error`, exit 2, other nonzero, missing | `error` | failed |
| wall-clock deadline | `timeout` | failed |
| gateway 429 / budget event | `budget_exceeded` | failed |

## 5. Evaluation trigger + contract — `orchestrator/lifecycle.py` tail, `evaluator/`

When all sandbox monitors return **and** run still `running`: →
`evaluating`. For each sandbox **with `artifact_path`** (spawn-failed ones
skip), call the F7 contract — sequential `await` per sandbox, each wrapped in
`asyncio.wait_for(timeout=EVAL_TIMEOUT_SECONDS=1800)` + try/except:

```python
# evaluator/__init__.py — CONTRACT (F7 implements; orchestrator only calls)
async def evaluate_sandbox(
    *,
    run_id: UUID, sandbox_id: UUID, rubric_id: UUID,
    rubric: dict,                      # frozen Rubric.content
    artifact_dir: Path,                # <ARTIFACT_ROOT>/<run>/<sb>/
    workspace_dir: Path, transcript_path: Path,
    terminal_reason: str | None,
    model: str,                        # evaluator_model
    gateway_base_url: str,             # settings.GATEWAY_URL
    gateway_cred: str,                 # per-eval cred, sandbox-scoped,
                                       # max_tokens = spent + EVAL_TOKEN_BUDGET
    session_factory: sessionmaker,     # F7 writes Score rows + emits events
    timeout_seconds: float = 1800.0,
) -> list[Score]: ...                  # raises on failure
```

F7 writes `Score(sandbox_id, rubric_id, dimension, score, rationale)` rows and
emits `source="evaluator"` events; whether it spawns its own sandbox is F7's
business — the contract is transport-agnostic. Orchestrator mints a fresh
evaluator cred **per sandbox** inside the eval loop (`sandbox_id` = the
sandbox under evaluation, `ttl=EVAL_TIMEOUT+600`,
`max_tokens = tokens_used + EVAL_TOKEN_BUDGET`).

Run completion: per-sandbox eval exceptions → `error` event, continue. If
total `Score` rows for the run == 0 → `run.status="failed"` + error event
("evaluation produced no scores"); else → `done` + `finished_at`.

## 6. Concurrency & edge cases

- **N sandboxes parallel:** monitor tasks via `asyncio.gather`; provisioning
  sequential (cheap, simpler DB handling).
- **Partial failure:** spawn/eval failures are per-sandbox; run still reaches
  `done` if ≥1 sandbox produced scores. All-spawn-fail or zero-score → `failed`.
- **api restart:** lifespan `startup_sweep` (`orchestrator/runner.py`):
  `submitted` runs with no rubric → respawn rubric task; `provisioning`/
  `running`/`evaluating` → `failed` (+ sandboxes `failed`/`error`, best-effort
  `docker rm -f` by `heg.run_id` label). `rubric_review` untouched (human
  gate). In-process tasks die with the process — accepted MVP limitation,
  documented.
- **Duplicate submit:** no idempotency for MVP — each POST creates a run;
  UI disables double-submit. Note for post-MVP.
- **Cred TTL:** participant `ttl = wall_clock + 3600`; rubric/evaluator creds
  fresh at use, TTL ≥ their timeout. `max_tokens` = run budget on participant
  creds; `tokens_used + EVAL_TOKEN_BUDGET` (additive allowance) on
  rubric/evaluator creds.
- **`token_budget` is per-run**, not per-sandbox (gateway semantics). If
  per-sandbox budgets are wanted later, monitor sums per-sandbox
  `llm_response` tokens — flagged in open questions.
- **LLM timeouts:** rubric call 180 s + 1 retry; evaluator `wait_for` 1800 s.

## 7. File/module layout + wiring

```
orchestrator/
  state_machine.py   # RunStatus/SandboxStatus, ALLOWED_TRANSITIONS,
                     # transition(session, run, to) -> emits checkpoint event
  events.py          # emit(session, run_id, sandbox_id|None, type, payload)
                     #   -> Event(source="orchestrator") direct insert
  rubric.py          # RubricContent models, prompt, parse, draft_rubric(run_id)
  adapters.py        # ADAPTERS={"miniloop": adapters.miniloop.adapter};
                     #   get_adapter(name) -> module or raises
  provision.py       # stage_skillpacks, known_skillpacks() (dir names under
                     #   ./skillpacks for submit validation), build_env,
                     #   spawn_one(hc) -> Sandbox
  monitor.py         # watch_sandbox loop, parse/verdict, finalize_sandbox
  lifecycle.py       # run_lifecycle(run_id): provision -> gather monitors ->
                     #   evaluate -> done/failed
  runner.py          # _TASKS registry {run_id: Task}; start_rubric_task,
                     #   start_lifecycle_task, startup_sweep, shutdown
api/routers/runs.py  # §1 endpoints (thin: validate -> db -> runner calls)
api/main.py          # include runs.router; lifespan -> runner.startup_sweep /
                     #   shutdown (use @asynccontextmanager lifespan)
api/config.py        # + GATEWAY_CRED_SECRET, RUBRIC_MODEL="",
                     #   SANDBOX_IMAGE="heg-sandbox:latest",
                     #   MONITOR_POLL_SECONDS=5.0, EVAL_TIMEOUT_SECONDS=1800
api/schemas.py       # + run/rubric schemas; EventType += run_started,
                     #   context_compacted, terminal_state
evaluator/__init__.py# evaluate_sandbox stub (NotImplementedError until F7)
```

**Pattern:** approve/POST handlers only *register* `asyncio.Task`s; all
orchestration lives in `orchestrator/`. DB access from tasks: short-lived
`session_factory()` sessions called directly on the event-loop thread — sync
SQLAlchemy, but NEVER inside `asyncio.to_thread` (one shared sqlite
`:memory:`/StaticPool connection can't be hit from worker threads; file-backed
sqlite and Postgres pool per-session connections — see `db/session.py`).
`SandboxRuntime` singleton built once in `runner.py`
with `artifact_root=settings.ARTIFACT_ROOT`. Everything DI-friendly
(`runtime`, `session_factory`, `evaluator_fn` injectable) for tests.

**Required non-orchestrator changes:**
- `Dockerfile`: add docker CLI — `COPY --from=docker:cli
  /usr/local/bin/docker /usr/local/bin/docker` (verify tag/arch).
- `docker-compose.yml` api service: `- /var/run/docker.sock:/var/run/docker.sock`,
  replace `./data/artifacts:/data/artifacts` with `./data:/data` (workspace
  bind-mounts must be **host** paths — `data/sandboxes/` too), env
  `GATEWAY_CRED_SECRET: dev-secret-change-me` (+ optional `RUBRIC_MODEL`),
  `depends_on: gateway`. Host-run dev (`uvicorn api.main:app`) works unchanged.
- `api/config.py` additions above; `.env.example` mirrors them.

## 8. Test plan

Unit (`tests/unit/orchestrator/`, sqlite via `tests/conftest.py` pattern —
`Base.metadata.create_all`, `get_session_factory`):

- `test_state_machine.py` — every allowed transition; illegal → raises;
  `transition()` writes checkpoint event.
- `test_rubric.py` — `parse_rubric_json` (bare JSON, fenced, prose-wrapped,
  invalid→retry signal); `RubricContent` canonical-dimension enforcement;
  `draft_rubric` with mocked httpx transport → rubric row + `rubric_review`.
- `test_provision.py` — env dict per adapter (cred/model/max_iters/
  SYSTEM_PROMPT_EXTRA); skillpack staging copies only selected packs;
  cred claims (`max_tokens`, `ttl`); `SandboxSpec.skillpacks_host_path` set.
- `test_monitor.py` — verdict mapping table (fake runtime: status/logs/
  events); deadline→`timeout`; 429 event→`budget_exceeded`; exit-code
  fallbacks; done-grace.
- `test_lifecycle.py` — `FakeRuntime` (scripted statuses/logs) + fake
  evaluator: happy path 2 sandboxes→`done`+scores; one spawn fails→still
  `done`; all fail→`failed`; evaluator raises→`failed` only if zero scores.
- `tests/unit/api/test_runs.py` — POST 201/422s (bad adapter, bad pack,
  empty configs); GET list/detail; PUT rubric guards (wrong status→409,
  bad content→422); approve guards + transition to `provisioning`
  (mock `runner.start_lifecycle_task`).

Integration (`tests/integration/test_orchestrator.py`, `@pytest.mark.docker`):
build `heg-testapp` + `heg-sandbox` images; run lifecycle with
`SANDBOX_IMAGE=heg-testapp:latest` + `wall_clock_seconds=15` → assert
`timeout` terminal + artifacts; second case with miniloop image and no
reachable gateway → `error` terminal. Skips without docker (existing
`_require_docker` fixture pattern).

## 9. Open questions for orchestrator

1. **`token_budget` scope:** per-run (recommended — matches gateway
   enforcement today) or per-sandbox (needs monitor-side per-sandbox token
   accounting + asymmetric cred budgets)? *Default: per-run.*
2. **Evaluator placement:** contract is an in-process async callable; F7 may
   spawn its own sandbox internally. Confirm F7 owns that decision —
   orchestrator stays agnostic. *Default: yes, contract as spec'd.*
3. **Rubric model:** `RUBRIC_MODEL` env override, default = participant
   `model_name` (keeps MVP single-model; metered uniformly). A fixed cheap
   default (e.g. `qwen2.5-coder:3b`) may draft better JSON — preference?
   *Default: env override → model_name.*
4. **api-in-compose Docker socket:** adding `docker` CLI +
   `/var/run/docker.sock` to the api image is the standard local-dev pattern
   but is root-equivalent on the host. Acceptable for this local single-user
   tool? *Default: yes; alternative is host-run api for Phase 2.*
5. **Initial workspace:** MVP uses an empty per-sandbox workspace (greenfield
   tasks like "build 2048"). Any need for a seeded repo/template in Phase 2?
   *Default: no — post-MVP.*
