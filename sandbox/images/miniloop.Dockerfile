# Miniloop harness sandbox image (F4/F5) — build context is the REPO ROOT:
#   docker build -f sandbox/images/miniloop.Dockerfile -t heg-sandbox:latest .
#
# Runs the miniloop coding agent inside the locked-down `heg-sandbox`
# network (see sandbox/runtime.py). All model traffic must go through the
# AI gateway at http://gateway:8080 — there is no other egress.
#
# Env contract expected by the miniloop entrypoint (set by the adapter /
# orchestrator via SandboxSpec.env):
#   GATEWAY_BASE_URL  e.g. http://gateway:8080  (runtime injects this default)
#   GATEWAY_CRED      per-sandbox bearer credential for the gateway
#   MODEL_NAME        model id to request (same model across all sandboxes)
#   TASK_PROMPT       the task text for this run
#   WORKSPACE         /workspace (runtime injects this default)
#   EVENTS_INGEST_URL optional direct event-ingest endpoint (F6)

FROM python:3.12-slim

# Miniloop sources + the `done` CLI are vendored in the repo. `adapters/`
# may still be a stub while F5 lands — the image builds regardless; the
# entrypoint only works once adapters/miniloop provides a `miniloop`
# package importable via PYTHONPATH (see below).
WORKDIR /opt/miniloop
COPY adapters/ /opt/miniloop/adapters/
COPY sandbox/images/done /usr/local/bin/done
RUN chmod +x /usr/local/bin/done

# httpx is miniloop's only external dependency.
RUN pip install --no-cache-dir httpx

# Unprivileged agent user; /workspace is the bind-mounted task workspace.
RUN useradd -m agent && mkdir -p /workspace && chown agent /workspace
USER agent

WORKDIR /workspace

# /opt/miniloop covers a top-level `miniloop` package;
# /opt/miniloop/adapters covers `adapters/miniloop/` laid out as an
# importable package (i.e. containing __main__.py / __init__.py).
ENV PYTHONPATH=/opt/miniloop:/opt/miniloop/adapters \
    PYTHONUNBUFFERED=1

CMD ["python", "-m", "miniloop"]
