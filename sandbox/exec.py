"""Locked exec sandbox (PR7/PR8) — run candidate build/tests/coverage.

Executes a candidate workspace inside a one-shot ``heg-exec`` container and
returns the measured outcome. Used by ``scoring.exec.ExecSandboxCollector``
(the ``exec`` signal) and by PR8's generated-test-suite runner
(``evaluator.testgen``), which passes ``test_target=`` to point pytest at an
externally supplied suite instead of the workspace's own tests.

Isolation posture (same family as ``sandbox/preview.py``'s desktop runner,
tightened — exec needs NO network at all, not even the gateway):

- ``--network none`` — zero egress; no DNS, no peers, no published ports.
- ``--user 0:0`` + ``--cap-drop ALL`` + ``--cap-add SETUID --cap-add
  SETGID`` + ``no-new-privileges`` — the in-image runner is root ONLY so
  every candidate phase can exec demoted to nobody (65534). The uid split
  is what makes the result channels unforgeable: a same-uid candidate
  could write fake ``HEG_EXEC_RESULT`` lines through ``/proc/*/fd/1`` and
  rewrite ``/out``; a different-uid one can reach neither (and can't
  snoop/swallow the runner's stdout or environ either).
- ``--cpus``/``--memory``/``--pids-limit`` resource bounds.
- ``--tmpfs`` size caps on the only candidate-writable dirs (``/tmp`` and
  ``/work``) so candidate code can't fill the host disk; retried without
  them on daemons that reject the mount opts.
- Candidate workspace bind-mounted **read-only** at ``/srv/src``; the
  in-image runner copies it to the container-local ``/work`` (byte-capped)
  before executing — the artifact on disk is never written to. An optional
  ``test_target`` suite (PR8) mounts read-only at ``/srv/test_target`` and
  is likewise staged to a writable dir by the in-image runner.
- Detached lifecycle — ``docker run -d`` → ``docker wait`` → bounded
  ``docker logs --tail`` → ``docker rm -f``. Container output NEVER
  streams into api memory: an attached ``docker run`` buffers ALL stdout+
  stderr in-process for the whole timeout, so a ``while True: print()``
  candidate could OOM the control plane. Container stdout is also
  runner-only output (phases are captured, demoted code can't reach the
  console fd), so it stays small.
- Result channel: the runner writes ``/out/exec_result.json`` inside the
  container rootfs (root-owned, never mounted — candidate phases can't
  write it at all) and prints a ``HEG_EXEC_RESULT <json>`` sentinel as
  its final stdout line. The sentinel stays authoritative; the file —
  fetched via ``docker cp`` only after the container is confirmed exited —
  is the fallback. Nothing is parsed while the container may still run.
- A forced ``docker rm -f`` always runs — nothing leaks. (No ``--rm``:
  the container must outlive ``docker wait`` so logs are still fetchable.)
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
import re
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
#: Refuse to parse a result file larger than this — /out is root-owned and
#: unreachable by candidate phases, but bound what we read back anyway.
MAX_RESULT_BYTES = 1024 * 1024
#: Merged container output retained on the outcome (for warnings/debugging).
LOG_TAIL_CHARS = 4000
#: ``docker logs --tail N`` bound — how much container output the api ever
#: pulls back. Line-bounded like ``SandboxRuntime.logs``/``monitor.py``; the
#: sentinel is the runner's final line, so it always lands inside the tail.
LOG_TAIL_LINES = 1000
#: Docker label marking exec containers (metadata only — lifecycle is by
#: exact name + explicit rm, never by label sweeps).
LABEL_EXEC = "heg.exec"
#: ``docker run -d`` returns immediately — the spawn cap only guards a wedged
#: daemon/CLI, not the workload (``docker wait`` owns the real timeout).
_SPAWN_TIMEOUT_SECONDS = 120.0
#: Per-call cap on the post-exit ``docker logs``/``docker rm`` fetches.
_DOCKER_CALL_TIMEOUT_SECONDS = 60.0
#: /work tmpfs headroom on top of the workspace copy cap — __pycache__,
#: pytest/.coverage artifacts share the same mount.
_WORK_TMPFS_HEADROOM_BYTES = 64 * 1024 * 1024
#: Stderr hints that a ``docker run`` failure is about the disk-bound mount
#: opts (unsupported on exotic daemons) — triggers the unbounded retry.
_MOUNT_OPT_FAIL_RE = re.compile(
    r"tmpfs|storage-opt|mount option|unknown option|invalid option",
    re.IGNORECASE,
)

#: In-container mount points (see exec.Dockerfile / exec_runner.py).
CONTAINER_SRC = "/srv/src"
CONTAINER_WORK = "/work"
CONTAINER_OUT = "/out"
CONTAINER_TMP = "/tmp"
#: Read-only mount for an external test suite (PR8 ``test_target=``) — the
#: in-image runner copies it to a writable dir and runs pytest against it
#: instead of auto-detecting workspace test files.
CONTAINER_TEST_TARGET = "/srv/test_target"


def _default_exec_image() -> str:
    """Exec image resolution: ``EXEC_IMAGE`` env → api settings → default.

    ``api.config`` is imported lazily and failure-tolerated so this module
    stays importable standalone (unit tests, PR8 tooling).
    """
    env = os.environ.get("EXEC_IMAGE")
    if env:
        return env
    try:
        from api.config import get_settings

        configured = get_settings().EXEC_IMAGE
        if configured:
            return configured
    except Exception:  # standalone use without api config
        logger.debug("EXEC_IMAGE: api settings unavailable", exc_info=True)
    return EXEC_IMAGE


@dataclass(frozen=True)
class ExecLimits:
    """Resource + time bounds for one exec container."""

    cpus: str = "1"
    memory: str = "1g"
    pids: int = 256
    #: Outer docker-wait cap — inner phase timeouts (60+180+60=300) plus
    #: headroom for copy/scan/process startup so the runner can report
    #: inner timeouts as data instead of being killed mid-payload.
    timeout_seconds: float = 420.0
    #: Inner per-phase caps (in-container, env-injected).
    build_timeout_seconds: float = 60.0
    test_timeout_seconds: float = 180.0
    #: Cap on the /srv/src → /work copy.
    max_workspace_bytes: int = 300 * 1024 * 1024
    #: tmpfs size cap for /tmp (coverage data, junit xml, pytest tmp files).
    #: Empty disables the mount — not recommended.
    tmp_size: str = "256m"
    #: tmpfs size cap for /work (the copied candidate tree + build/coverage
    #: artifacts). Empty derives it from ``max_workspace_bytes`` + headroom.
    work_size: str = ""


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
        self.image = image or _default_exec_image()
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

    def _work_tmpfs_size(self) -> str:
        """tmpfs size for /work — copy cap + headroom for build artifacts."""
        if self.limits.work_size:
            return self.limits.work_size
        mib = (
            self.limits.max_workspace_bytes + _WORK_TMPFS_HEADROOM_BYTES
        ) // (1024 * 1024)
        return f"{mib}m"

    def _build_args(
        self,
        *,
        name: str,
        workspace: Path,
        test_target: Path | None = None,
        bounded: bool = True,
    ) -> list[str]:
        """``docker run`` argv for one exec container (testable, no side effects).

        ``bounded=False`` drops the tmpfs disk bounds — the retry path for
        daemons that reject the mount opts (the rest of the isolation stays).
        """
        limits = self.limits
        args = [
            "run",
            # Detached: ``docker wait`` + a bounded ``docker logs --tail``
            # replace streaming stdout — candidate output can't buffer
            # unboundedly in api memory. No ``--rm``: the container must
            # outlive ``docker wait`` so logs stay fetchable; we rm -f.
            "-d",
            "--init",
            "--name",
            name,
            "--network",
            "none",  # zero egress — exec needs no gateway, no DNS, no peers
            "--restart",
            "no",
            # Runner is root ONLY to demote candidate phases to nobody —
            # the uid split keeps /out and /proc/*/fd/1 out of candidate
            # reach, which is what makes the result channels unforgeable.
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "SETUID",
            "--cap-add",
            "SETGID",
            "--security-opt",
            "no-new-privileges",
            "--cpus",
            limits.cpus,
            "--memory",
            limits.memory,
            "--pids-limit",
            str(limits.pids),
        ]
        if bounded:
            # tmpfs size caps on the only candidate-writable dirs — the
            # rootfs and /out are root-owned, so once /tmp and /work are
            # bounded a candidate can't fill the host disk (the copy cap
            # alone doesn't bound caches/coverage/test junk). mode=1777:
            # the runner copies in as uid 0, demoted phases write as nobody.
            if limits.tmp_size:
                args += [
                    "--tmpfs",
                    f"{CONTAINER_TMP}:size={limits.tmp_size},mode=1777",
                ]
            work_size = self._work_tmpfs_size()
            if work_size:
                args += [
                    "--tmpfs",
                    f"{CONTAINER_WORK}:size={work_size},mode=1777",
                ]
        args += [
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
            # No /out mount: the result file stays in the container rootfs
            # (root-owned, unreachable by demoted phases) and comes out via
            # ``docker cp`` once the container has exited.
            "-v",
            f"{self._rt.to_host_path(workspace)}:{CONTAINER_SRC}:ro",
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
    def _sentinel_result(
        stdout: str, warnings: list[str]
    ) -> dict[str, Any] | None:
        """LAST ``HEG_EXEC_RESULT`` line parsed, else None."""
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

    @staticmethod
    def _file_result(
        out_dir: Path, warnings: list[str]
    ) -> dict[str, Any] | None:
        """``/out/exec_result.json`` payload (docker-cp'd) — the fallback."""
        path = out_dir / RESULT_FILENAME
        try:
            if path.is_symlink():
                # Candidate-planted link — never follow it into host files.
                warnings.append("result file is a symlink — ignored")
                return None
            if path.is_file():
                if path.stat().st_size > MAX_RESULT_BYTES:
                    warnings.append(
                        f"result file over {MAX_RESULT_BYTES} bytes — ignored"
                    )
                    return None
                data = json.loads(
                    path.read_text(encoding="utf-8", errors="replace")
                )
                if isinstance(data, dict):
                    return data
                warnings.append("result file is not a JSON object")
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"result file unreadable: {exc}")
        return None

    @staticmethod
    def _parse_result(
        out_dir: Path, stdout: str, warnings: list[str]
    ) -> dict[str, Any] | None:
        """Result payload: LAST stdout sentinel wins; the cp'd file is the
        fallback.

        Both channels are runner-only — candidate phases exec under a
        different uid so they can neither write ``/out`` (root-owned) nor
        print sentinels through ``/proc/*/fd/1`` — and the file is fetched
        via ``docker cp`` only after a confirmed exit. The last-wins and
        disagreement checks stay as belt-and-suspenders.
        """
        sentinel = ExecRunner._sentinel_result(stdout, warnings)
        file_data = ExecRunner._file_result(out_dir, warnings)
        if sentinel is not None:
            if file_data is not None and file_data != sentinel:
                warnings.append(
                    "result file disagrees with the stdout sentinel — "
                    "trusting the sentinel"
                )
            return sentinel
        return file_data

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
        timeout = (
            float(timeout_seconds)
            if timeout_seconds is not None
            else self.limits.timeout_seconds
        )
        started = time.monotonic()
        spawned = False
        try:
            spawned = self._spawn(name, ws, outcome, test_target=target_dir)
        except (OSError, subprocess.SubprocessError) as exc:
            outcome.error = f"docker run failed: {type(exc).__name__}: {exc}"

        if spawned:
            self._wait(name, timeout, outcome)
            # Logs + result still fetchable on a stopped container.
            self._collect(name, work_dir / "out", outcome)
        self._remove(name)
        outcome.duration_seconds = round(time.monotonic() - started, 2)
        # Scratch dir is diagnostic-only once the payload is parsed — drop it.
        shutil.rmtree(work_dir, ignore_errors=True)
        return outcome

    def _spawn(
        self,
        name: str,
        ws: Path,
        outcome: ExecOutcome,
        test_target: Path | None = None,
    ) -> bool:
        """``docker run -d`` the exec container; True when it started.

        On a failure whose stderr points at the tmpfs/mount opts, retries
        once WITHOUT the disk bounds — a degraded-but-working exec beats a
        hard failure on a daemon that doesn't support them.
        """
        args = self._build_args(
            name=name, workspace=ws, test_target=test_target
        )
        proc = self._rt._docker(
            args, check=False, timeout=_SPAWN_TIMEOUT_SECONDS
        )
        if proc.returncode == 0:
            return True
        if "--tmpfs" in args and _MOUNT_OPT_FAIL_RE.search(
            (proc.stderr or "") + (proc.stdout or "")
        ):
            outcome.warnings.append(
                "daemon rejected tmpfs disk bounds — retrying without them"
            )
            args = self._build_args(
                name=name,
                workspace=ws,
                test_target=test_target,
                bounded=False,
            )
            proc = self._rt._docker(
                args, check=False, timeout=_SPAWN_TIMEOUT_SECONDS
            )
            if proc.returncode == 0:
                return True
        detail = ((proc.stderr or "") + " " + (proc.stdout or "")).strip()[-500:]
        outcome.error = f"docker run failed (exit {proc.returncode}): {detail}"
        return False

    def _wait(self, name: str, timeout: float, outcome: ExecOutcome) -> None:
        """``docker wait`` under the outer cap; records exit_code/timed_out."""
        try:
            proc = self._rt._docker(["wait", name], check=False, timeout=timeout)
        except subprocess.TimeoutExpired:
            outcome.timed_out = True
            outcome.error = f"exec container exceeded {timeout:g}s — killed"
            return
        if proc.returncode == 0:
            try:
                outcome.exit_code = int(proc.stdout.strip().splitlines()[-1])
                return
            except (ValueError, IndexError):
                outcome.warnings.append(
                    f"unparseable docker wait output: {proc.stdout.strip()!r}"
                )
        else:
            outcome.warnings.append(
                f"docker wait failed (exit {proc.returncode}): "
                f"{(proc.stderr or '').strip()}"
            )
        running, exit_code = self._inspect_state(name)
        outcome.exit_code = exit_code
        if running is not False:
            # Exit not positively confirmed — the container may still be
            # running, so nothing collectable now is a finished result.
            outcome.timed_out = True
            outcome.error = "exec container exit unconfirmed — killed"

    def _inspect_state(self, name: str) -> tuple[bool | None, int | None]:
        """(running, exit_code) for when ``docker wait``'s output is unusable.

        ``.State.ExitCode`` alone reports 0 for a still-RUNNING container —
        only an explicit ``Running == false`` counts as a confirmed exit.
        """
        try:
            proc = self._rt._docker(
                [
                    "inspect",
                    "-f",
                    "{{.State.Running}}|{{.State.ExitCode}}",
                    name,
                ],
                check=False,
                timeout=30,
            )
            if proc.returncode == 0:
                running_s, _, code_s = proc.stdout.strip().partition("|")
                running = {"true": True, "false": False}.get(running_s)
                try:
                    exit_code: int | None = int(code_s)
                except ValueError:
                    exit_code = None
                return running, exit_code
        except (OSError, subprocess.SubprocessError):
            pass
        return None, None

    def _collect(self, name: str, out_dir: Path, outcome: ExecOutcome) -> None:
        """Bounded ``docker logs --tail`` + result fetch, before removal."""
        stdout_tail = ""
        try:
            proc = self._rt._docker(
                ["logs", "--tail", str(LOG_TAIL_LINES), name],
                check=False,
                timeout=_DOCKER_CALL_TIMEOUT_SECONDS,
            )
            if proc.returncode != 0:
                outcome.warnings.append(
                    f"docker logs failed (exit {proc.returncode}): "
                    f"{(proc.stderr or '').strip()}"
                )
            stdout_tail = proc.stdout or ""
            stderr_tail = proc.stderr or ""
            merged = stdout_tail + (
                ("\n" if stdout_tail and stderr_tail else "") + stderr_tail
            )
            outcome.logs_tail = merged[-LOG_TAIL_CHARS:]
        except (OSError, subprocess.SubprocessError) as exc:
            outcome.warnings.append(
                f"docker logs failed: {type(exc).__name__}: {exc}"
            )

        if outcome.timed_out:
            # The runner emits its result only on completion — anything
            # collectable before a confirmed exit is pre-completion data.
            return
        self._cp_result(name, out_dir, outcome)
        outcome.result = self._parse_result(
            out_dir, stdout_tail, outcome.warnings
        )
        if outcome.result is None and outcome.error is None:
            outcome.error = (
                "exec container produced no result payload "
                f"(exit {outcome.exit_code})"
            )

    def _cp_result(self, name: str, out_dir: Path, outcome: ExecOutcome) -> None:
        """``docker cp`` the result file out of the stopped container.

        The file lives in the container rootfs (root-owned) — cp works on
        stopped containers, so this only ever runs after a confirmed exit.

        The destination is ``out_dir`` VERBATIM — not ``to_host_path``.
        Unlike ``docker run -v`` sources (daemon-resolved), ``docker cp``'s
        local destination is resolved in the CALLING CLIENT's filesystem —
        the CLI streams the archive and extracts where it runs (same rule
        ``SandboxRuntime.snapshot`` documents). Inside the api container
        that means the container-side ``/data/exec/...`` path: it exists
        via the ``./data:/data`` bind mount (created by the mkdir below)
        and the extracted file lands on the host through the same mount.
        Passing the host-translated path fails — ``/Users/...`` isn't
        mounted inside the container (``invalid output path: directory
        ... does not exist``).
        """
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            proc = self._rt._docker(
                [
                    "cp",
                    f"{name}:{CONTAINER_OUT}/{RESULT_FILENAME}",
                    str(out_dir),
                ],
                check=False,
                timeout=_DOCKER_CALL_TIMEOUT_SECONDS,
            )
            if proc.returncode != 0:
                outcome.warnings.append(
                    "docker cp of result file failed "
                    f"(exit {proc.returncode}): {(proc.stderr or '').strip()}"
                )
        except (OSError, subprocess.SubprocessError) as exc:
            outcome.warnings.append(
                f"docker cp of result file failed: {type(exc).__name__}: {exc}"
            )

    def _remove(self, name: str) -> None:
        """Forced ``docker rm -f`` — tolerates an already-absent container."""
        try:
            self._rt._docker(
                ["rm", "-f", name],
                check=False,
                timeout=_DOCKER_CALL_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            logger.warning("exec: docker rm -f %s failed", name, exc_info=True)
