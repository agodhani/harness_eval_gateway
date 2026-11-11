"""F4 sandbox runtime — Docker lifecycle, images, and specs for agent sandboxes."""

from sandbox.runtime import SandboxRuntime, SnapshotResult, SpawnResult
from sandbox.spec import SandboxSpec

__all__ = ["SandboxRuntime", "SandboxSpec", "SnapshotResult", "SpawnResult"]
