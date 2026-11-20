"""F9 — model price table + cost estimation for run analytics.

Static per-model USD rates (per million tokens, billed sides only). Local
models carry ``local=True`` + zero rates so the UI can label them
"local — $0" instead of implying a spend that doesn't exist.

The table is a module constant so tests (and the analytics router) can
assert/iterate it directly. Rates are ballpark list prices — they power
*estimates* for harness comparison, not invoices.

Models absent from the table are **unpriced** (PR21): resolvers return
``None`` and analytics emit a null cost rather than billing unknown traffic
at an arbitrary fallback rate.

PR28 — the static table is now the FALLBACK layer under the persistent
``model_prices`` table (``PriceBook``). Each ``model_prices`` row carries an
fnmatch ``model_glob``; rows are scanned in declaration order (``created_at``,
``id``) and the FIRST glob hit wins — a row's rate then overrides whatever
the static table would have guessed for that model, and a model no glob or
static key covers stays unpriced (null cost, never a fallback rate).
"""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING, Iterable

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from db.models import ModelPriceRow


class ModelPrice(BaseModel):
    """USD per million tokens for one model (or one glob)."""

    input_per_mtok_usd: float
    output_per_mtok_usd: float
    # Discounted rate for cache-read input tokens (Anthropic prompt caching,
    # OpenRouter ``input_cache_read``); None = bill them at the input rate.
    cache_read_per_mtok_usd: float | None = None
    # Free/local inference (Ollama) — still metered for tokens, never billed.
    local: bool = False


PRICE_TABLE: dict[str, ModelPrice] = {
    "claude-sonnet-4.5": ModelPrice(input_per_mtok_usd=3.0, output_per_mtok_usd=15.0),
    "claude-opus-4.1": ModelPrice(input_per_mtok_usd=15.0, output_per_mtok_usd=75.0),
    "gpt-4o": ModelPrice(input_per_mtok_usd=2.5, output_per_mtok_usd=10.0),
    "gpt-4o-mini": ModelPrice(input_per_mtok_usd=0.15, output_per_mtok_usd=0.6),
    "qwen3:8b": ModelPrice(input_per_mtok_usd=0.0, output_per_mtok_usd=0.0, local=True),
}


def glob_matches(pattern: str, model: str) -> bool:
    """Case-insensitive fnmatch with the PROVIDER_ROUTES namespace rule.

    A pattern containing no ``/`` is a LOCAL-name pattern and never matches
    a ``vendor/model`` id — ``gpt-4*`` must not silently reprice OpenRouter's
    ``openai/gpt-4o``; write ``openai/*`` or the exact namespaced id for
    that. Empty pattern/model never matches.
    """
    pat = pattern.strip().lower()
    name = model.strip().lower()
    if not pat or not name:
        return False
    if "/" in name and "/" not in pat:
        return False
    return fnmatch.fnmatchcase(name, pat)


def estimate_cost_usd(
    price: ModelPrice,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
) -> float:
    """Estimated spend in USD for a token split at the given rate.

    ``cache_read_tokens`` are counted inside ``input_tokens`` by every usage
    payload the gateway emits (``usage_from_anthropic`` folds
    ``cache_read_input_tokens`` into the prompt side), so when a cache rate
    is declared they MOVE to it — never double-billed; with no declared
    cache rate everything bills at the input rate exactly as before.
    """
    cache_rate = price.cache_read_per_mtok_usd
    if cache_rate is None or cache_read_tokens <= 0:
        billable_input = input_tokens
        cached = 0
        cache_rate = 0.0
    else:
        cached = min(cache_read_tokens, input_tokens)
        billable_input = input_tokens - cached
    return (
        billable_input * price.input_per_mtok_usd
        + cached * cache_rate
        + output_tokens * price.output_per_mtok_usd
    ) / 1_000_000


def _row_price(row: ModelPriceRow) -> ModelPrice:
    return ModelPrice(
        input_per_mtok_usd=row.input_per_mtok_usd,
        output_per_mtok_usd=row.output_per_mtok_usd,
        cache_read_per_mtok_usd=row.cache_read_per_mtok_usd,
        local=bool(row.local),
    )


class PriceBook:
    """Effective price lookup: ``model_prices`` globs over ``PRICE_TABLE``.

    Built once per request from the table's rows in declaration order.
    ``key_for`` returns the resolved PRICE KEY for a model — the first
    matching row's ``model_glob`` (the glob is the key: two models matching
    it share the rate and the same ``price_model`` in analytics), else the
    model verbatim when it is a static table key, else ``None`` (unpriced,
    PR21 — no guessed rate).
    """

    def __init__(self, rows: Iterable[ModelPriceRow]) -> None:
        self._rows = list(rows)
        self._by_glob = {r.model_glob: r for r in self._rows}

    def key_for(self, model: str | None) -> str | None:
        """Price key for ``model``: first matching glob, static key, or None."""
        if not model:
            return None
        for row in self._rows:
            if glob_matches(row.model_glob, model):
                return row.model_glob
        return model if model in PRICE_TABLE else None

    def price_for(self, key: str | None) -> ModelPrice | None:
        """Rate for a resolved key — a row's glob first, then the static key."""
        if key is None:
            return None
        row = self._by_glob.get(key)
        if row is not None:
            return _row_price(row)
        return PRICE_TABLE.get(key)

    def table(self) -> dict[str, ModelPrice]:
        """Merged view for the what-if picker: static keys + row globs.

        A glob equal to a static key shadows the static rate — the book is
        layered, so the picker's option must show the effective rate.
        """
        out: dict[str, ModelPrice] = dict(PRICE_TABLE)
        for row in self._rows:
            out[row.model_glob] = _row_price(row)
        return out


def load_book(session: Session) -> PriceBook:
    """The request's price book: every ``model_prices`` row in declaration
    order (first-match scan order)."""
    from db.models import ModelPriceRow

    rows = (
        session.execute(
            select(ModelPriceRow).order_by(ModelPriceRow.created_at, ModelPriceRow.id)
        )
        .scalars()
        .all()
    )
    return PriceBook(rows)


def resolve_price_model(
    price_model: str | None,
    run_model: str,
    book: PriceBook | None = None,
) -> str | None:
    """Pick the price-table key to cost a run at.

    ``price_model`` (the query param) wins when supplied and known —
    resolved through the layered book, so a ``model_prices`` glob is a valid
    explicit pick and can reprice a whole run at its row's rate. Otherwise
    the run's own model resolves the same way (glob hit → the glob, static
    key → itself). Returns None when the resolved model isn't listed —
    an unknown explicit ``price_model`` becomes a 422, while an unlisted
    run model means *unpriced*: no cost estimate is produced rather than
    billing the traffic at another model's rate.
    """
    if book is None:
        book = PriceBook(())
    if price_model is not None:
        return book.key_for(price_model)
    return book.key_for(run_model)
