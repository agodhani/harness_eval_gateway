"use client";

/**
 * PR22 — /presets/[slug]: preset detail.
 *
 * One GET /api/presets/{slug}/analytics call carries everything: the preset
 * itself, every run submitted from it, per-model + per-config score
 * leaderboards (mean `overall` pooled over each bucket's scored cells),
 * the pooled mean + n, and the score-over-time trend (recharts line).
 * User presets get edit/delete actions (PATCH bumps revision; delete is
 * confirmed); vendored presets are read-only by design.
 */
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  ApiError,
  deletePreset,
  getPresetAnalytics,
  type PresetAnalytics,
  type PresetLeaderboardEntry,
} from "@/lib/api";
import { formatTs, shortId } from "@/lib/format";
import { C, ChartFrame, ChartTip } from "@/app/runs/[id]/analysis/charts";
import { SavePresetDialog } from "@/components/SavePresetDialog";
import {
  Badge,
  Card,
  EmptyState,
  ErrorNote,
  InfoNote,
  Loading,
  statusTone,
} from "@/components/ui";

function scoreTone(score: number): "good" | "mid" | "bad" {
  if (score >= 8) return "good";
  if (score >= 5) return "mid";
  return "bad";
}

function Leaderboard({
  title,
  entries,
  kind,
}: {
  title: string;
  entries: PresetLeaderboardEntry[];
  kind: "model" | "config";
}) {
  return (
    <Card title={title}>
      {entries.length === 0 ? (
        <EmptyState>No scored cells yet.</EmptyState>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>{kind === "model" ? "Model" : "Config"}</th>
                {kind === "config" ? <th>Adapter</th> : null}
                <th>Mean overall</th>
                <th>Cells (n)</th>
                <th>Runs</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e) => (
                <tr key={e.key}>
                  <td className="mono">{e.label}</td>
                  {kind === "config" ? (
                    <td>
                      {e.adapter ? (
                        <Badge tone="outline">{e.adapter}</Badge>
                      ) : (
                        <span className="faint">—</span>
                      )}
                    </td>
                  ) : null}
                  <td className="num">
                    <span
                      className={`score-value score-value--${scoreTone(e.mean_overall_score)}`}
                    >
                      {e.mean_overall_score}
                    </span>
                  </td>
                  <td className="num">{e.n}</td>
                  <td className="num">{e.runs}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

export default function PresetDetailClient({ slug }: { slug: string }) {
  const router = useRouter();
  const [data, setData] = useState<PresetAnalytics | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [deleting, setDeleting] = useState(false);

  async function reload() {
    try {
      setData(await getPresetAnalytics(slug));
      setError(null);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.detail : "Failed to load preset analytics.",
      );
    }
  }

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      await Promise.resolve();
      try {
        const d = await getPresetAnalytics(slug);
        if (cancelled) return;
        setData(d);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(
          err instanceof ApiError
            ? err.status === 404
              ? `Preset "${slug}" not found — it may have been deleted.`
              : err.detail
            : "Failed to load preset.",
        );
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [slug]);

  const preset = data?.preset ?? null;

  async function onDelete() {
    if (!preset || deleting) return;
    if (
      !window.confirm(
        `Delete preset "${preset.slug}"? Runs keep their snapshot — only the library entry goes away.`,
      )
    ) {
      return;
    }
    setDeleting(true);
    try {
      await deletePreset(preset.slug);
      router.push("/presets");
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Delete failed.");
      setDeleting(false);
    }
  }

  if (!preset) {
    return (
      <main className="container container--narrow">
        {error ? (
          <>
            <ErrorNote title="Could not load preset">{error}</ErrorNote>
            <p style={{ marginTop: "1rem" }}>
              <Link href="/presets" className="btn btn--sm">
                ← Back to presets
              </Link>
            </p>
          </>
        ) : (
          <Loading label="Loading preset…" />
        )}
      </main>
    );
  }

  const trendRows = (data?.trend ?? []).map((t, i) => ({
    name: `#${i + 1}`,
    score: t.mean_overall_score,
    run: shortId(t.run_id),
    meta: t,
  }));
  const isUser = preset.source === "user";

  return (
    <main className="container">
      <div className="page-header">
        <div>
          <span className="kicker"><b>03</b> — preset detail</span>
          <div className="row" style={{ marginBottom: "0.35rem" }}>
            <h1 className="mono" style={{ fontSize: "1.15rem" }}>
              {preset.title}
            </h1>
            <Badge tone={isUser ? "blue" : "violet"}>{preset.source}</Badge>
            {preset.category ? <Badge tone="outline">{preset.category}</Badge> : null}
            {isUser && preset.revision > 1 ? (
              <Badge tone="gray">rev {preset.revision}</Badge>
            ) : null}
          </div>
          <p className="subtitle">
            <code>{preset.slug}</code>
            {preset.description ? ` — ${preset.description}` : ""}
            {preset.tags.length > 0 ? (
              <>
                {" "}
                ·{" "}
                {preset.tags.map((t) => (
                  <span key={t} className="chip" style={{ marginRight: "0.3rem" }}>
                    {t}
                  </span>
                ))}
              </>
            ) : null}
          </p>
        </div>
        <div className="row">
          <Link
            href={`/submit?preset=${encodeURIComponent(preset.slug)}`}
            className="btn btn--primary btn--sm"
          >
            + New run on this preset
          </Link>
          {isUser ? (
            <>
              <button type="button" className="btn btn--sm" onClick={() => setEditing(true)}>
                Edit
              </button>
              <button
                type="button"
                className="btn btn--sm btn--danger"
                onClick={() => void onDelete()}
                disabled={deleting}
              >
                {deleting ? "Deleting…" : "Delete"}
              </button>
            </>
          ) : null}
          <Link href="/presets" className="btn btn--sm btn--ghost">
            ← All presets
          </Link>
        </div>
      </div>

      {notice ? <InfoNote>{notice}</InfoNote> : null}
      {error ? <ErrorNote>{error}</ErrorNote> : null}

      <div className="kpi-strip" style={{ marginBottom: "1rem" }}>
        <div className="kpi">
          <div className="kpi__label">mean overall</div>
          <div className="kpi__value">
            {data?.mean_overall_score != null ? data.mean_overall_score : "—"}
          </div>
          <div className="kpi__sub">pooled over scored cells</div>
        </div>
        <div className="kpi">
          <div className="kpi__label">scored cells (n)</div>
          <div className="kpi__value">{data?.n_scored ?? 0}</div>
          <div className="kpi__sub">sandboxes with an overall score</div>
        </div>
        <div className="kpi">
          <div className="kpi__label">runs</div>
          <div className="kpi__value">{data?.runs.length ?? 0}</div>
          <div className="kpi__sub">submitted on this preset</div>
        </div>
        <div className="kpi">
          <div className="kpi__label">revision</div>
          <div className="kpi__value">{preset.revision}</div>
          <div className="kpi__sub">
            {isUser ? "bump on every edit" : "vendored — immutable"}
          </div>
        </div>
      </div>

      <div className="stack">
        <Card title="Task prompt">
          <p className="task-text">{preset.task_prompt}</p>
          {preset.suggested_adapter || preset.suggested_models.length ? (
            <dl className="kv" style={{ marginTop: "0.9rem" }}>
              {preset.suggested_adapter ? (
                <>
                  <dt>suggested adapter</dt>
                  <dd className="mono">{preset.suggested_adapter}</dd>
                </>
              ) : null}
              {preset.suggested_models.length ? (
                <>
                  <dt>suggested models</dt>
                  <dd className="mono">{preset.suggested_models.join(", ")}</dd>
                </>
              ) : null}
            </dl>
          ) : null}
        </Card>

        <Card title={`Score over time (${trendRows.length})`}>
          {trendRows.length === 0 ? (
            <EmptyState>No scored runs yet — the trend appears after the first evaluation.</EmptyState>
          ) : (
            <ChartFrame height={220}>
              <LineChart data={trendRows} margin={{ top: 6, right: 12, bottom: 4, left: 0 }}>
                <CartesianGrid stroke={C.grid} vertical={false} />
                <XAxis
                  dataKey="name"
                  tick={{ fill: C.text, fontSize: 11, fontFamily: "var(--mono)" }}
                  axisLine={{ stroke: C.axis }}
                  tickLine={false}
                />
                <YAxis
                  domain={[0, 10]}
                  tick={{ fill: C.text, fontSize: 11, fontFamily: "var(--mono)" }}
                  tickLine={false}
                  axisLine={false}
                  width={32}
                />
                <Tooltip
                  content={
                    <ChartTip
                      footer={(d) => {
                        const t = d?.meta as { run_id?: string; scored_sandboxes?: number } | undefined;
                        return t?.run_id ? (
                          <span>
                            run <code>{shortId(t.run_id)}</code> · {t.scored_sandboxes ?? 0} scored cell
                            {(t.scored_sandboxes ?? 0) === 1 ? "" : "s"}
                          </span>
                        ) : null;
                      }}
                    />
                  }
                  cursor={{ fill: "rgba(255, 255, 255, 0.045)" }}
                />
                <Line
                  type="monotone"
                  dataKey="score"
                  name="mean overall"
                  stroke={C.violet}
                  strokeWidth={1.5}
                  dot={{ r: 3, fill: C.violet, strokeWidth: 0 }}
                />
              </LineChart>
            </ChartFrame>
          )}
        </Card>

        <div className="split">
          <Leaderboard title="Leaderboard — by model" entries={data?.by_model ?? []} kind="model" />
          <Leaderboard title="Leaderboard — by config" entries={data?.by_config ?? []} kind="config" />
        </div>

        <Card title={`Runs on this preset (${data?.runs.length ?? 0})`}>
          {(data?.runs.length ?? 0) === 0 ? (
            <EmptyState>
              No runs yet —{" "}
              <Link href={`/submit?preset=${encodeURIComponent(preset.slug)}`}>
                start the first one
              </Link>
              .
            </EmptyState>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Run</th>
                    <th>Status</th>
                    <th>Mean overall</th>
                    <th>Scored</th>
                    <th>Sandboxes</th>
                    <th>Created</th>
                    <th>Finished</th>
                  </tr>
                </thead>
                <tbody>
                  {(data?.runs ?? []).map((r) => (
                    <tr key={r.run_id}>
                      <td className="num">
                        <Link href={`/runs/${r.run_id}`} title={r.run_id}>
                          {shortId(r.run_id)}
                        </Link>
                      </td>
                      <td>
                        <Badge tone={statusTone(r.status)}>{r.status}</Badge>
                      </td>
                      <td className="num">
                        {r.mean_overall_score != null ? (
                          <span className={`score-value score-value--${scoreTone(r.mean_overall_score)}`}>
                            {r.mean_overall_score}
                          </span>
                        ) : (
                          <span className="faint">—</span>
                        )}
                      </td>
                      <td className="num">{r.scored_sandboxes}</td>
                      <td className="num">{r.sandbox_count}</td>
                      <td className="num faint">{formatTs(r.created_at)}</td>
                      <td className="num faint">{formatTs(r.finished_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>

      {editing && isUser ? (
        <SavePresetDialog
          existing={preset}
          onClose={() => setEditing(false)}
          onSaved={() => {
            setEditing(false);
            setNotice("Preset saved — revision bumped.");
            void reload();
          }}
        />
      ) : null}
    </main>
  );
}
