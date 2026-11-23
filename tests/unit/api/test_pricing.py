"""PR28 — api/pricing.py layered price book (``model_prices`` over static).

Hermetic: ``PriceBook`` consumes plain ``ModelPriceRow`` objects — no DB.
Covers the glob matcher's namespace rule, first-match precedence, the
table-over-static layering, PR21's unpriced-is-None honesty, and the
cache-read rate split in ``estimate_cost_usd``.
"""

from __future__ import annotations

import pytest

from api.pricing import (
    PRICE_TABLE,
    ModelPrice,
    PriceBook,
    estimate_cost_usd,
    glob_matches,
    resolve_price_model,
)
from db.models import ModelPriceRow


def _row(
    glob: str,
    *,
    inp: float = 1.0,
    out: float = 2.0,
    cache_read: float | None = None,
    local: bool = False,
    source: str = "user",
) -> ModelPriceRow:
    return ModelPriceRow(
        model_glob=glob,
        input_per_mtok_usd=inp,
        output_per_mtok_usd=out,
        cache_read_per_mtok_usd=cache_read,
        local=local,
        source=source,
    )


# ---------------------------------------------------------------------------
# glob_matches
# ---------------------------------------------------------------------------


def test_glob_matches_case_insensitive() -> None:
    assert glob_matches("GPT-4*", "gpt-4o")
    assert glob_matches("qwen3:*", "QWEN3:8B")


def test_glob_exact_and_star() -> None:
    assert glob_matches("gpt-4o", "gpt-4o")
    assert not glob_matches("gpt-4o", "gpt-4o-mini")
    assert glob_matches("*", "anything")


def test_glob_namespace_rule() -> None:
    """A bare (no ``/``) pattern must not silently reprice vendor-namespaced
    ids — ``gpt-4*`` never matches ``openai/gpt-4o``; a namespaced pattern
    does."""
    assert not glob_matches("gpt-4*", "openai/gpt-4o")
    assert glob_matches("openai/*", "openai/gpt-4o")
    assert glob_matches("openai/gpt-4o", "openai/gpt-4o")


def test_glob_empty_never_matches() -> None:
    assert not glob_matches("", "gpt-4o")
    assert not glob_matches("gpt-*", "")
    assert not glob_matches("  ", "gpt-4o")


# ---------------------------------------------------------------------------
# PriceBook — first-match precedence + static fallback
# ---------------------------------------------------------------------------


def test_first_matching_row_wins() -> None:
    """Rows scan in declaration order: an earlier broad glob beats a later
    specific one — declaration order IS the precedence knob."""
    book = PriceBook([_row("gpt-4*", inp=9.0), _row("gpt-4o", inp=1.0)])
    assert book.key_for("gpt-4o") == "gpt-4*"
    assert book.price_for(book.key_for("gpt-4o")).input_per_mtok_usd == 9.0


def test_later_row_catches_what_first_misses() -> None:
    book = PriceBook([_row("claude-*", inp=9.0), _row("gpt-4*", inp=3.0)])
    assert book.key_for("gpt-4o") == "gpt-4*"


def test_row_rate_overrides_static_table_rate() -> None:
    """A glob covering a static key shadows the static rate — layering, not
    just extension."""
    static_rate = PRICE_TABLE["gpt-4o"].input_per_mtok_usd
    book = PriceBook([_row("gpt-4o", inp=static_rate + 5.0)])
    price = book.price_for(book.key_for("gpt-4o"))
    assert price.input_per_mtok_usd == static_rate + 5.0


def test_static_fallback_when_no_glob_matches() -> None:
    book = PriceBook([_row("claude-*")])
    assert book.key_for("gpt-4o") == "gpt-4o"
    assert book.price_for("gpt-4o") == PRICE_TABLE["gpt-4o"]


def test_unlisted_model_is_unpriced() -> None:
    """PR21 honesty survives PR28: no glob + no static key → None, never a
    fallback rate."""
    book = PriceBook([_row("gpt-*")])
    assert book.key_for("my-fine-tune-7b") is None
    assert book.price_for(book.key_for("my-fine-tune-7b")) is None
    assert book.key_for(None) is None


def test_local_row_marks_free() -> None:
    book = PriceBook([_row("qwen*", local=True)])
    price = book.price_for(book.key_for("qwen3:8b"))
    assert price.local is True


def test_table_merges_rows_over_static() -> None:
    book = PriceBook([_row("gpt-4o", inp=99.0), _row("custom-*", inp=4.0)])
    merged = book.table()
    # glob row shadowing a static key shows the EFFECTIVE (row) rate
    assert merged["gpt-4o"].input_per_mtok_usd == 99.0
    # new glob appears as its own key alongside the static table
    assert merged["custom-*"].input_per_mtok_usd == 4.0
    assert set(PRICE_TABLE) <= set(merged)


# ---------------------------------------------------------------------------
# resolve_price_model through the layered book
# ---------------------------------------------------------------------------


def test_resolve_explicit_glob_pick() -> None:
    book = PriceBook([_row("gpt-*")])
    # an explicit ?price_model= may be a table glob OR a model a glob covers
    assert resolve_price_model("gpt-*", "qwen3:8b", book) == "gpt-*"
    assert resolve_price_model("gpt-4o", "qwen3:8b", book) == "gpt-*"


def test_resolve_explicit_pick_names_its_own_row() -> None:
    """A pick naming a row glob verbatim selects THAT row's rate — the
    pick names the row itself, so an earlier broader glob can't shadow
    it. Model-like picks keep codified first-match scanning."""
    book = PriceBook([_row("*", inp=1.0), _row("acme-*", inp=9.0)])
    assert resolve_price_model("acme-*", "whatever", book) == "acme-*"
    assert resolve_price_model("acme-7b", "whatever", book) == "*"
    assert book.key_for("acme-7b") == "*"


def test_resolve_explicit_pick_names_row_case_insensitively() -> None:
    """A case-variant pick still names the row — row identity is
    case-insensitive (upsert and glob matching both are), so an earlier
    broader glob can't shadow it through casing alone."""
    book = PriceBook([_row("*", inp=1.0), _row("acme-*", inp=9.0)])
    assert resolve_price_model("ACME-*", "whatever", book) == "acme-*"
    # the resolved key prices at that row's rate
    price = book.price_for("ACME-*")
    assert price is not None and price.input_per_mtok_usd == 9.0


def test_resolve_run_model_via_book() -> None:
    book = PriceBook([_row("qwen*")])
    assert resolve_price_model(None, "qwen3:8b", book) == "qwen*"


def test_resolve_unpriced_returns_none() -> None:
    assert resolve_price_model("nope-1b", "qwen3:8b") is None
    assert resolve_price_model(None, "nope-1b") is None


# ---------------------------------------------------------------------------
# estimate_cost_usd — cache-read rate split
# ---------------------------------------------------------------------------


def test_cache_read_bills_at_cache_rate() -> None:
    """Declared cache rate moves those tokens OFF the input rate — they're
    inside input_tokens already, never double-billed."""
    price = ModelPrice(
        input_per_mtok_usd=10.0,
        output_per_mtok_usd=20.0,
        cache_read_per_mtok_usd=1.0,
    )
    cost = estimate_cost_usd(
        price, input_tokens=1_000_000, output_tokens=100_000,
        cache_read_tokens=400_000,
    )
    # 600k @ $10 + 400k @ $1 + 100k @ $20, all /1e6 → $6 + $0.4 + $2 = $8.4
    assert cost == pytest.approx(8.4)


def test_no_cache_rate_bills_everything_at_input() -> None:
    price = ModelPrice(input_per_mtok_usd=10.0, output_per_mtok_usd=20.0)
    cost = estimate_cost_usd(
        price, input_tokens=1_000_000, output_tokens=100_000,
        cache_read_tokens=400_000,
    )
    assert cost == pytest.approx(12.0)  # cache tokens stay at input rate


def test_cache_read_clamped_to_input_total() -> None:
    """A usage payload reporting more cached than prompt tokens can't go
    negative — the excess is ignored."""
    price = ModelPrice(
        input_per_mtok_usd=10.0,
        output_per_mtok_usd=20.0,
        cache_read_per_mtok_usd=1.0,
    )
    cost = estimate_cost_usd(
        price, input_tokens=100, output_tokens=0, cache_read_tokens=999
    )
    assert cost == pytest.approx(100 * 1.0 / 1_000_000)


def test_local_price_costs_zero() -> None:
    price = ModelPrice(input_per_mtok_usd=0.0, output_per_mtok_usd=0.0, local=True)
    assert estimate_cost_usd(price, input_tokens=10**9, output_tokens=10**9) == 0.0
