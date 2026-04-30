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


_last_api_error: dict = {}   # module-level, readable by the UI


def _get(path: str, params: dict) -> Optional[dict]:
    key = _api_key()
    if not key:
        _last_api_error["msg"] = "No Polygon API key found in config/secrets.env"
        return None
    params["apiKey"] = key
    try:
        r = requests.get(f"{BASE}{path}", params=params, timeout=15)
        if r.status_code == 200:
            _last_api_error.clear()
            return r.json()
        if r.status_code == 401:
            _last_api_error["msg"] = "401 Unauthorized — check API key"
        elif r.status_code == 403:
            _last_api_error["msg"] = f"403 Forbidden — endpoint not in your plan: {path}"
        elif r.status_code == 429:
            _last_api_error["msg"] = "429 Rate limited — too many requests, wait a moment"
        else:
            _last_api_error["msg"] = f"HTTP {r.status_code}: {r.text[:120]}"
        logger.warning(f"Polygon {r.status_code} on {path}: {_last_api_error['msg']}")
        return None
    except Exception as e:
        _last_api_error["msg"] = f"Request error: {e}"
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


def get_option_trades(option_symbol: str, limit: int = 250) -> pd.DataFrame:
    """
    Fetch recent trade prints for a specific options contract.
    option_symbol: Polygon option ticker, e.g. 'O:AAPL241220C00150000'
                   or bare format 'AAPL241220C00150000' — O: prefix added automatically.
    Requires Polygon plan with options trades access.
    """
    if not option_symbol.startswith("O:"):
        option_symbol = f"O:{option_symbol}"

    data = _get(f"/v3/trades/{option_symbol}", {
        "limit": limit,
        "order": "desc",
        "sort":  "timestamp",
    })
    if not data or data.get("status") != "OK":
        return pd.DataFrame()

    rows = []
    for t in data.get("results", []):
        ts_ns = t.get("sip_timestamp") or t.get("participant_timestamp") or 0
        rows.append({
            "timestamp":  ts_ns,
            "price":      t.get("price"),
            "size":       t.get("size") or 0,
            "exchange":   t.get("exchange"),
            "conditions": t.get("conditions", []),
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ns", utc=True)
    return df.sort_values("timestamp").reset_index(drop=True)


def detect_sweeps(trades: pd.DataFrame, ask: float = None) -> list[dict]:
    """
    Identify sweep orders from trade prints.

    A sweep is multiple fills within a 2-second window totalling ≥ 50 contracts.
    Sweeps at/above the ask = aggressive bullish buying.
    Sweeps at/below a low price = aggressive bearish selling.
    Returns a list of sweep event dicts.
    """
    if trades.empty or len(trades) < 2:
        return []

    trades = trades.sort_values("timestamp").copy()
    trades["ts_s"] = trades["timestamp"].astype("int64") // 1_000_000_000

    sweeps = []
    used = set()
    for i in trades.index:
        if i in used:
            continue
        t0 = trades.at[i, "ts_s"]
        window = trades[(trades["ts_s"] >= t0) & (trades["ts_s"] <= t0 + 2)]
        if len(window) < 2:
            continue
        total = int(window["size"].sum())
        if total < 50:
            continue
        avg_px = float((window["price"] * window["size"]).sum() / total)
        if ask and ask > 0:
            if avg_px >= ask * 0.98:
                side = "🟢 BUY sweep"
            elif avg_px <= ask * 0.80:
                side = "🔴 SELL sweep"
            else:
                side = "⚪ Unknown"
        else:
            side = "⚪ Unknown"
        sweeps.append({
            "time":      trades.at[i, "timestamp"].strftime("%H:%M:%S UTC"),
            "contracts": total,
            "avg_price": round(avg_px, 2),
            "fills":     len(window),
            "side":      side,
            "notional":  int(total * avg_px * 100),
        })
        used.update(window.index)

    return sweeps


def get_option_iv_history(option_symbol: str, days: int = 30) -> pd.DataFrame:
    """
    Daily OHLCV bars for a specific options contract.
    Use the 'close' price column to track premium movement over time.
    option_symbol: Polygon option ticker, e.g. 'O:SPY251219C00560000'
    Requires Polygon plan with options aggregates access.
    """
    if not option_symbol.startswith("O:"):
        option_symbol = f"O:{option_symbol}"

    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    to_date   = datetime.now().strftime("%Y-%m-%d")

    data = _get(
        f"/v2/aggs/ticker/{option_symbol}/range/1/day/{from_date}/{to_date}",
        {"adjusted": "true", "sort": "asc", "limit": 50},
    )
    if not data or not data.get("results"):
        return pd.DataFrame()

    rows = [
        {
            "date":   datetime.fromtimestamp(bar["t"] / 1000).strftime("%Y-%m-%d"),
            "open":   bar.get("o"),
            "high":   bar.get("h"),
            "low":    bar.get("l"),
            "close":  bar.get("c"),
            "volume": bar.get("v"),
            "vwap":   bar.get("vw"),
        }
        for bar in data["results"]
    ]
    return pd.DataFrame(rows)


def get_news(symbol: str, limit: int = 20) -> list[dict]:
    """Stub — News requires Stocks plan."""
    return []


def get_top_volume_options(top_n: int = 25) -> pd.DataFrame:
    """
    Fetch the highest-volume option contracts across the US market.

    Strategy: sample the 20 most liquid option tickers (ETFs + mega-caps)
    using the per-ticker Polygon snapshot (works on Starter plan).
    Fetch top 50 contracts each sorted by volume, combine, return top_n.
    """
    # Broad universe: ETFs + mega-caps + high-vol names across sectors
    LIQUID = [
        # Index ETFs
        "SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "TLT", "HYG",
        # Sector ETFs
        "XLF", "XLE", "XLK", "XLV", "XBI", "ARKK", "SMH",
        # Mega-cap tech
        "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA",
        # Semis / hardware
        "AMD", "INTC", "MU", "ARM", "AVGO", "TSM",
        # High-vol / meme / crypto-adjacent
        "PLTR", "COIN", "MSTR", "HOOD", "SOFI", "RIVN", "GME",
        # Finance
        "JPM", "BAC", "GS", "C", "MS",
        # Energy
        "XOM", "CVX", "OXY",
        # Biotech / health
        "MRNA", "BNTX", "LLY", "UNH",
        # Consumer / retail
        "NFLX", "DIS", "NKE", "BABA",
        # Other liquid names
        "F", "T", "UBER", "LYFT", "SQ",
    ]

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _fetch_one(sym):
        data = _get(f"/v3/snapshot/options/{sym}", {
            "limit": 250, "order": "asc",
        })
        if not data or data.get("status") != "OK":
            return []
        rows = []
        for r in data.get("results", []):
            greeks = r.get("greeks") or {}
            detail = r.get("details") or {}
            quote  = r.get("last_quote") or {}
            day    = r.get("day") or {}
            bid    = quote.get("bid") or 0
            ask    = quote.get("ask") or 0
            exp    = detail.get("expiration_date", "")
            vol    = day.get("volume") or 0
            if vol == 0:
                continue
            mid = (bid + ask) / 2 if (bid or ask) else (day.get("close") or 0)
            rows.append({
                "ticker":        sym,
                "type":          detail.get("contract_type", ""),
                "strike":        detail.get("strike_price"),
                "expiration":    exp,
                "dte":           max(0, (datetime.strptime(exp, "%Y-%m-%d") - datetime.now()).days) if exp else None,
                "volume":        vol,
                "open_interest": r.get("open_interest") or 0,
                "mid":           mid,
                "iv":            r.get("implied_volatility"),
                "delta":         greeks.get("delta"),
                "vega":          greeks.get("vega"),
                "notional":      int(vol * mid * 100),
            })
        return rows

    all_rows = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_fetch_one, sym): sym for sym in LIQUID}
        for fut in as_completed(futures):
            try:
                all_rows.extend(fut.result())
            except Exception as e:
                logger.warning(f"Top-volume fetch failed for {futures[fut]}: {e}")

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df = df[df["volume"] > 0]

    # Keep only the single highest-volume contract per ticker
    df = (df.sort_values("volume", ascending=False)
            .drop_duplicates(subset="ticker", keep="first")
            .nlargest(top_n, "volume")
            .reset_index(drop=True))
    return df
