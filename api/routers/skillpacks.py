"""F10 — skillpack browsing + GitHub import (``/api/skillpacks*``).

Endpoints:

- ``GET /api/skillpacks`` — every selectable pack: vendored presets plus
  user-imported ones under ``data/skillpacks``. This is the pack list the
  submit form and run validation consult.
- ``POST /api/skillpacks/discover`` — scan a GitHub link (repo root,
  ``/tree/<branch>/<path>``, ``/blob/<branch>/<path>/SKILL.md`` or
  ``raw.githubusercontent.com``), returning the SKILL.md candidates.
- ``POST /api/skillpacks/import`` — persist the selected skills: ``mode=pack``
  writes one named pack (taken name → 409 + ``suggested_name`` for the
  rename-confirm flow); ``mode=merge`` appends into an existing imported
  pack; ``mode=split`` creates one pack per skill (auto-renamed on
  collision).
- ``DELETE /api/skillpacks/{name}`` — remove an imported pack (vendored
  packs are protected).

GitHub traffic goes through an injectable ``Fetcher`` (dependency) — tests
swap it via ``app.dependency_overrides``; network code never runs in unit
tests. CPU/disk work (tar scan, extraction, writes) hops through
``asyncio.to_thread`` to keep the loop free.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from api.config import get_settings
from api.schemas import (
    DiscoveredSkill,
    ImportedSkill,
    PackImportResult,
    SkillpackDiscoverIn,
    SkillpackDiscoverOut,
    SkillpackImportIn,
    SkillpackImportOut,
    SkillPackOut,
)
from orchestrator import provision
from skillpack_import import github, store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/skillpacks", tags=["skillpacks"])


def get_fetcher() -> github.Fetcher:
    """Per-request fetcher — overridden in tests, real httpx in prod."""
    return github.HttpxFetcher()


FetcherDep = Annotated[github.Fetcher, Depends(get_fetcher)]

_FETCH_STATUS = {
    "invalid_url": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "ref_mismatch": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "not_found": status.HTTP_404_NOT_FOUND,
    "no_skills": status.HTTP_404_NOT_FOUND,
    "too_large": status.HTTP_413_CONTENT_TOO_LARGE,
    "rate_limited": status.HTTP_502_BAD_GATEWAY,
    "network": status.HTTP_502_BAD_GATEWAY,
    "bad_archive": status.HTTP_502_BAD_GATEWAY,
}
_STORE_STATUS = {
    "invalid_name": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "unknown_selection": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "empty": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "name_conflict": status.HTTP_409_CONFLICT,
    "vendored_protected": status.HTTP_409_CONFLICT,
    "not_found": status.HTTP_404_NOT_FOUND,
}


def _fetch_err(exc: github.FetchError) -> HTTPException:
    return HTTPException(
        status_code=_FETCH_STATUS.get(exc.kind, status.HTTP_502_BAD_GATEWAY),
        detail=exc.detail,
    )


def _store_err(exc: store.PackStoreError) -> HTTPException:
    detail: str | dict[str, object] = exc.detail
    if isinstance(exc, store.NameConflictError):
        # Structured so the UI can run its confirm-and-rename flow.
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
# GET /api/skillpacks — vendored + imported pack listing
# ---------------------------------------------------------------------------


@router.get("")
async def list_skillpacks() -> list[SkillPackOut]:
    """Every pack selectable in ``harness_configs[].skill_packs``.

    Imported packs share the same name namespace as vendored ones (import
    rejects vendored-name collisions), so ``name`` is a stable selection
    token for run submission.
    """
    settings = get_settings()
    packs = await asyncio.to_thread(
        store.list_packs, settings.SKILLPACKS_DIR, store.imported_root(settings)
    )
    return [
        SkillPackOut(
            name=p.name,
            source=p.source,
            skill_count=p.skill_count,
            skills=p.skills,
            origin_url=p.origin_url,
            imported_at=p.imported_at,
        )
        for p in packs
    ]


# ---------------------------------------------------------------------------
# POST /api/skillpacks/discover — scan a GitHub link for SKILL.md files
# ---------------------------------------------------------------------------


def _scan_scope(snap: github.RepoSnapshot) -> tuple[str, str | None]:
    """(scan subpath, exact-dir filter) for a fetched snapshot.

    Blob links restrict discovery to the linked SKILL.md's own directory:
    the scan covers the dir (so file_count counts siblings) and the result
    keeps only that exact dir — the linked skill is the sole candidate.
    """
    if snap.kind == "blob":
        parent = posixpath.dirname(snap.subpath)
        return parent, parent or "."
    return snap.subpath, None


@router.post("/discover")
async def discover_skillpack(
    body: SkillpackDiscoverIn, fetcher: FetcherDep
) -> SkillpackDiscoverOut:
    settings = get_settings()
    try:
        target = github.parse_github_url(body.url)
        snap = await github.fetch_repo(
            target, fetcher, token=settings.GITHUB_TOKEN
        )
        subpath, only_dir = _scan_scope(snap)
        candidates = await asyncio.to_thread(
            github.scan_skills, snap.data, subpath=subpath
        )
        if only_dir is not None:
            candidates = [c for c in candidates if c.dir == only_dir]
        if not candidates:
            scope = snap.subpath or "repository root"
            raise github.FetchError(
                "no_skills", f"no SKILL.md files found under {scope!r}"
            )
    except github.FetchError as exc:
        raise _fetch_err(exc) from exc
    return SkillpackDiscoverOut(
        url=snap.canonical_url,
        repo=snap.repo,
        ref=snap.ref,
        commit_sha=snap.commit_sha,
        subpath=snap.subpath,
        kind=snap.kind,
        skills=[
            DiscoveredSkill(
                dir=c.dir,
                name=c.name,
                description=c.description,
                file_count=c.file_count,
                has_frontmatter=c.has_frontmatter,
            )
            for c in candidates
        ],
    )


# ---------------------------------------------------------------------------
# POST /api/skillpacks/import — persist selected skills as a pack
# ---------------------------------------------------------------------------


@router.post("/import", status_code=status.HTTP_201_CREATED)
async def import_skillpack(
    body: SkillpackImportIn, fetcher: FetcherDep
) -> SkillpackImportOut:
    settings = get_settings()
    try:
        target = github.parse_github_url(body.url)
        imp_root = store.imported_root(settings)
        vendored_names = set(provision.known_skillpacks(settings.SKILLPACKS_DIR))
        # One pack-name namespace across both roots (see AGENTS.md).
        taken = vendored_names | store.existing_pack_names(imp_root)

        # Pack-target resolution happens BEFORE the fetch so a colliding
        # name fails fast — the UI re-submits with the renamed pack_name.
        pack_name: str | None = None
        if body.mode != "split":
            pack_name = store.sanitize_dir_name(body.pack_name)
            if body.mode == "pack" and pack_name in taken:
                raise store.NameConflictError(
                    pack_name,
                    source="vendored" if pack_name in vendored_names else "imported",
                    suggested_name=store.suggest_pack_name(taken, pack_name),
                )
            if body.mode == "merge" and pack_name in vendored_names:
                raise store.PackStoreError(
                    "vendored_protected",
                    f"{pack_name!r} is a vendored pack — can't append to it",
                )

        snap = await github.fetch_repo(
            target,
            fetcher,
            token=settings.GITHUB_TOKEN,
            pinned_ref=body.ref,
            pinned_subpath=body.subpath,
        )

        wanted = {s.strip().strip("/") for s in body.skills if s.strip()}
        if not wanted:
            raise store.PackStoreError("empty", "no skill directories selected")
        extracted = await asyncio.to_thread(
            github.extract_skill_files, snap.data, wanted
        )
        missing = sorted(d for d in wanted if d not in extracted)
        if missing:
            raise store.PackStoreError(
                "unknown_selection",
                f"skill dirs not found in {snap.repo}@{snap.ref}: "
                + ", ".join(repr(d) for d in missing),
            )

        provenance = {
            "url": snap.canonical_url or body.url,
            "repo": snap.repo,
            "ref": snap.ref,
            "commit_sha": snap.commit_sha,
        }
        results: list[store.ImportedPackResult] = []
        if body.mode == "split":
            # Each skill → its own pack named after the skill dir; taken
            # names auto-rename (x → x-2, x-3…) — reported via issues.
            for d in sorted(wanted):
                want = store.pack_name_for_skill(d)
                free = store.suggest_pack_name(taken, want)
                res = await asyncio.to_thread(
                    store.write_pack,
                    imp_root,
                    free,
                    [store.SkillPayload(source_dir=d, files=extracted[d])],
                    provenance=provenance,
                    vendored_names=vendored_names,
                )
                if free != want:
                    res.issues.insert(
                        0, f"pack name {want!r} taken — imported as {free!r}"
                    )
                taken.add(free)
                results.append(res)
        else:
            payloads = [
                store.SkillPayload(source_dir=d, files=extracted[d])
                for d in sorted(wanted)
            ]
            results.append(
                await asyncio.to_thread(
                    store.write_pack,
                    imp_root,
                    pack_name or "",
                    payloads,
                    provenance=provenance,
                    vendored_names=vendored_names,
                )
            )
    except github.FetchError as exc:
        raise _fetch_err(exc) from exc
    except store.PackStoreError as exc:
        raise _store_err(exc) from exc

    log.info(
        "imported %d skill(s) from %s@%s -> packs %s",
        sum(len(r.imported) for r in results),
        snap.repo,
        snap.ref,
        [r.pack for r in results],
    )
    return SkillpackImportOut(
        packs=[
            PackImportResult(
                pack=r.pack,
                source=r.source,
                skill_count=r.skill_count,
                imported=[ImportedSkill(**vars(i)) for i in r.imported],
                issues=r.issues,
            )
            for r in results
        ]
    )


# ---------------------------------------------------------------------------
# DELETE /api/skillpacks/{name} — remove an imported pack
# ---------------------------------------------------------------------------


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skillpack(name: str) -> None:
    """Delete an imported pack. Vendored packs are protected (409)."""
    settings = get_settings()
    vendored_names = set(provision.known_skillpacks(settings.SKILLPACKS_DIR))
    if name in vendored_names:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{name!r} is a vendored pack and cannot be deleted",
        )
    try:
        await asyncio.to_thread(store.delete_pack, store.imported_root(settings), name)
    except store.PackStoreError as exc:
        raise _store_err(exc) from exc
