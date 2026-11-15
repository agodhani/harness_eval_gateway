"""ExecRunner unit tests (PR7) — fake docker plumbing, no daemon.

Covers the docker-run argv (the isolation posture IS the feature — detached
``run -d``, root-runner + demoted phases, tmpfs disk bounds, caps), result
parsing (sentinel primary / file fallback, spoof ordering, symlink guard),
and the full detached ``execute`` lifecycle against a stub runtime — run →
wait → bounded logs → docker cp → rm — including timeouts, missing image,
the unconfirmed-exit gate, and the no-unbounded-buffer contract (api never
sees more than ``docker logs --tail N``).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from sandbox.exec import (
    CONTAINER_TMP,
    CONTAINER_WORK,
    LOG_TAIL_CHARS,
    LOG_TAIL_LINES,
    RESULT_FILENAME,
    RESULT_SENTINEL,
    ExecLimits,
    ExecOutcome,
    ExecRunner,
)


def _cp(
    args: list[str], rc: int = 0, out: str = "", err: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, rc, out, err)


class _FakeRuntime:
    """Duck-typed SandboxRuntime for ExecRunner — records docker calls and
    fabricates CompletedProcess results per subcommand for the detached
    run → wait → logs → rm lifecycle."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        run_proc: subprocess.CompletedProcess[str] | None = None,
        run_exc: Exception | None = None,
        run_procs: list[subprocess.CompletedProcess[str]] | None = None,
        wait_proc: subprocess.CompletedProcess[str] | None = None,
        wait_exc: Exception | None = None,
        logs_proc: subprocess.CompletedProcess[str] | None = None,
        inspect_proc: subprocess.CompletedProcess[str] | None = None,
        image_ok: bool = True,
        docker_ok: bool = True,
        write_result: dict | None = None,
    ) -> None:
        self.artifact_root = tmp_path / "artifacts"
        self.run_proc = run_proc
        self.run_exc = run_exc
        #: Queue for sequential ``run`` results (tmpfs-retry coverage);
        #: falls back to ``run_proc``.
        self.run_procs = list(run_procs or [])
        self.wait_proc = wait_proc
        self.wait_exc = wait_exc
        self.logs_proc = logs_proc
        self.inspect_proc = inspect_proc
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
        head = args[0] if args else ""
        if head == "info":
            return _cp(args, 0 if self.docker_ok else 1)
        if head == "image":
            return _cp(args, 0 if self.image_ok else 1)
        if head == "run":
            if self.run_exc is not None:
                raise self.run_exc
            if self.run_procs:
                return self.run_procs.pop(0)
            return self.run_proc or _cp(args, 0, "containerid\n")
        if head == "wait":
            if self.wait_exc is not None:
                raise self.wait_exc
            return self.wait_proc or _cp(args, 0, "0\n")
        if head == "logs":
            return self.logs_proc or _cp(args, 0)
        if head == "cp":
            # args: cp <name>:/out/exec_result.json <dest_dir>
            if self.write_result is None:
                return _cp(args, 1, err="No such container:path")
            dest = Path(args[-1])
            dest.mkdir(parents=True, exist_ok=True)
            (dest / RESULT_FILENAME).write_text(json.dumps(self.write_result))
            return _cp(args, 0)
        if head == "inspect":
            return self.inspect_proc or _cp(args, 0, "false|0\n")
        # rm / anything else — succeed quietly
        return _cp(args, 0)


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
    args = runner._build_args(name="hegexec-test", workspace=ws)
    # Detached, no --rm: wait+logs replace streaming stdout; we rm -f later.
    assert args[:4] == ["run", "-d", "--init", "--name"]
    assert "--rm" not in args
    assert args[args.index("--network") + 1] == "none"
    # Runner is uid 0 only to demote candidate phases to nobody — the uid
    # split is what keeps /out and /proc/*/fd/1 out of candidate reach.
    assert args[args.index("--user") + 1] == "0:0"
    assert args[args.index("--cap-drop") + 1] == "ALL"
    cap_adds = [args[i + 1] for i, a in enumerate(args) if a == "--cap-add"]
    assert cap_adds == ["SETUID", "SETGID"]
    assert "no-new-privileges" in args
    assert args[args.index("--cpus") + 1] == "2"
    assert args[args.index("--memory") + 1] == "512m"
    assert args[args.index("--pids-limit") + 1] == str(
        runner.limits.pids
    )
    # workspace mounted read-only at /srv/src — the ONLY mount; /out is a
    # root-owned rootfs dir (no candidate-writable bind mount exists).
    vols = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
    assert vols == [f"{ws}:/srv/src:ro"]
    assert args[-1] == runner.image


def test_run_args_bound_disk_writes(tmp_path: Path) -> None:
    """tmpfs size caps ride the run argv so candidate writes stay bounded."""
    rt = _FakeRuntime(tmp_path)
    runner = _runner(tmp_path, rt)
    args = runner._build_args(name="hegexec-x", workspace=_ws(tmp_path))
    tmpfs = [args[i + 1] for i, a in enumerate(args) if a == "--tmpfs"]
    assert f"{CONTAINER_TMP}:size=256m,mode=1777" in tmpfs
    # /work derives from the 300MiB copy cap + headroom, world-writable so
    # the root runner can copy in and demoted phases can write artifacts.
    work = [t for t in tmpfs if t.startswith(f"{CONTAINER_WORK}:")]
    assert work and "mode=1777" in work[0]


def test_run_args_unbounded_drops_tmpfs(tmp_path: Path) -> None:
    """bounded=False is the graceful-degrade retry path."""
    rt = _FakeRuntime(tmp_path)
    runner = _runner(tmp_path, rt)
    args = runner._build_args(
        name="hegexec-x",
        workspace=_ws(tmp_path),
        bounded=False,
    )
    assert "--tmpfs" not in args


def test_container_prefix_avoids_heg_namespace() -> None:
    """hegexec-* must not match the heg-* sandbox/eval/preview namespace."""
    from sandbox.exec import CONTAINER_PREFIX

    assert CONTAINER_PREFIX == "hegexec-"
    assert not CONTAINER_PREFIX.startswith("heg-")


def test_run_args_with_test_target(tmp_path: Path) -> None:
    """PR8: test_target adds a read-only suite mount + EXEC_TEST_TARGET env;
    the candidate workspace stays untouched and the image stays last."""
    from sandbox.exec import CONTAINER_TEST_TARGET

    rt = _FakeRuntime(tmp_path)
    runner = _runner(tmp_path, rt)
    ws = _ws(tmp_path)
    target = tmp_path / "generated_tests"
    target.mkdir()
    args = runner._build_args(
        name="hegexec-test", workspace=ws, test_target=target
    )
    vols = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
    assert f"{ws}:/srv/src:ro" in vols
    assert f"{target}:{CONTAINER_TEST_TARGET}:ro" in vols
    envs = [args[i + 1] for i, a in enumerate(args) if a == "-e"]
    assert f"EXEC_TEST_TARGET={CONTAINER_TEST_TARGET}" in envs
    # base args unchanged: still network-less, image still the last token
    assert args[args.index("--network") + 1] == "none"
    assert args[-1] == runner.image


def test_run_args_without_test_target_has_no_mount(tmp_path: Path) -> None:
    rt = _FakeRuntime(tmp_path)
    runner = _runner(tmp_path, rt)
    ws = _ws(tmp_path)
    args = runner._build_args(name="hegexec-test", workspace=ws)
    assert not any("EXEC_TEST_TARGET" in a for a in args)
    vols = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
    assert vols == [f"{ws}:/srv/src:ro"]  # /srv/src only — /out is not mounted


def test_execute_non_dir_test_target_fails_fast(tmp_path: Path) -> None:
    """A bogus target must not reach docker run — ``-v`` would silently
    auto-create the missing source dir on the host."""
    rt = _FakeRuntime(tmp_path)
    runner = _runner(tmp_path, rt)
    outcome = runner.execute(
        _ws(tmp_path), test_target=tmp_path / "does-not-exist"
    )
    assert outcome.ok is False
    assert "not a directory" in (outcome.error or "")
    assert not [c for c in rt.calls if c and c[0] == "run"]


def test_execute_with_test_target_passes_mount(tmp_path: Path) -> None:
    payload = {"version": 1, "phases": {}, "warnings": []}
    rt = _FakeRuntime(
        tmp_path,
        write_result=payload,
        run_proc=subprocess.CompletedProcess([], 0, "", ""),
    )
    runner = _runner(tmp_path, rt)
    target = tmp_path / "generated_tests"
    target.mkdir()
    outcome = runner.execute(_ws(tmp_path), test_target=target)
    assert outcome.ok is True
    run_args = next(c for c in rt.calls if c and c[0] == "run")
    assert any("EXEC_TEST_TARGET" in a for a in run_args)
    assert any(f"{target}" in a and ":ro" in a for a in run_args)


# ----------------------------------------------------------------------
# result parsing — sentinel primary, file fallback, spoof ordering
# ----------------------------------------------------------------------


def test_parse_result_sentinel_primary(tmp_path: Path) -> None:
    """Sentinel + file agreeing → sentinel returned, no disagreement warn."""
    out = tmp_path / "out"
    out.mkdir()
    payload = {"version": 1, "phases": {"build": {"ok": True}}}
    (out / RESULT_FILENAME).write_text(json.dumps(payload))
    stdout = f"noise\n{RESULT_SENTINEL}{json.dumps(payload)}\n"
    warnings: list[str] = []
    assert ExecRunner._parse_result(out, stdout, warnings) == payload
    assert warnings == []


def test_parse_result_sentinel_beats_conflicting_file(tmp_path: Path) -> None:
    """/out is candidate-writable — a planted file can't outrank the
    runner's final stdout sentinel."""
    out = tmp_path / "out"
    out.mkdir()
    fake = {"version": 1, "phases": {"build": {"ok": True}}, "planted": True}
    real = {"version": 1, "phases": {"build": {"ok": False}}}
    (out / RESULT_FILENAME).write_text(json.dumps(fake))
    stdout = f"{RESULT_SENTINEL}{json.dumps(real)}\n"
    warnings: list[str] = []
    assert ExecRunner._parse_result(out, stdout, warnings) == real
    assert any("disagrees" in w for w in warnings)


def test_parse_result_file_fallback(tmp_path: Path) -> None:
    """No sentinel (stdout lost) → the result file still carries the run."""
    out = tmp_path / "out"
    out.mkdir()
    payload = {"version": 1, "phases": {"build": {"ok": True}}}
    (out / RESULT_FILENAME).write_text(json.dumps(payload))
    warnings: list[str] = []
    assert ExecRunner._parse_result(out, "", warnings) == payload
    assert warnings == []


def test_parse_result_sentinel_last_wins(tmp_path: Path) -> None:
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


def test_parse_result_symlink_file_ignored(tmp_path: Path) -> None:
    """A candidate-planted symlink must not be followed into host files."""
    out = tmp_path / "out"
    out.mkdir()
    secret = tmp_path / "host_secret.json"
    secret.write_text(json.dumps({"phases": {"pwned": True}}))
    (out / RESULT_FILENAME).symlink_to(secret)
    warnings: list[str] = []
    assert ExecRunner._parse_result(out, "", warnings) is None
    assert any("symlink" in w for w in warnings)


# ----------------------------------------------------------------------
# execute() — detached lifecycle against the fake runtime
# ----------------------------------------------------------------------


def test_execute_happy_path_reads_result_file(tmp_path: Path) -> None:
    payload = {"version": 1, "phases": {"build": {"ok": True}}, "warnings": []}
    rt = _FakeRuntime(tmp_path, write_result=payload)
    runner = _runner(tmp_path, rt)
    outcome = runner.execute(_ws(tmp_path))
    assert outcome.ok is True
    assert outcome.result == payload
    assert outcome.exit_code == 0
    assert outcome.error is None
    assert outcome.container.startswith("hegexec-")
    # detached lifecycle: run -d → wait → logs → cp → rm -f, in order
    heads = [c[0] for c in rt.calls if c and c[0] != "info"]
    assert heads == ["image", "run", "wait", "logs", "cp", "rm"]
    run_call = next(c for c in rt.calls if c[0] == "run")
    assert "-d" in run_call
    wait_call = next(c for c in rt.calls if c[0] == "wait")
    assert wait_call[-1] == outcome.container
    rm_call = next(c for c in rt.calls if c[0] == "rm")
    assert rm_call[:2] == ["rm", "-f"] and rm_call[2] == outcome.container
    # scratch work dir cleaned up
    assert not list((tmp_path / "exec-scratch").glob("*"))


def test_execute_reads_sentinel_from_log_tail(tmp_path: Path) -> None:
    payload = {"version": 1, "phases": {}}
    rt = _FakeRuntime(
        tmp_path,
        write_result=None,
        logs_proc=_cp(
            [], 0, f"noise\n{RESULT_SENTINEL}{json.dumps(payload)}\n"
        ),
    )
    outcome = _runner(tmp_path, rt).execute(_ws(tmp_path))
    assert outcome.ok is True
    assert outcome.result == payload


def test_execute_no_result_is_error(tmp_path: Path) -> None:
    rt = _FakeRuntime(
        tmp_path,
        wait_proc=_cp([], 0, "137\n"),
        logs_proc=_cp([], 0, "died\n"),
    )
    outcome = _runner(tmp_path, rt).execute(_ws(tmp_path))
    assert outcome.ok is False
    assert outcome.exit_code == 137
    assert "no result payload" in (outcome.error or "")
    assert outcome.unavailable is False


def test_execute_timeout_kills_container(tmp_path: Path) -> None:
    rt = _FakeRuntime(
        tmp_path,
        wait_exc=subprocess.TimeoutExpired(["docker"], 300),
        logs_proc=_cp([], 0, "partial output\n"),
    )
    outcome = _runner(tmp_path, rt).execute(_ws(tmp_path))
    assert outcome.timed_out is True
    assert outcome.ok is False
    assert "killed" in (outcome.error or "")
    # logs still fetched (diagnostics) BEFORE the forced remove — but no
    # result fetch: nothing collectable while running is a real result
    heads = [c[0] for c in rt.calls if c and c[0] != "info"]
    assert heads == ["image", "run", "wait", "logs", "rm"]
    rm_call = next(c for c in rt.calls if c[0] == "rm")
    assert rm_call[:2] == ["rm", "-f"] and rm_call[2].startswith("hegexec-")
    assert outcome.logs_tail.rstrip().endswith("partial output")


def test_execute_timeout_ignores_planted_result_file(tmp_path: Path) -> None:
    """A result file present mid-run is never trusted — the result is only
    fetched (docker cp) after a confirmed exit, and a timeout skips it."""
    rt = _FakeRuntime(
        tmp_path,
        wait_exc=subprocess.TimeoutExpired(["docker"], 300),
        write_result={"version": 1, "phases": {"planted": True}},
    )
    outcome = _runner(tmp_path, rt).execute(_ws(tmp_path))
    assert outcome.timed_out is True
    assert outcome.result is None
    assert not [c for c in rt.calls if c[0] == "cp"]


def test_execute_wait_failure_still_running_distrusts_result(
    tmp_path: Path,
) -> None:
    """``docker wait`` failing while the container still runs must not
    trust collectable state — inspect's Running flag gates the fetch."""
    rt = _FakeRuntime(
        tmp_path,
        wait_proc=_cp([], 1, err="wait exploded"),
        inspect_proc=_cp([], 0, "true|0\n"),
        write_result={"version": 1, "phases": {"planted": True}},
        logs_proc=_cp([], 0, f"{RESULT_SENTINEL}{json.dumps({'x': 1})}\n"),
    )
    outcome = _runner(tmp_path, rt).execute(_ws(tmp_path))
    assert outcome.timed_out is True
    assert outcome.result is None
    assert outcome.error
    assert not [c for c in rt.calls if c[0] == "cp"]
    rm_call = next(c for c in rt.calls if c[0] == "rm")
    assert rm_call[:2] == ["rm", "-f"]


def test_execute_wait_failure_confirmed_exit_uses_inspect(
    tmp_path: Path,
) -> None:
    """wait-failure + Running=false → parse normally, exit code from inspect."""
    payload = {"version": 1, "phases": {"build": {"ok": True}}}
    rt = _FakeRuntime(
        tmp_path,
        wait_proc=_cp([], 1, err="wait exploded"),
        inspect_proc=_cp([], 0, "false|7\n"),
        write_result=payload,
    )
    outcome = _runner(tmp_path, rt).execute(_ws(tmp_path))
    assert outcome.timed_out is False
    assert outcome.exit_code == 7
    assert outcome.result == payload


def test_execute_never_buffers_container_output(tmp_path: Path) -> None:
    """The api-side bound: run is detached and logs are tail-capped — a
    print-loop candidate can't stream GBs into process memory."""
    rt = _FakeRuntime(
        tmp_path,
        logs_proc=_cp([], 0, "spam\n" * 100_000),  # ~500KB even in the fake
    )
    outcome = _runner(tmp_path, rt).execute(_ws(tmp_path))
    logs_call = next(c for c in rt.calls if c[0] == "logs")
    assert "--tail" in logs_call
    assert logs_call[logs_call.index("--tail") + 1] == str(LOG_TAIL_LINES)
    assert len(outcome.logs_tail) <= LOG_TAIL_CHARS
    # every docker call in the flow is a bounded one-shot — no attached
    # `docker run` streaming exists anymore
    assert all(c[0] != "run" or "-d" in c for c in rt.calls)


def test_execute_spawn_retries_without_disk_bounds(tmp_path: Path) -> None:
    """A daemon that rejects tmpfs opts gets one unbounded retry."""
    payload = {"version": 1, "phases": {}}
    rt = _FakeRuntime(
        tmp_path,
        run_procs=[
            _cp(
                [],
                125,
                err="docker: Error response from daemon: invalid mount "
                "option tmpfs size",
            ),
            _cp([], 0, "containerid\n"),
        ],
        write_result=payload,
    )
    outcome = _runner(tmp_path, rt).execute(_ws(tmp_path))
    run_calls = [c for c in rt.calls if c[0] == "run"]
    assert len(run_calls) == 2
    assert "--tmpfs" in run_calls[0]
    assert "--tmpfs" not in run_calls[1]
    assert outcome.ok is True
    assert any("tmpfs" in w for w in outcome.warnings)


def test_execute_spawn_failure_is_error(tmp_path: Path) -> None:
    rt = _FakeRuntime(
        tmp_path, run_proc=_cp([], 1, err="some other docker error")
    )
    outcome = _runner(tmp_path, rt).execute(_ws(tmp_path))
    assert outcome.ok is False
    assert "docker run failed" in (outcome.error or "")
    # no retry without bounds — the failure wasn't mount-opt related
    run_calls = [c for c in rt.calls if c[0] == "run"]
    assert len(run_calls) == 1
    # rm -f still ran (defensive cleanup of a possibly-created container)
    assert [c for c in rt.calls if c[:2] == ["rm", "-f"]]


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
