"""Anlamsal tekilleştirme + flaş haber (embedding modeli ve ağ yok: sahte vektörler / sahte LLM)."""
import json

import numpy as np
import pytest

from bulten import flash
from bulten.config import load_settings
from bulten.dedup import STATUS_SENT, SeenStore, SentRecord, hash_key
from bulten.models import Item
from bulten.semantic import can_merge, clean_title, drop_recently_sent, semantic_merge, sent_vectors


def fake_embed(groups: dict[str, int]):
    """Başlığında anahtar kelime geçenleri aynı vektöre koyar → benzerlik 1.0; diğerleri birbirine dik."""
    def fn(texts):
        out = []
        for n, t in enumerate(texts):
            v = np.zeros(64, dtype=np.float32)
            g = next((g for kw, g in groups.items() if kw in t.lower()), None)
            v[g if g is not None else 32 + n % 32] = 1.0
            out.append(v)
        return np.array(out)
    return fn


def it(title, source, kind="news", **kw):
    return Item(title=title, url=f"https://x/{abs(hash((title, source)))}", source=source, kind=kind, **kw)


def test_can_merge_rules():
    # Sürüm numarası çelişiyorsa farklı haber
    assert not can_merge(it("Introducing Gemini 3.8 Flash", "GN"), it("Gemini 3.7 Flash is here", "HN", "community"))
    # Biri diğerinin sayılarını kapsıyorsa olabilir
    assert can_merge(it("Acme AI valuation hits $22B - Reuters", "GN"),
                     it("Acme AI doubles valuation to $22B with $300M tender", "HN", "community"))
    # Aynı blogun iki yazısı asla birleşmez; ama Google News'te aynı kaynak içi birleşebilir
    assert not can_merge(it("Agents guide A", "SomeBlog", "blog"), it("Agents guide B", "SomeBlog", "blog"))
    assert can_merge(it("Hitachi and Fanuc partner - A", "GN"), it("Hitachi, Fanuc partnership - B", "GN"))
    # Makale–makale hiç birleşmez (arXiv ID zaten yakalıyor)
    assert not can_merge(it("Paper", "arXiv", "paper"), it("Paper", "HF", "paper"))
    assert clean_title(it("Big news - The Verge", "GN")) == "Big news"


def test_semantic_merge_keeps_primary_source():
    fn = fake_embed({"argon": 0, "tabfm": 1})
    items = [
        it("Gemini Argon reaction thread", "r/ML", "community", popularity=500),
        it("Introducing Gemini Argon", "DeepMind", "lab", priority=True),
        it("TabFM: zero-shot tabular", "HF", "paper", summary="uzun özet " * 10),
        it("Introducing TabFM", "Google Research", "lab", priority=True),
        it("Unrelated", "HN", "community"),
    ]
    out = semantic_merge(items, 0.86, fn)
    assert len(out) == 3
    argon = next(i for i in out if "Argon" in i.title)
    assert argon.source == "DeepMind" and argon.also_on == ["r/ML"] and argon.popularity == 500
    tab = next(i for i in out if "TabFM" in i.title)
    assert tab.source == "Google Research" and tab.summary.startswith("uzun özet")  # daha zengin özet alınır


def test_drop_recently_sent():
    fn = fake_embed({"argon": 0})
    sent = it("Introducing Gemini Argon", "DeepMind", "lab")
    (vec, nums), = sent_vectors([sent], fn)
    history = [SentRecord(vec, nums, hash_key("DeepMind"), "lab")]
    items = [it("Gemini Argon: hands-on review - Verge", "GN"), it("Something else", "GN")]
    assert [i.title for i in drop_recently_sent(items, history, 0.86, fn)] == ["Something else"]
    # Aynı blogun yeni yazısı, benzer olsa da tekrar sayılmaz
    assert drop_recently_sent([it("Gemini Argon deep dive", "DeepMind", "lab")], history, 0.86, fn)


def test_store_sent_metadata_and_quota(tmp_path):
    store = SeenStore(tmp_path / "s.json")
    a = it("A sufficiently long headline about a launch", "OpenAI", "lab")
    a.id = "url:a"
    store.add(a, STATUS_SENT, vec=np.ones(4), numbers=set())
    store.touch(a)
    assert store.is_sent(a)
    assert store.sent_count_on(store.today) == 1
    hist = store.recent_sent(7)
    assert hist[0].source_hash == hash_key("OpenAI") and hist[0].kind == "lab"


@pytest.fixture
def settings():
    return load_settings()


def test_flash_judge_and_render(settings):
    items = [it("Introducing GPT-7", "OpenAI", "lab"), it("Minor blog post", "OpenAI", "lab")]

    class FakeLLM:
        last_model = "fake"

        def chat(self, task, system, user, schema=None, temperature=0.3):
            assert task == "flash" and schema is flash.FLASH_SCHEMA and "{{context}}" not in system
            ps = json.loads(user)
            return json.dumps({"results": [
                {"id": ps[0]["id"], "score": 10, "headline": "OpenAI GPT-7'yi duyurdu", "text": "Büyük <haber>."},
                {"id": ps[1]["id"], "score": 3, "headline": "x", "text": "y"},
            ]})

    verdicts = flash.judge(items, FakeLLM(), settings, "ctx")
    assert [v.score for v in verdicts] == [10, 3]
    msg = flash.render_flash(verdicts[0])
    assert msg.startswith("⚡ <b>FLAŞ</b>") and "Büyük &lt;haber&gt;." in msg and 'href="https://x/' in msg


def test_flash_main_respects_quota_and_marks_sent(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(flash, "DATA_DIR", tmp_path)
    big = [it(f"Huge launch number {n} from a lab", "OpenAI", "lab") for n in range(5)]
    for n, b in enumerate(big):
        b.id = f"url:big{n}"
    monkeypatch.setattr(flash, "find_candidates", lambda s, store, daily: big)
    monkeypatch.setattr(flash, "load_context", lambda: "ctx")
    monkeypatch.setattr(flash, "judge", lambda items, llm, s, c: [flash.Verdict(i, 10, "H", "T") for i in items])
    monkeypatch.setattr(flash, "LLM", lambda s: None)
    sent = []

    class FakeTG:
        def send(self, blocks, silent=False):
            sent.append(blocks)

    monkeypatch.setattr(flash, "Telegram", FakeTG)
    monkeypatch.setattr("bulten.pipeline.embed_fn", lambda s: fake_embed({}))
    assert flash.main([]) == 0
    assert len(sent) == settings["flash"]["max_per_day"]  # kota: günde en fazla 3

    # Aynı gün ikinci çalışma: kota dolu → LLM çağrılmaz, hiçbir şey gönderilmez
    monkeypatch.setattr(flash, "judge", lambda *a: pytest.fail("kota doluyken LLM çağrılmamalı"))
    assert flash.main([]) == 0
    assert len(sent) == 3
    saved = json.loads((tmp_path / "flash_seen.json").read_text())
    assert sum(1 for v in saved["items"].values() if v.get("s") == "sent" and "e" in v) == 3


def test_secondary_sources_need_higher_score(settings):
    cfg = settings["flash"]
    lab = it("Introducing a new frontier model", "OpenAI", "lab")
    hn = it("Run a 125B model on a gaming GPU at 100 T/s", "Hacker News", "community", popularity=921)
    assert flash.required_score(lab, cfg) == 9
    assert flash.required_score(hn, cfg) == 10


def test_flash_chain_falls_back_to_score(settings):
    from bulten.llm import LLM

    free = LLM(settings, profile="free")
    assert free.chain("flash")[0]["reasoning_effort"] == "medium"   # ayrı, daha dikkatli zincir
    claude = LLM(settings, profile="claude")
    assert claude.chain("flash") == claude.chain("score")           # tanımlı değilse puanlama zinciri
