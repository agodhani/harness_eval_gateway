import Reveal from "./Reveal";
import Section from "./Section";

const CONSTANTS = [
  ["task_prompt", "one prompt, verbatim, to every sandbox"],
  ["workspace", "identical clean repo staged per sandbox"],
  ["rubric", "drafted by LLM, edited by you, frozen pre-launch"],
  ["egress", "one route out — the gateway. No telemetry holes"],
] as const;

// The measured axes — each matrix cell fixes one combination of these.
const AXES = [
  [
    "harness config",
    "adapter + skill packs + system prompt — the scaffolding under test",
  ],
  [
    "model",
    "the participant model set — each config runs on every selected model",
  ],
] as const;

export default function Method() {
  return (
    <Section
      id="method"
      index="01"
      kicker="the method"
      title={
        <>
          A controlled experiment,
          <br />
          not a vibe check.
        </>
      }
      lede="Agent CLIs ship different scaffolding — tool loops, system prompts, skill
      injection, compaction strategy — and models differ in capability. A run
      is a config × model matrix over one task: every other variable is held
      constant, so the deltas split cleanly between the two axes."
    >
      <div className="lp-cells lp-split-7-5">
        {/* variables table */}
        <Reveal className="lp-cell">
          <div className="lp-cell__head">
            <span className="kicker">experiment spec</span>
            <span
              className="kicker"
              style={{ letterSpacing: "0.06em", textTransform: "none" }}
            >
              n = configs × models
            </span>
          </div>
          <ul className="lp-varlist">
            {CONSTANTS.map(([k, v], i) => (
              <li key={k} className="lp-var">
                <span className="lp-var__n">
                  {String(i + 1).padStart(2, "0")}
                </span>
                <span className="lp-var__k">{k}</span>
                <span className="lp-var__v">{v}</span>
                <span className="lp-var__tag">held constant</span>
              </li>
            ))}
            {AXES.map(([k, v], i) => (
              <li key={k} className="lp-var lp-var--measured">
                <span className="lp-var__n">
                  {String(CONSTANTS.length + i + 1).padStart(2, "0")}
                </span>
                <span className="lp-var__k">{k}</span>
                <span className="lp-var__v">{v}</span>
                <span className="lp-var__tag">measured</span>
              </li>
            ))}
          </ul>
        </Reveal>

        {/* side column */}
        <div className="lp-vcells">
          <Reveal delay={90} className="lp-side-note">
            <span className="kicker">what a run proves</span>
            <p>
              When cell <span className="mono-inline">a</span> scores 8.4 and
              cell <span className="mono-inline">b</span> scores 5.9 on the
              same rubric, the matrix isolates the difference — one axis
              changed, the rest held. The delta is attributable to the config
              or the model.
            </p>
          </Reveal>
          <Reveal delay={160} className="lp-side-note">
            <span className="kicker">what it costs</span>
            <p>
              Ollama runs locally, so an eval costs electricity, not API budget.
              Every call is still metered uniformly — switch to OpenRouter or a
              direct provider later and the accounting is already there.
            </p>
          </Reveal>
        </div>
      </div>
    </Section>
  );
}
