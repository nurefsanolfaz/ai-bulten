from datetime import datetime, timedelta, timezone

from bulten.dedup import (
    STATUS_SCORED, STATUS_SENT, SeenStore, canonical_id, filter_new, merge_duplicates, normalize_url,
)
from bulten.models import Item

import numpy as np


def test_arxiv_ids_unify_across_sites():
    urls = [
        "https://arxiv.org/abs/2609.30415",
        "https://arxiv.org/abs/2609.30415v2",
        "https://arxiv.org/pdf/2609.30415v1",
        "https://huggingface.co/papers/2609.30415",
    ]
    assert {canonical_id(Item(title="x", url=u, source="s")) for u in urls} == {"arxiv:2609.30415"}


def test_normalize_url_strips_tracking_and_www():
    a = normalize_url("https://www.Example.com/post/?utm_source=x&id=3#top")
    b = normalize_url("http://example.com/post?id=3")
    assert a == b == "example.com/post?id=3"


def test_merge_duplicates_combines_signals():
    items = [
        Item(title="Great Speech Paper About Things", url="https://arxiv.org/abs/2609.00001", source="arXiv"),
        Item(title="Great Speech Paper About Things", url="https://huggingface.co/papers/2609.00001",
             source="HF", popularity=90, summary="longer summary here"),
        Item(title="Great Speech Paper About Things", url="https://blog.example.com/p", source="HN", popularity=120),
    ]
    out = merge_duplicates(items)
    assert len(out) == 1
    assert out[0].url.startswith("https://arxiv.org")
    assert out[0].popularity == 120
    assert out[0].also_on == ["HF", "HN"]
    assert out[0].summary == "longer summary here"


def test_seen_store_roundtrip_and_sent_is_sticky(tmp_path):
    store = SeenStore(tmp_path / "seen.json")
    it = Item(title="A sufficiently long title for hashing", url="https://x.com/a", source="s")
    it.id = canonical_id(it)
    store.add(it, STATUS_SENT, vec=np.ones(4), numbers=set())
    store.add(it, STATUS_SCORED)  # gönderildi bilgisi düşmemeli
    store.save()

    again = SeenStore(tmp_path / "seen.json")
    assert again.is_sent(it)
    # Farklı URL ama aynı başlık → yine görülmüş sayılır
    dup = Item(title="A sufficiently long title for hashing", url="https://y.com/b", source="t")
    dup.id = canonical_id(dup)
    assert again.is_seen(dup)


def test_prune_removes_old(tmp_path):
    store = SeenStore(tmp_path / "seen.json")
    store.items = {"old": {"d": "2000-01-01", "s": "scored"}, "new": {"d": store.today, "s": "sent"}}
    assert store.prune(90) == 1
    assert list(store.items) == ["new"]


def test_filter_new_bootstraps_scrape_sources_and_drops_old(tmp_path):
    store = SeenStore(tmp_path / "seen.json")
    now = datetime.now(timezone.utc)
    items = merge_duplicates([
        Item(title="Existing lab post on the page", url="https://lab.ai/news/a", source="Lab"),
        Item(title="Fresh paper from arxiv today", url="https://arxiv.org/abs/2609.11111", source="arXiv",
             published=now),
        Item(title="Ancient news from long ago", url="https://news.com/old", source="News",
             published=now - timedelta(days=10)),
    ])
    new = filter_new(items, store, {"Lab"}, lookback_hours=48)
    assert [i.source for i in new] == ["arXiv"]
    assert store.is_bootstrapped("Lab")

    # İkinci çalışma: lab sayfasında yeni bir link belirdi → artık gelmeli
    items2 = merge_duplicates([
        Item(title="Existing lab post on the page", url="https://lab.ai/news/a", source="Lab"),
        Item(title="Brand new lab announcement", url="https://lab.ai/news/b", source="Lab"),
    ])
    new2 = filter_new(items2, store, {"Lab"}, lookback_hours=48)
    assert [i.url for i in new2] == ["https://lab.ai/news/b"]


def test_saved_state_has_no_readable_personal_info(tmp_path):
    """Durum dosyası public repoda: link, başlık ve kaynak adı düz metin olarak yer almamalı."""
    store = SeenStore(tmp_path / "seen.json")
    secret = Item(title="Gizli ilgi alanim hakkinda cok ozel bir haber", url="https://ozel-sirket.com/haber",
                  source="OzelKaynak", kind="blog")
    secret.id = canonical_id(secret)
    filter_new([secret], store, {"OzelKaynak"}, lookback_hours=48)  # baseline + bootstrap
    other = Item(title="Ozel sirketin yeni ses modeli duyurusu 3.5", url="https://ozel-sirket.com/model",
                 source="OzelKaynak", kind="blog")
    other.id = canonical_id(other)
    store.add(other, STATUS_SENT, vec=np.arange(8, dtype=float), numbers={"3.5"})
    store.save()
    raw = (tmp_path / "seen.json").read_text().lower()
    for leak in ("ozel", "gizli", "haber", "model", "https"):
        assert leak not in raw, leak
    rec = SeenStore(tmp_path / "seen.json").recent_sent(7)[0]
    assert rec.numbers == {"3.5"} and rec.kind == "blog" and abs(np.linalg.norm(rec.vec) - 1) < 1e-3
