from __future__ import annotations

"""
Pure classification helpers (no I/O, no API):

  * sic_to_industry(sic_code)         -> one of ~24 GICS-style industry groups
  * cap_bucket(market_cap)            -> mega / large / mid / small / micro
  * committee_to_industries(id, name) -> set of industry groups a committee
                                         (or subcommittee) has jurisdiction over

The committee mapping combines a curated table keyed by thomas_id (parent
committees) with keyword matching on the committee/subcommittee name, so
subcommittees get sharper jurisdiction without hand-coding every id.
"""

INDUSTRIES = [
    "Energy", "Materials", "Capital Goods", "Commercial & Professional Services",
    "Transportation", "Automobiles & Components", "Consumer Durables & Apparel",
    "Consumer Services", "Retailing", "Food & Staples Retailing",
    "Food, Beverage & Tobacco", "Household & Personal Products",
    "Health Care Equipment & Services", "Pharmaceuticals & Biotech",
    "Banks", "Diversified Financials", "Insurance", "Real Estate",
    "Software & Services", "Technology Hardware", "Semiconductors",
    "Telecommunication Services", "Media & Entertainment", "Utilities", "Other",
]


def sic_to_industry(sic_code) -> str:
    """Map a numeric SIC code to an industry group. Specifics checked first."""
    try:
        s = int(sic_code)
    except (TypeError, ValueError):
        return "Other"

    # --- specific overrides (must precede the broad ranges) --------------- #
    if s == 3674:
        return "Semiconductors"
    if 2833 <= s <= 2836 or s == 8731:
        return "Pharmaceuticals & Biotech"
    if 7370 <= s <= 7379 or s == 7389:
        return "Software & Services"
    if 3840 <= s <= 3851 or s == 8011 or (8000 <= s <= 8099):
        return "Health Care Equipment & Services"
    if s == 2844 or 2840 <= s <= 2843:
        return "Household & Personal Products"
    if 3710 <= s <= 3716 or s == 2510 or s == 3011:
        return "Automobiles & Components"

    # --- broad ranges ----------------------------------------------------- #
    if 100 <= s <= 999:
        return "Food, Beverage & Tobacco"          # agriculture production
    if 1000 <= s <= 1099 or 1400 <= s <= 1499:
        return "Materials"                           # metal / nonmetallic mining
    if 1200 <= s <= 1399 or 2900 <= s <= 2999:
        return "Energy"                              # coal, oil & gas, refining
    if 1500 <= s <= 1799:
        return "Capital Goods"                       # construction
    if 2000 <= s <= 2199:
        return "Food, Beverage & Tobacco"
    if 2200 <= s <= 2399 or 3100 <= s <= 3199 or s == 3940:
        return "Consumer Durables & Apparel"
    if 2400 <= s <= 2499 or 2600 <= s <= 2699 or 2800 <= s <= 2899 \
            or 3200 <= s <= 3399:
        return "Materials"                           # wood, paper, chemicals, metals
    if 2700 <= s <= 2799:
        return "Media & Entertainment"               # publishing
    if 3400 <= s <= 3569 or 3580 <= s <= 3599 or 3720 <= s <= 3799:
        return "Capital Goods"                       # machinery, aerospace, defense
    if s == 3571 or s == 3572 or s == 3575 or s == 3576 or s == 3577 or s == 3578 or s == 3579 \
            or 3600 <= s <= 3673 or 3675 <= s <= 3699 or 3810 <= s <= 3829:
        return "Technology Hardware"
    if 3850 <= s <= 3873 or 3900 <= s <= 3999:
        return "Consumer Durables & Apparel"
    if 4000 <= s <= 4799:
        return "Transportation"
    if 4800 <= s <= 4899:
        # broadcasting (4832/4833) is media; the rest is telecom
        return "Media & Entertainment" if s in (4832, 4833) else "Telecommunication Services"
    if 4900 <= s <= 4999:
        return "Utilities"
    if 5000 <= s <= 5199:
        return "Capital Goods"                       # wholesale durable/industrial
    if s == 5411 or 5400 <= s <= 5499:
        return "Food & Staples Retailing"
    if 5800 <= s <= 5899:
        return "Consumer Services"                   # eating/drinking places
    if 5200 <= s <= 5999:
        return "Retailing"
    if 6000 <= s <= 6199:
        return "Banks"
    if 6200 <= s <= 6299 or 6700 <= s <= 6799:
        return "Diversified Financials"
    if 6300 <= s <= 6499:
        return "Insurance"
    if 6500 <= s <= 6599:
        return "Real Estate"
    if 7000 <= s <= 7299 or 7800 <= s <= 7999:
        return "Consumer Services"
    if 7300 <= s <= 7399 or 8700 <= s <= 8748:
        return "Commercial & Professional Services"
    return "Other"


def cap_bucket(market_cap, bounds: dict | None = None) -> str:
    """Bucket a market cap. `bounds` may override the lower thresholds (USD)."""
    if market_cap is None:
        return "unknown"
    b = bounds or {}
    mega = b.get("mega", 200e9)
    large = b.get("large", 10e9)
    mid = b.get("mid", 2e9)
    small = b.get("small", 3e8)
    mc = float(market_cap)
    if mc >= mega:
        return "mega"
    if mc >= large:
        return "large"
    if mc >= mid:
        return "mid"
    if mc >= small:
        return "small"
    return "micro"


SMALL_CAPS = {"small", "micro"}


# Curated jurisdiction by parent-committee thomas_id (House HS*, Senate SS*).
COMMITTEE_JURISDICTION: dict[str, list[str]] = {
    # Financial / tax
    "HSBA": ["Banks", "Diversified Financials", "Insurance", "Real Estate"],
    "SSBK": ["Banks", "Diversified Financials", "Insurance", "Real Estate"],
    "SSFI": ["Banks", "Diversified Financials", "Insurance",
             "Pharmaceuticals & Biotech", "Health Care Equipment & Services"],
    "HSWM": ["Banks", "Diversified Financials", "Insurance",
             "Pharmaceuticals & Biotech", "Health Care Equipment & Services"],
    # Energy / environment / resources
    "HSIF": ["Energy", "Utilities", "Telecommunication Services",
             "Media & Entertainment", "Pharmaceuticals & Biotech",
             "Health Care Equipment & Services"],
    "SSEG": ["Energy", "Utilities", "Materials"],
    "HSII": ["Energy", "Utilities", "Materials"],
    "SSEV": ["Utilities", "Materials", "Capital Goods"],
    # Commerce / tech / transport
    "SSCM": ["Telecommunication Services", "Media & Entertainment",
             "Transportation", "Technology Hardware", "Automobiles & Components"],
    "HSSY": ["Software & Services", "Technology Hardware", "Semiconductors",
             "Capital Goods"],
    "HSPW": ["Transportation", "Capital Goods"],
    # Defense / security / intelligence
    "HSAS": ["Capital Goods"],
    "SSAS": ["Capital Goods"],
    "HSHM": ["Capital Goods", "Software & Services"],
    "HSIG": ["Software & Services", "Technology Hardware", "Semiconductors", "Capital Goods"],
    "SLIN": ["Software & Services", "Technology Hardware", "Semiconductors", "Capital Goods"],
    # Agriculture
    "HSAG": ["Food, Beverage & Tobacco", "Food & Staples Retailing", "Materials"],
    "SSAF": ["Food, Beverage & Tobacco", "Food & Staples Retailing", "Materials"],
    # Health / veterans
    "SSHR": ["Pharmaceuticals & Biotech", "Health Care Equipment & Services"],
    "HSVR": ["Health Care Equipment & Services", "Pharmaceuticals & Biotech"],
    "SSVA": ["Health Care Equipment & Services", "Pharmaceuticals & Biotech"],
}

# Keyword -> industries, applied to committee AND subcommittee names.
_KEYWORDS: list[tuple[tuple[str, ...], list[str]]] = [
    (("health", "drug", "pharmaceutical", "medicaid", "medicare"),
     ["Pharmaceuticals & Biotech", "Health Care Equipment & Services"]),
    (("energy", "fossil", "renewable", "nuclear", "power"), ["Energy", "Utilities"]),
    (("financial", "banking", "securities", "capital markets", "monetary"),
     ["Banks", "Diversified Financials"]),
    (("insurance",), ["Insurance"]),
    (("housing", "real estate"), ["Real Estate"]),
    (("information technology", "cybersecurity", "semiconductor", "the internet"),
     ["Software & Services", "Technology Hardware", "Semiconductors"]),
    (("telecommunication", "communications", "broadband", "spectrum"),
     ["Telecommunication Services", "Media & Entertainment"]),
    (("defense", "armed", "military", "seapower", "airland", "strategic forces"),
     ["Capital Goods"]),
    (("space", "aerospace", "aviation"), ["Capital Goods", "Transportation"]),
    (("highway", "transit", "railroad", "transportation", "maritime", "coast guard"),
     ["Transportation"]),
    (("agriculture", "nutrition", "commodity", "forestry", "livestock"),
     ["Food, Beverage & Tobacco", "Food & Staples Retailing", "Materials"]),
    (("environment", "water", "wildlife"), ["Materials", "Utilities"]),
    (("mining", "minerals"), ["Materials"]),
    (("automobile", "auto"), ["Automobiles & Components"]),
]


def committee_to_industries(thomas_id: str, name: str = "") -> set[str]:
    """Industries a committee/subcommittee touches: curated parent map ∪ name keywords."""
    out: set[str] = set()
    parent = (thomas_id or "")[:4]
    out.update(COMMITTEE_JURISDICTION.get(parent, []))
    low = (name or "").lower()
    for keys, inds in _KEYWORDS:
        if any(k in low for k in keys):
            out.update(inds)
    return out
