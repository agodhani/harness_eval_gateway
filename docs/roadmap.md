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
  sandbox runtime + egress lockdown, miniloop + 4 CLI adapters (codex,
  claude_code, pi, deepseek), evaluator w/ judge retries, results UI,
  analytics (F9), skillpack import (F11), artifact preview static+desktop
  (F12).
- **PR2 preview fixes**: `type=` POST-body override on the artifact preview
  (`plan_for_type` picks the first candidate of a type; `entry`+`type` forces
  the plan — `static` serves any jailed file, `desktop` requires `.py`) and a
  `ScanCache`'d results-GET probe (`PREVIEW_SCAN_TTL_SECONDS`, default 30s).
  Details: `FEATURE_MAP.md` F12.
- **OpenRouter provider** (commit 8014338): `UPSTREAM_PROVIDER=ollama|openrouter`
  in `gateway/config.py`; client base normalizes to `/api/` (httpx
  concatenates base+request paths); Ollama-only body params
  (`think`/`num_ctx`/`keep_alive`/`options`) stripped for hosted providers;
  tests pin `UPSTREAM_PROVIDER=ollama` in `tests/conftest.py`. OpenRouter
  only serves `/v1/chat/completions` + `/v1/models` — `/v1/messages` and
  `/v1/responses` 404 there (claude_code/codex need ollama). Verified free
  ids: `particle-ai/particle-1:free`, `inclusionai/ling-3.2-flash:free`,
  `dots-studio/dots-3-note-preview:free`. Mandatory-reasoning models (ernie,
  lfm) 400 on miniloop's `reasoning_effort:"none"`.

## Dependency DAG

Hard edge (──▶) = must merge first (shared files or required infra).
Soft edge (╌▶) = works better after / benefits from.

```
PR1  Submit UX ────────▶ PR14 Model-vs-model ◀╌╌╌╌ PR12 Providers ──▶ PR18 Cache/replay
   │                                                         │
   ├╌ labels ╌▶ PR9  Timeline-analytics ──▶ PR10 Split-view   ├╌╌╌▶ PR13 Aider ╌╌╌▶ PR17 Hooks
   ├╌ labels ╌▶ PR10
   └╌ labels ╌▶ PR11 Graphical analysis

PR2  Preview fixes ────▶ PR3  Preview types ──▶ PR4  Thumbnails

PR6  Static analysis ──▶ PR7  Exec+coverage ──▶ PR8  Test-suite-gen
                            ▲
PR5  Re-score/rub-edits ╌╌╌╌┘

PR15 Queue/mem-aware ──▶ PR16 Add-next-prompt ╌╌▶ PR20 K8s
PR19 Auth/templates/share — independent
```

## Topological levels

- **Depth 0 (start immediately):** PR1, PR3, PR5, PR6, PR9, PR11, PR12,
  PR15, PR19 — plus PR13/PR17 if soft-only deps are acceptable.
- **Depth 1:** PR7 (←6, +5 soft), PR10 (←9), PR14 (←1, +12 soft),
  PR16 (←15), PR18 (←12).
- **Depth 2:** PR4 (←3), PR8 (←7).
- **Floating:** PR20 (soft ←15).

Critical path depth = 3 (`6→7→8`; `3→4` is depth 2 now that PR2 landed).
Max safe concurrency ≈ 9 lanes.

## Shared-file hotspots (merge-conflict risk)

- `api/schemas.py`, `web/lib/api.ts` — most PRs append here.
- `AGENTS.md`, `FEATURE_MAP.md` — every PR updates docs; rebase.
- `db/migrations/` — PR1 + PR5 add migrations; renumber revisions at merge.
- `orchestrator/` — PR5 (re-score trigger) vs PR15/PR16 (state machine);
  stagger C and I lanes or coordinate.
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

### PR3 — Preview types
- **Scope:** `python-server` preview type (run candidate code like desktop —
  needs the runner+socat-proxy topology, zero-egress internal net);
  more desktop types: pyglet/arcade/tkinter (baked deps or wheelhouse mount —
  no egress in preview nets).
- **Touch:** `sandbox/preview.py` (`scan_preview` candidates, `_spawn_*`),
  `api/routers/preview.py`, `sandbox/images/*-preview.Dockerfile`,
  `PreviewControls.tsx`, `api/config.py` `PREVIEW_*`.
- **Verify:** `tests/integration/test_preview.py` (@pytest.mark.docker).

### PR4 — Artifact thumbnails
- **Scope:** lightweight screenshots of produced artifacts for at-a-glance
  review before spawning a full preview sandbox (reuses preview spawn infra
  + a headless capture step).
- **Touch:** `sandbox/preview.py` or new `sandbox/thumbnails.py`, artifact
  store layout (`data/artifacts/<run>/<sandbox>/`), results API + UI.

### PR5 — Eval iteration: re-score + rubric edits
- **Scope:** re-score runs (re-run evaluator on stored artifacts — trigger
  endpoint + orchestrator hook); post-run rubric edits (versioned rubrics —
  `rubrics` get a version/parent, scores keep `rubric_version` provenance so
  old vs new scores stay distinguishable).
- **Touch:** `orchestrator/` (eval trigger — likely `runner.py`/`lifecycle.py`),
  `api/routers/results.py`, `evaluator/`, db `rubrics`/`scores` (migration),
  results UI (re-score button + version selector).
- **Verify:** `tests/unit/evaluator/`, orchestrator unit tests; re-score on a
  stored run end-to-end.

### PR6 — Objective scoring I: static analysis
- **Scope:** real cyclomatic complexity + static metrics as rubric signals
  (today LLM-assessed). No code execution — parse/analyze only.
- **Touch:** new module (e.g. `evaluator/analysis/` or `scoring/`),
  `evaluator/evaluate.py` wiring, `api/schemas.py` score payloads.
- **Verify:** unit tests on sample workspaces with known complexity.

### PR7 — Objective scoring II: exec + coverage
- **Scope:** execute candidate code in a locked sandbox — build success,
  test pass, coverage as score dimensions. Reuse sandbox-runtime isolation
  patterns (egress lockdown, resource bounds).
- **Touch:** `evaluator/` (exec staging), `sandbox/runtime.py` or a new
  exec sandbox spec, `api/routers/results.py`.
- **Depends on:** PR6 (signal plumbing), PR5 soft (evaluator wiring settles).
- **Verify:** integration test executing a trivial artifact.

### PR8 — Automated test-suite generation
- **Scope:** LLM-generated tests as the eval target — generate suite via
  gateway, run via PR7 exec infra, fold pass-rate into scoring.
- **Touch:** `evaluator/` (gen + exec orchestration), prompts.
- **Depends on:** PR7.

### PR9 — Timeline-side analytics
- **Scope:** per-sandbox metrics (cost/tokens/latency) rendered alongside
  the timeline or on event hover — F9 already computes everything; mostly
  UI placement.
- **Touch:** `web/app/runs/[id]/timeline/`, `web/lib/api.ts`
  (`getRunAnalytics` exists).
- **Verify:** visual + a unit test on any new transform.

### PR10 — Split-view trace comparison
- **Scope:** side-by-side, time-aligned event streams for two sandboxes on
  the timeline page (today: one merged stream + a single-sandbox filter
  dropdown); scrolling/diff-alignment UX TBD.
- **Touch:** `web/app/runs/[id]/timeline/` (new split component),
  `web/lib/api.ts` if new query params needed.
- **Depends on:** PR9 (same page — land it first).

### PR11 — Graphical analysis page + cross-run dashboards
- **Scope:** charts over F9 analytics + scores: per-dimension score
  comparison across sandboxes, token/cost/latency per sandbox, tool-call
  mix, eval-vs-participant spend split, cross-run trends; **also a totals
  row / aggregate column on the results score matrix** (per-sandbox winner
  at a glance). Decision needed: charting lib.
- **Touch:** new page (e.g. `web/app/runs/[id]/analysis/`), results matrix
  component, `api/routers/analytics.py` if new aggregates needed.
- **Verify:** renders on real run data; totals match matrix sums.

### PR12 — Providers (remainder)
- **Scope:** remaining upstreams — direct Anthropic + OpenAI adapters
  (OpenRouter already landed); per-request/per-model provider routing if
  wanted (today's switch is process-wide).
- **Touch:** `gateway/config.py`, `gateway/deps.py`,
  `gateway/main.py` (body transforms if APIs differ), `api/config.py`
  (key plumbing), `.env.example`.
- **Verify:** `tests/unit/gateway/test_upstream_provider.py` pattern.

### PR13 — Aider adapter (+ dsh helpers if needed)
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

### PR15 — Run pacing: submission queue + memory-aware scheduling
- **Scope:** serialize runs so a new submission queues behind the active
  one; gate `spawn_one` on host memory pressure (docker info/cgroup stats)
  and queue pending sandboxes until headroom exists (matters at 8x).
- **Touch:** `orchestrator/runner.py`, `orchestrator/provision.py`,
  `orchestrator/monitor.py`, run state machine (queued state?).
- **Verify:** unit tests with stubbed memory readings + queue ordering.

### PR16 — "Add next prompt" (multi-turn runs)
- **Scope:** continue a finished sandbox with a follow-up prompt on the
  same workspace — new phase in the event stream + artifact re-snapshot;
  resume-in-place vs respawn-from-snapshot TBD (decide at kickoff).
- **Touch:** `orchestrator/` state machine + lifecycle, `api/routers/runs.py`,
  sandbox runtime (resume semantics), run detail UI.
- **Depends on:** PR15 (same files; pacing settles first).

### PR17 — Higher-fidelity instrumentation via hooks/plugins
- **Scope:** richer agent-level events via harness hooks/plugins where the
  CLIs support them (beyond adapter-translated output).
- **Touch:** `adapters/common/` shim, `api/schemas.py` EventType (maybe new
  types — contract test AST-scans emitters), adapter translate fns.

### PR18 — Gateway response caching / deterministic replay
- **Scope:** cache upstream responses keyed on request for deterministic
  replay (dev/CI aid — cheap reruns).
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
