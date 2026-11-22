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
# PR27: adds ``runs.trials`` (int, default 1).
RUNS_TRIALS_REV = "d7e9f1a3b5c7"
# PR28 — current chain head (re-chained on top of RUNS_TRIALS_REV).
MODEL_PRICES_REV = "c4e8d1a7f3b5"


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
    assert list(heads) == [MODEL_PRICES_REV]


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


def test_model_prices_upgrade_downgrade(sqlite_url: str) -> None:
    """PR28 — model_prices table lands at head and drops cleanly."""
    command.upgrade(_cfg(), "head")
    cols = _columns(sqlite_url, "model_prices")
    assert {
        "id",
        "model_glob",
        "input_per_mtok_usd",
        "output_per_mtok_usd",
        "cache_read_per_mtok_usd",
        "local",
        "source",
        "created_at",
        "updated_at",
    } <= cols

    # model_glob is the upsert key — must be unique
    engine = create_engine(sqlite_url)
    try:
        uniques = {
            tuple(u["column_names"])
            for u in inspect(engine).get_unique_constraints("model_prices")
        }
        assert ("model_glob",) in uniques
        # cache_read nullable — providers without a cache rate store NULL
        col = next(
            c
            for c in inspect(engine).get_columns("model_prices")
            if c["name"] == "cache_read_per_mtok_usd"
        )
        assert col["nullable"] is True
    finally:
        engine.dispose()

    command.downgrade(_cfg(), SANDBOX_NAME_REV)
    engine = create_engine(sqlite_url)
    try:
        assert "model_prices" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_model_prices_roundtrip_on_sqlite(sqlite_url: str) -> None:
    """A migrated DB accepts + reads back a price row (ORM matches schema)."""
    from db.models import ModelPriceRow
    from db.session import make_engine, make_session_factory

    command.upgrade(_cfg(), "head")
    engine = make_engine(sqlite_url)
    sf = make_session_factory(engine)
    try:
        with sf() as s:
            s.add(
                ModelPriceRow(
                    model_glob="mig-test-*",
                    input_per_mtok_usd=1.25,
                    output_per_mtok_usd=2.5,
                    cache_read_per_mtok_usd=None,
                    local=False,
                    source="user",
                )
            )
            s.commit()
        with sf() as s:
            row = s.query(ModelPriceRow).filter_by(model_glob="mig-test-*").one()
            assert row.input_per_mtok_usd == 1.25
            assert row.local is False
            assert row.created_at is not None
            assert row.updated_at is not None
    finally:
        engine.dispose()


def test_runs_trials_upgrade_downgrade(sqlite_url: str) -> None:
    """PR27: ``runs.trials`` lands at head and drops below ``d7e9f1a3b5c7``."""
    command.upgrade(_cfg(), "head")
    assert "trials" in _columns(sqlite_url, "runs")

    command.downgrade(_cfg(), SANDBOX_NAME_REV)
    assert "trials" not in _columns(sqlite_url, "runs")

    command.upgrade(_cfg(), "head")
    assert "trials" in _columns(sqlite_url, "runs")


def test_runs_trials_matches_model(sqlite_url: str) -> None:
    """PR27: migrated column agrees with db.models — int, non-null, default 1."""
    command.upgrade(_cfg(), "head")
    engine = create_engine(sqlite_url)
    try:
        col = next(
            c for c in inspect(engine).get_columns("runs") if c["name"] == "trials"
        )
        assert col["nullable"] is False
        assert "INT" in str(col["type"]).upper()
    finally:
        engine.dispose()
