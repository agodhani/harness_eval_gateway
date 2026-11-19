---
slug: docs-writer
title: Write project documentation
description: Produce clear developer documentation for the code in the workspace.
category: docs
tags: [docs, writing, markdown, repo]
suggested_adapter: miniloop
---

The workspace contains a small code project with sparse or missing documentation.

Your task:

1. Read the code and work out what the project does, how it's structured, and how to run it.
2. Write a `README.md` at the workspace root covering: what the project does, requirements/setup, how to run it (with concrete commands), the main entry points and module layout, and a short usage example.
3. Where the public API is non-obvious, add or repair docstrings on the key functions/classes — accurate, concise, matching the real signatures.
4. Keep the docs honest: document only what the code actually does — no aspirational features, no invented flags or config.
5. If the project has behaviour that can be checked quickly (a CLI, an example script), verify your usage example actually works before documenting it.

The deliverable is the README plus any docstring improvements — do not otherwise modify the code.
