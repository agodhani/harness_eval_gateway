# DeepSeek Harness (dsh) sandbox image (Phase 2.5) — context is REPO ROOT:
#   docker build -f sandbox/images/deepseek.Dockerfile \
#       -t heg-sandbox-deepseek:latest .
#
# Runs `dsh --profile headless --json` inside the locked-down `heg-sandbox`
# network via the python shim `adapters.deepseek` (writes a
# $DSH_HOME/profiles/headless/cordis.patch.yml overlay pointing the deepseek
# provider at the gateway — see docs/p25-harness-adapters.md). Model traffic
# goes through DEEPSEEK_BASE_URL → the gateway's /v1/messages — no other
# egress.
#
# Env contract (set by orchestrator/provision.py via the adapter):
#   GATEWAY_BASE_URL / GATEWAY_CRED / MODEL_NAME / TASK_PROMPT / WORKSPACE
#   DEEPSEEK_API_KEY   per-sandbox cred
#   DEEPSEEK_BASE_URL  gateway URL
#   DSH_HOME           /home/agent/.dsh (profile overlay lives there)
#   EVENTS_INGEST_URL  optional direct event-ingest endpoint

FROM node:22-slim

# python3 runs the adapter shim; git is a standard agent workspace tool.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/harness
COPY adapters/ /opt/harness/adapters/
COPY sandbox/images/done /usr/local/bin/done
RUN chmod +x /usr/local/bin/done

# Pinned CLI version (manifest: adapters/deepseek/adapter.py).
RUN npm install -g @deepseek-ai/dsh@0.2.2-rc.1

RUN useradd -m agent && mkdir -p /workspace && chown agent /workspace
USER agent
ENV HOME=/home/agent

WORKDIR /workspace
ENV PYTHONPATH=/opt/harness \
    PYTHONUNBUFFERED=1

CMD ["python3", "-m", "adapters.deepseek"]
