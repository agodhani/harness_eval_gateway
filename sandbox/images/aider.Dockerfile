# Aider harness sandbox image (PR13) — build context is the REPO ROOT:
#   docker build -f sandbox/images/aider.Dockerfile \
#       -t heg-sandbox-aider:latest .
#
# Runs `aider --yes-always --message "<task>"` inside the locked-down
# `heg-sandbox` network via the python shim `adapters.aider` (writes
# $HOME/.aider.conf.yml pointing litellm's openai provider at the gateway's
# /v1/chat/completions — see docs/p25-harness-adapters.md). Aider emits
# plain text, not JSONL — the shim's translate_text hook maps its status
# lines onto normalized events. Model traffic via OPENAI_API_BASE — no
# other egress.
#
# Env contract (set by orchestrator/provision.py via the adapter):
#   GATEWAY_BASE_URL / GATEWAY_CRED / MODEL_NAME / TASK_PROMPT / WORKSPACE
#   OPENAI_API_KEY     per-sandbox cred (litellm openai provider)
#   OPENAI_API_BASE    <gateway>/v1 (OPENAI_BASE_URL set too — litellm>=1.68
#                      treats it as the canonical name)
#   EVENTS_INGEST_URL  optional direct event-ingest endpoint
#
# Aider is a Python CLI — unlike the node:slim images for the npm CLIs,
# python:3.12-slim supplies both the pinned CLI and the shim interpreter.
FROM python:3.12-slim

# git is required — aider auto-initializes the workspace repo and
# auto-commits applied edits (GIT_* identity comes from the adapter env).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Pinned CLI version (manifest: adapters/aider/adapter.py).
RUN pip install --no-cache-dir aider-chat==0.86.2

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

CMD ["python3", "-m", "adapters.aider"]
