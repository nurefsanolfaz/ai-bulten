import gzip
import json
from datetime import date, datetime, timezone

from bulten.archive import write_daily
from bulten.models import Item


def test_write_daily_records_all_items_with_scores(tmp_path):
    plain = Item(title="Sadece taranan", url="https://a", source="HN", kind="community", id="url:a",
                 section_scores={"work": 0.3, "ai": 0.5}, prefilter_score=0.5)
    cand = Item(title="Aday ama seçilmedi", url="https://b", source="arXiv", kind="paper", id="arxiv:1",
                published=datetime(2026, 10, 6, tzinfo=timezone.utc), section_scores={"work": 0.7},
                prefilter_score=0.7, score=5, section="work", reason="sıradan")
    sent = Item(title="Gönderilen", url="https://c", source="Lab", kind="lab", id="url:c", priority=True,
                score=9, section="ai", reason="büyük çıkış", also_on=["HN"])
    failed = Item(title="LLM yanıt vermedi", url="https://d", source="Lab", kind="lab", id="url:d")

    paths = write_daily(tmp_path, date(2026, 10, 6), [plain, cand, sent, failed], [cand, sent, failed], [sent],
                        "# Bülten\n")
    data, md = paths
    assert data == tmp_path / "veri" / "2026" / "10" / "2026-10-06.jsonl.gz"
    assert md.read_text() == "# Bülten\n" and md.parent.name == "2026"

    rows = {r["id"]: r for r in map(json.loads, gzip.open(data, "rt", encoding="utf-8"))}
    assert len(rows) == 4
    assert rows["url:a"]["candidate"] is False and rows["url:a"]["llm_score"] is None
    assert rows["url:a"]["prefilter_section"] == "ai"
    assert rows["arxiv:1"]["llm_score"] == 5 and rows["arxiv:1"]["llm_section"] == "work"
    assert rows["arxiv:1"]["published"].startswith("2026-10-06") and not rows["arxiv:1"]["sent"]
    assert rows["url:c"]["sent"] and rows["url:c"]["also_on"] == ["HN"] and rows["url:c"]["llm_reason"] == "büyük çıkış"
    assert rows["url:d"]["candidate"] and rows["url:d"]["llm_score"] is None  # aday ama puanlanamadı
    assert all("collected_at" in r for r in rows.values())


def test_write_daily_without_markdown(tmp_path):
    paths = write_daily(tmp_path, date(2026, 10, 6), [], [], [], None)
    assert len(paths) == 1 and paths[0].exists()
