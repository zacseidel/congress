from __future__ import annotations

"""
OCR engine for scanned (paper) House PTR filings.

Paper PTRs are image-only PDFs the text pipeline can't read. This module reads them:

  1. Render each page (pdf2image / poppler).
  2. Pick the page rotation that yields the most *public-stock* matches — Tesseract
     OSD is unreliable on these sparse forms, so we let the stock dictionary drive it.
  3. Detect the table grid from its ruled lines; the widest column is the asset name.
  4. OCR the asset column and fuzzy-match each row to a known ticker (built from the
     electronic-filing ledger + company_info). Rows that don't match a public stock
     (cash, crypto, talent-firm payments, attachment sheets) are simply dropped.
  5. For each matched row, read the transaction date (OCR) and the buy/sell type and
     dollar-range bucket (which checkbox is inked).

Only rows that match a real ticker AND carry a valid transaction date are returned,
which keeps precision high. Returned dollar amounts are bracket midpoints, like the
rest of the pipeline.
"""

import difflib
import re

import cv2
import numpy as np
import pytesseract

from utils import DATA_DIR, LEDGER_PATH, load_json, load_json_gz

# Standard House/Senate PTR disclosed amount ranges, in form column order.
AMOUNT_BUCKETS = [
    (1_001, 15_000), (15_001, 50_000), (50_001, 100_000), (100_001, 250_000),
    (250_001, 500_000), (500_001, 1_000_000), (1_000_001, 5_000_000),
    (5_000_001, 25_000_000), (25_000_001, 50_000_000), (50_000_001, 50_000_001),
]
TYPE_ORDER = ["P", "S", "S", "E"]          # Purchase, Sale, Sale(partial), Exchange
DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{2,4})")
MATCH_CUTOFF = 0.86                        # fuzzy name-match threshold
ROTATION_ACCEPT = 2                        # matches at/above this at the learned angle => accept it, skip the rotation scan
_ROTS = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}

# Company-name suffixes/share-class noise to strip before matching.
_SUFFIX = re.compile(
    r"\b(COMMON STOCK|CLASS [A-C]|CL [A-C]|CMN|COM|INC|CORP(ORATION)?|CO|COMPANY|LTD|"
    r"PLC|LP|HOLDINGS?|GROUP|THE|ADR|AMERICAN DEPOSITARY (SHARES|RECEIPTS?)|N V|S A|"
    r"ORDINARY SHARES?|TR|TRUST)\b")


# --------------------------------------------------------------------------- #
# Public-stock dictionary
# --------------------------------------------------------------------------- #
def norm(name: str) -> str:
    s = (name or "").upper()
    s = re.sub(r"[^A-Z0-9 &]", " ", s)
    s = _SUFFIX.sub(" ", s)
    s = s.replace(" AND ", " ").replace("&", " ")
    return re.sub(r"\s+", " ", s).strip()


def build_stock_map() -> dict[str, str]:
    """{normalized company name -> ticker}, rebuilt each run from TRUSTED sources only, in
    priority order (first source to claim a name wins):
      1. official electronic-filing asset names (exact ticker, congress-authoritative),
      2. Polygon company_info,
      3. the hedge 13F universe — SEC issuer names -> resolved tickers, as a gap-filler that
         broadens recall to names congress hasn't traded yet but that funds hold.
    OCR/manually-entered rows are excluded so an OCR misread can't feed a bad name->ticker
    pair back into the matcher. Grows automatically as new filings land and tickers resolve."""
    m: dict[str, str] = {}
    if LEDGER_PATH.exists():
        for v in load_json(LEDGER_PATH).values():
            if v.get("entered_by"):          # skip OCR/manual-sourced rows (not authoritative)
                continue
            if v.get("asset_name") and v.get("ticker"):
                k = norm(v["asset_name"])
                if len(k) >= 3:
                    m.setdefault(k, v["ticker"].upper())
    ci_path = DATA_DIR / "company_info.json"
    if ci_path.exists():
        for t, v in load_json(ci_path).items():
            if isinstance(v, dict) and v.get("name"):
                k = norm(v["name"])
                if len(k) >= 3:
                    m.setdefault(k, t.upper())
    # Hedge 13F issuer names -> resolved tickers (setdefault => only fills gaps the two
    # congress sources missed). Skip any name that maps to >1 ticker across the universe:
    # those are ETF fund-family labels ("ISHARES", "PROSHARES") or share-class collisions we
    # can't safely disambiguate, and are useless for OCR anyway (a PTR names the specific fund).
    sh_path = DATA_DIR / "hedge" / "stock_holders.json.gz"
    if sh_path.exists():
        by_name: dict[str, set] = {}
        for t, v in load_json_gz(sh_path).items():
            nm = (v or {}).get("issuer")
            if nm:
                k = norm(nm)
                if len(k) >= 3:
                    by_name.setdefault(k, set()).add(t.upper())
        for k, tickers in by_name.items():
            if len(tickers) == 1:
                m.setdefault(k, next(iter(tickers)))
    return m


def match_stock(asset: str, smap: dict, keys: list) -> tuple[str | None, float]:
    k = norm(asset)
    if len(k) < 3:
        return None, 0.0
    if k in smap:
        return smap[k], 1.0
    hit = difflib.get_close_matches(k, keys, n=1, cutoff=MATCH_CUTOFF)
    if hit:
        return smap[hit[0]], difflib.SequenceMatcher(None, k, hit[0]).ratio()
    return None, 0.0


def clean_date(m: re.Match, disclosure_iso: str | None = None) -> str | None:
    """Validate/expand an OCR'd MM/DD/YY[YY] date; return ISO or None if implausible.
    A transaction can't post after its disclosure date, and PTRs disclose within ~45
    days, so reject dates after disclosure or more than ~15 months before it."""
    from datetime import date as _date
    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if len(m.group(3)) == 2:
        y += 2000
    if not (1 <= mo <= 12 and 1 <= d <= 31 and 2000 <= y <= 2099):
        return None
    iso = f"{y:04d}-{mo:02d}-{d:02d}"
    if disclosure_iso:
        try:
            td, dd = _date.fromisoformat(iso), _date.fromisoformat(disclosure_iso)
            if td > dd or (dd - td).days > 460:
                return None
        except ValueError:
            pass
    return iso


# --------------------------------------------------------------------------- #
# Imaging
# --------------------------------------------------------------------------- #
def render_pages(pdf_bytes: bytes, dpi: int = 300) -> list[np.ndarray]:
    from pdf2image import convert_from_bytes
    return [np.array(im.convert("L")) for im in convert_from_bytes(pdf_bytes, dpi=dpi)]


def _rotate(img: np.ndarray, ang: int) -> np.ndarray:
    return img if ang == 0 else cv2.rotate(img, _ROTS[ang])


def _cluster(xs: list[int], gap: int = 15) -> list[int]:
    out: list[list[int]] = []
    for x in sorted(xs):
        if out and x - out[-1][-1] <= gap:
            out[-1].append(x)
        else:
            out.append([x])
    return [int(np.mean(c)) for c in out]


def _grid(img: np.ndarray) -> tuple[list[int], list[int]]:
    """Column/row boundaries from the form's ruled lines."""
    h, w = img.shape
    bw = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(h // 30, 1)))
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(w // 30, 1), 1))
    vsum = cv2.morphologyEx(bw, cv2.MORPH_OPEN, vk).sum(axis=0)
    hsum = cv2.morphologyEx(bw, cv2.MORPH_OPEN, hk).sum(axis=1)
    colx = _cluster([x for x in range(1, w) if vsum[x] > h * 0.3 * 255])
    rowy = _cluster([y for y in range(1, h) if hsum[y] > w * 0.3 * 255])
    return colx, rowy


def _asset_col(colx: list[int]) -> int:
    return max((colx[i + 1] - colx[i], i) for i in range(len(colx) - 1))[1]


def _column_rows(clean: np.ndarray, colx: list[int], rowy: list[int], ci: int) -> dict[int, str]:
    """OCR one grid column as a single strip; bucket words into grid rows by y-position.
    One tesseract pass yields every row's text for the column — far cheaper than OCRing each
    cell separately (which spawns a tesseract subprocess per cell)."""
    x0, x1 = colx[ci] + 4, colx[ci + 1] - 4
    if x1 <= x0:
        return {}
    data = pytesseract.image_to_data(clean[:, x0:x1], config="--psm 6",
                                     output_type=pytesseract.Output.DICT)
    rows: dict[int, list[str]] = {}
    for i, t in enumerate(data["text"]):
        t = t.strip()
        if not t or int(data["conf"][i]) < 25:
            continue
        cy = data["top"][i] + data["height"][i] // 2
        for ri in range(len(rowy) - 1):
            if rowy[ri] <= cy < rowy[ri + 1]:
                rows.setdefault(ri, []).append(t)
                break
    return {ri: " ".join(ws) for ri, ws in rows.items()}


def _asset_names(clean: np.ndarray, colx: list[int], rowy: list[int], aci: int) -> dict[int, str]:
    return _column_rows(clean, colx, rowy, aci)


def _match_count(img: np.ndarray, smap: dict, keys: list) -> int:
    """How many asset rows match a public stock at this rotation (rotation scorer)."""
    colx, rowy = _grid(img)
    if len(colx) < 6 or len(rowy) < 4:
        return 0
    clean = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    n = 0
    for asset in _asset_names(clean, colx, rowy, _asset_col(colx)).values():
        if match_stock(asset, smap, keys)[0]:
            n += 1
    return n


def _ocr_cell(clean: np.ndarray, y0: int, y1: int, x0: int, x1: int) -> str:
    sub = clean[y0 + 4:y1 - 4, x0 + 4:x1 - 4]
    return pytesseract.image_to_string(sub, config="--psm 7").strip() if sub.size else ""


def _extract_page(img: np.ndarray, smap: dict, keys: list, disclosure_date: str) -> list[dict]:
    colx, rowy = _grid(img)
    if len(colx) < 6 or len(rowy) < 4:
        return []
    h, w = img.shape
    bwinv = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(h // 30, 1)))
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(w // 30, 1), 1))
    lines = cv2.add(cv2.morphologyEx(bwinv, cv2.MORPH_OPEN, vk),
                    cv2.morphologyEx(bwinv, cv2.MORPH_OPEN, hk))
    ink = cv2.subtract(bwinv, lines) > 0
    clean = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    aci = _asset_col(colx)
    names = _asset_names(clean, colx, rowy, aci)
    # Which columns hold dates is a page-level property of the table. Find the candidates once
    # with a cheap per-column strip OCR, then confirm each row's date with a PRECISE per-cell
    # OCR (exact row crop) — this keeps the old per-row accuracy but only OCRs the ~1-2 date
    # columns per row instead of scanning all ~8, which was the bulk of the cost.
    right_cols = list(range(aci + 1, len(colx) - 1))
    col_strip = {ci: _column_rows(clean, colx, rowy, ci) for ci in right_cols}
    date_candidates = [ci for ci in right_cols
                       if any(DATE_RE.search(t) for t in col_strip[ci].values())]

    def col_ink(ri: int, ci: int) -> float:
        s = ink[rowy[ri] + 6:rowy[ri + 1] - 6, colx[ci] + 6:colx[ci + 1] - 6]
        return float(s.mean()) if s.size else 0.0

    out = []
    for ri, asset in names.items():
        ticker, score = match_stock(asset, smap, keys)
        if not ticker:
            continue
        y0, y1 = rowy[ri], rowy[ri + 1]
        # Precise per-cell OCR, but only on the columns that are dates page-wide.
        cell = {ci: _ocr_cell(clean, y0, y1, colx[ci], colx[ci + 1]) for ci in date_candidates}
        date_cols = [ci for ci in date_candidates if DATE_RE.search(cell[ci])]
        if not date_cols:
            continue                       # no date => not a transaction row; skip for precision
        first_dc, last_dc = date_cols[0], date_cols[-1]
        dm = DATE_RE.search(cell[first_dc])
        tx_date = clean_date(dm, disclosure_date) if dm else None

        # Transaction type: inked checkbox among columns between asset and first date.
        tg = list(range(aci + 1, first_dc))
        tx_type = None
        if tg:
            best, gi = max((col_ink(ri, c), gi) for gi, c in enumerate(tg))
            if best > 0.008:
                tx_type = TYPE_ORDER[min(gi, len(TYPE_ORDER) - 1)]
        if tx_type is None:
            continue            # can't tell buy vs sell — drop rather than guess (returns depend on it)
        # Amount bucket: inked checkbox among columns after the last date.
        ag = list(range(last_dc + 1, len(colx) - 1))
        amount = None
        if ag:
            best, gi = max((col_ink(ri, c), gi) for gi, c in enumerate(ag))
            if best > 0.008 and gi < len(AMOUNT_BUCKETS):
                amount = AMOUNT_BUCKETS[gi]

        out.append({
            "ticker": ticker, "match_score": round(score, 3),
            "tx_type": tx_type, "tx_date": tx_date or disclosure_date,
            "amount_min": amount[0] if amount else AMOUNT_BUCKETS[0][0],
            "amount_max": amount[1] if amount else AMOUNT_BUCKETS[0][1],
            "asset_name": asset[:80],
        })
    return out


def extract_filing(pdf_bytes: bytes, smap: dict, keys: list, disclosure_date: str,
                   dpi: int = 300) -> list[dict]:
    """Extract equity transaction rows from a scanned House PTR. Each row carries
    ticker, tx_type, tx_date, amount_min/max, match_score and asset_name."""
    # A PTR is a single scan, so pages share orientation (and mixed scans reuse a small set of
    # angles). Keep the angles seen so far in most-recently-used order and try them first — a
    # mis-rotated page scores 0 (broken grid), so the first learned angle that surfaces a
    # handful of stocks wins in one OCR pass. Only when no known orientation resolves a page do
    # we full-scan the remaining angles; the winner is promoted to the front for the next page.
    # Consecutive same-orientation pages cost 1 pass; each new orientation is discovered once.
    records = []
    learned = [0]
    for raw in render_pages(pdf_bytes, dpi=dpi):
        best_ang, best_n = 0, -1
        for ang in learned:                        # try known orientations, MRU first
            n = _match_count(_rotate(raw, ang), smap, keys)
            if n > best_n:
                best_ang, best_n = ang, n
            if n >= ROTATION_ACCEPT:
                break
        if best_n < ROTATION_ACCEPT:               # none resolved it — scan the untried angles
            for ang in (0, 90, 180, 270):
                if ang in learned:
                    continue
                n = _match_count(_rotate(raw, ang), smap, keys)
                if n > best_n:
                    best_ang, best_n = ang, n
        if best_n <= 0:
            continue
        if best_ang in learned:                    # promote winner to front (MRU)
            learned.remove(best_ang)
        learned.insert(0, best_ang)
        records.extend(_extract_page(_rotate(raw, best_ang), smap, keys, disclosure_date))
    return records
