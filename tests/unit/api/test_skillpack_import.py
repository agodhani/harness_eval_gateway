"""F11 — unit tests: GitHub URL parsing, tarball scan/extract, pack store,
multi-root provisioning. Hermetic — no HTTP (FakeFetcher), no DB.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.provision import known_skillpacks, stage_skillpacks
from skillpack_import import github, store
from skillpack_import.github import FetchError

from .conftest import SKILL_MD, FakeFetcher, make_tarball, skill_repo_files

# ---------------------------------------------------------------------------
# parse_github_url — accepted shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, kind, segments",
    [
        ("https://github.com/obra/superpowers", "root", ()),
        ("github.com/obra/superpowers", "root", ()),  # scheme-less
        ("https://github.com/obra/superpowers/", "root", ()),
        ("https://github.com/obra/superpowers.git", "root", ()),
        ("https://github.com/obra/superpowers?tab=readme#top", "root", ()),
        ("https://github.com/obra/superpowers/tree/main", "tree", ("main",)),
        (
            "https://github.com/obra/superpowers/tree/main/skills/x",
            "tree",
            ("main", "skills", "x"),
        ),
        (
            "https://github.com/o/r/blob/main/s/SKILL.md",
            "blob",
            ("main", "s", "SKILL.md"),
        ),
        (
            "https://raw.githubusercontent.com/o/r/main/s/SKILL.md",
            "blob",
            ("main", "s", "SKILL.md"),
        ),
    ],
)
def test_parse_github_url_accepted(url: str, kind: str, segments: tuple) -> None:
    t = github.parse_github_url(url)
    assert t.owner in ("obra", "o")
    assert t.kind == kind
    assert t.segments == segments


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "https://gitlab.com/o/r",                        # not github
        "obra/superpowers",                              # bare shorthand — no host
        "o/r/tree/main",
        "https://github.com/o",                          # missing repo
        "https://github.com/o/r/issues/1",               # unsupported page
        "https://github.com/o/r/pull/5",
        "https://github.com/o/r/settings",
        "https://github.com/o/r/tree",                   # tree w/o ref
        "https://github.com/o/r/blob/main/README.md",    # blob not SKILL.md
        "https://raw.githubusercontent.com/o/r/main",   # raw w/o file
        "https://github.com/bad owner/r",
        "ftp://github.com/o/r",                          # non-http scheme
    ],
)
def test_parse_github_url_rejected(url: str) -> None:
    with pytest.raises(FetchError) as ei:
        github.parse_github_url(url)
    assert ei.value.kind == "invalid_url"


def test_parse_preserves_slashy_branch_segments() -> None:
    """Branch names with slashes stay ambiguous — resolved at fetch time."""
    t = github.parse_github_url("https://github.com/o/r/tree/feature/x/skills")
    assert t.segments == ("feature", "x", "skills")


# ---------------------------------------------------------------------------
# fetch_repo — ref probing + pinned ref
# ---------------------------------------------------------------------------


def _body() -> bytes:
    return make_tarball(skill_repo_files())


async def test_fetch_repo_root_uses_head() -> None:
    f = FakeFetcher(_body())
    snap = await github.fetch_repo(
        github.parse_github_url("github.com/o/r"), f
    )
    assert snap.ref == "HEAD"
    assert snap.subpath == ""
    assert snap.repo == "o/r"
    assert snap.commit_sha == "abc1234"  # parsed from tarball root dir
    assert f.calls == ["https://codeload.github.com/o/r/tar.gz/HEAD"]


async def test_fetch_repo_probes_longest_ref_first() -> None:
    f = FakeFetcher(_body(), ok_refs={"feature/x"})
    snap = await github.fetch_repo(
        github.parse_github_url("github.com/o/r/tree/feature/x/skills"), f
    )
    # longest candidate feature/x/skills 404s; feature/x wins -> subpath skills
    assert [c.split("/tar.gz/")[1] for c in f.calls] == [
        "feature/x/skills",
        "feature/x",
    ]
    assert snap.ref == "feature/x"
    assert snap.subpath == "skills"


async def test_fetch_repo_simple_tree_link() -> None:
    f = FakeFetcher(_body(), ok_refs={"main"})
    snap = await github.fetch_repo(
        github.parse_github_url("github.com/o/r/tree/main/skills"), f
    )
    assert snap.ref == "main"
    assert snap.subpath == "skills"


async def test_fetch_repo_not_found() -> None:
    f = FakeFetcher(_body(), ok_refs=set())
    with pytest.raises(FetchError) as ei:
        await github.fetch_repo(github.parse_github_url("github.com/o/r"), f)
    assert ei.value.kind == "not_found"


async def test_fetch_repo_rate_limit() -> None:
    f = FakeFetcher(status=403, resp_headers={"x-ratelimit-remaining": "0"})
    with pytest.raises(FetchError) as ei:
        await github.fetch_repo(github.parse_github_url("github.com/o/r"), f)
    assert ei.value.kind == "rate_limited"


async def test_fetch_repo_upstream_500() -> None:
    f = FakeFetcher(status=500)
    with pytest.raises(FetchError) as ei:
        await github.fetch_repo(github.parse_github_url("github.com/o/r"), f)
    assert ei.value.kind == "network"


async def test_fetch_repo_pinned_ref_and_subpath() -> None:
    f = FakeFetcher(_body(), ok_refs={"deadbee"})
    snap = await github.fetch_repo(
        github.parse_github_url("github.com/o/r/tree/feature/x/skills"),
        f,
        pinned_ref="deadbee",
        pinned_subpath="skills",
    )
    assert snap.ref == "deadbee"
    assert snap.subpath == "skills"
    assert len(f.calls) == 1  # pinning skips probing


async def test_fetch_repo_sends_token() -> None:
    f = FakeFetcher(_body())
    await github.fetch_repo(
        github.parse_github_url("github.com/o/r"), f, token="ghp_x"
    )
    assert f.headers_seen[0] == {"Authorization": "Bearer ghp_x"}


# ---------------------------------------------------------------------------
# scan_skills / extract_skill_files — fixture tarballs
# ---------------------------------------------------------------------------


def test_scan_skills_full_repo() -> None:
    skills = github.scan_skills(_body())
    by_dir = {s.dir: s for s in skills}
    assert set(by_dir) == {"skills/linting", "skills/formatting", "group/nested/deep"}
    lint = by_dir["skills/linting"]
    assert lint.name == "linting"
    assert lint.description == "Lint things"
    assert lint.file_count == 3  # SKILL.md + references/ + scripts/
    assert lint.has_frontmatter
    deep = by_dir["group/nested/deep"]
    assert deep.name == "deep"
    assert deep.description == "no description"  # falls back to first body line


def test_scan_skills_subpath() -> None:
    skills = github.scan_skills(_body(), subpath="skills")
    assert {s.dir for s in skills} == {"skills/linting", "skills/formatting"}


def test_scan_skills_root_level_skill_md() -> None:
    body = make_tarball({"SKILL.md": SKILL_MD.format(name="root-skill", desc="r"),
                         "references/x.md": "x"})
    skills = github.scan_skills(body)
    assert [s.dir for s in skills] == ["."]
    assert skills[0].name == "root-skill"
    assert skills[0].file_count == 2


def test_scan_skills_name_fallback_to_dir() -> None:
    body = make_tarball({"s/x/SKILL.md": "no frontmatter body\n"})
    skills = github.scan_skills(body)
    assert skills[0].name == "x"
    assert skills[0].description == "no frontmatter body"
    assert not skills[0].has_frontmatter


def test_scan_skills_bad_archive() -> None:
    with pytest.raises(FetchError) as ei:
        github.scan_skills(b"not a tarball")
    assert ei.value.kind == "bad_archive"


def test_extract_skill_files_full_dirs() -> None:
    got = github.extract_skill_files(_body(), {"skills/linting", "skills/formatting"})
    assert set(got) == {"skills/linting", "skills/formatting"}
    assert set(got["skills/linting"]) == {
        "SKILL.md",
        "references/rules.md",
        "scripts/run.sh",
    }


def test_extract_skill_files_root_dir() -> None:
    got = github.extract_skill_files(_body(), {"."})
    # "." = the whole repo is the skill dir — every file belongs, keyed by
    # repo-relative path.
    assert "README.md" in got["."]
    assert "skills/linting/SKILL.md" in got["."]
    assert "docs/notes.md" in got["."]


def test_extract_skill_files_skips_traversal_members() -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        top = tarfile.TarInfo("root")
        top.type = tarfile.DIRTYPE
        tf.addfile(top)
        for name in ("root/s/SKILL.md", "root/s/../evil.txt"):
            data = b"x"
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    got = github.extract_skill_files(buf.getvalue(), {"s"})
    # 's/../evil.txt' normalizes OUT of s/ — never matches the s/ prefix.
    assert set(got["s"]) == {"SKILL.md"}


def test_extract_skill_files_missing_dir_absent() -> None:
    got = github.extract_skill_files(_body(), {"no/such"})
    assert got == {}


# ---------------------------------------------------------------------------
# store — names, write/merge/dedup, list, delete, imported_root
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,ok",
    [
        ("my-pack", True),
        ("pack_2", True),
        ("A1", True),
        ("..", False),
        ("a/b", False),
        ("bad name", False),
        ("", False),
        ("-leading", False),
        ("dot.name", False),
        ("x" * 64, True),
        ("x" * 65, False),
    ],
)
def test_sanitize_dir_name(name: str, ok: bool) -> None:
    if ok:
        assert store.sanitize_dir_name(name) == name
    else:
        with pytest.raises(store.PackStoreError) as ei:
            store.sanitize_dir_name(name)
        assert ei.value.kind == "invalid_name"


def test_imported_root_derives_from_artifact_root(tmp_path: Path) -> None:
    ns = SimpleNamespace(ARTIFACT_ROOT=tmp_path / "data" / "artifacts")
    assert store.imported_root(ns) == tmp_path / "data" / "skillpacks"
    ns2 = SimpleNamespace(
        ARTIFACT_ROOT=tmp_path / "x", IMPORTED_SKILLPACKS_DIR=tmp_path / "custom"
    )
    assert store.imported_root(ns2) == tmp_path / "custom"


def _payload(source_dir: str, name: str, extra: dict[str, bytes] | None = None) -> store.SkillPayload:
    files = {"SKILL.md": SKILL_MD.format(name=name, desc=f"{name} desc").encode()}
    files.update(extra or {})
    return store.SkillPayload(source_dir=source_dir, files=files)


def test_write_pack_creates_layout_and_manifest(tmp_path: Path) -> None:
    root = tmp_path / "imported"
    res = store.write_pack(
        root,
        "my-pack",
        [_payload("skills/linting", "linting")],
        provenance={"url": "https://github.com/o/r", "repo": "o/r", "ref": "main", "commit_sha": "abc"},
    )
    pack_dir = root / "my-pack"
    assert (pack_dir / "skills" / "linting" / "SKILL.md").is_file()
    manifest = json.loads((pack_dir / "PACK.json").read_text())
    assert manifest["source"] == "github-import"
    assert manifest["repo"] == "o/r"
    assert manifest["imports"][0]["skills"] == ["linting"]
    assert res.imported[0].dir == "linting"
    assert not res.imported[0].renamed
    assert res.skill_count == 1


def test_write_pack_dedupes_same_basename(tmp_path: Path) -> None:
    res = store.write_pack(
        tmp_path / "imp",
        "p",
        [
            _payload("a/lint", "lint-a"),
            _payload("b/lint", "lint-b"),
            _payload("c/lint", "lint-c"),
        ],
        provenance={},
    )
    dirs = [i.dir for i in res.imported]
    assert dirs == ["lint", "lint-2", "lint-3"]
    assert [i.renamed for i in res.imported] == [False, True, True]


def test_write_pack_merges_into_existing(tmp_path: Path) -> None:
    root = tmp_path / "imp"
    store.write_pack(root, "p", [_payload("s/one", "one")], provenance={})
    res = store.write_pack(root, "p", [_payload("s/one", "one-dup")], provenance={})
    # same source dir basename -> deduped to one-2; both skills present
    assert res.imported[0].dir == "one-2"
    assert res.skill_count == 2
    manifest = json.loads((root / "p" / "PACK.json").read_text())
    assert len(manifest["imports"]) == 2


def test_write_pack_rejects_vendored_name(tmp_path: Path) -> None:
    with pytest.raises(store.PackStoreError) as ei:
        store.write_pack(
            tmp_path / "imp",
            "superpowers",
            [_payload("s/x", "x")],
            provenance={},
            vendored_names={"superpowers", "spec-kit"},
        )
    assert ei.value.kind == "name_conflict"


# ---------------------------------------------------------------------------
# suggest_pack_name / pack-name helpers
# ---------------------------------------------------------------------------


def test_suggest_pack_name_first_free() -> None:
    assert store.suggest_pack_name({"a"}, "a") == "a-2"
    assert store.suggest_pack_name({"a", "a-2"}, "a") == "a-3"
    assert store.suggest_pack_name(set(), "free") == "free"


def test_suggest_pack_name_continues_numbered_stem() -> None:
    assert store.suggest_pack_name({"x-9"}, "x-9") == "x-10"
    assert store.suggest_pack_name({"x", "x-2"}, "x") == "x-3"


def test_suggest_pack_name_truncates_long_stem() -> None:
    base = "n" * 64
    cand = store.suggest_pack_name({base}, base)
    assert len(cand) <= 64
    assert cand.startswith("n" * 62)


def test_existing_pack_names(tmp_path: Path) -> None:
    root = tmp_path / "imp"
    (root / "p1" / "skills").mkdir(parents=True)
    (root / "p2").mkdir()
    (root / "file.txt").write_text("not a dir")
    assert store.existing_pack_names(root) == {"p1", "p2"}
    assert store.existing_pack_names(tmp_path / "nope") == set()


def test_pack_name_for_skill() -> None:
    assert store.pack_name_for_skill("skills/frontend-design") == "frontend-design"
    assert store.pack_name_for_skill("group/nested/deep") == "deep"
    assert store.pack_name_for_skill("weird name!") == "weird-name"


def test_write_pack_rejects_no_skill_md(tmp_path: Path) -> None:
    with pytest.raises(store.PackStoreError) as ei:
        store.write_pack(
            tmp_path / "imp",
            "p",
            [store.SkillPayload(source_dir="s/x", files={"notes.md": b"hi"})],
            provenance={},
        )
    assert ei.value.kind == "unknown_selection"


def test_write_pack_rejects_traversal_file(tmp_path: Path) -> None:
    with pytest.raises(store.PackStoreError) as ei:
        store.write_pack(
            tmp_path / "imp",
            "p",
            [
                store.SkillPayload(
                    source_dir="s/x",
                    files={"SKILL.md": b"---\nname: x\n---\n", "../evil.txt": b"e"},
                )
            ],
            provenance={},
        )
    assert ei.value.kind == "invalid_name"


def test_list_packs_merges_vendored_and_imported(tmp_path: Path) -> None:
    vend = tmp_path / "skillpacks"
    (vend / "vend-a" / "skills" / "s1").mkdir(parents=True)
    (vend / "vend-a" / "skills" / "s1" / "SKILL.md").write_text(
        SKILL_MD.format(name="s1", desc="d")
    )
    imp = tmp_path / "data" / "skillpacks"
    store.write_pack(imp, "imp-a", [_payload("s/x", "skill-x")], provenance={})

    packs = store.list_packs(vend, imp)
    by_name = {p.name: p for p in packs}
    assert by_name["vend-a"].source == "vendored"
    assert by_name["vend-a"].skills == ["s1"]
    assert by_name["imp-a"].source == "imported"
    assert by_name["imp-a"].skills == ["skill-x"]
    # missing roots are tolerated
    assert store.list_packs(tmp_path / "none1", tmp_path / "none2") == []


def test_delete_pack(tmp_path: Path) -> None:
    imp = tmp_path / "imp"
    store.write_pack(imp, "gone", [_payload("s/x", "x")], provenance={})
    store.delete_pack(imp, "gone")
    assert not (imp / "gone").exists()
    with pytest.raises(store.PackStoreError) as ei:
        store.delete_pack(imp, "gone")
    assert ei.value.kind == "not_found"


# ---------------------------------------------------------------------------
# provisioning across both roots
# ---------------------------------------------------------------------------


def test_known_skillpacks_merges_roots(tmp_path: Path) -> None:
    vend = tmp_path / "skillpacks"
    imp = tmp_path / "data" / "skillpacks"
    (vend / "pack-a").mkdir(parents=True)
    (imp / "imp-pack").mkdir(parents=True)
    assert known_skillpacks(vend, imp) == ["imp-pack", "pack-a"]
    assert known_skillpacks(vend) == ["pack-a"]
    assert known_skillpacks(tmp_path / "missing") == []


def test_stage_skillpacks_resolves_across_roots(tmp_path: Path) -> None:
    vend = tmp_path / "skillpacks"
    imp = tmp_path / "data" / "skillpacks"
    for root, pack in ((vend, "vend-a"), (imp, "imp-a")):
        d = root / pack / "skills" / "demo"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(SKILL_MD.format(name=f"d-{pack}", desc="x"))
    dest = stage_skillpacks(
        ["imp-a", "vend-a"], tmp_path / "staged",
        source_root=vend, extra_roots=(imp,),
    )
    assert (dest / "imp-a" / "skills" / "demo" / "SKILL.md").is_file()
    assert (dest / "vend-a" / "skills" / "demo" / "SKILL.md").is_file()
    with pytest.raises(ValueError, match="unknown skill pack"):
        stage_skillpacks(["nope"], tmp_path / "s2", source_root=vend, extra_roots=(imp,))
    with pytest.raises(ValueError, match="escapes"):
        stage_skillpacks([".."], tmp_path / "s3", source_root=vend, extra_roots=(imp,))
