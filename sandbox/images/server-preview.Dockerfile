# Web-server artifact preview image (PR3) — build context is the REPO ROOT:
#   docker build -f sandbox/images/server-preview.Dockerfile \
#       -t heg-preview-server:latest .
#
# Executes a candidate web app (flask / fastapi / uvicorn / django / aiohttp
# / bottle / tornado / stdlib http.server) and exposes it on :8000 so the
# preview's socat sidecar can publish it to localhost.
#
# Isolation model: this image runs on the --internal `heg-desktop` network
# (shared by all executed-code previews) — NO egress and NO published ports;
# the only reachable peer is its own socat proxy sidecar (see
# sandbox/preview.py::_spawn_server). Runtime flags add
# --cpus/--memory/--pids-limit, cap-drop ALL, no-new-privileges, nobody.
#
# Dependency note: candidate deps CANNOT be pip-installed at spawn time (the
# internal net has no egress), so the common web frameworks are baked in.
# requirements.txt entries beyond these won't be installed — the app fails
# at import and the runner exits visibly (logs + GET status "exited").
#
# Env contract (injected by PreviewManager._spawn_server):
#   APP_ENTRY   artifact .py entry relative to the staged copy, e.g. app.py
#   PORT/HOST   hints passed to the app (runner also relays :8000 -> actual)
#
FROM python:3.12-slim

# Frameworks agents most commonly produce; stdlib http.server needs nothing.
RUN pip install --no-cache-dir \
        "flask~=3.1" \
        "fastapi~=0.162" \
        "uvicorn[standard]~=0.54" \
        "django~=6.1" \
        "aiohttp~=3.14" \
        "bottle~=0.13" \
        "tornado~=6.6" \
        "gunicorn~=27.0"

# /work gets the staged writable copy of the artifact (the bind mount is
# :ro — apps may write state next to the entry); /srv/artifact-ro is the
# read-only mount point. Both owned by nobody so the entrypoint can copy
# under --user 65534.
RUN mkdir -p /work /srv/artifact-ro && chown -R 65534:65534 /work

COPY sandbox/images/server-preview-runner.py /runner.py

ENV PYTHONUNBUFFERED=1

USER 65534:65534
ENTRYPOINT ["python", "/runner.py"]
