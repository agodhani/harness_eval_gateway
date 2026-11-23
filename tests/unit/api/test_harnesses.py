"""GET /api/harnesses — adapter manifest endpoint (Phase 2.5)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from api.main import app


def test_list_harnesses() -> None:
    client = TestClient(app)
    resp = client.get("/api/harnesses")
    assert resp.status_code == 200
    manifests = {m["name"]: m for m in resp.json()}
    # Every registered adapter appears, miniloop included.
    assert {
        "miniloop",
        "aider",
        "claude_code",
        "codex",
        "deepseek",
        "pi",
        "prompt",  # PR25: no-sandbox pseudo-adapter is selectable too
    } <= set(manifests)
    for name in ("aider", "claude_code", "codex", "deepseek", "pi"):
        m = manifests[name]
        for key in ("display_name", "llm_protocol", "cli", "image", "requires"):
            assert m.get(key), f"{name}: manifest missing {key!r}"
    # Protocol claims match the gateway's Phase 2.5 endpoints.
    protocols = {manifests[n]["llm_protocol"] for n in manifests}
    assert "anthropic-messages" in protocols
    assert "openai-responses" in protocols
    assert "openai-chat-completions" in protocols


def test_harnesses_prompt_manifest_is_grouped_no_sandbox() -> None:
    """PR25: the prompt manifest carries the no_sandbox flag the submit
    form groups on — and declares no image/CLI (there is nothing to run)."""
    client = TestClient(app)
    m = next(x for x in client.get("/api/harnesses").json() if x["name"] == "prompt")
    assert m["no_sandbox"] is True
    assert m["display_name"]
    assert m["llm_protocol"] == "openai-chat-completions"
    assert "image" not in m
    assert "cli" not in m
