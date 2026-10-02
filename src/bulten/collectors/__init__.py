from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import httpx

from ..models import Item
from .arxiv import ArxivCollector
from .base import Collector
from .github_trending import GithubTrendingCollector
from .hackernews import HackerNewsCollector
from .hf_papers import HfPapersCollector
from .rss import RssCollector
from .scrape import ScrapeCollector

log = logging.getLogger(__name__)

REGISTRY: dict[str, type[Collector]] = {
    "rss": RssCollector,
    "arxiv": ArxivCollector,
    "hf_papers": HfPapersCollector,
    "hackernews": HackerNewsCollector,
    "scrape": ScrapeCollector,
    "github_trending": GithubTrendingCollector,
}


def collect_all(sources: list[dict], client: httpx.Client) -> tuple[list[Item], dict[str, str]]:
    """Tüm kaynakları paralel topla. Çöken kaynak bülteni durdurmaz; hatası raporlanır."""
    errors: dict[str, str] = {}

    def run(src: dict) -> list[Item]:
        cls = REGISTRY.get(src["type"])
        if cls is None:
            errors[src["name"]] = f"bilinmeyen tip: {src['type']}"
            return []
        try:
            items = cls(src, client).collect()
            log.info("%-45s %4d öğe", src["name"], len(items))
            return items
        except Exception as e:  # noqa: BLE001 — tek kaynak hatası tüm çalışmayı düşürmesin
            log.warning("%-45s HATA: %s", src["name"], e)
            errors[src["name"]] = str(e)[:200]
            return []

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run, sources))
    return [item for batch in results for item in batch], errors
