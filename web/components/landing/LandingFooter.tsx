import { GithubMark, LogoMark } from "./icons";

export default function LandingFooter() {
  return (
    <footer className="lp-footer">
      <div className="lp-wrap lp-footer__inner">
        <div className="lp-footer__grid">
          <div>
            <div className="lp-footer__brand">
              <LogoMark />
              <span>harness_eval_gateway</span>
            </div>
            <p className="lp-footer__lede">
              Comparative evaluation for AI coding-agent harnesses. One
              variable. Total observability. Scores you can defend.
            </p>
          </div>
          <div>
            <span className="kicker">repo</span>
            <ul className="lp-footer__links">
              {[
                ["source", "https://github.com/agodhani/harness_eval_gateway"],
                ["issues", "https://github.com/agodhani/harness_eval_gateway/issues"],
                ["AGENTS.md", "https://github.com/agodhani/harness_eval_gateway/blob/main/AGENTS.md"],
                ["FEATURE_MAP.md", "https://github.com/agodhani/harness_eval_gateway/blob/main/FEATURE_MAP.md"],
              ].map(([label, href]) => (
                <li key={label}>
                  <a href={href} target="_blank" rel="noreferrer" className="lp-link-line">
                    {label}
                  </a>
                </li>
              ))}
            </ul>
          </div>
          <div>
            <span className="kicker">stack</span>
            <p className="lp-footer__stack">
              fastapi · sqlalchemy/alembic · postgres 16
              <br />
              next.js · docker sandboxes · ollama
              <br />
              openai + anthropic + responses protocols
            </p>
          </div>
        </div>
        <div className="lp-footer__base">
          <span>harness_eval_gateway — an open eval rig for agent harnesses</span>
          <a
            href="https://github.com/agodhani/harness_eval_gateway"
            target="_blank"
            rel="noreferrer"
          >
            <GithubMark className="icon-sm" />
            github
          </a>
        </div>
      </div>
    </footer>
  );
}
