#!/usr/bin/env python3
"""Live smoke test: one real request per data source, parsed by the skills' own code.

The unit tests in tests/ are offline, so every parser in this pack was developed against
synthetic payloads. Run this where the network is open (by hand, or on a schedule) to check
that the real hosts still answer and that the skills still understand what they send back:

    python3 tools/smoke_live.py                    # PASS/FAIL table, one row per check
    python3 tools/smoke_live.py --symbol NVDA      # the ticker for the per-symbol checks
    python3 tools/smoke_live.py --binance-symbol ETH-USDT  # the pair for the Binance checks
    python3 tools/smoke_live.py --crypto-symbol ETH-USD    # the pair for the Coinbase checks
    python3 tools/smoke_live.py --only yahoo-5m,binance-5m,sec-filings
    python3 tools/smoke_live.py --json

Checks (each through the same function the skills call; the download cache is bypassed):
  yahoo-5m        tc.fetch_bars(SYM, "5m", "60d")      time-machine, entry, exit, broker, alerts, analyst
  yahoo-1d        tc.fetch_bars(SYM, "1d", "6mo")      scanner, analyst
  yahoo-quote     tc.quote(SYM)                         broker, exit, risk, budget
  nasdaq-symbols  tc.us_symbols()                       scanner (universe scans)
  news-rss        news.fetch_rss(SYM)                   news (Yahoo RSS feed + parse_rss)
  news-search     news.fetch_search(SYM)                news (Yahoo search + parse_search)
  sec-filings     tc.sec_recent_filings(SYM)            news, analyst
  house-index     pelosi.load_index([this year])        pelosi
  house-ptr       pelosi.parse_ptr_pdf(newest PTR)      pelosi (skipped when no electronic PTR was
                                                        filed in the last 60 days)
  binance-5m      tc.fetch_bars(PAIR, "5m", "2d")       crypto (Binance, the default): entry, exit,
                                                        time-machine, broker, alerts, analyst (klines,
                                                        paged 1,000 a request)
  binance-1d      tc.fetch_bars(PAIR, "1d", "6mo")      crypto (Binance): scanner, analyst
  binance-ticker  tc.quote(PAIR)                        crypto (Binance): broker, exit, risk, budget
  binance-products tc.product_info / crypto_symbols     crypto (Binance): scanner universe (USDT pairs),
                                                        size step, price tick, minimum order
  coinbase-5m     tc.fetch_bars(PAIR, "5m", "2d")       crypto (Coinbase, when chosen): the same skills
                                                        (paged, 300 candles a request)
  coinbase-1d     tc.fetch_bars(PAIR, "1d", "6mo")      crypto (Coinbase): scanner, analyst
  coinbase-ticker tc.quote(PAIR)                        crypto (Coinbase): broker, exit, risk, budget
  coinbase-products tc.product_info / crypto_symbols    crypto (Coinbase): scanner universe, size step
                                                        and price tick (entry, exit, risk, broker)

Each binance-* check runs with crypto prices pinned to Binance and each coinbase-* check to
Coinbase, whatever the crypto_exchange setting says, through tc's own $DAYTRADER_CRYPTO_EXCHANGE
override - so both exchanges are checked through the same tc.fetch_bars / tc.quote /
tc.product_info dispatch the skills use.

Each check asserts that the parse produced sane data (bars at the right spacing, prices > 0,
recent timestamps, plausible counts), not just that the host answered. The crypto checks also
hold a 24/7 market to 24/7 standards: candles on the 5-minute grid with no paging gaps, UTC-day
daily candles, a last trade from the last few hours. One failure never stops the others. Exit
code: 0 when every check that ran passed (a SKIP has nothing to test and does not fail the run),
1 when any check failed or the arguments were bad.

SEC asks automated clients for a contact email: set DAYTRADER_CONTACT=you@example.com (or the
contact_email setting) if sec-filings fails with HTTP 403. HTTP 451 is Binance's answer where it
does not serve the location (api.binance.com gives it to US addresses; the pack reads the
market-data host data-api.binance.vision instead); if a binance-* check ever gets one, its row
says so and points the desk at Coinbase's prices. $DAYTRADER_FIXTURES and $DAYTRADER_NOW are
ignored here: this test is about the live hosts and the real clock.
"""
from __future__ import annotations

import importlib.util
import math
import os
import re
import statistics
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Iterator, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent          # day-trader/
SHARED = ROOT / "shared"
SKILLS = ROOT / "skills"

if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

import trader_common as tc  # noqa: E402  (the canonical copy; every skill's copy is identical)

DEFAULT_SYMBOL = "TSLA"
DEFAULT_BINANCE_SYMBOL = "BTC-USDT"
DEFAULT_CRYPTO_SYMBOL = "BTC-USD"     # the Coinbase pair
MAX_AGE_DAYS = 7          # newest bar / quote: covers a long weekend plus a holiday
MAX_NEWS_AGE_DAYS = 30    # newest headline
MAX_FILING_AGE_DAYS = 180  # newest SEC filing
MIN_SESSIONS_5M = 20      # Yahoo keeps ~60 days of 5-minute bars (about 40 sessions)
MIN_DAILY_BARS = 60       # the scanner's own minimum
MIN_SYMBOLS = 3000        # about 5,900 US common stocks
PTR_LOOKBACK_DAYS = 60
MAX_CRYPTO_AGE_HOURS = 3   # newest crypto candle / trade: the market never closes
CRYPTO_5M_RANGE = "2d"     # two UTC days of 5-minute candles = a few pages of 300 (Coinbase) or 1,000 (Binance)
MIN_CRYPTO_COVERAGE = 0.9  # share of the 5-minute slots between the first and last candle that are present
MIN_CRYPTO_PRODUCTS = 100  # Coinbase lists several hundred products
MIN_CRYPTO_USD_PAIRS = 50
MIN_BINANCE_USDT_PAIRS = 100   # Binance trades several hundred spot pairs against USDT
MAX_BINANCE_MIN_ORDER = 100.0  # a pair's NOTIONAL minimum, in USDT (BTCUSDT: 5)
DETAIL_WIDTH = 90
YAHOO = "/".join(h.split(".")[0] for h in tc.YAHOO_HOSTS) + ".finance.yahoo.com"
BINANCE = tc.CRYPTO_EXCHANGES["binance"]["host"]        # data-api.binance.vision
COINBASE = tc.COINBASE_API.split("://", 1)[1]           # api.exchange.coinbase.com
EXCHANGE_ENV = "DAYTRADER_CRYPTO_EXCHANGE"


class CheckFailed(Exception):
    """The host answered, but what the parser made of it is not sane."""


class Skipped(Exception):
    """Nothing to test this time (e.g. no recent electronic PTR in the index)."""


def expect(ok: Any, message: str) -> None:
    if not ok:
        raise CheckFailed(message)


# ---------------------------------------------------------------------------
# Skill modules, imported by path (their `import trader_common` resolves to `tc` above)
# ---------------------------------------------------------------------------

_MODULES: Dict[str, ModuleType] = {}


def load_skill(skill: str, module: str) -> ModuleType:
    """Import skills/<skill>/scripts/<module>.py by path, once."""
    key = f"{skill}/{module}"
    if key not in _MODULES:
        path = SKILLS / skill / "scripts" / f"{module}.py"
        spec = importlib.util.spec_from_file_location(f"day_trader_smoke_{skill.replace('-', '_')}_{module}", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MODULES[key] = mod
    return _MODULES[key]


# ---------------------------------------------------------------------------
# Sanity helpers
# ---------------------------------------------------------------------------

def _when(ts: float) -> str:
    try:
        return f"{tc.et_date(ts)} {tc.et_hhmm(ts)} {tc.tz_label()}"
    except (ValueError, OverflowError, OSError):  # e.g. milliseconds read as seconds
        return f"unix time {ts:.0f}"


def _age(ts: float, now: datetime) -> str:
    days = (now.timestamp() - ts) / 86400
    if days < 1:
        return f"{max(0.0, days * 24):.0f}h"
    return f"{days:.0f}d"


def check_recent(ts: Any, now: datetime, max_days: float, what: str) -> float:
    expect(isinstance(ts, (int, float)) and not isinstance(ts, bool) and math.isfinite(ts),
           f"{what} has no usable timestamp ({ts!r})")
    expect(ts <= now.timestamp() + 86400, f"{what} is dated {_when(ts)}, in the future - timestamps parsed "
                                          "in the wrong unit?")
    expect(now.timestamp() - ts <= max_days * 86400,
           f"{what} is from {_when(ts)}, older than {max_days:g} days - stale or frozen data?")
    return float(ts)


def check_bars(bars: Sequence[tc.Bar], now: datetime, min_bars: int) -> None:
    expect(bars, "no bars parsed")
    expect(len(bars) >= min_bars, f"only {len(bars)} bars parsed (expected at least {min_bars})")
    bad = [b for b in bars if not all(isinstance(x, (int, float)) and math.isfinite(x) and x > 0
                                      for x in (b.open, b.high, b.low, b.close))
           or not b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high]
    expect(not bad, f"{len(bad)} bar(s) with a non-positive or inconsistent OHLC, e.g. {bad[0] if bad else ''}")
    check_recent(bars[-1].ts, now, MAX_AGE_DAYS, "the last bar")


# ---------------------------------------------------------------------------
# The checks: each returns a one-line detail, raises CheckFailed / FetchError / Skipped
# ---------------------------------------------------------------------------

def check_yahoo_5m(ctx: Dict[str, Any]) -> str:
    sym, now = ctx["symbol"], ctx["now"]
    bars, meta = tc.fetch_bars(sym, "5m", "60d")
    check_bars(bars, now, MIN_SESSIONS_5M)
    sessions = tc.group_sessions(bars)
    expect(len(sessions) >= MIN_SESSIONS_5M,
           f"only {len(sessions)} regular sessions of 5-minute bars (expected at least {MIN_SESSIONS_5M})")
    steps = [b.ts - a.ts for s in sessions.values() for a, b in zip(s, s[1:])]
    step = statistics.median(steps) if steps else 0
    expect(step == 300, f"bars are not 5 minutes apart (median step {step:.0f} s)")
    days = list(sessions)
    last = bars[-1]
    return (f"{len(bars):,} bars, {len(days)} sessions {days[0]}..{days[-1]}, last {_when(last.ts)} "
            f"close {tc.fmt_price(last.close)}")


def check_yahoo_1d(ctx: Dict[str, Any]) -> str:
    sym, now = ctx["symbol"], ctx["now"]
    bars, meta = tc.fetch_bars(sym, "1d", "6mo")
    check_bars(bars, now, MIN_DAILY_BARS)
    steps = [b.ts - a.ts for a, b in zip(bars, bars[1:])]
    expect(statistics.median(steps) >= 20 * 3600, f"bars are not daily (median step {statistics.median(steps):.0f} s)")
    no_volume = sum(1 for b in bars if not b.volume > 0)
    expect(no_volume <= len(bars) // 10, f"{no_volume} of {len(bars)} daily bars have no volume")
    last = bars[-1]
    return (f"{len(bars)} daily bars {bars[0].date}..{last.date}, last close {tc.fmt_price(last.close)} "
            f"volume {tc.fmt_big(last.volume)}")


def check_yahoo_quote(ctx: Dict[str, Any]) -> str:
    q = tc.quote(ctx["symbol"])
    price = q.get("price")
    expect(isinstance(price, float) and math.isfinite(price) and price > 0, f"price is not a positive number ({price!r})")
    ts = check_recent(q.get("time"), ctx["now"], MAX_AGE_DAYS, "the quote")
    currency = f" {q['currency']}" if q.get("currency") else ""
    name = f" ({q['name']})" if q.get("name") else ""
    return f"{q['symbol']} {tc.fmt_price(price)}{currency} at {_when(ts)}{name}"


def check_nasdaq_symbols(ctx: Dict[str, Any]) -> str:
    rows = tc.us_symbols(ttl=0)
    expect(len(rows) >= MIN_SYMBOLS, f"only {len(rows):,} common stocks parsed (expected at least {MIN_SYMBOLS:,})")
    symbols = {r["symbol"] for r in rows}
    missing = [s for s in ("AAPL", "MSFT", "JPM") if s not in symbols]
    expect(not missing, f"well-known symbols missing from the list: {', '.join(missing)}")
    by_exchange: Dict[str, int] = {}
    for r in rows:
        by_exchange[r["exchange"]] = by_exchange.get(r["exchange"], 0) + 1
    expect(by_exchange.get("NASDAQ") and by_exchange.get("NYSE"),
           f"expected both NASDAQ and NYSE listings, got {sorted(by_exchange)}")
    top = sorted(by_exchange.items(), key=lambda kv: -kv[1])[:3]
    return f"{len(rows):,} common stocks (" + ", ".join(f"{k} {v:,}" for k, v in top) + ")"


def _check_headlines(items: Sequence[Dict[str, Any]], now: datetime, source: str) -> str:
    expect(items, f"no headlines parsed from {source}")
    expect(all(it.get("title") for it in items), "a headline without a title")
    expect(any(str(it.get("link") or "").startswith("http") for it in items), "no headline has a link")
    dated = [it for it in items if it.get("ts") is not None]
    expect(dated, "no headline has a readable publish time")
    newest = max(dated, key=lambda it: it["ts"])
    check_recent(newest["ts"], now, MAX_NEWS_AGE_DAYS, "the newest headline")
    title = newest["title"] if len(newest["title"]) <= 50 else newest["title"][:47] + "..."
    return f"{len(items)} headlines ({len(dated)} dated), newest {_age(newest['ts'], now)} ago: {title}"


def check_news_rss(ctx: Dict[str, Any]) -> str:
    news = load_skill("news", "news")
    return _check_headlines(news.fetch_rss(ctx["symbol"]), ctx["now"], "the RSS feed")


def check_news_search(ctx: Dict[str, Any]) -> str:
    news = load_skill("news", "news")
    return _check_headlines(news.fetch_search(ctx["symbol"]), ctx["now"], "Yahoo search")


def check_sec_filings(ctx: Dict[str, Any]) -> str:
    filings = tc.sec_recent_filings(ctx["symbol"], limit=20)
    expect(filings, f"no filings parsed for {ctx['symbol']}")
    for f in filings:
        expect(f.get("form"), f"a filing without a form type: {f}")
        expect(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(f.get("filed") or "")), f"unreadable filing date: {f}")
        expect(re.fullmatch(r"\d{10}-\d{2}-\d{6}", str(f.get("accession") or "")), f"unexpected accession: {f}")
    filed = [f["filed"] for f in filings]
    expect(filed == sorted(filed, reverse=True), "filings are not newest first")
    newest = filings[0]
    expect(newest.get("accepted"), "the newest filing has no acceptance time (the news skill dates 8-Ks by it)")
    ts = tc.parse_et(newest["filed"] + "T12:00").timestamp()
    check_recent(ts, ctx["now"], MAX_FILING_AGE_DAYS, "the newest filing")
    eight_k = sum(1 for f in filings if str(f["form"]).startswith(("8-K", "6-K")))
    return f"{len(filings)} recent filings ({eight_k} 8-K/6-K), newest {newest['form']} filed {newest['filed']}"


def check_house_index(ctx: Dict[str, Any]) -> str:
    pelosi = load_skill("pelosi", "pelosi")
    today: date = ctx["now"].date()
    rows, warnings = pelosi.load_index([today.year], today)
    expect(rows, f"the {today.year} House index has no filings")
    dated = [r for r in rows if r.get("filed")]
    expect(dated, "no filing in the index has a readable filing date")
    late = [r for r in dated if r["filed"] > (today + timedelta(days=1)).isoformat()]
    expect(not late, f"{len(late)} filing(s) dated after today, e.g. {late[0]['filed'] if late else ''}")
    ptrs = [r for r in dated if r.get("filing_type") == "P"]
    expect(ptrs or today.timetuple().tm_yday <= 14,
           f"no Periodic Transaction Reports (FilingType P) among {len(rows):,} filings")
    ctx["house_rows"] = rows
    newest = f", newest filed {max(r['filed'] for r in ptrs)}" if ptrs else ""
    return f"{len(rows):,} filings in the {today.year} index, {len(ptrs):,} PTRs{newest}"


def check_house_ptr(ctx: Dict[str, Any]) -> str:
    rows = ctx.get("house_rows")
    if rows is None:
        raise Skipped("needs the house-index check to pass first")
    pelosi = load_skill("pelosi", "pelosi")
    today: date = ctx["now"].date()
    since = (today - timedelta(days=PTR_LOOKBACK_DAYS)).isoformat()
    electronic = [f for f in pelosi.ptr_filings(rows, since, today.isoformat()) if not f["paper_likely"]]
    if not electronic:
        raise Skipped(f"no electronic PTR filed since {since} - nothing to parse")
    f = electronic[0]
    parsed = pelosi.parse_ptr_pdf(pelosi._get(tc.http_get, f["url"], timeout=30))
    expect(parsed.get("status") == "parsed" and parsed.get("trades"),
           f"{f['url']} came back {parsed.get('status')} "
           f"({parsed.get('error') or 'no trades found in ' + str(parsed.get('text_chars')) + ' characters of text'})")
    trades = parsed["trades"]
    for t in trades:
        expect(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(t.get("transaction_date") or "")),
               f"a trade without a readable transaction date: {t}")
        expect(t.get("type") in pelosi.ACTIONS, f"a trade with an unknown type: {t}")
        expect(t.get("amount"), f"a trade without an amount range: {t}")
    t = trades[0]
    what = t.get("ticker") or (t.get("asset") or "?")[:20]
    return (f"DocID {f['doc_id']} ({f['member']}, filed {f['filed']}): {len(trades)} trade(s) via "
            f"{parsed.get('engine')}, e.g. {what} {pelosi.ACTIONS[t['type']]} {t['transaction_date']} {t['amount']}")


# ---------------------------------------------------------------------------
# Crypto: Binance (the default) and Coinbase Exchange. Each check runs on the crypto market's clock
# (UTC days), with crypto prices pinned to its own exchange.
# ---------------------------------------------------------------------------

@contextmanager
def exchange_scope(exchange: str) -> Iterator[None]:
    """Crypto prices from `exchange` inside the block, whatever the crypto_exchange setting says. It
    uses tc's own override ($DAYTRADER_CRYPTO_EXCHANGE, restored afterwards), so the checks go through
    the same tc.fetch_bars / tc.quote / tc.product_info / tc.crypto_symbols dispatch as the skills."""
    old = os.environ.get(EXCHANGE_ENV)
    os.environ[EXCHANGE_ENV] = exchange
    try:
        yield
    except tc.FetchError as e:
        if exchange == "binance" and "HTTP 451" in str(e):   # Binance's answer to a location it does not serve
            raise CheckFailed(f"{e} - Binance does not serve this location; the desk can use Coinbase's prices "
                              "instead: python3 skills/risk/scripts/risk.py config --crypto-exchange coinbase") from e
        raise
    finally:
        if old is None:
            os.environ.pop(EXCHANGE_ENV, None)
        else:
            os.environ[EXCHANGE_ENV] = old


def _fresh(ts: float, now: datetime, what: str) -> None:
    hours = (now.timestamp() - ts) / 3600
    expect(hours <= MAX_CRYPTO_AGE_HOURS, f"{what} is from {_when(ts)}, {hours:.1f} h old - a 24/7 market should "
                                          f"have one from the last {MAX_CRYPTO_AGE_HOURS} hours (stale or frozen data?)")


def _from_exchange(meta: Dict[str, Any], exchange: str, what: str) -> None:
    """The response came from the exchange the check pinned (the dispatch honoured the override)."""
    name = tc.CRYPTO_EXCHANGES[exchange]["name"]
    got = str(meta.get("exchangeName") or meta.get("exchange") or "")
    expect(got.startswith(name), f"the {what} came from {got or 'an unnamed source'!r}, not {name} - "
                                 "is the crypto_exchange override honoured?")


def _check_crypto_5m(ctx: Dict[str, Any], exchange: str, sym: str) -> str:
    now = ctx["now"]
    with exchange_scope(exchange), tc.market_scope(sym):
        bars, meta = tc.fetch_bars(sym, "5m", CRYPTO_5M_RANGE)
        _from_exchange(meta, exchange, "candles")
        check_bars(bars, now, 100)
        _fresh(bars[-1].ts, now, "the last candle")
        off_grid = [b for b in bars if b.ts % 300]
        expect(not off_grid, f"{len(off_grid)} candle(s) off the 5-minute grid, e.g. {_when(off_grid[0].ts) if off_grid else ''}")
        steps = [b.ts - a.ts for a, b in zip(bars, bars[1:])]
        expect(steps and statistics.median(steps) == 300,
               f"candles are not 5 minutes apart (median step {statistics.median(steps) if steps else 0:.0f} s)")
        span = (now.timestamp() - tc.range_seconds(CRYPTO_5M_RANGE), now.timestamp())
        expect(bars[0].ts <= span[0] + 2 * 3600, f"the candles start at {_when(bars[0].ts)}, not {CRYPTO_5M_RANGE} back "
                                                 "- were the start/end pages honoured?")
        slots = (bars[-1].ts - bars[0].ts) // 300 + 1
        expect(len(bars) >= MIN_CRYPTO_COVERAGE * slots,
               f"only {len(bars)} of {slots} five-minute candles between the first and the last - missing pages?")
        ctx[f"{exchange}_last"] = bars[-1]
        days = tc.group_sessions(bars)
        last = bars[-1]
        return (f"{len(bars):,} candles over {len(days)} UTC days {min(days)}..{max(days)}, last {_when(last.ts)} "
                f"close {tc.fmt_price(last.close)}")


def _check_crypto_1d(ctx: Dict[str, Any], exchange: str, sym: str) -> str:
    now = ctx["now"]
    with exchange_scope(exchange), tc.market_scope(sym):
        bars, meta = tc.fetch_bars(sym, "1d", "6mo")
        _from_exchange(meta, exchange, "daily candles")
        check_bars(bars, now, MIN_DAILY_BARS)
        expect(now.timestamp() - bars[-1].ts <= 2 * 86400,
               f"the last daily candle is from {bars[-1].date}, not today or yesterday (UTC)")
        off_day = [b for b in bars if b.ts % 86400]
        expect(not off_day, f"{len(off_day)} daily candle(s) do not start at 00:00 UTC, e.g. {_when(off_day[0].ts) if off_day else ''}"
                            " - the pack treats a crypto session as a UTC day")
        steps = [b.ts - a.ts for a, b in zip(bars, bars[1:])]
        expect(statistics.median(steps) == 86400, f"candles are not daily (median step {statistics.median(steps):.0f} s)")
        no_volume = sum(1 for b in bars if not b.volume > 0)
        expect(no_volume <= len(bars) // 10, f"{no_volume} of {len(bars)} daily candles have no volume")
        last = bars[-1]
        return (f"{len(bars)} daily candles {bars[0].date}..{last.date} (UTC days), last close "
                f"{tc.fmt_price(last.close)} volume {tc.fmt_big(last.volume)}")


def _check_crypto_ticker(ctx: Dict[str, Any], exchange: str, sym: str) -> str:
    now = ctx["now"]
    with exchange_scope(exchange), tc.market_scope(sym):
        q = tc.quote(sym)
        _from_exchange(q, exchange, "ticker")
        price = q.get("price")
        expect(isinstance(price, float) and math.isfinite(price) and price > 0, f"price is not a positive number ({price!r})")
        ts = check_recent(q.get("time"), now, MAX_AGE_DAYS, "the last trade")
        _fresh(ts, now, "the last trade")
        bid, ask = q.get("bid"), q.get("ask")
        expect(isinstance(bid, float) and isinstance(ask, float) and 0 < bid <= ask,
               f"no usable bid/ask ({bid!r} / {ask!r})")
        expect((ask - bid) / price <= 0.01, f"bid {tc.fmt_price(bid)} / ask {tc.fmt_price(ask)} is over 1% wide")
        expect(bid * 0.99 <= price <= ask * 1.01,
               f"the last trade {tc.fmt_price(price)} is far outside the bid/ask {tc.fmt_price(bid)} / {tc.fmt_price(ask)}")
        last = ctx.get(f"{exchange}_last")
        if last is not None:                      # the candles and the ticker must describe the same market
            gap = abs(price / last.close - 1) * 100
            expect(gap <= 5, f"the ticker {tc.fmt_price(price)} is {gap:.1f}% away from the last 5-minute close "
                             f"{tc.fmt_price(last.close)} - candle columns parsed in the wrong order?")
        prev = q.get("previous_close")
        day = f" · 24h open {tc.fmt_price(prev)}" if prev else " · no 24h stats (the change vs 24 h ago is unavailable)"
        currency = f" {q['currency']}" if q.get("currency") else ""
        return (f"{q['symbol']} {tc.fmt_price(price)}{currency} at {_when(ts)} · bid {tc.fmt_price(bid)} "
                f"/ ask {tc.fmt_price(ask)}{day}")


def check_binance_5m(ctx: Dict[str, Any]) -> str:
    return _check_crypto_5m(ctx, "binance", ctx["binance_symbol"])


def check_binance_1d(ctx: Dict[str, Any]) -> str:
    return _check_crypto_1d(ctx, "binance", ctx["binance_symbol"])


def check_binance_ticker(ctx: Dict[str, Any]) -> str:
    return _check_crypto_ticker(ctx, "binance", ctx["binance_symbol"])


BINANCE_UNIVERSE_URL = f"{tc.BINANCE_API}/exchangeInfo?permissions=SPOT&symbolStatus=TRADING"
LEVERAGED_SUFFIX = re.compile(r"(.+?)(UP|DOWN|BULL|BEAR)")


@contextmanager
def _one_response(url: str, payload: Any) -> Iterator[None]:
    """Serve `payload` for `url` through tc.get_json (any other URL goes to the network as usual), so the
    skills' own code parses the one live response this check already has instead of asking again."""
    saved = tc.get_json
    tc.get_json = lambda u, *a, **k: payload if u == url else saved(u, *a, **k)  # type: ignore[assignment]
    try:
        yield
    finally:
        tc.get_json = saved  # type: ignore[assignment]


def check_binance_products(ctx: Dict[str, Any]) -> str:
    sym = ctx["binance_symbol"]
    pair = tc.binance_symbol(sym)
    base, quote_ccy = tc.crypto_pair(pair)
    with exchange_scope("binance"):
        raw = tc.get_json(BINANCE_UNIVERSE_URL, 0)            # every trading spot pair, with its filters
        rows = raw.get("symbols") if isinstance(raw, dict) else None
        expect(isinstance(rows, list) and rows, "exchangeInfo listed no symbols")
        with _one_response(BINANCE_UNIVERSE_URL, raw):
            usdt = tc.crypto_symbols("USD")                   # the scanner's universe: Binance's USDT pairs
            info = tc.product_info(sym)                       # exchangeInfo?symbol=PAIR: the pair's own rules
    # The pair's own rules (exchangeInfo?symbol=PAIR), which entry, exit, risk and the broker size and price with.
    expect(info.get("symbol") == pair, f"product_info answered for {info.get('symbol')!r}, not {pair}")
    expect(str(info.get("status") or "").lower() == "online" and not info["trading_disabled"],
           f"{pair} is {info.get('status')}{' (trading disabled)' if info['trading_disabled'] else ''}")
    step, tick = info["base_increment"], info["quote_increment"]
    expect(0 < step <= 1 and 0 < tick <= 1, f"implausible size step {step!r} / price tick {tick!r} for {pair}")
    try:
        min_order = float(info.get("min_market_funds"))
    except (TypeError, ValueError):
        min_order = float("nan")
    expect(math.isfinite(min_order) and 0 < min_order <= MAX_BINANCE_MIN_ORDER,
           f"no plausible minimum order for {pair} (NOTIONAL filter: {info.get('min_market_funds')!r})")
    rules = f"{pair} trading, size step {tc.fmt_qty(step)}, tick {tick:g}, minimum order {min_order:g} {quote_ccy}"
    # The scanner's universe: every trading USDT pair but stablecoins and leveraged tokens (BTCUP = BTC + UP).
    # Any other trading USDT pair missing from it is a coin the scanner can never find.
    expect(len(usdt) >= MIN_BINANCE_USDT_PAIRS,
           f"only {len(usdt)} trading USDT pairs (expected at least {MIN_BINANCE_USDT_PAIRS}) - status fields renamed?")
    stable = [r["symbol"] for r in usdt if r["name"] in tc.STABLECOINS]
    expect(not stable, f"stablecoins in the scanner universe: {', '.join(stable)}")
    if quote_ccy == "USDT":
        expect(f"{base}-USDT" in {r["symbol"] for r in usdt}, f"{pair} is not among the trading USDT pairs")
    listed = {str(r.get("baseAsset", "")).upper() for r in rows if isinstance(r, dict)}
    kept = {r["name"] for r in usdt}
    missing = []
    for r in rows:
        b = str(r.get("baseAsset", "")).upper() if isinstance(r, dict) else ""
        if (not b or b in kept or str(r.get("quoteAsset", "")).upper() != "USDT" or b in tc.STABLECOINS
                or str(r.get("status", "")).upper() != "TRADING" or r.get("isSpotTradingAllowed") is False):
            continue
        lev = LEVERAGED_SUFFIX.fullmatch(b)
        if not (lev and lev.group(1) in listed):
            missing.append(b)
    problems = []
    if missing:
        problems.append(f"{len(missing)} trading USDT pair(s) missing from the scanner universe of {len(usdt):,}: "
                        f"{', '.join(sorted(missing)[:8])} - dropped as leveraged tokens? They are not a listed "
                        "coin plus UP/DOWN/BULL/BEAR")
    # Every universe pair must be a symbol tc reads as crypto: tc.fetch_bars takes anything else for a
    # stock ticker (and asks Yahoo), so the scanner skips such pairs and the desk cannot price them.
    unreadable = sorted(r["symbol"] for r in usdt if not tc.is_crypto_symbol(r["symbol"]))
    if unreadable:
        problems.append(f"{len(unreadable)} pair(s) in the scanner universe that tc.is_crypto_symbol cannot read "
                        f"(coin names its pair format does not read; the scanner skips them): {', '.join(unreadable[:8])}")
    expect(not problems, "; ".join(problems) + f" (the pair rules parsed fine: {rules})")
    return f"{len(usdt):,} trading USDT pairs (stablecoins left out); {rules}"


def check_coinbase_5m(ctx: Dict[str, Any]) -> str:
    return _check_crypto_5m(ctx, "coinbase", ctx["crypto_symbol"])


def check_coinbase_1d(ctx: Dict[str, Any]) -> str:
    return _check_crypto_1d(ctx, "coinbase", ctx["crypto_symbol"])


def check_coinbase_ticker(ctx: Dict[str, Any]) -> str:
    return _check_crypto_ticker(ctx, "coinbase", ctx["crypto_symbol"])


@contextmanager
def _one_product_list(rows: List[Dict[str, Any]]) -> Iterator[None]:
    """Serve tc.coinbase_products from `rows`, so the skills' own product_info and crypto_symbols
    parse the one live response instead of asking for the same list twice more (no cache here)."""
    saved = tc.coinbase_products
    tc.coinbase_products = lambda *a, **k: rows  # type: ignore[assignment]
    try:
        yield
    finally:
        tc.coinbase_products = saved  # type: ignore[assignment]


def check_coinbase_products(ctx: Dict[str, Any]) -> str:
    sym = ctx["crypto_symbol"]
    rows = tc.coinbase_products(ttl=0)
    expect(len(rows) >= MIN_CRYPTO_PRODUCTS, f"only {len(rows)} products listed (expected at least {MIN_CRYPTO_PRODUCTS})")
    with exchange_scope("coinbase"), _one_product_list(rows):
        usd = tc.crypto_symbols("USD")
        info = tc.product_info(sym)
    expect(len(usd) >= MIN_CRYPTO_USD_PAIRS,
           f"only {len(usd)} tradable USD pairs (expected at least {MIN_CRYPTO_USD_PAIRS}) - status fields renamed?")
    stable = [r["symbol"] for r in usd if r["name"] in tc.STABLECOINS]
    expect(not stable, f"stablecoins in the scanner universe: {', '.join(stable)}")
    if sym.endswith("-USD"):
        expect(sym in {r["symbol"] for r in usd}, f"{sym} is not among the tradable USD pairs")
    expect(str(info.get("status") or "").lower() == "online" and not info["trading_disabled"],
           f"{sym} is {info.get('status')}{' (trading disabled)' if info['trading_disabled'] else ''}")
    step, tick = info["base_increment"], info["quote_increment"]
    expect(0 < step <= 1 and 0 < tick <= 1, f"implausible size step {step!r} / price tick {tick!r} for {sym}")
    return (f"{len(rows):,} products, {len(usd):,} tradable USD pairs (stablecoins left out); {sym} online, "
            f"size step {tc.fmt_qty(step)}, tick {tick:g}")


CHECKS: List[Dict[str, Any]] = [
    {"name": "yahoo-5m", "host": YAHOO, "used_by": "time-machine, entry, exit, broker, alerts, analyst",
     "run": check_yahoo_5m},
    {"name": "yahoo-1d", "host": YAHOO, "used_by": "scanner, analyst", "run": check_yahoo_1d},
    {"name": "yahoo-quote", "host": YAHOO, "used_by": "broker, exit, risk, budget", "run": check_yahoo_quote},
    {"name": "nasdaq-symbols", "host": "www.nasdaqtrader.com", "used_by": "scanner (universe scans)",
     "run": check_nasdaq_symbols},
    {"name": "news-rss", "host": "feeds.finance.yahoo.com", "used_by": "news", "run": check_news_rss},
    {"name": "news-search", "host": YAHOO, "used_by": "news", "run": check_news_search},
    {"name": "sec-filings", "host": "www.sec.gov, data.sec.gov", "used_by": "news, analyst",
     "run": check_sec_filings},
    {"name": "house-index", "host": "disclosures-clerk.house.gov", "used_by": "pelosi", "run": check_house_index},
    {"name": "house-ptr", "host": "disclosures-clerk.house.gov", "used_by": "pelosi", "run": check_house_ptr},
    {"name": "binance-5m", "host": BINANCE,
     "used_by": "crypto (default): entry, exit, time-machine, broker, alerts, analyst", "run": check_binance_5m},
    {"name": "binance-1d", "host": BINANCE, "used_by": "crypto (default): scanner, analyst", "run": check_binance_1d},
    {"name": "binance-ticker", "host": BINANCE, "used_by": "crypto (default): broker, exit, risk, budget",
     "run": check_binance_ticker},
    {"name": "binance-products", "host": BINANCE, "used_by": "crypto (default): scanner, entry, exit, risk, broker",
     "run": check_binance_products},
    {"name": "coinbase-5m", "host": COINBASE,
     "used_by": "crypto (coinbase): entry, exit, time-machine, broker, alerts, analyst", "run": check_coinbase_5m},
    {"name": "coinbase-1d", "host": COINBASE, "used_by": "crypto (coinbase): scanner, analyst", "run": check_coinbase_1d},
    {"name": "coinbase-ticker", "host": COINBASE, "used_by": "crypto (coinbase): broker, exit, risk, budget",
     "run": check_coinbase_ticker},
    {"name": "coinbase-products", "host": COINBASE, "used_by": "crypto (coinbase): scanner, entry, exit, risk, broker",
     "run": check_coinbase_products},
]
CHECK_NAMES = [c["name"] for c in CHECKS]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_one(check: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Run one check; whatever goes wrong becomes its FAIL row (never an exception)."""
    started = time.monotonic()
    try:
        status, detail = "PASS", check["run"](ctx)
    except Skipped as e:
        status, detail = "SKIP", str(e)
    except (CheckFailed, tc.FetchError) as e:
        status, detail = "FAIL", str(e)
    except Exception as e:  # a parser crash on a real payload is exactly what this test is for
        status, detail = "FAIL", f"{type(e).__name__}: {e}"
    return {"name": check["name"], "host": check["host"], "used_by": check["used_by"], "status": status,
            "seconds": round(time.monotonic() - started, 2), "detail": detail}


def run_checks(symbol: str = DEFAULT_SYMBOL, only: Optional[Sequence[str]] = None,
               now: Optional[datetime] = None, crypto_symbol: str = DEFAULT_CRYPTO_SYMBOL,
               binance_symbol: str = DEFAULT_BINANCE_SYMBOL) -> Dict[str, Any]:
    now = now or tc.now_et()
    if only and "house-ptr" in only and "house-index" not in only:
        only = [*only, "house-index"]          # the PTR check picks its filing from the index
    ctx: Dict[str, Any] = {"symbol": symbol, "crypto_symbol": crypto_symbol, "binance_symbol": binance_symbol,
                           "now": now}
    rows = [run_one(c, ctx) for c in CHECKS if not only or c["name"] in only]
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in ("PASS", "FAIL", "SKIP")}
    return {"ok": counts["FAIL"] == 0, "symbol": symbol, "crypto_symbol": crypto_symbol,
            "binance_symbol": binance_symbol, "checked_at": now.isoformat(timespec="seconds"),
            "passed": counts["PASS"], "failed": counts["FAIL"], "skipped": counts["SKIP"], "checks": rows,
            "notes": []}


def _cut(s: str, n: int = DETAIL_WIDTH) -> str:
    return s if len(s) <= n else s[:n - 3] + "..."


def render(res: Dict[str, Any]) -> str:
    out = [f"LIVE SMOKE TEST · {res['symbol']} · {res['checked_at'][:16].replace('T', ' ')} ET · "
           "one live request per data source, no cache"]
    out += [f"note: {n}" for n in res["notes"]]
    out += ["", tc.table([[r["name"], r["host"], r["status"], f"{r['seconds']:.1f}s", _cut(r["detail"])]
                          for r in res["checks"]], ["check", "host", "result", "time", "detail"])]
    trouble = [r for r in res["checks"] if r["status"] != "PASS" and len(r["detail"]) > DETAIL_WIDTH]
    if trouble:
        out += ["", "In full:"] + [f"  {r['status']} {r['name']} ({r['host']}): {r['detail']}" for r in trouble]
    blocked = list(dict.fromkeys(r["host"] for r in res["checks"] if r["status"] == "FAIL"
                                 and ("blocked by proxy" in r["detail"] or "allow-listed" in r["detail"])))
    out.append("")
    if blocked:
        out.append(f"Blocked: {'; '.join(blocked)} - allow outbound HTTPS to these hosts (README: Network "
                   "access), then run this again.")
    n = len(res["checks"])
    skipped = f", {res['skipped']} skipped" if res["skipped"] else ""
    if res["ok"]:
        out.append(f"OK: {res['passed']} of {n} checks passed{skipped} - the parsers understand today's live data.")
    else:
        out.append(f"FAILED: {res['failed']} of {n} checks failed{skipped} ({res['passed']} passed).")
    return "\n".join(out)


@contextmanager
def live_env() -> Iterator[List[str]]:
    """No download cache, no offline fixtures, no replay clock - restored afterwards."""
    names = ("DAYTRADER_NO_CACHE", "DAYTRADER_FIXTURES", "DAYTRADER_NOW")
    saved = {k: os.environ.get(k) for k in names}
    ignored = [k for k in names[1:] if os.environ.get(k)]
    os.environ["DAYTRADER_NO_CACHE"] = "1"
    for k in names[1:]:
        os.environ.pop(k, None)
    try:
        yield ignored
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def parse_only(value: Optional[str]) -> Optional[List[str]]:
    if value is None:
        return None
    names = [x.strip() for x in value.split(",") if x.strip()]
    unknown = [x for x in names if x not in CHECK_NAMES]
    if unknown or not names:
        raise ValueError(f"--only: unknown check {', '.join(unknown) or '(none given)'}; "
                         f"choose from {', '.join(CHECK_NAMES)}")
    return names


def build_parser():
    p = tc.make_parser("Live smoke test: one real request per data source, parsed by the skills' own code. "
                       "Exit 0 = every check that ran passed, 1 = a check failed.")
    p.add_argument("--symbol", default=DEFAULT_SYMBOL, help=f"ticker for the per-symbol checks (default {DEFAULT_SYMBOL})")
    p.add_argument("--binance-symbol", default=DEFAULT_BINANCE_SYMBOL, metavar="PAIR",
                   help=f"Binance pair for the binance-* checks (default {DEFAULT_BINANCE_SYMBOL})")
    p.add_argument("--crypto-symbol", default=DEFAULT_CRYPTO_SYMBOL, metavar="PAIR",
                   help=f"Coinbase pair for the coinbase-* checks (default {DEFAULT_CRYPTO_SYMBOL})")
    p.add_argument("--only", metavar="CHECKS", help="comma-separated subset: " + ", ".join(CHECK_NAMES))
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    symbol = args.symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,9}", symbol):
        raise ValueError(f"--symbol: {args.symbol!r} is not a ticker")
    if tc.is_crypto_symbol(symbol):
        raise ValueError(f"--symbol: {symbol} is a crypto pair - the Yahoo checks need a stock ticker; pass "
                         f"crypto pairs with --binance-symbol (Binance checks) or --crypto-symbol (Coinbase checks)")
    binance = args.binance_symbol.strip().upper()
    if not tc.is_crypto_symbol(binance):
        raise ValueError(f"--binance-symbol: {args.binance_symbol!r} is not a Binance pair (e.g. BTC-USDT)")
    crypto = args.crypto_symbol.strip().upper()
    if not tc.is_crypto_symbol(crypto):
        raise ValueError(f"--crypto-symbol: {args.crypto_symbol!r} is not a Coinbase pair (e.g. BTC-USD)")
    only = parse_only(args.only)
    with live_env() as ignored:
        res = run_checks(symbol, only, crypto_symbol=crypto, binance_symbol=binance)
    res["notes"] = [f"${k} is set and ignored here - this test always asks the live hosts and the real clock"
                    for k in ignored]
    tc.emit(res, args.json, render)
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    tc.run(main)
