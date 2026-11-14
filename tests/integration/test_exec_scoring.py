"""PR7 exec+coverage integration tests — need a live Docker daemon.

Marked ``docker``; run with ``uv run pytest -m docker tests/integration/test_exec_scoring.py``.

Executes REAL candidate workspaces in the locked ``heg-exec`` container:
zero-egress network, nobody user, resource caps, read-only artifact mount.
The ``heg-exec:latest`` image is built once per session from
``sandbox/images/exec.Dockerfile`` (skips when the build fails, e.g. no
network for pip). Containers are ``hegexec-*`` named and ``--rm`` — nothing
here touches ``heg-*`` run/preview/eval containers.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sandbox.exec import EXEC_IMAGE, ExecRunner
from scoring.collectors import COLLECTORS, collect_signals
from scoring.exec import ExecSandboxCollector
from scoring.models import AnalysisReport
from scoring.static import StaticAnalysisCollector

pytestmark = pytest.mark.docker

REPO_ROOT = Path(__file__).resolve().parents[2]
EXEC_DOCKERFILE = REPO_ROOT / "sandbox" / "images" / "exec.Dockerfile"


def _docker(*args: str, check: bool = False, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=check, timeout=timeout
    )


@pytest.fixture(autouse=True)
def _require_docker() -> None:
    proc = subprocess.run(
        ["docker", "info"], capture_output=True, timeout=30, check=False
    )
    if proc.returncode != 0:
        pytest.skip("docker daemon unavailable")


@pytest.fixture(scope="session")
def exec_image() -> str:
    """Build heg-exec:latest once per session."""
    proc = _docker(
        "build",
        "-f",
        str(EXEC_DOCKERFILE),
        "-t",
        EXEC_IMAGE,
        str(REPO_ROOT),
        timeout=900,
    )
    if proc.returncode != 0:
        pytest.skip(
            f"{EXEC_IMAGE} build failed (pip registry or docker issue):\n"
            f"{proc.stderr[-1500:]}"
        )
    return EXEC_IMAGE


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """A tiny python project: one module + a tests/ dir with 2 passing tests.

    Lives in a ``workspace/`` SUBDIR of tmp_path so the runner's
    ``exec-scratch`` work_root (sibling) never lands inside the artifact.
    """
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\n"
        "def mul(a, b):\n    r = 0\n    for _ in range(b):\n        r += a\n    return r\n"
    )
    tests = ws / "tests"
    tests.mkdir()
    (tests / "test_calc.py").write_text(
        "from calc import add, mul\n\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n\n"
        "def test_mul():\n    assert mul(3, 4) == 12\n"
    )
    return ws


def _collector(tmp_path: Path, image: str) -> ExecSandboxCollector:
    return ExecSandboxCollector(
        enabled=True,
        runner=ExecRunner(
            image=image,
            work_root=tmp_path / "exec-scratch",
        ),
    )


def test_exec_collects_build_tests_coverage(
    workspace: Path, tmp_path: Path, exec_image: str
) -> None:
    """A trivial artifact: build ok, 2/2 tests pass, coverage measured."""
    before = {p.name for p in workspace.iterdir()}
    report = _collector(tmp_path, exec_image).collect(workspace)

    assert report.name == "exec"
    assert report.status == "ok", report.error or report.warnings
    m = report.metrics
    assert m["build_ok"] is True
    assert m["tests_detected"] is True
    assert m["tests_ok"] is True
    assert m["tests_collected"] == 2
    assert m["tests_passed"] == 2
    assert m["tests_failed"] == 0
    assert m["coverage_percent"] is not None
    assert 50.0 <= float(m["coverage_percent"]) <= 100.0
    assert m["timed_out"] is False
    # isolation posture recorded on the report
    assert m["limits"]["network"] == "none"

    # workspace is never modified — :ro mount + /work copy
    assert {p.name for p in workspace.iterdir()} == before
    assert not (workspace / ".coverage").exists()

    # container was --rm'd — nothing named hegexec-* lingers
    leftover = _docker(
        "ps", "-aq", "--filter", "name=^/hegexec-", check=False
    ).stdout.split()
    assert leftover == []


def test_exec_reports_broken_build(tmp_path: Path, exec_image: str) -> None:
    """A syntax-error submission: signal still measures (status ok) with
    build_ok=False — the red build IS the measurement."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "broken.py").write_text("def f(:\n    pass\n")  # SyntaxError
    report = _collector(tmp_path, exec_image).collect(ws)

    assert report.status == "ok", report.error or report.warnings
    m = report.metrics
    assert m["build_ok"] is False
    assert m["build_exit_code"] != 0
    assert m["tests_detected"] is False  # no test files


def test_collect_signals_registry_includes_exec(
    workspace: Path, tmp_path: Path, exec_image: str
) -> None:
    """The default registry produces a second 'exec' SignalReport in the
    same AnalysisReport envelope (SCORING_EXEC is pinned off in conftest,
    so the registry's own collector reports 'skipped' deterministically —
    while an explicitly-enabled collector measures for real)."""
    report = _collector(tmp_path, exec_image).collect(workspace)
    assert report.status == "ok"

    # Full-envelope path: both collectors in one AnalysisReport.
    full = collect_signals(
        workspace,
        collectors=[
            StaticAnalysisCollector(),
            ExecSandboxCollector(
                enabled=True,
                runner=ExecRunner(image=exec_image, work_root=tmp_path / "s2"),
            ),
        ],
    )
    assert isinstance(full, AnalysisReport)
    assert [s.name for s in full.signals] == ["static_analysis", "exec"]
    exec_sig = full.signal("exec")
    assert exec_sig is not None and exec_sig.status == "ok"
    assert exec_sig.metrics["tests_passed"] == 2


def test_default_registry_skips_exec_under_test_env(workspace: Path) -> None:
    """conftest pins SCORING_EXEC=0 — the registry's own exec collector must
    self-skip rather than spawn containers in unmarked unit tests."""
    names = [c.name for c in COLLECTORS]
    assert names == ["static_analysis", "exec"]
    rep = collect_signals(workspace)
    exec_sig = rep.signal("exec")
    assert exec_sig is not None and exec_sig.status == "skipped"


# ----------------------------------------------------------------------
# PR8 — EXEC_TEST_TARGET: external suite runs against the workspace
# ----------------------------------------------------------------------


def test_exec_with_test_target_runs_external_suite(
    workspace: Path, tmp_path: Path, exec_image: str
) -> None:
    """An externally supplied suite (e.g. LLM-generated) mounts read-only
    and runs INSTEAD of workspace tests — the workspace has no test files,
    so autodetect would find nothing; the target suite still runs + passes
    against the candidate module, and coverage attributes to /work."""
    gen = tmp_path / "generated_tests"
    gen.mkdir()
    (gen / "test_gen_calc.py").write_text(
        "from calc import add, mul\n\n\n"
        "def test_add_generated():\n    assert add(2, 3) == 5\n\n\n"
        "def test_mul_generated():\n    assert mul(2, 5) == 10\n"
    )

    runner = ExecRunner(image=exec_image, work_root=tmp_path / "exec-scratch")
    outcome = runner.execute(workspace, test_target=gen)

    assert outcome.ok, outcome.error or outcome.warnings
    result = outcome.result or {}
    assert result.get("test_target")  # mount path recorded in the payload
    phases = result.get("phases") or {}
    tests = phases.get("tests") or {}
    assert tests.get("detected") is True
    assert tests.get("collected") == 2
    assert tests.get("passed") == 2
    assert tests.get("ok") is True
    # coverage measured on the candidate code, not the test suite
    cov = phases.get("coverage") or {}
    assert cov.get("percent") is not None
    assert float(cov["percent"]) >= 80.0
    # the suite mount is never written to (read-only in-container too)
    assert sorted(p.name for p in gen.iterdir()) == ["test_gen_calc.py"]
    assert not (gen / "__pycache__").exists()
    # nothing leaked
    leftover = _docker(
        "ps", "-aq", "--filter", "name=^/hegexec-", check=False
    ).stdout.split()
    assert leftover == []


def test_exec_with_test_target_records_failures(
    workspace: Path, tmp_path: Path, exec_image: str
) -> None:
    """A generated suite that fails reports pass counts + failed names —
    the red suite IS the measurement (status semantics unchanged)."""
    gen = tmp_path / "generated_tests"
    gen.mkdir()
    (gen / "test_gen_calc.py").write_text(
        "from calc import add, mul\n\n\n"
        "def test_add_ok():\n    assert add(1, 1) == 2\n\n\n"
        "def test_mul_wrong():\n    assert mul(2, 2) == 5  # intentionally wrong\n"
    )

    runner = ExecRunner(image=exec_image, work_root=tmp_path / "exec-scratch")
    outcome = runner.execute(workspace, test_target=gen)

    assert outcome.ok, outcome.error
    tests = (outcome.result or {}).get("phases", {}).get("tests") or {}
    assert tests.get("collected") == 2
    assert tests.get("passed") == 1
    assert tests.get("failed") == 1
    assert tests.get("ok") is False
    assert "test_gen_calc::test_mul_wrong" in (tests.get("failed_tests") or [])
