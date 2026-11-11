export default function Home() {
  return (
    <main
      style={{
        minHeight: "100vh",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: "1rem",
        fontFamily: "monospace",
      }}
    >
      <h1>harness_eval_gateway — MVP scaffold</h1>
      <p>Comparative evaluation of AI coding-agent harnesses via a custom AI gateway.</p>
      <nav style={{ display: "flex", gap: "1.5rem" }}>
        {/* F1: submission form */}
        <a href="/submit">Submit a run</a>
        {/* F6/F8: run list, timeline, results */}
        <a href="/runs">Runs</a>
      </nav>
      <p style={{ opacity: 0.6 }}>
        API: <code>http://localhost:8000/health</code>
      </p>
    </main>
  );
}
