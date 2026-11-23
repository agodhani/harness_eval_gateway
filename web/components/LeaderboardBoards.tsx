/**
 * PR32 — shared leaderboard tables (/leaderboard page and the preset-
 * filtered embed on /presets/[slug]).
 *
 * Renders `GET /api/analytics/leaderboard` entries: rank, label (+ shared
 * config name on the family board), adapter badge, mean `overall` with
 * the sample-sd spread, scored cells n (of `sandboxes` total — coverage
 * stays honest), and distinct contributing runs. Pure presentational —
 * the caller owns fetching + filter state.
 */
import type { Leaderboard, LeaderboardEntry } from "@/lib/api";
import { Badge, Card, EmptyState } from "@/components/ui";

function scoreTone(score: number): "good" | "mid" | "bad" {
  if (score >= 8) return "good";
  if (score >= 5) return "mid";
  return "bad";
}

/** `9.0` → `9`, `6.67` → `6.67` — trims a trailing `.0` only. */
function fmtScore(v: number): string {
  return v.toFixed(2).replace(/\.?0+$/, "");
}

export function BoardTable({
  title,
  entries,
  kind,
}: {
  title: string;
  entries: LeaderboardEntry[];
  kind: "model" | "config";
}) {
  return (
    <Card title={title}>
      {entries.length === 0 ? (
        <EmptyState>No scored cells in scope.</EmptyState>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th style={{ width: "2rem" }}>#</th>
                <th>{kind === "model" ? "Model" : "Config family"}</th>
                {kind === "config" ? <th>Adapter</th> : null}
                <th>Mean overall</th>
                <th>Cells</th>
                <th>Runs</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e, i) => (
                <tr key={e.key}>
                  <td className="num faint">{i + 1}</td>
                  <td className="mono">
                    {e.name ? (
                      <>
                        <span title={`family key: ${e.key}`}>{e.name}</span>
                        <div
                          className="faint"
                          style={{ fontSize: "0.7rem" }}
                          title={e.key}
                        >
                          {e.label}
                        </div>
                      </>
                    ) : (
                      <span title={e.key}>{e.label}</span>
                    )}
                  </td>
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
                      title={
                        e.sd_overall_score != null
                          ? `mean ${e.mean_overall_score} ± ${e.sd_overall_score} (sample sd, n=${e.n})`
                          : `mean over ${e.n} scored cell${e.n === 1 ? "" : "s"}`
                      }
                    >
                      {fmtScore(e.mean_overall_score)}
                    </span>
                    {e.sd_overall_score != null ? (
                      <span className="faint" style={{ fontSize: "0.72rem" }}>
                        {" "}
                        ±{fmtScore(e.sd_overall_score)}
                      </span>
                    ) : null}
                  </td>
                  <td className="num">
                    <span title={`${e.n} scored of ${e.sandboxes} cells in scope`}>
                      {e.n}
                      {e.sandboxes > e.n ? (
                        <span className="faint" style={{ fontSize: "0.72rem" }}>
                          /{e.sandboxes}
                        </span>
                      ) : null}
                    </span>
                  </td>
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

export default function LeaderboardBoards({ data }: { data: Leaderboard }) {
  return (
    <div className="split">
      <BoardTable
        title="Leaderboard — by model"
        entries={data.by_model}
        kind="model"
      />
      <BoardTable
        title="Leaderboard — by config family"
        entries={data.by_config}
        kind="config"
      />
    </div>
  );
}
