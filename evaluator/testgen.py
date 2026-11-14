"""LLM-generated test suites as an objective scoring signal (PR8).

The eval target isn't only what the candidate shipped — it's also how the
submission behaves under a suite the SUBMISSION didn't write. Flow:

1. Gather the workspace's python sources (budget-capped) + rubric context
   into a generation prompt; call ``{gateway_base_url}/v1/chat/completions``
   with the per-eval run cred — traffic is metered/budgeted like everything
   else through the gateway.
2. Parse the model output into ``test_*.py`` files. Deterministic
   containment: every file must byte-compile AND define at least one
   ``test_*`` function — invalid chunks are dropped (reported), never
   written, and a fully unusable generation degrades to ``status="skipped"``
   instead of crashing eval.
3. Persist the valid suite to ``<artifact>/generated_tests/`` (provenance —
   separate from the candidate's ``workspace/``) with a ``_manifest.json``
   recording model/files/rejections.
4. Run the suite against the untouched workspace via
   :meth:`sandbox.exec.ExecRunner.execute` with ``test_target=`` — the
   locked ``heg-exec`` container mounts it read-only, so generated tests
   execute with zero egress and can't modify the artifact. Coverage still
   attributes to ``--source=/work`` (the candidate code), and the build
   phase never compiles the generated files — a broken generated test can
   only fail the test phase, not the candidate's build signal.
5. Return a ``generated_tests`` :class:`SignalReport` (pass rate, failures)
   — the evaluator merges it into the ``AnalysisReport`` envelope so the
   judge sees measured functional evidence.

Env knobs (all optional):

- ``SCORING_TESTGEN`` — ``0``/``false``/``off`` disables (``skipped``);
  default ON. ``tests/conftest.py`` pins it off so unit tests never call an
  LLM or spawn containers.
- ``TESTGEN_MODEL`` — generation model override; default = evaluator model.
- ``TESTGEN_SOURCE_BUDGET_BYTES`` — total candidate-source bytes fed into
  the prompt (default 24000).
- ``TESTGEN_MAX_FILES`` — cap on written test files (default 8).
- ``TESTGEN_MAX_TOKENS`` — request ``max_tokens`` (default 4096).
- ``TESTGEN_TIMEOUT_SECONDS`` — generation HTTP timeout (default 240).

Status semantics mirror ``scoring.exec``: ``skipped`` = declined (disabled,
non-python workspace, gateway/image unavailable, model produced no runnable
test code); ``error`` = a step ran but produced no measurement (HTTP
failure, exec container died without a payload); ``ok`` = measured — a red
generated suite IS the measurement, not an infra failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from evaluator.prompts import TESTGEN_SYSTEM_PROMPT, build_testgen_prompt
from scoring.exec import _env_enabled, _flat_metrics, _has_python_sources
from scoring.models import SignalReport
from scoring.static import SKIP_DIRS

if TYPE_CHECKING:
    from sandbox.exec import ExecRunner

logger = logging.getLogger(__name__)

SIGNAL_NAME = "generated_tests"
#: Directory (under the artifact dir) the generated suite is persisted to —
#: a sibling of ``workspace/``, never inside it.
GENERATED_TESTS_DIRNAME = "generated_tests"
#: Provenance file inside the suite dir (``_`` prefix → never test-collected).
MANIFEST_FILENAME = "_manifest.json"

DEFAULT_SOURCE_BUDGET_BYTES = 24_000
DEFAULT_MAX_FILES = 8
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT_SECONDS = 240.0
#: Per-file cap — one giant output chunk is almost certainly a model rant.
MAX_FILE_BYTES = 64_000


class TestgenError(Exception):
    """Generation-side failure (bad response, no usable content)."""


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# source gathering
# ---------------------------------------------------------------------------


def _gather_sources(
    workspace: Path, *, budget_bytes: int
) -> list[tuple[str, str]]:
    """``(workspace-relative path, text)`` for ``*.py`` outside dep dirs.

    Smallest files first — a budget spent on one giant module must not
    starve every other file out of the prompt. The last included file is
    truncated to the remaining budget with a marker so it is still
    represented; files that can't fit at all are skipped.
    """
    candidates: list[tuple[int, Path]] = []
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            path = Path(dirpath) / name
            try:
                candidates.append((path.stat().st_size, path))
            except OSError:
                continue
    candidates.sort(key=lambda t: (t[0], str(t[1])))

    out: list[tuple[str, str]] = []
    remaining = budget_bytes
    for _size, path in candidates:
        if remaining <= 0:
            break
        rel = path.relative_to(workspace).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(text.encode("utf-8", errors="replace")) > remaining:
            # Byte-accurate truncation is overkill — char slice, mark it.
            text = text[:remaining] + "\n# ... [truncated — source budget]\n"
        out.append((rel, text))
        remaining -= len(text.encode("utf-8", errors="replace"))
    return out


# ---------------------------------------------------------------------------
# model output parsing — deterministic containment
# ---------------------------------------------------------------------------

#: ``### FILE: test_x.py`` / ``## FILE: ...`` / ``# FILE: ...`` markers.
_FILE_MARKER_RE = re.compile(r"^#{1,4}\s*FILE:\s*(?P<name>\S+?)\s*$", re.MULTILINE)
#: Test-file names we accept as-is; anything else is renamed deterministically.
_TEST_NAME_RE = re.compile(r"^test_[A-Za-z0-9_]*\.py$")
#: A chunk must define a test to be worth writing (parroting the source
#: listing back is the common small-model failure this catches).
_TEST_DEF_RE = re.compile(r"(def test_|class Test[A-Z(])")


def _strip_fences(text: str) -> str:
    """Drop surrounding ```python ... ``` fences from a code chunk."""
    lines = text.strip().splitlines()
    while lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    while lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _split_chunks(text: str) -> list[tuple[str | None, str]]:
    """Split model output on FILE markers → ``(name, body)``; unnamed when
    the model emitted a single unmarked file."""
    matches = list(_FILE_MARKER_RE.finditer(text))
    if not matches:
        return [(None, text)]
    return [
        (
            m.group("name"),
            text[m.end() : matches[i + 1].start() if i + 1 < len(matches) else len(text)],
        )
        for i, m in enumerate(matches)
    ]


def _safe_name(raw: str | None, index: int, used: set[str]) -> str:
    """A collision-free ``test_*.py`` filename for one chunk."""
    name = Path(raw or "").name
    if not _TEST_NAME_RE.match(name):
        name = f"test_generated_{index}.py"
    base = name[:-3]
    n = 2
    while name in used:
        name = f"{base}_{n}.py"
        n += 1
    used.add(name)
    return name


def _validate_chunk(code: str) -> str | None:
    """None when usable test code; a rejection reason string otherwise."""
    if len(code.encode("utf-8", errors="replace")) > MAX_FILE_BYTES:
        return f"over {MAX_FILE_BYTES} bytes"
    if not _TEST_DEF_RE.search(code):
        return "no test functions defined"
    try:
        compile(code, "<generated>", "exec")
    except SyntaxError as exc:
        return f"SyntaxError: {exc.msg} (line {exc.lineno})"
    return None


def parse_test_files(text: str) -> tuple[list[tuple[str, str]], list[dict[str, str]]]:
    """Model output → ``(valid [(name, code)], rejected [{name, reason}])``.

    Every acceptance gate is deterministic — no LLM judgement involved:
    fence stripping, FILE-marker splitting, test-def presence, byte cap,
    ``compile()`` syntax check.
    """
    used: set[str] = set()
    valid: list[tuple[str, str]] = []
    rejected: list[dict[str, str]] = []
    for index, (raw_name, body) in enumerate(_split_chunks(text)):
        name = _safe_name(raw_name, index, used)
        code = _strip_fences(body)
        if not code:
            rejected.append({"name": name, "reason": "empty chunk"})
            continue
        reason = _validate_chunk(code)
        if reason is not None:
            rejected.append({"name": name, "reason": reason})
            continue
        valid.append((name, code + "\n"))
    return valid, rejected


# ---------------------------------------------------------------------------
# gateway call + suite persistence
# ---------------------------------------------------------------------------


def _message_content(resp_json: dict[str, Any]) -> str:
    choices = resp_json.get("choices") or []
    if not choices:
        raise TestgenError(f"chat response has no choices: {resp_json!r:.200}")
    content = (choices[0].get("message") or {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise TestgenError("chat response message content is empty")
    return content


async def _request_suite(
    client: httpx.AsyncClient,
    *,
    gateway_base_url: str,
    cred: str,
    model: str,
    user_prompt: str,
    max_tokens: int,
) -> str:
    """One chat-completions call → raw model output (retries live upstream)."""
    resp = await client.post(
        f"{gateway_base_url.rstrip('/')}/v1/chat/completions",
        json={
            "model": model,
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "stream": False,
            "messages": [
                {"role": "system", "content": TESTGEN_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        },
        headers={"Authorization": f"Bearer {cred}"},
    )
    resp.raise_for_status()
    return _message_content(resp.json())


def _write_suite(
    gen_dir: Path,
    files: list[tuple[str, str]],
    *,
    manifest: dict[str, Any],
) -> None:
    """Replace ``gen_dir`` with the valid suite + ``_manifest.json``."""
    if gen_dir.exists():
        shutil.rmtree(gen_dir)
    gen_dir.mkdir(parents=True)
    for name, code in files:
        (gen_dir / name).write_text(code, encoding="utf-8")
    (gen_dir / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _build_runner() -> ExecRunner:
    from sandbox.exec import ExecRunner

    return ExecRunner()


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


async def generate_tests_signal(
    *,
    workspace: Path | str,
    gen_dir: Path | str,
    gateway_base_url: str,
    cred: str,
    model: str | None = None,
    rubric: dict[str, Any] | None = None,
    runner: ExecRunner | None = None,
    enabled: bool | None = None,
    client: httpx.AsyncClient | None = None,
    source_budget_bytes: int | None = None,
    max_files: int | None = None,
    max_tokens: int | None = None,
    timeout_seconds: float | None = None,
) -> SignalReport:
    """Generate + execute an LLM-written pytest suite → ``generated_tests``
    :class:`SignalReport`. Never raises for LLM/docker problems — every
    failure mode lands on the report as ``skipped``/``error``.
    """
    report = SignalReport(name=SIGNAL_NAME, status="ok")
    metrics: dict[str, Any] = report.metrics
    ws = Path(workspace)
    gen_path = Path(gen_dir)

    enabled_flag = (
        enabled if enabled is not None else _env_enabled("SCORING_TESTGEN", True)
    )
    if not enabled_flag:
        report.status = "skipped"
        report.warnings.append("test-suite generation disabled (SCORING_TESTGEN=0)")
        return report
    if not ws.is_dir():
        report.status = "skipped"
        report.warnings.append(f"workspace is not a directory: {ws}")
        return report
    if not _has_python_sources(ws):
        report.status = "skipped"
        report.warnings.append("no python sources — test generation supports python only")
        return report
    if not cred:
        report.status = "skipped"
        report.warnings.append("no gateway credential — cannot meter generation")
        return report

    budget = source_budget_bytes or _env_int(
        "TESTGEN_SOURCE_BUDGET_BYTES", DEFAULT_SOURCE_BUDGET_BYTES
    )
    file_cap = max_files or _env_int("TESTGEN_MAX_FILES", DEFAULT_MAX_FILES)
    tok_cap = max_tokens or _env_int("TESTGEN_MAX_TOKENS", DEFAULT_MAX_TOKENS)
    timeout = timeout_seconds or _env_float(
        "TESTGEN_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS
    )
    gen_model = (os.environ.get("TESTGEN_MODEL") or "").strip() or (model or "")

    sources = _gather_sources(ws, budget_bytes=budget)
    if not sources:
        report.status = "skipped"
        report.warnings.append("no readable python sources to feed the generator")
        return report

    metrics.update(
        {
            "model": gen_model,
            "source_files": len(sources),
            "source_bytes": sum(len(t.encode("utf-8")) for _, t in sources),
            "tests_dir": str(gen_path),
        }
    )

    # ---- generate (metered gateway call) ------------------------------------
    prompt = build_testgen_prompt(rubric, sources)
    own_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout)
    try:
        raw = await _request_suite(
            client,
            gateway_base_url=gateway_base_url,
            cred=cred,
            model=gen_model,
            user_prompt=prompt,
            max_tokens=tok_cap,
        )
    except Exception as exc:  # noqa: BLE001 — transport/HTTP/contract failures
        report.status = "error"
        report.error = f"test generation call failed: {type(exc).__name__}: {exc}"
        return report
    finally:
        if own_client:
            await client.aclose()

    # ---- parse + validate (deterministic containment) -----------------------
    valid, rejected = parse_test_files(raw)
    metrics["files_returned"] = len(valid) + len(rejected)
    if len(valid) > file_cap:
        report.warnings.append(
            f"ignored {len(valid) - file_cap} file(s) over TESTGEN_MAX_FILES={file_cap}"
        )
        valid = valid[:file_cap]
    metrics["files_written"] = len(valid)
    metrics["files_rejected"] = len(rejected)
    if rejected:
        for rej in rejected[:10]:
            report.warnings.append(f"dropped {rej['name']}: {rej['reason']}")
    if not valid:
        report.status = "skipped"
        report.warnings.append(
            "model produced no runnable test code (all chunks rejected)"
        )
        return report

    # ---- persist provenance (<artifact>/generated_tests/) -------------------
    manifest = {
        "model": gen_model,
        "generated_at": datetime.now(UTC).isoformat(),
        "files": [{"name": n, "bytes": len(c.encode())} for n, c in valid],
        "rejected": rejected,
    }
    try:
        _write_suite(gen_path, valid, manifest=manifest)
    except OSError as exc:
        report.status = "error"
        report.error = f"could not persist generated suite: {exc}"
        return report

    # ---- execute via the locked heg-exec container --------------------------
    runner = runner or _build_runner()
    outcome = await asyncio.to_thread(runner.execute, ws, test_target=gen_path)
    report.warnings.extend(outcome.warnings)
    if outcome.unavailable:
        report.status = "skipped"
        if outcome.error:
            report.warnings.append(outcome.error)
        return report
    if not outcome.ok:
        report.status = "error"
        report.error = outcome.error or "exec produced no result payload"
        if outcome.logs_tail:
            report.warnings.append(f"container logs: {outcome.logs_tail}")
        metrics["timed_out"] = outcome.timed_out
        metrics["duration_seconds"] = outcome.duration_seconds
        return report

    # ---- measured ------------------------------------------------------------
    metrics.update(_flat_metrics(outcome))
    result = outcome.result or {}
    tests = (result.get("phases") or {}).get("tests") or {}
    collected = tests.get("collected") or 0
    metrics["pass_rate"] = (
        round((tests.get("passed") or 0) / collected, 3) if collected else None
    )
    metrics["failed_tests"] = tests.get("failed_tests") or []
    metrics["suite_generated"] = True
    report.warnings.extend(str(w) for w in result.get("warnings") or [])
    return report
