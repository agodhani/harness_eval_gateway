"""F9 — model price table + cost estimation for run analytics.

Static per-model USD rates (per million tokens, billed sides only). Local
models carry ``local=True`` + zero rates so the UI can label them
"local — $0" instead of implying a spend that doesn't exist.

The table is a module constant so tests (and the analytics router) can
assert/iterate it directly. Rates are ballpark list prices — they power
*estimates* for harness comparison, not invoices.
"""

from __future__ import annotations

from pydantic import BaseModel


class ModelPrice(BaseModel):
    """USD per million tokens for one model."""

    input_per_mtok_usd: float
    output_per_mtok_usd: float
    # Free/local inference (Ollama) — still metered for tokens, never billed.
    local: bool = False


# Insertion order matters: the first entry is the fallback price model when a
# run's model isn't listed and no explicit ?price_model= is given.
PRICE_TABLE: dict[str, ModelPrice] = {
    "claude-sonnet-4.5": ModelPrice(input_per_mtok_usd=3.0, output_per_mtok_usd=15.0),
    "claude-opus-4.1": ModelPrice(input_per_mtok_usd=15.0, output_per_mtok_usd=75.0),
    "gpt-4o": ModelPrice(input_per_mtok_usd=2.5, output_per_mtok_usd=10.0),
    "gpt-4o-mini": ModelPrice(input_per_mtok_usd=0.15, output_per_mtok_usd=0.6),
    "qwen3:8b": ModelPrice(input_per_mtok_usd=0.0, output_per_mtok_usd=0.0, local=True),
}


def estimate_cost_usd(
    price: ModelPrice, *, input_tokens: int, output_tokens: int
) -> float:
    """Estimated spend in USD for a token split at the given rate."""
    return (
        input_tokens * price.input_per_mtok_usd
        + output_tokens * price.output_per_mtok_usd
    ) / 1_000_000


def resolve_price_model(price_model: str | None, run_model: str) -> str | None:
    """Pick the price-table key to cost a run at.

    ``price_model`` (the query param) wins when supplied and known; otherwise
    the run's own model when it's in the table; otherwise the first table
    entry. Returns None when an explicit ``price_model`` isn't in the table —
    the router turns that into a 422.
    """
    if price_model is not None:
        return price_model if price_model in PRICE_TABLE else None
    return run_model if run_model in PRICE_TABLE else next(iter(PRICE_TABLE))
