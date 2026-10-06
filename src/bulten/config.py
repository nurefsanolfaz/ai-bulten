"""Yapılandırma yükleme.

Herkese açık ayarlar `config/settings.yaml` ve `config/sources.yaml` içindedir. Kişiye özel her şey
(bölümler, kişisel kaynaklar, flaş kaynakları…) gizli profil dosyasında durur ve bunların üzerine yazılır:

    config/profile.yaml  (yerel, .gitignore'da)  →  yoksa PROFILE_YAML ortam değişkeni (Actions secret'ı)

Profil, settings.yaml ile aynı yapıdadır (sözlükler birleştirilir, listeler değiştirilir) ve ek olarak
`extra_sources:` listesiyle sources.yaml'a kaynak ekleyebilir.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
PROMPTS_DIR = ROOT / "prompts"
DATA_DIR = ROOT / "data"
ARCHIVE_DIR = ROOT / "out" / "arsiv"  # GitHub Actions bunu gizli arşiv reposuna commit eder

log = logging.getLogger(__name__)


def load_yaml(name: str):
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


def in_github_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


@lru_cache(maxsize=1)
def load_profile() -> dict:
    """Gizli profil. BULTEN_NO_PROFILE=1 → yok say (testler için)."""
    if os.environ.get("BULTEN_NO_PROFILE"):
        return {}
    path = CONFIG_DIR / "profile.yaml"
    if path.exists():
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    env = os.environ.get("PROFILE_YAML", "").strip()
    if env:
        return yaml.safe_load(env) or {}
    log.warning("config/profile.yaml ve PROFILE_YAML yok — genel örnek bölümler kullanılıyor")
    return {}


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_settings() -> dict:
    profile = {k: v for k, v in load_profile().items() if k != "extra_sources"}
    return deep_merge(load_yaml("settings.yaml"), profile)


def load_sources() -> list[dict]:
    sources = load_yaml("sources.yaml") + list(load_profile().get("extra_sources") or [])
    return [s for s in sources if s.get("enabled", True)]


def mask_private_in_logs() -> None:
    """GitHub Actions'ta profildeki ad ve başlıkları loglarda *** olarak gizle (public repo logları açıktır)."""
    if not in_github_actions():
        return
    profile = load_profile()
    secrets = {s.get("name", "") for s in profile.get("extra_sources") or []}
    secrets |= {s.get("title", "") for s in profile.get("sections") or []}
    for value in sorted(v for v in secrets if v and len(v) >= 3):
        print(f"::add-mask::{value}", flush=True)


def load_context() -> str:
    """Kişisel bağlam: config/context.md → CONTEXT_MD env → şablon (uyarıyla)."""
    path = CONFIG_DIR / "context.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    env = os.environ.get("CONTEXT_MD", "").strip()
    if env:
        return env
    log.warning("config/context.md ve CONTEXT_MD yok — şablon kullanılıyor, bülten kişisel olmayacak")
    return (CONFIG_DIR / "context.example.md").read_text(encoding="utf-8")


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Yerel geliştirme için minimal .env okuyucu (zaten tanımlı değişkenleri ezmez)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)
