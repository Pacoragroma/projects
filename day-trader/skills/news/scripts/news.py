#!/usr/bin/env python3
"""news - breaking headlines that could move your tickers, tagged and newest first.

Sources (free, no API key), merged and de-duplicated:
  * Yahoo Finance RSS headlines        feeds.finance.yahoo.com
  * Yahoo Finance search news          query1.finance.yahoo.com (query2 as fallback)
  * SEC EDGAR 8-K / 8-K/A / 6-K filings  www.sec.gov + data.sec.gov
One source failing only adds a warning; all of them failing exits 2 ("data error: ...").

Crypto pairs (BTC-USD, ETH-USD, ...) use the same Yahoo feeds for the symbol; SEC EDGAR is not
applicable to them, so it is not asked (shown as "n/a", never as a failure). A run of crypto
symbols only shows times in UTC (crypto days are UTC days); any run with a stock uses ET.

Usage examples:
  python3 scripts/news.py TSLA
  python3 scripts/news.py TSLA NVDA --since 90m --tags EARNINGS,DEAL
  python3 scripts/news.py TSLA --new-only        # only headlines you have not been shown yet
  python3 scripts/news.py TSLA --since 09:30     # since the open today (ET)
  python3 scripts/news.py AAPL --since 2026-10-29 --limit 50 --json
  python3 scripts/news.py BTC-USD ETH-USD --since 12h  # crypto: Yahoo only, times in UTC
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trader_common as tc

import email.utils
import hashlib
import html
import http.client
import re
import textwrap
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Pattern, Sequence, Tuple

RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US"
SEARCH_URL = "https://{host}/v1/finance/search?q={sym}&quotesCount=0&newsCount=20"
SEC_FORMS = {"8-K", "8-K/A", "6-K"}
FEED_TTL = 60            # seconds; news has to be fresh
SEEN_FILE = "news_seen.json"
SEEN_CAP = 500           # remembered keys per symbol
FRESH_MINUTES = 60       # anything this new is HIGH impact
FUTURE_SLACK = 300       # tolerate publisher clocks up to 5 min ahead
DUP_WINDOW = 12 * 3600   # same headline this close in time = same story
MAX_SYMBOLS = 25
SUMMARY_CHARS = 300

SOURCES = ("yahoo-rss", "yahoo-search", "sec-edgar")
SOURCE_LABELS = {"yahoo-rss": "Yahoo RSS", "yahoo-search": "Yahoo search", "sec-edgar": "SEC EDGAR"}

TAGS = ("EARNINGS", "GUIDANCE", "DEAL", "ANALYST", "FDA", "LEGAL", "OFFERING", "INSIDER", "EXEC", "MACRO")
HIGH_TAGS = frozenset({"EARNINGS", "GUIDANCE", "DEAL", "FDA", "OFFERING", "LEGAL"})


# ---------------------------------------------------------------------------
# Tagging (keyword/regex, case-insensitive)
# ---------------------------------------------------------------------------

def _rx(*parts: str) -> Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in parts), re.IGNORECASE)


_NUM = r"\$?\s?[\d.,]+\s*(?:million|billion|mln|bln|[mbk]n?)?"
_ROLE = (r"\b(?:ceo|cfo|coo|cto|cio|chief\s+\w+\s+officer|chair(?:man|woman|person)?|president|"
         r"executives?|directors?|board\s+members?|general\s+counsel|head\s+of\s+\w+)s?\b")
# Role first ("CFO departs"). Generic verbs (names, joins, taps, replace) only count role-second (_HIRE)
# or as "joins X as CFO": "CEO Tim Cook joins Trump", "CEO names three stocks" are not exec changes.
_MOVE = (r"\b(?:appoint\w*|hired|depart\w*|exits?|exited|oust\w*|fired|retir\w*|"
         r"successor|succeed\w*|replaced|replacement|promot\w*|leaves?|leaving|left|quits?)\b")
_HIRE = (r"\b(?:appoints?|appointed|names?|named|hires?|hired|taps?|tapped|promotes?|promoted|ousts?|"
         r"ousted|fires?|fired|picks?|picked|replaces?|replaced|replacing)\b")
_GUIDE_VERB = (r"\b(?:raise[sd]?|raising|cut(?:s|ting)?|lower(?:s|ed|ing)?|boost(?:s|ed)?|hike[sd]?|"
               r"trim(?:s|med)?|slash(?:es|ed)?|reaffirm(?:s|ed)?|reiterate[sd]?|withdraw(?:s|n)?|withdrew|"
               r"lift(?:s|ed)?|ups|upped|narrow(?:s|ed)?|widen(?:s|ed)?|suspend(?:s|ed)?|maintain(?:s|ed)?)\b")
_TRADE_VERB = (r"\b(?:buys?|bought|sells?|sold|purchases?|purchased|unloads?|unloaded|dumps?|dumped|"
               r"offloads?|offloaded|acquires?|acquired)\b")
_DRUG = r"\b(?:drug|therapy|treatment|vaccine|device|biosimilar|medicine|pill|injection)s?\b"
_APPROVE = r"\bapprov(?:al|als|es|ed|ing)\b"
_DEAL_WORD = (r"(?<!trade )(?<!best )(?<!top )(?<!early )(?<!friday )(?<!monday )(?<!day )(?<!holiday )"
              r"(?<!shopping )(?<!tech )(?<!good )(?<!great )(?<!sweet )\bdeals?\b(?!\s+of\s+the\s+day)")

TAG_RULES: Dict[str, Pattern[str]] = {
    "EARNINGS": _rx(
        r"\bearnings?\b", r"\beps\b", r"\brevenues?\b",
        r"\bquarterly\s+(?:results?|reports?|profits?|loss(?:es)?|sales|numbers|earnings)\b",
        r"\b(?:first|second|third|fourth)[- ]quarter\b", r"\bq[1-4]\b", r"\b[1-4]q(?:\d{2})?\b",
        r"\bresults of operations\b",
        r"\b(?:beats?|tops|topped|miss(?:es|ed)?|exceed(?:s|ed)?|falls?\s+short\s+of|fell\s+short\s+of)\b"
        r".{0,40}?\b(?:estimates?|expectations?|consensus|views?|forecasts?|(?<!wall )street|projections?)\b",
        r"\bitem\s*2\.02\b"),
    "GUIDANCE": _rx(
        r"\bguidance\b",
        r"(?<!microsoft )\boutlooks?\b"  # not the e-mail product
        r"(?!\.com|\s+(?:app|apps|e-?mail|mail|calendar|users?|outages?|accounts?|inbox)\b)",
        r"\bforecast(?:s|ed|ing)?\b", r"\bprofit\s+warning\b",
        _GUIDE_VERB + r".{0,30}?\b(?:guidance|outlook|forecasts?|full[- ]year|annual|fy\s?\d{0,4}|view)\b"),
    "DEAL": _rx(
        r"\bmerg(?:e|er|ers|es|ed|ing)\b",
        r"(?<!customer )(?<!user )(?<!subscriber )(?<!data )\bacqui(?:re|res|red|ring|sition|sitions)\b",
        r"\bbuy-?outs?\b", r"\btake-?overs?\b", _DEAL_WORD, r"\bpartnerships?\b",
        r"\bpartner(?:s|ed|ing)?\s+with\b", r"\bteam(?:s|ed)?\s+up\s+with\b", r"\bjoint\s+venture\b",
        r"\btender\s+offer\b",
        r"\b(?:award(?:s|ed)?|wins?|won|lands?|landed|secures?|secured|inks?|inked|signs?|signed)\b"
        r".{0,40}?\bcontracts?\b",
        r"\bcontracts?\s+awards?\b", r"(?<!\bat )\bstake\b", r"\bstakes\s+in\b",
        r"\b(?:agrees?|agreed|offers?|offered|bids?|moves?|plans?|seeks?|talks|looks?|looking)\s+to\s+"
        r"(?:buy|acquire|purchase|merge|take\s+over)\b",
        # "buys Wiz for $32 billion" / "sells unit for $10.5 billion" - but not buybacks or share trades
        r"\b(?:buys?|bought|buying|purchases?|purchased|sells?|sold|selling)\b(?!\s+back\b)"
        r"(?:(?!\b(?:shares|stock|stocks|stake|bitcoin)\b).){0,40}?\bfor\s+(?:about\s+|around\s+|nearly\s+|"
        r"roughly\s+|up\s+to\s+)?\$\s?[\d.,]+\s*(?:million|billion|trillion|mln|bln|[mbt]n?)\b",
        r"\btake[- ]private\b", r"\bgo(?:es|ing)?\s+private\b",
        r"\bitem\s*(?:1\.01|2\.01|5\.01)\b", r"\bmaterial\s+(?:definitive\s+)?agreement\b",
        r"\bacquisition\s+or\s+disposition\b", r"\bchange\s+in\s+control\b"),
    "ANALYST": _rx(
        r"(?<!software )(?<!firmware )(?<!hardware )(?<!system )(?<!network )"
        r"\b(?:up|down)grad(?:e|es|ed|ing)\b(?!\s+cycles?\b)", r"\bprice\s+targets?\b",
        r"\b(?:initiat|resum|start|launch|assum|reinstat|begin|began|pick)\w*\b.{0,25}?\bcoverage\b",
        r"\b(?:overweight|underweight|outperform|underperform|equal[- ]weight|market[- ]perform|"
        r"sector[- ]perform|peer[- ]perform)\b",
        r"\b(?:strong[- ]buy|buy|sell|hold|neutral)\s+rating\b",
        r"\b(?:reiterat|maintain)\w*\b.{0,25}?\b(?:buy|sell|hold|neutral|overweight|underweight|outperform|rating)\b"),
    "FDA": _rx(
        r"\bfda\b",
        r"\bclinical(?:ly)?\b.{0,20}?\b(?:trials?|stud(?:y|ies)|data|holds?|results?|program)\b",
        r"\b(?:late|mid|early)[- ]stage\s+(?:trials?|stud(?:y|ies)|data|testing)\b",
        r"\bphase\s*(?:[123]|i{1,3})(?:\s*[/-]\s*(?:[123]|i{1,3}))?[ab]?\b"
        r"(?=.{0,40}?\b(?:trials?|stud(?:y|ies)|data|results?|readouts?|endpoints?|patients|drug|candidate|"
        r"therapy|treatment|vaccine)\b)",
        r"\b(?:pdufa|biologics\s+license|new\s+drug\s+application|breakthrough\s+therapy|"
        r"fast[- ]track\s+designation|orphan\s+drug|primary\s+endpoint|top-?line\s+(?:data|results))\b",
        _DRUG + r".{0,40}?" + _APPROVE, _APPROVE + r".{0,40}?" + _DRUG),
    "LEGAL": _rx(
        r"\blawsuits?\b", r"\bsu(?:e|es|ed|ing)\b", r"\bprobes?\b", r"\bprobing\b",
        r"\binvestigat(?:e|es|ed|ing|ion|ions)\b",
        r"\b(?:sec|doj|ftc|cftc|finra|prosecutors?|regulators?|attorneys?\s+general)\b.{0,25}?"
        r"\b(?:charg(?:e|es|ed)|fines?|fined|penalt\w*|subpoena\w*|accus\w*|alleg\w*)\b",
        r"\bcharged\s+with\b", r"\bcriminal\b", r"\bindict(?:s|ed|ment|ments)?\b",
        r"\b(?:doj|justice\s+department|department\s+of\s+justice|ftc)\b", r"\bantitrust\b",
        r"\bsettlements?\b", r"\bagree[sd]?\s+to\s+settle\b",
        r"\bsettle[sd]?\b(?=.{0,30}?\b(?:lawsuits?|suits?|cases?|claims?|charges|probes?|investigations?|"
        r"disputes?|litigation|allegations)\b)",
        r"\bclass[- ]action\b", r"\bsubpoena\w*\b", r"\bfraud\w*\b", r"\blitigation\b",
        r"\bcourt\b(?!\s+of\s+public\s+opinion)", r"\bjury\b(?!\s+is\s+(?:still\s+)?out)",
        r"(?:^|[:;,\u2013\u2014-]\s*|\b(?:the|in|by|federal|appeals?|state|lower|higher|district|bankruptcy|"
        r"supreme|top|us|u\.s\.|eu|european|chinese|delaware|multiple|several|two|three|both)\s+)courts\b",
        r"\b(?:jury|court|guilty|trial|million|billion|mln|bln|[\d.,]+[mb]n?)\s+verdicts?\b",
        r"\bverdicts?\s+(?:against|in\s+favor|in\s+(?:\S+\s+){0,3}?(?:trial|case|suit|lawsuit|litigation))\b",
        r"\bfederal\s+judge\b",
        r"\bjudge\b.{0,15}?\b(?:rules?|ruled|blocks?|blocked|orders?|ordered|approves?|approved|rejects?|"
        r"rejected|dismiss\w*|denies|denied|grants?|granted|halts?|halted|toss(?:es|ed)?)\b",
        r"\bbankrupt(?:cy|cies)?\b", r"\bchapter\s+11\b", r"\bitem\s*1\.03\b"),
    "OFFERING": _rx(
        r"\b(?:public|secondary|stock|share|shares|equity|debt|notes?|bonds?|convertible|follow-on|"
        r"registered\s+direct|underwritten|at-the-market|primary|private|rights|warrants?|preferred|units?|"
        r"senior|common\s+stock|initial\s+public|depositary)\s+(?:stock\s+|share\s+)?offerings?\b",
        r"\$\s?[\d.,]+\s*(?:million|billion|mln|bln|[mb]n?)?\s+(?:[\w-]+\s+){0,3}?offerings?\b",
        r"(?<!raises )(?<!raise )(?<!raised )(?<!raising )(?<!hikes )(?<!hiked )(?<!cuts )(?<!lowers )(?<!new )"
        r"\b(?:prices?|priced|pricing|proposed|upsize[sd]?|upsizing|commences?|commenced)\b"
        r"(?:(?!\b(?:ai|cloud|software|products?|services?|subscriptions?|plans?|platforms?|tiers?)\b).){0,40}?"
        r"\bofferings?\b",
        r"\bdilut(?:ion|ive|es|ed|ing)\b", r"\bconvertible\b", r"(?-i:\bATM\b)", r"\bat-the-market\b",
        r"\bshelf\s+(?:registration|offering|filing|prospectus)\b",
        r"\b(?:mixed|universal|files?|filed|filing)\b.{0,15}?\bshelf\b",
        r"\bsecondary\s+(?:share\s+|stock\s+)?(?:sale|placement)\b", r"\bprivate\s+placement\b", r"\bipos?\b",
        r"\b424b\d?\b", r"\bform\s+s-[13]\b", r"\bitem\s*3\.02\b", r"\bunregistered\s+equity\s+sale\b",
        r"\braise[sd]?\s+\$\s?[\d.,]+\s*(?:million|billion|mln|bln|[mb]n?)\b"),
    "INSIDER": _rx(
        r"(?<!business )\binsiders?\b(?!\s+(?:monkey|intelligence)\b)", r"\bform\s*4\b", r"\b10b5-1\b",
        r"\b(?:ceo|cfo|coo|cto|chair(?:man|woman|person)?|directors?|founders?|co-founders?|president|"
        r"executives?|officers?|billionaire)\b.{0,40}?" + _TRADE_VERB + r".{0,40}?\b(?:shares|stock|stake)\b",
        r"\b(?:sells?|sold|buys?|bought|unloads?|unloaded|dumps?|dumped|offloads?|offloaded)\s+"
        r"(?:another\s+|more\s+)?" + _NUM + r"\s+(?:worth\s+of\s+|of\s+|in\s+)?(?:[\w.&'-]+\s+){0,2}?(?:shares|stock)\b"),
    "EXEC": _rx(
        r"\b(?:resigns?|resigned|resignation|steps?\s+down|stepped\s+down|stepping\s+down|steps?\s+aside)\b",
        _ROLE + r".{0,40}?" + _MOVE, _HIRE + r".{0,40}?" + _ROLE,
        r"\bjoin(?:s|ed|ing)?\b.{0,40}?\bas\s+(?:its\s+|the\s+)?(?:new\s+|next\s+|interim\s+|acting\s+)?" + _ROLE,
        r"\bitem\s*5\.02\b", r"\bdirector/officer\s+change\b", r"\bsuccession\b",
        r"\b(?:new|interim|acting)\s+(?:ceo|cfo|coo|cto|chief)\b"),
    "MACRO": _rx(
        r"(?-i:\bFed\b(?!\s+[Uu]p\b))", r"\bfederal\s+reserve\b", r"\bfomc\b", r"\bpowell\b", r"\bcpi\b", r"\bppi\b",
        r"\bpce\b", r"\binflation\b", r"\bjobs\s+report\b", r"\b(?:non-?farm\s+)?payrolls\b",
        r"\bunemployment\s+(?:rate|claims)\b", r"\bjobless\s+claims\b", r"\bgdp\b", r"\btariffs?\b",
        r"\btrade\s+(?:war|wars|talks|tensions|truce|deals?)\b", r"\brecession\b", r"\binterest[- ]rates?\b",
        r"\brate\s+(?:cuts?|hikes?|decision|increases?|reductions?|path)\b",
        r"\b(?:cuts?|hikes?|raises?|holds?|lowers?|keeps?|leaves?|pauses?)\s+(?:interest\s+)?rates\b",
        r"\b(?:treasury|bond)\s+yields?\b", r"\b10-year\s+(?:treasury|yield|note)\b",
        r"\bexport\s+(?:controls?|curbs?|restrictions?|bans?|limits|rules)\b",
        r"\bgovernment\s+shutdown\b", r"\bdebt\s+ceiling\b"),
}


def tag_headline(title: str, summary: str = "") -> List[str]:
    """Topic tags for a headline (+ optional summary), in canonical TAGS order.

    Title and summary are matched separately so a pattern never spans the two.
    """
    parts = [p for p in (title or "", (summary or "")[:600]) if p]
    return [tag for tag in TAGS if any(TAG_RULES[tag].search(p) for p in parts)]


def is_high_impact(tags: Iterable[str], age_minutes: Optional[float]) -> bool:
    """HIGH = a market-moving topic, or anything published within the last hour."""
    return bool(HIGH_TAGS.intersection(tags)) or (age_minutes is not None and age_minutes <= FRESH_MINUTES)


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------

_SINCE_RX = re.compile(r"(\d+(?:\.\d+)?)\s*(m|mins?|minutes?|h|hrs?|hours?|d|days?|w|wks?|weeks?)")


def parse_since(value: str, now: datetime) -> datetime:
    """'90m' / '24h' / '2d' / '1w' / 'today' / '09:30' (today) / '2026-10-29' / '2026-10-29T08:00'.

    Clock times and dates are in the active market's zone (ET; UTC for a crypto-only run)."""
    raw = (value or "").strip()
    v = raw.lower()
    m = _SINCE_RX.fullmatch(v)
    if m:
        n = float(m.group(1))
        if n <= 0:
            raise ValueError(f"--since must be a positive duration, got {raw!r}")
        unit = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[m.group(2)[0]]
        try:
            return now - timedelta(**{unit: n})
        except OverflowError:
            raise ValueError(f"--since {raw} reaches too far back; try something like 1w") from None
    if v == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        if re.fullmatch(r"\d{1,2}:\d{2}", v):  # a clock time means today at that time (ET / UTC)
            dt = tc.parse_et(f"{now:%Y-%m-%d}T{tc.hhmm(tc.parse_hhmm(v))}")
        else:
            dt = tc.parse_et(raw)
    except (ValueError, OverflowError, OSError):
        raise ValueError(f"--since: expected a duration like 90m, 24h, 2d, a clock time like 09:30 or a date "
                         f"like 2026-10-29, got {raw!r}") from None
    if dt > now:
        raise ValueError(f"--since {raw} is in the future (now is {now:%Y-%m-%d %H:%M} {tc.tz_label()})")
    return dt


def humanize_age(minutes: Optional[float]) -> str:
    if minutes is None:
        return "?"
    m = int(max(0.0, minutes))
    if m < 1:
        return "just now"
    if m < 60:
        return f"{m}m ago"
    h, mm = divmod(m, 60)
    if h < 24:
        return f"{h}h {mm:02d}m ago" if mm else f"{h}h ago"
    d, hh = divmod(h, 24)
    return f"{d}d {hh}h ago" if hh else f"{d}d ago"


def parse_pub_date(value: Any) -> Optional[float]:
    """RFC 822 (RSS), ISO 8601 or unix seconds/millis -> unix seconds (None if unparseable)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:  # milliseconds
            v /= 1000.0
        return v if v > 0 else None
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{9,13}", s):
        return parse_pub_date(int(s))
    dt: Optional[datetime] = None
    if re.match(r"\d{4}-\d{2}-\d{2}", s):
        try:
            dt = datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s)
        except ValueError:
            return None
    else:
        try:
            dt = email.utils.parsedate_to_datetime(s)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:  # RFC 822 "-0000" and naive ISO: treat as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def clean_text(value: Any) -> str:
    """Strip HTML tags/entities and collapse whitespace."""
    if value is None:
        return ""
    s = html.unescape(str(value))
    s = re.sub(r"<[^>]{0,500}>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_HOST_PUBLISHERS = {
    "finance.yahoo.com": "Yahoo Finance", "reuters.com": "Reuters", "bloomberg.com": "Bloomberg",
    "barrons.com": "Barron's", "wsj.com": "WSJ", "marketwatch.com": "MarketWatch", "cnbc.com": "CNBC",
    "fool.com": "Motley Fool", "investors.com": "Investor's Business Daily", "seekingalpha.com": "Seeking Alpha",
    "benzinga.com": "Benzinga", "zacks.com": "Zacks", "thestreet.com": "TheStreet", "ft.com": "Financial Times",
    "businesswire.com": "Business Wire", "prnewswire.com": "PR Newswire", "globenewswire.com": "GlobeNewswire",
    "investorplace.com": "InvestorPlace", "insidermonkey.com": "Insider Monkey", "gurufocus.com": "GuruFocus",
    "simplywall.st": "Simply Wall St.", "apnews.com": "AP", "sec.gov": "SEC EDGAR", "nytimes.com": "NYT",
    "techcrunch.com": "TechCrunch", "forbes.com": "Forbes", "fortune.com": "Fortune",
}


def publisher_from_url(url: str) -> str:
    host = (urllib.parse.urlsplit(url or "").hostname or "").lower()
    if not host:
        return ""
    for known, name in _HOST_PUBLISHERS.items():
        if host == known or host.endswith("." + known):
            return name
    return host[4:] if host.startswith("www.") else host


_TRACKING_PARAMS = re.compile(r"^(utm_.*|\.?tsrc|ncid|guccounter|guce_.*|soc_.*|yptr|fr|ref|cmpid|mod|src|siteid)$",
                              re.IGNORECASE)


def normalize_url(url: str) -> str:
    """Scheme-, www-, tracking-param- and trailing-slash-insensitive key for a link."""
    if not url:
        return ""
    parts = urllib.parse.urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""
    query = sorted((k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
                   if not _TRACKING_PARAMS.match(k))
    q = urllib.parse.urlencode(query)
    return host + parts.path.rstrip("/") + ("?" + q if q else "")


def normalize_title(title: str) -> str:
    """Lower-case alphanumerics only, minus a trailing ' - Publisher' / ' | Publisher' suffix."""
    t = clean_text(title).lower()
    stripped = re.sub(r"\s+[-|\u2013\u2014]\s+[^-|\u2013\u2014]{2,40}$", "", t)
    if len(stripped.split()) >= 4:
        t = stripped
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _same_ticker(a: str, b: str) -> bool:
    return tc.yahoo_symbol(a) == tc.yahoo_symbol(b)


def _new_item(symbol: str, source: str, title: str, link: str, ts: Optional[float], publisher: str = "",
              summary: str = "", kind: str = "headline", **extra: Any) -> Dict[str, Any]:
    title = clean_text(title)
    summary = clean_text(summary)
    link = (link or "").strip()
    pub_guess = not publisher
    item = {
        "symbols": [symbol], "sources": [source], "kind": kind, "title": title,
        "summary": summary[:SUMMARY_CHARS], "link": link,
        "publisher": clean_text(publisher) or publisher_from_url(link) or SOURCE_LABELS.get(source, source),
        "_publisher_guess": pub_guess, "ts": ts, "time_known": True,
        "tags": tag_headline(title, summary),
    }
    item.update(extra)
    return item


# ---------------------------------------------------------------------------
# Source parsers (pure)
# ---------------------------------------------------------------------------

def parse_rss(data: Any, symbol: str) -> List[Dict[str, Any]]:
    """Items from a Yahoo Finance RSS 2.0 feed (Atom <entry> tolerated)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    data = (data or b"").lstrip(b"\xef\xbb\xbf").lstrip()
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise tc.FetchError(f"Yahoo RSS for {symbol} is not a valid feed ({e})") from None
    if _local(root.tag) not in ("rss", "feed", "rdf"):
        raise tc.FetchError(f"Yahoo RSS for {symbol} returned <{_local(root.tag)}>, not an RSS feed")
    out: List[Dict[str, Any]] = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        f: Dict[str, str] = {}
        for child in el:
            name = _local(child.tag)
            text = "".join(child.itertext()).strip()  # also reads unescaped <p>/<b> markup inside
            if name == "link" and not text:
                text = (child.get("href") or "").strip()
            if text and name not in f:
                f[name] = text
        title = f.get("title", "")
        if not title or re.search(r"rss feed not found", title, re.IGNORECASE):
            continue
        link = f.get("link") or (f.get("guid", "") if f.get("guid", "").startswith("http") else "")
        ts = parse_pub_date(f.get("pubdate") or f.get("published") or f.get("updated") or f.get("date"))
        out.append(_new_item(symbol, "yahoo-rss", title, link, ts, publisher=f.get("source", ""),
                             summary=f.get("description") or f.get("summary") or ""))
    return out


def parse_search(payload: Any, symbol: str) -> List[Dict[str, Any]]:
    """News items from Yahoo's v1/finance/search response (flat or nested 'content' shape)."""
    if not isinstance(payload, dict):
        raise tc.FetchError(f"Yahoo search for {symbol}: unexpected response")
    news = payload.get("news")
    if news is None:
        err = (payload.get("finance") or {}).get("error") if isinstance(payload.get("finance"), dict) else None
        detail = f"{err.get('code')}: {err.get('description')}" if isinstance(err, dict) else "no 'news' field"
        raise tc.FetchError(f"Yahoo search for {symbol}: {detail}")
    if not isinstance(news, list):
        raise tc.FetchError(f"Yahoo search for {symbol}: 'news' is not a list")
    out: List[Dict[str, Any]] = []
    for n in news:
        if not isinstance(n, dict):
            continue
        c = n.get("content") if isinstance(n.get("content"), dict) else n
        title = c.get("title") or ""
        if not title:
            continue
        related = c.get("relatedTickers") or n.get("relatedTickers") or []
        if not related and isinstance(c.get("finance"), dict):  # newer nested shape
            related = [t.get("symbol") for t in c["finance"].get("stockTickers") or [] if isinstance(t, dict)]
        related = [r for r in related if isinstance(r, str) and r] if isinstance(related, list) else []
        if related and not any(_same_ticker(r, symbol) for r in related):
            continue  # an article about other tickers that matched the text search
        link = c.get("link") or ""
        if not link:
            for k in ("canonicalUrl", "clickThroughUrl"):
                if isinstance(c.get(k), dict) and c[k].get("url"):
                    link = c[k]["url"]
                    break
        publisher = c.get("publisher") or ""
        if not publisher and isinstance(c.get("provider"), dict):
            publisher = c["provider"].get("displayName") or ""
        ts = parse_pub_date(c.get("providerPublishTime") or c.get("pubDate") or c.get("displayTime"))
        out.append(_new_item(symbol, "yahoo-search", title, link, ts, publisher=publisher,
                             summary=c.get("summary") or ""))
    return out


def sec_accept_time(accepted: Optional[str], filed: Optional[str],
                    now: Optional[datetime] = None) -> Tuple[Optional[float], bool]:
    """(unix ts, time_known) for an EDGAR filing.

    EDGAR's acceptanceDateTime ends in 'Z' but carries Eastern wall-clock time (EDGAR runs on
    ET), so the Z is ignored. If that reading lands in the future, it is retried as real UTC.
    Without an acceptance time, the filing date at 00:00 ET is used and time_known is False.
    """
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)", accepted or "")
    if m:
        try:
            et = tc.parse_et(f"{m.group(1)}T{m.group(2)}")
            if now is not None and et.timestamp() > now.timestamp() + FUTURE_SLACK:
                utc = datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}").replace(tzinfo=timezone.utc)
                return utc.timestamp(), True
            return et.timestamp(), True
        except ValueError:
            pass
    if filed and re.fullmatch(r"\d{4}-\d{2}-\d{2}", filed):
        try:
            return tc.parse_et(filed).timestamp(), False
        except ValueError:
            pass
    return None, False


def filing_headline(form: str, items: Sequence[str]) -> str:
    """'8-K: Results of operations (earnings); Reg FD disclosure' (item 9.01 exhibits dropped)."""
    form = form or "?"
    named = [tc.EIGHT_K_ITEMS.get(i, f"Item {i}") for i in items if i != "9.01"]
    if not named and items:
        named = [tc.EIGHT_K_ITEMS.get(i, f"Item {i}") for i in items]
    label = form + (" (amendment)" if form.upper().endswith("/A") else "")
    if form.upper().startswith("6-K"):
        return f"{label}: Foreign issuer report" + (f" - {'; '.join(named)}" if named else "")
    return f"{label}: {'; '.join(named)}" if named else f"{label}: Current report"


def filing_items(filings: Iterable[Dict[str, Any]], symbol: str,
                 now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Turn tc.sec_recent_filings() rows into news items."""
    out: List[Dict[str, Any]] = []
    for f in filings:
        items = [str(i).strip() for i in (f.get("items") or []) if str(i).strip()]
        ts, known = sec_accept_time(f.get("accepted"), f.get("filed"), now)
        summary = "; ".join(f"Item {i}" for i in items)
        desc = clean_text(f.get("description"))
        if desc and desc.upper().replace("FORM ", "") not in (str(f.get("form") or "").upper(), ""):
            summary = f"{summary}; {desc}" if summary else desc
        it = _new_item(symbol, "sec-edgar", filing_headline(str(f.get("form") or ""), items), f.get("url") or "",
                       ts, publisher="SEC EDGAR", summary=summary, kind="filing",
                       form=f.get("form"), items=items, accession=f.get("accession"))
        it["time_known"] = known
        out.append(it)
    return out


# ---------------------------------------------------------------------------
# Merge, filter, rank (pure)
# ---------------------------------------------------------------------------

def _title_key(item: Dict[str, Any]) -> str:
    t = normalize_title(item.get("title", ""))
    if not t:
        return ""
    day = tc.et_date(item["ts"]) if item.get("ts") else ""
    return "t:" + _hash(f"{t}|{day}")


def _url_key(item: Dict[str, Any]) -> str:
    u = normalize_url(item.get("link", ""))
    return "u:" + _hash(u) if u else ""


def _merge_into(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
    for k in ("symbols", "sources", "tags"):
        for v in src[k]:
            if v not in dst[k]:
                dst[k].append(v)
    dst["tags"] = [t for t in TAGS if t in dst["tags"]]
    if src.get("ts") is not None and (dst.get("ts") is None or src["ts"] < dst["ts"]):
        dst["ts"], dst["time_known"] = src["ts"], src.get("time_known", True)
    if dst.get("_publisher_guess") and not src.get("_publisher_guess"):
        dst["publisher"], dst["_publisher_guess"] = src["publisher"], False
    if len(src.get("summary") or "") > len(dst.get("summary") or ""):
        dst["summary"] = src["summary"]
    if src.get("link") and (not dst.get("link") or (
            normalize_url(src["link"]) == normalize_url(dst["link"]) and len(src["link"]) < len(dst["link"]))):
        dst["link"] = src["link"]  # same article: keep the copy without tracking parameters
    dst["_keys"].update(src["_keys"])


def dedupe(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge copies of the same story: same normalized link, or same normalized title within 12h.

    The merged item keeps the earliest time, all symbols/sources/tags, and a real publisher name
    over one guessed from a link host. SEC filings merge only on an identical document URL.
    """
    ordered = sorted(items, key=lambda it: (it.get("ts") is None, it.get("ts") or 0))
    groups: List[Dict[str, Any]] = []
    by_url: Dict[str, int] = {}
    by_title: Dict[str, List[int]] = {}
    for raw in ordered:
        it = dict(raw, symbols=list(raw["symbols"]), sources=list(raw["sources"]), tags=list(raw["tags"]))
        ukey = _url_key(it)
        # Filing headlines are generated ("8-K: Other events") and can repeat for different filings
        # on the same day, so filings are identified by their unique document URL only.
        by_link_only = it.get("kind") == "filing" and bool(ukey)
        tnorm = "" if by_link_only else normalize_title(it.get("title", ""))
        it["_keys"] = {k for k in (ukey, "" if by_link_only else _title_key(it)) if k}
        gi = by_url.get(ukey) if ukey else None
        if gi is None and tnorm:
            for cand in by_title.get(tnorm, []):
                a, b = groups[cand].get("ts"), it.get("ts")
                if a is None or b is None or abs(a - b) <= DUP_WINDOW:
                    gi = cand
                    break
        if gi is None:
            groups.append(it)
            gi = len(groups) - 1
        else:
            _merge_into(groups[gi], it)
        if ukey:
            by_url.setdefault(ukey, gi)
        if tnorm and gi not in by_title.setdefault(tnorm, []):
            by_title[tnorm].append(gi)
    return groups


def clean_seen(data: Any) -> Dict[str, Any]:
    """news_seen.json as loaded -> {"symbols": {SYM: [str keys]}}; anything malformed is dropped."""
    symbols = data.get("symbols") if isinstance(data, dict) else None
    out: Dict[str, List[str]] = {}
    for sym, keys in (symbols.items() if isinstance(symbols, dict) else ()):
        if isinstance(sym, str) and isinstance(keys, list):
            good = [k for k in keys if isinstance(k, str)]
            if good:
                out[sym] = good
    return {"symbols": out}


def is_seen(item: Dict[str, Any], seen: Dict[str, Any]) -> bool:
    store = seen.get("symbols") or {}
    keys = item.get("_keys") or set()
    return any(k in set(store.get(sym) or []) for sym in item["symbols"] for k in keys)


def mark_seen(seen: Dict[str, Any], items: Iterable[Dict[str, Any]], cap: int = SEEN_CAP) -> Dict[str, Any]:
    """Remember each item's keys under each of its symbols; keep the newest `cap` keys per symbol."""
    store = seen.setdefault("symbols", {})
    for it in items:
        for sym in it["symbols"]:
            lst = [k for k in (store.get(sym) or []) if isinstance(k, str)]
            have = set(lst)
            lst.extend(k for k in sorted(it.get("_keys") or ()) if k not in have)
            store[sym] = lst[-cap:]
    return seen


def build_feed(raw: Iterable[Dict[str, Any]], now: datetime, since: datetime,
               tags: Sequence[str] = (), limit: int = 20,
               seen: Optional[Dict[str, Any]] = None,
               symbol_order: Sequence[str] = ()) -> Dict[str, Any]:
    """Window -> dedupe -> annotate (age, impact) -> tag filter -> new-only -> newest first -> limit.

    `tags` filters with OR semantics; the pseudo-tag HIGH matches high-impact items.
    `seen` (the news_seen.json store) drops items already shown; None disables that filter.
    """
    now_ts, since_ts = now.timestamp(), since.timestamp()
    skipped = {"undated": 0, "older": 0, "future": 0, "seen": 0, "tag_filter": 0}
    window: List[Dict[str, Any]] = []
    for it in raw:
        ts = it.get("ts")
        if ts is None:
            skipped["undated"] += 1
        elif ts < since_ts and not (it.get("time_known") is False and ts + 86400 > since_ts):
            skipped["older"] += 1   # a date-only filing counts if its day overlaps the window
        elif ts > now_ts + FUTURE_SLACK:
            skipped["future"] += 1  # after the as-of time (replays) or a broken publisher clock
        else:
            window.append(it)
    merged = dedupe(window)
    rank = {s: i for i, s in enumerate(symbol_order)}
    for it in merged:  # list a story's tickers in the order the user gave them
        it["symbols"].sort(key=lambda s: rank.get(s, len(rank)))
    wanted = {t.upper() for t in tags}
    out: List[Dict[str, Any]] = []
    for it in merged:
        age = max(0.0, (now_ts - it["ts"]) / 60.0)
        it["age_minutes"] = int(age)
        it["fresh"] = it.get("time_known", True) and age <= FRESH_MINUTES
        high = is_high_impact(it["tags"], age if it.get("time_known", True) else None)
        it["impact"] = "HIGH" if high else "normal"
        if wanted and not (wanted.intersection(it["tags"]) or ("HIGH" in wanted and high)):
            skipped["tag_filter"] += 1
            continue
        if seen is not None and is_seen(it, seen):
            skipped["seen"] += 1
            continue
        out.append(it)
    out.sort(key=lambda it: (-it["ts"], it["symbols"][0], it["title"]))
    return {"items": out[:max(0, limit)], "total": len(out), "skipped": skipped}


def public_item(it: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """JSON-safe view of an item (drops internals, adds ET time fields)."""
    d = tc.to_et(it["ts"])
    keys = sorted(it.get("_keys") or (), key=lambda k: (not k.startswith("u:"), k))
    out = {
        "id": (keys[0] if keys else _hash(it["title"])).split(":", 1)[-1],
        "symbol": it["symbols"][0], "symbols": it["symbols"], "title": it["title"],
        "publisher": it["publisher"], "link": it["link"],
        "published": d.isoformat(timespec="seconds"), "published_ts": int(it["ts"]),
        "date_et": d.strftime("%Y-%m-%d"), "time_et": d.strftime("%H:%M") if it.get("time_known", True) else None,
        "age_minutes": it.get("age_minutes"),
        "ago": humanize_age(it.get("age_minutes")) if it.get("time_known", True) else "date only",
        "tags": it["tags"], "impact": it.get("impact"), "fresh": bool(it.get("fresh")),
        "kind": it.get("kind", "headline"), "sources": it["sources"], "summary": it.get("summary", ""),
    }
    for k in ("form", "items", "accession"):
        if it.get(k) is not None:
            out[k] = it[k]
    return out


# ---------------------------------------------------------------------------
# Fetching (thin I/O; every request goes through trader_common)
# ---------------------------------------------------------------------------

def _q(symbol: str) -> str:
    return urllib.parse.quote(tc.yahoo_symbol(symbol), safe="")


def rss_url(symbol: str) -> str:
    return RSS_URL.format(sym=_q(symbol))


def search_url(symbol: str, host: str = tc.YAHOO_HOSTS[0]) -> str:
    return SEARCH_URL.format(host=host, sym=_q(symbol))


def _guarded(url: str, fn: Callable[[], Any]) -> Any:
    """Run one fetch+parse step; anything that goes wrong becomes a FetchError naming `url`.

    tc.http_get turns most network trouble into FetchError, but a body cut off mid-read
    (http.client.IncompleteRead) or a truncated gzip stream (EOFError) still escapes it, and a
    response of the wrong shape raises KeyError/TypeError/... in the parsers. Either way only
    this one source should fail, not the whole run.
    """
    try:
        return fn()
    except tc.FetchError:
        raise
    except (EOFError, http.client.HTTPException) as e:
        raise tc.FetchError(f"incomplete response from {url} ({type(e).__name__}: {e})") from e
    except (KeyError, TypeError, AttributeError, ValueError, IndexError) as e:
        raise tc.FetchError(f"unexpected response from {url} ({type(e).__name__}: {e})") from e


def fetch_rss(symbol: str) -> List[Dict[str, Any]]:
    url = rss_url(symbol)
    return _guarded(url, lambda: parse_rss(tc.cached_get(url, FEED_TTL), symbol))


def fetch_search(symbol: str) -> List[Dict[str, Any]]:
    first: Optional[tc.FetchError] = None
    for host in tc.YAHOO_HOSTS:
        url = search_url(symbol, host)
        try:
            return _guarded(url, lambda: parse_search(tc.get_json(url, FEED_TTL), symbol))
        except tc.FetchError as e:
            first = first or e
            if "HTTP 404" in str(e):
                break
    raise first if first else tc.FetchError(f"Yahoo search for {symbol} failed")


def _cik_for(symbol: str, ticker_map: Dict[str, Dict[str, str]]) -> Optional[str]:
    t = symbol.strip().upper()
    for cand in (t, t.replace("-", "."), t.replace(".", "-")):
        if cand in ticker_map:
            return ticker_map[cand]["cik"]
    return None


def fetch_sec(symbols: Sequence[str], since_date: str, now: datetime) -> List[Dict[str, Any]]:
    """One sequential pass over SEC for all symbols (polite to EDGAR's rate limit).

    Returns status rows: {source, symbols, status ok|failed|n/a, items, error}.
    """
    try:
        tmap = _guarded(tc.SEC_TICKERS_URL, tc.sec_ticker_map)
    except tc.FetchError as e:
        return [{"source": "sec-edgar", "symbols": list(symbols), "status": "failed", "items": [], "error": str(e)}]
    rows: List[Dict[str, Any]] = []
    by_cik: Dict[str, List[str]] = {}
    for s in symbols:
        cik = _cik_for(s, tmap)
        if cik:
            by_cik.setdefault(cik, []).append(s)
        else:  # ETFs, indexes, some foreign listings: no EDGAR filer - not a failure
            rows.append({"source": "sec-edgar", "symbols": [s], "status": "n/a", "items": [],
                         "error": f"{s} is not in SEC's ticker list"})
    caching = not (os.environ.get("DAYTRADER_FIXTURES") or os.environ.get("DAYTRADER_NO_CACHE"))
    # The ET-vs-UTC fallback in sec_accept_time only makes sense against the real clock; in a
    # $DAYTRADER_NOW replay a later filing is legitimately "in the future" and must stay so.
    live_now = None if os.environ.get("DAYTRADER_NOW") else now
    for cik, syms in by_cik.items():
        url = f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"
        try:
            if caching:
                # tc.sec_recent_filings reads submissions with a 15-minute cache; refresh it at FEED_TTL
                # first so a just-filed 8-K is not hidden behind a stale copy.
                _guarded(url, lambda: tc.sec_submissions(cik, ttl=FEED_TTL))
            filings = _guarded(url, lambda: tc.sec_recent_filings(syms[0], forms=SEC_FORMS, limit=40,
                                                                  since=since_date))
            items: List[Dict[str, Any]] = []
            for s in syms:
                items.extend(_guarded(url, lambda s=s: filing_items(filings, s, live_now)))
            rows.append({"source": "sec-edgar", "symbols": syms, "status": "ok", "items": items, "error": None})
        except (tc.FetchError, OSError) as e:
            rows.append({"source": "sec-edgar", "symbols": syms, "status": "failed", "items": [], "error": str(e)})
    return rows


def news_market(symbols: Sequence[str]) -> tc.Market:
    """The market whose clock a run uses: crypto when every symbol is a crypto pair, else US."""
    return tc.CRYPTO if symbols and all(tc.is_crypto_symbol(s) for s in symbols) else tc.US


def sec_not_applicable(symbol: str) -> Dict[str, Any]:
    """Status row for a crypto pair: SEC EDGAR has no filer for it, so it is not asked."""
    return {"source": "sec-edgar", "symbols": [symbol], "status": "n/a", "items": [],
            "error": f"{symbol} is a crypto pair - SEC EDGAR does not apply"}


def gather(symbols: Sequence[str], since: datetime, now: datetime,
           workers: int = 8) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Fetch every source for every symbol concurrently. Returns (raw items, status rows).

    Crypto pairs get the Yahoo feeds only: SEC EDGAR is not asked for them (status "n/a").
    Raises FetchError only when not a single source answered for any symbol.
    """
    def run_one(source: str, sym: str, fn: Callable[[str], List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        try:
            got = fn(sym)
            return [{"source": source, "symbols": [sym], "status": "ok", "items": got, "error": None}]
        except (tc.FetchError, OSError) as e:
            return [{"source": source, "symbols": [sym], "status": "failed", "items": [], "error": str(e)}]

    jobs: List[Callable[[], List[Dict[str, Any]]]] = []
    for s in symbols:
        jobs.append(lambda s=s: run_one("yahoo-rss", s, fetch_rss))
        jobs.append(lambda s=s: run_one("yahoo-search", s, fetch_search))
    stocks = [s for s in symbols if not tc.is_crypto_symbol(s)]
    if stocks:
        jobs.append(lambda: fetch_sec(stocks, since.strftime("%Y-%m-%d"), now))
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs)))) as ex:
        results = [row for rows in ex.map(lambda job: job(), jobs) for row in rows]
    results += [sec_not_applicable(s) for s in symbols if tc.is_crypto_symbol(s)]
    if not any(r["status"] == "ok" for r in results):
        firsts: Dict[str, str] = {}
        for r in results:
            if r["status"] == "failed":
                firsts.setdefault(r["source"], r["error"])
        detail = "; ".join(f"{SOURCE_LABELS[k]}: {v}" for k, v in firsts.items()) or "no source answered"
        raise tc.FetchError(f"all news sources failed - {detail}")
    raw = [it for r in results for it in r["items"]]
    return raw, results


def source_summary(rows: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Per-source status for output + human warnings for failures."""
    summary: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for src in SOURCES:
        mine = [r for r in rows if r["source"] == src]
        if not mine:
            continue
        ok = [s for r in mine if r["status"] == "ok" for s in r["symbols"]]
        failed = {s: r["error"] for r in mine if r["status"] == "failed" for s in r["symbols"]}
        na = [s for r in mine if r["status"] == "n/a" for s in r["symbols"]]
        summary.append({"source": src, "ok": ok, "failed": failed, "not_applicable": na,
                        "items": sum(len(r["items"]) for r in mine)})
        if failed:
            errs = sorted(set(failed.values()))
            warnings.append(f"{SOURCE_LABELS[src]} failed for {', '.join(failed)}: {errs[0]}"
                            + (f" (+{len(errs) - 1} other errors)" if len(errs) > 1 else ""))
    return summary, warnings


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _when(item: Dict[str, Any], now: datetime, tz: str = "ET") -> str:
    d = datetime.fromisoformat(item["published"])
    if item.get("time_et") is None:
        return d.strftime("%b %d") + f" {tz}"
    if item["date_et"] == now.strftime("%Y-%m-%d"):
        return d.strftime("%H:%M") + f" {tz}"
    return d.strftime("%b %d %H:%M") + f" {tz}"


def render(res: Dict[str, Any]) -> str:
    now = datetime.fromisoformat(res["as_of"])
    tz = res.get("tz") or "ET"
    items = res["items"]
    head = f"NEWS · {' '.join(res['symbols'])} · {res['since_label']}"
    if res["new_only"]:
        head += " · new since last check"
    if res["tags_filter"]:
        head += " · tags " + ",".join(res["tags_filter"])
    lines = [head]
    if not items:
        what = "No new headlines" if res["new_only"] else "No headlines"
        notes = []
        if res["tags_filter"]:  # say so, or "No headlines" reads as "no news at all"
            what += " tagged " + ",".join(res["tags_filter"])
            n = res["skipped"].get("tag_filter") or 0
            if n:
                notes.append(f"{n} other{'s' if n != 1 else ''} filtered out by --tags")
        if res["new_only"] and res["skipped"]["seen"]:
            notes.insert(0, f"{res['skipped']['seen']} already seen")
        extra = f" ({'; '.join(notes)})" if notes else ""
        window = ("in the " if res["since_label"].startswith("last ") else "") + res["since_label"]
        lines.append(f"{what} for {', '.join(res['symbols'])} {window}{extra}.")
    else:
        high = sum(1 for it in items if it["impact"] == "HIGH")
        more = f" (newest {len(items)} of {res['total']}; raise --limit for more)" if res["total"] > len(items) else ""
        lines.append(f"{len(items)} headline{'s' if len(items) != 1 else ''}, {high} high impact{more}"
                     f" · as of {now:%a %b %d %H:%M} {tz}")
        when_w = max(len(_when(it, now, tz)) for it in items)
        ago_w = max(len(it["ago"]) for it in items)
        sym_w = max(len(",".join(it["symbols"])) for it in items)
        for it in items:
            lines.append("")
            meta = [it["publisher"]] + ([" ".join(it["tags"])] if it["tags"] else [])
            lines.append(f"{'HIGH' if it['impact'] == 'HIGH' else '    '}  {_when(it, now, tz).ljust(when_w)}  "
                         f"{it['ago'].ljust(ago_w)}  {','.join(it['symbols']).ljust(sym_w)}  {' · '.join(meta)}")
            lines.extend(textwrap.wrap(it["title"], width=96, initial_indent="      ", subsequent_indent="      "))
            if it["link"]:
                lines.append("      " + it["link"])
    lines.append("")
    status = []
    for s in res["sources"]:
        n_sym = len(s["ok"]) + len(s["failed"]) + len(s["not_applicable"])
        if s["failed"] and not s["ok"]:
            st = "FAILED"
        elif s["failed"]:
            st = f"partial {len(s['ok'])}/{n_sym - len(s['not_applicable'])}"
        elif not s["ok"]:
            st = "n/a"
        else:
            st = "ok"
        status.append(f"{SOURCE_LABELS[s['source']]} {st}")
    lines.append("Sources: " + " · ".join(status))
    for w in res["warnings"]:
        lines.append(f"! {w}")
    lines.append("Tags are keyword guesses and HIGH = big topic or under 60 min old - read the story "
                 "before acting. Not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_SYMBOL_RX = re.compile(r"\^?[A-Z0-9][A-Z0-9.\-=]{0,14}")


def parse_symbols(values: Sequence[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for v in values:
        for s in str(v).split(","):
            s = s.strip().upper()
            if not s:
                continue
            if not _SYMBOL_RX.fullmatch(s):
                raise ValueError(f"not a ticker symbol: {s!r}")
            if tc.yahoo_symbol(s) not in seen:
                seen.add(tc.yahoo_symbol(s))
                out.append(s)
    if not out:
        raise ValueError("give at least one ticker, e.g. news.py TSLA")
    if len(out) > MAX_SYMBOLS:
        raise ValueError(f"at most {MAX_SYMBOLS} tickers per run (got {len(out)}); split the list")
    return out


def parse_tags(value: str) -> List[str]:
    tags = [t.strip().upper() for t in (value or "").split(",") if t.strip()]
    bad = [t for t in tags if t not in TAGS and t != "HIGH"]
    if bad:
        raise ValueError(f"unknown tag(s) {', '.join(bad)}; choose from {', '.join(TAGS)} (or HIGH)")
    return list(dict.fromkeys(tags))


def build_parser():
    p = tc.make_parser("Breaking headlines that could move your tickers: Yahoo Finance news + SEC 8-K/6-K "
                       "filings (stocks only; crypto pairs like BTC-USD get Yahoo news), merged, de-duplicated, "
                       "tagged and newest first.")
    p.add_argument("symbols", nargs="+", metavar="SYMBOL",
                   help="one or more tickers or crypto pairs (TSLA NVDA, TSLA,NVDA or BTC-USD)")
    p.add_argument("--since", default="24h",
                   help="how far back: 90m, 24h (default), 2d, 1w, today, a clock time today like 09:30, "
                        "or a date/time like 2026-10-29 or 2026-10-29T08:00 (ET; UTC for crypto-only runs)")
    p.add_argument("--limit", type=int, default=20, help="max headlines to show (default 20)")
    p.add_argument("--tags", default="",
                   help=f"only these tags, comma-separated (any match): {','.join(TAGS)} or HIGH")
    p.add_argument("--new-only", action="store_true",
                   help="only headlines not shown by an earlier --new-only run (remembered in news_seen.json)")
    return p   # tc's parser: usage errors exit 1 (2 means "data source down" here)


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:  # never crash on a headline the console cannot encode
        sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    args = build_parser().parse_intermixed_args(argv)
    symbols = parse_symbols(args.symbols)
    tags = parse_tags(args.tags)
    if args.limit < 1:
        raise ValueError("--limit must be at least 1")
    mkt = tc.set_market(news_market(symbols))   # before now_et: $DAYTRADER_NOW is read in its zone
    now = tc.now_et()
    since = parse_since(args.since, now)
    rel = _SINCE_RX.fullmatch(args.since.strip().lower())
    since_label = (f"last {args.since.strip()}" if rel
                   else f"since {since:%a %b %d %H:%M} {mkt.tz_label}")

    seen = None
    if args.new_only:
        seen = clean_seen(tc.load_json(SEEN_FILE, {}))

    raw, rows = gather(symbols, since, now)
    feed = build_feed(raw, now, since, tags=tags, limit=args.limit, seen=seen, symbol_order=symbols)
    if seen is not None:
        mark_seen(seen, feed["items"])
        seen["updated_at"] = tc.iso_now()
        tc.save_json(SEEN_FILE, seen)

    sources, warnings = source_summary(rows)
    res = {
        "symbols": symbols, "market": mkt.key, "tz": mkt.tz_label, "as_of": now.isoformat(timespec="seconds"),
        "since": since.isoformat(timespec="seconds"), "since_label": since_label,
        "new_only": bool(args.new_only), "tags_filter": tags,
        "total": feed["total"], "shown": len(feed["items"]),
        "high_impact": sum(1 for it in feed["items"] if it["impact"] == "HIGH"),
        "items": [public_item(it, now) for it in feed["items"]],
        "skipped": feed["skipped"], "sources": sources, "warnings": warnings,
        "disclaimer": "Headlines and keyword tags only - not trading signals. Not financial advice.",
    }
    if args.json:
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)
    tc.emit(res, args.json, render)
    return 0


if __name__ == "__main__":
    tc.run(main)
