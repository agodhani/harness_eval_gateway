---
slug: bugfix-repo
title: Find and fix a planted bug
description: Diagnose a failing behaviour in the seeded repository and fix it with a regression test.
category: bugfix
tags: [python, debugging, repo, tests]
suggested_adapter: miniloop
---

The workspace contains a small Python project with a real (but planted) bug — one public behaviour is wrong, and there is a failing or missing test that would have caught it.

Your task:

1. Read the project layout and locate the bug (a README or test suite may hint at the expected behaviour).
2. Reproduce it — write a small script or run the existing tests to confirm the wrong behaviour before changing anything.
3. Fix the bug with a minimal, correct change. Do not refactor unrelated code.
4. Add or repair a regression test that fails on the buggy code and passes on your fix.
5. Run the full test suite and leave the repo green.

Summarize your findings at the end: what the bug was, the root cause, and what you changed.
