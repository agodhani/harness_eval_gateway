"""PR22 — task preset library: store, endpoints, run resolution, analytics.

Hermetic: per-test file-backed sqlite (dependency-overridden ``get_db``), a
tmp vendored-preset dir swapped in via monkeypatched ``get_settings`` on
both routers, and stubbed orchestrator/modelcheck entry points so POST
/api/runs exercises preset resolution without touching gateway/docker.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api import presets as preset_store
from api.deps import get_db
from api.main import app
from api.routers import presets as presets_router
from api.routers import runs as runs_router
from db.models import Base, Run, Sandbox, Score, TaskPreset
from db.session import make_engine, make_session_factory
from orchestrator import runner

REPO_ROOT = Path(__file__).resolve().parents[3]
VENDORED_DIR = REPO_ROOT / "task_presets"

ALPHA_MD = """\
---
slug: alpha-preset
title: Alpha preset
description: the alpha task
category: testing
tags: [alpha, demo]
suggested_adapter: miniloop
suggested_models: [m1, m2]
---

Build the alpha thing.

Do it well.
"""

BETA_MD = """\
---
slug: beta-preset
title: Beta preset
description: the beta task
category: writing
tags: [beta]
---

Write the beta thing.
"""


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/presets.db")
    Base.metadata.create_all(engine)
    sf = make_session_factory(engine)
    yield sf
    engine.dispose()


@pytest.fixture()
def presets_dir(tmp_path: Path) -> Path:
    root = tmp_path / "task_presets"
    root.mkdir()
    (root / "alpha-preset.md").write_text(ALPHA_MD)
    (root / "beta-preset.md").write_text(BETA_MD)
    return root


@pytest.fixture()
def api(session_factory, presets_dir: Path, tmp_path: Path, monkeypatch):
    """TestClient with preset-aware settings on both routers."""
    ns = SimpleNamespace(
        DATABASE_URL="sqlite:///:memory:",
        SKILLPACKS_DIR=tmp_path / "skillpacks",
        IMPORTED_SKILLPACKS_DIR=tmp_path / "imported_skillpacks",
        TASK_PRESETS_DIR=presets_dir,
        ARTIFACT_ROOT=tmp_path / "artifacts",
    )
    monkeypatch.setattr(presets_router, "get_settings", lambda: ns)
    monkeypatch.setattr(runs_router, "get_settings", lambda: ns)

    async def _no_catalog(body, settings=None):
        return []

    monkeypatch.setattr(runs_router.modelcheck, "validate_run_models", _no_catalog)
    monkeypatch.setattr(runner, "start_rubric_task", lambda *a, **k: None)

    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    yield SimpleNamespace(
        client=TestClient(app),
        session_factory=session_factory,
        presets_dir=presets_dir,
    )
    app.dependency_overrides.pop(get_db, None)


def _run_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "task_prompt": "freeform task text",
        "model_name": "m1",
        "harness_configs": [{"adapter": "miniloop"}],
    }
    body.update(overrides)
    return body


def _mk_run(
    sf,
    *,
    preset_ref: str | None = None,
    created: datetime | None = None,
    status: str = "done",
) -> uuid.UUID:
    run = Run(
        task_prompt="p",
        model_name="m1",
        config={"harness_configs": [{"adapter": "miniloop", "name": "cfg-a"}]},
        preset_ref=preset_ref,
        status=status,
    )
    if created is not None:
        run.created_at = created
    with sf() as s:
        s.add(run)
        s.commit()
        return run.id


def _mk_sandbox(
    sf,
    run_id: uuid.UUID,
    *,
    harness_config: dict | None = None,
    name: str | None = None,
) -> uuid.UUID:
    sb = Sandbox(
        run_id=run_id,
        harness_config=harness_config
        if harness_config is not None
        else {"adapter": "miniloop", "name": "cfg-a", "model": "m1"},
        name=name,
        status="done",
    )
    with sf() as s:
        s.add(sb)
        s.commit()
        return sb.id


def _mk_score(
    sf,
    sandbox_id: uuid.UUID,
    score: float,
    *,
    dimension: str = "overall",
    seq: int | None = None,
) -> None:
    with sf() as s:
        s.add(
            Score(
                sandbox_id=sandbox_id,
                dimension=dimension,
                score=score,
                seq=seq,
            )
        )
        s.commit()


# ---------------------------------------------------------------------------
# store: vendored parsing / slug helpers / merged list
# ---------------------------------------------------------------------------


def test_parse_preset_md_fields_and_body() -> None:
    p = preset_store.parse_preset_md(ALPHA_MD, filename="alpha-preset.md")
    assert p.slug == "alpha-preset"
    assert p.title == "Alpha preset"
    assert p.description == "the alpha task"
    assert p.category == "testing"
    assert p.tags == ["alpha", "demo"]
    assert p.suggested_adapter == "miniloop"
    assert p.suggested_models == ["m1", "m2"]
    assert p.source == "vendored"
    assert p.revision == 1
    assert "Build the alpha thing." in p.task_prompt
    assert "---" not in p.task_prompt  # frontmatter is stripped


def test_parse_preset_md_rejects_bad_files(tmp_path: Path) -> None:
    # missing required keys
    with pytest.raises(preset_store.PresetError):
        preset_store.parse_preset_md("---\nslug: x\n---\nbody\n", filename="x.md")
    # slug/filename stem mismatch catches renamed-file bugs
    with pytest.raises(preset_store.PresetError):
        preset_store.parse_preset_md(
            "---\nslug: y\ntitle: t\ncategory: c\n---\nbody\n", filename="x.md"
        )
    # empty prompt body
    with pytest.raises(preset_store.PresetError):
        preset_store.parse_preset_md("---\nslug: x\ntitle: t\ncategory: c\n---\n", filename="x.md")


def test_list_vendored_sorted_and_skips_bad_file(tmp_path: Path) -> None:
    root = tmp_path / "task_presets"
    root.mkdir()
    (root / "zeta.md").write_text(BETA_MD.replace("beta-preset", "zeta"))
    (root / "alpha-preset.md").write_text(ALPHA_MD)
    (root / "broken.md").write_text("no frontmatter at all")
    (root / "notes.txt").write_text("not a preset — not .md")

    presets = preset_store.list_vendored(root)
    assert [p.slug for p in presets] == ["alpha-preset", "zeta"]
    assert all(p.source == "vendored" for p in presets)


def test_slug_helpers() -> None:
    for bad in ("Bad Slug", "with space", "-lead", "trail-", "a..b", "../x", ""):
        with pytest.raises(preset_store.PresetError):
            preset_store.validate_slug(bad)
    assert preset_store.validate_slug("  My-Slug ") == "my-slug"
    assert preset_store.slugify("Fix the Thing! (v2)") == "fix-the-thing-v2"
    taken = {"x", "x-2"}
    assert preset_store.suggest_slug(taken, "x") == "x-3"
    assert preset_store.suggest_slug({"a"}, "b") == "b"


def test_resolve_rejects_weird_slugs(tmp_path: Path, session_factory) -> None:
    """preset_ref/slug strings reach the store unvalidated — traversal-
    looking values must resolve to nothing, never read outside the root."""
    root = tmp_path / "task_presets"
    root.mkdir()
    (root / "ok.md").write_text("---\nslug: ok\ntitle: t\ncategory: c\n---\nbody\n")
    for bad in ("../escape", "..", "a/b", "x.md", "ok/../ok"):
        assert preset_store.get_vendored(root, bad) is None
        with session_factory() as s:
            assert preset_store.resolve(s, root, bad) is None


def test_merged_list_vendored_wins_on_clash(session_factory, tmp_path) -> None:
    """A user row that somehow shares a vendored slug (hand-edited DB) loses
    to the vendored file in the merged listing."""
    root = tmp_path / "task_presets"
    root.mkdir()
    (root / "alpha-preset.md").write_text(ALPHA_MD)
    with session_factory() as s:
        s.add(
            TaskPreset(
                slug="alpha-preset",
                title="impostor",
                task_prompt="fake",
            )
        )
        s.add(TaskPreset(slug="user-only", title="mine", task_prompt="real"))
        s.commit()
        merged = preset_store.list_all(s, root)
    by_slug = {p.slug: p for p in merged}
    assert by_slug["alpha-preset"].source == "vendored"
    assert by_slug["alpha-preset"].title == "Alpha preset"
    assert by_slug["user-only"].source == "user"


# ---------------------------------------------------------------------------
# vendored contract: the committed starter library parses + covers the
# required categories/slugs (behavioral — parsed via the real loader)
# ---------------------------------------------------------------------------


def test_vendored_library_parses_and_covers_required_presets() -> None:
    presets = preset_store.list_vendored(VENDORED_DIR)
    slugs = {p.slug for p in presets}
    required = {
        "game-2048",
        "wordle-html",
        "snake-pygame",
        "bugfix-repo",
        "refactor-module",
        "docs-writer",
        "csv-data-analysis",
        "extraction-classification",
        "sql-report",
        "support-reply",
    }
    assert required <= slugs
    for p in presets:
        assert p.slug == preset_store.validate_slug(p.slug)
        assert p.title and p.category and p.description
        # The markdown body IS the task prompt — must be usable as one.
        assert len(p.task_prompt) >= 100
        assert p.tags


def test_wordle_and_snake_presets_define_their_validation_targets() -> None:
    """The live-validation presets must name their runnable deliverables —
    wordle-html → a self-contained index.html; snake-pygame → pygame."""
    wordle = preset_store.get_vendored(VENDORED_DIR, "wordle-html")
    snake = preset_store.get_vendored(VENDORED_DIR, "snake-pygame")
    assert wordle is not None and snake is not None
    assert "index.html" in wordle.task_prompt
    assert "pygame" in snake.task_prompt.lower()


# ---------------------------------------------------------------------------
# GET /api/presets
# ---------------------------------------------------------------------------


def test_list_presets_merges_sources(api) -> None:
    rows = api.client.get("/api/presets").json()
    by_slug = {r["slug"]: r for r in rows}
    assert by_slug["alpha-preset"]["source"] == "vendored"
    assert by_slug["alpha-preset"]["category"] == "testing"
    assert by_slug["alpha-preset"]["tags"] == ["alpha", "demo"]
    assert by_slug["alpha-preset"]["runs_count"] == 0

    resp = api.client.post(
        "/api/presets/my-own",
        json={"title": "Mine", "task_prompt": "do my thing"},
    )
    assert resp.status_code == 201, resp.text
    rows = api.client.get("/api/presets").json()
    by_slug = {r["slug"]: r for r in rows}
    assert by_slug["my-own"]["source"] == "user"
    assert by_slug["my-own"]["revision"] == 1
    # vendored listed first (library order), then user
    assert rows[0]["source"] == "vendored"
    assert rows[-1]["slug"] == "my-own"


def test_get_preset_detail_and_404(api) -> None:
    resp = api.client.get("/api/presets/alpha-preset")
    assert resp.status_code == 200
    body = resp.json()
    assert body["slug"] == "alpha-preset"
    assert "Build the alpha thing." in body["task_prompt"]
    assert api.client.get("/api/presets/nope").status_code == 404


# ---------------------------------------------------------------------------
# POST /api/presets/{slug} — save-as-preset
# ---------------------------------------------------------------------------


def test_create_user_preset(api) -> None:
    resp = api.client.post(
        "/api/presets/support-v2",
        json={
            "title": "Support replies v2",
            "description": "draft replies",
            "task_prompt": "Reply to the tickets.",
            "category": "support",
            "tags": ["support", "writing"],
            "suggested_adapter": "miniloop",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["slug"] == "support-v2"
    assert body["source"] == "user"
    assert body["revision"] == 1
    assert body["suggested_adapter"] == "miniloop"
    assert body["created_at"] is not None

    detail = api.client.get("/api/presets/support-v2").json()
    assert detail["task_prompt"] == "Reply to the tickets."
    assert detail["tags"] == ["support", "writing"]


def test_create_preset_slug_validation_and_conflicts(api) -> None:
    # bad path slug → 422
    resp = api.client.post("/api/presets/Bad%20Slug", json={"title": "t", "task_prompt": "p"})
    assert resp.status_code == 422

    # vendored slug → 409 with the rename-flow payload
    resp = api.client.post("/api/presets/alpha-preset", json={"title": "t", "task_prompt": "p"})
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["kind"] == "name_conflict"
    assert detail["source"] == "vendored"
    assert detail["suggested_name"].startswith("alpha-preset")

    # user slug → same rename flow, source "user"
    api.client.post("/api/presets/mine", json={"title": "t", "task_prompt": "p"})
    resp = api.client.post("/api/presets/mine", json={"title": "t2", "task_prompt": "p2"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["source"] == "user"
    assert resp.json()["detail"]["suggested_name"] == "mine-2"

    # schema validation: missing title/prompt 422s
    resp = api.client.post("/api/presets/new", json={"task_prompt": "p"})
    assert resp.status_code == 422


def test_patch_preset_bumps_revision(api) -> None:
    api.client.post("/api/presets/editable", json={"title": "v1", "task_prompt": "p1"})
    resp = api.client.patch("/api/presets/editable", json={"title": "v2", "category": "docs"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "v2"
    assert body["category"] == "docs"
    assert body["task_prompt"] == "p1"  # untouched field preserved
    assert body["revision"] == 2

    # a no-op patch (all-None body) doesn't bump
    resp = api.client.patch("/api/presets/editable", json={})
    assert resp.json()["revision"] == 2


def test_patch_and_delete_protect_vendored(api) -> None:
    resp = api.client.patch("/api/presets/alpha-preset", json={"title": "hax"})
    assert resp.status_code == 409
    resp = api.client.delete("/api/presets/alpha-preset")
    assert resp.status_code == 409
    # and they're still there, unharmed
    assert api.client.get("/api/presets/alpha-preset").status_code == 200

    resp = api.client.patch("/api/presets/ghost", json={"title": "x"})
    assert resp.status_code == 404
    assert api.client.delete("/api/presets/ghost").status_code == 404


def test_delete_user_preset(api) -> None:
    api.client.post("/api/presets/doomed", json={"title": "t", "task_prompt": "p"})
    resp = api.client.delete("/api/presets/doomed")
    assert resp.status_code == 204
    assert api.client.get("/api/presets/doomed").status_code == 404
    # a run's preset_ref dangling after delete is fine — prompt is verbatim


# ---------------------------------------------------------------------------
# run submission with preset_ref
# ---------------------------------------------------------------------------


def test_submit_run_with_preset_ref_snapshots_prompt(api) -> None:
    """preset_ref resolves → the preset body becomes the stored task_prompt
    (verbatim), with slug + revision provenance columns."""
    resp = api.client.post("/api/runs", json=_run_body(preset_ref="alpha-preset"))
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["preset_ref"] == "alpha-preset"
    assert data["preset_revision"] == 1
    # the preset body wins over the submitted text — provenance is honest
    assert "Build the alpha thing." in data["task_prompt"]
    assert data["task_prompt"] != "freeform task text"


def test_submit_run_unknown_preset_ref_422s(api) -> None:
    resp = api.client.post("/api/runs", json=_run_body(preset_ref="ghost"))
    assert resp.status_code == 422
    assert "preset" in resp.text.lower()


def test_submit_run_no_preset_stays_freeform(api) -> None:
    data = api.client.post("/api/runs", json=_run_body()).json()
    assert data["preset_ref"] is None
    assert data["preset_revision"] is None
    assert data["task_prompt"] == "freeform task text"


def test_user_preset_revision_snapshot_on_submit(api) -> None:
    api.client.post("/api/presets/evolving", json={"title": "t", "task_prompt": "v1 prompt"})
    first = api.client.post("/api/runs", json=_run_body(preset_ref="evolving")).json()
    assert first["preset_revision"] == 1
    assert first["task_prompt"] == "v1 prompt"

    api.client.patch("/api/presets/evolving", json={"task_prompt": "v2 prompt"})
    second = api.client.post("/api/runs", json=_run_body(preset_ref="evolving")).json()
    assert second["preset_revision"] == 2
    assert second["task_prompt"] == "v2 prompt"


def test_runs_list_preset_column_and_filter(api) -> None:
    api.client.post("/api/runs", json=_run_body(preset_ref="alpha-preset"))
    api.client.post("/api/runs", json=_run_body())
    api.client.post("/api/runs", json=_run_body(preset_ref="beta-preset"))

    rows = api.client.get("/api/runs").json()
    assert {r["preset_ref"] for r in rows} == {
        "alpha-preset",
        "beta-preset",
        None,
    }
    filtered = api.client.get("/api/runs", params={"preset": "alpha-preset"}).json()
    assert len(filtered) == 1
    assert filtered[0]["preset_ref"] == "alpha-preset"


# ---------------------------------------------------------------------------
# GET /api/presets/{slug}/analytics
# ---------------------------------------------------------------------------


def test_preset_analytics_runs_leaderboards_trend(api, session_factory) -> None:
    now = datetime.now(UTC)
    # newest first: r2 submitted after r1
    r1 = _mk_run(session_factory, preset_ref="alpha-preset", created=now - timedelta(days=2))
    r2 = _mk_run(session_factory, preset_ref="alpha-preset", created=now - timedelta(days=1))
    r3 = _mk_run(session_factory, preset_ref="beta-preset", created=now)
    _mk_run(session_factory, preset_ref=None)  # unpreset'd run — excluded

    # r1: two scored cells (same model, same config name) + one unscored
    s1a = _mk_sandbox(session_factory, r1)
    s1b = _mk_sandbox(session_factory, r1)
    _mk_sandbox(
        session_factory,
        r1,
        harness_config={"adapter": "miniloop", "name": "cfg-b", "model": "m2"},
    )
    _mk_score(session_factory, s1a, 8.0, seq=1)
    _mk_score(session_factory, s1b, 6.0)
    # r2: one cell on a different model + named config
    s2 = _mk_sandbox(
        session_factory,
        r2,
        harness_config={"adapter": "miniloop", "name": "cfg-b", "model": "m2"},
    )
    _mk_score(session_factory, s2, 9.0)
    # r3 (beta) — scored, must not leak into alpha's numbers
    s3 = _mk_sandbox(session_factory, r3)
    _mk_score(session_factory, s3, 1.0)
    # a rescore: s1a's older score superseded by a newer insert — seq
    # stamps insertion order (sqlite created_at is second-precision, and
    # the uuid PK doesn't order inserts), which is what "newest wins" keys on
    _mk_score(session_factory, s1a, 10.0, seq=2)

    resp = api.client.get("/api/presets/alpha-preset/analytics")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["preset"]["slug"] == "alpha-preset"
    assert data["preset"]["runs_count"] == 2
    # last scored run = r2 → mean 9.0
    assert data["preset"]["last_run_mean"] == 9.0

    # every preset run listed, newest first; means over latest overalls
    assert [r["run_id"] for r in data["runs"]] == [str(r2), str(r1)]
    r1_row = data["runs"][1]
    assert r1_row["mean_overall_score"] == 8.0  # (10 + 6) / 2 — rescore wins
    assert r1_row["scored_sandboxes"] == 2
    assert r1_row["sandbox_count"] == 3

    # per-model board: m1 pooled (10,6) → 8.0; m2 → 9.0
    models = {e["key"]: e for e in data["by_model"]}
    assert set(models) == {"m1", "m2"}
    assert models["m1"]["mean_overall_score"] == 8.0
    assert models["m1"]["n"] == 2 and models["m1"]["runs"] == 1
    assert models["m2"]["mean_overall_score"] == 9.0

    # per-config board: cfg-a (2 cells) + cfg-b (1 cell), model excluded
    configs = {e["key"]: e for e in data["by_config"]}
    assert set(configs) == {"name:cfg-a", "name:cfg-b"}
    assert configs["name:cfg-a"]["n"] == 2
    assert configs["name:cfg-b"]["runs"] == 1
    assert data["by_config"][0]["adapter"] == "miniloop"

    # pooled mean over every scored alpha cell: (10 + 6 + 9) / 3
    assert data["mean_overall_score"] == 8.33
    assert data["n_scored"] == 3

    # trend: scored runs oldest-first (r1 then r2); r3/beta excluded
    assert [t["run_id"] for t in data["trend"]] == [str(r1), str(r2)]
    assert data["trend"][0]["mean_overall_score"] == 8.0


def test_preset_analytics_empty_and_unscored(api, session_factory) -> None:
    """No runs → empty-but-200 aggregates; unscored runs stay out of the
    boards/trend but appear in the run list."""
    data = api.client.get("/api/presets/alpha-preset/analytics").json()
    assert data["runs"] == []
    assert data["by_model"] == [] and data["by_config"] == []
    assert data["mean_overall_score"] is None
    assert data["n_scored"] == 0
    assert data["trend"] == []

    r = _mk_run(session_factory, preset_ref="alpha-preset", status="running")
    _mk_sandbox(session_factory, r)
    data = api.client.get("/api/presets/alpha-preset/analytics").json()
    assert len(data["runs"]) == 1
    assert data["runs"][0]["mean_overall_score"] is None
    assert data["by_model"] == []
    assert data["trend"] == []

    assert api.client.get("/api/presets/ghost/analytics").status_code == 404


# ---------------------------------------------------------------------------
# PR23 — suggested_rubric + rubric_template/rubric_hint submit wiring
# ---------------------------------------------------------------------------

GAMMA_MD = """\
---
slug: gamma-preset
title: Gamma preset
description: the gamma task
category: extraction
tags: [gamma]
suggested_rubric: extraction-accuracy
---

Extract the gamma things.
"""


def _run_config(api, run_id: str) -> dict:
    detail = api.client.get(f"/api/runs/{run_id}").json()
    return detail["config"]


def test_suggested_rubric_frontmatter_parsing() -> None:
    """Vendored ``suggested_rubric: <name>`` → ``{"template": name}``;
    a ``hint:`` prefix maps to the domain-hint form."""
    preset = preset_store.parse_preset_md(GAMMA_MD, filename="gamma-preset.md")
    assert preset.suggested_rubric == {"template": "extraction-accuracy"}

    hinted = GAMMA_MD.replace(
        "suggested_rubric: extraction-accuracy", "suggested_rubric: hint: support tickets"
    )
    preset = preset_store.parse_preset_md(hinted, filename="gamma-preset.md")
    assert preset.suggested_rubric == {"hint": "support tickets"}


def test_vendored_suggested_rubrics_resolve() -> None:
    """Every vendored preset that declares ``suggested_rubric`` names a
    template that actually resolves in the merged library — a committed
    typo would silently never apply, so the contract test pins it."""
    from api import rubric_templates as template_store

    preset_templates = {}
    for p in preset_store.list_vendored(VENDORED_DIR):
        if not p.suggested_rubric:
            continue
        name = p.suggested_rubric.get("template")
        assert name, f"{p.slug}: suggested_rubric without template"
        preset_templates[p.slug] = name
        tpl = template_store.get_vendored(REPO_ROOT / "rubric_templates", name)
        assert tpl is not None, (
            f"{p.slug}: suggested_rubric template {name!r} not vendored"
        )
    # the non-code presets picked their domain templates
    assert preset_templates.get("extraction-classification") == "extraction-accuracy"
    assert preset_templates.get("support-reply") == "support-tone-compliance"


def test_create_and_patch_suggested_rubric(api) -> None:
    resp = api.client.post(
        "/api/presets/custom-rubric",
        json={
            "title": "Custom",
            "task_prompt": "do the custom thing",
            "suggested_rubric": {"template": "code-default"},
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["suggested_rubric"] == {"template": "code-default"}

    # invalid shapes 422 at the boundary
    for bad in ("nope", {"bogus": 1}, {}, {"template": ""}):
        resp = api.client.post(
            "/api/presets/bad-shape",
            json={
                "title": "Bad",
                "task_prompt": "x",
                "suggested_rubric": bad,
            },
        )
        assert resp.status_code == 422, bad

    resp = api.client.patch(
        "/api/presets/custom-rubric",
        json={"suggested_rubric": {"hint": "tickets"}},
    )
    assert resp.status_code == 200
    assert resp.json()["suggested_rubric"] == {"hint": "tickets"}

    # explicit null clears it
    resp = api.client.patch(
        "/api/presets/custom-rubric", json={"suggested_rubric": None}
    )
    assert resp.status_code == 200
    assert resp.json()["suggested_rubric"] is None


def test_submit_preset_suggested_rubric_fills_template(api, presets_dir: Path) -> None:
    """A preset's ``suggested_rubric`` supplies the run's rubric_template
    ONLY when the submit didn't name one — explicit always wins."""
    (presets_dir / "gamma-preset.md").write_text(GAMMA_MD)

    resp = api.client.post(
        "/api/runs",
        json=_run_body(task_prompt="ignored", preset_ref="gamma-preset"),
    )
    assert resp.status_code == 201, resp.text
    config = _run_config(api, resp.json()["id"])
    assert config["rubric_template"] == "extraction-accuracy"
    # category flows as the domain hint too
    assert config["rubric_hint"] == "extraction"

    # explicit template overrides the suggestion
    resp = api.client.post(
        "/api/runs",
        json=_run_body(
            task_prompt="ignored",
            preset_ref="gamma-preset",
            rubric_template="code-default",
        ),
    )
    assert resp.status_code == 201, resp.text
    config = _run_config(api, resp.json()["id"])
    assert config["rubric_template"] == "code-default"


def test_submit_preset_category_becomes_hint(api) -> None:
    """No suggested_rubric, no explicit hint → the preset's category is the
    domain hint (the PR23 hint fallback chain)."""
    resp = api.client.post(
        "/api/runs",
        json=_run_body(task_prompt="ignored", preset_ref="beta-preset"),
    )
    assert resp.status_code == 201, resp.text
    config = _run_config(api, resp.json()["id"])
    assert config["rubric_hint"] == "writing"
    assert "rubric_template" not in config

    # an explicit hint always wins over the category
    resp = api.client.post(
        "/api/runs",
        json=_run_body(
            task_prompt="ignored",
            preset_ref="beta-preset",
            rubric_hint="poetry",
        ),
    )
    assert resp.status_code == 201, resp.text
    assert _run_config(api, resp.json()["id"])["rubric_hint"] == "poetry"


def test_submit_rubric_template_validation(api) -> None:
    """Explicit ``rubric_template`` must resolve — a typo 422s, not a
    silent freeform fallback."""
    resp = api.client.post(
        "/api/runs",
        json=_run_body(rubric_template="no-such-template"),
    )
    assert resp.status_code == 422
    assert "rubric_template" in resp.json()["detail"]

    resp = api.client.post(
        "/api/runs",
        json=_run_body(
            rubric_template="code-default", rubric_hint="code task"
        ),
    )
    assert resp.status_code == 201, resp.text
    config = _run_config(api, resp.json()["id"])
    assert config["rubric_template"] == "code-default"
    assert config["rubric_hint"] == "code task"


def test_submit_inline_suggested_rubric_becomes_basis(api, session_factory) -> None:
    """A user preset's inline ``{"dimensions": [...]}`` suggestion persists
    as ``config.rubric_basis`` — the draft path uses it verbatim."""
    with session_factory() as s:
        s.add(
            TaskPreset(
                slug="inline-rubric",
                title="Inline",
                task_prompt="do inline things",
                category="custom",
                suggested_rubric={
                    "dimensions": [
                        {"name": "accuracy", "min_score": 0.0, "max_score": 10.0},
                    ]
                },
            )
        )
        s.commit()
    resp = api.client.post(
        "/api/runs",
        json=_run_body(task_prompt="ignored", preset_ref="inline-rubric"),
    )
    assert resp.status_code == 201, resp.text
    config = _run_config(api, resp.json()["id"])
    assert config["rubric_basis"]["dimensions"][0]["name"] == "accuracy"
    assert "rubric_template" not in config
    assert config["rubric_hint"] == "custom"
