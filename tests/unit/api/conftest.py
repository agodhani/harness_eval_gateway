"""Shared helpers for skillpack-import tests — in-memory codeload fixtures."""

from __future__ import annotations

import io
import re
import tarfile
from collections.abc import Mapping

from skillpack_import.github import FetchResponse


def make_tarball(
    files: Mapping[str, bytes | str], *, root: str = "acme-repo-abc1234"
) -> bytes:
    """Build a gzipped tarball like codeload's ``<owner>-<repo>-<sha>/…``.

    Keys are repo-relative paths; values str (utf-8) or bytes.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        top = tarfile.TarInfo(root)
        top.type = tarfile.DIRTYPE
        tf.addfile(top)
        for name, content in files.items():
            data = content.encode() if isinstance(content, str) else content
            ti = tarfile.TarInfo(f"{root}/{name}")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


SKILL_MD = """\
---
name: {name}
description: {desc}
---

# {name}

Instructions body.
"""


def skill_repo_files() -> dict[str, str]:
    """A representative skills repo: two skills under skills/, one nested."""
    return {
        "README.md": "# repo\n",
        "skills/linting/SKILL.md": SKILL_MD.format(name="linting", desc="Lint things"),
        "skills/linting/references/rules.md": "rules\n",
        "skills/linting/scripts/run.sh": "#!/bin/sh\n",
        "skills/formatting/SKILL.md": SKILL_MD.format(name="formatting", desc="Fmt"),
        "group/nested/deep/SKILL.md": "---\nname: deep\n---\nno description\n",
        "docs/notes.md": "not a skill\n",
    }


class FakeFetcher:
    """Injectable Fetcher — serves ``body`` for whitelisted refs, else 404.

    ``ok_refs=None`` accepts every ref; a set restricts which codeload refs
    return 200 (drives the longest-first probing). ``status`` forces a fixed
    failure status for every call.
    """

    def __init__(
        self,
        body: bytes = b"",
        *,
        ok_refs: set[str] | None = None,
        status: int | None = None,
        resp_headers: dict | None = None,
    ) -> None:
        self.body = body
        self.ok_refs = ok_refs
        self.status = status
        self.resp_headers = resp_headers or {}
        self.calls: list[str] = []
        self.headers_seen: list[dict | None] = []

    async def get(
        self, url: str, *, headers: dict | None = None, max_bytes: int
    ) -> FetchResponse:
        self.calls.append(url)
        self.headers_seen.append(headers)
        if self.status is not None:
            return FetchResponse(status=self.status, headers=self.resp_headers)
        m = re.search(r"/tar\.gz/(.+)$", url)
        ref = m.group(1) if m else ""
        if self.ok_refs is None or ref in self.ok_refs:
            return FetchResponse(status=200, body=self.body, headers={})
        return FetchResponse(status=404, headers={})
