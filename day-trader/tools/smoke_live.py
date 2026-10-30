#!/usr/bin/env python3
"""Live smoke test: one real request per data source, parsed by the skills' own code.

The unit tests in tests/ are offline, so every parser in this pack was developed against
synthetic payloads. Run this where the network is open (by hand, or on a schedule) to check
that the real hosts still answer and that the skills still understand what they send back:

    python3 tools/smoke_live.py                    # PASS/FAIL table, one row per check
    python3 tools/smoke_live.py --symbol NVDA      # the ticker for the per-symbol checks
    python3 tools/smoke_live.py --only yahoo-5m,sec-filings
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

Each check asserts that the parse produced sane data (bars at the right spacing, prices > 0,
recent timestamps, plausible counts), not just that the host answered. One failure never stops
the others. Exit code: 0 when every check that ran passed (a SKIP has nothing to test and does
not fail the run), 1 when any check failed or the arguments were bad.

SEC asks automated clients for a contact email: set DAYTRADER_CONTACT=you@example.com (or the
contact_email setting) if sec-filings fails with HTTP 403. $DAYTRADER_FIXTURES and
$DAYTRADER_NOW are ignored here: this test is about the live hosts and the real clock.
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
MAX_AGE_DAYS = 7          # newest bar / quote: covers a long weekend plus a holiday
MAX_NEWS_AGE_DAYS = 30    # newest headline
MAX_FILING_AGE_DAYS = 180  # newest SEC filing
MIN_SESSIONS_5M = 20      # Yahoo keeps ~60 days of 5-minute bars (about 40 sessions)
MIN_DAILY_BARS = 60       # the scanner's own minimum
MIN_SYMBOLS = 3000        # about 5,900 US common stocks
PTR_LOOKBACK_DAYS = 60
DETAIL_WIDTH = 90
YAHOO = "/".join(h.split(".")[0] for h in tc.YAHOO_HOSTS) + ".finance.yahoo.com"


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
        return f"{tc.et_date(ts)} {tc.et_hhmm(ts)} ET"
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
               now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or tc.now_et()
    if only and "house-ptr" in only and "house-index" not in only:
        only = [*only, "house-index"]          # the PTR check picks its filing from the index
    ctx: Dict[str, Any] = {"symbol": symbol, "now": now}
    rows = [run_one(c, ctx) for c in CHECKS if not only or c["name"] in only]
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in ("PASS", "FAIL", "SKIP")}
    return {"ok": counts["FAIL"] == 0, "symbol": symbol, "checked_at": now.isoformat(timespec="seconds"),
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
    p.add_argument("--only", metavar="CHECKS", help="comma-separated subset: " + ", ".join(CHECK_NAMES))
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    symbol = args.symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,9}", symbol):
        raise ValueError(f"--symbol: {args.symbol!r} is not a ticker")
    only = parse_only(args.only)
    with live_env() as ignored:
        res = run_checks(symbol, only)
    res["notes"] = [f"${k} is set and ignored here - this test always asks the live hosts and the real clock"
                    for k in ignored]
    tc.emit(res, args.json, render)
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    tc.run(main)
