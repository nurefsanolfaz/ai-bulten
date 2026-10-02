"""LLM istemcisi: profil seçimi, zincir yedeği, Claude istek biçimi (ağ çağrısı yok)."""
from types import SimpleNamespace

import httpx
import pytest

from bulten.config import load_settings
from bulten.llm import LLM, LLMError, Usage, estimate_cost


@pytest.fixture
def settings():
    return load_settings()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "LLM_PROFILE"):
        monkeypatch.delenv(k, raising=False)


class FakeMessages:
    def __init__(self, stop_reason="end_turn", text='{"ok": true}'):
        self.calls = []
        self.stop_reason, self.text = stop_reason, text

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            stop_details=None,
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=self.text)],
            usage=SimpleNamespace(input_tokens=100, output_tokens=50,
                                  cache_creation_input_tokens=0, cache_read_input_tokens=900),
        )


def _with_fake_claude(llm, messages):
    llm._anthropic = SimpleNamespace(beta=SimpleNamespace(messages=messages))


def test_profile_from_env_and_unknown(settings, monkeypatch):
    monkeypatch.setenv("LLM_PROFILE", "claude")
    assert LLM(settings).profile == "claude"
    with pytest.raises(LLMError):
        LLM(settings, profile="yok-boyle-profil")


def test_all_profiles_reference_known_providers(settings):
    for name, prof in settings["llm"]["profiles"].items():
        for task in ("score", "write"):
            assert prof[task], f"{name}.{task} boş"
            for step in prof[task]:
                assert step["provider"] in settings["providers"]


def test_primary_only_and_missing_keys(settings, monkeypatch):
    llm = LLM(settings, profile="claude", primary_only=True)
    assert [s["model"] for s in llm.chains["write"]] == ["claude-opus-5"]
    with pytest.raises(LLMError, match="API anahtarı"):
        llm.chat("write", "sys", "user")


def test_claude_request_shape(settings, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    llm = LLM(settings, profile="claude", primary_only=True)
    fake = FakeMessages()
    _with_fake_claude(llm, fake)

    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
              "additionalProperties": False}
    assert llm.chat("write", "SİSTEM", "KULLANICI", schema=schema) == '{"ok": true}'

    call = fake.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["output_config"] == {"format": {"type": "json_schema", "schema": schema}, "effort": "medium"}
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "temperature" not in call  # yeni Claude modellerinde kaldırıldı
    assert llm.usage[0].input_tokens == 1000 and llm.usage[0].cache_read_tokens == 900

    # Haiku adımında effort/fallbacks gönderilmemeli
    llm.chat("score", "s", "u", schema=schema)
    haiku = fake.calls[1]
    assert haiku["model"] == "claude-haiku-4-5"
    assert "effort" not in haiku["output_config"] and "fallbacks" not in haiku


def test_claude_refusal_falls_through_chain(settings, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    llm = LLM(settings, profile="claude")
    _with_fake_claude(llm, FakeMessages(stop_reason="refusal"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert "generativelanguage" in str(request.url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "gemini yazdı"}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

    llm.http = httpx.Client(transport=httpx.MockTransport(handler))
    assert llm.chat("write", "s", "u") == "gemini yazdı"
    assert llm.last_model == "gemini/gemini-3.5-flash"


def test_openai_rate_limit_per_day_skips_to_next(settings, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test")
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    llm = LLM(settings, profile="free")

    def handler(request: httpx.Request) -> httpx.Response:
        if "groq" in str(request.url):
            return httpx.Response(429, text="Rate limit reached: tokens per day (TPD). Please try again in 7m2s")
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    llm.http = httpx.Client(transport=httpx.MockTransport(handler))
    assert llm.chat("score", "s", "u", schema={}) == "{}"
    assert llm.last_model.startswith("gemini/")


def test_estimate_cost(settings):
    usage = [Usage("score", "claude-haiku-4-5", 1_000_000, 100_000), Usage("write", "gemini-3.5-flash", 10**6, 10**6)]
    assert estimate_cost(usage, settings["pricing"]) == pytest.approx(1.0 + 0.5)
