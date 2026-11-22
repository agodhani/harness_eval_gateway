"""PR24 — task-input seeds: archive safety, hashing, prepare/copy, runtime hook.

Hermetic: tmp_path trees only. Git clone tests use a faked ``git_runner``
(no subprocess); the runtime-spawn seed copy is exercised against a real
``SandboxRuntime`` whose ``_docker``/``ensure_network`` are stubbed — the
workspace copy + ``SpawnResult.seed_sha`` are the real code path.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from sandbox import seeds
from sandbox.runtime import SandboxRuntime
from sandbox.spec import SandboxSpec

# ---------------------------------------------------------------------------
# archive builders
# ---------------------------------------------------------------------------


def _tar_bytes(
    members: dict[str, bytes | str],
    *,
    root: str | None = None,
) -> bytes:
    """Build an in-memory tar. ``root`` prefixes every member name."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, content in members.items():
            data = content.encode() if isinstance(content, str) else content
            full = f"{root}/{name}" if root else name
            ti = tarfile.TarInfo(full)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _zip_bytes(members: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            data = content.encode() if isinstance(content, str) else content
            zf.writestr(name, data)
    return buf.getvalue()


def _write_tar(path: Path, members: dict[str, bytes | str], *, root=None) -> Path:
    path.write_bytes(_tar_bytes(members, root=root))
    return path


def _stage_upload(uploads: Path, archive: Path, *, upload_id: str) -> Path:
    """Mimic the uploads router's staged layout + meta.json."""
    d = uploads / upload_id
    d.mkdir(parents=True)
    # longest-suffix-first, same as the router's _stored_suffix
    lower = archive.name.lower()
    suffix = next(
        (s for s in sorted(seeds.ALLOWED_UPLOAD_SUFFIXES, key=len, reverse=True)
         if lower.endswith(s)),
        archive.suffix,
    )
    (d / f"{seeds.UPLOAD_ARCHIVE_NAME}{suffix}").write_bytes(archive.read_bytes())
    (d / seeds.UPLOAD_META_NAME).write_text(
        json.dumps(
            {
                "filename": archive.name,
                "size": archive.stat().st_size,
                "sha256": "abc",
            }
        )
    )
    return d


UPLOAD_ID = "12345678-1234-5678-9abc-def012345678"


# ---------------------------------------------------------------------------
# member-name validation + extraction jail
# ---------------------------------------------------------------------------


def test_member_relpath_rejects_traversal_absolute_and_friends() -> None:
    for bad in (
        "../evil.py",
        "a/../../evil.py",
        "/etc/passwd",
        "C:/win/system.ini",
        "..\\evil.py",
        "a\\..\\b.py",
        "x\x00y.py",
        "",
    ):
        with pytest.raises(seeds.SeedError):
            seeds._member_relpath(bad)
    assert seeds._member_relpath("a/b/c.py").as_posix() == "a/b/c.py"
    assert seeds._member_relpath(".") is None


def test_check_archive_ok_and_kind_detection(tmp_path: Path) -> None:
    tar_p = _write_tar(tmp_path / "a.tar", {"x.py": "1"})
    zip_p = tmp_path / "a.zip"
    zip_p.write_bytes(_zip_bytes({"x.py": "1"}))
    assert seeds.check_archive(tar_p) == "tar"
    assert seeds.check_archive(zip_p) == "zip"


def test_check_archive_rejects_garbage_and_empty(tmp_path: Path) -> None:
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"not an archive at all")
    with pytest.raises(seeds.SeedError, match="not a readable"):
        seeds.check_archive(junk)

    empty = tmp_path / "empty.tar"
    empty.write_bytes(_tar_bytes({}))
    with pytest.raises(seeds.SeedError, match="no extractable files"):
        seeds.check_archive(empty)


def test_extract_archive_rejects_traversal_and_specials(tmp_path: Path) -> None:
    # zip member with ../
    zip_p = tmp_path / "evil.zip"
    zip_p.write_bytes(_zip_bytes({"../evil.py": "x", "ok.py": "y"}))
    with pytest.raises(seeds.SeedError, match="escapes"):
        seeds.extract_archive(zip_p, tmp_path / "out1")
    assert not (tmp_path / "evil.py").exists()

    # tar symlink member — even in-tree links never come from archives
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        data = b"x"
        ti = tarfile.TarInfo("real.py")
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))
        link = tarfile.TarInfo("link.py")
        link.type = tarfile.SYMTYPE
        link.linkname = "real.py"
        tf.addfile(link)
    sym = tmp_path / "sym.tar"
    sym.write_bytes(buf.getvalue())
    with pytest.raises(seeds.SeedError, match="not a regular file"):
        seeds.extract_archive(sym, tmp_path / "out2")


def test_extract_archive_flattens_single_root_dir(tmp_path: Path) -> None:
    tar_p = _write_tar(
        tmp_path / "r.tar", {"a.py": "1", "sub/b.py": "2"}, root="proj-abc123"
    )
    dest = tmp_path / "ws"
    n = seeds.extract_archive(tar_p, dest)
    assert n == 2
    assert (dest / "a.py").read_text() == "1"
    assert (dest / "sub" / "b.py").read_text() == "2"
    # the common root itself is stripped — not nested under proj-abc123/
    assert not (dest / "proj-abc123").exists()


def test_extract_archive_keeps_flat_layout(tmp_path: Path) -> None:
    tar_p = _write_tar(tmp_path / "f.tar", {"a.py": "1", "other/c.py": "3"})
    dest = tmp_path / "ws"
    seeds.extract_archive(tar_p, dest)
    assert (dest / "a.py").is_file()
    assert (dest / "other" / "c.py").is_file()


def test_extract_archive_size_and_file_caps(tmp_path: Path) -> None:
    big = tmp_path / "big.tar"
    big.write_bytes(_tar_bytes({"a.bin": b"x" * 1024, "b.bin": b"y" * 1024}))
    with pytest.raises(seeds.SeedError, match="size cap"):
        seeds.extract_archive(big, tmp_path / "o1", max_bytes=1500)
    with pytest.raises(seeds.SeedError, match="file cap"):
        seeds.extract_archive(big, tmp_path / "o2", max_files=1)


def test_check_archive_enforces_the_same_member_caps(tmp_path: Path) -> None:
    """Upload-check must catch what extraction would reject — a staged
    upload can never turn unusable at provisioning."""
    p = tmp_path / "big.zip"
    p.write_bytes(_zip_bytes({"a.bin": b"x" * 1024, "b.bin": b"y" * 1024}))
    assert seeds.check_archive(p) == "zip"  # under the defaults
    with pytest.raises(seeds.SeedError, match="size cap"):
        seeds.check_archive(p, max_bytes=1500)
    with pytest.raises(seeds.SeedError, match="file cap"):
        seeds.check_archive(p, max_files=1)


def test_check_archive_and_extract_reject_encrypted_zip(tmp_path: Path) -> None:
    """An encrypted zip member passes no extraction — it must fail at check
    time too (the 422 boundary), not as a surprise RuntimeError later."""
    raw = bytearray(_zip_bytes({"x.py": "1"}))
    assert raw[:4] == b"PK\x03\x04"
    raw[6] |= 0x01  # local header flag bit 0 = encrypted
    cd = raw.find(b"PK\x01\x02")
    assert cd > 0
    raw[cd + 8] |= 0x01  # central-directory flag
    p = tmp_path / "enc.zip"
    p.write_bytes(bytes(raw))
    with pytest.raises(seeds.SeedError, match="encrypted"):
        seeds.check_archive(p)
    with pytest.raises(seeds.SeedError, match="encrypted"):
        seeds.extract_archive(p, tmp_path / "out")
    assert not (tmp_path / "out" / "x.py").exists()


def test_check_and_extract_translate_corrupt_zip_central_directory(
    tmp_path: Path,
) -> None:
    """A zip with an intact EOCD but a corrupt later CD entry passes
    ``is_zipfile`` (it only reads the EOCD) then dies inside ``infolist()``
    — the boundary must surface SeedError, never raw BadZipFile."""
    raw = bytearray(_zip_bytes({"a.txt": "1", "b.txt": "2"}))
    first = raw.find(b"PK\x01\x02")
    second = raw.find(b"PK\x01\x02", first + 4)
    assert first > 0 and second > 0
    raw[second : second + 4] = b"XX\x01\x02"  # corrupt the second CD magic
    p = tmp_path / "corrupt.zip"
    p.write_bytes(bytes(raw))
    assert zipfile.is_zipfile(p)  # the premise: magic check can't save you
    with pytest.raises(seeds.SeedError):
        seeds.check_archive(p)
    with pytest.raises(seeds.SeedError):
        seeds.extract_archive(p, tmp_path / "out")


def test_check_and_extract_translate_truncated_tar(tmp_path: Path) -> None:
    """A tar cut inside a member's data raises tarfile.ReadError when the
    scan tries to skip to the next header — the boundary must surface
    SeedError, never the raw library error."""
    raw = _tar_bytes({"a.txt": "x" * 300, "b.txt": "y" * 300, "c.txt": "3"})
    # a.txt: hdr 0-511 + data 512-1023; b.txt: hdr 1024-1535 + data
    # 1536-2047 — cut mid-b.txt-data so getmembers() dies skipping it.
    p = tmp_path / "trunc.tar"
    p.write_bytes(raw[:1636])
    with pytest.raises(seeds.SeedError):
        seeds.check_archive(p)
    with pytest.raises(seeds.SeedError):
        seeds.extract_archive(p, tmp_path / "out")


def test_check_and_extract_translate_truncated_targz(tmp_path: Path) -> None:
    """A .tar.gz cut mid-deflate-stream propagates EOFError even out of
    ``tarfile.is_tarfile`` — kind DETECTION itself sits inside the same
    SeedError boundary."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in (("a.txt", b"1"), ("b.txt", b"2"), ("c.txt", b"3")):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    raw = buf.getvalue()
    p = tmp_path / "trunc.tar.gz"
    p.write_bytes(raw[: len(raw) // 2])
    with pytest.raises(seeds.SeedError):
        seeds.check_archive(p)
    with pytest.raises(seeds.SeedError):
        seeds.extract_archive(p, tmp_path / "out")


def test_check_and_extract_reject_file_dir_name_conflicts(tmp_path: Path) -> None:
    """file ``d`` + file ``d/x`` (implicit dir) or dir ``d/`` + file ``d``
    can't coexist on disk — reject at check, never crash mid-extract. The
    ``d`` + ``d/x`` shape is also what the flatten-strip silently dropped
    (``x`` + ``x/y`` kept only ``y``)."""
    zp = tmp_path / "conflict.zip"
    zp.write_bytes(_zip_bytes({"d": "1", "d/x.py": "2"}))
    with pytest.raises(seeds.SeedError, match="conflict"):
        seeds.check_archive(zp)
    with pytest.raises(seeds.SeedError, match="conflict"):
        seeds.extract_archive(zp, tmp_path / "o1")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        di = tarfile.TarInfo("d")
        di.type = tarfile.DIRTYPE
        tf.addfile(di)
        for name, data in (("d", b"1"), ("e.py", b"2")):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    tp = tmp_path / "conflict.tar"
    tp.write_bytes(buf.getvalue())
    with pytest.raises(seeds.SeedError, match="conflict"):
        seeds.check_archive(tp)
    with pytest.raises(seeds.SeedError, match="conflict"):
        seeds.extract_archive(tp, tmp_path / "o2")


def test_duplicate_file_members_are_not_conflicts(tmp_path: Path) -> None:
    """Duplicate FILE entries are legal tar/zip content (last write wins) —
    the conflict check must not over-reject."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for data in (b"old", b"new"):
            ti = tarfile.TarInfo("a.py")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    p = tmp_path / "dup.tar"
    p.write_bytes(buf.getvalue())
    assert seeds.check_archive(p) == "tar"
    dest = tmp_path / "out"
    seeds.extract_archive(p, dest)
    assert (dest / "a.py").read_bytes() == b"new"


def test_check_and_extract_reject_unsupported_zip_compression(
    tmp_path: Path,
) -> None:
    """``z.open`` raises NotImplementedError for methods outside
    store/deflate/bzip2/lzma (AES-99 & friends) — metadata the check must
    reject before any staged upload reaches extraction."""
    raw = bytearray(_zip_bytes({"x.py": "1"}))
    raw[8:10] = (99).to_bytes(2, "little")  # local-header method field
    cd = raw.find(b"PK\x01\x02")
    assert cd > 0
    raw[cd + 10 : cd + 12] = (99).to_bytes(2, "little")  # central-dir method
    p = tmp_path / "aes.zip"
    p.write_bytes(bytes(raw))
    with pytest.raises(seeds.SeedError, match="compression"):
        seeds.check_archive(p)
    with pytest.raises(seeds.SeedError, match="compression"):
        seeds.extract_archive(p, tmp_path / "out")


def test_dir_members_count_toward_the_file_cap(tmp_path: Path) -> None:
    """The cap bounds ALL materialized entries — an archive of empty dirs
    can't flood inodes under it the way a file flood can't."""
    members = {f"d{i}/": b"" for i in range(6)}
    members["file.txt"] = "x"
    p = tmp_path / "dirs.zip"
    p.write_bytes(_zip_bytes(members))  # 6 dir + 1 file = 7 entries
    assert seeds.check_archive(p, max_files=7) == "zip"
    with pytest.raises(seeds.SeedError, match="file cap"):
        seeds.check_archive(p, max_files=6)
    with pytest.raises(seeds.SeedError, match="file cap"):
        seeds.extract_archive(p, tmp_path / "out", max_files=6)


# ---------------------------------------------------------------------------
# hash_tree + copy_seed
# ---------------------------------------------------------------------------


def test_hash_tree_deterministic_and_content_sensitive(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    for root in (a, b):
        (root / "sub").mkdir(parents=True)
        (root / "f.py").write_text("x = 1")
        (root / "sub" / "g.txt").write_text("hi")
    assert seeds.hash_tree(a) == seeds.hash_tree(b)
    (b / "f.py").write_text("x = 2")
    assert seeds.hash_tree(a) != seeds.hash_tree(b)
    # same bytes under a different relpath → different hash (path is content)
    (b / "f.py").write_text("x = 1")
    (b / "sub" / "g.txt").rename(b / "sub" / "h.txt")
    assert seeds.hash_tree(a) != seeds.hash_tree(b)


def test_copy_seed_identical_bytes_identical_hash(tmp_path: Path) -> None:
    src = tmp_path / "seed"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "m.py").write_text("m = 1")
    (src / "README.md").write_text("# seed")
    h1 = seeds.copy_seed(src, tmp_path / "ws1")
    h2 = seeds.copy_seed(src, tmp_path / "ws2")
    assert h1 == h2
    assert (tmp_path / "ws2" / "pkg" / "m.py").read_text() == "m = 1"
    # copy is independent of the source afterwards
    (src / "pkg" / "m.py").write_text("mutated")
    assert (tmp_path / "ws1" / "pkg" / "m.py").read_text() == "m = 1"


def test_copy_seed_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(seeds.SeedError, match="does not exist"):
        seeds.copy_seed(tmp_path / "nope", tmp_path / "ws")


# ---------------------------------------------------------------------------
# prepare_run_seed — archive path
# ---------------------------------------------------------------------------


def test_prepare_run_seed_archive_roundtrip_and_idempotent(tmp_path: Path) -> None:
    uploads = tmp_path / "uploads"
    archive = _write_tar(
        tmp_path / "seed.tar.gz",
        {"app/main.py": "print('x')", "docs/readme.md": "hi"},
        root="bundle",
    )
    _stage_upload(uploads, archive, upload_id=UPLOAD_ID)

    seed = seeds.prepare_run_seed(
        "run-1",
        {"archive_id": UPLOAD_ID},
        seed_root=tmp_path / "seeds",
        uploads=uploads,
    )
    assert seed is not None
    assert seed.path == tmp_path / "seeds" / "run-1" / "content"
    assert (seed.path / "app" / "main.py").read_text() == "print('x')"
    assert (seed.path / "docs" / "readme.md").is_file()
    assert seed.source["type"] == "archive"
    assert seed.source["upload_id"] == UPLOAD_ID
    assert seed.source["filename"] == "seed.tar.gz"
    # meta.json lands next to content/
    meta = seeds._seed_meta(tmp_path / "seeds" / "run-1")
    assert meta and meta["sha256"] == seed.sha256

    # second call reuses the materialization (idempotent)
    seed2 = seeds.prepare_run_seed(
        "run-1",
        {"archive_id": UPLOAD_ID},
        seed_root=tmp_path / "seeds",
        uploads=uploads,
    )
    assert seed2.sha256 == seed.sha256
    assert seed2.source == seed.source


def test_prepare_run_seed_unknown_archive_id_fails(tmp_path: Path) -> None:
    with pytest.raises(seeds.SeedError, match="not found"):
        seeds.prepare_run_seed(
            "run-x",
            {"archive_id": "00000000-0000-0000-0000-000000000000"},
            seed_root=tmp_path / "seeds",
            uploads=tmp_path / "uploads",
        )


def test_prepare_run_seed_archive_id_cannot_escape_uploads(tmp_path: Path) -> None:
    """The jail: an archive_id that resolves outside <uploads> never reads."""
    uploads = tmp_path / "uploads"
    outside = tmp_path / "elsewhere"
    outside.mkdir(parents=True)
    uploads.mkdir(parents=True)
    # a would-be seed staged OUTSIDE the uploads root
    (outside / "archive.tar").write_bytes(_tar_bytes({"x.py": "1"}))
    assert seeds.resolve_upload_archive(uploads, "../elsewhere") is None
    with pytest.raises(seeds.SeedError, match="not found"):
        seeds.prepare_run_seed(
            "run-x",
            {"archive_id": "../elsewhere"},
            seed_root=tmp_path / "seeds",
            uploads=uploads,
        )


def test_prepare_run_seed_drops_escapee_symlinks(tmp_path: Path) -> None:
    """A symlinked seed file pointing out of the tree is dropped + warned."""
    uploads = tmp_path / "uploads"
    archive = _write_tar(tmp_path / "s.tar", {"a.py": "1"})
    _stage_upload(uploads, archive, upload_id=UPLOAD_ID)

    seed_dir = tmp_path / "seeds" / "run-e" / "content"
    seed_dir.mkdir(parents=True)
    # Pre-stage the materialization via extract, then inject an escapee link
    # and let prepare see a dirty tree? — instead exercise the sweep through
    # a repo seed: faked git materializes content + an escaping symlink.
    target = tmp_path / "secret.txt"
    target.write_text("host secret")

    def fake_git(argv: list[str]) -> str:
        if argv[2] == "checkout":
            content = Path(argv[1])
            (content / "good.py").write_text("1")
            (content / "evil").symlink_to(target)
            (content / "ok-link").symlink_to(content / "good.py")
        if argv[-1] == "HEAD":
            return "cafe\n"
        return ""

    seed = seeds.prepare_run_seed(
        "run-e",
        {"repo_url": "https://github.com/o/r"},
        seed_root=tmp_path / "seeds",
        uploads=uploads,
        git_runner=fake_git,
    )
    assert not (seed.path / "evil").exists() and not (seed.path / "evil").is_symlink()
    assert (seed.path / "ok-link").is_symlink()  # in-tree links survive
    assert any("escapee" in w or "symlink" in w for w in seed.warnings)


# ---------------------------------------------------------------------------
# prepare_run_seed — git path (faked subprocess)
# ---------------------------------------------------------------------------


def test_git_runner_isolates_host_config(monkeypatch) -> None:
    """git must run hermetic to host gitconfig — an ambient insteadOf or
    hooksPath would bypass the https-only public-clone gate."""
    seen: dict = {}

    def fake_run(args, **kwargs):
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(args, 0, "ok\n", "")

    monkeypatch.setattr(seeds.subprocess, "run", fake_run)
    assert seeds._git(["status"], timeout=5) == "ok\n"
    env = seen["env"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_SYSTEM"] == os.devnull


def test_git_runner_strips_ambient_git_env(monkeypatch) -> None:
    """GIT_* env vars are the env-based sibling of ambient gitconfig —
    GIT_CONFIG_KEY_n/VALUE_n can inject insteadOf/hooksPath, GIT_DIR /
    GIT_WORK_TREE redirect the whole init/fetch/checkout sequence."""
    monkeypatch.setenv("GIT_DIR", "/evil/repo")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/evil/hooks")
    seen: dict = {}

    def fake_run(args, **kwargs):
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(seeds.subprocess, "run", fake_run)
    seeds._git(["status"], timeout=5)
    env = seen["env"]
    assert "GIT_DIR" not in env
    assert "GIT_CONFIG_COUNT" not in env
    assert "GIT_CONFIG_KEY_0" not in env
    assert "GIT_CONFIG_VALUE_0" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"


def test_git_runner_timeout_is_one_deadline_across_calls(monkeypatch) -> None:
    """SEED_GIT_TIMEOUT_SECONDS caps the whole fetch+checkout sequence, so
    a drip-feeding remote can't hold it for N × cap."""
    spawned: list = []

    def fake_run(*args, **kwargs):
        spawned.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(seeds.subprocess, "run", fake_run)
    runner = seeds.git_runner(0)  # budget already exhausted at creation
    with pytest.raises(seeds.SeedError, match="timed out"):
        runner(["status"])
    assert spawned == []  # no subprocess ever launched


def test_prepare_run_seed_rejects_credentialed_repo_url(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_git(argv: list[str]) -> str:
        calls.append(list(argv))
        return ""

    with pytest.raises(seeds.SeedError, match="credential"):
        seeds.prepare_run_seed(
            "run-c",
            {"repo_url": "https://user:secret@github.com/acme/proj"},
            seed_root=tmp_path / "seeds",
            uploads=tmp_path / "uploads",
            git_runner=fake_git,
        )
    assert calls == []


def test_prepare_run_seed_rejects_option_shaped_ref(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_git(argv: list[str]) -> str:
        calls.append(list(argv))
        return ""

    for bad_ref in ("--tags", "--upload-pack=/bin/x", "ref with space"):
        with pytest.raises(seeds.SeedError, match="ref"):
            seeds.prepare_run_seed(
                "run-r",
                {"repo_url": "https://github.com/acme/proj", "ref": bad_ref},
                seed_root=tmp_path / "seeds",
                uploads=tmp_path / "uploads",
                git_runner=fake_git,
            )
    assert calls == []  # no git invocation ever saw the hostile ref


def test_prepare_run_seed_rejects_malformed_or_secret_bearing_url(
    tmp_path: Path,
) -> None:
    """Empty-netloc URLs must fail at the seed boundary, not as a spawn
    error per cell; a query/fragment can carry a token that would persist
    verbatim into run.config — both rejected before any git call."""
    calls: list[list[str]] = []

    def fake_git(argv: list[str]) -> str:
        calls.append(list(argv))
        return ""

    for bad in (
        "https://",
        "https:///path",
        "https://github.com/o/r?private_token=secret",
        "https://github.com/o/r#frag",
    ):
        with pytest.raises(seeds.SeedError):
            seeds.prepare_run_seed(
                "run-b",
                {"repo_url": bad},
                seed_root=tmp_path / "seeds",
                uploads=tmp_path / "uploads",
                git_runner=fake_git,
            )
    assert calls == []


def test_prepare_run_seed_git_clone_pinned_ref(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_git(argv: list[str]) -> str:
        calls.append(list(argv))
        if argv[2] == "checkout":
            Path(argv[1], "repo_file.py").write_text("cloned = True")
            Path(argv[1], "README.md").write_text("readme")
        if argv[-1] == "HEAD":
            return "deadbeefcafe\n"
        return ""

    seed = seeds.prepare_run_seed(
        "run-g",
        {"repo_url": "https://github.com/acme/proj", "ref": "v1.2.3"},
        seed_root=tmp_path / "seeds",
        uploads=tmp_path / "uploads",
        git_runner=fake_git,
    )
    assert seed is not None
    # init → remote add → fetch --depth 1 <ref> → checkout FETCH_HEAD → rev-parse
    fetch = next(c for c in calls if "fetch" in c)
    assert "--depth" in fetch and "1" in fetch
    assert "v1.2.3" in fetch  # pinned ref is what gets fetched
    assert any("checkout" in c and "FETCH_HEAD" in c for c in calls)
    assert seed.source == {
        "type": "repo",
        "url": "https://github.com/acme/proj",
        "ref": "v1.2.3",
        "resolved_sha": "deadbeefcafe",
    }
    assert (seed.path / "repo_file.py").is_file()


def test_prepare_run_seed_git_no_ref_fetches_head(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_git(argv: list[str]) -> str:
        calls.append(list(argv))
        if argv[2] == "checkout":
            Path(argv[1], "f.py").write_text("1")
        if argv[-1] == "HEAD":
            return "aa\n"
        return ""

    seed = seeds.prepare_run_seed(
        "run-h",
        {"repo_url": "https://github.com/acme/proj"},
        seed_root=tmp_path / "seeds",
        uploads=tmp_path / "uploads",
        git_runner=fake_git,
    )
    fetch = next(c for c in calls if "fetch" in c)
    assert fetch[-1] == "HEAD"  # remote default branch
    assert seed.source["ref"] is None
    assert seed.source["resolved_sha"] == "aa"


def test_prepare_run_seed_git_failure_raises_seederror(tmp_path: Path) -> None:
    def bad_git(argv: list[str]) -> str:
        if "fetch" in argv:
            raise seeds.SeedError("git fetch failed (exit 128): not found")
        return ""

    with pytest.raises(seeds.SeedError, match="fetch"):
        seeds.prepare_run_seed(
            "run-i",
            {"repo_url": "https://github.com/acme/missing"},
            seed_root=tmp_path / "seeds",
            uploads=tmp_path / "uploads",
            git_runner=bad_git,
        )
    # no half-materialized seed dir survives a failed prep
    assert not (tmp_path / "seeds" / "run-i" / "content").exists()


def test_prepare_run_seed_rejects_non_https_url(tmp_path: Path) -> None:
    def fake_git(argv: list[str]) -> str:  # never reached
        return ""

    with pytest.raises(seeds.SeedError, match="https"):
        seeds.prepare_run_seed(
            "run-j",
            {"repo_url": "git://github.com/acme/proj"},
            seed_root=tmp_path / "seeds",
            uploads=tmp_path / "uploads",
            git_runner=fake_git,
        )


def test_prepare_run_seed_source_validation(tmp_path: Path) -> None:
    kw = {"seed_root": tmp_path / "seeds", "uploads": tmp_path / "uploads"}
    assert seeds.prepare_run_seed("r", {}, **kw) is None
    assert seeds.prepare_run_seed("r", None, **kw) is None
    with pytest.raises(seeds.SeedError, match="exactly one"):
        seeds.prepare_run_seed(
            "r",
            {"repo_url": "https://x/y", "archive_id": UPLOAD_ID},
            **kw,
        )
    with pytest.raises(seeds.SeedError, match="requires repo_url"):
        seeds.prepare_run_seed("r", {"archive_id": UPLOAD_ID, "ref": "v1"}, **kw)


# ---------------------------------------------------------------------------
# uploads_root / upload resolution
# ---------------------------------------------------------------------------


def test_uploads_root_defaults_to_artifact_sibling(tmp_path: Path) -> None:
    settings = type("S", (), {"ARTIFACT_ROOT": tmp_path / "data" / "artifacts"})()
    root = seeds.uploads_root(settings)
    assert root == (tmp_path / "data" / "uploads").resolve()
    settings2 = type(
        "S",
        (),
        {
            "ARTIFACT_ROOT": tmp_path / "data" / "artifacts",
            "UPLOADS_DIR": tmp_path / "custom",
        },
    )()
    assert seeds.uploads_root(settings2) == (tmp_path / "custom").resolve()


def test_uploads_root_rejects_relative_override(tmp_path: Path) -> None:
    """A relative UPLOADS_DIR resolves against the api process cwd — off
    the ./data mount and out of the jail contract — so it's rejected."""
    settings = type(
        "S",
        (),
        {
            "ARTIFACT_ROOT": tmp_path / "data" / "artifacts",
            "UPLOADS_DIR": "relative/uploads",
        },
    )()
    with pytest.raises(seeds.SeedError, match="absolute"):
        seeds.uploads_root(settings)


def test_resolve_upload_archive_and_meta(tmp_path: Path) -> None:
    uploads = tmp_path / "uploads"
    archive = _write_tar(tmp_path / "s.tar", {"a.py": "1"})
    _stage_upload(uploads, archive, upload_id=UPLOAD_ID)
    found = seeds.resolve_upload_archive(uploads, UPLOAD_ID)
    assert found is not None and found.name.startswith(seeds.UPLOAD_ARCHIVE_NAME)
    meta = seeds.read_upload_meta(uploads, UPLOAD_ID)
    assert meta and meta["filename"] == "s.tar"
    assert seeds.resolve_upload_archive(uploads, "nope-not-here") is None
    assert seeds.read_upload_meta(uploads, "nope") is None


# ---------------------------------------------------------------------------
# runtime.spawn — seed copy + SpawnResult.seed_sha (real runtime, faked docker)
# ---------------------------------------------------------------------------


class _DockerStub:
    """SandboxRuntime with docker calls stubbed — the FS work (workspace
    mkdir, seed copy, state file) runs for real."""

    def __init__(self, rt: SandboxRuntime) -> None:
        self.rt = rt
        self.docker_calls: list[list[str]] = []

    def docker(self, args, *, check=True, timeout=None):
        self.docker_calls.append(list(args))
        if args and args[0] == "inspect":
            return subprocess.CompletedProcess(args, 1, "", "no such container")
        if args and args[0] == "network":
            return subprocess.CompletedProcess(args, 0, "[]", "")
        return subprocess.CompletedProcess(args, 0, "cid-stub\n", "")


def _stub_runtime(tmp_path: Path) -> tuple[SandboxRuntime, _DockerStub]:
    rt = SandboxRuntime(
        artifact_root=tmp_path / "artifacts",
        state_dir=tmp_path / "sandboxes",
    )
    stub = _DockerStub(rt)
    rt._docker = stub.docker  # type: ignore[method-assign]
    rt.ensure_network = lambda: {"network": "stub"}  # type: ignore[method-assign]
    return rt, stub


def test_runtime_spawn_copies_seed_and_returns_seed_sha(tmp_path: Path) -> None:
    rt, stub = _stub_runtime(tmp_path)
    seed_dir = tmp_path / "seeds" / "run-1" / "content"
    (seed_dir / "src").mkdir(parents=True)
    (seed_dir / "src" / "app.py").write_text("print('seeded')")
    (seed_dir / "README.md").write_text("task input")

    ws = tmp_path / "sandboxes" / "sb-1" / "workspace"
    spec = SandboxSpec(
        sandbox_id="sb-1",
        run_id="run-1",
        image="img:latest",
        env={},
        workspace_host_path=ws,
        seed_host_path=seed_dir,
    )
    res = rt.spawn(spec)
    assert res.seed_sha is not None
    assert (ws / "src" / "app.py").read_text() == "print('seeded')"
    assert (ws / "README.md").is_file()
    # seed_sha equals the workspace content hash — the cross-cell proof
    assert res.seed_sha == seeds.hash_tree(ws)
    # docker run happened (stubbed) and the workspace was bind-mounted
    run_call = next(c for c in stub.docker_calls if c and c[0] == "run")
    assert any("/workspace" in a for a in run_call)


def test_runtime_spawn_two_specs_identical_seed_sha(tmp_path: Path) -> None:
    rt, _stub = _stub_runtime(tmp_path)
    seed_dir = tmp_path / "seeds" / "run-1" / "content"
    (seed_dir / "pkg").mkdir(parents=True)
    (seed_dir / "pkg" / "m.py").write_text("m = 1")

    results = [
        rt.spawn(
            SandboxSpec(
                sandbox_id=f"sb-{i}",
                run_id="run-1",
                image="img",
                env={},
                workspace_host_path=tmp_path / "sandboxes" / f"sb-{i}" / "workspace",
                seed_host_path=seed_dir,
            )
        )
        for i in (1, 2)
    ]
    # identical bytes per cell — the card's verify condition
    assert results[0].seed_sha is not None
    assert results[0].seed_sha == results[1].seed_sha
    # and each workspace independently holds the bytes (not a shared mount)
    (tmp_path / "sandboxes" / "sb-1" / "workspace" / "pkg" / "m.py").write_text("x")
    assert (
        tmp_path / "sandboxes" / "sb-2" / "workspace" / "pkg" / "m.py"
    ).read_text() == "m = 1"


def test_runtime_spawn_unseeded_returns_none_seed_sha(tmp_path: Path) -> None:
    rt, _stub = _stub_runtime(tmp_path)
    res = rt.spawn(
        SandboxSpec(
            sandbox_id="sb-plain",
            run_id="run-1",
            image="img",
            env={},
            workspace_host_path=tmp_path / "sandboxes" / "sb-plain" / "workspace",
        )
    )
    assert res.seed_sha is None
