#!/usr/bin/env python3
"""Watch Binance live for a few seconds: trades, buy vs sell flow, the forming 1-minute candle and
the top of the order book. PUBLIC market data only.

It listens to Binance's public market-data WebSocket (data-stream.binance.vision): no account, no
keys, and nothing is sent but the subscription, so nothing here can place an order. The main stream
(stream.binance.com) answers 451 from US cloud servers; this mirror does not. Standard library
only. Behind an HTTPS proxy ($HTTPS_PROXY) it opens a CONNECT tunnel.

  python3 tools/binance_live.py                          # 15 s of BTC-USDT and ETH-USDT
  python3 tools/binance_live.py --symbols SOL-USDT,ZAMA-USDT --seconds 30 --json

Per pair: trades, the dollars traded, how much of it was buyers lifting the ask versus sellers
hitting the bid (Binance's "buyer is the maker" flag), the price range, the last price and the
biggest single trade. For the first pair also the forming 1-minute candle and the best bid and ask
with the size on the top five levels. Exit codes: 0 ok, 1 bad input, 2 the stream is unreachable.
"""
import base64
import json
import os
import socket
import ssl
import struct
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
import trader_common as tc  # noqa: E402

HOST = "data-stream.binance.vision"
PORT = 443
MAX_SECONDS = 120
MAX_SYMBOLS = 10
OP_TEXT, OP_CLOSE, OP_PING, OP_PONG = 1, 8, 9, 10


# ---------------------------------------------------------------------------
# A minimal WebSocket client (RFC 6455): enough for a read-only market-data stream
# ---------------------------------------------------------------------------

def _read_head(sock):
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            raise tc.FetchError(f"{HOST} closed the connection before answering")
        head += chunk
        if len(head) > 65536:
            raise tc.FetchError(f"{HOST}: an oversized response header")
    headers, _, rest = head.partition(b"\r\n\r\n")      # a proxy's "200 Connection established" has no headers
    return headers.split(b"\r\n", 1)[0].decode("latin-1"), rest


def open_tunnel(host=HOST, port=PORT, timeout=15.0):
    """A TLS socket to host:port, through $HTTPS_PROXY's CONNECT tunnel when one is set."""
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    try:
        if proxy:
            p = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
            raw = socket.create_connection((p.hostname, p.port or 80), timeout=timeout)
            req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            if p.username:
                cred = f"{urllib.parse.unquote(p.username)}:{urllib.parse.unquote(p.password or '')}"
                req += f"Proxy-Authorization: Basic {base64.b64encode(cred.encode()).decode()}\r\n"
            raw.sendall((req + "\r\n").encode())
            status, _ = _read_head(raw)
            if " 200" not in status:
                raise tc.FetchError(f"the proxy refused a tunnel to {host} ({status}); "
                                    "it may need to be allow-listed")
        else:
            raw = socket.create_connection((host, port), timeout=timeout)
        return ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    except (OSError, ssl.SSLError) as e:
        raise tc.FetchError(f"cannot reach {host}: {e}") from e


def stream_path(pairs):
    """The combined-stream path: aggregate trades for every pair, plus the first pair's 1-minute
    candle and top-five order book (once a second)."""
    low = [p.lower() for p in pairs]
    streams = [f"{p}@aggTrade" for p in low] + [f"{low[0]}@kline_1m", f"{low[0]}@depth5@1000ms"]
    return "/stream?streams=" + "/".join(streams)


class Frames:
    """Reads server frames from a socket (servers never mask); sends masked client frames."""

    def __init__(self, sock, buf=b""):
        self.sock, self.buf = sock, buf

    def _take(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise tc.FetchError(f"{HOST} closed the stream")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def next(self):
        """(opcode, payload) of the next frame. Binance sends whole text frames; continuation
        frames are not expected and are returned as they come."""
        b0, b1 = self._take(2)
        opcode, n = b0 & 0x0F, b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._take(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._take(8))[0]
        mask = self._take(4) if b1 & 0x80 else None
        data = self._take(n)
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return opcode, data

    def send(self, opcode, payload=b""):
        if len(payload) > 125:
            raise ValueError("control frames carry at most 125 bytes")
        mask = os.urandom(4)
        self.sock.sendall(bytes([0x80 | opcode, 0x80 | len(payload)]) + mask
                          + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def connect(path, timeout=15.0):
    sock = open_tunnel(timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    try:
        sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {HOST}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        status, rest = _read_head(sock)
    except OSError as e:
        raise tc.FetchError(f"{HOST}: {e}") from e
    if " 101" not in status:
        raise tc.FetchError(f"{HOST} did not open the stream ({status})")
    return Frames(sock, rest)


def listen(frames, seconds, clock=time.monotonic):
    """The stream's JSON messages for `seconds` (answers pings; stops early on a close)."""
    start, out = clock(), []
    while clock() - start < seconds:
        try:
            op, data = frames.next()
        except socket.timeout:
            continue
        except OSError as e:
            raise tc.FetchError(f"{HOST}: {e}") from e
        if op == OP_PING:
            frames.send(OP_PONG, data[:125])
        elif op == OP_CLOSE:
            break
        elif op == OP_TEXT:
            out.append(json.loads(data))
    try:
        frames.send(OP_CLOSE)
    except (OSError, ValueError):
        pass
    return out


# ---------------------------------------------------------------------------
# What happened
# ---------------------------------------------------------------------------

def summarize(messages, pairs):
    trades = {p: {"trades": 0, "traded_usd": 0.0, "buy_usd": 0.0, "sell_usd": 0.0, "last": None,
                  "high": None, "low": None, "biggest": None} for p in pairs}
    candle = book = None
    for m in messages:
        stream, d = m.get("stream", ""), m.get("data") or {}
        if stream.endswith("@aggTrade") and d.get("s") in trades:
            t = trades[d["s"]]
            px, usd = float(d["p"]), float(d["p"]) * float(d["q"])
            side = "sell" if d.get("m") else "buy"            # buyer is the maker: a seller hit the bid
            t["trades"] += 1
            t["traded_usd"] += usd
            t[f"{side}_usd"] += usd
            t["last"] = px
            t["high"] = px if t["high"] is None else max(t["high"], px)
            t["low"] = px if t["low"] is None else min(t["low"], px)
            if t["biggest"] is None or usd > t["biggest"]["usd"]:
                t["biggest"] = {"usd": usd, "side": side, "price": px}
        elif "@kline_" in stream and d.get("k"):
            k = d["k"]
            candle = {"symbol": k["s"], "interval": k["i"],
                      "opened": datetime.fromtimestamp(k["t"] / 1000, timezone.utc).strftime("%H:%M UTC"),
                      "open": float(k["o"]), "high": float(k["h"]), "low": float(k["l"]), "close": float(k["c"]),
                      "volume": float(k["v"]), "closed": bool(k["x"])}
        elif "@depth" in stream and d.get("bids") and d.get("asks"):
            bid, ask = float(d["bids"][0][0]), float(d["asks"][0][0])
            book = {"symbol": pairs[0], "best_bid": bid, "best_ask": ask, "spread": ask - bid,
                    "spread_bps": round((ask - bid) / ((ask + bid) / 2) * 1e4, 4),
                    "bid_size_top5": sum(float(q) for _, q in d["bids"]),
                    "ask_size_top5": sum(float(q) for _, q in d["asks"])}
    for t in trades.values():
        for k in ("traded_usd", "buy_usd", "sell_usd"):
            t[k] = round(t[k], 2)
        t["buy_share_pct"] = round(100 * t["buy_usd"] / t["traded_usd"], 1) if t["traded_usd"] else None
        if t["biggest"]:
            t["biggest"]["usd"] = round(t["biggest"]["usd"], 2)
    return {"pairs": trades, "candle_1m": candle, "book": book, "messages": len(messages)}


def render(out):
    lines = [f"BINANCE LIVE · {out['as_of']} · {out['seconds']:g} s on {out['host']} · {out['messages']} messages · "
             "public data, no account"]
    for pair, t in out["pairs"].items():
        if not t["trades"]:
            lines.append(f"  {pair:<11} no trades in the window")
            continue
        rng = (f"{tc.fmt_price(t['low'])}-{tc.fmt_price(t['high'])}" if t["low"] != t["high"]
               else tc.fmt_price(t["last"]))
        big = t["biggest"]
        lines.append(f"  {pair:<11} last {tc.fmt_price(t['last']):>11} · range {rng} · {t['trades']} trades, "
                     f"{tc.fmt_money(t['traded_usd'])} traded ({t['buy_share_pct']:.0f}% buyers) · biggest "
                     f"{tc.fmt_money(big['usd'])} {big['side']} at {tc.fmt_price(big['price'])}")
    c = out.get("candle_1m")
    if c:
        state = "closed" if c["closed"] else "forming"
        lines.append(f"  1-min candle {c['symbol']} {c['opened']} ({state}): O {tc.fmt_price(c['open'])} "
                     f"H {tc.fmt_price(c['high'])} L {tc.fmt_price(c['low'])} C {tc.fmt_price(c['close'])} · "
                     f"volume {c['volume']:g}")
    b = out.get("book")
    if b:
        lines.append(f"  order book {b['symbol']}: bid {tc.fmt_price(b['best_bid'])} / ask {tc.fmt_price(b['best_ask'])} "
                     f"(spread {b['spread']:.8g}, {b['spread_bps']:.4g} bps) · top-5 size {b['bid_size_top5']:g} bid vs "
                     f"{b['ask_size_top5']:g} ask")
    return "\n".join(lines)


def build_parser():
    p = tc.make_parser(__doc__.split("\n\n", 1)[0])
    p.add_argument("--symbols", default="BTC-USDT,ETH-USDT",
                   help=f"comma-separated Binance pairs, up to {MAX_SYMBOLS} (default BTC-USDT,ETH-USDT)")
    p.add_argument("--seconds", type=float, default=15.0, help=f"how long to listen, up to {MAX_SECONDS} (default 15)")
    return p


def parse_pairs(text):
    pairs = []
    for raw in (text or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        if not tc.is_crypto_symbol(raw):
            raise ValueError(f"{raw!r} is not a crypto pair (use e.g. BTC-USDT)")
        pair = tc.binance_symbol(raw)
        if pair not in pairs:
            pairs.append(pair)
    if not pairs:
        raise ValueError("--symbols is empty (use e.g. BTC-USDT,ETH-USDT)")
    if len(pairs) > MAX_SYMBOLS:
        raise ValueError(f"at most {MAX_SYMBOLS} pairs at once, not {len(pairs)}")
    return pairs


def main():
    args = build_parser().parse_args()
    if not 0 < args.seconds <= MAX_SECONDS:
        raise ValueError(f"--seconds must be above 0 and at most {MAX_SECONDS}, not {args.seconds:g}")
    pairs = parse_pairs(args.symbols)
    started = time.monotonic()
    frames = connect(stream_path(pairs))
    frames.sock.settimeout(2.0)
    messages = listen(frames, args.seconds)
    out = {"host": HOST, "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
           "seconds": round(time.monotonic() - started, 1), "real_money": False, **summarize(messages, pairs)}
    tc.emit(out, args.json, render)
    return 0


if __name__ == "__main__":
    tc.run(main)
