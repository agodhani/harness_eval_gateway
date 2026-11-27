"""Run-matrix expansion helpers (PR14 — model-vs-model comparisons).

A run's comparison matrix is ``harness_configs × participant models``. The
run's model list is ``model_name`` (the primary/default model, always
included) plus any ``models`` extras listed in ``run.config`` — deduped,
order-preserving, blanks dropped. A harness config may pin ``model`` to a
single value: that config then expands to exactly one cell at its pin,
overriding the run's model list for that config (the pin counts as a
participant even when it isn't in the run's model list).

Single-model runs expand to exactly the submitted ``harness_configs``
(pre-PR14 shape); multi-model runs get one sandbox per (model, config)
cell with ``model`` injected into the cell's harness-config dict so the
sandbox row self-describes which model it ran.

PR27 adds a third axis — ``trials``: every effective config × model cell is
replicated N times, each replica stamped ``trial_index`` (1-based; the key
is absent entirely when ``trials == 1``, matching the ``model``-injection
pattern so single-shot runs stay bit-identical to pre-PR27). Trials of one
cell stay adjacent (cell-major ordering) so a run's sandbox list groups
each cell's repeats together.

Pure functions only — safe to import from both ``api.*`` and ``orchestrator.*``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

#: JSON key carrying the 1-based repeat index on an expanded cell's
#: harness-config dict (absent on single-trial runs).
TRIAL_INDEX_KEY = "trial_index"


def _clean(model: object) -> str:
    return str(model).strip() if model is not None else ""


def participant_models(model_name: str, models: Iterable[object] | None) -> list[str]:
    """Effective participant model set for a run spec.

    ``model_name`` always leads (it's ``runs.model_name`` — the rubric
    default, price fallback, and evaluator default); ``models`` entries are
    appended deduped — entries equal to ``model_name`` or repeats collapse.
    """
    out = [_clean(model_name)] if _clean(model_name) else []
    for raw in models or []:
        m = _clean(raw)
        if m and m not in out:
            out.append(m)
    return out


def run_models(config: Mapping[str, Any] | None, model_name: str) -> list[str]:
    """Effective participant-model set of a stored run config.

    The run's model list (``config.models``) plus any per-config ``model``
    pins in ``config.harness_configs`` — a pin overrides the run list for
    its config but still appears in run/results ``models`` fields and
    analytics rollups.
    """
    cfg = config or {}
    pinned = [
        hc.get("model")
        for hc in cfg.get("harness_configs") or []
        if isinstance(hc, Mapping)
    ]
    return participant_models(model_name, [*(cfg.get("models") or []), *pinned])


def expanded_configs(
    harness_configs: Iterable[Mapping[str, Any]],
    models: Iterable[object] | None,
    default_model: str,
    *,
    trials: int = 1,
) -> list[dict[str, Any]]:
    """Expand ``harness_configs`` into per-sandbox config dicts.

    Single-model runs return the configs verbatim (no ``model`` key injected
    — identical to pre-PR14 rows). Multi-model runs inject ``model`` into
    every cell, ordered model-major so same-model sandboxes stay adjacent in
    the sandbox list / timeline / results.

    A config carrying a ``model`` pin expands to exactly one cell at its
    pin — overriding the run list for that config — emitted at the pin's
    position when it's a listed model, appended in config order otherwise.
    Identical expanded cells collapse (e.g. a pin onto a model the same
    config would reach via expansion anyway).

    ``trials`` (PR27) then replicates every cell N times — a 1-based
    ``trial_index`` stamps each replica, cell-major so a cell's repeats
    stay adjacent. ``trials <= 1`` emits no key at all (unchanged
    pre-PR27 shape).
    """
    model_list = participant_models(default_model, models)
    configs = [dict(hc) for hc in harness_configs]
    if len(model_list) <= 1:
        cells = configs
    else:
        cells = []
        listed = set(model_list)
        for model in model_list:
            for hc in configs:
                pin = _clean(hc.get("model"))
                if pin:
                    if pin == model:
                        cells.append(hc)
                else:
                    cells.append({**hc, "model": model})
        cells.extend(
            hc
            for hc in configs
            if (pin := _clean(hc.get("model"))) and pin not in listed
        )
        deduped: list[dict[str, Any]] = []
        for cell in cells:
            if cell not in deduped:
                deduped.append(cell)
        cells = deduped
    trials = max(1, int(trials or 1))
    if trials <= 1:
        return cells
    out: list[dict[str, Any]] = []
    for cell in cells:
        for trial in range(1, trials + 1):
            out.append({**cell, TRIAL_INDEX_KEY: trial})
    return out


def judge_models(config: Mapping[str, Any] | None, model_name: str) -> list[str]:
    """Effective judge panel of a stored run config (PR29).

    ``config.evaluator_models`` (ordered 1–3 panel, ``evaluator_model``
    already folded in as panel head) when the run spec carried one;
    otherwise the single ``config.evaluator_model`` falling back to the
    run's primary ``model_name`` — the same default chain the evaluator
    resolves. Always returns ≥1 entry for a well-formed run; panel order
    is stable (panel[0] is the primary judge).
    """
    cfg = config or {}
    panel = cfg.get("evaluator_models")
    if isinstance(panel, list):
        out: list[str] = []
        for m in panel:
            s = _clean(m)
            if s and s not in out:
                out.append(s)
        if out:
            return out
    single = _clean(cfg.get("evaluator_model")) or _clean(model_name)
    return [single] if single else []


def same_model_eval(config: Mapping[str, Any] | None, model_name: str) -> bool:
    """Whether any configured judge also runs in a participant cell."""
    cfg = config or {}
    configs = [
        hc
        for hc in cfg.get("harness_configs") or []
        if isinstance(hc, Mapping)
    ]
    cells = expanded_configs(configs or [{}], cfg.get("models"), model_name)
    actual_participants = {sandbox_model(cell, model_name) for cell in cells}
    return bool(actual_participants.intersection(judge_models(cfg, model_name)))


def sandbox_model(harness_config: Mapping[str, Any] | None, default_model: str) -> str:
    """The participant model one sandbox runs: its config's ``model`` cell
    (PR14 multi-model matrix) else the run's primary ``model_name``."""
    override = _clean((harness_config or {}).get("model"))
    return override or _clean(default_model)


def trial_index(harness_config: Mapping[str, Any] | None) -> int | None:
    """The 1-based repeat index stamped on a trialed cell (PR27).

    ``None`` on untrialed sandboxes (trials=1 runs and rows predating
    PR27) — callers treat it as "trial 1 of 1" for display.
    """
    raw = (harness_config or {}).get(TRIAL_INDEX_KEY)
    if raw is None:
        return None
    try:
        idx = int(raw)
    except (TypeError, ValueError):
        return None
    return idx if idx > 0 else None


def cell_key(harness_config: Mapping[str, Any] | None) -> str:
    """Stable identity of a sandbox's config × model cell (PR27).

    The harness-config dict minus ``trial_index``, serialized with sorted
    keys — every trial of one cell produces the identical key, which is
    what results/analytics group on to report mean ± sd across repeats.
    """
    cfg = {
        k: v
        for k, v in (harness_config or {}).items()
        if k != TRIAL_INDEX_KEY
    }
    return json.dumps(cfg, sort_keys=True, default=str)
