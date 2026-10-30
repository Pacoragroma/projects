"""Shared helpers for the day-trader skill pack.

Standard library only, Python 3.9+.

The canonical copy lives in day-trader/shared/trader_common.py. Every skill ships an
identical copy in its own scripts/ folder so each skill still works when installed on
its own. Edit the canonical copy, then run `python3 tools/sync_common.py`.

Data sources (all free, no API key):
  * Yahoo Finance chart API  - daily and intraday OHLCV bars, last price
  * Nasdaq Trader symbol directory - every US-listed symbol
  * SEC EDGAR - ticker -> CIK map, company filings
  * House Clerk financial disclosures - congressional trade reports (pelosi skill)

State (settings, saved trade plans, the paper-trading ledger, alerts, caches) lives in
$DAYTRADER_HOME, default ~/.day-trader.
"""
from __future__ import annotations

import argparse
import copy
import csv
import gzip
import hashlib
import io
import json
import math
import os
import random
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

VERSION = "1.0.0"
DISCLAIMER = (
    "Not financial advice. Paper trading only - nothing here can place a real order. "
    "Simulations and backtests are not guarantees."
)


class FetchError(RuntimeError):
    """A data source could not be reached or returned something unusable."""


class StateError(RuntimeError):
    """A local state file is missing, corrupt, or inconsistent."""


# ---------------------------------------------------------------------------
# Local state
# ---------------------------------------------------------------------------

def home() -> Path:
    """Directory for all local state. Override with $DAYTRADER_HOME."""
    p = Path(os.environ.get("DAYTRADER_HOME") or (Path.home() / ".day-trader")).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_path(name: str) -> Path:
    p = home() / name
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_json(name: str, default: Any = None) -> Any:
    """Read a JSON state file relative to home(); return a copy of `default` if absent."""
    p = state_path(name)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return copy.deepcopy(default)
    except json.JSONDecodeError as e:
        raise StateError(f"{p} is not valid JSON ({e}); fix or delete it") from e


def save_json(name: str, data: Any) -> Path:
    """Atomically write a JSON state file relative to home()."""
    p = state_path(name)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
            f.write("\n")
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


DEFAULT_SETTINGS: Dict[str, Any] = {
    "account_size": 25000.0,        # paper account equity in dollars
    "risk_per_trade_pct": 0.2,      # % of account risked per trade (25k * 0.2% = $50)
    "max_position_pct": 25.0,       # max notional of one position as % of account
    "daily_max_loss": 150.0,        # stop trading for the day past this loss ($)
    "max_open_risk": 150.0,         # total risk across open positions ($)
    "max_trades_per_day": 5,
    "max_consecutive_losses": 3,    # tilt guard
    "min_reward_risk": 1.0,         # warn below this reward:risk
    "ticket_ttl_minutes": 15,       # unconfirmed paper tickets expire
    "contact_email": "",            # sent to SEC EDGAR in the User-Agent, as SEC asks
}


def load_settings() -> Dict[str, Any]:
    s = dict(DEFAULT_SETTINGS)
    stored = load_json("settings.json", {}) or {}
    s.update({k: v for k, v in stored.items() if k in DEFAULT_SETTINGS})
    return s


def save_settings(updates: Dict[str, Any]) -> Dict[str, Any]:
    unknown = sorted(set(updates) - set(DEFAULT_SETTINGS))
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(unknown)}")
    stored = load_json("settings.json", {}) or {}
    for k, v in updates.items():
        default = DEFAULT_SETTINGS[k]
        if isinstance(default, bool):
            v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
        elif isinstance(default, int):
            v = int(v)
        elif isinstance(default, float):
            v = float(v)
            if v < 0:
                raise ValueError(f"{k} must be >= 0")
        stored[k] = v
    save_json("settings.json", stored)
    return load_settings()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _user_agent(url: str) -> str:
    host = urllib.parse.urlsplit(url).hostname or ""
    if host == "sec.gov" or host.endswith(".sec.gov"):
        # SEC asks automated clients to identify themselves with a contact address.
        contact = os.environ.get("DAYTRADER_CONTACT") or load_settings().get("contact_email") or ""
        return f"day-trader-skill/{VERSION} {contact}".strip() if contact else f"day-trader-skill/{VERSION} (personal research)"
    return BROWSER_UA


def fixture_name(url: str) -> str:
    """File name used for `url` in $DAYTRADER_FIXTURES (offline tests and demos)."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", url.split("://", 1)[-1]).strip("_")[:120]
    return f"{slug}__{hashlib.sha1(url.encode('utf-8')).hexdigest()[:10]}"


def http_get(url: str, *, headers: Optional[Dict[str, str]] = None, timeout: float = 20.0,
             retries: int = 3) -> bytes:
    """GET `url` and return the body. Retries 429/5xx and network errors with backoff.

    If $DAYTRADER_FIXTURES is set, responses are read from that directory instead of the
    network (file name = fixture_name(url)); a missing fixture raises FetchError.
    """
    fixtures = os.environ.get("DAYTRADER_FIXTURES")
    if fixtures:
        p = Path(fixtures) / fixture_name(url)
        if p.exists():
            return p.read_bytes()
        raise FetchError(f"offline fixture missing for {url} (expected {p.name})")

    hdrs = {"User-Agent": _user_agent(url), "Accept": "*/*", "Accept-Encoding": "gzip"}
    if headers:
        hdrs.update(headers)
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(min(2 ** attempt, 8) + random.random() * 0.3)
                continue
            hint = ""
            if e.code == 403:
                hint = " - access denied; if you are in a sandbox, this host may need to be allow-listed"
            elif e.code == 404:
                hint = " - not found"
            elif e.code == 429:
                hint = " - rate limited; wait a minute and retry with fewer requests"
            raise FetchError(f"HTTP {e.code} for {url}{hint}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            if attempt < retries:
                time.sleep(min(2 ** attempt, 8) + random.random() * 0.3)
                continue
            reason = getattr(e, "reason", e)
            raise FetchError(f"network error for {url}: {reason}") from e
    raise FetchError(f"giving up on {url}")  # pragma: no cover - loop always returns or raises


def cached_get(url: str, ttl: float = 0, **kw: Any) -> bytes:
    """http_get with an on-disk cache under home()/cache. ttl in seconds; 0 disables."""
    if ttl <= 0 or os.environ.get("DAYTRADER_FIXTURES") or os.environ.get("DAYTRADER_NO_CACHE"):
        return http_get(url, **kw)
    p = home() / "cache" / fixture_name(url)
    try:
        if time.time() - p.stat().st_mtime < ttl:
            return p.read_bytes()
    except FileNotFoundError:
        pass
    data = http_get(url, **kw)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, p)
    return data


def get_json(url: str, ttl: float = 0, **kw: Any) -> Any:
    raw = cached_get(url, ttl, **kw)
    try:
        return json.loads(raw)
    except ValueError as e:
        raise FetchError(f"expected JSON from {url}, got: {raw[:120]!r}") from e


def get_text(url: str, ttl: float = 0, **kw: Any) -> str:
    return cached_get(url, ttl, **kw).decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Time (US/Eastern, regular session 09:30-16:00)
# ---------------------------------------------------------------------------

try:  # tzdata may be missing (e.g. some Windows Pythons) - fall back to the US DST rule.
    from zoneinfo import ZoneInfo

    ET: Any = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - depends on platform
    ET = None

MARKET_OPEN = 9 * 60 + 30   # minutes after midnight ET
MARKET_CLOSE = 16 * 60


def _us_eastern_offset(utc_dt: datetime) -> timedelta:
    """UTC offset for US Eastern at `utc_dt` (DST: 2nd Sun Mar 07:00Z - 1st Sun Nov 06:00Z)."""
    y = utc_dt.year
    mar1 = datetime(y, 3, 1, tzinfo=timezone.utc)
    dst_start = mar1.replace(day=1 + (6 - mar1.weekday()) % 7 + 7, hour=7)
    nov1 = datetime(y, 11, 1, tzinfo=timezone.utc)
    dst_end = nov1.replace(day=1 + (6 - nov1.weekday()) % 7, hour=6)
    return timedelta(hours=-4) if dst_start <= utc_dt < dst_end else timedelta(hours=-5)


def to_et(ts: float) -> datetime:
    """Unix seconds -> aware datetime in US/Eastern."""
    utc = datetime.fromtimestamp(ts, tz=timezone.utc)
    if ET is not None:
        return utc.astimezone(ET)
    return utc.astimezone(timezone(_us_eastern_offset(utc)))


def parse_et(value: str) -> datetime:
    """Parse an ISO date/time. Naive values are taken as US/Eastern wall-clock time."""
    dt = datetime.fromisoformat(value.strip())
    if dt.tzinfo is None:
        if ET is not None:
            dt = dt.replace(tzinfo=ET)
        else:
            guess = (dt + timedelta(hours=5)).replace(tzinfo=timezone.utc)
            dt = dt.replace(tzinfo=timezone(_us_eastern_offset(guess)))
    return to_et(dt.timestamp())


def now_et() -> datetime:
    """Current time in US/Eastern. $DAYTRADER_NOW (ISO, ET) overrides it for replays/tests."""
    override = os.environ.get("DAYTRADER_NOW")
    if override:
        return parse_et(override)
    return to_et(time.time())


def today_et() -> str:
    return now_et().strftime("%Y-%m-%d")


def iso_now() -> str:
    return now_et().isoformat(timespec="seconds")


def et_date(ts: float) -> str:
    return to_et(ts).strftime("%Y-%m-%d")


def et_hhmm(ts: float) -> str:
    return to_et(ts).strftime("%H:%M")


def minute_of_day(ts: float) -> int:
    d = to_et(ts)
    return d.hour * 60 + d.minute


def parse_hhmm(value: str) -> int:
    """'10:15' -> 615 (minutes after midnight)."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"expected HH:MM, got {value!r}")
    return int(m.group(1)) * 60 + int(m.group(2))


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def in_regular_session(ts: float) -> bool:
    return MARKET_OPEN <= minute_of_day(ts) < MARKET_CLOSE


def is_market_open(at: Optional[datetime] = None) -> bool:
    """True during 09:30-16:00 ET on weekdays (exchange holidays are not modelled)."""
    d = at or now_et()
    return d.weekday() < 5 and MARKET_OPEN <= d.hour * 60 + d.minute < MARKET_CLOSE


# ---------------------------------------------------------------------------
# Bars (Yahoo Finance chart API)
# ---------------------------------------------------------------------------

@dataclass
class Bar:
    ts: int          # bar start, unix seconds
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def date(self) -> str:
        return et_date(self.ts)

    @property
    def time(self) -> str:
        return et_hhmm(self.ts)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["date"], d["time"] = self.date, self.time
        return d


YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")


def yahoo_symbol(symbol: str) -> str:
    """Nasdaq-style 'BRK.B' -> Yahoo-style 'BRK-B'."""
    return re.sub(r"[./ ]", "-", symbol.strip().upper())


def parse_yahoo_chart(payload: Dict[str, Any]) -> Tuple[List[Bar], Dict[str, Any]]:
    """Turn a v8/finance/chart JSON payload into (bars, meta). Rows with gaps are dropped."""
    chart = (payload or {}).get("chart") or {}
    err = chart.get("error")
    if err:
        raise FetchError(f"Yahoo: {err.get('code')}: {err.get('description')}")
    results = chart.get("result") or []
    if not results:
        raise FetchError("Yahoo returned no chart data")
    r = results[0] or {}
    meta = r.get("meta") or {}
    stamps = r.get("timestamp") or []
    quote = (((r.get("indicators") or {}).get("quote")) or [{}])[0] or {}
    cols = [quote.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    bars: List[Bar] = []
    for i, t in enumerate(stamps):
        try:
            o, h, l, c = (cols[j][i] for j in range(4))
        except IndexError:
            continue
        if o is None or h is None or l is None or c is None:
            continue
        v = cols[4][i] if i < len(cols[4]) and cols[4][i] is not None else 0
        o, h, l, c = float(o), float(h), float(l), float(c)
        if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)):
            continue
        bars.append(Bar(int(t), o, max(h, o, c), min(l, o, c), c, float(v)))
    bars.sort(key=lambda b: b.ts)
    return bars, meta


def fetch_bars(symbol: str, interval: str = "5m", range_: str = "60d", prepost: bool = False,
               ttl: Optional[float] = None) -> Tuple[List[Bar], Dict[str, Any]]:
    """OHLCV bars from Yahoo. Intraday intervals: 1m (last ~7d), 5m/15m (last 60d)."""
    if ttl is None:
        ttl = 30 if interval.endswith("m") else 900
    params = urllib.parse.urlencode(
        {"interval": interval, "range": range_, "includePrePost": "true" if prepost else "false"})
    last: Optional[FetchError] = None
    for host in YAHOO_HOSTS:
        url = f"https://{host}/v8/finance/chart/{urllib.parse.quote(yahoo_symbol(symbol))}?{params}"
        try:
            return parse_yahoo_chart(get_json(url, ttl))
        except FetchError as e:
            last = e
            if "HTTP 404" in str(e) or str(e).startswith("Yahoo:"):
                break  # unknown symbol - the other host will say the same
    raise FetchError(f"{symbol}: {last}")


def quote(symbol: str) -> Dict[str, Any]:
    """Latest regular-session price and basics from the chart endpoint."""
    bars, meta = fetch_bars(symbol, "1m", "1d", ttl=15)
    price = meta.get("regularMarketPrice") or (bars[-1].close if bars else None)
    if price is None:
        raise FetchError(f"{symbol}: no price available")
    return {
        "symbol": symbol.upper(),
        "price": float(price),
        "previous_close": meta.get("chartPreviousClose") or meta.get("previousClose"),
        "time": meta.get("regularMarketTime"),
        "currency": meta.get("currency"),
        "exchange": meta.get("fullExchangeName") or meta.get("exchangeName"),
        "name": meta.get("longName") or meta.get("shortName"),
    }


def regular_session(bars: Iterable[Bar]) -> List[Bar]:
    return [b for b in bars if in_regular_session(b.ts)]


def group_sessions(bars: Iterable[Bar], regular_only: bool = True) -> Dict[str, List[Bar]]:
    """{'YYYY-MM-DD': [bars...]} in date order (ET dates)."""
    out: Dict[str, List[Bar]] = {}
    for b in bars:
        if regular_only and not in_regular_session(b.ts):
            continue
        out.setdefault(b.date, []).append(b)
    return dict(sorted(out.items()))


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def sma(values: Sequence[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(values: Sequence[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < n:
        return out
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / n
    out[n - 1] = e
    for i in range(n, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def rsi(closes: Sequence[float], n: int = 14) -> List[Optional[float]]:
    """Wilder's RSI."""
    out: List[Optional[float]] = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / n, losses / n

    def value(g: float, l: float) -> float:
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + g / l)

    out[n] = value(avg_g, avg_l)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0.0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0.0)) / n
        out[i] = value(avg_g, avg_l)
    return out


def true_ranges(bars: Sequence[Bar]) -> List[float]:
    out: List[float] = []
    for i, b in enumerate(bars):
        if i == 0:
            out.append(b.high - b.low)
        else:
            pc = bars[i - 1].close
            out.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    return out


def atr(bars: Sequence[Bar], n: int = 14) -> Optional[float]:
    """Latest Wilder ATR, or None with fewer than n bars."""
    trs = true_ranges(bars)
    if len(trs) < n:
        return None
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def vwap(bars: Sequence[Bar]) -> List[float]:
    """Cumulative VWAP over `bars` (pass a single session)."""
    out: List[float] = []
    pv = vol = 0.0
    for b in bars:
        tp = (b.high + b.low + b.close) / 3.0
        pv += tp * b.volume
        vol += b.volume
        out.append(pv / vol if vol > 0 else tp)
    return out


# ---------------------------------------------------------------------------
# Symbol universe (Nasdaq Trader symbol directory)
# ---------------------------------------------------------------------------

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
EXCHANGE_NAMES = {"Q": "NASDAQ", "A": "NYSE American", "N": "NYSE", "P": "NYSE Arca",
                  "Z": "Cboe BZX", "V": "IEX"}
_NON_COMMON = re.compile(
    r"\b(warrants?|rights?|units?|preferred|preference|notes? due|debentures?|subordinated)\b|%",
    re.IGNORECASE)


def parse_symbol_file(text: str, source: str) -> List[Dict[str, Any]]:
    """Parse nasdaqlisted.txt (source='nasdaq') or otherlisted.txt (source='other')."""
    rows: List[Dict[str, Any]] = []
    for r in csv.DictReader(io.StringIO(text), delimiter="|"):
        sym = (r.get("Symbol") or r.get("ACT Symbol") or "").strip()
        if not sym or sym.startswith("File Creation Time"):
            continue
        if (r.get("Test Issue") or "").strip().upper() == "Y":
            continue
        exch = "Q" if source == "nasdaq" else (r.get("Exchange") or "").strip()
        rows.append({
            "symbol": sym,
            "name": (r.get("Security Name") or "").strip(),
            "exchange": EXCHANGE_NAMES.get(exch, exch),
            "etf": (r.get("ETF") or "").strip().upper() == "Y",
        })
    return rows


def is_common_stock(row: Dict[str, Any]) -> bool:
    """Drop ETFs, warrants, units, rights, preferreds and notes."""
    if row.get("etf"):
        return False
    if any(ch in row["symbol"] for ch in "$^=~"):
        return False
    return not _NON_COMMON.search(row.get("name") or "")


def us_symbols(include_etfs: bool = False, ttl: float = 86400) -> List[Dict[str, Any]]:
    """Every US-listed common stock (about 5,900 names), optionally plus ETFs."""
    rows = (parse_symbol_file(get_text(NASDAQ_LISTED_URL, ttl), "nasdaq")
            + parse_symbol_file(get_text(OTHER_LISTED_URL, ttl), "other"))
    seen, out = set(), []
    for r in rows:
        if r["symbol"] in seen:
            continue
        if is_common_stock(r) or (include_etfs and r["etf"]):
            seen.add(r["symbol"])
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# SEC EDGAR
# ---------------------------------------------------------------------------

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
EIGHT_K_ITEMS = {
    "1.01": "Material agreement", "1.02": "Agreement terminated", "1.03": "Bankruptcy",
    "1.05": "Cybersecurity incident", "2.01": "Acquisition or disposition completed",
    "2.02": "Results of operations (earnings)", "2.03": "New debt obligation",
    "2.04": "Debt acceleration", "2.05": "Restructuring / exit costs", "2.06": "Impairment",
    "3.01": "Delisting notice", "3.02": "Unregistered equity sale",
    "3.03": "Change to holders' rights", "4.01": "Auditor change",
    "4.02": "Financials no longer reliable", "5.01": "Change in control",
    "5.02": "Director/officer change", "5.03": "Charter or bylaw change",
    "5.07": "Shareholder vote", "7.01": "Reg FD disclosure", "8.01": "Other events",
    "9.01": "Financial statements and exhibits",
}


def sec_ticker_map(ttl: float = 86400) -> Dict[str, Dict[str, str]]:
    data = get_json(SEC_TICKERS_URL, ttl)
    rows = data.values() if isinstance(data, dict) else data
    return {str(v["ticker"]).upper(): {"cik": f"{int(v['cik_str']):010d}", "title": v.get("title", "")}
            for v in rows}


def sec_cik(ticker: str) -> Optional[str]:
    """10-digit CIK for a ticker, or None."""
    m = sec_ticker_map()
    t = ticker.strip().upper()
    for cand in (t, t.replace("-", "."), t.replace(".", "-")):
        if cand in m:
            return m[cand]["cik"]
    return None


def sec_submissions(cik: str, ttl: float = 900) -> Dict[str, Any]:
    return get_json(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", ttl)


def sec_recent_filings(ticker: str, forms: Optional[Iterable[str]] = None, limit: int = 20,
                       since: Optional[str] = None) -> List[Dict[str, Any]]:
    """Newest-first filings for a ticker. `forms` e.g. {'8-K', '4'}; `since` 'YYYY-MM-DD'."""
    cik = sec_cik(ticker)
    if not cik:
        raise FetchError(f"{ticker} is not in SEC's ticker list")
    recent = ((sec_submissions(cik).get("filings") or {}).get("recent")) or {}
    wanted = set(forms) if forms else None

    def col(name: str, i: int) -> Any:
        values = recent.get(name) or []
        return values[i] if i < len(values) else None

    out: List[Dict[str, Any]] = []
    for i in range(len(recent.get("accessionNumber") or [])):
        form = col("form", i)
        filed = col("filingDate", i) or ""
        if wanted and form not in wanted:
            continue
        if since and filed < since:
            continue
        acc = col("accessionNumber", i)
        doc = col("primaryDocument", i) or ""
        out.append({
            "form": form,
            "filed": filed,
            "accepted": col("acceptanceDateTime", i),
            "accession": acc,
            "items": [x.strip() for x in (col("items", i) or "").split(",") if x.strip()],
            "description": col("primaryDocDescription", i),
            "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}",
        })
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Trade plans (written by entry/exit/time-machine, read by risk/broker/alerts)
# ---------------------------------------------------------------------------

def _safe_symbol(symbol: str) -> str:
    return re.sub(r"[^A-Z0-9._-]", "_", symbol.strip().upper())


def validate_levels(side: str, entry: float, stop: float, target: Optional[float]) -> List[str]:
    """Problems with a long/short plan's prices (empty list = OK)."""
    errs: List[str] = []
    if side not in ("long", "short"):
        errs.append(f"side must be long or short, not {side!r}")
        return errs
    for name, v in (("entry", entry), ("stop", stop), ("target", target)):
        if v is not None and (not math.isfinite(v) or v <= 0):
            errs.append(f"{name} must be a positive price")
    if errs:
        return errs
    if side == "long":
        if stop >= entry:
            errs.append("for a long, the stop must be below the entry")
        if target is not None and target <= entry:
            errs.append("for a long, the target must be above the entry")
    else:
        if stop <= entry:
            errs.append("for a short, the stop must be above the entry")
        if target is not None and target >= entry:
            errs.append("for a short, the target must be below the entry")
    return errs


def reward_risk(side: str, entry: float, stop: float, target: float) -> float:
    risk = abs(entry - stop)
    reward = (target - entry) if side == "long" else (entry - target)
    return reward / risk if risk > 0 else float("nan")


def save_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Store/merge a plan at plans/<SYMBOL>.json. Needs symbol, side, entry, stop."""
    for k in ("symbol", "side", "entry", "stop"):
        if plan.get(k) in (None, ""):
            raise ValueError(f"plan is missing {k}")
    errs = validate_levels(plan["side"], float(plan["entry"]), float(plan["stop"]),
                           None if plan.get("target") is None else float(plan["target"]))
    if errs:
        raise ValueError("; ".join(errs))
    merged = load_plan(plan["symbol"]) or {}
    merged.update(plan)
    merged["symbol"] = plan["symbol"].upper()
    merged.setdefault("created_at", iso_now())
    merged["updated_at"] = iso_now()
    save_json(f"plans/{_safe_symbol(plan['symbol'])}.json", merged)
    return merged


def load_plan(symbol: str) -> Optional[Dict[str, Any]]:
    return load_json(f"plans/{_safe_symbol(symbol)}.json", None)


def list_plans() -> List[Dict[str, Any]]:
    d = home() / "plans"
    if not d.exists():
        return []
    plans = []
    for p in sorted(d.glob("*.json")):
        try:
            plans.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return plans


# ---------------------------------------------------------------------------
# Paper ledger (broker writes it; budget, risk and analyst read it)
#
# ledger.json = {"tickets": [ticket...], "trades": [trade...]}
# ticket: id, symbol, side, qty, entry, stop, target, setup, status
#         (pending|confirmed|cancelled|expired), created_at, expires_at, trade_id
# trade:  id, ticket_id, symbol, side, qty, entry_price, stop, target, setup,
#         opened_at, status (open|closed), exit_price, closed_at, exit_reason, pnl, r_multiple
# ---------------------------------------------------------------------------

LEDGER_FILE = "ledger.json"


def load_ledger() -> Dict[str, Any]:
    led = load_json(LEDGER_FILE, {"tickets": [], "trades": []}) or {}
    led.setdefault("tickets", [])
    led.setdefault("trades", [])
    return led


def save_ledger(ledger: Dict[str, Any]) -> None:
    save_json(LEDGER_FILE, ledger)


def new_id(prefix: str, existing: Iterable[str]) -> str:
    """e.g. new_id('TKT', ids) -> 'TKT-20261030-003'."""
    stem = f"{prefix}-{now_et().strftime('%Y%m%d')}-"
    n = [int(x[len(stem):]) for x in existing if x.startswith(stem) and x[len(stem):].isdigit()]
    return f"{stem}{(max(n) + 1) if n else 1:03d}"


def trade_initial_risk(trade: Dict[str, Any]) -> float:
    return abs(float(trade["entry_price"]) - float(trade["stop"])) * float(trade["qty"])


def trade_pnl(trade: Dict[str, Any], price: float) -> float:
    q, e = float(trade["qty"]), float(trade["entry_price"])
    return (price - e) * q if trade["side"] == "long" else (e - price) * q


def _on_day(stamp: Optional[str], day: str) -> bool:
    return bool(stamp) and str(stamp)[:10] == day


def day_summary(ledger: Optional[Dict[str, Any]] = None, day: Optional[str] = None) -> Dict[str, Any]:
    """Today's paper-trading numbers: realized P&L, open risk, trade count, loss streak."""
    led = ledger if ledger is not None else load_ledger()
    day = day or today_et()
    trades = led.get("trades", [])
    opened = [t for t in trades if _on_day(t.get("opened_at"), day)]
    closed = sorted((t for t in trades if t.get("status") == "closed" and _on_day(t.get("closed_at"), day)),
                    key=lambda t: t.get("closed_at") or "")
    open_trades = [t for t in trades if t.get("status") == "open"]
    realized = sum(float(t.get("pnl") or 0) for t in closed)
    streak = 0
    for t in reversed(closed):
        if float(t.get("pnl") or 0) < 0:
            streak += 1
        else:
            break
    return {
        "date": day,
        "trades_today": len(opened),
        "closed_today": len(closed),
        "wins": sum(1 for t in closed if float(t.get("pnl") or 0) > 0),
        "losses": sum(1 for t in closed if float(t.get("pnl") or 0) < 0),
        "realized_pnl": round(realized, 2),
        "open_positions": len(open_trades),
        "open_risk": round(sum(trade_initial_risk(t) for t in open_trades), 2),
        "risked_today": round(sum(trade_initial_risk(t) for t in opened), 2),
        "loss_streak": streak,
    }


def budget_check(new_risk: float = 0.0, settings: Optional[Dict[str, Any]] = None,
                 ledger: Optional[Dict[str, Any]] = None, day: Optional[str] = None) -> Dict[str, Any]:
    """Would taking `new_risk` dollars of risk now break a daily limit?"""
    s = settings or load_settings()
    summ = day_summary(ledger, day)
    used = max(0.0, -summ["realized_pnl"]) + summ["open_risk"]
    reasons: List[str] = []
    if used + new_risk > s["daily_max_loss"] + 1e-9:
        reasons.append(f"worst-case loss today would be ${used + new_risk:,.2f}, over the "
                       f"${s['daily_max_loss']:,.2f} daily max loss")
    if summ["open_risk"] + new_risk > s["max_open_risk"] + 1e-9:
        reasons.append(f"open risk would be ${summ['open_risk'] + new_risk:,.2f}, over the "
                       f"${s['max_open_risk']:,.2f} limit")
    if new_risk > 0 and summ["trades_today"] >= s["max_trades_per_day"]:
        reasons.append(f"already {summ['trades_today']} trades today (max {s['max_trades_per_day']})")
    if new_risk > 0 and summ["loss_streak"] >= s["max_consecutive_losses"]:
        reasons.append(f"{summ['loss_streak']} losses in a row - tilt guard says stop for today")
    return {
        "ok": not reasons,
        "reasons": reasons,
        "new_risk": round(new_risk, 2),
        "loss_budget_used": round(used, 2),
        "loss_budget_left": round(max(0.0, s["daily_max_loss"] - used), 2),
        "summary": summ,
        "limits": {k: s[k] for k in ("daily_max_loss", "max_open_risk", "max_trades_per_day",
                                     "max_consecutive_losses")},
    }


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------

def position_size(entry: float, stop: float, account: Optional[float] = None,
                  risk_pct: Optional[float] = None, risk_dollars: Optional[float] = None,
                  max_position_pct: Optional[float] = None,
                  settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Shares so that hitting the stop loses at most the risk budget, capped by max position size."""
    s = settings or load_settings()
    account = float(account if account is not None else s["account_size"])
    if risk_dollars is None:
        risk_dollars = account * float(risk_pct if risk_pct is not None else s["risk_per_trade_pct"]) / 100.0
    per_share = abs(float(entry) - float(stop))
    if per_share <= 0:
        raise ValueError("entry and stop must differ")
    if entry <= 0:
        raise ValueError("entry must be positive")
    shares = int(math.floor(risk_dollars / per_share + 1e-9))
    cap_pct = float(max_position_pct if max_position_pct is not None else s["max_position_pct"])
    max_notional = account * cap_pct / 100.0
    capped_by = "risk"
    if shares * entry > max_notional + 1e-9:
        shares = int(math.floor(max_notional / entry + 1e-9))
        capped_by = "max_position_pct"
    return {
        "shares": shares,
        "risk_per_share": round(per_share, 4),
        "risk_budget": round(risk_dollars, 2),
        "risk_used": round(shares * per_share, 2),
        "notional": round(shares * entry, 2),
        "account": account,
        "max_notional": round(max_notional, 2),
        "capped_by": capped_by,
    }


# ---------------------------------------------------------------------------
# CLI and output helpers
# ---------------------------------------------------------------------------

def make_parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--json", action="store_true", help="print machine-readable JSON")
    return p


def add_json_flag(parser: argparse.ArgumentParser) -> None:
    """For subcommand parsers: accept --json after the subcommand too."""
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="print machine-readable JSON")


def emit(data: Any, as_json: bool, render: Callable[[Any], str]) -> None:
    if as_json:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(render(data))


def run(main: Callable[[], Optional[int]]) -> None:
    """Run a CLI entry point with friendly errors: exit 1 bad input/state, 2 data source."""
    try:
        code = main()
    except FetchError as e:
        print(f"data error: {e}", file=sys.stderr)
        sys.exit(2)
    except (ValueError, StateError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(code or 0)


def fmt_price(x: Optional[float]) -> str:
    if x is None:
        return "-"
    return f"{x:,.2f}" if abs(x) >= 1 else f"{x:.4f}"


def fmt_money(x: Optional[float], signed: bool = False) -> str:
    if x is None:
        return "-"
    sign = "-" if x < 0 else ("+" if signed and x > 0 else "")
    return f"{sign}${abs(x):,.2f}"


def fmt_pct(x: Optional[float], digits: int = 1, signed: bool = False) -> str:
    if x is None:
        return "-"
    return f"{x:+.{digits}f}%" if signed else f"{x:.{digits}f}%"


def fmt_big(x: Optional[float]) -> str:
    """1234567 -> '1.23M'."""
    if x is None:
        return "-"
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(x) >= div:
            return f"{x / div:.2f}{unit}"
    return f"{x:.0f}"


def table(rows: Sequence[Sequence[Any]], headers: Sequence[str]) -> str:
    """Plain-text table; numbers right-aligned."""
    cells = [[str(h) for h in headers]] + [["" if c is None else str(c) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    numeric = [all(_looks_numeric(r[i]) for r in cells[1:]) and len(cells) > 1 for i in range(len(headers))]
    lines = []
    for j, r in enumerate(cells):
        lines.append("  ".join(c.rjust(widths[i]) if numeric[i] and j else c.ljust(widths[i])
                               for i, c in enumerate(r)).rstrip())
        if j == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def _looks_numeric(s: str) -> bool:
    return bool(re.fullmatch(r"[-+$]*[\d,.]+[%KMBTx]?|-", s.strip()))
