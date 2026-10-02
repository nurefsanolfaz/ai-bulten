from __future__ import annotations

from datetime import datetime, timezone
from time import struct_time
from calendar import timegm

import httpx

from ..models import Item

USER_AGENT = "Mozilla/5.0 (compatible; ai-bulten/0.1; +https://github.com/)"


def make_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
    )


def struct_to_dt(t: struct_time | None) -> datetime | None:
    if not t:
        return None
    return datetime.fromtimestamp(timegm(t), tz=timezone.utc)


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


class Collector:
    """Her kaynak tipi için bir alt sınıf. `collect` hata fırlatabilir; pipeline yakalar."""

    def __init__(self, source: dict, client: httpx.Client):
        self.source = source
        self.client = client
        self.name: str = source["name"]
        self.max_items: int = source.get("max_items", 50)

    def collect(self) -> list[Item]:
        raise NotImplementedError

    def item(self, **kwargs) -> Item:
        kwargs.setdefault("source", self.name)
        kwargs.setdefault("kind", self.source.get("kind", "news"))
        kwargs.setdefault("priority", self.source.get("priority") == "high")
        return Item(**kwargs)
