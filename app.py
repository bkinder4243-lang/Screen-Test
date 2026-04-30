"""
Options Swing Trade Screener — Long Calls & Puts Only
Signals: Finviz technicals · Reddit sentiment · Polygon unusual activity
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import streamlit as st
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from screener.polygon_client    import (get_options_chain, get_unusual_activity, key_is_working,
                                        get_top_volume_options, get_option_trades, detect_sweeps,
                                        get_option_iv_history)
from screener.finviz_client     import get_technicals
from screener.oi_tracker        import save_snapshot, get_oi_change, get_iv_history
from screener.stocktwits_client import get_sentiment
from screener.scorer            import build_result, ScreenerResult
from screener.universe          import load_universe, UNIVERSES
from screener.etf_universe      import ETF_UNIVERSE, get_etf_holdings, etf_category
from screener.journal           import add_entry, add_entry_raw, get_entries, close_entry, delete_entry, reprice_all_open, update_notes
from screener.ticker_analysis   import full_analysis
from screener.polygon_client    import get_spot_price
from screener.intraday          import get_relative_strength, gex_flip_level
from screener.conviction        import score_trade, build_entry_card

st.set_page_config(page_title="Options Screener", page_icon="📊", layout="wide")

# ── Strategy definitions ──────────────────────────────────────────────────────
STRATEGIES = {
    "🔍 All Setups": {
        "setup_filter":     None,
        "direction_filter": None,
        "description":      "Show every setup detected across all patterns. Use this to survey the full watchlist.",
        "entry":   [],
        "ideal":   [],
        "avoid":   [],
    },
    "🚀 Breakout → Long Call": {
        "setup_filter":     "Breakout",
        "direction_filter": "Long Call",
        "description":      "Price at/near 52-week high with bullish momentum. Buy calls expecting trend continuation.",
        "entry": [
            "Price within 2% of 52W high",
            "Analyst signal: Buy or Strong Buy",
            "Relative volume elevated (≥1.2×)",
        ],
        "ideal": [
            "Unusual call flow present (institutional confirmation)",
            "RSI 55–70 — strong but not overextended",
            "Clean break above prior resistance on high volume",
        ],
        "avoid": [
            "RSI > 75 — overextended, use Blow-off Top Put instead",
            "High IV environment — premium is expensive at highs",
            "Earnings within your DTE window (binary event risk)",
        ],
    },
    "📈 MA Reclaim → Long Call": {
        "setup_filter":     "MA Reclaim",
        "direction_filter": "Long Call",
        "description":      "Price reclaimed SMA50 after a pullback. Buy calls expecting resumption of the prior uptrend.",
        "entry": [
            "Price just crossed above SMA50 (within 5%)",
            "RSI recovering from below 50",
            "Prior trend was bullish before the pullback",
        ],
        "ideal": [
            "RSI 45–60 — recovering with room to run",
            "Unusual call flow confirms institutional buying",
            "Volume spike on the reclaim candle",
        ],
        "avoid": [
            "SMA200 is sloping down — macro trend is bearish",
            "Multiple failed SMA50 reclaims in recent months",
            "Sector is in a downtrend",
        ],
    },
    "💚 Oversold Bounce → Long Call": {
        "setup_filter":     "Oversold",
        "direction_filter": "Long Call",
        "description":      "RSI < 35, price extended to the downside. Buy calls anticipating mean reversion toward the moving averages.",
        "entry": [
            "RSI < 35 (oversold territory)",
            "Price significantly below SMA20 and SMA50",
            "No fundamental deterioration (guidance cut, earnings miss)",
        ],
        "ideal": [
            "RSI < 30 — deeply oversold, higher snap-back potential",
            "High relative volume on down day — possible selling climax",
            "Bullish sentiment signal (crowd capitulating = contrarian buy)",
        ],
        "avoid": [
            "Downtrend with no catalyst for reversal",
            "Sector-wide weakness (not ticker-specific)",
            "Earnings within DTE window (gap risk)",
        ],
    },
    "🔴 Blow-off Top → Long Put": {
        "setup_filter":     "Blow-off Top",
        "direction_filter": "Long Put",
        "description":      "RSI > 75, price over-extended after a strong rally. Buy puts expecting a pullback or at minimum consolidation.",
        "entry": [
            "RSI > 75 (over-extended)",
            "Price at or near 52W high after parabolic move",
            "3–5 consecutive strong up days with no pause",
        ],
        "ideal": [
            "RSI > 80 — extreme extension",
            "Analyst signal: Hold or Sell (institutional skepticism)",
            "Unusual put activity — smart money hedging/fading",
        ],
        "avoid": [
            "Strong earnings catalyst can sustain elevated RSI",
            "Low float stocks can stay irrational far longer",
            "Buying puts into a broad market rally",
        ],
    },
    "📉 MA Breakdown → Long Put": {
        "setup_filter":     "MA Breakdown",
        "direction_filter": "Long Put",
        "description":      "Price broke below SMA50 with bearish momentum. Buy puts expecting continuation of the downtrend.",
        "entry": [
            "Price below SMA50",
            "Analyst signal: Hold, Sell, or Strong Sell",
            "Sector showing broad weakness",
        ],
        "ideal": [
            "Price also below SMA200 — macro trend is bearish",
            "Unusual put flow (institutional hedging / directional short)",
            "Elevated relative volume on breakdown day",
        ],
        "avoid": [
            "RSI < 30 — already oversold, reversal risk is high",
            "Strong support level just below current price",
            "Market-wide selloff (systemic, not ticker-specific)",
        ],
    },
    "🟡 Consolidation Watch": {
        "setup_filter":     "Consolidation",
        "direction_filter": None,
        "description":      "Price coiling in a tight range near SMA20 with neutral RSI. Watch for breakout — do not enter until direction is confirmed.",
        "entry": [
            "Price within 3% of SMA20",
            "RSI 40–60 (neutral, coiled)",
            "Decreasing volume (compression before expansion)",
        ],
        "ideal": [
            "Tight range for 5+ days (longer compression = bigger move)",
            "Unusual activity appears first — confirms breakout direction",
            "Clear support and resistance defining the range",
        ],
        "avoid": [
            "Entering before breakout direction is confirmed",
            "Choppy broad market — false breakouts are common",
            "Earnings imminent (can force the break but unpredictably)",
        ],
    },
}

STRATEGY_NAMES = list(STRATEGIES.keys()) + ["🛠️ Custom Strategy"]

# ── Sidebar ───────────────────────────────────────────────────────────────────
st.sidebar.title("⚙️ Settings")

@st.cache_data(ttl=60)
def _check_polygon_key():
    return key_is_working()

poly_ok = _check_polygon_key()
from screener.polygon_client import _last_api_error as _poly_err
_poly_status = "🟢" if poly_ok else "🔴"
_poly_detail  = f" — {_poly_err['msg']}" if not poly_ok and _poly_err.get("msg") else ""
st.sidebar.markdown(
    f"{_poly_status} Polygon.io (options + greeks){_poly_detail}  \n"
    "🟢 Finviz (technicals)  \n"
    "🟢 Reddit (sentiment)"
)
st.sidebar.divider()

# ── Strategy selector ─────────────────────────────────────────────────────────
st.sidebar.subheader("Strategy")
selected_strategy = st.sidebar.selectbox(
    "Screen for:",
    STRATEGY_NAMES,
    index=0,
    help="Choose a preset setup or build your own with Custom Strategy.",
)

# ── Custom strategy sliders ───────────────────────────────────────────────────
custom_cfg = {}
if selected_strategy == "🛠️ Custom Strategy":
    st.sidebar.markdown("**Direction**")
    custom_cfg["direction"] = st.sidebar.radio(
        "Trade direction", ["Long Call", "Long Put", "Either"], horizontal=True, label_visibility="collapsed"
    )
    st.sidebar.markdown("**RSI range**")
    custom_cfg["rsi_range"] = st.sidebar.slider("RSI", 0, 100, (30, 70), label_visibility="collapsed")

    st.sidebar.markdown("**SMA50 position**")
    custom_cfg["sma50"] = st.sidebar.radio(
        "SMA50", ["Above", "Below", "Any"], horizontal=True, label_visibility="collapsed"
    )

    st.sidebar.markdown("**Min relative volume**")
    custom_cfg["min_rel_vol"] = st.sidebar.slider(
        "Rel vol", 0.5, 5.0, 1.0, step=0.1, label_visibility="collapsed",
        help="Minimum today's volume vs average. 1.0 = average, 2.0 = twice average."
    )

    st.sidebar.markdown("**Distance from 52W high (%)**")
    custom_cfg["high_range"] = st.sidebar.slider(
        "52W high %", -50, 0, (-10, 0), label_visibility="collapsed",
        help="Negative = below high. (-2, 0) = within 2% of high (breakout zone)."
    )

    st.sidebar.markdown("**Min bullish sentiment (%)**")
    custom_cfg["min_bull_pct"] = st.sidebar.slider(
        "Bullish %", 0, 100, 0, label_visibility="collapsed",
        help="Minimum Reddit bullish % required. 0 = no filter."
    )

    custom_cfg["require_unusual"] = st.sidebar.checkbox("Require unusual activity", value=False)

    strat = {
        "setup_filter":     None,
        "direction_filter": None if custom_cfg["direction"] == "Either" else custom_cfg["direction"],
        "description":      "User-defined criteria. Results must satisfy all active filters.",
        "entry":  [],
        "ideal":  [],
        "avoid":  [],
    }
else:
    strat = STRATEGIES[selected_strategy]

st.sidebar.divider()

# ── Universe selector ─────────────────────────────────────────────────────────
st.sidebar.subheader("Universe")
UNIVERSE_OPTIONS = list(UNIVERSES.keys()) + ["ETFs"]
universe_name = st.sidebar.radio(
    "Ticker universe",
    UNIVERSE_OPTIONS,
    horizontal=True,
    label_visibility="collapsed",
)

if universe_name == "Custom":
    DEFAULT = "SPY,QQQ,IWM,AAPL,MSFT,NVDA,TSLA,AMD,META,AMZN,JPM,GS,XLE,XLK,XLV,XLI"
    try:
        default_list = ",".join(pd.read_csv("data/tickers.csv")["ticker"].dropna().tolist())
    except Exception:
        default_list = DEFAULT
    raw = st.sidebar.text_area("Tickers", value=default_list, height=100)
    tickers = [t.strip().upper() for t in raw.replace("\n", ",").split(",") if t.strip()]
elif universe_name == "ETFs":
    tickers = ETF_UNIVERSE
    st.sidebar.caption(f"{len(tickers)} ETFs across sectors, themes & markets · two-pass scan")
else:
    try:
        tickers = load_universe(universe_name)
        st.sidebar.caption(f"{len(tickers)} tickers · two-pass scan (tech filter first, then options)")
    except Exception as e:
        st.sidebar.error(f"Could not load {universe_name}: {e}")
        tickers = []

st.sidebar.divider()
st.sidebar.subheader("Filters")

max_premium = st.sidebar.slider(
    "Max premium per contract ($)",
    min_value=1.0, max_value=20.0, value=6.0, step=0.5,
    help="Mid-price cap. $6 = $600/contract."
)

dte_range = st.sidebar.slider("DTE window", 7, 90, (21, 45))

min_delta = st.sidebar.slider(
    "Min |delta| for strike",
    min_value=0.20, max_value=0.70, value=0.40, step=0.05,
    help="0.40–0.50 = ATM-ish directional play"
)

max_iv = st.sidebar.slider(
    "Max IV (avg for expiry window)",
    min_value=10, max_value=100, value=60, step=5,
    format="%d%%",
    help="Filter out options with elevated implied volatility. 60% = skip anything above 60% IV."
)

st.sidebar.divider()
unusual_only     = st.sidebar.checkbox("🔥 Unusual activity only", value=False)
exclude_earnings = st.sidebar.checkbox("🚫 Exclude earnings risk", value=True,
    help="Hide tickers with earnings inside your DTE window (binary event risk)")

# ── Main ──────────────────────────────────────────────────────────────────────
st.title("📊 Options Swing Screener")
tab_scanner, tab_deep, tab_decide, tab_journal = st.tabs(["📊 Scanner", "🔍 Ticker Deep Dive", "🎯 Trade Decision", "📓 Journal"])


# ── Scan helpers ──────────────────────────────────────────────────────────────
def _fetch_tech_only(sym: str) -> tuple:
    return sym, get_technicals(sym)

def _fetch_full_result(args: tuple) -> ScreenerResult:
    sym, fin, poly_key_ok, max_mid, dte_min, dte_max = args
    senti = get_sentiment(sym)
    chain = get_options_chain(sym, dte_min=7, dte_max=90) if poly_key_ok else None
    ua    = get_unusual_activity(sym, chain) if chain is not None else {}
    return build_result(sym, fin, senti, None, chain, ua,
                        max_mid=max_mid, dte_min=dte_min, dte_max=dte_max)

def _setup_passes(finviz, sel_strategy, strat_cfg, cust_cfg):
    if finviz is None:
        return False
    setup = finviz.get("setup", "Neutral")
    if sel_strategy == "🛠️ Custom Strategy":
        rsi = finviz.get("rsi") or 50
        rng = cust_cfg.get("rsi_range", (0, 100))
        if not (rng[0] <= rsi <= rng[1]):
            return False
        sma50 = finviz.get("sma50_diff_pct")
        if cust_cfg.get("sma50") == "Above" and (sma50 is None or sma50 < 0):
            return False
        if cust_cfg.get("sma50") == "Below" and (sma50 is None or sma50 >= 0):
            return False
        if (finviz.get("rel_volume") or 0) < cust_cfg.get("min_rel_vol", 1.0):
            return False
        return True
    if strat_cfg.get("setup_filter") and setup != strat_cfg["setup_filter"]:
        return False
    if setup == "Neutral":
        return False
    return True

def _confluence_label(holding_setup: str, etf_setup: str) -> str:
    """Rate the confluence between ETF setup and a top holding's setup."""
    bullish_setups = {"Breakout", "MA Reclaim", "Oversold"}
    bearish_setups = {"Blow-off Top", "MA Breakdown"}
    neutral_setups = {"Consolidation", "Neutral"}

    if etf_setup in ("Consolidation", "Breakout", "MA Reclaim", "Oversold"):
        if holding_setup in bullish_setups:
            return "🟢🟢 High — holding breaking out while ETF coils/reclaims"
        if holding_setup == "Consolidation":
            return "🟢 Moderate — both coiling, watch for breakout"
        if holding_setup in bearish_setups:
            return "🔴 Low — holding diverging bearishly"
    if etf_setup in ("Blow-off Top", "MA Breakdown"):
        if holding_setup in bearish_setups:
            return "🔴🔴 High — holding breaking down with ETF"
        if holding_setup in bullish_setups:
            return "⚠️ Diverging — holding bullish vs bearish ETF"
    return "⚪ Neutral"


# ── Most-active options cache (5-min TTL) ─────────────────────────────────────
@st.cache_data(ttl=300, show_spinner=False)
def _cached_top_volume():
    return get_top_volume_options(top_n=25)


# ── Scanner tab ───────────────────────────────────────────────────────────────
def _render_scanner_tab():
    # ── Pinned: most active options today ─────────────────────────────────────
    with st.expander("🔥 Most Active Options Today", expanded=True):
        col_refresh, col_note = st.columns([1, 5])
        with col_refresh:
            if st.button("↺ Refresh", key="refresh_top_vol", help="Re-fetch top volume (cached 5 min)"):
                st.cache_data.clear()
        with col_note:
            st.caption("Top 25 contracts by volume across the entire US options market · refreshes every 5 min")

        with st.spinner("Loading most active options…"):
            tv = _cached_top_volume()

        if tv.empty:
            st.warning(
                "Market-wide options snapshot unavailable. "
                "This endpoint may require a Polygon plan above Starter. "
                "Check your plan at polygon.io/dashboard."
            )
        else:
            disp = tv.copy()
            disp["type"]          = disp["type"].str.upper()
            disp["strike"]        = disp["strike"].apply(lambda x: f"${x:.0f}")
            disp["mid"]           = disp["mid"].apply(lambda x: f"${x:.2f}")
            disp["iv"]            = disp["iv"].apply(lambda x: f"{x:.0%}" if pd.notna(x) else "—")
            disp["delta"]         = disp["delta"].apply(lambda x: f"{x:.2f}" if pd.notna(x) else "—")
            disp["volume"]        = disp["volume"].apply(lambda x: f"{int(x):,}")
            disp["open_interest"] = disp["open_interest"].apply(lambda x: f"{int(x):,}")
            if "notional" in disp.columns:
                disp["notional"] = disp["notional"].apply(
                    lambda x: f"${x/1_000_000:.1f}M" if x >= 1_000_000 else f"${int(x):,}"
                )
            col_rename = {
                "ticker": "Ticker", "type": "Type", "strike": "Strike",
                "expiration": "Expiry", "dte": "DTE", "volume": "Volume",
                "open_interest": "OI", "mid": "Mid", "iv": "IV",
                "delta": "Delta", "notional": "Notional",
            }
            ordered = ["ticker","type","strike","expiration","dte","volume","open_interest","mid","iv","delta","notional"]
            disp = disp[[c for c in ordered if c in disp.columns]].rename(columns=col_rename)
            st.dataframe(
                disp, hide_index=True, use_container_width=True,
                column_config={
                    col: st.column_config.TextColumn(width="small")
                    for col in disp.columns if col != "Expiry"
                },
            )

    st.divider()
    st.markdown(f"### {selected_strategy}")
    st.caption(strat["description"])

    if strat["entry"]:
        col_e, col_i, col_a = st.columns(3)
        with col_e:
            st.markdown("**Entry criteria**")
            for item in strat["entry"]:
                st.markdown(f"- {item}")
        with col_i:
            st.markdown("**Ideal conditions**")
            for item in strat["ideal"]:
                st.markdown(f"- {item}")
        with col_a:
            st.markdown("**Avoid when**")
            for item in strat["avoid"]:
                st.markdown(f"- {item}")

    st.divider()

    LARGE_UNIVERSE = len(tickers) > 30

    if st.button("▶ Run Screener", type="primary"):
        if not tickers:
            st.error("No tickers loaded. Check your universe selection.")
            return

        raw_results = []
        prog  = st.progress(0)
        label = st.empty()

        if LARGE_UNIVERSE:
            label.write(f"**Pass 1 of 2** — technicals for {len(tickers)} tickers…")
            tech_data = {}

            with ThreadPoolExecutor(max_workers=6) as ex:
                futures = {ex.submit(_fetch_tech_only, t): t for t in tickers}
                done = 0
                for fut in as_completed(futures):
                    done += 1
                    prog.progress(int(done / len(tickers) * 50))
                    sym, fin = fut.result()
                    tech_data[sym] = fin

            survivors = [
                t for t in tickers
                if _setup_passes(tech_data.get(t), selected_strategy, strat, custom_cfg)
            ]
            if not survivors:
                survivors = [t for t in tickers if tech_data.get(t) is not None]

            label.write(f"**Pass 2 of 2** — options + sentiment for **{len(survivors)}** tickers…")
            for i, sym in enumerate(survivors):
                prog.progress(50 + int((i + 1) / len(survivors) * 50))
                label.write(f"Fetching options for **{sym}** ({i+1}/{len(survivors)})…")
                try:
                    raw_results.append(_fetch_full_result((
                        sym, tech_data.get(sym), poly_ok,
                        max_premium, dte_range[0], dte_range[1],
                    )))
                except Exception as e:
                    st.warning(f"Skipped {sym}: {e}")
        else:
            for i, ticker in enumerate(tickers):
                prog.progress(int((i + 1) / len(tickers) * 100))
                label.write(f"Scanning **{ticker}** ({i+1}/{len(tickers)})…")
                finviz = get_technicals(ticker)
                senti  = get_sentiment(ticker)
                chain  = get_options_chain(ticker, dte_min=7, dte_max=90) if poly_ok else None
                ua     = get_unusual_activity(ticker, chain) if chain is not None else {}
                raw_results.append(build_result(
                    ticker, finviz, senti, None, chain, ua,
                    max_mid=max_premium, dte_min=dte_range[0], dte_max=dte_range[1],
                ))

        prog.empty()
        label.empty()

        if not raw_results:
            st.error("No data returned.")
            st.stop()

        st.session_state["results"] = raw_results

    if "results" not in st.session_state:
        st.info("Configure your strategy and tickers in the sidebar, then click **▶ Run Screener**.")
        return

    results = st.session_state["results"]

    # ── Filter ────────────────────────────────────────────────────────────────
    def keep(r: ScreenerResult) -> bool:
        if r.composite is None:
            return False
        if selected_strategy == "🛠️ Custom Strategy":
            if strat["direction_filter"] and r.strategy != strat["direction_filter"]:
                return False
            rsi = r.rsi or 50
            if not (custom_cfg["rsi_range"][0] <= rsi <= custom_cfg["rsi_range"][1]):
                return False
            if custom_cfg["sma50"] == "Above" and (r.sma50_diff_pct is None or r.sma50_diff_pct < 0):
                return False
            if custom_cfg["sma50"] == "Below" and (r.sma50_diff_pct is None or r.sma50_diff_pct >= 0):
                return False
            if (r.rel_volume or 0) < custom_cfg["min_rel_vol"]:
                return False
            if r.pct_from_52w_high is not None:
                if not (custom_cfg["high_range"][0] <= r.pct_from_52w_high <= custom_cfg["high_range"][1]):
                    return False
            if custom_cfg["min_bull_pct"] > 0 and (r.bullish_pct is None or r.bullish_pct < custom_cfg["min_bull_pct"]):
                return False
            if custom_cfg["require_unusual"] and not r.unusual:
                return False
        else:
            if strat["setup_filter"] and r.setup != strat["setup_filter"]:
                return False
            if strat["direction_filter"] and r.strategy != strat["direction_filter"]:
                return False
            if strat["setup_filter"] is None and strat["direction_filter"] is None:
                if r.strategy is None:
                    return False
            if r.setup == "Neutral":
                return False
            if unusual_only and not r.unusual:
                return False
        if r.has_options:
            mid = (r.rec_bid + r.rec_ask) / 2
            if mid > max_premium:
                return False
            if abs(r.rec_delta or 0) < min_delta:
                return False
        if r.avg_iv is not None and r.avg_iv > (max_iv / 100):
            return False
        if exclude_earnings and r.earnings_within_dte:
            return False
        return True

    shown = sorted([r for r in results if keep(r)],
                   key=lambda r: abs(r.composite or 0), reverse=True)

    total_unusual = sum(1 for r in results if r.unusual)
    st.success(
        f"Scanned **{len(results)}** tickers — "
        f"**{len(shown)}** match **{selected_strategy}** · "
        f"🔥 {total_unusual} with unusual activity"
    )

    if not shown:
        st.warning(
            f"No tickers matched **{selected_strategy}**. "
            "Try 'All Setups', raising max premium, or widening DTE window."
        )
        with st.expander("Debug — all scores"):
            debug_rows = []
            for r in sorted(results, key=lambda x: abs(x.composite or 0), reverse=True):
                mid = (r.rec_bid + r.rec_ask) / 2 if r.rec_bid and r.rec_ask else None
                debug_rows.append({
                    "Symbol":    r.symbol,
                    "Score":     f"{r.composite:+.2f}" if r.composite is not None else "—",
                    "Strategy":  r.strategy or "no conviction",
                    "Setup":     r.setup or "—",
                    "Has Chain": "✓" if r.has_options else "✗",
                    "Mid":       f"${mid:.2f}" if mid else "—",
                    "Delta":     f"{r.rec_delta:.2f}" if r.rec_delta else "—",
                })
            st.dataframe(pd.DataFrame(debug_rows), hide_index=True, use_container_width=True)
        return

    # ── Results table ─────────────────────────────────────────────────────────
    st.subheader(f"Matches — {selected_strategy}")
    rows = [r.to_row() for r in shown]
    df   = pd.DataFrame(rows)
    small_cols = ["Dir","Score","RSI","Rel Vol","Unusual","Strike","Mid","Delta","IV Env","IV Premium","DTE","Earnings","BE Move","R/BE"]
    st.dataframe(
        df, use_container_width=True, hide_index=True,
        column_config={c: st.column_config.TextColumn(width="small") for c in small_cols},
    )

    # ── Detail panel ──────────────────────────────────────────────────────────
    st.divider()
    sel = st.selectbox("Inspect:", [r.symbol for r in shown])
    r   = next(x for x in shown if x.symbol == sel)

    # Strategy fit flags
    _has_unusual = r.unusual
    _rsi = r.rsi or 50
    fit_flags = []
    if selected_strategy == "🚀 Breakout → Long Call":
        if r.rel_volume and r.rel_volume >= 1.2:  fit_flags.append(("✅", "Elevated relative volume"))
        else:                                      fit_flags.append(("⚠️", "Relative volume below 1.2× — weaker conviction"))
        if r.pct_from_52w_high is not None and r.pct_from_52w_high >= -2:
                                                   fit_flags.append(("✅", f"Within {abs(r.pct_from_52w_high):.1f}% of 52W high"))
        if _has_unusual:                           fit_flags.append(("✅", "Unusual call flow — institutional confirmation"))
        if _rsi > 75:                              fit_flags.append(("⚠️", f"RSI {_rsi:.0f} — overextended, pullback risk"))
        elif _rsi >= 55:                           fit_flags.append(("✅", f"RSI {_rsi:.0f} — strong momentum"))
    elif selected_strategy == "📈 MA Reclaim → Long Call":
        if r.sma50_diff_pct is not None and 0 <= r.sma50_diff_pct <= 5:
                                                   fit_flags.append(("✅", f"Price {r.sma50_diff_pct:+.1f}% above SMA50"))
        if _has_unusual:                           fit_flags.append(("✅", "Unusual activity — buying confirmed"))
        if r.rel_volume and r.rel_volume >= 1.2:  fit_flags.append(("✅", "Volume spike on reclaim"))
        else:                                      fit_flags.append(("⚠️", "Low volume — reclaim not yet confirmed"))
    elif selected_strategy == "💚 Oversold Bounce → Long Call":
        if _rsi < 30:                              fit_flags.append(("✅", f"RSI {_rsi:.0f} — deeply oversold"))
        elif _rsi < 35:                            fit_flags.append(("✅", f"RSI {_rsi:.0f} — oversold"))
        if r.bullish_pct is not None and r.bullish_pct < 40:
                                                   fit_flags.append(("✅", "Bearish crowd sentiment — contrarian opportunity"))
        if r.rel_volume and r.rel_volume >= 1.5:  fit_flags.append(("✅", "High volume on down day — possible selling climax"))
    elif selected_strategy == "🔴 Blow-off Top → Long Put":
        if _rsi > 80:                              fit_flags.append(("✅", f"RSI {_rsi:.0f} — extreme extension"))
        elif _rsi > 75:                            fit_flags.append(("✅", f"RSI {_rsi:.0f} — overextended"))
        if _has_unusual and r.unusual_type == "put": fit_flags.append(("✅", "Unusual put flow — smart money fading"))
        elif _has_unusual:                         fit_flags.append(("⚠️", "Unusual activity is CALLS — insiders still bullish"))
        if r.iv_warning:                           fit_flags.append(("⚠️", f"IV {r.avg_iv:.0%} — puts are expensive"))
    elif selected_strategy == "📉 MA Breakdown → Long Put":
        if r.sma50_diff_pct is not None and r.sma50_diff_pct < 0:
                                                   fit_flags.append(("✅", f"Price {r.sma50_diff_pct:+.1f}% below SMA50"))
        if _has_unusual and r.unusual_type == "put": fit_flags.append(("✅", "Unusual put flow — institutional positioning"))
        if _rsi < 30:                              fit_flags.append(("⚠️", f"RSI {_rsi:.0f} — oversold, reversal risk"))
        else:                                      fit_flags.append(("✅", f"RSI {_rsi:.0f} — room to fall"))
    elif selected_strategy == "🟡 Consolidation Watch":
        if r.consolidation_triggered:
            fit_flags.append(("✅", "⚡ Breakout trigger fired — volume expanded above 5-day range today"))
        else:
            fit_flags.append(("⚠️", "Still coiling — wait for volume expansion + price breakout before entering"))
        if _has_unusual:                           fit_flags.append(("✅", f"Unusual {r.unusual_type} flow — direction signal confirmed"))

    if r.earnings_within_dte and r.days_to_earnings is not None:
        st.error(f"🚨 **Earnings in {r.days_to_earnings} days** — inside your DTE window. Binary event risk: IV will spike then crush. Consider a shorter DTE or skip.")
    if r.iv_warning:
        st.warning(f"⚠️ **Elevated IV ({r.avg_iv:.0%})** — buying expensive premium. Consider sizing down.")
    if r.iv_premium is not None and r.iv_premium > 1.20:
        st.warning(f"⚠️ **IV Rich ({r.iv_premium:.2f}×)** — implied vol is {r.iv_premium:.2f}× the 30-day realized vol. Options are historically expensive here.")

    if fit_flags:
        with st.expander(f"Strategy fit — {sel}", expanded=True):
            for icon, msg in fit_flags:
                st.markdown(f"{icon} {msg}")

    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("**Technicals — Finviz**")
        setup_colors = {"Breakout":"🟢","MA Reclaim":"🟢","Oversold":"🟢",
                        "Blow-off Top":"🔴","MA Breakdown":"🔴",
                        "Consolidation":"🟡","Neutral":"⚪"}
        icon = setup_colors.get(r.setup or "", "⚪")
        setup_label = r.setup or "—"
        if r.setup == "Consolidation" and r.consolidation_triggered:
            setup_label = "Consolidation ⚡ Triggered"
            icon = "🟢"
        st.metric("Setup",      f"{icon} {setup_label}")
        st.metric("Signal",     r.signal or "—")
        st.metric("RSI",        f"{r.rsi:.0f}" if r.rsi else "—")
        st.metric("Day Chg",    f"{r.change_pct:+.2f}%" if r.change_pct is not None else "—")
        st.metric("Rel Volume", f"{r.rel_volume:.1f}x" if r.rel_volume else "—")
        if r.sma20_diff_pct is not None: st.metric("vs SMA20", f"{r.sma20_diff_pct:+.1f}%")
        if r.sma50_diff_pct is not None: st.metric("vs SMA50", f"{r.sma50_diff_pct:+.1f}%")
        if r.pct_from_52w_high is not None: st.metric("From 52W High", f"{r.pct_from_52w_high:+.1f}%")
    with c2:
        st.markdown("**Sentiment — Reddit**")
        bull = r.bullish_pct
        st.metric("Bullish %",   f"{bull:.0f}%" if bull is not None else "—")
        if bull is not None: st.progress(int(bull))
        st.metric("Posts found", r.reddit_posts or "—")
        if r.unusual:
            st.markdown("---")
            st.markdown("**🔥 Unusual Activity**")
            st.metric("Vol / OI", f"{r.unusual_ratio:.1f}x")
            st.metric("Contract", f"{r.unusual_type.upper() if r.unusual_type else '—'} ${r.unusual_strike:.0f}" if r.unusual_strike else "—")
            st.metric("Expiry",   r.unusual_exp or "—")
    with c3:
        st.markdown("**Options Recommendation**")
        st.metric("Composite", f"{r.composite:+.2f}" if r.composite is not None else "—")
        st.metric("Strategy",  r.strategy or "—")
        if r.strategy_note: st.caption(r.strategy_note)
        st.metric("IV Env",     r.iv_env())
        st.metric("IV Premium", r.iv_premium_label() + (f" ({r.iv_premium:.2f}×)" if r.iv_premium else ""))
        if r.days_to_earnings is not None:
            earnings_label = f"⚠️ {r.days_to_earnings}d" if r.earnings_within_dte else f"{r.days_to_earnings}d"
            st.metric("Earnings", earnings_label)
        if r.has_options:
            mid = (r.rec_bid + r.rec_ask) / 2
            st.metric("Strike",  f"${r.rec_strike:.2f}")
            st.metric("Premium", f"${mid:.2f}  (${mid*100:.0f}/contract)")
            st.metric("Delta",   f"{r.rec_delta:.2f}")
            st.metric("IV",      f"{r.rec_iv:.1%}" if r.rec_iv else "—")
            st.metric("DTE",     r.rec_dte)
            if r.reward_to_breakeven is not None:
                st.metric("R/Breakeven", f"{r.reward_to_breakeven:.1f}×",
                          help="(Target − Breakeven) ÷ Premium. >1.5× is good risk/reward.")
        else:
            st.info("No strike found within budget/delta filters" if poly_ok else "Polygon key inactive")

    # ── Price targets ──────────────────────────────────────────────────────────
    st.divider()
    st.markdown("**Price Targets**")
    pt1, pt2, pt3 = st.columns(3)
    with pt1:
        st.markdown("*Option breakeven (at expiry)*")
        if r.option_breakeven:
            st.metric("Stock must reach", f"${r.option_breakeven:.2f}",
                      delta=f"{r.pct_to_breakeven:+.1f}% from here" if r.pct_to_breakeven is not None else None)
            st.caption(f"Strike {'+ ' if r.strategy == 'Long Call' else '− '}premium paid. Must exceed by expiry to profit.")
        else:
            st.info("No option selected")
    with pt2:
        st.markdown("*Technical target (setup-based)*")
        if r.technical_target and r.price:
            st.metric(r.setup or "Target", f"${r.technical_target:.2f}",
                      delta=f"{r.pct_to_tech_tgt:+.1f}% from here" if r.pct_to_tech_tgt is not None else None)
            atr_note = f" (ATR={r.atr:.2f})" if r.atr else ""
            target_logic = {
                "Breakout":     f"52W high + ATR×3{atr_note} — continuation above breakout",
                "MA Reclaim":   f"price + ATR×3{atr_note} — rally to prior swing high",
                "Oversold":     "SMA20 — mean reversion target",
                "Blow-off Top": "SMA50 — pullback to 50-day MA",
                "MA Breakdown": f"price − ATR×3{atr_note} — volatility-adjusted downside",
                "Consolidation":f"price + ATR×2{atr_note} — breakout expansion target",
            }
            st.caption(target_logic.get(r.setup or "", "Setup-based estimate"))
        else:
            st.info("No technical target for this setup")
    with pt3:
        st.markdown("*Analyst consensus target*")
        if r.analyst_target and r.price:
            st.metric("Wall St. target", f"${r.analyst_target:.2f}",
                      delta=f"{r.pct_to_analyst_tgt:+.1f}% from here" if r.pct_to_analyst_tgt is not None else None)
            direction = "above" if (r.pct_to_analyst_tgt or 0) > 0 else "below"
            st.caption(f"Analyst consensus is {abs(r.pct_to_analyst_tgt or 0):.1f}% {direction} current price")
        else:
            st.info("No analyst target available")

    # ── ETF Holdings Drill-Down ───────────────────────────────────────────────
    holdings = get_etf_holdings(sel)
    if holdings:
        st.divider()
        category = etf_category(sel)
        st.markdown(f"**🔍 Top Holdings Confluence — {sel}** · *{category}*")
        st.caption(
            "Scan the ETF's top holdings for breakout/reclaim signals. "
            "When the ETF is consolidating and a top holding is breaking out, "
            "that's high-confluence — sector rotation is flowing into that stock."
        )

        drill_state_key  = f"drill_results_{sel}"
        drill_button_key = f"drill_btn_{sel}"
        if st.button(f"🔍 Scan top {len(holdings)} holdings of {sel}", key=drill_button_key):
            st.session_state[drill_state_key] = True

        if st.session_state.get(drill_state_key):
            holding_results = []
            h_prog  = st.progress(0)
            h_label = st.empty()
            for i, sym in enumerate(holdings):
                h_prog.progress(int((i + 1) / len(holdings) * 100))
                h_label.write(f"Scanning **{sym}**…")
                try:
                    fin = get_technicals(sym)
                    if fin:
                        holding_results.append({
                            "Symbol":     sym,
                            "Setup":      fin.get("setup", "—"),
                            "RSI":        f"{fin['rsi']:.0f}" if fin.get("rsi") else "—",
                            "Signal":     fin.get("signal", "—"),
                            "vs SMA50":   f"{fin['sma50_diff_pct']:+.1f}%" if fin.get("sma50_diff_pct") is not None else "—",
                            "From 52W Hi":f"{fin['pct_from_52w_high']:+.1f}%" if fin.get("pct_from_52w_high") is not None else "—",
                            "Rel Vol":    f"{fin['rel_volume']:.1f}x" if fin.get("rel_volume") else "—",
                            "Confluence": _confluence_label(fin.get("setup",""), r.setup),
                        })
                except Exception:
                    pass
            h_prog.empty()
            h_label.empty()

            if holding_results:
                setup_priority = ["Breakout","MA Reclaim","Oversold","Blow-off Top","MA Breakdown","Consolidation","Neutral","—"]
                holding_results.sort(key=lambda x: setup_priority.index(x["Setup"]) if x["Setup"] in setup_priority else 99)
                h_df = pd.DataFrame(holding_results)
                st.dataframe(
                    h_df, use_container_width=True, hide_index=True,
                    column_config={
                        "Confluence": st.column_config.TextColumn(width="medium"),
                        "Setup":      st.column_config.TextColumn(width="medium"),
                    }
                )
                breakouts = [x["Symbol"] for x in holding_results if x["Setup"] in ("Breakout","MA Reclaim")]
                if breakouts:
                    st.success(f"**High confluence:** {', '.join(breakouts)} showing Breakout/MA Reclaim while {sel} is in {r.setup}.")
            else:
                st.info("Could not fetch technicals for holdings.")

    # ── Save to Journal ───────────────────────────────────────────────────────
    st.divider()
    st.markdown("**📓 Journal**")
    with st.form(key=f"journal_form_{sel}"):
        j_col1, j_col2 = st.columns([1, 2])
        with j_col1:
            j_status = st.radio("Status", ["Watching", "Entered"], horizontal=True,
                                help="Watching = paper trade · Entered = real position")
        with j_col2:
            j_notes = st.text_area("Notes / thesis", placeholder="Why this play? Key levels to watch?", height=80)
        if st.form_submit_button("📓 Save to Journal"):
            if not r.has_options:
                st.warning("No option strike found — save anyway as a watchlist entry?")
            entry_id = add_entry(r, status=j_status, notes=j_notes)
            st.success(f"Saved **{r.symbol}** to journal (#{entry_id}) as **{j_status}**. View in the Journal tab.")

    # ── Full chain ────────────────────────────────────────────────────────────
    if poly_ok:
        with st.expander(f"Full options chain — {sel}"):
            chain = get_options_chain(sel, 7, 90)
            if chain is not None and not chain.empty:
                col_ct, col_dte = st.columns(2)
                ct_sel  = col_ct.radio("Type", ["call", "put", "both"], horizontal=True)
                dte_sel = col_dte.slider("DTE", 0, 90, dte_range, key="chain_dte")
                view = chain[(chain["dte"] >= dte_sel[0]) & (chain["dte"] <= dte_sel[1])]
                if ct_sel != "both":
                    view = view[view["type"] == ct_sel]
                view = view.sort_values(["expiration", "strike"])
                disp = view[["strike","expiration","dte","type","bid","ask","mid",
                              "iv","delta","gamma","theta","vega","volume","open_interest"]].copy()
                disp["bid"] = disp["bid"].map("${:.2f}".format)
                disp["ask"] = disp["ask"].map("${:.2f}".format)
                disp["mid"] = disp["mid"].map("${:.2f}".format)
                disp["iv"]  = disp["iv"].apply(lambda x: f"{x:.1%}" if pd.notna(x) else "—")
                for g in ["delta","gamma","theta","vega"]:
                    disp[g] = disp[g].apply(lambda x: f"{x:.3f}" if pd.notna(x) else "—")
                st.dataframe(disp, use_container_width=True, hide_index=True, height=380)

with tab_scanner:
    _render_scanner_tab()

# ── Deep Dive tab ─────────────────────────────────────────────────────────────
with tab_deep:
    st.markdown("### 🔍 Single-Ticker Options Analysis")
    st.caption("Full chain breakdown — PCR, max pain, GEX, call/put walls, IV surface, unusual flow, and confluence score.")

    dd_col1, dd_col2 = st.columns([2, 5])
    with dd_col1:
        dd_sym = st.text_input("Ticker", placeholder="e.g. AAPL", key="dd_ticker",
                               label_visibility="collapsed").upper().strip()
    with dd_col2:
        dd_dte_max = st.slider("Chain DTE window", 7, 120, 90, key="dd_dte",
                               help="How many days out to load the chain")

    run_dd = st.button("▶ Analyze", key="dd_run", type="primary")

    if run_dd and dd_sym:
        with st.spinner(f"Fetching {dd_sym} options chain…"):
            dd_chain = get_options_chain(dd_sym, dte_min=0, dte_max=dd_dte_max)
            dd_spot  = get_spot_price(dd_sym)

        if dd_chain is None or dd_chain.empty:
            st.error(f"No options data returned for **{dd_sym}**. Check the ticker or Polygon key.")
        elif not dd_spot:
            st.error(f"Could not fetch spot price for **{dd_sym}**.")
        else:
            # Persist OI + IV snapshot for change tracking
            save_snapshot(dd_sym, dd_chain)
            dd_chain = get_oi_change(dd_sym, dd_chain)
            st.session_state["dd_result"] = {
                "sym":   dd_sym,
                "chain": dd_chain,
                "spot":  dd_spot,
            }

    if "dd_result" in st.session_state:
        dr    = st.session_state["dd_result"]
        sym   = dr["sym"]
        chain = dr["chain"]
        spot  = dr["spot"]

        with st.spinner("Computing analysis…"):
            ana = full_analysis(chain, spot)

        if not ana:
            st.warning("Analysis returned no data.")
        else:
            pcr_d  = ana["pcr"]
            pain   = ana["max_pain"]
            walls  = ana["walls"]
            gex_df = ana["gex_df"]
            net_g  = ana["net_gex"]
            term   = ana["term"]
            skew   = ana["skew"]
            clust  = ana["clusters"]
            flow   = ana["flow"]
            hi_iv  = ana["high_iv"]
            conf   = ana["confluence"]

            # ── Banner: Confluence Lean ────────────────────────────────────────
            lean_color = {"🟢🟢": "success", "🟢": "success",
                          "🔴🔴": "error",   "🔴": "error"}.get(
                conf["lean"].split()[0], "info")
            getattr(st, lean_color)(
                f"**{sym} @ ${spot:.2f}** — Confluence lean: **{conf['lean']}** (score {conf['score']:+d})"
            )

            # ── Row 1: Key metrics ─────────────────────────────────────────────
            pv = pcr_d.get("pcr_volume")
            po = pcr_d.get("pcr_oi")
            gex_bn = net_g / 1_000_000_000

            def _pcr_delta(v):
                if v is None: return None
                if v < 0.7:   return "bullish"
                if v > 1.0:   return "bearish"
                return "neutral"

            ma, mb, mc, md = st.columns(4)
            ma.metric("Spot",      f"${spot:.2f}")
            mb.metric("Max Pain",  f"${pain:.2f}" if pain else "—",
                      delta=f"{(spot-pain)/pain*100:+.1f}% from spot" if pain else None)
            mc.metric("Net GEX",   f"${gex_bn:+.2f}B",
                      delta="Pinning" if gex_bn > 0 else "Vol expansion",
                      delta_color="normal" if gex_bn > 0 else "inverse")
            md.metric("PCR (vol)", f"{pv:.2f}" if pv else "—",
                      delta=_pcr_delta(pv), delta_color="inverse")

            me, mf, mg, mh = st.columns(4)
            me.metric("PCR (OI)",    f"{po:.2f}" if po else "—")
            mf.metric("Call Volume", f"{pcr_d['call_volume']:,}")
            mg.metric("Put Volume",  f"{pcr_d['put_volume']:,}")
            mh.metric("Call OI",     f"{pcr_d['call_oi']:,}")

            st.divider()

            # ── Prime Trade box ───────────────────────────────────────────────
            def _find_prime_trade(ch, sp):
                """
                Score every contract on 5 criteria and return the best call + put.
                Criteria: delta proximity (0.45 target), gamma rank, above-median OI,
                          IV not overpriced (≤ chain avg × 1.2), theta/premium ratio.
                """
                iv_clean  = ch["iv"].dropna()
                iv_clean  = iv_clean[(iv_clean >= 0.05) & (iv_clean <= 3.0)]
                avg_iv    = float(iv_clean.mean()) if not iv_clean.empty else None
                iv_ceil   = (avg_iv * 1.2) if avg_iv else 3.0
                med_oi    = float(ch["open_interest"].median())
                out = {}
                for opt_type in ["call", "put"]:
                    s = ch[
                        (ch["type"] == opt_type) &
                        (ch["dte"].between(14, 60)) &
                        (ch["mid"] > 0.20) &
                        (ch["open_interest"] >= med_oi) &
                        ch["delta"].notna() & ch["gamma"].notna() &
                        ch["iv"].notna() &
                        (ch["iv"] <= iv_ceil) & (ch["iv"] >= 0.05)
                    ].copy()
                    tgt = 0.45 if opt_type == "call" else -0.45
                    if opt_type == "call":
                        s = s[s["delta"].between(0.28, 0.68)]
                    else:
                        s = s[s["delta"].between(-0.68, -0.28)]
                    if s.empty:
                        out[opt_type] = None; continue
                    s["_d"] = 1 - (s["delta"] - tgt).abs().clip(upper=0.25) / 0.25
                    mx_g = s["gamma"].max()
                    s["_g"] = s["gamma"] / mx_g if mx_g > 0 else 0
                    mx_o = s["open_interest"].max()
                    s["_o"] = s["open_interest"] / mx_o if mx_o > 0 else 0
                    mn_iv, mx_iv = s["iv"].min(), s["iv"].max()
                    s["_iv"] = 1 - (s["iv"] - mn_iv) / (mx_iv - mn_iv + 1e-9)
                    if "theta" in s.columns and s["theta"].notna().any():
                        s["_tr"] = s["theta"].abs() / s["mid"].clip(lower=0.01)
                        mx_tr = s["_tr"].max()
                        s["_t"] = 1 - s["_tr"] / (mx_tr + 1e-9)
                    else:
                        s["_t"] = 0.5
                    s["score"] = (s["_d"]*0.30 + s["_g"]*0.25 +
                                  s["_o"]*0.20 + s["_iv"]*0.15 + s["_t"]*0.10)
                    out[opt_type] = s.nlargest(1, "score").iloc[0]
                return out

            prime = _find_prime_trade(chain, spot)
            pc, pp = prime.get("call"), prime.get("put")

            if pc is not None or pp is not None:
                st.markdown("### 💎 Prime Trade")
                st.caption(
                    "Best-scoring contract per side: delta near 0.45, above-median OI, "
                    "IV ≤ chain avg × 1.2, highest gamma, manageable theta decay."
                )
                pt1, pt2 = st.columns(2)

                def _render_prime(col, row, label, color):
                    if row is None:
                        col.info(f"No qualifying {label} found.")
                        return
                    iv_str    = f"{row['iv']:.0%}"      if pd.notna(row.get("iv"))    else "—"
                    delta_str = f"{row['delta']:.2f}"   if pd.notna(row.get("delta")) else "—"
                    gamma_str = f"{row['gamma']:.4f}"   if pd.notna(row.get("gamma")) else "—"
                    theta_str = f"{row['theta']:.3f}"   if pd.notna(row.get("theta")) else "—"
                    vega_str  = f"{row['vega']:.3f}"    if pd.notna(row.get("vega"))  else "—"
                    mid_str   = f"${row['mid']:.2f}"
                    oi_str    = f"{int(row['open_interest']):,}"
                    vol_str   = f"{int(row['volume']):,}"
                    cost_str  = f"${row['mid']*100:.0f}"
                    with col:
                        with st.container(border=True):
                            st.markdown(f"**{label}** — ${row['strike']:.2f} · {row['expiration']} · {int(row['dte'])}DTE")
                            g1, g2, g3 = st.columns(3)
                            g1.metric("Mid",   mid_str, delta=f"{cost_str}/contract", delta_color="off")
                            g2.metric("Delta", delta_str)
                            g3.metric("IV",    iv_str)
                            g4, g5, g6 = st.columns(3)
                            g4.metric("Gamma",  gamma_str)
                            g5.metric("Theta",  theta_str)
                            g6.metric("Vega",   vega_str)
                            g7, g8 = st.columns(2)
                            g7.metric("Open Interest", oi_str)
                            g8.metric("Volume Today",  vol_str)

                _render_prime(pt1, pc, "📈 Prime Call", "success")
                _render_prime(pt2, pp, "📉 Prime Put",  "error")
                st.divider()

            # ── Row 2: Walls + Unusual Flow ────────────────────────────────────
            w1, w2, w3 = st.columns(3)

            def _fmt_oi_change(v):
                if v is None or (isinstance(v, float) and pd.isna(v)):
                    return "—"
                v = int(v)
                return f"+{v:,}" if v > 0 else f"{v:,}"

            with w1:
                st.markdown("**📈 Call Walls**")
                cw = walls["call_walls"].copy()
                # Merge OI change for wall strikes from the chain
                if "oi_change" in chain.columns:
                    oi_ch = (chain[chain["type"] == "call"]
                             .groupby("strike")["oi_change"].sum().reset_index()
                             .rename(columns={"oi_change": "_oi_ch"}))
                    cw = cw.merge(oi_ch, on="strike", how="left")
                    cw["OI Δ"] = cw["_oi_ch"].apply(_fmt_oi_change)
                cw["strike"]        = cw["strike"].apply(lambda x: f"${x:.0f}")
                cw["open_interest"] = cw["open_interest"].apply(lambda x: f"{int(x):,}")
                cw["volume"]        = cw["volume"].apply(lambda x: f"{int(x):,}")
                rename_cw = {"strike": "Strike", "expiration": "Expiry",
                             "open_interest": "OI", "volume": "Vol"}
                cw = cw.rename(columns=rename_cw)
                show_cw = [c for c in ["Strike","Expiry","OI","OI Δ","Vol"] if c in cw.columns]
                st.dataframe(cw[show_cw], hide_index=True, use_container_width=True,
                             column_config={c: st.column_config.TextColumn(width="small")
                                            for c in show_cw if c != "Expiry"})

            with w2:
                st.markdown("**📉 Put Walls**")
                pw = walls["put_walls"].copy()
                if "oi_change" in chain.columns:
                    oi_ch = (chain[chain["type"] == "put"]
                             .groupby("strike")["oi_change"].sum().reset_index()
                             .rename(columns={"oi_change": "_oi_ch"}))
                    pw = pw.merge(oi_ch, on="strike", how="left")
                    pw["OI Δ"] = pw["_oi_ch"].apply(_fmt_oi_change)
                pw["strike"]        = pw["strike"].apply(lambda x: f"${x:.0f}")
                pw["open_interest"] = pw["open_interest"].apply(lambda x: f"{int(x):,}")
                pw["volume"]        = pw["volume"].apply(lambda x: f"{int(x):,}")
                rename_pw = {"strike": "Strike", "expiration": "Expiry",
                             "open_interest": "OI", "volume": "Vol"}
                pw = pw.rename(columns=rename_pw)
                show_pw = [c for c in ["Strike","Expiry","OI","OI Δ","Vol"] if c in pw.columns]
                st.dataframe(pw[show_pw], hide_index=True, use_container_width=True,
                             column_config={c: st.column_config.TextColumn(width="small")
                                            for c in show_pw if c != "Expiry"})

            with w3:
                st.markdown("**🔥 Top Unusual Flow**")
                if not flow.empty:
                    fl = flow.copy()
                    fl["iv"]           = fl["iv"].apply(lambda x: f"{x:.0%}" if pd.notna(x) else "—")
                    fl["delta"]        = fl["delta"].apply(lambda x: f"{x:.2f}" if pd.notna(x) else "—")
                    fl["mid"]          = fl["mid"].apply(lambda x: f"${x:.2f}")
                    fl["notional"]     = fl["notional"].apply(lambda x: f"${int(x):,}")
                    fl["vol_oi_ratio"] = fl["vol_oi_ratio"].apply(lambda x: f"{x:.1f}x")
                    fl["strike"]       = fl["strike"].apply(lambda x: f"${x:.2f}")
                    fl["type"]         = fl["type"].str.upper()
                    show_fl = [c for c in ["type","strike","expiration","dte","volume",
                                           "vol_oi_ratio","notional","mid","delta","iv"]
                               if c in fl.columns]
                    st.dataframe(fl[show_fl], hide_index=True, use_container_width=True,
                                 column_config={"type": st.column_config.TextColumn("Type", width="small")})
                else:
                    st.info("No unusual flow found (need vol ≥ 100, OI ≥ 50, notional ≥ $25K).")

            st.divider()

            # ── Sweep Detection ────────────────────────────────────────────────
            with st.expander("🌊 Sweep Detection (institutional aggression)", expanded=True):
                st.caption(
                    "A sweep = 2+ fills within 2 seconds totalling ≥ 50 contracts. "
                    "Sweeps at the ask = aggressive buying. Checks top 3 unusual-flow contracts."
                )
                if not flow.empty and "option_symbol" in flow.columns:
                    top_contracts = flow["option_symbol"].dropna().head(3).tolist()
                    any_sweeps = False
                    for opt_sym in top_contracts:
                        if not opt_sym:
                            continue
                        trades_df = get_option_trades(opt_sym, limit=200)
                        sweeps = detect_sweeps(trades_df)
                        if sweeps:
                            any_sweeps = True
                            st.markdown(f"**{opt_sym}**")
                            sw_df = pd.DataFrame(sweeps)
                            sw_df["notional"] = sw_df["notional"].apply(
                                lambda x: f"${x/1_000_000:.2f}M" if x >= 1_000_000 else f"${x:,}"
                            )
                            sw_df.columns = ["Time", "Contracts", "Avg Price", "Fills", "Side", "Notional"]
                            st.dataframe(sw_df, hide_index=True, use_container_width=True)
                    if not any_sweeps:
                        st.info("No sweeps detected on top unusual contracts. Either no aggressive fills today or trades endpoint requires plan upgrade.")
                else:
                    st.info("Run analysis with unusual flow data to enable sweep detection.")

            st.divider()

            # ── Row 3: Volume clusters + High IV ──────────────────────────────
            vc1, vc2 = st.columns(2)

            with vc1:
                st.markdown("**⚡ Top Volume Contracts**")
                if not clust.empty:
                    cl = clust.copy()
                    cl["iv"]       = cl["iv"].apply(lambda x: f"{x:.0%}" if pd.notna(x) else "—")
                    cl["delta"]    = cl["delta"].apply(lambda x: f"{x:.2f}" if pd.notna(x) else "—")
                    cl["mid"]      = cl["mid"].apply(lambda x: f"${x:.2f}")
                    cl["notional"] = cl["notional"].apply(lambda x: f"${int(x):,}")
                    cl["strike"]   = cl["strike"].apply(lambda x: f"${x:.2f}")
                    cl["type"]     = cl["type"].str.upper()
                    st.dataframe(
                        cl[["type","strike","expiration","dte","volume","open_interest","mid","delta","iv","notional"]],
                        hide_index=True, use_container_width=True,
                    )
                else:
                    st.info("No volume data.")

            with vc2:
                st.markdown("**🌡️ Highest IV Contracts**")
                if not hi_iv.empty:
                    hi = hi_iv.copy()
                    hi["iv"]     = hi["iv"].apply(lambda x: f"{x:.0%}")
                    hi["delta"]  = hi["delta"].apply(lambda x: f"{x:.2f}" if pd.notna(x) else "—")
                    hi["mid"]    = hi["mid"].apply(lambda x: f"${x:.2f}")
                    hi["strike"] = hi["strike"].apply(lambda x: f"${x:.2f}")
                    hi["type"]   = hi["type"].str.upper()
                    st.dataframe(
                        hi[["type","strike","expiration","dte","iv","mid","delta","volume","open_interest"]],
                        hide_index=True, use_container_width=True,
                    )
                else:
                    st.info("No high-IV contracts found.")

            st.divider()

            # ── Row 4: IV Term Structure + IV Skew ────────────────────────────
            ts1, ts2 = st.columns(2)

            with ts1:
                st.markdown("**📅 IV Term Structure** (avg IV by DTE bucket)")
                if not term.empty:
                    t = term.copy()
                    t["avg_iv"] = t["avg_iv"].apply(lambda x: f"{x:.1%}")
                    t.columns   = ["DTE Bucket", "Avg IV", "# Contracts"]
                    st.dataframe(t, hide_index=True, use_container_width=True)
                    ivs = ana["term"]["avg_iv"].values
                    if len(ivs) >= 2 and ivs[0] > ivs[-1] * 1.15:
                        st.warning("⚠️ IV backwardation — near-term vol elevated. Likely event/earnings premium.")
                else:
                    st.info("Not enough data for term structure.")

            with ts2:
                st.markdown("**📐 IV Skew by Expiry** (call IV − put IV)")
                if not skew.empty:
                    sk = skew.copy()
                    sk["call_iv"] = sk["call_iv"].apply(lambda x: f"{x:.1%}")
                    sk["put_iv"]  = sk["put_iv"].apply(lambda x: f"{x:.1%}")
                    sk["skew"]    = sk["skew"].apply(
                        lambda x: f"+{x:.1%} 📈" if x > 0.02 else (f"{x:.1%} 📉" if x < -0.02 else f"{x:.1%}")
                    )
                    st.dataframe(sk, hide_index=True, use_container_width=True)
                    st.caption("Positive = call IV > put IV (bullish demand). Negative = normal protective put buying.")
                else:
                    st.info("Not enough data for skew.")

            st.divider()

            # ── IV History (stored snapshots) + Contract Price History ─────────
            ih1, ih2 = st.columns(2)

            with ih1:
                st.markdown("**📈 IV History — avg IV over time**")
                iv_hist = get_iv_history(sym, days=30)
                if not iv_hist.empty and len(iv_hist) > 1:
                    iv_hist_disp = iv_hist.rename(columns={"snapshot_date": "Date", "avg_iv": "Avg IV"})
                    iv_hist_disp["Avg IV"] = iv_hist_disp["Avg IV"].round(4)
                    st.line_chart(iv_hist_disp.set_index("Date")["Avg IV"])
                    st.caption("Stored from each time you run Deep Dive on this ticker. Builds up over days.")
                else:
                    st.info("IV history builds as you run Deep Dive on this ticker over multiple days. Check back tomorrow.")

            with ih2:
                st.markdown("**💰 Contract Price History (top unusual contract)**")
                if not flow.empty and "option_symbol" in flow.columns:
                    top_opt = flow["option_symbol"].dropna().iloc[0] if len(flow) > 0 else None
                    if top_opt:
                        price_hist = get_option_iv_history(top_opt, days=30)
                        if not price_hist.empty:
                            st.line_chart(price_hist.set_index("date")[["close","vwap"]])
                            st.caption(f"Daily close & VWAP for `{top_opt}` — requires options aggregates plan access.")
                        else:
                            st.info("No price history returned. Requires Polygon options aggregates access.")
                    else:
                        st.info("No unusual flow contract available.")
                else:
                    st.info("No unusual flow data.")

            st.divider()

            # ── GEX by Strike chart ────────────────────────────────────────────
            with st.expander("📊 Gamma Exposure (GEX) by Strike", expanded=False):
                if not gex_df.empty:
                    st.caption(
                        "Positive GEX = dealers long gamma → price-pinning near that strike. "
                        "Negative GEX = dealers short gamma → moves accelerate through that level."
                    )
                    gex_disp = gex_df.copy()
                    gex_disp["gex_m"]  = gex_disp["gex_m"].round(1)
                    gex_disp["strike"] = gex_disp["strike"].apply(lambda x: f"${x:.2f}")
                    gex_disp.columns   = ["Strike", "GEX ($M)", "GEX_M"]
                    st.bar_chart(gex_disp.set_index("Strike")["GEX ($M)"])
                else:
                    st.info("No gamma data — greeks not available from Polygon Starter plan on this ticker.")

            # ── Confluence signals detail ──────────────────────────────────────
            with st.expander("🧭 Confluence Signals Detail", expanded=True):
                for icon, msg in conf["signals"]:
                    st.markdown(f"{icon} {msg}")

# ── Trade Decision tab ────────────────────────────────────────────────────────
with tab_decide:
    st.markdown("### 🎯 Trade Decision Panel")
    st.caption("Enter a ticker and direction. Every signal scores green/yellow/red in real time. Pull the trigger only when score ≥ 60.")

    td_c1, td_c2, td_c3, td_c4 = st.columns([2, 2, 2, 1])
    with td_c1:
        td_sym = st.text_input("Ticker", placeholder="e.g. NVDA", key="td_sym",
                               label_visibility="collapsed").upper().strip()
    with td_c2:
        td_dir = st.radio("Direction", ["Long Call", "Long Put"],
                          horizontal=True, key="td_dir", label_visibility="collapsed")
    with td_c3:
        td_dte = st.slider("Max DTE", 7, 60, 45, key="td_dte")
    with td_c4:
        td_run = st.button("▶ Analyze", key="td_run", type="primary", use_container_width=True)

    if td_run and td_sym:
        with st.spinner(f"Loading {td_sym} — intraday bars, options chain, flow…"):
            # Always fetch full 90-day window; td_dte only filters strike selection
            td_chain = get_options_chain(td_sym, dte_min=0, dte_max=90)
            td_spot  = get_spot_price(td_sym)
            td_rs    = get_relative_strength(td_sym)
            td_fin   = get_technicals(td_sym)

        if not td_spot:
            st.error(f"Could not fetch price for {td_sym}.")
        elif td_chain is None or td_chain.empty:
            from screener.polygon_client import _last_api_error
            _detail = _last_api_error.get("msg", "unknown error — check Polygon key and plan")
            st.error(f"No options chain for {td_sym}. Polygon: {_detail}")
        else:
            st.session_state["td_result"] = {
                "sym": td_sym, "dir": td_dir, "chain": td_chain,
                "spot": td_spot, "rs": td_rs, "fin": td_fin,
            }

    if "td_result" not in st.session_state:
        st.info("Enter a ticker and click **▶ Analyze** to score the trade.")
    else:
        td = st.session_state["td_result"]
        sym   = td["sym"]
        dirx  = td["dir"]
        chain = td["chain"]
        spot  = td["spot"]
        rs    = td["rs"]
        fin   = td["fin"] or {}

        # ── Compute all signals ───────────────────────────────────────────────
        from screener.ticker_analysis import (pcr, max_pain, net_gex,
                                               top_unusual_flow, gex_by_strike)
        import time as _time

        last_updated = _time.strftime("%H:%M:%S")

        pcr_d   = pcr(chain)
        pain    = max_pain(chain)
        gex_df  = gex_by_strike(chain, spot)
        net_g   = net_gex(gex_df)
        flip    = gex_flip_level(chain, spot)
        flow    = top_unusual_flow(chain, top_n=8)

        # IV premium
        iv_vals = chain["iv"].dropna()
        iv_vals = iv_vals[(iv_vals >= 0.05) & (iv_vals <= 3.0)]
        avg_iv  = float(iv_vals.mean()) if not iv_vals.empty else None
        hv30    = fin.get("hv_30")
        iv_prem = round(avg_iv / hv30, 2) if avg_iv and hv30 and hv30 > 0 else None

        # Best strike for entry card
        bullish   = dirx == "Long Call"
        opt_type  = "call" if bullish else "put"
        side      = chain[(chain["type"] == opt_type) &
                          (chain["dte"] >= 7) & (chain["dte"] <= td_dte) &
                          (chain["mid"] > 0)]
        best_row  = None
        if not side.empty and "delta" in side.columns:
            side = side[side["iv"].between(0.05, 3.0) | side["iv"].isna()].copy()
            side["delta_diff"] = (side["delta"].abs() - 0.45).abs()
            best_row = side.nsmallest(1, "delta_diff").iloc[0] if not side.empty else None

        # Sweep check on top unusual contract
        sweep_found = False
        if not flow.empty and "option_symbol" in flow.columns:
            top_opt = flow["option_symbol"].dropna().head(1).tolist()
            if top_opt:
                from screener.polygon_client import get_option_trades, detect_sweeps
                td_trades = get_option_trades(top_opt[0], limit=200)
                sweep_found = len(detect_sweeps(td_trades)) > 0

        # Technical target from finviz
        tech_target = fin.get("technical_target") if fin else None

        # Score
        result = score_trade(
            direction=dirx, spot=spot, intraday=rs,
            pcr_data=pcr_d, pain=pain, net_gex=net_g,
            gex_flip=flip, flow=flow, iv_premium=iv_prem,
            sweep_found=sweep_found,
        )
        score_val = result["score"]

        # Entry card
        card = build_entry_card(
            symbol=sym, direction=dirx, spot=spot,
            strike=float(best_row["strike"]) if best_row is not None else None,
            premium=float(best_row["mid"])   if best_row is not None else None,
            delta=float(best_row["delta"])   if best_row is not None and pd.notna(best_row.get("delta")) else None,
            dte=int(best_row["dte"])         if best_row is not None else None,
            expiry=str(best_row["expiration"]) if best_row is not None else None,
            tech_target=tech_target,
            iv=float(best_row["iv"])         if best_row is not None and pd.notna(best_row.get("iv")) else None,
            score=score_val,
        )

        # ── Conviction banner ─────────────────────────────────────────────────
        st.divider()
        banner_fn = {"success": st.success, "warning": st.warning, "error": st.error}.get(result["color"], st.info)
        banner_fn(
            f"**{sym}** · {dirx} · Score **{score_val}/100** — **{result['grade']}**"
            f"   ·   Last updated {last_updated}"
        )

        # ── Score bar ─────────────────────────────────────────────────────────
        st.progress(score_val / 100)

        st.divider()

        # ── Entry card ────────────────────────────────────────────────────────
        st.markdown("#### 📋 Entry Card")
        if card["strike"]:
            r1c1, r1c2, r1c3, r1c4 = st.columns(4)
            r1c1.metric("Direction",       dirx)
            r1c2.metric("Strike",          f"${card['strike']:.2f}")
            r1c3.metric("Expiry",          f"{card['expiry']}")
            r1c4.metric("DTE",             f"{card['dte']} days")

            r2c1, r2c2, r2c3, r2c4 = st.columns(4)
            r2c1.metric("Entry Premium",   f"${card['entry_mid']:.2f}")
            r2c2.metric("Cost / Contract", f"${card['entry_cost']:.0f}" if card['entry_cost'] else "—")
            r2c3.metric("Delta",           f"{card['delta']:.2f}" if card['delta'] else "—")
            r2c4.metric("IV",              f"{card['iv']:.0%}" if card['iv'] else "—")

            r3c1, r3c2, r3c3, r3c4 = st.columns(4)
            r3c1.metric("Breakeven",       f"${card['breakeven']:.2f}" if card['breakeven'] else "—")
            r3c2.metric("% to Breakeven",  f"{card['pct_to_be']:+.1f}%" if card['pct_to_be'] is not None else "—")
            r3c3.metric("Stock Target",    f"${card['tech_target']:.2f}" if card['tech_target'] else "—")
            r3c4.metric("Target Premium",  f"${card['target_prem']:.2f}" if card['target_prem'] else "—")

            r4c1, r4c2, r4c3, r4c4 = st.columns(4)
            r4c1.metric("Stop Premium",    f"${card['stop_prem']:.2f}" if card['stop_prem'] else "—",
                        delta="−50% of entry", delta_color="off")
            r4c2.metric("R : R",           f"{card['rr']:.1f}×" if card['rr'] else "—",
                        delta="✓ good" if (card['rr'] or 0) >= 1.5 else "↓ low",
                        delta_color="normal" if (card['rr'] or 0) >= 1.5 else "inverse")
            r4c3.metric("Max Pain",        f"${pain:.2f}" if pain else "—")
            r4c4.metric("GEX Flip",        f"${flip:.2f}" if flip else "—")
        else:
            st.warning("No suitable strike found within DTE / delta filters.")

        st.divider()

        # ── Intraday snapshot ─────────────────────────────────────────────────
        st.markdown("#### 📊 Intraday Snapshot")
        bars = rs.get("bars", pd.DataFrame())
        if not bars.empty:
            chart_df = bars[["ts", "close", "session_vwap"]].copy()
            chart_df = chart_df.set_index("ts").tail(120)
            chart_df.columns = ["Price", "VWAP"]
            st.line_chart(chart_df, color=["#60a5fa", "#f59e0b"])

            id1, id2, id3, id4, id5 = st.columns(5)
            id1.metric("Spot",     f"${spot:.2f}")
            id2.metric("vs VWAP",  f"{rs['vs_vwap']:+.2f}%" if rs.get('vs_vwap') is not None else "—",
                       delta_color="normal" if (rs.get('vs_vwap') or 0) > 0 else "inverse")
            id3.metric("Day Chg",  f"{rs['symbol_chg']:+.2f}%" if rs.get('symbol_chg') is not None else "—")
            id4.metric("vs SPY",   f"{rs['rs_ratio']:+.2f}%" if rs.get('rs_ratio') is not None else "—",
                       delta_color="normal" if (rs.get('rs_ratio') or 0) > 0 else "inverse")
            id5.metric("Day High / Low", f"${rs.get('day_high','—')} / ${rs.get('day_low','—')}")
        else:
            id1, id2, id3 = st.columns(3)
            id1.metric("Spot",     f"${spot:.2f}")
            id2.metric("GEX Flip", f"${flip:.2f}" if flip else "—")
            id3.metric("Max Pain", f"${pain:.2f}" if pain else "—")

        st.divider()

        # ── Signal checklist ──────────────────────────────────────────────────
        st.markdown("#### 🧭 Signal Breakdown")
        for sig in result["signals"]:
            icon, label, msg, pts, pts_max = sig
            if pts_max > 0:
                bar_pct = int(pts / pts_max * 100)
                pts_str = f"**{pts}/{pts_max}**"
            else:
                bar_pct = 0
                pts_str = ""
            col_icon, col_label, col_msg, col_pts = st.columns([0.5, 1.5, 6, 1])
            col_icon.markdown(icon)
            col_label.markdown(f"**{label}**")
            col_msg.markdown(msg)
            if pts_str:
                col_pts.markdown(pts_str)

        st.divider()

        # ── Quick save to journal ─────────────────────────────────────────────
        st.markdown("#### 💾 Log This Trade")
        if card["strike"]:
            with st.form("td_journal_form"):
                j_status = st.radio("Status", ["Watching", "Entered"], horizontal=True)
                j_notes  = st.text_area("Notes", placeholder=f"Conviction {score_val}/100 — {result['grade']}", height=60)
                if st.form_submit_button("📓 Save to Journal", type="primary"):
                    eid = add_entry_raw(
                        symbol=sym,
                        strategy=dirx,
                        setup=fin.get("setup", "—"),
                        signal=fin.get("signal", "—"),
                        strike=card["strike"],
                        contract_type="call" if dirx == "Long Call" else "put",
                        expiry=card["expiry"],
                        dte=card["dte"],
                        entry_premium=card["entry_mid"],
                        delta=card["delta"],
                        iv=card["iv"],
                        stock_price=spot,
                        technical_target=card["tech_target"],
                        option_breakeven=card["breakeven"],
                        pct_to_breakeven=card["pct_to_be"],
                        composite_score=float(score_val),
                        status=j_status,
                        notes=j_notes or f"Conviction {score_val}/100 — {result['grade']}",
                    )
                    st.success(f"Saved **{sym}** to journal (#{eid}) as **{j_status}**.")

# ── Journal tab ───────────────────────────────────────────────────────────────
with tab_journal:
    st.markdown("### 📓 Trade Journal")
    st.caption("Track option plays — real positions (Entered) and paper trades (Watching).")

    j_filter_col, j_action_col = st.columns([2, 1])
    with j_filter_col:
        status_filter = st.radio("Show", ["All", "Watching", "Entered", "Closed"],
                                 horizontal=True)
    with j_action_col:
        refresh_prices = st.button("🔄 Refresh P&L", help="Re-price all open entries via Black-Scholes + live price")

    df_journal = get_entries(status_filter)

    if df_journal.empty:
        st.info("No journal entries yet. Run the screener, inspect a ticker, and click **📓 Save to Journal**.")
    else:
        # Auto-reprice open entries; cache for 60s to avoid hitting yfinance on every rerender
        import time as _jtime
        _cache_age = _jtime.time() - st.session_state.get("_reprice_ts", 0)
        if refresh_prices or _cache_age > 60:
            with st.spinner("Fetching live premiums…"):
                st.session_state["_repriced"]   = reprice_all_open()
                st.session_state["_reprice_ts"] = _jtime.time()
        repriced = st.session_state.get("_repriced", {})

        # ── Summary cards ─────────────────────────────────────────────────────
        open_df   = df_journal[df_journal["status"].isin(["Watching","Entered"])]
        closed_df = df_journal[df_journal["status"] == "Closed"]

        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Total plays",   len(df_journal))
        s2.metric("Open / Watching", len(open_df))
        s3.metric("Closed", len(closed_df))
        if not closed_df.empty and closed_df["realized_pnl_pct"].notna().any():
            avg_win = closed_df["realized_pnl_pct"].mean()
            s4.metric("Avg closed P&L", f"{avg_win:+.1f}%",
                      delta="profitable" if avg_win > 0 else "loss",
                      delta_color="normal" if avg_win > 0 else "inverse")

        st.divider()

        # ── Entries ───────────────────────────────────────────────────────────
        for _, row in df_journal.iterrows():
            entry_id   = int(row["id"])
            sym        = row["symbol"]
            status     = row["status"]
            strategy   = row["strategy"] or "—"
            setup      = row["setup"] or "—"
            strike     = row["strike"]
            expiry     = row["expiry"] or "—"
            entry_prem = row["entry_premium"]
            dte_entry  = row["dte_at_entry"]
            notes      = row["notes"] or ""

            rp = repriced.get(entry_id, {})
            cur_prem  = rp.get("current_premium")
            upnl      = rp.get("unrealized_pnl")
            upnl_pct  = rp.get("unrealized_pnl_pct")
            cur_stock = rp.get("current_stock")
            dte_left  = rp.get("dte_remaining")
            expired   = rp.get("expired", False)

            def _safe_float(v):
                if v is None: return None
                if isinstance(v, bytes):
                    try: return float(int.from_bytes(v, 'little'))
                    except: return None
                try: return float(v)
                except: return None
            strike     = _safe_float(strike)
            entry_prem = _safe_float(entry_prem)
            status_icon = {"Watching": "👁️", "Entered": "✅", "Closed": "🔒"}.get(status, "")

            # Build header — include live premium + P&L for open entries
            _base = (f"${strike:.0f} strike · exp {expiry}"
                     if strike is not None else f"exp {expiry}")
            if status in ("Watching", "Entered") and entry_prem:
                if cur_prem is not None and upnl_pct is not None:
                    _pnl_sign = "+" if upnl_pct >= 0 else ""
                    _header_price = f"entry ${entry_prem:.2f} → now ${cur_prem:.2f}  ({_pnl_sign}{upnl_pct:.1f}%)"
                else:
                    _header_price = f"entry ${entry_prem:.2f}"
            elif status == "Closed" and entry_prem:
                _header_price = f"entry ${entry_prem:.2f}"
            else:
                _header_price = ""
            header = f"{status_icon} **{sym}** · {strategy} · {_base} · {_header_price}"

            with st.expander(header, expanded=(status == "Entered")):
                # ── Live premium bar (Watching / Entered only) ────────────────
                if status in ("Watching", "Entered") and entry_prem:
                    lp1, lp2, lp3, lp4 = st.columns(4)
                    lp1.metric("Entry Premium",   f"${entry_prem:.2f}",
                               delta=f"${entry_prem*100:.0f}/contract", delta_color="off")
                    if cur_prem is not None:
                        lp2.metric("Current Premium", f"${cur_prem:.2f}",
                                   delta=f"${cur_prem*100:.0f}/contract", delta_color="off")
                        lp3.metric("Unrealized P&L",
                                   f"${upnl:+.2f}" if upnl is not None else "—",
                                   delta=f"{upnl_pct:+.1f}%" if upnl_pct is not None else None,
                                   delta_color="normal" if (upnl or 0) >= 0 else "inverse")
                        lp4.metric("Stock Now",
                                   f"${cur_stock:.2f}" if cur_stock else "—",
                                   delta=f"{dte_left}d left" if dte_left is not None else None,
                                   delta_color="off")
                    else:
                        lp2.metric("Current Premium", "—")
                        lp3.metric("Unrealized P&L",  "—")
                        lp4.metric("Stock Now",
                                   f"${cur_stock:.2f}" if cur_stock else "—")
                    if expired:
                        st.error("⚠️ Option has expired")
                    st.divider()
                d1, d2, d3 = st.columns(3)
                with d1:
                    st.markdown("**Entry details**")
                    st.write(f"**Setup:** {setup}")
                    st.write(f"**Score:** {row['composite_score']:+.2f}" if row['composite_score'] else "")
                    st.write(f"**Delta at entry:** {row['entry_delta']:.2f}" if row['entry_delta'] else "")
                    st.write(f"**IV at entry:** {row['entry_iv']:.1%}" if row['entry_iv'] else "")
                    st.write(f"**DTE at entry:** {dte_entry}")
                    st.write(f"**Stock price:** ${row['stock_price_entry']:.2f}" if row['stock_price_entry'] else "")
                    st.write(f"**Added:** {row['added_date']}")

                with d2:
                    st.markdown("**Targets**")
                    if row['option_breakeven']:
                        st.write(f"**Breakeven:** ${row['option_breakeven']:.2f} ({row['pct_to_breakeven']:+.1f}%)" if row['pct_to_breakeven'] else f"**Breakeven:** ${row['option_breakeven']:.2f}")
                    if row['technical_target']:
                        st.write(f"**Tech target:** ${row['technical_target']:.2f}")
                    if row['analyst_target']:
                        st.write(f"**Analyst target:** ${row['analyst_target']:.2f}")

                    if status in ("Watching", "Entered") and dte_left is not None:
                        st.write(f"**DTE left:** {dte_left}")

                    elif status == "Closed":
                        st.markdown("---")
                        st.markdown("**Closed**")
                        st.write(f"**Exit price:** ${row['exit_price']:.2f}" if row['exit_price'] else "")
                        st.write(f"**Exit date:** {row['exit_date']}" if row['exit_date'] else "")
                        if row['realized_pnl_pct'] is not None:
                            color = "green" if row['realized_pnl_pct'] >= 0 else "red"
                            st.markdown(
                                f"**Realized P&L:** "
                                f"<span style='color:{color}'>${row['realized_pnl']:+.2f} ({row['realized_pnl_pct']:+.1f}%)</span>",
                                unsafe_allow_html=True
                            )

                with d3:
                    st.markdown("**Actions**")
                    if notes:
                        st.markdown(f"*{notes}*")

                    if status in ("Watching", "Entered"):
                        with st.form(key=f"close_{entry_id}"):
                            exit_px = st.number_input("Exit premium ($)", min_value=0.0, step=0.05,
                                                      value=float(cur_prem) if cur_prem else 0.0,
                                                      key=f"exit_px_{entry_id}")
                            if st.form_submit_button("🔒 Close position"):
                                close_entry(entry_id, exit_px)
                                st.rerun()

                    note_key = f"note_{entry_id}"
                    new_note = st.text_area("Update notes", value=notes, key=note_key, height=80)
                    if st.button("Save notes", key=f"save_note_{entry_id}"):
                        update_notes(entry_id, new_note)
                        st.rerun()

                    if st.button("🗑️ Delete", key=f"del_{entry_id}"):
                        delete_entry(entry_id)
                        st.rerun()

        # ── Export ────────────────────────────────────────────────────────────
        st.divider()
        csv = get_entries("All").to_csv(index=False)
        st.download_button("⬇️ Export journal as CSV", data=csv,
                           file_name="options_journal.csv", mime="text/csv")
