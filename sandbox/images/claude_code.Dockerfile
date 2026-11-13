# Claude Code harness sandbox image (Phase 2.5) — context is the REPO ROOT:
#   docker build -f sandbox/images/claude_code.Dockerfile \
#       -t heg-sandbox-claude:latest .
#
# Runs `claude -p --output-format stream-json --verbose
# --dangerously-skip-permissions` inside the locked-down `heg-sandbox`
# network via the python shim `adapters.claude_code` (seeds ~/.claude.json so
# headless mode skips onboarding — see docs/p25-harness-adapters.md).
# Model traffic is the Anthropic Messages API at the gateway's /v1/messages —
# there is no other egress.
#
# Env contract (set by orchestrator/provision.py via the adapter):
#   GATEWAY_BASE_URL / GATEWAY_CRED / MODEL_NAME / TASK_PROMPT / WORKSPACE
#   ANTHROPIC_BASE_URL   gateway URL (claude's API endpoint override)
#   ANTHROPIC_AUTH_TOKEN per-sandbox cred (sent as Bearer)
#   ANTHROPIC_MODEL      run model (plus SMALL_FAST/HAIKU fallbacks)
#   EVENTS_INGEST_URL    optional direct event-ingest endpoint

FROM node:22-slim

# python3 runs the adapter shim; git is a standard agent workspace tool.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# Pinned CLI version (manifest: adapters/claude_code/adapter.py).
RUN npm install -g @anthropic-ai/claude-code@2.1.325

WORKDIR /opt/harness
COPY adapters/ /opt/harness/adapters/
COPY sandbox/images/done /usr/local/bin/done
RUN chmod +x /usr/local/bin/done

RUN useradd -m agent && mkdir -p /workspace && chown agent /workspace
USER agent
ENV HOME=/home/agent

WORKDIR /workspace
ENV PYTHONPATH=/opt/harness \
    PYTHONUNBUFFERED=1

CMD ["python3", "-m", "adapters.claude_code"]
