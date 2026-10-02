"""LLM profillerini aynı girdi üzerinde karşılaştır.

    python -m bulten.compare                          # tüm profiller
    python -m bulten.compare --profiles free,claude   # seçili profiller
    python -m bulten.compare --reuse                  # önceki adayları tekrar kullan (toplama yok)
    python -m bulten.compare --telegram               # raporu ve her bülteni Telegram'a da gönder

Adil olsun diye:
  - Tüm profiller AYNI aday listesini alır (bir kez toplanır, out/compare/candidates.json'a yazılır).
  - Her profilin sadece zincirdeki İLK modelleri kullanılır (yedeğe düşme yok).
  - seen.json'a hiçbir şey yazılmaz.

Çıktı: out/compare/report.md (özet tablo, uyum analizi, seçimler) + her profil için <profil>.md
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import combinations

import numpy as np

from .config import (
    DATA_DIR, ROOT, load_context, load_dotenv, load_settings, load_sources, mask_private_in_logs,
)
from .dedup import SeenStore
from .llm import LLM, estimate_cost
from .models import Item
from .pipeline import filter_sources, gather_candidates, setup_logging
from .scorer import score_items, select
from .telegram import Telegram
from .writer import Newsletter, render_markdown, render_telegram, write_newsletter

log = logging.getLogger("bulten.compare")
OUT = ROOT / "out" / "compare"


@dataclass
class Result:
    profile: str
    score_model: str = "-"
    write_model: str = "-"
    ok: bool = False
    skipped: str = ""
    score_seconds: float = 0.0
    write_seconds: float = 0.0
    n_scored: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    fallback_writer: bool = False
    words: int = 0
    scores: dict[str, float] = field(default_factory=dict)          # item id → puan
    selected: dict[str, list[str]] = field(default_factory=dict)    # bölüm → item id listesi
    newsletter: Newsletter | None = None


# ---------------------------------------------------------------- aday önbelleği

def save_candidates(items: list[Item]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    data = []
    for it in items:
        d = asdict(it)
        d["published"] = it.published.isoformat() if it.published else None
        data.append(d)
    (OUT / "candidates.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def load_candidates() -> list[Item]:
    data = json.loads((OUT / "candidates.json").read_text(encoding="utf-8"))
    items = []
    for d in data:
        d["published"] = datetime.fromisoformat(d["published"]) if d["published"] else None
        items.append(Item(**d))
    return items


# ---------------------------------------------------------------- tek profil

def run_profile(profile: str, candidates: list[Item], settings: dict, context: str, today) -> Result:
    res = Result(profile)
    llm = LLM(settings, profile=profile, primary_only=True)
    res.score_model = llm.chains["score"][0]["model"]
    res.write_model = llm.chains["write"][0]["model"]
    missing = [t for t in ("score", "write") if not llm.available(t)]
    if missing:
        env = {llm.providers[llm.chains[t][0]["provider"]]["api_key_env"] for t in missing}
        res.skipped = f"API anahtarı yok: {', '.join(sorted(env))}"
        log.warning("%s atlandı — %s", profile, res.skipped)
        return res

    items = copy.deepcopy(candidates)
    log.info("━━ %s: puanlama (%s)…", profile, res.score_model)
    t0 = time.monotonic()
    scored = score_items(items, llm, settings, context)
    res.score_seconds = time.monotonic() - t0
    res.n_scored = len(scored)
    res.scores = {i.id: i.score for i in scored}
    selected = select(scored, settings)
    res.selected = {sec: [i.id for i in lst] for sec, lst in selected.items()}

    if not any(selected.values()):
        res.ok = res.n_scored > 0
        log.warning("%s: hiçbir öğe eşiği geçmedi", profile)
    else:
        log.info("━━ %s: yazım (%s)…", profile, res.write_model)
        t0 = time.monotonic()
        nl = write_newsletter(selected, llm, settings, context, today)
        res.write_seconds = time.monotonic() - t0
        nl.footer = f"Karşılaştırma · profil: {profile} · puanlayan: {res.score_model} · yazan: {res.write_model}"
        res.newsletter, res.fallback_writer, res.ok = nl, nl.fallback, True
        res.words = sum(len(p.text.split()) for _, ps in nl.sections for p in ps) + len(nl.intro.split())

    res.tokens_in = sum(u.input_tokens for u in llm.usage)
    res.tokens_out = sum(u.output_tokens for u in llm.usage)
    res.cost = estimate_cost(llm.usage, settings.get("pricing", {}))
    return res


# ---------------------------------------------------------------- analiz

def spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3 or np.std(a) == 0 or np.std(b) == 0:
        return None
    ra = np.argsort(np.argsort(a)).astype(float)
    rb = np.argsort(np.argsort(b)).astype(float)
    return float(np.corrcoef(ra, rb)[0, 1])


def agreement(r1: Result, r2: Result) -> tuple[float | None, float | None]:
    common = sorted(set(r1.scores) & set(r2.scores))
    rho = spearman([r1.scores[c] for c in common], [r2.scores[c] for c in common])
    s1 = {i for ids in r1.selected.values() for i in ids}
    s2 = {i for ids in r2.selected.values() for i in ids}
    jac = len(s1 & s2) / len(s1 | s2) if (s1 | s2) else None
    return rho, jac


def fmt(x: float | None, pct: bool = False) -> str:
    if x is None:
        return "–"
    return f"{x:.0%}" if pct else f"{x:.2f}"


def build_report(results: list[Result], candidates: list[Item], settings: dict) -> str:
    by_id = {c.id: c for c in candidates}
    ran = [r for r in results if r.ok]
    L = [f"# LLM Profil Karşılaştırması — {datetime.now():%Y-%m-%d %H:%M}", "",
         f"Aynı {len(candidates)} aday üzerinde, her profilin birincil modelleriyle.", "",
         "## Özet", "",
         "| Profil | Puanlama modeli | Yazım modeli | Puanlanan | Süre (puan + yazım) | Token (girdi/çıktı) "
         "| Tahmini maliyet (gün / ay) | Kelime | Not |",
         "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        if r.skipped:
            L.append(f"| {r.profile} | {r.score_model} | {r.write_model} | – | – | – | – | – | ⏭ {r.skipped} |")
            continue
        note = "⚠️ yazım yedeğe düştü" if r.fallback_writer else ("❌ başarısız" if not r.ok else "✓")
        L.append(
            f"| **{r.profile}** | {r.score_model} | {r.write_model} | {r.n_scored}/{len(candidates)} "
            f"| {r.score_seconds:.0f} + {r.write_seconds:.0f} sn | {r.tokens_in:,} / {r.tokens_out:,} "
            f"| ${r.cost:.3f} / ${r.cost * 30:.2f} | {r.words} | {note} |"
        )
    L += ["", "> Maliyet, önbellek indirimi olmadan hesaplanan üst sınırdır; ücretsiz kotadaki modeller $0 sayılır.", ""]

    if len(ran) >= 2:
        L += ["## Puanlama uyumu", "",
              "- **Spearman ρ**: iki profilin aynı öğelere verdiği puanların sıralama benzerliği (1 = aynı sıra)",
              "- **Seçim örtüşmesi**: bültene seçilen haberlerin Jaccard benzerliği", "",
              "| Profil A | Profil B | Spearman ρ | Seçim örtüşmesi |", "|---|---|---|---|"]
        for a, b in combinations(ran, 2):
            rho, jac = agreement(a, b)
            L.append(f"| {a.profile} | {b.profile} | {fmt(rho)} | {fmt(jac, pct=True)} |")
        L.append("")

    L += ["## Seçilen haberler", ""]
    for sec in settings["sections"]:
        L += [f"### {sec['emoji']} {sec['title']}", ""]
        all_ids: list[str] = []
        for r in ran:
            for i in r.selected.get(sec["id"], []):
                if i not in all_ids:
                    all_ids.append(i)
        if not all_ids:
            L += ["_(hiçbir profil seçmedi)_", ""]
            continue
        L.append("| Haber | " + " | ".join(r.profile for r in ran) + " |")
        L.append("|---|" + "---|" * len(ran))
        for i in all_ids:
            title = by_id[i].title[:80].replace("|", "/") if i in by_id else i
            cells = []
            for r in ran:
                mark = "✅ " if i in r.selected.get(sec["id"], []) else ""
                cells.append(f"{mark}{r.scores.get(i, 0):.0f}" if i in r.scores else "–")
            L.append(f"| [{title}]({by_id[i].url if i in by_id else ''}) | " + " | ".join(cells) + " |")
        L.append("")

    L += ["## Bültenler", ""]
    for r in ran:
        if r.newsletter:
            L.append(f"- [{r.profile}]({r.profile}.md)")
    L += ["", "Karar verirken: Türkçenin akıcılığı, haberlerin doğru anlatılması (uydurma var mı?), "
          "kişisel bölümlerdeki önerilerin somutluğu ve maliyet.", ""]
    return "\n".join(L)


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bulten.compare")
    ap.add_argument("--profiles", help="virgülle ayrılmış profil adları (varsayılan: hepsi)")
    ap.add_argument("--reuse", action="store_true", help="out/compare/candidates.json'u tekrar kullan")
    ap.add_argument("--telegram", action="store_true", help="her profilin bültenini Telegram'a gönder")
    ap.add_argument("--source", action="append", help="sadece adı bu metni içeren kaynaklar")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    load_dotenv()
    mask_private_in_logs()
    settings = load_settings()
    context = load_context()
    today = (datetime.now(timezone.utc) + timedelta(hours=settings["timezone_offset_hours"])).date()
    profiles = args.profiles.split(",") if args.profiles else list(settings["llm"]["profiles"])

    if args.reuse and (OUT / "candidates.json").exists():
        candidates = load_candidates()
        log.info("Önceki %d aday yüklendi", len(candidates))
    else:
        sources = filter_sources(load_sources(), args.source)
        # seen.json okunur ama asla kaydedilmez
        candidates = gather_candidates(
            settings, sources, SeenStore(DATA_DIR / "seen.json"), SeenStore(DATA_DIR / "flash_seen.json")
        ).candidates
        save_candidates(candidates)
    if not candidates:
        log.error("Karşılaştırılacak aday yok")
        return 1

    results = [run_profile(p.strip(), candidates, settings, context, today) for p in profiles]

    OUT.mkdir(parents=True, exist_ok=True)
    for r in results:
        if r.newsletter:
            (OUT / f"{r.profile}.md").write_text(render_markdown(r.newsletter), encoding="utf-8")
    report = build_report(results, candidates, settings)
    (OUT / "report.md").write_text(report, encoding="utf-8")
    print("\n" + report.split("## Seçilen haberler")[0])
    log.info("Rapor: %s", OUT / "report.md")

    if args.telegram:
        tg = Telegram()
        tg.send_document(OUT / "report.md", caption="🧪 LLM profil karşılaştırma raporu (tam tablo ve seçilen haberler)")
        for r in results:
            if r.newsletter:
                blocks = render_telegram(r.newsletter)
                blocks[0] = f"🧪 <b>Karşılaştırma: {r.profile}</b>\n\n" + blocks[0]
                tg.send(blocks, silent=True)
    return 0 if any(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
