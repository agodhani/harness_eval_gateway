import Reveal from "./Reveal";
import Section from "./Section";
import { TYPE_COLOR, type EvType } from "@/lib/sim";

const SOURCES = ["gateway", "harness", "sandbox", "evaluator", "orchestrator"] as const;

const TYPES: EvType[] = [
  "run_started",
  "llm_request",
  "llm_response",
  "tool_call",
  "shell_command",
  "file_op",
  "skill_invocation",
  "test_execution",
  "checkpoint",
  "done_signal",
  "terminal_state",
  "error",
  "eval_score",
];

const LAYERS = [
  {
    name: "passive capture",
    where: "at the gateway",
    desc: "LLM requests and responses are recorded straight off the wire — tokens in/out, cache r/w, latency, cost, failures. The agent never knows it's measured.",
  },
  {
    name: "active translation",
    where: "at the adapter",
    desc: "Tool calls, shell commands, file ops — the adapter translates native harness output (stream-json, logs, hooks) into the same normalized event schema.",
  },
] as const;

export default function Telemetry() {
  return (
    <Section
      id="telemetry"
      index="05"
      kicker="telemetry"
      title={
        <>
          An append-only memory
          <br />
          of everything that happened.
        </>
      }
      lede="One events table. Five sources. Fourteen types. Every record is
      timestamped, sequenced and immutable — corrections are new events, never
      edits. The same stream powers the live timeline over SSE and the
      post-run replay."
    >
      <div className="lp-cells lp-split-7-5" style={{ border: "none", background: "transparent", gap: "3rem" }}>
        <div>
          {/* two-layer diagram */}
          <div className="lp-vcells" style={{ border: "1px solid var(--border)" }}>
            {LAYERS.map((l, i) => (
              <Reveal key={l.name} delay={i * 80} className="lp-layer">
                <div className="lp-layer__head">
                  <span className="lp-layer__n">layer {i + 1}</span>
                  <h3 className="lp-layer__name">{l.name}</h3>
                  <span className="lp-layer__where">{l.where}</span>
                </div>
                <p className="lp-layer__desc">{l.desc}</p>
              </Reveal>
            ))}
          </div>

          <Reveal delay={140}>
            <div className="lp-sources">
              <span className="kicker">sources</span>
              {SOURCES.map((s) => (
                <span key={s} className="lp-src">
                  {s}
                </span>
              ))}
            </div>
          </Reveal>
        </div>

        {/* event type matrix */}
        <Reveal delay={120}>
          <div className="lp-panel ticks" style={{ height: "100%" }}>
            <span className="tick-b" />
            <div className="lp-cell__head">
              <span className="kicker">event types</span>
              <span
                className="kicker"
                style={{ letterSpacing: "0.06em", textTransform: "none" }}
              >
                append-only · jsonb payload
              </span>
            </div>
            <div className="lp-evgrid">
              {TYPES.map((t) => (
                <div key={t}>
                  <span className={TYPE_COLOR[t]}>{t}</span>
                </div>
              ))}
              <div>
                <span className="lp-t-faint">context_compacted</span>
              </div>
            </div>
            <div className="lp-panel__foot">
              a contract test AST-scans every emitter — an event type can’t
              ship without schema coverage.
            </div>
          </div>
        </Reveal>
      </div>
    </Section>
  );
}
