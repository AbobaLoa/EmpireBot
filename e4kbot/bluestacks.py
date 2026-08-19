from __future__ import annotations

import ctypes
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from loguru import logger
from PIL import Image

from e4kbot.paths import SHOTS_DIR

BLUESTACKS_PROCESS_HINTS = ("HD-Player.exe", "Bluestacks.exe", "BlueStacks")
GAME_PACKAGE_HINTS = (
    "air.com.goodgamestudios.empirefourkingdoms",
    "com.goodgamestudios.empire",
    "empirefourkingdoms",
    "goodgamestudios",
)


def _ps_list_process_names() -> set[str]:
    try:
        raw = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-Process | Select-Object -ExpandProperty ProcessName",
            ],
            text=True,
            timeout=12,
        )
    except Exception as exc:
        logger.warning(f"Не удалось получить список процессов: {exc}")
        return set()
    return {line.strip().lower() for line in raw.splitlines() if line.strip()}


def bluestacks_running() -> bool:
    names = _ps_list_process_names()
    return any(
        hint.lower().replace(".exe", "") in names
        or any(hint.lower().replace(".exe", "") in name for name in names)
        for hint in BLUESTACKS_PROCESS_HINTS
    )


def find_adb_binary(configured: str = "") -> str | None:
    if configured:
        path = Path(configured)
        if path.exists():
            return str(path)
    for candidate in (
        shutil.which("adb"),
        r"C:\Program Files\BlueStacks_nxt\HD-Adb.exe",
        r"C:\Program Files\BlueStacks\HD-Adb.exe",
        r"C:\Program Files (x86)\BlueStacks\HD-Adb.exe",
        r"C:\Program Files\BlueStacks_msi5\HD-Adb.exe",
    ):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


class AdbClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        bs = config.get("bluestacks") or {}
        self.host = str(bs.get("adb_host") or "127.0.0.1")
        self.ports = [int(p) for p in (bs.get("adb_ports") or [5555, 5556])]
        self.adb = find_adb_binary(str(bs.get("adb_path") or ""))
        self.serial: str | None = None
        self.display_size: tuple[int, int] = (900, 1600)

    def available(self) -> bool:
        return bool(self.adb)

    def _run(self, args: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
        if not self.adb:
            raise RuntimeError("ADB не найден")
        return subprocess.run(
            [self.adb, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="ignore",
        )

    def connect(self) -> str | None:
        if not self.adb:
            logger.warning("ADB не найден — скрины через окно BlueStacks")
            return None
        for port in self.ports:
            serial = f"{self.host}:{port}"
            self._run(["connect", serial], timeout=8)
            devices = self._run(["devices"], timeout=8).stdout
            if serial in devices and "offline" not in devices:
                self.serial = serial
                logger.info(f"ADB подключён: {serial}")
                return serial
        logger.warning("ADB устройства BlueStacks не найдены")
        return None

    def shell(self, command: str, timeout: int = 20) -> str:
        if not self.serial:
            return ""
        proc = self._run(["-s", self.serial, "shell", command], timeout=timeout)
        if proc.returncode:
            logger.debug(f"ADB shell failed: {proc.stderr.strip()}")
        return proc.stdout or ""

    def game_running(self, package: str) -> bool:
        focus = self.shell("dumpsys window | grep -E mCurrentFocus|mFocusedApp")
        haystack = (focus or "").lower()
        if any(hint in haystack for hint in GAME_PACKAGE_HINTS):
            return True
        if package and package.lower() in haystack:
            return True
        activities = self.shell("dumpsys activity activities | grep -i empire")
        blob = f"{focus}\n{activities}".lower()
        return any(hint in blob for hint in GAME_PACKAGE_HINTS)

    def tap(self, x: int, y: int) -> None:
        if self.serial:
            proc = self._run(
                ["-s", self.serial, "shell", "input", "tap", str(int(x)), str(int(y))],
                timeout=8,
            )
            if proc.returncode == 0:
                return
            logger.debug(f"ADB tap unavailable, using window click: {proc.stderr.strip()}")
        click_game_window(self.config, int(x), int(y), self.display_size)

    def text(self, value: str) -> None:
        if not self.serial:
            raise RuntimeError("ADB не подключён")
        safe = str(value).replace(" ", "%s")
        self.shell(f"input text {safe}")

    def key(self, keycode: int) -> None:
        self.shell(f"input keyevent {int(keycode)}")

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 450) -> None:
        if self.serial:
            proc = self._run(
                [
                    "-s",
                    self.serial,
                    "shell",
                    "input",
                    "swipe",
                    str(int(x1)),
                    str(int(y1)),
                    str(int(x2)),
                    str(int(y2)),
                    str(int(duration_ms)),
                ],
                timeout=8,
            )
            if proc.returncode == 0:
                return
        drag_game_window(self.config, x1, y1, x2, y2, self.display_size, duration_ms)

    def screencap(self) -> Image.Image | None:
        if not self.adb or not self.serial:
            return None
        try:
            raw = subprocess.check_output(
                [self.adb, "-s", self.serial, "exec-out", "screencap", "-p"],
                timeout=20,
            )
            from io import BytesIO

            image = Image.open(BytesIO(raw)).convert("RGB")
            self.display_size = image.size
            return image
        except Exception as exc:
            logger.debug(f"ADB screencap failed: {exc}")
            return None


def _render_window(hwnd: int) -> int:
    """Return the BlueStacks render child instead of its toolbar frame."""
    try:
        import win32gui

        children: list[int] = []

        def _cb(child: int, _: Any) -> None:
            if win32gui.GetClassName(child) == "BlueStacksApp":
                children.append(child)

        win32gui.EnumChildWindows(hwnd, _cb, None)
        return children[0] if children else hwnd
    except Exception:
        return hwnd


def click_game_window(
    config: dict[str, Any],
    x: int,
    y: int,
    source_size: tuple[int, int],
) -> None:
    """Click an Android-space point through the visible BlueStacks window."""
    import win32api
    import win32con
    import win32gui

    hints = (config.get("bluestacks") or {}).get("window_title_hints")
    window = find_game_window(list(hints) if hints else None)
    if not window:
        raise RuntimeError("Окно BlueStacks не найдено для клика")
    hwnd = _render_window(window[0])
    px, py, mapping = game_window_point(hwnd, x, y, source_size)
    logger.debug(
        "BlueStacks point screenshot=({}, {}) source={} -> screen=({}, {}), "
        "render_client={} origin={}",
        x,
        y,
        source_size,
        px,
        py,
        mapping["client_size"],
        mapping["screen_origin"],
    )
    win32api.SetCursorPos((px, py))
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0)
    time.sleep(0.06)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0)


def game_window_point(
    hwnd: int,
    x: int,
    y: int,
    source_size: tuple[int, int],
) -> tuple[int, int, dict[str, tuple[int, int]]]:
    """Map screenshot pixels to the render child's client rectangle."""
    import win32gui

    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    width, height = right - left, bottom - top
    if width < 50 or height < 50:
        raise RuntimeError("Окно BlueStacks свёрнуто")
    screen_left, screen_top = win32gui.ClientToScreen(hwnd, (left, top))
    src_w, src_h = source_size
    sx = max(0, min(src_w - 1, int(x)))
    sy = max(0, min(src_h - 1, int(y)))
    px = screen_left + round(sx * width / src_w)
    py = screen_top + round(sy * height / src_h)
    return px, py, {
        "client_size": (width, height),
        "screen_origin": (screen_left, screen_top),
    }


def drag_game_window(
    config: dict[str, Any],
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    source_size: tuple[int, int],
    duration_ms: int = 450,
) -> None:
    import win32api
    import win32con
    import win32gui

    hints = (config.get("bluestacks") or {}).get("window_title_hints")
    window = find_game_window(list(hints) if hints else None)
    if not window:
        raise RuntimeError("Окно BlueStacks не найдено для жеста")
    hwnd = _render_window(window[0])
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    width, height = right - left, bottom - top
    screen_left, screen_top = win32gui.ClientToScreen(hwnd, (left, top))
    src_w, src_h = source_size

    def point(x: int, y: int) -> tuple[int, int]:
        return (
            screen_left + round(x * width / src_w),
            screen_top + round(y * height / src_h),
        )

    start, finish = point(x1, y1), point(x2, y2)
    win32api.SetCursorPos(start)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0)
    steps = max(4, int(duration_ms / 40))
    for index in range(1, steps + 1):
        nx = round(start[0] + (finish[0] - start[0]) * index / steps)
        ny = round(start[1] + (finish[1] - start[1]) * index / steps)
        win32api.SetCursorPos((nx, ny))
        time.sleep(duration_ms / steps / 1000)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0)


def _window_enum() -> list[tuple[int, str]]:
    import win32gui

    found: list[tuple[int, str]] = []

    def _cb(hwnd: int, _: Any) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd) or ""
        if title.strip():
            found.append((hwnd, title))

    win32gui.EnumWindows(_cb, None)
    return found


def find_game_window(hints: list[str] | None = None) -> tuple[int, str] | None:
    hints = [h.lower() for h in (hints or ["BlueStacks", "HD-Player", "Empire"])]
    try:
        windows = _window_enum()
    except Exception as exc:
        logger.warning(f"Поиск окна: {exc}")
        return None
    for hwnd, title in windows:
        low = title.lower()
        if any(hint in low for hint in hints):
            return hwnd, title
    return None


def screenshot_window(hwnd: int) -> Image.Image | None:
    try:
        import win32gui
        import win32ui
        from PIL import Image as PilImage

        left, top, right, bottom = win32gui.GetClientRect(hwnd)
        width, height = right - left, bottom - top
        if width < 50 or height < 50:
            return None
        left, top = win32gui.ClientToScreen(hwnd, (left, top))
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)
        ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 3)
        bmpinfo = bitmap.GetInfo()
        bmpstr = bitmap.GetBitmapBits(True)
        image = PilImage.frombuffer(
            "RGB",
            (bmpinfo["bmWidth"], bmpinfo["bmHeight"]),
            bmpstr,
            "raw",
            "BGRX",
            0,
            1,
        )
        win32gui.DeleteObject(bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)
        return image.convert("RGB")
    except Exception:
        try:
            import mss
            import win32gui

            rect = win32gui.GetWindowRect(hwnd)
            with mss.mss() as sct:
                shot = sct.grab(
                    {
                        "left": rect[0],
                        "top": rect[1],
                        "width": max(1, rect[2] - rect[0]),
                        "height": max(1, rect[3] - rect[1]),
                    }
                )
                return Image.frombytes("RGB", shot.size, shot.rgb)
        except Exception as exc:
            logger.debug(f"window screenshot failed: {exc}")
            return None


def capture_game_image(config: dict[str, Any], adb: AdbClient | None = None) -> Image.Image | None:
    if adb:
        image = adb.screencap()
        if image is not None:
            return image
    hints = (config.get("bluestacks") or {}).get("window_title_hints") or [
        "BlueStacks",
        "HD-Player",
        "Empire",
    ]
    window = find_game_window(list(hints))
    if not window:
        return None
    return screenshot_window(window[0])


def save_shot(image: Image.Image, name: str) -> Path:
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SHOTS_DIR / name
    image.save(path, "PNG")
    return path


def wait_until_ready(config: dict[str, Any], adb: AdbClient | None = None) -> None:
    if not config.get("require_bluestacks", True):
        return
    while not bluestacks_running():
        logger.info("BlueStacks не запущен — бот ждёт HD-Player")
        time.sleep(5)
    if not config.get("require_game_running", True) or not adb:
        return
    package = str((config.get("bluestacks") or {}).get("package") or "")
    if not adb.serial:
        return
    while not adb.game_running(package):
        logger.info("Игра в BlueStacks не на переднем плане — ждём")
        time.sleep(5)
