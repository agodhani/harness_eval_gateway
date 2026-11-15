# Exec-scoring sandbox image (PR7) — build context is the REPO ROOT:
#   docker build -f sandbox/images/exec.Dockerfile -t heg-exec:latest .
#
# One-shot container that runs candidate build/tests/coverage for the
# scoring `exec` signal (see sandbox/exec.py + scoring/exec.py). Spawned by
# ExecRunner with `--network none`, dropped caps, no-new-privileges, the
# nobody user, and CPU/memory/pids bounds — there is no egress at all, not
# even the gateway (exec needs no model calls).
#
# Mounts/dirs:
#   /srv/src  candidate workspace, read-only (bind-mounted artifact dir)
#   /work     writable copy target (mode 1777 — runner copies as uid 0,
#             candidate phases write build artifacts as nobody)
#   /out      result channel — plain root-owned image dir, NOT mounted.
#             exec_result.json is written here AND printed to stdout as a
#             `HEG_EXEC_RESULT <json>` sentinel; the host retrieves the
#             file with `docker cp` after the container exits.

FROM python:3.12-slim

# pytest + coverage are the only exec deps. Pinned major ranges; the image
# is rebuilt per-repo-release so loose pins keep security updates flowing.
RUN pip install --no-cache-dir "pytest>=8,<9" "coverage>=7,<8"

COPY sandbox/images/exec_runner.py /usr/local/bin/heg-exec-runner

# /work is where the runner copies the candidate tree before exec'ing —
# mode 1777 so the runner (uid 0) can copy in and the demoted nobody
# phases can write build artifacts. /out stays root-owned 755: candidate
# phases run as nobody and cannot touch the result channel.
RUN chmod 755 /usr/local/bin/heg-exec-runner \
    && mkdir -p /work /out \
    && chmod 1777 /work

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    COVERAGE_FILE=/tmp/.coverage \
    HOME=/tmp

# The runner must be uid 0 with cap SETUID/SETGID so every candidate
# phase can exec demoted to nobody — ExecRunner always passes
# `--user 0:0 --cap-drop ALL --cap-add SETUID --cap-add SETGID`. The image
# default stays nobody so a bare run hits the runner's euid check and
# exits loudly instead of executing candidate code un-demoted.
USER 65534:65534
WORKDIR /work

ENTRYPOINT ["python", "/usr/local/bin/heg-exec-runner"]
