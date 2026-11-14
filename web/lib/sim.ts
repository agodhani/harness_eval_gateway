/* Scripted replay of a run's event stream — drives the landing hero console.
   Vocabulary mirrors api/schemas.py EventType / TerminalState. */

export type EvType =
  | "run_started"
  | "llm_request"
  | "llm_response"
  | "tool_call"
  | "shell_command"
  | "file_op"
  | "skill_invocation"
  | "test_execution"
  | "checkpoint"
  | "done_signal"
  | "terminal_state"
  | "error"
  | "eval_score";

export type SimEvent = {
  at: number; // tick index
  sb: 0 | 1 | 2; // sandbox index — 2 = evaluator lane
  type: EvType;
  text: string;
  tok?: number;
};

export const SANDBOXES = [
  { id: "sb·a", adapter: "miniloop", packs: "superpowers", terminal: "agent_done", score: 8.4 },
  { id: "sb·b", adapter: "codex", packs: "spec-kit", terminal: "stuck_loop", score: 5.9 },
] as const;

export const SCRIPT: SimEvent[] = [
  { at: 0, sb: 0, type: "run_started", text: "sandbox provisioned · image heg/miniloop" },
  { at: 0, sb: 1, type: "run_started", text: "sandbox provisioned · image heg/codex" },
  { at: 1, sb: 0, type: "checkpoint", text: "workspace staged · identical repo" },
  { at: 1, sb: 1, type: "checkpoint", text: "workspace staged · identical repo" },
  { at: 2, sb: 0, type: "llm_request", text: "POST /v1/chat/completions" },
  { at: 2, sb: 1, type: "llm_request", text: "POST /v1/responses" },
  { at: 3, sb: 0, type: "skill_invocation", text: "use_skill · superpowers/brainstorming" },
  { at: 3, sb: 1, type: "llm_response", text: "stream tee · usage normalized", tok: 4120 },
  { at: 4, sb: 0, type: "llm_response", text: "qwen3:8b · 214ms", tok: 3044 },
  { at: 5, sb: 1, type: "shell_command", text: "npx create-vite game --template react-ts" },
  { at: 5, sb: 0, type: "tool_call", text: "shell · mkdir -p src && ls" },
  { at: 6, sb: 0, type: "file_op", text: "write src/game/grid.ts" },
  { at: 6, sb: 1, type: "llm_request", text: "POST /v1/responses" },
  { at: 7, sb: 1, type: "llm_response", text: "stream tee · usage normalized", tok: 6981 },
  { at: 8, sb: 0, type: "llm_request", text: "POST /v1/chat/completions" },
  { at: 8, sb: 1, type: "file_op", text: "write src/App.tsx" },
  { at: 9, sb: 0, type: "llm_response", text: "qwen3:8b · 188ms", tok: 5112 },
  { at: 10, sb: 1, type: "shell_command", text: "npm run build" },
  { at: 10, sb: 0, type: "file_op", text: "write src/game/merge.ts" },
  { at: 11, sb: 1, type: "error", text: "build failed · TS2322 mergeTiles" },
  { at: 12, sb: 0, type: "tool_call", text: "shell · node --check dist/grid.js" },
  { at: 12, sb: 1, type: "llm_request", text: "POST /v1/responses" },
  { at: 13, sb: 1, type: "llm_response", text: "stream tee · usage normalized", tok: 8402 },
  { at: 14, sb: 0, type: "llm_request", text: "POST /v1/chat/completions" },
  { at: 14, sb: 1, type: "file_op", text: "patch src/App.tsx · retry 1" },
  { at: 15, sb: 0, type: "llm_response", text: "qwen3:8b · 231ms", tok: 7788 },
  { at: 16, sb: 1, type: "shell_command", text: "npm run build" },
  { at: 16, sb: 0, type: "file_op", text: "write src/game/spawn.ts" },
  { at: 17, sb: 1, type: "error", text: "build failed · TS2322 mergeTiles" },
  { at: 18, sb: 0, type: "test_execution", text: "vitest · 6 passed" },
  { at: 18, sb: 1, type: "llm_request", text: "POST /v1/responses" },
  { at: 19, sb: 1, type: "file_op", text: "patch src/App.tsx · retry 2" },
  { at: 19, sb: 0, type: "llm_request", text: "POST /v1/chat/completions" },
  { at: 20, sb: 1, type: "shell_command", text: "npm run build" },
  { at: 20, sb: 0, type: "llm_response", text: "qwen3:8b · 202ms", tok: 6409 },
  { at: 21, sb: 1, type: "error", text: "build failed · TS2322 mergeTiles" },
  { at: 21, sb: 0, type: "skill_invocation", text: "use_skill · superpowers/test-driven" },
  { at: 22, sb: 1, type: "terminal_state", text: "stuck_loop · 3 identical tool calls" },
  { at: 22, sb: 0, type: "file_op", text: "write src/ui/Board.tsx" },
  { at: 23, sb: 0, type: "llm_request", text: "POST /v1/chat/completions" },
  { at: 24, sb: 0, type: "llm_response", text: "qwen3:8b · 246ms", tok: 8873 },
  { at: 25, sb: 0, type: "shell_command", text: "npm run build && npm test" },
  { at: 26, sb: 0, type: "test_execution", text: "build ok · 11 passed" },
  { at: 27, sb: 0, type: "tool_call", text: "shell · /usr/local/bin/done" },
  { at: 28, sb: 0, type: "done_signal", text: "POST /internal/done · 204" },
  { at: 29, sb: 0, type: "terminal_state", text: "agent_done · workspace snapshotted" },
  { at: 31, sb: 2, type: "checkpoint", text: "evaluator sandbox online · rubric v1 frozen" },
  { at: 32, sb: 2, type: "llm_request", text: "judge artifact sb·a × 4 dimensions" },
  { at: 33, sb: 2, type: "llm_request", text: "judge artifact sb·b × 4 dimensions" },
  { at: 34, sb: 2, type: "eval_score", text: "scores written · matrix ready" },
];

export const TICK_MS = 640;
export const RESULTS_AT = 34; // tick when evaluator starts
export const HOLD_MS = 5200; // hold on results before replay
export const TOTAL_TICKS = 35;

export const TYPE_COLOR: Record<EvType, string> = {
  run_started: "lp-t-signal",
  checkpoint: "lp-t-faint",
  llm_request: "lp-t-violet",
  llm_response: "lp-t-violet",
  tool_call: "lp-t-fg",
  shell_command: "lp-t-dim",
  file_op: "lp-t-faint",
  skill_invocation: "lp-t-signal",
  test_execution: "lp-t-info",
  done_signal: "lp-t-signal",
  terminal_state: "lp-t-warn",
  error: "lp-t-err",
  eval_score: "lp-t-signal",
};
