from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from e4kbot.config import save_config
from e4kbot.control import CONTROL, apply_public_settings, public_settings
from e4kbot.state import StateStore


class ControlPanel:
    def __init__(self, config: dict[str, Any], store: StateStore) -> None:
        self.config = config
        self.store = store
        self.root = tk.Tk()
        self.root.title("EmpireBot")
        self.root.geometry("360x640+40+40")
        self.root.minsize(340, 560)
        self.root.configure(bg="#12141c")
        self.root.attributes("-topmost", CONTROL.always_on_top)
        self._binding = False
        self._status = tk.StringVar()
        self._hotkey = tk.StringVar(value=f"Клавиша: {CONTROL.hotkey}")
        self._enabled_label = tk.StringVar()
        self._build()
        CONTROL.on_change(self._schedule_refresh)
        self._refresh()
        self.root.after(500, self._tick)

    def _build(self) -> None:
        pad = {"padx": 12, "pady": 6}
        title = tk.Label(
            self.root,
            text="EmpireBot",
            fg="#e4c27a",
            bg="#12141c",
            font=("Segoe UI", 16, "bold"),
        )
        title.pack(anchor="w", **pad)

        self.toggle_btn = tk.Button(
            self.root,
            text="ВЫКЛ",
            font=("Segoe UI", 18, "bold"),
            command=self._toggle,
            relief="flat",
            height=2,
        )
        self.toggle_btn.pack(fill="x", padx=12, pady=(0, 4))

        tk.Label(
            self.root,
            textvariable=self._enabled_label,
            fg="#9aa3b5",
            bg="#12141c",
            font=("Segoe UI", 10),
        ).pack(anchor="w", padx=12)

        hotkey_row = tk.Frame(self.root, bg="#12141c")
        hotkey_row.pack(fill="x", padx=12, pady=6)
        tk.Label(
            hotkey_row,
            textvariable=self._hotkey,
            fg="#f3f4f8",
            bg="#12141c",
            font=("Segoe UI", 11, "bold"),
        ).pack(side="left")
        tk.Button(
            hotkey_row,
            text="Сменить",
            command=self._start_bind,
            relief="flat",
            bg="#2a3144",
            fg="#f3f4f8",
        ).pack(side="right")

        tk.Label(
            self.root,
            text="Нажми назначенную букву в любой момент — бот сразу отпустит мышь.",
            fg="#9aa3b5",
            bg="#12141c",
            wraplength=320,
            justify="left",
            font=("Segoe UI", 9),
        ).pack(anchor="w", padx=12)

        tk.Label(
            self.root,
            textvariable=self._status,
            fg="#c9d0de",
            bg="#12141c",
            wraplength=320,
            justify="left",
            font=("Segoe UI", 9),
        ).pack(anchor="w", padx=12, pady=(8, 4))

        settings = tk.LabelFrame(
            self.root,
            text=" Настройки ",
            fg="#e4c27a",
            bg="#1c2130",
            font=("Segoe UI", 10, "bold"),
        )
        settings.pack(fill="both", expand=True, padx=12, pady=8)

        self.kind = tk.StringVar()
        self.dry_run = tk.BooleanVar()
        self.use_feathers = tk.BooleanVar()
        self.gold_fallback = tk.BooleanVar()
        self.always_on_top = tk.BooleanVar()
        self.max_concurrent = tk.IntVar()
        self.delay_min = tk.IntVar()
        self.delay_max = tk.IntVar()
        self.input_method = tk.StringVar()

        self._load_vars()

        grid = tk.Frame(settings, bg="#1c2130")
        grid.pack(fill="both", expand=True, padx=10, pady=8)

        tk.Label(grid, text="Цель", fg="#9aa3b5", bg="#1c2130").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            grid,
            textvariable=self.kind,
            values=["baron", "nomad", "shogun"],
            state="readonly",
            width=16,
        ).grid(row=0, column=1, sticky="ew", pady=3)

        tk.Label(grid, text="В пути, макс", fg="#9aa3b5", bg="#1c2130").grid(row=1, column=0, sticky="w")
        tk.Spinbox(grid, from_=1, to=30, textvariable=self.max_concurrent, width=16).grid(
            row=1, column=1, sticky="ew", pady=3
        )

        tk.Label(grid, text="Пауза атак, сек", fg="#9aa3b5", bg="#1c2130").grid(row=2, column=0, sticky="w")
        delays = tk.Frame(grid, bg="#1c2130")
        delays.grid(row=2, column=1, sticky="ew", pady=3)
        tk.Spinbox(delays, from_=1, to=60, textvariable=self.delay_min, width=6).pack(side="left")
        tk.Label(delays, text="–", fg="#9aa3b5", bg="#1c2130").pack(side="left", padx=4)
        tk.Spinbox(delays, from_=1, to=60, textvariable=self.delay_max, width=6).pack(side="left")

        tk.Label(grid, text="Клики", fg="#9aa3b5", bg="#1c2130").grid(row=3, column=0, sticky="w")
        ttk.Combobox(
            grid,
            textvariable=self.input_method,
            values=["mouse", "adb"],
            state="readonly",
            width=16,
        ).grid(row=3, column=1, sticky="ew", pady=3)

        tk.Checkbutton(
            grid,
            text="DRY-RUN (не отправлять)",
            variable=self.dry_run,
            fg="#f3f4f8",
            bg="#1c2130",
            selectcolor="#12141c",
            activebackground="#1c2130",
            activeforeground="#f3f4f8",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=2)
        tk.Checkbutton(
            grid,
            text="Использовать перья",
            variable=self.use_feathers,
            fg="#f3f4f8",
            bg="#1c2130",
            selectcolor="#12141c",
            activebackground="#1c2130",
            activeforeground="#f3f4f8",
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=2)
        tk.Checkbutton(
            grid,
            text="Если перьев нет — золото",
            variable=self.gold_fallback,
            fg="#f3f4f8",
            bg="#1c2130",
            selectcolor="#12141c",
            activebackground="#1c2130",
            activeforeground="#f3f4f8",
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=2)
        tk.Checkbutton(
            grid,
            text="Панель поверх окон",
            variable=self.always_on_top,
            command=self._apply_topmost,
            fg="#f3f4f8",
            bg="#1c2130",
            selectcolor="#12141c",
            activebackground="#1c2130",
            activeforeground="#f3f4f8",
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=2)

        grid.columnconfigure(1, weight=1)

        tk.Button(
            settings,
            text="Сохранить настройки",
            command=self._save,
            relief="flat",
            bg="#3d4a2f",
            fg="#f3f4f8",
            font=("Segoe UI", 10, "bold"),
        ).pack(fill="x", padx=10, pady=(0, 10))

        self.root.bind("<KeyPress>", self._on_key)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _load_vars(self) -> None:
        data = public_settings(self.config)
        self.kind.set(data["current_target_kind"])
        self.dry_run.set(data["dry_run"])
        self.use_feathers.set(data["use_feathers"])
        self.gold_fallback.set(data["gold_fallback_when_no_feathers"])
        self.always_on_top.set(data["always_on_top"])
        self.max_concurrent.set(data["max_concurrent_attacks"])
        self.delay_min.set(data["attack_delay_min"])
        self.delay_max.set(data["attack_delay_max"])
        self.input_method.set(data["input"])

    def _toggle(self) -> None:
        CONTROL.toggle()
        self._refresh()

    def _start_bind(self) -> None:
        self._binding = True
        self._hotkey.set("Нажми букву или F1–F12…")

    def _on_key(self, event: tk.Event) -> None:  # type: ignore[name-defined]
        if not self._binding:
            return
        keysym = str(event.keysym)
        if len(keysym) == 1 and keysym.isalnum():
            hotkey = keysym.upper()
        elif keysym.upper().startswith("F") and keysym[1:].isdigit():
            hotkey = keysym.upper()
        else:
            return
        self._binding = False
        CONTROL.set_hotkey(hotkey)
        control = dict(self.config.get("control") or {})
        control["hotkey"] = CONTROL.hotkey
        self.config["control"] = control
        save_config(self.config)
        self._hotkey.set(f"Клавиша: {CONTROL.hotkey}")

    def _apply_topmost(self) -> None:
        CONTROL.always_on_top = bool(self.always_on_top.get())
        self.root.attributes("-topmost", CONTROL.always_on_top)

    def _save(self) -> None:
        apply_public_settings(
            self.config,
            {
                "current_target_kind": self.kind.get(),
                "dry_run": self.dry_run.get(),
                "max_concurrent_attacks": self.max_concurrent.get(),
                "attack_delay_min": self.delay_min.get(),
                "attack_delay_max": self.delay_max.get(),
                "use_feathers": self.use_feathers.get(),
                "gold_fallback_when_no_feathers": self.gold_fallback.get(),
                "always_on_top": self.always_on_top.get(),
                "input": self.input_method.get(),
                "hotkey": CONTROL.hotkey,
            },
        )
        save_config(self.config)
        self._apply_topmost()
        self._status.set("Настройки сохранены")

    def _schedule_refresh(self) -> None:
        try:
            self.root.after(0, self._refresh)
        except tk.TclError:
            pass

    def _refresh(self) -> None:
        on = CONTROL.is_enabled()
        self.toggle_btn.configure(
            text="ВКЛ — ищет цели" if on else "ВЫКЛ — мышь свободна",
            bg="#2f6b45" if on else "#8a3030",
            fg="#ffffff",
            activebackground="#3d8556" if on else "#a33b3b",
        )
        self._enabled_label.set(
            f"Нажми {CONTROL.hotkey} или эту кнопку, чтобы {'выключить' if on else 'включить'}"
        )
        self._hotkey.set(
            "Нажми букву или F1–F12…" if self._binding else f"Клавиша: {CONTROL.hotkey}"
        )

    def _tick(self) -> None:
        live = self.store.live
        if not CONTROL.is_enabled():
            mode = "пауза"
        elif live.mode in {"search", "pause", "paused"}:
            mode = "поиск"
        else:
            mode = live.mode or "idle"
        parts = [
            f"{mode}",
            live.last_coords or "—",
            f"в пути {len(self.store.in_flight())}",
        ]
        if live.last_error:
            parts.append(live.last_error)
        self._status.set(" · ".join(parts))
        try:
            self.root.after(500, self._tick)
        except tk.TclError:
            pass

    def _on_close(self) -> None:
        CONTROL.shutdown()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def run_panel(config: dict[str, Any], store: StateStore) -> None:
    ControlPanel(config, store).run()
