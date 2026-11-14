# Desktop artifact preview image (F12 + PR3) — build context is the REPO ROOT:
#   docker build -f sandbox/images/desktop-preview.Dockerfile \
#       -t heg-preview-desktop:latest .
#
# Executes a candidate GUI artifact on a virtual display (TigerVNC's Xvnc)
# and streams it over noVNC/websockify on :6080. GUI stacks baked in:
# pygame-ce, pyglet, arcade (PR3), plus tkinter via the system `tk` libs
# (the slim python ships the _tkinter module but not libtk8.6.so).
#
# Isolation model: this image runs on the --internal `heg-desktop` network —
# NO egress and NO published ports; the only reachable peer is its own socat
# proxy sidecar (see sandbox/preview.py::_spawn_desktop). Runtime flags add
# --cpus/--memory/--pids-limit, cap-drop ALL, no-new-privileges, nobody.
#
# Dependency note: candidate deps CANNOT be pip-installed at spawn time (the
# internal net has no egress), so the GUI stacks are baked in.
# requirements.txt entries beyond them won't be installed — the app will
# fail at import and the runner exits visibly (logs + GET status "exited").
#
# Env contract (injected by PreviewManager._spawn_desktop):
#   APP_ENTRY      artifact .py entry relative to the staged copy, e.g. main.py
#   VNC_GEOMETRY   optional, default 1024x768
#
# Multi-stage: pymunk (arcade dep) has no aarch64 wheel for the version
# arcade pins, so wheels are built in a gcc stage and installed --no-index —
# the runtime image carries no toolchain.

FROM python:3.12-slim AS wheels
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN pip wheel --no-cache-dir --wheel-dir /wheels \
        websockify==0.12.0 \
        pygame-ce==2.5.3 \
        pyglet==2.1.19 \
        arcade==3.3.3

FROM python:3.12-slim

# TigerVNC bundles the virtual X server + VNC in one (Xvnc). The `novnc`
# Debian package provides the web UI at /usr/share/novnc (vnc.html).
# `tk` gives tkinter its runtime libs; the mesa stack (libgl/libglx/dri)
# + X libs give pyglet/arcade software GL (llvmpipe) under Xvnc.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tigervnc-standalone-server \
        novnc \
        tk \
        libgl1 \
        libglx-mesa0 \
        libgl1-mesa-dri \
        libegl1 \
        libx11-6 \
        libxext6 \
        libxrandr2 \
        libxi6 \
        libxinerama1 \
        libxcursor1 \
        libxxf86vm1 \
        fontconfig \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY --from=wheels /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links /wheels \
        websockify==0.12.0 \
        pygame-ce==2.5.3 \
        pyglet==2.1.19 \
        arcade==3.3.3 \
    && rm -rf /wheels

# /work gets the staged writable copy of the artifact (the bind mount is
# :ro — games often write saves/configs next to the entry); /srv/artifact-ro
# is the read-only mount point. Both owned by nobody so the entrypoint can
# `cp -a` under --user 65534.
RUN mkdir -p /work /srv/artifact-ro && chown -R 65534:65534 /work

COPY sandbox/images/desktop-preview-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV NOVNC_WEB=/usr/share/novnc \
    DISPLAY=:0 \
    PYTHONUNBUFFERED=1

USER 65534:65534
ENTRYPOINT ["/entrypoint.sh"]
