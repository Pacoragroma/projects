"""tools/binance_live.py: the WebSocket frames, the summary and the input checks, all offline.

The live stream itself is exercised by running the tool; these tests never open a network socket
except to a closed local port, which must read as "data source down" (exit 2).
"""
import importlib.util
import json
import os
import struct
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import ROOT, run_cli  # noqa: E402

TOOL = ROOT / "tools" / "binance_live.py"
OPEN_MS = int(datetime(2026, 11, 1, 11, 24, tzinfo=timezone.utc).timestamp() * 1000)
_spec = importlib.util.spec_from_file_location("binance_live", TOOL)
live = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(live)


class FakeSocket:
    """recv() hands out the queued bytes in small chunks; sendall() records what was sent."""

    def __init__(self, data=b"", chunk=7):
        self.data, self.chunk, self.sent = bytearray(data), chunk, []

    def recv(self, n):
        out = bytes(self.data[:min(n, self.chunk)])
        del self.data[:len(out)]
        return out

    def sendall(self, b):
        self.sent.append(bytes(b))


def frame(opcode, payload, mask=None):
    n = len(payload)
    head = bytes([0x80 | opcode])
    mbit = 0x80 if mask else 0
    if n < 126:
        head += bytes([mbit | n])
    elif n < 65536:
        head += bytes([mbit | 126]) + struct.pack(">H", n)
    else:
        head += bytes([mbit | 127]) + struct.pack(">Q", n)
    if mask:
        return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return head + payload


def agg(sym, price, qty, buyer_is_maker):
    return {"stream": f"{sym.lower()}@aggTrade", "data": {"e": "aggTrade", "s": sym, "p": str(price), "q": str(qty),
                                                         "m": buyer_is_maker}}


class FrameTests(unittest.TestCase):
    def test_short_long_and_masked_frames_decode(self):
        big = json.dumps({"stream": "x", "data": {"pad": "y" * 300}}).encode()
        sock = FakeSocket(frame(1, b'{"a":1}') + frame(1, big) + frame(1, b"masked", mask=b"\x01\x02\x03\x04")
                          + frame(9, b"hi"))
        f = live.Frames(sock)
        self.assertEqual(f.next(), (1, b'{"a":1}'))
        self.assertEqual(f.next(), (1, big))                          # 16-bit length
        self.assertEqual(f.next(), (1, b"masked"))
        self.assertEqual(f.next(), (9, b"hi"))

    def test_client_frames_are_masked_and_control_frames_stay_small(self):
        sock = FakeSocket()
        live.Frames(sock).send(live.OP_PONG, b"hi")
        [sent] = sock.sent
        self.assertEqual((sent[0], sent[1]), (0x80 | 10, 0x80 | 2))   # FIN + pong, masked, 2 bytes
        mask = sent[2:6]
        self.assertEqual(bytes(b ^ mask[i % 4] for i, b in enumerate(sent[6:])), b"hi")
        with self.assertRaises(ValueError):
            live.Frames(sock).send(live.OP_PONG, b"x" * 126)

    def test_a_closed_stream_is_a_data_error(self):
        with self.assertRaises(live.tc.FetchError):
            live.Frames(FakeSocket(b"\x81")).next()

    def test_a_header_less_proxy_answer_parses(self):
        status, rest = live._read_head(FakeSocket(b"HTTP/1.1 200 Connection established\r\n\r\n"))
        self.assertEqual((status, rest), ("HTTP/1.1 200 Connection established", b""))
        for chunk in (7, 4096):                      # frames may arrive with the header or after it
            sock = FakeSocket(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n" + frame(1, b"{}"),
                              chunk=chunk)
            status, rest = live._read_head(sock)
            self.assertEqual(status, "HTTP/1.1 101 Switching Protocols")
            self.assertEqual(live.Frames(sock, rest).next(), (1, b"{}"))

    def test_listen_answers_pings_and_stops_at_a_close(self):
        msg = json.dumps(agg("BTCUSDT", 100, 1, False)).encode()
        sock = FakeSocket(frame(9, b"p") + frame(1, msg) + frame(8, b"") + frame(1, msg))
        ticks = iter(range(100))
        out = live.listen(live.Frames(sock), seconds=50, clock=lambda: next(ticks))
        self.assertEqual(out, [json.loads(msg)])                      # the frame after the close is not read
        self.assertEqual(sock.sent[0][0], 0x80 | 10)                  # the pong
        self.assertEqual(sock.sent[-1][0], 0x80 | 8)                  # and our own close


class SummaryTests(unittest.TestCase):
    def test_flow_range_candle_and_book(self):
        msgs = [agg("BTCUSDT", 100.0, 2.0, False),                    # a buyer lifted the ask: $200
                agg("BTCUSDT", 99.0, 1.0, True),                      # a seller hit the bid: $99
                agg("BTCUSDT", 101.0, 5.0, False),                    # $505, the biggest
                agg("ETHUSDT", 10.0, 3.0, True),
                agg("DOGEUSDT", 1.0, 1.0, False),                     # not asked for: ignored
                {"stream": "btcusdt@kline_1m", "data": {"k": {"s": "BTCUSDT", "i": "1m", "t": OPEN_MS,
                 "o": "100", "h": "101", "l": "99", "c": "101", "v": "8", "x": False}}},
                {"stream": "btcusdt@depth5@1000ms", "data": {"bids": [["100.99", "1.5"], ["100.98", "0.5"]],
                                                             "asks": [["101.01", "2"], ["101.02", "1"]]}}]
        s = live.summarize(msgs, ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        btc = s["pairs"]["BTCUSDT"]
        self.assertEqual((btc["trades"], btc["traded_usd"], btc["buy_usd"], btc["sell_usd"]), (3, 804.0, 705.0, 99.0))
        self.assertEqual((btc["last"], btc["high"], btc["low"], btc["buy_share_pct"]), (101.0, 101.0, 99.0, 87.7))
        self.assertEqual(btc["biggest"], {"usd": 505.0, "side": "buy", "price": 101.0})
        self.assertEqual((s["pairs"]["ETHUSDT"]["sell_usd"], s["pairs"]["ETHUSDT"]["buy_share_pct"]), (30.0, 0.0))
        self.assertEqual((s["pairs"]["SOLUSDT"]["trades"], s["pairs"]["SOLUSDT"]["buy_share_pct"]), (0, None))
        self.assertNotIn("DOGEUSDT", s["pairs"])
        self.assertEqual((s["candle_1m"]["opened"], s["candle_1m"]["close"], s["candle_1m"]["closed"]),
                         ("11:24 UTC", 101.0, False))
        self.assertEqual((s["book"]["best_bid"], s["book"]["best_ask"], s["book"]["bid_size_top5"],
                          s["book"]["ask_size_top5"]), (100.99, 101.01, 2.0, 3.0))
        self.assertAlmostEqual(s["book"]["spread"], 0.02, places=9)
        self.assertEqual(s["messages"], 7)
        text = live.render({"host": live.HOST, "as_of": "x", "seconds": 15, "real_money": False, **s})
        self.assertIn("88% buyers", text)
        self.assertIn("SOLUSDT     no trades in the window", text)

    def test_stream_path_and_pairs(self):
        self.assertEqual(live.parse_pairs("BTC-USDT, ethusdt,BTCUSDT"), ["BTCUSDT", "ETHUSDT"])
        self.assertEqual(live.stream_path(["BTCUSDT", "ETHUSDT"]),
                         "/stream?streams=btcusdt@aggTrade/ethusdt@aggTrade/btcusdt@kline_1m/btcusdt@depth5@1000ms")
        for bad, words in (("TSLA", "not a crypto pair"), (" , ", "is empty"),
                           (",".join(f"C{i}-USDT" for i in range(11)), "at most 10")):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, words):
                live.parse_pairs(bad)


class CliTests(unittest.TestCase):
    def test_bad_input_is_exit_1_before_any_connection(self):
        unreachable = {"HTTPS_PROXY": "http://127.0.0.1:9"}           # a connection would fail with exit 2
        for args, words in ((["--seconds", "0"], "--seconds must be above 0"),
                            (["--seconds", "121"], "at most 120"),
                            (["--symbols", "TSLA"], "not a crypto pair")):
            with self.subTest(args=args):
                r = run_cli(TOOL, *args, env=unreachable)
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn(words, r.stderr)
                self.assertNotIn("Traceback", r.stderr)

    def test_an_unreachable_stream_is_exit_2(self):
        r = run_cli(TOOL, "--seconds", "1", env={"HTTPS_PROXY": "http://127.0.0.1:9"})
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("data error: cannot reach data-stream.binance.vision", r.stderr)
        self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
