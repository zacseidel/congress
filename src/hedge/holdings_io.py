from __future__ import annotations

"""
Compact, lossless codec for the holdings ledger.

The ledger is 2.5M rows of {cik, manager, filing_date, report_date, form, accession,
cusip, issuer, value, shares, put_call}. Stored as a flat dict of denormalized rows it
gzips to ~60 MB — because the manager name, issuer, dates, form, accession and every
field name are repeated on every one of the 2.5M rows.

This codec normalizes on write into side tables (funds, filings, securities) and stores
the 2.5M rows COLUMNAR (parallel arrays: filing_idx, sec_idx, value, shares, put_call),
with whole-dollar values/shares int-encoded. Columnar keeps homogeneous data together so
gzip compresses it far better than mixed row records — ~18 MB vs ~60 MB denormalized.

`load_holdings` expands back to the exact same {key: row} dict, so nothing downstream
changes — callers still get denormalized rows keyed by "cik:filing_date:cusip[:put_call]".
"""

import gzip
import json
from pathlib import Path


def _key(cik, filing_date, cusip, put_call) -> str:
    k = f"{cik}:{filing_date}:{cusip}"
    return f"{k}:{put_call}" if put_call else k


def _num(x):
    """Int-encode whole numbers (13F values are whole dollars, shares whole counts)."""
    try:
        return int(x) if float(x) == int(float(x)) else x
    except (TypeError, ValueError):
        return x


def save_holdings(path: Path, holdings: dict) -> None:
    managers: dict = {}                    # cik -> manager name
    fil_index: dict = {}
    filings: list = []                     # [cik, fdate, rdate, form, acc]
    sec_index: dict = {}
    securities: list = []                  # [cusip, issuer]
    fi_col: list = []; si_col: list = []; val: list = []; sh: list = []; pc: list = []

    for r in holdings.values():
        cik = r["cik"]
        managers.setdefault(str(cik), r["manager"])
        fk = (cik, r["filing_date"], r["report_date"], r["form"], r["accession"])
        fi = fil_index.get(fk)
        if fi is None:
            fi = fil_index[fk] = len(filings)
            filings.append([cik, r["filing_date"], r["report_date"], r["form"], r["accession"]])
        cusip = r["cusip"]
        si = sec_index.get(cusip)
        if si is None:
            si = sec_index[cusip] = len(securities)
            securities.append([cusip, r["issuer"]])
        fi_col.append(fi); si_col.append(si)
        val.append(_num(r["value"])); sh.append(_num(r["shares"]))
        pc.append(r["put_call"] or 0)

    obj = {"v": 2, "managers": managers, "filings": filings, "securities": securities,
           "filing_idx": fi_col, "sec_idx": si_col, "value": val, "shares": sh, "put_call": pc}
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"), default=str)


def load_holdings(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        obj = json.load(f)
    managers, filings, securities = obj["managers"], obj["filings"], obj["securities"]
    out: dict = {}
    if "holdings" in obj:      # legacy v1 row-arrays (kept for one-time migration)
        cols = ((r[0], r[1], r[2], r[3], r[4]) for r in obj["holdings"])
    else:                      # v2 columnar
        cols = zip(obj["filing_idx"], obj["sec_idx"], obj["value"], obj["shares"], obj["put_call"])
    for fi, si, value, shares, pcv in cols:
        cik, fdate, rdate, form, acc = filings[fi]
        cusip, issuer = securities[si]
        put_call = None if pcv == 0 else pcv
        out[_key(cik, fdate, cusip, put_call)] = {
            "cik": cik, "manager": managers.get(str(cik)),
            "filing_date": fdate, "report_date": rdate, "form": form, "accession": acc,
            "cusip": cusip, "issuer": issuer, "value": value, "shares": shares,
            "put_call": put_call,
        }
    return out
