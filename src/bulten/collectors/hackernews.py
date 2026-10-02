from __future__ import annotations

import time
from datetime import datetime, timezone

from ..textutil import clean_html
from .base import Collector


class HackerNewsCollector(Collector):
    """Hacker News (Algolia API): son 36 saatte belli puanı geçmiş hikâyeler."""

    def collect(self):
        since = int(time.time()) - 36 * 3600
        resp = self.client.get(
            self.source["url"],
            params={
                "tags": "story",
                "numericFilters": f"created_at_i>{since},points>={self.source.get('min_points', 40)}",
                "hitsPerPage": min(self.max_items, 1000),
            },
        )
        resp.raise_for_status()
        items = []
        for hit in resp.json().get("hits", []):
            hn_url = f"https://news.ycombinator.com/item?id={hit['objectID']}"
            points, comments = hit.get("points") or 0, hit.get("num_comments") or 0
            summary = f"Hacker News'te {points} puan, {comments} yorum. Tartışma: {hn_url}"
            if hit.get("story_text"):
                summary += " — " + clean_html(hit["story_text"], 800)
            items.append(
                self.item(
                    title=clean_html(hit.get("title", ""), 300),
                    url=hit.get("url") or hn_url,
                    summary=summary,
                    published=datetime.fromtimestamp(hit["created_at_i"], tz=timezone.utc),
                    popularity=points,
                )
            )
        return items
