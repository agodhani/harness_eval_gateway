# Business-eval roadmap — PR plan (phase 3)

Handoff doc for implementation agents. Read `AGENTS.md` (architecture,
conventions, decisions log), `FEATURE_MAP.md` (per-feature code paths +
verify steps), and `docs/roadmap.md` (landed PR1–PR20 — do not redo) first.
Same conventions: Python 3.12 type hints, pydantic at boundaries, append-only
events, `uv run pytest` + `uv run ruff check .` + `cd web && npm run lint &&
npm run build` stay green, update `FEATURE_MAP.md` + `AGENTS.md` as part of
done. Work each PR on its own branch; rebase on main before merge.

## Goal

Grow the platform from a coding-agent benchmark into a **business AI
evaluation tool**: compare models, harnesses, and prompts side by side on the
tasks a team actually cares about — including non-coding tasks (extraction,
summarisation, Q&A, classification) — with defensible numbers.

Source: page/capability audit (2026-11-17). The three blockers it found:

1. **Scoring is locked to four code-quality dimensions**
   (`orchestrator/rubric.py::CANONICAL_DIMENSIONS`).
2. **A task can't take inputs** — the workspace always starts empty
   (`sandbox/runtime.py` `ws.mkdir`; no seed path exists).
3. **Each matrix cell runs once** — rankings rest on a single sample, and
   there's no cost/speed honesty layer to act on the result.

## Scope

**In:** the preset-task library (PR22 anchor) plus audit tiers 1–3 —
use-case coverage (custom rubrics, task inputs, prompt-only mode, suites,
reusable rubrics), defensibility (repeats, per-sandbox budgets, real
pricing, judge quality, speed), and comparison surfaces (heatmap,
cross-run compare/leaderboard, quality-vs-cost, output diff, export,
dashboard).

**Out (unchanged, still open elsewhere):** audit tier 4 — auth/roles,
API keys & CI gating, notifications, prompt/response capture + retention,
safety signals, multi-host/K8s — plus previously planned PR16 (multi-turn),
PR19 (auth/templates/share), PR20 (K8s). This plan neither blocks nor
includes them; PR22's preset library is the natural future home for PR19's
"saved run templates".

## Dependency DAG

Hard edge (──▶) = must merge first. Soft edge (╌▶) = better after.

```
PR21 fixes ─╌╌╌▶ (everything — do first, tiny)

PR22 presets ──▶ PR26 suites ──▶ (leaderboards group by preset)
   │    └──────▶ PR32 compare/leaderboard
   └╌╌╌▶ PR23 rubric templates (preset.suggested_rubric)

PR23 rubrics ─╌▶ PR26 suites (custom dims incl. task_success)
          ─╌╌▶ PR29 judge quality (judge prompt reads dynamic dims)

PR25 prompt adapter ─╌▶ PR26 suites (prompt outputs get checkers)
PR27 repeats ─╌╌╌▶ PR31 heatmap (cells = mean±sd) ╌▶ PR32
PR28 budgets/pricing ─╌▶ PR33 scatter (honest cost axis)
PR30 speed ─╌╌╌▶ PR33 scatter (speed axis/point size)

PR32 ─╌╌▶ PR36 dashboard (reuses leaderboard aggregates)
PR31/32 ─╌▶ PR35 export (exports the matrix shapes it settles)
```

## Waves (max ~3 lanes in flight)

- **Wave A — foundations:** PR21, PR22, PR25, PR28.
- **Wave B — use-case breadth:** PR23, PR24, PR27, PR30, PR34.
- **Wave C — benchmark depth:** PR26, PR29.
- **Wave D — comparison surfaces:** PR31, PR32, PR33, PR35, PR36.

## Data model additions

One Alembic migration per PR that needs it; renumber revisions at merge —
this table is the intended owner of each column.

| Table / column | Shape | PR |
|---|---|---|
| `task_presets` | `id uuid pk, slug str uniq, title, description, task_prompt text, category str, tags jsonb, source "vendored"\|"user", revision int, suggested_rubric jsonb null (PR23), suite jsonb null (PR26), created_at, updated_at` | 22 |
| `runs.preset_ref` + `runs.preset_revision` | `str` (vendored slug or user-preset slug) + `int`, both nullable | 22 |
| `rubric_templates` | `id uuid pk, name uniq, description, content jsonb, source, created_at` | 23 |
| `runs.trials` | `int` default 1; `sandboxes.harness_config.trial_index` int | 27 |
| `harness_configs.model_options` | `jsonb` (temperature, seed, max_tokens…) on the run spec | 27 |
| `model_prices` | `id, model_glob uniq, input/output/cache_read per-Mtok, local bool, source, updated_at` | 28 |
| `scores.judge_model`, `scores.source`, `scores.note` | `str`, `"llm"\|"human"`, `text` | 29 |
| `pairwise_votes` | `id, run_id fk, left_sandbox fk, right_sandbox fk, dimension, winner, note, created_at` | 29 |
| `runs.share_token` | `str uniq` nullable — stretch only | 35 |

No migration needed for: prompt-cell kind (lives in
`harness_config.kind`), speed metrics (event payload fields), task-input
seeds (stored under `data/` + `harness_config.task_inputs`), export,
heatmap, output browser.

## Shared-file hotspots

- `api/schemas.py`, `web/lib/api.ts` — nearly every PR appends; rebase.
- `orchestrator/rubric.py`, `evaluator/prompts.py` — PR23 vs PR29.
- `orchestrator/matrix.py`, `api/routers/results.py`, results page —
  PR27 vs PR31 vs PR32.
- `gateway/metering.py` — PR28 vs PR30.
- `db/migrations/`, `AGENTS.md`, `FEATURE_MAP.md` — every PR; rebase.

---

## PR cards

### PR21 — Audit fixes batch (bughunt)
- **Scope:** five misleading behaviours found in the audit.
  1. Cost fallback honesty: unlisted models currently price at
     `claude-sonnet-4.5` (first `PRICE_TABLE` entry). Minimum fix:
     `_price_key`/`resolve_price_model` returns `None` for unlisted →
     `estimated_cost_usd=null`, UI shows `unpriced`. (Full fix = PR28.)
  2. Run-detail "Cancel run" button shows on in-flight statuses
     (provisioning/running/evaluating) that the API 409s — gate the
     button to cancellable statuses and fix the "stops its sandboxes"
     tooltip; show "delete run" for terminal states (hard-delete exists).
  3. `GET /api/runs` + runs page: `offset` exists but the UI fetches
     `limit:100` once — add pagination or "load more".
  4. Landing copy says "Benchmark the harness, not the model" / "same
     checkpoint" — update hero + `Method` constants table for the
     config×model matrix.
  5. Results "total" row ignores rubric `weight` — compute weighted
     normalised total (score/max×weight) or label the row "unweighted Σ".
- **Touch:** `api/pricing.py`, `api/routers/analytics.py`,
  `api/routers/runs.py`, `web/app/runs/[id]/RunDetailClient.tsx`,
  `web/app/runs/page.tsx`, `web/components/landing/{Hero,Method}.tsx`,
  `web/app/runs/[id]/results/ResultsClient.tsx`.
- **Verify:** pytest + a local-model analytics call shows `unpriced`;
  eslint clean.

### PR22 — Task preset library ⭐ anchor
- **Scope:** presets the user picks instead of writing a prompt, custom
  prompt remains, and any custom prompt can be saved as a new preset.
  - Vendored presets: `task_presets/<slug>.md` files — YAML frontmatter
    (`slug, title, description, category, tags`, optional
    `suggested_adapter`, `suggested_models`) + markdown body = task
    prompt. Ship ~8 starters across categories: game/app build (2048),
    bugfix-on-repo (pairs w/ PR24 seed), refactor, docs-writer, data
    analysis, extraction/classification, SQL, support-reply drafting.
  - `task_presets` table (above) for user-created; list endpoint merges
    vendored + user like `GET /api/skillpacks` does (`source` field).
    Slug namespace shared — user presets can't collide with vendored.
  - API: `GET /api/presets`, `GET/POST /api/presets/{slug}`,
    `PATCH` (user only, bumps `revision`), `DELETE` (user only).
  - Run spec: `RunCreate.preset_ref` optional; `create_run` resolves it,
    snapshots `preset_ref`+`preset_revision` onto the run. Task text still
    stored verbatim on `task_prompt` (comparison integrity).
  - Submit UI: Task card gets `custom | preset` mode toggle. Preset mode
    = searchable picker (category chips + text filter) → prefills
    read-only prompt preview; "edit as custom copy" detaches back to
    custom with the text carried over. Custom mode gets a
    "save as preset" popover (slug/title/category/tags → POST).
    Run detail gets "save prompt as preset" too.
  - `/presets` index page (table: title, category, runs-count, last-run
    mean score) + `/presets/[slug]` detail: every run on the preset,
    per-model and per-config leaderboard across those runs (mean
    overall, n), score-over-time trend (drift), + "new run on this
    preset" → `/submit?preset=slug` prefills.
  - `runs.preset_ref` surfaced: RunSummary gains `preset_ref`; runs list
    gains a preset column/filter value.
- **Touch:** `task_presets/` (new dir + seed files),
  `db/models.py`+migration, `api/routers/presets.py` (new),
  `api/routers/runs.py`, `api/schemas.py`, `api/routers/analytics.py`
  (preset leaderboard aggregate or reuse per-run results client-side),
  `web/app/submit/page.tsx`, `web/app/presets/` (new),
  `web/components/SiteHeader.tsx` (nav), `web/lib/api.ts`.
- **Verify:** run submitted from preset carries `preset_ref`; same-preset
  runs group on `/presets/[slug]`; save-as-preset round-trips.

### PR23 — Custom rubric dimensions + rubric templates
- **Scope:** unlock `CANONICAL_DIMENSIONS`. `RubricContent` accepts 1–12
  arbitrary dimensions (normalized names, dedup, min/max scale kept);
  evaluator `EVAL_PROMPT` + `scores` table already take arbitrary names —
  remove the canonical validator, alias map stays as a soft
  normalizer. `cyclomatic_complexity` dims remain meaningful only for
  code tasks — the scoring collectors keep reporting what they can and
  the judge brief says "not applicable" cleanly.
  - Rubric gen prompt gains the task's domain hint (preset category or
    submitted `rubric_hint`) so non-coding tasks get sensible dims.
  - `rubric_templates` table + vendored seeds (`rubric_templates/*.json`:
    code-default = today's four, extraction-accuracy,
    summarisation-fidelity, support-tone-compliance). Submit form
    optional `rubric_template` → used as the gen prompt's basis instead
    of freeform.
  - UI: form rubric editor (name/title/description/weight/scale rows +
    criteria list) replacing raw JSON on run detail AND the
    results-page version editor; raw JSON kept behind a toggle.
    `dimensionOrder` in `web/lib/api.ts` becomes "canonical first, then
    alpha" instead of fixed-order.
- **Touch:** `orchestrator/rubric.py`, `evaluator/prompts.py`,
  `evaluator/scores.py`, `api/routers/results.py` (version validation),
  `db/models.py`+migration, `rubric_templates/` (new), submit + run
  detail + `RescoreControls`, `web/lib/api.ts`.
- **Depends:** soft ← PR22 (preset `suggested_rubric` field populated).
- **Verify:** a rubric with dims `accuracy/tone/compliance` drafts,
  freezes, scores, and renders on matrix/charts with no canonical dim
  present.

### PR24 — Task inputs: seeded workspace
- **Scope:** `RunCreate.task_inputs`: optional `repo_url` (public git,
  shallow clone at pinned ref) or uploaded archive (zip/tar via
  `POST /api/uploads` → staged under `data/uploads/`). Provision copies
  the seed into every sandbox's `workspace_host_path` before spawn —
  identical bytes per cell (hash recorded on the sandbox row as
  `harness_config.seed_sha`). Submit UI: "Inputs" section on the Task
  card (repo URL or file picker); presets can declare a `seed` hint.
- **Touch:** `api/schemas.py`, `api/routers/runs.py` (+ new
  `uploads.py`), `orchestrator/provision.py`, `sandbox/runtime.py`
  (seed copy + hash), `web/app/submit/`, `api/routers/results.py`
  (echo seed in run config).
- **Verify:** two cells on a seeded run produce identical
  `seed_sha`; artifact workspace contains the seed + agent changes;
  jail: seeds can't escape `data/` (reuse artifact-jail pattern).

### PR25 — Prompt-only adapter (non-agentic cells)
- **Scope:** matrix cells that skip containers — one model call per
  cell. New pseudo-adapter `prompt` in the harness registry: when a
  config selects it, the orchestrator runs `orchestrator/prompt_cell.py`
  instead of `spawn_one`: mint a sandbox-scoped cred, call the gateway
  (`/v1/chat/completions`; `system_prompt_extra` → system message),
  emit `llm_request`/`llm_response` (gateway-side automatic) +
  `run_started`/`terminal_state` events on the sandbox row, write
  `output.txt` (+meta) as the artifact, mark `harness_complete`.
  Retries: one retry on upstream 5xx/timeout. The sandbox row, timeline,
  eval, and results pipeline are unchanged — a prompt cell is a
  zero-container sandbox.
- **Touch:** `orchestrator/prompt_cell.py` (new), `orchestrator/adapters.py`
  registry + lifecycle routing, `api/routers/harnesses.py` manifest,
  submit adapter dropdown (group "no sandbox"), eval sandbox spec
  (judge reads `output.txt` instead of workspace — reuse bounded
  file-read helpers), results artifact/preview guards (prompt cells
  have no preview).
- **Verify:** a `prompt` × 3-models run completes with no docker
  containers started; scores land; cost/tokens meter normally.

### PR26 — Benchmark suites + deterministic checkers ✅ shipped
- **Scope (as shipped):** suites are vendored files —
  `benchmark_suites/<slug>/suite.yaml` lists `tasks: [{id, preset_ref |
  prompt, adapter?, checks: [{check, spec}]}]` (preset-ref tasks reuse
  PR22's library; inline prompts cover freeform/prompt cells). The
  checker set is `exact | contains | regex | json_schema | numeric_tol`
  (output-oriented — prompt cells measure `output.txt` at the artifact
  root) plus `file_exists | grep | py_test | output_grep`
  (artifact/workspace-oriented; `py_test` = the card's `exec_exit_zero`
  — candidate tests run inside the locked `heg-exec` container via
  `ExecRunner`, never on the host).
  - `POST /api/suites/{slug}/run` fans out one normal run per task
    under a shared `suite_runs` batch row; `runs` carry
    `suite_ref`/`suite_index`/`suite_run_id` provenance. Adapter-pinned
    tasks with no matching harness config 422 before anything starts.
  - Post-artifact/pre-judge, the lifecycle runs the task's declared
    checks and persists append-only `checker_results` rows
    (pass/fail/skipped/error + detail) — deterministic outcomes kept
    strictly beside judge scores. A `suite` signal (pass rate +
    per-check results) lands in `analysis.json`/`ANALYSIS.md`, a
    `suite_pass` score row (0–10 scale) records the pass rate, and a
    flat `suite` summary rides `SandboxResult` — same envelope pattern
    as `generated_tests`.
  - `GET /api/suite-runs/{id}` aggregates the batch task × cell:
    per-sandbox `judge_overall` beside checker counts/pass rate, plus
    pooled `check_pass_rate` + `mean_judge_overall`. `?suite=` filters
    `GET /api/runs`; rescoring re-reads the current suite.yaml
    (newest-wins on `checker_results.id`).
  - Web: `/suites` browse, `/suites/[slug]` detail + minimal launcher,
    `/suite-runs/[id]` batch matrix, a "Deterministic checks" card on
    results (✓/✗/⊘/! per check + `suite_pass`), suite provenance on
    run detail + runs list, nav link.
  - Example suite `benchmark_suites/starter-regression/` covers a
    container task (`preset_ref`, file/grep/py_test checks) and a
    prompt-only task (`adapter: prompt`, output checkers).
- **Touch (shipped):** `benchmark_suites/` (new dir + suite.yaml schema),
  `api/suites.py` (vendored store: slug-safe, jailed paths, Pydantic
  checks), `api/routers/suites.py` (new), `scoring/checkers.py` (new —
  the `suite` signal's measurement layer), `scoring/{models,render}.py`
  (signal surface), `orchestrator/lifecycle.py` (post-artifact pass),
  `api/routers/{runs,results}.py`, `api/schemas.py`, `api/config.py`
  (`BENCHMARK_SUITES_DIR`), `db/models.py` + migration
  `e4f5a6b7c8d9` (`suite_runs`, `checker_results`, `runs.suite_*`),
  `web/lib/api.ts`, `web/app/{suites,suite-runs}/`,
  `web/app/runs/[id]/results/ResultsClient.tsx`, nav.
- **Depends:** PR22 (preset_ref tasks + provenance snapshot pattern);
  soft ← PR23 (custom dim `task_success` to fold pass rate into) +
  PR25 (prompt outputs — output checkers already target the PR25
  `output.txt` convention).
- **Verify:** a multi-case prompt-cell task yields per-case
  pass/fail + pass rate in results without judge involvement
  (`checker_results` + flat `suite` summary are DB-derived); a
  container task's `py_test` executes only inside `heg-exec`.

### PR27 — Repeats per cell + per-config model settings
- **Scope:** statistical defensibility. `RunCreate.trials` (1–8, default
  1) multiplies the matrix — each cell runs N times, `trial_index`
  stamped on the sandbox row + name suffix (`cfg · model · t3`).
  Results matrix groups trials under one config×model column header
  showing mean ± sd + n, expandable to per-trial scores; analytics adds
  variance columns; leaderboard reports `mean overall (±sd, n=3)`.
  - `HarnessConfigIn.model_options`: `{temperature?, seed?,
  max_tokens?, reasoning?}` — honored by adapters that support them
  (miniloop: temperature/max_tokens direct; CLI harnesses: mapped where
  the harness exposes a flag, else dropped with a submit-time
  `warning`). Gateway passes known keys through.
- **Touch:** `api/schemas.py`, `orchestrator/matrix.py`,
  `orchestrator/provision.py`, `adapters/miniloop/agent.py`
  (`MINILOOP_*` env already exists — wire options), adapter contract
  `env()`, submit UI (trials stepper + advanced per-config options),
  `api/routers/{results,analytics}.py` (trial rollup),
  results/analysis UI (grouping, error bars).
- **Verify:** trials=3 × 2 configs = 6 sandboxes; matrix shows
  `7.8 ±0.6 (n=3)`; per-trial expansion correct.

### PR28 — Budgets & pricing accuracy
- **Scope:** budgets that can't be starved, prices that aren't guesses.
  - `BudgetsIn` gains `sandbox_token_budget` (per-cell cap enforced by
    the monitor per sandbox, alongside the existing per-run ceiling)
    and `max_cost_usd` (priced models only: the monitor converts
    `estimated_cost_usd` per sandbox each poll; new terminal reason is
    reused `budget_exceeded` with `payload.limit="cost"`).
  - `model_prices` table + `GET/PUT /api/prices` — glob-matched
    (`gpt-4*` style, first match wins) layered over the static
    `PRICE_TABLE`; `local` flag; a "sync" that pulls OpenRouter
    `/models` pricing when that provider is configured. Unknown model →
    `price_model=null`, cost `null` → UI `unpriced` (PR21's honesty fix
    made permanent).
  - Price picker on results/analysis gains "add rate" inline.
- **Touch:** `db/models.py`+migration, `api/pricing.py`,
  `api/routers/analytics.py` (+ new `prices.py` router),
  `gateway/metering.py` (per-sandbox claims — mint per-sandbox
  `max_tokens` from `sandbox_token_budget`), `orchestrator/monitor.py`,
  submit budgets section, analytics UI.
- **Verify:** a $0.05-cost-cap run terminates `budget_exceeded`;
  unlisted model shows `unpriced` not sonnet rates; PUT a price →
  cost appears.

### PR29 — Judge quality: panel, human override, blind A/B
- **Scope:**
  - `evaluator_models: list` on run spec (1–3, extends `evaluator_model`
    — keep single-model field for back-compat). Each judge scores every
    artifact; `scores.judge_model` recorded; results cells show mean +
    spread badge when judges disagree > 2 pts; `same_model_eval` applies
    per judge.
  - Human override: `POST /api/sandboxes/{id}/scores` writes
    `source="human"` rows with `note`; they win the newest-wins merge
    for their (dimension, sandbox) pair; UI = inline edit on results
    cells + a "human" badge.
  - Blind pairwise: `POST /api/runs/{id}/pair` returns two anonymized
    artifacts (A/B labels, order randomized); reviewer picks winner per
    dimension → `pairwise_votes` rows; results page gains a "votes"
    summary (win rate per sandbox) + judge-vs-human agreement stat.
- **Touch:** `api/schemas.py`, `orchestrator/lifecycle.py` (spawn per
  judge), `evaluator/` (judge_model stamping), results API+UI,
  artifact anonymization helper (strip sandbox id/meta from the staged
  view).
- **Depends:** soft ← PR23 (dynamic dims in judge prompt + votes).
- **Verify:** 2-judge run lands 2 score rows per dim with mean shown;
  human edit overrides; a pairwise vote round-trips.

### PR30 — Speed metrics
- **Scope:** gateway records `ttft_ms` (first response byte/chunk),
  `total_ms`, derived `tokens_per_sec` on `llm_response` payloads
  (streamed + buffered paths); analytics adds `ttft avg/p95`, `tok/s
  avg`, `wall_clock` per cell + model rollup; analysis page latency
  chart gains TTFT; metrics bar adds a speed chip.
- **Touch:** `gateway/main.py`+`metering.py` (timing already partially
  there — extend), `api/routers/analytics.py`,
  `web/app/runs/[id]/{analysis,timeline}/`, `web/lib/api.ts`.
- **Verify:** llm_response payloads carry `ttft_ms`; chart renders on a
  streamed + non-streamed model alike.

### PR31 — Config × model heatmap + honest totals + winner card
- **Scope:** results page gains a `matrix | grid` view toggle: rows =
  harness configs (named label), cols = models, cell = mean overall
  (across trials when PR27 landed) color-scaled — readable at 32 cells.
  Totals row becomes weighted-normalised (respect `weight` + `min/max`
  scale). "Winner" card: top cell by weighted mean, with its cost/time/
  pass-rate alongside, plus runner-up Δ.
- **Touch:** `web/app/runs/[id]/results/` (new `GridView` component +
  weighted-total fix), `api/routers/results.py` (precomputed rollups if
  client math gets awkward).
- **Depends:** soft ← PR27 (trial means fill cells) — ships without it.
- **Verify:** grid renders 8×4; weighted total differs from raw Σ on a
  weighted rubric.

### PR32 — Cross-run compare + leaderboards ✅ shipped
- **Scope:**
  - `/compare?runs=id1,id2` — two runs side by side: spec diff
    (models/configs/budgets/rubric version), score matrices,
    analytics deltas, terminal-reason mix.
  - `/leaderboard` — global rankings: by model (mean overall across all
    scored sandboxes, n, ±sd) and by harness config family (adapter +
    pack set); filters: preset, date range, min trials. Data =
    `GET /api/analytics/leaderboard` (new aggregate over scores join
    sandboxes join runs).
  - `/presets/[slug]` (PR22) embeds the leaderboard filtered to that
    preset — "compare prompts that are the same" surface.
- **Touch:** `api/routers/analytics.py` (new aggregate),
  `web/app/compare/`, `web/app/leaderboard/` (new), nav, `web/lib/api.ts`.
- **Depends:** PR22 (`preset_ref` grouping) — leaderboard works
  unfiltered before that but the preset view needs it.
- **Verify:** leaderboard ranks 2 models correctly on fixture data;
  compare shows spec diff for a resubmitted-with-changes pair.

### PR33 — Quality / cost / speed scatter + auto summary
- **Scope:** analysis page gains a scatter: x = est. cost per task
  (PR28 honest prices), y = weighted score (PR31), point size = tok/s or
  wall-clock (PR30), color = config; Pareto frontier highlighted;
  unpriced cells plotted on a tokens axis with a note. Below it a
  generated summary card — deterministic template (no LLM call):
  "X scored highest (8.4, +1.9 vs runner-up) at $0.03/run in 4.2 min;
  Y is 60% cheaper for −0.4 score."
- **Touch:** `web/app/runs/[id]/analysis/` (new chart + summary
  component), `web/lib/` transform + tests.
- **Depends:** soft ← PR28 + PR30 + PR31.
- **Verify:** scatter renders on mixed local+hosted run; unpriced cells
  don't silently vanish.

### PR34 — Artifact file browser + side-by-side diff
- **Scope:** `GET /api/sandboxes/{id}/files` — jailed, depth/size-bounded
  listing of the artifact `workspace/`; `GET .../files/{path}` returns
  text (size-capped) or download; `?diff=<other_sandbox_id>&path=` →
  server-side unified diff for text files. UI: results artifacts cell
  gets "browse" → modal/drawer file tree + viewer; "diff vs" picker →
  split diff view (reuse the PR10 split idiom for file lists; per-file
  diff in a `<pre>` or lightweight diff lib).
- **Touch:** `api/routers/results.py` (new endpoints, jail like the
  download path), `web/app/runs/[id]/results/` (browser + diff
  components), `web/lib/api.ts`.
- **Verify:** browse a 2048 artifact, open `index.html`; diff two
  sandboxes' `index.html` shows unified diff; path traversal 404s.

### PR35 — Export + share
- **Scope:** `GET /api/runs/{id}/export?fmt=json|csv` — full run bundle:
  spec, rubric, score matrix (+versions), analytics, events-lite
  (summary rows, not full payloads); CSV = flattened per-cell rows.
  Buttons on results/analysis headers. Stretch (auth-adjacent):
  `runs.share_token` + read-only `/share/{token}` route rendering
  results+timeline read-only — flag for PR19 territory; implement only
  if cheap.
- **Touch:** `api/routers/results.py` (export endpoint),
  `web/app/runs/[id]/{results,analysis}/` (download buttons),
  `web/lib/api.ts`.
- **Verify:** JSON export re-imports/validates against schemas; CSV
  opens in sheets with one row per sandbox×dimension.

### PR36 — Dashboard home ✅ shipped
- **Scope:** `/dashboard` (nav + landing CTA): cards — total est. spend
  over time, runs per week, model leaderboard top-5 (PR32 aggregate),
  harness leaderboard top-5, terminal-reason mix donut, queue/active
  status, recent runs table. New `GET /api/analytics/summary` aggregate
  (or compose from leaderboard + trends client-side if cheaper).
- **Touch:** `web/app/dashboard/` (new), `api/routers/analytics.py`,
  `SiteHeader`, `web/lib/api.ts`.
- **Depends:** soft ← PR32 (leaderboard aggregate reuse).
- **Verify:** dashboard renders on a seeded DB; empty state clean.

---

## Definition-of-done additions for this program

- Any PR touching matrix expansion or run spec updates the matrix-cap
  math + submit hint text.
- Preset text is compared by content hash too: `/presets/[slug]` warns
  when a run's stored `task_prompt` drifted from the preset revision it
  claims (`preset_revision` mismatch badge).
- Prompt cells must not regress the "every sandbox is egress-locked"
  guarantee: the control-plane runner uses the same sandbox-scoped cred
  rules (no `x-heg-provider`).
- Every new analytics aggregate ships a `node --test` transform test or
  a pytest equivalent (house convention).
