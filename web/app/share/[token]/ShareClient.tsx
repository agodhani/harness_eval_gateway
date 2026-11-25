"use client";

/**
 * PR35 (F28) — the read-only share view behind `/share/{token}`.
 *
 * Renders the `GET /api/share/{token}` bundle: the run's spec header, the
 * score matrix (same rows/columns idiom as the results page — per-sandbox
 * columns, one row per rubric dimension plus the judge `overall`), and an
 * events-lite timeline reusing the timeline's {@link EventRow} renderer
 * (payloads arrive pre-projected to scalar fields — expanding a row shows
 * that bounded projection, never a transcript).
 *
 * No auth controls, no mutations — this is the surface a minted link
 * exposes; everything interactive on the results/timeline pages is
 * intentionally absent (PR19 will gate the control-plane side).
 */
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ApiError,
  dimensionOrder,
  getShareBundle,
  harnessConfigLabel,
  type SandboxResult,
  type ShareBundle,
} from "@/lib/api";
import { formatTs, shortId } from "@/lib/format";
import {
  Badge,
  Card,
  EmptyState,
  ErrorNote,
  InfoNote,
  Loading,
  statusTone,
} from "@/components/ui";
import { EventRow } from "@/app/runs/[id]/timeline/EventRow";

/** Max event rows rendered — the bundle can carry up to the server cap;
 * a static share page stays light instead of mounting tens of thousands
 * of rows (the truncation note reports the true total). */
const EVENT_RENDER_CAP = 500;

function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return fallback;
}

function scoreTone(score: number): "good" | "mid" | "bad" {
  if (score >= 8) return "good";
  if (score >= 5) return "mid";
  return "bad";
}

export default function ShareClient({ token }: { token: string }) {
  const [bundle, setBundle] = useState<ShareBundle | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      await Promise.resolve(); // async context → no setState-in-effect
      if (cancelled) return;
      try {
        const b = await getShareBundle(token);
        if (!cancelled) setBundle(b);
      } catch (e) {
        if (!cancelled) {
          setError(
            e instanceof ApiError && e.status === 404
              ? "This share link is invalid or has been revoked."
              : errMsg(e, "Could not load the shared run."),
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token]);

  const sandboxes = useMemo(() => bundle?.results.sandboxes ?? [], [bundle]);
  const nameBySandbox = useMemo(() => {
    const m = new Map<string, string>();
    for (const sb of sandboxes) {
      m.set(sb.sandbox_id, sb.name || shortId(sb.sandbox_id));
    }
    return m;
  }, [sandboxes]);

  const dimensionKeys = useMemo(() => {
    const set = new Set<string>();
    for (const sb of sandboxes) {
      for (const d of Object.keys(sb.dimensions ?? {})) set.add(d);
    }
    return dimensionOrder([...set]);
  }, [sandboxes]);

  const events = bundle?.events ?? [];
  const shownEvents = events.slice(0, EVENT_RENDER_CAP);
  const hiddenCount = Math.max(events.length - shownEvents.length, 0);

  const toggle = useCallback((id: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  if (loading) {
    return (
      <main className="container">
        <Loading label="Loading shared run…" />
      </main>
    );
  }

  if (error || !bundle) {
    return (
      <main className="container container--narrow">
        <ErrorNote title="Could not load shared run">
          {error ?? "Share bundle unavailable."}
        </ErrorNote>
        <p style={{ marginTop: "1rem" }}>
          <Link href="/" className="btn btn--sm">
            ← home
          </Link>
        </p>
      </main>
    );
  }

  const run = bundle.run;
  const participantModels = run.models?.length ? run.models : [run.model_name];

  return (
    <main className="container">
      <div className="page-header">
        <div>
          <span className="kicker">
            <b>share</b> — read-only view · score matrix · event index
          </span>
          <div className="row" style={{ marginBottom: "0.3rem" }}>
            <h1>Shared run</h1>
            <span className="mono faint" title={run.id}>
              {shortId(run.id, 13)}…
            </span>
            {run.status ? (
              <Badge tone={statusTone(run.status)}>{run.status}</Badge>
            ) : null}
          </div>
          <p className="subtitle">
            {run.task_prompt.slice(0, 120)}
            {run.task_prompt.length > 120 ? "…" : ""}
            {" · "}
            <code>{participantModels.join(", ")}</code>
            {run.evaluator_model ? (
              <>
                {" "}
                · judged by <code>{run.evaluator_model}</code>
              </>
            ) : null}
            {run.finished_at ? <> · {formatTs(run.finished_at)}</> : null}
          </p>
        </div>
      </div>

      <div className="stack">
        <InfoNote>
          Read-only share view — the score matrix and the events-lite index
          for this run, exactly as captured. Artifact files and the control
          API are not part of the share surface.
        </InfoNote>

        {sandboxes.length === 0 ? (
          <Card>
            <EmptyState>
              No sandboxes in this run — nothing was scored.
            </EmptyState>
          </Card>
        ) : (
          <Card title="Score matrix" className="card--flush">
            <div className="matrix-wrap">
              <table className="matrix">
                <thead>
                  <tr>
                    <th style={{ minWidth: "200px" }}>Dimension</th>
                    {sandboxes.map((sb) => (
                      <th key={sb.sandbox_id}>
                        <div className="row" style={{ gap: "0.4rem" }}>
                          <span className="mono" title={sb.sandbox_id}>
                            {shortId(sb.sandbox_id)}
                          </span>
                          <Badge tone={statusTone(sb.status)}>{sb.status}</Badge>
                        </div>
                        {sb.name ? (
                          <div className="dim-name" title={sb.name}>
                            {sb.name}
                          </div>
                        ) : null}
                        <div className="dim-desc">
                          {harnessConfigLabel(sb.harness_config)}
                        </div>
                        {sb.terminal_reason ? (
                          <div className="dim-desc">
                            <Badge tone={statusTone(sb.terminal_reason)}>
                              {sb.terminal_reason}
                            </Badge>
                          </div>
                        ) : null}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  <tr className="row-overall">
                    <td>
                      <div className="dim-name">overall</div>
                      <div className="dim-desc">judge-reported aggregate</div>
                    </td>
                    {sandboxes.map((sb) => (
                      <td key={sb.sandbox_id} className="score-cell">
                        <ShareScoreCell sb={sb} dim="overall" />
                      </td>
                    ))}
                  </tr>
                  {dimensionKeys.map((dim) => (
                    <tr key={dim}>
                      <td>
                        <div className="dim-name">{dim}</div>
                      </td>
                      {sandboxes.map((sb) => (
                        <td key={sb.sandbox_id} className="score-cell">
                          <ShareScoreCell sb={sb} dim={dim} />
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        )}

        <Card
          title={`Events · ${bundle.events_total}`}
          className="card--flush"
        >
          {events.length === 0 ? (
            <EmptyState>No events recorded for this run.</EmptyState>
          ) : (
            <>
              <div className="timeline-scroll">
                {shownEvents.map((ev) => (
                  <EventRow
                    key={ev.id}
                    ev={ev}
                    expanded={expanded.has(ev.id)}
                    onToggle={toggle}
                    sandboxName={nameBySandbox.get(ev.sandbox_id ?? "")}
                  />
                ))}
              </div>
              {hiddenCount > 0 ? (
                <p className="chart-legend">
                  +{hiddenCount} more events not rendered (share view is
                  capped at {EVENT_RENDER_CAP} rows).
                </p>
              ) : null}
              {bundle.events_truncated ? (
                <p className="chart-legend">
                  The bundle itself was truncated — {bundle.events_total}{" "}
                  events exist; the earliest {events.length} are in the
                  bundle.
                </p>
              ) : null}
            </>
          )}
        </Card>
      </div>
    </main>
  );
}

/** One matrix cell — the score (colored like the results matrix) plus the
 * judge's rationale inline (a share view earns the full text; expanding
 * it is unnecessary on a static page). */
function ShareScoreCell({ sb, dim }: { sb: SandboxResult; dim: string }) {
  const entry = dim === "overall" ? sb.overall : sb.dimensions?.[dim];
  if (!entry) return <span className="score-na">—</span>;
  return (
    <div>
      <span className={`score-value score-value--${scoreTone(entry.score)}`}>
        {entry.score}
      </span>
      {entry.rationale ? (
        <div className="rationale">{entry.rationale}</div>
      ) : null}
    </div>
  );
}
