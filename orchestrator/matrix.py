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

Pure functions only — safe to import from both ``api.*`` and ``orchestrator.*``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


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
    its config but still counts as a participant (same-model-eval flag,
    run/results ``models`` fields, analytics rollups).
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
    """
    model_list = participant_models(default_model, models)
    configs = [dict(hc) for hc in harness_configs]
    if len(model_list) <= 1:
        return configs
    cells: list[dict[str, Any]] = []
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
    return deduped


def sandbox_model(harness_config: Mapping[str, Any] | None, default_model: str) -> str:
    """The participant model one sandbox runs: its config's ``model`` cell
    (PR14 multi-model matrix) else the run's primary ``model_name``."""
    override = _clean((harness_config or {}).get("model"))
    return override or _clean(default_model)
