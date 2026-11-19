"""PR22 — task preset library (``/api/presets*``).

Endpoints:

- ``GET /api/presets`` — merged preset list: vendored ``task_presets/*.md``
  files plus user-saved ``task_presets`` rows, each carrying ``source``
  (``vendored`` | ``user``) and the browse aggregates ``runs_count`` +
  ``last_run_mean`` (the newest *scored* run's mean ``overall``).
- ``GET /api/presets/{slug}`` — one preset, same shape.
- ``GET /api/presets/{slug}/analytics`` — everything the preset detail page
  needs in one shot: every run whose ``preset_ref`` matches, per-model and
  per-config score leaderboards (mean ``overall`` pooled over each bucket's
  scored sandbox cells + n), the pooled mean, and the score-over-time trend.
- ``POST /api/presets/{slug}`` — save a task prompt as a USER preset at the
  path's slug ("save prompt as preset" from the submit form / run detail).
  Slug namespace is shared with vendored presets — a taken slug 409s with a
  structured ``name_conflict`` carrying ``suggested_name`` for the
  confirm-and-rename flow. ``revision`` starts at 1.
- ``PATCH /api/presets/{slug}`` — partial edit of a user preset; bumps
  ``revision`` so runs snapshot which preset version they ran on.
- ``DELETE /api/presets/{slug}`` — remove a user preset. Vendored presets
  are protected (409) everywhere; runs keep their ``preset_ref`` — the
  stored task_prompt stays the verbatim contract.

Score semantics mirror the cross-run trends endpoint: a sandbox's score is
its LATEST ``overall`` row (asc order, newest wins) and a run's mean is the
mean over its sandboxes' latest overalls. Unscored runs/cells drop out of
leaderboards and the trend but stay in the run list.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api import presets as preset_store
from api.config import get_settings
from api.deps import get_db
from api.schemas import (
    PresetAnalytics,
    PresetCreate,
    PresetLeaderboardEntry,
    PresetOut,
    PresetRunPoint,
    PresetTrendPoint,
    PresetUpdate,
)
from db.models import Run, Sandbox, Score, TaskPreset
from evaluator.scores import OVERALL_DIMENSION
from orchestrator.matrix import sandbox_model

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/presets", tags=["presets"])

DbSession = Annotated[Session, Depends(get_db)]

_STORE_STATUS = {
    "invalid_slug": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "invalid_body": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "name_conflict": status.HTTP_409_CONFLICT,
    "vendored_protected": status.HTTP_409_CONFLICT,
    "not_found": status.HTTP_404_NOT_FOUND,
}


def _preset_err(exc: preset_store.PresetError) -> HTTPException:
    detail: str | dict[str, object] = exc.detail
    if exc.kind == "name_conflict":
        # Structured so the UI can run its confirm-and-rename flow
        # (same contract as the skillpack-import conflict).
        detail = {
            "kind": exc.kind,
            "message": exc.detail,
            "name": exc.name,
            "source": exc.source,
            "suggested_name": exc.suggested_name,
        }
    return HTTPException(
        status_code=_STORE_STATUS.get(exc.kind, status.HTTP_500_INTERNAL_SERVER_ERROR),
        detail=detail,
    )


# ---------------------------------------------------------------------------
# response assembly + score aggregation
# ---------------------------------------------------------------------------


def _preset_out(
    preset: preset_store.Preset,
    stats: dict[str, tuple[int, float | None]],
) -> PresetOut:
    runs_count, last_mean = stats.get(preset.slug, (0, None))
    return PresetOut(
        slug=preset.slug,
        title=preset.title,
        description=preset.description,
        task_prompt=preset.task_prompt,
        category=preset.category,
        tags=preset.tags,
        source=preset.source,
        revision=preset.revision,
        suggested_adapter=preset.suggested_adapter,
        suggested_models=preset.suggested_models,
        suggested_rubric=preset.suggested_rubric,
        suite=preset.suite,
        created_at=preset.created_at,
        updated_at=preset.updated_at,
        runs_count=runs_count,
        last_run_mean=last_mean,
    )


def _latest_overalls(
    session: Session, run_ids: list[UUID]
) -> tuple[dict[UUID, float], dict[UUID, Sandbox]]:
    """Latest ``overall`` score per sandbox + the run's sandbox rows.

    Same "rows ordered asc → newest wins" rule as the results/trends
    endpoints (rubric versions + rescore insert newer rows).
    """
    sandboxes = (
        session.execute(select(Sandbox).where(Sandbox.run_id.in_(run_ids))).scalars().all()
        if run_ids
        else []
    )
    by_id = {sb.id: sb for sb in sandboxes}
    scores = (
        session.execute(
            select(Score)
            .where(Score.sandbox_id.in_(list(by_id)))
            .order_by(
                Score.created_at.asc(),
                Score.seq.asc().nulls_first(),
                Score.id,
            )
        )
        .scalars()
        .all()
        if by_id
        else []
    )
    latest: dict[UUID, float] = {}
    for sc in scores:
        if sc.dimension == OVERALL_DIMENSION:
            latest[sc.sandbox_id] = sc.score
    return latest, by_id


def _run_means(
    latest: dict[UUID, float], sandboxes: dict[UUID, Sandbox]
) -> dict[UUID, list[float]]:
    """run_id -> its sandboxes' latest overalls (scored cells only)."""
    out: dict[UUID, list[float]] = {}
    for sb_id, score in latest.items():
        out.setdefault(sandboxes[sb_id].run_id, []).append(score)
    return out


def _list_stats(session: Session, slugs: set[str]) -> dict[str, tuple[int, float | None]]:
    """slug -> (runs_count, last scored run's mean overall) for the browse
    list — one pass over every preset-tagged run."""
    if not slugs:
        return {}
    runs = (
        session.execute(
            select(Run).where(Run.preset_ref.in_(slugs)).order_by(Run.created_at.desc())
        )
        .scalars()
        .all()
    )
    latest, sandboxes = _latest_overalls(session, [r.id for r in runs])
    means = _run_means(latest, sandboxes)
    out: dict[str, tuple[int, float | None]] = {}
    by_slug: dict[str, list[Run]] = {}
    for run in runs:
        by_slug.setdefault(run.preset_ref or "", []).append(run)
    for slug, slug_runs in by_slug.items():
        last_mean: float | None = None
        for run in slug_runs:  # newest first
            overalls = means.get(run.id)
            if overalls:
                last_mean = round(sum(overalls) / len(overalls), 2)
                break
        out[slug] = (len(slug_runs), last_mean)
    return out


def _config_key(harness_config: dict) -> tuple[str, str]:
    """(group key, display label) for the per-config leaderboard.

    A PR1 named config groups by its name — that's the stable comparison
    axis across runs. Unnamed configs group by adapter + pack set +
    prompt-extra (``model`` deliberately excluded — the model board owns
    that axis), displayed ``adapter · packs`` like the results matrix.
    """
    hc = harness_config or {}
    adapter = str(hc.get("adapter") or hc.get("harness") or "?")
    packs = [str(p) for p in hc.get("skill_packs") or []]
    name = str(hc.get("name") or "").strip()
    if name:
        return f"name:{name}", name
    extra = str(hc.get("system_prompt_extra") or "")
    key = f"cfg:{adapter}|{','.join(sorted(packs))}|{extra}"
    label = " · ".join([adapter, *packs]) if packs else adapter
    return key, label


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------


@router.get("")
async def list_presets(session: DbSession) -> list[PresetOut]:
    """Merged preset library — vendored files first, then user presets."""
    settings = get_settings()
    presets = preset_store.list_all(session, settings.TASK_PRESETS_DIR)
    stats = _list_stats(session, {p.slug for p in presets})
    return [_preset_out(p, stats) for p in presets]


@router.get("/{slug}")
async def get_preset(slug: str, session: DbSession) -> PresetOut:
    settings = get_settings()
    preset = preset_store.resolve(session, settings.TASK_PRESETS_DIR, slug)
    if preset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"preset {slug!r} not found",
        )
    stats = _list_stats(session, {slug})
    return _preset_out(preset, stats)


@router.get("/{slug}/analytics")
async def preset_analytics(slug: str, session: DbSession) -> PresetAnalytics:
    """Preset detail aggregation: runs + per-model/per-config leaderboards
    + pooled mean + score-over-time trend."""
    settings = get_settings()
    preset = preset_store.resolve(session, settings.TASK_PRESETS_DIR, slug)
    if preset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"preset {slug!r} not found",
        )
    stats = _list_stats(session, {slug})

    runs = (
        session.execute(select(Run).where(Run.preset_ref == slug).order_by(Run.created_at.desc()))
        .scalars()
        .all()
    )
    run_by_id = {r.id: r for r in runs}
    latest, sandboxes = _latest_overalls(session, [r.id for r in runs])
    means = _run_means(latest, sandboxes)

    run_points: list[PresetRunPoint] = []
    sb_count: dict[UUID, int] = {}
    for sb in sandboxes.values():
        sb_count[sb.run_id] = sb_count.get(sb.run_id, 0) + 1
    for run in runs:
        overalls = means.get(run.id, [])
        run_points.append(
            PresetRunPoint(
                run_id=run.id,
                status=run.status,
                created_at=run.created_at,
                finished_at=run.finished_at,
                sandbox_count=sb_count.get(run.id, 0),
                mean_overall_score=(round(sum(overalls) / len(overalls), 2) if overalls else None),
                scored_sandboxes=len(overalls),
            )
        )

    # Leaderboards — scored cells bucketed by participant model / by harness
    # config; each cell contributes its latest overall to its bucket's mean.
    by_model: dict[str, dict] = {}
    by_config: dict[str, dict] = {}
    all_scores: list[float] = []
    for sb_id, score in latest.items():
        sb = sandboxes[sb_id]
        run = run_by_id[sb.run_id]
        all_scores.append(score)

        model = sandbox_model(sb.harness_config or {}, run.model_name)
        mb = by_model.setdefault(model, {"scores": [], "runs": set()})
        mb["scores"].append(score)
        mb["runs"].add(sb.run_id)

        ckey, clabel = _config_key(sb.harness_config or {})
        cb = by_config.setdefault(
            ckey,
            {
                "scores": [],
                "runs": set(),
                "label": clabel,
                "adapter": str(
                    (sb.harness_config or {}).get("adapter")
                    or (sb.harness_config or {}).get("harness")
                    or "?"
                ),
                "name": str((sb.harness_config or {}).get("name") or "").strip() or None,
            },
        )
        cb["scores"].append(score)
        cb["runs"].add(sb.run_id)

    def _board(raw: dict, *, config: bool) -> list[PresetLeaderboardEntry]:
        entries = [
            PresetLeaderboardEntry(
                key=key,
                label=bucket.get("label") or key,
                adapter=bucket.get("adapter") if config else None,
                name=bucket.get("name") if config else None,
                mean_overall_score=round(sum(bucket["scores"]) / len(bucket["scores"]), 2),
                n=len(bucket["scores"]),
                runs=len(bucket["runs"]),
            )
            for key, bucket in raw.items()
        ]
        # Best first; deterministic ties (more cells, then key).
        entries.sort(key=lambda e: (-e.mean_overall_score, -e.n, e.key))
        return entries

    # Trend — one point per run with >=1 scored cell, oldest first.
    trend_runs = sorted(
        (r for r in runs if means.get(r.id)),
        key=lambda r: (r.created_at, r.id),
    )
    trend = [
        PresetTrendPoint(
            run_id=r.id,
            created_at=r.created_at,
            finished_at=r.finished_at,
            mean_overall_score=round(sum(means[r.id]) / len(means[r.id]), 2),
            scored_sandboxes=len(means[r.id]),
        )
        for r in trend_runs
    ]

    return PresetAnalytics(
        preset=_preset_out(preset, stats),
        runs=run_points,
        by_model=_board(by_model, config=False),
        by_config=_board(by_config, config=True),
        mean_overall_score=(round(sum(all_scores) / len(all_scores), 2) if all_scores else None),
        n_scored=len(all_scores),
        trend=trend,
    )


@router.post("/{slug}", status_code=status.HTTP_201_CREATED)
async def create_preset(slug: str, body: PresetCreate, session: DbSession) -> PresetOut:
    """Save a task prompt as a user preset at the path's slug."""
    settings = get_settings()
    try:
        slug = preset_store.validate_slug(slug)
        taken = preset_store.taken_slugs(session, settings.TASK_PRESETS_DIR)
        if slug in taken:
            vendored = preset_store.get_vendored(settings.TASK_PRESETS_DIR, slug)
            raise preset_store.PresetError(
                "name_conflict",
                f"preset slug {slug!r} is already taken",
                name=slug,
                source="vendored" if vendored is not None else "user",
                suggested_name=preset_store.suggest_slug(taken, slug),
            )
        row = TaskPreset(
            slug=slug,
            title=body.title,
            description=body.description,
            task_prompt=body.task_prompt,
            category=body.category,
            tags=body.tags,
            source="user",
            revision=1,
            suggested_adapter=body.suggested_adapter,
            suggested_models=body.suggested_models,
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            # Concurrent create lost the slug race — same rename flow.
            session.rollback()
            taken = preset_store.taken_slugs(session, settings.TASK_PRESETS_DIR)
            raise preset_store.PresetError(
                "name_conflict",
                f"preset slug {slug!r} is already taken",
                name=slug,
                source="user",
                suggested_name=preset_store.suggest_slug(taken, slug),
            ) from None
        session.refresh(row)  # server-default timestamps
    except preset_store.PresetError as exc:
        raise _preset_err(exc) from exc

    stats = _list_stats(session, {slug})
    return _preset_out(preset_store.user_to_preset(row), stats)


@router.patch("/{slug}")
async def update_preset(slug: str, body: PresetUpdate, session: DbSession) -> PresetOut:
    """Partial-edit a USER preset; bumps ``revision`` on every change."""
    settings = get_settings()
    try:
        slug = preset_store.validate_slug(slug)
        if preset_store.get_vendored(settings.TASK_PRESETS_DIR, slug) is not None:
            raise preset_store.PresetError(
                "vendored_protected",
                f"{slug!r} is a vendored preset — it can't be edited "
                "(copy it via POST /api/presets/{new-slug} instead)",
            )
        row = preset_store.get_user(session, slug)
        if row is None:
            raise preset_store.PresetError("not_found", f"preset {slug!r} not found")
        changed = False
        for field_name in (
            "title",
            "task_prompt",
            "description",
            "category",
            "tags",
            "suggested_adapter",
            "suggested_models",
        ):
            value = getattr(body, field_name)
            if value is not None:
                setattr(row, field_name, value)
                changed = True
        if changed:
            row.revision = (row.revision or 1) + 1
            session.commit()
            session.refresh(row)  # pick up server-side onupdate timestamps
    except preset_store.PresetError as exc:
        raise _preset_err(exc) from exc

    stats = _list_stats(session, {slug})
    return _preset_out(preset_store.user_to_preset(row), stats)


@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_preset(slug: str, session: DbSession) -> None:
    """Delete a user preset (vendored presets are protected).

    Runs keep their ``preset_ref`` — the stored task_prompt is verbatim, so
    a deleted preset only removes the reusable library entry, not history.
    """
    settings = get_settings()
    try:
        slug = preset_store.validate_slug(slug)
        if preset_store.get_vendored(settings.TASK_PRESETS_DIR, slug) is not None:
            raise preset_store.PresetError(
                "vendored_protected",
                f"{slug!r} is a vendored preset and cannot be deleted",
            )
        row = preset_store.get_user(session, slug)
        if row is None:
            raise preset_store.PresetError("not_found", f"preset {slug!r} not found")
        session.delete(row)
        session.commit()
    except preset_store.PresetError as exc:
        raise _preset_err(exc) from exc
