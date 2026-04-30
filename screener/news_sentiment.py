"""VADER sentiment analysis on Polygon.io news articles."""

import logging
from typing import Optional
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

logger = logging.getLogger(__name__)
_analyzer = SentimentIntensityAnalyzer()


def score_articles(articles: list[dict]) -> Optional[dict]:
    """
    Run VADER on a list of Polygon news articles.

    Each article: {"title": str, "description": str, "published": str}

    Returns dict with:
        avg_compound, bullish_count, bearish_count, neutral_count,
        news_score (-2 to +2), article_count
    """
    if not articles:
        return None

    compounds = []
    bullish = bearish = neutral = 0

    for a in articles:
        text = f"{a.get('title', '')} {a.get('description', '')}".strip()
        if not text:
            continue

        scores = _analyzer.polarity_scores(text)
        c = scores["compound"]
        compounds.append(c)

        if c >= 0.05:
            bullish += 1
        elif c <= -0.05:
            bearish += 1
        else:
            neutral += 1

    if not compounds:
        return None

    avg = sum(compounds) / len(compounds)
    # Scale compound (-1 to +1) to score (-2 to +2)
    news_score = avg * 2

    return {
        "avg_compound":   round(avg, 3),
        "bullish_count":  bullish,
        "bearish_count":  bearish,
        "neutral_count":  neutral,
        "news_score":     round(news_score, 2),
        "article_count":  len(compounds),
    }
