import Link from "next/link";
import Reveal from "./Reveal";
import RunConsole from "./RunConsole";
import { ArrowRight, GithubMark } from "./icons";

export default function Hero() {
  return (
    <section className="lp-hero lp-bg-grid">
      {/* radial dim over grid */}
      <div className="lp-hero__fade" />
      <div className="lp-wrap lp-hero__inner">
        <Reveal>
          <div className="row" style={{ gap: "1rem" }}>
            <span className="kicker">comparative agent-harness evaluation</span>
            <span className="lp-hline" aria-hidden />
            <span
              className="kicker"
              style={{ color: "var(--accent)", letterSpacing: "0.1em", textTransform: "none" }}
            >
              open source · local-first
            </span>
          </div>
        </Reveal>

        <Reveal delay={90}>
          <h1>
            <span className="lp-hero__brand">harness_eval_gateway</span>
            <br />
            Benchmark the <span className="lp-t-signal lp-glow">harness</span>,
            <br />
            not the model.
          </h1>
        </Reveal>

        <div className="lp-hero__grid">
          <Reveal delay={180}>
            <p className="lp-hero__lede">
              Coding agents differ in their scaffolding — tool loops, system
              prompts, skill packs. This platform runs them head-to-head on the
              same task with the same model, inside locked-down Docker
              sandboxes, while a custom AI gateway meters every token.
            </p>
            <div className="lp-hero__ctas">
              <Link href="/submit" className="btn btn--primary">
                Submit a run
              </Link>
              <a href="#pipeline" className="lp-cta-ghost">
                How a run works
                <ArrowRight className="icon-sm" />
              </a>
              <a
                href="https://github.com/agodhani/harness_eval_gateway"
                target="_blank"
                rel="noreferrer"
                className="lp-cta-ghost"
              >
                <GithubMark className="icon-sm" />
                Repo
              </a>
            </div>
          </Reveal>

          <Reveal delay={260}>
            <dl className="lp-stats">
              {[
                ["5", "harness adapters"],
                ["3", "gateway protocols"],
                ["∞", "events captured"],
              ].map(([v, l]) => (
                <div key={l} className="lp-stat">
                  <dt>{l}</dt>
                  <dd>{v}</dd>
                </div>
              ))}
            </dl>
            <p className="lp-hero__note">
              miniloop · codex · claude_code · pi · deepseek
              <br />
              /v1/chat/completions · /v1/messages · /v1/responses
            </p>
          </Reveal>
        </div>

        <Reveal delay={320} className="lp-console-wrap">
          <div className="lp-hero__console-label">
            <span className="kicker">live run — replay</span>
            <span
              className="kicker"
              style={{ letterSpacing: "0.06em", textTransform: "none" }}
            >
              append-only event stream · SSE
            </span>
          </div>
          <RunConsole />
        </Reveal>

        <div className="lp-scroll-cue">
          <div className="lp-scroll-line" />
        </div>
      </div>
    </section>
  );
}
