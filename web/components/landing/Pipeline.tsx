import Reveal from "./Reveal";
import Section from "./Section";

const STEPS = [
  {
    state: "submitted",
    name: "Submit the spec",
    desc: "Task prompt, model, harness configs, skill packs, evaluator model — one POST /api/runs expands into the run matrix.",
  },
  {
    state: "rubric_review",
    name: "Rubric drafted, human-approved",
    desc: "An LLM drafts scoring dimensions from the task spec. You edit and approve in the UI — then it's frozen identically for every sandbox.",
  },
  {
    state: "provisioning",
    name: "Provision N sandboxes",
    desc: "Per-harness Docker images, bind-mounted skill packs, a done mechanism on PATH, and a clean workspace staged byte-for-byte identical.",
  },
  {
    state: "running",
    name: "Execute under observation",
    desc: "Agents loop. All model traffic routes through the gateway with per-sandbox credentials; adapters translate native output into normalized events.",
  },
  {
    state: "terminal",
    name: "Terminate & snapshot",
    desc: "Each sandbox exits on a defined terminal state. The workspace is snapshotted to the artifact store with its full transcript.",
  },
  {
    state: "evaluating",
    name: "Judge in a sandbox of its own",
    desc: "The evaluator — same infra, traffic through the gateway — scores every artifact per frozen rubric dimension, with rationale.",
  },
  {
    state: "done",
    name: "Results & replay",
    desc: "Score matrix, per-sandbox metrics and cost, timeline replay, downloadable artifacts — plus live previews of what each agent built.",
  },
] as const;

const TERMINAL = [
  ["agent_done", "agent invoked the injected done mechanism", "lp-t-signal"],
  ["harness_complete", "harness loop finished on its own", "lp-t-fg"],
  ["timeout", "60 min wall-clock cap", "lp-t-warn"],
  ["budget_exceeded", "token/cost budget — enforced at the gateway", "lp-t-warn"],
  ["stuck_loop", "3 consecutive identical tool calls", "lp-t-warn"],
  ["error", "crash / unrecoverable failure", "lp-t-err"],
] as const;

export default function Pipeline() {
  return (
    <Section
      id="pipeline"
      index="02"
      kicker="run lifecycle"
      title={
        <>
          Submit to scores,
          <br />
          as a state machine.
        </>
      }
      lede="A run is a strict lifecycle — submitted → rubric_review → [queued →] provisioning →
      running → evaluating → done. Every transition is an event; every event is
      persisted. Nothing about a run is ephemeral."
    >
      <div className="lp-cells lp-split-7-5" style={{ border: "none", background: "transparent", gap: "3rem" }}>
        {/* steps */}
        <ol className="lp-steps">
          {STEPS.map((s, i) => (
            <Reveal as="li" key={s.state} delay={i * 40}>
              <div className="lp-step">
                <span className="lp-step__n">
                  {String(i + 1).padStart(2, "0")}
                </span>
                <div>
                  <div className="lp-step__head">
                    <h3 className="lp-step__name">{s.name}</h3>
                    <span className="lp-step__state">{s.state}</span>
                  </div>
                  <p className="lp-step__desc">{s.desc}</p>
                </div>
              </div>
            </Reveal>
          ))}
        </ol>

        {/* termination policy */}
        <div>
          <Reveal delay={120} className="lp-sticky">
            <div className="lp-panel ticks">
              <span className="tick-b" />
              <div className="lp-cell__head">
                <span className="kicker">termination policy</span>
              </div>
              <ul className="lp-term">
                {TERMINAL.map(([state, desc, color]) => (
                  <li key={state}>
                    <span className={`lp-term__state ${color}`}>{state}</span>
                    <span className="lp-term__desc">{desc}</span>
                  </li>
                ))}
              </ul>
              <div className="lp-panel__foot">
                caps — 60min wall clock · $2 spend · 3 identical tool calls.
                <br />
                every run ends in a defined state. no zombie sandboxes.
              </div>
            </div>
          </Reveal>
        </div>
      </div>
    </Section>
  );
}
