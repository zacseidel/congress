from __future__ import annotations

"""
Shared utilities: config + logging + JSON IO, a polite HTTP session for the
House/Senate disclosure sites, a rate-limited+cached Polygon.io client, and
small date helpers.

Adapted from the sibling `insiders` project's utils.py.
"""

import json
import logging
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import requests
import yaml

ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config.yaml"
DATA_DIR = ROOT / "data"
# Normalized transaction ledger (House + Senate), keyed by tx_id.
LEDGER_PATH = DATA_DIR / "transactions.json"
# Scanned/paper filings we can't machine-read — surfaced in the report for manual/Claude review.
UNPARSED_PATH = DATA_DIR / "unparsed_filings.json"
# Filings reviewed by hand and dismissed as "not applicable" (no public-equity trades),
# so the fetchers don't re-flag them into the queue on the next run.
REVIEWED_PATH = DATA_DIR / "reviewed_filings.json"
CACHE_DIR = DATA_DIR / "cache"
POLYGON_CACHE = CACHE_DIR / "polygon"          # ticker details
FINANCIALS_CACHE = CACHE_DIR / "financials"
NEWS_CACHE = CACHE_DIR / "news"                # recent headlines per ticker
AGGS_CACHE = CACHE_DIR / "aggs"                # 2yr daily bars per ticker
GROUPED_CACHE = CACHE_DIR / "grouped"          # all-ticker closes per date
PTR_PDF_CACHE = CACHE_DIR / "ptr_pdfs"

POLYGON_BASE = "https://api.polygon.io"

# Share-class tickers Polygon writes with a dot (e.g. BRK.B); the disclosure filings
# store them dot-stripped (BRKB), which 404s on Polygon and leaves the position
# unpriced. Map our ledger ticker -> Polygon symbol only at the API boundary, so the
# ledger keeps its canonical (dot-stripped) form everywhere else.
TICKER_ALIASES = {
    "BRKA": "BRK.A", "BRKB": "BRK.B", "BFA": "BF.A", "BFB": "BF.B",
    "HEIA": "HEI.A", "UHALB": "UHAL.B", "LENB": "LEN.B", "LGFA": "LGF.A",
    "LGFB": "LGF.B", "MOGA": "MOG.A", "MOGB": "MOG.B", "CWENA": "CWEN.A",
    "GEFB": "GEF.B", "CRDA": "CRD.A", "CRDB": "CRD.B", "BIOB": "BIO.B",
    "TAPA": "TAP.A", "PARAA": "PARA.A", "STZB": "STZ.B", "HVTA": "HVT.A",
}
TICKER_ALIASES_REV = {v: k for k, v in TICKER_ALIASES.items()}


def polygon_ticker(t: str) -> str:
    """Our ledger ticker -> Polygon symbol (share classes carry a dot)."""
    return TICKER_ALIASES.get((t or "").upper(), t)


def _load_dotenv() -> None:
    """Load KEY=value pairs from .env into os.environ (existing env vars win)."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def setup_logging(name: str) -> logging.Logger:
    # Line-buffer stderr so progress shows live even when piped/backgrounded
    # (block buffering otherwise hides it until the buffer fills or the run ends).
    try:
        import sys
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger(name)


def load_json(path: Path) -> Any:
    with open(path) as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def load_json_gz(path: Path) -> Any:
    import gzip
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def save_json_gz(path: Path, data: Any) -> None:
    """Compact, gzipped JSON — for machine-only caches (grouped daily, aggregates)."""
    import gzip
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), default=str)


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #

def parse_date(s: str) -> Optional[date]:
    """Parse the common disclosure date formats; return None on failure."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def slugify(s: str) -> str:
    """Lowercase ASCII slug: letters/digits to-, collapse repeats."""
    import re
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def most_recent_trading_day(d: Optional[date] = None) -> date:
    """Return d (or today), stepped back off weekends. Grouped-daily handles holidays."""
    d = d or date.today()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


# --------------------------------------------------------------------------- #
# HTTP (House Clerk + Senate eFD)
# --------------------------------------------------------------------------- #

def get_user_agent() -> str:
    return os.environ.get("HTTP_USER_AGENT") or load_config()["http"]["user_agent"]


def http_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = get_user_agent()
    s.headers["Accept-Encoding"] = "gzip, deflate"
    return s


# --------------------------------------------------------------------------- #
# Rate limiting + Polygon client
# --------------------------------------------------------------------------- #

def fmt_duration(seconds: float) -> str:
    """Human-readable duration, e.g. '4m20s' or '1h05m'."""
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m{s:02d}s"


class Progress:
    """Lightweight progress reporter: logs count, percent, elapsed and ETA."""

    def __init__(self, total: int, label: str, log: Optional[logging.Logger] = None,
                 every: int = 1):
        self.total = max(int(total), 0)
        self.label = label
        self.log = log or logging.getLogger("progress")
        self.every = max(int(every), 1)
        self.i = 0
        self.start = time.monotonic()

    def step(self, msg: str = "") -> None:
        self.i += 1
        if self.i != self.total and self.i % self.every:
            return
        elapsed = time.monotonic() - self.start
        rate = self.i / elapsed if elapsed > 0 else 0
        eta = (self.total - self.i) / rate if rate > 0 else 0
        pct = (100 * self.i / self.total) if self.total else 100
        self.log.info("  %s %d/%d (%.0f%%) | elapsed %s | eta %s%s",
                      self.label, self.i, self.total, pct,
                      fmt_duration(elapsed), fmt_duration(eta),
                      f" | {msg}" if msg else "")

    def done(self) -> None:
        self.log.info("  %s complete: %d in %s",
                      self.label, self.i, fmt_duration(time.monotonic() - self.start))


class RateLimiter:
    """Simple fixed-interval rate limiter."""

    def __init__(self, calls_per_minute: float):
        self._interval = 60.0 / calls_per_minute
        self._last_call = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_call
        if elapsed < self._interval:
            time.sleep(self._interval - elapsed)
        self._last_call = time.monotonic()


class PolygonClient:
    BASE = POLYGON_BASE

    def __init__(self, api_key: str, cfg: dict):
        self._key = api_key
        self._cfg = cfg
        self._limiter = RateLimiter(cfg["rate_limit_calls_per_min"])
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "CongressTradesTracker/1.0"
        for d in (POLYGON_CACHE, FINANCIALS_CACHE, NEWS_CACHE, AGGS_CACHE, GROUPED_CACHE):
            d.mkdir(parents=True, exist_ok=True)

    # -- low level ---------------------------------------------------------- #
    def _get(self, url: str, params: Optional[dict] = None) -> dict:
        if params is None:
            params = {}
        params["apiKey"] = self._key
        max_retries = self._cfg.get("max_retries", 3)
        for attempt in range(max_retries):
            self._limiter.wait()
            resp = self._session.get(url, params=params, timeout=30)
            if resp.status_code == 429:
                wait = 60 * (attempt + 1)
                logging.getLogger("polygon").warning(
                    "Rate limited, sleeping %ds (attempt %d)", wait, attempt + 1
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"Max retries exceeded for {url}")

    def _safe_err(self, e: Exception) -> str:
        return str(e).replace(self._key, "***")

    @staticmethod
    def _cache_fresh(path: Path, ttl_days: int) -> bool:
        if not path.exists():
            return False
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        return (datetime.now() - mtime).days < ttl_days

    # -- endpoints ---------------------------------------------------------- #
    def ticker_details(self, ticker: str) -> Optional[dict]:
        path = POLYGON_CACHE / f"{ticker}.json"
        if self._cache_fresh(path, self._cfg.get("details_ttl_days", 180)):
            return load_json(path)
        url = f"{self.BASE}/v3/reference/tickers/{polygon_ticker(ticker)}"
        try:
            data = self._get(url)
            result = data.get("results", {}) or {}
            save_json(path, result)
            return result
        except Exception as e:
            # Cache a definitive 'not found' (delisted/funds/ADRs) so we don't re-query
            # it every run and burn the free-tier budget; let transient errors retry.
            if getattr(getattr(e, "response", None), "status_code", None) == 404:
                save_json(path, {})
                logging.getLogger("polygon").debug("ticker_details 404 (cached) %s", ticker)
            else:
                logging.getLogger("polygon").warning(
                    "ticker_details failed for %s: %s", ticker, self._safe_err(e))
            return None

    def ticker_news(self, ticker: str) -> list:
        path = NEWS_CACHE / f"{ticker}.json"
        if self._cache_fresh(path, self._cfg.get("news_ttl_days", 7)):
            return load_json(path)
        url = f"{self.BASE}/v2/reference/news"
        try:
            data = self._get(url, {"ticker": ticker, "limit": self._cfg["news_limit"]})
            results = data.get("results", []) or []
            save_json(path, results)
            return results
        except Exception as e:
            logging.getLogger("polygon").warning(
                "ticker_news failed for %s: %s", ticker, self._safe_err(e))
            return load_json(path) if path.exists() else []

    def financials(self, ticker: str) -> Optional[dict]:
        """Most recent annual financial report (income statement + balance sheet)."""
        path = FINANCIALS_CACHE / f"{ticker}.json"
        if self._cache_fresh(path, self._cfg.get("financials_ttl_days", 90)):
            return load_json(path)
        url = f"{self.BASE}/vX/reference/financials"
        try:
            data = self._get(url, {"ticker": ticker, "timeframe": "annual",
                                   "limit": 1, "order": "desc", "sort": "filing_date"})
            results = data.get("results", []) or []
            result = results[0] if results else {}
            save_json(path, result)
            return result
        except Exception as e:
            logging.getLogger("polygon").warning(
                "financials failed for %s: %s", ticker, self._safe_err(e))
            return None

    def aggregates(self, ticker: str, from_date: str, to_date: str,
                   use_cache: bool = True) -> list:
        """Daily bars [from, to]. Cached per ticker with aggregates_ttl_days."""
        path = AGGS_CACHE / f"{ticker}.json.gz"
        ttl = self._cfg.get("aggregates_ttl_days", 7)
        if use_cache and self._cache_fresh(path, ttl):
            return load_json_gz(path)
        url = f"{self.BASE}/v2/aggs/ticker/{polygon_ticker(ticker)}/range/1/day/{from_date}/{to_date}"
        try:
            data = self._get(url, {"adjusted": "true", "sort": "asc", "limit": 50000})
            results = data.get("results", []) or []
            save_json_gz(path, results)
            return results
        except Exception as e:
            if getattr(getattr(e, "response", None), "status_code", None) == 404:
                save_json_gz(path, [])     # definitive miss — cache so we don't retry every run
                logging.getLogger("polygon").debug("aggregates 404 (cached) %s", ticker)
            else:
                logging.getLogger("polygon").warning(
                    "aggregates failed for %s: %s", ticker, self._safe_err(e))
            return []

    def grouped_daily(self, day: date, keep: Optional[set] = None) -> dict[str, float]:
        """
        {TICKER: close} for `day`. Permanently cached; empty file marks a known
        non-trading day. Falls back up to 4 prior days if `day` isn't a trading day.

        `keep`: if given, the saved snapshot is pruned to these tickers (we only
                ever read congress-traded tickers + the benchmark). Tickers missing
                from a pruned snapshot are priced from their own `aggs` history by
                the caller, so no grouped re-fetch is ever needed for new names.
        """
        for delta in range(5):
            target = day - timedelta(days=delta)
            cache_path = GROUPED_CACHE / f"{target.isoformat()}.json.gz"
            if cache_path.exists():
                data = load_json_gz(cache_path)
                if data:
                    return data
                continue  # empty == non-trading day, try previous
            url = f"{self.BASE}/v2/aggs/grouped/locale/us/market/stocks/{target.isoformat()}"
            try:
                payload = self._get(url, {"adjusted": "true"})
            except Exception as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                # Today's (or a future) snapshot isn't available yet on the free tier — that's
                # expected; we fall back to the prior trading day. Only warn on real failures.
                if status == 403 or target >= date.today():
                    logging.getLogger("polygon").debug(
                        "grouped daily for %s not available yet — using an earlier close", target)
                else:
                    logging.getLogger("polygon").warning(
                        "grouped_daily failed for %s: %s", target, self._safe_err(e))
                continue
            results = payload.get("results") or []
            if not results:
                save_json_gz(cache_path, {})  # non-trading day marker
                continue
            # Map Polygon symbols back to our ledger tickers (BRK.B -> BRKB) so share
            # classes aren't dropped by the keep-set filter.
            prices = {}
            for r in results:
                if "T" not in r or "c" not in r:
                    continue
                t = TICKER_ALIASES_REV.get(r["T"], r["T"])
                if keep is None or t in keep:
                    prices[t] = r["c"]
            save_json_gz(cache_path, prices)
            logging.getLogger("polygon").info(
                "Fetched grouped daily %s: %d tickers (kept)", target, len(prices))
            return prices
        return {}
