"""
Crowd sentiment — Reddit (r/wallstreetbets, r/stocks, r/investing).
Falls back gracefully; no API key required for read-only access.
"""

import logging
import re
import requests
from typing import Optional

logger = logging.getLogger(__name__)

SUBREDDITS = ["wallstreetbets", "stocks", "options", "investing"]

# Simple positive/negative word lists
_BULL_WORDS = re.compile(r"\b(buy|bull|calls?|long|moon|bullish|breakout|upside|rally|squeeze|gains?)\b", re.I)
_BEAR_WORDS  = re.compile(r"\b(sell|bear|puts?|short|crash|bearish|breakdown|downside|dump|loss|drop)\b", re.I)


def _reddit_search(ticker: str, subreddit: str, limit: int = 25) -> list[str]:
    """Fetch post titles mentioning ticker from Reddit JSON API (no auth needed)."""
    url = f"https://www.reddit.com/r/{subreddit}/search.json"
    try:
        r = requests.get(
            url,
            params={"q": ticker, "restrict_sr": 1, "limit": limit, "sort": "new"},
            headers={"User-Agent": "options-screener/1.0"},
            timeout=8,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        return [p["data"]["title"] for p in data.get("data", {}).get("children", [])]
    except Exception:
        return []


def get_sentiment(symbol: str) -> Optional[dict]:
    """
    Score crowd sentiment for a ticker using Reddit post titles.

    Returns dict with:
        bullish_count, bearish_count, total_with_sentiment,
        bullish_pct, sentiment_score (-2 to +2),
        watchers (post count used as proxy)
    """
    all_titles: list[str] = []
    for sub in SUBREDDITS:
        titles = _reddit_search(symbol, sub, limit=15)
        all_titles.extend(titles)

    if not all_titles:
        logger.warning(f"Reddit: no posts found for {symbol}")
        return None

    bullish = sum(1 for t in all_titles if _BULL_WORDS.search(t))
    bearish = sum(1 for t in all_titles if _BEAR_WORDS.search(t))
    total_sentiment = bullish + bearish

    if total_sentiment > 0:
        bullish_pct = bullish / total_sentiment * 100
    else:
        bullish_pct = 50.0

    # Map 0–100% → -2 to +2
    sentiment_score = max(-2.0, min(2.0, (bullish_pct - 50) / 25))

    return {
        "bullish_count":        bullish,
        "bearish_count":        bearish,
        "total_with_sentiment": total_sentiment,
        "bullish_pct":          round(bullish_pct, 1),
        "sentiment_score":      round(sentiment_score, 2),
        "watchers":             len(all_titles),   # total posts found
    }
