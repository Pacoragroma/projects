#!/usr/bin/env python3
"""pelosi_ptr - House Periodic Transaction Report text -> transactions (pure, no I/O).

parse_ptr_text() takes the text an extractor pulled out of an electronically filed PTR (pypdf,
or pelosi_pdf's stdlib extractor) and returns one dict per transaction row. Each row ends with
`<type> <transaction date> <notification date> <amount>`, e.g.

    SP NVIDIA Corporation - Common Stock (NVDA) [ST] P 01/14/2025 01/14/2025 $250,001 - $500,000
    F S: New
    D: Purchased 50 call options with a strike price of $80 and an expiration date of 12/19/2025.

The owner code (SP/JT/DC, blank = the member), asset name, (TICKER) and [asset type] precede it,
and label lines follow it (F S = Filing Status, S O = Subholding Of, D = Description,
L = Location, C = Comments). Extractors break these rows differently - cell by cell, by visual
line, or with the asset wrapped around the dates - so the parser anchors on the
type/dates/amount core and re-assembles the asset text around it.

Usage (debugging aid):  python3 scripts/pelosi_ptr.py extracted.txt
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

OWNERS = {"": "self", "SP": "spouse", "JT": "joint", "DC": "dependent child"}
ACTIONS = {"P": "BUY", "S": "SELL", "S (partial)": "PARTIAL SALE", "E": "EXCHANGE"}
ASSET_TYPES = {  # short labels; the full code list is in reference.md
    "ST": "stock", "OP": "option", "EF": "ETF", "MF": "mutual fund", "GS": "government bond",
    "CS": "corporate bond", "CT": "crypto", "PS": "private stock", "RE": "REIT", "RS": "RSU",
    "ET": "ETN", "AB": "asset-backed", "BA": "bank account", "HN": "hedge/PE fund",
    "HE": "hedge/PE fund", "OI": "ownership interest", "OL": "ownership interest", "RP": "real estate",
    "FU": "futures", "FE": "currency", "PM": "precious metal", "VA": "annuity", "FN": "annuity",
    "OT": "other", "TR": "trust", "SA": "stock appreciation right", "MA": "managed account",
    "BK": "brokerage account", "IR": "IRA", "IH": "IRA cash", "4K": "401(k)", "5C": "529 plan",
    "5F": "529 plan", "5P": "529 plan", "DB": "pension", "PE": "pension", "VI": "insurance",
    "WU": "insurance", "CO": "collectible", "DO": "debt owed", "IC": "investment club",
    "EQ": "excepted fund", "DS": "statutory trust", "FA": "farm", "IP": "royalties", "MO": "mineral rights",
}
TICKER_RX = re.compile(r"^[A-Z]{1,5}(?:[.\-/][A-Z0-9]{1,3})?$")
# Parenthesised words that are not symbols. Keep real tickers out of this set: CL (Colgate),
# SE (Sea Ltd), AG (First Majestic), SA (Seabridge Gold) and USD (ProShares) all trade.
NOT_TICKERS = {"ADR", "ADS", "ETF", "ETN", "LLC", "LP", "LTD", "INC", "US", "REIT", "PLC",
               "NV", "FKA", "AKA", "NEW", "OLD", "CUSIP"}


def parse_mdy(value: Optional[str]) -> Optional[str]:
    """'1/14/2025' -> '2025-01-14' (None when it is not a real date)."""
    m = re.fullmatch(r"\s*(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})\s*", value or "")
    if not m:
        return None
    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    if not 1990 <= y <= 2100:
        return None
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def _money(s: str) -> int:
    return int(round(float(s.replace(",", ""))))


# ---------------------------------------------------------------------------
# PTR text -> transactions (pure)
# ---------------------------------------------------------------------------

_DATE = r"\d{1,2}/\d{1,2}/\d{4}"
_CORE_RX = re.compile(
    r"(?<![A-Za-z0-9&.\-/])(?P<type>P|E|S(?:\s*\(\s*(?i:partial)\s*\))?)\s*(?P<tdate>" + _DATE + r")"
    r"(?:\s*(?P<ndate>" + _DATE + r"))?")
_MONEY_RX = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")
_CODE_RX = re.compile(r"\[\s*([A-Z0-9]{2})\s*\]")
_PARTIAL_RX = re.compile(r"\s*\(\s*partial\s*\)", re.I)
_TICKER_PAREN_RX = re.compile(r"\(\s*([A-Z][A-Z0-9.\-/]{0,8})\s*\)")
_OWNER_RX = re.compile(r"^(SP|JT|DC)(?=\s|$)")
_MARKER_RX = re.compile(r"^(?P<label>[^\W\d_][^\s:]{0,20}(?:\s+[^\W\d_][^\s:]{0,20}){0,2})\s*:\s*(?P<val>.*)$")
_LABEL_WORDS = {"filing", "status", "subholding", "of", "description", "location", "comments", "comment"}
_META_KEYS = {"FS": "filing_status", "SO": "subholding_of", "D": "description", "L": "location",
              "C": "comments"}
_CONTINUES = {"description", "comments", "subholding_of"}
_HEADER_WORDS = {"id", "owner", "asset", "transaction", "type", "date", "notification", "amount",
                 "cap.", "cap", "gains", ">", "$200?", "gains>"}
_DROP_RX = re.compile(
    r"^(?:Clerk of the House of Representatives\b.*|Page \d+ of \d+|"
    r"(?:Name|Status|State/District)\s*:.*|Periodic Transaction Report|P T R|Filer Information|F I|"
    r"Transactions|T)$", re.I)
_END_RX = re.compile(
    r"^(?:\*\s*For the complete list|.*asset-type-codes|I P O\b|C S\b|Initial Public Offerings?\b|"
    r"Certification and Signature|Yes No$|Yes$|No$|I CERTIFY\b|Digitally Signed\b|Did you purchase\b)", re.I)
_FILING_ID_RX = re.compile(r"\bFiling ID\s*#\s*\d+\b", re.I)
_CONTINUATION_WORDS = {
    "stock", "common", "class", "shares", "share", "inc", "corp", "corporation", "co", "company",
    "ltd", "llc", "lp", "l.p", "plc", "n.v", "nv", "s.a", "sa", "ag", "se", "holdings", "holding",
    "trust", "fund", "etf", "index", "units", "unit", "ordinary", "depositary", "depository",
    "series", "preferred", "adr", "ads", "sponsored", "shs", "cl", "cap", "inc.", "corp.", "co.",
    "ltd.", "group", "partners", "portfolio", "shares,"}
END_LINE = "<<END>>"


def _garbled(line: str) -> bool:
    """pypdf turns glyphs with no Unicode mapping into Latin-1 junk ('FÍÐÍÒË SØÅØÙ×')."""
    letters = [c for c in line if c.isalpha()]
    return len(letters) >= 3 and sum(1 for c in letters if not c.isascii()) >= 0.4 * len(letters)


def _marker(line: str) -> Optional[Tuple[str, str]]:
    """'F S : New' / 'Filing Status: New' / 'D: text' -> ('filing_status', 'New')."""
    m = _MARKER_RX.match(line)
    if not m:
        return None
    initials = ""
    for w in m.group("label").split():
        if not w[0].isupper():
            return None
        if len(w) == 1 or w.lower() in _LABEL_WORDS or not w.isascii():
            initials += w[0]
        else:
            return None
    key = _META_KEYS.get(initials)
    return (key, m.group("val").strip()) if key else None


def _clean_lines(text: str) -> List[str]:
    """Normalize extractor output into content lines; page furniture removed, section ends marked."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", " ")
    for ch in "\u2010\u2011\u2012\u2013\u2014\u2212":  # dashes -> "-"
        text = text.replace(ch, "-")
    out: List[str] = []
    for raw in text.split("\n"):
        s = " ".join(_FILING_ID_RX.sub(" ", raw.replace("\u00a0", " ")).split())
        if not s or _DROP_RX.match(s) or all(w.lower() in _HEADER_WORDS for w in s.split()):
            continue
        # Labels glued onto other text ('... $15,000 F S: New D: Bought.') start their own line,
        # and so does a second row glued onto the first ('... $15,000 SP Apple Inc. (AAPL) [ST] ...').
        s = re.sub(r"(?<=\S)\s+(?=(?:F\s?S|S\s?O)\s?:\s)", "\n", s)
        s = re.sub(r"(?m)^((?:F\s?S|Filing Status)\s?:\s*(?:New|Amended)\b)\s+(?=(?:[DLC]|S\s?O)\s?:\s)",
                   "\\1\n", s)
        s = re.sub(r"(?<=\S)\s+(?=(?:SP|JT|DC)\s+[^\n]*\[[A-Z0-9]{2}\])", "\n", s)
        for piece in s.split("\n"):
            if _marker(piece):
                out.append(piece)
            elif _END_RX.match(piece) or _garbled(piece):
                out.append(END_LINE)
            else:
                out.append(piece)
    return out


def _kind(line: str) -> str:
    s = line.strip()
    if not s:
        return "empty"
    if s == END_LINE:
        return "end"
    if _marker(s):
        return "marker"
    return "text"


def _proper_start(text: str) -> bool:
    """Does `text` look like the first line of an asset name (not a wrapped continuation)?"""
    t = text.strip()
    if not t:
        return False
    first = t.split()[0]
    if first[0] in "([-&,.;:/'\"" or not (first[0].isupper() or first[0].isdigit()):
        return False
    return first.strip(",;:").lower() not in _CONTINUATION_WORDS


_ABBREV_END = re.compile(r"\b(?:Inc|Corp|Co|Cos|Ltd|Plc|N\.V|S\.A|L\.P|A\.G|S\.E|Bros|Intl|Mfg|Hldgs|Grp|"
                         r"Tr|Ser|Cl|U\.S|Jr|Sr)\.$", re.I)
_SMALL_WORDS = {"of", "and", "the", "de", "for", "on", "in", "at", "to", "a", "an", "&", "du", "la"}


def _name_like(line: str) -> bool:
    """Could this line be part of an asset name (title case, no sentence end)? Descriptions are prose."""
    s = line.strip()
    if not s or (s.endswith(".") and not _ABBREV_END.search(s)):
        return False
    words = s.split()
    lower = [w for w in words if w[0].islower() and w.lower() not in _SMALL_WORDS
             and not (len(w) > 1 and w[1].isupper())]  # iShares, eBay are names
    return len(lower) <= len(words) // 4


def _asset_start(lines: List[str], kinds: List[str], lo: int) -> int:
    """Index of the first line of the asset that ends this segment (the text before a core)."""
    n = len(lines)
    anchor = None
    if n and lines[-1].strip() and n - 1 >= lo:
        anchor = n - 1                   # text on the same line, right before the type/date
    else:
        for j in range(n - 1, lo - 1, -1):
            if kinds[j] == "text" and _CODE_RX.search(lines[j]):
                anchor = j
                break
        if anchor is None:
            for j in range(n - 1, lo - 1, -1):
                if kinds[j] == "text":
                    anchor = j
                    break
    if anchor is None:
        return n
    start = anchor

    def text_from(k: int) -> str:
        return " ".join(lines[i].strip() for i in range(k, n) if lines[i].strip())

    while True:
        t = text_from(start)
        if _OWNER_RX.match(t) or _proper_start(t):
            break
        j = start - 1
        while j >= lo and kinds[j] == "empty":
            j -= 1
        if j < lo or kinds[j] != "text" or anchor - j > 3 or not _name_like(lines[j]):
            break
        start = j
    j = start - 1                        # owner code alone on the line above (cell-order text)
    while j >= lo and kinds[j] == "empty":
        j -= 1
    if j >= lo and lines[j].strip() in ("SP", "JT", "DC") and not _OWNER_RX.match(text_from(start)):
        start = j
    return start


def _split_segment(lines: List[str], has_prev: bool, prev_needs_code: bool,
                   has_cur: bool) -> Tuple[List[str], str, List[str]]:
    """Text between two transaction cores -> (current asset lines, previous asset tail, previous meta)."""
    kinds = [_kind(l) for l in lines]
    if has_cur:
        lo = 0
        for j, k in enumerate(kinds):
            if k in ("marker", "end"):
                lo = j + 1
        a = _asset_start(lines, kinds, lo)
        cur, rest = lines[a:], lines[:a]
    else:
        cut = next((j for j, k in enumerate(kinds) if k == "end"), len(lines))
        cur, rest = [], lines[:cut]
    tail = ""
    if has_prev and prev_needs_code:
        rk = [_kind(l) for l in rest]
        first = next((j for j, k in enumerate(rk) if k in ("marker", "end")), len(rest))
        pre = "\n".join(rest[:first])
        mc = _CODE_RX.search(pre)
        if mc:
            end = mc.end()
            mp = _PARTIAL_RX.match(pre, end)  # the type cell's wrapped '(partial)' sits right after it
            if mp:
                end = mp.end()
            tail = pre[:end]
            remainder = pre[end:]
            rest = ([remainder] if remainder.strip() else []) + rest[first:]
    meta: List[str] = []
    if has_prev:
        started = False
        for line in rest:
            k = _kind(line)
            if k == "end":
                break
            if k == "marker":
                started = True
            if started:
                meta.append(line)
    return cur, tail, meta


def _parse_meta(lines: List[str]) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    cur = None
    for line in lines:
        s = line.strip()
        if not s:
            continue
        mk = _marker(s)
        if mk:
            cur, val = mk
            fields[cur] = (fields[cur] + "; " + val) if fields.get(cur) and val else (val or fields.get(cur, ""))
        elif cur in _CONTINUES:
            fields[cur] = (fields.get(cur, "") + " " + s).strip()
        else:
            cur = None
    return {k: v for k, v in fields.items() if v}


_AMOUNT_LABEL_RX = re.compile(r"(?:^|(?<=\s))((?:Spouse/DC\s+)?Over|Spouse/DC)[ \t]*$", re.I)


def _open_ended_label(before: str) -> Optional[Tuple[str, str]]:
    """'Over' / 'Spouse/DC Over' in the amount cell before its '$' figure -> (label, other text).

    The amount column is right of the asset column, so on each visual line its words come last:
    'P 01/02/2025 01/03/2025 Spouse/DC Over' then '(AAPL) [ST] $1,000,000' (the asset wrapped).
    """
    words: List[str] = []
    rest: List[str] = []
    for line in before.split("\n"):
        m = _AMOUNT_LABEL_RX.search(line)
        if m:
            words.append(m.group(1))
            line = line[:m.start(1)]
        rest.append(line)
    joined = " ".join(words)
    if not re.search(r"\bOver\b", joined, re.I):
        return None
    label = "Spouse/DC Over" if re.search(r"Spouse/DC", joined, re.I) else "Over"
    return label, "\n".join(rest)


def _read_amount(body: str, pos: int, limit: int) -> Optional[Dict[str, Any]]:
    """The amount range right after a transaction's dates (it may wrap or interleave with asset text)."""
    seg = body[pos:limit]
    lines = seg.split("\n")
    keep = [lines[0]]
    for line in lines[1:3]:              # the amount cell is at most 3 visual lines away
        if _kind(line) in ("marker", "end"):
            break
        keep.append(line)
    seg = "\n".join(keep)
    m1 = _MONEY_RX.search(seg)
    if not m1:
        return None
    before = seg[:m1.start()]
    lo = _money(m1.group(1))
    over = _open_ended_label(before)
    if over:
        label, interleave = over
        return {"text": f"{label} ${lo:,}", "min": lo + 1, "max": None, "end": pos + m1.end(),
                "interleave": interleave}
    rest = seg[m1.end():]
    dash = re.match(r"\s*-\s*", rest)
    if dash:
        m2 = _MONEY_RX.search(rest, dash.end())
        if m2:
            hi = _money(m2.group(1))
            return {"text": f"${lo:,} - ${hi:,}", "min": min(lo, hi), "max": max(lo, hi),
                    "end": pos + m1.end() + m2.end(), "interleave": before + "\n" + rest[dash.end():m2.start()]}
        # '$1,001 -' whose upper bound is missing: keep it open-ended rather than invent one
        return {"text": f"${lo:,} - ?", "min": lo, "max": None, "end": pos + m1.end() + dash.end(),
                "interleave": before}
    return {"text": f"${lo:,}", "min": lo, "max": lo, "end": pos + m1.end(), "interleave": before}


def _finish_asset(parts: List[str]) -> Dict[str, Any]:
    raw = " ".join(" ".join(p.split()) for p in parts if p and p.strip())
    owner = ""
    m = _OWNER_RX.match(raw)
    if m:
        owner, raw = m.group(1), raw[m.end():].strip()
    code = None
    codes = list(_CODE_RX.finditer(raw))
    if codes:
        code = codes[-1].group(1)
        raw = raw[:codes[-1].start()].strip()
    ticker = None
    for tm in reversed(list(_TICKER_PAREN_RX.finditer(raw))):
        cand = tm.group(1)
        if TICKER_RX.match(cand) and cand not in NOT_TICKERS:
            ticker = cand.replace("/", ".")
            raw = (raw[:tm.start()] + " " + raw[tm.end():]).strip()
            break
    name = re.sub(r"\s+", " ", raw).strip(" -,;")
    return {"owner": owner, "asset": name, "ticker": ticker, "asset_type": code}


def option_details(description: Optional[str], asset_type: Optional[str]) -> Optional[Dict[str, Any]]:
    """Call/put, strike, expiry and contracts from an option trade's description, when stated."""
    d = description or ""
    if asset_type != "OP" and not re.search(r"\b(?:call|put)s?\b.{0,20}\boptions?\b", d, re.I):
        return None
    out: Dict[str, Any] = {}
    if re.search(r"\bcalls?\b", d, re.I):
        out["kind"] = "call"
    elif re.search(r"\bputs?\b", d, re.I):
        out["kind"] = "put"
    m = re.search(r"strike(?:\s+price)?(?:\s+(?:of|at))?\s*:?\s*\$\s?(\d[\d,]*(?:\.\d+)?)", d, re.I)
    if m:
        out["strike"] = float(m.group(1).replace(",", ""))
    m = re.search(r"expir\w*(?:\s+date)?(?:\s+(?:of|on))?\s*:?\s*(\d{1,2}/\d{1,2}/\d{2,4})", d, re.I)
    if m and parse_mdy(m.group(1)):
        out["expires"] = parse_mdy(m.group(1))
    m = re.search(r"\b(\d[\d,]*)\s+(?:call|put)?\s*(?:options?|contracts?)\b", d, re.I)
    if m:
        out["contracts"] = int(m.group(1).replace(",", ""))
    if re.search(r"\bexercis", d, re.I):
        out["exercised"] = True
    return out or None


def parse_ptr_text(text: str) -> List[Dict[str, Any]]:
    """Transactions in the text of an electronically filed PTR (pure; tolerant of line breaks).

    Each row ends with `<type> <transaction date> <notification date> <amount>`; the asset (owner
    code, name, (TICKER), [asset type]) precedes it and `F S:` / `S O:` / `D:` lines follow it.
    """
    lines = _clean_lines(text)
    body = "\n".join(lines)
    matches = [m for m in _CORE_RX.finditer(body) if parse_mdy(m.group("tdate"))]
    rows: List[Dict[str, Any]] = []
    for i, m in enumerate(matches):
        if rows and m.start() < rows[-1]["end"]:
            continue
        limit = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        amt = _read_amount(body, m.end(), limit)
        if amt is None and not m.group("ndate"):
            continue  # a lone 'P 1/2/2025' with no amount is not a transaction row
        rows.append({"m": m, "amt": amt, "end": amt["end"] if amt else m.end()})

    trades: List[Dict[str, Any]] = []
    assets: List[List[str]] = []
    for i in range(len(rows) + 1):
        start = rows[i - 1]["end"] if i else 0
        end = rows[i]["m"].start() if i < len(rows) else len(body)
        prev_needs_code = bool(i) and not _CODE_RX.search(" ".join(assets[i - 1]))
        cur, tail, meta = _split_segment(body[start:end].split("\n"), bool(i), prev_needs_code, i < len(rows))
        if i:
            if tail:
                assets[i - 1].append(tail)
            trades[i - 1].update(_parse_meta(meta))
        if i < len(rows):
            r = rows[i]
            parts = list(cur)
            interleave = (r["amt"] or {}).get("interleave") or ""
            if interleave.strip() and not _CODE_RX.search(" ".join(parts)):
                parts.append(interleave)
            assets.append(parts)
            raw_type = r["m"].group("type")
            ttype = "S (partial)" if "partial" in raw_type.lower() else raw_type
            amt = r["amt"] or {}
            trades.append({
                "type": ttype,
                "action": ACTIONS[ttype],
                "transaction_date": parse_mdy(r["m"].group("tdate")),
                "notification_date": parse_mdy(r["m"].group("ndate")),
                "amount": amt.get("text"),
                "amount_min": amt.get("min"),
                "amount_max": amt.get("max"),
            })
    out = []
    for t, parts in zip(trades, assets):
        if t["type"] == "S" and any(_PARTIAL_RX.search(p) for p in parts):
            # 'S (partial)' whose type cell wrapped: '(partial)' was read with the wrapped asset text
            t.update(type="S (partial)", action=ACTIONS["S (partial)"])
            parts = [_PARTIAL_RX.sub(" ", p) for p in parts]
        a = _finish_asset(parts)
        t.update(a)
        t["owner_label"] = OWNERS.get(a["owner"], a["owner"])
        t["asset_label"] = ASSET_TYPES.get(a["asset_type"] or "", (a["asset_type"] or "").lower() or "unknown")
        opt = option_details(t.get("description"), a["asset_type"])
        if opt:
            t["option"] = opt
        m = re.search(r"\b(\d[\d,]*)\s+shares\b", t.get("description") or "", re.I)
        if m:
            t["shares"] = int(m.group(1).replace(",", ""))
        out.append({k: t.get(k) for k in (
            "owner", "owner_label", "asset", "ticker", "asset_type", "asset_label", "type", "action",
            "transaction_date", "notification_date", "amount", "amount_min", "amount_max",
            "filing_status", "subholding_of", "description", "location", "comments", "option", "shares")
            if t.get(k) is not None or k in ("ticker", "amount", "amount_min", "amount_max", "notification_date")})
    return out


if __name__ == "__main__":  # pragma: no cover - manual debugging aid
    with open(sys.argv[1], encoding="utf-8") as fh:
        print(json.dumps(parse_ptr_text(fh.read()), indent=2))
