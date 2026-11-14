# Exec-scoring sandbox image (PR7) — build context is the REPO ROOT:
#   docker build -f sandbox/images/exec.Dockerfile -t heg-exec:latest .
#
# One-shot container that runs candidate build/tests/coverage for the
# scoring `exec` signal (see sandbox/exec.py + scoring/exec.py). Spawned by
# ExecRunner with `--network none`, dropped caps, no-new-privileges, the
# nobody user, and CPU/memory/pids bounds — there is no egress at all, not
# even the gateway (exec needs no model calls).
#
# Mounts:
#   /srv/src  candidate workspace, read-only (bind-mounted artifact dir)
#   /work     writable copy target (image dir owned by nobody)
#   /out      result channel — exec_result.json is written here AND printed
#             to stdout as a `HEG_EXEC_RESULT <json>` sentinel (fallback)

FROM python:3.12-slim

# pytest + coverage are the only exec deps. Pinned major ranges; the image
# is rebuilt per-repo-release so loose pins keep security updates flowing.
RUN pip install --no-cache-dir "pytest>=8,<9" "coverage>=7,<8"

COPY sandbox/images/exec_runner.py /usr/local/bin/heg-exec-runner

# /work is where the runner copies the candidate tree before exec'ing —
# owned by nobody so cap-dropped, non-root exec can write build artifacts.
RUN chmod 755 /usr/local/bin/heg-exec-runner \
    && mkdir -p /work /out \
    && chown -R 65534:65534 /work /out

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    COVERAGE_FILE=/tmp/.coverage \
    HOME=/tmp

# Safety net: nobody, even if a caller forgets `--user` on docker run.
USER 65534:65534
WORKDIR /work

ENTRYPOINT ["python", "/usr/local/bin/heg-exec-runner"]
