"""
Trade conviction scorer for intraday options entries.

Aggregates 7 independent signals into a 0-100 score:
  1. VWAP alignment          (20 pts)
  2. Relative strength       (15 pts)
  3. Options flow bias       (15 pts)
  4. GEX structure           (15 pts)
  5. IV regime               (10 pts)
  6. Sweep confirmation      (15 pts)
  7. Max pain momentum       (10 pts)

Score thresholds:
  80-100 → HIGH CONVICTION
  60-79  → MODERATE
  40-59  → MARGINAL — wait
  0-39   → PASS
"""

from __future__ import annotations
from typing import Optional
import pandas as pd


def score_intraday_entry_signals(
    direction: str,
    spot: float,
    intraday: dict,
    pcr_data: dict,
    pain: Optional[float],
    net_gex: float,
    gex_flip: Optional[float],
    flow: pd.DataFrame,
    iv_premium: Optional[float],
    sweep_found: bool = False,
) -> dict:
    bullish = direction == "Long Call"
    score   = 0
    signals = []   # (icon, label, pts_earned, pts_max)

    # ── 1. VWAP alignment (20 pts) ───────────────────────────────────────────
    vd = intraday.get("vs_vwap")
    if vd is not None:
        if bullish:
            if vd > 0.20:
                pts, icon, msg = 20, "🟢", f"Price {vd:+.2f}% above VWAP — clean bullish structure"
            elif vd > -0.20:
                pts, icon, msg = 10, "🟡", f"Price hugging VWAP ({vd:+.2f}%) — watch for sustained reclaim"
            else:
                pts, icon, msg = 0, "🔴", f"Price {vd:+.2f}% below VWAP — fights call thesis"
        else:
            if vd < -0.20:
                pts, icon, msg = 20, "🟢", f"Price {vd:+.2f}% below VWAP — clean bearish structure"
            elif vd < 0.20:
                pts, icon, msg = 10, "🟡", f"Price hugging VWAP ({vd:+.2f}%) — watch for breakdown"
            else:
                pts, icon, msg = 0, "🔴", f"Price {vd:+.2f}% above VWAP — fights put thesis"
        score += pts
        signals.append((icon, "VWAP", msg, pts, 20))
    else:
        signals.append(("⚪", "VWAP", "Intraday bars unavailable — needs Polygon plan upgrade", 0, 20))

    # ── 2. Relative strength vs SPY (15 pts) ────────────────────────────────
    rs = intraday.get("rs_ratio")
    if rs is not None:
        if bullish:
            if rs > 0.5:
                pts, icon, msg = 15, "🟢", f"Leading SPY by {rs:+.2f}% — strong RS confirms long"
            elif rs > -0.3:
                pts, icon, msg = 8, "🟡", f"In-line with SPY ({rs:+.2f}%) — neutral RS"
            else:
                pts, icon, msg = 0, "🔴", f"Lagging SPY by {abs(rs):.2f}% — weak RS, avoid calls"
        else:
            if rs < -0.5:
                pts, icon, msg = 15, "🟢", f"Lagging SPY by {abs(rs):.2f}% — relative weakness confirms put"
            elif rs < 0.3:
                pts, icon, msg = 8, "🟡", f"In-line with SPY ({rs:+.2f}%) — neutral RS"
            else:
                pts, icon, msg = 0, "🔴", f"Leading SPY by {rs:.2f}% — strong RS, avoid puts"
        score += pts
        signals.append((icon, "Rel Strength", msg, pts, 15))
    else:
        signals.append(("⚪", "Rel Strength", "RS data unavailable", 0, 15))

    # ── 3. Options flow bias / PCR (15 pts) ──────────────────────────────────
    pv = pcr_data.get("pcr_volume")
    if pv is not None:
        if bullish:
            if pv < 0.70:
                pts, icon, msg = 15, "🟢", f"PCR(vol) {pv:.2f} — call-dominant flow confirms bull"
            elif pv < 1.0:
                pts, icon, msg = 8, "🟡", f"PCR(vol) {pv:.2f} — mixed flow, mild call lean"
            else:
                pts, icon, msg = 0, "🔴", f"PCR(vol) {pv:.2f} — put-heavy flow contradicts call"
        else:
            if pv > 1.20:
                pts, icon, msg = 15, "🟢", f"PCR(vol) {pv:.2f} — put-dominant flow confirms bear"
            elif pv > 0.90:
                pts, icon, msg = 8, "🟡", f"PCR(vol) {pv:.2f} — mixed flow, mild put lean"
            else:
                pts, icon, msg = 0, "🔴", f"PCR(vol) {pv:.2f} — call-heavy flow contradicts put"
        score += pts
        signals.append((icon, "Options Flow", msg, pts, 15))

        # Unusual flow direction (informational, no additional pts)
        if not flow.empty and "type" in flow.columns:
            call_n = (flow["type"] == "call").sum()
            put_n  = (flow["type"] == "put").sum()
            flow_ok = (bullish and call_n >= put_n) or (not bullish and put_n > call_n)
            signals.append((
                "🟢" if flow_ok else "⚠️",
                "Unusual Flow",
                f"Unusual flow: {call_n} call vs {put_n} put contracts — {'confirms' if flow_ok else 'contradicts'} direction",
                0, 0,
            ))
    else:
        signals.append(("⚪", "Options Flow", "PCR data unavailable", 0, 15))

    # ── 4. GEX structure (15 pts) ────────────────────────────────────────────
    if gex_flip is not None:
        gex_bn = net_gex / 1_000_000_000
        if bullish:
            if spot > gex_flip:
                pts, icon, msg = 15, "🟢", f"Above GEX flip ${gex_flip:.2f} — dealers long gamma, supportive for calls"
            else:
                pts, icon, msg = 5, "🟡", f"Below GEX flip ${gex_flip:.2f} — negative gamma zone, moves amplified"
        else:
            if spot < gex_flip:
                pts, icon, msg = 15, "🟢", f"Below GEX flip ${gex_flip:.2f} — negative gamma, downside acceleration"
            else:
                pts, icon, msg = 5, "🟡", f"Above GEX flip ${gex_flip:.2f} — positive gamma may pin price"
        score += pts
        signals.append((icon, "GEX Structure", msg, pts, 15))
    elif net_gex != 0:
        gex_bn = net_gex / 1_000_000_000
        if (bullish and net_gex < 0) or (not bullish and net_gex < 0):
            pts, icon, msg = 10, "🟢", f"Net GEX ${gex_bn:+.2f}B — negative gamma, moves will amplify"
        else:
            pts, icon, msg = 5, "🟡", f"Net GEX ${gex_bn:+.2f}B — positive gamma, price-pinning environment"
        score += pts
        signals.append((icon, "GEX Structure", msg, pts, 15))
    else:
        signals.append(("⚪", "GEX Structure", "No gamma data available", 0, 15))

    # ── 5. IV regime (10 pts) ────────────────────────────────────────────────
    if iv_premium is not None:
        if iv_premium < 0.80:
            pts, icon, msg = 10, "🟢", f"IV Premium {iv_premium:.2f}× — cheap vol, buy premium aggressively"
        elif iv_premium < 1.20:
            pts, icon, msg = 6, "🟡", f"IV Premium {iv_premium:.2f}× — fair vol, acceptable entry"
        else:
            pts, icon, msg = 0, "🔴", f"IV Premium {iv_premium:.2f}× — rich vol, buying expensive premium. Size down."
        score += pts
        signals.append((icon, "IV Regime", msg, pts, 10))
    else:
        signals.append(("⚪", "IV Regime", "IV Premium unavailable (need HV30)", 0, 10))

    # ── 6. Sweep confirmation (15 pts) ───────────────────────────────────────
    if sweep_found:
        pts, icon, msg = 15, "🟢", "Active sweep detected on this ticker — institutional aggression present"
    else:
        pts, icon, msg = 5, "🟡", "No sweep detected — retail flow only, wait for institutional confirmation"
    score += pts
    signals.append((icon, "Sweep", msg, pts, 15))

    # ── 7. Max pain gravity (10 pts) ─────────────────────────────────────────
    if pain and spot:
        diff_pct = (spot - pain) / pain * 100
        if bullish:
            if spot > pain and diff_pct < 6:
                pts, icon, msg = 10, "🟢", f"Moving away from max pain ${pain:.2f} upward — momentum aligned"
            elif spot < pain:
                pts, icon, msg = 7, "🟢", f"Below max pain ${pain:.2f} — gravity pulls price up, supports calls"
            else:
                pts, icon, msg = 3, "🟡", f"Price far above max pain ${pain:.2f} ({diff_pct:+.1f}%) — less gravity support"
        else:
            if spot < pain and abs(diff_pct) < 6:
                pts, icon, msg = 10, "🟢", f"Moving away from max pain ${pain:.2f} downward — momentum aligned"
            elif spot > pain:
                pts, icon, msg = 7, "🟢", f"Above max pain ${pain:.2f} — gravity pulls price down, supports puts"
            else:
                pts, icon, msg = 3, "🟡", f"Price far below max pain ${pain:.2f} ({diff_pct:+.1f}%) — less gravity support"
        score += pts
        signals.append((icon, "Max Pain", msg, pts, 10))
    else:
        signals.append(("⚪", "Max Pain", "Max pain unavailable", 0, 10))

    # ── Grade ────────────────────────────────────────────────────────────────
    if score >= 80:
        grade, color = "HIGH CONVICTION", "success"
    elif score >= 60:
        grade, color = "MODERATE", "success"
    elif score >= 40:
        grade, color = "MARGINAL — wait", "warning"
    else:
        grade, color = "PASS", "error"

    return {"score": score, "grade": grade, "color": color, "signals": signals}


def format_entry_card_metrics(
    symbol: str,
    direction: str,
    spot: float,
    strike: Optional[float],
    premium: Optional[float],
    delta: Optional[float],
    dte: Optional[int],
    expiry: Optional[str],
    tech_target: Optional[float],
    iv: Optional[float],
    score: int,
) -> dict:
    """
    Compute all entry parameters for a specific option contract.
    Target premium uses intrinsic value at tech_target + residual time value.
    Stop is set at -50% of premium (standard long option stop).
    """
    bullish = direction == "Long Call"

    breakeven = pct_to_be = None
    if premium is not None and strike is not None:
        breakeven = round(strike + premium if bullish else strike - premium, 2)
        pct_to_be = round((breakeven - spot) / spot * 100, 2)

    max_loss_per_contract = round(premium * 100, 2) if premium else None
    stop_premium          = round(premium * 0.50, 2) if premium else None

    target_prem = rr = None
    if tech_target and premium and strike:
        intrinsic = max(0.0, tech_target - strike) if bullish else max(0.0, strike - tech_target)
        # Keep 20% residual time value if the move happens early in the DTE window
        target_prem = round(intrinsic + premium * 0.20, 2)
        if target_prem > premium > 0:
            rr = round((target_prem - premium) / premium, 2)

    return {
        "symbol":      symbol,
        "direction":   direction,
        "strike":      strike,
        "expiry":      expiry,
        "dte":         dte,
        "entry_mid":   premium,
        "entry_cost":  max_loss_per_contract,
        "delta":       delta,
        "iv":          iv,
        "breakeven":   breakeven,
        "pct_to_be":   pct_to_be,
        "tech_target": tech_target,
        "target_prem": target_prem,
        "stop_prem":   stop_premium,
        "rr":          rr,
        "score":       score,
    }
