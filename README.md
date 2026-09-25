# Omarkets

Local TradingView watchlist dashboard for Omarchy. A small Python server
serves a mono UI of theme-aware cards with live prices and intraday
sparklines. Quotes come from TradingView; the watchlist lives on disk.

## Omarchy app

```bash
./scripts/install-omarchy.sh
```

Then open **Walker → Omarkets** (or run `omarkets`). It starts the local server, opens a Chromium `--app` window (dedicated profile), and **stops the server when you close the window**.

```bash
./scripts/uninstall-omarchy.sh   # removes launcher; keeps ~/.local/state/omarkets/
```

## Usage

Launch with `omarkets` after install, or from the repo:

```bash
python3 server.py
```

Then open [http://127.0.0.1:1987/](http://127.0.0.1:1987/).

- **Add** a symbol in the right panel: `$AAPL`, `gold`, `solana`, or `NASDAQ:AAPL`
- **Reorder** by dragging cards or watchlist rows (they share one order)
- **Click** a card to select it and switch 5m / D / W / M charts
- **Remove** a symbol with × on its watchlist row
- **Refresh** prices from the header (the page also polls on its own)

Watchlist lives at `~/.local/state/omarkets/watchlist.json`.

## Features

- Card grid: price, day change, TradingView sparkline
- Drag to reorder — both the cards and the watchlist rows (they share one order)
- Right watchlist: add / remove / reorder
- Ticker autocomplete: type `$AAPL`, `gold`, `solana`, `treasury` and pick a match
  (top ~1000 US stocks + top 100 crypto, commodities, and fixed income)
- Follows Omarchy theme (`~/.local/state/omarchy/current/theme/colors.toml`)
- Default list: indices, rates, commodities, crypto

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
