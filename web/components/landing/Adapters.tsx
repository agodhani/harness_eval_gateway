import Reveal from "./Reveal";
import Section from "./Section";

const ADAPTERS = [
  {
    name: "miniloop",
    role: "reference harness",
    proto: "/v1/chat/completions",
    desc: "The platform's own minimal coding agent — a tool-call loop with shell, file I/O, use_skill and done. The control instrument every CLI is compared against.",
    status: "shipped",
  },
  {
    name: "codex",
    role: "OpenAI Codex CLI",
    proto: "/v1/responses",
    desc: "Pinned npm CLI on node:slim, launched in JSONL mode by a stdlib shim that translates native records into normalized events.",
    status: "shipped",
  },
  {
    name: "claude_code",
    role: "Anthropic CLI",
    proto: "/v1/messages",
    desc: "Anthropic-SDK protocol adapter. Native config written at provision time; stream-json output becomes the event feed.",
    status: "shipped",
  },
  {
    name: "pi",
    role: "OpenAI-compatible CLI",
    proto: "/v1/chat/completions",
    desc: "Streams unconditionally — the gateway honors stream:true with a metering tee so usage is still captured exactly.",
    status: "shipped",
  },
  {
    name: "deepseek",
    role: "dsh CLI",
    proto: "/v1/messages",
    desc: "DeepSeek's dsh on node:24-slim. Same shim pattern, same event contract — proof the adapter surface generalizes.",
    status: "shipped",
  },
  {
    name: "aider",
    role: "Python CLI (litellm)",
    proto: "/v1/chat/completions",
    desc: "aider --message on python:slim. No JSONL mode — the shim maps its plain-text status lines (Applied edit, Tokens, Commit) onto the same normalized events.",
    status: "shipped",
  },
] as const;

const CONTRACT = `interface HarnessAdapter {
  image_spec()               // sandbox image
  install_skillpacks(packs)  // vendored + imported
  env(url, run_creds)        // wire → gateway
  launch(task_prompt)        // non-interactive
  translate_events(stream)   // → Event[]
  completion_signal()        // done detection
}`;

export default function Adapters() {
  return (
    <Section
      id="adapters"
      index="04"
      kicker="harness adapters"
      title={
        <>
          Any agent that speaks
          <br />a protocol can be measured.
        </>
      }
      lede="Each harness plugs in through a six-function adapter contract: build the
      image, install skill packs, wire gateway env, launch non-interactively,
      translate its output, detect completion. Six adapters ship today."
    >
      <div className="lp-cells lp-cells-2 lp-cells-3">
        {ADAPTERS.map((a, i) => (
          <Reveal key={a.name} delay={i * 60} className="lp-adapter">
            <div className="lp-adapter__top">
              <span className="lp-adapter__name">{a.name}</span>
              <span className="lp-adapter__status">{a.status}</span>
            </div>
            <div className="lp-adapter__role">{a.role}</div>
            <p className="lp-adapter__desc">{a.desc}</p>
            <div className="lp-adapter__proto">{a.proto}</div>
          </Reveal>
        ))}

        {/* contract card */}
        <Reveal
          delay={ADAPTERS.length * 60}
          className="lp-cell--ink lp-contract"
        >
          <span className="kicker">the contract</span>
          <pre>{CONTRACT}</pre>
        </Reveal>
      </div>

      {/* skillpacks strip */}
      <Reveal delay={100}>
        <div className="lp-packs">
          <span className="kicker">skill packs</span>
          <div className="lp-packs__list">
            {["superpowers ×14", "mattpocock-skills ×28", "spec-kit ×9"].map((p) => (
              <span key={p}>{p}</span>
            ))}
            <span>+ import any pack straight from GitHub</span>
          </div>
        </div>
      </Reveal>
    </Section>
  );
}
