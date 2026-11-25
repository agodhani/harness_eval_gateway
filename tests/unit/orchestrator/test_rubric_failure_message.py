"""Rubric-draft failure messages surface the gateway's error body."""

import httpx

from orchestrator.rubric import _failure_message


def test_http_status_error_includes_gateway_body() -> None:
    request = httpx.Request("POST", "http://gateway:8080/v1/chat/completions")
    response = httpx.Response(
        404, text='{"error":{"message":"model \'nope:1b\' not found"}}', request=request
    )
    exc = httpx.HTTPStatusError("404", request=request, response=response)
    message = _failure_message(exc)
    assert "404" in message
    assert "model 'nope:1b' not found" in message


def test_other_errors_keep_type_and_text() -> None:
    assert _failure_message(ValueError("bad rubric")) == "ValueError: bad rubric"
