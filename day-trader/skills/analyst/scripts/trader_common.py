"""Shared helpers for the day-trader skill pack.

Standard library only, Python 3.9+.

The canonical copy lives in day-trader/shared/trader_common.py. Every skill ships an
identical copy in its own scripts/ folder so each skill still works when installed on
its own. Edit the canonical copy, then run `python3 tools/sync_common.py`.

Data sources (all free, no API key):
  * Yahoo Finance chart API  - US stocks: daily and intraday OHLCV bars, last price
  * Coinbase Exchange public market data - crypto (BTC-USD, ETH-USD, ...): candles, ticker, products
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
import http.client
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
import zlib
from contextlib import contextmanager
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
    "crypto_fee_pct": 0.6,          # paper fee per crypto fill, % of notional (Coinbase's entry taker tier)
    "contact_email": "",            # sent to SEC EDGAR in the User-Agent, as SEC asks
}


def _load_object(name: str) -> Optional[Dict[str, Any]]:
    """load_json for files that must hold a JSON object (None if the file is absent)."""
    data = load_json(name, None)
    if data is not None and not isinstance(data, dict):
        raise StateError(f"{state_path(name)} should contain a JSON object; fix or delete it")
    return data


def load_settings() -> Dict[str, Any]:
    s = dict(DEFAULT_SETTINGS)
    stored = _load_object("settings.json") or {}
    s.update({k: v for k, v in stored.items() if k in DEFAULT_SETTINGS})
    return s


def save_settings(updates: Dict[str, Any]) -> Dict[str, Any]:
    unknown = sorted(set(updates) - set(DEFAULT_SETTINGS))
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(unknown)}")
    stored = _load_object("settings.json") or {}
    for k, v in updates.items():
        default = DEFAULT_SETTINGS[k]
        if isinstance(default, bool):
            v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
        elif isinstance(default, int):
            v = int(v)
            if v < 0:
                raise ValueError(f"{k} must be >= 0")
        elif isinstance(default, float):
            v = float(v)
            if not math.isfinite(v) or v < 0:
                raise ValueError(f"{k} must be a number >= 0")
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


def _host_hint(url: str, status: int, proxy: bool = False) -> str:
    host = urllib.parse.urlsplit(url).hostname or ""
    if status == 403 and not proxy and (host == "sec.gov" or host.endswith(".sec.gov")) and not (
            os.environ.get("DAYTRADER_CONTACT") or load_settings().get("contact_email")):
        return (" - SEC asks for a contact email in the User-Agent: set DAYTRADER_CONTACT=you@example.com"
                " (or the contact_email setting); in a sandbox the host may also need to be allow-listed")
    if status in (403, 407):
        return f" - access denied; if you are in a sandbox, {host} may need to be allow-listed"
    if status == 404:
        return " - not found"
    if status == 429:
        return " - rate limited; wait a minute and retry with fewer requests"
    return ""


_PROXY_DENIED = re.compile(r"Tunnel connection failed:\s*40[37]", re.IGNORECASE)


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
            raise FetchError(f"HTTP {e.code} for {url}{_host_hint(url, e.code)}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError,
                http.client.HTTPException, EOFError, zlib.error) as e:
            reason = getattr(e, "reason", e)
            if _PROXY_DENIED.search(str(reason)):
                # A proxy refusing the tunnel is a policy decision, not a blip: don't retry.
                raise FetchError(f"blocked by proxy for {url} ({reason}){_host_hint(url, 403, proxy=True)}") from e
            if attempt < retries:
                time.sleep(min(2 ** attempt, 8) + random.random() * 0.3)
                continue
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
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, p)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return data


def forget_cached(url: str) -> None:
    """Drop the cached copy of `url` (e.g. after it turned out to be an error page)."""
    try:
        (home() / "cache" / fixture_name(url)).unlink()
    except FileNotFoundError:
        pass


def get_json(url: str, ttl: float = 0, **kw: Any) -> Any:
    raw = cached_get(url, ttl, **kw)
    try:
        return json.loads(raw)
    except ValueError as e:
        raise FetchError(f"expected JSON from {url}, got: {raw[:120]!r}") from e


def get_text(url: str, ttl: float = 0, **kw: Any) -> str:
    return cached_get(url, ttl, **kw).decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Markets and time
#
# Two markets share the desk. US stocks (the default) trade 09:30-16:00 US/Eastern on weekdays,
# priced from Yahoo Finance. Crypto (symbols like BTC-USD) trades 24/7: a "session" is a UTC
# calendar day, prices come from Coinbase Exchange's public API, and sizes are fractional.
# The *active* market decides what "now", "today", "the session" and HH:MM mean below; scripts
# switch it with use_market_for(symbol). The historical names (to_et, now_et, et_hhmm,
# in_regular_session, ...) follow the active market, so in crypto mode they are UTC.
# ---------------------------------------------------------------------------

try:  # tzdata may be missing (e.g. some Windows Pythons) - fall back to the US DST rule.
    from zoneinfo import ZoneInfo

    ET: Any = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - depends on platform
    ET = None


@dataclass(frozen=True)
class Market:
    key: str            # "us" | "crypto"
    name: str
    tz_label: str       # how times are labelled: "ET" | "UTC"
    open_min: int       # session open, minutes after local midnight
    close_min: int      # session close (exclusive)
    every_day: bool     # trades on weekends too
    time_stop: str      # default "flat by" time, local HH:MM
    fractional: bool    # position sizes may be fractional
    source: str         # where prices come from


US = Market("us", "US stocks", "ET", 9 * 60 + 30, 16 * 60, False, "15:55", False, "Yahoo Finance")
CRYPTO = Market("crypto", "Crypto", "UTC", 0, 24 * 60, True, "23:55", True, "Coinbase Exchange")
MARKETS = {"us": US, "crypto": CRYPTO}

CRYPTO_QUOTES = ("USD", "USDC", "USDT", "EUR", "GBP", "BTC", "ETH", "DAI")
_CRYPTO_SYMBOL = re.compile(r"[A-Z0-9]{2,12}-(?:%s)" % "|".join(CRYPTO_QUOTES))


def is_crypto_symbol(symbol: Optional[str]) -> bool:
    """'BTC-USD', 'ETH-USDC', 'SOL-EUR' -> True; stock tickers (including 'BRK-B') -> False."""
    return bool(_CRYPTO_SYMBOL.fullmatch((symbol or "").strip().upper()))


def market_for(symbol: Optional[str] = None) -> Market:
    """The market `symbol` trades in. With no symbol: $DAYTRADER_MARKET (us|crypto), else US."""
    if symbol:
        return CRYPTO if is_crypto_symbol(symbol) else US
    forced = (os.environ.get("DAYTRADER_MARKET") or "").strip().lower()
    if forced and forced not in MARKETS:
        raise ValueError(f"DAYTRADER_MARKET must be 'us' or 'crypto', not {forced!r}")
    return MARKETS.get(forced, US)


_ACTIVE: Market = US
MARKET_OPEN = US.open_min    # minutes after local midnight; rebound by set_market()
MARKET_CLOSE = US.close_min


def market() -> Market:
    """The active market."""
    return _ACTIVE


def set_market(m: Any) -> Market:
    """Make `m` (a Market or 'us'/'crypto') the active market."""
    global _ACTIVE, MARKET_OPEN, MARKET_CLOSE
    if isinstance(m, str):
        if m.lower() not in MARKETS:
            raise ValueError(f"market must be 'us' or 'crypto', not {m!r}")
        m = MARKETS[m.lower()]
    _ACTIVE = m
    MARKET_OPEN, MARKET_CLOSE = m.open_min, m.close_min
    return m


def use_market_for(symbol: Optional[str] = None) -> Market:
    """Activate the market of `symbol` (see market_for) and return it."""
    return set_market(market_for(symbol))


@contextmanager
def market_scope(symbol_or_market: Any):
    """Temporarily activate a market: `with market_scope("BTC-USD"): ...`."""
    previous = _ACTIVE
    target = symbol_or_market if isinstance(symbol_or_market, Market) else (
        MARKETS[symbol_or_market] if symbol_or_market in MARKETS else market_for(symbol_or_market))
    set_market(target)
    try:
        yield target
    finally:
        set_market(previous)


try:
    set_market(market_for(None))
except ValueError:  # a bad $DAYTRADER_MARKET is reported when a script asks for it
    pass


def tz_label() -> str:
    return _ACTIVE.tz_label


def _us_eastern_offset(utc_dt: datetime) -> timedelta:
    """UTC offset for US Eastern at `utc_dt` (DST: 2nd Sun Mar 07:00Z - 1st Sun Nov 06:00Z)."""
    y = utc_dt.year
    mar1 = datetime(y, 3, 1, tzinfo=timezone.utc)
    dst_start = mar1.replace(day=1 + (6 - mar1.weekday()) % 7 + 7, hour=7)
    nov1 = datetime(y, 11, 1, tzinfo=timezone.utc)
    dst_end = nov1.replace(day=1 + (6 - nov1.weekday()) % 7, hour=6)
    return timedelta(hours=-4) if dst_start <= utc_dt < dst_end else timedelta(hours=-5)


def to_local(ts: float, m: Optional[Market] = None) -> datetime:
    """Unix seconds -> aware datetime in the market's time zone (US/Eastern or UTC)."""
    utc = datetime.fromtimestamp(ts, tz=timezone.utc)
    if (m or _ACTIVE).tz_label == "UTC":
        return utc
    if ET is not None:
        return utc.astimezone(ET)
    return utc.astimezone(timezone(_us_eastern_offset(utc)))


def to_et(ts: float) -> datetime:
    """Unix seconds -> aware datetime in the active market's zone (US/Eastern for stocks)."""
    return to_local(ts)


def parse_iso_utc(value: str) -> float:
    """Unix seconds for an ISO stamp such as '2026-10-30T14:15:00.123456789Z' (naive = UTC)."""
    text = (value or "").strip().replace(" ", "T")
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?)(\.\d+)?(Z|[+-]\d{2}:?\d{2})?", text)
    if not m:
        raise ValueError(f"not an ISO time: {value!r}")
    base, frac, zone = m.group(1), m.group(2) or "", m.group(3) or "Z"
    zone = "+00:00" if zone == "Z" else (zone if ":" in zone else zone[:3] + ":" + zone[3:])
    dt = datetime.fromisoformat(base + zone)
    return dt.timestamp() + (float("0" + frac) if frac else 0.0)


def parse_et(value: str) -> datetime:
    """Parse an ISO date/time. Naive values are wall-clock time in the active market's zone."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        if _ACTIVE.tz_label == "UTC":
            dt = dt.replace(tzinfo=timezone.utc)
        elif ET is not None:
            dt = dt.replace(tzinfo=ET)
        else:
            guess = (dt + timedelta(hours=5)).replace(tzinfo=timezone.utc)
            dt = dt.replace(tzinfo=timezone(_us_eastern_offset(guess)))
    return to_local(dt.timestamp())


def now_et() -> datetime:
    """Current time in the active market's zone. $DAYTRADER_NOW (ISO) overrides it for replays."""
    override = os.environ.get("DAYTRADER_NOW")
    if override:
        return parse_et(override)
    return to_local(time.time())


def today_et() -> str:
    return now_et().strftime("%Y-%m-%d")


def iso_now() -> str:
    return now_et().isoformat(timespec="seconds")


def et_date(ts: float) -> str:
    return to_local(ts).strftime("%Y-%m-%d")


def et_hhmm(ts: float) -> str:
    return to_local(ts).strftime("%H:%M")


def minute_of_day(ts: float) -> int:
    d = to_local(ts)
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
    return _ACTIVE.open_min <= minute_of_day(ts) < _ACTIVE.close_min


def is_market_open(at: Optional[datetime] = None) -> bool:
    """US: 09:30-16:00 ET on weekdays (holidays not modelled). Crypto: always."""
    d = at or now_et()
    if not _ACTIVE.every_day and d.weekday() >= 5:
        return False
    return _ACTIVE.open_min <= d.hour * 60 + d.minute < _ACTIVE.close_min


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
    chart = payload.get("chart") if isinstance(payload, dict) else None
    if not isinstance(chart, dict):
        raise FetchError("unexpected Yahoo chart response")
    err = chart.get("error")
    if err:
        if isinstance(err, dict):
            raise FetchError(f"Yahoo: {err.get('code')}: {err.get('description')}")
        raise FetchError(f"Yahoo: {err}")
    results = chart.get("result") or []
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        raise FetchError("Yahoo returned no chart data")
    r = results[0]
    meta = r.get("meta") if isinstance(r.get("meta"), dict) else {}
    stamps = r.get("timestamp") or []
    quotes = (r.get("indicators") or {}).get("quote") if isinstance(r.get("indicators"), dict) else None
    quote = quotes[0] if isinstance(quotes, list) and quotes and isinstance(quotes[0], dict) else {}
    cols = [quote.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    bars: List[Bar] = []
    for i, t in enumerate(stamps):
        try:
            o, h, l, c = (cols[j][i] for j in range(4))
        except IndexError:
            continue
        if o is None or h is None or l is None or c is None:
            continue
        v = cols[4][i] if i < len(cols[4]) and isinstance(cols[4][i], (int, float)) else 0
        try:
            o, h, l, c = float(o), float(h), float(l), float(c)
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)):
            continue
        bars.append(Bar(int(t), o, max(h, o, c), min(l, o, c), c, float(v)))
    bars.sort(key=lambda b: b.ts)
    return bars, meta


def fetch_bars(symbol: str, interval: str = "5m", range_: str = "60d", prepost: bool = False,
               ttl: Optional[float] = None) -> Tuple[List[Bar], Dict[str, Any]]:
    """OHLCV bars. Stocks from Yahoo (1m: last ~7d, 5m/15m: last 60d); crypto from Coinbase."""
    if is_crypto_symbol(symbol):
        return coinbase_bars(symbol, interval, range_, ttl)
    if ttl is None:
        ttl = 30 if interval.endswith("m") else 900
    params = urllib.parse.urlencode(
        {"interval": interval, "range": range_, "includePrePost": "true" if prepost else "false"})
    errors: List[str] = []
    for host in YAHOO_HOSTS:
        url = f"https://{host}/v8/finance/chart/{urllib.parse.quote(yahoo_symbol(symbol))}?{params}"
        try:
            return parse_yahoo_chart(get_json(url, ttl))
        except FetchError as e:
            errors.append(str(e))
            if "HTTP 404" in str(e) or str(e).startswith("Yahoo:"):
                break  # unknown symbol - the other host will say the same
    raise FetchError(f"{symbol}: " + " | ".join(errors))


def quote(symbol: str) -> Dict[str, Any]:
    """Latest price and basics (stocks: Yahoo regular session; crypto: Coinbase ticker)."""
    if is_crypto_symbol(symbol):
        return coinbase_quote(symbol)
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
    """{'YYYY-MM-DD': [bars...]} in date order (dates in the active market's zone)."""
    out: Dict[str, List[Bar]] = {}
    for b in bars:
        if regular_only and not in_regular_session(b.ts):
            continue
        out.setdefault(b.date, []).append(b)
    return dict(sorted(out.items()))


# ---------------------------------------------------------------------------
# Crypto prices (Coinbase Exchange public market data - no account, no API key)
# ---------------------------------------------------------------------------

COINBASE_API = "https://api.exchange.coinbase.com"
COINBASE_GRANULARITY = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "60m": 3600, "6h": 21600, "1d": 86400}
COINBASE_MAX_CANDLES = 300      # per request
STABLECOINS = {"USDT", "USDC", "DAI", "PYUSD", "EURC", "GUSD", "USDP", "PAX", "BUSD", "TUSD", "FDUSD", "USDS"}


def range_seconds(range_: str) -> int:
    """'60d' / '5d' / '6mo' / '1y' -> seconds."""
    m = re.fullmatch(r"\s*(\d+)\s*(d|mo|y)\s*", range_ or "")
    if not m:
        raise ValueError(f"unsupported range {range_!r} (use e.g. 1d, 5d, 60d, 6mo, 1y)")
    n, unit = int(m.group(1)), m.group(2)
    return n * {"d": 86400, "mo": 31 * 86400, "y": 366 * 86400}[unit]


def _utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def coinbase_candle_windows(product: str, interval: str, range_: str,
                            now_ts: float) -> List[Tuple[str, int, int]]:
    """The candle requests covering `range_` up to `now_ts`: [(url, first_start, last_start)].

    Windows are aligned to multiples of 300 candles from the epoch, so the URL of a window
    that has fully closed never changes and its response can be cached for a long time.
    """
    gran = COINBASE_GRANULARITY.get(interval)
    if not gran:
        raise ValueError(f"unsupported crypto interval {interval!r} (use {', '.join(COINBASE_GRANULARITY)})")
    end = (int(now_ts) // gran) * gran                   # start of the candle forming now
    start = end - range_seconds(range_) + gran
    chunk = gran * COINBASE_MAX_CANDLES
    out = []
    a = (start // chunk) * chunk
    while a <= end:
        last = min(a + chunk - gran, end)
        url = (f"{COINBASE_API}/products/{urllib.parse.quote(product)}/candles?granularity={gran}"
               f"&start={_utc_iso(a)}&end={_utc_iso(last)}")
        out.append((url, a, last))
        a += chunk
    return out


def parse_coinbase_candles(rows: Any) -> List[Bar]:
    """[[time, low, high, open, close, volume], ...] (newest first) -> bars, oldest first."""
    if not isinstance(rows, list):
        detail = rows.get("message") if isinstance(rows, dict) else str(rows)[:120]
        raise FetchError(f"unexpected Coinbase candles response: {detail}")
    by_ts: Dict[int, Bar] = {}
    for r in rows:
        if not isinstance(r, (list, tuple)) or len(r) < 6:
            continue
        try:
            t, lo, hi, op, cl, vol = (float(x) for x in r[:6])
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(x) and x > 0 for x in (lo, hi, op, cl)) or not math.isfinite(t):
            continue
        by_ts[int(t)] = Bar(int(t), op, max(hi, op, cl), min(lo, op, cl), cl,
                            max(vol, 0.0) if math.isfinite(vol) else 0.0)
    return [by_ts[k] for k in sorted(by_ts)]


def _cache_fresh(url: str, ttl: float) -> bool:
    if ttl <= 0 or os.environ.get("DAYTRADER_FIXTURES") or os.environ.get("DAYTRADER_NO_CACHE"):
        return False
    try:
        return time.time() - (home() / "cache" / fixture_name(url)).stat().st_mtime < ttl
    except FileNotFoundError:
        return False


def coinbase_bars(symbol: str, interval: str = "5m", range_: str = "60d",
                  ttl: Optional[float] = None) -> Tuple[List[Bar], Dict[str, Any]]:
    """Coinbase candles for a product such as BTC-USD (paginated, ~0.15 s between requests)."""
    product = symbol.strip().upper()
    now_ts = now_et().timestamp()
    live_ttl = ttl if ttl is not None else (30 if interval.endswith("m") else 300)
    gran = COINBASE_GRANULARITY.get(interval) or 0
    windows = coinbase_candle_windows(product, interval, range_, now_ts)
    range_start = (int(now_ts) // gran) * gran - range_seconds(range_) + gran
    by_ts: Dict[int, Bar] = {}
    fetched = 0
    for url, _first, last in windows:
        window_ttl = 7 * 86400 if last + gran <= now_ts else live_ttl   # closed windows never change
        if fetched and not _cache_fresh(url, window_ttl) and not os.environ.get("DAYTRADER_FIXTURES"):
            time.sleep(0.15)   # public rate limit is ~10 requests/second
        try:
            rows = get_json(url, window_ttl)
        except FetchError as e:
            raise FetchError(f"{product}: {e}") from e
        fetched += 1
        for b in parse_coinbase_candles(rows):
            if range_start <= b.ts <= now_ts:   # aligned windows reach back past the range
                by_ts[b.ts] = b
    bars = [by_ts[k] for k in sorted(by_ts)]
    if not bars:
        raise FetchError(f"{product}: Coinbase returned no candles - is it a listed product (e.g. BTC-USD)?")
    base, _, quote_ccy = product.partition("-")
    meta = {"symbol": product, "currency": quote_ccy, "exchangeName": "Coinbase",
            "fullExchangeName": "Coinbase Exchange", "regularMarketPrice": bars[-1].close,
            "regularMarketTime": bars[-1].ts, "dataGranularity": interval, "market": "crypto",
            "shortName": base}
    return bars, meta


def coinbase_quote(symbol: str) -> Dict[str, Any]:
    """Last trade, bid and ask from the Coinbase ticker; previous_close = price 24 h ago."""
    product = symbol.strip().upper()
    base, _, quote_ccy = product.partition("-")
    t = get_json(f"{COINBASE_API}/products/{urllib.parse.quote(product)}/ticker", ttl=10)
    try:
        price = float(t["price"])
    except (KeyError, TypeError, ValueError) as e:
        raise FetchError(f"{product}: unexpected Coinbase ticker response") from e
    when = None
    try:
        when = parse_iso_utc(t["time"]) if t.get("time") else None
    except ValueError:
        pass
    prev = None
    try:
        prev = float(get_json(f"{COINBASE_API}/products/{urllib.parse.quote(product)}/stats", ttl=60)["open"])
    except (FetchError, KeyError, TypeError, ValueError):
        pass

    def num(key: str) -> Optional[float]:
        try:
            return float(t[key])
        except (KeyError, TypeError, ValueError):
            return None
    return {"symbol": product, "price": price, "previous_close": prev, "time": when,
            "currency": quote_ccy, "exchange": "Coinbase Exchange", "name": base,
            "bid": num("bid"), "ask": num("ask")}


def coinbase_products(ttl: float = 86400) -> List[Dict[str, Any]]:
    data = get_json(f"{COINBASE_API}/products", ttl)
    if not isinstance(data, list):
        raise FetchError("unexpected Coinbase products response")
    return [p for p in data if isinstance(p, dict) and p.get("id")]


def product_info(symbol: str) -> Dict[str, Any]:
    """Coinbase product rules: base_increment (size step) and quote_increment (price tick)."""
    product = symbol.strip().upper()
    for p in coinbase_products():
        if str(p["id"]).upper() == product:
            def inc(key: str, default: float) -> float:
                try:
                    v = float(p.get(key))
                    return v if v > 0 else default
                except (TypeError, ValueError):
                    return default
            return {"symbol": product, "base_increment": inc("base_increment", 1e-8),
                    "quote_increment": inc("quote_increment", 0.01),
                    "status": p.get("status"), "trading_disabled": bool(p.get("trading_disabled")),
                    "min_market_funds": p.get("min_market_funds")}
    raise FetchError(f"{product} is not a Coinbase product")


def crypto_symbols(quote: str = "USD", include_stable: bool = False, ttl: float = 86400) -> List[Dict[str, Any]]:
    """Tradable Coinbase products quoted in `quote` (stablecoin bases dropped by default)."""
    out = []
    for p in coinbase_products(ttl):
        if str(p.get("quote_currency", "")).upper() != quote.upper():
            continue
        if str(p.get("status", "online")).lower() != "online" or p.get("trading_disabled") or p.get("cancel_only"):
            continue
        base = str(p.get("base_currency", "")).upper()
        if not include_stable and base in STABLECOINS:
            continue
        out.append({"symbol": str(p["id"]).upper(), "name": base, "exchange": "Coinbase", "etf": False})
    return sorted(out, key=lambda r: r["symbol"])


def fee_pct(m: Optional[Market] = None, settings: Optional[Dict[str, Any]] = None) -> float:
    """Paper fee per fill in % of notional: crypto_fee_pct for crypto, 0 for US stocks."""
    if (m or _ACTIVE).key != "crypto":
        return 0.0
    return float((settings or load_settings())["crypto_fee_pct"])


def qty_step(m: Optional[Market] = None) -> float:
    """Default size increment: whole shares for stocks, 1e-8 for crypto (use product_info for exact)."""
    return 1e-8 if (m or _ACTIVE).fractional else 1.0


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
    out: Dict[str, Dict[str, str]] = {}
    try:
        for v in rows:
            if isinstance(v, dict) and v.get("ticker") and v.get("cik_str") is not None:
                out[str(v["ticker"]).upper()] = {"cik": f"{int(v['cik_str']):010d}", "title": v.get("title", "")}
    except (TypeError, ValueError) as e:
        raise FetchError(f"unexpected SEC ticker list format: {e}") from e
    if not out:
        raise FetchError("SEC ticker list was empty or in an unexpected format")
    return out


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
                       since: Optional[str] = None, ttl: float = 900) -> List[Dict[str, Any]]:
    """Newest-first filings for a ticker. `forms` e.g. {'8-K', '4'}; `since` 'YYYY-MM-DD'."""
    cik = sec_cik(ticker)
    if not cik:
        raise FetchError(f"{ticker} is not in SEC's ticker list")
    subs = sec_submissions(cik, ttl)
    filings = subs.get("filings") if isinstance(subs, dict) else None
    recent = filings.get("recent") if isinstance(filings, dict) else None
    if not isinstance(recent, dict):
        raise FetchError(f"unexpected SEC submissions format for {ticker}")
    wanted = set(forms) if forms else None

    def col(name: str, i: int) -> Any:
        values = recent.get(name)
        return values[i] if isinstance(values, list) and i < len(values) else None

    out: List[Dict[str, Any]] = []
    for i in range(len(recent.get("accessionNumber") or [])):
        form = col("form", i)
        filed = col("filingDate", i) or ""
        if wanted and form not in wanted:
            continue
        if since and filed < since:
            continue
        acc = col("accessionNumber", i)
        if not isinstance(acc, str):
            continue
        doc = col("primaryDocument", i) or ""
        out.append({
            "form": form,
            "filed": filed,
            "accepted": col("acceptanceDateTime", i),
            "accession": acc,
            "items": [x.strip() for x in str(col("items", i) or "").split(",") if x.strip()],
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
    target = merged.get("target")
    errs = validate_levels(merged["side"], float(merged["entry"]), float(merged["stop"]),
                           None if target is None else float(target))
    if errs:
        raise ValueError("saved plan would be inconsistent: " + "; ".join(errs)
                         + " (pass a new target, or set it to null)")
    merged.setdefault("created_at", iso_now())
    merged["updated_at"] = iso_now()
    save_json(f"plans/{_safe_symbol(plan['symbol'])}.json", merged)
    return merged


def load_plan(symbol: str) -> Optional[Dict[str, Any]]:
    return _load_object(f"plans/{_safe_symbol(symbol)}.json")


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
#         (pending|working|confirmed|cancelled|expired), created_at, expires_at, trade_id
#         ("working" = confirmed limit order not filled yet; the broker skill owns ticket logic)
# trade:  id, ticket_id, symbol, side, qty, entry_price, stop, target, setup,
#         opened_at, status (open|closed), exit_price, closed_at, exit_reason, pnl, r_multiple
# ---------------------------------------------------------------------------

LEDGER_FILE = "ledger.json"


def load_ledger() -> Dict[str, Any]:
    led = _load_object(LEDGER_FILE) or {}
    led.setdefault("tickets", [])
    led.setdefault("trades", [])
    if not isinstance(led["tickets"], list) or not isinstance(led["trades"], list):
        raise StateError(f"{state_path(LEDGER_FILE)}: 'tickets' and 'trades' must be lists")
    return led


def save_ledger(ledger: Dict[str, Any]) -> None:
    save_json(LEDGER_FILE, ledger)


def new_id(prefix: str, existing: Iterable[str]) -> str:
    """e.g. new_id('TKT', ids) -> 'TKT-20261030-003'."""
    stem = f"{prefix}-{now_et().strftime('%Y%m%d')}-"
    n = [int(x[len(stem):]) for x in existing if x.startswith(stem) and x[len(stem):].isdigit()]
    return f"{stem}{(max(n) + 1) if n else 1:03d}"


def trade_initial_risk(trade: Dict[str, Any]) -> float:
    """Dollars lost if the stop is hit (0 once the stop has moved past the entry)."""
    try:
        e, st, q = float(trade["entry_price"]), float(trade["stop"]), float(trade["qty"])
    except (KeyError, TypeError, ValueError) as err:
        raise StateError(f"ledger trade {trade.get('id', '?')} is missing entry_price/stop/qty") from err
    side = trade.get("side")
    if side not in ("long", "short"):
        raise StateError(f"ledger trade {trade.get('id', '?')} has side {side!r}; expected long or short")
    per_share = (e - st) if side == "long" else (st - e)
    return max(0.0, per_share) * q


def trade_pnl(trade: Dict[str, Any], price: float) -> float:
    q, e = float(trade["qty"]), float(trade["entry_price"])
    return (price - e) * q if trade["side"] == "long" else (e - price) * q


def _et_day(stamp: Optional[str]) -> Optional[str]:
    """ET calendar date of an ISO stamp (handles 'Z' and other offsets)."""
    if not stamp:
        return None
    text = str(stamp).strip()
    try:
        return parse_et(text[:-1] + "+00:00" if text.endswith("Z") else text).strftime("%Y-%m-%d")
    except ValueError:
        return text[:10]


def _on_day(stamp: Optional[str], day: str) -> bool:
    return _et_day(stamp) == day


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

def _floor_step(x: float, step: float) -> float:
    n = math.floor(x / step + 1e-9)
    if step >= 1:
        return float(int(n * step))
    decimals = max(0, -int(math.floor(math.log10(step))))
    return round(n * step, decimals)


def position_size(entry: float, stop: float, account: Optional[float] = None,
                  risk_pct: Optional[float] = None, risk_dollars: Optional[float] = None,
                  max_position_pct: Optional[float] = None,
                  settings: Optional[Dict[str, Any]] = None, step: Optional[float] = None,
                  fee: Optional[float] = None) -> Dict[str, Any]:
    """Size so that hitting the stop loses at most the risk budget, capped by max position size.

    `step` is the size increment (default: 1 share for stocks, 1e-8 for crypto). `fee` is the
    paper fee per fill in % (default: the active market's); fees for the entry and the stop
    exit count toward the loss, so a stopped-out trade still stays within budget.
    """
    s = settings or load_settings()
    account = float(account if account is not None else s["account_size"])
    if not math.isfinite(account) or account <= 0:
        raise ValueError("account size must be positive")
    if risk_dollars is None:
        risk_dollars = account * float(risk_pct if risk_pct is not None else s["risk_per_trade_pct"]) / 100.0
    if not math.isfinite(float(risk_dollars)) or risk_dollars <= 0:
        raise ValueError("risk per trade must be positive")
    if not (math.isfinite(float(entry)) and math.isfinite(float(stop))) or entry <= 0 or stop <= 0:
        raise ValueError("entry and stop must be positive prices")
    per_share = abs(float(entry) - float(stop))
    if per_share <= 0:
        raise ValueError("entry and stop must differ")
    step = float(step) if step else qty_step()
    if not math.isfinite(step) or step <= 0:
        raise ValueError("size step must be positive")
    fee_rate = (fee if fee is not None else fee_pct(settings=s)) / 100.0
    loss_per_share = per_share + fee_rate * (float(entry) + float(stop))
    shares = _floor_step(risk_dollars / loss_per_share, step)
    cap_pct = float(max_position_pct if max_position_pct is not None else s["max_position_pct"])
    if not math.isfinite(cap_pct) or cap_pct <= 0:
        raise ValueError("max position % must be positive")
    max_notional = account * cap_pct / 100.0
    capped_by = "risk"
    if shares * entry > max_notional + 1e-9:
        shares = _floor_step(max_notional / entry, step)
        capped_by = "max_position_pct"
    if step >= 1:
        shares = int(shares)
    return {
        "shares": shares,
        "risk_per_share": round(per_share, 4) if per_share >= 0.01 else per_share,
        "risk_budget": round(risk_dollars, 2),
        "risk_used": round(shares * per_share, 2),
        "fees_at_stop": round(shares * fee_rate * (float(entry) + float(stop)), 2),
        "notional": round(shares * entry, 2),
        "account": account,
        "max_notional": round(max_notional, 2),
        "capped_by": capped_by,
        "step": step,
        "fee_pct": round(fee_rate * 100, 4),
    }


# ---------------------------------------------------------------------------
# CLI and output helpers
# ---------------------------------------------------------------------------

class ArgumentParser(argparse.ArgumentParser):
    """argparse with usage errors on exit code 1 (exit 2 means "data source down" in this pack)."""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        sys.exit(1)


def make_parser(description: str) -> argparse.ArgumentParser:
    p = ArgumentParser(description=description)
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
    except (ValueError, StateError, OverflowError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:  # e.g. an unusable $DAYTRADER_HOME
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(code or 0)


def fmt_price(x: Optional[float]) -> str:
    if x is None:
        return "-"
    if abs(x) >= 1:
        return f"{x:,.2f}"
    if x == 0 or abs(x) >= 0.001:
        return f"{x:.4f}"
    return f"{x:.{min(12, 3 - int(math.floor(math.log10(abs(x)))))}f}"   # 0.00001234 -> 4 significant digits


def fmt_qty(q: Optional[float]) -> str:
    """8 -> '8'; 0.01234567 -> '0.01234567'; 1.5 -> '1.5'."""
    if q is None:
        return "-"
    if float(q).is_integer():
        return f"{int(q):,}"
    return f"{q:.8f}".rstrip("0").rstrip(".")


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
