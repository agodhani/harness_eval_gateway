# PR: F12 — Artifact preview (static serving + desktop/noVNC streaming)

**Branch:** `feat/p25-artifact-preview` → `main`
**Feature:** F12 in `FEATURE_MAP.md`

Lets a reviewer **see and run** the artifact a sandbox produced, straight from
the results page — before this, artifacts were download-only. Two preview
types ship behind the same Preview column:

- **static** — artifact workspaces containing HTML are served as-is
  (`python -m http.server`) so the user opens the built game/site in a new
  tab.
- **desktop** — pygame (and future GUI) artifacts are *executed* inside a
  hardened container under Xvnc and streamed to the browser via noVNC, so an
  interactive game is playable without installing anything locally.

## UX

The Artifacts table gains a Preview column (`web/app/runs/[id]/results/`):

| Detected entries | Control |
|---|---|
| 1 | plain **Preview** button |
| >1 | dropdown of every detected option (`index.html (site)`, `game/main.py (desktop)`) + Preview — the detected default is preselected, never a hidden pick |
| 0 | disabled button + "not supported" |

Clicking spawns a preview and shows a type badge (`site`/`desktop`), an
**↗ open** link, a **stop** chip, and the expiry time. One live preview per
sandbox (POST is idempotent). No manual path entry anywhere in the UI.

## Isolation design

Artifact code is untrusted — it's treated as hostile both times it's
touched (detection never executes it; spawning only ever runs *inside*
containers).

### Static (`heg-preview` bridge, localhost-only)

- Dedicated bridge network `heg-preview` (not `heg-sandbox`) with IP
  masquerade disabled — no published-port-on-`--internal` workaround needed
  because Docker silently drops port bindings on internal networks; the
  bridge + `127.0.0.1` publish is the reachable-isolated compromise
  (decision: **Option A**, egress control is best-effort at the net level).
- Container: `python:3.12-slim`, `--user 65534:65534`, `--read-only`,
  `--cap-drop ALL`, `no-new-privileges`, workspace bind-mounted **ro**,
  port published on `127.0.0.1` only → `http://localhost:<port>/`.

### Desktop (`heg-desktop` internal net + socat sidecar)

Executed code gets the stronger posture:

- Runner (`heg-preview-desktop` image: Xvnc + noVNC + pygame-ce,
  `ENV DISPLAY=:0`) attaches **only** to `heg-desktop`, created
  `--internal` → zero egress, zero published ports. Workspace is staged
  into `/work` (GUI apps need writes).
- A pinned `alpine/socat:1.8.1.3` sidecar straddles `heg-desktop` +
  `heg-preview` and forwards the published localhost port to the runner's
  noVNC port. Dumb TCP pipe — nothing for the artifact to exploit; the API
  never joins the runner's network (proxying through the API was rejected:
  it would give candidate code a path to the control plane).
- Resource bounds: `--cpus`, `--memory`, `--pids-limit`
  (`PREVIEW_DESKTOP_CPUS/MEMORY/PIDS`, defaults 1 cpu / 512MiB / 256 pids).
- Deps beyond baked-in pygame can't install (no egress) — the app fails at
  import and the runner exits visibly.

### Shared lifecycle

- **Label registry** — `heg.preview_id`, `heg.run_id`, `heg.sandbox_id`,
  `heg.preview_role` (`runner`/`proxy`), `heg.preview_type`, entry — so all
  state is restart-safe docker-side, not in memory or Postgres. Stop/remove
  and the reaper work out-of-process and clean up *all* containers of a
  preview (runner + proxy together).
- **TTL reaper** — `PREVIEW_TTL_SECONDS` (900s default), swept every
  `PREVIEW_REAP_SECONDS` (30s); registered as an api lifespan task.
- Artifact paths are jailed under `ARTIFACT_ROOT` (traversal + symlink
  escapes rejected) — same jail as artifact download.

## Detection: typed candidate list

`scan_preview(workspace)` → `PreviewScan{plan, candidates}` — **every**
detected runnable entry as typed `PreviewPlan`s, ordered default-first;
`plan`/`candidates[0]` is only the dropdown's default:

- `.html`/`.htm` → static. Order: root `index.*` → other root html → nested
  `*/index.html`.
- `.py` → desktop, with **conservative pygame evidence**: the file itself
  imports pygame (line-anchored regex, comments ignored), *or* it's
  entry-shaped (named entry like `main.py`/`game.py`, or the only `.py` in
  its dir) **and** `requirements.txt` names pygame on a non-comment line.
- Scanned at root + ≤3 nested levels; hidden dirs, `node_modules`,
  `__pycache__`, `tests`, `venv`, `dist`, `build`, `docs`, `site-packages`
  and symlinks skipped. Static wins as default when both match; desktop
  entries still appear in the dropdown.
- `plan_for_entry()` validates an explicit `entry` against the workspace
  jail (exists, not a symlink, `.html`/`.htm`/`.py`, resolves inside) — the
  API escape hatch; an explicit `.py` skips evidence scanning because the
  pick *is* the disambiguation and isolation is identical either way.

## Endpoints (`api/routers/preview.py`)

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/runs/{run}/sandboxes/{sb}/preview` | `201 PreviewOut`; optional body `{entry: "game/main.py"}`; idempotent — returns the live preview if one exists; `422` when no runnable candidate / bad entry |
| `GET` | …same | current preview or `404` |
| `DELETE` | …same | `204`; removes runner + proxy |

`GET /api/runs/{id}/results` now returns per-sandbox `previewable`,
`preview_type`, and `preview_candidates: [{entry, type}]` (every detected
option, default-first) so the UI can render choices without extra calls.

`PreviewPlan.type`/`PreviewOut.type` stay open (`static | desktop | …`) for
`python-server` previews later.

## Files changed (vs `main`)

| Area | Files |
|---|---|
| Manager/detection | `sandbox/preview.py` (new, ~1050 loc) — `Preview`, `PreviewScan`, `scan_preview`, `detect_preview`, `plan_for_entry`, `PreviewManager` (spawn/get/stop-all/reaper) |
| API | `api/routers/preview.py` (new), `api/routers/results.py` (+`PreviewCandidate`, probing), `api/config.py` (+`PREVIEW_*`), `api/main.py` (reaper lifespan) |
| Desktop image | `sandbox/images/desktop-preview.Dockerfile`, `…-entrypoint.sh` (Xvnc + websockify + app, supervisor-lite) |
| Web | `web/lib/api.ts` (types + `startPreview({entry})`), `…/results/PreviewControls.tsx` (new), `…/results/ResultsClient.tsx` (Preview column) |
| Tests | `tests/unit/sandbox/test_preview.py` (~1020 loc), `tests/unit/api/test_preview.py` (~600 loc), `tests/integration/test_preview.py` (~330 loc) |
| Docs | `FEATURE_MAP.md` (F12), `AGENTS.md` (decisions), this file |

## Verification

- `uv run ruff check .` — clean
- `uv run pytest -m "not docker"` — **350 passed** (110 of them
  preview-specific: detection, jail, manager lifecycle, API contract,
  stop-all, idempotent POST, candidate ordering, `entry=` override)
- `uv run pytest tests/integration/test_preview.py` — **4 passed, real
  Docker**: static container actually serves an html artifact; a real
  pygame app runs under Xvnc/noVNC and reports `display ok`, proxied via
  the socat sidecar on the internal net
- `cd web && npm run lint` + `npm run build` — clean
- Screenshots: `docs/screenshots/p25-artifact-preview/`

## Decisions

- **Option A egress for static**: dedicated bridge + localhost-bound port +
  hardened container; `--internal` was verified to silently drop published
  ports, so bridge-level masquerade-off is the best-effort egress cap.
- **Proxy-hop isolation for desktop**: executed artifact on a no-egress
  internal net; only the pinned socat sidecar is reachable from the host.
  Never give the runner a route to the control plane.
- **Auto-detect + dropdown, no typing**: every detected candidate is shown,
  the detected default is preselected; `entry=` remains the scripted escape
  hatch (jailed, evidence-bypassing for `.py`).
- **No user TTL**: fixed 15-min TTL + 30s reaper; previews are ephemeral
  reviewer conveniences, not services.
- Detection never executes artifact code; execution only ever happens in
  the locked-down desktop runner.

## Follow-ups (not in this PR)

- `python-server` previews (uvicorn/flask apps — needs a serve-command
  contract), more desktop stacks (pyglet/arcade/tkinter — needs baked deps
  or a wheelhouse mount; no egress means deps must pre-exist).
- Cache `previewable`/`preview_type` probing (per-sandbox FS probe on every
  results GET — fine at MVP scale).
- ~~F10 id collision with sibling Phase-2 branches~~ — resolved: renumbered
  to F12 on rebase onto `main` (F10/F11 claimed by in-flight siblings).
