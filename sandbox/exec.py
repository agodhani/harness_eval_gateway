"""Locked exec sandbox (PR7/PR8) — run candidate build/tests/coverage.

Executes a candidate workspace inside a one-shot ``heg-exec`` container and
returns the measured outcome. Used by ``scoring.exec.ExecSandboxCollector``
(the ``exec`` signal) and by PR8's generated-test-suite runner
(``evaluator.testgen``), which passes ``test_target=`` to point pytest at an
externally supplied suite instead of the workspace's own tests.

Isolation posture (same family as ``sandbox/preview.py``'s desktop runner,
tightened — exec needs NO network at all, not even the gateway):

- ``--network none`` — zero egress; no DNS, no peers, no published ports.
- ``--user 65534:65534`` + ``--cap-drop ALL`` + ``no-new-privileges``.
- ``--cpus``/``--memory``/``--pids-limit`` resource bounds.
- Candidate workspace bind-mounted **read-only** at ``/srv/src``; the
  in-image runner copies it to the container-local ``/work`` (byte-capped)
  before executing — the artifact on disk is never written to.
- Result channel: a scratch dir mounted at ``/out`` (``exec_result.json``)
  with a ``HEG_EXEC_RESULT <json>`` stdout-sentinel fallback; the last
  sentinel wins so candidate stdout can't spoof the payload.
- ``--rm`` + a forced ``docker rm -f`` on timeout — nothing leaks.
- Per-phase inner timeouts (compile/test) inside the runner plus an outer
  docker timeout here.

Container names use the ``hegexec-`` prefix — deliberately OUTSIDE the
``heg-*`` sandbox/eval/preview name space, so exec cleanup can never touch
live run containers, and ops filters can tell them apart at a glance.

Synchronous like :class:`sandbox.runtime.SandboxRuntime`; async callers wrap
in ``asyncio.to_thread`` (the evaluator already does for signal collection).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sandbox.runtime import SandboxRuntime

logger = logging.getLogger(__name__)

EXEC_IMAGE = "heg-exec:latest"
#: Container name prefix — see module docstring for why it avoids ``heg-*``.
CONTAINER_PREFIX = "hegexec-"
RESULT_FILENAME = "exec_result.json"
RESULT_SENTINEL = "HEG_EXEC_RESULT "
#: Refuse to parse a result file larger than this — a candidate can write to
#: /out, so bound what we read back.
MAX_RESULT_BYTES = 1024 * 1024
#: Merged container output retained on the outcome (for warnings/debugging).
LOG_TAIL_CHARS = 4000
#: Docker label marking exec containers (metadata only — lifecycle is by
#: exact name + --rm, never by label sweeps).
LABEL_EXEC = "heg.exec"

#: In-container mount points (see exec.Dockerfile / exec_runner.py).
CONTAINER_SRC = "/srv/src"
CONTAINER_OUT = "/out"
#: Read-only mount for an external test suite (PR8 ``test_target=``) — the
#: in-image runner copies it to a writable dir and runs pytest against it
#: instead of auto-detecting workspace test files.
CONTAINER_TEST_TARGET = "/srv/test_target"


@dataclass(frozen=True)
class ExecLimits:
    """Resource + time bounds for one exec container."""

    cpus: str = "1"
    memory: str = "1g"
    pids: int = 256
    #: Outer docker-run cap — must exceed the inner phase timeouts.
    timeout_seconds: float = 300.0
    #: Inner per-phase caps (in-container, env-injected).
    build_timeout_seconds: float = 60.0
    test_timeout_seconds: float = 180.0
    #: Cap on the /srv/src → /work copy.
    max_workspace_bytes: int = 300 * 1024 * 1024


@dataclass
class ExecOutcome:
    """What one :meth:`ExecRunner.execute` call produced.

    ``result`` is the parsed runner payload (``{"phases": {...}}``) when the
    container completed and reported — present even when tests FAIL (a red
    suite is a valid measurement, not an infra error). ``error`` is set on
    infrastructure failure (spawn failure, no result payload, timeout kill);
    ``unavailable`` marks "can't run at all" (no daemon, image not built)
    so callers can report ``skipped`` instead of ``error``.
    """

    image: str
    container: str = ""
    result: dict[str, Any] | None = None
    error: str | None = None
    unavailable: bool = False
    timed_out: bool = False
    exit_code: int | None = None
    duration_seconds: float = 0.0
    logs_tail: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when a result payload was captured (says nothing about the
        candidate's build/tests passing — read ``result["phases"]``)."""
        return self.result is not None


class ExecRunner:
    """Docker-backed runner for exec scoring.

    Composes :class:`SandboxRuntime` for the docker-CLI plumbing and
    ``to_host_path`` bind-source translation (compose-mode api: work dirs
    must live under the data tree — ``work_root`` defaults to
    ``<artifact_root>/../exec``).

    ``execute`` is the whole lifecycle: check image → docker run → wait →
    parse result → cleanup. The workspace is never modified.
    """

    def __init__(
        self,
        *,
        runtime: SandboxRuntime | None = None,
        image: str | None = None,
        limits: ExecLimits | None = None,
        work_root: str | Path | None = None,
        container_prefix: str = CONTAINER_PREFIX,
    ) -> None:
        self._rt = runtime or SandboxRuntime()
        self.image = image or os.environ.get("EXEC_IMAGE") or EXEC_IMAGE
        self.limits = limits or ExecLimits()
        self.container_prefix = container_prefix
        if work_root is not None:
            self.work_root = Path(work_root)
        else:
            # <data>/exec — sibling of artifacts/, inside the host-path
            # translation roots so bind sources resolve in compose mode.
            self.work_root = Path(self._rt.artifact_root).parent / "exec"
        self._docker_ok: bool | None = None

    # -- availability probes ------------------------------------------------

    def docker_available(self) -> bool:
        """Cached ``docker info`` — a missing daemon is 'unavailable'."""
        if self._docker_ok is None:
            try:
                proc = self._rt._docker(["info"], check=False, timeout=30)
                self._docker_ok = proc.returncode == 0
            except (OSError, subprocess.SubprocessError):
                self._docker_ok = False
        return self._docker_ok

    def image_available(self) -> bool:
        """True when the exec image exists locally (never pulls)."""
        try:
            proc = self._rt._docker(
                ["image", "inspect", self.image], check=False, timeout=30
            )
            return proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    # -- docker args ---------------------------------------------------------

    def _build_args(
        self,
        *,
        name: str,
        workspace: Path,
        out_dir: Path,
        test_target: Path | None = None,
    ) -> list[str]:
        """``docker run`` argv for one exec container (testable, no side effects)."""
        limits = self.limits
        args = [
            "run",
            "--rm",
            "--init",
            "--name",
            name,
            "--network",
            "none",  # zero egress — exec needs no gateway, no DNS, no peers
            "--restart",
            "no",
            "--user",
            "65534:65534",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--cpus",
            limits.cpus,
            "--memory",
            limits.memory,
            "--pids-limit",
            str(limits.pids),
            "-e",
            "HOME=/tmp",
            "-e",
            f"EXEC_BUILD_TIMEOUT_SECONDS={limits.build_timeout_seconds:g}",
            "-e",
            f"EXEC_TEST_TIMEOUT_SECONDS={limits.test_timeout_seconds:g}",
            "-e",
            f"EXEC_MAX_WORKSPACE_BYTES={limits.max_workspace_bytes}",
            "--label",
            f"{LABEL_EXEC}=1",
            "--workdir",
            "/work",
            # The candidate tree is READ-ONLY — the in-image runner copies it
            # to /work before executing, so the artifact is never written to.
            "-v",
            f"{self._rt.to_host_path(workspace)}:{CONTAINER_SRC}:ro",
            "-v",
            f"{self._rt.to_host_path(out_dir)}:{CONTAINER_OUT}",
        ]
        if test_target is not None:
            # PR8: an externally supplied suite (e.g. generated_tests) —
            # mounted read-only like /srv/src; EXEC_TEST_TARGET switches the
            # in-image runner's pytest phase from workspace autodetect to
            # this directory. Coverage stays --source=/work (candidate code).
            args += [
                "-e",
                f"EXEC_TEST_TARGET={CONTAINER_TEST_TARGET}",
                "-v",
                f"{self._rt.to_host_path(test_target)}:{CONTAINER_TEST_TARGET}:ro",
            ]
        args.append(self.image)
        return args

    # -- result parsing -------------------------------------------------------

    @staticmethod
    def _parse_result(
        out_dir: Path, stdout: str, warnings: list[str]
    ) -> dict[str, Any] | None:
        """Result payload from /out file, else the last stdout sentinel."""
        path = out_dir / RESULT_FILENAME
        try:
            if path.is_file():
                if path.stat().st_size > MAX_RESULT_BYTES:
                    warnings.append(
                        f"result file over {MAX_RESULT_BYTES} bytes — ignored"
                    )
                else:
                    data = json.loads(
                        path.read_text(encoding="utf-8", errors="replace")
                    )
                    if isinstance(data, dict):
                        return data
                    warnings.append("result file is not a JSON object")
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"result file unreadable: {exc}")

        # Fallback: LAST sentinel line wins — the runner prints it after all
        # phases, so candidate stdout can never spoof a later one.
        sentinel_lines = [
            ln for ln in stdout.splitlines() if ln.startswith(RESULT_SENTINEL)
        ]
        for line in reversed(sentinel_lines):
            try:
                data = json.loads(line[len(RESULT_SENTINEL) :])
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                if len(sentinel_lines) > 1:
                    warnings.append(
                        f"{len(sentinel_lines) - 1} earlier result "
                        "sentinel(s) ignored (candidate-spoofed or rerun)"
                    )
                return data
        return None

    # -- lifecycle -------------------------------------------------------------

    def execute(
        self,
        workspace: Path | str,
        *,
        timeout_seconds: float | None = None,
        test_target: Path | str | None = None,
    ) -> ExecOutcome:
        """Run the exec container over ``workspace`` (never modifies it).

        ``test_target`` (PR8) is a host directory holding an externally
        supplied pytest suite — mounted read-only at
        :data:`CONTAINER_TEST_TARGET` and run INSTEAD of the workspace's own
        detected tests. A non-directory target is a caller bug: fail fast on
        the outcome rather than let ``docker -v`` auto-create a stray dir.

        Never raises for docker problems — failures land on the outcome so a
        missing daemon/image can't break evaluation.
        """
        ws = Path(workspace).resolve()
        outcome = ExecOutcome(image=self.image)
        target_dir: Path | None = None
        if test_target is not None:
            target_dir = Path(test_target).resolve()
            if not target_dir.is_dir():
                outcome.error = f"exec test target is not a directory: {target_dir}"
                return outcome
        if not self.docker_available():
            outcome.error = "docker daemon unavailable"
            outcome.unavailable = True
            return outcome
        if not self.image_available():
            outcome.error = (
                f"exec image {self.image!r} not found — build it: docker build "
                f"-f sandbox/images/exec.Dockerfile -t {self.image} ."
            )
            outcome.unavailable = True
            return outcome

        name = f"{self.container_prefix}{uuid.uuid4().hex[:12]}"
        outcome.container = name
        work_dir = self.work_root / name
        out_dir = work_dir / "out"
        timeout = (
            float(timeout_seconds)
            if timeout_seconds is not None
            else self.limits.timeout_seconds
        )
        started = time.monotonic()
        proc: subprocess.CompletedProcess[str] | None = None
        try:
            out_dir.mkdir(parents=True, mode=0o777)
            out_dir.chmod(0o777)  # nobody inside the container must write here
            args = self._build_args(
                name=name, workspace=ws, out_dir=out_dir, test_target=target_dir
            )
            proc = self._rt._docker(args, check=False, timeout=timeout)
        except subprocess.TimeoutExpired:
            outcome.timed_out = True
            outcome.error = f"exec container exceeded {timeout:g}s — killed"
            self._rt._docker(["rm", "-f", name], check=False, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            outcome.error = f"docker run failed: {type(exc).__name__}: {exc}"
        finally:
            outcome.duration_seconds = round(time.monotonic() - started, 2)

        if proc is not None:
            outcome.exit_code = proc.returncode
            merged = (proc.stdout or "") + ("\n" if proc.stdout else "") + (
                proc.stderr or ""
            )
            outcome.logs_tail = merged[-LOG_TAIL_CHARS:]
            outcome.result = self._parse_result(
                out_dir, proc.stdout or "", outcome.warnings
            )
            if outcome.result is None:
                outcome.error = (
                    "exec container produced no result payload "
                    f"(exit {proc.returncode})"
                )
        # Scratch dir is diagnostic-only once the payload is parsed — drop it.
        shutil.rmtree(work_dir, ignore_errors=True)
        return outcome
