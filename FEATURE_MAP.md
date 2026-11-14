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
| F10 | Multi-harness adapters (Codex, Claude Code, Pi, DeepSeek) | ✅ | `adapters/common/`, `adapters/{codex,claude_code,pi,deepseek}/`, `orchestrator/adapters.py`, `sandbox/images/*.Dockerfile` | `tests/unit/adapters/`, `tests/integration/test_adapter_images.py` |
| F11 | Skillpack import from GitHub | ✅ | `skillpack_import/`, `api/routers/skillpacks.py`, `web/app/submit/ImportSkillPackDialog.tsx` | `tests/unit/api/test_skillpacks.py`, `test_skillpack_import.py` |
| F12 | Artifact preview (static html + desktop/pygame via noVNC, locked-down containers) | ✅ | `sandbox/preview.py`, `api/routers/preview.py`, `sandbox/images/desktop-preview.Dockerfile`, `web/app/runs/[id]/results/PreviewControls.tsx` | `tests/unit/sandbox/test_preview.py`, `tests/unit/api/test_preview.py`, `tests/integration/test_preview.py` |
| F13 | Graphical analysis page + cross-run trends (recharts) | ✅ | `web/app/runs/[id]/analysis/`, `api/routers/analytics.py` (`GET /api/analytics/runs`), `web/app/runs/[id]/results/ResultsClient.tsx` (totals row) | `tests/unit/api/test_analytics.py` (trends section) |

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
  skill pack selections — five adapters available, F10), evaluator model;
  `POST /api/runs` persists spec with status `submitted`. The adapter
  dropdown is populated live from `GET /api/harnesses` (static fallback list
  when the API is unreachable); model inputs use the ComboInput suggestion
  dropdown.
- **Where:** `web/app/submit/page.tsx` + form components,
  `web/components/ComboInput.tsx` (model suggestion dropdown),
  `web/app/globals.css` (`.combo*`),
  `web/lib/api.ts` (`listHarnesses`, `HarnessManifest`, `ADAPTERS`),
  `api/routers/runs.py`, `api/routers/harnesses.py`, `api/schemas.py`,
  db `runs` table
- **Verify:**
  - UI: fill form → submit → run appears with status `submitted`
  - API: `POST /api/runs` → 201 + run id; invalid spec → 422
  - Tests: `tests/unit/api/test_runs.py` (incl. `test_create_run_multi_adapter`),
    `tests/unit/api/test_harnesses.py`
- **Depends on:** F0

### F2 — Orchestrator ✅

- **What:** run state machine (`submitted → rubric_review → [queued →]
  provisioning → running → evaluating → done | failed`); matrix expansion
  (harness configs × model); rubric draft generated via gateway → frozen on
  user approval; termination policy watcher (timeout, budget, stuck-loop,
  done signal).
- **Pacing (PR15):** one active run at a time — approvals while a run
  executes (or is queued) park in `queued` and promote FIFO by approval
  (`pacing.place_run` / `claim_next_queued`; promotion is wired into
  `runner._lifecycle_then_promote` + re-kicked by `startup_sweep`). Spawn
  memory gate: `spawn_one` waits for `SPAWN_MIN_FREE_MEM_MB` host headroom
  before `docker run` (probed via `/proc/meminfo` → `sysconf` → `docker
  info`+`stats`), capped by `SPAWN_MEM_WAIT_SECONDS`, expiry fails that
  sandbox — remaining configs then fail fast (one probe each) so pressure
  can't hold the slot for N×cap. Pacing events: `run_queued`/`run_dequeued`
  (run scope),
  `spawn_mem_wait`/`spawn_mem_release`/`spawn_mem_timeout` (sandbox scope).
- **Where:** `orchestrator/` (`state_machine.py`, `lifecycle.py`,
  `provision.py`, `monitor.py`, `rubric.py`, `runner.py`, `pacing.py`,
  `events.py`, `adapters.py`), db `runs`/`sandboxes`/`rubrics`
- **Verify:**
  - Unit: `tests/unit/orchestrator/` (`test_state_machine.py`,
    `test_lifecycle.py`, `test_monitor.py`, `test_provision.py`,
    `test_rubric.py`, `test_pacing.py`), queue E2E in
    `tests/unit/api/test_runs.py::test_approve_queues_behind_active_run`
  - Manual: submit → edit/approve rubric → sandboxes spawn → terminal states
    recorded correctly; approve a second run mid-flight → `queued` badge →
    promotes when the first finishes
- **Depends on:** F3, F4, F5

### F3 — AI gateway ✅

- **What:** OpenAI-compatible `/v1/chat/completions` endpoint → Ollama
  upstream — non-streaming by default, metered SSE pass-through when the
  caller sets `stream: true` (pi and other OpenAI-compatible CLIs stream
  unconditionally); Anthropic Messages `/v1/messages` +
  `/v1/messages/count_tokens` (Claude Code, dsh) and OpenAI Responses
  `/v1/responses` (Codex) pass-throughs, streaming preserved with a metering
  tee; `GET /v1/models` for CLI probes; per-run/sandbox HMAC credentials
  (`heg.<b64>.<sig>`); records tokens in/out, latency, failures; token
  budgets (429 + `error` event); emits `llm_request`/`llm_response`/`error`
  events. `/internal/events` (harness-event proxy, cred or ingest-token
  auth) + `/internal/done` (done_signal) for sandbox egress.
- **Where:** `gateway/` (`main.py`, `creds.py`, `metering.py`, `deps.py`,
  `config.py`, `mint_cred.py` CLI), compose service `gateway` (:8081→8080)
- **Verify:**
  - Unit: `tests/unit/gateway/` (creds, metering, proxy — hermetic; protocol
    pass-throughs + SSE usage normalization covered)
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
- **Run-detail console:** `web/components/RunConsoleLive.tsx` — per-sandbox
  event lanes on the run detail page (backlog + SSE + `after_id` poll, same
  merge as the timeline; evaluator/orchestrator traffic routed to a bottom
  lane). Shares the `evc-*` stylesheet in `globals.css` with the landing
  replay console.
- **Verify:**
  - Unit: `tests/unit/api/test_events.py` + `test_event_types.py` (13+ tests)
  - Live: SSE verified via curl; M2 events persisted
  - UI: run → timeline page shows live events; run detail shows live lanes
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
  **Objective signals (PR6):** before staging, the candidate workspace is
  measured by the `scoring` package's signal collectors — `static_analysis`
  runs lizard (multi-language, parse-only, no code execution) for real
  cyclomatic complexity, nloc, function counts, nesting depth, and language
  mix. The report is staged into the eval workspace as `ANALYSIS.md`
  (judge-facing brief; judge is told to score `cyclomatic_complexity` from
  measured values) + `analysis.json`, persisted at
  `<artifact>/analysis.json`, summarized in an `eval_analysis` checkpoint
  event, and re-validated + embedded as `analysis` on
  `GET /api/runs/{id}/results` sandboxes. Candidate-authored
  `ANALYSIS.md`/`analysis.json` files are dropped at staging (reserved
  names — fake measured evidence can't reach the judge). Collection never
  blocks evaluation: collector failures degrade to `status="error"` signal
  entries / `status="unavailable"` event. Collector registry
  (`scoring/collectors.py::COLLECTORS`) is the extension point for PR7's
  exec/coverage signals.
- **Where:** `evaluator/` (`evaluate.py`, `sandbox_spec.py`, `scores.py`,
  `prompts.py`), `scoring/` (`models.py` — analysis.json/API pydantic
  contract, `collectors.py` — SignalCollector protocol + registry,
  `static.py` — lizard collector, `render.py` — ANALYSIS.md), db `scores`,
  `api/routers/results.py` (`SandboxResult.analysis`),
  `api/schemas.py` (AnalysisReport re-export)
- **Verify:**
  - Unit: `tests/unit/evaluator/` (`test_evaluate.py`, `test_scores.py`,
    `test_prompts.py`) — mocked runtime; staging, timeout salvage, malformed
    scores covered; `tests/unit/scoring/` (`test_static.py`,
    `test_collectors.py`, `test_render.py`) — workspaces with known CC
    values, skip/cap/symlink rules, collector isolation, markdown render
  - Results API: `tests/unit/api/test_results.py` — analysis embedded when
    `<artifact>/analysis.json` exists+valid, null otherwise
  - Manual: scores + rationale visible on results page; rationale for
    cyclomatic_complexity cites measured CC values
- **Depends on:** F2, F4, artifacts from a completed run

### F8 — Results UI ✅

- **What:** run list; per-run matrix (sandbox × rubric dimension); score +
  rationale view; link to timeline replay; artifact download; flag when
  evaluator model == participant model. The run-detail rubric table also
  renders per-sandbox score columns + an `overall` row once results exist
  (`RunDetailClient.tsx` `RubricView` + `getRunResults` poll while
  `evaluating`).
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

### F10 — Multi-harness adapters ✅

- **What:** pluggable adapter registry so runs can compare real harness CLIs,
  not just miniloop configs. Registered adapters: `miniloop`, `codex`
  (@openai/codex), `claude_code` (@anthropic-ai/claude-code), `pi`
  (@mariozechner/pi-coding-agent), `deepseek` (@deepseek-ai/dsh). A shared
  stdlib-only shim (`adapters/common/`) runs inside each image: writes the
  harness's native config pointing at the gateway, launches the CLI in
  machine-readable JSONL mode, translates native records → normalized
  events, posts them to `/internal/events`, emits `terminal_state` + prints
  `HEG_RESULT {…}`. Per-adapter Docker images pin the npm CLI version;
  `orchestrator/provision.py` resolves each config's image from
  `image_spec()["image"]` (miniloop keeps the `SANDBOX_IMAGE` fallback).
  `GET /api/harnesses` serves adapter manifests (display name, LLM protocol,
  CLI, image) for the submit form; `HarnessConfigIn.adapter` validates
  against the registry. Design doc: `docs/p25-harness-adapters.md`.
- **Where:** `adapters/common/` (`runner.py`, `contract.py`),
  `adapters/{codex,claude_code,pi,deepseek}/` (`adapter.py`, `config.py`,
  `translate.py`, `__main__.py`), `orchestrator/adapters.py`,
  `orchestrator/provision.py`, `sandbox/images/{codex,claude_code,pi,
  deepseek}.Dockerfile`, `api/routers/harnesses.py`
- **Verify:**
  - Unit: `tests/unit/adapters/test_adapter_contract.py` (contract across all
    adapters: manifest, image_spec, env, launch, translation → valid
    `EventType`s, `HEG_RESULT` parsing) + per-adapter tests under
    `tests/unit/adapters/<name>/`
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
  see/play the artifact in a new browser tab. Types: `"static"` (html) and
  `"desktop"` (pygame executed under Xvnc, streamed via noVNC).
  - `GET /api/runs/{run}/results` → per-sandbox `previewable` +
    `preview_type` (cheap FS probe of the jailed workspace).
  - `POST /api/runs/{run}/sandboxes/{sandbox}/preview` → `201
    {preview_id, url, type, status, entry, expires_at}`; idempotent (a live
    preview is returned as-is — unless an explicit `entry`/`type` requests
    a different plan, which stops it and respawns); `404`
    run/sandbox/workspace missing, `422` zero candidates / bad `entry` /
    unsupported type, `502` docker failure. Optional body `{"entry":
    "rel/path"}` picks an entry — jailed to the workspace, must exist and
    be non-symlinked; `.html`/`.htm` → static, `.py` → desktop (explicit
    `.py` bypasses pygame evidence — the pick IS the disambiguation).
    `{"type": "static"|"desktop"}` overrides the pick: alone it selects the
    first detected candidate of that type (a both-matching workspace
    defaults to static — `{"type": "desktop"}` reaches the game); with
    `entry` it forces the plan — `static` serves any jailed file (view a
    `.py`'s source — the entry is only the URL opened), `desktop` still
    requires a `.py` entry. No body → the scan's default
    (`candidates[0]`).
  - `GET .../preview` → current state (any status) or `404`; `DELETE
    .../preview` → `204` stop.
- **Detection** (`sandbox/preview.py::scan_preview` → `PreviewScan{plan,
  candidates}`): `candidates` = EVERY runnable entry as typed plans,
  ordered default-first — `plan`/`candidates[0]` is just the dropdown's
  default selection, never a hidden decision. Static entries first
  (root `index.html`/`.htm` → other root `*.html` sorted → `*/index.html`
  one level down sorted), then desktop `.py` entries (root + nested ≤3
  levels; hidden dirs, symlinks, `_DESKTOP_SKIP_DIRS` —
  node_modules/__pycache__/tests/venv/dist/build/docs/... — skipped;
  ordered shallower → named-rank → path). A `.py` is a candidate iff it
  has pygame evidence — file imports pygame (line-anchored; comments
  can't match), OR entry-shaped (named entry, or a dir containing
  exactly one `.py`) AND a scanned `requirements.txt` names pygame on a
  non-comment line. So a `static+pygame` workspace defaults to static
  yet still offers the desktop entry; helper .py files with real pygame
  imports are offered too.
- **UI**: `preview_candidates` (typed `{entry,type}`) rides
  `SandboxResult` → the dropdown lists each candidate plus type-override
  variants (`index.html (site)` / `game/main.py (desktop)` /
  `game/main.py (view source)` — a `.py` can be served instead of
  executed) + Preview button POSTing `{entry, type}`; one option → plain
  button; zero → disabled button + "not supported" (artifact still
  downloadable — preview is an add-on).
- **Static posture** (`_spawn_static`): `python:3.12-slim` running
  `python -m http.server` over the workspace bind-mounted `:ro`; nobody
  user, `--read-only` rootfs, `--cap-drop ALL`, `no-new-privileges`.
  Dedicated bridge `heg-preview` (NOT `--internal` — Docker silently drops
  published ports on internal networks, verified) with
  `enable_ip_masquerade=false` (egress block on Linux; inert on Docker
  Desktop). Port publishes on `127.0.0.1` ephemeral only — no LAN exposure.
- **Desktop posture** (`_spawn_desktop` — executes candidate code, so
  stricter): the runner sits alone on `--internal` `heg-desktop` (zero
  egress, zero published ports, only peer = its own proxy) with
  `--cpus`/`--memory`/`--pids-limit` bounds (`PREVIEW_DESKTOP_*` defaults
  1 / 512m / 256), cap-drop ALL, no-new-privileges, nobody. A pinned
  `alpine/socat:1.8.1.3` sidecar (`heg.preview_role=proxy`) straddles
  `heg-desktop` + `heg-preview` — a dumb TCP pipe publishing `127.0.0.1`
  port → runner's websockify `:6080` (noVNC UI at `/vnc.html`). The runner
  image `heg-preview-desktop` (`sandbox/images/desktop-preview.Dockerfile`,
  repo-root build context) bundles TigerVNC Xvnc + novnc + websockify +
  pygame-ce; candidate deps beyond pygame can't install (no egress) — the
  app fails at import and the runner exits visibly. Artifact is mounted
  `:ro` then copied to writable `/work` (games may save state).
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
  `plan_for_type` overrides, `ScanCache`, `PreviewManager`),
  `api/routers/preview.py` (+ reaper),
  `api/config.py` (`PREVIEW_*` incl. `PREVIEW_SCAN_TTL_SECONDS`),
  `api/routers/results.py` (cached previewable probe via `_scan_cache`),
  `sandbox/images/desktop-preview.Dockerfile` +
  `desktop-preview-entrypoint.sh`,
  `web/lib/api.ts` (`getPreview`/`startPreview`/`stopPreview`, `PreviewOut`),
  `web/app/runs/[id]/results/PreviewControls.tsx`
- **Verify:**
  - Unit: `tests/unit/sandbox/test_preview.py` (static + desktop detection
    — nested depth bound, skip-dirs, candidate ordering/default-first,
    non-named `.py` evidence, comment-stripped requirements,
    `plan_for_entry` jail/validation + `type=` override, `plan_for_type`,
    `ScanCache` hit/expiry/LRU/clear; manager via stubbed docker —
    runner/proxy args, role labels, stop-all) +
    `tests/unit/api/test_preview.py` (lifecycle, `entry=`/`type=`
    overrides, live-preview replace-on-different-plan, default-spawn
    semantics, typed `preview_candidates`, cached results probe)
  - Docker: `tests/integration/test_preview.py` (`@pytest.mark.docker` —
    static spawn → HTTP 200, localhost-only binding, reaper; desktop spawn
    builds `heg-preview-desktop` once per session → real pygame executes
    (PYGAME_OK in logs), runner on internal net with limits, sidecar
    publishes localhost:6080, vnc.html 200, both containers cleaned)
  - Manual: results page → "Preview" on a pygame artifact → noVNC desktop
    opens in new tab; "■ stop". Build image first:
    `docker build -f sandbox/images/desktop-preview.Dockerfile -t
    heg-preview-desktop:latest .`
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
  (grouped horizontal bars + "Σ leader" line), tool-call mix (stacked,
  top-7 tools + `other`), tokens in/out (stacked), LLM latency
  (avg/p95/max), estimated cost per sandbox (price-model dropdown →
  client-side recompute, same trick as AnalyticsSection — local models show
  a note instead of empty bars), participant-vs-evaluator spend split
  (stacked; judge priced at the evaluator model's rate when listed), and a
  cross-run trends ComposedChart (stacked participant+evaluator token bars
  + mean-overall-score line on a right 0–10 axis; newest 20 runs, current
  run accent-highlighted, hover shows run meta). The results score matrix
  also gains a **totals row** (Σ dimension scores, `total`) — distinct from
  the judge-reported `overall` — with the max cell flagged `leader`.
- **API:** `GET /api/analytics/runs?limit=&price_model=` →
  `RunTrends{price_model|null, price_table, points[]}` — per-run roll-up of
  the same participant/evaluator event split (eval boundary per sandbox),
  latest-`overall`-score-per-sandbox mean, participant + judge tokens,
  errors, cost. No `price_model` → each point costed at its own run's
  resolved price ("auto", `price_model:null`); with it → uniform repricing.
  Newest-first; `limit` 1–100 default 20.
- **Where:** `api/routers/analytics.py` (`get_run_trends` +
  `_eval_boundaries`/`_is_eval_event` helpers, also used by the per-run
  endpoint), `api/schemas.py` (`RunTrendPoint`, `RunTrends`),
  `web/app/runs/[id]/analysis/` (`page.tsx`, `AnalysisClient.tsx`,
  `charts.tsx`), `web/app/runs/[id]/results/ResultsClient.tsx` (`row-total`
  + `dimTotals`/`maxTotal` memos), `web/lib/api.ts` (`getRunTrends`,
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
  - UI: results page → matrix `total` row sums match the dimension columns
    and flags the leader; run detail → "Analysis →" → all cards render on a
    scored run; cost dropdown reprices the cost + split charts client-side.
- **Depends on:** F6 (events), F7 (scores), F8 (results page), F9
  (analytics aggregation)

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
