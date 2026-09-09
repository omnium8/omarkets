#!/usr/bin/env python3
"""Omarkets — local TradingView watchlist dashboard."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DEFAULT_WATCHLIST = ROOT / "data" / "watchlist.default.json"
TICKERS_PATH = ROOT / "data" / "tickers.json"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "omarkets"
WATCHLIST_PATH = STATE_DIR / "watchlist.json"
COLORS_TOML = (
    Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    / "omarchy"
    / "current"
    / "theme"
    / "colors.toml"
)
PORT = int(os.environ.get("OMARKETS_PORT", "1987"))
HOST = os.environ.get("OMARKETS_HOST", "127.0.0.1")

# TradingView: EXCHANGE:SYMBOL (allows ! for futures). Also plain / $TICKER aliases.
SYMBOL_RE = re.compile(r"^\$?[A-Za-z0-9][A-Za-z0-9._=/-]{0,31}$|^[A-Za-z0-9][A-Za-z0-9._]{0,15}:[A-Za-z0-9][A-Za-z0-9._=!/-]{0,31}$")
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
TV_SCANNER = "https://scanner.tradingview.com/global/scan"
TV_WS_HOST = "data.tradingview.com"
TV_WS_PATH = "/socket.io/websocket"
MAX_HTTP_BYTES = 1_500_000
HTTP_TIMEOUT = 12
QUOTE_CACHE_TTL = 60
CHART_GAP_MIN = 1.0  # randomized gap between chart fetches (1–2s, never bursts)
CHART_GAP_MAX = 2.0
CHART_REFRESH_SEC = 12 * 60  # re-pull each sparkline at most this often (5m bars)
CHART_REFRESH_JITTER = 90  # ± seconds per symbol so they don't all refresh at once
CHART_IDLE_SEC = 5.0  # warm-loop sleep when nothing needs a refresh

# Friendly / $ticker / legacy Yahoo → TradingView
ALIASES = {
    # indices
    "SPX": "SP:SPX",
    "$SPX": "SP:SPX",
    "SP500": "SP:SPX",
    "GSPC": "SP:SPX",
    "^GSPC": "SP:SPX",
    "DJI": "TVC:DJI",
    "$DJI": "TVC:DJI",
    "DJIA": "TVC:DJI",
    "^DJI": "TVC:DJI",
    "IXIC": "TVC:IXIC",
    "$IXIC": "TVC:IXIC",
    "NDX": "TVC:IXIC",
    "NASDAQ": "TVC:IXIC",
    "^IXIC": "TVC:IXIC",
    "DAX": "XETR:DAX",
    "$DAX": "XETR:DAX",
    "GDAXI": "XETR:DAX",
    "^GDAXI": "XETR:DAX",
    # rates
    "US10Y": "TVC:US10Y",
    "$US10Y": "TVC:US10Y",
    "TNX": "TVC:US10Y",
    "^TNX": "TVC:US10Y",
    "US01Y": "TVC:US01Y",
    "$US01Y": "TVC:US01Y",
    "US1Y": "TVC:US01Y",
    "IRX": "TVC:US01Y",
    "^IRX": "TVC:US01Y",
    # commodities
    "GC": "COMEX:GC1!",
    "$GC": "COMEX:GC1!",
    "GOLD": "COMEX:GC1!",
    "GC=F": "COMEX:GC1!",
    "SI": "COMEX:SI1!",
    "$SI": "COMEX:SI1!",
    "SILVER": "COMEX:SI1!",
    "SI=F": "COMEX:SI1!",
    "CL": "NYMEX:CL1!",
    "$CL": "NYMEX:CL1!",
    "$WTI": "NYMEX:CL1!",
    "WTI": "NYMEX:CL1!",
    "CL=F": "NYMEX:CL1!",
    # crypto
    "BTC": "CRYPTO:BTCUSD",
    "$BTC": "CRYPTO:BTCUSD",
    "BTCUSD": "CRYPTO:BTCUSD",
    "BTC-USD": "CRYPTO:BTCUSD",
    "ETH": "CRYPTO:ETHUSD",
    "$ETH": "CRYPTO:ETHUSD",
    "ETHUSD": "CRYPTO:ETHUSD",
    "ETH-USD": "CRYPTO:ETHUSD",
}

_cache_lock = threading.Lock()
_quote_cache: dict[str, dict] = {}  # symbol -> {ts, data}
_chart_lock = threading.Lock()
_chart_next = 0.0

# Top-N US tickers (data/tickers.json) for $TICKER search + resolution.
_tickers_lock = threading.Lock()
_tickers_list: list[dict] | None = None   # [{"s","n","d"}, ...] by market cap
_tickers_index: dict[str, str] = {}       # TICKER -> EXCHANGE:SYMBOL (first wins)


def load_tickers() -> list[dict]:
    """Load and cache the top-ticker list; empty list if unavailable."""
    global _tickers_list, _tickers_index
    with _tickers_lock:
        if _tickers_list is not None:
            return _tickers_list
        lst: list[dict] = []
        index: dict[str, str] = {}
        try:
            data = json.loads(TICKERS_PATH.read_text(encoding="utf-8"))
            for t in data.get("tickers") or []:
                sym = str(t.get("s") or "")
                name = str(t.get("n") or "")
                if not sym or not name:
                    continue
                lst.append(
                    {"s": sym, "n": name, "d": str(t.get("d") or ""), "g": str(t.get("g") or "other")}
                )
                index.setdefault(name.upper(), sym)  # highest market cap wins
        except Exception:
            pass
        _tickers_list = lst
        _tickers_index = index
        return _tickers_list


def search_tickers(query: str, limit: int = 12) -> list[dict]:
    """Rank the top-ticker list against a query (symbol prefix > symbol > name)."""
    q = str(query or "").strip().upper().lstrip("$")
    tickers = load_tickers()
    if not q:
        return []
    scored: list[tuple[int, int, dict]] = []
    for rank, t in enumerate(tickers):
        name = t["n"].upper()
        desc = t["d"].upper()
        if name == q:
            score = 0
        elif name.startswith(q):
            score = 1
        elif q in name:
            score = 2
        elif desc.startswith(q):
            score = 3
        elif q in desc:
            score = 4
        else:
            continue
        scored.append((score, rank, t))  # rank tiebreak keeps market-cap order
    scored.sort(key=lambda x: (x[0], x[1]))
    return [t for _, _, t in scored[:limit]]


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)


def write_watchlist_file(payload: dict) -> None:
    ensure_state_dir()
    tmp = WATCHLIST_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, WATCHLIST_PATH)


def short_ticker(tv_symbol: str) -> str:
    """SP:SPX → $SPX, CRYPTO:BTCUSD → $BTC."""
    special = {
        "CRYPTO:BTCUSD": "$BTC",
        "CRYPTO:ETHUSD": "$ETH",
        "COMEX:GC1!": "$GC",
        "COMEX:SI1!": "$SI",
        "NYMEX:CL1!": "$CL",
        "TVC:US10Y": "$US10Y",
        "TVC:US01Y": "$US01Y",
    }
    if tv_symbol in special:
        return special[tv_symbol]
    base = tv_symbol.split(":", 1)[-1]
    return f"${base}"


def resolve_symbol(raw: str) -> str:
    """Normalize user / legacy input to a TradingView EXCHANGE:SYMBOL."""
    s = str(raw or "").strip().upper().replace(" ", "")
    if not s:
        raise ValueError("empty symbol")
    if s in ALIASES:
        return ALIASES[s]
    # already EXCHANGE:SYMBOL
    if ":" in s:
        if not SYMBOL_RE.match(s):
            raise ValueError(f"invalid symbol: {raw!r}")
        return s
    # plain equity → look up the real exchange in the top-ticker table,
    # else assume NASDAQ (AAPL, MSFT, …)
    bare = s[1:] if s.startswith("$") else s
    load_tickers()  # ensure the index is populated
    if bare in _tickers_index:
        return _tickers_index[bare]
    if bare.isalpha() and 1 <= len(bare) <= 5:
        return f"NASDAQ:{bare}"
    if bare in ALIASES:
        return ALIASES[bare]
    raise ValueError(f"unknown symbol: {raw!r} (try EXCHANGE:SYMBOL or $TICKER)")


def ensure_watchlist() -> None:
    ensure_state_dir()
    if not WATCHLIST_PATH.is_file():
        data = json.loads(DEFAULT_WATCHLIST.read_text(encoding="utf-8"))
        symbols = [normalize_entry(e) for e in data.get("symbols", [])]
        write_watchlist_file({"symbols": symbols})
        return
    # migrate legacy Yahoo / junk entries in place
    try:
        raw = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
        changed = False
        out = []
        seen = set()
        for e in raw.get("symbols") or []:
            try:
                entry = normalize_entry(e)
            except Exception:
                changed = True
                continue
            if entry["symbol"] in seen:
                changed = True
                continue
            # detect yahoo-shaped originals
            orig = e.get("symbol") if isinstance(e, dict) else e
            if str(orig).upper() != entry["symbol"] and str(orig).upper() in ALIASES:
                changed = True
            seen.add(entry["symbol"])
            out.append(entry)
        if changed or len(out) != len(raw.get("symbols") or []):
            write_watchlist_file({"symbols": out})
    except Exception:
        pass


def load_watchlist() -> dict:
    ensure_watchlist()
    raw = WATCHLIST_PATH.read_text(encoding="utf-8")
    data = json.loads(raw)
    symbols = data.get("symbols")
    if not isinstance(symbols, list):
        raise ValueError("watchlist.symbols must be a list")
    return {"symbols": [normalize_entry(e) for e in symbols]}


def normalize_entry(entry) -> dict:
    if isinstance(entry, str):
        sym = resolve_symbol(entry)
        return {
            "symbol": sym,
            "group": "other",
            "label": short_ticker(sym),
            "ticker": short_ticker(sym),
        }
    if not isinstance(entry, dict):
        raise ValueError("invalid watchlist entry")
    sym = resolve_symbol(str(entry.get("symbol", "")))
    group = str(entry.get("group") or "other").strip().lower()[:32]
    label = str(entry.get("label") or short_ticker(sym)).strip()[:64]
    return {
        "symbol": sym,
        "group": group or "other",
        "label": label or short_ticker(sym),
        "ticker": short_ticker(sym),
    }


def save_watchlist(data: dict) -> None:
    symbols = [normalize_entry(e) for e in data.get("symbols", [])]
    seen = set()
    unique = []
    for e in symbols:
        if e["symbol"] in seen:
            continue
        seen.add(e["symbol"])
        unique.append(e)
    write_watchlist_file({"symbols": unique})


def parse_colors_toml(text: str) -> dict:
    colors = {"mode": "dark"}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        colors[key] = val
    return colors


def load_theme() -> dict:
    font = "JetBrainsMono Nerd Font, JetBrains Mono, ui-monospace, monospace"
    try:
        import subprocess

        out = subprocess.run(
            ["omarchy-font-current"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            font = f'"{out.stdout.strip()}", ui-monospace, monospace'
    except Exception:
        pass

    colors = {
        "mode": "dark",
        "accent": "#7aa2f7",
        "background": "#1a1b26",
        "dark_background": "#13141c",
        "lighter_background": "#24283b",
        "foreground": "#a9b1d6",
        "dark_foreground": "#565f89",
        "bright_foreground": "#c0caf5",
        "red": "#f7768e",
        "green": "#9ece6a",
        "yellow": "#e0af68",
        "selection": "#292e42",
        "muted": "#414868",
    }
    try:
        if COLORS_TOML.is_file():
            colors.update(parse_colors_toml(COLORS_TOML.read_text(encoding="utf-8")))
    except Exception:
        pass
    return {"colors": colors, "fontFamily": font, "path": str(COLORS_TOML)}


def tradingview_quotes(symbols: list[str]) -> dict[str, dict]:
    """Return {tv_symbol: {price, changePct}} via TradingView scanner."""
    tickers = []
    for sym in symbols:
        try:
            tickers.append(resolve_symbol(sym))
        except Exception:
            if ":" in sym:
                tickers.append(sym)
    tickers = list(dict.fromkeys(tickers))
    if not tickers:
        return {}
    payload = json.dumps(
        {
            "symbols": {"tickers": tickers, "query": {"types": []}},
            # TV `change` is already a percent (e.g. -0.58 = -0.58%)
            "columns": ["close", "change", "change_abs", "description"],
        }
    ).encode()
    req = urllib.request.Request(
        TV_SCANNER,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": UA,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        raw = resp.read(MAX_HTTP_BYTES + 1)
    if len(raw) > MAX_HTTP_BYTES:
        raise RuntimeError("tv response too large")
    data = json.loads(raw.decode("utf-8"))
    out = {}
    for row in data.get("data") or []:
        tv_sym = row.get("s")
        if not tv_sym:
            continue
        d = row.get("d") or []
        close = d[0] if d else None
        change_pct = d[1] if len(d) > 1 else None
        change_abs = d[2] if len(d) > 2 else None
        desc = d[3] if len(d) > 3 else ""
        if close is None:
            continue
        out[tv_sym] = {
            "price": float(close),
            "changePct": float(change_pct) if change_pct is not None else None,
            "change": float(change_abs) if change_abs is not None else None,
            "shortName": str(desc or "")[:64],
            "source": "tradingview",
        }
    return out


def _chart_throttle() -> None:
    """Serialize all chart fetches, 1–2s apart (randomized so calls never burst)."""
    global _chart_next
    with _chart_lock:
        now = time.time()
        wait = _chart_next - now
        if wait > 0:
            time.sleep(wait)
        _chart_next = time.time() + random.uniform(CHART_GAP_MIN, CHART_GAP_MAX)


def _ws_send_text(ssock: ssl.SSLSocket, text: str) -> None:
    data = text.encode()
    mask = os.urandom(4)
    ln = len(data)
    hdr = bytearray([0x81])
    if ln < 126:
        hdr.append(0x80 | ln)
    elif ln < 65536:
        hdr.append(0x80 | 126)
        hdr.extend(ln.to_bytes(2, "big"))
    else:
        hdr.append(0x80 | 127)
        hdr.extend(ln.to_bytes(8, "big"))
    hdr.extend(mask)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    ssock.sendall(bytes(hdr) + masked)


def _ws_recv_texts(ssock: ssl.SSLSocket, pending: bytes, deadline: float) -> tuple[list[str], bytes]:
    buf = pending
    out: list[str] = []
    while time.time() < deadline:
        ssock.settimeout(max(0.05, deadline - time.time()))
        try:
            chunk = ssock.recv(65536)
            if not chunk:
                break
            buf += chunk
        except socket.timeout:
            if out:
                break
            continue
        while True:
            if len(buf) < 2:
                break
            b1, b2 = buf[0], buf[1]
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            ln = b2 & 0x7F
            i = 2
            if ln == 126:
                if len(buf) < 4:
                    break
                ln = int.from_bytes(buf[2:4], "big")
                i = 4
            elif ln == 127:
                if len(buf) < 10:
                    break
                ln = int.from_bytes(buf[2:10], "big")
                i = 10
            if masked:
                if len(buf) < i + 4:
                    break
                mask = buf[i : i + 4]
                i += 4
            else:
                mask = b""
            if len(buf) < i + ln:
                break
            payload = buf[i : i + ln]
            buf = buf[i + ln :]
            if mask:
                payload = bytes(b ^ mask[j % 4] for j, b in enumerate(payload))
            if opcode == 0x1:
                out.append(payload.decode("utf-8", "replace"))
            elif opcode == 0x8:
                return out, buf
            elif opcode == 0x9:
                # pong
                frame = bytearray([0x8A, 0x80 | len(payload)])
                m = os.urandom(4)
                frame.extend(m)
                frame.extend(b ^ m[j % 4] for j, b in enumerate(payload))
                ssock.sendall(frame)
    return out, buf


def _tv_iter_payloads(messages: list[str]):
    for m in messages:
        for part in re.split(r"~m~\d+~m~", m):
            part = part.strip()
            if not part:
                continue
            if part.startswith("~h~"):
                yield ("heartbeat", part)
                continue
            try:
                yield ("json", json.loads(part))
            except Exception:
                yield ("text", part)


def tradingview_chart(tv_symbol: str, bars: int = 100, resolution: str = "5") -> list[dict]:
    """Fetch intraday OHLCV closes via TradingView chart websocket. Returns [{t,c}, ...]."""
    _chart_throttle()
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {TV_WS_PATH} HTTP/1.1\r\n"
        f"Host: {TV_WS_HOST}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"Origin: https://www.tradingview.com\r\n"
        f"User-Agent: {UA}\r\n"
        f"\r\n"
    ).encode()
    raw = socket.create_connection((TV_WS_HOST, 443), timeout=HTTP_TIMEOUT)
    ssock = ssl.create_default_context().wrap_socket(raw, server_hostname=TV_WS_HOST)
    try:
        ssock.sendall(req)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = ssock.recv(4096)
            if not chunk:
                raise RuntimeError("tv ws handshake closed")
            buf += chunk
        head, _, pending = buf.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n")[0]:
            raise RuntimeError(f"tv ws handshake failed: {head.split(b'\\n')[0]!r}")

        def send_obj(obj: dict) -> None:
            payload = json.dumps(obj, separators=(",", ":"))
            _ws_send_text(ssock, f"~m~{len(payload)}~m~{payload}")

        # drain greeting
        msgs, pending = _ws_recv_texts(ssock, pending, time.time() + 2)
        for kind, payload in _tv_iter_payloads(msgs):
            if kind == "heartbeat":
                _ws_send_text(ssock, f"~m~{len(payload)}~m~{payload}")

        cs = "cs_" + hashlib.md5(os.urandom(8)).hexdigest()[:8]
        send_obj({"m": "chart_create_session", "p": [cs, ""]})
        send_obj(
            {
                "m": "resolve_symbol",
                "p": [cs, "sds_sym_1", f'={{"symbol":"{tv_symbol}","adjustment":"splits"}}'],
            }
        )
        send_obj({"m": "create_series", "p": [cs, "sds_1", "s1", "sds_sym_1", resolution, bars]})

        series: list[dict] = []
        deadline = time.time() + 10
        while time.time() < deadline:
            msgs, pending = _ws_recv_texts(ssock, pending, min(deadline, time.time() + 2))
            if not msgs:
                if series:
                    break
                continue
            done = False
            for kind, payload in _tv_iter_payloads(msgs):
                if kind == "heartbeat":
                    _ws_send_text(ssock, f"~m~{len(payload)}~m~{payload}")
                    continue
                if kind != "json" or not isinstance(payload, dict):
                    continue
                m = payload.get("m")
                if m == "timescale_update":
                    block = (payload.get("p") or [None, {}])[1] or {}
                    sds = (block.get("sds_1") or {}).get("s") or []
                    for row in sds:
                        v = row.get("v") or []
                        if len(v) < 5:
                            continue
                        series.append({"t": int(v[0]), "c": float(v[4])})
                elif m == "series_error":
                    raise RuntimeError(str(payload.get("p")))
                elif m == "series_completed":
                    done = True
            if done:
                break
        if not series:
            raise RuntimeError(f"no tv series for {tv_symbol}")
        # timescale may send unordered / overlapping chunks
        series.sort(key=lambda p: p["t"])
        dedup = []
        seen = set()
        for p in series:
            if p["t"] in seen:
                continue
            seen.add(p["t"])
            dedup.append(p)
        return dedup
    finally:
        try:
            ssock.close()
        except Exception:
            pass


def fetch_chart_series(tv_symbol: str) -> tuple[list[dict], str]:
    """Return (series, source) for a TradingView symbol."""
    series = tradingview_chart(resolve_symbol(tv_symbol))
    return series, "tradingview"


def warm_charts_loop():
    """Fill sparklines one symbol at a time, ≥1s apart (TradingView charts first)."""
    while True:
        try:
            wl = load_watchlist()
            fetched_any = False
            for entry in wl["symbols"]:
                sym = entry["symbol"]
                with _cache_lock:
                    hit = _quote_cache.get(sym)
                # `seriesTs` tracks when the sparkline itself was last pulled;
                # price refreshes bump `ts` but not this, so charts refresh on
                # their own slow cadence, not every quote poll. `seriesTtl` is a
                # per-symbol jittered interval so they don't all come due at once.
                if (
                    hit
                    and hit["data"].get("series")
                    and time.time() - hit["data"].get("seriesTs", 0)
                    < hit["data"].get("seriesTtl", CHART_REFRESH_SEC)
                ):
                    continue
                try:
                    series, source = fetch_chart_series(sym)
                    data = empty_quote(sym)
                    if hit:
                        data.update(
                            {
                                k: hit["data"].get(k)
                                for k in (
                                    "price",
                                    "change",
                                    "changePct",
                                    "shortName",
                                    "currency",
                                    "exchange",
                                    "previousClose",
                                )
                            }
                        )
                    if series:
                        data["series"] = series
                        data["seriesTs"] = time.time()
                        data["seriesTtl"] = CHART_REFRESH_SEC + random.uniform(
                            -CHART_REFRESH_JITTER, CHART_REFRESH_JITTER
                        )
                        data["stale"] = False
                        data["error"] = None
                        data["source"] = source
                        if data.get("price") is None:
                            data["price"] = series[-1]["c"]
                    with _cache_lock:
                        _quote_cache[sym] = {"ts": time.time(), "data": data}
                    fetched_any = True
                    print(f"chart {sym} <- {source} pts={len(series)}", flush=True)
                except Exception as e:
                    print(f"chart {sym} fail: {e}", flush=True)
                    continue
            if not fetched_any:
                time.sleep(CHART_IDLE_SEC)
        except Exception as e:
            print(f"warm loop error: {e}", flush=True)
            time.sleep(10)


def empty_quote(symbol: str, error: str | None = None) -> dict:
    return {
        "symbol": symbol,
        "currency": "",
        "exchange": "",
        "shortName": "",
        "price": None,
        "change": None,
        "changePct": None,
        "previousClose": None,
        "series": [],
        "updated": int(time.time()),
        "stale": True,
        "error": error,
        "source": None,
    }


def json_response(handler: BaseHTTPRequestHandler, code: int, obj) -> None:
    body = json.dumps(obj).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def read_json_body(handler: BaseHTTPRequestHandler, limit: int = 64_000) -> dict:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0 or length > limit:
        raise ValueError("bad body size")
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8"))


class Handler(BaseHTTPRequestHandler):
    server_version = "Omarkets/1.0"

    def log_message(self, fmt, *args):
        sys_stderr = __import__("sys").stderr
        sys_stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self.serve_file(STATIC / "index.html", "text/html; charset=utf-8")
        if path.startswith("/static/"):
            rel = path[len("/static/") :]
            if ".." in rel or rel.startswith("/"):
                return self.send_error(400)
            return self.serve_file(STATIC / rel)

        if path == "/api/theme":
            return json_response(self, 200, load_theme())
        if path == "/api/search":
            q = (qs.get("q") or [""])[0]
            try:
                limit = min(25, max(1, int((qs.get("limit") or ["12"])[0])))
            except ValueError:
                limit = 12
            return json_response(self, 200, {"results": search_tickers(q, limit)})
        if path == "/api/watchlist":
            try:
                return json_response(self, 200, load_watchlist())
            except Exception as e:
                return json_response(self, 500, {"error": str(e)})
        if path == "/api/quotes":
            try:
                wl = load_watchlist()
                force = qs.get("force", ["0"])[0] == "1"
                symbols = [e["symbol"] for e in wl["symbols"]]
                # Prices: TradingView batch. Charts: warm_charts_loop @ 1s.
                tv_hint: dict = {}
                try:
                    tv_hint = tradingview_quotes(symbols)
                except Exception:
                    tv_hint = {}

                quotes = []
                for entry in wl["symbols"]:
                    sym = entry["symbol"]
                    tip = tv_hint.get(sym)
                    q = None
                    with _cache_lock:
                        hit = _quote_cache.get(sym)
                    if hit and not force and (time.time() - hit["ts"] < QUOTE_CACHE_TTL):
                        q = dict(hit["data"])
                    elif hit and force:
                        q = dict(hit["data"])
                        q["stale"] = True
                    if q is None:
                        q = empty_quote(sym)
                    if tip and tip.get("price") is not None:
                        series = q.get("series") or []
                        q.update(
                            {
                                "price": tip["price"],
                                "change": tip.get("change"),
                                "changePct": tip.get("changePct"),
                                "shortName": tip.get("shortName") or q.get("shortName") or "",
                                "stale": bool(q.get("stale")) or not series,
                                "error": None if series else q.get("error"),
                                "source": "tradingview",
                                "series": series,
                            }
                        )
                        with _cache_lock:
                            prev = _quote_cache.get(sym)
                            prev_data = (prev or {}).get("data", {})
                            keep_series = prev_data.get("series") or series
                            stored = dict(q)
                            stored["series"] = keep_series
                            # preserve the chart's own timestamp + jittered ttl;
                            # a price hint must not reset the sparkline's clock
                            stored["seriesTs"] = prev_data.get("seriesTs", 0)
                            stored["seriesTtl"] = prev_data.get("seriesTtl", CHART_REFRESH_SEC)
                            _quote_cache[sym] = {"ts": time.time(), "data": stored}
                            q["series"] = keep_series
                    q = dict(q)
                    q["group"] = entry["group"]
                    q["label"] = entry["label"] or q.get("shortName") or entry["symbol"]
                    q["ticker"] = entry.get("ticker") or short_ticker(sym)
                    quotes.append(q)
                return json_response(
                    self,
                    200,
                    {"quotes": quotes, "fetchedAt": int(time.time())},
                )
            except Exception as e:
                return json_response(self, 500, {"error": str(e)})
        if path == "/api/chart":
            try:
                symbol = resolve_symbol((qs.get("symbol") or [""])[0])
                tf = (qs.get("tf") or ["I"])[0].upper()
                # I = intraday 5m (default sparkline); D/W/M = daily/weekly/monthly
                res_map = {"I": "5", "D": "D", "W": "W", "M": "M"}
                bars_map = {"I": 100, "D": 180, "W": 156, "M": 120}
                if tf not in res_map:
                    tf = "I"
                series = tradingview_chart(symbol, bars=bars_map[tf], resolution=res_map[tf])
                return json_response(
                    self,
                    200,
                    {
                        "symbol": symbol,
                        "ticker": short_ticker(symbol),
                        "tf": tf,
                        "series": series,
                        "price": series[-1]["c"] if series else None,
                        "source": "tradingview",
                    },
                )
            except Exception as e:
                return json_response(self, 502, {"error": str(e)[:200]})

        self.send_error(404)

    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/watchlist":
            return self.send_error(404)
        try:
            data = read_json_body(self)
            save_watchlist(data)
            return json_response(self, 200, load_watchlist())
        except Exception as e:
            return json_response(self, 400, {"error": str(e)})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/watchlist/add":
            return self.send_error(404)
        try:
            body = read_json_body(self)
            entry = normalize_entry(body)
            wl = load_watchlist()
            wl["symbols"] = [e for e in wl["symbols"] if e["symbol"] != entry["symbol"]]
            wl["symbols"].append(entry)
            save_watchlist(wl)
            return json_response(self, 200, load_watchlist())
        except Exception as e:
            return json_response(self, 400, {"error": str(e)})

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/watchlist":
            return self.send_error(404)
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            symbol = resolve_symbol((qs.get("symbol") or [""])[0])
        except Exception as e:
            return json_response(self, 400, {"error": str(e)})
        try:
            wl = load_watchlist()
            wl["symbols"] = [e for e in wl["symbols"] if e["symbol"] != symbol]
            save_watchlist(wl)
            return json_response(self, 200, load_watchlist())
        except Exception as e:
            return json_response(self, 400, {"error": str(e)})

    def serve_file(self, path: Path, content_type: str | None = None):
        if not path.is_file() or not str(path.resolve()).startswith(str(STATIC.resolve())):
            if path.name == "index.html" and path.parent == STATIC:
                pass
            elif not path.is_file():
                return self.send_error(404)
        if not path.is_file():
            return self.send_error(404)
        data = path.read_bytes()
        if content_type is None:
            if path.suffix == ".css":
                content_type = "text/css; charset=utf-8"
            elif path.suffix == ".js":
                content_type = "application/javascript; charset=utf-8"
            elif path.suffix == ".svg":
                content_type = "image/svg+xml"
            else:
                content_type = "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)


def main():
    ensure_watchlist()
    print(f"tickers loaded: {len(load_tickers())}", flush=True)
    threading.Thread(target=warm_charts_loop, name="omarkets-charts", daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Omarkets http://{HOST}:{PORT}/  (watchlist {WATCHLIST_PATH})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", flush=True)


if __name__ == "__main__":
    main()
