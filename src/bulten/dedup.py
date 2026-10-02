"""Tekilleştirme: kaynaklar arası kopyaları birleştir, daha önce görülenleri at.

Kalıcı durum `data/seen.json` dosyasında tutulur ve GitHub Actions her çalışmada repoya commit eder.
Repo herkese açık olduğu için dosyada okunabilir bilgi yoktur: anahtarlar hash'lenir, gönderilen
haberlerin başlığı yerine sadece embedding vektörü saklanır.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import numpy as np

from .models import Item

log = logging.getLogger(__name__)

_ARXIV_ID = re.compile(
    r"(?:arxiv\.org/(?:abs|pdf|html)/|huggingface\.co/papers/|alphaxiv\.org/abs/)(\d{4}\.\d{4,5})", re.I
)
_TRACKING = re.compile(r"^(utm_|ref$|ref_|source$|fbclid|gclid|mc_|s$)", re.I)
_NON_ALNUM = re.compile(r"[^a-z0-9ğüşıöç]+")

STATUS_SENT = "sent"          # bültende gönderildi
STATUS_SCORED = "scored"      # LLM puanladı ama seçilmedi → tekrar puanlanmasın
STATUS_BASELINE = "baseline"  # scrape kaynağının ilk çalışmasında zaten sayfada olan link


def arxiv_id(url: str) -> str | None:
    m = _ARXIV_ID.search(url)
    return m.group(1) if m else None


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if not _TRACKING.match(k)))
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("", host, path, query, ""))[2:]  # "//host/path" → "host/path"


def canonical_id(item: Item) -> str:
    aid = arxiv_id(item.url)
    return f"arxiv:{aid}" if aid else f"url:{normalize_url(item.url)}"


def title_key(title: str) -> str | None:
    norm = _NON_ALNUM.sub(" ", title.lower()).strip()
    # Google News başlıklarının sonundaki " - Yayıncı" kısmı farklı olabilir; kısa başlıklar güvenilmez
    if len(norm) < 25:
        return None
    return "t:" + hashlib.sha1(norm.encode()).hexdigest()[:16]


def _merge(into: Item, other: Item) -> None:
    into.popularity = max(into.popularity, other.popularity)
    into.priority = into.priority or other.priority
    if other.source != into.source and other.source not in into.also_on:
        into.also_on.append(other.source)
    if len(other.summary) > len(into.summary):
        into.summary = other.summary
    if into.published is None:
        into.published = other.published


def merge_duplicates(items: list[Item]) -> list[Item]:
    """Aynı çalışmada farklı kaynaklardan gelen aynı öğeyi birleştir (ör. arXiv + HF Papers + HN)."""
    by_key: dict[str, Item] = {}
    out: list[Item] = []
    for item in items:
        item.id = item.id or canonical_id(item)
        keys = [item.id] + ([tk] if (tk := title_key(item.title)) else [])
        existing = next((by_key[k] for k in keys if k in by_key), None)
        if existing:
            _merge(existing, item)
            for k in keys:
                by_key.setdefault(k, existing)
        else:
            out.append(item)
            for k in keys:
                by_key[k] = item
    return out


def hash_key(key: str) -> str:
    """Durum dosyası herkese açık repoda durur: linkler/başlıklar/kaynak adları düz metin saklanmaz."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


@dataclass
class SentRecord:
    """Gönderilmiş bir haberin anlamsal tekrar kontrolü için gereken, geri okunamaz izi."""
    vec: np.ndarray        # başlık embedding'i (başlığın kendisi saklanmaz)
    numbers: set[str]      # başlıktaki sayılar (sürüm çelişkisi kuralı için)
    source_hash: str
    kind: str


class SeenStore:
    VERSION = 2

    def __init__(self, path: Path):
        self.path = path
        self.items: dict[str, dict] = {}
        self._bootstrapped: set[str] = set()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") == self.VERSION:
                self.items = data.get("items", {})
                self._bootstrapped = set(data.get("bootstrapped", []))
            else:
                log.warning("%s eski biçimde (v%s), sıfırdan başlanıyor", path.name, data.get("version"))
        self.today = date.today().isoformat()

    def keys_for(self, item: Item) -> list[str]:
        keys = [item.id] + ([tk] if (tk := title_key(item.title)) else [])
        return [hash_key(k) for k in keys]

    def is_bootstrapped(self, source: str) -> bool:
        return hash_key(source) in self._bootstrapped

    def mark_bootstrapped(self, source: str) -> None:
        self._bootstrapped.add(hash_key(source))

    def is_seen(self, item: Item) -> bool:
        return any(k in self.items for k in self.keys_for(item))

    def is_sent(self, item: Item) -> bool:
        return any(self.items.get(k, {}).get("s") == STATUS_SENT for k in self.keys_for(item))

    def sent_count_on(self, day: str) -> int:
        """O gün gönderilen öğe sayısı (flaş haber günlük kotası için)."""
        return sum(1 for v in self.items.values() if v.get("sd") == day and "k" in v)

    def recent_sent(self, days: int) -> list[SentRecord]:
        """Son N günde gönderilenlerin izleri — farklı başlıklı tekrarları yakalamak için."""
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        out = []
        for v in self.items.values():
            if v.get("s") == STATUS_SENT and "e" in v and v.get("sd", v["d"]) >= cutoff:
                vec = np.frombuffer(base64.b64decode(v["e"]), dtype=np.float16).astype(np.float32)
                out.append(SentRecord(vec / max(float(np.linalg.norm(vec)), 1e-9), set(v.get("n", [])),
                                      v.get("src", ""), v.get("k", "news")))
        return out

    def touch(self, item: Item) -> None:
        """Hâlâ bir kaynakta görünen kaydın tarihini tazele (budanmasın)."""
        for k in self.keys_for(item):
            if k in self.items:
                self.items[k]["d"] = self.today

    def add(self, item: Item, status: str, vec: np.ndarray | None = None, numbers: set[str] | None = None) -> None:
        keys = self.keys_for(item)
        for k in keys:
            prev = self.items.get(k, {}).get("s")
            if prev == STATUS_SENT and status != STATUS_SENT:
                continue  # "gönderildi" bilgisini asla düşürme
            entry = {"d": self.today, "s": status}
            if status == STATUS_SENT:
                entry["sd"] = self.today  # gönderim günü ("d" her görüldüğünde tazelenir, bu değişmez)
                if k == keys[0]:
                    entry.update(k=item.kind, src=hash_key(item.source), n=sorted(numbers or []))
                    if vec is not None:
                        entry["e"] = base64.b64encode(np.asarray(vec, dtype=np.float16).tobytes()).decode()
            self.items[k] = entry

    def prune(self, retention_days: int) -> int:
        cutoff = (date.today() - timedelta(days=retention_days)).isoformat()
        old = [k for k, v in self.items.items() if v["d"] < cutoff]
        for k in old:
            del self.items[k]
        return len(old)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": self.VERSION,
            "bootstrapped": sorted(self._bootstrapped),
            "items": dict(sorted(self.items.items())),
        }
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")


def filter_new(
    items: list[Item],
    store: SeenStore,
    scrape_sources: set[str],
    lookback_hours: int,
) -> list[Item]:
    """Birleştirilmiş öğelerden yalnızca yeni ve güncel olanları döndür."""
    # 1) Scrape kaynaklarının ilk çalışması: sayfadaki her şey baseline olur, bülten sel basmaz
    first_run = {s for s in scrape_sources if not store.is_bootstrapped(s)}
    fresh: list[Item] = []
    for item in items:
        if item.source in first_run:
            store.add(item, STATUS_BASELINE)
        else:
            fresh.append(item)
    for name in first_run:
        if any(i.source == name for i in items):
            store.mark_bootstrapped(name)
            log.info("%s: ilk çalışma, mevcut linkler baseline olarak kaydedildi", name)

    # 2) Daha önce görülenler + çok eski olanlar
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    out = []
    for item in fresh:
        if store.is_seen(item):
            store.touch(item)
        elif item.published and item.published < cutoff:
            continue
        else:
            out.append(item)
    return out
