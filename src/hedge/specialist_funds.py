from __future__ import annotations

"""Normalize the small, editorially curated specialist-fund configuration."""


def configured_specialists(hedge_config: dict) -> list[dict]:
    specialists = []
    seen = set()
    for raw in hedge_config.get("specialist_funds", []):
        if not isinstance(raw, dict):
            continue
        try:
            cik = int(raw["cik"])
        except (KeyError, TypeError, ValueError):
            continue
        if cik in seen:
            continue
        seen.add(cik)
        specialists.append({
            "cik": cik,
            "label": str(raw.get("label") or cik),
            "category": str(raw.get("category") or "Specialist"),
        })
    return specialists


def configured_specialist_ciks(hedge_config: dict) -> set[int]:
    return {record["cik"] for record in configured_specialists(hedge_config)}
