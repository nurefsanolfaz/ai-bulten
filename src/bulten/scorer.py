"""LLM ile puanlama ve bölümlere göre seçim."""
from __future__ import annotations

import json
import logging

from .config import load_prompt
from .llm import LLM, LLMError, parse_json
from .models import Item

log = logging.getLogger(__name__)


def score_schema(section_ids: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "section": {"type": "string", "enum": [*section_ids, "none"]},
                        "score": {"type": "integer"},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "section", "score", "reason"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    }


def sections_block(sections: list[dict]) -> str:
    return "\n".join(f'- "{s["id"]}" — {s["title"]}: {s["description"]}' for s in sections)


def _item_payload(cid: str, item: Item) -> dict:
    signals = []
    if item.popularity:
        signals.append(f"popülerlik={item.popularity}")
    if item.also_on:
        signals.append("ayrıca: " + ", ".join(item.also_on))
    return {
        "id": cid,
        "source": item.source,
        "type": item.kind,
        "title": item.title,
        "summary": item.summary[:600],
        **({"signals": "; ".join(signals)} if signals else {}),
    }


def score_items(items: list[Item], llm: LLM, settings: dict, context: str) -> list[Item]:
    """Öğeleri puanlar; puanlanabilenleri döndürür (başarısız gruplar atlanır)."""
    sections = settings["sections"]
    valid_sections = {s["id"] for s in sections} | {"none"}
    system = (
        load_prompt("score.md")
        .replace("{{context}}", context.strip())
        .replace("{{sections}}", sections_block(sections))
    )
    batch_size = settings["llm"]["score_batch_size"]
    schema = score_schema([s["id"] for s in sections])
    scored: list[Item] = []

    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        ids = {f"c{start + n}": item for n, item in enumerate(batch)}
        user = json.dumps([_item_payload(cid, it) for cid, it in ids.items()], ensure_ascii=False, indent=1)
        try:
            raw = llm.chat("score", system, user, schema=schema, temperature=0.1)
            results = parse_json(raw).get("results", [])
        except (LLMError, ValueError) as e:
            log.warning("Puanlama grubu %d başarısız, atlanıyor: %s", start // batch_size + 1, e)
            continue

        for r in results:
            item = ids.get(str(r.get("id")))
            if item is None:
                continue
            try:
                item.score = float(r.get("score", 0))
            except (TypeError, ValueError):
                continue
            item.section = r.get("section") if r.get("section") in valid_sections else "none"
            item.reason = str(r.get("reason", ""))[:300]
            scored.append(item)
        log.info("Puanlama %d/%d (%s)", min(start + batch_size, len(items)), len(items), llm.last_model)

    return scored


def select(items: list[Item], settings: dict) -> dict[str, list[Item]]:
    min_score = settings["scoring"]["min_score"]
    out: dict[str, list[Item]] = {}
    for sec in settings["sections"]:
        pool = [i for i in items if i.section == sec["id"] and i.score >= min_score]
        pool.sort(key=lambda i: (i.score, i.prefilter_score), reverse=True)
        out[sec["id"]] = pool[: sec["count"]]
    return out
