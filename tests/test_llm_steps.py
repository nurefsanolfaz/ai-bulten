"""Puanlama ve yazım adımları — sahte LLM ile (API anahtarı gerekmez)."""
import json
from datetime import date

from bulten.config import load_settings
from bulten.llm import LLMError
from bulten.models import Item
from bulten.scorer import score_items, select
from bulten.writer import render_markdown, render_telegram, write_newsletter


class FakeLLM:
    def __init__(self, responder):
        self.responder = responder
        self.last_model = "fake/model"

    def chat(self, task, system, user, schema=None, temperature=0.3):
        assert schema is not None
        return self.responder(task, system, user)


def _items(n):
    return [Item(title=f"Item {k} <script>", url=f"https://ex.com/{k}?a=1&b=2", source="Src") for k in range(n)]


def test_score_and_select():
    settings = load_settings()

    ids = [s["id"] for s in settings["sections"]]

    def responder(task, system, user):
        assert "{{context}}" not in system
        payload = json.loads(user)
        labels = [*ids, "bogus"]
        return json.dumps({"results": [
            {"id": p["id"], "section": labels[i % len(labels)], "score": 10 - i, "reason": "r"}
            for i, p in enumerate(payload)
        ]})

    items = _items(14)
    scored = score_items(items, FakeLLM(responder), settings, "bağlam")
    assert len(scored) == 14
    assert {i.section for i in scored} == {*ids, "none"}
    sel = select(scored, settings)
    assert all(i.score >= settings["scoring"]["min_score"] for v in sel.values() for i in v)
    assert sel["work"][0].score >= sel["work"][-1].score


def test_score_batch_failure_is_skipped():
    settings = load_settings()

    def responder(task, system, user):
        raise LLMError("down")

    assert score_items(_items(3), FakeLLM(responder), settings, "ctx") == []


def test_write_uses_only_known_ids_and_escapes_html():
    settings = load_settings()
    a, b = _items(2)
    selected = {"work": [a], "ai": [b], "applied": []}

    def responder(task, system, user):
        return json.dumps({
            "intro": "Günaydın & hoş geldin",
            "sections": [
                {"id": "ai", "paragraphs": [{"item_ids": ["ai1"], "headline": "H<2>", "text": "Metin 2"}]},
                {"id": "work", "paragraphs": [
                    {"item_ids": ["work1"], "headline": "H1", "text": "Metin 1"},
                    {"item_ids": ["uydurma9"], "headline": "Uydurma", "text": "Kaynaksız paragraf"},
                ]},
            ],
            "closing": "Son",
        })

    nl = write_newsletter(selected, FakeLLM(responder), settings, "ctx", date(2026, 9, 28))
    assert [s["id"] for s, _ in nl.sections] == ["work", "ai"]  # settings sırası
    assert all(p.headline != "Uydurma" for _, ps in nl.sections for p in ps)
    html_out = "\n".join(render_telegram(nl))
    assert "&lt;script&gt;" not in html_out  # başlık paragraf başlığı olarak değil, link etiketi kaynak adı
    assert "H&lt;2&gt;" in html_out and "Günaydın &amp; hoş geldin" in html_out
    assert 'href="https://ex.com/0?a=1&amp;b=2"' in html_out
    assert "28 Eylül 2026, Pazartesi" in html_out
    assert "[Src](https://ex.com/0?a=1&b=2)" in render_markdown(nl)


def test_write_falls_back_when_llm_fails():
    settings = load_settings()
    a = _items(1)[0]
    a.reason = "gerekçe"

    def responder(task, system, user):
        raise LLMError("down")

    nl = write_newsletter({"work": [a]}, FakeLLM(responder), settings, "ctx", date(2026, 9, 28))
    assert nl.sections and nl.sections[0][1][0].text == "gerekçe"


def test_telegram_layout_is_scannable_and_well_formed():
    import re
    from bulten.writer import Newsletter, Paragraph

    settings = load_settings()
    s0, s1 = settings["sections"][:2]
    a, b, c = _items(3)
    nl = Newsletter(day=date(2026, 10, 6), intro="Giriş", closing="Kapanış", footer="27 kaynak")
    nl.sections = [(s0, [Paragraph("Bir", "metin bir", [a]), Paragraph("İki", "metin iki", [b])]),
                   (s1, [Paragraph("Üç", "metin üç", [c])])]
    blocks = render_telegram(nl)
    text = "\n\n".join(blocks)
    assert "📌 <b>Bugün:</b>" in blocks[0] and f"{s0['emoji']} {s0['title']} <b>2</b>" in blocks[0]
    assert "<b>1. Bir</b>" in text and "<b>2. İki</b>" in text and "<b>1. Üç</b>" in text  # bölüm içinde numara
    assert text.count("<blockquote expandable>") == 3
    assert s0["title"].upper() in blocks[1] and "<b>2. İki</b>" not in blocks[1]  # bölüm başlığı ilk haberde
    for tag in ("b", "i", "blockquote", "code", "a"):  # Telegram açık kalan etiketi reddeder
        assert len(re.findall(rf"<{tag}[ >]", text)) == text.count(f"</{tag}>"), tag
