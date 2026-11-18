"""Test-suite generation signal tests (PR8) — fake gateway + fake exec runner.

Hermetic: the gateway call is a stub async client, the exec run is a canned
ExecOutcome — no LLM, no docker. Pins the contract: skip/error semantics,
deterministic containment of bad model output, provenance layout
(``<artifact>/generated_tests/`` + ``_manifest.json``), and the metrics the
judge brief + results API read.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from evaluator.testgen import (
    GENERATED_TESTS_DIRNAME,
    MANIFEST_FILENAME,
    generate_tests_signal,
    parse_test_files,
)
from sandbox.exec import ExecOutcome

RUBRIC = {
    "summary": "Build a calculator module.",
    "criteria": [
        {"id": "add", "text": "add(a, b) returns the sum", "dimension": "functional_requirements"}
    ],
}

GOOD_OUTPUT = """\
### FILE: test_calc.py
```python
from calc import add


def test_add():
    assert add(1, 2) == 3
```

### FILE: test_more.py
import pytest


def test_marker():
    assert True
"""

SINGLE_FILE_OUTPUT = """\
```python
from calc import add


def test_single():
    assert add(2, 2) == 4
```
"""


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    """Duck-typed httpx.AsyncClient — records calls, returns canned text.

    ``texts`` queues per-call entries for the raised-cap retry path:
    ``str`` → assistant content, ``Exception`` → raised, ``dict`` → the
    whole response payload (e.g. ``{"choices": []}``). Falls back to
    ``text`` once the queue is exhausted.
    """

    def __init__(
        self,
        text: str = GOOD_OUTPUT,
        exc: Exception | None = None,
        texts: list[Any] | None = None,
    ) -> None:
        self._text = text
        self._exc = exc
        self._texts = list(texts) if texts else []
        self.calls: list[dict[str, Any]] = []

    async def post(
        self,
        url: str,
        json: dict | None = None,
        headers: dict | None = None,
        **kwargs: Any,
    ) -> _FakeResponse:
        self.calls.append(
            {"url": url, "json": json, "headers": headers, **kwargs}
        )
        if self._exc is not None:
            raise self._exc
        entry: Any = self._text
        if self._texts:
            entry = self._texts.pop(0)
        if isinstance(entry, Exception):
            raise entry
        if isinstance(entry, dict):
            return _FakeResponse(entry)
        return _FakeResponse({"choices": [{"message": {"content": entry}}]})

    async def aclose(self) -> None:
        return None


class _FakeRunner:
    """Returns a canned ExecOutcome; records workspace + test_target."""

    def __init__(self, outcome: ExecOutcome) -> None:
        self.outcome = outcome
        self.seen: Path | None = None
        self.target: Path | None = None

    def execute(
        self, workspace: Path, *, test_target: Path | None = None, **_kw: object
    ) -> ExecOutcome:
        self.seen = workspace
        self.target = test_target
        return self.outcome


def _exec_payload(tests: dict | None = None, coverage: dict | None = None) -> dict:
    return {
        "version": 1,
        "language": "python",
        "test_target": "/srv/test_target",
        "phases": {
            "copy": {"ok": True, "bytes": 100, "files": 2, "truncated": False},
            "scan": {"python_files": 1},
            "build": {"ok": True, "exit_code": 0, "timed_out": False, "duration_seconds": 0.3},
            "tests": tests,
            "coverage": coverage,
        },
        "warnings": [],
    }


def _ok_outcome(payload: dict | None = None) -> ExecOutcome:
    return ExecOutcome(
        image="heg-exec:latest",
        container="hegexec-gen1",
        result=_exec_payload() if payload is None else payload,
        exit_code=0,
        duration_seconds=2.5,
    )


@pytest.fixture()
def python_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    return ws


@pytest.fixture()
def gen_dir(tmp_path: Path) -> Path:
    return tmp_path / "artifact" / GENERATED_TESTS_DIRNAME


async def _gen(
    ws: Path,
    gen_dir: Path,
    *,
    client: _FakeClient | None = None,
    runner: _FakeRunner | None = None,
    **kwargs: Any,
):
    kwargs.setdefault("enabled", True)
    return await generate_tests_signal(
        workspace=ws,
        gen_dir=gen_dir,
        gateway_base_url="http://gw.test:8080",
        cred="heg.testcred",
        model="eval-model",
        rubric=RUBRIC,
        client=client or _FakeClient(),
        runner=runner or _FakeRunner(_ok_outcome()),
        **kwargs,
    )


# ----------------------------------------------------------------------
# skip / decline rules
# ----------------------------------------------------------------------


async def test_disabled_skips(python_ws: Path, gen_dir: Path) -> None:
    report = await _gen(python_ws, gen_dir, enabled=False)
    assert report.name == "generated_tests"
    assert report.status == "skipped"
    assert "disabled" in report.warnings[0]
    assert not gen_dir.exists()


async def test_env_flag_disables(python_ws: Path, gen_dir: Path, monkeypatch) -> None:
    monkeypatch.setenv("SCORING_TESTGEN", "off")
    report = await generate_tests_signal(
        workspace=python_ws,
        gen_dir=gen_dir,
        gateway_base_url="http://gw.test",
        cred="x",
        client=_FakeClient(),
    )
    assert report.status == "skipped"


async def test_non_python_workspace_skips(tmp_path: Path, gen_dir: Path) -> None:
    (tmp_path / "index.html").write_text("<html></html>")
    report = await _gen(tmp_path, gen_dir)
    assert report.status == "skipped"
    assert "no python sources" in report.warnings[0]


async def test_missing_workspace_skips(tmp_path: Path, gen_dir: Path) -> None:
    report = await _gen(tmp_path / "nope", gen_dir)
    assert report.status == "skipped"


async def test_missing_cred_skips(python_ws: Path, gen_dir: Path) -> None:
    report = await generate_tests_signal(
        workspace=python_ws,
        gen_dir=gen_dir,
        gateway_base_url="http://gw.test",
        cred="",
        enabled=True,
        client=_FakeClient(),
    )
    assert report.status == "skipped"
    assert "credential" in report.warnings[0]


# ----------------------------------------------------------------------
# generation call + deterministic containment
# ----------------------------------------------------------------------


async def test_gateway_call_contract(python_ws: Path, gen_dir: Path) -> None:
    client = _FakeClient()
    await _gen(python_ws, gen_dir, client=client)
    call = client.calls[0]
    assert call["url"] == "http://gw.test:8080/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer heg.testcred"
    body = call["json"]
    assert body["model"] == "eval-model"
    assert body["stream"] is False
    msgs = body["messages"]
    assert msgs[0]["role"] == "system"
    # rubric context + source listing land in the user prompt
    assert "Build a calculator" in msgs[1]["content"]
    assert "calc.py" in msgs[1]["content"]


async def test_model_env_override(python_ws: Path, gen_dir: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESTGEN_MODEL", "gen-model-x")
    client = _FakeClient()
    await _gen(python_ws, gen_dir, client=client)
    assert client.calls[0]["json"]["model"] == "gen-model-x"


async def test_http_failure_is_error(python_ws: Path, gen_dir: Path) -> None:
    client = _FakeClient(exc=httpx.ConnectError("refused"))
    report = await _gen(python_ws, gen_dir, client=client)
    assert report.status == "error"
    assert "generation call failed" in (report.error or "")
    assert not gen_dir.exists()


async def test_empty_choices_is_error(python_ws: Path, gen_dir: Path) -> None:
    class EmptyClient(_FakeClient):
        async def post(self, url, json=None, headers=None, **_kw):
            return _FakeResponse({"choices": []})

    report = await _gen(python_ws, gen_dir, client=EmptyClient())
    assert report.status == "error"


async def test_garbage_output_skips_without_crashing(
    python_ws: Path, gen_dir: Path
) -> None:
    """Prose/markdown-rant output must degrade to skipped — never raise."""
    client = _FakeClient(text="I cannot write tests for this code. Sorry!")
    report = await _gen(python_ws, gen_dir, client=client)
    assert report.status == "skipped"
    assert "no runnable test code" in report.warnings[-1]
    assert report.metrics["files_rejected"] >= 1


async def test_partial_output_salvages_valid_files(
    python_ws: Path, gen_dir: Path
) -> None:
    mixed = (
        "### FILE: test_ok.py\n"
        "def test_ok():\n    assert True\n"
        "### FILE: test_bad.py\n"
        "def test_bad(:\n    syntax error\n"
        "### FILE: notatest.py\n"
        "x = 1  # no tests here\n"
    )
    runner = _FakeRunner(_ok_outcome())
    report = await _gen(python_ws, gen_dir, client=_FakeClient(text=mixed), runner=runner)
    assert report.status == "ok"
    assert report.metrics["files_written"] == 1
    assert report.metrics["files_rejected"] == 2
    written = {p.name for p in gen_dir.glob("test_*.py")}
    assert written == {"test_ok.py"}


# ----------------------------------------------------------------------
# raised-cap retry — thinking/slow model failure modes
# ----------------------------------------------------------------------


async def test_empty_content_retries_with_raised_caps(
    python_ws: Path, gen_dir: Path
) -> None:
    """Live-observed qwen3:8b failure: reasoning burned the output budget
    → empty content. ONE retry at TESTGEN_MAX_TOKENS_RETRY + 2x timeout."""
    client = _FakeClient(texts=["", GOOD_OUTPUT])
    report = await _gen(
        python_ws, gen_dir, client=client, timeout_seconds=240.0
    )
    assert report.status == "ok"
    assert len(client.calls) == 2
    assert client.calls[0]["json"]["max_tokens"] == 4096
    assert client.calls[1]["json"]["max_tokens"] == 8192  # raised cap
    assert client.calls[1]["timeout"] == 480.0  # 2x the per-attempt timeout
    assert report.metrics["retried"] is True
    assert any("retrying" in w for w in report.warnings)


async def test_zero_test_files_retry(python_ws: Path, gen_dir: Path) -> None:
    """A response that parses to zero runnable files retries the SAME
    prompt once before degrading to skipped."""
    garbage = "I cannot write tests for this code. Sorry!"
    client = _FakeClient(texts=[garbage, GOOD_OUTPUT])
    report = await _gen(python_ws, gen_dir, client=client)
    assert report.status == "ok"
    assert len(client.calls) == 2
    # same prompt + model on both attempts — only the caps change
    assert (
        client.calls[0]["json"]["messages"]
        == client.calls[1]["json"]["messages"]
    )
    assert report.metrics["retried"] is True


async def test_zero_files_twice_still_skips(python_ws: Path, gen_dir) -> None:
    """Both attempts yield garbage → the degraded skipped path applies."""
    garbage = "no tests here, sorry"
    client = _FakeClient(texts=[garbage, garbage])
    report = await _gen(python_ws, gen_dir, client=client)
    assert report.status == "skipped"
    assert len(client.calls) == 2


async def test_empty_content_twice_is_error(python_ws: Path, gen_dir) -> None:
    """Empty content on BOTH attempts → error (the retry is exhausted)."""
    client = _FakeClient(texts=["", ""])
    report = await _gen(python_ws, gen_dir, client=client)
    assert report.status == "error"
    assert len(client.calls) == 2


async def test_timeout_retries_once(python_ws: Path, gen_dir) -> None:
    """Live-observed 35B judge failure: ReadTimeout at the base cap earns
    the raised-cap + doubled-timeout retry."""
    client = _FakeClient(texts=[httpx.ReadTimeout("slow model"), GOOD_OUTPUT])
    report = await _gen(python_ws, gen_dir, client=client)
    assert report.status == "ok"
    assert len(client.calls) == 2
    assert report.metrics["retried"] is True


async def test_timeout_twice_is_error(python_ws: Path, gen_dir: Path) -> None:
    client = _FakeClient(
        texts=[httpx.ReadTimeout("t1"), httpx.ReadTimeout("t2")]
    )
    report = await _gen(python_ws, gen_dir, client=client)
    assert report.status == "error"
    assert len(client.calls) == 2


async def test_retry_token_cap_env_override(
    python_ws: Path, gen_dir: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TESTGEN_MAX_TOKENS_RETRY", "16384")
    client = _FakeClient(texts=["", GOOD_OUTPUT])
    report = await _gen(python_ws, gen_dir, client=client)
    assert report.status == "ok"
    assert client.calls[1]["json"]["max_tokens"] == 16384


async def test_no_retry_on_first_success(python_ws: Path, gen_dir: Path) -> None:
    """A usable first response makes exactly one call — no wasted spend."""
    client = _FakeClient()
    report = await _gen(python_ws, gen_dir, client=client)
    assert report.status == "ok"
    assert len(client.calls) == 1
    assert report.metrics["retried"] is False


async def test_retry_timeout_clamped_to_attempt_deadline(
    python_ws: Path, gen_dir: Path
) -> None:
    """The raised-cap retry must fit inside the caller's attempt deadline —
    a retry that outlives the eval envelope gets hard-cancelled by the
    orchestrator's wait_for instead of degrading to a report."""
    client = _FakeClient(texts=[httpx.ReadTimeout("t1"), GOOD_OUTPUT])
    report = await _gen(
        python_ws,
        gen_dir,
        client=client,
        timeout_seconds=960.0,
        attempt_deadline=time.monotonic() + 300.0,
    )
    assert report.status == "ok"
    assert len(client.calls) == 2
    assert 0 < client.calls[1]["timeout"] <= 300.0


async def test_retry_skipped_when_attempt_budget_exhausted(
    python_ws: Path, gen_dir: Path
) -> None:
    """Below the retry-budget floor the retry never fires — degrade to
    skipped so the judge keeps the rest of the eval envelope."""
    client = _FakeClient(texts=[httpx.ReadTimeout("t1"), GOOD_OUTPUT])
    report = await _gen(
        python_ws,
        gen_dir,
        client=client,
        attempt_deadline=time.monotonic() + 10.0,
    )
    assert report.status == "skipped"
    assert len(client.calls) == 1
    assert report.metrics["retried"] is False


# ----------------------------------------------------------------------
# persistence + exec + metrics
# ----------------------------------------------------------------------


async def test_suite_written_and_executed(python_ws: Path, gen_dir: Path) -> None:
    payload = _exec_payload(
        tests={
            "detected": True,
            "test_files": 2,
            "collected": 4,
            "passed": 3,
            "failed": 1,
            "errors": 0,
            "skipped": 0,
            "failed_tests": ["test_calc::test_sub"],
            "ok": False,
            "exit_code": 1,
            "timed_out": False,
            "duration_seconds": 1.2,
            "target_dir": "/tmp/gen_tests",
        },
        coverage={"ok": True, "percent": 66.0, "statements": 10, "covered": 6, "missing": 4},
    )
    runner = _FakeRunner(_ok_outcome(payload))
    report = await _gen(python_ws, gen_dir, runner=runner)

    assert report.status == "ok"
    # exec ran against the untouched workspace w/ the generated dir as target
    assert runner.seen == python_ws
    assert runner.target == gen_dir
    # suite persisted (provenance) — test files + manifest, outside workspace/
    written = sorted(p.name for p in gen_dir.iterdir())
    assert "test_calc.py" in written
    assert "test_more.py" in written
    manifest = json.loads((gen_dir / MANIFEST_FILENAME).read_text())
    assert manifest["model"] == "eval-model"
    assert {f["name"] for f in manifest["files"]} == {"test_calc.py", "test_more.py"}
    # never inside the candidate workspace
    assert not (python_ws / GENERATED_TESTS_DIRNAME).exists()
    assert not (python_ws / "test_calc.py").exists()

    m = report.metrics
    assert m["suite_generated"] is True
    assert m["model"] == "eval-model"
    assert m["files_written"] == 2
    assert m["tests_detected"] is True
    assert m["tests_collected"] == 4
    assert m["tests_passed"] == 3
    assert m["tests_failed"] == 1
    assert m["pass_rate"] == 0.75
    assert m["failed_tests"] == ["test_calc::test_sub"]
    assert m["coverage_percent"] == 66.0


async def test_exec_unavailable_skips(python_ws: Path, gen_dir: Path) -> None:
    outcome = ExecOutcome(
        image="heg-exec:latest",
        error="exec image 'heg-exec:latest' not found",
        unavailable=True,
    )
    report = await _gen(python_ws, gen_dir, runner=_FakeRunner(outcome))
    assert report.status == "skipped"
    assert "not found" in report.warnings[-1]
    # the suite was still persisted — provenance survives the skip
    assert (gen_dir / MANIFEST_FILENAME).exists()


async def test_exec_no_payload_is_error(python_ws: Path, gen_dir: Path) -> None:
    outcome = ExecOutcome(
        image="heg-exec:latest",
        error="exec container produced no result payload (exit 137)",
        exit_code=137,
        duration_seconds=5.0,
    )
    report = await _gen(python_ws, gen_dir, runner=_FakeRunner(outcome))
    assert report.status == "error"
    assert "no result payload" in (report.error or "")


async def test_max_files_cap(python_ws: Path, gen_dir: Path) -> None:
    many = "".join(
        f"### FILE: test_f{i}.py\ndef test_f{i}():\n    assert True\n"
        for i in range(5)
    )
    report = await _gen(
        python_ws, gen_dir, client=_FakeClient(text=many), max_files=2
    )
    assert report.status == "ok"
    assert report.metrics["files_written"] == 2
    assert len(list(gen_dir.glob("test_*.py"))) == 2


async def test_source_budget_bounds_prompt(python_ws: Path, gen_dir: Path) -> None:
    (python_ws / "big.py").write_text("x = '" + "y" * 5000 + "'\n")
    client = _FakeClient()
    report = await _gen(
        python_ws, gen_dir, client=client, source_budget_bytes=100
    )
    assert report.status == "ok"
    prompt = client.calls[0]["json"]["messages"][1]["content"]
    assert "[truncated" in prompt
    # every source still represented in the listing
    assert "calc.py" in prompt and "big.py" in prompt


async def test_source_gather_skips_symlinks(
    python_ws: Path, gen_dir: Path, tmp_path: Path
) -> None:
    """A candidate-planted symlink must not leak host files into the
    generation prompt — artifact workspaces preserve links."""
    secret = tmp_path / "outside_secret.py"
    secret.write_text("SECRET = 'cafef00d'\n")
    (python_ws / "leak.py").symlink_to(secret)
    client = _FakeClient()
    await _gen(python_ws, gen_dir, client=client)
    prompt = client.calls[0]["json"]["messages"][1]["content"]
    assert "cafef00d" not in prompt
    assert "leak.py" not in prompt


# ----------------------------------------------------------------------
# parse_test_files — pure parser unit tests
# ----------------------------------------------------------------------


def test_parse_marker_split() -> None:
    valid, rejected = parse_test_files(GOOD_OUTPUT)
    assert [n for n, _ in valid] == ["test_calc.py", "test_more.py"]
    assert rejected == []
    assert "def test_add" in valid[0][1]


def test_parse_single_unmarked_file() -> None:
    valid, _ = parse_test_files(SINGLE_FILE_OUTPUT)
    assert len(valid) == 1
    assert valid[0][0].startswith("test_")
    assert "def test_single" in valid[0][1]


def test_parse_rejects_non_test_and_broken_code() -> None:
    text = (
        "### FILE: helper.py\nx = 1\n"
        "### FILE: test_broken.py\ndef test_x(:\n"
        "### FILE: test_real.py\ndef test_real():\n    assert True\n"
    )
    valid, rejected = parse_test_files(text)
    assert [n for n, _ in valid] == ["test_real.py"]
    reasons = [r["reason"] for r in rejected]
    assert any("no test functions" in r for r in reasons)
    assert any("SyntaxError" in r for r in reasons)


def test_parse_sanitizes_names_and_dedupes() -> None:
    """Traversal-y names are basename'd; dupes get suffixed; non-test names
    are renamed to a deterministic test_*.py."""
    text = (
        "### FILE: ../evil/test_a.py\ndef test_a():\n    assert True\n"
        "### FILE: test_a.py\ndef test_a2():\n    assert True\n"
        "### FILE: deep/nest/test_a.py\ndef test_a3():\n    assert True\n"
        "### FILE: helpers.py\ndef test_h():\n    assert True\n"
    )
    valid, _ = parse_test_files(text)
    names = [n for n, _ in valid]
    assert names == [
        "test_a.py",
        "test_a_2.py",
        "test_a_3.py",
        "test_generated_3.py",
    ]
    assert all("/" not in n for n in names)


# ----------------------------------------------------------------------
# generation call timeout bound
# ----------------------------------------------------------------------


def test_default_timeout_outwaits_gateway_worst_case() -> None:
    """TESTGEN_TIMEOUT_SECONDS' default must exceed the gateway's worst-case
    silent hold on one call: (1 + UPSTREAM_RETRY_ATTEMPTS) attempts each
    burning the whole UPSTREAM_TIMEOUT_SECONDS, plus the summed exponential
    retry backoff — derived from GatewaySettings' declared defaults so a
    gateway-side bump that breaks the guarantee fails this test."""
    from gateway.config import GatewaySettings

    from evaluator.testgen import DEFAULT_TIMEOUT_SECONDS

    fields = GatewaySettings.model_fields
    attempts = 1 + int(fields["UPSTREAM_RETRY_ATTEMPTS"].default)
    upstream_timeout = float(fields["UPSTREAM_TIMEOUT_SECONDS"].default)
    backoff_base = float(fields["UPSTREAM_RETRY_BACKOFF_SECONDS"].default)
    worst_case = attempts * upstream_timeout + sum(
        backoff_base * (2**i) for i in range(attempts - 1)
    )
    assert DEFAULT_TIMEOUT_SECONDS > worst_case
