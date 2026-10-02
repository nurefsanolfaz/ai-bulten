from bulten.llm import parse_json
from bulten.telegram import pack
from bulten.textutil import clean_html, has_any_keyword, keyword_hits


def test_keywords_respect_word_boundaries():
    assert not has_any_keyword("Send an email with HTML", ["ai", "ml"])
    assert has_any_keyword("New AI model released", ["ai"])
    assert keyword_hits("Image segmentation for drone imagery", ["image segmentation", "drone", "lidar"]) == 2


def test_clean_html():
    assert clean_html("<p>Hello&nbsp;<b>world</b></p>\n\n") == "Hello world"


def test_parse_json_tolerates_fences_and_preamble():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Tabii, işte sonuç: {"a": [1, 2]} umarım yardımcı olur') == {"a": [1, 2]}


def test_pack_respects_limit_and_keeps_order():
    blocks = [f"blok {n} " + "x" * 900 for n in range(10)]
    msgs = pack(blocks, limit=2000)
    assert all(len(m) <= 2000 for m in msgs)
    joined = "\n\n".join(msgs)
    assert [joined.index(f"blok {n} ") for n in range(10)] == sorted(joined.index(f"blok {n} ") for n in range(10))


def test_pack_splits_single_huge_block():
    msgs = pack(["satır\n" * 2000], limit=1000)
    assert len(msgs) > 1 and all(len(m) <= 1000 for m in msgs)


def test_telegram_silent_flag_reaches_api():
    import json as _json

    import httpx

    from bulten.telegram import Telegram

    sent = []

    def handler(request):
        sent.append(_json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    tg = Telegram(token="t", chat_id="1", client=httpx.Client(transport=httpx.MockTransport(handler)))
    tg.send(["merhaba"], silent=True)
    tg.send(["merhaba"])
    assert [p["disable_notification"] for p in sent] == [True, False]
