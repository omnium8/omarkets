# Omarkets

Local TradingView watchlist dashboard for Omarchy — mono UI, theme-aware cards with intraday charts.

## Omarchy app

```bash
./scripts/install-omarchy.sh
```

Then open **Walker → Omarkets** (or run `omarkets`). It starts the local server, opens a Chromium `--app` window (dedicated profile), and **stops the server when you close the window**.

```bash
./scripts/uninstall-omarchy.sh   # removes launcher; keeps ~/.local/state/omarkets/
```


## Features

- Card grid: price, day change, TradingView sparkline
- Drag to reorder — both the cards and the watchlist rows (they share one order)
- Right watchlist: add / remove / reorder
- Ticker autocomplete: type `$AAPL`, `gold`, `solana`, `treasury` and pick a match
  (top ~1000 US stocks + top 100 crypto, commodities, and fixed income)
- Follows Omarchy theme (`~/.local/state/omarchy/current/theme/colors.toml`)
- Default list: indices, rates, commodities, crypto

Watchlist lives at `~/.local/state/omarkets/watchlist.json`.

## Symbols

Canonical form is TradingView `EXCHANGE:SYMBOL` (e.g. `SP:SPX`, `NASDAQ:AAPL`).

You can also type friendly / `$` tickers:

| Type | Resolves to |
|---|---|
| `$SPX` / `SPX` | `SP:SPX` |
| `$BTC` / `BTC` | `CRYPTO:BTCUSD` |
| `AAPL` | `NASDAQ:AAPL` |
| `XOM` | `NYSE:XOM` (real exchange from the ticker list) |
| `COMEX:GC1!` | as-is |

Bare tickers resolve against `data/tickers.json` so they land on the right
exchange; unknown alpha tickers still fall back to `NASDAQ:`. The file holds
~1000 US stocks (by market cap) plus the top 100 crypto, commodities, and
fixed-income names, each tagged with a group so picks land in the right bucket.
Rebuild with `./scripts/build-tickers.py`.

## Notes

- **Prices:** TradingView scanner (one batch request).
- **Charts:** TradingView websocket candles, one symbol per second.
