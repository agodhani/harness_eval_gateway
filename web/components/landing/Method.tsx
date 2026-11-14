import Reveal from "./Reveal";
import Section from "./Section";

const CONSTANTS = [
  ["task_prompt", "one prompt, verbatim, to every sandbox"],
  ["model", "same checkpoint — harness capability is what's measured"],
  ["workspace", "identical clean repo staged per sandbox"],
  ["rubric", "drafted by LLM, edited by you, frozen pre-launch"],
  ["egress", "one route out — the gateway. No telemetry holes"],
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
      injection, compaction strategy. Comparing them on different models or
      different tasks tells you nothing. This platform holds every variable
      constant except the one you're measuring."
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
              n = 2 sandboxes
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
            <li className="lp-var lp-var--measured">
              <span className="lp-var__n">06</span>
              <span className="lp-var__k">harness</span>
              <span className="lp-var__v">
                adapter + skill packs + system prompt — the independent variable
              </span>
              <span className="lp-var__tag">measured</span>
            </li>
          </ul>
        </Reveal>

        {/* side column */}
        <div className="lp-vcells">
          <Reveal delay={90} className="lp-side-note">
            <span className="kicker">what a run proves</span>
            <p>
              When sandbox <span className="mono-inline">a</span> scores 8.4 and
              sandbox <span className="mono-inline">b</span> scores 5.9 on the
              same rubric, the model didn’t change — the harness did. The delta
              is attributable.
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
