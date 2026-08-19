from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from e4kbot.client import BlueStacksEngine, parse_march_seconds
from e4kbot.state import StateStore
from e4kbot.vision import (
    ROBBER_TEMPLATE,
    choose_nearest_main_castle,
    choose_movement,
    choose_shortest_candidate,
    find_robber_candidates,
    find_picker_confirm_button,
    find_formation_unit_slots,
    formation_wave_diagnostics,
    generic_modal_diagnostics,
    flank_fill_allowed,
    is_burning_candidate,
    movement_confirm_diagnostics,
    no_commanders_diagnostics,
    ocr_text,
    parse_coordinate_pair,
    parse_ratio,
    popup_action,
)


class ParsingTests(unittest.TestCase):
    def test_parses_march_times(self) -> None:
        self.assertEqual(parse_march_seconds("00:01:26"), 86)
        self.assertEqual(parse_march_seconds("12:34"), 754)
        self.assertIsNone(parse_march_seconds("unknown"))
        self.assertEqual(parse_march_seconds("00:03:08"), 188)

    def test_parses_capacity(self) -> None:
        self.assertEqual(parse_ratio("7 / 7"), (7, 7))
        self.assertEqual(parse_ratio("Юниты 0/130"), (0, 130))
        self.assertEqual(parse_ratio("1 05/7"), (5, 7))
        self.assertEqual(parse_ratio("15/7"), (5, 7))

    def test_chooses_shortest_measured_candidate(self) -> None:
        measured = [((0.2, 0.3), 90), ((0.7, 0.5), 42), ((0.5, 0.4), 65)]
        self.assertEqual(choose_shortest_candidate(measured), (0.7, 0.5))

    def test_prefers_feather_and_falls_back_only_at_zero(self) -> None:
        self.assertEqual(choose_movement(3), "feather")
        self.assertEqual(choose_movement(0), "gold")
        self.assertEqual(choose_movement(None), "unknown")

    def test_chooses_target_nearest_main_castle_coordinates(self) -> None:
        candidates = [(0.50, 0.54, 0.9), (0.80, 0.80, 0.95)]
        chosen = choose_nearest_main_castle(
            candidates,
            main_castle=(607, 738),
            viewport=(607, 738),
        )
        self.assertEqual(chosen, (0.50, 0.54))

    def test_parses_main_castle_coordinates(self) -> None:
        self.assertEqual(parse_coordinate_pair("Замок X:607/Y:738"), (607, 738))

    def test_requires_seventy_percent_fill(self) -> None:
        self.assertTrue(flank_fill_allowed(7, 10))
        self.assertTrue(flank_fill_allowed(5, 7))
        self.assertFalse(flank_fill_allowed(4, 7))


class VisionTests(unittest.TestCase):
    def test_finds_robber_template_on_map(self) -> None:
        template = Image.open(ROBBER_TEMPLATE).convert("RGB")
        image = Image.new("RGB", (900, 1600), (104, 151, 57))
        image.paste(template, (200, 300))
        image.paste(template, (600, 900))
        found = find_robber_candidates(image, threshold=0.75)
        self.assertEqual(len(found), 2)

    def test_detects_safe_modal_close(self) -> None:
        image = Image.new("RGB", (900, 1600), (90, 130, 55))
        draw = ImageDraw.Draw(image)
        draw.rectangle((720, 100, 800, 180), fill=(205, 35, 20))
        action = popup_action(image)
        self.assertIsNotNone(action)
        assert action is not None
        self.assertGreater(action[0], 0.7)

    def test_reads_authoritative_travel_duration_fixture(self) -> None:
        fixture = ROBBER_TEMPLATE.parent / "travel_duration_fixture.png"
        self.assertEqual(ocr_text(Image.open(fixture), psm=6), "00:03:08")

    def test_filters_burning_castle_signature(self) -> None:
        image = Image.new("RGB", (900, 1600), (104, 151, 57))
        draw = ImageDraw.Draw(image)
        draw.ellipse((420, 760, 480, 850), fill=(238, 70, 15))
        draw.ellipse((425, 700, 475, 770), fill=(190, 190, 180))
        self.assertTrue(is_burning_candidate(image, (0.50, 0.50)))

    def test_picker_confirm_is_green_and_right_side(self) -> None:
        image = Image.new("RGB", (900, 1600), (55, 35, 25))
        confirm = Image.open(ROBBER_TEMPLATE.parent / "picker_confirm.png").convert("RGB")
        image.paste(confirm, (600, 1200))
        draw = ImageDraw.Draw(image)
        draw.rectangle((170, 1210, 300, 1350), fill=(205, 30, 20))
        point = find_picker_confirm_button(image, threshold=0.70)
        self.assertIsNotNone(point)
        assert point is not None
        self.assertGreater(point[0], 0.55)
        self.assertGreater(point[1], 0.70)

    def test_picker_diagnostic_mode_never_clicks(self) -> None:
        image = Image.new("RGB", (900, 1600), (55, 35, 25))
        confirm = Image.open(ROBBER_TEMPLATE.parent / "picker_confirm.png").convert("RGB")
        image.paste(confirm, (600, 1200))
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.adb = Mock()
        engine._image = Mock(return_value=image)
        valid, reason, _ = engine.diagnose_unit_picker_confirm(click=False)
        self.assertTrue(valid)
        self.assertEqual(reason, "diagnostic_only")
        engine.adb.tap.assert_not_called()

    def test_picker_confirm_rejects_red_cancel(self) -> None:
        image = Image.new("RGB", (900, 1600), (55, 35, 25))
        draw = ImageDraw.Draw(image)
        draw.ellipse((170, 1200, 300, 1340), fill=(210, 25, 20))
        self.assertIsNone(find_picker_confirm_button(image, threshold=0.60))

    def test_movement_confirm_is_green_right_and_distinct_from_cancel(self) -> None:
        image = Image.new("RGB", (900, 1600), (55, 35, 25))
        draw = ImageDraw.Draw(image)
        draw.rectangle((100, 1280, 310, 1370), fill=(205, 30, 20))
        draw.rectangle((590, 1280, 810, 1370), fill=(65, 165, 10))
        draw.line((665, 1325, 700, 1350, 750, 1300), fill="white", width=16)
        diagnostic = movement_confirm_diagnostics(image)
        self.assertTrue(diagnostic["valid"])
        self.assertGreater(diagnostic["point"][0], 0.5)
        self.assertLessEqual(diagnostic["red_ratio"], 0.05)

    def test_movement_diagnostic_mode_never_clicks(self) -> None:
        image = Image.new("RGB", (900, 1600), (55, 35, 25))
        draw = ImageDraw.Draw(image)
        draw.rectangle((590, 1280, 810, 1370), fill=(65, 165, 10))
        draw.line((665, 1325, 700, 1350, 750, 1300), fill="white", width=16)
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.adb = Mock()
        engine._image = Mock(return_value=image)
        valid, reason, _ = engine.diagnose_movement_confirm(click=False)
        self.assertTrue(valid)
        self.assertEqual(reason, "diagnostic_only")
        engine.adb.tap.assert_not_called()

    def test_no_commanders_close_is_top_right_red_only(self) -> None:
        image = Image.new("RGB", (900, 1600), (45, 30, 20))
        draw = ImageDraw.Draw(image)
        draw.rectangle((180, 300, 720, 1050), fill=(225, 195, 145))
        draw.rectangle((620, 320, 700, 400), fill=(210, 25, 20))
        draw.rectangle((210, 900, 330, 1000), fill=(210, 25, 20))
        diagnostic = no_commanders_diagnostics(
            image, "Нет свободных военачальников"
        )
        self.assertTrue(diagnostic["valid"])
        self.assertGreater(diagnostic["point"][0], 0.5)
        self.assertLess(diagnostic["point"][1], 0.5)

    def test_generic_modal_prefers_red_close_over_green(self) -> None:
        image = Image.new("RGB", (900, 1600), (40, 25, 20))
        draw = ImageDraw.Draw(image)
        draw.rectangle((170, 250, 730, 1100), fill=(225, 195, 145))
        draw.rectangle((630, 275, 705, 350), fill=(210, 25, 20))
        draw.rectangle((520, 920, 700, 1000), fill=(55, 165, 15))
        diagnostic = generic_modal_diagnostics(image)
        self.assertTrue(diagnostic["valid"])
        self.assertEqual(diagnostic["action"], "close")

    def test_generic_modal_excludes_attack_formation(self) -> None:
        image = Image.new("RGB", (900, 1600), (105, 65, 35))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 980, 900, 1210), fill=(230, 175, 25))
        diagnostic = generic_modal_diagnostics(image)
        self.assertTrue(diagnostic["excluded"])
        self.assertFalse(diagnostic["valid"])

    def test_transition_polling_is_bounded(self) -> None:
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.config = {"vision": {"poll_interval_seconds": 0.01}}
        engine._image = Mock(return_value=Image.new("RGB", (10, 10)))
        self.assertIsNone(engine._wait_for(lambda _: False, timeout=0.04))
        self.assertLessEqual(engine._image.call_count, 6)

    def test_unit_slot_target_never_overlaps_wave_header(self) -> None:
        image = Image.new("RGB", (900, 1600), (105, 65, 35))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 1000, 899, 1080), fill=(230, 175, 25))
        draw.rectangle((0, 1081, 899, 1320), fill=(220, 205, 180))
        draw.rectangle((0, 1321, 899, 1400), fill=(230, 175, 25))
        draw.line((35, 1170, 95, 1170), fill=(45, 30, 20), width=18)
        draw.line((65, 1140, 65, 1200), fill=(45, 30, 20), width=18)
        wave = formation_wave_diagnostics(image)
        slots = find_formation_unit_slots(image)
        self.assertTrue(wave["expanded"])
        self.assertTrue(slots)
        header = wave["first_header"]
        self.assertFalse(
            header[0] <= slots[0][0] <= header[2]
            and header[1] <= slots[0][1] <= header[3]
        )


class StateTests(unittest.TestCase):
    def test_failed_movement_transition_is_never_registered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            engine = BlueStacksEngine.__new__(BlueStacksEngine)
            engine.config = {"dry_run": False}
            engine.store = store
            engine.telegram = Mock()
            engine.adb = Mock()
            engine._next_commander = 1
            engine.read_region = Mock(return_value="")
            engine.diagnose_movement_confirm = Mock(
                return_value=(False, "movement_confirm_transition_failed", None)
            )
            with patch("e4kbot.client.capture_game_image", return_value=None):
                result = engine._finish_attack(
                    "baron",
                    {"kingdom": 0, "x": 607, "y": 743},
                    one_way=188,
                    movement="feather",
                )
            self.assertEqual(result, "movement_confirm_transition_failed")
            self.assertEqual(store.live.marches, [])
            self.assertEqual(store.live.cooldowns, {})

    def test_dry_run_does_not_create_march_or_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            engine = BlueStacksEngine.__new__(BlueStacksEngine)
            engine.config = {"dry_run": True}
            engine.store = store
            engine.telegram = Mock()
            engine.adb = Mock()
            engine._next_commander = 1
            engine.read_region = Mock(return_value="")
            engine.tap_rel = Mock()
            engine.diagnose_movement_confirm = Mock(
                return_value=(True, "diagnostic_only", None)
            )
            with patch("e4kbot.client.capture_game_image", return_value=None):
                result = engine._finish_attack(
                    "baron",
                    {"kingdom": 0, "x": 607, "y": 743},
                    one_way=188,
                    movement="gold",
                )
            self.assertEqual(result, "baron")
            self.assertEqual(store.live.marches, [])
            self.assertEqual(store.live.cooldowns, {})

    def test_tracks_arrival_and_return_then_reconciles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            march = store.register_march(
                1,
                1,
                "baron",
                0,
                100,
                200,
                86,
                movement="gold",
            )
            self.assertAlmostEqual(march.arrive_at - march.sent_at, 86, delta=0.1)
            self.assertAlmostEqual(march.return_at - march.sent_at, 172, delta=0.1)
            self.assertAlmostEqual(
                march.cooldown_until - march.arrive_at,
                3 * 60 * 60,
                delta=0.1,
            )
            self.assertFalse(store.target_available("baron", 0, 100, 200))
            updated = store.update_return_timer(1, 51)
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertAlmostEqual(updated.return_at - time.time(), 51, delta=1)
            self.assertEqual(updated.timer_source, "screen")
            restarted = StateStore(Path(directory) / "state.json")
            self.assertFalse(restarted.target_available("baron", 0, 100, 200))
            self.assertGreater(
                restarted.target_cooldown_until("baron", 0, 100, 200),
                time.time(),
            )

    def test_excludes_cooling_target_before_nearest_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            store.live.cooldowns[store.target_key("baron", 0, 607, 738)] = time.time() + 60
            candidates = [
                ((0.50, 0.54, 0.95), (607, 738)),
                ((0.60, 0.54, 0.90), (609, 738)),
            ]
            eligible = [
                candidate
                for candidate, coords in candidates
                if store.target_available("baron", 0, coords[0], coords[1])
            ]
            chosen = choose_nearest_main_castle(
                eligible,
                main_castle=(607, 738),
                viewport=(607, 738),
            )
            self.assertEqual(chosen, (0.60, 0.54))


if __name__ == "__main__":
    unittest.main()
