"""Finviz technicals — signal, RSI, MAs, setup pattern detection."""

import logging
from typing import Optional
from finvizfinance.quote import finvizfinance

try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:
    _YF_AVAILABLE = False

logger = logging.getLogger(__name__)

SIGNAL_SCORES = {
    "Strong Buy":  2.0,
    "Buy":         1.0,
    "Hold":        0.0,
    "Sell":       -1.0,
    "Strong Sell":-2.0,
}


def _numeric_recom(val) -> tuple[str, float]:
    """Map Finviz 1–5 Recom scale → label + score (-2 to +2)."""
    try:
        v = float(val)
    except (TypeError, ValueError):
        return str(val), 0.0
    score = max(-2.0, min(2.0, -(v - 3)))
    if v <= 1.5:   label = "Strong Buy"
    elif v <= 2.5: label = "Buy"
    elif v <= 3.5: label = "Hold"
    elif v <= 4.5: label = "Sell"
    else:          label = "Strong Sell"
    return label, score


def _parse_float(raw) -> Optional[float]:
    """Strip %, commas, and trailing text; return float or None."""
    if raw is None:
        return None
    try:
        s = str(raw).replace(",", "").replace("%", "").split()[0]
        return float(s)
    except Exception:
        return None


def _parse_sma_pct(raw) -> Optional[float]:
    """Finviz SMA fields look like '8.45%' — return signed float."""
    return _parse_float(raw)


def _parse_52w(raw) -> tuple[Optional[float], Optional[float]]:
    """
    Finviz 52W High/Low look like '216.82 -3.45%'.
    Returns (price, pct_diff).  pct_diff is negative when current < 52W high.
    """
    if raw is None:
        return None, None
    parts = str(raw).split()
    price = _parse_float(parts[0]) if parts else None
    pct   = _parse_float(parts[1]) if len(parts) > 1 else None
    return price, pct


def _price_context(symbol: str) -> dict:
    """
    Fetch price history + calendar via yfinance. Computes:
      range_5d_pct        : (5d high - 5d low) / close × 100
      sma9_slope          : SMA9 delta over 5 sessions (rising = > 0)
      sma50_slope         : SMA50 delta over 5 sessions
      hv_30               : 30-day annualized historical volatility
      consolidation_triggered : True when consolidation box is breaking out
      days_to_earnings    : calendar days until next earnings (None if unknown)
    Returns empty dict on any failure.
    """
    import math as _math
    if not _YF_AVAILABLE:
        return {}
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="60d", interval="1d", auto_adjust=True)
        if hist is None or len(hist) < 15:
            return {}

        close = hist["Close"].squeeze()
        high  = hist["High"].squeeze()
        low   = hist["Low"].squeeze()
        volume = hist["Volume"].squeeze()

        # 5-day tight range
        h5 = float(high.iloc[-5:].max())
        l5 = float(low.iloc[-5:].min())
        c  = float(close.iloc[-1])
        range_5d_pct = (h5 - l5) / c * 100 if c > 0 else None

        # SMA slopes (today vs 5 sessions ago)
        sma9  = close.rolling(9).mean()
        sma50 = close.rolling(50).mean()
        sma9_slope  = (float(sma9.iloc[-1])  - float(sma9.iloc[-6]))  if len(sma9)  >= 6 else None
        sma50_slope = (float(sma50.iloc[-1]) - float(sma50.iloc[-6])) if len(sma50) >= 6 else None

        # 30-day historical volatility (annualized)
        if len(close) >= 22:
            log_rets = close.pct_change().dropna()
            hv_30 = float(log_rets.iloc[-21:].std() * _math.sqrt(252))
        else:
            hv_30 = None

        # Consolidation breakout trigger:
        # volume today > 1.5× 10-day avg AND relative to yesterday's close breaking higher
        consol_triggered = False
        if len(volume) >= 11 and len(close) >= 6:
            avg_vol_10d = float(volume.iloc[-11:-1].mean())
            today_vol   = float(volume.iloc[-1])
            price_above_5d_high = c > float(high.iloc[-6:-1].max())
            consol_triggered = (today_vol > avg_vol_10d * 1.5 and price_above_5d_high)

        # Earnings date
        days_to_earnings = None
        try:
            from datetime import date as _date
            cal = ticker.calendar
            if isinstance(cal, dict) and "Earnings Date" in cal:
                edate = cal["Earnings Date"]
                if isinstance(edate, list):
                    edate = edate[0]
                if edate is not None:
                    if hasattr(edate, "date"):
                        edate = edate.date()
                    days_to_earnings = max(0, (_date.fromisoformat(str(edate)) - _date.today()).days)
        except Exception:
            pass

        return {
            "range_5d_pct":           round(range_5d_pct, 2) if range_5d_pct is not None else None,
            "sma9_slope":             round(sma9_slope,  4) if sma9_slope  is not None else None,
            "sma50_slope":            round(sma50_slope, 4) if sma50_slope is not None else None,
            "hv_30":                  round(hv_30, 4) if hv_30 is not None else None,
            "consolidation_triggered": consol_triggered,
            "days_to_earnings":       days_to_earnings,
        }
    except Exception as e:
        logger.debug(f"_price_context failed for {symbol}: {e}")
        return {}


def detect_setup(info: dict) -> str:
    """
    Classify the current technical pattern.

    Priority order (first match wins):
      Blow-off Top  — RSI > 75  (over-extended, put candidate)
      Breakout      — price within 2% of 52W High (call candidate)
      Oversold      — RSI < 35  (reversal candidate, call)
      MA Breakdown  — price below SMA50 (bearish, put)
      MA Reclaim    — price just above SMA50, within 5% (call)
      Consolidation — 5-day range < 4%, rising 9 DMA + rising 50 DMA (tight box into uptrend)
      Neutral       — nothing else matches
    """
    rsi              = info.get("rsi") or 50
    sma20_diff       = info.get("sma20_diff_pct")   # + means price above SMA20
    sma50_diff       = info.get("sma50_diff_pct")
    pct_from_52w_high = info.get("pct_from_52w_high")  # negative = below high

    if rsi > 75:
        return "Blow-off Top"

    if pct_from_52w_high is not None and pct_from_52w_high >= -2.0:
        return "Breakout"

    if rsi < 35:
        return "Oversold"

    if sma50_diff is not None and sma50_diff < 0:
        return "MA Breakdown"

    if sma50_diff is not None and 0 <= sma50_diff <= 5:
        return "MA Reclaim"

    range_5d  = info.get("range_5d_pct")
    sma9_slope  = info.get("sma9_slope")
    sma50_slope = info.get("sma50_slope")
    if (range_5d is not None and range_5d < 4.0
            and sma9_slope  is not None and sma9_slope  > 0
            and sma50_slope is not None and sma50_slope > 0):
        return "Consolidation"

    return "Neutral"


def get_technicals(symbol: str) -> Optional[dict]:
    """
    Fetch Finviz snapshot for a symbol.

    Returns dict with technicals, setup pattern, and scoring.
    """
    try:
        info = finvizfinance(symbol).ticker_fundament()

        signal_raw, signal_score = _numeric_recom(info.get("Recom", "3"))

        rsi = _parse_float(info.get("RSI (14)", "50")) or 50.0

        change_raw = info.get("Change", "0%")
        change_pct = _parse_float(change_raw) or 0.0

        price = _parse_float(info.get("Price", "0")) or 0.0

        rel_volume = _parse_float(info.get("Rel Volume")) or 1.0

        sma20_diff  = _parse_sma_pct(info.get("SMA20"))
        sma50_diff  = _parse_sma_pct(info.get("SMA50"))
        sma200_diff = _parse_sma_pct(info.get("SMA200"))

        w52_high_price, pct_from_52w_high = _parse_52w(info.get("52W High"))
        w52_low_price,  _                 = _parse_52w(info.get("52W Low"))

        target_price = _parse_float(info.get("Target Price"))
        atr          = _parse_float(info.get("ATR (14)"))

        data = {
            "signal":           signal_raw,
            "signal_score":     signal_score,
            "rsi":              rsi,
            "change_pct":       change_pct,
            "price":            price,
            "sector":           info.get("Sector", "—"),
            "industry":         info.get("Industry", "—"),
            "beta":             _parse_float(info.get("Beta")),
            "avg_volume":       info.get("Avg Volume", "—"),
            "rel_volume":       rel_volume,
            "sma20_diff_pct":   sma20_diff,
            "sma50_diff_pct":   sma50_diff,
            "sma200_diff_pct":  sma200_diff,
            "week52_high":      w52_high_price,
            "week52_low":       w52_low_price,
            "pct_from_52w_high": pct_from_52w_high,
            "target_price":     target_price,
            "atr":              atr,
        }

        data.update(_price_context(symbol))
        data["setup"] = detect_setup(data)
        return data

    except Exception as e:
        logger.warning(f"Finviz failed for {symbol}: {e}")
        return None
