"""
Intraday data helpers — 1-min bars, session VWAP, relative strength, GEX flip level.
Data source: yfinance (free, no API key). Polygon path removed — requires paid plan.
"""

from __future__ import annotations
import logging
from typing import Optional
import pandas as pd

logger = logging.getLogger(__name__)


def get_intraday_bars(symbol: str) -> pd.DataFrame:
    """
    Fetch today's 1-minute OHLCV bars via yfinance (free).
    Computes running session VWAP = cumsum(TP×Vol) / cumsum(Vol).
    """
    try:
        import yfinance as yf
        raw = yf.download(symbol, period="1d", interval="1m", progress=False, auto_adjust=True)
        if raw.empty:
            return pd.DataFrame()

        # Flatten multi-level columns produced by yfinance when downloading single ticker
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = [col[0].lower() for col in raw.columns]
        else:
            raw.columns = [c.lower() for c in raw.columns]

        df = raw.reset_index().rename(columns={"Datetime": "ts", "datetime": "ts"})
        df["ts"] = pd.to_datetime(df["ts"], utc=True)

        df["tp"]           = (df["high"] + df["low"] + df["close"]) / 3
        df["cum_tp_vol"]   = (df["tp"] * df["volume"]).cumsum()
        df["cum_vol"]      = df["volume"].cumsum()
        df["session_vwap"] = (df["cum_tp_vol"] / df["cum_vol"]).round(4)

        return df[["ts", "open", "high", "low", "close", "volume", "session_vwap"]]
    except Exception as e:
        logger.warning(f"yfinance intraday bars failed for {symbol}: {e}")
        return pd.DataFrame()


def get_relative_strength(symbol: str, benchmark: str = "SPY") -> dict:
    """
    Intraday % change of symbol vs benchmark (SPY default).
    Returns alpha (outperformance), VWAP diff, and intraday metrics.
    """
    sym_bars   = get_intraday_bars(symbol)
    bench_bars = get_intraday_bars(benchmark)

    empty = {
        "symbol_chg": None, "bench_chg": None,
        "rs_ratio":   None, "leading":   None,
        "vs_vwap":    None, "vwap":      None,
        "current":    None, "day_high":  None,
        "day_low":    None, "bars":      sym_bars,
    }
    if sym_bars.empty or bench_bars.empty:
        return empty

    sym_open  = sym_bars["open"].iloc[0]
    sym_last  = sym_bars["close"].iloc[-1]
    ben_open  = bench_bars["open"].iloc[0]
    ben_last  = bench_bars["close"].iloc[-1]

    if not sym_open or not ben_open:
        return empty

    sym_chg = (sym_last - sym_open) / sym_open * 100
    ben_chg = (ben_last - ben_open) / ben_open * 100
    rs      = round(sym_chg - ben_chg, 3)
    vwap    = sym_bars["session_vwap"].iloc[-1]
    vs_vwap = round((sym_last - vwap) / vwap * 100, 3) if vwap else None

    return {
        "symbol_chg": round(sym_chg, 3),
        "bench_chg":  round(ben_chg, 3),
        "rs_ratio":   rs,
        "leading":    rs > 0,
        "vs_vwap":    vs_vwap,
        "vwap":       round(float(vwap), 2) if vwap else None,
        "current":    round(float(sym_last), 2),
        "day_high":   round(float(sym_bars["high"].max()), 2),
        "day_low":    round(float(sym_bars["low"].min()), 2),
        "bars":       sym_bars,
    }


def gex_flip_level(chain: pd.DataFrame, spot: float) -> Optional[float]:
    """
    Find the strike where cumulative GEX (ascending strike order) crosses zero.

    Below flip → negative gamma zone → dealer hedging amplifies moves.
    Above flip → positive gamma zone → dealer hedging pins price.
    """
    if chain.empty or "gamma" not in chain.columns:
        return None

    df = chain[chain["gamma"].notna() & (chain["open_interest"] > 0)].copy()
    if df.empty:
        return None

    df["gex"] = df.apply(
        lambda r: (
            -r["gamma"] * r["open_interest"] * 100 * spot ** 2
            if r["type"] == "call"
            else  r["gamma"] * r["open_interest"] * 100 * spot ** 2
        ),
        axis=1,
    )

    by_strike  = df.groupby("strike")["gex"].sum().sort_index()
    cumulative = by_strike.cumsum()

    prev_sign = None
    for strike, val in cumulative.items():
        sign = val >= 0
        if prev_sign is not None and sign != prev_sign:
            return float(strike)
        prev_sign = sign

    return None
