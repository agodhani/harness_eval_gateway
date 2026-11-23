"""PR14 — run-matrix expansion helpers (orchestrator/matrix.py).

The comparison axis is harness_configs × participant models; these pin the
pure spec math: participant set resolution, per-cell expansion, and the
per-sandbox model lookup used by provision/results/analytics.
"""

from __future__ import annotations

from orchestrator.matrix import (
    cell_key,
    expanded_configs,
    participant_models,
    run_models,
    sandbox_model,
    trial_index,
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
# run_models — stored-config participant set (run list ∪ per-config pins)
# ---------------------------------------------------------------------------


def test_run_models_list_only() -> None:
    cfg = {"models": ["m1", "m2"], "harness_configs": [{"adapter": "a"}]}
    assert run_models(cfg, "m1") == ["m1", "m2"]


def test_run_models_includes_pins_outside_the_list() -> None:
    cfg = {
        "models": ["m1", "m2"],
        "harness_configs": [{"adapter": "a", "model": "m9"}, {"adapter": "a"}],
    }
    assert run_models(cfg, "m1") == ["m1", "m2", "m9"]


def test_run_models_tolerates_missing_and_blank() -> None:
    assert run_models(None, "m1") == ["m1"]
    assert run_models({"harness_configs": [{"model": "m1"}]}, "m1") == ["m1"]
    cfg = {"harness_configs": [{"model": "  "}, "not-a-dict", {"model": " m2 "}]}
    assert run_models(cfg, "m1") == ["m1", "m2"]


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


# per-config model pins


def test_expanded_configs_pin_overrides_run_list() -> None:
    """A pinned config emits exactly one cell at its pin — it does not
    fan out across the run's model list like unpinned configs."""
    hcs = [{"adapter": "a", "model": "m9"}, {"adapter": "b"}]
    out = expanded_configs(hcs, ["m2"], "m1")
    # unpinned config × [m1, m2] first (model-major); the pin on an
    # unlisted model appends in config order after the matrix cells.
    assert out == [
        {"adapter": "b", "model": "m1"},
        {"adapter": "b", "model": "m2"},
        {"adapter": "a", "model": "m9"},
    ]


def test_expanded_configs_pin_on_listed_model_stays_in_group() -> None:
    """A pin naming a listed model emits inside that model's group."""
    hcs = [{"adapter": "a", "model": "m2"}, {"adapter": "b"}]
    out = expanded_configs(hcs, ["m2"], "m1")
    assert out == [
        {"adapter": "b", "model": "m1"},
        {"adapter": "a", "model": "m2"},
        {"adapter": "b", "model": "m2"},
    ]


def test_expanded_configs_dedupes_identical_cells() -> None:
    """A pin onto a model the same config reaches via expansion anyway
    collapses to one cell — no duplicate sandboxes."""
    hcs = [{"adapter": "a", "model": "m1"}, {"adapter": "a"}]
    out = expanded_configs(hcs, ["m2"], "m1")
    assert out == [{"adapter": "a", "model": "m1"}, {"adapter": "a", "model": "m2"}]


def test_expanded_configs_single_model_pin_verbatim() -> None:
    """Single-model runs return configs verbatim — a submitted pin rides
    through as the cell's ``model`` (sandbox_model resolves it)."""
    hcs = [{"adapter": "a", "model": "m9"}]
    out = expanded_configs(hcs, None, "m1")
    assert out == [{"adapter": "a", "model": "m9"}]
    assert sandbox_model(out[0], "m1") == "m9"


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


# ---------------------------------------------------------------------------
# PR27 — trials: every effective cell × N replicas, trial_index stamped
# ---------------------------------------------------------------------------


def test_expanded_configs_trials_multiplies_single_model() -> None:
    """2 configs × 1 model × 3 trials = 6 sandboxes."""
    hcs = [{"adapter": "miniloop"}, {"adapter": "codex"}]
    out = expanded_configs(hcs, None, "m1", trials=3)
    assert len(out) == 6
    assert [hc["adapter"] for hc in out] == [
        "miniloop", "miniloop", "miniloop", "codex", "codex", "codex",
    ]


def test_expanded_configs_trials_multiplies_multi_model() -> None:
    """2 configs × 2 models × 2 trials = 8 sandboxes."""
    hcs = [{"adapter": "miniloop"}, {"adapter": "codex"}]
    out = expanded_configs(hcs, ["m2"], "m1", trials=2)
    assert len(out) == 8
    # model-major cells, then cell-major trials inside each cell
    assert [hc["model"] for hc in out] == ["m1", "m1", "m1", "m1", "m2", "m2", "m2", "m2"]


def test_expanded_configs_trial_index_one_based_cell_major() -> None:
    """Each cell's replicas get 1-based trial_index, staying adjacent."""
    hcs = [{"adapter": "a", "name": "x"}, {"adapter": "b", "name": "y"}]
    out = expanded_configs(hcs, None, "m1", trials=3)
    assert [(hc["name"], hc["trial_index"]) for hc in out] == [
        ("x", 1), ("x", 2), ("x", 3), ("y", 1), ("y", 2), ("y", 3),
    ]


def test_expanded_configs_trials_one_emits_no_key() -> None:
    """trials=1 keeps the pre-PR27 shape — no trial_index key at all."""
    out = expanded_configs([{"adapter": "a"}], None, "m1", trials=1)
    assert out == [{"adapter": "a"}]
    assert "trial_index" not in out[0]


def test_expanded_configs_trials_with_model_pin() -> None:
    """A pinned config's single cell replicates like any other."""
    hcs = [{"adapter": "a", "model": "m9"}, {"adapter": "b"}]
    out = expanded_configs(hcs, ["m2"], "m1", trials=2)
    assert out == [
        {"adapter": "b", "model": "m1", "trial_index": 1},
        {"adapter": "b", "model": "m1", "trial_index": 2},
        {"adapter": "b", "model": "m2", "trial_index": 1},
        {"adapter": "b", "model": "m2", "trial_index": 2},
        {"adapter": "a", "model": "m9", "trial_index": 1},
        {"adapter": "a", "model": "m9", "trial_index": 2},
    ]


# ---------------------------------------------------------------------------
# trial_index / cell_key
# ---------------------------------------------------------------------------


def test_trial_index_reads_stamped_value() -> None:
    assert trial_index({"adapter": "a", "trial_index": 3}) == 3
    assert trial_index({"adapter": "a"}) is None
    assert trial_index(None) is None
    # defensive: non-int / non-positive values read as untrialed
    assert trial_index({"trial_index": "x"}) is None
    assert trial_index({"trial_index": 0}) is None
    assert trial_index({"trial_index": "2"}) == 2


def test_cell_key_groups_trials_of_one_cell() -> None:
    """Every trial of one config × model cell shares the identical key."""
    base = {"adapter": "a", "model": "m1", "skill_packs": ["x"]}
    t1 = cell_key({**base, "trial_index": 1})
    t3 = cell_key({**base, "trial_index": 3})
    assert t1 == t3
    # untrialed shape groups with the same cell
    assert cell_key(base) == t1
    # different cells differ
    assert cell_key({**base, "model": "m2"}) != t1
    assert cell_key({**base, "adapter": "b"}) != t1
