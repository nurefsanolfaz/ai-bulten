from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ..textutil import clean_html
from .base import Collector

_NUM = re.compile(r"[\d,]+")


class GithubTrendingCollector(Collector):
    """github.com/trending sayfası (resmi API yok, HTML okunur)."""

    def collect(self):
        resp = self.client.get(self.source["url"])
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        items = []
        for row in soup.select("article.Box-row"):
            link = row.select_one("h2 a")
            if not link or not link.get("href"):
                continue
            repo = link["href"].strip("/")
            desc_el = row.select_one("p")
            desc = clean_html(desc_el.get_text(" ") if desc_el else "", 500)
            lang_el = row.select_one('[itemprop="programmingLanguage"]')
            stars_today = 0
            for span in row.select("span"):
                if "stars today" in span.get_text():
                    m = _NUM.search(span.get_text())
                    stars_today = int(m.group().replace(",", "")) if m else 0
            summary = f"{desc} (GitHub'da bugün {stars_today} yıldız"
            summary += f", dil: {lang_el.get_text(strip=True)})" if lang_el else ")"
            items.append(
                self.item(
                    title=f"{repo} — GitHub'da trend",
                    url=f"https://github.com/{repo}",
                    summary=summary,
                    popularity=stars_today,
                )
            )
        return items[: self.max_items]
