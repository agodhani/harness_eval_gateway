import Reveal from "./Reveal";
import Section from "./Section";
import { ArrowUpRight, GithubMark } from "./icons";

const CMDS: [string, string][] = [
  ["git clone https://github.com/agodhani/harness_eval_gateway && cd harness_eval_gateway", ""],
  ["ollama pull qwen2.5-coder:3b", "# start Ollama first"],
  ["docker compose up -d --wait postgres", "# postgres :5433"],
  ["docker compose run --rm --build api alembic upgrade head", "# schema"],
  ["docker build -f sandbox/images/miniloop.Dockerfile -t heg-sandbox:latest .", ""],
  ["docker build -f sandbox/images/exec.Dockerfile -t heg-exec:latest .", ""],
  ["docker compose up -d --wait", "# api :8001 · gateway :8081"],
  ["cd web && npm ci && npm run dev", "# ui → localhost:3000"],
];

const PREREQS = ["docker + compose", "node 22.18+", "ollama · qwen2.5-coder:3b"];

export default function Quickstart() {
  return (
    <Section
      id="start"
      index="07"
      kicker="run it"
      title={
        <>
          Local-first. One compose file.
          <br />
          No account, no auth.
        </>
      }
      lede="The whole platform — control API, gateway, Postgres, Next.js UI,
      sandboxed agents — runs on your machine against local Ollama models.
      Point the gateway at OpenRouter or a provider key when you want to
      spend real money measuring things."
    >
      <div className="lp-cells lp-split-8-4">
        <Reveal className="lp-cell lp-term-window">
          <div className="lp-term-window__bar">
            <span className="lp-term-window__dot" />
            <span className="lp-term-window__dot" />
            <span className="lp-term-window__dot" />
            <span className="lp-term-window__path">~/work</span>
          </div>
          <div className="lp-term-window__body">
            {CMDS.map(([cmd, note], i) => (
              <div key={i} className="lp-cmd">
                <span className="lp-cmd__p">❯</span>
                <span className="lp-cmd__c">{cmd}</span>
                {note ? <span className="lp-cmd__n">{note}</span> : null}
              </div>
            ))}
            <div className="lp-cmd" style={{ marginTop: "0.3rem" }}>
              <span className="lp-cmd__p">❯</span>
              <span className="lp-t-dim">
                submit a run — “replica of 2048”, 2 harnesses, 1 model
              </span>
              <span className="evc-caret evc-caret-block" style={{ height: "0.9rem", width: "0.5rem" }} />
            </div>
          </div>
        </Reveal>

        <div className="lp-vcells">
          <Reveal delay={80} className="lp-side-note">
            <span className="kicker">prereqs</span>
            <div className="lp-prereqs">
              {PREREQS.map((p) => (
                <span key={p}>{p}</span>
              ))}
            </div>
            <a
              href="https://github.com/agodhani/harness_eval_gateway#quickstart"
              className="lp-repolink"
            >
              Full setup, including native Linux Ollama
              <ArrowUpRight className="icon-sm" />
            </a>
          </Reveal>
          <Reveal delay={140} className="lp-side-note">
            <span className="kicker">verify</span>
            <div className="lp-verify">
              <div>
                <span className="lp-t-faint">GET :8001/health</span>
                <span className="lp-t-signal">{`{"status":"ok"}`}</span>
              </div>
              <div>
                <span className="lp-t-faint">GET :8001/health/db</span>
                <span className="lp-t-signal">{`{"ok":true}`}</span>
              </div>
              <div>
                <span className="lp-t-faint">GET :8001/api/models</span>
                <span className="lp-t-signal">qwen2.5-coder:3b</span>
              </div>
            </div>
            <a
              href="https://github.com/agodhani/harness_eval_gateway"
              target="_blank"
              rel="noreferrer"
              className="lp-repolink"
            >
              <GithubMark className="icon-sm" />
              agodhani/harness_eval_gateway
              <ArrowUpRight className="icon-sm" />
            </a>
          </Reveal>
        </div>
      </div>
    </Section>
  );
}
