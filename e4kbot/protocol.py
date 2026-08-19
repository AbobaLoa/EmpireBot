from __future__ import annotations

import time
from typing import Any

from loguru import logger

from e4kbot.bluestacks import AdbClient, capture_game_image, save_shot
from e4kbot.config import enabled_account, load_config, server_endpoint
from e4kbot.paths import add_legacy_bot_path
from e4kbot.safety import (
    commander_number_ok,
    concurrent_ok,
    triangular_delay,
)
from e4kbot.state import StateStore
from e4kbot.telegram_bot import TelegramReporter


def _shot(config: dict[str, Any], adb: AdbClient | None, prefix: str) -> Path | None:
    image = capture_game_image(config, adb)
    if image is None:
        return None
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return save_shot(image, f"{prefix}_{stamp}.png")


def commander_slot_number(lord: dict[str, Any], lords_data: dict[str, Any]) -> int | None:
    vis = lord.get("VIS")
    lord_id = int(lord.get("ID", -1))
    for index, slot in enumerate(lords_data.get("C") or [], start=1):
        if not isinstance(slot, dict):
            continue
        if vis is not None and int(slot.get("VIS", -10**9)) == int(vis):
            return index
        if int(slot.get("ID", -10**9)) == lord_id:
            return index
    return None


def movement_one_way(
    wrappers: list[dict[str, Any]],
    player_id: int | None,
    lord_id: int,
    tx: int,
    ty: int,
) -> int | None:
    from movements_intel import parse_all_movements_for_ui

    rows = parse_all_movements_for_ui(wrappers, player_id=player_id)
    for row in rows:
        dest = row.get("to") or {}
        if int(dest.get("x", -1)) != int(tx) or int(dest.get("y", -1)) != int(ty):
            continue
        eta = row.get("eta_sec")
        if eta:
            return int(eta)
    for row in rows:
        eta = row.get("eta_sec")
        if eta and row.get("kind") in {"attack", "raid"}:
            return int(eta)
    return None


class ProtocolEngine:
    def __init__(
        self,
        config: dict[str, Any],
        store: StateStore,
        telegram: TelegramReporter,
        adb: AdbClient | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.telegram = telegram
        self.adb = adb
        self.socket = None
        self.player_id: int | None = None
        add_legacy_bot_path(config.get("legacy_bot_path"))

    def connect(self) -> None:
        from threading import Thread

        from pygge.gge_socket import GgeSocket

        if not self.config.get("live_api_allowed") or not self.config.get("accept_risk"):
            raise RuntimeError(
                "Протокол выключен. В config.json поставь live_api_allowed=true и accept_risk=true"
            )
        account = enabled_account(self.config)
        url, zone = server_endpoint(self.config)
        self.socket = GgeSocket(url, zone)
        Thread(target=self.socket.run_forever, daemon=True).start()
        Thread(target=self.socket.keep_alive, daemon=True).start()
        self.socket.init_socket()
        self.socket.login_e4k(account["username"], account["password"])
        self.store.live.account = str(account["username"])
        logger.info(f"Вход: {account['username']}")

    def close(self) -> None:
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
            self.socket = None

    def run_cycle(self) -> str:
        from fleet.event_intel import EventKind, detect_primary_event

        modes = self.config.get("modes") or {}
        event = None
        try:
            event = detect_primary_event(self.socket, lang="ru")
        except Exception as exc:
            logger.debug(f"event detect: {exc}")

        if modes.get("prefer_events", True) and event is not None:
            if event.kind == EventKind.NOMAD and modes.get("nomads", True):
                return self._attack_npcs("nomad", (27, 35), 0)
            if event.kind == EventKind.SAMURAI and modes.get("shogun", True):
                return self._attack_npcs("shogun", (29,), 0)

        if modes.get("nomads") and event is not None and event.kind == EventKind.NOMAD:
            return self._attack_npcs("nomad", (27, 35), 0)
        if modes.get("shogun") and event is not None and event.kind == EventKind.SAMURAI:
            return self._attack_npcs("shogun", (29,), 0)
        if modes.get("barons", True):
            baron = self.config.get("baron_attacks") or {}
            return self._attack_npcs(
                "baron",
                (int(baron.get("npc_type", 2)),),
                int(baron.get("kingdom", 0)),
            )
        return "idle"

    def _attack_npcs(self, kind: str, npc_types: tuple[int, ...], kingdom: int) -> str:
        from baron_attacks import (
            _attack_sources_for_lords,
            _available_attack_lords,
            _available_units,
            _cra_status_text,
            _format_wave_report,
            _main_castle_from_gcl,
            _resolve_cra_lord_id,
            _send_attack_with_status,
            _unit_stats,
            build_army_plans_for_lords,
            payload_data,
        )
        from movements_intel import (
            extract_bet_level,
            extract_commander_slot_cap,
            fetch_movement_wrappers,
            resolve_feathers_ptt,
        )

        assert self.socket is not None
        account = self.store.live.account
        dry_run = bool(self.config.get("dry_run", True))
        cap_cmd = int(self.config.get("max_commander_number") or 30)
        gcl = self.socket.get_castles(quiet=True)
        source = _main_castle_from_gcl(gcl, kingdom)
        if not source:
            raise RuntimeError("Главный замок не найден")
        if self.player_id is None:
            pid = payload_data(gcl).get("PID")
            self.player_id = int(pid) if pid is not None else None

        self.socket.go_to_castle(kingdom, int(source["castle_id"]), quiet=True)
        lords_data = payload_data(self.socket.get_lords(quiet=True))
        slot_cap = min(extract_commander_slot_cap(lords_data), cap_cmd)
        bet_level = extract_bet_level(lords_data)
        movement_wrappers = fetch_movement_wrappers(self.socket)
        lords, meta = _available_attack_lords(
            lords_data,
            {"M": movement_wrappers},
            self.player_id,
            bet_level,
            slot_cap,
        )
        attack_sources = _attack_sources_for_lords(
            lords, movement_wrappers, self.player_id, source
        )
        in_flight = len(self.store.in_flight())
        ok_conc, conc_msg = concurrent_ok(in_flight, self.config)
        if not ok_conc:
            logger.info(conc_msg)
            return "wait_return"

        sent = 0
        for entry in attack_sources:
            self.store.prune()
            in_flight = len(self.store.in_flight())
            ok_conc, conc_msg = concurrent_ok(in_flight, self.config)
            if not ok_conc:
                logger.info(conc_msg)
                break

            lord = entry["lord"]
            row_source = entry["source"]
            commander_no = commander_slot_number(lord, lords_data)
            ok_cmd, cmd_msg = commander_number_ok(commander_no, self.config)
            if not ok_cmd:
                self.store.live.stopped_reason = cmd_msg
                self.telegram.report_stop(cmd_msg)
                return "stop"
            if commander_no is None:
                logger.warning("Не удалось определить номер военачальника — пропуск")
                continue

            go_to = self.socket.go_to_castle(
                kingdom, int(row_source["castle_id"]), quiet=True
            )
            go_to_data = payload_data(go_to)
            units = _available_units(go_to_data.get("gui") or {})
            unit_stats = _unit_stats(go_to_data)
            try:
                army, plan = build_army_plans_for_lords(
                    self.config.get("baron_attacks") or {}, units, unit_stats, 1
                )[0]
            except RuntimeError:
                continue

            target = self._closest_npc(
                int(row_source["kingdom"]),
                npc_types,
                int(row_source["x"]),
                int(row_source["y"]),
            )
            if not target:
                continue
            tx, ty = int(target["x"]), int(target["y"])
            kid = int(target.get("kingdom", row_source["kingdom"]))
            lord_cra_id = _resolve_cra_lord_id(lord, lords_data)
            adi = payload_data(
                self.socket.get_target_infos(
                    kid,
                    int(row_source["x"]),
                    int(row_source["y"]),
                    tx,
                    ty,
                    quiet=True,
                )
            )
            scid = int(adi.get("SCID") or int(row_source["castle_id"]))
            feathers = resolve_feathers_ptt(
                self.config, self.config.get("baron_attacks") or {}
            )
            shot = _shot(self.config, self.adb, f"{kind}_{tx}_{ty}")
            extra = f"⚔️ {_format_wave_report(plan)}"
            if dry_run:
                one_way = int(adi.get("TT") or adi.get("AT") or 180)
                march = self.store.register_march(
                    commander_no,
                    lord_cra_id,
                    kind,
                    kid,
                    tx,
                    ty,
                    one_way,
                    str(shot) if shot else "",
                )
                self.telegram.report_attack(
                    account,
                    kind,
                    kid,
                    tx,
                    ty,
                    commander_no,
                    one_way,
                    int(march.return_at - march.sent_at),
                    shot,
                    extra=extra,
                    dry_run=True,
                )
            else:
                status, detail = _send_attack_with_status(
                    self.socket,
                    kid,
                    int(row_source["x"]),
                    int(row_source["y"]),
                    tx,
                    ty,
                    army,
                    lord_cra_id,
                    int((self.config.get("baron_attacks") or {}).get("horses_type", -1)),
                    feathers,
                    scid=scid,
                )
                if status != 0:
                    logger.warning(_cra_status_text(status, detail))
                    continue
                time.sleep(1.2)
                wrappers = fetch_movement_wrappers(self.socket)
                one_way = movement_one_way(
                    wrappers, self.player_id, lord_cra_id, tx, ty
                ) or int(adi.get("TT") or 180)
                march = self.store.register_march(
                    commander_no,
                    lord_cra_id,
                    kind,
                    kid,
                    tx,
                    ty,
                    one_way,
                    str(shot) if shot else "",
                )
                self.telegram.report_attack(
                    account,
                    kind,
                    kid,
                    tx,
                    ty,
                    commander_no,
                    one_way,
                    int(march.return_at - march.sent_at),
                    shot,
                    extra=extra,
                    dry_run=False,
                )
            sent += 1
            delay = triangular_delay(self.config.get("attack_delay_seconds") or [4, 10])
            self.store.live.next_attack_at = time.time() + delay
            self.store.save()
            time.sleep(delay)
        return f"{kind}:{sent}"

    def _closest_npc(
        self,
        kingdom: int,
        npc_types: tuple[int, ...],
        source_x: int,
        source_y: int,
    ) -> dict[str, int] | None:
        from baron_attacks import payload_data

        assert self.socket is not None
        baron_cfg = self.config.get("baron_attacks") or {}
        for npc_type in npc_types:
            closest = self.socket.get_closest_npc(
                kingdom,
                npc_type,
                min_level=int(baron_cfg.get("min_level", 1)),
                max_level=int(baron_cfg.get("max_level", -1)),
                quiet=True,
            )
            data = payload_data(closest)
            if data.get("X") is not None and data.get("Y") is not None:
                return {
                    "x": int(data["X"]),
                    "y": int(data["Y"]),
                    "npc_type": npc_type,
                    "kingdom": kingdom,
                }
        return None
