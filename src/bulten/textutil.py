"""Metin yardımcıları: HTML temizleme, anahtar kelime eşleştirme."""
from __future__ import annotations

import html
import re
from functools import lru_cache

from bs4 import BeautifulSoup

_WS = re.compile(r"\s+")


def clean_html(text: str, limit: int = 1200) -> str:
    if not text:
        return ""
    if "<" in text:
        text = BeautifulSoup(text, "html.parser").get_text(" ")
    text = _WS.sub(" ", html.unescape(text)).strip()
    return text[:limit]


@lru_cache(maxsize=512)
def _kw_pattern(keyword: str) -> re.Pattern:
    # Kelime sınırlarıyla eşleştir: "ai" → "email" içinde eşleşmesin
    return re.compile(r"(?<![a-z0-9])" + re.escape(keyword.lower()) + r"(?![a-z0-9])")


def keyword_hits(text: str, keywords: list[str]) -> int:
    text = text.lower()
    return sum(1 for kw in keywords if _kw_pattern(kw).search(text))


def has_any_keyword(text: str, keywords: list[str]) -> bool:
    text = text.lower()
    return any(_kw_pattern(kw).search(text) for kw in keywords)
