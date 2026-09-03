from __future__ import annotations

"""Normalize the small, editorially curated specialist- and watchlist-fund config."""


def _configured_funds(hedge_config: dict, key: str, default_category: str) -> list[dict]:
    records = []
    seen = set()
    for raw in hedge_config.get(key, []) or []:
        if not isinstance(raw, dict):
            continue
        try:
            cik = int(raw["cik"])
        except (KeyError, TypeError, ValueError):
            continue
        if cik in seen:
            continue
        seen.add(cik)
        records.append({
            "cik": cik,
            "label": str(raw.get("label") or cik),
            "category": str(raw.get("category") or default_category),
        })
    return records


def configured_specialists(hedge_config: dict) -> list[dict]:
    return _configured_funds(hedge_config, "specialist_funds", "Specialist")


def configured_specialist_ciks(hedge_config: dict) -> set[int]:
    return {record["cik"] for record in configured_specialists(hedge_config)}


def configured_watchlist(hedge_config: dict) -> list[dict]:
    """General high-conviction pins — not a thematic specialist set, so they do
    not participate in specialist-overlap."""
    return _configured_funds(hedge_config, "watchlist_funds", "Watchlist")


def configured_watchlist_ciks(hedge_config: dict) -> set[int]:
    return {record["cik"] for record in configured_watchlist(hedge_config)}


def configured_pin_ciks(hedge_config: dict) -> set[int]:
    """Every editorially pinned CIK: specialists + watchlist + explicit pool/watchlist pins."""
    pins = configured_specialist_ciks(hedge_config) | configured_watchlist_ciks(hedge_config)
    for key in ("pool_pins", "watchlist_pins"):
        for raw in hedge_config.get(key, []) or []:
            try:
                pins.add(int(raw))
            except (TypeError, ValueError):
                continue
    return pins
