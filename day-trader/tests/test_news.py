"""Tests for skills/news/scripts/news.py (offline, synthetic data only)."""
import json
import os
import sys
import time
import unittest
from datetime import timedelta
from pathlib import Path

# tests/ is not a package: make `import helpers` work for `python3 -m unittest tests.test_news` too.
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import helpers  # noqa: E402
from helpers import TempHomeTestCase, et_ts, fake_http, run_cli, skill_script, tc, write_fixture  # noqa: E402

news = helpers.load_skill_module("news", "news")
SCRIPT = skill_script("news", "news.py")
NOW = "2026-10-30T10:15"


def now_dt():
    return tc.parse_et(NOW)


def ts(day, hhmm):
    return float(et_ts(day, hhmm))


def item(title, when, symbol="TSLA", source="yahoo-rss", link="", publisher="", summary="", kind="headline"):
    return news._new_item(symbol, source, title, link, when, publisher=publisher, summary=summary, kind=kind)


def rss(*items, symbol="TSLA"):
    body = []
    for title, link, pub, extra in items:
        body.append(f"<item><title>{title}</title><link>{link}</link><pubDate>{pub}</pubDate>{extra}</item>")
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>'
            f"<description>Latest Financial News for {symbol}</description>"
            f"<link>http://finance.yahoo.com/q/h?s={symbol}</link>"
            + "".join(body) + "</channel></rss>")


RSS_TSLA = rss(
    ("Tesla beats third-quarter estimates, raises delivery outlook",
     "https://finance.yahoo.com/news/tesla-beats-140200123.html?.tsrc=rss", "Fri, 30 Oct 2026 14:02:00 +0000",
     "<description>&lt;p&gt;Shares jump &amp;amp; more.&lt;/p&gt;</description><guid isPermaLink=\"false\">g1</guid>"),
    ("Tesla-SpaceX Merger Mania Strikes Wall Street Ahead of Earnings",
     "https://www.barrons.com/articles/tesla-spacex-51234?mod=rss", "Fri, 30 Oct 2026 12:04:00 +0000",
     "<source url=\"https://www.barrons.com\">Barron's</source>"),
    ("Tesla Stock: What to Watch This Week", "https://finance.yahoo.com/news/old-1.html",
     "Mon, 26 Oct 2026 13:15:00 +0000", ""),
)

SEARCH_TSLA = {"explains": [], "count": 3, "quotes": [], "news": [
    {"uuid": "u1", "title": "Tesla beats third-quarter estimates, raises delivery outlook", "publisher": "Reuters",
     "link": "https://finance.yahoo.com/news/tesla-beats-140200123.html",
     "providerPublishTime": et_ts("2026-10-30", "10:02"), "type": "STORY", "relatedTickers": ["TSLA"]},
    {"uuid": "u2", "title": "Morgan Stanley upgrades Tesla to Overweight", "publisher": "Investor's Business Daily",
     "link": "https://finance.yahoo.com/m/abc/ms-upgrades-tesla.html",
     "providerPublishTime": et_ts("2026-10-30", "09:41"), "type": "STORY", "relatedTickers": ["TSLA", "MS"]},
    {"uuid": "u3", "title": "Rivian lands Amazon van contract", "publisher": "Bloomberg",
     "link": "https://finance.yahoo.com/news/rivian.html",
     "providerPublishTime": et_ts("2026-10-30", "09:00"), "type": "STORY", "relatedTickers": ["RIVN", "AMZN"]},
]}

SEC_TICKERS = {"0": {"cik_str": 1318605, "ticker": "TSLA", "title": "Tesla, Inc."},
               "1": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"}}
SEC_SUBS = {"cik": "1318605", "filings": {"recent": {
    "accessionNumber": ["0001628280-26-071234", "0001628280-26-070001", "0000950170-26-001111"],
    "filingDate": ["2026-10-29", "2026-10-29", "2026-10-20"],
    "acceptanceDateTime": ["2026-10-29T16:05:12.000Z", "2026-10-29T08:00:00.000Z", "2026-10-20T17:01:00.000Z"],
    "form": ["8-K", "4", "8-K"],
    "items": ["2.02,9.01", "", "5.02"],
    "primaryDocument": ["tsla-20261029.htm", "form4.xml", "tsla-8k.htm"],
    "primaryDocDescription": ["8-K", "FORM 4", "8-K"]}}}
SUBS_URL_TSLA = "https://data.sec.gov/submissions/CIK0001318605.json"


class TagTests(unittest.TestCase):
    POSITIVE = {
        "Tesla-SpaceX Merger Mania Strikes Wall Street Ahead of Earnings": {"EARNINGS", "DEAL"},
        "Tesla beats third-quarter estimates, raises delivery outlook": {"EARNINGS", "GUIDANCE"},
        "Snap shares sink after Q3 EPS miss": {"EARNINGS"},
        "Intel cuts full-year guidance": {"GUIDANCE"},
        "Morgan Stanley upgrades Tesla to Overweight, raises price target to $450": {"ANALYST"},
        "Goldman Sachs initiates coverage of Rivian with Neutral rating": {"ANALYST"},
        "Moderna's Phase 3 flu vaccine trial meets primary endpoint": {"FDA"},
        "FDA approves Vertex's non-opioid pain drug": {"FDA"},
        "DOJ sues Apple in antitrust case": {"LEGAL"},
        "NHTSA opens probe into Tesla FSD crashes": {"LEGAL"},
        "Boeing agrees to settle lawsuit over 737 MAX": {"LEGAL"},
        "Plug Power announces $500 million public offering": {"OFFERING"},
        "AMC files mixed shelf, launches ATM program": {"OFFERING"},
        "Lucid prices $1.1 billion convertible notes offering": {"OFFERING"},
        "Musk sells $5 billion worth of Tesla shares": {"INSIDER"},
        "Nvidia CEO Jensen Huang sells 240,000 shares under 10b5-1 plan": {"INSIDER"},
        "Starbucks CEO steps down; company names new chief executive": {"EXEC"},
        "Intel names Lip-Bu Tan as CEO": {"EXEC"},
        "Fed holds rates steady, Powell signals patience": {"MACRO"},
        "Nvidia faces new export controls on H20 chips": {"MACRO"},
        "Lockheed wins $2.3 billion Pentagon contract": {"DEAL"},
        "Berkshire trims stake in Bank of America": {"DEAL"},
    }
    NEGATIVE = [
        "Tesla shares rise as Musk touts robotaxi",
        "Board approves 10-for-1 stock split",        # 'approval' is not FDA without drug context
        "Tesla plans phase 2 of Gigafactory Texas expansion",
        "Oil settles higher as supply fears grow",    # 'settles' is not LEGAL without legal context
        "Salesforce launches new AI offering for banks",
        "Tesla's future at stake as robotaxi launches",
        "Fed up with delays, investors dump shares",
        "FedEx shares jump",
        "Tesla rolls out FSD v14 software upgrade",
        "Tesla buys back $10 billion of stock",
        "Nvidia tops $5 trillion market value",
    ]

    def test_positive_headlines(self):
        for title, want in self.POSITIVE.items():
            self.assertEqual(set(news.tag_headline(title)), want, title)

    def test_negative_headlines(self):
        for title in self.NEGATIVE:
            self.assertEqual(news.tag_headline(title), [], title)

    def test_case_insensitive_and_canonical_order(self):
        self.assertEqual(news.tag_headline("ACME RAISES GUIDANCE AFTER EARNINGS BEAT"), ["EARNINGS", "GUIDANCE"])
        self.assertEqual(news.tag_headline("ACME (ATM) program"), ["OFFERING"])
        self.assertEqual(news.tag_headline("cash at the atm"), [])  # ATM is case-sensitive

    def test_summary_is_tagged_separately(self):
        self.assertEqual(news.tag_headline("Tesla stock moves", "Ahead of earnings next week"), ["EARNINGS"])
        self.assertEqual(news.tag_headline("", ""), [])

    def test_8k_items_in_summary(self):
        self.assertEqual(news.tag_headline("8-K: Material agreement", "Item 1.01; Item 9.01"), ["DEAL"])
        self.assertEqual(news.tag_headline("8-K: Director/officer change", "Item 5.02"), ["EXEC"])
        self.assertEqual(news.tag_headline("8-K: Unregistered equity sale", "Item 3.02"), ["OFFERING"])
        self.assertEqual(news.tag_headline("8-K: Bankruptcy", "Item 1.03"), ["LEGAL"])
        self.assertIn("EARNINGS", news.tag_headline("8-K: Results of operations (earnings)", "Item 2.02"))

    def test_high_impact(self):
        self.assertTrue(news.is_high_impact(["EARNINGS"], 600))
        self.assertTrue(news.is_high_impact(["ANALYST"], 59))      # fresh
        self.assertFalse(news.is_high_impact(["ANALYST"], 61))
        self.assertFalse(news.is_high_impact([], None))
        for t in ("GUIDANCE", "DEAL", "FDA", "OFFERING", "LEGAL"):
            self.assertTrue(news.is_high_impact([t], 1e6), t)
        for t in ("INSIDER", "EXEC", "MACRO"):
            self.assertFalse(news.is_high_impact([t], 1e6), t)


class SinceAndTimeTests(unittest.TestCase):
    def test_parse_since(self):
        now = now_dt()
        self.assertEqual(now - news.parse_since("24h", now), timedelta(hours=24))
        self.assertEqual(now - news.parse_since("90m", now), timedelta(minutes=90))
        self.assertEqual(now - news.parse_since("2d", now), timedelta(days=2))
        self.assertEqual(now - news.parse_since("1w", now), timedelta(weeks=1))
        self.assertEqual(now - news.parse_since("1.5 hours", now), timedelta(minutes=90))
        d = news.parse_since("2026-10-29", now)
        self.assertEqual((d.strftime("%Y-%m-%d %H:%M"), d.utcoffset()), ("2026-10-29 00:00", timedelta(hours=-4)))
        self.assertEqual(news.parse_since("2026-10-30T08:00", now).strftime("%H:%M"), "08:00")
        self.assertEqual(news.parse_since("today", now).strftime("%Y-%m-%d %H:%M"), "2026-10-30 00:00")
        for bad in ("30", "0h", "-2d", "yesterday-ish", "", "2026-11-02"):
            with self.assertRaises(ValueError, msg=bad):
                news.parse_since(bad, now)

    def test_humanize_age(self):
        self.assertEqual(news.humanize_age(0.4), "just now")
        self.assertEqual(news.humanize_age(12), "12m ago")
        self.assertEqual(news.humanize_age(131), "2h 11m ago")
        self.assertEqual(news.humanize_age(180), "3h ago")
        self.assertEqual(news.humanize_age(60 * 27), "1d 3h ago")
        self.assertEqual(news.humanize_age(-5), "just now")

    def test_parse_pub_date(self):
        utc = tc.parse_et("2026-10-30T08:04").timestamp()  # 12:04 UTC == 08:04 EDT
        for v in ("Fri, 30 Oct 2026 12:04:00 +0000", "Fri, 30 Oct 2026 12:04:00 GMT",
                  "Fri, 30 Oct 2026 08:04:00 -0400", "Fri, 30 Oct 2026 12:04:00 -0000",
                  "2026-10-30T12:04:00Z", "2026-10-30T12:04:00.000Z", "2026-10-30T08:04:00-04:00",
                  int(utc), int(utc) * 1000, str(int(utc))):
            self.assertAlmostEqual(news.parse_pub_date(v), utc, msg=repr(v), delta=0.001)
        for bad in (None, "", "garbage", "Fri, 99 Foo", True, 0):
            self.assertIsNone(news.parse_pub_date(bad), repr(bad))

    def test_normalizers(self):
        self.assertEqual(news.normalize_url("https://www.Barrons.com/articles/x/?mod=rss&utm_source=y"),
                         news.normalize_url("http://barrons.com/articles/x"))
        self.assertNotEqual(news.normalize_url("https://ex.com/a?id=1"), news.normalize_url("https://ex.com/a?id=2"))
        self.assertEqual(news.normalize_title("Tesla Beats Estimates, Shares Jump - Reuters"),
                         news.normalize_title("Tesla beats estimates; shares jump"))
        self.assertEqual(news.publisher_from_url("https://www.reuters.com/x"), "Reuters")
        self.assertEqual(news.publisher_from_url("https://news.example.org/x"), "news.example.org")
        self.assertEqual(news.clean_text("<p>A &amp; B</p>\n  c"), "A & B c")


class ParserTests(unittest.TestCase):
    def test_parse_rss(self):
        items = news.parse_rss(RSS_TSLA, "TSLA")
        self.assertEqual(len(items), 3)
        a, b = items[0], items[1]
        self.assertEqual(a["title"], "Tesla beats third-quarter estimates, raises delivery outlook")
        self.assertEqual(a["summary"], "Shares jump & more.")
        self.assertEqual(a["publisher"], "Yahoo Finance")       # guessed from the link host
        self.assertTrue(a["_publisher_guess"])
        self.assertEqual(tc.et_hhmm(a["ts"]), "10:02")
        self.assertEqual(a["tags"], ["EARNINGS", "GUIDANCE"])
        self.assertEqual(b["publisher"], "Barron's")             # from <source>
        self.assertFalse(b["_publisher_guess"])
        self.assertEqual(a["symbols"], ["TSLA"])
        self.assertEqual(a["sources"], ["yahoo-rss"])

    def test_parse_rss_tolerance(self):
        weird = ("﻿  " + rss(
            ("Yahoo! Finance: RSS feed not found", "http://finance.yahoo.com", "Fri, 30 Oct 2026 12:00:00 +0000", ""),
            ("<![CDATA[Acme & Co. wins contract]]>", "https://ex.com/a", "not a date", ""),
            ("No link story", "", "Fri, 30 Oct 2026 12:00:00 +0000", ""),
        ))
        items = news.parse_rss(weird, "ACME")
        self.assertEqual([i["title"] for i in items], ["Acme & Co. wins contract", "No link story"])
        self.assertIsNone(items[0]["ts"])
        self.assertEqual(items[1]["link"], "")
        atom = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom story</title>'
                '<link href="https://ex.com/atom"/><updated>2026-10-30T12:00:00Z</updated></entry></feed>')
        got = news.parse_rss(atom.encode(), "X")
        self.assertEqual((got[0]["title"], got[0]["link"]), ("Atom story", "https://ex.com/atom"))
        self.assertEqual(news.parse_rss(rss(), "X"), [])

    def test_parse_rss_rejects_non_feeds(self):
        for body in ("<html><body>Consent</body></html>", "<<not xml", "", b"{\"json\": true}"):
            with self.assertRaises(tc.FetchError):
                news.parse_rss(body, "TSLA")

    def test_parse_search(self):
        items = news.parse_search(SEARCH_TSLA, "TSLA")
        self.assertEqual([i["title"][:12] for i in items], ["Tesla beats ", "Morgan Stanl"])  # RIVN/AMZN dropped
        self.assertEqual(items[0]["publisher"], "Reuters")
        self.assertEqual(items[0]["ts"], et_ts("2026-10-30", "10:02"))
        self.assertEqual(items[1]["tags"], ["ANALYST"])

    def test_parse_search_shapes_and_errors(self):
        nested = {"news": [{"id": "x", "content": {
            "title": "Acme to acquire Widget Co", "pubDate": "2026-10-30T13:00:00Z",
            "provider": {"displayName": "Business Wire"}, "canonicalUrl": {"url": "https://ex.com/acme"},
            "finance": {"stockTickers": [{"symbol": "BRK-B"}]}}},
            {"title": "no tickers listed", "link": "https://ex.com/2", "providerPublishTime": 1793368920},
            {"title": "", "link": "https://ex.com/3"}, "junk"]}
        items = news.parse_search(nested, "BRK.B")
        self.assertEqual(len(items), 2)
        self.assertEqual((items[0]["publisher"], items[0]["link"], items[0]["tags"]),
                         ("Business Wire", "https://ex.com/acme", ["DEAL"]))
        self.assertEqual(news.parse_search({"news": []}, "X"), [])
        with self.assertRaises(tc.FetchError):
            news.parse_search({"finance": {"result": None, "error": {"code": "Bad Request", "description": "x"}}}, "X")
        for bad in ([], {"news": "nope"}, None):
            with self.assertRaises(tc.FetchError):
                news.parse_search(bad, "X")


class SecTests(unittest.TestCase):
    def test_headlines(self):
        self.assertEqual(news.filing_headline("8-K", ["2.02", "9.01"]), "8-K: Results of operations (earnings)")
        self.assertEqual(news.filing_headline("8-K", ["9.01"]), "8-K: Financial statements and exhibits")
        self.assertEqual(news.filing_headline("8-K", ["1.01", "2.03"]), "8-K: Material agreement; New debt obligation")
        self.assertEqual(news.filing_headline("8-K/A", ["5.02"]), "8-K/A (amendment): Director/officer change")
        self.assertEqual(news.filing_headline("8-K", ["6.01"]), "8-K: Item 6.01")
        self.assertEqual(news.filing_headline("8-K", []), "8-K: Current report")
        self.assertEqual(news.filing_headline("6-K", []), "6-K: Foreign issuer report")

    def test_acceptance_time_is_eastern(self):
        t, known = news.sec_accept_time("2026-10-29T16:05:12.000Z", "2026-10-29")
        self.assertTrue(known)
        self.assertEqual(tc.to_et(t).strftime("%Y-%m-%d %H:%M"), "2026-10-29 16:05")

    def test_acceptance_time_utc_fallback_and_date_only(self):
        now = tc.parse_et("2026-10-30T10:15")
        # Read as ET this would be 13:00 (future) -> it must have been real UTC -> 09:00 ET.
        t, _ = news.sec_accept_time("2026-10-30T13:00:00.000Z", "2026-10-30", now)
        self.assertEqual(tc.et_hhmm(t), "09:00")
        t, _ = news.sec_accept_time("2026-10-30T10:00:00.000Z", "2026-10-30", now)
        self.assertEqual(tc.et_hhmm(t), "10:00")
        t, known = news.sec_accept_time(None, "2026-10-29")
        self.assertFalse(known)
        self.assertEqual(tc.to_et(t).strftime("%Y-%m-%d %H:%M"), "2026-10-29 00:00")
        self.assertEqual(news.sec_accept_time("junk", "junk"), (None, False))

    def test_filing_items(self):
        rows = [{"form": "8-K", "filed": "2026-10-29", "accepted": "2026-10-29T16:05:12.000Z",
                 "accession": "a-1", "items": ["2.02", "9.01"], "description": "8-K",
                 "url": "https://www.sec.gov/Archives/edgar/data/1318605/a1/tsla.htm"}]
        it = news.filing_items(rows, "TSLA")[0]
        self.assertEqual(it["title"], "8-K: Results of operations (earnings)")
        self.assertEqual((it["kind"], it["publisher"], it["form"]), ("filing", "SEC EDGAR", "8-K"))
        self.assertEqual(it["tags"], ["EARNINGS"])
        self.assertEqual(it["summary"], "Item 2.02; Item 9.01")


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.now = now_dt()
        self.since = self.now - timedelta(hours=24)

    def feed(self, raw, **kw):
        return news.build_feed(raw, self.now, self.since, **kw)

    def test_dedupe_same_link_across_sources_keeps_best_fields(self):
        raw = news.parse_rss(RSS_TSLA, "TSLA") + news.parse_search(SEARCH_TSLA, "TSLA")
        out = self.feed(raw)
        titles = [i["title"] for i in out["items"]]
        self.assertEqual(titles.count("Tesla beats third-quarter estimates, raises delivery outlook"), 1)
        top = out["items"][0]
        self.assertEqual(top["publisher"], "Reuters")            # real name beats host guess
        self.assertEqual(top["sources"], ["yahoo-rss", "yahoo-search"])
        self.assertEqual(top["link"], "https://finance.yahoo.com/news/tesla-beats-140200123.html")
        self.assertEqual(top["summary"], "Shares jump & more.")
        self.assertEqual(out["total"], 3)                        # 3 unique in window, 1 too old
        self.assertEqual(out["skipped"]["older"], 1)

    def test_same_title_merges_within_12h_not_across_days(self):
        t = "Tesla Stock Is Rising Today. Here's Why."
        raw = [item(t, ts("2026-10-30", "09:00"), link="https://a.com/1"),
               item(t + " - Motley Fool", ts("2026-10-30", "09:20"), link="https://b.com/2", source="yahoo-search")]
        self.assertEqual(len(news.dedupe(raw)), 1)
        far = [item(t, ts("2026-10-28", "09:00"), link="https://a.com/1"),
               item(t, ts("2026-10-30", "09:00"), link="https://a.com/9")]
        self.assertEqual(len(news.dedupe(far)), 2)

    def test_cross_symbol_story_lists_both_tickers_in_query_order(self):
        raw = [item("Chip stocks slide on export curbs", ts("2026-10-30", "09:00"), symbol="NVDA", link="https://x.com/c"),
               item("Chip stocks slide on export curbs", ts("2026-10-30", "09:05"), symbol="AMD", link="https://x.com/c")]
        out = self.feed(raw, symbol_order=["AMD", "NVDA"])
        self.assertEqual(len(out["items"]), 1)
        self.assertEqual(out["items"][0]["symbols"], ["AMD", "NVDA"])
        self.assertEqual(out["items"][0]["ts"], ts("2026-10-30", "09:00"))  # earliest copy

    def test_distinct_filings_with_same_headline_are_not_merged(self):
        rows = [{"form": "8-K", "filed": "2026-10-30", "accepted": f"2026-10-30T0{h}:00:00.000Z",
                 "items": ["8.01"], "url": f"https://www.sec.gov/Archives/edgar/data/1/{h}/d.htm"} for h in (7, 8)]
        out = self.feed(news.filing_items(rows, "TSLA"))
        self.assertEqual(len(out["items"]), 2)
        self.assertTrue(all(k.startswith("u:") for i in out["items"] for k in i["_keys"]))

    def test_window_future_undated_and_order(self):
        raw = [item("A old", ts("2026-10-28", "09:00"), link="https://e.com/a"),
               item("B new", ts("2026-10-30", "10:00"), link="https://e.com/b"),
               item("C newer", ts("2026-10-30", "10:10"), link="https://e.com/c"),
               item("D future", ts("2026-10-30", "11:00"), link="https://e.com/d"),
               item("E undated", None, link="https://e.com/e")]
        out = self.feed(raw)
        self.assertEqual([i["title"] for i in out["items"]], ["C newer", "B new"])
        self.assertEqual(out["skipped"], {"undated": 1, "older": 1, "future": 1, "seen": 0, "tag_filter": 0})

    def test_impact_age_tag_filter_and_limit(self):
        raw = [item("Analyst upgrades Acme", ts("2026-10-30", "10:00"), link="https://e.com/1"),  # fresh -> HIGH
               item("Analyst upgrades Beta", ts("2026-10-30", "08:00"), link="https://e.com/2"),  # old ANALYST
               item("Gamma misses earnings estimates", ts("2026-10-29", "16:30"), link="https://e.com/3"),
               item("Delta shares drift", ts("2026-10-30", "07:00"), link="https://e.com/4")]
        out = self.feed(raw)
        by = {i["title"].split()[2] if i["title"].startswith("Analyst") else i["title"].split()[0]: i
              for i in out["items"]}
        self.assertEqual((by["Acme"]["impact"], by["Acme"]["fresh"], by["Acme"]["age_minutes"]), ("HIGH", True, 15))
        self.assertEqual(by["Beta"]["impact"], "normal")
        self.assertEqual(by["Gamma"]["impact"], "HIGH")
        self.assertEqual(by["Delta"]["impact"], "normal")
        self.assertEqual(len(self.feed(raw, tags=["ANALYST"])["items"]), 2)
        self.assertEqual([i["title"][:5] for i in self.feed(raw, tags=["HIGH"])["items"]], ["Analy", "Gamma"])
        self.assertEqual(self.feed(raw, tags=["FDA"])["items"], [])
        lim = self.feed(raw, limit=1)
        self.assertEqual((len(lim["items"]), lim["total"]), (1, 4))

    def test_date_only_filing_kept_if_day_overlaps_window(self):
        rows = [{"form": "8-K", "filed": "2026-10-29", "accepted": None, "items": ["7.01"],
                 "url": "https://www.sec.gov/x/1.htm"}]
        out = self.feed(news.filing_items(rows, "TSLA"))
        self.assertEqual(len(out["items"]), 1)
        it = out["items"][0]
        self.assertEqual(it["impact"], "normal")  # not 'fresh' just because the time is unknown
        pub = news.public_item(it, self.now)
        self.assertEqual((pub["time_et"], pub["ago"]), (None, "date only"))

    def test_public_item_shape(self):
        out = self.feed(news.parse_search(SEARCH_TSLA, "TSLA"))
        pub = news.public_item(out["items"][0], self.now)
        for k in ("id", "symbol", "symbols", "title", "publisher", "link", "published", "published_ts", "time_et",
                  "age_minutes", "ago", "tags", "impact", "fresh", "kind", "sources"):
            self.assertIn(k, pub)
        self.assertFalse(any(k.startswith("_") for k in pub))
        self.assertEqual((pub["time_et"], pub["ago"], pub["published"]), ("10:02", "13m ago", "2026-10-30T10:02:00-04:00"))
        json.dumps(pub)


class SeenTests(unittest.TestCase):
    def setUp(self):
        self.now = now_dt()
        self.since = self.now - timedelta(hours=24)

    def test_new_only_cycle(self):
        seen = {"symbols": {}}
        first = news.build_feed(news.parse_rss(RSS_TSLA, "TSLA"), self.now, self.since, seen=seen)
        self.assertEqual(len(first["items"]), 2)
        news.mark_seen(seen, first["items"])
        again = news.build_feed(news.parse_rss(RSS_TSLA, "TSLA"), self.now, self.since, seen=seen)
        self.assertEqual((again["items"], again["skipped"]["seen"]), ([], 2))
        # The same story via another source (other publisher suffix, URL without tracking) is still seen.
        more = news.parse_search(SEARCH_TSLA, "TSLA")
        third = news.build_feed(more, self.now, self.since, seen=seen)
        self.assertEqual([i["title"] for i in third["items"]], ["Morgan Stanley upgrades Tesla to Overweight"])

    def test_seen_is_per_symbol_and_capped(self):
        seen = {"symbols": {}}
        items = [dict(item(f"Story number {n} about widgets", ts("2026-10-30", "09:00"), link=f"https://e.com/{n}"),
                      _keys={f"u:{n:04d}"}) for n in range(30)]
        news.mark_seen(seen, items, cap=10)
        self.assertEqual(len(seen["symbols"]["TSLA"]), 10)
        self.assertEqual(seen["symbols"]["TSLA"][-1], "u:0029")
        other = dict(item("Story number 29 about widgets", ts("2026-10-30", "09:00"), symbol="NVDA"), _keys={"u:0029"})
        self.assertFalse(news.is_seen(other, seen))
        news.mark_seen(seen, [items[29]])
        self.assertEqual(seen["symbols"]["TSLA"].count("u:0029"), 1)  # no duplicates


class CliInputTests(unittest.TestCase):
    def test_symbols_and_tags(self):
        self.assertEqual(news.parse_symbols(["tsla,nvda", "TSLA", "brk.b", "BRK-B"]), ["TSLA", "NVDA", "BRK.B"])
        for bad in ([""], ["TS LA"], ["$$$"], [f"S{i}" for i in range(30)]):
            with self.assertRaises(ValueError):
                news.parse_symbols(bad)
        self.assertEqual(news.parse_tags("deal, earnings,DEAL,high"), ["DEAL", "EARNINGS", "HIGH"])
        with self.assertRaises(ValueError):
            news.parse_tags("EARNINGS,BOGUS")

    def test_parser_accepts_skill_md_flags(self):
        p = news.build_parser()
        a = p.parse_intermixed_args(["TSLA", "--since", "90m", "NVDA", "--tags", "EARNINGS,DEAL",
                                     "--limit", "5", "--new-only", "--json"])
        self.assertEqual((a.symbols, a.since, a.tags, a.limit, a.new_only, a.json),
                         (["TSLA", "NVDA"], "90m", "EARNINGS,DEAL", 5, True, True))


class GatherTests(TempHomeTestCase):
    NOW = NOW

    def routes(self, **over):
        r = {"feeds.finance.yahoo.com/rss/2.0/headline?s=TSLA": RSS_TSLA,
             "v1/finance/search?q=TSLA": SEARCH_TSLA,
             "company_tickers.json": SEC_TICKERS,
             "CIK0001318605.json": SEC_SUBS}
        r.update(over)
        return {k: v for k, v in r.items() if v is not None}

    def test_all_sources(self):
        now = now_dt()
        with fake_http(self.routes()):
            raw, rows = news.gather(["TSLA"], now - timedelta(hours=24), now)
        self.assertEqual({r["source"]: r["status"] for r in rows},
                         {"yahoo-rss": "ok", "yahoo-search": "ok", "sec-edgar": "ok"})
        out = news.build_feed(raw, now, now - timedelta(hours=24))
        self.assertIn("8-K: Results of operations (earnings)", [i["title"] for i in out["items"]])
        self.assertNotIn("4", [i.get("form") for i in out["items"]])  # Form 4 is not a news form here

    def test_one_source_down_is_a_warning(self):
        now = now_dt()
        down = tc.FetchError("HTTP 403 for https://query1.finance.yahoo.com/v1/finance/search?q=TSLA - access denied")
        with fake_http(self.routes(**{"v1/finance/search?q=TSLA": down})) as m:
            raw, rows = news.gather(["TSLA"], now - timedelta(hours=24), now)
        self.assertTrue(raw)
        summary, warnings = news.source_summary(rows)
        self.assertEqual(len(warnings), 1)
        self.assertIn("Yahoo search failed for TSLA", warnings[0])
        self.assertIn("query1.finance.yahoo.com", warnings[0])
        hosts = [c.args[0].split("/")[2] for c in m.call_args_list if "search" in c.args[0]]
        self.assertEqual(sorted(set(hosts)), ["query1.finance.yahoo.com", "query2.finance.yahoo.com"])

    def test_all_sources_down_raises(self):
        now = now_dt()
        with fake_http({}):
            with self.assertRaises(tc.FetchError) as cm:
                news.gather(["TSLA"], now - timedelta(hours=24), now)
        msg = str(cm.exception)
        self.assertNotIn("\n", msg)
        for host in ("feeds.finance.yahoo.com", "query1.finance.yahoo.com", "www.sec.gov"):
            self.assertIn(host, msg)

    def test_etf_without_sec_filer_is_not_a_failure(self):
        now = now_dt()
        routes = {"headline?s=SPY": rss(("S&amp;P 500 hits record as CPI cools", "https://e.com/spy",
                                         "Fri, 30 Oct 2026 13:00:00 +0000", ""), symbol="SPY"),
                  "search?q=SPY": {"news": []}, "company_tickers.json": SEC_TICKERS}
        with fake_http(routes):
            raw, rows = news.gather(["SPY"], now - timedelta(hours=24), now)
        sec = [r for r in rows if r["source"] == "sec-edgar"][0]
        self.assertEqual(sec["status"], "n/a")
        self.assertEqual(news.source_summary(rows)[1], [])
        self.assertEqual(raw[0]["tags"], ["MACRO"])

    def test_sec_submissions_refreshed_at_feed_ttl(self):
        os.environ.pop("DAYTRADER_NO_CACHE", None)
        now = now_dt()
        with fake_http(self.routes()) as m:
            rows = news.fetch_sec(["TSLA"], "2026-10-29", now)
            subs = [c for c in m.call_args_list if "CIK0001318605" in c.args[0]]
            self.assertEqual(len(subs), 1)  # sec_recent_filings then read the fresh cached copy
            cache = self.home / "cache" / tc.fixture_name(SUBS_URL_TSLA)
            old = time.time() - 120          # older than our 60 s TTL, younger than the shared 15 min
            os.utime(cache, (old, old))
            news.fetch_sec(["TSLA"], "2026-10-29", now)
            subs = [c for c in m.call_args_list if "CIK0001318605" in c.args[0]]
            self.assertEqual(len(subs), 2)
        self.assertEqual(rows[0]["status"], "ok")
        self.assertEqual([i["title"] for i in rows[0]["items"]], ["8-K: Results of operations (earnings)"])

    def test_render_text(self):
        now = now_dt()
        with fake_http(self.routes()):
            raw, rows = news.gather(["TSLA"], now - timedelta(hours=24), now)
        feed = news.build_feed(raw, now, now - timedelta(hours=24))
        sources, warnings = news.source_summary(rows)
        res = {"symbols": ["TSLA"], "as_of": now.isoformat(), "since_label": "last 24h", "new_only": False,
               "tags_filter": [], "total": feed["total"], "skipped": feed["skipped"], "sources": sources,
               "warnings": warnings, "items": [news.public_item(i, now) for i in feed["items"]]}
        text = news.render(res)
        self.assertIn("NEWS · TSLA · last 24h", text)
        self.assertIn("HIGH  10:02 ET", text)
        self.assertIn("13m ago", text)
        self.assertIn("Reuters · EARNINGS GUIDANCE", text)
        self.assertIn("Oct 29 16:05 ET", text)
        self.assertIn("Sources: Yahoo RSS ok · Yahoo search ok · SEC EDGAR ok", text)
        self.assertIn("Not financial advice", text)


class CliSubprocessTests(TempHomeTestCase):
    NOW = NOW

    def setUp(self):
        super().setUp()
        self.fx = self.home / "fx"
        write_fixture(self.fx, news.rss_url("TSLA"), RSS_TSLA)
        write_fixture(self.fx, news.search_url("TSLA"), SEARCH_TSLA)
        write_fixture(self.fx, tc.SEC_TICKERS_URL, SEC_TICKERS)
        write_fixture(self.fx, SUBS_URL_TSLA, SEC_SUBS)
        self.env = {"DAYTRADER_HOME": str(self.home / "state"), "DAYTRADER_FIXTURES": str(self.fx),
                    "DAYTRADER_NOW": NOW}

    def cli(self, *args, **env):
        return run_cli(SCRIPT, *args, env=dict(self.env, **env))

    def test_text_output(self):
        r = self.cli("TSLA")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("NEWS · TSLA · last 24h", r.stdout)
        self.assertIn("4 headlines, 4 high impact", r.stdout)
        self.assertIn("Tesla-SpaceX Merger Mania Strikes Wall Street Ahead of Earnings", r.stdout)
        self.assertIn("Barron's · EARNINGS DEAL", r.stdout)
        self.assertNotIn("What to Watch This Week", r.stdout)  # older than 24h

    def test_json_and_partial_failure(self):
        r = self.cli("TSLA", "NVDA", "--json", "--since", "2d")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["symbols"], ["TSLA", "NVDA"])
        self.assertEqual([i["published"] for i in data["items"]],
                         sorted((i["published"] for i in data["items"]), reverse=True))
        self.assertEqual(data["items"][0]["title"], "Tesla beats third-quarter estimates, raises delivery outlook")
        self.assertTrue(data["warnings"])
        self.assertIn("warning: Yahoo RSS failed for NVDA", r.stderr)

    def test_new_only_tracks_what_was_shown(self):
        first = self.cli("TSLA", "--new-only", "--limit", "2")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("2 headlines", first.stdout)
        second = json.loads(self.cli("TSLA", "--new-only", "--json").stdout)
        self.assertEqual(second["shown"], 2)
        self.assertEqual(second["skipped"]["seen"], 2)
        third = self.cli("TSLA", "--new-only")
        self.assertIn("No new headlines for TSLA in the last 24h (4 already seen).", third.stdout)
        seen = json.loads((self.home / "state" / "news_seen.json").read_text())
        self.assertIn("TSLA", seen["symbols"])

    def test_tags_filter(self):
        data = json.loads(self.cli("TSLA", "--tags", "analyst", "--json").stdout)
        self.assertEqual([i["title"] for i in data["items"]], ["Morgan Stanley upgrades Tesla to Overweight"])

    def test_bad_input_exits_1(self):
        for args in (["TSLA", "--tags", "NOPE"], ["TSLA", "--since", "soon"], ["TSLA", "--limit", "0"], [],
                     ["TSLA", "--live"]):
            r = self.cli(*args)
            self.assertEqual(r.returncode, 1, (args, r.stderr))
            self.assertNotIn("Traceback", r.stderr)

    def test_since_overflow_and_corrupt_seen_exit_cleanly(self):
        r = self.cli("TSLA", "--since", "1000000d")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        state = self.home / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "news_seen.json").write_text('{"symbols": {"TSLA": 5}}')
        r = self.cli("TSLA", "--new-only", "--since", "09:30")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("since Fri Oct 30 09:30 ET", r.stdout)
        self.assertIn("2 headlines", r.stdout)

    def test_no_data_exits_2_with_one_line(self):
        r = self.cli("TSLA", DAYTRADER_FIXTURES=str(self.home / "empty"))
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")
        lines = r.stderr.strip().splitlines()
        self.assertEqual(len(lines), 1, r.stderr)
        self.assertTrue(lines[0].startswith("data error: all news sources failed"))


class ReviewRegressionTests(TempHomeTestCase):
    """Bugs found in review; each test failed before its fix."""
    NOW = NOW

    # Everyday headlines that used to get a false HIGH-impact tag (or a false EXEC/INSIDER/ANALYST tag).
    TRAPS = {
        "The best Black Friday deals on Apple AirPods and iPads": "DEAL",
        "Amazon Prime Day 2026: the best early deals": "DEAL",
        "US-China trade deal lifts stocks": "DEAL",                     # MACRO, not a company deal
        "Customer acquisition costs rise for Netflix": "DEAL",
        "Tesla buys back stock for $10 billion": "DEAL",              # buyback, not M&A
        "Musk buys $1 billion of Tesla shares": "DEAL",               # INSIDER, not M&A
        "Director sold 1,000 shares for $400,000": "DEAL",
        "Microsoft Outlook outage hits thousands of users": "GUIDANCE",
        "Regulators back Tesla charging plug as US standard": "LEGAL",
        "Tesla courts Chinese buyers with new financing": "LEGAL",
        "Tesla Inc. courts Chinese buyers": "LEGAL",
        "Wall Street's verdict on Tesla's robotaxi day": "LEGAL",
        "Tesla's Optimus faces the court of public opinion": "LEGAL",
        "Tesla stock tops $400 as Wall Street cheers robotaxi": "EARNINGS",
        "Salesforce raises prices for its AI offering": "OFFERING",
        "Apple CEO Tim Cook joins Trump at White House dinner": "EXEC",
        "Nvidia CEO Huang names three stocks he likes": "EXEC",
        "Tesla CEO Musk says robotaxi will replace Uber": "EXEC",
        "Insider Monkey: is Tesla the best EV stock to buy?": "INSIDER",
        "Tesla stock falls - Business Insider": "INSIDER",
        "Apple's iPhone upgrade cycle is finally here": "ANALYST",
    }
    # ...while the real thing must still be tagged.
    KEEP = {
        "AMD stock jumps on OpenAI deal": "DEAL",
        "Google buys Wiz for $32 billion": "DEAL",
        "Salesforce to buy Informatica for about $8 billion": "DEAL",
        "Boeing sells Jeppesen unit for $10.55 billion": "DEAL",
        "Sources say Apple in talks to buy Perplexity": "DEAL",
        "Nvidia inks deals with Saudi AI firms": "DEAL",
        "Biggest deals of the week: Pfizer, Merck": "DEAL",
        "US signs trade deals with Japan and Korea": "MACRO",
        "Deal of the day: AirPods Pro for $169": None,
        "Microsoft raises outlook as cloud demand surges": "GUIDANCE",
        "SEC charges former Tesla executive": "LEGAL",
        "Courts block Trump tariffs": "LEGAL",
        "Exclusive: federal courts split on Tesla Autopilot claims": "LEGAL",
        "Apple hit with $300 million verdict in patent case": "LEGAL",
        "Tesla tops Street estimates": "EARNINGS",
        "Nvidia beats Wall Street expectations": "EARNINGS",
        "Acme prices upsized offering of common stock": "OFFERING",
        "Former Tesla CFO joins Rivian as CEO": "EXEC",
        "Tesla replaces CFO": "EXEC",
        "Insiders sell $50 million of Acme stock": "INSIDER",
        "Morgan Stanley upgrades Tesla to Overweight": "ANALYST",
    }

    def test_tag_false_positives(self):
        for title, tag in self.TRAPS.items():
            self.assertNotIn(tag, news.tag_headline(title), title)
        for title, tag in self.KEEP.items():
            if tag is None:
                self.assertEqual(news.tag_headline(title), [], title)
            else:
                self.assertIn(tag, news.tag_headline(title), title)

    def test_nested_html_description_is_read(self):
        xml = ('<rss version="2.0"><channel><item><title>Acme update</title><link>https://e.com/a</link>'
               '<pubDate>Fri, 30 Oct 2026 14:02:00 +0000</pubDate>'
               '<description><p>FDA approves <b>Acme</b> drug</p></description></item></channel></rss>')
        it = news.parse_rss(xml, "ACME")[0]
        self.assertEqual(it["summary"], "FDA approves Acme drug")
        self.assertEqual(it["tags"], ["FDA"])

    def test_since_too_far_back_is_bad_input(self):
        for v in ("1000000d", "99999999999d", "9999999999999999999999m"):
            with self.assertRaises(ValueError, msg=v):
                news.parse_since(v, now_dt())

    def test_since_clock_time_means_today_et(self):
        self.assertEqual(news.parse_since("09:30", now_dt()).isoformat(), "2026-10-30T09:30:00-04:00")
        self.assertEqual(news.parse_since("8:00", now_dt()).strftime("%Y-%m-%d %H:%M"), "2026-10-30 08:00")
        with self.assertRaises(ValueError):
            news.parse_since("11:00", now_dt())   # later today = in the future
        with self.assertRaises(ValueError):
            news.parse_since("25:00", now_dt())

    def _routes(self, **over):
        r = {"headline?s=TSLA": RSS_TSLA, "search?q=TSLA": SEARCH_TSLA,
             "company_tickers.json": SEC_TICKERS, "CIK0001318605.json": SEC_SUBS}
        r.update(over)
        return r

    def test_malformed_sec_response_is_one_failed_source(self):
        now = now_dt()
        bad_subs = {"filings": {"recent": "not-a-table"}}
        for over in ({"company_tickers.json": {"error": "x"}}, {"company_tickers.json": [1, 2]},
                     {"company_tickers.json": {"0": {"ticker": "TSLA"}}}, {"CIK0001318605.json": bad_subs}):
            with fake_http(self._routes(**over)):
                raw, rows = news.gather(["TSLA"], now - timedelta(hours=24), now)
            status = {r["source"]: r["status"] for r in rows}
            self.assertEqual(status, {"yahoo-rss": "ok", "yahoo-search": "ok", "sec-edgar": "failed"}, over)
            self.assertTrue(raw)
            self.assertIn("SEC EDGAR failed for TSLA", news.source_summary(rows)[1][0])

    def test_transport_errors_that_escape_http_get_fail_one_source(self):
        import http.client
        now = now_dt()
        for exc in (http.client.IncompleteRead(b"partial"), EOFError("Compressed file ended")):
            with fake_http(self._routes(**{"search?q=TSLA": exc, "headline?s=TSLA": exc})):
                raw, rows = news.gather(["TSLA"], now - timedelta(hours=24), now)
            status = {r["source"]: r["status"] for r in rows}
            self.assertEqual(status, {"yahoo-rss": "failed", "yahoo-search": "failed", "sec-edgar": "ok"}, exc)
            self.assertEqual([i["title"] for i in raw], ["8-K: Results of operations (earnings)"])

    def test_corrupt_seen_values_are_ignored_not_a_crash(self):
        seen = news.clean_seen({"symbols": {"TSLA": 5, "NVDA": ["u:abc", 7], "AMD": "abc"}, "updated_at": "x"})
        self.assertEqual(seen["symbols"], {"NVDA": ["u:abc"]})
        for bad in (None, [], {"symbols": []}, {"symbols": "x"}):
            self.assertEqual(news.clean_seen(bad), {"symbols": {}})

    def test_empty_after_tag_filter_says_so(self):
        now = now_dt()
        res = {"symbols": ["TSLA"], "as_of": now.isoformat(), "since_label": "last 24h", "new_only": False,
               "tags_filter": ["MACRO"], "total": 0, "items": [], "sources": [], "warnings": [],
               "skipped": {"undated": 0, "older": 0, "future": 0, "seen": 0, "tag_filter": 4}}
        text = news.render(res)
        self.assertIn("No headlines tagged MACRO for TSLA in the last 24h (4 others filtered out by --tags).", text)
        self.assertNotIn("No headlines for TSLA", text)


if __name__ == "__main__":
    unittest.main()
