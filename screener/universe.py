"""
Ticker universe lists — fetched from Wikipedia with in-process caching.
Falls back to hardcoded lists if the fetch fails.
"""

import logging
from functools import lru_cache
from io import StringIO
import requests
import pandas as pd

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; options-screener/1.0)"}

logger = logging.getLogger(__name__)

DOW30 = [
    "AAPL","AMGN","AXP","BA","CAT","CRM","CSCO","CVX","DIS","DOW",
    "GS","HD","HON","IBM","INTC","JNJ","JPM","KO","MCD","MMM",
    "MRK","MSFT","NKE","PG","TRV","UNH","V","VZ","WBA","WMT",
]

_NASDAQ100_FALLBACK = [
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","GOOG","TSLA","AVGO","COST",
    "NFLX","AMD","PEP","ADBE","CSCO","TMUS","QCOM","INTC","INTU","AMAT",
    "TXN","AMGN","HON","BKNG","SBUX","ISRG","GILD","ADP","MDLZ","REGN",
    "VRTX","MU","LRCX","ADI","PANW","KLAC","SNPS","CDNS","ASML","MELI",
    "MNST","CTAS","KDP","ORLY","FTNT","ABNB","MAR","PYPL","WDAY","DXCM",
    "CEG","PCAR","AZN","MRVL","CPRT","ODFL","ROST","NXPI","TEAM","IDXX",
    "FAST","VRSK","PAYX","KHC","CTSH","EXC","BIIB","WBD","ZS","DLTR",
    "SGEN","XEL","ALGN","ANSS","SPLK","FANG","SWKS","LCID","MTCH","WBA",
    "ILMN","SIRI","OKTA","ZM","DOCU","CRWD","DDOG","SNOW","COIN","RBLX",
]

_SP500_FALLBACK: list[str] = []  # too large to hardcode; will warn on failure


def _wiki_tables(url: str) -> list:
    html = requests.get(url, headers=_HEADERS, timeout=15).text
    return pd.read_html(StringIO(html))


@lru_cache(maxsize=None)
def nasdaq100_tickers() -> list[str]:
    try:
        tables = _wiki_tables("https://en.wikipedia.org/wiki/Nasdaq-100")
        for df in tables:
            col = next((c for c in df.columns if "ticker" in str(c).lower() or "symbol" in str(c).lower()), None)
            if col and len(df) > 50:
                tickers = df[col].astype(str).str.replace(r"\[.*\]", "", regex=True).str.strip().tolist()
                logger.info(f"Fetched {len(tickers)} NASDAQ-100 tickers from Wikipedia")
                return tickers
    except Exception as e:
        logger.warning(f"NASDAQ-100 Wikipedia fetch failed: {e} — using fallback list")
    return _NASDAQ100_FALLBACK


@lru_cache(maxsize=None)
def sp500_tickers() -> list[str]:
    try:
        tables = _wiki_tables("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
        for df in tables:
            col = next((c for c in df.columns if "symbol" in str(c).lower() or "ticker" in str(c).lower()), None)
            if col and len(df) > 400:
                tickers = (
                    df[col].astype(str)
                    .str.replace(r"\[.*\]", "", regex=True)
                    .str.replace(".", "-", regex=False)
                    .str.strip()
                    .tolist()
                )
                logger.info(f"Fetched {len(tickers)} S&P 500 tickers from Wikipedia")
                return tickers
    except Exception as e:
        logger.warning(f"S&P 500 Wikipedia fetch failed: {e}")
    if _SP500_FALLBACK:
        return _SP500_FALLBACK
    raise RuntimeError("Could not load S&P 500 tickers and no fallback available.")


UNIVERSES = {
    "Custom":      None,
    "Dow 30":      DOW30,
    "NASDAQ 100":  "lazy:nasdaq100",
    "S&P 500":     "lazy:sp500",
}


def load_universe(name: str) -> list[str] | None:
    """Return ticker list for the given universe name, or None for Custom."""
    if name == "Custom":
        return None
    if name == "Dow 30":
        return DOW30
    if name == "NASDAQ 100":
        return nasdaq100_tickers()
    if name == "S&P 500":
        return sp500_tickers()
    return None
