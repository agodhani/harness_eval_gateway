# Codex CLI harness sandbox image (Phase 2.5) — build context is the REPO ROOT:
#   docker build -f sandbox/images/codex.Dockerfile -t heg-sandbox-codex:latest .
#
# Runs `codex exec --json` inside the locked-down `heg-sandbox` network via
# the python shim `adapters.codex` (writes $CODEX_HOME/config.toml pointing a
# custom "heg" provider at the gateway's /v1/responses — see
# docs/p25-harness-adapters.md). All model traffic must go through the AI
# gateway at http://gateway:8080 — there is no other egress.
#
# Env contract (set by orchestrator/provision.py via the adapter):
#   GATEWAY_BASE_URL / GATEWAY_CRED / MODEL_NAME / TASK_PROMPT / WORKSPACE
#   HEG_API_KEY      same cred — codex reads it via env_key in config.toml
#   CODEX_HOME       /home/agent/.codex (config.toml lives there)
#   EVENTS_INGEST_URL optional direct event-ingest endpoint

FROM node:22-slim

# python3 runs the adapter shim; git is a standard agent workspace tool.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/harness
COPY adapters/ /opt/harness/adapters/
COPY sandbox/images/done /usr/local/bin/done
RUN chmod +x /usr/local/bin/done

# Pinned CLI version (manifest: adapters/codex/adapter.py).
RUN npm install -g @openai/codex@0.169.0

RUN useradd -m agent && mkdir -p /workspace && chown agent /workspace
USER agent
ENV HOME=/home/agent

WORKDIR /workspace
ENV PYTHONPATH=/opt/harness \
    PYTHONUNBUFFERED=1

CMD ["python3", "-m", "adapters.codex"]
