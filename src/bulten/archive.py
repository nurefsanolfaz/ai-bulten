"""Günlük arşiv: o gün ilk kez görülen tüm öğeler (+ LLM puanları) ve gönderilen bülten.

Dosyalar `out/arsiv/` altına yazılır; GitHub Actions bunları ayrı, GİZLİ arşiv reposuna commit eder
(public bot reposuna değil — puanlar ve gönderilenler okurun ilgi alanlarını ele verir).

    out/arsiv/veri/YYYY/MM/YYYY-MM-DD.jsonl.gz
    out/arsiv/bultenler/YYYY/YYYY-MM-DD.md
"""
from __future__ import annotations

import gzip
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

from .models import Item

log = logging.getLogger(__name__)


def record(item: Item, candidate_ids: set[int], sent_ids: set[int]) -> dict:
    is_candidate = id(item) in candidate_ids
    best = max(item.section_scores, key=item.section_scores.get) if item.section_scores else None
    return {
        "id": item.id,
        "title": item.title,
        "url": item.url,
        "source": item.source,
        "kind": item.kind,
        "published": item.published.isoformat() if item.published else None,
        "summary": item.summary,
        "popularity": item.popularity,
        "also_on": item.also_on,
        "prefilter_score": round(item.prefilter_score, 4) if item.section_scores else None,
        "prefilter_section": best,
        "candidate": is_candidate,
        # Aday olup LLM'den yanıt alınamayanlarda section boş kalır → puan da yok sayılır
        "llm_score": item.score if (is_candidate and item.section) else None,
        "llm_section": (item.section or None) if is_candidate else None,
        "llm_reason": (item.reason or None) if is_candidate else None,
        "sent": id(item) in sent_ids,
    }


def write_daily(
    out_dir: Path,
    day: date,
    items: list[Item],
    candidates: list[Item],
    sent: list[Item],
    markdown: str | None,
) -> list[Path]:
    """Günün arşiv dosyalarını yaz; yazılan yolları döndür."""
    candidate_ids, sent_ids = {id(i) for i in candidates}, {id(i) for i in sent}
    collected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data_path = out_dir / "veri" / f"{day:%Y}" / f"{day:%m}" / f"{day.isoformat()}.jsonl.gz"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(data_path, "wt", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps({**record(item, candidate_ids, sent_ids), "collected_at": collected_at},
                               ensure_ascii=False) + "\n")
    written = [data_path]
    if markdown:
        md_path = out_dir / "bultenler" / f"{day:%Y}" / f"{day.isoformat()}.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(markdown, encoding="utf-8")
        written.append(md_path)
    log.info("Arşiv: %d öğe (%d aday, %d gönderilen) yazıldı", len(items), len(candidates), len(sent))
    return written
