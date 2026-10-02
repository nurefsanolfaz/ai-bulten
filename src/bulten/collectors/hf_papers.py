from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..textutil import clean_html
from .base import Collector, parse_iso


class HfPapersCollector(Collector):
    """Hugging Face Daily Papers — topluluğun öne çıkardığı makaleler (upvote = popülerlik)."""

    def collect(self):
        today = datetime.now(timezone.utc).date()
        items = []
        for back in range(self.source.get("days", 3)):
            day = today - timedelta(days=back)
            resp = self.client.get(self.source["url"], params={"date": day.isoformat(), "limit": 100})
            resp.raise_for_status()
            for entry in resp.json():
                paper = entry.get("paper", {})
                pid = paper.get("id")
                if not pid:
                    continue
                summary = clean_html(paper.get("summary", ""))
                if paper.get("githubRepo"):
                    summary += f" [Kod: {paper['githubRepo']}]"
                items.append(
                    self.item(
                        title=clean_html(paper.get("title") or entry.get("title", ""), 300),
                        url=f"https://huggingface.co/papers/{pid}",
                        summary=summary,
                        published=parse_iso(paper.get("submittedOnDailyAt") or entry.get("publishedAt")),
                        popularity=int(paper.get("upvotes") or 0),
                    )
                )
        items.sort(key=lambda i: i.popularity, reverse=True)
        return items[: self.max_items]
