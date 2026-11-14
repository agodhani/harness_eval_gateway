"""ExecRunner unit tests (PR7) — fake docker plumbing, no daemon.

Covers the docker-run argv (the isolation posture IS the feature), result
parsing (file primary / sentinel fallback, spoof ordering), and the full
``execute`` lifecycle against a stub runtime — timeouts, missing image,
result capture.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from sandbox.exec import (
    CONTAINER_OUT,
    RESULT_FILENAME,
    RESULT_SENTINEL,
    ExecLimits,
    ExecOutcome,
    ExecRunner,
)


class _FakeRuntime:
    """Duck-typed SandboxRuntime for ExecRunner — records docker calls and
    fabricates CompletedProcess results per subcommand."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        run_proc: subprocess.CompletedProcess[str] | None = None,
        run_exc: Exception | None = None,
        image_ok: bool = True,
        docker_ok: bool = True,
        write_result: dict | None = None,
    ) -> None:
        self.artifact_root = tmp_path / "artifacts"
        self.run_proc = run_proc
        self.run_exc = run_exc
        self.image_ok = image_ok
        self.docker_ok = docker_ok
        self.write_result = write_result
        self.calls: list[list[str]] = []

    def to_host_path(self, path: str | Path) -> Path:
        return Path(path)

    def _docker(
        self,
        args: list[str],
        *,
        check: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        if args and args[0] == "info":
            return subprocess.CompletedProcess(
                args, 0 if self.docker_ok else 1, "", ""
            )
        if args and args[0] == "image":
            return subprocess.CompletedProcess(
                args, 0 if self.image_ok else 1, "", ""
            )
        if args and args[0] == "run":
            if self.run_exc is not None:
                raise self.run_exc
            if self.write_result is not None:
                out_dir = self._out_dir_from_args(args)
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / RESULT_FILENAME).write_text(
                    json.dumps(self.write_result)
                )
            return self.run_proc or subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    @staticmethod
    def _out_dir_from_args(args: list[str]) -> Path:
        for i, arg in enumerate(args):
            if arg == "-v" and args[i + 1].endswith(f":{CONTAINER_OUT}"):
                return Path(args[i + 1].rsplit(":", 1)[0])
        raise AssertionError("no /out mount in run args")


def _runner(
    tmp_path: Path, rt: _FakeRuntime, **kwargs: object
) -> ExecRunner:
    return ExecRunner(
        runtime=rt,  # type: ignore[arg-type]
        work_root=tmp_path / "exec-scratch",
        **kwargs,  # type: ignore[arg-type]
    )


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "app.py").write_text("x = 1\n")
    return ws


# ----------------------------------------------------------------------
# run argv — the isolation posture
# ----------------------------------------------------------------------


def test_run_args_lock_down_container(tmp_path: Path) -> None:
    rt = _FakeRuntime(tmp_path)
    runner = _runner(tmp_path, rt, limits=ExecLimits(cpus="2", memory="512m"))
    ws = _ws(tmp_path)
    out_dir = tmp_path / "exec-scratch" / "x" / "out"
    args = runner._build_args(
        name="hegexec-test", workspace=ws, out_dir=out_dir
    )
    joined = " ".join(args)
    assert args[:4] == ["run", "--rm", "--init", "--name"]
    assert "--network none" in joined.replace(" --network ", " --network ")
    assert args[args.index("--network") + 1] == "none"
    assert args[args.index("--user") + 1] == "65534:65534"
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in args
    assert args[args.index("--cpus") + 1] == "2"
    assert args[args.index("--memory") + 1] == "512m"
    assert args[args.index("--pids-limit") + 1] == str(
        runner.limits.pids
    )
    # workspace mounted read-only at /srv/src; /out is the rw result channel
    vols = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
    assert f"{ws}:/srv/src:ro" in vols
    assert any(v.endswith(f":{CONTAINER_OUT}") for v in vols)
    assert args[-1] == runner.image


def test_container_prefix_avoids_heg_namespace() -> None:
    """hegexec-* must not match the heg-* sandbox/eval/preview namespace."""
    from sandbox.exec import CONTAINER_PREFIX

    assert CONTAINER_PREFIX == "hegexec-"
    assert not CONTAINER_PREFIX.startswith("heg-")


# ----------------------------------------------------------------------
# result parsing — file primary, sentinel fallback, spoof ordering
# ----------------------------------------------------------------------


def test_parse_result_prefers_out_file(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    payload = {"version": 1, "phases": {"build": {"ok": True}}}
    (out / RESULT_FILENAME).write_text(json.dumps(payload))
    warnings: list[str] = []
    got = ExecRunner._parse_result(out, "", warnings)
    assert got == payload
    assert warnings == []


def test_parse_result_sentinel_fallback_last_wins(tmp_path: Path) -> None:
    """Candidate-printed sentinels appear BEFORE the real one — last wins."""
    fake = RESULT_SENTINEL + json.dumps({"phases": {"fake": True}})
    real = RESULT_SENTINEL + json.dumps({"version": 1, "phases": {}})
    stdout = f"test output\n{fake}\nmore output\n{real}\n"
    warnings: list[str] = []
    got = ExecRunner._parse_result(tmp_path, stdout, warnings)
    assert got == {"version": 1, "phases": {}}
    assert any("spoofed" in w for w in warnings)


def test_parse_result_no_payload(tmp_path: Path) -> None:
    assert ExecRunner._parse_result(tmp_path, "no marker here", []) is None


def test_parse_result_oversized_file_ignored(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / RESULT_FILENAME).write_bytes(b" " * (1024 * 1024 + 1))
    warnings: list[str] = []
    assert ExecRunner._parse_result(out, "", warnings) is None
    assert any("bytes" in w for w in warnings)


# ----------------------------------------------------------------------
# execute() lifecycle against the fake runtime
# ----------------------------------------------------------------------


def test_execute_happy_path_reads_result_file(tmp_path: Path) -> None:
    payload = {"version": 1, "phases": {"build": {"ok": True}}, "warnings": []}
    rt = _FakeRuntime(
        tmp_path,
        write_result=payload,
        run_proc=subprocess.CompletedProcess([], 0, "pytest out\n", ""),
    )
    runner = _runner(tmp_path, rt)
    outcome = runner.execute(_ws(tmp_path))
    assert outcome.ok is True
    assert outcome.result == payload
    assert outcome.exit_code == 0
    assert outcome.error is None
    assert outcome.container.startswith("hegexec-")
    # scratch work dir cleaned up
    assert not list((tmp_path / "exec-scratch").glob("*"))


def test_execute_sentinel_fallback(tmp_path: Path) -> None:
    payload = {"version": 1, "phases": {}}
    rt = _FakeRuntime(
        tmp_path,
        write_result=None,
        run_proc=subprocess.CompletedProcess(
            [], 0, f"noise\n{RESULT_SENTINEL}{json.dumps(payload)}\n", ""
        ),
    )
    outcome = _runner(tmp_path, rt).execute(_ws(tmp_path))
    assert outcome.ok is True
    assert outcome.result == payload


def test_execute_no_result_is_error(tmp_path: Path) -> None:
    rt = _FakeRuntime(
        tmp_path, run_proc=subprocess.CompletedProcess([], 137, "died", "")
    )
    outcome = _runner(tmp_path, rt).execute(_ws(tmp_path))
    assert outcome.ok is False
    assert outcome.exit_code == 137
    assert "no result payload" in (outcome.error or "")
    assert outcome.unavailable is False


def test_execute_timeout_kills_container(tmp_path: Path) -> None:
    rt = _FakeRuntime(
        tmp_path, run_exc=subprocess.TimeoutExpired(["docker"], 300)
    )
    outcome = _runner(tmp_path, rt).execute(_ws(tmp_path))
    assert outcome.timed_out is True
    assert outcome.ok is False
    assert "killed" in (outcome.error or "")
    rm_calls = [c for c in rt.calls if c[:2] == ["rm", "-f"]]
    assert rm_calls and rm_calls[0][2].startswith("hegexec-")


def test_execute_missing_daemon_is_unavailable(tmp_path: Path) -> None:
    rt = _FakeRuntime(tmp_path, docker_ok=False)
    outcome = _runner(tmp_path, rt).execute(_ws(tmp_path))
    assert outcome.unavailable is True
    assert outcome.ok is False


def test_execute_missing_image_is_unavailable(tmp_path: Path) -> None:
    rt = _FakeRuntime(tmp_path, image_ok=False)
    outcome = _runner(tmp_path, rt).execute(_ws(tmp_path))
    assert outcome.unavailable is True
    assert "exec.Dockerfile" in (outcome.error or "")
    # never attempted a run
    assert not [c for c in rt.calls if c and c[0] == "run"]


def test_outcome_ok_requires_result() -> None:
    outcome = ExecOutcome(image="img")
    assert outcome.ok is False
    outcome.result = {"phases": {}}
    assert outcome.ok is True
