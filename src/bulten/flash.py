"""Flaş haber: saatlik kontrol, gerçekten büyük gelişmeleri günlük bülteni beklemeden bildir.

    python -m bulten.flash             # kontrol et, flaş varsa Telegram'a gönder
    python -m bulten.flash --dry-run   # gönderme, durumu kaydetme; ne gönderileceğini yazdır

Durum `data/flash_seen.json` dosyasında tutulur (günlük bültenin `seen.json`'undan ayrı, böylece iki
workflow aynı dosyaya yazıp çakışmaz). İki taraf da diğerinin dosyasını okur:
  - Flaş, günlük bültende zaten yer almış haberleri göndermez.
  - Günlük bülten, gün içinde flaş olarak gönderilmiş haberleri tekrar etmez.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import collectors
from .collectors.base import make_client
from .collectors.scrape import enrich
from .config import (
    DATA_DIR, in_github_actions, load_context, load_dotenv, load_prompt, load_settings, load_sources,
    mask_private_in_logs,
)
from .dedup import STATUS_SCORED, SeenStore, filter_new, merge_duplicates
from .llm import LLM, LLMError, parse_json
from .models import Item
from .pipeline import embed_fn, mark_sent, setup_logging
from .prefilter import ai_gate
from .semantic import drop_recently_sent, semantic_merge
from .telegram import Telegram

log = logging.getLogger("bulten.flash")

FLASH_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "score": {"type": "integer"},
                    "headline": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["id", "score", "headline", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


@dataclass
class Verdict:
    item: Item
    score: float
    headline: str
    text: str


def judge(items: list[Item], llm: LLM, settings: dict, context: str) -> list[Verdict]:
    """LLM ile flaş değerlendirmesi. Başarısız grup atlanır (o öğeler bir sonraki saat tekrar denenir)."""
    system = load_prompt("flash.md").replace("{{context}}", context.strip())
    size = settings["llm"]["score_batch_size"]
    verdicts: list[Verdict] = []
    for start in range(0, len(items), size):
        ids = {f"f{start + n}": it for n, it in enumerate(items[start : start + size])}
        payload = [
            {"id": cid, "source": ", ".join([it.source, *it.also_on]), "title": it.title,
             "summary": it.summary[:700], **({"popularity": it.popularity} if it.popularity else {})}
            for cid, it in ids.items()
        ]
        try:
            raw = llm.chat("score", system, json.dumps(payload, ensure_ascii=False, indent=1),
                           schema=FLASH_SCHEMA, temperature=0.1)
            results = parse_json(raw).get("results", [])
        except (LLMError, ValueError) as e:
            log.warning("Flaş değerlendirmesi başarısız, atlanıyor: %s", e)
            continue
        for r in results:
            if (item := ids.get(str(r.get("id")))) is None:
                continue
            try:
                score = float(r.get("score", 0))
            except (TypeError, ValueError):
                continue
            verdicts.append(Verdict(item, score, str(r.get("headline", "")).strip(), str(r.get("text", "")).strip()))
    return verdicts


def render_flash(v: Verdict) -> str:
    link = f'<a href="{html.escape(v.item.url)}">{html.escape(v.item.source, quote=False)}</a>'
    extra = f" (ayrıca: {html.escape(', '.join(v.item.also_on), quote=False)})" if v.item.also_on else ""
    return (
        f"⚡ <b>FLAŞ</b> — <b>{html.escape(v.headline or v.item.title, quote=False)}</b>\n\n"
        f"{html.escape(v.text, quote=False)}\n\n🔗 {link}{extra}"
    )


def find_candidates(settings: dict, store: SeenStore, daily: SeenStore) -> list[Item]:
    cfg = settings["flash"]
    sources = [s for s in load_sources() if s["name"] in set(cfg["sources"])]
    client = make_client()
    raw, _errors = collectors.collect_all(sources, client)
    items = merge_duplicates(raw)

    scrape_names = {s["name"] for s in sources if s["type"] == "scrape"}
    new = filter_new(items, store, scrape_names, cfg["lookback_hours"])
    new = [i for i in new if not daily.is_seen(i)]  # günlük bültende zaten ele alınmış
    mins = cfg.get("min_popularity", {})
    new = [i for i in new if i.popularity >= mins.get(i.source, 0)]
    gated = {s["name"] for s in sources if s.get("require_ai_keywords")}
    new = ai_gate(new, gated, settings["prefilter"]["ai_keywords"])
    if not new:
        return []

    to_enrich = [i for i in new if i.source in scrape_names]
    if to_enrich:
        enrich(to_enrich[:10], client)
    dd = settings["prefilter"]["semantic_dedup"]
    fn = embed_fn(settings)
    new = semantic_merge(new, dd["threshold"], fn)
    history = store.recent_sent(dd["history_days"]) + daily.recent_sent(dd["history_days"])
    return drop_recently_sent(new, history, dd["threshold"], fn)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bulten.flash")
    ap.add_argument("--dry-run", action="store_true", help="Telegram'a gönderme, durumu kaydetme")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    load_dotenv()
    mask_private_in_logs()
    settings = load_settings()
    cfg = settings["flash"]
    store = SeenStore(DATA_DIR / "flash_seen.json")
    daily = SeenStore(DATA_DIR / "seen.json")  # sadece okunur

    candidates = find_candidates(settings, store, daily)
    quota = cfg["max_per_day"] - store.sent_count_on(store.today)
    log.info("Flaş adayı: %d · bugünkü kalan kota: %d", len(candidates), max(quota, 0))

    alerts: list[Verdict] = []
    if candidates and quota > 0:
        llm = LLM(settings)
        verdicts = judge(candidates, llm, settings, load_context())
        public_log = in_github_actions()  # public repo logunda hangi haberlere bakıldığı görünmesin
        for v in verdicts:
            log.info("  %2.0f/10  %s", v.score, "(başlık gizli)" if public_log else v.item.title[:90])
            store.add(v.item, STATUS_SCORED)  # bir kez değerlendirilen tekrar sorulmaz
        alerts = sorted((v for v in verdicts if v.score >= cfg["min_score"]), key=lambda v: -v.score)[:quota]

    if args.dry_run:
        for v in alerts:
            if in_github_actions():  # public log yerine Telegram'a önizleme
                Telegram().send(["🧪 <b>ÖNİZLEME (dry-run)</b>\n\n" + render_flash(v)], silent=True)
            else:
                print("\n" + render_flash(v))
        log.info("Dry-run: %d flaş gönderilecekti; durum kaydedilmedi", len(alerts))
        return 0

    if alerts:
        local_hour = (datetime.now(timezone.utc) + timedelta(hours=settings["timezone_offset_hours"])).hour
        quiet = settings.get("quiet_hours", [0, 7])
        tg = Telegram()
        for v in alerts:
            tg.send([render_flash(v)], silent=quiet[0] <= local_hour < quiet[1])
            mark_sent(store, [v.item], settings)
            log.info("⚡ Flaş gönderildi (%.0f/10)", v.score)
    store.prune(settings["seen_retention_days"])
    store.save()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
