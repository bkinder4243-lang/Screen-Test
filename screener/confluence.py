"""
Confluence scorer — Combines institutional flow, intraday momentum, and gamma structure.

Signals (weights):
    institutional_flow (40%)  — unusual activity score
    intraday_momentum  (40%)  — VWAP alignment + relative strength vs SPY
    gamma_structure    (20%)  — GEX flip + IV regime

Score range: 0–100. Grade: HIGH (80+), MODERATE (60–79), MARGINAL (40–59), PASS (<40).
"""

from dataclasses import dataclass
from typing import Optional
import logging

logger = logging.getLogger(__name__)


@dataclass
class ConfluenceResult:
    """Confluence score breakdown."""
    confluence_score: float        # 0–100, composite
    flow_signal: str              # "unusual bullish", "unusual bearish", "none"
    vwap_alignment: float         # 0–20 pts
    rs_bias: float                # 0–20 pts
    gex_favorable: bool           # True if price above GEX flip (bullish) or below (bearish)
    iv_regime: str                # "cheap", "fair", "rich"
    conviction_grade: str         # "HIGH", "MODERATE", "MARGINAL", "PASS"


def compute_confluence(screener_result, intraday_data=None, chain_data=None) -> ConfluenceResult:
    """
    Compute confluence score combining flow + intraday + gamma signals.

    Args:
        screener_result: ScreenerResult from scanner
        intraday_data: dict from get_relative_strength() with vs_vwap, rs_ratio, current, etc.
        chain_data: dict with 'gex_flip_level' and 'net_gex' from ticker_analysis

    Returns:
        ConfluenceResult with confluence_score (0–100) and signal breakdown
    """

    # ===== SIGNAL 1: Institutional Flow (40%) =====
    # unusual activity = 40 pts if detected, 0 otherwise
    institutional_flow_points = 0.0
    flow_signal = "none"

    if screener_result.unusual:
        if screener_result.unusual_type == "call":
            flow_signal = "unusual bullish"
        elif screener_result.unusual_type == "put":
            flow_signal = "unusual bearish"
        else:
            flow_signal = "unusual bullish"  # default to bullish
        institutional_flow_points = 40.0

    # ===== SIGNAL 2: Intraday Momentum (40%) =====
    vwap_alignment_points = 0.0
    relative_strength_vs_spy_points = 0.0

    if intraday_data:
        # VWAP alignment: 0–20 pts
        vs_vwap = intraday_data.get("vs_vwap", 0.0)  # % distance from session VWAP
        if vs_vwap > 0.20:      # bullish, price well above VWAP
            vwap_alignment_points = 20.0
        elif vs_vwap > -0.20:   # near VWAP, neutral
            vwap_alignment_points = 10.0
        else:                   # bearish, price below VWAP
            vwap_alignment_points = 0.0

        # Relative strength vs SPY: 0–20 pts
        rs_ratio = intraday_data.get("rs_ratio", 0.0)  # symbol_chg% - SPY_chg%
        if rs_ratio > 0.5:      # leading SPY by >0.5%
            relative_strength_vs_spy_points = 20.0
        elif rs_ratio > -0.3:   # within ±0.3% of SPY
            relative_strength_vs_spy_points = 8.0
        else:                   # lagging SPY
            relative_strength_vs_spy_points = 0.0

    intraday_momentum_points = vwap_alignment_points + relative_strength_vs_spy_points  # max 40

    # ===== SIGNAL 3: Gamma Structure (20%) =====
    gex_structure_points = 0.0
    gex_favorable = False
    iv_regime = "fair"

    if chain_data:
        # GEX flip: favorable if price above flip (for bullish) or below (for bearish)
        # For now, assume bullish bias (calls) if unusual is call, puts if put
        gex_flip_level_strike = chain_data.get("gex_flip_level")
        current_price = screener_result.price or 0.0

        if gex_flip_level_strike and current_price > 0:
            is_bullish = (flow_signal == "unusual bullish" or flow_signal == "none")
            if is_bullish and current_price > gex_flip_level_strike:
                # Bullish trade, price above GEX flip = favorable (dealer short gamma)
                gex_favorable = True
                gex_structure_points = 12.0
            elif not is_bullish and current_price < gex_flip_level_strike:
                # Bearish trade, price below GEX flip = favorable
                gex_favorable = True
                gex_structure_points = 12.0
            else:
                # Unfavorable GEX structure
                gex_structure_points = 0.0
                gex_favorable = False

    # IV regime: 0–8 pts
    iv_premium_regime_points = 0.0
    if screener_result.iv_premium is not None:
        if screener_result.iv_premium < 0.80:
            iv_regime = "cheap"
            iv_premium_regime_points = 8.0
        elif screener_result.iv_premium < 1.20:
            iv_regime = "fair"
            iv_premium_regime_points = 4.0
        else:
            iv_regime = "rich"
            iv_premium_regime_points = 0.0

    gamma_structure_points = gex_structure_points + iv_premium_regime_points  # max 20

    # ===== WEIGHTED COMPOSITE =====
    # Each category scales to 100 based on its max possible points, then weighted
    # flow (max 40) → [0-100], intraday (max 40) → [0-100], gamma (max 20) → [0-100]
    # Then weight them: 40% + 40% + 20% = 100
    signal_weights = {"institutional_flow": 0.40, "intraday_momentum": 0.40, "gamma_structure": 0.20}
    institutional_flow_normalized = (institutional_flow_points / 40.0) * 100 if institutional_flow_points > 0 or institutional_flow_points == 0 else 0
    intraday_momentum_normalized = (intraday_momentum_points / 40.0) * 100 if intraday_momentum_points > 0 or intraday_momentum_points == 0 else 0
    gamma_structure_normalized = (gamma_structure_points / 20.0) * 100 if gamma_structure_points > 0 or gamma_structure_points == 0 else 0

    confluence_score = (
        institutional_flow_normalized * signal_weights["institutional_flow"] +
        intraday_momentum_normalized * signal_weights["intraday_momentum"] +
        gamma_structure_normalized * signal_weights["gamma_structure"]
    )

    # Clamp to 0–100
    confluence_score = max(0.0, min(100.0, confluence_score))

    # Map to conviction grade
    if confluence_score >= 80:
        conviction_grade = "HIGH"
    elif confluence_score >= 60:
        conviction_grade = "MODERATE"
    elif confluence_score >= 40:
        conviction_grade = "MARGINAL"
    else:
        conviction_grade = "PASS"

    return ConfluenceResult(
        confluence_score=confluence_score,
        flow_signal=flow_signal,
        vwap_alignment=vwap_alignment_points,
        rs_bias=relative_strength_vs_spy_points,
        gex_favorable=gex_favorable,
        iv_regime=iv_regime,
        conviction_grade=conviction_grade,
    )
