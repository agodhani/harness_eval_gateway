"""Direct judge fallback tests — fake gateway client, no LLM, no docker.

Hermetic: the gateway call is a stub async client recording requests and
returning canned chat payloads (same pattern as ``test_testgen.py``). Pins
the contract: request shape (``response_format``, auth, model), the 4xx
re-try-without-json-object path, bounded prompt material, and error mapping
to ``DirectJudgeError``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from evaluator.judge_direct import (
    DirectJudgeError,
    build_direct_prompt,
    direct_fallback_enabled,
    judge_direct,
)

RUBRIC = {
    "summary": "Say hi.",
    "dimensions": [
        {"name": "functional_requirements", "title": "Functional requirements"},
        {"name": "code_quality", "title": "Code quality"},
    ],
    "criteria": [{"id": "c1", "text": "prints a greeting", "dimension": "functional_requirements"}],
}

SCORES_OBJ = {
    "scores": [
        {"dimension": "functional_requirements", "score": 8, "rationale": "hello.txt present"},
        {"dimension": "code_quality", "score": 6, "rationale": "one file"},
    ],
    "overall": 7.0,
    "summary": "Minimal greeting program.",
}


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://gw.test/v1/chat/completions")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=request, response=response
            )

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    """Duck-typed httpx.AsyncClient — records calls, returns canned text."""

    def __init__(
        self,
        text: str | None = None,
        exc: Exception | None = None,
        statuses: list[int] | None = None,
    ) -> None:
        self._text = text if text is not None else json.dumps(SCORES_OBJ)
        self._exc = exc
        self._statuses = list(statuses or [])
        self.calls: list[dict[str, Any]] = []

    async def post(
        self, url: str, json: dict | None = None, headers: dict | None = None
    ) -> _FakeResponse:
        # Snapshot the body — the caller may mutate it for the 4xx retry.
        self.calls.append(
            {"url": url, "json": dict(json or {}), "headers": dict(headers or {})}
        )
        if self._exc is not None:
            raise self._exc
        if self._statuses:
            return _FakeResponse({}, status_code=self._statuses.pop(0))
        return _FakeResponse({"choices": [{"message": {"content": self._text}}]})

    async def aclose(self) -> None:
        return None


@pytest.fixture()
def eval_ws(tmp_path: Path) -> Path:
    """Staged eval workspace: candidate files + evaluator-staged files."""
    ws = tmp_path / "eval_workspace"
    ws.mkdir()
    (ws / "hello.txt").write_text("hello\n")
    (ws / "main.py").write_text("print('hi')\n")
    (ws / "rubric.json").write_text("{}")
    (ws / "EVAL_PROMPT.md").write_text("# judge brief\n")
    (ws / "EVAL_TRANSCRIPT.log").write_text("MINILOOP_RESULT {}\n")
    (ws / "scores.json").write_text("{broken pseudo-json")
    return ws


async def _judge(ws: Path, *, client: _FakeClient | None = None, **kwargs: Any):
    kwargs.setdefault("client", client or _FakeClient())
    return await judge_direct(
        eval_workspace=ws,
        rubric=RUBRIC,
        gateway_base_url="http://gw.test:8080",
        cred="heg.testcred",
        model="eval-model",
        **kwargs,
    )


# ----------------------------------------------------------------------
# happy path + request contract
# ----------------------------------------------------------------------


async def test_direct_judge_returns_parsed_scores(eval_ws: Path) -> None:
    parsed = await _judge(eval_ws)
    assert [e.dimension for e in parsed.entries] == [
        "functional_requirements",
        "code_quality",
    ]
    assert parsed.overall == 7.0


async def test_gateway_call_contract(eval_ws: Path) -> None:
    client = _FakeClient()
    await _judge(eval_ws, client=client)
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["url"] == "http://gw.test:8080/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer heg.testcred"
    body = call["json"]
    assert body["model"] == "eval-model"
    assert body["stream"] is False
    assert body["response_format"] == {"type": "json_object"}
    msgs = body["messages"]
    assert msgs[0]["role"] == "system"
    assert "scores" in msgs[0]["content"]
    user = msgs[1]["content"]
    # rubric context + candidate sources land in the prompt
    assert "Say hi." in user
    assert "functional_requirements" in user
    assert "prints a greeting" in user
    assert "main.py" in user
    assert "hello.txt" in user


async def test_evaluator_staged_files_not_sources(eval_ws: Path) -> None:
    """rubric.json / EVAL_* / ANALYSIS.* / scores.json never enter the
    source gather — they ride their own prompt sections."""
    client = _FakeClient()
    await _judge(eval_ws, client=client)
    user = client.calls[0]["json"]["messages"][1]["content"]
    sources_section = user.split("# Submission files")[-1]
    assert "rubric.json" not in sources_section
    assert "EVAL_PROMPT.md" not in sources_section
    assert "EVAL_TRANSCRIPT.log" not in sources_section
    assert "scores.json" not in sources_section
    assert "{broken pseudo-json" not in user


async def test_analysis_and_transcript_included(eval_ws: Path) -> None:
    (eval_ws / "ANALYSIS.md").write_text("# Analysis\n\nCyclomatic complexity: mean 1.0\n")
    (eval_ws / "EVAL_TRANSCRIPT.log").write_text("tail-line\n")
    client = _FakeClient()
    await _judge(eval_ws, client=client)
    user = client.calls[0]["json"]["messages"][1]["content"]
    assert "Cyclomatic complexity" in user
    assert "tail-line" in user


async def test_binary_path_listed_without_contents_and_generated_tests_skipped(
    eval_ws: Path,
) -> None:
    (eval_ws / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    gen = eval_ws / "generated_tests"
    gen.mkdir()
    (gen / "test_x.py").write_text("def test_x():\n    assert True\n")
    client = _FakeClient()
    await _judge(eval_ws, client=client)
    user = client.calls[0]["json"]["messages"][1]["content"]
    assert '- "blob.bin" (binary)' in user
    assert "\x00\x01\x02binary" not in user
    assert "test_x.py" not in user


async def test_fallback_includes_hidden_deliverables_but_not_harness_scaffolding(eval_ws):
    (eval_ws / ".vscode").mkdir()
    (eval_ws / ".vscode" / "settings.json").write_text('{"editor.tabSize": 2}')
    (eval_ws / ".gitignore").write_text(".aider*\n")
    (eval_ws / ".aider.chat.history.md").write_text("harness history")
    client = _FakeClient()
    await _judge(eval_ws, client=client)
    user = client.calls[0]["json"]["messages"][1]["content"]
    assert "### FILE: .vscode/settings.json" in user
    assert '"editor.tabSize": 2' in user
    assert ".gitignore" not in user
    assert ".aider.chat.history.md" not in user


async def test_gather_sources_skips_symlinks(eval_ws: Path, tmp_path: Path) -> None:
    """Workspace symlinks survive staging — the host-side gather must not
    follow them or files outside the workspace land in the prompt."""
    secret = tmp_path / "host_secret.txt"
    secret.write_text("HOST-SECRET-DEADBEEF\n")
    (eval_ws / "leak.txt").symlink_to(secret)
    (eval_ws / "dangling.txt").symlink_to(tmp_path / "nonexistent")
    client = _FakeClient()
    await _judge(eval_ws, client=client)
    user = client.calls[0]["json"]["messages"][1]["content"]
    sources_section = user.split("# Submission files")[-1]
    assert "HOST-SECRET-DEADBEEF" not in user
    assert "leak.txt" not in sources_section
    assert "dangling.txt" not in sources_section


async def test_symlinked_staged_files_not_read(eval_ws: Path, tmp_path: Path) -> None:
    """ANALYSIS.md / EVAL_TRANSCRIPT.log live in the judge-writable staged
    workspace — a planted symlink must not leak host files either."""
    secret = tmp_path / "host_secret.txt"
    secret.write_text("HOST-SECRET-CAFEBABE\n")
    (eval_ws / "EVAL_TRANSCRIPT.log").unlink()
    for name in ("ANALYSIS.md", "EVAL_TRANSCRIPT.log"):
        (eval_ws / name).symlink_to(secret)
    client = _FakeClient()
    await _judge(eval_ws, client=client)
    user = client.calls[0]["json"]["messages"][1]["content"]
    assert "HOST-SECRET-CAFEBABE" not in user


async def test_prior_error_and_terminal_reason_in_prompt(eval_ws: Path) -> None:
    client = _FakeClient()
    await _judge(
        eval_ws,
        client=client,
        prior_error="scores_invalid: single-quoted strings",
        terminal_reason="timeout",
    )
    user = client.calls[0]["json"]["messages"][1]["content"]
    assert "single-quoted strings" in user
    assert "ONE JSON object" in user
    assert "timeout" in user


async def test_single_quoted_output_repaired(eval_ws: Path) -> None:
    """The direct judge's own pseudo-JSON goes through parse_scores_json's
    literal_eval path too."""
    text = "{'scores': [{'dimension': 'd', 'score': 5, 'rationale': 'ok'}]}"
    parsed = await _judge(eval_ws, client=_FakeClient(text=text))
    assert parsed.entries[0].dimension == "d"


# ----------------------------------------------------------------------
# response_format retry
# ----------------------------------------------------------------------


async def test_4xx_retries_once_without_response_format(eval_ws: Path) -> None:
    """An upstream rejecting response_format gets one retry without it."""
    client = _FakeClient(statuses=[400])
    parsed = await _judge(eval_ws, client=client)
    assert parsed.entries
    assert len(client.calls) == 2
    assert "response_format" in client.calls[0]["json"]
    assert "response_format" not in client.calls[1]["json"]


async def test_second_4xx_fails(eval_ws: Path) -> None:
    client = _FakeClient(statuses=[400, 422])
    with pytest.raises(DirectJudgeError):
        await _judge(eval_ws, client=client)
    assert len(client.calls) == 2


async def test_5xx_does_not_retry(eval_ws: Path) -> None:
    client = _FakeClient(statuses=[500])
    with pytest.raises(DirectJudgeError):
        await _judge(eval_ws, client=client)
    assert len(client.calls) == 1


# ----------------------------------------------------------------------
# failures
# ----------------------------------------------------------------------


async def test_transport_failure_raises(eval_ws: Path) -> None:
    client = _FakeClient(exc=httpx.ConnectError("refused"))
    with pytest.raises(DirectJudgeError, match="refused"):
        await _judge(eval_ws, client=client)


async def test_empty_choices_raises(eval_ws: Path) -> None:
    class EmptyClient(_FakeClient):
        async def post(self, url, json=None, headers=None):
            return _FakeResponse({"choices": []})

    with pytest.raises(DirectJudgeError):
        await _judge(eval_ws, client=EmptyClient())


async def test_unusable_output_raises(eval_ws: Path) -> None:
    client = _FakeClient(text="I cannot score this submission. Sorry!")
    with pytest.raises(DirectJudgeError, match="unusable"):
        await _judge(eval_ws, client=client)


async def test_no_cred_raises(eval_ws: Path) -> None:
    with pytest.raises(DirectJudgeError, match="credential"):
        await judge_direct(
            eval_workspace=eval_ws,
            rubric=RUBRIC,
            gateway_base_url="http://gw.test",
            cred="",
            model="m",
            client=_FakeClient(),
        )


# ----------------------------------------------------------------------
# env knobs
# ----------------------------------------------------------------------


def test_direct_fallback_enabled_flag(monkeypatch) -> None:
    assert direct_fallback_enabled() is True
    monkeypatch.setenv("EVAL_DIRECT_FALLBACK", "0")
    assert direct_fallback_enabled() is False
    monkeypatch.setenv("EVAL_DIRECT_FALLBACK", "off")
    assert direct_fallback_enabled() is False
    monkeypatch.setenv("EVAL_DIRECT_FALLBACK", "1")
    assert direct_fallback_enabled() is True


async def test_max_tokens_env_knob(eval_ws: Path, monkeypatch) -> None:
    monkeypatch.setenv("EVAL_DIRECT_MAX_TOKENS", "2048")
    client = _FakeClient()
    await _judge(eval_ws, client=client)
    assert client.calls[0]["json"]["max_tokens"] == 2048


async def test_source_budget_bounds_prompt(eval_ws: Path, monkeypatch) -> None:
    (eval_ws / "big.py").write_text("x = '" + "y" * 5000 + "'\n")
    (eval_ws / "biggest.py").write_text("z" * 6000)
    client = _FakeClient()
    await _judge(eval_ws, client=client, source_budget_bytes=400)
    user = client.calls[0]["json"]["messages"][1]["content"]
    assert "[truncated" in user
    # every source still represented
    assert "main.py" in user and "big.py" in user
    assert '- "biggest.py" (source budget)' in user
    assert "z" * 100 not in user


async def test_many_over_budget_paths_have_a_compact_bounded_listing(eval_ws):
    for i in range(1001):
        (eval_ws / f"candidate-{i:04}.txt").write_text("a" * 100)
    client = _FakeClient()
    await _judge(eval_ws, client=client, source_budget_bytes=24_000)
    user = client.calls[0]["json"]["messages"][1]["content"]
    section = user.split("# Submission files", 1)[1].split("# Candidate transcript", 1)[0]
    contents, omitted = section.split("## Present files (contents omitted)")
    assert len(("# Submission files" + section).encode()) <= 24_000
    assert omitted.count("(source budget)") == 50
    unlisted = 1001 - contents.count("### FILE: candidate-") - 50
    assert f"and {unlisted} more files not listed" in omitted
    assert "candidate-1000.txt" not in omitted


# ----------------------------------------------------------------------
# build_direct_prompt — pure builder unit tests
# ----------------------------------------------------------------------


def test_build_direct_prompt_prior_error_capped() -> None:
    prompt = build_direct_prompt(RUBRIC, prior_error="x" * 1000)
    assert "x" * 400 not in prompt


def test_build_direct_prompt_minimal() -> None:
    prompt = build_direct_prompt({"dimensions": []})
    assert "Rubric dimensions" in prompt
    assert "JSON" in prompt


# ----------------------------------------------------------------------
# call timeout bound
# ----------------------------------------------------------------------


def test_default_timeout_outwaits_gateway_worst_case() -> None:
    """EVAL_DIRECT_TIMEOUT_SECONDS' default must exceed the gateway's
    worst-case silent hold on one call: (1 + UPSTREAM_RETRY_ATTEMPTS)
    attempts each burning the whole UPSTREAM_TIMEOUT_SECONDS, plus the
    summed exponential retry backoff — derived from GatewaySettings'
    declared defaults so a gateway-side bump that breaks the guarantee
    fails this test."""
    from evaluator.judge_direct import DEFAULT_TIMEOUT_SECONDS
    from gateway.config import GatewaySettings

    fields = GatewaySettings.model_fields
    attempts = 1 + int(fields["UPSTREAM_RETRY_ATTEMPTS"].default)
    upstream_timeout = float(fields["UPSTREAM_TIMEOUT_SECONDS"].default)
    backoff_base = float(fields["UPSTREAM_RETRY_BACKOFF_SECONDS"].default)
    worst_case = attempts * upstream_timeout + sum(
        backoff_base * (2**i) for i in range(attempts - 1)
    )
    assert DEFAULT_TIMEOUT_SECONDS > worst_case


def test_direct_prompt_pr23_domain_neutral() -> None:
    """PR23: the system prompt no longer frames the task as coding-only and
    carries the not-applicable rule so a rubric without code dimensions
    still instructs the judge to emit every row."""
    from evaluator.judge_direct import DIRECT_SYSTEM_PROMPT

    assert "coding-agent benchmark" not in DIRECT_SYSTEM_PROMPT
    assert "not applicable" in DIRECT_SYSTEM_PROMPT
    # arbitrary rubric dims render verbatim in the user message
    rubric = {
        "dimensions": [
            {"name": "accuracy", "min_score": 0, "max_score": 10},
            {"name": "tone", "min_score": 0, "max_score": 10},
        ]
    }
    prompt = build_direct_prompt(rubric)
    assert "`accuracy`" in prompt
    assert "`tone`" in prompt
