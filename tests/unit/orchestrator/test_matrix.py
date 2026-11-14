"""PR14 — run-matrix expansion helpers (orchestrator/matrix.py).

The comparison axis is harness_configs × participant models; these pin the
pure spec math: participant set resolution, per-cell expansion, and the
per-sandbox model lookup used by provision/results/analytics.
"""

from __future__ import annotations

from orchestrator.matrix import (
    expanded_configs,
    participant_models,
    sandbox_model,
)

# ---------------------------------------------------------------------------
# participant_models
# ---------------------------------------------------------------------------


def test_participant_models_default_is_primary_only() -> None:
    assert participant_models("qwen3:8b", None) == ["qwen3:8b"]
    assert participant_models("qwen3:8b", []) == ["qwen3:8b"]


def test_participant_models_appends_deduped_extras() -> None:
    assert participant_models("a", ["b", "c"]) == ["a", "b", "c"]
    # primary + repeats + blanks collapse, order preserved
    assert participant_models("a", ["a", "b", "b", " ", "c"]) == ["a", "b", "c"]
    # non-str entries are stringified defensively
    assert participant_models("a", [1, None, "b"]) == ["a", "1", "b"]


def test_participant_models_strips_whitespace() -> None:
    assert participant_models(" a ", [" b "]) == ["a", "b"]


# ---------------------------------------------------------------------------
# expanded_configs
# ---------------------------------------------------------------------------


def test_expanded_configs_single_model_returns_raw() -> None:
    hcs = [{"adapter": "miniloop", "skill_packs": ["x"]}]
    out = expanded_configs(hcs, None, "qwen3:8b")
    assert out == hcs
    assert "model" not in out[0]  # no key injected for single-model runs


def test_expanded_configs_single_model_explicit_still_raw() -> None:
    """models=[primary] collapses to one model → unexpanded shape."""
    hcs = [{"adapter": "miniloop"}, {"adapter": "codex"}]
    out = expanded_configs(hcs, ["qwen3:8b"], "qwen3:8b")
    assert len(out) == 2
    assert all("model" not in hc for hc in out)


def test_expanded_configs_multi_model_model_major() -> None:
    hcs = [{"adapter": "miniloop"}, {"adapter": "codex"}]
    out = expanded_configs(hcs, ["m2"], "m1")
    assert len(out) == 4  # 2 configs × 2 models
    # model-major: all m1 cells then all m2 cells
    assert [hc["model"] for hc in out] == ["m1", "m1", "m2", "m2"]
    assert [hc["adapter"] for hc in out] == [
        "miniloop", "codex", "miniloop", "codex",
    ]
    # other keys preserved
    assert out[0]["adapter"] == "miniloop"


def test_expanded_configs_dedupes_models() -> None:
    hcs = [{"adapter": "miniloop"}]
    out = expanded_configs(hcs, ["m1", "m2", "m2"], "m1")
    assert [hc["model"] for hc in out] == ["m1", "m2"]


def test_expanded_configs_empty_configs() -> None:
    assert expanded_configs([], ["m2"], "m1") == []


# ---------------------------------------------------------------------------
# sandbox_model
# ---------------------------------------------------------------------------


def test_sandbox_model_cell_override_wins() -> None:
    assert sandbox_model({"model": "gpt-4o"}, "qwen3:8b") == "gpt-4o"


def test_sandbox_model_falls_back_to_run_default() -> None:
    assert sandbox_model({"adapter": "miniloop"}, "qwen3:8b") == "qwen3:8b"
    assert sandbox_model({}, "qwen3:8b") == "qwen3:8b"
    assert sandbox_model(None, "qwen3:8b") == "qwen3:8b"
    assert sandbox_model({"model": "  "}, "qwen3:8b") == "qwen3:8b"
