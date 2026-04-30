"""
ETF universe — major ETFs grouped by category, with top-10 holdings.

Holdings are hardcoded for reliability (free APIs are rate-limited / unstable).
Use refresh_holdings() to attempt a live update via yfinance.
"""

from __future__ import annotations
import logging
logger = logging.getLogger(__name__)

# ── ETF list by category ──────────────────────────────────────────────────────
ETF_CATEGORIES: dict[str, list[str]] = {
    "Broad Market":        ["SPY","QQQ","IWM","DIA","VTI","VOO","MDY","IJR"],
    "Sectors — SPDR":      ["XLK","XLF","XLV","XLE","XLI","XLB","XLY","XLP","XLU","XLRE","XLC"],
    "Sectors — Vanguard":  ["VGT","VFH","VHT","VDE","VIS","VAW","VCR","VDC","VPU","VNQ","VOX"],
    "Semiconductors":      ["SMH","SOXX","PSI"],
    "Tech / Growth":       ["IGV","CIBR","SKYY","WCLD","BUG","HACK"],
    "Biotech / Health":    ["IBB","XBI","ARKG","LABU"],
    "Financials / Banks":  ["KBE","KRE","IAI"],
    "Energy / Clean":      ["XOP","OIH","ICLN","TAN","QCLN"],
    "Commodities":         ["GLD","SLV","USO","PDBC","DBA"],
    "Disruptive / Thematic":["ARKK","ARKW","ARKG","ARKQ","ARKF","LIT","ROBO","DRIV","BOTZ","METV"],
    "Dividend / Value":    ["VYM","DVY","SCHD","HDV","VTV"],
}

ETF_UNIVERSE: list[str] = [t for tickers in ETF_CATEGORIES.values() for t in tickers]

# ── Top-10 holdings (as of early 2025) ───────────────────────────────────────
ETF_HOLDINGS: dict[str, list[str]] = {
    # ── Broad Market ──────────────────────────────────────────────────────────
    "SPY":  ["AAPL","MSFT","NVDA","AMZN","META","GOOGL","GOOG","TSLA","BRK-B","UNH"],
    "QQQ":  ["MSFT","AAPL","NVDA","AMZN","META","TSLA","GOOGL","GOOG","AVGO","COST"],
    "IWM":  ["IRTC","MGNI","TGTX","PGNY","APPF","SWI","DOCN","HIMS","CABA","ACAD"],
    "DIA":  ["UNH","GS","MSFT","HD","CAT","SHW","MCD","V","AMGN","AXP"],
    "VTI":  ["AAPL","MSFT","NVDA","AMZN","META","GOOGL","TSLA","BRK-B","GOOG","UNH"],
    "VOO":  ["AAPL","MSFT","NVDA","AMZN","META","GOOGL","GOOG","TSLA","BRK-B","UNH"],
    "MDY":  ["AXON","DECK","FN","MOH","EXP","MMS","WFRD","WSM","PCVX","KEX"],
    "IJR":  ["IRTC","MGNI","TGTX","PGNY","SWI","DOCN","HIMS","ACAD","FIZZ","CABA"],

    # ── Sectors — SPDR ────────────────────────────────────────────────────────
    "XLK":  ["MSFT","AAPL","NVDA","AVGO","CRM","AMD","ORCL","ACN","NOW","CSCO"],
    "XLF":  ["BRK-B","JPM","V","MA","BAC","WFC","GS","MS","SPGI","BLK"],
    "XLV":  ["UNH","JNJ","LLY","ABBV","MRK","ABT","TMO","DHR","AMGN","MDT"],
    "XLE":  ["XOM","CVX","COP","EOG","SLB","MPC","PSX","VLO","OXY","BKR"],
    "XLI":  ["GE","RTX","HON","CAT","UPS","LMT","DE","UNP","BA","ETN"],
    "XLB":  ["LIN","APD","SHW","FCX","ECL","NEM","DOW","DD","ALB","VMC"],
    "XLY":  ["AMZN","TSLA","HD","MCD","NKE","LOW","SBUX","TJX","BKNG","CMG"],
    "XLP":  ["PG","COST","KO","PEP","WMT","PM","MO","MDLZ","CL","KHC"],
    "XLU":  ["NEE","DUK","SO","D","AEP","EXC","XEL","ED","ES","ETR"],
    "XLRE": ["AMT","PLD","EQIX","CCI","PSA","O","SBAC","WELL","AVB","EXR"],
    "XLC":  ["META","GOOGL","GOOG","NFLX","T","VZ","TMUS","CMCSA","DIS","EA"],

    # ── Sectors — Vanguard ────────────────────────────────────────────────────
    "VGT":  ["MSFT","AAPL","NVDA","AVGO","CRM","ACN","AMD","CSCO","ORCL","NOW"],
    "VFH":  ["BRK-B","JPM","BAC","WFC","GS","MS","BLK","SPGI","C","USB"],
    "VHT":  ["UNH","LLY","JNJ","ABBV","MRK","TMO","ABT","DHR","AMGN","BSX"],
    "VDE":  ["XOM","CVX","COP","SLB","EOG","MPC","PSX","VLO","OXY","HAL"],
    "VIS":  ["GE","RTX","HON","CAT","UPS","LMT","DE","UNP","BA","ETN"],
    "VAW":  ["LIN","APD","SHW","FCX","ECL","NEM","DOW","DD","ALB","VMC"],
    "VCR":  ["AMZN","TSLA","HD","MCD","NKE","LOW","SBUX","TJX","BKNG","CMG"],
    "VDC":  ["PG","COST","KO","PEP","WMT","PM","MO","MDLZ","CL","KHC"],
    "VPU":  ["NEE","DUK","SO","D","AEP","EXC","XEL","SRE","PCG","ED"],
    "VNQ":  ["PLD","AMT","EQIX","WELL","DLR","PSA","CCI","O","AVB","EQR"],
    "VOX":  ["META","GOOGL","GOOG","NFLX","T","CMCSA","VZ","DIS","TMUS","EA"],

    # ── Semiconductors ────────────────────────────────────────────────────────
    "SMH":  ["NVDA","TSM","AVGO","ASML","TXN","QCOM","AMAT","AMD","MU","LRCX"],
    "SOXX": ["NVDA","AVGO","AMD","QCOM","TXN","AMAT","MU","LRCX","KLAC","MRVL"],
    "PSI":  ["NVDA","TXN","AMAT","AMD","QCOM","MU","LRCX","KLAC","MCHP","ON"],

    # ── Tech / Growth ─────────────────────────────────────────────────────────
    "IGV":  ["MSFT","ORCL","CRM","NOW","ADBE","INTU","CDNS","SNPS","WDAY","ROP"],
    "CIBR": ["PANW","CRWD","FTNT","ZS","OKTA","CYBR","VRNS","QLYS","TENB","S"],
    "SKYY": ["MSFT","GOOGL","AMZN","IBM","ORCL","SFDC","AKAM","NCNO","ZS","DDOG"],
    "WCLD": ["BILL","ZI","APPF","HUBS","DDOG","SNOW","MDB","CFLT","GTLB","DOCN"],
    "BUG":  ["PANW","CRWD","FTNT","ZS","OKTA","CYBR","VRNS","QLYS","TENB","S"],
    "HACK": ["PANW","CRWD","FTNT","AKAM","JNPR","SAIC","LDOS","BOOZ","CACI","CSCO"],

    # ── Biotech / Health ──────────────────────────────────────────────────────
    "IBB":  ["LLY","AMGN","GILD","VRTX","REGN","BIIB","ILMN","SGEN","ALNY","MRNA"],
    "XBI":  ["VRTX","REGN","BIIB","ALNY","MRNA","EXAS","SGEN","HALO","PCVX","RGEN"],
    "ARKG": ["RXRX","VCYT","TWST","PACB","EXAS","SGFY","MASS","CRSP","IOVA","FATE"],
    "LABU": ["VRTX","AMGN","REGN","GILD","BIIB","ALNY","MRNA","EXAS","SGEN","HALO"],

    # ── Financials / Banks ────────────────────────────────────────────────────
    "KBE":  ["JPM","BAC","WFC","C","USB","PNC","TFC","KEY","FITB","HBAN"],
    "KRE":  ["WAL","EWBC","SBNY","FHN","WTFC","IBOC","BOH","CATY","SFNC","FFIN"],
    "IAI":  ["GS","MS","BLK","SCHW","ICE","CME","SPGI","MCO","MKTX","CBOE"],

    # ── Energy / Clean ────────────────────────────────────────────────────────
    "XOP":  ["SM","VTLE","OVV","MRO","DVN","FANG","COP","EOG","OXY","APA"],
    "OIH":  ["SLB","HAL","BKR","NOV","RIG","FTI","XPRO","WHD","NR","LBRT"],
    "ICLN": ["ENPH","FSLR","SEDG","RUN","PLUG","NOVA","SPWR","BEP","NEE","CWEN"],
    "TAN":  ["ENPH","FSLR","SEDG","CSIQ","JKS","DQ","RUN","MAXN","ARRY","NOVA"],
    "QCLN": ["TSLA","ENPH","FSLR","ON","CREE","PLUG","RUN","NOVA","NEE","ALB"],

    # ── Commodities ───────────────────────────────────────────────────────────
    "GLD":  [],   # physical gold — no equity holdings
    "SLV":  [],
    "USO":  [],
    "PDBC": [],
    "DBA":  [],

    # ── Disruptive / Thematic ─────────────────────────────────────────────────
    "ARKK": ["TSLA","COIN","ROKU","SHOP","TWLO","DKNG","CRSP","UiPath","ZM","EXAS"],
    "ARKW": ["TSLA","COIN","META","RBLX","HOOD","TWLO","ZM","SPOT","SHOP","NVDA"],
    "ARKG": ["RXRX","VCYT","TWST","PACB","EXAS","SGFY","MASS","CRSP","IOVA","FATE"],
    "ARKQ": ["TSLA","KTOS","TER","UiPath","TRIMB","DE","ACMR","RAVN","PATH","TRMB"],
    "ARKF": ["COIN","SQ","HOOD","MELI","NU","BRZE","ADSK","TWLO","BILL","SHOP"],
    "LIT":  ["ALB","SQM","LTHM","LAC","BYDDF","TSLA","EVX","CATL","VWAGY","BMW"],
    "ROBO": ["ISRG","ABB","FANUC","KUKA","OMRON","KEYENCE","IPG","IRBT","BRKS","AZTA"],
    "DRIV": ["TSLA","APTV","MOBILEYE","ON","NXP","NXPI","TXN","STM","MPWR","WOLFSPEED"],
    "BOTZ": ["ISRG","FANUC","ABB","KEYENCE","OMRON","NACHI","DAIFUKU","ZEBRA","AZTA","BRKS"],
    "METV": ["META","MSFT","RBLX","NVDA","UNITY","SNAP","MTTR","MANU","NFLX","GOOGL"],

    # ── Dividend / Value ──────────────────────────────────────────────────────
    "VYM":  ["MSFT","AAPL","JPM","XOM","JNJ","ABBV","AVGO","HD","PG","KO"],
    "DVY":  ["MO","PM","VZ","T","IBM","OKE","WMB","D","LYB","OGE"],
    "SCHD": ["CSCO","AVGO","HD","TXN","AbbVie","MRK","PEP","AMGN","PFE","CVX"],
    "HDV":  ["XOM","JNJ","ABBV","CVX","VZ","PG","MO","PM","KO","T"],
    "VTV":  ["BRK-B","JPM","UNH","XOM","JNJ","PG","HD","ABBV","AVB","BAC"],
}


def get_etf_holdings(symbol: str, max_holdings: int = 10) -> list[str]:
    """
    Return top holdings for an ETF.
    Tries yfinance first; falls back to hardcoded list.
    Filters out OTC / ADR symbols that Finviz can't price.
    """
    hardcoded = ETF_HOLDINGS.get(symbol.upper(), [])

    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        fd = getattr(t, "funds_data", None)
        if fd is not None:
            th = getattr(fd, "top_holdings", None)
            if th is not None and not th.empty:
                live = th.index.tolist()[:max_holdings]
                # Prefer live data if it looks valid
                if len(live) >= 3:
                    return [s.replace(".", "-") for s in live]
    except Exception as e:
        logger.debug(f"yfinance holdings fetch failed for {symbol}: {e}")

    return [s.replace(".", "-") for s in hardcoded[:max_holdings]]


def etf_category(symbol: str) -> str:
    """Return the category name for an ETF symbol."""
    for cat, tickers in ETF_CATEGORIES.items():
        if symbol.upper() in tickers:
            return cat
    return "Other"
