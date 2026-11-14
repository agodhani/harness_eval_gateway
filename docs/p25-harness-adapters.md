# Phase 2.5 — Real harness adapters (design notes)

Feature ask: "Allow for people to add harnesses for each of the tests — i.e.
deepseek harness, pi, claude code (open sourced harness), codex harness —
these can be the defaults for now."

This document captures the research + decisions so reviewers don't have to
redo them. Where the ask was ambiguous the call made is recorded and flagged.

## What the four harnesses are

| Adapter name | Upstream | Install | Non-interactive mode | Wire protocol |
|---|---|---|---|---|
| `miniloop` (existing) | our own agent | vendored | `python -m miniloop` | OpenAI chat completions |
| `claude_code` | `claude` (Anthropic, `@anthropic-ai/claude-code` npm) | `npm i -g` | `claude -p --output-format stream-json --verbose --dangerously-skip-permissions` | Anthropic Messages (`/v1/messages`) |
| `codex` | `codex` (OpenAI, `@openai/codex` npm) | `npm i -g` | `codex exec --json --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox` | OpenAI **Responses** (`/v1/responses`) |
| `pi` | `pi` (`@mariozechner/pi-coding-agent`, badlogic/pi-mono) | `npm i -g` | `pi --mode json --provider heg --model <id>` | OpenAI chat completions (`openai-completions`) |
| `deepseek` | `dsh` (`@deepseek-ai/dsh`, deepseek-ai/deepseek-harness) | `npm i -g` | `dsh --profile headless --json <task>` | DeepSeek "Messages" API (Anthropic-compatible per its docs) |
| `aider` (PR13) | `aider` (`aider-chat` pip, Aider-AI/aider) | `pip install` | `aider --yes-always --message <task>` | OpenAI chat completions via litellm's `openai` provider |

Ambiguities resolved:

- **"deepseek harness"** → DeepSeek's official open-source agent harness
  (`deepseek-ai/deepseek-harness`, npm `@deepseek-ai/dsh`; binaries `dsh` /
  `deepseek`). Not a model — a plugin-composed agent runtime with a
  `headless` profile (`dsh --profile headless --json "<task>"`, one shot,
  JSONL events, exit 0 iff `final.reason.kind == "completed"`).
- **"pi"** → Mario Zechner's `pi-mono` coding agent
  (`@mariozechner/pi-coding-agent`), the most defensible "pi" harness.
- **"claude code (open sourced harness)"** → the `@anthropic-ai/claude-code`
  CLI (open-sourced by Anthropic in 2025/26).
- **Codex protocol**: `wire_api = "chat"` was **removed** in Feb 2026 —
  current codex only speaks the OpenAI **Responses** API. Rather than pin an
  old codex, we add a `/v1/responses` endpoint to the gateway (see below).

## Key discovery that shapes the gateway work

**Ollama ≥ 0.14 natively serves all three protocols** on the same upstream
base URL:

- `/v1/chat/completions` (existing gateway endpoint — OpenAI chat)
- `/v1/messages` (Anthropic Messages incl. streaming + tool calls — verified
  against Ollama 0.40.16 on this dev box)
- `/v1/responses` (OpenAI Responses incl. SSE streaming — verified 200)

So the gateway stays a **pure pass-through with metering** — no protocol
translation. Each new endpoint gets: cred auth → run-budget gate → forward to
`OLLAMA_UPSTREAM_URL` verbatim → `llm_request`/`llm_response`/`error` events.
For `stream: true` bodies the response is an SSE stream that is tee'd to the
client while usage is scraped from the terminal SSE events
(`message_start`/`message_delta` for Anthropic, `response.completed` for
Responses).

Usage normalization keeps the canonical `prompt_tokens` /
`completion_tokens` / `total_tokens` keys (analytics + budget accounting
unchanged); Anthropic `input_tokens`/`output_tokens` +
`cache_read_input_tokens`/`cache_creation_input_tokens` and Responses
`input_tokens`/`output_tokens`/`input_tokens_details.cached_tokens` map onto
them. Budget estimate additionally reads `max_output_tokens` (Responses) and
`max_tokens` (Anthropic always sends it).

New gateway surface:

- `POST /v1/messages` — Anthropic proxy (stream + non-stream).
- `POST /v1/messages/count_tokens` — thin cred-auth pass-through (Claude Code
  calls it for context management; not metered).
- `POST /v1/responses` — OpenAI Responses proxy (stream + non-stream).
- `GET /v1/models` — thin cred-auth pass-through (CLIs probe it).
- `/v1/chat/completions` honors `stream: true` (metered SSE tee +
  `stream_options.include_usage` injection); callers without `stream` keep
  the buffered non-streamed path. Verified necessary live: pi's
  `openai-completions` provider hard-codes `stream: true` and its SSE reader
  treats a forced-JSON body as an empty assistant turn.
- Auth additionally accepts `x-api-key` — Anthropic-SDK convention; dsh's
  Messages adapter sends the credential there, not in `Authorization`.

## Adapter registry + manifest

`orchestrator/adapters.py` keeps the module registry and gains:

- `MANIFEST` dict per adapter module (`name`, `display_name`, `description`,
  `llm_protocol`, `image`, `cli`, `cli_version`, `requires`) — powers
  `GET /api/harnesses` and the UI dropdown.
- `adapter_image(adapter, fallback)` → `image_spec()["image"]` or the
  `SANDBOX_IMAGE` fallback (miniloop keeps no `image` key so the existing
  `SANDBOX_IMAGE` override path — used by the evaluator and tests — is
  unchanged).
- `harnesses()` → manifest list for the API.

`HarnessConfigIn` gains an optional `image` field (per-config image override;
empty → adapter default). `provision.spawn_one` resolves
`harness_config.image > adapter.image_spec()["image"] > settings.SANDBOX_IMAGE`.

## The shim pattern (why every CLI adapter has `__main__.py`)

External CLIs don't emit our normalized event types or call our ingest API,
so each adapter ships a **Python shim** baked into its image:

```
docker CMD: python3 -m adapters.<name>
  → adapters/<name>/__main__.py → adapters/common/runner.py
      1. read env contract (GATEWAY_BASE_URL, GATEWAY_CRED, MODEL_NAME,
         TASK_PROMPT, WORKSPACE, SKILLPACKS_ROOT, EVENTS_INGEST_URL,
         SYSTEM_PROMPT_EXTRA)
      2. write harness-native config (codex config.toml, pi models.json,
         claude .claude.json, dsh cordis.patch.yml)
      3. emit run_started
      4. spawn CLI argv (cwd=/workspace, stderr merged into stdout)
      5. per JSONL stdout line: echo raw line to stderr (transcript) +
         translate → normalized events → stdout JSONL + POST /internal/events
         (cred auth → run/sandbox ids stamped)
      6. on exit: emit terminal_state + print `HEG_RESULT {json}` marker;
         exit 0, or 2 on error
```

Normalized events are plain `{"type": <EventType>, "payload": {...}}` dicts —
`translate_events(lines)` per adapter is a pure function, unit-tested with
recorded CLI fixtures. `parse_terminal_state(logs)` scans for the shared
`HEG_RESULT ` marker; monitor's `_verdict` already handles the
done-signal/exit-code fallbacks.

Prompt footer appended by the shim for every CLI harness (keeps `agent_done`
reachable): workspace note, staged skill-pack dir list (only when non-empty),
the `done` binary instruction, and `system_prompt_extra` as its own section.
Harnesses with a native append flag (`claude --append-system-prompt`,
`pi --append-system-prompt`) get `system_prompt_extra` natively instead.

Event mapping conventions across CLI harnesses:

| Native | Normalized |
|---|---|
| session/thread/turn lifecycle | `checkpoint` (stage=…) |
| shell-exec tool call (Bash/command_execution/bash) | `shell_command` |
| file tool call (Read/Write/Edit/Glob/Grep/file_change/read/edit/write/find/ls) | `file_op` |
| any other tool call | `tool_call` |
| compaction (pi `compaction_end`, claude compact) | `context_compacted` |
| assistant text/final message | `checkpoint` (stage=assistant_message, truncated) |
| tool/harness errors | `error` |
| result/final/turn.completed | `terminal_state` + `HEG_RESULT` |

## Per-adapter wiring

### codex (`heg-sandbox-codex:latest`)

- Shim writes `$CODEX_HOME/config.toml`:
  `model`, `model_provider = "heg"`,
  `[model_providers.heg] base_url = "$GATEWAY_BASE_URL/v1",
  env_key = "HEG_API_KEY", wire_api = "responses"`.
- Env: `HEG_API_KEY=$GATEWAY_CRED`, `CODEX_HOME`, `CI`/`CODEX_SANDBOX` left
  unset (bypass flag handles approvals).
- `codex exec --json` item stream → `command_execution`→shell_command,
  `file_change`→file_op, `agent_message`→checkpoint,
  `turn.completed`→checkpoint(usage) + result marker.

### claude_code (`heg-sandbox-claude:latest`)

- Env: `ANTHROPIC_BASE_URL=$GATEWAY_BASE_URL` (client appends
  `/v1/messages`), `ANTHROPIC_AUTH_TOKEN=$GATEWAY_CRED` (sent as Bearer),
  `ANTHROPIC_MODEL`/`ANTHROPIC_SMALL_FAST_MODEL`=$MODEL_NAME,
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_TELEMETRY=1`,
  `DISABLE_AUTOUPDATER=1`, `DISABLE_ERROR_REPORTING=1`,
  `DISABLE_BUG_COMMAND=1`.
- Shim writes `~/.claude.json` (`hasCompletedOnboarding`, bypass-accepted
  flags) — first-run onboarding otherwise blocks headless mode.
- stream-json: `system.init`→checkpoint, assistant `tool_use` blocks→
  tool_call/shell_command/file_op, `result`→terminal (is_error→error).

### pi (`heg-sandbox-pi:latest`)

- Shim writes `~/.pi/agent/models.json`: provider `heg` with
  `baseUrl="$GATEWAY_BASE_URL/v1"`, `api="openai-completions"`,
  `apiKey="$GATEWAY_CRED"` (env interpolation).
- `pi --mode json --provider heg --model <id> --api-key $GATEWAY_CRED
  --no-session "<prompt>"`.
- Events: `tool_execution_start`→tool_call/shell_command/file_op,
  `compaction_end`→context_compacted, `message_end`(assistant)→checkpoint,
  `agent_settled`→terminal (aborted→error), `auto_retry_*`→checkpoint.

### deepseek (`heg-sandbox-deepseek:latest`)

- Env: `DEEPSEEK_API_KEY=$GATEWAY_CRED`, `DEEPSEEK_BASE_URL=$GATEWAY_BASE_URL`,
  `DSH_HOME=/home/agent/.dsh`.
- Shim writes `$DSH_HOME/profiles/headless/cordis.patch.yml` — a top-level
  **YAML array of cordis loader patch entries** (`{id, config}` replaces a
  config row wholesale; `{id, disabled}` toggles; `{insert: [...]}` appends;
  see `applyPatches` in `@deepseek-ai/dsh-app-boot`). Verified against
  `dsh --profile headless --dump-default-config` entry ids:
  `llm-deepseek` (`@deepseek-ai/dsh-llm-deepseek-api-key`, provider
  `deepseek-official`) gets `baseURL`/`apiKeyEnv`/`maxTokens`;
  `agent-default-model` gets `provider: deepseek-official` + the run model.
- `maxTokens` is clamped (default 16384, `DSH_MAX_TOKENS` override): dsh's
  256k default exceeds the run budget gate's `max_tokens` estimate →
  instant 429.
- dsh requires `node >=24.9` → its image uses `node:24-slim` (others 22).
- `dsh --profile headless --json --permission-mode danger-full-access
  --approval-policy never "<task>"`.
- Events: `session`→checkpoint, `tool_call`→tool_call/shell_command/file_op,
  `tool_result`(error)→error, `final`→terminal (reason.kind=="completed"
  →harness_complete else error), `error`→error.

### aider (`heg-sandbox-aider:latest`) — PR13

- First Python-CLI adapter: `python:3.12-slim` + `pip install
  aider-chat==0.86.2` (not the npm pattern) + `git` (aider auto-initializes
  the workspace repo and auto-commits applied edits — the commits double
  as `Commit <sha>` checkpoint events and a reviewable git log in the
  artifact). `GIT_AUTHOR_*`/`GIT_COMMITTER_*` env supplies identity.
- Shim writes `$HOME/.aider.conf.yml` (aider reads CWD → git-root → HOME;
  HOME wins among conf files and keeps the file out of the workspace
  artifact): `openai-api-base: <gw>/v1`, `model`/`weak-model:
  openai/<model>` (the `openai/` prefix is aider's litellm route to
  OpenAI-compatible endpoints; weak-model covers commit messages +
  history summarization — pinning it to the run model keeps the
  comparison axis clean), `yes-always`, `auto-commits`, `detect-urls:
  false`, `check-update: false`, `analytics-disable: true`, `pretty:
  false`, `stream: false`, `fancy-input: false`, and history-file
  relocation into `$HOME`. Cred stays in env (`OPENAI_API_KEY`), never on
  disk; `OPENAI_API_BASE` + `OPENAI_BASE_URL` both set (litellm ≥1.68
  renamed the canonical var and briefly broke `openai-api-base`).
- **No JSONL mode** — aider prints a human transcript only. The shim
  gains a `translate_text(line)` hook in `run_shim` (and
  `contract.cli_translate_events`): an ordered regex table maps status
  lines → events (`Applied edit to`→file_op, `Running`→shell_command,
  `Tokens:`→checkpoint llm_usage, `Commit`→checkpoint commit,
  `Finished summarizing`→context_compacted, `did not conform`/litellm
  failures→error). JSON-looking lines are dropped by `translate_obj`
  (they can only be echoed command output — translating them would let
  candidate files forge events).
- `agent_done` works because aider runs suggested shell commands under
  `--yes-always` → the prompt footer's `/usr/local/bin/done` call POSTs
  `/internal/done` like every other adapter.
- Verified argv basis: `aider --yes-always --message` is the documented
  headless form; headless flag set cross-checked against GitHub's gh-aw
  aider engine manifest (0.86.2).

## Version pins (reproducible images)

`@openai/codex@0.169.0` · `@anthropic-ai/claude-code@2.1.325` ·
`@mariozechner/pi-coding-agent@0.73.1` · `@deepseek-ai/dsh@0.2.2-rc.1`
(dsh only ships rc tags — latest at write time) · `aider-chat==0.86.2`
(pip — the Python CLI). Each Dockerfile records the pin + the research URL.

## Egress / creds

All harness traffic goes through the gateway with the per-sandbox `heg.*`
cred — each adapter maps it onto the CLI's expected credential env
(`HEG_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`,
models.json `apiKey`). Sandboxes stay on the `--internal` `heg-sandbox`
network; the only egress is `http://gateway:8080`.

## Known gaps / follow-ups

- Resolved on first live runs: cordis.patch.yml row shape (patch-entry
  array), `x-api-key` credential header, dsh `maxTokens` budget clash, pi
  streaming, `done` as a shell keyword (prompt footer now requires
  `/usr/local/bin/done`). All four adapters proven live end-to-end on an
  isolated gateway+sandbox topology (real Ollama `qwen3:8b` traffic,
  artifact file created, `HEG_RESULT` terminal markers).
- CLI harnesses can't honour `max_iters`; wall-clock/token budgets still cap
  them (documented in manifest + UI copy).
- `skill_packs` for CLI harnesses = read-only mount + prompt hint (no native
  skill invocation) — `skill_invocation` events stay miniloop-only.
- npm >=11 blocks postinstall scripts by default; dsh's
  `@deepseek-ai/dsh-subprocess-local` spawn-helper install script is
  skipped — the CLI still runs (verified live) but native subprocess/pty
  helpers are unbuilt. **PR13 decision: skip `--allow-scripts`.** The
  roadmap only sanctions enabling it if evals hit TTY-dependent tasks —
  none have (every shipped adapter runs end-to-end on the portable
  fallbacks), and allowing postinstall scripts widens the supply-chain
  surface for zero demonstrated need. Revisit the moment an eval fails on
  dsh's pty/native-subprocess path; enabling = `--allow-scripts` for dsh's
  package set in `sandbox/images/deepseek.Dockerfile`.
- aider emits no JSONL stream (PR13) — mid-run events come from the
  heuristic `translate_text` line table, so its timeline is coarser than
  the JSONL adapters' (no per-turn tool_call ids, no assistant-text
  events; the raw transcript still lands in `docker logs`). If aider ever
  ships a machine-readable mode, switch `adapters/aider` to it and retire
  the regex table.
