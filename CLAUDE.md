# Options Swing Trade Screener — Claude Context

## What This Is
A Streamlit-based options swing trade screener built for **Long Calls and Long Puts only**.
No spreads, no condors, no theta strategies. Pure directional swing plays.

## Trading Profile
- **Style:** Swing trades, 21–45 DTE, targeting 5–20% stock moves
- **Budget:** $300–$600/contract (max_mid slider, default $6.00)
- **Delta target:** 0.40–0.50 (ATM-ish directional, not lottery tickets)
- **Setups traded:** Breakout, MA Reclaim, Oversold Bounce, Blow-off Top, MA Breakdown, Consolidation
- **Hard rules:** No earnings within DTE window. No Rich IV (IV Premium > 1.2×).

---

## Architecture

### Data Pipeline
```
Finviz (technicals + setup) → Reddit (sentiment) → Polygon (options chain + greeks) → Scorer → Streamlit UI
```

**Pass 1 (fast, parallel):** Finviz-only scan for all tickers. Filters by setup pattern.  
**Pass 2 (slow, sequential):** Options chain fetch from Polygon only for setup-matched tickers.

### Files
| File | Purpose |
|---|---|
| `app.py` | Streamlit UI — strategies, filters, detail panel, journal tab |
| `screener/finviz_client.py` | Finviz technicals + yfinance OHLC (HV30, ATR, earnings, consolidation trigger) |
| `screener/polygon_client.py` | Polygon options chain + unusual activity detection |
| `screener/scorer.py` | Composite scoring, strike selection, ScreenerResult dataclass |
| `screener/stocktwits_client.py` | Reddit sentiment (wallstreetbets, stocks, options, investing) |
| `screener/universe.py` | Dow30, NASDAQ-100, S&P-500 ticker lists (Wikipedia scrape) |
| `screener/etf_universe.py` | 71 US ETFs across 11 categories with hardcoded top-10 holdings |
| `screener/journal.py` | SQLite trade journal with Black-Scholes live P&L repricing |

---

## Signal Weights (Composite Score)
```python
WEIGHTS = {
    "technical":        0.50,   # Finviz analyst signal (Strong Buy → Strong Sell)
    "unusual_activity": 0.40,   # Polygon vol/OI flow — institutional grade
    "sentiment":        0.10,   # Reddit — demoted, useful only as contrarian context
}
```
Score range: -2.0 (strong bearish) → +2.0 (strong bullish)  
|score| > 0.5 required to generate a strategy recommendation.

---

## Setup Detection Logic (`finviz_client.py → detect_setup()`)
Priority order — first match wins:
1. **Blow-off Top** — RSI > 75 → Long Put
2. **Breakout** — price within 2% of 52W High → Long Call
3. **Oversold** — RSI < 35 → Long Call
4. **MA Breakdown** — price below SMA50 → Long Put
5. **MA Reclaim** — price 0–5% above SMA50 → Long Call
6. **Consolidation** — 5-day range < 4% AND 9-DMA rising AND 50-DMA rising → watch for trigger
7. **Neutral** — no pattern

**Consolidation Trigger:** Volume today > 1.5× 10-day avg AND price breaks above 5-day high → shows as "⚡ Triggered" in green.

All setup metrics (range_5d_pct, sma9_slope, sma50_slope, hv_30, consolidation_triggered, days_to_earnings) are computed from yfinance 60-day OHLC history fetched inside `_price_context()`.

---

## Options Chain (Polygon.io — Starter Plan)
- Endpoint: `/v3/snapshot/options/{symbol}` (paginated, 250 per page)
- DTE window fetched: 7–90 days (wider than trade window to catch unusual activity)
- **No live bid/ask on Starter plan** → Black-Scholes fills theoretical prices when all bids = 0
- Greeks (delta, gamma, theta, vega) and IV are live from Polygon
- Sane IV filter: contracts with IV < 5% or > 300% excluded from avg_iv and strike selection

### Strike Selection (`_best_strike()`)
When `technical_target` is available:
- 60% weight: delta proximity to 0.45 target
- 40% weight: Reward-to-Breakeven = (target − breakeven) / premium paid

When no target: pure delta proximity.

---

## IV Environment Metrics
Two complementary signals:
- **IV Env** (absolute): Low < 20%, Med 20–40%, High > 40%
- **IV Premium** (relative): `avg_iv / hv_30` (30-day historical vol from yfinance)
  - < 0.80 → Cheap (buy premium aggressively)
  - 0.80–1.20 → Fair
  - > 1.20 → Rich (size down or skip)

**IV Rich > 1.2× is the most important filter for long-option buyers.** Options priced above realized vol have negative expected value at entry.

---

## Price Targets
| Setup | Target Logic |
|---|---|
| Breakout | `max(52W_high × 1.02, price + ATR×3)` |
| MA Reclaim | `price + ATR×3` |
| Oversold | SMA20 price (mean reversion) |
| Blow-off Top | SMA50 price (pullback) |
| MA Breakdown | `price − ATR×3` |
| Consolidation | `price + ATR×2` |

ATR is 14-day from Finviz (`ATR (14)` field).

---

## Unusual Activity Detection (`polygon_client.py → get_unusual_activity()`)
Institutional flow signal — NOT retail noise:
- Minimum volume: 500 (falls back to 100 for low-volume tickers)
- Minimum OI: 200
- Minimum notional: $50,000 (volume × mid × 100)
- Vol/OI ratio: ≥ 1.5×
- Scoring: `vol/OI × log10(notional)` — large premium trades score higher than high-ratio penny trades
- Returns the single highest-scoring contract as the primary signal

---

## Earnings Filter
- `days_to_earnings` from `yfinance.Ticker(sym).calendar["Earnings Date"][0]`
- `earnings_within_dte = days_to_earnings <= dte_max`
- Sidebar: "🚫 Exclude earnings risk" checkbox (default ON)
- Red banner fires in detail panel when earnings inside trade window
- ETFs return `days_to_earnings = None` (no earnings calendar)

---

## Trade Journal (`screener/journal.py`)
- SQLite at `data/journal.db`
- Statuses: Watching → Entered → Closed
- Live P&L repricing: Black-Scholes using yfinance spot price
- Tracks: entry premium, delta, IV, strike, expiry, stock price, all price targets
- Realized P&L computed on close: `(exit_price − entry_premium) / entry_premium × 100`

---

## Universes
- **Custom** — manual ticker list from sidebar
- **Dow 30** — hardcoded 30 tickers
- **NASDAQ 100** — Wikipedia scrape with `requests` + `StringIO` (avoids macOS SSL issue)
- **S&P 500** — Wikipedia scrape
- **ETFs** — 71 US ETFs across 11 categories (no international)

ETF drill-down: scan top holdings of a selected ETF for confluence (ETF consolidating + holding breaking out = high-conviction entry).

---

## Known Constraints
- **Polygon Starter plan** has no live bid/ask quotes — all prices are Black-Scholes theoretical
- **Finviz ATR** field key is `"ATR (14)"` not `"ATR"`
- **yfinance calendar** returns a `dict` not a DataFrame — parse with `cal["Earnings Date"][0]`
- **Wikipedia NASDAQ-100** scrape uses `requests` + `StringIO` because macOS urllib has SSL cert issues
- **ThreadPoolExecutor closures** defined inside Streamlit button blocks fail silently — all scan helpers (`_fetch_tech_only`, `_fetch_full_result`) must be at module level
- **Streamlit widget/state key conflict** — button keys and session state keys must be different strings

---

## Strategies Defined in `app.py`
| Strategy | Setup Filter | Direction |
|---|---|---|
| All Setups | None | None |
| Breakout → Long Call | Breakout | Long Call |
| MA Reclaim → Long Call | MA Reclaim | Long Call |
| Oversold Bounce → Long Call | Oversold | Long Call |
| Blow-off Top → Long Put | Blow-off Top | Long Put |
| MA Breakdown → Long Put | MA Breakdown | Long Put |
| Consolidation Watch | Consolidation | None (wait for trigger) |
| Custom Strategy | User sliders | User choice |

---

## Development Notes
- Run server: double-click `startup screener.command` on Desktop
- Streamlit port: 8501
- Python: `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3`
- Secrets: `config/secrets.env` (gitignored) — needs `POLYGON_API_KEY`

---

## Integration with Financial Dashboard

This screener is embedded as an iframe inside the Claude Financial Dashboard (React/Vite app).

- Financial Dashboard: `/Users/williamkinder/Desktop/CLAUDE FINANCIAL DASHBOARD/`
- The "Options Screener" tab in the dashboard nav loads `http://localhost:8501` in an iframe
- Must run with `--server.enableCORS false --server.enableXsrfProtection false` for iframe to work
- Unified launcher: `startup dashboard.command` on Desktop starts both apps together
- Standalone launcher: `startup screener.command` on Desktop starts only this app

Do not add Streamlit `X-Frame-Options` or CORS headers — they break the iframe embedding.
