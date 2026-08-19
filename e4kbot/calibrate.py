from __future__ import annotations

import json
import time
from pathlib import Path

from e4kbot.bluestacks import AdbClient, capture_game_image
from e4kbot.config import load_config
from e4kbot.paths import LAYOUTS_DIR


BUTTONS = [
    "map",
    "search",
    "coord_x",
    "coord_y",
    "search_go",
    "target_center",
    "target_attack",
    "start_attack_confirm",
    "attack_cancel",
    "formation_close",
    "wave_clear",
    "center_flank",
    "unit_slot",
    "unit_slot_second",
    "picker_max",
    "picker_max_second",
    "picker_add",
    "unit_picker_confirm",
    "picker_confirm",
    "picker_cancel",
    "formation_attack",
    "feather_option",
    "gold_option",
    "travel_confirm",
    "movement_confirm",
    "travel_cancel",
]

REGIONS = [
    "main_castle_coords",
    "viewport_x",
    "viewport_y",
    "commander_number",
    "formation_units",
    "formation_tools",
    "picker_units",
    "feather_count",
    "travel_duration",
    "march_time",
    "return_timer",
    "castle_preview",
]


def main() -> None:
    try:
        import tkinter as tk
        from PIL import ImageTk
    except Exception as exc:
        raise SystemExit(f"Нужен tkinter/Pillow для калибровки: {exc}") from exc

    config = load_config()
    adb = AdbClient(config)
    adb.connect()
    image = capture_game_image(config, adb)
    if image is None:
        raise SystemExit("Нет скрина. Включи BlueStacks и игру.")

    LAYOUTS_DIR.mkdir(parents=True, exist_ok=True)
    out = {
        "resolution": list(image.size),
        "buttons": {},
        "regions": {},
    }
    queue = [(name, "point") for name in BUTTONS] + [(name, "region") for name in REGIONS]
    clicks: list[tuple[float, float]] = []

    root = tk.Tk()
    root.title("Калибровка E4K")
    preview = image.copy()
    preview.thumbnail((1280, 720))
    scale_x = image.size[0] / preview.size[0]
    scale_y = image.size[1] / preview.size[1]
    photo = ImageTk.PhotoImage(preview)
    label = tk.Label(root, image=photo)
    status = tk.Label(root, text="", font=("Segoe UI", 12))
    label.pack()
    status.pack()

    def prompt() -> None:
        if not queue:
            path = LAYOUTS_DIR / "default.json"
            path.write_text(json.dumps(out, indent=2), encoding="utf-8")
            status.config(text=f"Сохранено: {path}")
            root.after(1200, root.destroy)
            return
        name, kind = queue[0]
        if kind == "point":
            status.config(text=f"Клик по кнопке: {name}")
        else:
            need = 2 - len(clicks)
            status.config(text=f"Регион {name}: клик {3 - need}/2 (левый верх, правый низ)")

    def on_click(event: tk.Event) -> None:  # type: ignore[name-defined]
        if not queue:
            return
        name, kind = queue[0]
        nx = event.x / preview.size[0]
        ny = event.y / preview.size[1]
        if kind == "point":
            out["buttons"][name] = [round(nx, 4), round(ny, 4)]
            queue.pop(0)
            clicks.clear()
        else:
            clicks.append((nx, ny))
            if len(clicks) == 2:
                (x1, y1), (x2, y2) = clicks
                out["regions"][name] = [
                    round(min(x1, x2), 4),
                    round(min(y1, y2), 4),
                    round(max(x1, x2), 4),
                    round(max(y1, y2), 4),
                ]
                queue.pop(0)
                clicks.clear()
        prompt()

    label.bind("<Button-1>", on_click)
    prompt()
    root.mainloop()
    _ = (scale_x, scale_y)


if __name__ == "__main__":
    main()
