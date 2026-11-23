"""PR23 — rubric template library: vendored store, merged resolve, endpoints.

Hermetic: per-test file-backed sqlite (dependency-overridden ``get_db``) and
a monkeypatched ``get_settings`` on the router pointing at a tmp templates
dir; the vendored-seed contract test reads the committed ``rubric_templates/``
directory directly.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api import rubric_templates as template_store
from api.deps import get_db
from api.main import app
from api.routers import rubric_templates as templates_router
from db.models import Base, RubricTemplate
from db.session import make_engine, make_session_factory
from orchestrator.rubric import CANONICAL_DIMENSIONS, RubricContent

REPO_ROOT = Path(__file__).resolve().parents[3]
VENDORED_DIR = REPO_ROOT / "rubric_templates"

TEMPLATE_JSON = {
    "name": "tmp-template",
    "description": "a test template",
    "content": {
        "summary": "x",
        "dimensions": [
            {"name": "accuracy", "min_score": 0.0, "max_score": 10.0},
            {"name": "tone", "min_score": 0.0, "max_score": 10.0},
        ],
        "criteria": [],
    },
}


@pytest.fixture()
def session_factory(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/templates.db")
    Base.metadata.create_all(engine)
    sf = make_session_factory(engine)
    yield sf
    engine.dispose()


@pytest.fixture()
def templates_dir(tmp_path: Path) -> Path:
    root = tmp_path / "rubric_templates"
    root.mkdir()
    (root / "tmp-template.json").write_text(json.dumps(TEMPLATE_JSON))
    return root


@pytest.fixture()
def api(session_factory, templates_dir: Path, monkeypatch):
    ns = SimpleNamespace(RUBRIC_TEMPLATES_DIR=templates_dir)
    monkeypatch.setattr(templates_router, "get_settings", lambda: ns)

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
        templates_dir=templates_dir,
    )
    app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# vendored seeds contract — every committed file parses + validates
# ---------------------------------------------------------------------------


def test_vendored_seeds_contract() -> None:
    """All committed ``rubric_templates/*.json`` parse into valid templates:
    name == filename stem, content satisfies RubricContent, the required
    seeds exist."""
    templates = template_store.list_vendored(VENDORED_DIR)
    names = {t.name for t in templates}
    assert {
        "code-default",
        "extraction-accuracy",
        "summarisation-fidelity",
        "support-tone-compliance",
    } <= names
    by_name = {t.name: t for t in templates}
    # code-default is today's canonical four — the basis preserving the
    # pre-PR23 dimension set.
    assert {d["name"] for d in by_name["code-default"].content["dimensions"]} == set(
        CANONICAL_DIMENSIONS
    )
    for path in sorted(VENDORED_DIR.glob("*.json")):
        tpl = template_store.parse_template_json(
            path.read_text(), filename=path.name
        )
        assert tpl.name == path.stem
        assert tpl.source == "vendored"
        # content is already the normalized frozen shape
        RubricContent.model_validate(tpl.content)


def test_parse_template_json_rejects_bad_files(tmp_path: Path) -> None:
    root = tmp_path / "rubric_templates"
    root.mkdir()
    bad = {
        "bad.json": "not json",
        "name-mismatch.json": json.dumps({**TEMPLATE_JSON, "name": "other"}),
        "no-content.json": json.dumps({"name": "no-content", "description": "x"}),
        "bad-dims.json": json.dumps(
            {**TEMPLATE_JSON, "content": {"dimensions": []}}
        ),
        "bad name.json": json.dumps({**TEMPLATE_JSON, "name": "bad name"}),
    }
    # name-mismatch's stem is valid; give each file a stem matching the
    # required name where needed
    for fname, text in bad.items():
        (root / fname).write_text(text)
    for fname in bad:
        with pytest.raises(template_store.TemplateError):
            template_store.parse_template_json(
                (root / fname).read_text(), filename=fname
            )


# ---------------------------------------------------------------------------
# store — merged resolve + user rows
# ---------------------------------------------------------------------------


def test_resolve_vendored_then_user(session_factory, templates_dir: Path) -> None:
    with session_factory() as s:
        tpl = template_store.resolve(s, templates_dir, "tmp-template")
        assert tpl is not None and tpl.source == "vendored"
        s.add(
            RubricTemplate(
                name="user-template",
                description="u",
                content={"summary": "x", "dimensions": [{"name": "d1"}]},
            )
        )
        s.commit()
        tpl = template_store.resolve(s, templates_dir, "user-template")
        assert tpl is not None and tpl.source == "user"
        assert template_store.resolve(s, templates_dir, "missing") is None
        # traversal-shaped names never reach the filesystem
        assert template_store.resolve(s, templates_dir, "../runs") is None


def test_list_all_merges_namespace(session_factory, templates_dir: Path) -> None:
    with session_factory() as s:
        s.add(
            RubricTemplate(
                name="tmp-template",  # collides with the vendored file
                description="user copy",
                content={"summary": "x", "dimensions": [{"name": "d1"}]},
            )
        )
        s.add(
            RubricTemplate(
                name="user-only",
                description="u",
                content={"summary": "x", "dimensions": [{"name": "d1"}]},
            )
        )
        s.commit()
        listed = template_store.list_all(s, templates_dir)
        by_name = {t.name: t for t in listed}
        assert by_name["tmp-template"].source == "vendored"  # vendored wins
        assert by_name["user-only"].source == "user"
        assert len(listed) == 2


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------


def test_list_and_get_templates(api) -> None:
    client = api.client
    with api.session_factory() as s:
        s.add(
            RubricTemplate(
                name="user-template",
                description="user row",
                content={"summary": "x", "dimensions": [{"name": "d1"}]},
            )
        )
        s.commit()

    listed = client.get("/api/rubric-templates")
    assert listed.status_code == 200
    names = {t["name"] for t in listed.json()}
    assert names == {"tmp-template", "user-template"}
    by_name = {t["name"]: t for t in listed.json()}
    assert by_name["tmp-template"]["source"] == "vendored"
    assert by_name["user-template"]["source"] == "user"

    one = client.get("/api/rubric-templates/tmp-template")
    assert one.status_code == 200
    body = one.json()
    assert body["name"] == "tmp-template"
    assert [d["name"] for d in body["content"]["dimensions"]] == [
        "accuracy",
        "tone",
    ]

    assert client.get("/api/rubric-templates/nope").status_code == 404
    assert client.get("/api/rubric-templates/..%2Fruns").status_code in (
        404,
        422,
    )
