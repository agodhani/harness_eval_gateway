"""F10 — /api/skillpacks endpoints: list, discover, import, delete.

Hermetic: fake vendored packs dir, imported root under tmp_path (derived
from ARTIFACT_ROOT like production), FakeFetcher for all GitHub traffic, and
the runs-submit machinery stubbed exactly like test_runs.py so the
"imported packs are selectable for runs" check exercises the real
validation path.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.deps import get_db
from api.main import app
from api.routers import runs as runs_router
from api.routers import skillpacks as skillpacks_router
from db.models import Base, Run
from db.session import make_engine, make_session_factory
from orchestrator import lifecycle as lifecycle_mod
from orchestrator import rubric as rubric_mod
from orchestrator import runner, state_machine

from .conftest import SKILL_MD, FakeFetcher, make_tarball, skill_repo_files


@pytest.fixture()
def session_factory(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/packs.db")
    Base.metadata.create_all(engine)
    sf = make_session_factory(engine)
    yield sf
    engine.dispose()


@pytest.fixture()
def vendored_dir(tmp_path: Path) -> Path:
    root = tmp_path / "skillpacks"
    d = root / "pack-a" / "skills" / "demo"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(SKILL_MD.format(name="demo", desc="vendored demo"))
    (root / "pack-b").mkdir(parents=True)
    return root


@pytest.fixture()
def fake_fetcher() -> FakeFetcher:
    # ok_refs restricts which codeload refs return 200 — "main/skills" 404s so
    # probing falls back to "main" (subpath="skills"), like real resolution.
    return FakeFetcher(
        make_tarball(skill_repo_files()), ok_refs={"main", "deadbee", "HEAD"}
    )


@pytest.fixture()
def api(session_factory, vendored_dir: Path, tmp_path: Path, fake_fetcher, monkeypatch):
    """TestClient wired to tmp pack roots + FakeFetcher."""
    ns = SimpleNamespace(
        DATABASE_URL="sqlite:///:memory:",
        GATEWAY_URL="http://gateway.test:8080",
        GATEWAY_CRED_SECRET="unit-test-secret",
        RUBRIC_MODEL="",
        SANDBOX_IMAGE="heg-test:latest",
        MONITOR_POLL_SECONDS=0.01,
        EVAL_TIMEOUT_SECONDS=5.0,
        HOST_DATA_ROOT="",
        SKILLPACKS_DIR=vendored_dir,
        ARTIFACT_ROOT=tmp_path / "data" / "artifacts",
        IMPORTED_SKILLPACKS_DIR=None,
        GITHUB_TOKEN="",
    )
    (tmp_path / "data" / "skillpacks").mkdir(parents=True)

    async def fake_draft_rubric(run_id, **_kwargs):
        with session_factory() as s:
            run = s.get(Run, run_id)
            if run is None or run.status != "submitted":
                return
            state_machine.transition(s, run, "rubric_review")
            s.commit()

    async def fake_run_lifecycle(run_id, **_kwargs):
        return None

    monkeypatch.setattr(skillpacks_router, "get_settings", lambda: ns)
    monkeypatch.setattr(runs_router, "get_settings", lambda: ns)
    monkeypatch.setattr(runner, "_session_factory", lambda: session_factory)
    monkeypatch.setattr(runner, "_settings", lambda: ns)
    monkeypatch.setattr(rubric_mod, "draft_rubric", fake_draft_rubric)
    monkeypatch.setattr(lifecycle_mod, "run_lifecycle", fake_run_lifecycle)

    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[skillpacks_router.get_fetcher] = lambda: fake_fetcher
    yield SimpleNamespace(
        client=TestClient(app),
        session_factory=session_factory,
        settings=ns,
        fetcher=fake_fetcher,
        imported_root=tmp_path / "data" / "skillpacks",
        vendored_dir=vendored_dir,
    )
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(skillpacks_router.get_fetcher, None)


# ---------------------------------------------------------------------------
# GET /api/skillpacks
# ---------------------------------------------------------------------------


def test_list_skillpacks_vendored(api) -> None:
    resp = api.client.get("/api/skillpacks")
    assert resp.status_code == 200
    packs = {p["name"]: p for p in resp.json()}
    assert packs["pack-a"]["source"] == "vendored"
    assert packs["pack-a"]["skills"] == ["demo"]
    assert packs["pack-b"]["skill_count"] == 0


def test_list_includes_imported(api) -> None:
    _do_import(api.client)
    resp = api.client.get("/api/skillpacks")
    packs = {p["name"]: p for p in resp.json()}
    assert packs["gh-pack"]["source"] == "imported"
    assert packs["gh-pack"]["skill_count"] == 1
    assert packs["gh-pack"]["skills"] == ["linting"]
    assert packs["gh-pack"]["origin_url"] == "https://github.com/acme/repo/tree/main/skills"


# ---------------------------------------------------------------------------
# POST /api/skillpacks/discover
# ---------------------------------------------------------------------------


def test_discover_tree_link(api) -> None:
    resp = api.client.post(
        "/api/skillpacks/discover",
        json={"url": "https://github.com/acme/repo/tree/main/skills"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["repo"] == "acme/repo"
    assert data["ref"] == "main"
    assert data["subpath"] == "skills"
    assert data["commit_sha"] == "abc1234"
    by_dir = {s["dir"]: s for s in data["skills"]}
    assert set(by_dir) == {"skills/linting", "skills/formatting"}
    assert by_dir["skills/linting"]["name"] == "linting"
    assert by_dir["skills/linting"]["description"] == "Lint things"
    assert by_dir["skills/linting"]["file_count"] == 3
    assert by_dir["skills/linting"]["has_frontmatter"] is True


def test_discover_blob_link_single_skill(api) -> None:
    resp = api.client.post(
        "/api/skillpacks/discover",
        json={"url": "https://github.com/acme/repo/blob/main/skills/linting/SKILL.md"},
    )
    assert resp.status_code == 200, resp.text
    skills = resp.json()["skills"]
    assert len(skills) == 1
    assert skills[0]["dir"] == "skills/linting"
    assert skills[0]["file_count"] == 3  # sibling files counted


def test_discover_invalid_urls(api) -> None:
    for url in (
        "",
        "https://gitlab.com/a/b",
        "https://github.com/acme",
        "https://github.com/acme/repo/issues/3",
        "https://github.com/acme/repo/blob/main/README.md",
        "not a url",
    ):
        resp = api.client.post("/api/skillpacks/discover", json={"url": url})
        assert resp.status_code == 422, f"{url} -> {resp.status_code}"


def test_discover_repo_not_found(api, monkeypatch) -> None:
    app.dependency_overrides[skillpacks_router.get_fetcher] = lambda: FakeFetcher(
        ok_refs=set()
    )
    try:
        resp = api.client.post(
            "/api/skillpacks/discover", json={"url": "github.com/acme/missing"}
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides[skillpacks_router.get_fetcher] = lambda: api.fetcher


def test_discover_no_skills(api) -> None:
    body = make_tarball({"README.md": "no skills here"})
    app.dependency_overrides[skillpacks_router.get_fetcher] = lambda: FakeFetcher(body)
    try:
        resp = api.client.post(
            "/api/skillpacks/discover", json={"url": "github.com/acme/empty"}
        )
        assert resp.status_code == 404
        assert "no SKILL.md" in resp.json()["detail"]
    finally:
        app.dependency_overrides[skillpacks_router.get_fetcher] = lambda: api.fetcher


def test_discover_upstream_error(api) -> None:
    app.dependency_overrides[skillpacks_router.get_fetcher] = lambda: FakeFetcher(
        status=500
    )
    try:
        resp = api.client.post(
            "/api/skillpacks/discover", json={"url": "github.com/acme/repo"}
        )
        assert resp.status_code == 502
    finally:
        app.dependency_overrides[skillpacks_router.get_fetcher] = lambda: api.fetcher


# ---------------------------------------------------------------------------
# POST /api/skillpacks/import
# ---------------------------------------------------------------------------


def _do_import(client, **overrides) -> dict:
    body = {
        "url": "https://github.com/acme/repo/tree/main/skills",
        "skills": ["skills/linting"],
        "pack_name": "gh-pack",
    }
    body.update(overrides)
    resp = client.post("/api/skillpacks/import", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_import_as_new_pack(api) -> None:
    data = _do_import(api.client, skills=["skills/linting", "group/nested/deep"])
    assert data["pack"] == "gh-pack"
    assert data["source"] == "imported"
    assert data["skill_count"] == 2
    imported = {i["source_dir"]: i for i in data["imported"]}
    assert imported["skills/linting"]["dir"] == "linting"
    assert imported["skills/linting"]["file_count"] == 3
    assert imported["group/nested/deep"]["dir"] == "deep"

    pack_dir = api.imported_root / "gh-pack"
    assert (pack_dir / "skills" / "linting" / "SKILL.md").is_file()
    assert (pack_dir / "skills" / "linting" / "references" / "rules.md").is_file()
    assert (pack_dir / "PACK.json").is_file()


def test_import_appends_to_existing_pack(api) -> None:
    _do_import(api.client, skills=["skills/linting"])
    data = _do_import(api.client, skills=["skills/formatting"])
    assert data["skill_count"] == 2
    assert (api.imported_root / "gh-pack" / "skills" / "formatting").is_dir()


def test_import_same_skill_dedupes(api) -> None:
    _do_import(api.client, skills=["skills/linting"])
    data = _do_import(api.client, skills=["skills/linting"])
    assert data["imported"][0]["dir"] == "linting-2"
    assert data["imported"][0]["renamed"] is True
    assert data["skill_count"] == 2


def test_import_rejects_vendored_name(api) -> None:
    resp = api.client.post(
        "/api/skillpacks/import",
        json={
            "url": "github.com/acme/repo",
            "skills": ["skills/linting"],
            "pack_name": "pack-a",
        },
    )
    assert resp.status_code == 409
    assert "vendored" in resp.json()["detail"]


def test_import_rejects_bad_pack_name(api) -> None:
    for name in ("../evil", "bad name", "", "a/b"):
        resp = api.client.post(
            "/api/skillpacks/import",
            json={
                "url": "github.com/acme/repo",
                "skills": ["skills/linting"],
                "pack_name": name,
            },
        )
        assert resp.status_code == 422, f"{name!r} -> {resp.status_code}"


def test_import_rejects_unknown_skill_dir(api) -> None:
    resp = api.client.post(
        "/api/skillpacks/import",
        json={
            "url": "github.com/acme/repo",
            "skills": ["skills/linting", "no/such"],
            "pack_name": "x",
        },
    )
    assert resp.status_code == 422
    assert "no/such" in resp.json()["detail"]


def test_import_non_skill_dir_rejected(api) -> None:
    """A real repo dir without a SKILL.md is not importable."""
    resp = api.client.post(
        "/api/skillpacks/import",
        json={
            "url": "github.com/acme/repo",
            "skills": ["docs"],
            "pack_name": "x",
        },
    )
    assert resp.status_code == 422


def test_import_pinned_ref_skips_probing(api) -> None:
    _do_import(
        api.client,
        ref="deadbee",
        subpath="skills",
        skills=["skills/linting"],
    )
    # one pinned fetch, no probing chain
    assert api.fetcher.calls == [
        "https://codeload.github.com/acme/repo/tar.gz/deadbee"
    ]


# ---------------------------------------------------------------------------
# imported packs are selectable + staged for runs
# ---------------------------------------------------------------------------


def _post_run(client, skill_packs: list[str]) -> dict:
    resp = client.post(
        "/api/runs",
        json={
            "task_prompt": "create hello.txt",
            "model_name": "qwen3:8b",
            "harness_configs": [{"adapter": "miniloop", "skill_packs": skill_packs}],
        },
    )
    return resp


def test_imported_pack_passes_run_validation(api) -> None:
    _do_import(api.client)
    resp = _post_run(api.client, ["gh-pack", "pack-a"])
    assert resp.status_code == 201, resp.text
    cfg = resp.json()["config"]["harness_configs"][0]
    assert cfg["skill_packs"] == ["gh-pack", "pack-a"]
    # background rubric task actually completes against the fake
    run_id = resp.json()["id"]
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if api.client.get(f"/api/runs/{run_id}").json()["status"] == "rubric_review":
            break
        time.sleep(0.05)
    else:
        raise AssertionError("run never reached rubric_review")


def test_unknown_pack_still_rejected(api) -> None:
    resp = _post_run(api.client, ["not-a-pack"])
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# DELETE /api/skillpacks/{name}
# ---------------------------------------------------------------------------


def test_delete_imported_pack(api) -> None:
    _do_import(api.client)
    resp = api.client.delete("/api/skillpacks/gh-pack")
    assert resp.status_code == 204
    assert not (api.imported_root / "gh-pack").exists()
    packs = {p["name"] for p in api.client.get("/api/skillpacks").json()}
    assert "gh-pack" not in packs


def test_delete_vendored_pack_protected(api) -> None:
    resp = api.client.delete("/api/skillpacks/pack-a")
    assert resp.status_code == 409


def test_delete_unknown_pack_404(api) -> None:
    resp = api.client.delete("/api/skillpacks/nope")
    assert resp.status_code == 404


def test_delete_rejects_traversal(api) -> None:
    resp = api.client.delete("/api/skillpacks/..%2F..")
    assert resp.status_code in (404, 422)
