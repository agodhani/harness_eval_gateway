"""CLI to mint gateway bearer credentials for testing.

    uv run python -m gateway.mint_cred --run-id <uuid> --sandbox-id <uuid>
        [--ttl-seconds 86400] [--max-tokens N] [--secret S]

Prints the credential to stdout — use it as ``Authorization: Bearer <cred>``
against ``/v1/chat/completions`` and the ``/internal/*`` endpoints.

``--run-id``/``--sandbox-id`` must be UUIDs (they key into the events table).
The secret defaults to ``GATEWAY_CRED_SECRET`` (env or .env).
"""

from __future__ import annotations

import argparse
import sys
import uuid

from gateway.config import get_settings
from gateway.creds import mint_cred


def _uuid_arg(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a valid UUID: {value!r}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gateway.mint_cred",
        description="Mint an heg.* run/sandbox credential for the AI gateway.",
    )
    parser.add_argument("--run-id", required=True, type=_uuid_arg)
    parser.add_argument("--sandbox-id", required=True, type=_uuid_arg)
    parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=86_400,
        help="credential lifetime in seconds (default 86400 = 24h)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="per-cred token budget override (default: DEFAULT_RUN_TOKEN_BUDGET)",
    )
    parser.add_argument(
        "--secret",
        default=None,
        help="HMAC secret (default: GATEWAY_CRED_SECRET env / .env)",
    )
    args = parser.parse_args(argv)

    secret = args.secret or get_settings().GATEWAY_CRED_SECRET
    cred = mint_cred(
        run_id=args.run_id,
        sandbox_id=args.sandbox_id,
        secret=secret,
        ttl_seconds=args.ttl_seconds,
        max_tokens=args.max_tokens,
    )
    print(cred)
    return 0


if __name__ == "__main__":
    sys.exit(main())
