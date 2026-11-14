"use client";

import { useEffect, useRef, useState } from "react";
import {
  HOLD_MS,
  RESULTS_AT,
  SANDBOXES,
  SCRIPT,
  TICK_MS,
  TOTAL_TICKS,
  TYPE_COLOR,
  type SimEvent,
} from "@/lib/sim";

function fmtTok(n: number) {
  return n.toLocaleString("en-US");
}

function fmtClock(ms: number) {
  const s = Math.floor(ms / 1000);
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

const TERMINAL_MOD: Record<string, string> = {
  agent_done: "evc-lane__state--signal",
  stuck_loop: "evc-lane__state--warn",
};

export default function RunConsole() {
  const [tick, setTick] = useState(0);
  const [cycle, setCycle] = useState(0);
  const colRefs = useRef(new Map<number, HTMLDivElement>());
  const evalRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (tick >= TOTAL_TICKS) {
      const hold = setTimeout(() => {
        setTick(0);
        setCycle((c) => c + 1);
      }, HOLD_MS);
      return () => clearTimeout(hold);
    }
    const t = setTimeout(() => setTick((v) => v + 1), TICK_MS);
    return () => clearTimeout(t);
  }, [tick]);

  useEffect(() => {
    for (const el of colRefs.current.values()) {
      el.scrollTop = el.scrollHeight;
    }
    const el = evalRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [tick]);

  const events = SCRIPT.filter((e) => e.at <= tick);
  const results = tick >= RESULTS_AT + 1;
  const elapsed = Math.min(tick, TOTAL_TICKS) * TICK_MS;

  return (
    <div className="evc lp-panel ticks lp-sweep">
      <span className="tick-b" />
      {/* console header */}
      <div className="evc__head">
        <span className="evc__run">
          <span className="evc-dot evc-dot-live" />
          RUN&nbsp;a58dd3de
        </span>
        <span className="evc__meta">task — “replica of 2048”</span>
        <span className="evc__meta">model — qwen3:8b</span>
        <span className="evc__right">
          <span>{fmtClock(elapsed)}</span>
          <span className="lp-t-signal">{results ? "evaluating" : "running"}</span>
        </span>
      </div>

      {/* sandbox lanes */}
      <div className="evc-lanes">
        {SANDBOXES.map((sb, i) => {
          const sbEvents = events.filter((e) => e.sb === i);
          const toks = sbEvents.reduce((a, e) => a + (e.tok ?? 0), 0);
          const term = sbEvents.findLast((e) => e.type === "terminal_state");
          return (
            <div key={sb.id} className="evc-lane">
              <div className="evc-lane__head">
                <span className="evc-lane__id">{sb.id}</span>
                <span className="evc-lane__spec">
                  {sb.adapter} · {sb.packs}
                </span>
                <span
                  className={`evc-lane__state ${term ? TERMINAL_MOD[sb.terminal] : ""}`}
                >
                  {term ? sb.terminal : "exec"}
                </span>
              </div>
              <div
                ref={(el) => {
                  if (el) colRefs.current.set(i, el);
                  else colRefs.current.delete(i);
                }}
                className="evc-lane__events"
              >
                {sbEvents.map((e: SimEvent, j) => (
                  <div key={`${cycle}-${i}-${j}`} className="evc-ev evc-line">
                    <span className={`evc-ev__type ${TYPE_COLOR[e.type]}`}>
                      {e.type}
                    </span>
                    <span className="evc-ev__text">{e.text}</span>
                    {e.tok ? (
                      <span className="evc-ev__tok">{fmtTok(e.tok)} tok</span>
                    ) : null}
                  </div>
                ))}
                {!term && sbEvents.length > 0 && (
                  <span className="evc-caret evc-caret-block" />
                )}
              </div>
              <div className="evc-lane__foot">
                <span>{fmtTok(toks)} tok</span>
                <span>$0.00 ollama</span>
                <span className="evc-lane__score">
                  score{" "}
                  <b className={results ? "lp-t-signal" : "lp-t-faint"}>
                    {results ? sb.score.toFixed(1) : "—"}
                  </b>
                </span>
              </div>
            </div>
          );
        })}
      </div>

      {/* evaluator lane */}
      <div className="evc-eval">
        <div className="evc-eval__head">
          <span className="evc-eval__label">evaluator</span>
          <span className="evc-eval__sub">sandboxed judge · rubric v1 frozen</span>
        </div>
        <div ref={evalRef} className="evc-eval__events">
          {events
            .filter((e) => e.sb === 2)
            .map((e, j) => (
              <div key={`${cycle}-e-${j}`} className="evc-ev evc-line">
                <span className={`evc-ev__type ${TYPE_COLOR[e.type]}`}>
                  {e.type}
                </span>
                <span className="evc-ev__text">{e.text}</span>
              </div>
            ))}
          {tick < RESULTS_AT && (
            <span className="lp-t-faint">awaiting terminal states…</span>
          )}
        </div>
      </div>
    </div>
  );
}
