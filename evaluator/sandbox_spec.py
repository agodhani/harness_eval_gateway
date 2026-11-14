"""SandboxSpec/env construction for the evaluator sandbox (F7).

The evaluator reuses the miniloop harness as its judge agent: same image
(``heg-sandbox:latest``), same env contract (``adapters.miniloop.adapter.env``),
same tools (read_file / write_file / shell / done). What differs is the
staged workspace (candidate repo + rubric.json + EVAL_PROMPT.md +
ANALYSIS.md/analysis.json when collected) and the task prompt.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from adapters.miniloop import adapter as miniloop_adapter
from evaluator.prompts import EVAL_TASK_PROMPT
from sandbox.runtime import DEFAULT_IMAGE
from sandbox.spec import SandboxSpec

#: In-network gateway endpoint — guaranteed by SandboxRuntime.ensure_network
#: (the gateway container is attached to ``heg-sandbox`` with alias
#: ``gateway``). This is what GATEWAY_BASE_URL must be *inside* the sandbox.
SANDBOX_GATEWAY_URL = "http://gateway:8080"

#: Hostnames that only resolve on the HOST/control-plane side. A caller that
#: passes e.g. ``http://localhost:8081`` (host-run dev GATEWAY_URL) would
#: strand the sandbox — it has no route to the host — so these are rewritten
#: to the canonical in-network endpoint.
_LOCAL_HOSTS = {
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
    "host.docker.internal",
}

#: Default iteration budget for the judge — enough to list files, read the
#: rubric + key sources, write scores.json, verify, and call done.
DEFAULT_EVAL_MAX_ITERS = 30


def sandbox_gateway_url(gateway_base_url: str) -> str:
    """Map a control-plane gateway URL to the sandbox-reachable one.

    Anything already naming a non-local host (e.g. ``http://gateway:8080``
    from compose ``GATEWAY_URL``) passes through verbatim. Localhost-style
    hosts are rewritten to :data:`SANDBOX_GATEWAY_URL` because sandboxes sit
    on the egress-locked ``heg-sandbox`` network where only the ``gateway``
    alias exists.
    """
    url = (gateway_base_url or "").strip().rstrip("/") or SANDBOX_GATEWAY_URL
    host = (urlparse(url).hostname or "").lower()
    if host in _LOCAL_HOSTS:
        return SANDBOX_GATEWAY_URL
    return url


def eval_sandbox_id(sandbox_id: object) -> str:
    """Container/state id for the eval sandbox of participant ``sandbox_id``.

    Prefixed ``eval-`` so the docker name (``heg-eval-<uuid>``) can never
    collide with the participant's ``heg-<uuid>`` container or state file.
    """
    return f"eval-{sandbox_id}"


def build_eval_env(
    *,
    gateway_base_url: str,
    cred: str,
    model: str,
    max_iters: int = DEFAULT_EVAL_MAX_ITERS,
    max_tokens: int | None = None,
) -> dict[str, str]:
    """Env vars for the eval sandbox, via the miniloop adapter contract.

    ``EVENTS_INGEST_URL`` points at the gateway's ``/internal/events`` — the
    same egress path participant sandboxes use (cred auth stamps run/sandbox
    ids; each per-eval cred carries the sandbox under evaluation, so judge
    events land on that sandbox's timeline).

    ``max_tokens`` raises the judge's per-request output cap when set —
    thinking judges burn completion tokens before emitting tool calls, so
    the 4096 miniloop default can truncate scores.json into invalid JSON.
    """
    gw = sandbox_gateway_url(gateway_base_url)
    return miniloop_adapter.env(
        gateway_url=gw,
        cred=cred,
        task_prompt=EVAL_TASK_PROMPT,
        model=model,
        events_ingest_url=f"{gw}/internal/events",
        max_iters=max_iters,
        max_tokens=max_tokens,
    )


def build_eval_spec(
    *,
    sandbox_id: str,
    run_id: str,
    eval_workspace: Path,
    env: dict[str, str],
    image: str = DEFAULT_IMAGE,
) -> SandboxSpec:
    """SandboxSpec for one eval run.

    ``eval_workspace`` (host path) is bind-mounted at ``/workspace`` — it is a
    throwaway copy, so agent writes (scores.json) landing there are exactly
    what we want. ``cmd=None`` uses the image default (``python -m miniloop``).
    """
    return SandboxSpec(
        sandbox_id=sandbox_id,
        run_id=run_id,
        image=image,
        env=env,
        workspace_host_path=Path(eval_workspace),
        cmd=None,
    )
