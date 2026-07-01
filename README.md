# Congressional Trades Tracker

Tracks stock trades disclosed by members of the U.S. House and Senate under the STOCK Act, enriches each
security with market data from [Polygon.io](https://polygon.io), and publishes a static site to GitHub
Pages. It ranks members by the return you would have earned by **copying each trade on its public
disclosure date**, and surfaces the stocks that out-performing members are buying.

## What it answers

- **Whose trades would have made you the most money?** Members ranked by dollar-weighted return of a
  "follow the disclosure" strategy, benchmarked against SPY.
- **What are the winners buying?** Stocks purchased by multiple out-performers, in size.
- **Dig into any stock:** 2-year price chart with buy/sell markers, company description, recent news, and
  key financials.
- **Who trades well in what — and is it suspicious?** A Member×Industry skill map, an interactive
  Member↔Industry↔Committee network, and a "regulated bets" feed: a composite signal that rewards
  beating the market in *small-caps* in industries the member's *committee oversees*, bought by
  *multiple* members. Committee data: the free `unitedstates/congress-legislators` project.

## How returns are computed

- **Entry:** each purchase is priced at the close on/after its public disclosure date.
- **Exit:** a sale closes the member's entire open position in that ticker at the sale's disclosure-date
  close; positions never sold are held to today.
- **Weighting:** each position is weighted by the midpoint of its disclosed dollar range (filings report
  ranges, not share counts), so larger bets count more.
- **Alpha:** return above SPY over the same window.

## Pipeline

| Stage | Script | Notes |
|-------|--------|-------|
| Fetch House PTRs | `src/fetch_house.py` | Clerk annual `FD.zip` index → PTR PDFs (`pypdf`) |
| Fetch Senate PTRs | `src/fetch_senate.py` | eFD agreement + DataTables search → electronic PTR HTML |
| Fetch committees | `src/fetch_committees.py` | unitedstates JSON → match members → committee jurisdiction |
| OCR scanned PTRs | `src/ocr_scanned.py` | reads paper House PTRs (`ocr_ptr.py`: rotate + grid + fuzzy stock match) into the ledger |
| Enrich | `src/enrich.py` | Polygon details / news / financials / 2yr bars (cached, capped) |
| Charts | `src/fetch_charts.py` | matplotlib PNGs with buy/sell markers (no API calls) |
| Performance | `src/compute_performance.py` | position model + grouped-daily pricing + SPY alpha |
| Rank | `src/score_and_rank.py` | leaderboard, out-performers, stock/consensus rollups |
| Graph | `src/build_graph.py` | SIC→industry + cap classification, signal feed, skill map, network (no API) |
| Report | `src/generate_report.py` | renders `docs/` (report + member + stock + map + graph + industries + index) |

Classification lives in `src/taxonomy.py` (SIC→industry, cap buckets, committee→industry jurisdiction).

Output lives in `docs/` and is served by GitHub Pages.

## Setup

1. **Secrets** (repo → Settings → Secrets and variables → Actions):
   - `POLYGON_API_KEY` — a Polygon.io key (free tier works; the pipeline paces to 5 calls/min).
   - `HTTP_USER_AGENT` — a descriptive UA with contact info, e.g. `CongressTradesTracker/1.0 (you@example.com)`.
2. **GitHub Pages:** Settings → Pages → Deploy from branch → `main` / `/docs`.
3. **Schedule:** the workflow runs weekly (Mondays) and on manual dispatch.

### Local run

```bash
pip install -r requirements.txt
cp .env.example .env   # add POLYGON_API_KEY and HTTP_USER_AGENT
python src/backfill.py            # full build (run repeatedly on free tier until enrich reports 0 new)
# or stage by stage:
python src/fetch_house.py --year 2025 --limit 20   # quick test slice
```

On the Polygon free tier a cold start is slow (≈4 calls per ticker at 5 calls/min). Enrichment is
resumable — re-run until `enrich` reports `0 new tickers`. The weekly Action enriches up to 60 new
tickers per run, so the full universe fills in over the first several runs while reports still publish.

### Scanned (paper) filings

Members who file on paper produce image-only PDFs the text pipeline can't read; they're collected in
`data/unparsed_filings.json`. Two ways to recover them:

**Automatic OCR (default).** `src/ocr_scanned.py` (run in the pipeline / weekly Action) reads the House
paper filings: it renders each page, picks the rotation that surfaces the most real stocks, detects the
table grid, OCRs the asset column, and fuzzy-matches each row to a known ticker (dictionary built from the
electronic-filing ledger + `company_info`). Rows that aren't public stocks (cash, crypto, talent-firm
payments, attachment sheets) are dropped; rows that match a ticker **and** carry a valid date are written to
`data/transactions.json` tagged `entered_by="ocr"` (with a `match_score`). Filings with no public stocks are
recorded in `data/reviewed_filings.json` so they aren't re-flagged. It's capped per run
(`ocr.max_filings_per_run`) and resumable. Needs system `tesseract-ocr` + `poppler-utils`.

```bash
python src/ocr_scanned.py                    # OCR the next batch from the queue
python src/ocr_scanned.py --member khanna    # one member
python src/ocr_scanned.py --max-filings 5
```

OCR isn't perfect (an occasional misread digit in a date/amount); the `entered_by="ocr"` tag makes those
rows easy to spot-check or bulk-revert. Senate paper filings aren't OCR'd yet (different form + access path).

**Manual entry (fallback).** To transcribe a filing by hand instead:

```bash
python src/manual_entry.py                 # work through the whole queue
python src/manual_entry.py --member khanna # filter to one member
python src/manual_entry.py --no-open       # don't auto-open PDFs in the browser
```

Each filing's source PDF opens in your browser automatically. For every transaction it prompts for ticker /
buy-sell / owner / date / amount-range and writes rows to `data/transactions.json`. Per-filing commands:
`u` undo last, `p` mark *not applicable* (e.g. a non-stock payment — recorded in `data/reviewed_filings.json`
so it doesn't return to the queue), `q` stop without saving. When you finish it offers to run the full
pipeline (`backfill.py`) so the new trades are priced and ranked and the leaderboard / out-performer list
refresh; pass `--run` to do that automatically, or run `python src/backfill.py` yourself later.

## Disclaimer

For informational and educational purposes only — **not investment advice**. Trades come from public
disclosures that report dollar ranges (not exact share counts) and may contain errors. Some members file
on paper, producing scanned image PDFs that can't be machine-read — those are **not** in the leaderboard
and are instead listed in the report's "Filings to review with Claude" section (tracked in
`data/unparsed_filings.json`) so the gap is transparent and each report can be transcribed by hand or with
Claude. Returns are a stylized backtest from public filing dates and do not reflect members' actual prices,
timing, or taxes. Past performance does not predict future results.
