"use client";

/**
 * PR32 — /leaderboard: global rankings across every scored sandbox.
 *
 * Two boards from `GET /api/analytics/leaderboard` — by participant model
 * and by harness-config family (adapter + pack set) — each carrying mean
 * `overall` ± sample sd over n scored cells and the distinct run count.
 * Filters mirror the endpoint: preset (dropdown from GET /api/presets),
 * date range on runs.created_at, min trials per cell (PR27 runs.trials).
 * Applied filters sync to the URL so a view is shareable.
 */
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  getLeaderboard,
  listPresets,
  type Leaderboard,
  type TaskPreset,
} from "@/lib/api";
import LeaderboardBoards from "@/components/LeaderboardBoards";
import { EmptyState, ErrorNote, Loading } from "@/components/ui";

/** Date input → the inclusive UNTIL bound (end of that UTC day). The
 * server binds `runs.created_at <= until`; a bare `YYYY-MM-DD` parses to
 * midnight and would exclude the day itself. */
function untilOf(date: string): string | undefined {
  return date ? `${date}T23:59:59.999Z` : undefined;
}

export default function LeaderboardClient() {
  const router = useRouter();
  const params = useSearchParams();
  const urlPreset = params.get("preset") ?? "";
  const urlSince = params.get("since") ?? "";
  const urlUntil = params.get("until") ?? "";
  const urlMinTrials = params.get("min_trials") ?? "";

  // Filter form state seeds from the URL params (which drive the fetch);
  // Apply writes them back, and back/forward navigation re-syncs them via
  // the prev-key compare below so the form always shows applied filters.
  const [preset, setPreset] = useState(urlPreset);
  const [since, setSince] = useState(urlSince);
  const [until, setUntil] = useState(urlUntil);
  const [minTrials, setMinTrials] = useState(urlMinTrials);
  const urlKey = [urlPreset, urlSince, urlUntil, urlMinTrials].join("|");
  const [syncedKey, setSyncedKey] = useState(urlKey);
  if (syncedKey !== urlKey) {
    setSyncedKey(urlKey);
    setPreset(urlPreset);
    setSince(urlSince);
    setUntil(urlUntil);
    setMinTrials(urlMinTrials);
  }
  const [data, setData] = useState<Leaderboard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [presets, setPresets] = useState<TaskPreset[]>([]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await getLeaderboard({
        preset: urlPreset || undefined,
        since: urlSince || undefined,
        until: untilOf(urlUntil),
        minTrials: Number(urlMinTrials) > 1 ? Number(urlMinTrials) : undefined,
      });
      setData(d);
      setError(null);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.detail : "Failed to load leaderboard.",
      );
    } finally {
      setLoading(false);
    }
  }, [urlPreset, urlSince, urlUntil, urlMinTrials]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      await Promise.resolve();
      if (!cancelled) await load();
    })();
    return () => {
      cancelled = true;
    };
  }, [load]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      await Promise.resolve();
      try {
        const p = await listPresets();
        if (!cancelled) setPresets(p);
      } catch {
        /* preset dropdown just stays empty — freeform slugs still filter */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  function apply() {
    const sp = new URLSearchParams();
    if (preset) sp.set("preset", preset);
    if (since) sp.set("since", since);
    if (until) sp.set("until", until);
    if (Number(minTrials) > 1) sp.set("min_trials", String(Number(minTrials)));
    const q = sp.toString();
    router.replace(q ? `/leaderboard?${q}` : "/leaderboard", { scroll: false });
  }

  function reset() {
    setPreset("");
    setSince("");
    setUntil("");
    setMinTrials("");
    router.replace("/leaderboard", { scroll: false });
  }

  const filtered =
    Boolean(urlPreset) || Boolean(urlSince) || Boolean(urlUntil) || Boolean(urlMinTrials);

  return (
    <main className="container">
      <div className="page-header">
        <div>
          <span className="kicker"><b>06</b> — leaderboard</span>
          <h1 className="mono" style={{ fontSize: "1.15rem", marginBottom: "0.35rem" }}>
            Cross-run rankings
          </h1>
          <p className="subtitle">
            mean judge <code>overall</code> pooled over every scored sandbox
            cell in scope — by participant model and by harness-config
            family (adapter + pack set)
          </p>
        </div>
      </div>

      <div className="card" style={{ marginBottom: "1rem" }}>
        <div className="card__body">
          <div className="row" style={{ flexWrap: "wrap", gap: "0.6rem" }}>
            <label className="faint" style={{ fontSize: "0.75rem" }}>
              preset{" "}
              <select
                className="select"
                value={preset}
                onChange={(e) => setPreset(e.target.value)}
              >
                <option value="">all presets + freeform</option>
                {preset && !presets.some((p) => p.slug === preset) ? (
                  <option value={preset}>{preset}</option>
                ) : null}
                {presets.map((p) => (
                  <option key={p.slug} value={p.slug}>
                    {p.slug}
                  </option>
                ))}
              </select>
            </label>
            <label className="faint" style={{ fontSize: "0.75rem" }}>
              since{" "}
              <input
                type="date"
                className="input"
                value={since}
                onChange={(e) => setSince(e.target.value)}
              />
            </label>
            <label className="faint" style={{ fontSize: "0.75rem" }}>
              until{" "}
              <input
                type="date"
                className="input"
                value={until}
                onChange={(e) => setUntil(e.target.value)}
              />
            </label>
            <label className="faint" style={{ fontSize: "0.75rem" }}>
              min trials{" "}
              <input
                type="number"
                className="input"
                min={1}
                max={8}
                style={{ width: "4.5rem" }}
                value={minTrials}
                placeholder="1"
                onChange={(e) => setMinTrials(e.target.value)}
              />
            </label>
            <button type="button" className="btn btn--sm btn--primary" onClick={apply}>
              Apply
            </button>
            {filtered ? (
              <button type="button" className="btn btn--sm btn--ghost" onClick={reset}>
                Reset
              </button>
            ) : null}
          </div>
        </div>
      </div>

      {error ? <ErrorNote>{error}</ErrorNote> : null}

      {loading && !data ? (
        <Loading label="Ranking…" />
      ) : data ? (
        <>
          <div className="kpi-strip" style={{ marginBottom: "1rem" }}>
            <div className="kpi">
              <div className="kpi__label">runs</div>
              <div className="kpi__value">{data.runs_count}</div>
              <div className="kpi__sub">in scope{filtered ? " (filtered)" : ""}</div>
            </div>
            <div className="kpi">
              <div className="kpi__label">scored cells</div>
              <div className="kpi__value">
                {data.n_scored}
                <span className="faint" style={{ fontSize: "0.8rem" }}>
                  {" "}
                  / {data.sandboxes_count}
                </span>
              </div>
              <div className="kpi__sub">of sandboxes in scope</div>
            </div>
            <div className="kpi">
              <div className="kpi__label">models ranked</div>
              <div className="kpi__value">{data.by_model.length}</div>
              <div className="kpi__sub">with ≥1 scored cell</div>
            </div>
            <div className="kpi">
              <div className="kpi__label">families ranked</div>
              <div className="kpi__value">{data.by_config.length}</div>
              <div className="kpi__sub">adapter + pack set</div>
            </div>
          </div>
          <LeaderboardBoards data={data} />
          <p className="faint" style={{ marginTop: "0.8rem", fontSize: "0.75rem" }}>
            mean ± sample sd over the latest <code>overall</code> score of each
            cell — <b>n</b> scored of <b>sandboxes</b> total ·
            {data.min_trials > 1
              ? ` only runs submitted with trials ≥ ${data.min_trials} count ·`
              : ""}{" "}
            <a href="/compare">compare two runs side by side →</a>
          </p>
        </>
      ) : (
        <EmptyState>Nothing ranked yet — score a run first.</EmptyState>
      )}
    </main>
  );
}
