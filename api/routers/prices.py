"""PR28 — persistent price table: user-editable glob rates + OpenRouter sync.

``GET /api/prices`` lists every ``model_prices`` row (declaration order —
the first-match scan order ``PriceBook`` uses) alongside the static
``PRICE_TABLE`` fallback layer and whether an OpenRouter catalog sync is
available under the current provider config.

``PUT /api/prices`` upserts a user rate by ``model_glob`` (the row's glob is
the price key analytics resolves — ``gpt-4*`` reprices every ``gpt-4o*`` cell
at once). User rows layer OVER the static table; deleting one restores the
static fallback for whatever it covered. ``local=true`` marks free inference
— every surface renders the row "local — $0" — so PUT 422s it beside any
nonzero rate rather than billing money under a free label.

``DELETE /api/prices/{id}`` removes one row.

``POST /api/prices/sync`` pulls the OpenRouter catalog through the GATEWAY
(``GET /v1/models`` with an ``x-heg-provider: openrouter`` service-cred
override — the api never talks to providers directly, so the request rides
the same provider/auth path as metering traffic). OpenRouter ``pricing.*``
values are USD PER TOKEN strings; they're stored as per-Mtok rates to match
the rest of the table. Entries missing either the prompt or the completion
rate are skipped (a half-priced model is as dishonest as a free-priced
paid one). Sync only touches ``source="openrouter"`` rows — ``user`` rows
are never clobbered.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.config import get_settings
from api.deps import get_db
from api.pricing import PRICE_TABLE, ModelPrice
from api.routers.models import GatewayClient, _error_message, _service_cred
from db.models import ModelPriceRow

router = APIRouter(tags=["prices"])

DbSession = Annotated[Session, Depends(get_db)]

# Cap on catalog entries persisted per sync call — OpenRouter lists a few
# hundred; the bound is defensive (a misbehaving upstream can't flood the
# table), not a real limit.
_MAX_SYNC_ROWS = 5000


class PriceEntryOut(BaseModel):
    """One ``model_prices`` row (declaration-order position drives matching)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    model_glob: str
    input_per_mtok_usd: float
    output_per_mtok_usd: float
    cache_read_per_mtok_usd: float | None
    local: bool
    source: str
    created_at: datetime
    updated_at: datetime


class PriceUpsertIn(BaseModel):
    """PUT /api/prices body — create or replace the rate for ``model_glob``.

    ``local`` marks free inference (every surface renders the row
    "local — $0"), so pairing it with a nonzero rate is refused rather
    than coerced — accepting it would bill money under a free label.
    Zero/null rates stay valid.
    """

    model_glob: Annotated[str, StringConstraints(strip_whitespace=True)] = Field(
        min_length=1, max_length=255
    )
    input_per_mtok_usd: float = Field(default=0.0, ge=0)
    output_per_mtok_usd: float = Field(default=0.0, ge=0)
    # Nullable like the column: absent/null bills cached input at the input rate.
    cache_read_per_mtok_usd: float | None = Field(default=None, ge=0)
    local: bool = False

    @model_validator(mode="after")
    def _local_forbids_nonzero_rates(self) -> PriceUpsertIn:
        if not self.local:
            return self
        nonzero = [
            name
            for name, rate in (
                ("input_per_mtok_usd", self.input_per_mtok_usd),
                ("output_per_mtok_usd", self.output_per_mtok_usd),
                ("cache_read_per_mtok_usd", self.cache_read_per_mtok_usd),
            )
            if rate
        ]
        if nonzero:
            raise ValueError(
                "local rows are never billed — rates must be 0 or null "
                f"(nonzero: {', '.join(nonzero)})"
            )
        return self


class PricesOut(BaseModel):
    """GET /api/prices — table rows + the static fallback layer."""

    prices: list[PriceEntryOut]
    static: dict[str, ModelPrice]
    # POST /api/prices/sync is only meaningful when openrouter is configured.
    sync_available: bool


class PriceSyncOut(BaseModel):
    """POST /api/prices/sync — what the catalog pull changed."""

    synced: int  # added + updated
    added: int
    updated: int
    skipped_user: int  # existing source="user" rows left untouched
    skipped_unpriced: int  # remote entries with no usable pricing block
    remote_count: int
    prices: list[PriceEntryOut]


def _price_rows(session: Session) -> list[ModelPriceRow]:
    return (
        session.execute(
            select(ModelPriceRow).order_by(ModelPriceRow.created_at, ModelPriceRow.id)
        )
        .scalars()
        .all()
    )


def _openrouter_configured(settings: Any) -> bool:
    """OpenRouter is a configured provider for this deployment.

    The api process sees provider config (``UPSTREAM_PROVIDER`` /
    ``PROVIDER_ROUTES``) but NOT the provider API keys — the gateway owns
    those; this is a config check, not a liveness check.
    """
    if str(settings.UPSTREAM_PROVIDER).strip().lower() == "openrouter":
        return True
    raw = (settings.PROVIDER_ROUTES or "").strip()
    if not raw:
        return False
    try:
        routes = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(routes, dict):
        return False
    return any(str(v).strip().lower() == "openrouter" for v in routes.values())


def _per_mtok(value: Any) -> float | None:
    """OpenRouter ``pricing.*`` value (USD per token, as a string/number) →
    USD per million tokens; None when absent or non-numeric."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(rate) or rate < 0:
        return None
    return rate * 1_000_000


@router.get("/api/prices", response_model=PricesOut)
def list_prices(session: DbSession) -> PricesOut:
    return PricesOut(
        prices=[PriceEntryOut.model_validate(r) for r in _price_rows(session)],
        static=PRICE_TABLE,
        sync_available=_openrouter_configured(get_settings()),
    )


@router.put("/api/prices", response_model=PriceEntryOut)
def upsert_price(body: PriceUpsertIn, session: DbSession) -> PriceEntryOut:
    """Create/update the user rate for ``model_glob``.

    Upsert by glob — a matching row keeps its declaration-order position
    (first-match scan order) while its rate updates; editing a synced row
    flips its source to ``user`` so catalog sync never overwrites it.
    """
    # Post-parse check: a field-level inf rejection would serialize the
    # echoed body into the 422 payload → 500 (ge=0 already covers NaN).
    if any(
        rate is not None and not math.isfinite(rate)
        for rate in (
            body.input_per_mtok_usd,
            body.output_per_mtok_usd,
            body.cache_read_per_mtok_usd,
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="rates must be finite numbers",
        )
    row = (
        session.execute(
            select(ModelPriceRow)
            .where(func.lower(ModelPriceRow.model_glob) == body.model_glob.lower())
            .order_by(ModelPriceRow.created_at, ModelPriceRow.id)
        )
        .scalars()
        .first()
    )
    if row is None:
        row = ModelPriceRow(model_glob=body.model_glob, source="user")
        session.add(row)
    else:
        row.source = "user"
    row.input_per_mtok_usd = body.input_per_mtok_usd
    row.output_per_mtok_usd = body.output_per_mtok_usd
    row.cache_read_per_mtok_usd = body.cache_read_per_mtok_usd
    row.local = body.local
    session.commit()
    session.refresh(row)
    return PriceEntryOut.model_validate(row)


@router.delete("/api/prices/{price_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_price(price_id: uuid.UUID, session: DbSession) -> None:
    row = session.get(ModelPriceRow, price_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="price not found")
    session.delete(row)
    session.commit()


@router.post("/api/prices/sync", response_model=PriceSyncOut)
async def sync_prices(session: DbSession, client: GatewayClient) -> PriceSyncOut:
    """Sync the OpenRouter catalog's pricing into ``model_prices``.

    Rides the gateway's ``/v1/models`` pass-through with an
    ``x-heg-provider: openrouter`` service-cred override (service creds are
    the only cred class the override gate accepts — same pattern as
    ``GET /api/models?provider=``). Entries missing a parseable prompt OR
    completion rate are skipped — inserting a zero or half rate would
    price a probably-paid model as free, which is the guessing PR28
    exists to kill. Existing ``source="user"`` rows (even when the glob
    equals a remote id) are never overwritten.
    """
    settings = get_settings()
    if not _openrouter_configured(settings):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "openrouter is not a configured provider — set "
                "UPSTREAM_PROVIDER=openrouter or route a model glob to it "
                "via PROVIDER_ROUTES"
            ),
        )
    headers = {
        "Authorization": f"Bearer {_service_cred(settings.GATEWAY_CRED_SECRET)}",
        "x-heg-provider": "openrouter",
    }
    try:
        resp = await client.get("/v1/models", headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"cannot reach gateway at {settings.GATEWAY_URL}: {exc}",
        ) from exc
    if resp.status_code != status.HTTP_200_OK:
        raise HTTPException(
            status_code=resp.status_code, detail=await _error_message(resp)
        )
    try:
        body = resp.json()
    except ValueError:
        body = {}
    data = body.get("data") if isinstance(body, dict) else None
    entries = [m for m in (data if isinstance(data, list) else []) if isinstance(m, dict)]

    existing: dict[str, ModelPriceRow] = {}
    for r in _price_rows(session):
        existing.setdefault(r.model_glob.lower(), r)
    added = updated = skipped_user = skipped_unpriced = 0
    for entry in entries[:_MAX_SYNC_ROWS]:
        mid = entry.get("id")
        if not isinstance(mid, str):
            continue
        mid = mid.strip()
        # model_glob is String(255) — an oversized id can't persist, so
        # skip it like a malformed entry rather than failing the commit.
        if not mid or len(mid) > 255:
            continue
        pricing = entry.get("pricing")
        pricing = pricing if isinstance(pricing, dict) else {}
        inp = _per_mtok(pricing.get("prompt"))
        out = _per_mtok(pricing.get("completion"))
        if inp is None or out is None:
            # Either side missing → skip rather than bill it $0 — a
            # half-priced model is the guessing PR28 exists to kill.
            skipped_unpriced += 1
            continue
        row = existing.get(mid.lower())
        if row is not None and row.source == "user":
            skipped_user += 1
            continue
        if row is None:
            row = ModelPriceRow(model_glob=mid, source="openrouter")
            session.add(row)
            existing[row.model_glob.lower()] = row
            added += 1
        else:
            updated += 1
        row.input_per_mtok_usd = inp or 0.0
        row.output_per_mtok_usd = out or 0.0
        # OpenRouter keys the cache discount "input_cache_read"; absent →
        # NULL → those tokens bill at the input rate like before.
        row.cache_read_per_mtok_usd = _per_mtok(pricing.get("input_cache_read"))
        row.local = False
    session.commit()
    return PriceSyncOut(
        synced=added + updated,
        added=added,
        updated=updated,
        skipped_user=skipped_user,
        skipped_unpriced=skipped_unpriced,
        remote_count=len(entries),
        prices=[PriceEntryOut.model_validate(r) for r in _price_rows(session)],
    )
