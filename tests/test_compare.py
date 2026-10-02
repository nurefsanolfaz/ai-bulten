import json
from datetime import date, datetime, timezone

import bulten.compare as compare
from bulten.compare import Result, agreement, build_report, run_profile, spearman
from bulten.config import load_settings
from bulten.models import Item


def test_spearman_and_agreement():
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0
    assert spearman([1, 2], [1, 2]) is None  # çok az ortak öğe

    a = Result("a", ok=True, scores={"x": 9, "y": 5, "z": 2}, selected={"work": ["x", "y"]})
    b = Result("b", ok=True, scores={"x": 8, "y": 6, "z": 1}, selected={"work": ["x"], "ai": ["z"]})
    rho, jac = agreement(a, b)
    assert rho == 1.0 and jac == 1 / 3


def test_candidates_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(compare, "OUT", tmp_path)
    it = Item(title="T", url="https://u", source="S", id="url:u", also_on=["X"],
              published=datetime(2026, 9, 28, tzinfo=timezone.utc), section_scores={"work": 0.5})
    compare.save_candidates([it])
    back = compare.load_candidates()[0]
    assert back == it


def test_run_profile_skips_without_keys(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    res = run_profile("claude", [], load_settings(), "ctx", date(2026, 9, 28))
    assert not res.ok and "ANTHROPIC_API_KEY" in res.skipped


def test_run_profile_and_report_with_fake_llm(monkeypatch):
    settings = load_settings()
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.setenv("GEMINI_API_KEY", "x")

    def fake_chat(self, task, system, user, schema=None, temperature=0.3):
        from bulten.llm import Usage
        self.usage.append(Usage(task, self.chains[task][0]["model"], 1000, 200))
        self.last_model = self.chains[task][0]["model"]
        if task == "score":
            return json.dumps({"results": [
                {"id": p["id"], "section": "work", "score": 8, "reason": "r"} for p in json.loads(user)]})
        return json.dumps({"intro": "Merhaba", "closing": "Son", "sections": [
            {"id": "work", "paragraphs": [{"item_ids": ["work1"], "headline": "B", "text": "bir iki üç"}]}]})

    monkeypatch.setattr("bulten.llm.LLM.chat", fake_chat)
    cands = [Item(title=f"Haber {n}", url=f"https://h/{n}", source="S", id=f"url:h/{n}") for n in range(3)]
    res = run_profile("free", cands, settings, "ctx", date(2026, 9, 28))
    assert res.ok and res.n_scored == 3 and res.words == 4
    assert res.tokens_in == 2000 and res.cost == 0  # ücretsiz modeller

    skipped = Result("claude", skipped="API anahtarı yok: ANTHROPIC_API_KEY")
    report = build_report([res, skipped], cands, settings)
    assert "| **free** |" in report and "⏭" in report and "[Haber 0](https://h/0)" in report
