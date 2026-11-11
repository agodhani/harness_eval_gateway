"""gateway/creds.py — mint/verify roundtrip, signature, expiry, dev cred."""

from __future__ import annotations

import uuid

import pytest

from gateway.creds import (
    DEV_CRED_TOKEN,
    DEV_RUN_ID,
    DEV_SANDBOX_ID,
    CredError,
    mint_cred,
    verify_cred,
)

SECRET = "test-secret"
RUN_ID = uuid.uuid4()
SANDBOX_ID = uuid.uuid4()


def test_mint_verify_roundtrip() -> None:
    token = mint_cred(
        run_id=RUN_ID, sandbox_id=SANDBOX_ID, secret=SECRET, ttl_seconds=60
    )
    assert token.startswith("heg.")
    claims = verify_cred(token, secret=SECRET)
    assert claims.run_id == str(RUN_ID)
    assert claims.sandbox_id == str(SANDBOX_ID)
    assert claims.max_tokens is None
    assert claims.dev is False
    assert claims.exp > claims.iat


def test_max_tokens_claim_roundtrip() -> None:
    token = mint_cred(
        run_id=RUN_ID,
        sandbox_id=SANDBOX_ID,
        secret=SECRET,
        ttl_seconds=60,
        max_tokens=1234,
    )
    claims = verify_cred(token, secret=SECRET)
    assert claims.max_tokens == 1234


def test_wrong_secret_rejected() -> None:
    token = mint_cred(run_id=RUN_ID, sandbox_id=SANDBOX_ID, secret=SECRET)
    with pytest.raises(CredError, match="signature"):
        verify_cred(token, secret="other-secret")


def test_tampered_payload_rejected() -> None:
    token = mint_cred(run_id=RUN_ID, sandbox_id=SANDBOX_ID, secret=SECRET)
    prefix, _payload, sig = token.split(".")
    # Forge a payload with a different run_id, keep the old signature.
    forged = mint_cred(run_id=uuid.uuid4(), sandbox_id=SANDBOX_ID, secret="x")
    forged_payload = forged.split(".")[1]
    with pytest.raises(CredError):
        verify_cred(f"{prefix}.{forged_payload}.{sig}", secret=SECRET)


def test_expired_rejected() -> None:
    token = mint_cred(
        run_id=RUN_ID, sandbox_id=SANDBOX_ID, secret=SECRET, ttl_seconds=-10
    )
    with pytest.raises(CredError, match="expired"):
        verify_cred(token, secret=SECRET)


def test_expiry_boundary() -> None:
    now = 1_000_000.0
    token = mint_cred(
        run_id=RUN_ID,
        sandbox_id=SANDBOX_ID,
        secret=SECRET,
        ttl_seconds=100,
        now=now,
    )
    # just before exp -> ok; at/after exp -> rejected
    verify_cred(token, secret=SECRET, now=now + 99)
    with pytest.raises(CredError, match="expired"):
        verify_cred(token, secret=SECRET, now=now + 100)


def test_malformed_tokens_rejected() -> None:
    for bad in ["", "heg", "heg.", "heg.x", "abc.def.ghi", "heg..sig"]:
        with pytest.raises(CredError):
            verify_cred(bad, secret=SECRET)


def test_dev_cred_accepted_with_any_secret() -> None:
    claims = verify_cred(DEV_CRED_TOKEN, secret="anything")
    assert claims.dev is True
    assert claims.run_id == str(DEV_RUN_ID)
    assert claims.sandbox_id == str(DEV_SANDBOX_ID)
