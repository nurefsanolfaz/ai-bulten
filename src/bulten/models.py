from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Item:
    """Tüm kaynaklardan gelen öğelerin ortak modeli."""

    title: str
    url: str
    source: str                         # kaynak adı (sources.yaml → name)
    kind: str = "news"                  # paper | lab | news | blog | community | code
    summary: str = ""
    published: datetime | None = None   # timezone-aware UTC; bilinmiyorsa None
    priority: bool = False              # True → ön filtreyi atlar
    popularity: int = 0                 # HN puanı, HF upvote vb.
    also_on: list[str] = field(default_factory=list)  # aynı öğenin göründüğü diğer kaynaklar

    # pipeline tarafından doldurulur
    id: str = ""
    prefilter_score: float = 0.0
    section_scores: dict[str, float] = field(default_factory=dict)
    score: float = 0.0
    section: str = ""
    reason: str = ""

    def text_for_embedding(self) -> str:
        return f"{self.title}. {self.summary[:500]}"
