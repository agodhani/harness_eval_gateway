"""Sandbox runtime — Docker lifecycle for agent sandboxes (F4).

Every sandbox container joins the internal ``heg-sandbox`` bridge network
(``docker network create --internal`` => no external egress). The only
reachable service is the AI gateway, which ``ensure_network()`` attaches to
that network with the DNS alias ``gateway`` — sandboxes reach it at
``http://gateway:8080``. See AGENTS.md / FEATURE_MAP.md (F4).

Typical orchestrator flow::

    rt = SandboxRuntime()
    rt.ensure_network()               # idempotent; spawn() calls it too
    rt.spawn(spec)                    # -> running container ``heg-<sandbox_id>``
    ...                               # agent runs; all model traffic via gateway
    rt.stop(sid, remove=False)        # stop but keep the container
    snap = rt.snapshot(sid, run_id)   # docker cp + logs + meta.json
    rt.remove(sid)

``stop()`` defaults to ``remove=True`` (``docker stop`` + ``docker rm``);
pass ``remove=False`` when ``snapshot()`` still needs the container —
``docker cp`` and ``docker logs`` require it to exist (stopped is fine).

This module owns container lifecycle plus on-disk debug/artifact state only.
Run/sandbox/artifact DB rows belong to the orchestrator — ``snapshot()``
returns paths, it does not write DB rows.

Synchronous on purpose: the ``docker`` CLI is sync and orchestration calls
are infrequent; wrap in ``asyncio.to_thread`` if a caller needs async.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sandbox.spec import SandboxSpec

logger = logging.getLogger(__name__)

NETWORK_NAME = "heg-sandbox"
CONTAINER_PREFIX = "heg-"
GATEWAY_ALIAS = "gateway"
GATEWAY_PORT = 8080
DEFAULT_IMAGE = "heg-sandbox:latest"
DEFAULT_GATEWAY_CONTAINER = "harness_eval_gateway-gateway-1"

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOCKER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
_SENSITIVE_ENV_RE = re.compile(r"cred|key|token|secret|pass", re.IGNORECASE)

# Vendored skill packs are bind-mounted read-only into every sandbox at
# SKILLPACKS_CONTAINER_ROOT so harnesses (miniloop's SKILLPACKS_ROOT default)
# find them without baking the packs into the image.
_SKILLPACKS_HOST_DIR = _REPO_ROOT / "skillpacks"
SKILLPACKS_CONTAINER_ROOT = "/opt/skillpacks"

# Injected into every sandbox unless the spec overrides them — they are
# infrastructure constants of the locked-down network, not harness config.
_DEFAULT_ENV = {
    "GATEWAY_BASE_URL": f"http://{GATEWAY_ALIAS}:{GATEWAY_PORT}",
    "WORKSPACE": "/workspace",
    "SKILLPACKS_ROOT": SKILLPACKS_CONTAINER_ROOT,
}


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _redact_env(env: dict[str, str]) -> dict[str, str]:
    """Mask values that look like credentials before writing state files."""
    return {
        key: ("***" if _SENSITIVE_ENV_RE.search(key) else value)
        for key, value in env.items()
    }


@dataclass(frozen=True)
class SpawnResult:
    """Handle returned by :meth:`SandboxRuntime.spawn`."""

    sandbox_id: str
    run_id: str
    container_id: str
    container_name: str
    image: str


@dataclass(frozen=True)
class SnapshotResult:
    """Artifact paths produced by :meth:`SandboxRuntime.snapshot`."""

    sandbox_id: str
    run_id: str
    artifact_dir: Path
    workspace_dir: Path
    transcript_path: Path
    meta_path: Path


class SandboxRuntime:
    """Docker-backed lifecycle for agent sandboxes (see module docstring)."""

    def __init__(
        self,
        *,
        network: str = NETWORK_NAME,
        gateway_container: str | None = None,
        artifact_root: str | Path | None = None,
        state_dir: str | Path | None = None,
    ) -> None:
        self.network = network
        self.gateway_container = (
            gateway_container
            or os.environ.get("GATEWAY_CONTAINER")
            or DEFAULT_GATEWAY_CONTAINER
        )
        raw_root = artifact_root if artifact_root is not None else os.environ.get(
            "ARTIFACT_ROOT"
        )
        root = Path(raw_root) if raw_root else _REPO_ROOT / "data" / "artifacts"
        self.artifact_root = (
            root if root.is_absolute() else _REPO_ROOT / root
        ).resolve()
        if state_dir is not None:
            raw_state = Path(state_dir)
            self.state_dir = (
                raw_state if raw_state.is_absolute() else _REPO_ROOT / raw_state
            ).resolve()
        else:
            # data/sandboxes/ — sibling of data/artifacts/.
            self.state_dir = self.artifact_root.parent / "sandboxes"

    # ------------------------------------------------------------------
    # docker CLI plumbing
    # ------------------------------------------------------------------

    def _docker(
        self,
        args: list[str],
        *,
        check: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if check and proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip()
            raise RuntimeError(
                f"docker {' '.join(args)} failed (exit {proc.returncode}): {detail}"
            )
        return proc

    @staticmethod
    def container_name(sandbox_id: str) -> str:
        """Docker container name for a sandbox id (``heg-<sandbox_id>``)."""
        name = f"{CONTAINER_PREFIX}{sandbox_id}"
        if not _DOCKER_NAME_RE.match(name):
            raise ValueError(
                f"sandbox_id {sandbox_id!r} produces invalid container name {name!r}"
            )
        return name

    def _container_exists(self, name: str) -> bool:
        return self._docker(["inspect", name], check=False).returncode == 0

    def _container_inspect(self, name: str) -> dict[str, Any] | None:
        proc = self._docker(["inspect", name], check=False)
        if proc.returncode != 0:
            return None
        try:
            data = json.loads(proc.stdout)
            return data[0] if data else None
        except (json.JSONDecodeError, IndexError, KeyError):
            return None

    # ------------------------------------------------------------------
    # network
    # ------------------------------------------------------------------

    def ensure_network(self) -> dict[str, Any]:
        """Create the internal sandbox network and attach the gateway (idempotent).

        - Creates ``heg-sandbox`` with ``--internal`` (no external egress) if
          missing. If it already exists but is *not* internal, logs a loud
          warning — that silently breaks the egress-lockdown guarantee.
        - Connects ``self.gateway_container`` (env ``GATEWAY_CONTAINER``,
          default ``harness_eval_gateway-gateway-1``) to the network with
          alias ``gateway`` so sandboxes resolve ``http://gateway:8080``.
          Tolerates the gateway container not existing yet (another lane may
          not have built it) — logs a warning and continues.
        """
        proc = self._docker(["network", "inspect", self.network], check=False)
        created = False
        if proc.returncode != 0:
            self._docker(
                [
                    "network",
                    "create",
                    "--internal",
                    "--driver",
                    "bridge",
                    self.network,
                ]
            )
            created = True
            logger.info("created internal docker network %r", self.network)
        else:
            try:
                info = json.loads(proc.stdout)[0]
            except (json.JSONDecodeError, IndexError):
                info = {}
            if not info.get("Internal", False):
                logger.warning(
                    "docker network %r exists but is NOT internal — "
                    "sandboxes on it will have external egress!",
                    self.network,
                )

        gateway_attached = self._attach_gateway()
        return {
            "network": self.network,
            "created": created,
            "gateway_container": self.gateway_container,
            "gateway_attached": gateway_attached,
        }

    def _attach_gateway(self) -> bool:
        info = self._container_inspect(self.gateway_container)
        if info is None:
            logger.warning(
                "gateway container %r not found — cannot attach it to %r; "
                "sandboxes will have no model route until it exists",
                self.gateway_container,
                self.network,
            )
            return False
        networks = info.get("NetworkSettings", {}).get("Networks", {})
        endpoint = networks.get(self.network)
        if endpoint is not None:
            aliases = set(endpoint.get("Aliases") or [])
            if GATEWAY_ALIAS in aliases:
                return True
            # Attached but without the `gateway` alias — reconnect to fix it.
            logger.info(
                "gateway container %r is on %r without alias %r — reconnecting",
                self.gateway_container,
                self.network,
                GATEWAY_ALIAS,
            )
            self._docker(
                [
                    "network",
                    "disconnect",
                    "-f",
                    self.network,
                    self.gateway_container,
                ]
            )
        self._docker(
            [
                "network",
                "connect",
                "--alias",
                GATEWAY_ALIAS,
                self.network,
                self.gateway_container,
            ]
        )
        logger.info(
            "attached gateway container %r to %r as %r",
            self.gateway_container,
            self.network,
            GATEWAY_ALIAS,
        )
        return True

    def attached_containers(self) -> list[str]:
        """Names of running containers currently attached to the sandbox network."""
        proc = self._docker(["network", "inspect", self.network], check=False)
        if proc.returncode != 0:
            return []
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return []
        containers = (data[0].get("Containers") or {}) if data else {}
        return sorted(
            str(entry.get("Name", "")).lstrip("/") for entry in containers.values()
        )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def spawn(self, spec: SandboxSpec) -> SpawnResult:
        """``docker run -d`` one sandbox container on the internal network.

        Raises ``RuntimeError`` if a container named ``heg-<sandbox_id>``
        already exists — the orchestrator owns uniqueness. Creates
        ``workspace_host_path`` if missing and writes a debug state file to
        ``state_dir/<sandbox_id>.json`` (env values that look like
        credentials are redacted).
        """
        self.ensure_network()
        name = self.container_name(spec.sandbox_id)
        if self._container_exists(name):
            raise RuntimeError(
                f"refusing to spawn: container {name!r} already exists "
                "(orchestrator must use unique sandbox_ids)"
            )

        ws = Path(spec.workspace_host_path)
        ws.mkdir(parents=True, exist_ok=True)
        ws = ws.resolve()

        env = {**_DEFAULT_ENV, **spec.env}
        args = [
            "run",
            "-d",
            "--name",
            name,
            "--network",
            self.network,
            "--restart",
            "no",
            "--workdir",
            "/workspace",
            "--label",
            f"heg.run_id={spec.run_id}",
            "--label",
            f"heg.sandbox_id={spec.sandbox_id}",
            "--label",
            f"heg.image={spec.image}",
        ]
        for key, value in env.items():
            args += ["-e", f"{key}={value}"]
        args += ["-v", f"{ws}:/workspace"]
        # Read-only skill packs mount — skipped (debug-logged) if the host
        # dir is absent so sandboxes still spawn on a stripped checkout.
        if _SKILLPACKS_HOST_DIR.is_dir():
            args += [
                "-v",
                f"{_SKILLPACKS_HOST_DIR}:{SKILLPACKS_CONTAINER_ROOT}:ro",
            ]
        else:
            logger.debug(
                "skillpacks dir %r not found — skipping %s mount",
                _SKILLPACKS_HOST_DIR,
                SKILLPACKS_CONTAINER_ROOT,
            )
        args += [spec.image]
        if spec.cmd:
            args += [str(part) for part in spec.cmd]

        proc = self._docker(args, timeout=180)
        container_id = proc.stdout.strip()
        started_at = _utcnow_iso()
        self._write_state(spec, name, container_id, env, started_at)
        logger.info(
            "spawned sandbox %r as %r (image=%s, run=%s)",
            spec.sandbox_id,
            name,
            spec.image,
            spec.run_id,
        )
        return SpawnResult(
            sandbox_id=spec.sandbox_id,
            run_id=spec.run_id,
            container_id=container_id,
            container_name=name,
            image=spec.image,
        )

    def _write_state(
        self,
        spec: SandboxSpec,
        container_name: str,
        container_id: str,
        env: dict[str, str],
        started_at: str,
    ) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "sandbox_id": spec.sandbox_id,
            "run_id": spec.run_id,
            "container": container_name,
            "container_id": container_id,
            "image": spec.image,
            "env": _redact_env(env),
            "workspace_host_path": str(Path(spec.workspace_host_path).resolve()),
            "started_at": started_at,
        }
        path = self.state_dir / f"{spec.sandbox_id}.json"
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def exec(
        self,
        sandbox_id: str,
        cmd: list[str],
        *,
        check: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``cmd`` (argv, no shell) inside a RUNNING sandbox container."""
        name = self.container_name(sandbox_id)
        return self._docker(
            ["exec", name, *[str(part) for part in cmd]],
            check=check,
            timeout=timeout,
        )

    def status(self, sandbox_id: str) -> dict[str, Any]:
        """Container state: ``status`` (running/exited/...), ``exit_code``, ``running``."""
        name = self.container_name(sandbox_id)
        proc = self._docker(
            [
                "inspect",
                "-f",
                "{{.State.Status}}|{{.State.ExitCode}}|{{.State.Running}}",
                name,
            ],
            check=False,
        )
        base = {"sandbox_id": sandbox_id, "container": name}
        if proc.returncode != 0:
            return {**base, "status": "missing", "exit_code": None, "running": False}
        parts = proc.stdout.strip().split("|")
        state = parts[0] if parts and parts[0] else "unknown"
        try:
            exit_code = int(parts[1]) if len(parts) > 1 and parts[1] else None
        except ValueError:
            exit_code = None
        running = len(parts) > 2 and parts[2] == "true"
        return {
            **base,
            "status": state,
            "exit_code": exit_code,
            "running": running,
        }

    def stop(self, sandbox_id: str, *, remove: bool = True, timeout: int = 10) -> None:
        """``docker stop`` the sandbox; ``docker rm`` it too unless ``remove=False``.

        Tolerates an already-absent container (logs a warning).
        """
        name = self.container_name(sandbox_id)
        if not self._container_exists(name):
            logger.warning("stop: container %r not found", name)
            return
        self._docker(["stop", "--time", str(timeout), name], timeout=timeout + 30)
        if remove:
            self.remove(sandbox_id)

    def remove(self, sandbox_id: str) -> None:
        """``docker rm -f`` the sandbox container (tolerates absence)."""
        name = self.container_name(sandbox_id)
        if not self._container_exists(name):
            logger.warning("remove: container %r not found", name)
            return
        self._docker(["rm", "-f", name])

    # ------------------------------------------------------------------
    # artifacts
    # ------------------------------------------------------------------

    def snapshot(self, sandbox_id: str, run_id: str) -> SnapshotResult:
        """Capture workspace + logs + meta into ``artifact_root/<run>/<sandbox>/``.

        Layout produced::

            <artifact_root>/<run_id>/<sandbox_id>/workspace/   # docker cp of /workspace
            <artifact_root>/<run_id>/<sandbox_id>/transcript.log  # docker logs (stdout+stderr)
            <artifact_root>/<run_id>/<sandbox_id>/meta.json       # ids, status, exit_code, finished_at

        The container must still exist — stopped is fine, removed is not —
        so call this BEFORE ``stop()``/``remove()`` (or use
        ``stop(remove=False)``). DB artifact rows are the orchestrator's job.
        """
        name = self.container_name(sandbox_id)
        if not self._container_exists(name):
            raise RuntimeError(
                f"cannot snapshot {name!r}: container does not exist "
                "(already removed?)"
            )

        artifact_dir = self.artifact_root / run_id / sandbox_id
        workspace_dir = artifact_dir / "workspace"
        transcript_path = artifact_dir / "transcript.log"
        meta_path = artifact_dir / "meta.json"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        # docker cp nests if the destination dir already exists — start clean
        # so repeat snapshots stay idempotent.
        if workspace_dir.exists():
            shutil.rmtree(workspace_dir)

        self._docker(["cp", f"{name}:/workspace", str(workspace_dir)], timeout=300)

        # docker logs writes container stdout->CLI stdout, stderr->CLI stderr;
        # STDERR->STDOUT merges them into the transcript in arrival order.
        with transcript_path.open("wb") as fh:
            proc = subprocess.run(
                ["docker", "logs", name],
                stdout=fh,
                stderr=subprocess.STDOUT,
                check=False,
            )
        if proc.returncode != 0:
            raise RuntimeError(
                f"docker logs {name} failed (exit {proc.returncode}); "
                f"partial output in {transcript_path}"
            )

        inspect = self._docker(
            [
                "inspect",
                "-f",
                "{{.Id}}|{{.State.Status}}|{{.State.ExitCode}}",
                name,
            ]
        )
        parts = inspect.stdout.strip().split("|")
        container_id = parts[0] if parts else ""
        c_status = parts[1] if len(parts) > 1 else "unknown"
        try:
            exit_code = int(parts[2]) if len(parts) > 2 and parts[2] else None
        except ValueError:
            exit_code = None

        meta = {
            "sandbox_id": sandbox_id,
            "run_id": run_id,
            "container": name,
            "container_id": container_id,
            "status": c_status,
            "exit_code": exit_code,
            "finished_at": _utcnow_iso(),
        }
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

        logger.info(
            "snapshot of %r written to %s", sandbox_id, artifact_dir
        )
        return SnapshotResult(
            sandbox_id=sandbox_id,
            run_id=run_id,
            artifact_dir=artifact_dir,
            workspace_dir=workspace_dir,
            transcript_path=transcript_path,
            meta_path=meta_path,
        )
