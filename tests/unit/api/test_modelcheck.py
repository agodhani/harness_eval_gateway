"""Submit-time model-catalog validation (api/modelcheck.py).

Hermetic: ``_fetch_catalog`` is monkeypatched — no gateway/ollama traffic.
Provider resolution (``_provider_for_model``) mirrors the gateway's
PROVIDER_ROUTES rules without touching gateway code.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api import modelcheck
from api.schemas import RunCreate


def _settings(**overrides) -> SimpleNamespace:
    ns = SimpleNamespace(
        GATEWAY_URL="http://gateway.test:8080",
        GATEWAY_CRED_SECRET="secret",
        UPSTREAM_PROVIDER="ollama",
        PROVIDER_ROUTES="",
        OLLAMA_BASE_URL="http://ollama.test:11434",
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def _body(**overrides) -> RunCreate:
    kwargs = {
        "task_prompt": "build a game",
        "model_name": "qwen3:8b",
        "harness_configs": [{"adapter": "miniloop"}],
        "evaluator_model": "eval-model",
    }
    kwargs.update(overrides)
    return RunCreate(**kwargs)


@pytest.fixture(autouse=True)
def _clear_cache():
    modelcheck.reset_catalog_cache()
    yield
    modelcheck.reset_catalog_cache()


def _patch_fetch(monkeypatch, catalog_or_exc):
    """Stub the per-provider fetch: dict -> catalog, exception -> raise."""
    calls: list[str] = []

    async def fake(settings, provider):
        calls.append(provider)
        if isinstance(catalog_or_exc, Exception):
            raise catalog_or_exc
        return catalog_or_exc

    monkeypatch.setattr(modelcheck, "_fetch_catalog", fake)
    return calls


# ---------------------------------------------------------------------------
# provider resolution
# ---------------------------------------------------------------------------


def test_provider_for_model_default_and_glob() -> None:
    s = _settings(PROVIDER_ROUTES='{"qwen*": "ollama", "anthropic/*": "openrouter"}')
    assert modelcheck._provider_for_model(s, "qwen3:8b") == "ollama"
    assert modelcheck._provider_for_model(s, "anthropic/claude:free") == "openrouter"
    assert modelcheck._provider_for_model(s, "unmatched") == "ollama"


def test_provider_for_model_local_glob_cannot_swallow_namespaced() -> None:
    """A glob without ``/`` must not hijack ``vendor/model`` ids (gateway's
    namespace rule mirrored api-side)."""
    s = _settings(PROVIDER_ROUTES='{"qwen*": "openrouter"}')
    assert modelcheck._provider_for_model(s, "qwen/qwen3-8b:free") == "ollama"
    assert modelcheck._provider_for_model(s, "qwen3:8b") == "openrouter"


def test_provider_for_model_invalid_routes_falls_back() -> None:
    s = _settings(PROVIDER_ROUTES="not-json{")
    assert modelcheck._provider_for_model(s, "qwen3:8b") == "ollama"


# ---------------------------------------------------------------------------
# validate_run_models — catalog presence (warnings)
# ---------------------------------------------------------------------------


async def test_absent_models_warn_not_422(monkeypatch) -> None:
    _patch_fetch(monkeypatch, {"qwen3:8b": frozenset({"tools"})})
    body = _body(models=["mystery:1"], evaluator_model="ghost:2")
    warnings = await modelcheck.validate_run_models(body, settings=_settings())
    assert any("evaluator_model 'ghost:2'" in w for w in warnings)
    assert any("models[] 'mystery:1'" in w for w in warnings)
    assert not any("qwen3:8b" in w for w in warnings)


async def test_per_config_pin_checked_and_labeled(monkeypatch) -> None:
    _patch_fetch(monkeypatch, {"qwen3:8b": frozenset({"tools"}), "eval-model": None})
    body = _body(
        harness_configs=[{"adapter": "miniloop", "model": "pinned-x"}],
    )
    warnings = await modelcheck.validate_run_models(body, settings=_settings())
    assert any("harness_configs[0].model 'pinned-x'" in w for w in warnings)


async def test_all_present_no_warnings(monkeypatch) -> None:
    _patch_fetch(
        monkeypatch,
        {"qwen3:8b": frozenset({"tools"}), "eval-model": frozenset({"tools"})},
    )
    assert await modelcheck.validate_run_models(_body(), settings=_settings()) == []


# ---------------------------------------------------------------------------
# validate_run_models — tool-capability check (hard 422)
# ---------------------------------------------------------------------------


async def test_no_tools_model_under_tool_adapter_422s(monkeypatch) -> None:
    """The observed failure class: qwen2.5vl-style catalogs report no
    ``tools`` — a tool-call harness would die mid-run, so submit 422s."""
    _patch_fetch(
        monkeypatch,
        {
            "qwen2.5vl:7b": frozenset({"completion", "vision"}),
            "qwen3:8b": frozenset({"tools"}),
        },
    )
    body = _body(model_name="qwen2.5vl:7b", evaluator_model="qwen3:8b")
    with pytest.raises(HTTPException) as excinfo:
        await modelcheck.validate_run_models(body, settings=_settings())
    assert excinfo.value.status_code == 422
    detail = excinfo.value.detail
    assert "does not support tools" in detail
    assert "model_name 'qwen2.5vl:7b'" in detail
    assert "vision" in detail  # provider's own capability report echoed


async def test_no_tools_per_config_pin_422s(monkeypatch) -> None:
    _patch_fetch(
        monkeypatch,
        {
            "qwen3:8b": frozenset({"tools"}),
            "eval-model": frozenset({"tools"}),
            "v-only": frozenset({"completion"}),
        },
    )
    body = _body(harness_configs=[{"adapter": "miniloop", "model": "v-only"}])
    with pytest.raises(HTTPException) as excinfo:
        await modelcheck.validate_run_models(body, settings=_settings())
    assert excinfo.value.status_code == 422
    assert "harness_configs[0].model 'v-only'" in excinfo.value.detail


async def test_no_tools_evaluator_model_422s(monkeypatch) -> None:
    """The eval judge is a miniloop sandbox — it sends tools= on every chat
    call, so the judge model needs the same provider capability."""
    _patch_fetch(
        monkeypatch,
        {
            "qwen3:8b": frozenset({"tools"}),
            "weak-judge": frozenset({"completion"}),
        },
    )
    body = _body(evaluator_model="weak-judge")
    with pytest.raises(HTTPException) as excinfo:
        await modelcheck.validate_run_models(body, settings=_settings())
    assert "evaluator_model 'weak-judge'" in excinfo.value.detail


async def test_tool_optional_adapter_pin_allows_no_tools(monkeypatch) -> None:
    """aider is TOOL_OPTIONAL (litellm text edit-formats): pinning a
    no-tools model on an aider config is not a hard failure."""
    _patch_fetch(
        monkeypatch,
        {
            "qwen3:8b": frozenset({"tools"}),
            "eval-model": frozenset({"tools"}),
            "v-only": frozenset({"completion", "vision"}),
        },
    )
    body = _body(harness_configs=[{"adapter": "aider", "model": "v-only"}])
    assert await modelcheck.validate_run_models(body, settings=_settings()) == []


async def test_no_capability_metadata_skips_tool_check(monkeypatch) -> None:
    """Provider reports ids only (caps=None) → absent-tool check can't
    fire; presence still counts (no warning for catalogued models)."""
    _patch_fetch(monkeypatch, {"qwen3:8b": None, "eval-model": None})
    assert await modelcheck.validate_run_models(_body(), settings=_settings()) == []


# ---------------------------------------------------------------------------
# fetch resilience + cache
# ---------------------------------------------------------------------------


async def test_fetch_failure_tolerated_no_warnings(monkeypatch) -> None:
    """Unreachable gateway/ollama → validation skipped entirely (offline
    tolerance): no 422, no warnings."""
    _patch_fetch(monkeypatch, RuntimeError("gateway down"))
    assert await modelcheck.validate_run_models(_body(), settings=_settings()) == []


async def test_catalog_cached_per_provider(monkeypatch) -> None:
    calls = _patch_fetch(monkeypatch, {"qwen3:8b": frozenset({"tools"})})
    for _ in range(3):
        await modelcheck.validate_run_models(_body(), settings=_settings())
    assert calls == ["ollama"]  # one fetch, then the ~60s TTL cache


async def test_multi_provider_routes_fetch_each_catalog(monkeypatch) -> None:
    """Models resolving to different providers get their own catalogs —
    same per-request, once-per-provider rule."""
    fetched: list[str] = []

    async def fake(settings, provider):
        fetched.append(provider)
        return {"qwen3:8b": frozenset({"tools"}), "eval-model": frozenset({"tools"})}

    monkeypatch.setattr(modelcheck, "_fetch_catalog", fake)
    settings = _settings(PROVIDER_ROUTES='{"vendor/*": "openrouter"}')
    body = _body(models=["vendor/m:free"])
    warnings = await modelcheck.validate_run_models(body, settings=settings)
    assert sorted(fetched) == ["ollama", "openrouter"]
    assert any("openrouter" in w for w in warnings)
