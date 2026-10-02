"""LLM ile Türkçe bülten yazımı ve Telegram HTML / Markdown çıktısı."""
from __future__ import annotations

import html
import json
import logging
from dataclasses import dataclass, field
from datetime import date

from .config import load_prompt
from .llm import LLM, LLMError, parse_json
from .models import Item

log = logging.getLogger(__name__)

AYLAR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
         "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
GUNLER = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]


WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "intro": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "paragraphs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "item_ids": {"type": "array", "items": {"type": "string"}},
                                "headline": {"type": "string"},
                                "text": {"type": "string"},
                            },
                            "required": ["item_ids", "headline", "text"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["id", "paragraphs"],
                "additionalProperties": False,
            },
        },
        "closing": {"type": "string"},
    },
    "required": ["intro", "sections", "closing"],
    "additionalProperties": False,
}


def turkish_date(d: date) -> str:
    return f"{d.day} {AYLAR[d.month - 1]} {d.year}, {GUNLER[d.weekday()]}"


@dataclass
class Paragraph:
    headline: str
    text: str
    items: list[Item]


@dataclass
class Newsletter:
    day: date
    intro: str = ""
    sections: list[tuple[dict, list[Paragraph]]] = field(default_factory=list)
    closing: str = ""
    footer: str = ""
    fallback: bool = False      # True → LLM yazamadı, sade liste kullanıldı

    @property
    def items(self) -> list[Item]:
        return [it for _, paras in self.sections for p in paras for it in p.items]


def _fallback(day: date, selected: dict[str, list[Item]], sections: list[dict]) -> Newsletter:
    """LLM yazımı başarısız olursa: başlık + puanlama gerekçesinden oluşan sade bülten."""
    nl = Newsletter(
        day=day,
        intro="Bugün bülten yazarı modele ulaşılamadı; öne çıkanları kısa liste olarak bırakıyorum.",
        fallback=True,
    )
    for sec in sections:
        paras = [Paragraph(i.title, i.reason, [i]) for i in selected.get(sec["id"], [])]
        if paras:
            nl.sections.append((sec, paras))
    return nl


def write_newsletter(
    selected: dict[str, list[Item]], llm: LLM, settings: dict, context: str, day: date
) -> Newsletter:
    sections = settings["sections"]
    ids: dict[str, Item] = {}
    payload = []
    for sec in sections:
        entries = []
        for n, item in enumerate(selected.get(sec["id"], []), 1):
            iid = f"{sec['id']}{n}"
            ids[iid] = item
            entries.append({
                "id": iid,
                "title": item.title,
                "source": ", ".join([item.source, *item.also_on]),
                "type": item.kind,
                "summary": item.summary[:900],
                "editor_note": item.reason,
            })
        if entries:
            payload.append({"id": sec["id"], "title": sec["title"], "items": entries})

    system = load_prompt("write.md").replace("{{context}}", context.strip())
    user = (
        f"Tarih: {turkish_date(day)}\n\nBugünün seçilmiş haberleri (bölümlere göre):\n"
        + json.dumps(payload, ensure_ascii=False, indent=1)
    )
    try:
        data = parse_json(llm.chat("write", system, user, schema=WRITE_SCHEMA, temperature=0.6))
    except (LLMError, ValueError) as e:
        log.error("Bülten yazımı başarısız, yedek formata geçiliyor: %s", e)
        return _fallback(day, selected, sections)

    nl = Newsletter(day=day, intro=str(data.get("intro", "")).strip(), closing=str(data.get("closing", "")).strip())
    by_id = {s["id"]: s for s in sections}
    used: set[str] = set()
    for sec_data in data.get("sections", []):
        sec = by_id.get(sec_data.get("id"))
        if not sec:
            continue
        paras = []
        for p in sec_data.get("paragraphs", []):
            items = [ids[i] for i in p.get("item_ids", []) if i in ids and i not in used]
            text = str(p.get("text", "")).strip()
            if not items or not text:
                continue  # kaynağı olmayan paragraf = olası uydurma → alma
            used.update(i for i in p["item_ids"] if i in ids)
            paras.append(Paragraph(str(p.get("headline", "")).strip() or items[0].title, text, items))
        if paras:
            nl.sections.append((sec, paras))

    if not nl.sections:
        log.error("Model geçerli paragraf üretmedi, yedek formata geçiliyor")
        return _fallback(day, selected, sections)
    # Bölüm sırasını settings'teki sıraya sabitle
    order = {s["id"]: n for n, s in enumerate(sections)}
    nl.sections.sort(key=lambda sp: order[sp[0]["id"]])
    return nl


# ---------------------------------------------------------------- render

def _e(s: str) -> str:
    return html.escape(s, quote=False)


def _links_html(items: list[Item]) -> str:
    return " · ".join(f'<a href="{html.escape(i.url)}">{_e(i.source)}</a>' for i in items)


def render_telegram(nl: Newsletter) -> list[str]:
    """Telegram HTML blokları. Mesaj bölme blok sınırlarında yapılır."""
    blocks = [f"<b>🗞 AI Bülteni</b> — {_e(turkish_date(nl.day))}"]
    if nl.intro:
        blocks[0] += f"\n\n<i>{_e(nl.intro)}</i>"
    for sec, paras in nl.sections:
        first = True
        for p in paras:
            body = f"<b>{_e(p.headline)}</b>\n{_e(p.text)}\n🔗 {_links_html(p.items)}"
            if first:
                body = f"<b>{sec['emoji']} {_e(sec['title'].upper())}</b>\n\n" + body
                first = False
            blocks.append(body)
    tail = []
    if nl.closing:
        tail.append(f"💬 {_e(nl.closing)}")
    if nl.footer:
        tail.append(f"<i>{_e(nl.footer)}</i>")
    if tail:
        blocks.append("\n\n".join(tail))
    return blocks


def render_markdown(nl: Newsletter) -> str:
    lines = [f"# AI Bülteni — {turkish_date(nl.day)}", ""]
    if nl.intro:
        lines += [f"_{nl.intro}_", ""]
    for sec, paras in nl.sections:
        lines += [f"## {sec['emoji']} {sec['title']}", ""]
        for p in paras:
            links = " · ".join(f"[{i.source}]({i.url})" for i in p.items)
            lines += [f"**{p.headline}**", "", p.text, "", f"🔗 {links}", ""]
    if nl.closing:
        lines += ["---", "", nl.closing, ""]
    if nl.footer:
        lines += [f"<sub>{nl.footer}</sub>", ""]
    return "\n".join(lines)


def render_quiet(day: date, footer: str) -> list[str]:
    return [
        f"<b>🗞 AI Bülteni</b> — {_e(turkish_date(day))}\n\n"
        "Bugün kriterlerine uyan kayda değer yeni bir gelişme bulamadım. Yarın görüşmek üzere! ☕"
        + (f"\n\n<i>{_e(footer)}</i>" if footer else "")
    ]
