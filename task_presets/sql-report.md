---
slug: sql-report
title: Write SQL queries + a report
description: Produce correct SQL and a findings report against a database in the workspace.
category: sql
tags: [sql, sqlite, data, report]
suggested_adapter: miniloop
suggested_rubric: code-default
---

The workspace contains a SQLite database (or `.sql` schema/seed files you can load into `sqlite3`).

Your task:

1. Explore the schema — tables, columns, keys, and representative rows.
2. Answer a reporting brief (pick the questions the data can answer — the workspace README or task notes will name them, else choose meaningful business questions like top performers, trends, anomalies).
3. Write each answer as a single SQL query saved under `queries/` (`q1_*.sql`, `q2_*.sql`, …), runnable with `sqlite3 <db> < queries/qN.sql`.
4. Verify every query actually executes and returns correct results — cross-check aggregates against raw rows where feasible. No syntactically valid but semantically wrong answers (watch NULL handling, JOIN fan-out, and grouping).
5. Write `REPORT.md`: each question, the query filename, the result table, and a one-line interpretation.

Deliverables: `queries/*.sql` plus `REPORT.md`. Every reported number must come from a checked-in query.
