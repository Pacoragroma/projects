"""Tests for skills/pelosi (offline, synthetic House Clerk index files and PTR PDFs only)."""
import io
import json
import os
import re
import shlex
import sys
import textwrap
import unittest
import zipfile
import zlib
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import escape

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import helpers  # noqa: E402
from helpers import TempHomeTestCase, fake_http, run_cli, skill_script, tc, write_fixture  # noqa: E402

pelosi = helpers.load_skill_module("pelosi", "pelosi")
ptr = helpers.load_skill_module("pelosi", "pelosi_ptr")
pdfx = helpers.load_skill_module("pelosi", "pelosi_pdf")
SCRIPT = skill_script("pelosi", "pelosi.py")
SKILL_MD = helpers.SKILLS / "pelosi" / "SKILL.md"
NOW = "2026-10-30T10:15"

# ---------------------------------------------------------------------------
# Synthetic PDFs laid out like the House e-filing system's PTRs
# ---------------------------------------------------------------------------

HELV = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]
GID0 = 100
BULLET = "•"


def _gid(ch):
    return ord(ch) + GID0 if 32 <= ord(ch) <= 126 else 300  # 300 = bullet


def _tounicode(heading):
    out = ["/CIDInit /ProcSet findresource begin", "12 dict begin", "begincmap",
           "1 begincodespacerange", "<0000> <FFFF>", "endcodespacerange"]
    if heading:  # small-caps heading font: only capitals, ':' and space map to Unicode
        chars = [" ", ":"] + [chr(c) for c in range(65, 91)]
        out += [f"{len(chars)} beginbfchar"] + [f"<{_gid(c):04X}> <{ord(c):04X}>" for c in chars] + ["endbfchar"]
    else:  # one incrementing range and one explicit array range, plus a bfchar
        out += ["2 beginbfrange", f"<{32 + GID0:04X}> <{80 + GID0:04X}> <0020>",
                f"<{81 + GID0:04X}> <{126 + GID0:04X}> [" + " ".join(f"<{c:04X}>" for c in range(81, 127)) + "]",
                "endbfrange", "1 beginbfchar", "<012C> <2022>", "endbfchar"]
    out += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    return "\n".join(out).encode()


def make_pdf(pages, cid=True, objstm=False):
    """pages: [[(x, y, text, 'B' body | 'H' heading font, size)]] -> PDF bytes."""
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>"}
    nxt, page_nums = 5, []
    if cid:
        d1, d2, u1, u2 = 5, 6, 7, 8
        nxt = 9
        w = f"{32 + GID0} [{' '.join(map(str, HELV))}] 300 [350]"
        for fnum, dnum, unum, base in ((3, d1, u1, "ABCDEF+Arial"), (4, d2, u2, "ABCDEG+ArialSC")):
            objs[fnum] = (f"<< /Type /Font /Subtype /Type0 /BaseFont /{base} /Encoding /Identity-H "
                          f"/DescendantFonts [{dnum} 0 R] /ToUnicode {unum} 0 R >>").encode()
            objs[dnum] = (f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /{base} /CIDSystemInfo "
                          f"<< /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> /DW 500 /W [{w}] >>").encode()
            objs[unum] = ("stream", _tounicode(fnum == 4))
    else:
        objs[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
        objs[4] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
    for items in pages:
        pnum, cnum = nxt, nxt + 1
        nxt += 2
        page_nums.append(pnum)
        objs[pnum] = f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {cnum} 0 R >>".encode()
        ops = []
        for x, y, text, font, size in items:
            if cid:
                s = "<" + "".join(f"{_gid(c):04X}" for c in text) + ">"
            else:
                s = "(" + text.replace(BULLET, "\x95").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"
            ops.append(f"BT /F{1 if font == 'B' else 2} {size} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm {s} Tj ET")
        objs[cnum] = ("stream", "\n".join(ops).encode("latin-1"))
    objs[2] = (f"<< /Type /Pages /Count {len(pages)} /Kids [{' '.join(f'{p} 0 R' for p in page_nums)}] "
               f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> >>").encode()  # inherited by the pages
    packed = {3, 4, 5, 6} if (objstm and cid) else ({3, 4} if objstm else set())
    out, offsets = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n"), {}
    for num in sorted(objs):
        if num in packed:
            continue
        body = objs[num]
        offsets[num] = len(out)
        if isinstance(body, tuple):
            data = zlib.compress(body[1])
            out += (f"{num} 0 obj\n<< /Length {len(data)} /Filter /FlateDecode >>\nstream\n".encode()
                    + data + b"\nendstream\nendobj\n")
        else:
            out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"
    size = max(objs) + 1
    if packed:  # fonts inside a compressed object stream + an xref stream (PDF 1.5 style)
        stm, xref = size, size + 1
        size += 2
        heads, bodies = [], b""
        for num in sorted(packed):
            heads.append(f"{num} {len(bodies)}")
            bodies += objs[num] + b"\n"
        head = (" ".join(heads) + "\n").encode()
        data = zlib.compress(head + bodies)
        offsets[stm] = len(out)
        out += (f"{stm} 0 obj\n<< /Type /ObjStm /N {len(packed)} /First {len(head)} /Length {len(data)} "
                f"/Filter /FlateDecode >>\nstream\n").encode() + data + b"\nendstream\nendobj\n"
        idx = {n: i for i, n in enumerate(sorted(packed))}
        xref_pos = len(out)
        rows = b""
        for num in range(size):
            if num == 0:
                rows += bytes([0, 0, 0, 0, 0, 255])
            elif num in packed:
                rows += bytes([2]) + stm.to_bytes(4, "big") + bytes([idx[num]])
            else:
                rows += bytes([1]) + (xref_pos if num == xref else offsets.get(num, 0)).to_bytes(4, "big") + b"\x00"
        data = zlib.compress(rows)
        out += (f"{xref} 0 obj\n<< /Type /XRef /Size {size} /W [1 4 1] /Root 1 0 R /Length {len(data)} "
                f"/Filter /FlateDecode >>\nstream\n").encode() + data + b"\nendstream\nendobj\n"
        out += f"startxref\n{xref_pos}\n%%EOF\n".encode()
        return bytes(out)
    xref_pos = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for num in range(1, size):
        out += f"{offsets.get(num, 0):010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    return bytes(out)


def ptr_layout(name, district, rows, filing_id="20026590", signed="10/28/2026"):
    """Pages for make_pdf: header, filer info, the transaction table (cells, wrapped asset and
    amount, label lines below each row), then the IPO and certification sections."""
    pages, items, y = [], [], [0.0]

    def header():
        items.append((450, 770, f"Filing ID #{filing_id}", "B", 8))
        items.append((40, 750, f"Clerk of the House of Representatives {BULLET} Legislative Resource Center "
                               f"{BULLET} B81 Cannon Building {BULLET} Washington, DC 20515", "B", 7))
        y[0] = 720

    def table_header():
        for x, t in ((40, "ID"), (60, "Owner"), (95, "Asset"), (300, "Transaction"), (355, "Date"),
                     (410, "Notification"), (470, "Amount"), (560, "Cap.")):
            items.append((x, y[0], t, "B", 8))
        for x, t in ((300, "Type"), (410, "Date"), (560, "Gains >")):
            items.append((x, y[0] - 9, t, "B", 8))
        items.append((560, y[0] - 18, "$200?", "B", 8))
        y[0] -= 34

    header()
    items.append((40, y[0], "Periodic Transaction Report", "H", 14))
    items.append((40, y[0] - 26, "Filer Information", "H", 11))
    y[0] -= 44
    for label, val in (("Name:", name), ("Status:", "Member"), ("State/District:", district)):
        items += [(40, y[0], label, "B", 9), (120, y[0], val, "B", 9)]
        y[0] -= 13
    items.append((40, y[0] - 10, "Transactions", "H", 11))
    y[0] -= 28
    table_header()
    for r in rows:
        asset_lines = textwrap.wrap(r["asset"], 38)
        amt = r["amount"]
        if " - " in amt:  # the Amount column is narrow: ranges wrap after the dash ...
            amt_lines = [amt.split(" - ")[0] + " -", amt.split(" - ")[1]]
        elif "Over $" in amt:  # ... and open-ended amounts before the figure
            amt_lines = [amt.split(" $")[0], "$" + amt.split(" $")[1]]
        else:
            amt_lines = [amt]
        meta = [("Filing Status:", r.get("status", "New"))]
        if r.get("subholding"):
            meta.append(("Subholding Of:", r["subholding"]))
        if r.get("description"):
            meta.append(("Description:", r["description"]))
        need = 11 * max(len(asset_lines), len(amt_lines)) + 12 * (len(meta) + 3)
        if y[0] - need < 60:
            pages.append(items)
            items = []
            header()
            table_header()
        yy = y[0]
        if r.get("owner"):
            items.append((60, yy, r["owner"], "B", 8))
        items += [(95, yy - 11 * k, line, "B", 8) for k, line in enumerate(asset_lines)]
        items += [(300, yy, r["type"], "B", 8), (355, yy, r["tdate"], "B", 8), (410, yy, r["ndate"], "B", 8)]
        items += [(470, yy - 11 * k, line, "B", 8) for k, line in enumerate(amt_lines)]
        yy -= 11 * max(len(asset_lines), len(amt_lines)) + 3
        for label, val in meta:
            items.append((95, yy, label, "H", 8))
            width = sum(HELV[ord(c) - 32] for c in label) * 8 / 1000.0
            for vl in (textwrap.wrap(val, 90) if label == "Description:" else [val]):
                items.append((98 + width, yy, vl, "B", 8))
                yy -= 11
        y[0] = yy - 8
    items.append((40, y[0], "* For the complete list of asset type abbreviations, please visit "
                            "https://fd.house.gov/reference/asset-type-codes.aspx.", "B", 7))
    items += [(40, y[0] - 24, "Initial Public Offerings", "H", 11), (40, y[0] - 40, "Yes", "B", 9),
              (80, y[0] - 40, "No", "B", 9), (40, y[0] - 62, "Certification and Signature", "H", 11),
              (40, y[0] - 78, "I CERTIFY that the statements I have made on the attached Report are true, "
                              "complete, and correct to the best of my knowledge and belief.", "B", 8),
              (40, y[0] - 92, f"Digitally Signed: {name} , {signed}", "B", 8)]
    pages.append(items)
    return pages


def row(asset, type_, tdate, amount, owner="SP", ndate=None, **kw):
    d = {"owner": owner, "asset": asset, "type": type_, "tdate": tdate, "ndate": ndate or tdate, "amount": amount}
    d.update(kw)
    return d


PELOSI_ROWS = [
    row("Alphabet Inc. - Class A Common Stock (GOOGL) [OP]", "P", "10/14/2026", "$250,001 - $500,000",
        description="Purchased 50 call options with a strike price of $150 and an expiration date of 1/15/2027."),
    row("Apple Inc. - Common Stock (AAPL) [ST]", "S", "10/01/2026", "$5,000,001 - $25,000,000",
        description="Sold 31,600 shares."),
    row("NVIDIA Corporation - Common Stock (NVDA) [ST]", "P", "09/16/2026", "$500,001 - $1,000,000",
        description="Exercised 500 call options purchased 11/22/2025 (50,000 shares) at a strike price of $12."),
    row("Microsoft Corporation - Common Stock (MSFT) [ST]", "S (partial)", "10/20/2026", "$1,001 - $15,000",
        owner="", ndate="10/21/2026", subholding="Schwab Brokerage"),
    row("Vistra Corp. Common Stock (VST) [OP]", "P", "10/14/2026", "$100,001 - $250,000", owner="JT",
        description="Call options; strike price $80; expires 12/18/2026"),
    row("US Treasury Bill 4.2% due 03/2027 [GS]", "E", "10/03/2026", "Over $50,000,000", owner="DC"),
]
EXPECTED = [  # (owner, ticker, asset_type, type, transaction_date, amount)
    ("SP", "GOOGL", "OP", "P", "2026-10-14", "$250,001 - $500,000"),
    ("SP", "AAPL", "ST", "S", "2026-10-01", "$5,000,001 - $25,000,000"),
    ("SP", "NVDA", "ST", "P", "2026-09-16", "$500,001 - $1,000,000"),
    ("", "MSFT", "ST", "S (partial)", "2026-10-20", "$1,001 - $15,000"),
    ("JT", "VST", "OP", "P", "2026-10-14", "$100,001 - $250,000"),
    ("DC", None, "GS", "E", "2026-10-03", "Over $50,000,000"),
]


def pelosi_pdf_bytes(cid=True, objstm=False, rows=None):
    return make_pdf(ptr_layout("Hon. Nancy Pelosi", "CA11", rows or PELOSI_ROWS), cid=cid, objstm=objstm)


def paper_pdf():
    """A scanned filing: one page that only paints an image (no text layer)."""
    content = b"q 612 0 0 792 0 0 cm /Im1 Do Q"
    return (b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Count 1 /Kids [3 0 R] >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /XObject << /Im1 5 0 R >> >> /Contents 4 0 R >>\nendobj\n"
            + f"4 0 obj\n<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream\nendobj\n"
            b"5 0 obj\n<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray "
            b"/BitsPerComponent 8 /Filter /DCTDecode /Length 4 >>\nstream\n\xff\xd8\xff\xd9\nendstream\nendobj\n"
            b"trailer\n<< /Root 1 0 R >>\n%%EOF\n")


# ---------------------------------------------------------------------------
# Synthetic House Clerk index zips
# ---------------------------------------------------------------------------

FIELDS = ["Prefix", "Last", "First", "Suffix", "FilingType", "StateDst", "Year", "FilingDate", "DocID"]


def rec(last, first, st, filed, doc, ftype="P"):
    return {"Prefix": "Hon.", "Last": last, "First": first, "FilingType": ftype, "StateDst": st,
            "Year": filed.split("/")[-1], "FilingDate": filed, "DocID": doc}


def index_zip(year, recs, xml=True, txt=True, broken_xml=False):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        if txt:
            z.writestr(f"{year}FD.txt", "\t".join(FIELDS) + "\n" + "".join(
                "\t".join(str(r.get(k, "")) for k in FIELDS) + "\n" for r in recs))
        if xml:
            body = "".join("<Member>" + "".join(f"<{k}>{escape(str(r[k]))}</{k}>" if r.get(k) else f"<{k} />"
                                                for k in FIELDS) + "</Member>\n" for r in recs)
            doc = '﻿<?xml version="1.0" encoding="utf-8"?>\n<FinancialDisclosure>\n' + body
            z.writestr(f"{year}FD.xml", (doc if broken_xml else doc + "</FinancialDisclosure>\n").encode("utf-8"))
    return b.getvalue()


INDEX_2026 = [rec("Pelosi", "Nancy", "CA11", "10/28/2026", "20026590"),
              rec("Doe", "Jane", "TX02", "10/20/2026", "20026601"),
              rec("Smith", "John", "OH04", "10/15/2026", "8221999"),
              rec("Pelosi", "Nancy", "CA11", "5/15/2026", "10066000", ftype="O"),
              rec("Pelosi", "Nancy", "CA11", "8/01/2026", "20025500")]
INDEX_2025 = [rec("Pelosi", "Nancy", "CA11", "12/19/2025", "20024100")]
DOE_ROWS = [row("NVIDIA Corporation - Common Stock (NVDA) [ST]", "S", "08/29/2026", "$15,001 - $50,000",
                owner="", ndate="09/02/2026"),
            row("Palantir Technologies Inc. - Class A Common Stock (PLTR) [ST]", "P", "10/06/2026",
                "$1,001 - $15,000")]
OLD_PELOSI = [row("NVIDIA Corporation - Common Stock (NVDA) [OP]", "P", "11/21/2025", "$1,000,001 - $5,000,000",
                  description="Purchased 50 call options with a strike price of $120 and an expiration date "
                              "of 12/18/2026.")]
AUG_PELOSI = [row("Tempus AI, Inc. - Class A Common Stock (TEM) [OP]", "P", "07/15/2026", "$50,001 - $100,000")]


def routes():
    """URL -> body for every file the default test world serves."""
    u = pelosi.ptr_url
    return {
        pelosi.INDEX_URL.format(year=2026): index_zip(2026, INDEX_2026),
        pelosi.INDEX_URL.format(year=2025): index_zip(2025, INDEX_2025),
        u(2026, "20026590"): pelosi_pdf_bytes(),
        u(2026, "20026601"): make_pdf(ptr_layout("Hon. Jane Doe", "TX02", DOE_ROWS, "20026601"), cid=False),
        u(2026, "8221999"): paper_pdf(),
        u(2026, "20025500"): make_pdf(ptr_layout("Hon. Nancy Pelosi", "CA11", AUG_PELOSI, "20025500")),
        u(2025, "20024100"): make_pdf(ptr_layout("Hon. Nancy Pelosi", "CA11", OLD_PELOSI, "20024100")),
    }


def run_main(*argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = pelosi.main(list(argv))
    return code, buf.getvalue()


def summary(trades):
    return [(t["owner"], t["ticker"], t["asset_type"], t["type"], t["transaction_date"], t["amount"]) for t in trades]


# ---------------------------------------------------------------------------
# PTR text parsing (pure)
# ---------------------------------------------------------------------------

class ParsePtrTextTests(unittest.TestCase):
    def test_single_line_row_from_the_spec(self):
        t = ptr.parse_ptr_text("SP NVIDIA Corporation - Common Stock (NVDA) [ST] P 01/14/2025 01/14/2025 "
                               "$250,001 - $500,000\nF S: New\nFiling ID #20026589")
        self.assertEqual(len(t), 1)
        t = t[0]
        self.assertEqual((t["owner"], t["owner_label"], t["ticker"], t["asset_type"], t["asset_label"]),
                         ("SP", "spouse", "NVDA", "ST", "stock"))
        self.assertEqual(t["asset"], "NVIDIA Corporation - Common Stock")
        self.assertEqual((t["type"], t["action"]), ("P", "BUY"))
        self.assertEqual((t["transaction_date"], t["notification_date"]), ("2025-01-14", "2025-01-14"))
        self.assertEqual((t["amount"], t["amount_min"], t["amount_max"]), ("$250,001 - $500,000", 250001, 500000))
        self.assertEqual(t["filing_status"], "New")

    def test_option_row_with_description(self):
        text = ("SP NVIDIA Corporation - Common Stock (NVDA) [OP] P 12/20/2024 12/20/2024 $1,000,001 - $5,000,000\n"
                "F S: New\nS O: Brokerage Account\nD: Call options; strike price $80; expires 12/19/2025\n")
        t = ptr.parse_ptr_text(text)[0]
        self.assertEqual((t["ticker"], t["asset_type"], t["asset_label"]), ("NVDA", "OP", "option"))
        self.assertEqual(t["subholding_of"], "Brokerage Account")
        self.assertEqual(t["description"], "Call options; strike price $80; expires 12/19/2025")
        self.assertEqual(t["option"], {"kind": "call", "strike": 80.0, "expires": "2025-12-19"})
        self.assertEqual((t["amount_min"], t["amount_max"]), (1000001, 5000000))

    def test_cell_order_text_split_over_many_lines(self):
        text = ("SP\nAlphabet Inc. - Class A Common\nStock (GOOGL) [OP]\nP\n01/14/2025\n01/14/2025\n$250,001 -\n"
                "$500,000\nF S : New\nD : Purchased 50 call options with a strike price of $150 and an expiration "
                "date of\n1/16/2026.\nDC\nTesla, Inc. - Common Stock (TSLA)\n[ST]\nS (partial)\n02/12/2025\n"
                "02/12/2025\n$1,001 - $15,000\n")
        t = ptr.parse_ptr_text(text)
        self.assertEqual(summary(t), [("SP", "GOOGL", "OP", "P", "2025-01-14", "$250,001 - $500,000"),
                                      ("DC", "TSLA", "ST", "S (partial)", "2025-02-12", "$1,001 - $15,000")])
        self.assertEqual(t[0]["asset"], "Alphabet Inc. - Class A Common Stock")
        self.assertTrue(t[0]["description"].endswith("expiration date of 1/16/2026."))
        self.assertEqual(t[0]["option"], {"kind": "call", "strike": 150.0, "expires": "2026-01-16", "contracts": 50})
        self.assertEqual(t[1]["action"], "PARTIAL SALE")

    def test_visual_lines_with_asset_wrapped_around_the_dates(self):
        text = ("SP Microsoft Corporation - Common Stock S 01/03/2024 01/03/2024 $1,000,001 -\n"
                "(MSFT) [ST] $5,000,000\nF S : New\n"          # amount and asset both wrap
                "SP Alphabet Inc. - Class A Common\nP 01/14/2025 01/14/2025 $250,001 - $500,000\n"
                "Stock (GOOGL) [OP]\nF S : New\n")              # vertically centred row
        t = ptr.parse_ptr_text(text)
        self.assertEqual([(x["ticker"], x["asset"], x["amount"]) for x in t], [
            ("MSFT", "Microsoft Corporation - Common Stock", "$1,000,001 - $5,000,000"),
            ("GOOGL", "Alphabet Inc. - Class A Common Stock", "$250,001 - $500,000")])

    def test_description_continuation_is_not_glued_to_a_self_owned_asset(self):
        text = ("JT Amazon.com, Inc. - Common Stock (AMZN) [ST] P 03/03/2025 03/04/2025 $15,001 - $50,000\n"
                "F S: New\nD: Bought through a managed account at my advisor's discretion with\n"
                "no input from me.\nMicrosoft Corporation - Common\nStock (MSFT) [ST] S 03/05/2025 03/06/2025 "
                "$1,001 - $15,000\nF S: New\nD: Long note about the sale written by the member in plain words and\n"
                "this particular trade in their account\nCompany Holdings Incorporated -\nP 03/07/2025 03/07/2025 "
                "$1,001 - $15,000\nCommon Stock (CHI) [ST]\nF S: New\n")
        t = ptr.parse_ptr_text(text)
        self.assertEqual([x["owner"] for x in t], ["JT", "", ""])
        self.assertEqual(t[0]["description"],
                         "Bought through a managed account at my advisor's discretion with no input from me.")
        self.assertEqual(t[1]["asset"], "Microsoft Corporation - Common Stock")
        self.assertTrue(t[1]["description"].endswith("this particular trade in their account"))
        self.assertEqual((t[2]["ticker"], t[2]["asset"]), ("CHI", "Company Holdings Incorporated - Common Stock"))

    def test_amount_forms_and_labels(self):
        text = ("Apple Inc. (AAPL) [ST] E 03/07/2025 03/07/2025 Spouse/DC Over $1,000,000\nF S: Amended\nL: US\n"
                "SP Visa Inc. (V) [ST] P 05/01/2025 05/01/2025 $1,001 – $15,000 F S: New D: Small purchase.\n"
                "SP AT&T Inc. (T) [ST] S 05/02/2025 05/02/2025 Over $50,000,000\n"
                "Bitcoin [CT] P 07/01/2025 07/02/2025 $1,001 - $15,000\n")
        t = ptr.parse_ptr_text(text)
        self.assertEqual([(x["ticker"], x["amount"], x["amount_min"], x["amount_max"]) for x in t], [
            ("AAPL", "Spouse/DC Over $1,000,000", 1000001, None), ("V", "$1,001 - $15,000", 1001, 15000),
            ("T", "Over $50,000,000", 50000001, None), (None, "$1,001 - $15,000", 1001, 15000)])
        self.assertEqual((t[0]["filing_status"], t[0]["location"]), ("Amended", "US"))
        self.assertEqual((t[1]["filing_status"], t[1]["description"]), ("New", "Small purchase."))
        self.assertEqual((t[3]["asset"], t[3]["asset_label"]), ("Bitcoin", "crypto"))
        self.assertEqual(t[2]["action"], "SELL")
        self.assertEqual(t[0]["action"], "EXCHANGE")

    def test_page_furniture_between_rows_is_ignored(self):
        text = ("SP Visa Inc. (V) [ST] P 05/01/2025 05/01/2025 $1,001 - $15,000\nF S: New\nD: First page.\n"
                "Filing ID #20030001\nClerk of the House of Representatives • Legislative Resource Center\n"
                "ID Owner Asset Transaction Date Notification Amount Cap.\nType Date Gains >\n$200?\n"
                "SP Berkshire Hathaway Inc. (BRK.B) [ST] P 05/03/2025 05/03/2025 $50,001 - $100,000\nF S: New\n"
                "* For the complete list of asset type abbreviations, please visit "
                "https://fd.house.gov/reference/asset-type-codes.aspx.\nI P O\nYes No\nC S\n"
                "I CERTIFY that the statements I have made on the attached Report are true.\n"
                "Digitally Signed: Hon. Jane Doe , 05/20/2025\n")
        t = ptr.parse_ptr_text(text)
        self.assertEqual([x["ticker"] for x in t], ["V", "BRK.B"])
        self.assertEqual(t[0]["description"], "First page.")
        self.assertEqual(t[1]["filing_status"], "New")  # nothing from the certification leaks in

    def test_pypdf_style_garbled_labels(self):
        text = ("PÉÖÍÓÈÍÇ TÖÅÒ×ÅÇØÍÓÒ RÉÔÓÖØ\nName: Hon. Jane Doe\nTÖÅÒ×ÅÇØÍÓÒ×\n"
                "Palantir Technologies Inc. Class A Common Stock\n(PLTR) [ST]\nP 06/02/2025 06/03/2025 $1,001 -\n"
                "$15,000\nFÍÐÍÒË SØÅØÙ×:New\nSÙÆÌÓÐÈÍÒË OÊ: IRA\nDÉ×ÇÖÍÔØÍÓÒ:Bought.\n"
                "IÒÍØÍÅÐ PÙÆÐÍÇ OÊÊÉÖÍÒË×\nYes No\n")
        t = ptr.parse_ptr_text(text)
        self.assertEqual(len(t), 1)
        self.assertEqual((t[0]["ticker"], t[0]["owner"]), ("PLTR", ""))
        self.assertEqual((t[0]["filing_status"], t[0]["subholding_of"], t[0]["description"]), ("New", "IRA", "Bought."))

    def test_no_rows_and_false_positives(self):
        self.assertEqual(ptr.parse_ptr_text(""), [])
        self.assertEqual(ptr.parse_ptr_text("Clerk of the House\nName: Hon. X\nAnnual report text 01/02/2025"), [])
        # a lone type letter + date inside a description, with no amount, is not a row
        self.assertEqual(ptr.parse_ptr_text("D: Plan P 01/02/2025 adopted"), [])
        # an impossible date is not a row either
        self.assertEqual(ptr.parse_ptr_text("Apple (AAPL) [ST] P 13/45/2025 01/02/2025 $1,001 - $15,000"), [])

    def test_option_details(self):
        self.assertIsNone(ptr.option_details("Sold 100 shares.", "ST"))
        self.assertEqual(ptr.option_details("Purchased 20 put options with a strike price of $95.50 and an "
                                            "expiration date of 6/20/26.", "OP"),
                         {"kind": "put", "strike": 95.5, "expires": "2026-06-20", "contracts": 20})
        self.assertEqual(ptr.option_details("", "OP"), None)
        self.assertTrue(ptr.option_details("Exercised 500 call options (50,000 shares)", "ST")["exercised"])

    def test_real_tickers_that_look_like_words_are_kept(self):
        # CL (Colgate), SE (Sea Ltd), AG (First Majestic), SA (Seabridge Gold), USD (ProShares) are real symbols
        for name, sym in (("Colgate-Palmolive Company - Common Stock", "CL"), ("Sea Limited - ADS", "SE"),
                          ("First Majestic Silver Corp. Ordinary Shares (Canada)", "AG"),
                          ("Seabridge Gold, Inc. Ordinary Shares (Canada)", "SA"),
                          ("ProShares Ultra Semiconductors", "USD")):
            with self.subTest(sym=sym):
                t = ptr.parse_ptr_text(f"SP {name} ({sym}) [ST] P 01/02/2025 01/03/2025 $1,001 - $15,000\nF S: New")
                self.assertEqual((t[0]["ticker"], t[0]["asset"]), (sym, name))
        # a parenthesised word that is not a symbol is still not a ticker
        t = ptr.parse_ptr_text("Royal Dutch Shell PLC (ADR) [ST] S 01/02/2025 01/03/2025 $1,001 - $15,000")
        self.assertIsNone(t[0]["ticker"])

    def test_open_ended_amount_wrapping_beside_a_wrapped_asset(self):
        # visual-line text (stdlib engine): the amount cell's 'Over' / 'Spouse/DC Over' ends the dates line
        # and its '$' figure sits on the next line, after the wrapped asset text
        cases = [
            ("SP Apple Inc. - Common Stock P 01/02/2025 01/03/2025 Spouse/DC Over\n(AAPL) [ST] $1,000,000\nF S : New\n",
             ("Apple Inc. - Common Stock", "AAPL", "Spouse/DC Over $1,000,000", 1000001, None)),
            ("SP Apple Inc. - Common Stock P 01/02/2025 01/03/2025 Spouse/DC\n(AAPL) [ST] Over $1,000,000\nF S : New\n",
             ("Apple Inc. - Common Stock", "AAPL", "Spouse/DC Over $1,000,000", 1000001, None)),
            ("DC US Treasury Bill 4.2% due P 01/02/2025 01/03/2025 Over\n03/2027 [GS] $50,000,000\nF S : New\n",
             ("US Treasury Bill 4.2% due 03/2027", None, "Over $50,000,000", 50000001, None)),
            ("SP Apple Inc. (AAPL) [ST] P 01/02/2025 01/03/2025 Spouse/DC Over\n$1,000,000\nF S : New\n",
             ("Apple Inc.", "AAPL", "Spouse/DC Over $1,000,000", 1000001, None)),
        ]
        for text, want in cases:
            with self.subTest(text=text):
                t = ptr.parse_ptr_text(text)
                self.assertEqual(len(t), 1)
                self.assertEqual((t[0]["asset"], t[0]["ticker"], t[0]["amount"], t[0]["amount_min"],
                                  t[0]["amount_max"]), want)

    def test_partial_sale_whose_type_cell_wraps(self):
        # visual-line text: the type cell 'S (partial)' wraps, '(partial)' lands beside the wrapped asset
        for text in ("SP Microsoft Corp - Common S 01/02/2025 01/03/2025 $1,001 -\nStock (MSFT) [ST] (partial) $15,000\n"
                     "F S : New\n",
                     "SP Microsoft Corp - Common S 01/02/2025 01/03/2025 $1,001 - $15,000\nStock (MSFT) [ST] (partial)\n"
                     "F S : New\n"):
            with self.subTest(text=text):
                t = ptr.parse_ptr_text(text)
                self.assertEqual(len(t), 1)
                self.assertEqual((t[0]["type"], t[0]["action"], t[0]["asset"], t[0]["ticker"], t[0]["amount"]),
                                 ("S (partial)", "PARTIAL SALE", "Microsoft Corp - Common Stock", "MSFT",
                                  "$1,001 - $15,000"))
        # a plain sale stays a sale
        t = ptr.parse_ptr_text("SP Microsoft Corp (MSFT) [ST] S 01/02/2025 01/03/2025 $1,001 - $15,000\nF S: New\n")
        self.assertEqual(t[0]["type"], "S")

    def test_parse_mdy(self):
        self.assertEqual(ptr.parse_mdy("1/4/2025"), "2025-01-04")
        self.assertEqual(ptr.parse_mdy("12/19/25"), "2025-12-19")
        for bad in ("", None, "2/30/2025", "01/14/0202", "2025-01-14"):
            self.assertIsNone(ptr.parse_mdy(bad))


# ---------------------------------------------------------------------------
# PDF text extraction
# ---------------------------------------------------------------------------

class PdfExtractionTests(unittest.TestCase):
    def test_stdlib_extractor_rebuilds_visual_lines(self):
        text = pdfx.extract_text_stdlib(pelosi_pdf_bytes())
        lines = text.splitlines()
        self.assertIn("SP Alphabet Inc. - Class A Common Stock P 10/14/2026 10/14/2026 $250,001 -", lines)
        self.assertIn("(GOOGL) [OP] $500,000", lines)
        self.assertIn("F S : New", lines)                     # small-caps glyphs without Unicode dropped
        self.assertIn("State/District: CA11", lines)
        self.assertIn(f"Clerk of the House of Representatives {BULLET} Legislative Resource Center {BULLET} "
                      f"B81 Cannon Building {BULLET} Washington, DC 20515", lines)

    def test_every_font_and_object_layout_parses_identically(self):
        for cid in (True, False):
            for objstm in (False, True):
                with self.subTest(cid=cid, objstm=objstm):
                    text = pdfx.extract_text_stdlib(pelosi_pdf_bytes(cid=cid, objstm=objstm))
                    self.assertEqual(summary(ptr.parse_ptr_text(text)), EXPECTED)

    def test_multi_page_filing(self):
        tickers = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "CRM"]
        rows = [row(f"Company {t} Holdings Incorporated - Common Stock ({t}) [{'OP' if i % 5 == 0 else 'ST'}]",
                    ["P", "S", "S (partial)", "E"][i % 4], f"{i % 12 + 1:02d}/{i % 27 + 1:02d}/2026",
                    ["$1,001 - $15,000", "$15,001 - $50,000", "$1,000,001 - $5,000,000", "Over $50,000,000"][i % 4],
                    owner=["SP", "", "JT"][i % 3],
                    description=("A long description that wraps across two lines because the member wrote "
                                 "many words about this particular trade in the account") if i % 2 else None)
                for i, t in enumerate(tickers * 3)]
        pdf = make_pdf(ptr_layout("Hon. Jane Doe", "TX02", rows), cid=True)
        self.assertGreater(len(pdfx.Document(pdf).pages()), 2)
        got = ptr.parse_ptr_text(pdfx.extract_text_stdlib(pdf))
        self.assertEqual(len(got), len(rows))
        for g, r in zip(got, rows):
            self.assertEqual((g["owner"], g["type"], g["amount"]), (r["owner"], r["type"], r["amount"]))
            self.assertIn(f"({g['ticker']})", r["asset"])
            self.assertEqual(g.get("description"), r.get("description"))

    @unittest.skipUnless(pdfx.have_pypdf(), "pypdf not installed")
    def test_pypdf_text_parses_to_the_same_trades(self):
        for cid in (True, False):
            text = pdfx.extract_text_pypdf(pelosi_pdf_bytes(cid=cid))
            self.assertEqual(summary(ptr.parse_ptr_text(text)), EXPECTED)

    def test_cmap_and_string_escapes(self):
        cmap, lens = pdfx.parse_cmap(b"1 begincodespacerange <00> <FF> endcodespacerange\n"
                                     b"2 beginbfchar <01> <0041> <02> <D83DDE00> endbfchar\n"
                                     b"2 beginbfrange <10> <12> <0061> <20> <21> [<0058> <0059>] endbfrange")
        self.assertEqual(lens, {1})
        self.assertEqual(cmap, {1: "A", 2: "\U0001F600", 0x10: "a", 0x11: "b", 0x12: "c", 0x20: "X", 0x21: "Y"})
        lx = pdfx._Lexer(rb"(a\(b\)\\c\101\
d (nested)) <48 49> /Na#6De 12 0 R")
        self.assertEqual(lx.token(), b"a(b)\\cAd (nested)")
        self.assertEqual(lx.token(), b"HI")
        self.assertEqual(lx.token(), "Name")
        self.assertIsInstance(lx.parse(), pdfx.Ref)

    def test_paper_damaged_and_non_pdf_inputs(self):
        self.assertEqual(pdfx.extract_text_stdlib(paper_pdf()), "")
        self.assertEqual(pelosi.parse_ptr_pdf(paper_pdf())["status"], "paper")
        broken = pelosi.parse_ptr_pdf(b"%PDF-1.7\nnothing usable here")
        self.assertEqual(broken["status"], "unparsed")
        self.assertIn("no pages", broken["error"])
        with self.assertRaises(tc.FetchError):
            pelosi.parse_ptr_pdf(b"<html>Access denied</html>")

    def test_code_length_follows_the_font_type_not_the_tounicode_codespace(self):
        # Simple fonts always use 1-byte codes and Identity-H always 2-byte codes, whatever codespace
        # a producer wrote into the font's ToUnicode CMap.
        def doc(font, tounicode, content):
            tu, c = zlib.compress(tounicode), zlib.compress(content)
            return (b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
                    b"2 0 obj\n<< /Type /Pages /Count 1 /Kids [4 0 R] >>\nendobj\n3 0 obj\n" + font + b"\nendobj\n"
                    b"4 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R "
                    b">> >> /Contents 5 0 R >>\nendobj\n"
                    + b"5 0 obj\n<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(c) + c + b"\nendstream\nendobj\n"
                    + b"6 0 obj\n<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(tu) + tu + b"\nendstream\nendobj\n"
                    b"7 0 obj\n<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Arial /CIDSystemInfo << /Registry "
                    b"(Adobe) /Ordering (Identity) /Supplement 0 >> /DW 556 >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n")
        line = "SP Apple Inc. (AAPL) [ST] P 01/02/2025 01/03/2025 $1,001 - $15,000"
        cmap = ("begincmap\n1 begincodespacerange\n{}\nendcodespacerange\n1 beginbfrange\n<0020> <007E> <0020>\n"
                "endbfrange\nendcmap")
        simple = doc(b"<< /Type /Font /Subtype /TrueType /BaseFont /Arial /Encoding /WinAnsiEncoding /ToUnicode 6 0 R >>",
                     cmap.format("<0000> <FFFF>").encode(),
                     b"BT /F1 8 Tf 40 700 Td (" + line.replace("(", "\\(").replace(")", "\\)").encode() + b") Tj ET")
        cid = doc(b"<< /Type /Font /Subtype /Type0 /BaseFont /Arial /Encoding /Identity-H /DescendantFonts [7 0 R] "
                  b"/ToUnicode 6 0 R >>", cmap.format("<00> <FF>").encode(),
                  b"BT /F1 8 Tf 40 700 Td <" + "".join(f"{ord(ch):04X}" for ch in line).encode() + b"> Tj ET")
        for name, blob in (("simple", simple), ("identity-h", cid)):
            with self.subTest(name):
                self.assertEqual(pdfx.extract_text_stdlib(blob), line)

    def test_undecodable_pdf_is_not_called_a_paper_filing(self):
        # "paper" tells the user the filing is a scanned image; an encrypted PDF or one whose page
        # content cannot be decoded may well be an electronic filing, so it must be "unparsed"
        content = b"BT /F1 8 Tf 40 700 Td (SP Apple Inc. \\(AAPL\\) [ST] P 01/02/2025 01/03/2025 $1,001 - $15,000) Tj ET"
        def doc(stream_dict, body, font=b"/Subtype /Type1 /BaseFont /Helvetica", trailer_extra=b""):
            objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
                    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> "
                    b"/Contents 4 0 R >>",
                    b"<< /Length %d " % len(body) + stream_dict + b" >>\nstream\n" + body + b"\nendstream",
                    b"<< /Type /Font " + font + b" >>",
                    b"<< /Filter /Standard /V 2 /R 3 /O <00> /U <00> /P -4 >>",
                    b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Arial /CIDSystemInfo << /Registry (Adobe) "
                    b"/Ordering (Identity) /Supplement 0 >> >>"]
            out, offs = bytearray(b"%PDF-1.4\n"), []
            for i, o in enumerate(objs, 1):   # with a real xref, so pypdf opens it too
                offs.append(len(out))
                out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
            xref = len(out)
            out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % x for x in offs)
            out += b"trailer\n<< /Size %d /Root 1 0 R " % (len(objs) + 1) + trailer_extra + b">>\nstartxref\n%d\n%%%%EOF\n" % xref
            return bytes(out)
        readable = doc(b"", content)
        broken = {
            "encrypted": doc(b"/Filter /FlateDecode", bytes(range(7, 250)), trailer_extra=b"/Encrypt 6 0 R /ID [<01> <01>] "),
            "lzw": doc(b"/Filter /LZWDecode", b"\x80\x0b\x60\x50\x22\x0c\x0c\x85\x01"),
            # text drawn with a CID font that has no ToUnicode map: the glyphs are there, the letters are not
            "unmapped": doc(b"", content, b"/Subtype /Type0 /BaseFont /Arial /Encoding /Identity-H /DescendantFonts [7 0 R]"),
        }
        for engine in ("stdlib", "auto"):
            with mock.patch.dict(os.environ, {pdfx.ENGINE_ENV: engine}):
                self.assertEqual(pelosi.parse_ptr_pdf(readable)["status"], "parsed")
                self.assertEqual(pelosi.parse_ptr_pdf(paper_pdf())["status"], "paper")
                for name, blob in broken.items():
                    with self.subTest(engine=engine, pdf=name):
                        r = pelosi.parse_ptr_pdf(blob)
                        self.assertEqual(r["status"], "unparsed")
                        self.assertTrue(r.get("error"))

    def test_engine_override(self):
        with mock.patch.dict(os.environ, {pdfx.ENGINE_ENV: "stdlib"}):
            self.assertEqual(pdfx.engines(), ["stdlib"])
            r = pelosi.parse_ptr_pdf(pelosi_pdf_bytes())
            self.assertEqual((r["status"], r["engine"], len(r["trades"])), ("parsed", "stdlib", 6))


# ---------------------------------------------------------------------------
# Index, filters, summaries (pure)
# ---------------------------------------------------------------------------

class IndexTests(unittest.TestCase):
    def test_parse_zip_xml_with_bom(self):
        rows = pelosi.parse_index_zip(index_zip(2026, INDEX_2026), 2026)
        self.assertEqual(len(rows), 5)
        r = rows[0]
        self.assertEqual((r["doc_id"], r["last"], r["first"], r["filing_type"], r["state_district"], r["filed"]),
                         ("20026590", "Pelosi", "Nancy", "P", "CA11", "2026-10-28"))
        self.assertEqual(r["suffix"], "")

    def test_namespaced_xml_and_odd_dates(self):
        xml = (b'<?xml version="1.0"?><FinancialDisclosure xmlns="urn:example"><Member><Last>Pelosi</Last>'
               b'<First>Nancy</First><FilingType>P</FilingType><StateDst>CA11</StateDst><Year>2026</Year>'
               b'<FilingDate>10/28/2026 12:00:00 AM</FilingDate><DocID>20026590</DocID></Member>'
               b'<Member><Last>Doe</Last><FilingType>P</FilingType><FilingDate>soon</FilingDate><DocID>1</DocID>'
               b'</Member></FinancialDisclosure>')
        rows = pelosi.parse_index_xml(xml, 2026)
        self.assertEqual([(r["last"], r["filed"]) for r in rows], [("Pelosi", "2026-10-28"), ("Doe", None)])
        self.assertEqual(pelosi.index_date("2026-10-28"), "2026-10-28")

    def test_txt_fallback_and_bad_zip(self):
        rows = pelosi.parse_index_zip(index_zip(2026, INDEX_2026, broken_xml=True), 2026)
        self.assertEqual([r["doc_id"] for r in rows], [x["DocID"] for x in INDEX_2026])
        rows = pelosi.parse_index_zip(index_zip(2026, INDEX_2026, xml=False), 2026)
        self.assertEqual(len(rows), 5)
        with self.assertRaises(tc.FetchError):
            pelosi.parse_index_zip(b"<html>maintenance</html>", 2026)
        with self.assertRaises(tc.FetchError):
            pelosi.parse_index_zip(index_zip(2026, [], xml=False, txt=False), 2026)

    def test_ptr_filings_filter_and_sort(self):
        rows = pelosi.parse_index_zip(index_zip(2026, INDEX_2026), 2026)
        f = pelosi.ptr_filings(rows, "2026-09-30", "2026-10-30")
        self.assertEqual([x["doc_id"] for x in f], ["20026590", "20026601", "8221999"])  # no annual report
        self.assertEqual(f[0]["url"], "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20026590.pdf")
        self.assertEqual((f[0]["member"], f[0]["district"]), ("Nancy Pelosi", "CA-11"))
        self.assertTrue(f[2]["paper_likely"])
        self.assertEqual([x["doc_id"] for x in pelosi.ptr_filings(rows, None, None, "pelosi")], ["20026590", "20025500"])
        self.assertEqual(len(pelosi.ptr_filings(rows, "2026-10-29", "2026-10-30")), 0)

    def test_member_matching_and_labels(self):
        r = {"first": "Nancy", "last": "Pelosi", "suffix": "", "state_district": "CA11"}
        for q in ("pelosi", "PELOSI", "Nancy Pelosi", "Pelosi, Nancy", "pel", "CA11", "ca-11"):
            self.assertTrue(pelosi.member_matches(r, q), q)
        for q in ("Nancy Smith", "CA12", "", "osi"):
            self.assertFalse(pelosi.member_matches(r, q), q)
        self.assertEqual(pelosi.district_label("CA05"), "CA-5")
        self.assertEqual(pelosi.district_label("AK00"), "AK-AL")
        # the at-large label the output prints can be typed back in
        ak = {"first": "Nick", "last": "Begich", "suffix": "", "state_district": "AK00"}
        for q in ("AK-AL", "ak-al", "AKAL", "AK00", "AK0"):
            self.assertTrue(pelosi.member_matches(ak, q), q)
        self.assertFalse(pelosi.member_matches(r, "CA-AL"))
        # surnames that look like 'state + AL' are still names (Richard Neal is MA-1, not NE at large)
        neal = {"first": "Richard", "last": "Neal", "suffix": "", "state_district": "MA01"}
        for q in ("neal", "Neal", "Richard Neal"):
            self.assertTrue(pelosi.member_matches(neal, q), q)
        self.assertFalse(pelosi.member_matches({"first": "X", "last": "Y", "suffix": "", "state_district": "NE00"}, "Neal"))

    def test_member_matching_ignores_accents(self):
        for last, q in (("Sánchez", "sanchez"), ("Sánchez", "Linda Sánchez"), ("Sanchez", "Sánchez"),
                        ("Velázquez", "velazquez"), ("Barragán", "Nanette Barragan")):
            row_ = {"first": "Linda" if "Linda" in q else "X", "last": last, "suffix": "", "state_district": "CA38"}
            if "Nanette" in q:
                row_["first"] = "Nanette"
            with self.subTest(last=last, q=q):
                self.assertTrue(pelosi.member_matches(row_, q))
        self.assertEqual(pelosi.member_name({"first": "Thomas", "last": "Kean", "suffix": "Jr."}), "Thomas Kean Jr.")
        self.assertEqual(pelosi.normalize_ticker("brk.b"), pelosi.normalize_ticker("BRK-B"))

    def test_window_crosses_january(self):
        from datetime import date
        self.assertEqual(pelosi.window(30, date(2026, 1, 10)), ("2025-12-11", "2026-01-10", [2025, 2026]))
        self.assertEqual(pelosi.window(30, date(2026, 10, 30))[2], [2026])

    def test_enrich_lag_and_summaries(self):
        filing = {"member": "Jane Doe", "district": "TX-2", "filed": "2026-10-20", "doc_id": "1", "url": "u",
                  "trades": ptr.parse_ptr_text(
                      "NVIDIA Corp (NVDA) [ST] S 08/29/2026 09/02/2026 $15,001 - $50,000\n"
                      "SP NVIDIA Corp (NVDA) [OP] P 10/06/2026 10/06/2026 $1,001 - $15,000\n"
                      "SP NVIDIA Corp (NVDA) [ST] P 10/10/2026 10/10/2026 Over $50,000,000\n")}
        trades = pelosi.enrich_trades(filing)
        self.assertEqual([(t["lag_days"], t["late"]) for t in trades], [(52, True), (14, False), (10, False)])
        self.assertEqual(pelosi.lag_summary(trades), {"median": 14, "max": 52, "min": 10, "late": 1})
        g = pelosi.summarize_members(trades)[0]
        self.assertEqual((g["buys"], g["sells"], g["trades"], g["first_trade"], g["last_trade"]),
                         (2, 1, 3, "2026-08-29", "2026-10-10"))
        self.assertEqual((g["amount_min_sum"], g["open_ended"]), (15001 + 1001 + 50000001, True))
        tk = pelosi.summarize_tickers(trades)[0]
        self.assertEqual((tk["ticker"], tk["buys"], tk["sells"]), ("NVDA", 2, 1))
        self.assertEqual([t["transaction_date"] for t in pelosi.sort_trades(trades)],
                         ["2026-10-10", "2026-10-06", "2026-08-29"])
        self.assertEqual(pelosi.lag_summary([])["median"], None)


# ---------------------------------------------------------------------------
# Fetching, caching and the commands (tc.http_get patched)
# ---------------------------------------------------------------------------

class CommandTests(TempHomeTestCase):
    NOW = NOW

    def setUp(self):
        super().setUp()
        p = mock.patch.object(pelosi, "PDF_DELAY", 0)
        p.start()
        self.addCleanup(p.stop)

    def test_recent_lists_filings_without_downloading_pdfs(self):
        with fake_http(routes()) as m:
            code, out = run_main("recent", "--json")
        self.assertEqual(code, 0)
        d = json.loads(out)
        self.assertEqual((d["since"], d["as_of"], d["filings_total"], d["trades_mode"]), ("2026-09-30", "2026-10-30", 3, False))
        self.assertEqual([f["doc_id"] for f in d["filings"]], ["20026590", "20026601", "8221999"])
        self.assertEqual([c.args[0] for c in m.call_args_list], [pelosi.INDEX_URL.format(year=2026)])

    def test_recent_member_reads_trades_by_default(self):
        with fake_http(routes()):
            code, out = run_main("recent", "--member", "pelosi", "--json")
        d = json.loads(out)
        self.assertTrue(d["trades_mode"])
        self.assertEqual(d["total_trades"], 6)
        first = d["trades"][0]  # newest transaction first
        self.assertEqual((first["ticker"], first["transaction_date"], first["filed"], first["lag_days"]),
                         ("MSFT", "2026-10-20", "2026-10-28", 8))
        self.assertEqual(first["member"], "Nancy Pelosi")
        self.assertEqual(d["scan"]["downloaded"], 1)
        self.assertEqual(d["lag"]["max"], 42)

    def test_recent_trades_text_marks_paper_and_late(self):
        with fake_http(routes()):
            code, out = run_main("recent", "--trades")
        self.assertIn("Searched 3 of 3 filings (3 downloaded); 1 paper filing (scanned, no text) could not be read.", out)
        self.assertRegex(out, r"2026-08-29\s+Jane Doe \(TX-2\)\s+self\s+NVDA\s+NVIDIA Corporation\s+SELL\s+"
                              r"\$15,001 - \$50,000\s+2026-10-20\s+52\*")
        self.assertIn("8221999.pdf", out)
        self.assertIn("STOCK Act allows up to 45 days", out)
        self.assertIn(tc.DISCLAIMER, out)

    def test_ticker_across_years_and_cache(self):
        os.environ.pop("DAYTRADER_NO_CACHE")
        with fake_http(routes()) as m:
            code, out = run_main("ticker", "nvda", "--json")
            calls_first = len(m.call_args_list)
            d = json.loads(out)
            self.assertEqual(d["ticker"], "NVDA")
            self.assertEqual([(t["member"], t["transaction_date"]) for t in d["trades"]],
                             [("Nancy Pelosi", "2026-09-16"), ("Jane Doe", "2026-08-29"), ("Nancy Pelosi", "2025-11-21")])
            self.assertEqual([(g["member"], g["buys"], g["sells"]) for g in d["members"]],
                             [("Nancy Pelosi", 2, 0), ("Jane Doe", 0, 1)])
            self.assertEqual((d["scan"]["in_window"], d["scan"]["downloaded"], d["scan"]["paper"]), (5, 5, 1))
            self.assertTrue((self.home / "cache" / "ptr" / "20026590.json").exists())
            code, out = run_main("ticker", "NVDA", "--json")
            d2 = json.loads(out)
        self.assertEqual(d2["scan"]["cached"], 5)
        self.assertEqual(d2["trades"], d["trades"])
        self.assertEqual(len(m.call_args_list) - calls_first, 0)  # both indexes cached too (6 h ttl)

    def test_max_filings_cap_reports_what_was_not_searched(self):
        with fake_http(routes()):
            code, out = run_main("ticker", "NVDA", "--max-filings", "2")
        self.assertIn("Searched 2 of 5 filings (2 downloaded). Results only cover filings back to 2026-10-20. "
                      "3 older filings not searched yet", out)

    def test_coverage_date_ignores_older_cached_filings_past_the_gap(self):
        # An older filing cached by an earlier command must not make a capped scan claim it covered
        # everything back to that filing's date: the filings in between were never read.
        os.environ.pop("DAYTRADER_NO_CACHE")
        with fake_http(routes()):
            run_main("filing", "20024100", "--json")          # caches the 2025-12-19 filing
            code, out = run_main("ticker", "NVDA", "--max-filings", "1", "--json")
            d = json.loads(out)
            code, text = run_main("ticker", "NVDA", "--max-filings", "1")   # continues one filing further
        self.assertEqual((d["scan"]["searched"], d["scan"]["not_searched"]), (2, 3))
        self.assertEqual(d["scan"]["oldest_searched"], "2026-10-28")
        self.assertIn("Searched 3 of 5 filings (2 cached, 1 downloaded). Results only cover filings back to "
                      "2026-10-20. 2 older filings not searched yet", text)
        self.assertNotIn("back to 2025-12-19", text)

    def test_unread_filings_are_not_called_not_searched(self):
        with fake_http(routes()):
            code, out = run_main("ticker", "NVDA")
        self.assertIn("8221999.pdf", out)  # the paper filing was opened, it just has no text
        self.assertNotIn("Not searched - these filings", out)

    def test_truncated_download_is_a_failed_filing_not_a_crash(self):
        import http.client
        r = routes()
        r[pelosi.ptr_url(2026, "20026601")] = http.client.IncompleteRead(b"%PDF-1.5 partial", 90000)
        with fake_http(r):
            code, out = run_main("recent", "--trades", "--json")
        d = json.loads(out)
        self.assertEqual((d["scan"]["failed"], d["scan"]["downloaded"]), (1, 2))
        self.assertEqual([f["status"] for f in d["unread"]], ["error", "paper"])
        r = routes()
        r[pelosi.INDEX_URL.format(year=2026)] = EOFError("Compressed file ended before the end-of-stream marker")
        with fake_http(r):
            with self.assertRaises(tc.FetchError):
                run_main("recent")

    def test_member_command_and_did_you_mean(self):
        with fake_http(routes()):
            code, out = run_main("member", "Nancy Pelosi", "--json")
            d = json.loads(out)
            self.assertEqual(d["members"], [{"member": "Nancy Pelosi", "district": "CA-11"}])
            self.assertEqual(d["total_trades"], 8)
            self.assertEqual(d["tickers"][0]["ticker"], "NVDA")
            code, out = run_main("member", "pelosy")
        self.assertIn("Did you mean: Nancy Pelosi?", out)

    def test_filing_command(self):
        with fake_http(routes()):
            code, out = run_main("filing", "20026601", "--json")
            d = json.loads(out)
            self.assertEqual(d["filing"]["status"], "parsed")
            self.assertEqual([t["ticker"] for t in d["trades"]], ["NVDA", "PLTR"])
            code, out = run_main("filing", "8221999")
            self.assertIn("Paper filing", out)
            with self.assertRaises(ValueError):
                run_main("filing", "20099999")

    def test_download_failures(self):
        r = routes()
        r[pelosi.ptr_url(2026, "20026601")] = tc.FetchError("HTTP 404 for x - not found")
        with fake_http(r):
            code, out = run_main("recent", "--trades", "--json")
        d = json.loads(out)
        self.assertEqual(d["scan"]["failed"], 1)
        self.assertTrue(any("could not be downloaded" in w for w in d["warnings"]))
        self.assertEqual([f["status"] for f in d["unread"]], ["error", "paper"])
        failing = {k: (tc.FetchError("HTTP 403 for x - access denied") if "ptr-pdfs" in k else v) for k, v in routes().items()}
        with fake_http(failing) as m:
            with self.assertRaises(tc.FetchError):
                run_main("ticker", "NVDA")
        pdf_calls = [c for c in m.call_args_list if "ptr-pdfs" in c.args[0]]
        self.assertEqual(len(pdf_calls), pelosi.MAX_CONSECUTIVE_FAILURES)  # gives up instead of hammering

    def test_one_index_year_down_is_a_warning_all_down_is_an_error(self):
        r = routes()
        r[pelosi.INDEX_URL.format(year=2025)] = tc.FetchError("HTTP 503 for 2025FD.zip")
        with fake_http(r):
            code, out = run_main("ticker", "NVDA", "--json")
        d = json.loads(out)
        self.assertTrue(any("2025" in w for w in d["warnings"]))
        self.assertEqual(len(d["trades"]), 2)
        with fake_http({"house.gov": tc.FetchError("HTTP 403 - access denied")}):
            with self.assertRaises(tc.FetchError):
                run_main("recent")

    def test_bad_cached_index_is_forgotten(self):
        os.environ.pop("DAYTRADER_NO_CACHE")
        url = pelosi.INDEX_URL.format(year=2026)
        with fake_http({url: b"<html>maintenance page</html>"}):
            with self.assertRaises(tc.FetchError):
                run_main("recent")
        self.assertFalse((self.home / "cache" / tc.fixture_name(url)).exists())

    def test_input_validation(self):
        for argv in (["ticker", "NV DA!"], ["recent", "--days", "0"], ["recent", "--limit", "-1"],
                     ["member", "x"], ["filing", "abc"]):
            with self.assertRaises(ValueError, msg=argv):
                run_main(*argv)
        with self.assertRaises(SystemExit) as cm, redirect_stdout(io.StringIO()), \
                mock.patch("sys.stderr", io.StringIO()):
            pelosi.main(["recent", "--live"])
        self.assertEqual(cm.exception.code, 1)


class NewYearIndexTests(TempHomeTestCase):
    """Early January: last year's index was cached in late December, while it was still growing."""
    NOW = "2026-01-05T10:00"

    def test_last_years_index_cached_in_december_is_refreshed(self):
        import time
        os.environ.pop("DAYTRADER_NO_CACHE")
        url25, url26 = pelosi.INDEX_URL.format(year=2025), pelosi.INDEX_URL.format(year=2026)
        stale = index_zip(2025, [rec("Doe", "Jane", "TX02", "12/29/2025", "20024200")])
        cache = self.home / "cache" / tc.fixture_name(url25)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(stale)
        three_days_ago = time.time() - 3 * 86400            # fetched on Dec 31 (wall clock)
        os.utime(cache, (three_days_ago, three_days_ago))
        fresh = index_zip(2025, [rec("Doe", "Jane", "TX02", "12/29/2025", "20024200"),
                                 rec("Pelosi", "Nancy", "CA11", "12/31/2025", "20024300")])
        with fake_http({url25: fresh, url26: index_zip(2026, [])}):
            code, out = run_main("recent", "--json")
        d = json.loads(out)
        self.assertEqual([f["doc_id"] for f in d["filings"]], ["20024300", "20024200"])
        # much later in the year, last year's index is settled and cached for longer
        from datetime import date
        self.assertEqual(pelosi.index_ttl(2025, date(2026, 1, 5)), pelosi.INDEX_TTL)
        self.assertEqual(pelosi.index_ttl(2026, date(2026, 1, 5)), pelosi.INDEX_TTL)
        self.assertEqual(pelosi.index_ttl(2025, date(2026, 6, 1)), pelosi.PAST_INDEX_TTL)
        self.assertEqual(pelosi.index_ttl(2024, date(2026, 1, 5)), pelosi.PAST_INDEX_TTL)


class SkillDocTests(unittest.TestCase):
    def test_every_command_in_skill_md_parses(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertIn("Scripts live in this skill's `scripts/` folder. Run them from the skill's folder "
                      "(`python3 scripts/<tool>.py ...`) or by absolute path. Python 3.9+, standard library only.", text)
        self.assertTrue(text.startswith("---\nname: pelosi\ndescription:"))
        cmds = re.findall(r"python3 scripts/pelosi\.py([^\n#`]*)", text)
        self.assertGreater(len(cmds), 5)
        parser = pelosi.build_parser()
        for c in cmds:
            with self.subTest(cmd=c):
                parser.parse_args(shlex.split(c))


# ---------------------------------------------------------------------------
# The real CLI as a subprocess, offline via $DAYTRADER_FIXTURES
# ---------------------------------------------------------------------------

class CliTests(TempHomeTestCase):
    def setUp(self):
        super().setUp()
        self.fx = self.home / "fixtures"
        for url, body in routes().items():
            write_fixture(self.fx, url, body)
        self.env = {"DAYTRADER_HOME": str(self.home), "DAYTRADER_FIXTURES": str(self.fx), "DAYTRADER_NOW": NOW}

    def test_recent_member_json(self):
        r = run_cli(SCRIPT, "recent", "--member", "pelosi", "--json", env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual(summary(sorted(d["trades"], key=lambda t: EXPECTED.index(next(
            e for e in EXPECTED if e[1] == t["ticker"] and e[3] == t["type"])))), EXPECTED)

    def test_ticker_text(self):
        r = run_cli(SCRIPT, "ticker", "NVDA", env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WHO TRADED NVDA", r.stdout)
        self.assertRegex(r.stdout, r"Nancy Pelosi \(CA-11\)\s+2\s+0\s+2\s+2025-11-21\s+2026-09-16")
        self.assertIn("call, strike $120, exp 2026-12-18, 50 contracts", r.stdout)

    def test_data_down_and_bad_input_exit_codes(self):
        env = dict(self.env, DAYTRADER_FIXTURES=str(self.home / "empty"))
        r = run_cli(SCRIPT, "recent", env=env)
        self.assertEqual(r.returncode, 2)
        self.assertTrue(r.stderr.startswith("data error: "), r.stderr)
        self.assertIn("disclosures-clerk.house.gov", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        r = run_cli(SCRIPT, "ticker", "NOT A TICKER", env=self.env)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)

    def test_block_pages_name_the_host_in_the_data_error(self):
        # a proxy or maintenance page served with HTTP 200 instead of the zip / the PDFs
        fx = self.home / "blocked"
        write_fixture(fx, pelosi.INDEX_URL.format(year=2026), b"<html>Site maintenance</html>")
        r = run_cli(SCRIPT, "recent", env=dict(self.env, DAYTRADER_FIXTURES=str(fx)))
        self.assertEqual(r.returncode, 2)
        self.assertTrue(r.stderr.startswith("data error: "), r.stderr)
        self.assertIn("disclosures-clerk.house.gov", r.stderr)
        fx = self.home / "blocked-pdfs"
        for url, body in routes().items():
            write_fixture(fx, url, b"<html>Access denied</html>" if "ptr-pdfs" in url else body)
        r = run_cli(SCRIPT, "member", "doe", env=dict(self.env, DAYTRADER_FIXTURES=str(fx)))
        self.assertEqual(r.returncode, 2)
        self.assertIn("disclosures-clerk.house.gov", r.stderr)
        self.assertEqual(len(r.stderr.strip().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
