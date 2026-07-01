from __future__ import annotations

"""
Transcribe scanned/paper PTRs into the structured ledger by hand.

Some members file on paper, producing image-only PDFs that the automated pipeline
can't read (see `data/unparsed_filings.json` and the report's "Filings to review
with Claude" section). This CLI walks that queue: it shows each flagged filing with
a link to its source PDF, prompts for the equity transactions, writes them into
`data/transactions.json` in the same shape the fetchers produce, and drops the
filing from the queue.

Each filing's source PDF opens in your browser automatically. Mark filings with no
publicly-traded stock (e.g. an exec-search-firm payment) as "not applicable" with
`p` — they're recorded in `data/reviewed_filings.json` so they don't come back.

When you finish, it offers to run the full pipeline (`backfill.py`) so the new
tickers get priced and the leaderboard / out-performer list refresh; otherwise
nothing recomputes until you run it yourself.

Usage:
  python src/manual_entry.py                 # work through the whole queue
  python src/manual_entry.py --member khanna # only filings whose member matches
  python src/manual_entry.py --no-open       # don't auto-open PDFs in the browser
  python src/manual_entry.py --run           # run backfill.py afterwards, no prompt
"""

import argparse
import subprocess
import sys
import webbrowser
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import (LEDGER_PATH, REVIEWED_PATH, UNPARSED_PATH, load_json, parse_date,
                   save_json, setup_logging)

log = setup_logging("manual_entry")

# Standard House/Senate PTR disclosed amount ranges, in form order.
AMOUNT_BUCKETS = [
    (1_001, 15_000),
    (15_001, 50_000),
    (50_001, 100_000),
    (100_001, 250_000),
    (250_001, 500_000),
    (500_001, 1_000_000),
    (1_000_001, 5_000_000),
    (5_000_001, 25_000_000),
    (25_000_001, 50_000_000),
    (50_000_001, 50_000_001),   # "over $50,000,000"
]
TYPE_ALIASES = {"p": "P", "buy": "P", "purchase": "P",
                "s": "S", "sell": "S", "sale": "S",
                "e": "E", "exchange": "E"}
OWNERS = {"SP", "JT", "DC"}


def _fmt_amount(lo: int, hi: int) -> str:
    return f"over ${lo - 1:,}" if lo == hi else f"${lo:,} – ${hi:,}"


def _ask(prompt: str, default: str = "") -> str:
    """input() with a shown default; treats EOF/Ctrl-C as 'stop' via sentinel."""
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return "\x00"
    return val or default


def _ask_type() -> str | None:
    while True:
        v = _ask("  Type (P=buy / S=sell / E=exchange)", "P")
        if v == "\x00":
            return None
        t = TYPE_ALIASES.get(v.lower())
        if t:
            return t
        print("    Enter P, S, or E (or buy/sell/exchange).")


def _ask_owner() -> str | None:
    while True:
        v = _ask("  Owner (SP=spouse / JT=joint / DC=dependent, blank=self)", "")
        if v == "\x00":
            return None
        if not v or v.upper() in OWNERS:
            return v.upper()
        print("    Enter SP, JT, DC, or leave blank.")


def _ask_date(default_iso: str) -> str | None:
    while True:
        v = _ask("  Transaction date (MM/DD/YYYY)", default_iso)
        if v == "\x00":
            return None
        d = parse_date(v)
        if d:
            return d.isoformat()
        print("    Couldn't read that date — try MM/DD/YYYY.")


def _ask_amount() -> tuple[int, int] | None:
    print("  Amount range:")
    for i, (lo, hi) in enumerate(AMOUNT_BUCKETS, 1):
        print(f"    {i:>2}. {_fmt_amount(lo, hi)}")
    while True:
        v = _ask("  Pick 1-%d" % len(AMOUNT_BUCKETS), "1")
        if v == "\x00":
            return None
        if v.isdigit() and 1 <= int(v) <= len(AMOUNT_BUCKETS):
            return AMOUNT_BUCKETS[int(v) - 1]
        print("    Enter a number from the list.")


def _open_pdf(url: str, enabled: bool) -> None:
    if not enabled:
        return
    try:
        webbrowser.open(url)
    except Exception:
        pass  # headless / no browser — the URL is printed anyway


def _enter_filing(rec: dict, open_browser: bool = True) -> list[dict] | str | None:
    """Prompt for the equity transactions on one filing. Returns the list of
    transactions, the string "PASS" to dismiss it as not applicable, or None if the
    user aborted (Ctrl-C/EOF / 'q')."""
    print("\n" + "=" * 72)
    print(f"  {rec['member']}  ({rec['chamber']}{', ' + rec['state'] if rec.get('state') else ''})")
    print(f"  Disclosed: {rec['disclosure_date']}   Doc: {rec['doc_id']}")
    print(f"  PDF: {rec['source_url']}")
    print("=" * 72)
    _open_pdf(rec["source_url"], open_browser)
    print("  Enter each stock transaction. Commands: blank = finish · 'u' = undo last"
          " · 'p' = pass (not applicable, remove from queue) · 'q' = stop without saving.")
    txs: list[dict] = []
    while True:
        tk = _ask("\n  Ticker (blank = finish)")
        if tk == "\x00" or tk.lower() == "q":
            return None
        if tk.lower() in ("p", "pass", "na", "n/a"):
            return "PASS"
        if not tk:
            return txs
        if tk.lower() == "u":
            if txs:
                gone = txs.pop()
                print(f"    removed {gone['ticker']}")
            else:
                print("    nothing to undo")
            continue
        ticker = tk.upper().replace(".", "")
        if not ticker.isalpha() or len(ticker) > 5:
            print("    That doesn't look like a stock ticker (1-5 letters). Skipped.")
            continue
        tx_type = _ask_type()
        if tx_type is None:
            return None
        owner = _ask_owner()
        if owner is None:
            return None
        tx_date = _ask_date(rec["disclosure_date"])
        if tx_date is None:
            return None
        amount = _ask_amount()
        if amount is None:
            return None
        asset = _ask("  Asset name (optional)", "")
        if asset == "\x00":
            return None
        txs.append({"ticker": ticker, "tx_type": tx_type, "owner": owner,
                    "tx_date": tx_date, "amount": amount, "asset_name": asset})
        label = {"P": "BUY", "S": "SELL", "E": "EXCH"}[tx_type]
        print(f"    + {label} {ticker} {_fmt_amount(*amount)} on {tx_date}"
              f"  ({len(txs)} so far)")


def _commit(rec: dict, key: str, txs: list[dict], ledger: dict, unparsed: dict) -> None:
    today = date.today().isoformat()
    chamber, doc_id = rec["chamber"], rec["doc_id"]
    for i, t in enumerate(txs):
        tx_id = f"{chamber}:{doc_id}:m{i}"
        ledger[tx_id] = {
            "tx_id": tx_id, "first_seen": today, "chamber": chamber,
            "member": rec["member"], "member_id": rec["member_id"], "party": "",
            "state": rec.get("state", ""), "district": rec.get("district", ""),
            "ticker": t["ticker"], "asset_name": t["asset_name"], "owner": t["owner"],
            "tx_type": t["tx_type"], "tx_date": t["tx_date"],
            "disclosure_date": rec["disclosure_date"],
            "amount_min": t["amount"][0], "amount_max": t["amount"][1],
            "amount_mid": (t["amount"][0] + t["amount"][1]) / 2,
            "doc_id": doc_id, "source_url": rec["source_url"], "entered_by": "manual",
        }
    unparsed.pop(key, None)
    save_json(LEDGER_PATH, ledger)
    save_json(UNPARSED_PATH, unparsed)


def _run_backfill() -> None:
    print("\nRunning the pipeline (fetch → price → rank → report)…\n" + "-" * 72)
    rc = subprocess.run([sys.executable, str(Path(__file__).parent / "backfill.py")]).returncode
    print("-" * 72)
    print(f"backfill finished (exit code {rc}). The leaderboard and out-performer list are refreshed."
          if rc == 0 else f"backfill exited with code {rc} — check the output above.")


def run(member_filter: str | None = None, open_browser: bool = True,
        auto_run: bool = False) -> None:
    if not UNPARSED_PATH.exists():
        print("No data/unparsed_filings.json yet — run fetch_house.py / fetch_senate.py first.")
        return
    unparsed = load_json(UNPARSED_PATH)
    ledger = load_json(LEDGER_PATH) if LEDGER_PATH.exists() else {}
    reviewed = load_json(REVIEWED_PATH) if REVIEWED_PATH.exists() else []

    def dismiss(key: str, rec: dict) -> None:
        """Drop a filing from the queue and remember it as reviewed/not-applicable."""
        if rec["doc_id"] not in reviewed:
            reviewed.append(rec["doc_id"])
        unparsed.pop(key, None)
        save_json(UNPARSED_PATH, unparsed)
        save_json(REVIEWED_PATH, sorted(reviewed))

    def pending_keys() -> list[str]:
        keys = [k for k, v in unparsed.items()
                if not member_filter or member_filter.lower() in v["member"].lower()]
        return sorted(keys, key=lambda k: (unparsed[k]["member"], unparsed[k]["disclosure_date"]))

    n_filings = n_txs = 0
    while True:
        keys = pending_keys()
        if not keys:
            print("\nQueue empty — nothing left to transcribe"
                  + (f" for '{member_filter}'." if member_filter else "."))
            break
        print(f"\nPending filings ({len(keys)}):")
        for i, k in enumerate(keys, 1):
            v = unparsed[k]
            print(f"  {i:>3}. {v['member']:<28} {v['chamber']:<6} {v['disclosure_date']}")
        sel = _ask("\nSelect a number (Enter = first, 'q' = quit)", "1")
        if sel == "\x00" or sel.lower() == "q":
            break
        if not (sel.isdigit() and 1 <= int(sel) <= len(keys)):
            print("  Not a valid selection.")
            continue
        key = keys[int(sel) - 1]
        rec = unparsed[key]
        txs = _enter_filing(rec, open_browser)
        if txs is None:
            print("  (aborted — nothing saved; filing stays in the queue)")
            continue
        if txs == "PASS":
            dismiss(key, rec)
            print("  marked not applicable — removed from the queue.")
            continue
        if not txs:
            if _ask("  No transactions entered. Mark this filing reviewed (remove from"
                    " queue)? (y/N)", "N").lower() == "y":
                dismiss(key, rec)
                print("  marked reviewed — removed from the queue.")
            continue
        print(f"\n  Ready to save {len(txs)} transaction(s) for {rec['member']}.")
        if _ask("  Save? (Y/n)", "Y").lower() in ("y", ""):
            _commit(rec, key, txs, ledger, unparsed)
            n_filings += 1
            n_txs += len(txs)
            print(f"  saved. ({len(txs)} transactions)")
        else:
            print("  discarded.")

    if n_txs:
        print(f"\nDone: {n_txs} transactions across {n_filings} filing(s) added to the ledger.")
        if auto_run or _ask("Run the pipeline now to price and rank them? (y/N)", "N").lower() == "y":
            _run_backfill()
        else:
            print("Skipped. Run `python src/backfill.py` when you're ready to refresh the leaderboard.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Manually transcribe scanned PTR filings.")
    ap.add_argument("--member", help="only filings whose member name contains this text")
    ap.add_argument("--no-open", action="store_true",
                    help="don't auto-open each filing's PDF in the browser")
    ap.add_argument("--run", action="store_true",
                    help="run the full pipeline (backfill.py) afterwards without asking")
    args = ap.parse_args()
    run(args.member, open_browser=not args.no_open, auto_run=args.run)


if __name__ == "__main__":
    main()
