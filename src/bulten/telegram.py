"""Telegram Bot API ile gönderim.

Chat ID'ni öğrenmek için: bota Telegram'dan bir mesaj at, sonra
    python -m bulten.telegram
komutunu çalıştır (TELEGRAM_BOT_TOKEN tanımlı olmalı).
"""
from __future__ import annotations

import logging
import os
import re
import time

import httpx

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
LIMIT = 4000  # Telegram sınırı 4096; etiketler için pay bırak
_TAG = re.compile(r"<[^>]+>")


def pack(blocks: list[str], limit: int = LIMIT) -> list[str]:
    """Blokları mesaj sınırını aşmadan birleştir; tek başına çok uzun bloğu satırlardan böl."""
    messages: list[str] = []
    current = ""
    for block in blocks:
        pieces = [block] if len(block) <= limit else _split_long(block, limit)
        for piece in pieces:
            candidate = f"{current}\n\n{piece}" if current else piece
            if len(candidate) <= limit:
                current = candidate
            else:
                messages.append(current)
                current = piece
    if current:
        messages.append(current)
    return messages


def _split_long(text: str, limit: int) -> list[str]:
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # satır bile çok uzunsa (nadiren) sert kes
            out.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


class Telegram:
    def __init__(self, token: str | None = None, chat_id: str | None = None, client: httpx.Client | None = None):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        self.client = client or httpx.Client(timeout=30)
        if not self.token or not self.chat_id:
            raise RuntimeError("TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID tanımlı olmalı")

    def _post(self, method: str, payload: dict) -> httpx.Response:
        for _ in range(3):
            resp = self.client.post(API.format(token=self.token, method=method), json=payload)
            if resp.status_code == 429:
                wait = resp.json().get("parameters", {}).get("retry_after", 5)
                time.sleep(wait + 1)
                continue
            return resp
        return resp

    def send_message(self, text: str, silent: bool = False) -> None:
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "disable_notification": silent,
        }
        resp = self._post("sendMessage", payload)
        if resp.status_code == 400 and "parse" in resp.text.lower():
            # HTML hatası olursa düz metin olarak yine de gönder
            log.warning("Telegram HTML hatası, düz metne geçiliyor: %s", resp.text[:200])
            payload.pop("parse_mode")
            payload["text"] = _TAG.sub("", text)
            resp = self._post("sendMessage", payload)
        if resp.status_code != 200:
            raise RuntimeError(f"Telegram hatası {resp.status_code}: {resp.text[:300]}")

    def send_document(self, path, caption: str = "") -> None:
        """Dosya gönder (ör. karşılaştırma raporu) — public loga/artifact'e koymak yerine."""
        with open(path, "rb") as f:
            resp = self.client.post(
                API.format(token=self.token, method="sendDocument"),
                data={"chat_id": self.chat_id, "caption": caption[:1000]},
                files={"document": (path.name, f, "text/markdown")},
            )
        if resp.status_code != 200:
            raise RuntimeError(f"Telegram dosya hatası {resp.status_code}: {resp.text[:300]}")

    def send(self, blocks: list[str], silent: bool = False) -> int:
        """silent=True → mesaj gelir ama telefon bildirim sesi çalmaz (gece saatleri için)."""
        messages = pack(blocks)
        for n, msg in enumerate(messages):
            self.send_message(msg, silent=silent)
            if n < len(messages) - 1:
                time.sleep(1)  # sıralamayı ve flood limitini koru
        return len(messages)


def _print_chat_ids() -> None:
    from .config import load_dotenv

    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Önce TELEGRAM_BOT_TOKEN tanımla (.env ya da ortam değişkeni)")
    resp = httpx.get(API.format(token=token, method="getUpdates"), timeout=30).json()
    chats = {}
    for upd in resp.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("first_name")
    if not chats:
        print("Hiç mesaj bulunamadı. Bota Telegram'dan bir mesaj (ör. /start) gönderip tekrar dene.")
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}   ({name})")


if __name__ == "__main__":
    _print_chat_ids()
