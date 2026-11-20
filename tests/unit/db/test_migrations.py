"""Alembic migration tests — run upgrade/downgrade on scratch sqlite.

env.py resolves the DB URL via ``api.config.get_settings().DATABASE_URL``;
tests point that at a tmp sqlite file via monkeypatched env var + cache clear.
No Postgres/docker required.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

import api.config

REPO_ROOT = Path(__file__).resolve().parents[3]
INITIAL_REV = "f0a1b2c3d4e5"
SANDBOX_NAME_REV = "a2b3c4d5e6f7"
TASK_PRESETS_REV = "d1e2f3a4b5c6"  # PR22 — repoint if a parallel PR lands first


@pytest.fixture()
def sqlite_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite:///{tmp_path}/migrated.db"
    monkeypatch.setenv("DATABASE_URL", url)
    api.config.get_settings.cache_clear()
    yield url
    api.config.get_settings.cache_clear()


def _cfg() -> Config:
    """Fileless Config — the ini's only used knobs are script_location and
    the logging setup, and env.py's ``fileConfig`` call (keyed on
    ``config_file_name``) would run with disable_existing_loggers=True and
    silence every logger for the REST of the pytest process."""
    cfg = Config()
    cfg.set_main_option("script_location", str(REPO_ROOT / "db" / "migrations"))
    return cfg


def _columns(url: str, table: str) -> set[str]:
    engine = create_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


def test_migration_chain_single_headed() -> None:
    heads = ScriptDirectory.from_config(_cfg()).get_heads()
    assert list(heads) == [TASK_PRESETS_REV]


def test_sandbox_name_upgrade_downgrade(sqlite_url: str) -> None:
    command.upgrade(_cfg(), "head")
    assert "name" in _columns(sqlite_url, "sandboxes")

    # back to the initial schema: the column goes away
    command.downgrade(_cfg(), INITIAL_REV)
    assert "name" not in _columns(sqlite_url, "sandboxes")
    assert _columns(sqlite_url, "sandboxes")  # table itself survives

    # and re-apply cleanly
    command.upgrade(_cfg(), "head")
    assert "name" in _columns(sqlite_url, "sandboxes")


def test_sandbox_name_matches_model(sqlite_url: str) -> None:
    """Migrated schema agrees with db.models metadata (nullable str(255))."""
    command.upgrade(_cfg(), "head")
    engine = create_engine(sqlite_url)
    try:
        col = next(
            c for c in inspect(engine).get_columns("sandboxes") if c["name"] == "name"
        )
        assert col["nullable"] is True
        assert str(col["type"]) == "VARCHAR(255)"
    finally:
        engine.dispose()


def test_task_presets_upgrade_downgrade(sqlite_url: str) -> None:
    """PR22: task_presets table + runs.preset_ref/preset_revision round-trip."""
    command.upgrade(_cfg(), "head")
    preset_cols = _columns(sqlite_url, "task_presets")
    assert {
        "id",
        "slug",
        "title",
        "description",
        "task_prompt",
        "category",
        "tags",
        "source",
        "revision",
        "suggested_adapter",
        "suggested_models",
        "created_at",
        "updated_at",
    } <= preset_cols
    run_cols = _columns(sqlite_url, "runs")
    assert {"preset_ref", "preset_revision"} <= run_cols

    command.downgrade(_cfg(), SANDBOX_NAME_REV)
    inspector_tables = set()
    engine = create_engine(sqlite_url)
    try:
        inspector_tables = set(inspect(engine).get_table_names())
        run_cols = {c["name"] for c in inspect(engine).get_columns("runs")}
    finally:
        engine.dispose()
    assert "task_presets" not in inspector_tables
    assert "preset_ref" not in run_cols
    assert "preset_revision" not in run_cols

    command.upgrade(_cfg(), "head")
    assert "slug" in _columns(sqlite_url, "task_presets")


def test_task_presets_slug_unique(sqlite_url: str) -> None:
    """The migrated table enforces the shared slug namespace (unique slug)."""
    command.upgrade(_cfg(), "head")
    engine = create_engine(sqlite_url)
    try:
        uniques = [
            uc
            for uc in inspect(engine).get_unique_constraints("task_presets")
            if "slug" in uc.get("column_names", [])
        ]
        # sqlite reports unique indexes separately — check both surfaces.
        indexes = [
            ix
            for ix in inspect(engine).get_indexes("task_presets")
            if ix.get("unique") and ix.get("column_names") == ["slug"]
        ]
        assert uniques or indexes
    finally:
        engine.dispose()
