"""
Lightweight rule-based sentiment scoring before LLM inference.
Uses keyword matching to score each headline -1.0 to 1.0.
"""

from typing import List, Literal
from models.schemas import NewsItem, NewsSentiment

BULLISH_KEYWORDS = {
    "surge": 0.8, "rally": 0.7, "gain": 0.6, "rise": 0.5, "up": 0.4,
    "growth": 0.6, "profit": 0.6, "beat": 0.7, "record": 0.6, "high": 0.4,
    "positive": 0.5, "strong": 0.5, "boost": 0.6, "bull": 0.7, "upside": 0.5,
    "recovery": 0.5, "outperform": 0.7, "upgrade": 0.6, "buy": 0.5,
    # ── Geopolitical de-escalation ────────────────────────────────────────────
    "ceasefire": 0.7, "peace deal": 0.8, "truce": 0.7, "diplomacy": 0.5,
    "negotiation": 0.4, "de-escalat": 0.7, "sanctions lifted": 0.8,
    "trade deal": 0.7, "treaty": 0.6, "resolution": 0.5, "stability": 0.5,
}

BEARISH_KEYWORDS = {
    "fall": -0.6, "drop": -0.6, "decline": -0.6, "loss": -0.7, "crash": -0.9,
    "plunge": -0.8, "sell": -0.5, "bear": -0.7, "downside": -0.5, "weak": -0.5,
    "concern": -0.4, "risk": -0.4, "warn": -0.6, "fear": -0.6, "cut": -0.5,
    "negative": -0.5, "below": -0.3, "miss": -0.6, "downgrade": -0.7,
    # ── Geopolitical / war keywords ───────────────────────────────────────────
    # Armed conflict & escalation
    "war": -0.9, "airstrike": -0.9, "missile": -0.8, "bomb": -0.8,
    "invasion": -0.9, "attack": -0.7, "conflict": -0.7, "escalat": -0.8,
    "ceasefire violation": -0.7, "offensive": -0.7, "troops": -0.6,
    "military action": -0.8, "nuclear": -0.9, "chemical weapon": -0.9,
    "drone strike": -0.8, "shelling": -0.8, "artillery": -0.7,
    # Sanctions & trade war
    "sanction": -0.7, "tariff": -0.6, "trade war": -0.8, "embargo": -0.7,
    "blockade": -0.7, "ban": -0.5, "restriction": -0.5, "levy": -0.4,
    "retaliatory": -0.7, "countermeasure": -0.6,
    # Political instability
    "coup": -0.9, "overthrow": -0.9, "assassination": -0.9, "protest": -0.5,
    "riot": -0.7, "unrest": -0.6, "crisis": -0.7, "instability": -0.6,
    "martial law": -0.8, "emergency": -0.6, "shutdown": -0.6,
    # Energy & supply chain shocks
    "oil shock": -0.8, "supply disruption": -0.7, "pipeline": -0.5,
    "strait": -0.6, "chokepoint": -0.7, "blockade": -0.7,
    # Macro / financial contagion
    "recession": -0.8, "default": -0.8, "sovereign debt": -0.7,
    "inflation surge": -0.7, "stagflation": -0.8, "currency crisis": -0.8,
    "banking crisis": -0.9, "contagion": -0.8, "liquidity crunch": -0.8,
}


def score_text(text: str) -> float:
    text_lower = text.lower()
    score = 0.0
    count = 0
    for word, weight in BULLISH_KEYWORDS.items():
        if word in text_lower:
            score += weight
            count += 1
    for word, weight in BEARISH_KEYWORDS.items():
        if word in text_lower:
            score += weight
            count += 1
    return round(score / max(count, 1), 3) if count else 0.0


def analyze_sentiment(news_items: List[NewsItem]) -> NewsSentiment:
    for item in news_items:
        item.sentiment_score = score_text(f"{item.title} {item.summary}")

    if not news_items:
        aggregate = 0.0
    else:
        aggregate = round(sum(i.sentiment_score for i in news_items) / len(news_items), 3)

    if aggregate > 0.2:
        label: Literal["BEARISH", "NEUTRAL", "BULLISH"] = "BULLISH"
    elif aggregate < -0.2:
        label = "BEARISH"
    else:
        label = "NEUTRAL"

    return NewsSentiment(items=news_items, aggregate_score=aggregate, label=label)


def format_news_for_prompt(sentiment: NewsSentiment, max_items: int = 5) -> str:
    lines = []
    for item in sentiment.items[:max_items]:
        sign = "+" if item.sentiment_score >= 0 else ""
        lines.append(f"- [{item.source}] {item.title} (score: {sign}{item.sentiment_score})")
    return "\n".join(lines) if lines else "No recent news available."
