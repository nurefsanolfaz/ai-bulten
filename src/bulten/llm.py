"""Sağlayıcıdan bağımsız LLM istemcisi.

- OpenAI uyumlu sağlayıcılar (Groq, Gemini, OpenRouter): /chat/completions, httpx ile
- Claude: resmi Anthropic SDK'sı (Messages API), JSON şemasıyla yapılandırılmış çıktı

Aktif profilin zinciri sırayla denenir: anahtarı olmayan sağlayıcı atlanır, hata veren/kotası
dolan modelden sonrakine geçilir. Her çağrının token kullanımı `usage` listesine kaydedilir.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

_RETRY_IN = re.compile(r"try again in (?:(\d+)m)?([\d.]+)s", re.I)
MAX_WAIT = 90  # saniye — dakikalık token limitine takılınca beklenecek en uzun süre


class LLMError(RuntimeError):
    pass


@dataclass
class Usage:
    task: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    seconds: float = 0.0


def _wait_seconds(resp: httpx.Response) -> float:
    if ra := resp.headers.get("retry-after"):
        try:
            return float(ra)
        except ValueError:
            pass
    if m := _RETRY_IN.search(resp.text):
        return int(m.group(1) or 0) * 60 + float(m.group(2))
    return 20.0


def active_profile(settings: dict) -> str:
    return os.environ.get("LLM_PROFILE") or settings["llm"]["profile"]


class LLM:
    def __init__(self, settings: dict, profile: str | None = None, primary_only: bool = False):
        """primary_only=True → sadece zincirin ilk modeli (karşılaştırma için adil test)."""
        self.providers = settings["providers"]
        self.profile = profile or active_profile(settings)
        profiles = settings["llm"]["profiles"]
        if self.profile not in profiles:
            raise LLMError(f"Bilinmeyen LLM profili: {self.profile} (mevcut: {', '.join(profiles)})")
        self.chains = {task: steps[:1] if primary_only else steps for task, steps in profiles[self.profile].items()}
        self.http = httpx.Client(timeout=httpx.Timeout(180.0, connect=15.0))
        self._anthropic = None
        self.last_model: str | None = None
        self.usage: list[Usage] = []

    def available(self, task: str) -> list[dict]:
        return [s for s in self.chains[task] if os.environ.get(self.providers[s["provider"]]["api_key_env"])]

    def chat(self, task: str, system: str, user: str, schema: dict | None = None, temperature: float = 0.3) -> str:
        """Bir görev (score/write) için yanıt metni döndürür. `schema` verilirse yanıt JSON olur."""
        steps = self.available(task)
        if not steps:
            raise LLMError(f"'{self.profile}' profilinde '{task}' için API anahtarı tanımlı model yok")
        errors = []
        for step in steps:
            name = f"{step['provider']}/{step['model']}"
            started = time.monotonic()
            try:
                if self.providers[step["provider"]].get("type") == "anthropic":
                    text, usage = self._call_anthropic(step, system, user, schema)
                else:
                    text, usage = self._call_openai(step, system, user, schema is not None, temperature)
            except LLMError as e:
                log.warning("LLM %s başarısız: %s", name, e)
                errors.append(f"{name}: {e}")
                continue
            usage.task, usage.model, usage.seconds = task, step["model"], time.monotonic() - started
            self.usage.append(usage)
            self.last_model = name
            return text
        raise LLMError("Tüm modeller başarısız → " + " | ".join(errors))

    # ------------------------------------------------------------ OpenAI uyumlu

    def _call_openai(self, step: dict, system: str, user: str, json_mode: bool, temperature: float):
        prov = self.providers[step["provider"]]
        body: dict = {
            "model": step["model"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if step.get("reasoning_effort"):
            body["reasoning_effort"] = step["reasoning_effort"]
        headers = {"Authorization": f"Bearer {os.environ[prov['api_key_env']]}"}

        for attempt in range(4):
            try:
                resp = self.http.post(f"{prov['base_url']}/chat/completions", json=body, headers=headers)
            except httpx.HTTPError as e:
                if attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise LLMError(f"bağlantı hatası: {e}") from e

            if resp.status_code == 429:
                wait = _wait_seconds(resp)
                # Günlük kota dolduysa beklemenin anlamı yok → sonraki modele geç
                if wait > MAX_WAIT or "per day" in resp.text.lower() or attempt == 3:
                    raise LLMError(f"kota/limit (429): {resp.text[:200]}")
                log.info("Hız limiti, %.0f sn bekleniyor…", wait)
                time.sleep(wait + 1)
                continue
            if resp.status_code >= 500 and attempt < 3:
                # 503 "high demand" genelde birkaç dakikada geçer: 15 + 30 + 45 sn bekle, sonra yedeğe geç
                log.info("Sunucu hatası %d, %d sn sonra tekrar denenecek…", resp.status_code, 15 * (attempt + 1))
                time.sleep(15 * (attempt + 1))
                continue
            if resp.status_code != 200:
                raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")

            try:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, ValueError) as e:
                raise LLMError(f"beklenmeyen yanıt: {resp.text[:300]}") from e
            if not content or not content.strip():
                raise LLMError("boş yanıt")
            u = data.get("usage") or {}
            return content, Usage("", "", u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
        raise LLMError("yeniden denemeler tükendi")

    # ------------------------------------------------------------ Claude

    def _call_anthropic(self, step: dict, system: str, user: str, schema: dict | None):
        import anthropic  # sadece Claude profili kullanılırken gerekli

        if self._anthropic is None:
            self._anthropic = anthropic.Anthropic(
                api_key=os.environ[self.providers[step["provider"]]["api_key_env"]], max_retries=3
            )

        output_config: dict = {}
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        if step.get("effort"):
            output_config["effort"] = step["effort"]
        kwargs: dict = {}
        if output_config:
            kwargs["output_config"] = output_config
        if step.get("fallbacks"):
            kwargs["fallbacks"] = step["fallbacks"]
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]

        try:
            response = self._anthropic.beta.messages.create(
                model=step["model"],
                max_tokens=16000,
                # Sistem promptu tüm puanlama gruplarında aynı → önbelleğe al (yeterince uzunsa)
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                **kwargs,
            )
        except anthropic.RateLimitError as e:
            raise LLMError(f"hız limiti (429): {e.message}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"HTTP {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"bağlantı hatası: {e}") from e

        if response.stop_reason == "refusal":
            raise LLMError(f"model isteği reddetti: {response.stop_details}")
        if response.stop_reason == "max_tokens":
            raise LLMError("yanıt max_tokens sınırında kesildi")
        text = "".join(b.text for b in response.content if b.type == "text")
        if not text.strip():
            raise LLMError("boş yanıt")
        u = response.usage
        return text, Usage(
            "", "",
            (u.input_tokens or 0) + (u.cache_creation_input_tokens or 0) + (u.cache_read_input_tokens or 0),
            u.output_tokens or 0,
            u.cache_read_input_tokens or 0,
        )


def estimate_cost(usage: list[Usage], pricing: dict) -> float:
    """Tahmini $ maliyet (önbellek indirimi hesaba katılmaz → üst sınır)."""
    total = 0.0
    for u in usage:
        price_in, price_out = pricing.get(u.model, (0.0, 0.0))
        total += (u.input_tokens * price_in + u.output_tokens * price_out) / 1_000_000
    return total


def parse_json(text: str) -> dict:
    """Model yanıtından JSON nesnesini çıkar (kod bloğu / önsöz toleranslı)."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise
