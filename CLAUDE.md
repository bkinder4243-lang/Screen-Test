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
| `app.py` | Streamlit UI — 4 tabs: Scanner, Deep Dive, Trade Decision, Journal |
| `screener/finviz_client.py` | Finviz technicals + yfinance OHLC (HV30, ATR, earnings, consolidation trigger) |
| `screener/polygon_client.py` | Polygon options chain, unusual activity, sweep detection, most-active sampler |
| `screener/scorer.py` | Composite scoring, strike selection, ScreenerResult dataclass |
| `screener/stocktwits_client.py` | Reddit sentiment (wallstreetbets, stocks, options, investing) |
| `screener/universe.py` | Dow30, NASDAQ-100, S&P-500 ticker lists (Wikipedia scrape) |
| `screener/etf_universe.py` | 71 US ETFs across 11 categories with hardcoded top-10 holdings |
| `screener/journal.py` | SQLite trade journal with Black-Scholes live P&L repricing |
| `screener/intraday.py` | yfinance 1-min bars, session VWAP, relative strength vs SPY, GEX flip level |
| `screener/conviction.py` | 0-100 conviction scorer (7 signals) + entry card builder |
| `screener/oi_tracker.py` | SQLite OI snapshots — daily OI delta tracking per contract |
| `screener/ticker_analysis.py` | PCR, max pain, net GEX, GEX by strike, OI walls, top unusual flow |

---

## Tabs

### 📊 Scanner
Main swing trade screener. Two-pass scan (Finviz → Polygon). Pinned "Most Active Options Today" at top.

### 🔍 Ticker Deep Dive
Full options chain for any ticker. Call/put OI walls with expiry dates and OI delta (daily change). Sweep detection expander. IV history row.

### 🎯 Trade Decision Panel
Real-time confluence panel for intraday option entries. Scores 7 signals (0–100). Shows full entry card and VWAP chart. Quick save to journal.

### 📓 Journal
SQLite-backed trade log. Live Black-Scholes P&L repricing. Statuses: Watching → Entered → Closed.

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

## Most Active Options (`polygon_client.py → get_top_volume_options()`)
Samples ~55 high-liquidity tickers (index ETFs, sector ETFs, mega-caps, semis, finance, energy, biotech, high-vol names). Fetches top 250 contracts per ticker in parallel (8 threads), deduplicates to 1 contract per ticker (highest volume), returns top 25 unique tickers sorted by volume.

- Uses per-ticker `/v3/snapshot/options/{symbol}` — **works on Polygon Starter plan**
- Global endpoint `/v3/snapshot/options` (no ticker) requires paid plan — not used
- Cached 5 min in Streamlit via `@st.cache_data(ttl=300)`

---

## Sweep Detection (`polygon_client.py → detect_sweeps()`)
Identifies institutional sweep orders from trade prints:
- 2+ fills within a 2-second window totalling ≥ 50 contracts
- Fills at/above ask × 0.98 = aggressive bullish BUY sweep
- Fills at/below ask × 0.80 = aggressive bearish SELL sweep
- Requires Polygon plan with options trades access (`/v3/trades/{optionSymbol}`)

---

## OI Change Tracking (`screener/oi_tracker.py`)
- SQLite at `data/oi_tracker.db`, table `oi_snapshots`
- `save_snapshot(symbol, chain)` — writes today's OI per contract
- `get_oi_change(symbol, chain)` — returns OI delta vs yesterday per contract
- `get_iv_history(symbol, days)` — returns daily avg IV trend for a ticker
- OI Δ column shown in call/put walls of Deep Dive tab

---

## Conviction Scorer (`screener/conviction.py → score_trade()`)
0-100 composite score across 7 signals for intraday entries:

| Signal | Max Pts | Source |
|---|---|---|
| VWAP alignment | 20 | yfinance 1-min bars (session VWAP) |
| Relative strength vs SPY | 15 | yfinance (symbol % chg − SPY % chg) |
| Options flow / PCR | 15 | Polygon chain (put/call volume ratio) |
| GEX structure | 15 | Polygon chain (gamma exposure flip level) |
| IV regime | 10 | avg_iv / hv30 premium ratio |
| Sweep confirmation | 15 | Polygon trades endpoint |
| Max pain gravity | 10 | Polygon chain (max pain strike) |

Grades: 80–100 = HIGH CONVICTION · 60–79 = MODERATE · 40–59 = MARGINAL · 0–39 = PASS

---

## Entry Card (`screener/conviction.py → build_entry_card()`)
Displayed in Trade Decision tab as 4 full-width rows of 4 metrics each:
- Row 1: Direction · Strike · Expiry · DTE
- Row 2: Entry Premium · Cost/Contract · Delta · IV
- Row 3: Breakeven · % to Breakeven · Stock Target · Target Premium
- Row 4: Stop Premium (−50%) · R:R · Max Pain · GEX Flip

Target premium = intrinsic value at tech target + 20% residual time value.
Stop = −50% of entry premium (standard long-option rule).

---

## Intraday Data (`screener/intraday.py`)
- **Source: yfinance** (free, no API key) — `yf.download(sym, period="1d", interval="1m")`
- Polygon intraday bars removed — requires paid plan
- Session VWAP = `cumsum(TP × Vol) / cumsum(Vol)` computed from 1-min bars
- `get_relative_strength()` returns: symbol_chg, bench_chg, rs_ratio, vs_vwap, vwap, current, day_high, day_low, bars DataFrame
- `gex_flip_level()` — cumulative GEX by strike, returns strike where sign crosses zero

---

## Polygon API Error Handling
- `_last_api_error: dict` — module-level variable in `polygon_client.py`
- Populated by `_get()` for 401 (bad key), 403 (wrong plan), 429 (rate limited), other errors; cleared on success
- Sidebar health check cached 60s via `@st.cache_data(ttl=60)` — prevents rate-limit exhaustion from firing on every rerender
- Error detail shown in sidebar when 🔴 and in Trade Decision error message

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
- `add_entry_raw()` — saves directly from Trade Decision panel (no ScreenerResult needed)
- SQLite can return numeric columns as bytes — use `_safe_float()` helper in app.py to decode

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
- **Polygon global options snapshot** (`/v3/snapshot/options` with no ticker) requires paid plan — use per-ticker sampler instead
- **Polygon intraday bars** (`/v2/aggs/ticker/{sym}/range/1/minute/...`) may require paid plan — use yfinance instead
- **Finviz ATR** field key is `"ATR (14)"` not `"ATR"`
- **yfinance calendar** returns a `dict` not a DataFrame — parse with `cal["Earnings Date"][0]`
- **yfinance 1-min download** returns multi-level columns — flatten with `col[0].lower()`
- **Wikipedia NASDAQ-100** scrape uses `requests` + `StringIO` because macOS urllib has SSL cert issues
- **ThreadPoolExecutor closures** defined inside Streamlit button blocks fail silently — all scan helpers must be at module level
- **Streamlit widget/state key conflict** — button keys and session state keys must be different strings
- **`st.stop()` blocks all tabs** — never use inside a `with tab:` block; wrap tab content in a function and use `return`
- **SQLite bytes columns** — strike and numeric fields can come back as `bytes`; decode with `int.from_bytes(v, 'little')` via `_safe_float()` helper

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
