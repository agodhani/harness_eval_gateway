import Reveal from "./Reveal";
import Section from "./Section";

const ENDPOINTS = [
  ["POST", "/v1/chat/completions", "OpenAI-compatible · streaming preserved via metering tee"],
  ["POST", "/v1/messages", "Anthropic-compatible · Claude Code, dsh"],
  ["POST", "/v1/messages/count_tokens", "pre-flight probes from CLI harnesses"],
  ["POST", "/v1/responses", "Codex CLI pass-through"],
  ["GET", "/v1/models", "harness capability probes"],
  ["POST", "/internal/events", "normalized harness events · cred or ingest-token auth"],
  ["POST", "/internal/done", "agent done-signal → terminal state"],
] as const;

const METERED = [
  ["model", "qwen3:8b"],
  ["tokens_in", "6,412"],
  ["tokens_out", "1,209"],
  ["cache_r/w", "0 / 0"],
  ["latency", "214ms"],
  ["cost", "$0.0000"],
] as const;

export default function Gateway() {
  return (
    <Section
      id="gateway"
      index="03"
      kicker="the gateway"
      title={
        <>
          The only way out
          <br />
          is through the meter.
        </>
      }
      lede="A custom AI gateway — not LiteLLM, because observability is the product —
      is the single egress route for all model traffic. Per-run HMAC credentials
      map every request to a run and sandbox. Streams are tee'd, not buffered,
      so token accounting is exact even mid-flight."
    >
      <div className="lp-cells lp-split-7-5">
        {/* endpoints */}
        <Reveal className="lp-cell">
          <div className="lp-cell__head">
            <span className="kicker">protocol fan-out</span>
          </div>
          <ul className="lp-endpoints">
            {ENDPOINTS.map(([m, p, d]) => (
              <li key={p + m} className="lp-endpoint">
                <span
                  className={`lp-endpoint__m ${m === "GET" ? "lp-t-info" : "lp-t-signal"}`}
                >
                  {m}
                </span>
                <span className="lp-endpoint__p">{p}</span>
                <span className="lp-endpoint__d">{d}</span>
              </li>
            ))}
          </ul>
        </Reveal>

        {/* metering + creds */}
        <div className="lp-vcells">
          <Reveal delay={90} className="lp-side-note">
            <span className="kicker">per-run credentials</span>
            <div className="lp-cred">
              <span className="lp-t-faint">Authorization: Bearer</span>{" "}
              <span className="lp-t-violet">heg.</span>
              <span className="lp-t-fg">a58dd3de.sb·a</span>
              <span className="lp-t-violet">.7f3a9c…</span>
            </div>
            <p>
              HMAC-signed, scoped to one sandbox in one run. Traffic can't
              impersonate a sibling — and a leaked cred attributes itself.
            </p>
          </Reveal>
          <Reveal delay={160} className="lp-side-note">
            <span className="kicker">every call recorded</span>
            <dl className="lp-meter">
              {METERED.map(([k, v]) => (
                <div key={k}>
                  <dt>{k}</dt>
                  <dd>{v}</dd>
                </div>
              ))}
            </dl>
            <p style={{ marginTop: "1rem" }}>
              Cross a token or cost budget and the gateway answers{" "}
              <span className="mono-inline lp-t-warn">429</span> and writes the{" "}
              <span className="mono-inline lp-t-warn">budget_exceeded</span>{" "}
              event itself — enforcement lives below the agent, where it can't
              be argued with.
            </p>
          </Reveal>
        </div>
      </div>
    </Section>
  );
}
