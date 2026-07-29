from __future__ import annotations

"""Shared ticker-universe helpers for unified Congress + hedge stock pages.

The rendered stock-page set is intentionally bounded:

* every ticker in a congressional disclosure (including sale/exchange-only
  names, because current-report and member-page rows link to them); plus
* hedge names selected by ``generate_hedge_stocks`` as top alpha drivers or
  skill-weighted new buys.

Keeping this definition in one place prevents the renderer from creating pages
that the enrichment, price, and chart stages do not know about.
"""

from typing import Iterable

from utils import DATA_DIR, load_json, load_json_gz

LEDGER_PATH = DATA_DIR / "transactions.json"
HEDGE_PAGES_PATH = DATA_DIR / "hedge" / "stock_pages.json"
HEDGE_HOLDERS_PATH = DATA_DIR / "hedge" / "stock_holders.json.gz"


def _rows(ledger: dict | Iterable[dict] | None = None) -> list[dict]:
    if ledger is None:
        if not LEDGER_PATH.exists():
            return []
        ledger = load_json(LEDGER_PATH)
    return list(ledger.values()) if isinstance(ledger, dict) else list(ledger)


def congressional_tickers(ledger: dict | Iterable[dict] | None = None,
                          purchases_only: bool = False) -> set[str]:
    """Return normalized tickers in the congressional ledger."""
    return {
        (row.get("ticker") or "").upper()
        for row in _rows(ledger)
        if row.get("ticker") and (not purchases_only or row.get("tx_type") == "P")
    }


def hedge_featured_tickers(require_data: bool = True) -> set[str]:
    """Return the bounded hedge-feature list, optionally requiring holder data."""
    if not HEDGE_PAGES_PATH.exists():
        return set()
    featured = {
        (ticker or "").upper()
        for ticker in load_json(HEDGE_PAGES_PATH).get("tickers", [])
        if ticker
    }
    if require_data:
        if not HEDGE_HOLDERS_PATH.exists():
            return set()
        featured &= set(load_json_gz(HEDGE_HOLDERS_PATH))
    return featured


def stock_page_tickers(ledger: dict | Iterable[dict] | None = None) -> set[str]:
    """Tickers eligible for a rendered page."""
    return congressional_tickers(ledger) | hedge_featured_tickers()


def price_tickers(ledger: dict | Iterable[dict] | None = None,
                  benchmark: str | None = None) -> set[str]:
    """Tickers that need price histories for performance or a stock page."""
    out = congressional_tickers(ledger) | hedge_featured_tickers()
    if benchmark:
        out.add(benchmark.upper())
    return out
