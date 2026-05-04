"""
Single-ticker deep-dive analysis from the options chain.

All inputs come from an already-fetched chain DataFrame (no extra API calls).
Computes: PCR, max pain, call/put walls, GEX, IV term structure,
          IV skew, volume clusters, top unusual flow, confluence score.
"""

from __future__ import annotations
import math
import logging
from typing import Optional
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ── Put/Call Ratios ────────────────────────────────────────────────────────────

def pcr(chain: pd.DataFrame) -> dict:
    """Volume-based and OI-based put/call ratios."""
    calls = chain[chain["type"] == "call"]
    puts  = chain[chain["type"] == "put"]
    call_vol = calls["volume"].sum()
    put_vol  = puts["volume"].sum()
    call_oi  = calls["open_interest"].sum()
    put_oi   = puts["open_interest"].sum()
    return {
        "call_volume":   int(call_vol),
        "put_volume":    int(put_vol),
        "call_oi":       int(call_oi),
        "put_oi":        int(put_oi),
        "pcr_volume":    round(put_vol / call_vol, 3) if call_vol > 0 else None,
        "pcr_oi":        round(put_oi  / call_oi,  3) if call_oi  > 0 else None,
    }


# ── Max Pain ───────────────────────────────────────────────────────────────────

def max_pain(chain: pd.DataFrame) -> Optional[float]:
    """
    Strike where total dollar loss of all option buyers is minimized.
    Market makers have incentive to pin price near this level at expiry.
    """
    strikes = sorted(chain["strike"].dropna().unique())
    if not strikes:
        return None

    min_pain  = float("inf")
    pain_strike = strikes[0]

    calls = chain[chain["type"] == "call"][["strike", "open_interest"]].copy()
    puts  = chain[chain["type"] == "put"][["strike",  "open_interest"]].copy()

    for k in strikes:
        # ITM call loss: calls with strike < k expire worthless for buyers
        call_loss = ((k - calls["strike"]).clip(lower=0) * calls["open_interest"]).sum() * 100
        # ITM put loss: puts with strike > k expire worthless for buyers
        put_loss  = ((puts["strike"] - k).clip(lower=0)  * puts["open_interest"]).sum()  * 100
        total = call_loss + put_loss
        if total < min_pain:
            min_pain    = total
            pain_strike = k

    return float(pain_strike)


# ── Call / Put Walls ───────────────────────────────────────────────────────────

def oi_walls(chain: pd.DataFrame, top_n: int = 10) -> dict:
    """
    Strikes with highest open interest — these act as S/R levels.
    Call wall = overhead resistance. Put wall = downside support.
    Expiry shown is the single expiration with the most OI at that strike.
    """
    def _top_walls(side_df: pd.DataFrame) -> pd.DataFrame:
        # Aggregate OI/volume across all expirations per strike
        agg = (
            side_df.groupby("strike")[["open_interest", "volume"]]
            .sum()
            .reset_index()
            .nlargest(top_n, "open_interest")
        )
        # For each top strike, find the expiry with the highest OI
        dominant = (
            side_df.loc[side_df.groupby("strike")["open_interest"].idxmax(),
                        ["strike", "expiration"]]
        )
        result = agg.merge(dominant, on="strike", how="left")
        return result[["strike", "expiration", "open_interest", "volume"]].reset_index(drop=True)

    calls = chain[chain["type"] == "call"]
    puts  = chain[chain["type"] == "put"]
    return {
        "call_walls": _top_walls(calls),
        "put_walls":  _top_walls(puts),
    }


# ── Gamma Exposure (GEX) ───────────────────────────────────────────────────────

def gex_by_strike(chain: pd.DataFrame, spot: float) -> pd.DataFrame:
    """
    Dealer gamma exposure per strike.

    Convention: dealers are short options they sell to the market.
      GEX(call) = -gamma × OI × 100 × spot²   (dealers short calls = short gamma)
      GEX(put)  = +gamma × OI × 100 × spot²   (dealers short puts  = long gamma)

    Aggregate GEX > 0 → dealers long gamma → they sell rips / buy dips → price pins.
    Aggregate GEX < 0 → dealers short gamma → they buy rips / sell dips → vol expands.
    """
    df = chain.copy()
    df = df[df["gamma"].notna() & (df["open_interest"] > 0)]
    df["gex"] = df.apply(
        lambda r: (
            -r["gamma"] * r["open_interest"] * 100 * spot ** 2
            if r["type"] == "call"
            else r["gamma"] * r["open_interest"] * 100 * spot ** 2
        ),
        axis=1,
    )
    agg = df.groupby("strike")["gex"].sum().reset_index()
    agg["gex_m"] = agg["gex"] / 1_000_000   # display in $M
    return agg.sort_values("strike")


def net_gex(gex_df: pd.DataFrame) -> float:
    """Sum of all dealer GEX — positive = pinning, negative = vol expansion."""
    return float(gex_df["gex"].sum())


# ── IV Term Structure ──────────────────────────────────────────────────────────

def iv_term_structure(chain: pd.DataFrame) -> pd.DataFrame:
    """
    Average IV by DTE bucket. Backwardation (near > far IV) signals fear/event risk.
    """
    df = chain[chain["iv"].between(0.05, 3.0)].copy()
    bins   = [0, 7, 14, 21, 30, 45, 60, 90, 999]
    labels = ["0-7", "7-14", "14-21", "21-30", "30-45", "45-60", "60-90", "90+"]
    df["dte_bucket"] = pd.cut(df["dte"], bins=bins, labels=labels, right=False)
    term = (
        df.groupby("dte_bucket", observed=True)["iv"]
        .agg(avg_iv="mean", count="count")
        .reset_index()
    )
    term["avg_iv"] = term["avg_iv"].round(4)
    return term[term["count"] > 0]


# ── IV Skew ────────────────────────────────────────────────────────────────────

def iv_skew(chain: pd.DataFrame, dte_min: int = 21, dte_max: int = 45) -> pd.DataFrame:
    """
    Per-expiry: ATM call IV vs ATM put IV.
    Negative skew (put IV > call IV) = downside protection demand (normal).
    Unusually positive skew (call IV > put IV) = strong bullish call buying.
    """
    window = chain[chain["dte"].between(dte_min, dte_max) & chain["iv"].between(0.05, 3.0)]
    rows = []
    for exp, grp in window.groupby("expiration"):
        c = grp[grp["type"] == "call"].nlargest(1, "open_interest")
        p = grp[grp["type"] == "put"].nlargest(1, "open_interest")
        if not c.empty and not p.empty:
            call_iv = float(c["iv"].iloc[0])
            put_iv  = float(p["iv"].iloc[0])
            dte_val = int(grp["dte"].iloc[0])
            rows.append({
                "expiration": exp,
                "dte":        dte_val,
                "call_iv":    round(call_iv, 4),
                "put_iv":     round(put_iv, 4),
                "skew":       round(call_iv - put_iv, 4),  # positive = call IV > put IV
            })
    return pd.DataFrame(rows).sort_values("dte") if rows else pd.DataFrame()


# ── Volume Clusters ────────────────────────────────────────────────────────────

def volume_clusters(chain: pd.DataFrame, top_n: int = 15) -> pd.DataFrame:
    """
    Top contracts by today's volume. High volume = informed money moving.
    """
    df = chain[chain["volume"] > 0].copy()
    df = df.nlargest(top_n, "volume")[
        ["type", "strike", "expiration", "dte", "volume", "open_interest",
         "mid", "delta", "iv"]
    ].reset_index(drop=True)
    df["notional"] = (df["volume"] * df["mid"] * 100).round(0).astype(int)
    return df


# ── Top Unusual Flow ───────────────────────────────────────────────────────────

def top_unusual_flow(chain: pd.DataFrame, top_n: int = 8) -> pd.DataFrame:
    """
    Top contracts by vol/OI ratio × log10(notional).
    Filters: volume ≥ 100, OI ≥ 50, notional ≥ $25K.
    """
    df = chain[(chain["volume"] >= 100) & (chain["open_interest"] >= 50) & (chain["mid"] > 0)].copy()
    if df.empty:
        return pd.DataFrame()
    df["notional"]    = df["volume"] * df["mid"] * 100
    df = df[df["notional"] >= 25_000]
    if df.empty:
        return pd.DataFrame()
    df["vol_oi_ratio"] = (df["volume"] / df["open_interest"]).round(2)
    df["ua_score"]     = df["vol_oi_ratio"] * df["notional"].apply(lambda x: math.log10(max(x, 1)))
    keep_cols = [c for c in
                 ["option_symbol", "type", "strike", "expiration", "dte", "volume",
                  "open_interest", "vol_oi_ratio", "notional", "mid", "delta", "iv"]
                 if c in df.columns]
    top = df.nlargest(top_n, "ua_score")[keep_cols].reset_index(drop=True)
    return top


# ── High IV Contracts ──────────────────────────────────────────────────────────

def high_iv_contracts(chain: pd.DataFrame, top_n: int = 10, dte_min: int = 7) -> pd.DataFrame:
    """Contracts with highest IV — highest implied move expectation."""
    df = chain[
        (chain["iv"].between(0.05, 3.0)) &
        (chain["dte"] >= dte_min) &
        (chain["open_interest"] >= 50) &
        (chain["mid"] > 0)
    ].nlargest(top_n, "iv")[
        ["type", "strike", "expiration", "dte", "iv", "volume", "open_interest", "mid", "delta"]
    ].reset_index(drop=True)
    return df


# ── Confluence Score ───────────────────────────────────────────────────────────

def confluence_score(
    spot:       float,
    pcr_data:   dict,
    pain:       Optional[float],
    net_gex_v:  float,
    walls:      dict,
    flow:       pd.DataFrame,
    term:       pd.DataFrame,
) -> dict:
    """
    Aggregate all signals into a directional lean with individual evidence items.
    Returns: score (-10 to +10), lean, signals list.
    """
    signals = []
    score   = 0

    # ── PCR Volume ────────────────────────────────────────────────────────────
    pv = pcr_data.get("pcr_volume")
    if pv is not None:
        if pv < 0.60:
            signals.append(("🟢", f"PCR(vol) {pv:.2f} — strong call dominance, bullish flow"))
            score += 2
        elif pv < 0.80:
            signals.append(("🟢", f"PCR(vol) {pv:.2f} — calls outpacing puts, mildly bullish"))
            score += 1
        elif pv > 1.30:
            signals.append(("🔴", f"PCR(vol) {pv:.2f} — heavy put buying, bearish flow"))
            score -= 2
        elif pv > 1.00:
            signals.append(("🔴", f"PCR(vol) {pv:.2f} — puts leading calls, mildly bearish"))
            score -= 1
        else:
            signals.append(("⚪", f"PCR(vol) {pv:.2f} — neutral"))

    # ── Max Pain ──────────────────────────────────────────────────────────────
    if pain and spot > 0:
        diff_pct = (spot - pain) / pain * 100
        if diff_pct > 3:
            signals.append(("🟡", f"Spot ${spot:.2f} is {diff_pct:+.1f}% above max pain ${pain:.2f} — gravity pulls price down"))
            score -= 1
        elif diff_pct < -3:
            signals.append(("🟢", f"Spot ${spot:.2f} is {diff_pct:+.1f}% below max pain ${pain:.2f} — gravity pulls price up"))
            score += 1
        else:
            signals.append(("⚪", f"Spot near max pain ${pain:.2f} ({diff_pct:+.1f}%) — neutral"))

    # ── GEX ───────────────────────────────────────────────────────────────────
    gex_bn = net_gex_v / 1_000_000_000
    if abs(gex_bn) > 0.1:
        if gex_bn > 0:
            signals.append(("🟡", f"GEX +${gex_bn:.1f}B — dealers long gamma, price likely to pin near key strikes"))
        else:
            signals.append(("🟠", f"GEX -${abs(gex_bn):.1f}B — dealers short gamma, moves will be amplified (vol expansion risk)"))

    # ── Call Walls (overhead resistance / target) ─────────────────────────────
    if not walls["call_walls"].empty:
        top_cw = walls["call_walls"].iloc[0]
        if top_cw["strike"] > spot:
            signals.append(("🟡", f"Call wall at ${top_cw['strike']:.0f} ({int(top_cw['open_interest']):,} OI) — resistance / upside target"))
        else:
            signals.append(("🟢", f"Call wall at ${top_cw['strike']:.0f} is below spot — calls are support, bullish"))
            score += 1

    # ── Put Walls (downside support) ──────────────────────────────────────────
    if not walls["put_walls"].empty:
        top_pw = walls["put_walls"].iloc[0]
        if top_pw["strike"] < spot:
            signals.append(("🟢", f"Put wall at ${top_pw['strike']:.0f} ({int(top_pw['open_interest']):,} OI) — strong downside support floor"))
            score += 1
        else:
            signals.append(("🔴", f"Put wall at ${top_pw['strike']:.0f} is above spot — puts loaded above price, bearish pressure"))
            score -= 1

    # ── Unusual Flow Bias ─────────────────────────────────────────────────────
    if not flow.empty:
        call_flow = (flow["type"] == "call").sum()
        put_flow  = (flow["type"] == "put").sum()
        total_flow = len(flow)
        if call_flow > put_flow * 1.5:
            signals.append(("🟢", f"Unusual flow: {call_flow}/{total_flow} top contracts are calls — institutional call buying"))
            score += 2
        elif put_flow > call_flow * 1.5:
            signals.append(("🔴", f"Unusual flow: {put_flow}/{total_flow} top contracts are puts — institutional put buying"))
            score -= 2
        else:
            signals.append(("⚪", f"Unusual flow: mixed ({call_flow} calls / {put_flow} puts)"))

    # ── IV Term Structure ─────────────────────────────────────────────────────
    if len(term) >= 2:
        near_iv = term.iloc[0]["avg_iv"]
        far_iv  = term.iloc[-1]["avg_iv"]
        if near_iv > far_iv * 1.15:
            signals.append(("🟠", f"IV backwardation: near-term IV {near_iv:.0%} > long-term {far_iv:.0%} — event/fear premium near-term"))
        elif far_iv > near_iv * 1.10:
            signals.append(("🟢", f"IV contango: near-term IV {near_iv:.0%} < long-term {far_iv:.0%} — normal, no near-term event"))
            score += 1

    # ── Final lean ────────────────────────────────────────────────────────────
    if score >= 3:      lean = "🟢🟢 Strong Bullish"
    elif score >= 1:    lean = "🟢 Mildly Bullish"
    elif score <= -3:   lean = "🔴🔴 Strong Bearish"
    elif score <= -1:   lean = "🔴 Mildly Bearish"
    else:               lean = "⚪ Neutral / Mixed"

    return {"score": score, "lean": lean, "signals": signals}


# ── Master entry point ─────────────────────────────────────────────────────────

def compute_full_options_analysis(chain: pd.DataFrame, spot: float) -> dict:
    """
    Run all analyses and return a single dict of results.
    Caller passes the already-fetched chain and live spot price.
    """
    if chain is None or chain.empty or spot <= 0:
        return {}

    pcr_data   = pcr(chain)
    pain       = max_pain(chain)
    walls      = oi_walls(chain)
    gex_df     = gex_by_strike(chain, spot)
    net_gex_v  = net_gex(gex_df)
    term       = iv_term_structure(chain)
    skew       = iv_skew(chain)
    clusters   = volume_clusters(chain)
    flow       = top_unusual_flow(chain)
    hi_iv      = high_iv_contracts(chain)
    conf       = confluence_score(spot, pcr_data, pain, net_gex_v, walls, flow, term)

    return {
        "pcr":        pcr_data,
        "max_pain":   pain,
        "walls":      walls,
        "gex_df":     gex_df,
        "net_gex":    net_gex_v,
        "term":       term,
        "skew":       skew,
        "clusters":   clusters,
        "flow":       flow,
        "high_iv":    hi_iv,
        "confluence": conf,
    }
