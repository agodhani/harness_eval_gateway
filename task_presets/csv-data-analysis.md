---
slug: csv-data-analysis
title: Analyze a CSV dataset
description: Explore a dataset in the workspace and produce a findings report with a reproducible script.
category: data-analysis
tags: [data, csv, python, analysis, report]
suggested_adapter: miniloop
---

The workspace contains one or more CSV data files. Analyze the data and produce a findings report.

Your task:

1. Load the CSV(s) with a script — `analyze.py` using pandas if available in the environment, otherwise the Python stdlib `csv` module. Do not hand-edit the data.
2. Profile the dataset: row/column counts, dtypes, missing values, and obvious anomalies (duplicates, impossible values, inconsistent categories).
3. Answer the interesting questions the data supports — e.g. distributions, top/bottom groupings, trends over time, notable correlations. Compute the numbers; do not estimate them.
4. Write `REPORT.md` with: dataset overview, data-quality issues found, your computed findings (each with the supporting numbers), and how to re-run the analysis.
5. Make `analyze.py` reproducible — `python analyze.py` regenerates every number quoted in the report, printed or written deterministically.

Deliverables: `analyze.py` and `REPORT.md`. Numbers in the report must match what the script computes.
