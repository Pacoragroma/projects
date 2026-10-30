#!/usr/bin/env python3
"""pelosi_pdf - text from House Periodic Transaction Report PDFs, standard library only.

pelosi.py uses pypdf when it is installed and this extractor otherwise (or as a second
opinion when pypdf finds no trades). It covers what electronically filed PTRs need:

  * plain and compressed objects (object streams), FlateDecode / ASCIIHex / ASCII85 streams
  * the page tree with inherited resources, and Form XObjects
  * simple fonts (WinAnsi / MacRoman / Standard + /Differences) and Type0 (CID) fonts,
    decoded through their /ToUnicode CMaps; glyphs with no Unicode mapping are dropped
    (that is why House PTR headings read "F S: New" for "Filing Status: New")
  * text operators Tj, TJ, ' and " with real positions (Td, TD, Tm, T*, cm, q/Q), so the
    output is rebuilt into visual lines, top to bottom, left to right

Scanned (image-only) PDFs have no text layer; extract_text() returns "" for them.

Usage:
  python3 scripts/pelosi_pdf.py filing.pdf            # print the text (debugging aid)
  python3 scripts/pelosi_pdf.py filing.pdf --engine pypdf
"""
from __future__ import annotations

import base64
import logging
import math
import os
import re
import sys
import zlib
from typing import Any, Dict, List, Optional, Sequence, Tuple

ENGINE_ENV = "DAYTRADER_PDF_ENGINE"     # auto (default) | stdlib | pypdf


class PdfError(ValueError):
    """The bytes are not a PDF this extractor can read."""


class PdfTextError(PdfError):
    """The pages draw content this extractor cannot decode, so 'no text' would be a false
    'scanned filing' verdict (undecodable content streams, glyphs without Unicode mapping)."""


class Name(str):
    """A PDF name object, stored without its leading slash."""


class Op(str):
    """A bare keyword: a content-stream operator, a delimiter, true/false/null or R."""


class Ref:
    __slots__ = ("num", "gen")

    def __init__(self, num: int, gen: int) -> None:
        self.num, self.gen = num, gen

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Ref({self.num} {self.gen})"


class Stream:
    __slots__ = ("dict", "raw")

    def __init__(self, d: Dict[str, Any], raw: bytes) -> None:
        self.dict, self.raw = d, raw


# ---------------------------------------------------------------------------
# Lexer / object parser
# ---------------------------------------------------------------------------

_WS = frozenset(b" \t\r\n\x00\x0c")
_REGULAR = re.compile(rb"[^\s()<>\[\]{}/%\x00]+")
_NUMBER = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)")
_LIT_PLAIN = re.compile(rb"[^\\()]+")
_ESCAPES = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12,
            ord("("): 40, ord(")"): 41, ord("\\"): 92}
_OPEN_DICT, _CLOSE_DICT, _OPEN_ARR, _CLOSE_ARR = Op("<<"), Op(">>"), Op("["), Op("]")


def _unescape_name(raw: bytes) -> str:
    if b"#" in raw:
        raw = re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), raw)
    return raw.decode("latin-1")


class _Lexer:
    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data, self.pos, self.n = data, pos, len(data)

    def skip_ws(self) -> None:
        d, p, n = self.data, self.pos, self.n
        while p < n:
            c = d[p]
            if c in _WS:
                p += 1
            elif c == 0x25:  # % comment to end of line
                while p < n and d[p] not in (10, 13):
                    p += 1
            else:
                break
        self.pos = p

    def token(self) -> Any:
        """Next token: int/float, bytes (string), Name, Op, or None at the end."""
        self.skip_ws()
        d, p = self.data, self.pos
        if p >= self.n:
            return None
        c = d[p]
        if c == 0x2F:  # /Name
            m = _REGULAR.match(d, p + 1)
            raw = m.group(0) if m else b""
            self.pos = p + 1 + len(raw)
            return Name(_unescape_name(raw))
        if c == 0x28:  # (literal string)
            return self._literal()
        if c == 0x3C:  # << or <hex>
            if d[p + 1:p + 2] == b"<":
                self.pos = p + 2
                return _OPEN_DICT
            e = d.find(b">", p + 1)
            e = self.n if e < 0 else e
            hexs = re.sub(rb"[^0-9A-Fa-f]", b"", d[p + 1:e])
            if len(hexs) % 2:
                hexs += b"0"
            self.pos = e + 1
            return bytes.fromhex(hexs.decode("ascii"))
        if c == 0x3E:
            if d[p + 1:p + 2] == b">":
                self.pos = p + 2
                return _CLOSE_DICT
            self.pos = p + 1
            return Op(">")
        if c in (0x5B, 0x5D, 0x7B, 0x7D, 0x29):
            self.pos = p + 1
            return Op(chr(c))
        m = _REGULAR.match(d, p)
        word = m.group(0) if m else d[p:p + 1]
        self.pos = p + len(word)
        if _NUMBER.fullmatch(word):
            try:
                return int(word) if re.fullmatch(rb"[+-]?\d+", word) else float(word)
            except ValueError:
                return 0
        return Op(word.decode("latin-1"))

    def _literal(self) -> bytes:
        d, n = self.data, self.n
        p, depth, out = self.pos + 1, 1, bytearray()
        while p < n:
            m = _LIT_PLAIN.match(d, p)
            if m:
                out += m.group(0)
                p = m.end()
                continue
            c = d[p]
            if c == 0x5C:  # backslash escape
                p += 1
                if p >= n:
                    break
                c = d[p]
                if c in _ESCAPES:
                    out.append(_ESCAPES[c])
                    p += 1
                elif 0x30 <= c <= 0x37:
                    q = p
                    while q < n and q < p + 3 and 0x30 <= d[q] <= 0x37:
                        q += 1
                    out.append(int(d[p:q], 8) & 0xFF)
                    p = q
                elif c == 0x0D:  # line continuation
                    p += 2 if d[p + 1:p + 2] == b"\n" else 1
                elif c == 0x0A:
                    p += 1
                else:
                    out.append(c)
                    p += 1
            elif c == 0x28:
                depth += 1
                out.append(c)
                p += 1
            else:  # ')'
                depth -= 1
                p += 1
                if depth == 0:
                    break
                out.append(c)
        self.pos = p
        return bytes(out)

    def parse(self, refs: bool = True) -> Any:
        return self.value(self.token(), refs)

    def value(self, tok: Any, refs: bool = True) -> Any:
        if isinstance(tok, Op):
            if tok == "<<":
                out: Dict[str, Any] = {}
                while True:
                    key = self.token()
                    if key is None or (isinstance(key, Op) and key == ">>"):
                        break
                    if isinstance(key, Name):
                        val = self.parse(refs)
                        if isinstance(val, Op) and val == ">>":
                            break
                        out[str(key)] = val
                return out
            if tok == "[":
                arr: List[Any] = []
                while True:
                    t = self.token()
                    if t is None or (isinstance(t, Op) and t == "]"):
                        break
                    arr.append(self.value(t, refs))
                return arr
            if tok == "true":
                return True
            if tok == "false":
                return False
            if tok == "null":
                return None
            return tok
        if refs and type(tok) is int:
            save = self.pos
            t2 = self.token()
            if type(t2) is int:
                t3 = self.token()
                if isinstance(t3, Op) and t3 == "R":
                    return Ref(tok, t2)
            self.pos = save
        return tok


# ---------------------------------------------------------------------------
# Stream filters
# ---------------------------------------------------------------------------

def _inflate(data: bytes) -> bytes:
    try:
        return zlib.decompress(data)
    except zlib.error:
        pass
    for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):  # tolerate truncated / headerless streams
        try:
            d = zlib.decompressobj(wbits)
            out = d.decompress(data)
            if out:
                return out + d.flush()
        except zlib.error:
            continue
    raise PdfError("corrupt Flate stream")


def _ascii_hex(data: bytes) -> bytes:
    h = re.sub(rb"[^0-9A-Fa-f]", b"", data.split(b">", 1)[0])
    if len(h) % 2:
        h += b"0"
    return bytes.fromhex(h.decode("ascii"))


def _ascii85(data: bytes) -> bytes:
    body = data.strip()
    if body.startswith(b"<~"):
        body = body[2:]
    body = body.split(b"~>", 1)[0]
    body = re.sub(rb"\s", b"", body)
    return base64.a85decode(body)


# ---------------------------------------------------------------------------
# Document: objects, streams, page tree
# ---------------------------------------------------------------------------

_OBJ_RX = re.compile(rb"(?<![0-9])(\d{1,10})\s+(\d{1,5})\s+obj\b")


class Document:
    def __init__(self, data: bytes) -> None:
        head = data[:1024]
        if b"%PDF" not in head:
            raise PdfError("not a PDF file")
        self.data = data
        self.objects: Dict[int, Any] = {}
        self.trailers: List[Dict[str, Any]] = []
        self.undecodable: List[str] = []   # why page content streams could not be decoded
        self._scan()
        if any(isinstance(t, dict) and t.get("Encrypt") is not None for t in self.trailers):
            # strings and streams are encrypted: without decryption it would look like a scan
            raise PdfError("encrypted PDF (the stdlib extractor cannot decrypt it; pypdf can)")
        self._expand_object_streams()

    # -- objects ---------------------------------------------------------
    def _scan(self) -> None:
        d, pos = self.data, 0
        while True:
            m = _OBJ_RX.search(d, pos)
            if not m:
                break
            lx = _Lexer(d, m.end())
            try:
                obj = lx.parse()
            except Exception:
                pos = m.end()
                continue
            end = lx.pos
            if isinstance(obj, dict):
                lx.skip_ws()
                if d.startswith(b"stream", lx.pos):
                    raw, end = self._stream_bytes(obj, lx.pos + 6)
                    obj = Stream(obj, raw)
                    if obj.dict.get("Type") == "XRef":
                        self.trailers.append(obj.dict)
            self.objects[int(m.group(1))] = obj
            pos = max(end, m.end())
        for m in re.finditer(rb"trailer\s*<<", d):
            lx = _Lexer(d, m.end() - 2)
            try:
                t = lx.parse()
            except Exception:
                continue
            if isinstance(t, dict):
                self.trailers.append(t)

    def _stream_bytes(self, dct: Dict[str, Any], p: int) -> Tuple[bytes, int]:
        d = self.data
        if d.startswith(b"\r\n", p):
            p += 2
        elif d[p:p + 1] in (b"\n", b"\r"):
            p += 1
        length = dct.get("Length")
        if type(length) is int and 0 <= length <= len(d) - p:
            e = p + length
            if d[e:e + 40].lstrip(b" \t\r\n\x00\x0c").startswith(b"endstream"):
                return d[p:e], e
        e = d.find(b"endstream", p)
        if e < 0:
            e = len(d)
        raw = d[p:e]
        if raw.endswith(b"\r\n"):
            raw = raw[:-2]
        elif raw.endswith((b"\n", b"\r")):
            raw = raw[:-1]
        return raw, e + 9

    def _expand_object_streams(self) -> None:
        for obj in list(self.objects.values()):
            if not (isinstance(obj, Stream) and obj.dict.get("Type") == "ObjStm"):
                continue
            try:
                data = self.decode(obj)
                n, first = int(obj.dict.get("N") or 0), int(obj.dict.get("First") or 0)
                lx = _Lexer(data)
                nums = [lx.token() for _ in range(2 * n)]
                for i in range(0, len(nums) - 1, 2):
                    onum, off = nums[i], nums[i + 1]
                    if type(onum) is not int or type(off) is not int or onum in self.objects:
                        continue
                    self.objects[onum] = _Lexer(data, first + off).parse()
            except Exception:
                continue

    def resolve(self, obj: Any, depth: int = 0) -> Any:
        while isinstance(obj, Ref) and depth < 32:
            obj = self.objects.get(obj.num)
            depth += 1
        return obj

    def decode(self, s: Stream) -> bytes:
        filters = self.resolve(s.dict.get("Filter"))
        if filters is None:
            return s.raw
        if not isinstance(filters, list):
            filters = [filters]
        data = s.raw
        for f in filters:
            f = str(self.resolve(f))
            if f in ("FlateDecode", "Fl"):
                data = _inflate(data)
            elif f in ("ASCIIHexDecode", "AHx"):
                data = _ascii_hex(data)
            elif f in ("ASCII85Decode", "A85"):
                data = _ascii85(data)
            else:  # images (DCT, CCITT, JBIG2, JPX) and rare LZW/RunLength: no text inside
                raise PdfError(f"unsupported stream filter {f}")
        return data

    # -- pages -----------------------------------------------------------
    def pages(self) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """[(page dict, resources)] in reading order."""
        out: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        seen: set = set()

        def walk(node: Any, res: Dict[str, Any], depth: int) -> None:
            if not isinstance(node, dict) or id(node) in seen or depth > 64:
                return
            seen.add(id(node))
            own = self.resolve(node.get("Resources"))
            res = own if isinstance(own, dict) else res
            kids = self.resolve(node.get("Kids"))
            if node.get("Type") == "Pages" or (node.get("Type") != "Page" and isinstance(kids, list)):
                for k in kids or []:
                    walk(self.resolve(k), res, depth + 1)
            else:
                out.append((node, res))

        root = None
        for t in reversed(self.trailers):
            root = self.resolve(t.get("Root"))
            if isinstance(root, dict):
                break
        if not isinstance(root, dict):
            root = next((o for o in self.objects.values()
                         if isinstance(o, dict) and o.get("Type") == "Catalog"), None)
        if isinstance(root, dict):
            walk(self.resolve(root.get("Pages")), {}, 0)
        if not out:  # broken page tree: every /Type /Page in object order
            for num in sorted(self.objects):
                o = self.objects[num]
                if isinstance(o, dict) and o.get("Type") == "Page":
                    r = self.resolve(o.get("Resources"))
                    out.append((o, r if isinstance(r, dict) else {}))
        return out

    def page_content(self, page: Dict[str, Any]) -> bytes:
        c = self.resolve(page.get("Contents"))
        parts = c if isinstance(c, list) else [c]
        chunks = []
        for part in parts:
            s = self.resolve(part)
            if isinstance(s, Stream):
                try:
                    chunks.append(self.decode(s))
                except PdfError as e:
                    self.undecodable.append(str(e))
                    continue
        return b"\n".join(chunks)


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------

# Helvetica advance widths for ASCII 32..126 (also used for the other standard fonts).
_HELVETICA = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]

_GLYPHS = {
    "space": " ", "exclam": "!", "quotedbl": '"', "numbersign": "#", "dollar": "$", "percent": "%",
    "ampersand": "&", "quotesingle": "'", "quoteright": "’", "quoteleft": "‘",
    "parenleft": "(", "parenright": ")", "asterisk": "*", "plus": "+", "comma": ",", "hyphen": "-",
    "minus": "-", "period": ".", "slash": "/", "zero": "0", "one": "1", "two": "2", "three": "3",
    "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "colon": ":",
    "semicolon": ";", "less": "<", "equal": "=", "greater": ">", "question": "?", "at": "@",
    "bracketleft": "[", "backslash": "\\", "bracketright": "]", "asciicircum": "^",
    "underscore": "_", "grave": "`", "braceleft": "{", "bar": "|", "braceright": "}",
    "asciitilde": "~", "endash": "–", "emdash": "—", "bullet": "•",
    "quotedblleft": "“", "quotedblright": "”", "ellipsis": "…", "nbspace": " ",
    "fi": "fi", "fl": "fl", "ff": "ff", "ffi": "ffi", "ffl": "ffl", "degree": "°",
    "section": "§", "paragraph": "¶", "copyright": "©", "registered": "®",
    "trademark": "™", "cent": "¢", "sterling": "£", "yen": "¥", "Euro": "€",
}


def _glyph_char(name: str) -> Optional[str]:
    if name in _GLYPHS:
        return _GLYPHS[name]
    if len(name) == 1 and name.isalpha():
        return name
    m = re.fullmatch(r"uni([0-9A-Fa-f]{4})+", name)
    if m:
        try:
            return "".join(chr(int(name[i:i + 4], 16)) for i in range(3, len(name), 4))
        except ValueError:
            return None
    m = re.fullmatch(r"u([0-9A-Fa-f]{4,6})", name)
    if m:
        try:
            return chr(int(m.group(1), 16))
        except ValueError:
            return None
    return None


def parse_cmap(data: bytes) -> Tuple[Dict[int, str], set]:
    """ToUnicode CMap -> ({code: text}, {code byte lengths from the codespace ranges})."""
    cmap: Dict[int, str] = {}
    lengths: set = set()

    def hexval(h: bytes) -> bytes:
        h = re.sub(rb"\s", b"", h)
        if len(h) % 2:
            h += b"0"
        return bytes.fromhex(h.decode("ascii"))

    def text(b: bytes) -> Optional[str]:
        if len(b) == 1:
            s = chr(b[0])
        else:
            if len(b) % 2:
                b = b + b"\x00"
            s = b.decode("utf-16-be", "ignore")
        s = s.replace("\x00", "")
        return s if s and s != "�" else None

    for blk in re.finditer(rb"begincodespacerange(.*?)endcodespacerange", data, re.S):
        for h in re.findall(rb"<([0-9A-Fa-f\s]*)>", blk.group(1)):
            lengths.add(max(1, len(re.sub(rb"\s", b"", h)) // 2))
    tok_rx = re.compile(rb"<([0-9A-Fa-f\s]*)>|(\[)|(\])|/([^\s<>\[\]/()]+)")
    for blk in re.finditer(rb"beginbfchar(.*?)endbfchar", data, re.S):
        toks = [m for m in tok_rx.finditer(blk.group(1))]
        for i in range(0, len(toks) - 1, 2):
            src, dst = toks[i], toks[i + 1]
            if src.group(1) is None:
                continue
            code = int.from_bytes(hexval(src.group(1)), "big") if src.group(1).strip() else 0
            if dst.group(1) is not None:
                t = text(hexval(dst.group(1)))
            elif dst.group(4) is not None:
                t = _glyph_char(dst.group(4).decode("latin-1"))
            else:
                t = None
            if t:
                cmap[code] = t
    for blk in re.finditer(rb"beginbfrange(.*?)endbfrange", data, re.S):
        toks = list(tok_rx.finditer(blk.group(1)))
        i = 0
        while i + 2 < len(toks):
            lo_t, hi_t, dst = toks[i], toks[i + 1], toks[i + 2]
            if lo_t.group(1) is None or hi_t.group(1) is None:
                i += 1
                continue
            lo = int.from_bytes(hexval(lo_t.group(1)), "big")
            hi = int.from_bytes(hexval(hi_t.group(1)), "big")
            if hi < lo or hi - lo > 65535:
                i += 3
                continue
            if dst.group(2) is not None:  # [<d1> <d2> ...]
                j, k = i + 3, 0
                while j < len(toks) and toks[j].group(3) is None:
                    if toks[j].group(1) is not None and lo + k <= hi:
                        t = text(hexval(toks[j].group(1)))
                        if t:
                            cmap[lo + k] = t
                        k += 1
                    j += 1
                i = j + 1
                continue
            if dst.group(1) is not None:
                base = hexval(dst.group(1))
                start = int.from_bytes(base, "big") if base else 0
                for k in range(hi - lo + 1):
                    try:
                        t = text((start + k).to_bytes(max(1, len(base)), "big"))
                    except OverflowError:
                        break
                    if t:
                        cmap[lo + k] = t
            i += 3
    return cmap, lengths


class _Font:
    def __init__(self, doc: Document, fd: Dict[str, Any]) -> None:
        self.cid = fd.get("Subtype") == "Type0"
        self.code_len = 2 if self.cid else 1
        self.cmap: Dict[int, str] = {}
        encoding = str(doc.resolve(fd.get("Encoding")) or "")
        tu = doc.resolve(fd.get("ToUnicode"))
        if isinstance(tu, Stream):
            try:
                self.cmap, lengths = parse_cmap(doc.decode(tu))
                # Simple fonts always use 1-byte codes and Identity-H/V 2-byte codes, whatever
                # codespace the producer wrote into ToUnicode; only other CID encodings use it.
                if self.cid and encoding not in ("Identity-H", "Identity-V") and len(lengths) == 1:
                    self.code_len = min(4, lengths.pop())
            except (PdfError, ValueError):
                self.cmap = {}
        base_font = str(doc.resolve(fd.get("BaseFont")) or "")
        self.table: List[Optional[str]] = [None] * 256
        self.widths: Dict[int, float] = {}
        self.default_width = 1000.0 if self.cid else 500.0
        if self.cid:
            self._cid_widths(doc, fd)
        else:
            self._simple_encoding(doc, fd)
            self._simple_widths(doc, fd, base_font)

    def _simple_encoding(self, doc: Document, fd: Dict[str, Any]) -> None:
        enc = doc.resolve(fd.get("Encoding"))
        base, diffs = None, None
        if isinstance(enc, Name):
            base = str(enc)
        elif isinstance(enc, dict):
            b = doc.resolve(enc.get("BaseEncoding"))
            base = str(b) if b else None
            diffs = doc.resolve(enc.get("Differences"))
        codec = {"WinAnsiEncoding": "cp1252", "MacRomanEncoding": "mac_roman"}.get(base or "", "cp1252")
        for i in range(32, 256):
            try:
                ch = bytes([i]).decode(codec)
            except UnicodeDecodeError:
                ch = bytes([i]).decode("latin-1")
            self.table[i] = ch if ch.isprintable() or ch == " " else None
        if base == "StandardEncoding" or base is None:
            self.table[0x27], self.table[0x60] = "'", "`"
        if isinstance(diffs, list):
            code = 0
            for item in diffs:
                item = doc.resolve(item)
                if type(item) is int:
                    code = item
                elif isinstance(item, Name):
                    if 0 <= code < 256:
                        self.table[code] = _glyph_char(str(item))
                    code += 1

    def _simple_widths(self, doc: Document, fd: Dict[str, Any], base_font: str) -> None:
        widths = doc.resolve(fd.get("Widths"))
        first = doc.resolve(fd.get("FirstChar"))
        desc = doc.resolve(fd.get("FontDescriptor"))
        if isinstance(desc, dict):
            mw = doc.resolve(desc.get("MissingWidth"))
            if isinstance(mw, (int, float)) and mw > 0:
                self.default_width = float(mw)
        if isinstance(widths, list) and type(first) is int:
            for i, w in enumerate(widths):
                w = doc.resolve(w)
                if isinstance(w, (int, float)):
                    self.widths[first + i] = float(w)
        elif "Courier" in base_font:
            self.default_width = 600.0
        else:
            for i, w in enumerate(_HELVETICA):
                self.widths[32 + i] = float(w)

    def _cid_widths(self, doc: Document, fd: Dict[str, Any]) -> None:
        desc = doc.resolve(fd.get("DescendantFonts"))
        if isinstance(desc, list) and desc:
            desc = doc.resolve(desc[0])
        if not isinstance(desc, dict):
            return
        dw = doc.resolve(desc.get("DW"))
        if isinstance(dw, (int, float)):
            self.default_width = float(dw)
        w = doc.resolve(desc.get("W"))
        if not isinstance(w, list):
            return
        i = 0
        while i < len(w):
            c = doc.resolve(w[i])
            nxt = doc.resolve(w[i + 1]) if i + 1 < len(w) else None
            if type(c) is int and isinstance(nxt, list):
                for k, v in enumerate(nxt):
                    v = doc.resolve(v)
                    if isinstance(v, (int, float)):
                        self.widths[c + k] = float(v)
                i += 2
            elif type(c) is int and type(nxt) is int and i + 2 < len(w):
                v = doc.resolve(w[i + 2])
                if isinstance(v, (int, float)) and 0 <= nxt - c <= 65535:
                    for code in range(c, nxt + 1):
                        self.widths[code] = float(v)
                i += 3
            else:
                i += 1

    def glyphs(self, s: bytes) -> List[Tuple[Optional[str], float, bool]]:
        """[(text or None if unmapped, advance in 1/1000 em, is single-byte space)]."""
        out = []
        step = self.code_len
        for i in range(0, len(s) - step + 1, step):
            code = s[i] if step == 1 else int.from_bytes(s[i:i + step], "big")
            ch = self.cmap.get(code)
            if ch is None and not self.cid and code < 256:
                ch = self.table[code]
            out.append((ch, self.widths.get(code, self.default_width), step == 1 and code == 32))
        return out


# ---------------------------------------------------------------------------
# Content interpretation and line rebuilding
# ---------------------------------------------------------------------------

_IDENT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _mul(m: Sequence[float], n: Sequence[float]) -> Tuple[float, ...]:
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D,
            e * A + f * C + E, e * B + f * D + F)


def _skip_inline_image(data: bytes, pos: int) -> int:
    i = data.find(b"ID", pos)
    if i < 0:
        return len(data)
    m = re.compile(rb"\sEI(?=[\s/\[<(]|$)").search(data, i + 2)
    return m.end() if m else len(data)


class _Interpreter:
    def __init__(self, doc: Document) -> None:
        self.doc = doc
        self.fonts: Dict[int, Optional[_Font]] = {}
        self.pieces: List[Tuple[float, float, float, float, str]] = []  # y, x0, x1, size, text
        self.unmapped = 0  # glyphs drawn that have no Unicode mapping

    def font(self, resources: Dict[str, Any], name: Any) -> Optional[_Font]:
        fonts = self.doc.resolve(resources.get("Font")) if isinstance(resources, dict) else None
        fd = self.doc.resolve(fonts.get(str(name))) if isinstance(fonts, dict) else None
        if not isinstance(fd, dict):
            return None
        key = id(fd)
        if key not in self.fonts:
            try:
                self.fonts[key] = _Font(self.doc, fd)
            except Exception:
                self.fonts[key] = None
        return self.fonts[key]

    def run(self, data: bytes, resources: Dict[str, Any], ctm: Tuple[float, ...], depth: int = 0) -> None:
        gs: Dict[str, Any] = {"ctm": ctm, "font": None, "size": 0.0, "tc": 0.0, "tw": 0.0,
                              "th": 1.0, "tl": 0.0, "rise": 0.0}
        stack: List[Dict[str, Any]] = []
        tm = tlm = _IDENT
        lx = _Lexer(data)
        ops: List[Any] = []
        while True:
            tok = lx.token()
            if tok is None:
                break
            if not isinstance(tok, Op):
                ops.append(tok)
                continue
            if tok in ("[", "<<"):
                ops.append(lx.value(tok, refs=False))
                continue
            op = str(tok)
            try:
                if op == "BI":
                    lx.pos = _skip_inline_image(data, lx.pos)
                elif op == "q":
                    stack.append(dict(gs))
                elif op == "Q":
                    if stack:
                        gs = stack.pop()
                elif op == "cm" and len(ops) >= 6:
                    gs["ctm"] = _mul([float(x) for x in ops[-6:]], gs["ctm"])
                elif op == "BT":
                    tm = tlm = _IDENT
                elif op == "Tf" and len(ops) >= 2:
                    gs["font"] = self.font(resources, ops[-2])
                    gs["size"] = float(ops[-1])
                elif op == "Tc" and ops:
                    gs["tc"] = float(ops[-1])
                elif op == "Tw" and ops:
                    gs["tw"] = float(ops[-1])
                elif op == "Tz" and ops:
                    gs["th"] = float(ops[-1]) / 100.0
                elif op == "TL" and ops:
                    gs["tl"] = float(ops[-1])
                elif op == "Ts" and ops:
                    gs["rise"] = float(ops[-1])
                elif op in ("Td", "TD") and len(ops) >= 2:
                    tx, ty = float(ops[-2]), float(ops[-1])
                    if op == "TD":
                        gs["tl"] = -ty
                    tlm = _mul((1.0, 0.0, 0.0, 1.0, tx, ty), tlm)
                    tm = tlm
                elif op == "Tm" and len(ops) >= 6:
                    tlm = tm = tuple(float(x) for x in ops[-6:])
                elif op == "T*":
                    tlm = _mul((1.0, 0.0, 0.0, 1.0, 0.0, -gs["tl"]), tlm)
                    tm = tlm
                elif op == "Tj" and ops:
                    tm = self.show(gs, tm, ops[-1])
                elif op in ("'", '"') and ops:
                    if op == '"' and len(ops) >= 3:
                        gs["tw"], gs["tc"] = float(ops[-3]), float(ops[-2])
                    tlm = _mul((1.0, 0.0, 0.0, 1.0, 0.0, -gs["tl"]), tlm)
                    tm = self.show(gs, tlm, ops[-1])
                elif op == "TJ" and ops and isinstance(ops[-1], list):
                    for el in ops[-1]:
                        if isinstance(el, bytes):
                            tm = self.show(gs, tm, el)
                        elif isinstance(el, (int, float)):
                            tx = -float(el) / 1000.0 * gs["size"] * gs["th"]
                            tm = (tm[0], tm[1], tm[2], tm[3], tm[4] + tx * tm[0], tm[5] + tx * tm[1])
                elif op == "Do" and ops and depth < 6:
                    self.form(ops[-1], resources, gs["ctm"], depth)
            except (TypeError, ValueError, ZeroDivisionError, OverflowError):
                pass  # one malformed operator must not lose the page
            ops = []

    def form(self, name: Any, resources: Dict[str, Any], ctm: Tuple[float, ...], depth: int) -> None:
        xobjs = self.doc.resolve(resources.get("XObject")) if isinstance(resources, dict) else None
        xo = self.doc.resolve(xobjs.get(str(name))) if isinstance(xobjs, dict) else None
        if not isinstance(xo, Stream) or xo.dict.get("Subtype") != "Form":
            return
        try:
            data = self.doc.decode(xo)
        except PdfError:
            return
        m = self.doc.resolve(xo.dict.get("Matrix"))
        mat = tuple(float(self.doc.resolve(x)) for x in m) if isinstance(m, list) and len(m) == 6 else _IDENT
        res = self.doc.resolve(xo.dict.get("Resources"))
        self.run(data, res if isinstance(res, dict) else resources, _mul(mat, ctm), depth + 1)

    def show(self, gs: Dict[str, Any], tm: Tuple[float, ...], s: Any) -> Tuple[float, ...]:
        font: Optional[_Font] = gs["font"]
        if font is None or not isinstance(s, bytes):
            return tm
        size, th, tc, tw = gs["size"], gs["th"], gs["tc"], gs["tw"]
        run: List[str] = []
        start = None
        for ch, w, is_space in font.glyphs(s):
            if ch is not None:
                if start is None:
                    start = tm
                run.append(ch)
            else:
                self.unmapped += 1
                if start is not None:
                    self.emit(start, tm, "".join(run), gs)
                    start, run = None, []
            adv = (w / 1000.0 * size + tc + (tw if is_space else 0.0)) * th
            tm = (tm[0], tm[1], tm[2], tm[3], tm[4] + adv * tm[0], tm[5] + adv * tm[1])
        if start is not None:
            self.emit(start, tm, "".join(run), gs)
        return tm

    def emit(self, start: Tuple[float, ...], end: Tuple[float, ...], text: str, gs: Dict[str, Any]) -> None:
        m0 = _mul(start, gs["ctm"])
        m1 = _mul(end, gs["ctm"])
        if abs(m0[1]) > 0.2 * abs(m0[0]) + 1e-9:  # rotated / vertical text (stamps, watermarks)
            return
        rise = gs["rise"]
        x0, y = m0[2] * rise + m0[4], m0[3] * rise + m0[5]
        x1 = m1[2] * rise + m1[4]
        size = abs(gs["size"]) * math.hypot(m0[2], m0[3]) or abs(gs["size"]) or 1.0
        if x1 < x0:
            x0, x1 = x1, x0
        self.pieces.append((y, x0, x1, size, text))


def _lines(pieces: List[Tuple[float, float, float, float, str]]) -> List[str]:
    """Group positioned text into visual lines (top to bottom, then left to right)."""
    rows: List[List[Tuple[float, float, float, float, str]]] = []
    ref_y = None
    ref_size = 0.0
    for p in sorted(pieces, key=lambda q: (-q[0], q[1])):
        if rows and ref_y is not None and abs(ref_y - p[0]) <= 0.5 * max(ref_size, p[3], 1.0):
            rows[-1].append(p)
            ref_size = max(ref_size, p[3])
        else:
            rows.append([p])
            ref_y, ref_size = p[0], p[3]
    out: List[str] = []
    for row in rows:
        row.sort(key=lambda q: q[1])
        parts: List[str] = []
        prev_x1 = None
        for y, x0, x1, size, text in row:
            if prev_x1 is not None and x0 - prev_x1 > 0.15 * size and parts \
                    and not parts[-1].endswith(" ") and not text.startswith(" "):
                parts.append(" ")
            parts.append(text)
            prev_x1 = x1 if prev_x1 is None else max(prev_x1, x1)
        line = " ".join("".join(parts).split())
        if line:
            out.append(line)
    return out


def extract_text_stdlib(data: bytes) -> str:
    """Visual-line text of every page (pages separated by blank lines). '' if no text layer."""
    doc = Document(data)
    pages = doc.pages()
    if not pages:
        raise PdfError("no pages found (damaged PDF)")
    pages_out: List[str] = []
    unmapped = 0
    for page, res in pages:
        interp = _Interpreter(doc)
        content = doc.page_content(page)
        if content:
            interp.run(content, res, _IDENT)
        unmapped += interp.unmapped
        pages_out.append("\n".join(_lines(interp.pieces)))
    text = "\n\n".join(p for p in pages_out if p).strip()
    # No text is only proof of a scanned (paper) filing when nothing got in the way of reading it.
    if not text and doc.undecodable:
        raise PdfTextError(f"page content could not be decoded ({doc.undecodable[0]})")
    if not text and unmapped:
        raise PdfTextError(f"{unmapped} text glyphs have no Unicode mapping")
    return text


# ---------------------------------------------------------------------------
# Engine selection (pypdf is optional)
# ---------------------------------------------------------------------------

def have_pypdf() -> bool:
    try:
        import pypdf  # noqa: F401
        return True
    except Exception:
        return False


def extract_text_pypdf(data: bytes) -> str:
    import io
    import pypdf
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    reader = pypdf.PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted:  # owner-password-only PDFs open with an empty user password
        reader.decrypt("")
    return "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()


def engines() -> List[str]:
    """Extractors to try, in order: pypdf first when importable (set $DAYTRADER_PDF_ENGINE to force one)."""
    want = (os.environ.get(ENGINE_ENV) or "auto").strip().lower()
    if want == "stdlib":
        return ["stdlib"]
    if want == "pypdf":
        return ["pypdf"] if have_pypdf() else ["stdlib"]
    return (["pypdf"] if have_pypdf() else []) + ["stdlib"]


def extract_text(data: bytes, engine: str = "stdlib") -> str:
    if engine == "pypdf":
        return extract_text_pypdf(data)
    return extract_text_stdlib(data)


if __name__ == "__main__":  # pragma: no cover - manual debugging aid
    import argparse
    ap = argparse.ArgumentParser(description="Print the text layer of a PDF.")
    ap.add_argument("pdf")
    ap.add_argument("--engine", choices=("stdlib", "pypdf"), default="stdlib")
    a = ap.parse_args()
    with open(a.pdf, "rb") as fh:
        blob = fh.read()
    try:
        print(extract_text(blob, a.engine))
    except PdfError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
