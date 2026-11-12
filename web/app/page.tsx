import Link from "next/link";

export default function Home() {
  return (
    <main className="container container--narrow" style={{ paddingTop: "3rem" }}>
      <div className="card">
        <div className="card__body" style={{ textAlign: "center", padding: "2.5rem 2rem" }}>
          <h1 style={{ fontSize: "1.6rem", marginBottom: "0.5rem" }}>
            harness_eval_gateway — MVP scaffold
          </h1>
          <p className="muted" style={{ maxWidth: "34rem", margin: "0 auto 1.5rem" }}>
            Comparative evaluation of AI coding-agent harnesses. Submit a task,
            approve the rubric, and watch sandboxes race on the same model while
            a custom AI gateway captures every token.
          </p>
          <div className="row" style={{ justifyContent: "center" }}>
            <Link href="/submit" className="btn btn--primary">
              Submit a run
            </Link>
            <Link href="/runs" className="btn">
              Browse runs
            </Link>
          </div>
          <p className="faint" style={{ marginTop: "1.75rem", fontSize: "0.8rem" }}>
            Control API: <code>http://localhost:8001</code> · events stream over SSE
          </p>
        </div>
      </div>
    </main>
  );
}
