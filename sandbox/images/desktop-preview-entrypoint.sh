#!/bin/sh
# heg-preview-desktop entrypoint — virtual display + noVNC web + run artifact.
#
# Process layout (all inside the runner, on the --internal heg-desktop net):
#   Xvnc :0           virtual display, VNC on localhost:5900 only
#   websockify :6080  serves /usr/share/novnc (vnc.html) + proxies ws -> :5900
#   python /work/...  the candidate app, DISPLAY=:0, cwd = entry's dir
#
# APP_ENTRY is detection-derived (a root-level .py name), never user input.
set -u

# Stage a writable copy — the bind mount is :ro and apps may write saves.
cp -a /srv/artifact-ro/. /work/ 2>/dev/null || true

Xvnc :0 -geometry "${VNC_GEOMETRY:-1024x768}" -depth 24 \
    -rfbport 5900 -localhost -SecurityTypes None >/tmp/xvnc.log 2>&1 &
websockify --web "${NOVNC_WEB:-/usr/share/novnc}" 6080 localhost:5900 \
    >/tmp/websockify.log 2>&1 &

# Let Xvnc come up before the app opens its display.
sleep 1

APP_ENTRY="${APP_ENTRY:-main.py}"
cd "/work/$(dirname "$APP_ENTRY")"
exec python "/work/$APP_ENTRY"
