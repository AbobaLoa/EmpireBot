from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loguru import logger

from e4kbot.config import load_config
from e4kbot.control import CONTROL
from e4kbot.engine import AttackBot
from e4kbot.miniapp import run_miniapp
from e4kbot.paths import DATA_DIR, LOG_DIR, ensure_dirs
from e4kbot.state import StateStore
from e4kbot.telegram_bot import TelegramReporter


def _run_bot_thread(bot: AttackBot) -> None:
    try:
        bot.start()
    except Exception:
        logger.exception("Поток бота упал")


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, SystemError, OverflowError, ValueError):
        return False


def acquire_singleton() -> bool:
    """Refuse a second run.py so two bots cannot click the same planning screen."""
    ensure_dirs()
    lock_path = DATA_DIR / "bot.lock"
    pid = os.getpid()
    if lock_path.exists():
        try:
            old_pid = int(lock_path.read_text(encoding="utf-8").strip())
        except ValueError:
            old_pid = 0
        if old_pid and old_pid != pid and _pid_running(old_pid):
            logger.error(
                "EmpireBot уже запущен (pid {}). Второй процесс не стартую.",
                old_pid,
            )
            return False
    lock_path.write_text(str(pid), encoding="utf-8")
    return True


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


def cmd_run(max_cycles: int = 0, no_panel: bool = False) -> None:
    if not acquire_singleton():
        return
    config = load_config()
    if max_cycles:
        config["max_cycles"] = int(max_cycles)
    CONTROL.configure(config, startup=True)
    CONTROL.restart_hotkey()
    store = StateStore()
    telegram = TelegramReporter(config.get("telegram") or {})
    if telegram.token:
        telegram.resolve_baron_thread()
        if telegram.message_thread_id:
            logger.info("Telegram topic thread_id={}", telegram.message_thread_id)
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
            config,
        ),
        daemon=True,
    )
    miniapp_thread.start()

    bot = AttackBot(config, store, telegram)
    bot_thread = threading.Thread(target=_run_bot_thread, args=(bot,), name="e4k-bot", daemon=True)
    bot_thread.start()
    logger.info(
        "Панель: кнопка вкл/выкл. Горячая клавиша {} выключает бота сразу и отпускает мышь.",
        CONTROL.hotkey,
    )
    try:
        if no_panel:
            bot_thread.join()
        else:
            from e4kbot.panel import run_panel

            run_panel(config, store)
    except KeyboardInterrupt:
        logger.info("Остановлено вручную")
    finally:
        bot.stop = True
        CONTROL.shutdown()


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
        choices=["run", "calibrate", "check", "worker"],
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="Stop after N completed attack/attempt cycles (0 = unlimited)",
    )
    parser.add_argument(
        "--no-panel",
        action="store_true",
        help="Run without the desktop control window",
    )
    args = parser.parse_args()
    if args.command == "calibrate":
        cmd_calibrate()
    elif args.command == "check":
        cmd_check()
    elif args.command == "worker":
        from e4kbot.runtime.worker import main as worker_main

        worker_main()
    else:
        cmd_run(max_cycles=args.max_cycles, no_panel=args.no_panel)


if __name__ == "__main__":
    main()
