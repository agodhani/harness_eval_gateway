#!/bin/sh
# heg-preview-thumb entrypoint — screenshot CAPTURE_URL to $THUMB_OUT.
#
# Runs chromium headless once and exits; the caller validates the PNG on
# the host side. `--virtual-time-budget` lets page JS/network settle fast
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
LOAD_MS="${THUMB_LOAD_MS:-30000}"
TIMEOUT_S="${THUMB_TIMEOUT_SECONDS:-45}"

echo "[heg-thumb] capturing $CAPTURE_URL -> $OUT (${WIDTH}x${HEIGHT}, budget ${BUDGET}ms)" >&2

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
    --timeout="$LOAD_MS" \
    --screenshot="$OUT" \
    "$CAPTURE_URL"
