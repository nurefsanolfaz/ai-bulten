"""Pipeline'ın LLM öncesi ortak kısmı: topla → birleştir → tekilleştir → ön filtre."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import collectors
from .collectors.base import make_client
from .collectors.scrape import enrich
from .dedup import STATUS_SENT, SeenStore, filter_new, merge_duplicates
from .models import Item
from .prefilter import ai_gate, embed, prefilter
from .semantic import drop_recently_sent, semantic_merge, sent_vectors

log = logging.getLogger("bulten")


@dataclass
class Gathered:
    n_items: int
    candidates: list[Item]
    errors: dict[str, str]
    new_items: list[Item] = field(default_factory=list)  # o gün ilk kez görülen tüm öğeler (arşiv için)


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpx2", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def filter_sources(sources: list[dict], names: list[str] | None) -> list[dict]:
    if not names:
        return sources
    return [s for s in sources if any(f.lower() in s["name"].lower() for f in names)]


def embed_fn(settings: dict):
    model = settings["prefilter"]["embedding_model"]
    return lambda texts: embed(texts, model)


def mark_sent(store: SeenStore, items: list[Item], settings: dict) -> None:
    """Gönderilenleri kaydet: başlık yerine embedding vektörü saklanır (repo herkese açık)."""
    for item, (vec, nums) in zip(items, sent_vectors(items, embed_fn(settings))):
        store.add(item, STATUS_SENT, vec=vec, numbers=nums)


def dedupe_semantically(items: list[Item], settings: dict, history_stores: list[SeenStore]) -> list[Item]:
    """Aynı çalışmadaki anlamca aynı haberleri birleştir, son günlerde gönderilmişleri ele."""
    cfg = settings["prefilter"]["semantic_dedup"]
    fn = embed_fn(settings)
    items = semantic_merge(items, cfg["threshold"], fn)
    history = [h for s in history_stores for h in s.recent_sent(cfg["history_days"])]
    return drop_recently_sent(items, history, cfg["threshold"], fn)


def gather_candidates(
    settings: dict, sources: list[dict], store: SeenStore, flash_store: SeenStore | None = None
) -> Gathered:
    client = make_client()
    raw, errors = collectors.collect_all(sources, client)
    items = merge_duplicates(raw)
    log.info("Toplam %d öğe (%d kopya birleştirildi), %d kaynak hatalı", len(items), len(raw) - len(items), len(errors))

    scrape_names = {s["name"] for s in sources if s["type"] == "scrape"}
    new = filter_new(items, store, scrape_names, settings["lookback_hours"])
    if flash_store:  # gün içinde flaş haber olarak gönderilenler bültende tekrar etmesin
        new = [i for i in new if not flash_store.is_sent(i)]
    log.info("Yeni öğe: %d", len(new))

    to_enrich = [i for i in new if i.source in scrape_names]
    if to_enrich:
        enrich(to_enrich[:30], client)

    gated = {s["name"] for s in sources if s.get("require_ai_keywords")}
    new = ai_gate(new, gated, settings["prefilter"]["ai_keywords"])
    new = dedupe_semantically(new, settings, [store] + ([flash_store] if flash_store else []))
    return Gathered(len(items), prefilter(new, settings), errors, new)
