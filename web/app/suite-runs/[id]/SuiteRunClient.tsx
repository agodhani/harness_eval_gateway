"use client";

/**
 * PR26 — /suite-runs/[id]: one suite batch (task × cell matrix).
 *
 * GET /api/suite-runs/{id} — every run the fan-out produced with its
 * sandbox cells: judge `overall` beside the deterministic checker roll-up
 * (pass/fail/skipped/error + pass rate). Expanding a cell lists each
 * check outcome. The batch header carries the pooled aggregates.
 */
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import {
  ApiError,
  getSuiteRun,
  type CheckerResult,
  type SuiteCell,
  type SuiteRunResults,
  type SuiteRunTask,
} from "@/lib/api";
import { formatTs, shortId } from "@/lib/format";
import {
  Badge,
  Card,
  EmptyState,
  ErrorNote,
  Loading,
  statusTone,
} from "@/components/ui";

function scoreTone(score: number): "good" | "mid" | "bad" {
  if (score >= 8) return "good";
  if (score >= 5) return "mid";
  return "bad";
}

/** ✅ / ❌ / ⊘ / ⚠ per check status. */
function checkTone(
  status: string,
): { tone: "green" | "red" | "amber" | "gray"; glyph: string } {
  switch (status) {
    case "pass":
      return { tone: "green", glyph: "✓" };
    case "fail":
      return { tone: "red", glyph: "✗" };
    case "skipped":
      return { tone: "gray", glyph: "⊘" };
    default:
      return { tone: "amber", glyph: "!" };
  }
}

function CheckBadge({ c }: { c: CheckerResult }) {
  const { tone, glyph } = checkTone(c.status);
  return (
    <span
      className={`badge badge--${tone}`}
      style={{ fontFamily: "var(--mono)" }}
      title={`${c.check} ${JSON.stringify(c.spec)} — ${c.detail}`}
    >
      {glyph} {c.check}
    </span>
  );
}

function passRateTone(rate: number | null): "green" | "amber" | "red" | "gray" {
  if (rate == null) return "gray";
  if (rate >= 0.8) return "green";
  if (rate >= 0.5) return "amber";
  return "red";
}

function CellChip({ cell }: { cell: SuiteCell }) {
  const [open, setOpen] = useState(false);
  const ch = cell.checks;
  return (
    <div className="stack" style={{ gap: "0.3rem" }}>
      <button
        type="button"
        className="btn btn--sm btn--ghost"
        style={{ justifyContent: "flex-start", fontFamily: "var(--mono)" }}
        onClick={() => setOpen((v) => !v)}
        title={cell.sandbox_id}
      >
        {cell.label}
      </button>
      <div className="row" style={{ gap: "0.35rem", flexWrap: "wrap" }}>
        <Badge tone={statusTone(cell.status)}>{cell.status}</Badge>
        {ch.total > 0 ? (
          <Badge tone={passRateTone(ch.pass_rate)}>
            {ch.passed}/{ch.total} checks
            {ch.pass_rate != null ? ` · ${Math.round(ch.pass_rate * 100)}%` : ""}
          </Badge>
        ) : (
          <span className="faint" style={{ fontSize: "0.72rem" }}>
            no checks
          </span>
        )}
        {(ch.skipped > 0 || ch.errors > 0) && (
          <span className="faint" style={{ fontSize: "0.7rem" }}>
            {ch.skipped > 0 ? `⊘${ch.skipped} ` : ""}
            {ch.errors > 0 ? `!${ch.errors}` : ""}
          </span>
        )}
      </div>
      {open && ch.checks.length > 0 ? (
        <div className="chip-row" style={{ marginTop: "0.2rem" }}>
          {ch.checks.map((c, i) => (
            <CheckBadge key={i} c={c} />
          ))}
        </div>
      ) : null}
      {open && cell.terminal_reason ? (
        <div className="faint" style={{ fontSize: "0.7rem" }}>
          terminal: {cell.terminal_reason}
        </div>
      ) : null}
    </div>
  );
}

function TaskCard({ task }: { task: SuiteRunTask }) {
  return (
    <Card
      title={
        <>
          <span className="faint" style={{ fontWeight: 400 }}>
            #{task.suite_index}
          </span>{" "}
          {task.task_id}
        </>
      }
    >
      <div className="row" style={{ marginTop: 0, marginBottom: "0.6rem" }}>
        <Link href={`/runs/${task.run_id}`} className="mono" title={task.run_id}>
          run {shortId(task.run_id)}
        </Link>
        {task.preset_ref ? (
          <Link href={`/presets/${task.preset_ref}`} className="mono">
            {task.preset_ref}
          </Link>
        ) : (
          <Badge tone="outline">inline prompt</Badge>
        )}
        <Badge tone={statusTone(task.status)}>{task.status}</Badge>
        <span className="faint" style={{ fontSize: "0.75rem" }}>
          {formatTs(task.created_at)}
          {task.finished_at ? ` → ${formatTs(task.finished_at)}` : ""}
        </span>
        <Link
          href={`/runs/${task.run_id}/results`}
          className="btn btn--sm btn--ghost"
          style={{ marginLeft: "auto" }}
        >
          results →
        </Link>
      </div>
      {task.cells.length === 0 ? (
        <EmptyState>No cells — run provisioned no sandboxes.</EmptyState>
      ) : (
        <div
          className="grid"
          style={{
            display: "grid",
            gap: "0.75rem",
            gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))",
          }}
        >
          {task.cells.map((cell) => (
            <div key={cell.sandbox_id} className="card" style={{ padding: "0.7rem" }}>
              <div className="row" style={{ marginBottom: "0.3rem" }}>
                <span className="faint mono" style={{ fontSize: "0.7rem" }}>
                  {shortId(cell.sandbox_id)}
                </span>
                <span className="mono" style={{ fontSize: "0.75rem" }}>
                  {cell.model}
                </span>
                {cell.judge_overall != null ? (
                  <span
                    className={`score-value score-value--${scoreTone(cell.judge_overall)}`}
                    style={{ marginLeft: "auto" }}
                    title="judge overall (0–10)"
                  >
                    {cell.judge_overall}
                  </span>
                ) : (
                  <span className="faint" style={{ marginLeft: "auto", fontSize: "0.7rem" }}>
                    no score
                  </span>
                )}
              </div>
              <CellChip cell={cell} />
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

export default function SuiteRunClient({ id }: { id: string }) {
  const [data, setData] = useState<SuiteRunResults | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const d = await getSuiteRun(id);
        if (cancelled) return;
        setData(d);
        setError(null);
        // Poll while any run is still live.
        if (d.runs.some((r) => !["done", "failed", "cancelled"].includes(r.status))) {
          setTimeout(() => void load(), 5000);
        }
      } catch (err) {
        if (cancelled) return;
        setError(
          err instanceof ApiError
            ? err.status === 404
              ? "Suite run not found."
              : err.detail
            : "Failed to load suite run.",
        );
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [id]);

  const agg = data?.aggregate ?? null;
  const checkedRate = useMemo(
    () => (agg?.check_pass_rate != null ? Math.round(agg.check_pass_rate * 100) : null),
    [agg],
  );

  if (!data) {
    return (
      <main className="container container--narrow">
        {error ? (
          <>
            <ErrorNote title="Could not load suite run">{error}</ErrorNote>
            <p style={{ marginTop: "1rem" }}>
              <Link href="/suites" className="btn btn--sm">
                ← Back to suites
              </Link>
            </p>
          </>
        ) : (
          <Loading label="Loading suite run…" />
        )}
      </main>
    );
  }

  return (
    <main className="container">
      <div className="page-header">
        <div>
          <span className="kicker"><b>05</b> — suite batch</span>
          <div className="row" style={{ marginBottom: "0.35rem" }}>
            <h1 className="mono" style={{ fontSize: "1.15rem" }}>
              <Link href={`/suites/${data.suite_slug}`}>{data.suite_slug}</Link>
            </h1>
            {data.suite_version != null ? (
              <Badge tone="violet">v{data.suite_version}</Badge>
            ) : null}
            <Badge tone="blue">{data.runs.length} runs</Badge>
          </div>
          <p className="subtitle">
            batch <code>{shortId(data.id)}</code>
            {data.created_at ? ` · ${formatTs(data.created_at)}` : ""}
          </p>
        </div>
        <div className="row">
          <Link href={`/suites/${data.suite_slug}`} className="btn btn--sm btn--ghost">
            ← Suite
          </Link>
        </div>
      </div>

      {error ? <ErrorNote>{error}</ErrorNote> : null}

      {agg ? (
        <div className="kpi-strip" style={{ marginBottom: "1rem" }}>
          <div className="kpi">
            <div className="kpi__label">check pass rate</div>
            <div className="kpi__value">
              {checkedRate != null ? `${checkedRate}%` : "—"}
            </div>
            <div className="kpi__sub">
              {agg.checks_passed}/{agg.checks_total} deterministic checks
            </div>
          </div>
          <div className="kpi">
            <div className="kpi__label">mean judge overall</div>
            <div className="kpi__value">
              {agg.mean_judge_overall != null ? agg.mean_judge_overall : "—"}
            </div>
            <div className="kpi__sub">{agg.cells_scored} scored cells</div>
          </div>
          <div className="kpi">
            <div className="kpi__label">cells</div>
            <div className="kpi__value">{agg.cells}</div>
            <div className="kpi__sub">sandboxes across {agg.tasks} tasks</div>
          </div>
          <div className="kpi">
            <div className="kpi__label">checks</div>
            <div className="kpi__value">{agg.checks_total}</div>
            <div className="kpi__sub">
              ✓{agg.checks_passed} ✗{agg.checks_failed} ⊘{agg.checks_skipped} !
              {agg.checks_errors}
            </div>
          </div>
        </div>
      ) : null}

      <div className="stack">
        {data.runs.map((t) => (
          <TaskCard key={t.run_id} task={t} />
        ))}
        {data.runs.length === 0 ? (
          <EmptyState>This batch created no runs.</EmptyState>
        ) : null}
      </div>
    </main>
  );
}
