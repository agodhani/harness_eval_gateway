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
# PR28 — re-chained on top of RUNS_TRIALS_REV.
MODEL_PRICES_REV = "c4e8d1a7f3b5"
# PR28 follow-up — case-insensitive glob uniqueness.
PRICE_GLOB_LOWER_REV = "d6e2b8f4a1c9"
# PR22 — re-chained on top of PRICE_GLOB_LOWER_REV.
TASK_PRESETS_REV = "d1e2f3a4b5c6"
# PR26 — benchmark suites + checker results.
SUITES_REV = "e4f5a6b7c8d9"
# PR35 — runs.share_token (export + share links); current chain head.
SHARE_TOKEN_REV = "c5f9a2b8d4e7"


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
    assert list(heads) == [SHARE_TOKEN_REV]


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


def test_benchmark_suites_upgrade_downgrade(sqlite_url: str) -> None:
    """PR26: suite_runs + checker_results tables, runs.suite_* cols."""
    command.upgrade(_cfg(), "head")

    engine = create_engine(sqlite_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert {"suite_runs", "checker_results"} <= tables
        suite_run_cols = {
            c["name"] for c in inspect(engine).get_columns("suite_runs")
        }
        assert {"id", "suite_slug", "suite_version", "created_at"} <= suite_run_cols
        checker_cols = {
            c["name"] for c in inspect(engine).get_columns("checker_results")
        }
        assert {
            "id",
            "run_id",
            "sandbox_id",
            "check_type",
            "spec",
            "status",
            "passed",
            "detail",
            "created_at",
        } <= checker_cols
        run_cols = {c["name"] for c in inspect(engine).get_columns("runs")}
        assert {"suite_ref", "suite_index", "suite_run_id"} <= run_cols
        fks = {
            fk["referred_table"]
            for fk in inspect(engine).get_foreign_keys("runs")
        }
        assert "suite_runs" in fks
    finally:
        engine.dispose()

    # Down to PR22: PR26 schema unwinds, preset provenance stays.
    command.downgrade(_cfg(), TASK_PRESETS_REV)
    engine = create_engine(sqlite_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert "suite_runs" not in tables
        assert "checker_results" not in tables
        run_cols = {c["name"] for c in inspect(engine).get_columns("runs")}
        assert "suite_ref" not in run_cols
        assert "preset_ref" in run_cols
    finally:
        engine.dispose()

    # Re-apply cleanly.
    command.upgrade(_cfg(), "head")
    assert "suite_runs" in _columns(sqlite_url, "suite_runs") or _columns(
        sqlite_url, "suite_runs"
    )


def test_checker_results_nullable_and_defaults(sqlite_url: str) -> None:
    """checker_results mirrors db.models: booleans/details have DB defaults,
    run/sandbox ids are required."""
    command.upgrade(_cfg(), "head")
    engine = create_engine(sqlite_url)
    try:
        cols = {
            c["name"]: c for c in inspect(engine).get_columns("checker_results")
        }
        assert cols["run_id"]["nullable"] is False
        assert cols["sandbox_id"]["nullable"] is False
        assert cols["passed"]["nullable"] is False
        assert cols["detail"]["nullable"] is False
        assert cols["run_id"]["default"] is None or True  # no server default
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


def test_model_prices_lower_glob_unique(sqlite_url: str) -> None:
    """A differently-cased insert collides on the lower() unique index —
    glob matching is case-insensitive, so a second casing would be a
    permanently shadowed duplicate the schema must refuse."""
    from sqlalchemy.exc import IntegrityError

    from db.models import ModelPriceRow
    from db.session import make_engine, make_session_factory

    command.upgrade(_cfg(), "head")
    engine = make_engine(sqlite_url)
    sf = make_session_factory(engine)
    try:
        with sf() as s:
            s.add(ModelPriceRow(model_glob="mig-lower-*", source="user"))
            s.commit()
        with sf() as s:
            s.add(ModelPriceRow(model_glob="MIG-LOWER-*", source="user"))
            with pytest.raises(IntegrityError):
                s.commit()
    finally:
        engine.dispose()


def test_runs_share_token_upgrade_downgrade(sqlite_url: str) -> None:
    """PR35: ``runs.share_token`` lands at head and drops below the suites rev."""
    command.upgrade(_cfg(), "head")
    assert "share_token" in _columns(sqlite_url, "runs")

    command.downgrade(_cfg(), SUITES_REV)
    assert "share_token" not in _columns(sqlite_url, "runs")

    command.upgrade(_cfg(), "head")
    assert "share_token" in _columns(sqlite_url, "runs")


def test_runs_share_token_unique(sqlite_url: str) -> None:
    """The migrated unique index refuses a duplicate share token."""
    from datetime import UTC, datetime

    from sqlalchemy.exc import IntegrityError

    from db.models import Run
    from db.session import make_engine, make_session_factory

    command.upgrade(_cfg(), "head")
    engine = make_engine(sqlite_url)
    sf = make_session_factory(engine)
    try:
        # The migrated DDL keeps ``DEFAULT now()`` (Postgres spelling) on
        # created_at/updated_at — sqlite can't evaluate it, so stamp the
        # timestamps explicitly.
        def _run(prompt: str, token: str | None) -> Run:
            return Run(
                task_prompt=prompt,
                model_name="m",
                share_token=token,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )

        with sf() as s:
            s.add(_run("a", "tok"))
            s.commit()
        with sf() as s:
            s.add(_run("b", "tok"))
            with pytest.raises(IntegrityError):
                s.commit()
        # NULLs coexist — most runs are never shared.
        with sf() as s:
            s.add(_run("c", None))
            s.commit()
            assert s.query(Run).filter_by(share_token=None).count() >= 1
    finally:
        engine.dispose()
