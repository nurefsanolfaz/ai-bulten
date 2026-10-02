"""Ön filtre: yüzlerce öğeyi LLM'e gidecek birkaç düzine adaya indir.

Her bölüm için skor = embedding benzerliği (bölüm açıklaması ↔ öğe)
                    + anahtar kelime bonusu + popülerlik bonusu
Her bölümün en iyi N adayı alınır; priority=high kaynaklar doğrudan geçer.
"""
from __future__ import annotations

import logging
import math
import os

import numpy as np

from .config import ROOT
from .models import Item
from .textutil import has_any_keyword, keyword_hits

log = logging.getLogger(__name__)

_model = None


def _embedder(model_name: str):
    global _model
    if _model is None:
        from fastembed import TextEmbedding  # ağır import; sadece gerektiğinde

        cache = os.environ.get("FASTEMBED_CACHE_PATH", str(ROOT / ".cache" / "fastembed"))
        _model = TextEmbedding(model_name=model_name, cache_dir=cache)
    return _model


def embed(texts: list[str], model_name: str) -> np.ndarray:
    vecs = np.array(list(_embedder(model_name).embed(texts, batch_size=64)), dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    return vecs / np.clip(norms, 1e-9, None)


def ai_gate(items: list[Item], gated_sources: set[str], ai_keywords: list[str]) -> list[Item]:
    """Genel kaynaklarda (HN, GitHub Trending…) AI ile ilgisiz öğeleri ele."""
    return [
        i for i in items
        if i.source not in gated_sources or has_any_keyword(f"{i.title} {i.summary}", ai_keywords)
    ]


def popularity_bonus(item: Item, weight: float) -> float:
    return weight * math.log10(1 + item.popularity) + 0.03 * len(item.also_on)


def prefilter(items: list[Item], settings: dict) -> list[Item]:
    cfg = settings["prefilter"]
    sections = settings["sections"]
    if not items:
        return []

    sec_vecs = embed([s["description"] for s in sections], cfg["embedding_model"])
    item_vecs = embed([i.text_for_embedding() for i in items], cfg["embedding_model"])
    sims = item_vecs @ sec_vecs.T  # (n_items, n_sections)

    kw_weight = cfg["keyword_weight"]
    for row, item in zip(sims, items):
        text = f"{item.title} {item.summary}"
        for sec, sim in zip(sections, row):
            hits = min(keyword_hits(text, sec["keywords"]), 3)
            bonus = popularity_bonus(item, sec.get("popularity_weight", 0.04))
            item.section_scores[sec["id"]] = float(sim) + kw_weight * hits + bonus
        item.prefilter_score = max(item.section_scores.values())

    chosen: dict[int, Item] = {id(i): i for i in items if i.priority}
    n_priority = len(chosen)
    for sec in sections:
        ranked = sorted(items, key=lambda i: i.section_scores[sec["id"]], reverse=True)
        for item in ranked[: cfg["per_section"]]:
            chosen.setdefault(id(item), item)

    rest = sorted((i for i in chosen.values() if not i.priority), key=lambda i: i.prefilter_score, reverse=True)
    budget = max(cfg["max_candidates"] - n_priority, 0)
    result = [i for i in chosen.values() if i.priority] + rest[:budget]
    log.info("Ön filtre: %d öğe → %d aday (%d öncelikli kaynak)", len(items), len(result), n_priority)
    return result
