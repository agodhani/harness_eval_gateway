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
        "claude_code",
        "codex",
        "deepseek",
        "pi",
    } <= set(manifests)
    for name in ("claude_code", "codex", "deepseek", "pi"):
        m = manifests[name]
        for key in ("display_name", "llm_protocol", "cli", "image", "requires"):
            assert m.get(key), f"{name}: manifest missing {key!r}"
    # Protocol claims match the gateway's Phase 2.5 endpoints.
    protocols = {manifests[n]["llm_protocol"] for n in manifests}
    assert "anthropic-messages" in protocols
    assert "openai-responses" in protocols
    assert "openai-chat-completions" in protocols
