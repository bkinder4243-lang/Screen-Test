"""
Options Swing Trade Screener — Long Calls & Puts Only
Signals: Finviz technicals · Reddit sentiment · Polygon unusual activity
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import streamlit as st
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from screener.polygon_client    import get_options_chain, get_unusual_activity, key_is_working
from screener.finviz_client     import get_technicals
from screener.stocktwits_client import get_sentiment
from screener.scorer            import build_result, ScreenerResult
from screener.universe          import load_universe, UNIVERSES
from screener.etf_universe      import ETF_UNIVERSE, get_etf_holdings, etf_category
from screener.journal           import add_entry, get_entries, close_entry, delete_entry, reprice_all_open, update_notes
from screener.ticker_analysis   import full_analysis
from screener.polygon_client    import get_spot_price

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

poly_ok = key_is_working()
st.sidebar.markdown(
    f"{'🟢' if poly_ok else '🔴'} Polygon.io (options + greeks)  \n"
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
tab_scanner, tab_deep, tab_journal = st.tabs(["📊 Scanner", "🔍 Ticker Deep Dive", "📓 Journal"])


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


# ── Scanner tab ───────────────────────────────────────────────────────────────
with tab_scanner:
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
            st.stop()

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
        st.stop()

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
        st.stop()

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
            m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
            pv = pcr_d.get("pcr_volume")
            po = pcr_d.get("pcr_oi")
            gex_bn = net_g / 1_000_000_000

            def _pcr_delta(v):
                if v is None: return None
                if v < 0.7:   return "bullish"
                if v > 1.0:   return "bearish"
                return "neutral"

            m1.metric("Spot",          f"${spot:.2f}")
            m2.metric("Max Pain",      f"${pain:.2f}" if pain else "—",
                      delta=f"{(spot-pain)/pain*100:+.1f}%" if pain else None)
            m3.metric("PCR (volume)",  f"{pv:.2f}" if pv else "—",
                      delta=_pcr_delta(pv), delta_color="inverse")
            m4.metric("PCR (OI)",      f"{po:.2f}" if po else "—")
            m5.metric("Call Volume",   f"{pcr_d['call_volume']:,}")
            m6.metric("Put Volume",    f"{pcr_d['put_volume']:,}")
            m7.metric("Net GEX",       f"${gex_bn:+.2f}B",
                      delta="Pinning" if gex_bn > 0 else "Vol expansion",
                      delta_color="normal" if gex_bn > 0 else "inverse")

            st.divider()

            # ── Row 2: Walls + Unusual Flow ────────────────────────────────────
            w1, w2, w3 = st.columns(3)

            with w1:
                st.markdown("**📈 Call Walls (resistance / upside targets)**")
                cw = walls["call_walls"].copy()
                cw.columns = ["Strike", "Open Interest", "Volume"]
                cw["Strike"] = cw["Strike"].apply(lambda x: f"${x:.2f}")
                cw["Open Interest"] = cw["Open Interest"].apply(lambda x: f"{int(x):,}")
                cw["Volume"] = cw["Volume"].apply(lambda x: f"{int(x):,}")
                st.dataframe(cw, hide_index=True, use_container_width=True)

            with w2:
                st.markdown("**📉 Put Walls (support / downside levels)**")
                pw = walls["put_walls"].copy()
                pw.columns = ["Strike", "Open Interest", "Volume"]
                pw["Strike"] = pw["Strike"].apply(lambda x: f"${x:.2f}")
                pw["Open Interest"] = pw["Open Interest"].apply(lambda x: f"{int(x):,}")
                pw["Volume"] = pw["Volume"].apply(lambda x: f"{int(x):,}")
                st.dataframe(pw, hide_index=True, use_container_width=True)

            with w3:
                st.markdown("**🔥 Top Unusual Flow (vol/OI × notional)**")
                if not flow.empty:
                    fl = flow.copy()
                    fl["iv"]         = fl["iv"].apply(lambda x: f"{x:.0%}" if pd.notna(x) else "—")
                    fl["delta"]      = fl["delta"].apply(lambda x: f"{x:.2f}" if pd.notna(x) else "—")
                    fl["mid"]        = fl["mid"].apply(lambda x: f"${x:.2f}")
                    fl["notional"]   = fl["notional"].apply(lambda x: f"${int(x):,}")
                    fl["vol_oi_ratio"] = fl["vol_oi_ratio"].apply(lambda x: f"{x:.1f}x")
                    fl["strike"]     = fl["strike"].apply(lambda x: f"${x:.2f}")
                    fl["type"]       = fl["type"].str.upper()
                    st.dataframe(
                        fl[["type","strike","expiration","dte","volume","vol_oi_ratio","notional","mid","delta","iv"]],
                        hide_index=True, use_container_width=True,
                        column_config={"type": st.column_config.TextColumn("Type", width="small")}
                    )
                else:
                    st.info("No unusual flow found (need vol ≥ 100, OI ≥ 50, notional ≥ $25K).")

            st.divider()

            # ── Row 3: Volume clusters + High IV ──────────────────────────────
            vc1, vc2 = st.columns(2)

            with vc1:
                st.markdown("**⚡ Top Volume Contracts (most active today)**")
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
                st.markdown("**🌡️ Highest IV Contracts (most expensive premium)**")
                if not hi_iv.empty:
                    hi = hi_iv.copy()
                    hi["iv"]    = hi["iv"].apply(lambda x: f"{x:.0%}")
                    hi["delta"] = hi["delta"].apply(lambda x: f"{x:.2f}" if pd.notna(x) else "—")
                    hi["mid"]   = hi["mid"].apply(lambda x: f"${x:.2f}")
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
                    # Backwardation warning
                    ivs = ana["term"]["avg_iv"].values
                    if len(ivs) >= 2 and ivs[0] > ivs[-1] * 1.15:
                        st.warning("⚠️ IV backwardation — near-term vol elevated. Likely event/earnings premium.")
                else:
                    st.info("Not enough data for term structure.")

            with ts2:
                st.markdown("**📐 IV Skew by Expiry** (call IV − put IV per expiry)")
                if not skew.empty:
                    sk = skew.copy()
                    sk["call_iv"] = sk["call_iv"].apply(lambda x: f"{x:.1%}")
                    sk["put_iv"]  = sk["put_iv"].apply(lambda x: f"{x:.1%}")
                    sk["skew"]    = sk["skew"].apply(
                        lambda x: f"+{x:.1%} 📈" if x > 0.02 else (f"{x:.1%} 📉" if x < -0.02 else f"{x:.1%}")
                    )
                    st.dataframe(sk, hide_index=True, use_container_width=True)
                    st.caption("Positive skew = call IV > put IV (bullish demand). Negative = normal protective buying.")
                else:
                    st.info("Not enough data for skew.")

            st.divider()

            # ── GEX by Strike chart ────────────────────────────────────────────
            with st.expander("📊 Gamma Exposure (GEX) by Strike", expanded=False):
                if not gex_df.empty:
                    st.caption(
                        "Positive GEX = dealers long gamma → price-pinning effect near that strike. "
                        "Negative GEX = dealers short gamma → moves accelerate through that level."
                    )
                    gex_disp = gex_df.copy()
                    gex_disp["gex_m"] = gex_disp["gex_m"].round(1)
                    gex_disp["strike"] = gex_disp["strike"].apply(lambda x: f"${x:.2f}")
                    gex_disp.columns = ["Strike", "GEX ($M)", "GEX_M"]
                    st.bar_chart(gex_disp.set_index("Strike")["GEX ($M)"])
                else:
                    st.info("No gamma data — greeks not available from Polygon Starter plan on this ticker.")

            # ── Confluence signals detail ──────────────────────────────────────
            with st.expander("🧭 Confluence Signals Detail", expanded=True):
                for icon, msg in conf["signals"]:
                    st.markdown(f"{icon} {msg}")

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
        # Reprice if requested
        repriced = {}
        if refresh_prices:
            with st.spinner("Fetching live prices…"):
                repriced = reprice_all_open()

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

            status_icon = {"Watching": "👁️", "Entered": "✅", "Closed": "🔒"}.get(status, "")
            header = (f"{status_icon} **{sym}** · {strategy} · "
                      f"${strike:.0f} strike · exp {expiry} · "
                      f"entry ${entry_prem:.2f}" if entry_prem else
                      f"{status_icon} **{sym}** · {strategy} · exp {expiry}")

            with st.expander(header, expanded=(status == "Entered")):
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

                    if status in ("Watching", "Entered") and cur_stock:
                        st.markdown("---")
                        st.markdown("**Current (live)**")
                        st.write(f"**Stock now:** ${cur_stock:.2f}")
                        if dte_left is not None:
                            st.write(f"**DTE left:** {dte_left}")
                        if expired:
                            st.warning("Option has expired")
                        elif cur_prem is not None:
                            st.write(f"**Est. premium:** ${cur_prem:.2f}")
                            color = "green" if (upnl or 0) >= 0 else "red"
                            st.markdown(
                                f"**Unrealized P&L:** "
                                f"<span style='color:{color}'>${upnl:+.2f} ({upnl_pct:+.1f}%)</span>",
                                unsafe_allow_html=True
                            )
                        else:
                            st.info("Click 🔄 Refresh P&L to see live value")

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
