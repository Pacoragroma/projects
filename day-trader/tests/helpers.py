"""Shared test utilities. Tests run offline: no test may touch the network.

Run everything from day-trader/:  python3 -m unittest discover -s tests
"""
from __future__ import annotations

import importlib
import json
import math
import os
import random
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent          # day-trader/
SHARED = ROOT / "shared"
SKILLS = ROOT / "skills"

if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

import trader_common as tc  # noqa: E402  (the canonical copy; skill copies are byte-identical)


def load_skill_module(skill: str, module: str):
    """Import skills/<skill>/scripts/<module>.py (its `import trader_common` resolves to `tc`)."""
    scripts = str(SKILLS / skill / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return importlib.import_module(module)


def skill_script(skill: str, script: str) -> Path:
    return SKILLS / skill / "scripts" / script


# ---------------------------------------------------------------------------
# Isolated state
# ---------------------------------------------------------------------------

@contextmanager
def temp_home(now: Optional[str] = None) -> Iterator[Path]:
    """Point $DAYTRADER_HOME at a fresh temp dir (and optionally pin $DAYTRADER_NOW, ET ISO)."""
    with tempfile.TemporaryDirectory() as d:
        env = {"DAYTRADER_HOME": d, "DAYTRADER_NO_CACHE": "1"}
        if now:
            env["DAYTRADER_NOW"] = now
        with mock.patch.dict(os.environ, env):
            if not now:
                os.environ.pop("DAYTRADER_NOW", None)
            # A developer's own demo/SEC settings must not leak into tests (patch.dict restores them).
            for var in ("DAYTRADER_FIXTURES", "DAYTRADER_CONTACT"):
                os.environ.pop(var, None)
            yield Path(d)


class TempHomeTestCase(unittest.TestCase):
    """Each test gets its own $DAYTRADER_HOME and a guard that fails on real network use."""

    NOW: Optional[str] = None  # e.g. "2026-10-30T10:15"

    def setUp(self) -> None:
        super().setUp()
        cm = temp_home(self.NOW)
        self.home = cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)
        guard = mock.patch("urllib.request.urlopen", side_effect=AssertionError("tests must not use the network"))
        guard.start()
        self.addCleanup(guard.stop)


# ---------------------------------------------------------------------------
# Synthetic market data
# ---------------------------------------------------------------------------

def et_ts(day: str, hhmm: str) -> int:
    """'2026-10-30', '09:30' -> unix seconds for that ET wall-clock time."""
    return int(tc.parse_et(f"{day}T{hhmm}").timestamp())


def trading_days(end: str, n: int) -> List[str]:
    """The n weekdays ending at `end` (inclusive), oldest first."""
    d = datetime.strptime(end, "%Y-%m-%d")
    out: List[str] = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    return list(reversed(out))


def bars_from_closes(day: str, closes: Sequence[float], start: str = "09:30", minutes: int = 5,
                     volume: float = 100_000, wick: float = 0.0) -> List[tc.Bar]:
    """Intraday bars where each bar opens at the previous close; `wick` adds symmetric high/low."""
    t0 = et_ts(day, start)
    bars: List[tc.Bar] = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        bars.append(tc.Bar(t0 + i * minutes * 60, o, max(o, c) + wick, min(o, c) - wick, c, volume))
        prev = c
    return bars


def random_walk_session(day: str, start_price: float, n_bars: int = 78, vol: float = 0.002,
                        drift: float = 0.0, seed: int = 0, volume: float = 100_000) -> List[tc.Bar]:
    """One regular session of 5-minute bars (78 bars = 09:30..15:55) as a seeded random walk."""
    rng = random.Random(seed)
    t0 = et_ts(day, "09:30")
    bars: List[tc.Bar] = []
    p = start_price
    for i in range(n_bars):
        o = p
        c = o * math.exp(rng.gauss(drift, vol))
        h = max(o, c) * (1 + abs(rng.gauss(0, vol / 2)))
        l = min(o, c) * (1 - abs(rng.gauss(0, vol / 2)))
        bars.append(tc.Bar(t0 + i * 300, round(o, 4), round(h, 4), round(l, 4), round(c, 4),
                           volume * (1 + rng.random())))
        p = c
    return bars


def random_walk_history(end_day: str, sessions: int, start_price: float = 100.0, vol: float = 0.002,
                        seed: int = 0, n_bars: int = 78) -> List[tc.Bar]:
    """`sessions` consecutive weekday sessions of 5-minute bars ending on `end_day`."""
    bars: List[tc.Bar] = []
    p = start_price
    for k, day in enumerate(trading_days(end_day, sessions)):
        s = random_walk_session(day, p, n_bars=n_bars, vol=vol, seed=seed * 1000 + k)
        bars.extend(s)
        p = s[-1].close
    return bars


def daily_bars(end_day: str, closes: Sequence[float], volumes: Optional[Sequence[float]] = None,
               wick_pct: float = 0.01, gaps: Optional[Dict[int, float]] = None) -> List[tc.Bar]:
    """Daily bars (stamped 09:30 ET) ending on end_day. gaps={index: open_price} overrides opens."""
    days = trading_days(end_day, len(closes))
    out: List[tc.Bar] = []
    prev = closes[0]
    for i, (day, c) in enumerate(zip(days, closes)):
        o = (gaps or {}).get(i, prev)
        h = max(o, c) * (1 + wick_pct)
        l = min(o, c) * (1 - wick_pct)
        v = volumes[i] if volumes else 1_000_000
        out.append(tc.Bar(et_ts(day, "09:30"), o, h, l, c, v))
        prev = c
    return out


def yahoo_payload(bars: Sequence[tc.Bar], symbol: str = "TEST", price: Optional[float] = None,
                  **meta: Any) -> Dict[str, Any]:
    """A v8/finance/chart-shaped payload for `bars`."""
    m = {"symbol": symbol, "currency": "USD", "exchangeTimezoneName": "America/New_York",
         "regularMarketPrice": price if price is not None else (bars[-1].close if bars else None),
         "chartPreviousClose": bars[0].open if bars else None}
    m.update(meta)
    return {"chart": {"error": None, "result": [{
        "meta": m,
        "timestamp": [b.ts for b in bars],
        "indicators": {"quote": [{
            "open": [b.open for b in bars], "high": [b.high for b in bars],
            "low": [b.low for b in bars], "close": [b.close for b in bars],
            "volume": [b.volume for b in bars]}]},
    }]}}


@contextmanager
def fake_bars(mapping: Dict[Any, Sequence[tc.Bar]], meta: Optional[Dict[str, Any]] = None) -> Iterator[mock.MagicMock]:
    """Patch tc.fetch_bars. mapping keys: 'SYM' or ('SYM', interval). Unknown -> FetchError."""
    def fake(symbol: str, interval: str = "5m", range_: str = "60d", prepost: bool = False, ttl: Any = None):
        sym = symbol.upper()
        bars = mapping.get((sym, interval), mapping.get(sym))
        if bars is None:
            raise tc.FetchError(f"{sym}: no fixture for interval {interval}")
        m = {"symbol": sym, "regularMarketPrice": bars[-1].close if bars else None}
        m.update(meta or {})
        return list(bars), m
    with mock.patch.object(tc, "fetch_bars", side_effect=fake) as m:
        yield m


@contextmanager
def fake_http(routes: Dict[str, Any]) -> Iterator[mock.MagicMock]:
    """Patch tc.http_get. routes: {url_substring: bytes | str | dict/list (JSON) | Exception}."""
    def fake(url: str, **kw: Any) -> bytes:
        for key, val in routes.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                if isinstance(val, bytes):
                    return val
                if isinstance(val, str):
                    return val.encode("utf-8")
                return json.dumps(val).encode("utf-8")
        raise tc.FetchError(f"no fake route for {url}")
    with mock.patch.object(tc, "http_get", side_effect=fake) as m:
        yield m


def write_fixture(directory: Path, url: str, body: Any) -> Path:
    """Write an offline fixture for `url` (used with $DAYTRADER_FIXTURES for CLI tests)."""
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / tc.fixture_name(url)
    if isinstance(body, bytes):
        p.write_bytes(body)
    elif isinstance(body, str):
        p.write_text(body, encoding="utf-8")
    else:
        p.write_text(json.dumps(body), encoding="utf-8")
    return p


def yahoo_chart_url(symbol: str, interval: str, range_: str, prepost: bool = False,
                    host: str = "query1.finance.yahoo.com") -> str:
    """The exact URL tc.fetch_bars requests first (for write_fixture)."""
    import urllib.parse
    params = urllib.parse.urlencode({"interval": interval, "range": range_,
                                     "includePrePost": "true" if prepost else "false"})
    return f"https://{host}/v8/finance/chart/{urllib.parse.quote(tc.yahoo_symbol(symbol))}?{params}"


def run_cli(script: Path, *args: str, env: Optional[Dict[str, str]] = None, timeout: float = 120):
    """Run a skill script as a subprocess (no network unless fixtures are missing -> exit 2)."""
    import subprocess
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True,
                          env=e, timeout=timeout)
