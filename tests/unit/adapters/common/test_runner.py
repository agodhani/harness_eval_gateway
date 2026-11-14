"""Tests for adapters/common/runner.py — ShimConfig, EventSink, run_shim."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any, Self

import pytest

from adapters.common import runner

# ---------------------------------------------------------------------------
# ShimConfig
# ---------------------------------------------------------------------------


def _env(**over: str) -> dict[str, str]:
    base = {
        "GATEWAY_BASE_URL": "http://gateway:8080/",
        "GATEWAY_CRED": "heg.cred.sig",
        "MODEL_NAME": "qwen3:8b",
        "TASK_PROMPT": "build 2048",
        "WORKSPACE": "/workspace",
        "SKILLPACKS_ROOT": "/opt/skillpacks",
        "EVENTS_INGEST_URL": "http://gateway:8080/internal/events",
        "HOME": "/home/agent",
    }
    base.update(over)
    return base


def test_shimconfig_from_env() -> None:
    cfg = runner.ShimConfig.from_env(_env(SYSTEM_PROMPT_EXTRA="be terse"))
    assert cfg.gateway_url == "http://gateway:8080"  # trailing slash stripped
    assert cfg.cred == "heg.cred.sig"
    assert cfg.model == "qwen3:8b"
    assert cfg.task_prompt == "build 2048"
    assert cfg.ingest_url == "http://gateway:8080/internal/events"
    assert cfg.system_prompt_extra == "be terse"
    assert cfg.home == "/home/agent"


def test_shimconfig_defaults() -> None:
    cfg = runner.ShimConfig.from_env(
        {"GATEWAY_BASE_URL": "g", "GATEWAY_CRED": "c", "MODEL_NAME": "m", "TASK_PROMPT": "t"}
    )
    assert cfg.workspace == "/workspace"
    assert cfg.skillpacks_root == "/opt/skillpacks"
    assert cfg.ingest_url is None


# ---------------------------------------------------------------------------
# EventSink
# ---------------------------------------------------------------------------


def test_eventsink_stdout_jsonl() -> None:
    buf = io.StringIO()
    sink = runner.EventSink(None, "cred", stdout=buf)
    ev = sink.emit("tool_call", {"name": "shell"})
    lines = buf.getvalue().strip().splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["type"] == "tool_call"
    assert obj["source"] == "harness"
    assert obj["seq"] == 1
    assert obj["payload"] == {"name": "shell"}
    assert ev["seq"] == 1
    assert sink.events_emitted == 1


def test_eventsink_posts_to_ingest(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[dict[str, Any]] = []

    class _Resp:
        status = 200

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *a: object) -> None:
            return None

    def fake_urlopen(req: Any, timeout: float = 0) -> _Resp:
        posted.append(
            {
                "url": req.full_url,
                "auth": req.headers.get("Authorization"),
                "body": json.loads(req.data),
            }
        )
        return _Resp()

    monkeypatch.setattr(runner.urllib.request, "urlopen", fake_urlopen)
    sink = runner.EventSink("http://gw/internal/events", "heg.cred", stdout=io.StringIO())
    sink.emit("checkpoint", {"stage": "x"})
    assert len(posted) == 1
    assert posted[0]["url"] == "http://gw/internal/events"
    assert posted[0]["auth"] == "Bearer heg.cred"
    assert posted[0]["body"]["type"] == "checkpoint"
    assert sink.ingest_failures == 0


def test_eventsink_ingest_failure_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(req: Any, timeout: float = 0) -> Any:
        raise OSError("conn refused")

    monkeypatch.setattr(runner.urllib.request, "urlopen", boom)
    sink = runner.EventSink("http://gw/internal/events", "c", stdout=io.StringIO())
    sink.emit("checkpoint", {})
    assert sink.ingest_failures == 1
    assert sink.events_emitted == 1


# ---------------------------------------------------------------------------
# build_task_text / staged_skillpacks
# ---------------------------------------------------------------------------


def test_build_task_text_footer(tmp_path: Path) -> None:
    (tmp_path / "superpowers").mkdir()
    (tmp_path / "notadir").write_text("x")
    cfg = runner.ShimConfig(
        gateway_url="g",
        cred="c",
        model="m",
        task_prompt="do the thing",
        skillpacks_root=str(tmp_path),
        system_prompt_extra="extra rules",
    )
    text = runner.build_task_text(cfg)
    assert text.startswith("do the thing")
    assert "superpowers" in text
    assert "done" in text  # done-mechanism hint
    assert "[additional instructions]" in text
    assert "extra rules" in text


def test_build_task_text_no_packs_no_extra(tmp_path: Path) -> None:
    cfg = runner.ShimConfig(
        gateway_url="g", cred="c", model="m", task_prompt="t", skillpacks_root=str(tmp_path)
    )
    text = runner.build_task_text(cfg)
    assert "Skill packs" not in text
    assert "[additional instructions]" not in text


def test_staged_skillpacks_missing_root() -> None:
    assert runner.staged_skillpacks("/nonexistent/dir") == []


# ---------------------------------------------------------------------------
# run_shim — with a stub CLI script
# ---------------------------------------------------------------------------


def _write_stub_cli(tmp_path: Path, body: str) -> list[str]:
    script = tmp_path / "fake_cli.py"
    script.write_text(body)
    return [sys.executable, str(script)]


def _translate(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Test translator: native {'type':'say'} → checkpoint; {'type':'finish'} → terminal_state."""
    t = obj.get("type")
    if t == "say":
        return [{"type": "checkpoint", "payload": {"stage": "say", "text": obj.get("text")}}]
    if t == "finish":
        return [
            {
                "type": "terminal_state",
                "payload": {"terminal_state": obj.get("state", "harness_complete")},
            }
        ]
    return []


def test_run_shim_streams_and_reports(tmp_path: Path) -> None:
    argv = _write_stub_cli(
        tmp_path,
        "import json\n"
        "print(json.dumps({'type':'say','text':'hello'}))\n"
        "print('non json noise')\n"
        "print(json.dumps({'type':'finish','state':'harness_complete'}))\n",
    )
    out, err = io.StringIO(), io.StringIO()
    cfg = runner.ShimConfig(
        gateway_url="g",
        cred="c",
        model="m",
        task_prompt="t",
        workspace=str(tmp_path),
    )
    rc = runner.run_shim(
        harness="fake",
        cfg=cfg,
        argv=argv,
        translate=_translate,
        stdout=out,
        stderr=err,
    )
    assert rc == 0
    raw_lines = out.getvalue().strip().splitlines()
    # Event lines are JSON dicts; the last line is the HEG_RESULT marker.
    lines = [json.loads(x) for x in raw_lines if x.startswith("{")]
    types = [e["type"] for e in lines]
    assert types == ["run_started", "checkpoint", "terminal_state", "terminal_state"]
    marker = raw_lines[-1]
    assert marker.startswith(runner.RESULT_MARKER)
    payload = json.loads(marker[len(runner.RESULT_MARKER) :])
    assert payload["terminal_state"] == "harness_complete"
    assert payload["harness"] == "fake"
    # raw CLI output echoed to stderr
    assert "non json noise" in err.getvalue()


def test_run_shim_error_exit(tmp_path: Path) -> None:
    argv = _write_stub_cli(tmp_path, "import sys\nsys.exit(3)\n")
    out, err = io.StringIO(), io.StringIO()
    cfg = runner.ShimConfig(gateway_url="g", cred="c", model="m", task_prompt="t", workspace=str(tmp_path))
    rc = runner.run_shim(harness="fake", cfg=cfg, argv=argv, translate=_translate, stdout=out, stderr=err)
    assert rc == 2
    marker = out.getvalue().strip().splitlines()[-1]
    assert json.loads(marker[len(runner.RESULT_MARKER) :])["terminal_state"] == "error"


def test_run_shim_missing_binary(tmp_path: Path) -> None:
    out, err = io.StringIO(), io.StringIO()
    cfg = runner.ShimConfig(gateway_url="g", cred="c", model="m", task_prompt="t", workspace=str(tmp_path))
    rc = runner.run_shim(
        harness="fake",
        cfg=cfg,
        argv=["/definitely/not/a/binary", "--x"],
        translate=_translate,
        stdout=out,
        stderr=err,
    )
    assert rc == 2
    assert json.loads(out.getvalue().strip().splitlines()[-1][len(runner.RESULT_MARKER) :])[
        "terminal_state"
    ] == "error"
    types = [json.loads(x)["type"] for x in out.getvalue().strip().splitlines()[:-1]]
    assert "error" in types


def test_run_shim_setup_failure(tmp_path: Path) -> None:
    out, err = io.StringIO(), io.StringIO()
    cfg = runner.ShimConfig(gateway_url="g", cred="c", model="m", task_prompt="t", workspace=str(tmp_path))

    def bad_setup(c: runner.ShimConfig) -> None:
        raise ValueError("nope")

    rc = runner.run_shim(
        harness="fake",
        cfg=cfg,
        argv=["true"],
        translate=_translate,
        setup=bad_setup,
        stdout=out,
        stderr=err,
    )
    assert rc == 2


def test_run_shim_translate_text(tmp_path: Path) -> None:
    """Plain-text CLIs (aider): non-JSON stdout lines hit translate_text."""
    argv = _write_stub_cli(
        tmp_path,
        "import json\n"
        "print('Applied edit to a.py')\n"
        "print('plain noise')\n"
        "print(json.dumps({'type':'finish','state':'harness_complete'}))\n",
    )

    def _text(line: str) -> list[dict[str, Any]]:
        if line.startswith("Applied edit to"):
            return [
                {
                    "type": "file_op",
                    "payload": {"op": "edit", "path": line.rsplit(" ", 1)[-1]},
                }
            ]
        return []

    out, err = io.StringIO(), io.StringIO()
    cfg = runner.ShimConfig(
        gateway_url="g", cred="c", model="m", task_prompt="t", workspace=str(tmp_path)
    )
    rc = runner.run_shim(
        harness="fake",
        cfg=cfg,
        argv=argv,
        translate=_translate,
        translate_text=_text,
        stdout=out,
        stderr=err,
    )
    assert rc == 0
    raw_lines = out.getvalue().strip().splitlines()
    types = [
        json.loads(x)["type"] for x in raw_lines if x.startswith("{")
    ]
    # "plain noise" produced nothing; "Applied edit" mapped via text hook.
    assert types == ["run_started", "file_op", "terminal_state", "terminal_state"]


def test_run_shim_translate_text_error(tmp_path: Path) -> None:
    """A raising translate_text degrades to an error event, never a crash."""
    argv = _write_stub_cli(tmp_path, "print('anything')\n")

    def _boom(line: str) -> list[dict[str, Any]]:
        raise ValueError("bad line")

    out, err = io.StringIO(), io.StringIO()
    cfg = runner.ShimConfig(
        gateway_url="g", cred="c", model="m", task_prompt="t", workspace=str(tmp_path)
    )
    rc = runner.run_shim(
        harness="fake",
        cfg=cfg,
        argv=argv,
        translate=_translate,
        translate_text=_boom,
        stdout=out,
        stderr=err,
    )
    assert rc == 0
    events = [
        json.loads(x) for x in out.getvalue().strip().splitlines() if x.startswith("{")
    ]
    assert events[1]["type"] == "error"
    assert events[1]["payload"]["stage"] == "translate"
    assert "bad line" in events[1]["payload"]["message"]


def test_run_shim_emits_run_started_first(tmp_path: Path) -> None:
    argv = _write_stub_cli(tmp_path, "pass\n")
    out = io.StringIO()
    cfg = runner.ShimConfig(gateway_url="g", cred="c", model="m", task_prompt="t", workspace=str(tmp_path))
    runner.run_shim(
        harness="fake", cfg=cfg, argv=argv, translate=_translate, stdout=out, stderr=io.StringIO()
    )
    first = json.loads(out.getvalue().strip().splitlines()[0])
    assert first["type"] == "run_started"
    assert first["payload"]["harness"] == "fake"


# ---------------------------------------------------------------------------
# parse_heg_result / translate_heg_marker
# ---------------------------------------------------------------------------


def test_parse_heg_result_last_wins() -> None:
    logs = (
        "noise\n"
        'HEG_RESULT {"terminal_state": "error", "exit_code": 2}\n'
        "more noise\n"
        'HEG_RESULT {"terminal_state": "harness_complete", "exit_code": 0}\n'
    )
    assert runner.parse_heg_result(logs) == "harness_complete"


def test_parse_heg_result_none() -> None:
    assert runner.parse_heg_result("nothing here\nMINILOOP_RESULT {}") is None


def test_translate_heg_marker() -> None:
    evs = runner.translate_heg_marker(
        ['HEG_RESULT {"terminal_state": "agent_done"}', "HEG_RESULT {bad", "other"]
    )
    assert len(evs) == 2
    assert evs[0]["type"] == "terminal_state"
    assert evs[0]["payload"]["terminal_state"] == "agent_done"
    assert "raw" in evs[1]["payload"]
