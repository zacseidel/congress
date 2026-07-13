from __future__ import annotations

"""
Single entry point — run the right refresh for today.

13F holdings publish quarterly, due ~45 days after quarter-end (the big waves land mid
Feb / May / Aug / Nov). Between waves the hedge holdings are static while congressional
disclosures change weekly, so the normal run is a light "congress + hedge reprice"
(re-mark the existing 13F book to today's prices, no EDGAR pull). But the FIRST run after
a new 13F wave becomes available must pull the new filings — a full hedge update.

This module figures out which case applies, LOGS the decision, and dispatches to
refresh_all.py with the matching flag:

  normal  -> refresh_all.py --hedge-reprice   (congress + reprice existing hedge + dashboard)
  new wave-> refresh_all.py --hedge-discover  (congress + full 13F fetch/rebuild + dashboard)

Detection: compare the filing wave the calendar says should be available now against the
newest wave we've actually ingested (max quarter in data/hedge/report_index.json, written
by rank_funds). A new wave that we haven't fetched -> full update. Because 13Fs keep
arriving through and just after the ~45-day deadline (deadline-day filers, amendments,
newly-qualifying funds), the full path also stays on for a DISCOVER_GRACE_DAYS landing tail
past the deadline even once the wave is ingested — so late/new filers get picked up in the
same wave rather than waiting a quarter. The full fetch is incremental, so these re-runs
only pull what's new.

Usage:
  python src/update.py                  # auto: reprice normally, full after a new 13F wave
  python src/update.py --push           # ...and commit + push when done
  python src/update.py --force-full     # force the full 13F fetch regardless
  python src/update.py --force-reprice  # force the light path regardless
  python src/update.py --dry-run        # print the decision, run nothing
"""

import argparse
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import DATA_DIR, load_json, setup_logging

log = setup_logging("update")
ROOT = Path(__file__).parent.parent
REPORT_INDEX = DATA_DIR / "hedge" / "report_index.json"
FILING_DEADLINE_DAYS = 45   # 13F-HR is due ~45 days after the reporting quarter ends
DISCOVER_GRACE_DAYS = 10    # keep re-discovering this long past the deadline (landing tail)


def _quarter(d: date) -> str:
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def _wave_deadline(today: date) -> date:
    """The ~45-day 13F deadline for the wave that becomes current in today's quarter."""
    qstart = date(today.year, ((today.month - 1) // 3) * 3 + 1, 1)
    return qstart + timedelta(days=FILING_DEADLINE_DAYS)


def expected_filing_wave(today: date) -> str:
    """The newest 13F filing wave that should be substantially available as of `today`.

    A quarter's filings are due ~45 days into the *following* quarter, i.e. ~day 45 of the
    current calendar quarter. Before that deadline the previous quarter's wave is the freshest
    complete one. Waves are labelled by the quarter in which the filings are made (matching
    rank_funds' snapshot key), so this is directly comparable to report_index.json.
    """
    qstart = date(today.year, ((today.month - 1) // 3) * 3 + 1, 1)
    if today >= qstart + timedelta(days=FILING_DEADLINE_DAYS):
        return _quarter(qstart)                       # this quarter's deadline has passed
    return _quarter(qstart - timedelta(days=1))       # else the prior quarter's wave is latest


def last_fetched_wave() -> str | None:
    """Newest filing wave we've actually ingested (max quarter in the hedge report index)."""
    if not REPORT_INDEX.exists():
        return None
    quarters = [e.get("quarter") for e in load_json(REPORT_INDEX) if e.get("quarter")]
    return max(quarters) if quarters else None         # "YYYYQn" sorts chronologically


def decide(today: date, force_full: bool, force_reprice: bool) -> tuple[bool, str]:
    expected, have = expected_filing_wave(today), last_fetched_wave()
    if force_full:
        return True, "forced (--force-full)"
    if force_reprice:
        return False, "forced (--force-reprice)"
    if have is None:
        return True, "no hedge data yet — cold start, running full 13F fetch"
    if expected > have:
        return True, f"new 13F wave available ({expected}; last ingested {have})"
    # Landing tail: the wave is ingested, but 13Fs keep arriving through and shortly after
    # the deadline (deadline-day filers EDGAR hadn't published, amendments, newly-qualifying
    # funds). Keep running the full discover for DISCOVER_GRACE_DAYS past the deadline so
    # those get pulled + re-ranked; the fetch is incremental, so re-runs only pull what's new.
    deadline = _wave_deadline(today)
    if expected == have and deadline <= today <= deadline + timedelta(days=DISCOVER_GRACE_DAYS):
        return True, (f"{expected} landing window (<={DISCOVER_GRACE_DAYS}d past deadline "
                      f"{deadline.isoformat()}) — re-discovering late/new filers")
    return False, f"no new 13F wave since last run (current {have})"


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the appropriate refresh; full after a new 13F wave.")
    ap.add_argument("--push", action="store_true", help="commit + push docs/ and data/ when done")
    ap.add_argument("--force-full", action="store_true", help="force the full 13F fetch")
    ap.add_argument("--force-reprice", action="store_true", help="force the light reprice path")
    ap.add_argument("--dry-run", action="store_true", help="log the decision, run nothing")
    args = ap.parse_args()

    full, why = decide(date.today(), args.force_full, args.force_reprice)
    log.info("=== %s ===  (%s)", "FULL 13F UPDATE" if full else "WEEKLY REPRICE", why)

    hedge_flag = "--hedge-discover" if full else "--hedge-reprice"
    cmd = [sys.executable, str(ROOT / "src" / "refresh_all.py"), hedge_flag]
    if args.push:
        cmd.append("--push")

    if args.dry_run:
        log.info("[dry-run] would run: %s", " ".join(cmd[1:]))
        return
    raise SystemExit(subprocess.run(cmd, cwd=str(ROOT)).returncode)


if __name__ == "__main__":
    main()
