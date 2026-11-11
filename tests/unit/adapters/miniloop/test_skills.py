"""Hermetic tests for miniloop skill-pack discovery."""

from pathlib import Path

from adapters.miniloop.skills import SkillCatalog, parse_frontmatter


def _write_skill(root: Path, rel: str, text: str) -> Path:
    path = root / rel / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


FRONT = "---\nname: {name}\ndescription: {desc}\n---\n\n# Title\n\nBody line.\n"


def test_catalog_finds_skills_recursively(tmp_path: Path) -> None:
    root = tmp_path / "packs"
    _write_skill(root, "demo/skills/foo", FRONT.format(name="foo", desc="Foo skill."))
    _write_skill(
        root, "demo/skills/group/bar", FRONT.format(name="bar", desc="Bar skill.")
    )
    catalog = SkillCatalog.load(root)
    assert len(catalog.entries) == 2
    names = {e.name for e in catalog.entries}
    assert names == {"foo", "bar"}
    assert all(e.pack == "demo" for e in catalog.entries)


def test_catalog_line_format(tmp_path: Path) -> None:
    root = tmp_path / "packs"
    _write_skill(root, "demo/skills/foo", FRONT.format(name="foo", desc="Does foo."))
    catalog = SkillCatalog.load(root)
    lines = catalog.catalog_lines()
    assert len(lines) == 1
    assert lines[0].startswith("- foo (demo): Does foo. — ")
    assert lines[0].endswith("demo/skills/foo/SKILL.md")


def test_catalog_missing_root(tmp_path: Path) -> None:
    catalog = SkillCatalog.load(tmp_path / "does-not-exist")
    assert catalog.entries == []
    assert catalog.catalog_lines() == []


def test_catalog_ignores_non_pack_files(tmp_path: Path) -> None:
    root = tmp_path / "packs"
    _write_skill(root, "demo/skills/foo", FRONT.format(name="foo", desc="d"))
    # A SKILL.md outside any pack's skills dir but inside the pack is tolerated
    # only via the fallback search; nested junk dirs are still found — that's
    # fine. Here we assert PACK.json etc. don't break loading.
    (root / "demo" / "PACK.json").write_text("{}")
    catalog = SkillCatalog.load(root)
    assert len(catalog.entries) == 1


def test_resolve_bare_and_pack_prefixed(tmp_path: Path) -> None:
    root = tmp_path / "packs"
    _write_skill(root, "packa/skills/shared", FRONT.format(name="shared", desc="a"))
    _write_skill(root, "packb/skills/other", FRONT.format(name="other", desc="b"))
    catalog = SkillCatalog.load(root)

    assert catalog.resolve("packa:shared").pack == "packa"  # type: ignore[union-attr]
    assert catalog.resolve("other").name == "other"  # type: ignore[union-attr]
    assert catalog.resolve("packb:missing") is None
    assert catalog.resolve("") is None


def test_resolve_by_dir_name_when_frontmatter_differs(tmp_path: Path) -> None:
    root = tmp_path / "packs"
    # frontmatter name differs from dir name — dir name still resolves
    _write_skill(root, "demo/skills/real-dir", FRONT.format(name="fm-name", desc="d"))
    catalog = SkillCatalog.load(root)
    assert catalog.resolve("real-dir") is not None
    assert catalog.resolve("fm-name") is not None


def test_missing_frontmatter_falls_back(tmp_path: Path) -> None:
    root = tmp_path / "packs"
    _write_skill(root, "demo/skills/dirnamed", "# Just a heading\n\nFirst body line here.\n")
    catalog = SkillCatalog.load(root)
    entry = catalog.entries[0]
    assert entry.name == "dirnamed"
    assert entry.description == "First body line here."


def test_quoted_and_folded_description() -> None:
    text = (
        "---\n"
        'name: "quoted-name"\n'
        "description: >\n"
        "  First line of desc.\n"
        "  Second line of desc.\n"
        "---\n\nBody.\n"
    )
    meta, body = parse_frontmatter(text)
    assert meta["name"] == "quoted-name"
    assert "First line of desc." in meta["description"]
    assert "Second line of desc." in meta["description"]
    assert body.strip().startswith("Body.")


def test_parse_frontmatter_absent() -> None:
    meta, body = parse_frontmatter("# No frontmatter\n\ntext\n")
    assert meta == {}
    assert body.startswith("# No frontmatter")


def test_spec_kit_style_nested_frontmatter(tmp_path: Path) -> None:
    """spec-kit SKILL.md files carry extra frontmatter keys (metadata:, etc.)
    — parser must not choke and must still get name/description."""
    root = tmp_path / "packs"
    _write_skill(
        root,
        "spec-kit/skills/speckit-plan",
        '---\nname: "speckit-plan"\ndescription: "Plan it."\n'
        'argument-hint: "optional"\nmetadata:\n  author: "github-spec-kit"\n'
        "user-invocable: true\n---\n\n## Body\n",
    )
    catalog = SkillCatalog.load(root)
    assert len(catalog.entries) == 1
    entry = catalog.entries[0]
    assert entry.name == "speckit-plan"
    assert entry.description == "Plan it."
