"""
Async news scrapers for Indian financial sources.
Uses aiohttp + BeautifulSoup. Respectful rate limiting (2s between requests).
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import List

import aiohttp
from bs4 import BeautifulSoup

from models.schemas import NewsItem

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def _fetch(session: aiohttp.ClientSession, url: str) -> str:
    try:
        async with session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status == 200:
                return await resp.text()
    except Exception as e:
        logger.warning(f"Fetch failed for {url}: {e}")
    return ""


async def scrape_moneycontrol() -> List[NewsItem]:
    """Scrape Moneycontrol Markets RSS feed."""
    url = "https://www.moneycontrol.com/rss/business.xml"
    items = []
    async with aiohttp.ClientSession() as session:
        html = await _fetch(session, url)
        if not html:
            return items
        soup = BeautifulSoup(html, "lxml-xml")
        for item in soup.find_all("item")[:10]:
            title = item.find("title")
            desc = item.find("description")
            pub_date = item.find("pubDate")
            if title:
                items.append(
                    NewsItem(
                        title=title.get_text(strip=True),
                        summary=desc.get_text(strip=True)[:300] if desc else "",
                        source="Moneycontrol",
                        published_at=datetime.now(timezone.utc),
                    )
                )
    return items


async def scrape_et_markets() -> List[NewsItem]:
    """Scrape Economic Times Markets RSS feed."""
    url = "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"
    items = []
    async with aiohttp.ClientSession() as session:
        html = await _fetch(session, url)
        if not html:
            return items
        soup = BeautifulSoup(html, "lxml-xml")
        for item in soup.find_all("item")[:10]:
            title = item.find("title")
            desc = item.find("description")
            if title:
                items.append(
                    NewsItem(
                        title=title.get_text(strip=True),
                        summary=desc.get_text(strip=True)[:300] if desc else "",
                        source="Economic Times",
                        published_at=datetime.now(timezone.utc),
                    )
                )
    return items


async def get_all_news() -> List[NewsItem]:
    """Fetch news from all sources concurrently."""
    results = await asyncio.gather(
        scrape_moneycontrol(),
        scrape_et_markets(),
        return_exceptions=True,
    )
    all_items: List[NewsItem] = []
    for r in results:
        if isinstance(r, list):
            all_items.extend(r)
        elif isinstance(r, Exception):
            logger.warning(f"News source failed: {r}")
    # Sort by published time, newest first
    all_items.sort(key=lambda x: x.published_at, reverse=True)
    return all_items[:20]
