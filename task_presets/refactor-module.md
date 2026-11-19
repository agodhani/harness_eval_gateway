---
slug: refactor-module
title: Refactor a tangled module
description: Restructure a hard-to-read module for clarity without changing behaviour.
category: refactor
tags: [python, refactoring, code-quality]
suggested_adapter: miniloop
---

The workspace contains a working Python module (or small app) that is functionally correct but hard to read: long functions, unclear names, duplicated logic, and/or deep nesting.

Your task:

1. Study the module and its tests. Establish the behavioural baseline first — run the existing tests (if any) or write a quick characterization check.
2. Refactor for readability and maintainability: extract well-named functions, remove duplication, flatten nested control flow, and replace magic numbers with named constants where appropriate.
3. Preserve observable behaviour exactly — same public API, same outputs, same error handling. Do not add features or fix unrelated bugs.
4. Keep the diff reviewable: prefer several focused changes over one rewrite.
5. Re-run the baseline (tests / characterization check) and confirm nothing changed behaviourally.

End with a short summary of the structural improvements you made.
