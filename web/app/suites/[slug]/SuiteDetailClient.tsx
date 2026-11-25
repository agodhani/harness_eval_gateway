"use client";

/**
 * PR26 — /suites/[slug]: suite detail.
 *
 * GET /api/suites/{slug} — the task list (preset_ref or inline prompt,
 * adapter pin, declared checks) plus the most recent fan-out batches.
 * A minimal launcher posts POST /api/suites/{slug}/run with a model name
 * + adapter selection (each produced config is `{adapter}`-only —
 * the full submit form stays the place for skill packs / budgets).
 */
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  ApiError,
  getSuite,
  listHarnesses,
  runSuite,
  type SuiteCheck,
  type SuiteDetail,
} from "@/lib/api";
import { formatTs, shortId, truncate } from "@/lib/format";
import {
  Badge,
  Card,
  EmptyState,
  ErrorNote,
  InfoNote,
  Loading,
} from "@/components/ui";

function checkLabel(c: SuiteCheck): string {
  const spec = c.spec ?? {};
  const brief = (v: unknown, n = 48) => truncate(String(v ?? ""), n);
  switch (c.check) {
    case "file_exists":
      return `file exists: ${brief(spec.path)}`;
    case "grep":
      return `${brief(spec.path)} ~ /${brief(spec.pattern)}/`;
    case "py_test":
      return `pytest: ${brief(spec.path)}`;
    case "output_grep":
      return `output ~ /${brief(spec.pattern)}/`;
    case "exact":
      return `output == ${brief(spec.equals)}`;
    case "contains":
      return `output contains ${brief(spec.substring)}`;
    case "json_schema":
      return "output validates JSON schema";
    case "numeric_tol":
      return `first number ≈ ${spec.value} ±${spec.tolerance ?? 0}`;
    default:
      return c.check;
  }
}

function RunLauncher({
  slug,
  pinnedAdapters,
}: {
  slug: string;
  pinnedAdapters: string[];
}) {
  const router = useRouter();
  const [model, setModel] = useState("");
  const [adapters, setAdapters] = useState<string[]>([]);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      await Promise.resolve();
      try {
        const hs = await listHarnesses();
        if (cancelled) return;
        const names = hs.map((h) => h.name).filter(Boolean);
        setAdapters(names);
        if (names.length && picked.size === 0) {
          const pinned = pinnedAdapters.filter((n) => names.includes(n));
          setPicked(new Set(pinned.length ? pinned : [names[0]]));
        }
      } catch {
        /* harness list down — the picker stays empty, launcher explains */
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const canSubmit = model.trim().length > 0 && picked.size > 0 && !submitting;

  async function onRun() {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const out = await runSuite(slug, {
        model_name: model.trim(),
        harness_configs: [...picked].map((a) => ({
          adapter: a,
          skill_packs: [],
        })),
      });
      router.push(`/suite-runs/${out.id}`);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.detail : "Suite launch failed.",
      );
      setSubmitting(false);
    }
  }

  return (
    <Card title="Run this suite">
      <p className="faint" style={{ marginTop: 0 }}>
        Fans out one run per suite task on the model below. Adapter-pinned
        tasks only land on matching configs — a pin with no matching
        selection 422s the batch (nothing starts).
      </p>
      <div className="row" style={{ flexWrap: "wrap", gap: "0.75rem" }}>
        <input
          className="input"
          style={{ width: "280px" }}
          type="text"
          placeholder="Participant model — e.g. qwen2.5-coder:3b"
          aria-label="Participant model"
          value={model}
          onChange={(e) => setModel(e.target.value)}
        />
        <div className="chip-row" role="group" aria-label="Adapters">
          {adapters.length === 0 ? (
            <span className="faint" style={{ fontSize: "0.8rem" }}>
              no adapters listed — is the API up?
            </span>
          ) : (
            adapters.map((a) => {
              const on = picked.has(a);
              return (
                <button
                  key={a}
                  type="button"
                  className={`filter-chip${on ? " is-active" : ""}`}
                  title={`${on ? "remove" : "add"} config {adapter: ${a}}`}
                  onClick={() =>
                    setPicked((prev) => {
                      const next = new Set(prev);
                      if (next.has(a)) next.delete(a);
                      else next.add(a);
                      return next;
                    })
                  }
                >
                  {a}
                </button>
              );
            })
          )}
        </div>
        <button
          type="button"
          className="btn btn--primary btn--sm"
          disabled={!canSubmit}
          onClick={() => void onRun()}
          title="POST /api/suites/{slug}/run — one run per task"
        >
          {submitting ? "Launching…" : "▶ Run suite"}
        </button>
      </div>
      {error ? <ErrorNote>{error}</ErrorNote> : null}
    </Card>
  );
}

export default function SuiteDetailClient({ slug }: { slug: string }) {
  const [suite, setSuite] = useState<SuiteDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      await Promise.resolve();
      try {
        const d = await getSuite(slug);
        if (cancelled) return;
        setSuite(d);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(
          err instanceof ApiError
            ? err.status === 404
              ? `Suite "${slug}" not found.`
              : err.detail
            : "Failed to load suite.",
        );
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [slug]);

  if (!suite) {
    return (
      <main className="container container--narrow">
        {error ? (
          <>
            <ErrorNote title="Could not load suite">{error}</ErrorNote>
            <p style={{ marginTop: "1rem" }}>
              <Link href="/suites" className="btn btn--sm">
                ← Back to suites
              </Link>
            </p>
          </>
        ) : (
          <Loading label="Loading suite…" />
        )}
      </main>
    );
  }

  return (
    <main className="container">
      <div className="page-header">
        <div>
          <span className="kicker"><b>05</b> — suite detail</span>
          <div className="row" style={{ marginBottom: "0.35rem" }}>
            <h1 className="mono" style={{ fontSize: "1.15rem" }}>
              {suite.title}
            </h1>
            <Badge tone="violet">v{suite.version}</Badge>
            <Badge tone="teal">{suite.task_count} tasks</Badge>
            <Badge tone="gray">{suite.checks_count} checks</Badge>
          </div>
          <p className="subtitle">
            <code>{suite.slug}</code>
            {suite.description ? ` — ${suite.description}` : ""}
          </p>
        </div>
        <div className="row">
          <Link href="/suites" className="btn btn--sm btn--ghost">
            ← All suites
          </Link>
        </div>
      </div>

      {error ? <ErrorNote>{error}</ErrorNote> : null}

      <div className="stack">
        <RunLauncher
          slug={suite.slug}
          pinnedAdapters={[
            ...new Set(
              suite.tasks.flatMap((t) => (t.adapter ? [t.adapter] : [])),
            ),
          ]}
        />

        <Card title={`Tasks (${suite.tasks.length})`}>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th style={{ width: "2rem" }}>#</th>
                  <th>Task</th>
                  <th>Source</th>
                  <th>Adapter</th>
                  <th>Checks</th>
                </tr>
              </thead>
              <tbody>
                {suite.tasks.map((t) => (
                  <tr key={t.index}>
                    <td className="num faint">{t.index}</td>
                    <td style={{ maxWidth: "360px" }}>
                      <span className="mono">{t.id}</span>
                      {t.prompt ? (
                        <div className="faint" style={{ fontSize: "0.72rem" }}>
                          {truncate(t.prompt, 100)}
                        </div>
                      ) : null}
                    </td>
                    <td>
                      {t.preset_ref ? (
                        <Link href={`/presets/${t.preset_ref}`} className="mono">
                          {t.preset_ref}
                        </Link>
                      ) : (
                        <Badge tone="outline">inline</Badge>
                      )}
                    </td>
                    <td>
                      {t.adapter ? (
                        <Badge
                          tone="amber"
                          title={`Task only runs on configs with adapter ${t.adapter}`}
                        >
                          {t.adapter}
                        </Badge>
                      ) : (
                        <span className="faint">any</span>
                      )}
                    </td>
                    <td style={{ maxWidth: "320px" }}>
                      {t.checks.length === 0 ? (
                        <span className="faint">—</span>
                      ) : (
                        <div className="chip-row">
                          {t.checks.map((c, i) => (
                            <span
                              key={i}
                              className="chip"
                              title={`${c.check}: ${JSON.stringify(c.spec)}`}
                            >
                              {checkLabel(c)}
                            </span>
                          ))}
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>

        <Card title={`Recent batches (${suite.recent_runs.length})`}>
          {suite.recent_runs.length === 0 ? (
            <EmptyState>
              No batches yet — run this suite to create the first.
            </EmptyState>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Batch</th>
                    <th>Suite version</th>
                    <th>Runs</th>
                    <th>Launched</th>
                  </tr>
                </thead>
                <tbody>
                  {suite.recent_runs.map((b) => (
                    <tr key={b.id}>
                      <td className="num">
                        <Link href={`/suite-runs/${b.id}`} title={b.id}>
                          {shortId(b.id)}
                        </Link>
                      </td>
                      <td className="num">
                        {b.suite_version != null ? `v${b.suite_version}` : "—"}
                      </td>
                      <td className="num">{b.run_count}</td>
                      <td className="num faint">{formatTs(b.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <InfoNote>
          Deterministic checks run post-artifact inside the eval pass — their
          pass/fail lands as <code>checker_results</code> plus a{" "}
          <code>suite_pass</code> score beside judge scores, never folded into
          them.
        </InfoNote>
      </div>
    </main>
  );
}
