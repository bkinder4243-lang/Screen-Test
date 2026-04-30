"""
Composite scorer — Long Call / Long Put only.

Signal sources and weights:
    technical        (Finviz RSI + analyst consensus)  40%
    sentiment        (Reddit bull/bear ratio)           30%
    unusual_activity (Polygon vol/OI spike)             30%

Score range: -2.0 (strong bearish) → +2.0 (strong bullish)
Only |score| > 0.5 produces a recommendation.
"""

from dataclasses import dataclass, field
from typing import Optional
import pandas as pd

WEIGHTS = {
    "technical":        0.50,   # Finviz signal + setup pattern
    "unusual_activity": 0.40,   # Institutional flow — highest alpha signal
    "sentiment":        0.10,   # Reddit — useful only as contrarian context
}

# Only Long Call / Long Put — target ATM-ish delta for pure directional plays
STRATEGY_MAP = [
    ( 0.5,  2.0, "Long Call", "call",  0.45, "Bullish — buy 40–50Δ call, 21–45 DTE"),
    (-2.0, -0.5, "Long Put",  "put",  -0.45, "Bearish — buy 40–50Δ put, 21–45 DTE"),
]

IV_LOW    = 0.20   # below 20% avg IV → cheap premium, good to buy
IV_HIGH   = 0.40   # above 40% avg IV → expensive, warn user


@dataclass
class ScreenerResult:
    symbol:           str
    price:            Optional[float]

    # Signal scores
    technical_score:  Optional[float]
    sentiment_score:  Optional[float]
    unusual_score:    Optional[float]
    composite:        Optional[float]

    # Technicals
    signal:           Optional[str]
    rsi:              Optional[float]
    sector:           Optional[str]
    change_pct:       Optional[float]
    rel_volume:       Optional[float]
    sma20_diff_pct:   Optional[float]
    sma50_diff_pct:   Optional[float]
    pct_from_52w_high: Optional[float]
    setup:            Optional[str]

    # Sentiment
    bullish_pct:      Optional[float]
    reddit_posts:     Optional[int]

    # Unusual activity
    unusual:          bool = False
    unusual_ratio:    float = 0.0
    unusual_type:     Optional[str] = None
    unusual_strike:   Optional[float] = None
    unusual_exp:      Optional[str] = None

    # Options recommendation
    strategy:         Optional[str] = None
    contract_type:    Optional[str] = None
    strategy_note:    Optional[str] = None

    # Best strike found
    rec_strike:       Optional[float] = None
    rec_bid:          Optional[float] = None
    rec_ask:          Optional[float] = None
    rec_delta:        Optional[float] = None
    rec_iv:           Optional[float] = None
    rec_dte:          Optional[int] = None
    has_options:      bool = False

    # IV environment
    avg_iv:           Optional[float] = None
    iv_warning:       bool = False

    # IV Premium (IV vs realized HV — key to knowing if options are cheap/expensive)
    hv_30:            Optional[float] = None   # 30-day historical volatility
    iv_premium:       Optional[float] = None   # avg_iv / hv_30  (>1.2 = expensive)

    # ATR (volatility-adjusted move expectation)
    atr:              Optional[float] = None

    # Earnings risk
    days_to_earnings:    Optional[int] = None
    earnings_within_dte: bool = False           # True if earnings fall inside trade window

    # Consolidation trigger
    consolidation_triggered: bool = False

    # Price targets
    analyst_target:      Optional[float] = None   # Finviz consensus target
    technical_target:    Optional[float] = None   # setup-based stock target
    option_breakeven:    Optional[float] = None   # stock price at option breakeven
    pct_to_breakeven:    Optional[float] = None   # % move needed to breakeven
    pct_to_analyst_tgt:  Optional[float] = None   # % to analyst target
    pct_to_tech_tgt:     Optional[float] = None   # % to technical target
    reward_to_breakeven: Optional[float] = None   # (target - breakeven) / premium paid

    def iv_env(self) -> str:
        if self.avg_iv is None:        return "—"
        if self.avg_iv < IV_LOW:       return "🟢 Low"
        if self.avg_iv < IV_HIGH:      return "🟡 Med"
        return "🔴 High"

    def iv_premium_label(self) -> str:
        """IV relative to 30-day realized vol — the real signal for option expensiveness."""
        if self.iv_premium is None:    return "—"
        if self.iv_premium < 0.80:     return "🟢 Cheap"
        if self.iv_premium < 1.20:     return "🟡 Fair"
        return "🔴 Rich"

    def direction_icon(self) -> str:
        c = self.composite
        if c is None:          return "—"
        if c >= 1.0:           return "🟢🟢"
        if c >= 0.5:           return "🟢"
        if c <= -1.0:          return "🔴🔴"
        if c <= -0.5:          return "🔴"
        return "🟡"

    def to_row(self) -> dict:
        mid = (self.rec_bid + self.rec_ask) / 2 if self.rec_bid and self.rec_ask else None
        return {
            "Symbol":       self.symbol,
            "Setup":        self.setup or "—",
            "Dir":          self.direction_icon(),
            "Score":        f"{self.composite:+.2f}" if self.composite is not None else "—",
            "Signal":       self.signal or "—",
            "RSI":          f"{self.rsi:.0f}" if self.rsi else "—",
            "Rel Vol":      f"{self.rel_volume:.1f}x" if self.rel_volume else "—",
            "Unusual":      f"🔥 {self.unusual_ratio:.1f}x" if self.unusual else "—",
            "Strategy":     self.strategy or "—",
            "Strike":       f"${self.rec_strike:.2f}" if self.rec_strike else "—",
            "Mid":          f"${mid:.2f}" if mid else "—",
            "Delta":        f"{self.rec_delta:.2f}" if self.rec_delta else "—",
            "IV Env":       self.iv_env(),
            "IV Premium":   self.iv_premium_label(),
            "DTE":          str(self.rec_dte) if self.rec_dte else "—",
            "Earnings":     f"⚠️ {self.days_to_earnings}d" if self.earnings_within_dte else (f"{self.days_to_earnings}d" if self.days_to_earnings is not None else "—"),
            "Breakeven":    f"${self.option_breakeven:.2f}" if self.option_breakeven else "—",
            "BE Move":      f"{self.pct_to_breakeven:+.1f}%" if self.pct_to_breakeven is not None else "—",
            "R/BE":         f"{self.reward_to_breakeven:.1f}x" if self.reward_to_breakeven is not None else "—",
            "Tech Target":  f"${self.technical_target:.2f}" if self.technical_target else "—",
            "Analyst Tgt":  f"${self.analyst_target:.2f}" if self.analyst_target else "—",
            "Analyst Upsd": f"{self.pct_to_analyst_tgt:+.1f}%" if self.pct_to_analyst_tgt is not None else "—",
            "Sector":       self.sector or "—",
        }


def _composite(technical: Optional[float],
               sentiment: Optional[float],
               unusual:   Optional[float]) -> Optional[float]:
    total_w = total_s = 0.0
    for score, key in [(technical, "technical"),
                       (sentiment, "sentiment"),
                       (unusual,   "unusual_activity")]:
        if score is not None:
            w = WEIGHTS[key]
            total_s += score * w
            total_w += w
    if total_w == 0:
        return None
    return round(total_s / total_w, 3)


def _pick_strategy(composite: float) -> Optional[tuple]:
    for lo, hi, strat, ctype, delta, note in STRATEGY_MAP:
        if lo <= composite <= hi:
            return strat, ctype, delta, note
    return None


def _technical_target(setup: Optional[str], price: float, finviz: dict) -> Optional[float]:
    """
    Estimate a stock price target based on the detected setup pattern.
    Uses ATR × 3 (≈21-30 day hold) when available; falls back to fixed %.

    Breakout      → 52W high × 1.05  or price + ATR×3
    MA Reclaim    → price + ATR×3    or price × 1.08
    Oversold      → SMA20 price      (mean reversion)
    Blow-off Top  → SMA50 price      (pullback to 50-day MA)
    MA Breakdown  → price - ATR×3    or price × 0.90
    Consolidation → price + ATR×2    (modest breakout target)
    """
    if not setup or price <= 0:
        return None

    sma20_diff = finviz.get("sma20_diff_pct")
    sma50_diff = finviz.get("sma50_diff_pct")
    w52_high   = finviz.get("week52_high")
    atr        = finviz.get("atr")

    if setup == "Breakout":
        base = w52_high if w52_high and w52_high > 0 else price
        if atr and atr > 0:
            return round(max(base * 1.02, price + atr * 3), 2)
        return round(base * 1.05, 2)

    if setup == "MA Reclaim":
        if atr and atr > 0:
            return round(price + atr * 3, 2)
        return round(price * 1.08, 2)

    if setup == "Oversold" and sma20_diff is not None:
        return round(price / (1 + sma20_diff / 100), 2)

    if setup == "Blow-off Top" and sma50_diff is not None:
        return round(price / (1 + sma50_diff / 100), 2)

    if setup == "MA Breakdown":
        if atr and atr > 0:
            return round(price - atr * 3, 2)
        return round(price * 0.90, 2)

    if setup == "Consolidation":
        if atr and atr > 0:
            return round(price + atr * 2, 2)

    return None


def _best_strike(chain: pd.DataFrame, contract_type: str,
                 target_delta: float, max_mid: float,
                 dte_min: int = 21, dte_max: int = 45,
                 tech_target: Optional[float] = None,
                 price: float = 0.0) -> Optional[dict]:
    """
    Find the best strike for a directional Long Call/Put.

    Scoring (when tech_target available):
      60% delta proximity + 40% reward-to-breakeven ratio
      R/BE = (target - breakeven) / premium — rewards strikes where
      the target price comfortably clears the breakeven.

    Falls back to delta-only if no tech_target.
    """
    if chain is None or chain.empty:
        return None

    window = chain[(chain["dte"] >= dte_min) & (chain["dte"] <= dte_max)]
    if window.empty:
        window = chain

    side = window[window["type"] == contract_type].copy()
    side = side[(side["volume"] >= 10) & (side["open_interest"] >= 50) & (side["bid"] > 0)]
    side = side.dropna(subset=["delta"])
    side = side[side["mid"] <= max_mid]
    # Remove garbage IV rows (deep-OTM spikes)
    if "iv" in side.columns:
        side = side[(side["iv"].isna()) | ((side["iv"] >= 0.05) & (side["iv"] <= 3.0))]

    if side.empty:
        side = window[window["type"] == contract_type].dropna(subset=["delta"])
        side = side[(side["bid"] > 0) & (side["mid"] <= max_mid)]
        if "iv" in side.columns:
            side = side[(side["iv"].isna()) | ((side["iv"] >= 0.05) & (side["iv"] <= 3.0))]

    if side.empty:
        return None

    side = side.copy()
    side["delta_diff"] = (side["delta"].abs() - abs(target_delta)).abs()

    if tech_target and price > 0 and not side.empty:
        if contract_type == "call":
            side["breakeven_price"] = side["strike"] + side["mid"]
            side["r2be"] = (tech_target - side["breakeven_price"]) / side["mid"].clip(lower=0.01)
        else:
            side["breakeven_price"] = side["strike"] - side["mid"]
            side["r2be"] = (side["breakeven_price"] - tech_target) / side["mid"].clip(lower=0.01)
        side["r2be"] = side["r2be"].clip(-5, 20)
        # Normalize delta_diff to 0–1 scale then combine
        max_dd = side["delta_diff"].max() or 1
        side["score"] = (0.60 * (1 - side["delta_diff"] / max_dd) +
                         0.40 * (side["r2be"] / 20))
        best = side.nlargest(1, "score").iloc[0]
        r2be_val = float(best["r2be"])
    else:
        best = side.nsmallest(1, "delta_diff").iloc[0]
        r2be_val = None

    return {
        "strike":            best["strike"],
        "bid":               best["bid"],
        "ask":               best["ask"],
        "delta":             best["delta"],
        "iv":                best.get("iv"),
        "dte":               int(best["dte"]) if pd.notna(best["dte"]) else None,
        "reward_to_breakeven": round(r2be_val, 2) if r2be_val is not None else None,
    }


def build_result(
    symbol:     str,
    finviz:     Optional[dict],
    stocktwits: Optional[dict],
    _news_sent, # unused (kept for API compat)
    chain:      Optional[pd.DataFrame],
    unusual_data: Optional[dict] = None,
    max_mid:    float = 15.0,
    dte_min:    int = 21,
    dte_max:    int = 45,
) -> ScreenerResult:

    tech_score = finviz["signal_score"]        if finviz      else None
    sent_score = stocktwits["sentiment_score"] if stocktwits  else None

    # Unusual activity → directional score contribution
    ua = unusual_data or {}
    unusual_flag = ua.get("unusual", False)
    if unusual_flag:
        hint       = ua.get("direction_hint", "bullish")
        ua_score   = 1.0 if hint == "bullish" else -1.0
    else:
        ua_score = None

    composite = _composite(tech_score, sent_score, ua_score)

    # IV environment from 21–45 DTE window
    # Filter garbage IV: deep-OTM contracts return 0-DTE style IV spikes (>300%)
    avg_iv = None
    if chain is not None and not chain.empty:
        window = chain[(chain["dte"] >= dte_min) & (chain["dte"] <= dte_max)]
        if not window.empty and "iv" in window.columns:
            iv_vals = window["iv"].dropna()
            iv_vals = iv_vals[(iv_vals >= 0.05) & (iv_vals <= 3.0)]  # 5%–300% sane range
            if not iv_vals.empty:
                avg_iv = float(iv_vals.mean())

    dte_max_used = dte_max

    result = ScreenerResult(
        symbol           = symbol,
        price            = finviz["price"]           if finviz      else None,
        technical_score  = tech_score,
        sentiment_score  = sent_score,
        unusual_score    = ua_score,
        composite        = composite,
        signal           = finviz["signal"]          if finviz      else None,
        rsi              = finviz["rsi"]             if finviz      else None,
        sector           = finviz["sector"]          if finviz      else None,
        change_pct       = finviz["change_pct"]      if finviz      else None,
        rel_volume       = finviz.get("rel_volume")  if finviz      else None,
        sma20_diff_pct   = finviz.get("sma20_diff_pct") if finviz  else None,
        sma50_diff_pct   = finviz.get("sma50_diff_pct") if finviz  else None,
        pct_from_52w_high= finviz.get("pct_from_52w_high") if finviz else None,
        setup            = finviz.get("setup")       if finviz      else None,
        bullish_pct      = stocktwits["bullish_pct"] if stocktwits  else None,
        reddit_posts     = stocktwits["watchers"]    if stocktwits  else None,
        unusual          = unusual_flag,
        unusual_ratio    = ua.get("vol_oi_ratio", 0.0),
        unusual_type     = ua.get("type"),
        unusual_strike   = ua.get("strike"),
        unusual_exp      = ua.get("expiration"),
        avg_iv           = avg_iv,
        iv_warning       = (avg_iv is not None and avg_iv > IV_HIGH),
        analyst_target   = finviz.get("target_price") if finviz else None,
        atr              = finviz.get("atr")          if finviz else None,
        hv_30            = finviz.get("hv_30")        if finviz else None,
        days_to_earnings = finviz.get("days_to_earnings") if finviz else None,
        consolidation_triggered = finviz.get("consolidation_triggered", False) if finviz else False,
    )

    # Earnings within DTE window?
    if result.days_to_earnings is not None:
        result.earnings_within_dte = result.days_to_earnings <= dte_max_used

    # IV Premium: current implied vs realized historical vol
    if avg_iv and result.hv_30 and result.hv_30 > 0:
        result.iv_premium = round(avg_iv / result.hv_30, 2)

    # Analyst target % upside
    price = result.price or 0
    if result.analyst_target and price > 0:
        result.pct_to_analyst_tgt = round((result.analyst_target - price) / price * 100, 1)

    # Technical target
    if finviz and result.setup and price > 0:
        result.technical_target = _technical_target(result.setup, price, finviz)
        if result.technical_target:
            result.pct_to_tech_tgt = round((result.technical_target - price) / price * 100, 1)

    if composite is not None:
        picked = _pick_strategy(composite)
        if picked:
            strat, ctype, delta, note = picked
            result.strategy      = strat
            result.contract_type = ctype
            result.strategy_note = note

            if chain is not None:
                best = _best_strike(chain, ctype, delta, max_mid, dte_min, dte_max,
                                    tech_target=result.technical_target, price=price)
                if best:
                    result.rec_strike  = best["strike"]
                    result.rec_bid     = best["bid"]
                    result.rec_ask     = best["ask"]
                    result.rec_delta   = best["delta"]
                    result.rec_iv      = best["iv"]
                    result.rec_dte     = best["dte"]
                    result.has_options = True
                    result.reward_to_breakeven = best.get("reward_to_breakeven")

                    # Option breakeven at expiry
                    mid = (best["bid"] + best["ask"]) / 2
                    if strat == "Long Call":
                        result.option_breakeven = round(best["strike"] + mid, 2)
                    else:
                        result.option_breakeven = round(best["strike"] - mid, 2)
                    if price > 0 and result.option_breakeven:
                        result.pct_to_breakeven = round(
                            (result.option_breakeven - price) / price * 100, 1
                        )

    return result
