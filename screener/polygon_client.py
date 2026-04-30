"""
Polygon.io client — Options Starter plan.

Covered endpoints:
  /v3/snapshot/options/{symbol}  — live chain with greeks (paginated)

Stock prices via yfinance (free, no key needed).
News endpoint NOT included in Options Starter.
"""

import os
import logging
import math
import requests
from datetime import datetime, timedelta
from typing import Optional
from dotenv import load_dotenv, dotenv_values
import pandas as pd

load_dotenv("config/secrets.env")
logger = logging.getLogger(__name__)

BASE = "https://api.polygon.io"


def _api_key() -> str:
    cfg = dotenv_values("config/secrets.env")
    return cfg.get("POLYGON_API_KEY", os.getenv("POLYGON_API_KEY", ""))


def _get(path: str, params: dict) -> Optional[dict]:
    key = _api_key()
    if not key:
        return None
    params["apiKey"] = key
    try:
        r = requests.get(f"{BASE}{path}", params=params, timeout=10)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 401:
            logger.warning("Polygon 401 — check API key in config/secrets.env")
        elif r.status_code == 403:
            logger.warning(f"Polygon 403 — endpoint not in your plan: {path}")
        else:
            logger.warning(f"Polygon {r.status_code} on {path}: {r.text[:100]}")
        return None
    except Exception as e:
        logger.warning(f"Polygon request error: {e}")
        return None


def key_is_working() -> bool:
    data = _get("/v3/snapshot/options/SPY", {"limit": 1})
    return data is not None and data.get("status") == "OK"


def get_options_chain(symbol: str, dte_min: int = 7, dte_max: int = 90) -> Optional[pd.DataFrame]:
    """
    Fetch full live options snapshot for a symbol.
    DTE default widened to 7–90 so unusual activity outside 21–45 is still visible.
    Returns DataFrame: strike, expiration, dte, type, bid, ask, mid,
                       iv, delta, gamma, theta, vega, volume, open_interest
    """
    exp_from = (datetime.now() + timedelta(days=dte_min)).strftime("%Y-%m-%d")
    exp_to   = (datetime.now() + timedelta(days=dte_max)).strftime("%Y-%m-%d")

    rows = []
    params = {
        "expiration_date.gte": exp_from,
        "expiration_date.lte": exp_to,
        "limit": 250,
        "order": "asc",
    }
    url = f"/v3/snapshot/options/{symbol}"

    while url:
        data = _get(url, params)
        if not data or data.get("status") != "OK":
            break

        for r in data.get("results", []):
            greeks = r.get("greeks") or {}
            detail = r.get("details") or {}
            quote  = r.get("last_quote") or {}
            day    = r.get("day") or {}
            bid    = quote.get("bid") or 0
            ask    = quote.get("ask") or 0
            rows.append({
                "option_symbol": r.get("ticker", ""),
                "strike":        detail.get("strike_price"),
                "expiration":    detail.get("expiration_date", ""),
                "type":          detail.get("contract_type", ""),
                "bid":           bid,
                "ask":           ask,
                "mid":           (bid + ask) / 2,
                "iv":            r.get("implied_volatility"),
                "delta":         greeks.get("delta"),
                "gamma":         greeks.get("gamma"),
                "theta":         greeks.get("theta"),
                "vega":          greeks.get("vega"),
                "volume":        day.get("volume") or 0,
                "open_interest": r.get("open_interest") or 0,
            })

        next_url = data.get("next_url")
        if next_url:
            url    = next_url.replace(BASE, "")
            params = {}
        else:
            break

    if not rows:
        return None

    df = pd.DataFrame(rows)
    df["dte"] = df["expiration"].apply(
        lambda x: max(0, (datetime.strptime(x, "%Y-%m-%d") - datetime.now()).days) if x else None
    )

    # Fill theoretical prices when bid/ask missing (Polygon Starter has no live quotes)
    if df["bid"].eq(0).all() and df["iv"].notna().any():
        spot = get_spot_price(symbol)
        if spot:
            def _fill_price(row):
                if row["bid"] == 0 and pd.notna(row["iv"]) and row["iv"] > 0 and row["dte"] > 0:
                    t = row["dte"] / 365.0
                    flag = 'c' if row["type"] == "call" else 'p'
                    price = _bs_price(flag, spot, row["strike"], t, row["iv"])
                    if price and price > 0.01:
                        spread = max(0.01, price * 0.02)
                        return price - spread, price + spread, price
                return row["bid"], row["ask"], row["mid"]

            prices = df.apply(_fill_price, axis=1, result_type="expand")
            df["bid"] = prices[0]
            df["ask"] = prices[1]
            df["mid"] = prices[2]

    return df


def get_unusual_activity(symbol: str, chain: pd.DataFrame) -> dict:
    """
    Scan an already-fetched chain for institutional-grade unusual flow.

    Scoring: vol/OI ratio × log10(notional) rewards large-premium trades,
    not just high ratios on tiny volume.

    Criteria:
      - volume >= 500  (filters noise)
      - open_interest >= 200
      - notional (volume × mid × 100) >= $50,000
      - dte >= 7  (avoids 0-DTE gamma scalping noise)
      - vol/OI >= 1.5
    """
    empty = {
        "unusual": False, "vol_oi_ratio": 0.0, "notional": 0,
        "strike": None, "expiration": None, "type": None,
        "volume": 0, "open_interest": 0, "direction_hint": None,
    }

    if chain is None or chain.empty:
        return empty

    c = chain[
        (chain["volume"] >= 500) &
        (chain["open_interest"] >= 200) &
        (chain["mid"] > 0) &
        (chain.get("dte", pd.Series(dtype=float)) >= 7)
    ].copy()
    if c.empty:
        # Fall back to looser thresholds for low-volume tickers
        c = chain[
            (chain["volume"] >= 100) &
            (chain["open_interest"] >= 50) &
            (chain["mid"] > 0)
        ].copy()
    if c.empty:
        return empty

    c["notional"]     = c["volume"] * c["mid"] * 100
    c = c[c["notional"] >= 50_000]
    if c.empty:
        return empty

    c["vol_oi_ratio"] = c["volume"] / c["open_interest"]
    c = c[c["vol_oi_ratio"] >= 1.5]
    if c.empty:
        return empty

    c["ua_score"] = c["vol_oi_ratio"] * c["notional"].apply(lambda x: math.log10(max(x, 1)))
    best = c.nlargest(1, "ua_score").iloc[0]

    return {
        "unusual":        True,
        "vol_oi_ratio":   round(float(best["vol_oi_ratio"]), 1),
        "notional":       int(best["notional"]),
        "strike":         best["strike"],
        "expiration":     best["expiration"],
        "type":           best["type"],
        "volume":         int(best["volume"]),
        "open_interest":  int(best["open_interest"]),
        "direction_hint": "bullish" if best["type"] == "call" else "bearish",
    }


def _bs_price(flag: str, S: float, K: float, t: float, iv: float, r: float = 0.05) -> Optional[float]:
    """Black-Scholes theoretical price. flag='c' for call, 'p' for put."""
    if t <= 0 or iv <= 0 or S <= 0 or K <= 0:
        return None
    try:
        from scipy.stats import norm
        d1 = (math.log(S / K) + (r + 0.5 * iv ** 2) * t) / (iv * math.sqrt(t))
        d2 = d1 - iv * math.sqrt(t)
        if flag == 'c':
            return S * norm.cdf(d1) - K * math.exp(-r * t) * norm.cdf(d2)
        else:
            return K * math.exp(-r * t) * norm.cdf(-d2) - S * norm.cdf(-d1)
    except Exception:
        return None


def get_spot_price(symbol: str) -> Optional[float]:
    """Real-time price from yfinance."""
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info
        return info.get("regularMarketPrice") or info.get("currentPrice")
    except Exception as e:
        logger.warning(f"yfinance failed for {symbol}: {e}")
        return None


def get_news(symbol: str, limit: int = 20) -> list[dict]:
    """Stub — News requires Stocks plan."""
    return []
