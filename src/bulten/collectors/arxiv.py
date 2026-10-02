from __future__ import annotations

import re

from ..textutil import clean_html
from .base import struct_to_dt
from .rss import RssCollector

_ANNOUNCE = re.compile(r"Announce Type:\s*([\w-]+)", re.I)
_ABSTRACT = re.compile(r"Abstract:\s*", re.I)


class ArxivCollector(RssCollector):
    """arXiv günlük RSS'i. Sadece yeni makaleler (new / cross) alınır, revizyonlar (replace) atlanır."""

    def collect(self):
        feed = self.fetch_feed()
        items = []
        for e in feed.entries:
            desc = e.get("summary", "")
            m = _ANNOUNCE.search(desc)
            if m and m.group(1).lower().startswith("replace"):
                continue
            parts = _ABSTRACT.split(desc, maxsplit=1)
            abstract = parts[1] if len(parts) == 2 else desc
            items.append(
                self.item(
                    title=clean_html(e.get("title", ""), 300),
                    url=e.get("link", ""),
                    summary=clean_html(abstract),
                    published=struct_to_dt(e.get("published_parsed")),
                )
            )
            if len(items) >= self.max_items:
                break
        return items
