"""Run/sandbox-scoped bearer credentials for the AI gateway.

Format::

    heg.<b64url-json-payload>.<b64url-hmac-sha256>

``payload`` = ``{"run_id": str, "sandbox_id": str|null, "iat": int,
"exp": int, "max_tokens"?: int}`` — compact JSON, base64url (no padding).

The signature is ``HMAC_SHA256(secret, "heg." + payload_b64)`` — JWT-style
``header.payload`` signing where the header is the constant ``heg``.

A static dev credential ``heg-dev`` is accepted for local testing before the
orchestrator exists; it maps to fixed dev UUIDs (see DEV_RUN_ID /
DEV_SANDBOX_ID) so it flows through the UUID-typed event pipeline. Events for
those ids only persist if matching runs/sandboxes rows exist — the proxy and
``/internal/*`` endpoints work regardless.

Mint one with::

    uv run python -m gateway.mint_cred --run-id <uuid> --sandbox-id <uuid>
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass

CRED_PREFIX = "heg"

#: Static credential for local dev / smoke tests (no orchestrator yet).
DEV_CRED_TOKEN = "heg-dev"
#: Fixed UUIDs the dev credential maps to (valid UUIDs for the events table).
DEV_RUN_ID = uuid.UUID("de000000-0000-4000-8000-000000000001")
DEV_SANDBOX_ID = uuid.UUID("de000000-0000-4000-8000-000000000002")


class CredError(Exception):
    """Credential verification failed: malformed, bad signature, or expired."""


@dataclass(frozen=True)
class CredClaims:
    """Verified credential contents."""

    run_id: str
    sandbox_id: str | None
    iat: int
    exp: int
    max_tokens: int | None = None
    dev: bool = False


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _signature(secret: str, payload_b64: str) -> bytes:
    return hmac.new(
        secret.encode("utf-8"),
        f"{CRED_PREFIX}.{payload_b64}".encode(),
        hashlib.sha256,
    ).digest()


def mint_cred(
    *,
    run_id: str | uuid.UUID,
    sandbox_id: str | uuid.UUID | None,
    secret: str,
    ttl_seconds: int = 86_400,
    max_tokens: int | None = None,
    now: float | None = None,
) -> str:
    """Mint a signed credential. ``now`` is injectable for tests."""
    issued = int(time.time() if now is None else now)
    payload: dict[str, object] = {
        "run_id": str(run_id),
        "sandbox_id": str(sandbox_id) if sandbox_id is not None else None,
        "iat": issued,
        "exp": issued + int(ttl_seconds),
    }
    if max_tokens is not None:
        payload["max_tokens"] = int(max_tokens)
    payload_b64 = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return f"{CRED_PREFIX}.{payload_b64}.{_b64url_encode(_signature(secret, payload_b64))}"


def verify_cred(token: str, *, secret: str, now: float | None = None) -> CredClaims:
    """Verify a credential; raises :class:`CredError` on any failure.

    Accepts the static ``heg-dev`` token (see module docstring).
    """
    if token == DEV_CRED_TOKEN:
        return CredClaims(
            run_id=str(DEV_RUN_ID),
            sandbox_id=str(DEV_SANDBOX_ID),
            iat=0,
            exp=2**31 - 1,
            dev=True,
        )

    parts = token.split(".")
    if len(parts) != 3 or parts[0] != CRED_PREFIX:
        raise CredError("malformed credential")
    _, payload_b64, sig_b64 = parts

    try:
        sig = _b64url_decode(sig_b64)
    except Exception as exc:
        raise CredError("malformed signature encoding") from exc
    if not hmac.compare_digest(sig, _signature(secret, payload_b64)):
        raise CredError("bad signature")

    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception as exc:
        raise CredError("malformed payload encoding") from exc
    if not isinstance(payload, dict):
        raise CredError("payload is not an object")

    run_id = payload.get("run_id")
    sandbox_id = payload.get("sandbox_id")
    iat = payload.get("iat")
    exp = payload.get("exp")
    max_tokens = payload.get("max_tokens")
    if not run_id or not isinstance(run_id, str):
        raise CredError("missing run_id claim")
    if sandbox_id is not None and not isinstance(sandbox_id, str):
        raise CredError("sandbox_id claim must be a string or null")
    if not isinstance(iat, int) or not isinstance(exp, int):
        raise CredError("iat/exp claims must be integers")
    if max_tokens is not None and (not isinstance(max_tokens, int) or max_tokens < 0):
        raise CredError("max_tokens claim must be a non-negative integer")

    now_ts = time.time() if now is None else now
    if now_ts >= exp:
        raise CredError("credential expired")

    return CredClaims(
        run_id=run_id,
        sandbox_id=sandbox_id,
        iat=iat,
        exp=exp,
        max_tokens=max_tokens,
    )
