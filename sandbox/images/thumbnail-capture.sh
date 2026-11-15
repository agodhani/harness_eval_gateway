#!/bin/sh
# heg-preview-thumb entrypoint — screenshot CAPTURE_URL to $THUMB_OUT.
#
# Polls CAPTURE_URL until the preview stack serves, then runs chromium
# headless once and exits; the caller validates the PNG on the host
# side. The readiness wait lives in-container: the preview's published
# port binds the HOST's loopback, which a containerized api can't
# reach — this container shares the preview network, so its view of
# CAPTURE_URL is exactly what chromium loads.
# `--virtual-time-budget` lets page JS/network settle fast
# (it fast-forwards timers while pausing on pending loads); the outer
# `timeout` is the hard bound — a hung page still yields exit 124 and the
# api treats a missing/invalid PNG as a capture failure.
#
# --no-sandbox is required (container runs unprivileged, no user
# namespaces); --disable-dev-shm-usage routes shared memory to /tmp
# (bind-mounted tmpfs) since docker's /dev/shm is tiny.
set -u

: "${CAPTURE_URL:?CAPTURE_URL is required}"
OUT="${THUMB_OUT:-/out/capture.png}"
WIDTH="${THUMB_WIDTH:-1280}"
HEIGHT="${THUMB_HEIGHT:-800}"
BUDGET="${THUMB_BUDGET_MS:-10000}"
READY_S="${THUMB_READY_SECONDS:-30}"
TIMEOUT_S="${THUMB_TIMEOUT_SECONDS:-45}"

echo "[heg-thumb] capturing $CAPTURE_URL -> $OUT (${WIDTH}x${HEIGHT}, budget ${BUDGET}ms)" >&2

# Any HTTP response — even an error page — proves the chain is serving.
deadline=$(( $(date +%s) + READY_S ))
until python3 - "$CAPTURE_URL" <<'PY'
import sys
import urllib.error
import urllib.request

try:
    urllib.request.urlopen(sys.argv[1], timeout=2.0)
except urllib.error.HTTPError:
    pass  # HTTP response of any status = the chain is up
except Exception:
    sys.exit(1)  # refused / unresolvable / timed out — not up yet
PY
do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "[heg-thumb] $CAPTURE_URL did not serve within ${READY_S}s" >&2
        exit 124
    fi
    sleep 0.5
done

exec timeout "$TIMEOUT_S" chromium \
    --headless=new \
    --no-sandbox \
    --disable-gpu \
    --disable-dev-shm-usage \
    --disable-extensions \
    --disable-background-networking \
    --disable-sync \
    --disable-component-update \
    --mute-audio \
    --no-first-run \
    --no-default-browser-check \
    --user-data-dir=/tmp/chrome-profile \
    --hide-scrollbars \
    --force-device-scale-factor=1 \
    --window-size="${WIDTH},${HEIGHT}" \
    --virtual-time-budget="$BUDGET" \
    --screenshot="$OUT" \
    "$CAPTURE_URL"
