"""Pydantic spec describing one agent sandbox to launch (F4)."""

from pathlib import Path

from pydantic import BaseModel, Field


class SandboxSpec(BaseModel):
    """Everything needed to ``docker run`` one harness sandbox.

    ``workspace_host_path`` is the host directory bind-mounted to
    ``/workspace`` inside the container — the orchestrator prepares an
    identical clean workspace per sandbox before calling ``spawn()``.
    ``cmd`` overrides the image's default CMD (e.g. ``python -m miniloop``);
    ``None`` uses the image default.
    """

    sandbox_id: str
    run_id: str
    image: str = "heg-sandbox:latest"
    env: dict[str, str] = Field(default_factory=dict)
    workspace_host_path: Path
    cmd: list[str] | None = None
