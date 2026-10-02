from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from ..models import Item
from ..textutil import clean_html
from .base import Collector

log = logging.getLogger(__name__)


def _slug_title(url: str) -> str:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    return slug.replace("-", " ").strip().capitalize()


class ScrapeCollector(Collector):
    """RSS'i olmayan sayfalar: link_pattern'e uyan linkleri toplar.

    Tarih bilgisi olmadığı için "yeni" olup olmadığına dedup katmanı karar verir
    (ilk çalışmada mevcut linkler baseline olarak kaydedilir).
    """

    def collect(self):
        resp = self.client.get(self.source["url"])
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        pattern = re.compile(self.source["link_pattern"])
        exclude = re.compile(self.source["exclude_pattern"]) if self.source.get("exclude_pattern") else None

        found: dict[str, str] = {}
        for a in soup.find_all("a", href=True):
            href = a["href"].split("#")[0].split("?")[0]
            if not pattern.search(href) or (exclude and exclude.search(href)):
                continue
            url = urljoin(self.source["url"], href)
            text = clean_html(a.get_text(" "), 200)
            # Aynı linke birden çok <a> olabilir; en anlamlı (en uzun ama makul) metni tut
            if url not in found or (len(text) > len(found[url]) and len(text) < 160):
                found[url] = text

        items = []
        for url, text in list(found.items())[: self.max_items]:
            items.append(self.item(title=text if len(text) >= 12 else _slug_title(url), url=url))
        return items


def enrich(items: list[Item], client: httpx.Client) -> None:
    """Yeni bulunan scrape öğeleri için sayfanın og:title / og:description bilgisini çek."""
    for item in items:
        try:
            resp = client.get(item.url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            log.info("enrich başarısız %s: %s", item.url, e)
            continue
        soup = BeautifulSoup(resp.text, "html.parser")

        def meta(*names: str) -> str:
            for n in names:
                tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
                if tag and tag.get("content"):
                    return clean_html(tag["content"], 800)
            return ""

        title = meta("og:title", "twitter:title") or (clean_html(soup.title.string, 300) if soup.title else "")
        if title:
            item.title = title
        item.summary = meta("og:description", "description", "twitter:description") or item.summary
