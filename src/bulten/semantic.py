"""Anlamsal tekilleştirme: aynı haberin farklı başlıklı kopyalarını birleştir / tekrar gönderme.

Embedding benzerliği tek başına yetmez — gerçek veride "Gemini 3.8 Flash" ile "Gemini 3.7 Flash"
0.97, "Mistral OCR 4" ile "OCR 3" 0.96 benzerlik verdi. Bu yüzden benzerliğe iki koruma eklenir:
  1. Başlıklardaki sayılar (sürüm, sayı numarası) çelişiyorsa → farklı haber.
  2. Aynı kaynaktan iki öğe → farklı haber (bir blogun iki yazısı kopya değildir).
     İstisna: haber toplayıcıları (kind=news, Google News) — orada aynı haber farklı yayıncılardan gelir.
Makale–makale çiftleri hiç birleştirilmez; onları arXiv ID'si zaten yakalar.
"""
from __future__ import annotations

import logging
import re
from typing import Callable

import numpy as np

from .dedup import SentRecord, _merge, hash_key
from .models import Item

log = logging.getLogger(__name__)

EmbedFn = Callable[[list[str]], np.ndarray]  # normalize edilmiş vektörler döndürür

_NUM = re.compile(r"\d+(?:[.,]\d+)*")
# Birleştirmede hangi öğe "ana" kalsın: birincil kaynak önce
KIND_RANK = {"lab": 0, "blog": 1, "paper": 2, "news": 3, "code": 4, "community": 5}


def clean_title(item: Item) -> str:
    title = item.title
    if item.kind == "news" and " - " in title:
        title = title.rsplit(" - ", 1)[0]  # Google News: "Başlık - Yayıncı"
    return title.strip()


def title_numbers(item: Item) -> set[str]:
    return set(_NUM.findall(clean_title(item)))


def _compatible(kind_a: str, src_a: str, nums_a: set[str], kind_b: str, src_b: str, nums_b: set[str]) -> bool:
    if kind_a == "paper" and kind_b == "paper":
        return False
    if src_a == src_b and kind_a != "news":
        return False
    # Sayılar çelişmemeli: {3.8} vs {3.7} → farklı haber; {22} vs {22, 300} → biri diğerini kapsıyor, olabilir
    return nums_a <= nums_b or nums_b <= nums_a


def can_merge(a: Item, b: Item) -> bool:
    return _compatible(a.kind, a.source, title_numbers(a), b.kind, b.source, title_numbers(b))


def _rank(item: Item) -> tuple:
    return (not item.priority, KIND_RANK.get(item.kind, 9), -len(item.summary))


def semantic_merge(items: list[Item], threshold: float, embed_fn: EmbedFn) -> list[Item]:
    """Aynı çalışmadaki anlamca aynı haberleri birleştir (en birincil kaynak ana öğe olur)."""
    if len(items) < 2:
        return items
    order = sorted(items, key=_rank)
    vecs = embed_fn([clean_title(i) for i in order])
    sims = vecs @ vecs.T
    absorbed: set[int] = set()
    out: list[Item] = []
    for a in range(len(order)):
        if a in absorbed:
            continue
        for b in np.nonzero(sims[a, a + 1 :] >= threshold)[0] + a + 1:
            if b not in absorbed and can_merge(order[a], order[b]):
                log.debug("Anlamsal kopya (%.2f): %s ⇐ %s", sims[a, b], order[a].title, order[b].title)
                _merge(order[a], order[b])
                absorbed.add(int(b))
        out.append(order[a])
    if absorbed:
        log.info("Anlamsal tekilleştirme: %d kopya birleştirildi", len(absorbed))
    return out


def drop_recently_sent(
    items: list[Item], history: list[SentRecord], threshold: float, embed_fn: EmbedFn
) -> list[Item]:
    """Son günlerde (farklı başlıkla) zaten gönderilmiş haberleri ele."""
    if not items or not history:
        return items
    sims = embed_fn([clean_title(i) for i in items]) @ np.stack([h.vec for h in history]).T
    out, dropped = [], 0
    for n, item in enumerate(items):
        src, nums = hash_key(item.source), title_numbers(item)
        if any(
            sims[n, m] >= threshold and _compatible(item.kind, src, nums, h.kind, h.source_hash, h.numbers)
            for m, h in enumerate(history)
        ):
            dropped += 1
            log.debug("Daha önce gönderilmiş haberin tekrarı atlandı: %s", item.title)
        else:
            out.append(item)
    if dropped:
        log.info("Daha önce gönderilmiş %d haberin farklı başlıklı tekrarı atlandı", dropped)
    return out


def sent_vectors(items: list[Item], embed_fn: EmbedFn) -> list[tuple[np.ndarray, set[str]]]:
    """Gönderilen öğeler için durum dosyasına yazılacak (vektör, sayılar) çiftleri."""
    if not items:
        return []
    vecs = embed_fn([clean_title(i) for i in items])
    return [(v, title_numbers(i)) for v, i in zip(vecs, items)]
