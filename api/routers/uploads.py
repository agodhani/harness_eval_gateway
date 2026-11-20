"""PR24 — task-input archive uploads (``/api/uploads*``).

The submit form stages a zip/tar archive here BEFORE submitting the run;
``POST /api/uploads`` streams it to ``<data>/uploads/<uuid>/archive<suffix>``
under the upload byte cap, hashes it, and runs the archive member safety
scan (``sandbox.seeds.check_archive`` — no extraction happens at upload
time). The returned upload id is what ``RunCreate.task_inputs.archive_id``
consumes; provisioning extracts the staged archive into the run's seed
tree with the same jail rules.

Layout::

    <data>/uploads/<uuid>/archive.zip     # the staged archive
    <data>/uploads/<uuid>/meta.json       # {id, filename, size, sha256, kind}

Safety: the upload id is server-generated (uuid); the client filename is
never used as a path part — only its suffix selects the stored name's
extension. Resolution + extraction stay jailed under ``<data>/uploads``
exactly like the artifact-download jail.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from api.config import get_settings
from api.schemas import UploadOut
from sandbox import seeds

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/uploads", tags=["uploads"])

_CHUNK = 1024 * 1024


def _stored_suffix(filename: str) -> str | None:
    """The whitelist-matched suffix for a client filename (lowercased),
    or None — multi-part suffixes (.tar.gz) are checked longest-first."""
    lower = filename.lower()
    for suffix in sorted(seeds.ALLOWED_UPLOAD_SUFFIXES, key=len, reverse=True):
        if lower.endswith(suffix):
            return suffix
    return None


def _meta_path(upload_dir: Path) -> Path:
    return upload_dir / seeds.UPLOAD_META_NAME


def _write_meta(upload_dir: Path, meta: dict) -> None:
    _meta_path(upload_dir).write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )


@router.post("", response_model=UploadOut, status_code=status.HTTP_201_CREATED)
async def upload_archive(
    file: Annotated[UploadFile, File(description="zip/tar archive to stage")],
) -> UploadOut:
    """Stage a task-input archive under ``<data>/uploads/<id>/``.

    422 on a missing/unsupported filename or an archive whose members are
    unsafe (traversal, absolute paths, links/specials, empty); 413 when the
    file exceeds ``UPLOAD_MAX_BYTES``.
    """
    settings = get_settings()
    filename = (file.filename or "").strip()
    if not filename:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="upload needs a filename",
        )
    suffix = _stored_suffix(filename)
    if suffix is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"unsupported archive type for {filename!r} — accepted: "
                + ", ".join(seeds.ALLOWED_UPLOAD_SUFFIXES)
            ),
        )

    root = seeds.uploads_root(settings)
    upload_id = uuid.uuid4()
    upload_dir = root / str(upload_id)
    staged = upload_dir / f"{seeds.UPLOAD_ARCHIVE_NAME}{suffix}"
    upload_dir.mkdir(parents=True, exist_ok=False)

    # Stream to disk with the byte cap enforced per chunk — never buffer a
    # multipart body in memory.
    sha = hashlib.sha256()
    size = 0
    try:
        with staged.open("wb") as dst:
            while chunk := await file.read(_CHUNK):
                size += len(chunk)
                if size > settings.UPLOAD_MAX_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail=(
                            f"archive exceeds the upload cap "
                            f"({settings.UPLOAD_MAX_BYTES} bytes)"
                        ),
                    )
                sha.update(chunk)
                dst.write(chunk)
    except BaseException:
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise

    # Member safety scan (no extraction) — traversal, absolute paths, links,
    # specials, empty archives all reject here and nothing is kept.
    try:
        kind = seeds.check_archive(staged)
    except seeds.SeedError as e:
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unsafe archive: {e}",
        ) from e

    meta = {
        "id": str(upload_id),
        "filename": filename,
        "size": size,
        "sha256": sha.hexdigest(),
        "kind": kind,
        "uploaded_at": datetime.now(UTC).isoformat(),
    }
    _write_meta(upload_dir, meta)
    log.info("staged upload %s (%s, %d bytes)", upload_id, filename, size)
    return UploadOut(
        id=upload_id,
        filename=filename,
        size=size,
        sha256=sha.hexdigest(),
        kind=kind,
        uploaded_at=meta["uploaded_at"],
    )


@router.get("/{upload_id}", response_model=UploadOut)
def get_upload(upload_id: uuid.UUID) -> UploadOut:
    """The staged upload's metadata — 404 for unknown/invalid ids."""
    root = seeds.uploads_root(get_settings())
    meta = seeds.read_upload_meta(root, str(upload_id))
    if meta is None or seeds.resolve_upload_archive(root, str(upload_id)) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="upload not found"
        )
    return UploadOut(
        id=upload_id,
        filename=str(meta.get("filename") or ""),
        size=int(meta.get("size") or 0),
        sha256=str(meta.get("sha256") or ""),
        kind=str(meta.get("kind") or ""),
        uploaded_at=str(meta.get("uploaded_at") or ""),
    )
