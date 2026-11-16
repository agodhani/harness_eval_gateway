# Thumbnail capture image (PR4) — build context is the REPO ROOT:
#   docker build -f sandbox/images/thumbnail-capture.Dockerfile \
#       -t heg-preview-thumb:latest .
#
# Headless-chromium screenshotter: the container joins the heg-preview
# bridge, loads CAPTURE_URL (a preview runner/proxy by container name),
# and writes a PNG to /out (a scratch dir bind-mounted by the caller).
#
# Threat posture: it renders candidate HTML/JS — the same trust boundary
# as a user's browser opening the preview URL. The artifact's *executed*
# code never runs here; it stays on the zero-egress heg-desktop net. The
# container itself runs nobody + read-only rootfs + a small tmpfs /tmp for
# chromium's profile (runtime flags in sandbox/thumbnails.py).
#
# Env contract (injected by Thumbnailer._run_capture):
#   CAPTURE_URL            required for static/python-server — the
#                          in-network URL chromium screenshots
#   THUMB_VNC_WS           desktop captures bypass chromium entirely:
#                          /vnc-shot.py connects ws://<proxy>:6080/websockify,
#                          waits for real framebuffer content, writes the
#                          screen pixels to /out/capture.png directly
#   THUMB_VNC_WAIT_SECONDS framebuffer-content deadline (default 30)
#   THUMB_VNC_SETTLE_SECONDS min render age before the shot (default 5)
#   THUMB_WIDTH/HEIGHT     viewport, default 1280x800
#   THUMB_BUDGET_MS        chromium --virtual-time-budget (post-load run
#                          time before the shot), default 10000
#   THUMB_TIMEOUT_SECONDS  hard kill bound on chromium, default 45
#   THUMB_READY_SECONDS    in-container wait for CAPTURE_URL to serve,
#                          default 30
#   (output path is fixed — /out/capture.png)
#
# python:3.12-slim base — same as the api + other preview images (typically
# already pulled). `chromium` is the Debian package (headless=new mode);
# fonts-dejavu keeps text rendering deterministic-ish.

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY sandbox/images/thumbnail-capture.sh /capture.sh
COPY sandbox/images/thumbnail-vnc.py /vnc-shot.py
RUN chmod +x /capture.sh /vnc-shot.py

ENV HOME=/tmp \
    PYTHONUNBUFFERED=1

USER 65534:65534
ENTRYPOINT ["/capture.sh"]
