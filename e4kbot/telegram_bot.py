from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import requests
from loguru import logger

BARON_THREAD_NAMES = ("барон разбойников", "бароны разбойников", "разбойников")
SAMURAI_THREAD_NAMES = (
    "вторжение самураев",
    "вторжения самураев",
    "лагери самураев",
    "лагеря самураев",
    "самураев",
)
NOMAD_THREAD_NAMES = (
    "вторжение кочевников",
    "вторжения кочевников",
    "лагери кочевников",
    "лагеря кочевников",
    "кочевников",
)


def parse_loot_amounts(text: str) -> tuple[int, int]:
    """Return (gold, rubies) parsed from an attack-report caption or OCR text."""
    if not text:
        return 0, 0
    gold = _first_amount(
        text,
        (
            r"(?:💰|золото|золот(?:а|о|ом)?|gold)\s*[:=]?\s*([0-9][0-9\s.,]*)\s*([kкmмbб]?)",
            r"([0-9][0-9\s.,]*)\s*([kкmмbб]?)\s*(?:💰|золото|gold)",
        ),
    )
    rubies = _first_amount(
        text,
        (
            r"(?:💎|рубин(?:ы|ов)?|ruby|rubies)\s*[:=]?\s*([0-9][0-9\s.,]*)\s*([kкmмbб]?)",
            r"([0-9][0-9\s.,]*)\s*([kкmмbб]?)\s*(?:💎|рубин(?:ы|ов)?|ruby|rubies)",
        ),
    )
    return gold, rubies


def _first_amount(text: str, patterns: tuple[str, ...]) -> int:
    blob = text.replace("\u00a0", " ")
    for pattern in patterns:
        match = re.search(pattern, blob, flags=re.IGNORECASE)
        if match:
            return _parse_amount(match.group(1), match.group(2) if match.lastindex and match.lastindex >= 2 else "")
    return 0


def _parse_amount(raw: str, suffix: str = "") -> int:
    compact = re.sub(r"[^\d.,]", "", str(raw or ""))
    if not compact:
        return 0
    if compact.count(",") == 1 and compact.count(".") == 0:
        compact = compact.replace(",", ".")
    else:
        compact = compact.replace(",", "")
    try:
        value = float(compact)
    except ValueError:
        digits = re.sub(r"\D", "", compact)
        value = float(digits) if digits else 0.0
    mark = (suffix or "").lower().replace("к", "k").replace("м", "m").replace("б", "b")
    if mark == "k":
        value *= 1_000
    elif mark == "m":
        value *= 1_000_000
    elif mark == "b":
        value *= 1_000_000_000
    return int(value)


def _thread_name_matches(name: str) -> bool:
    lowered = (name or "").strip().lower().replace("ё", "е")
    return any(token in lowered for token in BARON_THREAD_NAMES)


class TelegramReporter:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.enabled = bool(cfg.get("enabled"))
        self.token = str(cfg.get("bot_token") or "").strip()
        self.chat_id = str(cfg.get("chat_id") or cfg.get("chat_baron") or "")
        self.public_webapp_url = str(cfg.get("public_webapp_url") or "").strip()
        self.message_thread_id = self._coerce_thread(
            cfg.get("message_thread_id", cfg.get("thread_baron", cfg.get("thread_id")))
        )
        self.thread_samurai = self._coerce_thread(cfg.get("thread_samurai"))
        self.thread_nomad = self._coerce_thread(cfg.get("thread_nomad"))
        self.thread_summary = self._coerce_thread(cfg.get("thread_summary"))
        self.world_threads = {
            "Великая империя": self._coerce_thread(cfg.get("thread_great_empire")),
            "Вечнохолодный Ледник": self._coerce_thread(cfg.get("thread_everwinter_glacier")),
            "Пылающие Пески": self._coerce_thread(cfg.get("thread_burning_sands")),
            "Огненные Вершины": self._coerce_thread(cfg.get("thread_fire_peaks")),
            "Острова ураганов": self._coerce_thread(cfg.get("thread_storm_islands")),
        }
        self._base = f"https://api.telegram.org/bot{self.token}"

    @staticmethod
    def _coerce_thread(raw: Any) -> int | None:
        if raw in (None, "", 0, "0"):
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.token) and bool(self.chat_id)

    def _thread_payload(self, kind: str | None = None) -> dict[str, int]:
        thread = self.message_thread_id
        if kind in {"samurai", "samurai_camps"}:
            thread = self.thread_samurai or self.resolve_samurai_thread() or thread
        if kind in {"nomad", "nomad_camps"}:
            thread = self.thread_nomad or self.resolve_nomad_thread() or thread
        if thread:
            return {"message_thread_id": int(thread)}
        return {}

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

    def resolve_baron_thread(self) -> int | None:
        """Keep a configured topic id, otherwise learn it from recent forum messages."""
        if self.message_thread_id:
            return self.message_thread_id
        found = self._resolve_topic(BARON_THREAD_NAMES)
        if found:
            self.message_thread_id = found
        return self.message_thread_id

    def resolve_samurai_thread(self) -> int | None:
        if self.thread_samurai:
            return self.thread_samurai
        found = self._resolve_topic(SAMURAI_THREAD_NAMES)
        if found:
            self.thread_samurai = found
        return self.thread_samurai

    def resolve_nomad_thread(self) -> int | None:
        if self.thread_nomad:
            return self.thread_nomad
        found = self._resolve_topic(NOMAD_THREAD_NAMES)
        if found:
            self.thread_nomad = found
        return self.thread_nomad

    def _resolve_topic(self, names: tuple[str, ...]) -> int | None:
        if not self.token:
            return None
        payload = self._post("getUpdates", {"timeout": 0, "allowed_updates": json.dumps(["message"])})
        for update in reversed(payload.get("result") or []):
            message = update.get("message") or update.get("channel_post") or {}
            chat = message.get("chat") or {}
            if str(chat.get("id") or "") != str(self.chat_id):
                continue
            thread_id = message.get("message_thread_id")
            topic = (message.get("reply_to_message") or {}).get("forum_topic_created") or {}
            name = str(topic.get("name") or message.get("forum_topic_created", {}).get("name") or "")
            lowered = name.lower().replace("ё", "е")
            if thread_id and any(token in lowered for token in names):
                logger.info("Telegram topic «{}» → thread {}", name, thread_id)
                return int(thread_id)
        return None

    def send_text(self, text: str, kind: str | None = None) -> bool:
        if not self.ready:
            return False
        extra: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text[:4096],
            **self._thread_payload(kind),
        }
        if self.public_webapp_url:
            extra["reply_markup"] = json.dumps(_webapp_markup(self.public_webapp_url))
        return bool(self._post("sendMessage", extra).get("ok"))

    def _send_to_explicit_thread(self, text: str, thread: int | None) -> bool:
        if not self.ready or not thread:
            return False
        return bool(
            self._post(
                "sendMessage",
                {
                    "chat_id": self.chat_id,
                    "text": text[:4096],
                    "message_thread_id": int(thread),
                },
            ).get("ok")
        )

    def report_farm_report(self, report: dict[str, Any]) -> bool:
        world = str(report.get("world") or "")
        thread = self.world_threads.get(world)
        if not thread:
            logger.info("Telegram topic для мира «{}» не настроен — отчёт только на диске", world or "не определён")
            return False
        resources = ", ".join(f"{key} {value}" for key, value in (report.get("resources") or {}).items()) or "не распознано"
        return self._send_to_explicit_thread(
            "⚔️ Боевой отчёт\n"
            f"Мир: {world}\n"
            f"Цель: {report.get('target') or report.get('subject') or 'не распознано'}\n"
            f"Свои потери: {report.get('own_losses') if report.get('own_losses') is not None else 'не распознано'}\n"
            f"Добыча: {resources}",
            thread,
        )

    def report_farm_summary(self, summary: dict[str, Any]) -> bool:
        if not self.thread_summary:
            logger.info("Telegram topic «сводный отчет» не настроен — сводка только на диске")
            return False
        resources = ", ".join(f"{key} {value}" for key, value in (summary.get("resources") or {}).items()) or "нет"
        return self._send_to_explicit_thread(
            "📊 Сводный отчёт фермы\n"
            f"Отчётов: {int(summary.get('reports') or 0)}\n"
            f"Свои потери: {int(summary.get('own_losses') or 0)}\n"
            f"Добыча: {resources}",
            self.thread_summary,
        )

    def send_photo(self, image_path: Path, caption: str, kind: str | None = None) -> bool:
        if not self.ready or not image_path.exists():
            return False
        data: dict[str, Any] = {
            "chat_id": self.chat_id,
            "caption": caption[:1024],
            **self._thread_payload(kind),
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
        gold: int = 0,
        rubies: int = 0,
    ) -> None:
        prefix = "🧪 DRY-RUN" if dry_run else "✅ Атака отправлена"
        kind_ru = {
            "baron": "Барон",
            "robber_barons": "Барон",
            "barbarian_tower": "Варварская башня",
            "barbarian_towers": "Варварская башня",
            "desert_tower": "Башня в пустыне",
            "desert_towers": "Башня в пустыне",
            "cultist_tower": "Башня культистов",
            "cultist_towers": "Башня культистов",
            "storm_fort": "Форт ураганов",
            "storm_forts": "Форт ураганов",
            "nomad": "Лагерь кочевников",
            "nomad_camps": "Лагерь кочевников",
            "samurai": "Лагерь самураев",
            "samurai_camps": "Лагерь самураев",
            "shogun": "Лагерь сёгуна",
        }.get(kind, kind)
        mins, sec = divmod(max(0, int(one_way_sec)), 60)
        ret_m, ret_s = divmod(max(0, int(return_sec)), 60)
        parsed_gold, parsed_rubies = parse_loot_amounts(extra)
        gold = int(gold or parsed_gold)
        rubies = int(rubies or parsed_rubies)
        text = (
            f"{prefix}\n"
            f"🎮 {account}\n"
            f"🎯 {kind_ru} · K{kingdom} ({x}, {y})\n"
            f"🧑‍✈️ Военачальник №{commander_no}\n"
            f"⏱ До цели: {mins} мин {sec} сек\n"
            f"↩️ Возврат через: {ret_m} мин {ret_s} сек"
        )
        if gold:
            text += f"\n💰 Золото: {gold}"
        if rubies:
            text += f"\n💎 Рубины: {rubies}"
        if extra:
            text += f"\n{extra}"
        if screenshot and screenshot.exists():
            self.send_photo(screenshot, text, kind=kind)
        else:
            self.send_text(text, kind=kind)

    def report_samurai_complete(self, attacks: int, gold: int = 0, rubies: int = 0) -> None:
        text = (
            "✅ Вторжение самураев закрыто\n"
            f"⚔️ Атак: {int(attacks)}\n"
            f"💰 Золото: {int(gold)}\n"
            f"💎 Рубины: {int(rubies)}\n"
            "Модуль лагерей самураев выключен."
        )
        self.send_text(text, kind="samurai")

    def report_nomad_complete(
        self,
        attacks: int,
        gold: int = 0,
        rubies: int = 0,
        resources: dict[str, int] | None = None,
    ) -> None:
        lines = [
            "✅ Вторжение кочевников закрыто",
            f"⚔️ Атак: {int(attacks)}",
        ]
        labels = {
            "wood": "🌲 Дерево",
            "stone": "🪨 Камень",
            "food": "🍖 Еда",
            "gold": "💰 Золото",
            "rubies": "💎 Рубины",
            "coal": "⬛ Уголь",
            "iron": "⚙️ Железо",
        }
        loot = dict(resources or {})
        if gold and "gold" not in loot:
            loot["gold"] = int(gold)
        if rubies and "rubies" not in loot:
            loot["rubies"] = int(rubies)
        if loot:
            for key, label in labels.items():
                if loot.get(key):
                    lines.append(f"{label}: {int(loot[key])}")
            extra = [f"{key}: {value}" for key, value in loot.items() if key not in labels]
            lines.extend(extra)
        else:
            lines.append(f"💰 Золото: {int(gold)}")
            lines.append(f"💎 Рубины: {int(rubies)}")
        lines.append("Модуль лагерей кочевников выключен.")
        self.send_text("\n".join(lines), kind="nomad")

    def report_stop(self, reason: str) -> None:
        self.send_text(f"⛔ Бот остановил атаки\n{reason}")

    def report_status(self, text: str) -> None:
        self.send_text(text)

    def report_shutdown_summary(
        self,
        attacks: int,
        gold: int,
        rubies: int,
        reason: str = "BlueStacks не найден 5 раз подряд",
    ) -> None:
        self.send_text(
            "⛔ Бот остановлен\n"
            f"{reason}\n"
            f"⚔️ Атак отправлено: {int(attacks)}\n"
            f"💰 Золота: {int(gold)}\n"
            f"💎 Рубинов: {int(rubies)}"
        )


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
