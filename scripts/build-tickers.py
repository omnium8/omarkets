#!/usr/bin/env python3
"""Build data/tickers.json — top tickers used for `$TICKER` search + resolution.

Categories (all from the TradingView scanner, the same source the app uses for
quotes):

  - ~1000 US stocks / funds  by market cap
  -  ~100 crypto             by market cap  (canonical CRYPTO:*USD when it exists)
  -  ~100 commodities        futures front-month by volume (energy/metals/ag)
  -  ~100 fixed income       government bond yields, US + majors first

Each entry: {"s": "EXCHANGE:SYMBOL", "n": "TICKER", "d": "description", "g": group}

Run occasionally to refresh:  ./scripts/build-tickers.py
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "tickers.json"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

STOCK_COUNT = 1000
CRYPTO_COUNT = 100
COMMODITY_COUNT = 100
BOND_COUNT = 100

COMMODITY_SECTORS = ["Energy", "Metals", "Agricultural"]
# Major Western commodity exchanges (skips the higher-volume Chinese/other feeds
# so the list is the recognizable global benchmarks the app already uses).
COMMODITY_EXCHANGES = [
    "COMEX", "NYMEX", "CBOT", "ICEUS", "ICEEUR", "CME", "MGEX", "LME",
    "COMEX_MINI", "NYMEX_MINI", "CBOT_MINI",
]
# Preferred crypto exchanges when a coin has no canonical CRYPTO:*USD feed.
CRYPTO_EXCHANGES = ["BINANCE", "COINBASE", "BITSTAMP", "KRAKEN", "BYBIT", "OKX", "CRYPTOCOM"]
# Country priority for "top" bonds (ISO-2 prefix of the TradingView bond name).
BOND_COUNTRIES = [
    "US", "DE", "GB", "JP", "FR", "IT", "CA", "AU", "CH", "CN", "ES", "NL",
    "SE", "NO", "KR", "IN", "BR", "MX", "ZA", "SG", "HK", "NZ", "PT", "IE",
    "BE", "AT", "FI", "DK", "PL", "GR", "TR", "RU",
]


def scan(market: str, body: dict) -> list[dict]:
    req = urllib.request.Request(
        f"https://scanner.tradingview.com/{market}/scan",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": UA},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read()).get("data") or []


def validate(symbols: list[str]) -> set[str]:
    """Return the subset of EXCHANGE:SYMBOL that the global scanner prices."""
    out: set[str] = set()
    for i in range(0, len(symbols), 200):
        chunk = symbols[i : i + 200]
        payload = json.dumps(
            {"symbols": {"tickers": chunk, "query": {"types": []}}, "columns": ["close"]}
        ).encode()
        req = urllib.request.Request(
            "https://scanner.tradingview.com/global/scan",
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": UA},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        for row in data.get("data") or []:
            if row.get("s") and row.get("d"):
                out.add(row["s"])
    return out


def build_stocks() -> list[dict]:
    rows = scan(
        "america",
        {
            "filter": [{"left": "type", "operation": "in_range", "right": ["stock", "dr", "fund"]}],
            "options": {"lang": "en"},
            "markets": ["america"],
            "symbols": {"query": {"types": []}, "tickers": []},
            "columns": ["name", "description", "market_cap_basic"],
            "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"},
            "range": [0, STOCK_COUNT],
        },
    )
    out, seen = [], set()
    for r in rows:
        d = r.get("d") or []
        sym = r.get("s")
        if not sym or len(d) < 2 or not d[0] or sym in seen:
            continue
        seen.add(sym)
        out.append({"s": sym, "n": str(d[0]), "d": str(d[1] or ""), "g": "equities"})
    return out


def build_crypto() -> list[dict]:
    rows = scan(
        "crypto",
        {
            "columns": ["base_currency", "base_currency_desc", "market_cap_calc"],
            "sort": {"sortBy": "market_cap_calc", "sortOrder": "desc"},
            "range": [0, 1200],
            "options": {"lang": "en"},
        },
    )
    # Per base coin (market-cap order): candidate symbols, canonical first.
    order: list[str] = []
    desc: dict[str, str] = {}
    ex_syms: dict[str, dict[str, str]] = {}  # base -> {EXCHANGE: SYMBOL}
    for r in rows:
        sym = r.get("s") or ""
        d = r.get("d") or []
        base = d[0] if d else ""
        if not base or ":" not in sym:
            continue
        if base not in desc:
            order.append(base)
            desc[base] = str(d[1] or "")
            ex_syms[base] = {}
        exch = sym.split(":", 1)[0]
        ex_syms[base].setdefault(exch, sym)

    def candidates(base: str) -> list[str]:
        cands = [f"CRYPTO:{base}USD"]
        for exch in CRYPTO_EXCHANGES:
            if exch in ex_syms[base]:
                cands.append(ex_syms[base][exch])
        cands.extend(ex_syms[base].values())  # any remaining exchange
        seen, uniq = set(), []
        for c in cands:
            if c not in seen:
                seen.add(c)
                uniq.append(c)
        return uniq

    # Validate all candidates for enough top coins to fill the quota.
    consider = order[: CRYPTO_COUNT * 2]
    all_cands = [c for base in consider for c in candidates(base)]
    valid = validate(all_cands)

    out = []
    for base in consider:
        pick = next((c for c in candidates(base) if c in valid), None)
        if not pick:
            continue
        out.append({"s": pick, "n": base, "d": desc[base], "g": "crypto"})
        if len(out) >= CRYPTO_COUNT:
            break
    return out


def build_commodities() -> list[dict]:
    rows = scan(
        "futures",
        {
            "filter": [
                {"left": "sector", "operation": "in_range", "right": COMMODITY_SECTORS},
                {"left": "exchange", "operation": "in_range", "right": COMMODITY_EXCHANGES},
            ],
            "columns": ["name", "description", "sector", "volume"],
            "sort": {"sortBy": "volume", "sortOrder": "desc"},
            "range": [0, 400],
            "options": {"lang": "en"},
        },
    )
    out, seen = [], set()
    for r in rows:
        d = r.get("d") or []
        sym = r.get("s")
        name = d[0] if d else ""
        if not sym or not name or not name.endswith("1!"):  # front-month continuous only
            continue
        root = sym.split(":", 1)[-1]
        if root in seen:
            continue
        seen.add(root)
        out.append({"s": sym, "n": str(name), "d": str(d[1] or ""), "g": "commodities"})
        if len(out) >= COMMODITY_COUNT:
            break
    return out


def build_bonds() -> list[dict]:
    rows = scan(
        "bonds",
        {
            "columns": ["name", "description"],
            "range": [0, 2000],
            "options": {"lang": "en"},
        },
    )
    prio = {c: i for i, c in enumerate(BOND_COUNTRIES)}
    scored = []
    for r in rows:
        d = r.get("d") or []
        sym = r.get("s")
        name = d[0] if d else ""
        if not sym or not name:
            continue
        country = name[:2].upper()
        rank = prio.get(country, len(BOND_COUNTRIES))
        scored.append((rank, {"s": sym, "n": str(name), "d": str(d[1] or ""), "g": "fixed_income"}))
    # keep only bonds from prioritized countries, best first
    scored = [x for x in scored if x[0] < len(BOND_COUNTRIES)]
    scored.sort(key=lambda x: x[0])
    return [e for _, e in scored[:BOND_COUNT]]


def main() -> None:
    stocks = build_stocks()
    crypto = build_crypto()
    commodities = build_commodities()
    bonds = build_bonds()
    # Commodities first so they win search ties (e.g. "gold" → Gold Futures, not
    # Goldman Sachs) and index priority; then the rest.
    tickers = commodities + crypto + bonds + stocks
    OUT.write_text(json.dumps({"tickers": tickers}, separators=(",", ":")) + "\n", encoding="utf-8")
    print(
        f"wrote {len(tickers)} tickers -> {OUT}\n"
        f"  stocks={len(stocks)} crypto={len(crypto)} "
        f"commodities={len(commodities)} bonds={len(bonds)}"
    )


if __name__ == "__main__":
    main()
