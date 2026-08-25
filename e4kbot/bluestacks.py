from __future__ import annotations

import ctypes
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from loguru import logger
from PIL import Image

from e4kbot.control import CONTROL
from e4kbot.paths import SHOTS_DIR

BLUESTACKS_PROCESS_HINTS = ("HD-Player.exe", "Bluestacks.exe", "BlueStacks")
GAME_PACKAGE_HINTS = (
    "air.com.goodgamestudios.empirefourkingdoms",
    "com.goodgamestudios.empire",
    "empirefourkingdoms",
    "goodgamestudios",
)
WINDOW_EXCLUDE_HINTS = (
    "empirebot",
    "visual studio",
    "cursor",
    "notepad",
    "powershell",
    "windows terminal",
)
WINDOW_PREFERRED_HINTS = ("bluestacks", "hd-player")
RENDER_CLASS_HINTS = ("bluestacksapp", "android", "guest", "render")
_DPI_READY = False


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
                method = self._input_method()
                if method == "adb":
                    logger.info("Клики: тихий ADB input tap (курсор Windows не двигается)")
                else:
                    logger.info("Клики: видимый курсор по окну BlueStacks")
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

    def _input_method(self) -> str:
        return str((self.config.get("bluestacks") or {}).get("input") or "mouse").lower()

    def tap(
        self,
        x: int,
        y: int,
        source_size: tuple[int, int] | None = None,
    ) -> None:
        """Click in the visible BlueStacks window so the cursor moves; ADB is fallback."""
        CONTROL.check()
        size = source_size or self.display_size
        method = self._input_method()
        if method != "adb":
            try:
                click_game_window(self.config, int(x), int(y), size)
                return
            except Exception as exc:
                logger.warning("Клик мышью по окну не удался ({}), пробую ADB", exc)
        if self.serial:
            proc = self._run(
                ["-s", self.serial, "shell", "input", "tap", str(int(x)), str(int(y))],
                timeout=8,
            )
            if proc.returncode == 0:
                return
            logger.debug(f"ADB tap unavailable, using window click: {proc.stderr.strip()}")
        click_game_window(self.config, int(x), int(y), size)

    def text(self, value: str) -> None:
        if not self.serial:
            raise RuntimeError("ADB не подключён")
        safe = str(value).replace(" ", "%s")
        self.shell(f"input text {safe}")

    def key(self, keycode: int) -> None:
        """Send an Android keyevent; fall back to focused-window Escape when ADB shell is dead."""
        if self.serial:
            proc = self._run(
                ["-s", self.serial, "shell", "input", "keyevent", str(int(keycode))],
                timeout=8,
            )
            if proc.returncode == 0:
                return
            logger.debug("ADB keyevent failed ({}) — пробую окно BlueStacks", proc.stderr.strip())
        if int(keycode) == 4:
            press_escape_on_game(self.config)

    def swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 450,
        source_size: tuple[int, int] | None = None,
    ) -> None:
        CONTROL.check()
        size = source_size or self.display_size
        method = self._input_method()
        if method != "adb":
            try:
                drag_game_window(self.config, x1, y1, x2, y2, size, duration_ms)
                return
            except Exception as exc:
                logger.warning("Жест мышью по окну не удался ({}), пробую ADB", exc)
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
        drag_game_window(self.config, x1, y1, x2, y2, size, duration_ms)

    def wheel(
        self,
        x: int,
        y: int,
        delta: int = -240,
        source_size: tuple[int, int] | None = None,
    ) -> None:
        """Mouse wheel over a game point. Negative delta scrolls the list down."""
        CONTROL.check()
        size = source_size or self.display_size
        method = self._input_method()
        if method != "adb":
            try:
                wheel_game_window(self.config, int(x), int(y), size, delta)
                return
            except Exception as exc:
                logger.warning("Колёсико мыши не удалось ({}), пробую жест вниз", exc)
        self.swipe(int(x), int(y), int(x), int(y) + 36, 160, size)

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


def press_escape_on_game(config: dict[str, Any]) -> None:
    """VK_ESCAPE to the BlueStacks HWND — closes mid-map offer / hire overlays."""
    import win32con
    import win32gui

    hints = (config.get("bluestacks") or {}).get("window_title_hints")
    window = find_game_window(list(hints) if hints else None)
    if not window:
        return
    hwnd = window[0]
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.05)
    try:
        win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_ESCAPE, 0)
        win32gui.PostMessage(hwnd, win32con.WM_KEYUP, win32con.VK_ESCAPE, 0)
    except Exception:
        try:
            ctypes.windll.user32.keybd_event(0x1B, 0, 0, 0)
            ctypes.windll.user32.keybd_event(0x1B, 0, 2, 0)
        except Exception as exc:
            logger.debug("Escape в окно BlueStacks не отправился: {}", exc)


def _ensure_dpi_aware() -> None:
    """Use physical screen pixels so SetCursorPos matches HWND / client rect."""
    global _DPI_READY
    if _DPI_READY:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    _DPI_READY = True


def _window_dpi(hwnd: int) -> int:
    try:
        dpi = int(ctypes.windll.user32.GetDpiForWindow(hwnd))
        return dpi if dpi > 0 else 96
    except Exception:
        return 96


def _client_area(hwnd: int) -> tuple[int, int]:
    import win32gui

    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    return max(0, right - left), max(0, bottom - top)


def mapped_playfield(
    width: int,
    height: int,
    src_w: int,
    src_h: int,
) -> tuple[int, int, int, int]:
    """Inscribe the Android 900×1600 aspect inside the HWND client rect."""
    src_w = max(1, int(src_w))
    src_h = max(1, int(src_h))
    width = max(1, int(width))
    height = max(1, int(height))
    src_aspect = src_w / src_h
    dst_aspect = width / height
    if abs(dst_aspect - src_aspect) < 0.04:
        return 0, 0, width, height
    if dst_aspect > src_aspect:
        play_h = height
        play_w = max(1, round(height * src_aspect))
        return (width - play_w) // 2, 0, play_w, play_h
    play_w = width
    play_h = max(1, round(width / src_aspect))
    return 0, (height - play_h) // 2, play_w, play_h


def score_game_window(
    title: str,
    hints: list[str] | None = None,
    process: str = "",
) -> int:
    """Rank a top-level window. Negative means never click it (EmpireBot panel)."""
    low = (title or "").lower()
    proc = (process or "").lower().replace(".exe", "")
    if any(hint in low for hint in WINDOW_EXCLUDE_HINTS):
        return -1
    if "empirebot" in proc:
        return -1
    hints = [h.lower() for h in (hints or ["BlueStacks", "HD-Player"])]
    score = 0
    if any(hint in low for hint in WINDOW_PREFERRED_HINTS) or proc in {
        "hd-player",
        "bluestacks",
        "bluestacksservices",
    }:
        score += 100
    if "four kingdom" in low or "empirefourkingdoms" in low:
        score += 30
    for hint in hints:
        if hint in WINDOW_EXCLUDE_HINTS:
            continue
        if hint == "empire" and not (
            any(token in low for token in WINDOW_PREFERRED_HINTS)
            or "four kingdom" in low
            or "empirefourkingdoms" in low
        ):
            continue
        if hint and hint in low:
            score += 10
    return score


def _window_process_name(hwnd: int) -> str:
    try:
        import win32api
        import win32process

        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        handle = win32api.OpenProcess(0x1000, False, pid)
        try:
            return Path(win32process.GetModuleFileNameEx(handle, 0)).name
        finally:
            win32api.CloseHandle(handle)
    except Exception:
        return ""


def _render_window(hwnd: int) -> int:
    """Return the BlueStacks Android surface, not the sidebar / title-bar frame."""
    try:
        import win32gui

        named: list[int] = []
        large: list[int] = []
        parent_w, parent_h = _client_area(hwnd)
        parent_area = max(1, parent_w * parent_h)

        def _cb(child: int, _: Any) -> None:
            try:
                if not win32gui.IsWindowVisible(child):
                    return
                cls = (win32gui.GetClassName(child) or "").lower()
                width, height = _client_area(child)
                if width < 80 or height < 80:
                    return
                if cls == "bluestacksapp" or any(hint in cls for hint in RENDER_CLASS_HINTS):
                    named.append(child)
                elif width * height >= parent_area * 0.45:
                    large.append(child)
            except Exception:
                return

        win32gui.EnumChildWindows(hwnd, _cb, None)
        pool = named or large
        if not pool:
            return hwnd
        return max(pool, key=lambda child: (lambda size: size[0] * size[1])(_client_area(child)))
    except Exception:
        return hwnd


def _mouse_down_up() -> None:
    """Real left-button down/up via SendInput so BlueStacks sees the click."""
    import ctypes

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = (
            ("dx", ctypes.c_long),
            ("dy", ctypes.c_long),
            ("mouseData", ctypes.c_ulong),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        )

    class INPUT(ctypes.Structure):
        class _I(ctypes.Union):
            _fields_ = (("mi", MOUSEINPUT),)

        _anonymous_ = ("_i",)
        _fields_ = (("type", ctypes.c_ulong), ("_i", _I))

    extra = ctypes.c_ulong(0)
    for flags in (0x0002, 0x0004):  # LEFTDOWN, LEFTUP
        packet = INPUT()
        packet.type = 0
        packet.mi = MOUSEINPUT(0, 0, 0, flags, 0, ctypes.pointer(extra))
        ctypes.windll.user32.SendInput(1, ctypes.byref(packet), ctypes.sizeof(INPUT))
        time.sleep(0.04)


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
    try:
        win32gui.ShowWindow(window[0], win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(window[0])
    except Exception:
        pass
    CONTROL.check()
    px, py, mapping = game_window_point(hwnd, x, y, source_size)
    logger.debug(
        "BlueStacks click '{}' hwnd={} screenshot=({}, {}) source={} -> screen=({}, {}), "
        "playfield={} origin={} dpi={}",
        window[1],
        hwnd,
        x,
        y,
        source_size,
        px,
        py,
        mapping["playfield"],
        mapping["screen_origin"],
        mapping["dpi"],
    )
    CONTROL.check()
    origin_x, origin_y = mapping["screen_origin"]
    ox, oy, play_w, play_h = mapping["playfield"]
    if not (
        origin_x + ox <= px < origin_x + ox + play_w
        and origin_y + oy <= py < origin_y + oy + play_h
        and (px, py) != (0, 0)
    ):
        raise RuntimeError(
            f"Клик ({px}, {py}) вне игрового поля BlueStacks "
            f"origin={mapping['screen_origin']} playfield={mapping['playfield']}"
        )
    CONTROL.check()
    win32api.SetCursorPos((px, py))
    time.sleep(0.05)
    CONTROL.check()
    _mouse_down_up()
    try:
        client_x, client_y = win32gui.ScreenToClient(hwnd, (px, py))
        lparam = win32api.MAKELONG(int(client_x), int(client_y))
        win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
        time.sleep(0.03)
        win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lparam)
    except Exception:
        pass


def game_window_point(
    hwnd: int,
    x: int,
    y: int,
    source_size: tuple[int, int],
) -> tuple[int, int, dict[str, Any]]:
    """Map ADB screenshot pixels onto the game viewport inside the BlueStacks HWND."""
    import win32gui

    _ensure_dpi_aware()
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    width, height = right - left, bottom - top
    if width < 50 or height < 50:
        raise RuntimeError("Окно BlueStacks свёрнуто")
    screen_left, screen_top = win32gui.ClientToScreen(hwnd, (left, top))
    src_w, src_h = source_size
    ox, oy, play_w, play_h = mapped_playfield(width, height, src_w, src_h)
    sx = max(0, min(src_w - 1, int(x)))
    sy = max(0, min(src_h - 1, int(y)))
    px = screen_left + ox + round(sx * play_w / src_w)
    py = screen_top + oy + round(sy * play_h / src_h)
    window_rect = win32gui.GetWindowRect(hwnd)
    return px, py, {
        "client_size": (width, height),
        "screen_origin": (screen_left, screen_top),
        "playfield": (ox, oy, play_w, play_h),
        "window_rect": window_rect,
        "title_bar_offset": (screen_left - window_rect[0], screen_top - window_rect[1]),
        "dpi": _window_dpi(hwnd),
    }


def game_to_screen(
    hwnd: int,
    x: int,
    y: int,
    source_size: tuple[int, int],
) -> tuple[int, int]:
    """Convert a game-viewport pixel to a physical screen coordinate."""
    px, py, _ = game_window_point(hwnd, x, y, source_size)
    return px, py


def cursor_game_point(
    config: dict[str, Any],
    source_size: tuple[int, int] = (900, 1600),
) -> tuple[float, float] | None:
    """Return current cursor as normalized Android-space point inside BlueStacks."""
    import win32api
    import win32gui

    hints = (config.get("bluestacks") or {}).get("window_title_hints")
    window = find_game_window(list(hints) if hints else None)
    if not window:
        return None
    hwnd = _render_window(window[0])
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    width, height = right - left, bottom - top
    if width < 50 or height < 50:
        return None
    origin_x, origin_y = win32gui.ClientToScreen(hwnd, (left, top))
    src_w, src_h = source_size
    ox, oy, play_w, play_h = mapped_playfield(width, height, src_w, src_h)
    cursor_x, cursor_y = win32api.GetCursorPos()
    rel_x, rel_y = cursor_x - origin_x - ox, cursor_y - origin_y - oy
    if not (0 <= rel_x < play_w and 0 <= rel_y < play_h):
        return None
    source_x = rel_x * src_w / play_w
    source_y = rel_y * src_h / play_h
    return source_x / src_w, source_y / src_h


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
    start = game_to_screen(hwnd, x1, y1, source_size)
    finish = game_to_screen(hwnd, x2, y2, source_size)
    CONTROL.check()
    win32api.SetCursorPos(start)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0)
    steps = max(4, int(duration_ms / 40))
    try:
        for index in range(1, steps + 1):
            CONTROL.check()
            nx = round(start[0] + (finish[0] - start[0]) * index / steps)
            ny = round(start[1] + (finish[1] - start[1]) * index / steps)
            win32api.SetCursorPos((nx, ny))
            time.sleep(duration_ms / steps / 1000)
    finally:
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0)


def wheel_game_window(
    config: dict[str, Any],
    x: int,
    y: int,
    source_size: tuple[int, int],
    delta: int = -240,
) -> None:
    """Spin the mouse wheel over a game point. Negative delta = scroll down."""
    import win32api
    import win32con

    hints = (config.get("bluestacks") or {}).get("window_title_hints")
    window = find_game_window(list(hints) if hints else None)
    if not window:
        raise RuntimeError("Окно BlueStacks не найдено для колёсика")
    hwnd = _render_window(window[0])
    point = game_to_screen(hwnd, x, y, source_size)
    CONTROL.check()
    win32api.SetCursorPos(point)
    time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, int(delta), 0)



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
    hints = list(hints or ["BlueStacks", "HD-Player"])
    try:
        windows = _window_enum()
    except Exception as exc:
        logger.warning(f"Поиск окна: {exc}")
        return None
    ranked: list[tuple[int, int, str]] = []
    for hwnd, title in windows:
        process = _window_process_name(hwnd)
        score = score_game_window(title, hints, process)
        if score <= 0:
            continue
        ranked.append((score, hwnd, title))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    hwnd, title = ranked[0][1], ranked[0][2]
    return hwnd, title


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
    ]
    window = find_game_window(list(hints))
    if not window:
        return None
    return screenshot_window(_render_window(window[0]))


def diagnose_targeting(config: dict[str, Any], adb: AdbClient | None = None) -> dict[str, Any]:
    """Log HWND / DPI / mapped sample points. Does not click and does not send attacks."""
    _ensure_dpi_aware()
    hints = (config.get("bluestacks") or {}).get("window_title_hints")
    window = find_game_window(list(hints) if hints else None)
    info: dict[str, Any] = {
        "input": str((config.get("bluestacks") or {}).get("input") or "mouse"),
        "adb": getattr(adb, "serial", None),
        "screenshot": getattr(adb, "display_size", (900, 1600)),
    }
    if not window:
        logger.warning("Привязка кликов: окно BlueStacks не найдено (панель EmpireBot игнорируется)")
        return info
    import win32gui

    parent, title = window
    hwnd = _render_window(parent)
    cls = ""
    try:
        cls = win32gui.GetClassName(hwnd)
    except Exception:
        pass
    source = info["screenshot"]
    if adb:
        shot = adb.screencap()
        if shot is not None:
            source = shot.size
            info["screenshot"] = source
    center = (source[0] // 2, source[1] // 2)
    slot = (round(0.073 * source[0]), round(0.697 * source[1]))
    try:
        cx, cy, mapping = game_window_point(hwnd, center[0], center[1], source)
        sx, sy, _ = game_window_point(hwnd, slot[0], slot[1], source)
    except Exception as exc:
        logger.warning("Привязка кликов не рассчиталась: {}", exc)
        return info
    ox, oy, play_w, play_h = mapping["playfield"]
    origin = mapping["screen_origin"]
    inside = (
        origin[0] + ox <= cx < origin[0] + ox + play_w
        and origin[1] + oy <= cy < origin[1] + oy + play_h
        and (cx, cy) != (0, 0)
    )
    logger.info(
        "Привязка кликов: окно='{}' class={} hwnd={} process={} dpi={} "
        "window_rect={} client={} origin={} title_bar={} playfield={} "
        "ADB={} input={} центр_игры=({}, {}) слот_солдат=({}, {}) внутри_поля={}",
        title,
        cls,
        hwnd,
        _window_process_name(parent) or _window_process_name(hwnd),
        mapping["dpi"],
        mapping["window_rect"],
        mapping["client_size"],
        origin,
        mapping["title_bar_offset"],
        mapping["playfield"],
        info["adb"],
        info["input"],
        cx,
        cy,
        sx,
        sy,
        inside,
    )
    info.update(
        {
            "title": title,
            "hwnd": hwnd,
            "center": (cx, cy),
            "unit_slot": (sx, sy),
            "inside_playfield": inside,
            "mapping": mapping,
        }
    )
    return info


def save_shot(image: Image.Image, name: str) -> Path:
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SHOTS_DIR / name
    image.save(path, "PNG")
    return path


def probe_bluestacks(config: dict[str, Any], adb: AdbClient | None = None) -> str:
    """Return 'ok', 'no_process', or 'no_window' without waiting forever."""
    if not config.get("require_bluestacks", True):
        return "ok"
    if not bluestacks_running():
        return "no_process"
    if capture_game_image(config, adb) is None:
        return "no_window"
    return "ok"


def wait_until_ready(config: dict[str, Any], adb: AdbClient | None = None) -> None:
    if not config.get("require_bluestacks", True):
        return
    while not bluestacks_running():
        CONTROL.check()
        logger.info("BlueStacks не запущен — бот ждёт HD-Player")
        CONTROL.sleep(5)
    if not config.get("require_game_running", True) or not adb:
        return
    package = str((config.get("bluestacks") or {}).get("package") or "")
    if not adb.serial:
        return
    while not adb.game_running(package):
        CONTROL.check()
        logger.info("Игра в BlueStacks не на переднем плане — ждём")
        CONTROL.sleep(5)


_ensure_dpi_aware()
