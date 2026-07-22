from __future__ import annotations

"""
Local orchestrator — refresh BOTH reports and the combined dashboard in one command.

The 13F refresh is large enough to run on your own machine rather than GitHub Actions,
so this chains the whole thing end to end:

  1. Hedge 13F pipeline  (src/hedge/backfill_hedge.py) -> docs/hedge/ + stock_holders.json.gz
  2. Congress pipeline   (src/backfill.py)             -> docs/congress.html + unified docs/stocks/
  3. Combined dashboard  (src/generate_dashboard.py)   -> docs/index.html (site root)

Hedge runs first: it writes the per-ticker hedge data that the congress stock-page render
folds into the unified docs/stocks/ pages. Each stage is a standalone script; a failure
stops the run so you can fix and resume.

Suggested cadence (13F holdings only change quarterly; prices change daily):
  WEEKLY    python src/refresh_all.py --hedge-reprice   # congress fetch + hedge repriced to
                                                        # today's prices (no slow EDGAR pull)
  QUARTERLY python src/refresh_all.py --hedge-discover  # full: pull new 13Fs + rebuild pool

Usage:
  python src/refresh_all.py                       # congress + hedge (full pool) + dashboard
  python src/refresh_all.py --hedge-reprice       # hedge: reprice existing holdings only (fast)
  python src/refresh_all.py --hedge-top-n 500     # cap the hedge pool for a faster run
  python src/refresh_all.py --hedge-discover      # rebuild the hedge candidate pool first
  python src/refresh_all.py --skip-hedge          # congress + dashboard, hedge untouched
  python src/refresh_all.py --dashboard-only      # just re-render the top-level page
  python src/refresh_all.py --push                # commit + push docs/ and data/ when done
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import fmt_duration, setup_logging

log = setup_logging("refresh_all")
ROOT = Path(__file__).parent.parent


def _run(label: str, script: str, args=None) -> None:
    cmd = [sys.executable, str(ROOT / script)] + (args or [])
    log.info("=== %s ===  (%s)", label, " ".join(cmd[1:]))
    t0 = time.monotonic()
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        log.error("%s FAILED (exit %d) — stopping.", label, result.returncode)
        sys.exit(result.returncode)
    log.info("--- %s done in %s ---", label, fmt_duration(time.monotonic() - t0))


def _push() -> None:
    log.info("=== Commit + push ===")
    # Ensure the "ours" merge driver exists (a no-op that keeps our file) so the merge=ours
    # rules in .gitattributes auto-resolve data/ and docs/ conflicts to local. Idempotent.
    subprocess.run(["git", "config", "merge.ours.driver", "true"], cwd=str(ROOT))
    subprocess.run(["git", "add", "data/", "docs/"], cwd=str(ROOT))
    staged = subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=str(ROOT))
    if staged.returncode == 0:
        log.info("Nothing to commit.")
        return
    from datetime import date
    # Check each step: a failed pull/push used to pass silently, leaving the run "successful"
    # with nothing published. Stop at the first failure so the log shows what needs fixing.
    for label, cmd in [
        ("commit", ["git", "commit", "-m", f"Refresh reports {date.today().isoformat()}"]),
        # Merge (NOT rebase): rebase swaps ours/theirs, which would invert merge=ours and keep
        # the remote copies. A plain merge keeps local for every generated/data file.
        ("pull", ["git", "pull", "--no-rebase", "origin", "main"]),
        ("push", ["git", "push"]),
    ]:
        if subprocess.run(cmd, cwd=str(ROOT)).returncode != 0:
            log.error("git %s failed — results are built but NOT published.", label)
            sys.exit(1)
    log.info("Pushed.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-congress", action="store_true")
    ap.add_argument("--skip-hedge", action="store_true")
    ap.add_argument("--dashboard-only", action="store_true", help="only re-render docs/index.html")
    ap.add_argument("--hedge-top-n", type=int, help="cap the hedge candidate pool")
    ap.add_argument("--hedge-discover", action="store_true", help="rebuild the hedge pool first (quarterly)")
    ap.add_argument("--hedge-reprice", action="store_true",
                    help="weekly: re-mark existing hedge holdings to current prices, no EDGAR fetch")
    ap.add_argument("--no-dashboard", action="store_true")
    ap.add_argument("--push", action="store_true", help="commit + push when done")
    args = ap.parse_args()

    t0 = time.monotonic()
    if args.dashboard_only:
        _run("Combined dashboard", "src/generate_dashboard.py")
        log.info("Total: %s", fmt_duration(time.monotonic() - t0))
        return

    if not args.skip_hedge:
        hedge_args, label = [], "Hedge 13F pipeline"
        if args.hedge_reprice:
            hedge_args.append("--reprice")   # weekly: reprice only, no EDGAR fetch
            label = "Hedge reprice"
        else:
            if args.hedge_discover:
                hedge_args.append("--discover")
            if args.hedge_top_n:
                hedge_args += ["--top-n", str(args.hedge_top_n)]
        _run(label, "src/hedge/backfill_hedge.py", hedge_args)
    if not args.skip_congress:
        _run("Congress pipeline", "src/backfill.py")
    if not args.no_dashboard:
        _run("Combined dashboard", "src/generate_dashboard.py")
    if args.push:
        _push()
    log.info("All done in %s.", fmt_duration(time.monotonic() - t0))


if __name__ == "__main__":
    main()
