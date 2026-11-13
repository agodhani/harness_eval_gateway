"""Pydantic spec describing one agent sandbox to launch (F4)."""

from pathlib import Path

from pydantic import BaseModel, Field


class SandboxSpec(BaseModel):
    """Everything needed to ``docker run`` one harness sandbox.

    ``workspace_host_path`` is the directory bind-mounted to ``/workspace``
    inside the container — the orchestrator prepares an identical clean
    workspace per sandbox before calling ``spawn()``.
    ``skillpacks_host_path`` optionally overrides the default all-packs
    ``./skillpacks`` mount (per-harness-config subsets are staged into a
    per-sandbox dir by the orchestrator).
    ``cmd`` overrides the image's default CMD (e.g. ``python -m miniloop``);
    ``None`` uses the image default.

    Path fields are expressed **as seen by the spawning process** (the api
    container in compose, the repo checkout when host-run). When the runtime
    was built with ``host_data_root`` (env ``HOST_DATA_ROOT`` — set by
    docker-compose so ``-v`` sources are valid host-daemon paths), paths under
    the data tree are translated before being handed to ``docker run``;
    otherwise they are used verbatim.
    """

    sandbox_id: str
    run_id: str
    image: str = "heg-sandbox:latest"
    env: dict[str, str] = Field(default_factory=dict)
    workspace_host_path: Path
    skillpacks_host_path: Path | None = None
    cmd: list[str] | None = None
