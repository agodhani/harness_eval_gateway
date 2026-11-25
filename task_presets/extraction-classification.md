---
slug: extraction-classification
title: Extract structured records
description: Turn unstructured text inputs into validated structured output with a classification pass.
category: extraction
tags: [extraction, classification, json, data]
suggested_adapter: miniloop
suggested_rubric: extraction-accuracy
---

The workspace contains unstructured text inputs (documents, messages, or records). Convert them into structured, classified output.

Your task:

1. Inspect the inputs and define a consistent record schema (a JSON object per input) covering the fields present — e.g. entities, dates, amounts, references, and a `category` label from a small taxonomy you define and document.
2. Write an extractor (`extract.py`) that parses every input file and emits `records.json` (or `records.jsonl`) — one record per input, all records conforming to the same schema.
3. Handle the messy cases honestly: missing fields become `null` (never invented), ambiguous records get a `category` of `uncategorized` rather than a confident guess, and malformed inputs are skipped with a warning logged to `errors.txt` rather than crashing the run.
4. Validate: every output record parses as JSON and carries the declared fields.
5. Write a short `SUMMARY.md` — record counts per category, fields frequently missing, and any inputs skipped.

Deterministic parsing where structure allows it; the deliverables are `extract.py`, the records output, `errors.txt` if any skips occurred, and `SUMMARY.md`.
