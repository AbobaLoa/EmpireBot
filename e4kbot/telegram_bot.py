from __future__ import annotations

from pathlib import Path
from typing import Any

import requests
from loguru import logger


class TelegramReporter:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.enabled = bool(cfg.get("enabled"))
        self.token = str(cfg.get("bot_token") or "").strip()
        self.chat_id = str(cfg.get("chat_id") or cfg.get("chat_baron") or "")
        self.public_webapp_url = str(cfg.get("public_webapp_url") or "").strip()
        self._base = f"https://api.telegram.org/bot{self.token}"

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.token) and bool(self.chat_id)

    def _post(self, method: str, data: dict | None = None, files: dict | None = None) -> dict[str, Any]:
        if not self.token:
            return {}
        try:
            response = requests.post(
                f"{self._base}/{method}",
                data=data or {},
                files=files,
                timeout=20,
            )
            payload = response.json()
            if not payload.get("ok"):
                logger.warning(f"Telegram {method}: {str(payload)[:180]}")
            return payload
        except Exception as exc:
            logger.warning(f"Telegram {method} failed: {type(exc).__name__}")
            return {}

    def verify(self) -> dict[str, Any]:
        payload = self._post("getMe")
        return payload.get("result") or {}

    def send_text(self, text: str) -> bool:
        if not self.ready:
            return False
        extra: dict[str, Any] = {"chat_id": self.chat_id, "text": text[:4096]}
        if self.public_webapp_url:
            extra["reply_markup"] = _webapp_markup(self.public_webapp_url)
        import json

        if "reply_markup" in extra:
            extra["reply_markup"] = json.dumps(extra["reply_markup"])
        return bool(self._post("sendMessage", extra).get("ok"))

    def send_photo(self, image_path: Path, caption: str) -> bool:
        if not self.ready or not image_path.exists():
            return False
        import json

        data: dict[str, Any] = {
            "chat_id": self.chat_id,
            "caption": caption[:1024],
        }
        if self.public_webapp_url:
            data["reply_markup"] = json.dumps(_webapp_markup(self.public_webapp_url))
        with image_path.open("rb") as handle:
            return bool(
                self._post("sendPhoto", data, files={"photo": handle}).get("ok")
            )

    def set_menu_button(self) -> None:
        if not self.token or not self.public_webapp_url:
            return
        import json

        self._post(
            "setChatMenuButton",
            {
                "menu_button": json.dumps(
                    {
                        "type": "web_app",
                        "text": "Атаки",
                        "web_app": {"url": self.public_webapp_url},
                    }
                )
            },
        )

    def report_attack(
        self,
        account: str,
        kind: str,
        kingdom: int,
        x: int,
        y: int,
        commander_no: int,
        one_way_sec: int,
        return_sec: int,
        screenshot: Path | None = None,
        extra: str = "",
        dry_run: bool = False,
    ) -> None:
        prefix = "🧪 DRY-RUN" if dry_run else "✅ Атака отправлена"
        kind_ru = {
            "baron": "Барон",
            "nomad": "Лагерь кочевников",
            "shogun": "Лагерь сёгуна",
        }.get(kind, kind)
        mins, sec = divmod(max(0, int(one_way_sec)), 60)
        ret_m, ret_s = divmod(max(0, int(return_sec)), 60)
        text = (
            f"{prefix}\n"
            f"🎮 {account}\n"
            f"🎯 {kind_ru} · K{kingdom} ({x}, {y})\n"
            f"🧑‍✈️ Военачальник №{commander_no}\n"
            f"⏱ До цели: {mins} мин {sec} сек\n"
            f"↩️ Возврат через: {ret_m} мин {ret_s} сек"
        )
        if extra:
            text += f"\n{extra}"
        if screenshot and screenshot.exists():
            self.send_photo(screenshot, text)
        else:
            self.send_text(text)

    def report_stop(self, reason: str) -> None:
        self.send_text(f"⛔ Бот остановил атаки\n{reason}")

    def report_status(self, text: str) -> None:
        self.send_text(text)


def _webapp_markup(url: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "Открыть мини-приложение", "web_app": {"url": url}}]
        ]
    }


def format_cd(seconds: int) -> str:
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин {sec} сек"
    return f"{sec} сек"
