# skillpacks/

Vendored skill packs for the **miniloop** harness. In sandboxes this
directory is mounted **read-only at `/opt/skillpacks`**
(`SKILLPACKS_ROOT` env var; miniloop defaults to that path).

## Convention

Each pack is a directory containing skills in the SKILL.md format::

```
skillpacks/
  <pack>/
    PACK.json            # provenance: upstream url, commit, license, notes
    skills/
      <skill-name>/
        SKILL.md         # required — YAML-ish frontmatter + instructions
        references/      # optional supporting docs
        scripts/         # optional executable helpers
        assets/          # optional templates/files
```

`SKILL.md` files start with a small frontmatter block::

```markdown
---
name: my-skill
description: What it does and when to reach for it.
---

# Skill instructions...
```

miniloop scans `<root>/<pack>/skills/**/SKILL.md` recursively at startup,
parses `name`/`description`, injects a catalog into the system prompt, and
serves full skill contents through the `use_skill` tool.

## Packs

| Pack | Skills | Upstream | Notes |
|------|--------|----------|-------|
| `superpowers` | 14 | github.com/obra/superpowers | Only `skills/` vendored |
| `mattpocock-skills` | 28 | github.com/mattpocock/skills | Only `skills/engineering/` + `skills/productivity/` (the promoted buckets) |
| `spec-kit` | 9 | github.com/github/spec-kit | **Generated format**: produced by `specify init --integration claude` (specify-cli 1.4.0), not a raw copy. `skills/` = speckit-* SKILL.md dirs, `templates/` = `.specify/templates`, `scripts/` = `.specify/scripts/bash` (skills reference these; copy into workspace as `.specify/` when using a speckit skill) |

Per-pack provenance (upstream URL, commit SHA, license, vendored paths,
timestamp) lives in each pack's `PACK.json`.

## Adding a pack

1. Vendor skills under `<pack>/skills/**/SKILL.md` (frontmatter `name` +
   `description` recommended; missing fields fall back to the directory name
   and first body line).
2. Add a `PACK.json` with provenance.
3. Keep it small — trim binaries/assets but keep all `SKILL.md`,
   `references/`, and `scripts/` a skill depends on.
