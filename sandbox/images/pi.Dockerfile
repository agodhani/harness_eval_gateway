# Pi coding-agent harness sandbox image (Phase 2.5) — context is REPO ROOT:
#   docker build -f sandbox/images/pi.Dockerfile -t heg-sandbox-pi:latest .
#
# Runs `pi --mode json` inside the locked-down `heg-sandbox` network via the
# python shim `adapters.pi` (writes ~/.pi/agent/models.json declaring a "heg"
# provider at the gateway's /v1/chat/completions — see
# docs/p25-harness-adapters.md). There is no other egress.
#
# Env contract (set by orchestrator/provision.py via the adapter):
#   GATEWAY_BASE_URL / GATEWAY_CRED / MODEL_NAME / TASK_PROMPT / WORKSPACE
#   PI_CODING_AGENT_DIR  /home/agent/.pi/agent (models.json lives there)
#   EVENTS_INGEST_URL    optional direct event-ingest endpoint

FROM node:22-slim

# python3 runs the adapter shim; git is a standard agent workspace tool.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# Pinned CLI version (manifest: adapters/pi/adapter.py).
RUN npm install -g @mariozechner/pi-coding-agent@0.73.1

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

CMD ["python3", "-m", "adapters.pi"]
