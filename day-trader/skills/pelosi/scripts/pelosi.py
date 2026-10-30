#!/usr/bin/env python3
"""pelosi - congressional stock trades from House Periodic Transaction Reports (PTRs).

Data (free, no API key): the House Clerk's financial-disclosure index
  https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{YEAR}FD.zip
and each PTR's PDF
  https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{YEAR}/{DocID}.pdf
Electronic filings are text PDFs: pelosi_pdf.py gets their text (pypdf if installed, else its
stdlib extractor) and pelosi_ptr.py turns it into trades. Paper filings are scanned images: they
are listed with a link, never guessed at. Parsed filings are cached in
$DAYTRADER_HOME/cache/ptr/<DocID>.json, so reruns only download new filings.

Usage examples:
  python3 scripts/pelosi.py recent                          # PTR filings of the last 30 days
  python3 scripts/pelosi.py recent --days 7 --trades        # ... and the trades inside them
  python3 scripts/pelosi.py recent --member pelosi          # one member's recent trades
  python3 scripts/pelosi.py ticker NVDA --days 365          # which members traded NVDA
  python3 scripts/pelosi.py member "Nancy Pelosi" --days 365
  python3 scripts/pelosi.py filing 20026590                 # one filing's trades
  python3 scripts/pelosi.py ticker NVDA --json              # --json on any command

Exit codes: 0 ok · 1 bad input · 2 data source down ("data error: ...").
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trader_common as tc  # noqa: E402

import csv  # noqa: E402
import difflib  # noqa: E402
import io  # noqa: E402
import re  # noqa: E402
import statistics  # noqa: E402
import time  # noqa: E402
import unicodedata  # noqa: E402
import http.client  # noqa: E402
import zlib  # noqa: E402
import xml.etree.ElementTree as ET  # noqa: E402
import zipfile  # noqa: E402
import argparse  # noqa: E402
from datetime import date, timedelta  # noqa: E402
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple  # noqa: E402

import pelosi_pdf  # noqa: E402
from pelosi_ptr import (ACTIONS, ASSET_TYPES, OWNERS, TICKER_RX, option_details, parse_mdy,  # noqa: E402,F401
                        parse_ptr_text)

CLERK = "https://disclosures-clerk.house.gov"
INDEX_URL = CLERK + "/public_disc/financial-pdfs/{year}FD.zip"
PTR_URL = CLERK + "/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
INDEX_TTL = 6 * 3600              # current year's index
PAST_INDEX_TTL = 7 * 86400        # earlier years barely change
PDF_DELAY = 0.5                   # polite pause between PDF downloads (seconds)
MAX_CONSECUTIVE_FAILURES = 3      # stop downloading after this many failures in a row
STOCK_ACT_DAYS = 45               # latest a trade may be disclosed after it happened
PARSER_VERSION = 2                # bump to invalidate cache/ptr/*.json after parser changes
PROGRESS_EVERY = 25

SHORT_OWNERS = {"": "self", "SP": "spouse", "JT": "joint", "DC": "child"}


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------

def _days_between(later: Optional[str], earlier: Optional[str]) -> Optional[int]:
    if not later or not earlier:
        return None
    return (date.fromisoformat(later) - date.fromisoformat(earlier)).days


def district_label(state_district: str) -> str:
    """'CA11' -> 'CA-11', 'AK00' -> 'AK-AL' (at large)."""
    m = re.fullmatch(r"([A-Z]{2})(\d{1,2})", (state_district or "").strip().upper())
    if not m:
        return (state_district or "").strip()
    n = int(m.group(2))
    return f"{m.group(1)}-{'AL' if n == 0 else n}"


def member_name(row: Dict[str, Any]) -> str:
    parts = [row.get("first") or "", row.get("last") or "", row.get("suffix") or ""]
    return " ".join(" ".join(p.split()) for p in parts if p and p.strip()) or "(unknown member)"


def _norm(s: str) -> str:
    """Lowercase ASCII words: 'Sánchez' and 'Sanchez' both -> 'sanchez'."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())


def member_matches(row: Dict[str, Any], query: str) -> bool:
    """'pelosi', 'Nancy Pelosi', 'Pelosi, N' or a district like 'CA11' / 'CA-11' / 'AK-AL'."""
    q = (query or "").strip()
    # 'AK-AL' / 'AKAL' is at large (district 00); a lowercase 'neal' stays a surname
    m = re.fullmatch(r"([A-Za-z]{2})-?(\d{1,2})", q) or re.fullmatch(r"([A-Za-z]{2})-(AL)", q, re.I) \
        or re.fullmatch(r"([A-Z]{2})(AL)", q)
    if m:
        sd = (row.get("state_district") or "").upper()
        want = 0 if m.group(2).upper() == "AL" else int(m.group(2))
        if sd[:2] == m.group(1).upper() and sd[2:].isdigit() and int(sd[2:]) == want:
            return True
    tokens = _norm(q).split()
    words = _norm(" ".join([row.get("first") or "", row.get("last") or "", row.get("suffix") or ""])).split()
    return bool(tokens) and all(any(w.startswith(t) for w in words) for t in tokens)


def normalize_ticker(t: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (t or "").upper())


# ---------------------------------------------------------------------------
# House Clerk index ({YEAR}FD.zip -> {YEAR}FD.xml / {YEAR}FD.txt)
# ---------------------------------------------------------------------------

def index_date(value: Optional[str]) -> Optional[str]:
    """The index's FilingDate ('1/17/2025', tolerating a time suffix or ISO form) -> 'YYYY-MM-DD'."""
    v = (value or "").strip()
    m = re.match(r"(\d{1,2}/\d{1,2}/\d{4})\b", v)
    if m:
        return parse_mdy(m.group(1))
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})\b", v)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None
    return None


def _index_row(rec: Dict[str, str], index_year: int) -> Optional[Dict[str, Any]]:
    doc_id = (rec.get("DocID") or "").strip()
    if not doc_id:
        return None
    year = (rec.get("Year") or "").strip()
    return {
        "doc_id": doc_id,
        "prefix": (rec.get("Prefix") or "").strip(),
        "first": (rec.get("First") or "").strip(),
        "last": (rec.get("Last") or "").strip(),
        "suffix": (rec.get("Suffix") or "").strip(),
        "filing_type": (rec.get("FilingType") or "").strip().upper(),
        "state_district": (rec.get("StateDst") or "").strip().upper(),
        "year": int(year) if year.isdigit() else index_year,
        "index_year": index_year,
        "filed": index_date(rec.get("FilingDate")),
    }


def parse_index_xml(data: bytes, index_year: int) -> List[Dict[str, Any]]:
    root = ET.fromstring(data.lstrip(b"\xef\xbb\xbf").strip())

    def local(tag: str) -> str:  # tolerate a default XML namespace ('{ns}Member')
        return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""

    rows = []
    for m in (el for el in root.iter() if local(el.tag) == "Member"):
        rec = {local(child.tag): (child.text or "") for child in m}
        row = _index_row(rec, index_year)
        if row:
            rows.append(row)
    return rows


def parse_index_txt(text: str, index_year: int) -> List[Dict[str, Any]]:
    rows = []
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), delimiter="\t")
    for rec in reader:
        row = _index_row({k.strip(): v for k, v in rec.items() if k}, index_year)
        if row:
            rows.append(row)
    return rows


def parse_index_zip(blob: bytes, index_year: int) -> List[Dict[str, Any]]:
    """Every filing in a {YEAR}FD.zip (all filing types; PTRs are FilingType 'P')."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        raise tc.FetchError(f"House Clerk {index_year} index is not a zip file (got {blob[:60]!r})") from None
    names = zf.namelist()
    problems = []
    for name in [n for n in names if n.lower().endswith(".xml")]:
        try:
            return parse_index_xml(zf.read(name), index_year)
        except (ET.ParseError, ValueError, zipfile.BadZipFile) as e:
            problems.append(f"{name}: {e}")
    for name in [n for n in names if n.lower().endswith(".txt")]:
        try:
            rows = parse_index_txt(zf.read(name).decode("utf-8", "replace"), index_year)
            if rows:
                return rows
        except (csv.Error, ValueError, zipfile.BadZipFile) as e:
            problems.append(f"{name}: {e}")
    raise tc.FetchError(f"could not read the House Clerk {index_year} index "
                        f"({'; '.join(problems) or 'no XML or TXT file inside'})")


def ptr_url(year: int, doc_id: str) -> str:
    return PTR_URL.format(year=year, doc_id=doc_id)


def ptr_filings(rows: Iterable[Dict[str, Any]], since: Optional[str], until: Optional[str],
                member: Optional[str] = None) -> List[Dict[str, Any]]:
    """PTR filings (FilingType P) filed in [since, until], newest first, with display fields."""
    out = []
    for r in rows:
        if r.get("filing_type") != "P" or not r.get("filed"):
            continue
        if (since and r["filed"] < since) or (until and r["filed"] > until):
            continue
        if member and not member_matches(r, member):
            continue
        f = dict(r)
        f["member"] = member_name(r)
        f["district"] = district_label(r.get("state_district", ""))
        f["url"] = ptr_url(r["index_year"], r["doc_id"])
        f["paper_likely"] = r["doc_id"][:1] in ("8", "9")
        out.append(f)
    out.sort(key=lambda f: (f["filed"], f["doc_id"]), reverse=True)
    return out


def _forget_cached(url: str) -> None:
    """Drop a cached response that turned out to be unusable (tc.cached_get caches any 200)."""
    try:
        (tc.home() / "cache" / tc.fixture_name(url)).unlink()
    except OSError:
        pass


def index_ttl(year: int, today: date) -> float:
    """Cache lifetime of a year's index. Last year's keeps growing for a while after New Year
    (late-December filings get posted), so in January it is refreshed like the current one."""
    if year >= today.year or (year == today.year - 1 and today.month == 1):
        return INDEX_TTL
    return PAST_INDEX_TTL


# tc.http_get only converts network errors that are OSErrors; a body cut short (IncompleteRead)
# or a truncated gzip (EOFError / zlib.error) would otherwise escape as a traceback.
_TRANSFER_ERRORS = (http.client.HTTPException, EOFError, zlib.error)


def _get(fetch: Any, url: str, *args: Any, **kw: Any) -> bytes:
    try:
        return fetch(url, *args, **kw)
    except _TRANSFER_ERRORS as e:
        raise tc.FetchError(f"incomplete download from {url} ({type(e).__name__}: {e})") from e


def load_index(years: Sequence[int], today: date) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Index rows for `years`. One year failing is a warning; all failing is a FetchError."""
    rows: List[Dict[str, Any]] = []
    errors: List[Tuple[int, tc.FetchError]] = []
    for y in years:
        url = INDEX_URL.format(year=y)
        try:
            blob = _get(tc.cached_get, url, index_ttl(y, today))
        except tc.FetchError as e:
            errors.append((y, e))
            continue
        try:
            rows.extend(parse_index_zip(blob, y))
        except tc.FetchError as e:
            _forget_cached(url)
            errors.append((y, tc.FetchError(f"{e} from {url}")))  # name the host, as for HTTP errors
    if errors and len(errors) == len(years):
        raise errors[-1][1]
    warnings = [f"could not load the {y} House index ({e}); filings from {y} are missing" for y, e in errors]
    undated = sum(1 for r in rows if r.get("filing_type") == "P" and not r.get("filed"))
    if undated:
        warnings.append(f"{undated} PTR filing(s) in the index have no readable filing date and were skipped")
    return rows, warnings


def window(days: int, today: date) -> Tuple[str, str, List[int]]:
    since = today - timedelta(days=days)
    return since.isoformat(), today.isoformat(), list(range(since.year, today.year + 1))


def parse_ptr_pdf(data: bytes) -> Dict[str, Any]:
    """PDF bytes -> {status: parsed|unparsed|paper, trades, engine, text_chars}."""
    if b"%PDF" not in data[:1024]:
        raise tc.FetchError(f"expected a PDF, got: {data[:80]!r}")
    best: Dict[str, Any] = {"status": "paper", "trades": [], "engine": None, "text_chars": 0}
    errors = []
    readable = undecodable_text = False
    for engine in pelosi_pdf.engines():
        try:
            text = pelosi_pdf.extract_text(data, engine)
        except Exception as e:  # a broken PDF must not stop the scan
            errors.append(f"{engine}: {e}")
            undecodable_text = undecodable_text or isinstance(e, pelosi_pdf.PdfTextError)
            continue
        readable = True
        chars = len(re.sub(r"\s", "", text))
        trades = parse_ptr_text(text) if chars else []
        if len(trades) > len(best["trades"]) or (not best["trades"] and chars > best["text_chars"]):
            best = {"status": "parsed" if trades else ("unparsed" if chars >= 40 else "paper"),
                    "trades": trades, "engine": engine, "text_chars": chars}
    if not readable or (best["status"] == "paper" and undecodable_text):
        # No extractor could open it, or one saw page content it could not decode while the other
        # found no text: a damaged/unreadable PDF, not proof of a scanned paper filing.
        best.update(status="unparsed", error="; ".join(errors))
    return best


def enrich_trades(filing: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Attach member, filing and disclosure-lag fields to a parsed filing's trades."""
    out = []
    for t in filing.get("trades") or []:
        t = dict(t)
        t["member"] = filing.get("member")
        t["district"] = filing.get("district")
        t["filed"] = filing.get("filed")
        t["doc_id"] = filing.get("doc_id")
        t["url"] = filing.get("url")
        lag = _days_between(filing.get("filed"), t.get("transaction_date"))
        t["lag_days"] = lag
        t["late"] = lag is not None and lag > STOCK_ACT_DAYS
        out.append(t)
    return out


def sort_trades(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(trades, key=lambda t: (t.get("transaction_date") or "", t.get("filed") or "",
                                         t.get("doc_id") or ""), reverse=True)


def lag_summary(trades: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    lags = [t["lag_days"] for t in trades if t.get("lag_days") is not None]
    if not lags:
        return {"median": None, "max": None, "min": None, "late": 0}
    return {"median": statistics.median(lags), "max": max(lags), "min": min(lags),
            "late": sum(1 for x in lags if x > STOCK_ACT_DAYS)}


def summarize_members(trades: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per member: buys/sells, first/last trade date, sum of the disclosed ranges."""
    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for t in trades:
        key = (t.get("member") or "", t.get("district") or "")
        g = groups.setdefault(key, {"member": key[0], "district": key[1], "trades": 0, "buys": 0,
                                    "sells": 0, "partial_sales": 0, "exchanges": 0, "first_trade": None,
                                    "last_trade": None, "amount_min_sum": 0, "amount_max_sum": 0,
                                    "open_ended": False})
        g["trades"] += 1
        g[{"P": "buys", "S": "sells", "S (partial)": "partial_sales", "E": "exchanges"}[t["type"]]] += 1
        d = t.get("transaction_date")
        if d:
            g["first_trade"] = min(g["first_trade"] or d, d)
            g["last_trade"] = max(g["last_trade"] or d, d)
        g["amount_min_sum"] += t.get("amount_min") or 0
        if t.get("amount_max") is None:
            g["open_ended"] = True
        g["amount_max_sum"] += t.get("amount_max") or t.get("amount_min") or 0
    return sorted(groups.values(), key=lambda g: (g["last_trade"] or "", g["trades"]), reverse=True)


def summarize_tickers(trades: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = {}
    for t in trades:
        key = t.get("ticker") or (t.get("asset") or "?")
        g = groups.setdefault(key, {"ticker": t.get("ticker"), "asset": t.get("asset"), "trades": 0,
                                    "buys": 0, "sells": 0, "exchanges": 0, "last_trade": None})
        g["trades"] += 1
        if t["type"] == "P":
            g["buys"] += 1
        elif t["type"].startswith("S"):
            g["sells"] += 1
        else:
            g["exchanges"] += 1
        d = t.get("transaction_date")
        if d:
            g["last_trade"] = max(g["last_trade"] or d, d)
    return sorted(groups.values(), key=lambda g: (g["trades"], g["last_trade"] or ""), reverse=True)


# ---------------------------------------------------------------------------
# Downloading and caching parsed filings
# ---------------------------------------------------------------------------

def _cache_name(doc_id: str) -> Optional[str]:
    return f"cache/ptr/{doc_id}.json" if re.fullmatch(r"\d{1,15}", doc_id or "") else None


def cached_filing(doc_id: str) -> Optional[Dict[str, Any]]:
    name = _cache_name(doc_id)
    if not name or os.environ.get("DAYTRADER_NO_CACHE"):
        return None
    try:
        d = tc.load_json(name, None)
    except tc.StateError:
        return None  # a corrupt cache entry is simply re-downloaded
    if not isinstance(d, dict) or d.get("parser_version") != PARSER_VERSION or "status" not in d:
        return None
    return d


def store_filing(doc_id: str, parsed: Dict[str, Any]) -> None:
    name = _cache_name(doc_id)
    if name:
        rec = dict(parsed)
        rec.update(doc_id=doc_id, parser_version=PARSER_VERSION, fetched_at=tc.iso_now())
        tc.save_json(name, rec)


class FilingScanner:
    """Reads filings newest first: cache hits are free, downloads stop at `max_downloads`."""

    def __init__(self, max_downloads: int, delay: Optional[float] = None, progress_total: int = 0) -> None:
        self.max_downloads = max_downloads
        self.delay = PDF_DELAY if delay is None else delay
        self.progress_total = progress_total
        self.stats = {"cached": 0, "downloaded": 0, "failed": 0, "not_searched": 0}
        self.errors: List[str] = []
        self.stopped: Optional[str] = None
        self._streak = 0

    def load(self, filing: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(filing)
        hit = cached_filing(filing["doc_id"])
        if hit is not None:
            self.stats["cached"] += 1
            out.update(status=hit["status"], trades=hit.get("trades") or [], source="cache")
            return out
        attempts = self.stats["downloaded"] + self.stats["failed"]
        if self.stopped or attempts >= self.max_downloads:
            self.stats["not_searched"] += 1
            out.update(status="not_searched", trades=[])
            return out
        if attempts and self.delay > 0 and not os.environ.get("DAYTRADER_FIXTURES"):
            time.sleep(self.delay)
        try:
            parsed = parse_ptr_pdf(_get(tc.http_get, filing["url"], timeout=30))
        except tc.FetchError as e:
            msg = str(e) if filing["url"] in str(e) else f"{e} from {filing['url']}"
            self.stats["failed"] += 1
            self.errors.append(msg)
            self._streak += 1
            if self._streak >= MAX_CONSECUTIVE_FAILURES:
                self.stopped = msg
            out.update(status="error", error=msg, trades=[])
            return out
        self._streak = 0
        self.stats["downloaded"] += 1
        n = self.stats["downloaded"]
        if self.progress_total > PROGRESS_EVERY and n % PROGRESS_EVERY == 0:
            print(f"pelosi: downloaded {n} of up to {min(self.max_downloads, self.progress_total)} filings...",
                  file=sys.stderr)
        if not parsed.get("error"):  # a PDF no extractor could open is retried next run
            store_filing(filing["doc_id"], parsed)
        out.update(status=parsed["status"], trades=parsed["trades"], source="download")
        if parsed.get("error"):
            out["error"] = parsed["error"]
        return out

    def run(self, filings: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        self.progress_total = len(filings)
        loaded = [self.load(f) for f in filings]
        if self.stats["failed"] and not (self.stats["cached"] or self.stats["downloaded"]):
            raise tc.FetchError(self.errors[0])
        return loaded

    def summary(self, loaded: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        by_status: Dict[str, int] = {}
        for f in loaded:
            by_status[f["status"]] = by_status.get(f["status"], 0) + 1
        s = dict(self.stats)
        # Coverage is complete only down to the first filing that was skipped (filings are newest
        # first). Older filings cached by earlier runs are still searched, but they must not push
        # the "covered back to" date past the unread ones in between.
        done: List[str] = []
        for f in loaded:
            if f["status"] == "not_searched":
                break
            if f["status"] != "error" and f.get("filed"):
                done.append(f["filed"])
        s.update(in_window=len(loaded), searched=s["cached"] + s["downloaded"],
                 parsed=by_status.get("parsed", 0), paper=by_status.get("paper", 0),
                 unparsed=by_status.get("unparsed", 0), oldest_searched=min(done) if done else None)
        if self.stopped:
            s["stopped_after_errors"] = self.stopped
        return s


# ---------------------------------------------------------------------------
# Commands (thin I/O layer)
# ---------------------------------------------------------------------------

def _today() -> date:
    return tc.now_et().date()


def _check_days(days: int) -> int:
    if days < 1 or days > 3660:
        raise ValueError("--days must be between 1 and 3660")
    return days


def _check_count(name: str, n: int) -> int:
    if n < 0:
        raise ValueError(f"{name} must be 0 or more")
    return n


def _public_filing(f: Dict[str, Any], with_trades: bool = False) -> Dict[str, Any]:
    keys = ["doc_id", "member", "prefix", "first", "last", "suffix", "state_district", "district",
            "filed", "year", "url", "paper_likely"]
    out = {k: f.get(k) for k in keys}
    if "status" in f:
        out["status"] = f["status"]
        out["trade_count"] = len(f.get("trades") or [])
        if f.get("error"):
            out["error"] = f["error"]
    if with_trades:
        out["trades"] = enrich_trades(f)
    return out


def _unread(loaded: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [_public_filing(f) for f in loaded if f["status"] in ("paper", "unparsed", "error")]


def cmd_recent(args: Any) -> Dict[str, Any]:
    days = _check_days(args.days)
    limit = _check_count("--limit", args.limit)
    max_filings = _check_count("--max-filings", args.max_filings)
    today = _today()
    since, until, years = window(days, today)
    rows, warnings = load_index(years, today)
    filings = ptr_filings(rows, since, until, args.member)
    trades_mode = args.trades if args.trades is not None else bool(args.member)
    shown = filings[:limit] if limit else filings
    out: Dict[str, Any] = {"command": "recent", "as_of": until, "since": since, "days": days,
                           "member_query": args.member, "filings_total": len(filings),
                           "trades_mode": trades_mode, "warnings": warnings}
    if args.member and not filings:
        out["did_you_mean"] = did_you_mean(rows, args.member)
    if not trades_mode:
        out["filings"] = [_public_filing(f) for f in shown]
    else:
        scanner = FilingScanner(max_filings)
        loaded = scanner.run(shown)
        trades = sort_trades([t for f in loaded for t in enrich_trades(f)])
        out.update(filings=[_public_filing(f) for f in loaded], trades=trades, total_trades=len(trades),
                   scan=scanner.summary(loaded), lag=lag_summary(trades), unread=_unread(loaded))
        _scan_warnings(out, scanner)
    out["disclaimer"] = tc.DISCLAIMER
    return out


def cmd_ticker(args: Any) -> Dict[str, Any]:
    raw = (args.symbol or "").strip().upper()
    if not TICKER_RX.match(raw.replace("-", ".")):
        raise ValueError(f"{args.symbol!r} does not look like a ticker (e.g. NVDA, BRK.B)")
    days = _check_days(args.days)
    limit = _check_count("--limit", args.limit)
    max_filings = _check_count("--max-filings", args.max_filings)
    today = _today()
    since, until, years = window(days, today)
    rows, warnings = load_index(years, today)
    filings = ptr_filings(rows, since, until)
    scanner = FilingScanner(max_filings)
    loaded = scanner.run(filings)
    want = normalize_ticker(raw)
    trades = sort_trades([t for f in loaded for t in enrich_trades(f) if normalize_ticker(t.get("ticker")) == want])
    out = {"command": "ticker", "ticker": raw, "as_of": until, "since": since, "days": days,
           "members": summarize_members(trades), "total_trades": len(trades),
           "trades": trades[:limit] if limit else trades, "scan": scanner.summary(loaded),
           "lag": lag_summary(trades), "unread": _unread(loaded), "warnings": warnings,
           "disclaimer": tc.DISCLAIMER}
    _scan_warnings(out, scanner)
    return out


def cmd_member(args: Any) -> Dict[str, Any]:
    query = (args.name or "").strip()
    if len(_norm(query)) < 2:
        raise ValueError("give a member name (e.g. pelosi, \"Nancy Pelosi\") or a district like CA11")
    days = _check_days(args.days)
    limit = _check_count("--limit", args.limit)
    max_filings = _check_count("--max-filings", args.max_filings)
    today = _today()
    since, until, years = window(days, today)
    rows, warnings = load_index(years, today)
    filings = ptr_filings(rows, since, until, query)
    scanner = FilingScanner(max_filings)
    loaded = scanner.run(filings)
    trades = sort_trades([t for f in loaded for t in enrich_trades(f)])
    members = sorted({(f["member"], f["district"]) for f in filings})
    out = {"command": "member", "query": query, "as_of": until, "since": since, "days": days,
           "members": [{"member": m, "district": d} for m, d in members],
           "filings": [_public_filing(f) for f in loaded], "tickers": summarize_tickers(trades),
           "total_trades": len(trades), "trades": trades[:limit] if limit else trades,
           "scan": scanner.summary(loaded), "lag": lag_summary(trades), "unread": _unread(loaded),
           "warnings": warnings, "disclaimer": tc.DISCLAIMER}
    if not filings:
        out["did_you_mean"] = did_you_mean(rows, query)
    _scan_warnings(out, scanner)
    return out


def cmd_filing(args: Any) -> Dict[str, Any]:
    doc_id = (args.doc_id or "").strip()
    if not re.fullmatch(r"\d{4,15}", doc_id):
        raise ValueError(f"{args.doc_id!r} is not a House DocID (digits, e.g. 20026590)")
    today = _today()
    warnings: List[str] = []
    years = [args.year] if args.year else [today.year, today.year - 1]
    if args.year and not 2008 <= args.year <= today.year + 1:
        raise ValueError(f"--year {args.year} is out of range")
    found = None
    try:
        rows, warnings = load_index(years, today)
        found = next((f for f in ptr_filings(rows, None, None) if f["doc_id"] == doc_id), None)
    except tc.FetchError as e:
        if not args.year:
            raise
        warnings.append(f"could not load the {args.year} index ({e}); member details unknown")
    if found is None:
        if not args.year:
            raise ValueError(f"DocID {doc_id} is not a PTR in the {years[-1]}-{years[0]} House index; "
                             f"pass --year YEAR if it was filed earlier")
        found = {"doc_id": doc_id, "member": "(unknown member)", "district": "", "filed": None,
                 "year": args.year, "index_year": args.year, "url": ptr_url(args.year, doc_id),
                 "paper_likely": doc_id[:1] in ("8", "9")}
    scanner = FilingScanner(1)
    loaded = scanner.run([found])[0]
    trades = enrich_trades(loaded)
    return {"command": "filing", "filing": _public_filing(loaded), "trades": trades,
            "lag": lag_summary(trades), "warnings": warnings, "disclaimer": tc.DISCLAIMER}


def did_you_mean(rows: Sequence[Dict[str, Any]], query: str) -> List[str]:
    names = sorted({member_name(r) for r in rows if r.get("filing_type") == "P"})
    by_last = {}
    for r in rows:
        if r.get("filing_type") == "P" and r.get("last"):
            by_last.setdefault(r["last"].lower(), member_name(r))
    q = _norm(query)
    hits = difflib.get_close_matches(q.split()[-1] if q else q, list(by_last), n=3, cutoff=0.7)
    out = [by_last[h] for h in hits]
    for n in difflib.get_close_matches(q, [_norm(x) for x in names], n=3, cutoff=0.7):
        full = next(x for x in names if _norm(x) == n)
        if full not in out:
            out.append(full)
    return out[:3]


def _scan_warnings(out: Dict[str, Any], scanner: FilingScanner) -> None:
    s = scanner.stats
    if scanner.stopped:
        out["warnings"].append(f"stopped downloading after {MAX_CONSECUTIVE_FAILURES} failures in a row "
                               f"({scanner.stopped}); results are incomplete")
    elif s["failed"]:
        out["warnings"].append(f"{_plural(s['failed'], 'filing')} could not be downloaded ({scanner.errors[0]})")


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------

def _short(s: Optional[str], n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "\u2026"


def _asset_cell(t: Dict[str, Any], width: int = 36) -> str:
    name = re.sub(r"\s*-?\s*(Class [A-Z]\s+)?(Common|Ordinary) (Stock|Shares)\b",
                  lambda m: (" " + m.group(1).strip()) if m.group(1) else "", t.get("asset") or "").strip(" -")
    label = t.get("asset_label")
    if label and label != "stock":
        tag = f" ({label})"
        return _short(name or "?", max(8, width - len(tag))) + tag
    return _short(name or "?", width)


def _plural(n: int, word: str, plural: Optional[str] = None) -> str:
    return f"{n:,} {word if n == 1 else (plural or word + 's')}"


def _lag_cell(t: Dict[str, Any]) -> str:
    lag = t.get("lag_days")
    if lag is None:
        return "-"
    return f"{lag}*" if lag > STOCK_ACT_DAYS else str(lag)


def _member_cell(t: Dict[str, Any]) -> str:
    d = t.get("district")
    return f"{t.get('member')} ({d})" if d else (t.get("member") or "")


def _trade_table(trades: Sequence[Dict[str, Any]], show_member: bool = True) -> str:
    rows = []
    for t in trades:
        r = [t.get("transaction_date") or "?"]
        if show_member:
            r.append(_short(_member_cell(t), 30))
        r += [SHORT_OWNERS.get(t.get("owner") or "", t.get("owner_label") or ""), t.get("ticker") or "-",
              _asset_cell(t), t.get("action") or "", t.get("amount") or "?", t.get("filed") or "?", _lag_cell(t)]
        rows.append(r)
    headers = ["traded"] + (["member"] if show_member else []) + \
        ["owner", "ticker", "asset", "type", "amount", "filed", "lag(d)"]
    return tc.table(rows, headers)


def _option_lines(trades: Sequence[Dict[str, Any]]) -> List[str]:
    out = []
    for t in trades:
        o = t.get("option")
        if not o or t.get("asset_type") != "OP":
            continue
        bits = [o.get("kind") or "option"]
        if o.get("strike") is not None:
            bits.append(f"strike {tc.fmt_money(o['strike']).replace('.00', '')}")
        if o.get("expires"):
            bits.append(f"exp {o['expires']}")
        if o.get("contracts"):
            bits.append(f"{o['contracts']:,} contracts")
        out.append(f"  {t.get('ticker') or _short(t.get('asset'), 20)} {t.get('transaction_date') or ''} "
                   f"{t.get('action', '').lower()}: {', '.join(bits)}")
    return out


def _lag_line(lag: Dict[str, Any]) -> str:
    if lag.get("median") is None:
        return ""
    med = lag["median"]
    med_s = f"{med:g}"
    late = f"; {lag['late']} reported after the deadline (*)" if lag["late"] else ""
    return (f"Disclosure lag: median {med_s} days, longest {lag['max']} (the STOCK Act allows up to "
            f"{STOCK_ACT_DAYS} days{late}). These trades were public only after the fact - old news, not a signal.")


def _scan_line(s: Dict[str, Any]) -> str:
    parts = [f"Searched {s['searched']:,} of {_plural(s['in_window'], 'filing')}"]
    detail = []
    if s["cached"]:
        detail.append(f"{s['cached']} cached")
    if s["downloaded"]:
        detail.append(f"{s['downloaded']} downloaded")
    if detail:
        parts[0] += f" ({', '.join(detail)})"
    if s.get("paper"):
        parts.append(f"{_plural(s['paper'], 'paper filing')} (scanned, no text) could not be read")
    if s.get("unparsed"):
        parts.append(f"{_plural(s['unparsed'], 'filing')} could not be parsed")
    if s.get("failed"):
        parts.append(f"{s['failed']:,} failed to download")
    line = "; ".join(parts) + "."
    if s.get("not_searched"):
        back = f" Results only cover filings back to {s['oldest_searched']}." if s.get("oldest_searched") else ""
        line += (f"{back} {_plural(s['not_searched'], 'older filing')} not searched yet - rerun to continue "
                 f"(cached filings are free) or raise --max-filings.")
    return line


def _unread_lines(unread: Sequence[Dict[str, Any]], title: str = "Open these PDFs yourself "
                  "(they could not be read here):", cap: int = 8) -> List[str]:
    if not unread:
        return []
    why = {"paper": "paper filing", "unparsed": "could not parse", "error": "download failed"}
    rows = [[f.get("filed") or "?", _short(f.get("member"), 28), why.get(f.get("status"), f.get("status")),
             f.get("url")] for f in unread[:cap]]
    table = tc.table(rows, ["filed", "member", "why", "pdf"]).splitlines()
    out = [title] + ["  " + line for line in table]
    if len(unread) > cap:
        out.append(f"  ... and {len(unread) - cap} more (see --json)")
    return out


def _footer(out: Dict[str, Any]) -> List[str]:
    lines = [f"! {w}" for w in out.get("warnings") or []]
    lines.append("House only (Senate eFD needs an interactive session) - amounts are ranges, not exact figures.")
    lines.append(out["disclaimer"])
    return lines


def render_recent(out: Dict[str, Any]) -> str:
    who = f" · member: {out['member_query']}" if out.get("member_query") else ""
    head = (f"HOUSE TRADE REPORTS · PTRs filed {out['since']} to {out['as_of']} (last {out['days']} days){who}"
            f" · {_plural(out['filings_total'], 'filing')}")
    lines = [head]
    if not out["filings_total"]:
        lines.append("No Periodic Transaction Reports filed in this window.")
        if out.get("did_you_mean"):
            lines.append("Did you mean: " + ", ".join(out["did_you_mean"]) + "?")
        return "\n".join(lines + [""] + _footer(out))
    if not out["trades_mode"]:
        rows = [[f["filed"], _short(f["member"], 28), f["district"], f["doc_id"], f["url"]] for f in out["filings"]]
        lines += ["", tc.table(rows, ["filed", "member", "district", "doc id", "pdf"])]
        more = out["filings_total"] - len(out["filings"])
        lines.append("")
        if more > 0:
            lines.append(f"Showing the {len(out['filings'])} newest of {out['filings_total']} (--limit 0 for all).")
        lines.append("Add --trades to read the trades inside each filing, or --member NAME for one member.")
        lines.append(f"Members have up to {STOCK_ACT_DAYS} days to report a trade, so these are old news by design.")
        return "\n".join(lines + _footer(out))
    lines.append(_scan_line(out["scan"]))
    if out["trades"]:
        lines += ["", _trade_table(out["trades"])]
        opts = _option_lines(out["trades"])
        if opts:
            lines += ["", "Options:"] + opts
        lines += ["", _lag_line(out["lag"])] if out["lag"].get("median") is not None else []
    else:
        lines.append("No trades could be read from these filings.")
    more = out["filings_total"] - len(out["filings"])
    if more > 0:
        lines.append(f"{_plural(more, 'older filing')} in the window not read (--limit 0 for all).")
    unread = _unread_lines(out.get("unread") or [])
    if unread:
        lines += [""] + unread
    return "\n".join(lines + [""] + _footer(out))


def render_ticker(out: Dict[str, Any]) -> str:
    lines = [f"WHO TRADED {out['ticker']} · House PTRs filed {out['since']} to {out['as_of']} "
             f"(last {out['days']} days)",
             _scan_line(out["scan"])]
    if not out["total_trades"]:
        lines.append(f"No House member disclosed a {out['ticker']} trade in the filings searched.")
    else:
        rows = []
        for g in out["members"]:
            hi = "+" if g["open_ended"] else ""
            rows.append([_short(f"{g['member']} ({g['district']})" if g["district"] else g["member"], 30),
                         g["buys"], g["sells"] + g["partial_sales"], g["trades"], g["first_trade"] or "?",
                         g["last_trade"] or "?",
                         f"{tc.fmt_money(g['amount_min_sum']).replace('.00', '')} - "
                         f"{tc.fmt_money(g['amount_max_sum']).replace('.00', '')}{hi}"])
        lines += ["", tc.table(rows, ["member", "buys", "sells", "trades", "first", "last", "total (sum of ranges)"])]
        shown = len(out["trades"])
        title = "TRADES (newest first)" + (f" - {shown} of {out['total_trades']}, --limit 0 for all"
                                           if shown < out["total_trades"] else "")
        lines += ["", title, _trade_table(out["trades"])]
        opts = _option_lines(out["trades"])
        if opts:
            lines += ["", "Options:"] + opts
        lines += ["", _lag_line(out["lag"])] if out["lag"].get("median") is not None else []
    unread = _unread_lines(out.get("unread") or [], f"Could not be read here - these filings may also hold "
                                                    f"{out['ticker']} trades (open the PDFs):")
    if unread:
        lines += [""] + unread
    return "\n".join(lines + [""] + _footer(out))


def render_member(out: Dict[str, Any]) -> str:
    who = ", ".join(f"{m['member']} ({m['district']})" if m["district"] else m["member"] for m in out["members"])
    lines = [f"MEMBER TRADES · {who or out['query']} · PTRs filed {out['since']} to {out['as_of']} "
             f"(last {out['days']} days)"]
    if not out["members"]:
        lines.append(f"No PTR filings by a member matching {out['query']!r} in this window.")
        if out.get("did_you_mean"):
            lines.append("Did you mean: " + ", ".join(out["did_you_mean"]) + "?")
        lines.append("Try a longer --days, a last name only, or a district like CA11.")
        return "\n".join(lines + [""] + _footer(out))
    if len(out["members"]) > 1:
        lines.append(f"! {len(out['members'])} members match {out['query']!r} - "
                     f"add a first name or district to narrow it.")
    lines.append(_scan_line(out["scan"]))
    if out["trades"]:
        def counts(g: Dict[str, Any]) -> str:
            kinds = (("buys", "buy"), ("sells", "sell"), ("exchanges", "exchange"))
            bits = [_plural(g[k], w) for k, w in kinds if g[k]]
            return ", ".join(bits)
        top = "; ".join(f"{g['ticker'] or _short(g['asset'], 18)} {counts(g)}" for g in out["tickers"][:8])
        if len(out["tickers"]) > 8:
            top += f"; +{len(out['tickers']) - 8} more"
        lines += [f"Most traded: {top}", ""]
        shown = len(out["trades"])
        if shown < out["total_trades"]:
            lines.append(f"Newest {shown} of {out['total_trades']} trades (--limit 0 for all):")
        lines.append(_trade_table(out["trades"], show_member=len(out["members"]) > 1))
        opts = _option_lines(out["trades"])
        if opts:
            lines += ["", "Options:"] + opts
        lines += ["", _lag_line(out["lag"])] if out["lag"].get("median") is not None else []
    else:
        lines.append("No trades could be read from these filings.")
    unread = _unread_lines(out.get("unread") or [])
    if unread:
        lines += [""] + unread
    return "\n".join(lines + [""] + _footer(out))


def render_filing(out: Dict[str, Any]) -> str:
    f = out["filing"]
    who = f"{f['member']} ({f['district']})" if f.get("district") else f["member"]
    lines = [f"PTR {f['doc_id']} · {who} · filed {f.get('filed') or '?'} · {f['url']}"]
    status = f.get("status")
    if status == "parsed":
        lines += ["", _trade_table(out["trades"], show_member=False)]
        opts = _option_lines(out["trades"])
        if opts:
            lines += ["", "Options:"] + opts
        descs = [f"  {t.get('ticker') or _short(t.get('asset'), 20)}: {t['description']}"
                 for t in out["trades"] if t.get("description")]
        if descs:
            lines += ["", "Descriptions:"] + descs
        lines += ["", _lag_line(out["lag"])] if out["lag"].get("median") is not None else []
    elif status == "paper":
        lines.append("Paper filing (scanned image, no text) - open the PDF to read it.")
    elif status == "unparsed":
        lines.append("This PDF has text but no transaction rows could be read - open the PDF to read it.")
    elif status == "error":
        lines.append(f"Download failed: {f.get('error')}")
    return "\n".join(lines + [""] + _footer(out))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    """Bad flags are bad input: exit 1, not argparse's 2 (2 means a data source is down)."""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    p = tc.make_parser("Congressional stock trades from House Periodic Transaction Reports (House Clerk).")
    p.error = _Parser.error.__get__(p)  # type: ignore[method-assign]
    sub = p.add_subparsers(dest="command", metavar="{recent,ticker,member,filing}", parser_class=_Parser)
    sub.required = True

    r = sub.add_parser("recent", help="recent PTR filings (and their trades)")
    r.add_argument("--days", type=int, default=30, help="filed in the last N days (default 30)")
    r.add_argument("--member", help="only this member: last name, full name or district (CA11)")
    r.add_argument("--limit", type=int, default=20, help="newest N filings (default 20, 0 = all)")
    g = r.add_mutually_exclusive_group()
    g.add_argument("--trades", dest="trades", action="store_true", default=None,
                   help="download and parse each filing's trades (default with --member)")
    g.add_argument("--no-trades", dest="trades", action="store_false", help="just list the filings")
    r.add_argument("--max-filings", type=int, default=25, help="download at most N PDFs (default 25)")
    tc.add_json_flag(r)

    t = sub.add_parser("ticker", help="which members traded a ticker")
    t.add_argument("symbol", help="ticker, e.g. NVDA")
    t.add_argument("--days", type=int, default=365, help="filings filed in the last N days (default 365)")
    t.add_argument("--max-filings", type=int, default=200,
                   help="download at most N PDFs this run (default 200; cached filings are free)")
    t.add_argument("--limit", type=int, default=50, help="show the newest N trades (default 50, 0 = all)")
    tc.add_json_flag(t)

    m = sub.add_parser("member", help="one member's trades")
    m.add_argument("name", help="last name, full name or district, e.g. pelosi, \"Nancy Pelosi\", CA11")
    m.add_argument("--days", type=int, default=365, help="filings filed in the last N days (default 365)")
    m.add_argument("--max-filings", type=int, default=50, help="download at most N PDFs (default 50)")
    m.add_argument("--limit", type=int, default=50, help="show the newest N trades (default 50, 0 = all)")
    tc.add_json_flag(m)

    f = sub.add_parser("filing", help="the trades in one PTR by DocID")
    f.add_argument("doc_id", help="House DocID, e.g. 20026590")
    f.add_argument("--year", type=int, help="filing year (default: look it up in this and last year's index)")
    tc.add_json_flag(f)
    return p


COMMANDS = {"recent": (cmd_recent, render_recent), "ticker": (cmd_ticker, render_ticker),
            "member": (cmd_member, render_member), "filing": (cmd_filing, render_filing)}


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    fn, render = COMMANDS[args.command]
    out = fn(args)
    tc.emit(out, args.json, render)
    return 0


if __name__ == "__main__":
    tc.run(main)
