from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loguru import logger

from e4kbot.config import load_config
from e4kbot.engine import AttackBot
from e4kbot.miniapp import run_miniapp
from e4kbot.paths import LOG_DIR, ensure_dirs
from e4kbot.state import StateStore
from e4kbot.telegram_bot import TelegramReporter


def setup_logging() -> None:
    ensure_dirs()
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    logger.add(
        LOG_DIR / "bot_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        retention="14 days",
        encoding="utf-8",
        level="DEBUG",
    )


def cmd_run() -> None:
    config = load_config()
    store = StateStore()
    telegram = TelegramReporter(config.get("telegram") or {})
    me = telegram.verify() if telegram.token else {}
    if me:
        logger.info(f"Telegram: @{me.get('username')}")
        telegram.set_menu_button()

    tg_cfg = config.get("telegram") or {}
    miniapp_thread = threading.Thread(
        target=run_miniapp,
        args=(
            store,
            str(tg_cfg.get("miniapp_host") or "127.0.0.1"),
            int(tg_cfg.get("miniapp_port") or 8766),
        ),
        daemon=True,
    )
    miniapp_thread.start()

    bot = AttackBot(config, store, telegram)
    try:
        bot.start()
    except KeyboardInterrupt:
        bot.stop = True
        logger.info("Остановлено вручную")


def cmd_calibrate() -> None:
    from e4kbot.calibrate import main as calibrate_main

    calibrate_main()


def cmd_check() -> None:
    from e4kbot.bluestacks import AdbClient, bluestacks_running, capture_game_image, save_shot

    config = load_config()
    print("BlueStacks process:", bluestacks_running())
    adb = AdbClient(config)
    print("ADB binary:", adb.adb)
    print("ADB serial:", adb.connect())
    image = capture_game_image(config, adb)
    if image:
        path = save_shot(image, "check.png")
        print("Screenshot:", path)
    else:
        print("Screenshot: нет")
    telegram = TelegramReporter(config.get("telegram") or {})
    me = telegram.verify() if telegram.token else {}
    print("Telegram:", me.get("username") if me else "токен не задан / ошибка")
    print("engine:", config.get("engine"))
    print("attack_style:", config.get("attack_style"))
    print("dry_run:", config.get("dry_run"))
    print("Логин в игру не нужен — открываешь Empire сам в BlueStacks.")


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="E4K desktop attack bot")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "calibrate", "check"],
    )
    args = parser.parse_args()
    if args.command == "calibrate":
        cmd_calibrate()
    elif args.command == "check":
        cmd_check()
    else:
        cmd_run()


if __name__ == "__main__":
    main()
