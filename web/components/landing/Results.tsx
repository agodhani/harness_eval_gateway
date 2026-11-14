import Reveal from "./Reveal";
import Section from "./Section";

const DIMENSIONS = [
  ["functional_requirements", 9.1, 5.2],
  ["acceptance_criteria", 8.6, 6.4],
  ["code_quality", 8.0, 6.8],
  ["cyclomatic_complexity", 7.9, 5.1],
] as const;

const SB = [
  { id: "sb·a", adapter: "miniloop · superpowers", total: 8.4, terminal: "agent_done", color: "lp-t-signal" },
  { id: "sb·b", adapter: "codex · spec-kit", total: 5.9, terminal: "stuck_loop", color: "lp-t-warn" },
] as const;

const PREVIEWS = [
  {
    type: "static",
    desc: "Sites and web apps served straight from the snapshot on localhost — one click from the results page.",
  },
  {
    type: "desktop",
    desc: "pygame and desktop apps executed in a hardened container on an isolated network, streamed interactively over noVNC.",
  },
  {
    type: "locked down",
    desc: "Zero egress for candidate code, CPU/mem/pids bounds, 15-minute TTL reaper, registry on docker labels — previews can't touch the control plane.",
  },
] as const;

export default function Results() {
  return (
    <Section
      id="results"
      index="06"
      kicker="results"
      title={
        <>
          A number per dimension,
          <br />
          an artifact you can run.
        </>
      }
      lede="Scores land in a matrix — rubric dimensions down one side, sandboxes
      across the top — with the judge's rationale attached. And because the
      artifact is a real workspace, you don't just read the score: you run
      what the agent built."
    >
      <div className="lp-cells lp-split-7-5">
        {/* score matrix */}
        <Reveal className="lp-cell">
          <div className="lp-cell__head">
            <span className="kicker">score matrix · run a58dd3de</span>
            <span
              className="kicker"
              style={{ letterSpacing: "0.06em", textTransform: "none" }}
            >
              rubric v1 · frozen
            </span>
          </div>
          <div className="lp-cell__pad">
            {/* header */}
            <div className="lp-matrix__cols" style={{ marginBottom: "1rem", alignItems: "end" }}>
              <span />
              {SB.map((s) => (
                <div key={s.id} className="lp-matrix__colhead">
                  <div className={`lp-matrix__colhead-id ${s.color}`}>{s.id}</div>
                  <div className="lp-matrix__colhead-adapter">{s.adapter}</div>
                </div>
              ))}
            </div>
            {DIMENSIONS.map(([dim, a, b], i) => (
              <div key={dim} className="lp-matrix__row">
                <span className="lp-matrix__dim">{dim}</span>
                {[a, b].map((v, j) => (
                  <div key={j} className="lp-matrix__val">
                    <span className="lp-matrix__num">{v.toFixed(1)}</span>
                    <div className="lp-matrix__bar">
                      <div
                        className="lp-score-fill"
                        style={
                          {
                            width: `${v * 10}%`,
                            background: j === 0 ? "var(--accent)" : "var(--amber)",
                            "--rd": `${i * 120 + j * 60}ms`,
                          } as React.CSSProperties
                        }
                      />
                    </div>
                  </div>
                ))}
              </div>
            ))}
            {/* totals */}
            <div className="lp-matrix__row lp-matrix__row--total">
              <span
                className="kicker"
                style={{ letterSpacing: "0.1em" }}
              >
                weighted total
              </span>
              {SB.map((s) => (
                <div key={s.id} className="lp-matrix__val">
                  <span className={`lp-matrix__total ${s.color}`}>
                    {s.total.toFixed(1)}
                  </span>
                  <span className="lp-t-faint" style={{ fontFamily: "var(--mono)", fontSize: "0.62rem" }}> /10</span>
                </div>
              ))}
            </div>
            <p className="lp-matrix__note">
              judge note — “sb·a shipped a playable 2048 with tests; sb·b
              looped on an unresolved type error.” evaluator model ≠ participant
              model is flagged at submission.
            </p>
          </div>
        </Reveal>

        {/* artifact previews */}
        <div className="lp-vcells">
          {PREVIEWS.map((p, i) => (
            <Reveal key={p.type} delay={i * 80} className="lp-side-note">
              <div className="row" style={{ gap: "0.75rem" }}>
                <span className="kicker">{p.type}</span>
                {i === 1 && (
                  <span
                    className="kicker"
                    style={{
                      border: "1px solid var(--border-strong)",
                      padding: "0.1rem 0.4rem",
                      fontSize: "0.56rem",
                    }}
                  >
                    noVNC
                  </span>
                )}
              </div>
              <p>{p.desc}</p>
            </Reveal>
          ))}
        </div>
      </div>
    </Section>
  );
}
