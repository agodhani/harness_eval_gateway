import Link from "next/link";

export default function Home() {
  return (
    <main className="container container--narrow landing">
      <div className="landing__kicker-row">
        <span className="kicker">comparative agent-harness evaluation</span>
        <span className="hline" aria-hidden />
        <span className="kicker" style={{ color: "var(--accent)" }}>
          local · single-user · oss
        </span>
      </div>

      <h1>
        harness_eval_gateway — benchmark the <span className="sig">harness</span>,
        not the model.
      </h1>

      <p className="landing__lede">
        One task, one model, N harness configurations in locked-down Docker
        sandboxes. A custom AI gateway meters every token, a frozen rubric
        scores every artifact, and an append-only event stream remembers
        everything.
      </p>

      <div className="landing__ctas">
        <Link href="/submit" className="btn btn--primary">
          Submit a run
        </Link>
        <Link href="/runs" className="btn">
          Browse runs
        </Link>
      </div>

      <dl className="landing__spec">
        <div className="landing__spec-row">
          <dt>lifecycle</dt>
          <dd>submitted → rubric_review → provisioning → running → evaluating → done</dd>
        </div>
        <div className="landing__spec-row">
          <dt>adapters</dt>
          <dd>miniloop · codex · claude_code · pi · deepseek</dd>
        </div>
        <div className="landing__spec-row">
          <dt>gateway</dt>
          <dd>/v1/chat/completions · /v1/messages · /v1/responses · per-run creds</dd>
        </div>
        <div className="landing__spec-row">
          <dt>control api</dt>
          <dd>http://localhost:8001 · events stream over SSE</dd>
        </div>
      </dl>
    </main>
  );
}
