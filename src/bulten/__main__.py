"""Günlük bülten pipeline'ı.

    python -m bulten              # tam çalışma: topla → filtrele → puanla → yaz → Telegram → kaydet
    python -m bulten --dry-run    # durumu kaydetmez; bülteni ekrana yazar (GitHub Actions'ta: Telegram'a önizleme)
    python -m bulten --collect-only   # sadece kaynakları test et (LLM çağrısı yok)
    python -m bulten --profile claude # bu çalışma için başka LLM profili kullan
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

from .config import (
    ARCHIVE_DIR, DATA_DIR, ROOT, in_github_actions, load_context, load_dotenv, load_settings, load_sources,
    mask_private_in_logs,
)
from .dedup import STATUS_SCORED, SeenStore
from .llm import LLM, estimate_cost
from .pipeline import filter_sources, gather_candidates, mark_sent, setup_logging
from .scorer import score_items, select
from .telegram import Telegram
from .writer import render_markdown, render_quiet, render_telegram, write_newsletter

log = logging.getLogger("bulten")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bulten")
    ap.add_argument("--dry-run", action="store_true",
                    help="durumu kaydetme; bülteni ekrana yaz (GitHub Actions'ta Telegram'a önizleme olarak gönder)")
    ap.add_argument("--collect-only", action="store_true", help="sadece toplama + ön filtre raporu")
    ap.add_argument("--profile", help="LLM profili (settings.yaml → llm.profiles)")
    ap.add_argument("--source", action="append", help="sadece adı bu metni içeren kaynaklar (tekrarlanabilir)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    load_dotenv()
    mask_private_in_logs()

    settings = load_settings()
    sources = filter_sources(load_sources(), args.source)
    context = load_context()
    local_now = datetime.now(timezone.utc) + timedelta(hours=settings["timezone_offset_hours"])
    today = local_now.date()

    # 1) Topla, tekilleştir, ön filtre
    store = SeenStore(DATA_DIR / "seen.json")
    flash_store = SeenStore(DATA_DIR / "flash_seen.json")  # sadece okunur: flaşta gönderilenler tekrar etmesin
    gathered = gather_candidates(settings, sources, store, flash_store)
    candidates = gathered.candidates

    if args.collect_only:
        _report(candidates)
        return 0

    # 2) Puanla + seç
    llm = LLM(settings, profile=args.profile)
    log.info("LLM profili: %s", llm.profile)
    scored = score_items(candidates, llm, settings, context)
    if candidates and not scored:
        log.error("Hiçbir öğe puanlanamadı (LLM erişilemiyor?)")
        _notify_failure(args, "Bugün LLM puanlaması tamamen başarısız oldu; bülten gönderilemedi. Logları kontrol et.")
        return 1
    for item in scored:
        store.add(item, STATUS_SCORED)
    selected = select(scored, settings)

    # 3) Yaz
    optional = {s["name"] for s in sources if s.get("optional")}
    footer = _footer(gathered.n_items, len(sources), {k: v for k, v in gathered.errors.items() if k not in optional})
    n_selected = sum(len(v) for v in selected.values())
    nl = None
    if n_selected:
        nl = write_newsletter(selected, llm, settings, context, today)
        nl.footer = footer + (f" · yazan: {llm.last_model}" if llm.last_model else "")
        blocks = render_telegram(nl)
        markdown = render_markdown(nl)
    else:
        blocks = render_quiet(today, footer)
        markdown = f"# AI Bülteni — {today}\n\nBugün kayda değer yeni gelişme yok.\n"

    cost = estimate_cost(llm.usage, settings.get("pricing", {}))
    tokens = sum(u.input_tokens + u.output_tokens for u in llm.usage)
    log.info("LLM kullanımı: %d çağrı, %d token, tahmini $%.4f", len(llm.usage), tokens, cost)

    out_dir = ROOT / "out"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "latest.md").write_text(markdown, encoding="utf-8")
    (out_dir / "latest.html").write_text("\n\n".join(blocks).replace("\n", "<br>\n"), encoding="utf-8")

    # 4) Gönder + kaydet
    if args.dry_run:
        if in_github_actions():
            # Public repoda loglar herkese açık → kişisel bülteni loga değil, Telegram'a önizleme olarak gönder
            blocks[0] = "🧪 <b>ÖNİZLEME (dry-run)</b> — durum kaydedilmedi\n\n" + blocks[0]
            Telegram().send(blocks, silent=True)
            log.info("Dry-run: önizleme Telegram'a gönderildi, seen.json değiştirilmedi")
        else:
            print("\n" + "=" * 70 + "\n" + markdown)
            log.info("Dry-run: Telegram'a gönderilmedi, seen.json değiştirilmedi. Çıktı: out/latest.md")
        return 0

    quiet = settings.get("quiet_hours", [0, 7])  # bu saatler arasında bildirim sesi çalmasın
    silent = quiet[0] <= local_now.hour < quiet[1]
    n_msgs = Telegram().send(blocks, silent=silent)
    log.info("Telegram'a %d mesaj gönderildi", n_msgs)
    if nl:
        mark_sent(store, nl.items, settings)
    if settings.get("archive"):
        ARCHIVE_DIR.mkdir(exist_ok=True)
        (ARCHIVE_DIR / f"{today.isoformat()}.md").write_text(markdown, encoding="utf-8")
    pruned = store.prune(settings["seen_retention_days"])
    store.save()
    log.info("seen.json kaydedildi (%d kayıt, %d eski kayıt silindi)", len(store.items), pruned)
    return 0


def _footer(n_items: int, n_sources: int, errors: dict[str, str]) -> str:
    text = f"{n_sources} kaynaktan {n_items} öğe tarandı"
    if errors:
        text += f" · ⚠️ okunamayan: {', '.join(sorted(errors))}"
    return text


def _report(candidates) -> None:
    print(f"\n{len(candidates)} aday:\n")
    for i in sorted(candidates, key=lambda x: x.prefilter_score, reverse=True):
        best = max(i.section_scores, key=i.section_scores.get) if i.section_scores else "-"
        flag = "★" if i.priority else " "
        print(f"{flag} {i.prefilter_score:.3f} [{best:4}] {i.source[:22]:22} {i.title[:90]}")
    print("\nKaynak dağılımı:", dict(Counter(i.source for i in candidates).most_common()))


def _notify_failure(args, message: str) -> None:
    if args.dry_run:
        return
    try:
        Telegram().send([f"⚠️ <b>AI Bülteni</b>\n{message}"])
    except Exception as e:  # noqa: BLE001
        log.error("Hata bildirimi de gönderilemedi: %s", e)


if __name__ == "__main__":
    sys.exit(main())
