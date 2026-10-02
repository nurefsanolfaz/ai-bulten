from __future__ import annotations

import feedparser

from ..textutil import clean_html
from .base import Collector, struct_to_dt


class RssCollector(Collector):
    """RSS/Atom: lab blogları, bültenler, Reddit, Google News."""

    def fetch_feed(self):
        resp = self.client.get(self.source["url"])
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        if feed.bozo and not feed.entries:
            raise ValueError(f"feed okunamadı: {feed.bozo_exception}")
        return feed

    def collect(self):
        feed = self.fetch_feed()
        items = []
        for e in feed.entries[: self.max_items]:
            link = e.get("link")
            title = clean_html(e.get("title", ""), 300)
            if not link or not title:
                continue
            summary = e.get("summary") or ""
            if not summary and e.get("content"):
                summary = e.content[0].get("value", "")
            items.append(
                self.item(
                    title=title,
                    url=link,
                    summary=clean_html(summary),
                    published=struct_to_dt(e.get("published_parsed") or e.get("updated_parsed")),
                )
            )
        return items
