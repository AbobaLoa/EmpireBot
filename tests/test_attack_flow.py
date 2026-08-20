from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from e4kbot.client import BlueStacksEngine, HuntTarget, parse_march_seconds
from e4kbot.state import StateStore
from e4kbot.vision import (
    OFFER_RAIL_X,
    ROBBER_TEMPLATE,
    choose_nearest_main_castle,
    choose_movement,
    choose_shortest_candidate,
    find_robber_candidates,
    find_picker_confirm_button,
    find_empty_wave_warning_confirm,
    flank_fill_allowed,
    is_burning_candidate,
    is_green_hire_point,
    is_offer_rail_point,
    is_special_offers_screen,
    movement_confirm_diagnostics,
    no_commanders_diagnostics,
    ocr_text,
    parse_coordinate_pair,
    parse_ratio,
    popup_action,
    special_offers_close_point,
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
        image = Image.new("RGB", (900, 1600), (40, 35, 50))
        draw = ImageDraw.Draw(image)
        draw.rectangle((640, 90, 720, 170), fill=(205, 35, 20))
        action = popup_action(image)
        self.assertIsNotNone(action)
        assert action is not None
        self.assertGreater(action[0], 0.7)
        self.assertLess(action[0], 0.82)

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

    def test_picker_confirm_rejects_empty_wave_warning_check(self) -> None:
        image = Image.new("RGB", (900, 1600), (55, 35, 25))
        confirm = Image.open(ROBBER_TEMPLATE.parent / "picker_confirm.png").convert("RGB")
        image.paste(confirm, (600, 880))
        self.assertIsNone(find_picker_confirm_button(image, threshold=0.70))
        warning = find_empty_wave_warning_confirm(image, threshold=0.70)
        self.assertIsNotNone(warning)
        assert warning is not None
        self.assertGreater(warning[0], 0.55)
        self.assertGreaterEqual(warning[1], 0.48)
        self.assertLessEqual(warning[1], 0.68)

    def test_no_commanders_ocr_reads_garbled_inscription(self) -> None:
        from e4kbot.vision import _no_commanders_conclusion, _no_commanders_text_hit

        garbled = (
            "Bunsanne! Cenuyac y Te6R HeT CBOGOQHbIX BOCHANANbHMKOB. "
            "XOYeLWb HaHATb PeZepBHOO BOEHAYANbHUKA ANA ITOLO HanadeHna? Uena: 125"
        )
        self.assertTrue(_no_commanders_text_hit(garbled))
        conclusion = _no_commanders_conclusion(garbled)
        self.assertIn("нет свободных военачальников", conclusion)
        self.assertIn("нанять резерв", conclusion)
        self.assertIn("125", conclusion)
        self.assertIn("красным крестиком", conclusion)
        self.assertTrue(
            _no_commanders_text_hit(
                "Сейчас у тебя нет свободных наместников. Хочешь нанять резервного?"
            )
        )
        namestnik = _no_commanders_conclusion(
            "Сейчас у тебя нет свободных наместников. Хочешь нанять резервного?"
        )
        self.assertIn("наместников", namestnik)
        self.assertFalse(_no_commanders_text_hit("Начать нападение?"))
        live_garbled = (
            "Buumanne! Cenuyac y Te6A HET CBOOOQHbIX BOCHAYAIbHUKOB. "
            "XOYELUb HaHATb PeSe€pBHOIO BOeEHAYaNIbHUKa OA 3TOFO HanageHuna? LleHa: 125"
        )
        self.assertTrue(_no_commanders_text_hit(live_garbled))
        live_conclusion = _no_commanders_conclusion(live_garbled)
        self.assertIn("нет свободных военачальников", live_conclusion)
        self.assertIn("125", live_conclusion)
        self.assertIn("красным крестиком", live_conclusion)

    def test_no_commanders_asset_closes_red_not_green_hire(self) -> None:
        image = Image.open(ROBBER_TEMPLATE.parent / "no_commanders.png").convert("RGB")
        diagnostic = no_commanders_diagnostics(image)
        self.assertTrue(diagnostic["valid"], diagnostic)
        self.assertTrue(diagnostic["text"])
        self.assertIn("рубин", diagnostic["conclusion"])
        self.assertIn("красным крестиком", diagnostic["conclusion"])
        assert diagnostic["point"] is not None
        self.assertFalse(is_green_hire_point(*diagnostic["point"]))
        self.assertLess(diagnostic["point"][1], 0.55)

    def test_prepare_wave_fails_when_formation_still_empty(self) -> None:
        dummy = Image.new("RGB", (900, 1600), (80, 50, 30))
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.config = {"vision": {"picker_max_actions": 1, "minimum_flank_fill": 0.70}}
        engine.telegram = Mock()
        engine.store = Mock()
        engine.store.live = Mock()
        engine._image = Mock(return_value=dummy)
        engine.layout = {"buttons": {"unit_slot": [0.07, 0.70]}}
        engine._tap_norm_exact = Mock()
        engine._dismiss_empty_wave_warning = Mock(return_value=False)
        engine.tap_rel = Mock()
        engine._wait_for = Mock(return_value=dummy)
        engine._picker_overlay_open = Mock(return_value=False)
        engine._dump_picker_max = Mock(
            side_effect=lambda: setattr(engine, "_last_picker_fill", (10, 10)) or True
        )
        engine._read_ratio_from_image = Mock(
            side_effect=lambda _image, key: {
                "picker_units": (10, 10),
                "formation_units": (0, 19),
                "formation_tools": None,
            }.get(key)
        )
        engine.diagnose_unit_picker_confirm = Mock(return_value=(True, "confirmed", dummy))
        ok, reason = engine._prepare_single_center_wave()
        self.assertFalse(ok)
        self.assertEqual(reason, "formation_units_empty")

    def test_assume_picker_capacity_does_not_invent_full_fill(self) -> None:
        dummy = Image.new("RGB", (900, 1600), (80, 50, 30))
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine._last_picker_fill = None
        engine._read_ratio_from_image = Mock(return_value=(0, 19))
        self.assertEqual(engine._assume_picker_capacity(dummy, (0, 19)), (0, 19))
        engine._read_ratio_from_image = Mock(return_value=(19, 19))
        self.assertEqual(engine._assume_picker_capacity(dummy, (0, 19)), (19, 19))

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

    def test_filled_picker_confirm_does_not_abort_after_overlay_closes(self) -> None:
        picker = Image.new("RGB", (900, 1600), (55, 35, 25))
        formation = Image.new("RGB", (900, 1600), (80, 50, 30))
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.adb = Mock()
        engine._image = Mock(return_value=picker)
        engine._tap_norm_exact = Mock()
        engine._wait_for = Mock(return_value=formation)
        engine._read_ratio_from_image = Mock(
            side_effect=lambda image, key: (0, 10) if image is formation else (10, 10)
        )
        diagnostic = {
            "point": (0.73, 0.79),
            "popup_bounds": (0.08, 0.18, 0.92, 0.90),
            "template_score": 0.99,
            "green_ratio": 0.2,
            "check_ratio": 0.05,
            "valid": True,
        }
        with patch("e4kbot.client.picker_confirm_diagnostics", return_value=diagnostic):
            with patch("e4kbot.client.save_shot"):
                with patch("e4kbot.client.is_map_screen", return_value=False):
                    valid, reason, after = engine.diagnose_unit_picker_confirm(
                        click=True,
                        observed_fill=(10, 10),
                    )
        self.assertTrue(valid)
        self.assertEqual(reason, "confirmed")
        self.assertIs(after, formation)
        self.assertNotEqual(reason, "unit_picker_fill_not_retained")
        engine._tap_norm_exact.assert_called_once_with(0.73, 0.79)

    def test_observed_ten_of_ten_does_not_abort_on_zero_ocr_after_check(self) -> None:
        picker = Image.new("RGB", (900, 1600), (55, 35, 25))
        leftover = Image.new("RGB", (900, 1600), (80, 50, 30))
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.adb = Mock()
        engine._image = Mock(side_effect=[picker, leftover])
        engine._tap_norm_exact = Mock()
        engine._wait_for = Mock(return_value=None)
        engine._read_ratio_from_image = Mock(return_value=(0, 10))
        diagnostic = {
            "point": (0.73, 0.79),
            "popup_bounds": (0.08, 0.18, 0.92, 0.90),
            "template_score": 0.99,
            "green_ratio": 0.2,
            "check_ratio": 0.05,
            "valid": True,
        }
        with patch("e4kbot.client.picker_confirm_diagnostics", return_value=diagnostic):
            with patch("e4kbot.client.save_shot"):
                with patch("e4kbot.client.is_map_screen", return_value=False):
                    with patch("e4kbot.client.find_picker_confirm_button", return_value=(0.73, 0.79)):
                        with patch("e4kbot.client.find_picker_cards", return_value=[{"x": 1}]):
                            valid, reason, after = engine.diagnose_unit_picker_confirm(
                                click=True,
                                observed_fill=(10, 10),
                            )
        self.assertTrue(valid)
        self.assertEqual(reason, "confirmed")
        self.assertIs(after, leftover)
        engine._tap_norm_exact.assert_called_once_with(0.73, 0.79)

    def test_empty_picker_confirm_still_aborts(self) -> None:
        picker = Image.new("RGB", (900, 1600), (55, 35, 25))
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.adb = Mock()
        engine._image = Mock(return_value=picker)
        engine._tap_norm_exact = Mock()
        engine._wait_for = Mock()
        engine._read_ratio_from_image = Mock(return_value=(0, 10))
        diagnostic = {
            "point": (0.73, 0.79),
            "popup_bounds": (0.08, 0.18, 0.92, 0.90),
            "template_score": 0.99,
            "green_ratio": 0.2,
            "check_ratio": 0.05,
            "valid": True,
        }
        with patch("e4kbot.client.picker_confirm_diagnostics", return_value=diagnostic):
            with patch("e4kbot.client.save_shot"):
                valid, reason, after = engine.diagnose_unit_picker_confirm(click=True)
        self.assertFalse(valid)
        self.assertEqual(reason, "unit_picker_fill_not_retained")
        self.assertIsNone(after)
        engine._tap_norm_exact.assert_not_called()
        engine._wait_for.assert_not_called()

    def test_prepare_wave_continues_when_overlay_hides_ten_of_ten(self) -> None:
        dummy = Image.new("RGB", (900, 1600), (80, 50, 30))
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.config = {"vision": {"picker_max_actions": 1, "minimum_flank_fill": 0.70}}
        engine.telegram = Mock()
        engine.store = Mock()
        engine.store.live = Mock()
        engine._last_picker_fill = None
        engine._image = Mock(return_value=dummy)
        engine._dismiss_empty_wave_warning = Mock(return_value=False)
        engine.tap_rel = Mock()
        engine._wait_for = Mock(return_value=dummy)
        engine._picker_overlay_open = Mock(return_value=False)
        engine._select_best_picker_card = Mock(return_value=True)
        engine._is_plain_formation = Mock(return_value=False)
        engine._read_ratio_from_image = Mock(
            side_effect=lambda _image, key: {
                "picker_units": (10, 10),
                "formation_units": None,
                "formation_tools": None,
            }.get(key)
        )
        engine.diagnose_unit_picker_confirm = Mock(return_value=(True, "confirmed", dummy))
        engine._tap_norm = Mock()
        with patch("e4kbot.client.find_picker_max_control", return_value=None):
            with patch("e4kbot.client.CONTROL") as control:
                control.sleep = Mock()
                ok, reason = engine._prepare_single_center_wave()
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        taps = [call.args[0] for call in engine.tap_rel.call_args_list]
        self.assertIn("unit_slot", taps)
        self.assertIn("picker_max", taps)
        kwargs = engine.diagnose_unit_picker_confirm.call_args.kwargs
        self.assertEqual(kwargs.get("observed_fill"), (10, 10))
        self.assertTrue(kwargs.get("click"))

    def test_second_attack_clicks_confirm_after_max(self) -> None:
        dummy = Image.new("RGB", (900, 1600), (80, 50, 30))
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.config = {"vision": {"picker_max_actions": 1, "minimum_flank_fill": 0.70}}
        engine.telegram = Mock()
        engine.store = Mock()
        engine.store.live = Mock()
        engine._last_picker_fill = (10, 10)
        engine._image = Mock(return_value=dummy)
        engine.tap_rel = Mock()
        engine._wait_for = Mock(return_value=dummy)
        engine._is_plain_formation = Mock(return_value=True)
        engine._select_best_picker_card = Mock(return_value=True)
        engine._dismiss_empty_wave_warning = Mock(return_value=False)

        def dump_max() -> bool:
            engine._last_picker_fill = (10, 10)
            return True

        engine._dump_picker_max = Mock(side_effect=dump_max)
        engine._read_ratio_from_image = Mock(
            side_effect=lambda _image, key: {
                "picker_units": (10, 10),
                "formation_units": None,
                "formation_tools": None,
            }.get(key)
        )
        engine.diagnose_unit_picker_confirm = Mock(return_value=(True, "confirmed", dummy))
        ok, reason = engine._prepare_single_center_wave()
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        engine._dump_picker_max.assert_called_once()
        engine.diagnose_unit_picker_confirm.assert_called_once()
        kwargs = engine.diagnose_unit_picker_confirm.call_args.kwargs
        self.assertTrue(kwargs.get("click"))
        self.assertEqual(kwargs.get("observed_fill"), (10, 10))

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

    def test_selects_visible_robber_without_ocr_coordinates(self) -> None:
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine._blocked_screen_targets = []
        engine._selected_target_coords = None
        image = Image.new("RGB", (900, 1600), (104, 151, 57))
        chosen = engine._choose_visible_target_without_ocr(
            image,
            "baron",
            [(0.42, 0.51, 0.9), (0.70, 0.80, 0.8)],
        )
        self.assertEqual(chosen, (0.42, 0.51))
        self.assertIsNotNone(engine._selected_target_coords)

    def test_rejects_implausible_map_viewport(self) -> None:
        self.assertFalse(
            BlueStacksEngine._coords_plausible((607, 738), (614, 144))
        )
        self.assertTrue(
            BlueStacksEngine._coords_plausible((607, 738), (607, 735))
        )

    def test_offer_rail_zone_starts_at_eighty_two_percent(self) -> None:
        self.assertFalse(is_offer_rail_point(0.81, 0.04))
        self.assertTrue(is_offer_rail_point(0.82, 0.04))
        self.assertTrue(is_offer_rail_point(0.94, 0.034))
        self.assertTrue(is_offer_rail_point(0.90, 0.14))
        self.assertFalse(is_offer_rail_point(0.85, 0.96))
        self.assertEqual(OFFER_RAIL_X, 0.82)

    def test_popup_action_never_returns_offer_rail_point(self) -> None:
        image = Image.new("RGB", (900, 1600), (40, 35, 50))
        draw = ImageDraw.Draw(image)
        draw.rectangle((760, 90, 840, 170), fill=(205, 35, 20))
        action = popup_action(image)
        if action is not None:
            self.assertLess(action[0], OFFER_RAIL_X)

    def test_blocks_tap_on_offer_rail_during_map(self) -> None:
        green = Image.new("RGB", (900, 1600), (104, 151, 57))
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.adb = Mock()
        engine._image = Mock(return_value=green)
        engine._size = Mock(return_value=(900, 1600))
        engine._tap_norm(0.94, 0.034)
        engine.adb.tap.assert_not_called()

    def test_allows_special_offers_close_on_rail_when_overlay_open(self) -> None:
        image = Image.new("RGB", (900, 1600), (40, 35, 50))
        draw = ImageDraw.Draw(image)
        draw.rectangle((80, 220, 820, 1400), fill=(235, 220, 190))
        draw.rectangle((820, 40, 880, 100), fill=(205, 35, 20))
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.adb = Mock()
        engine._image = Mock(return_value=image)
        engine._size = Mock(return_value=(900, 1600))
        engine._plan_or_picker_open = Mock(return_value=False)
        with patch("e4kbot.client.is_special_offers_screen", return_value=True):
            with patch(
                "e4kbot.client.special_offers_close_point",
                return_value=(0.93, 0.04),
            ):
                with patch("e4kbot.client.time.sleep"):
                    engine._tap_norm_exact(0.93, 0.04)
        engine.adb.tap.assert_called_once()

    def test_special_offers_overlay_clicks_only_red_x(self) -> None:
        dummy = Image.new("RGB", (900, 1600), (40, 35, 50))
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.adb = Mock()
        engine._size = Mock(return_value=(900, 1600))
        engine._image = Mock(return_value=dummy)
        engine._plan_or_picker_open = Mock(return_value=False)
        with patch("e4kbot.client.is_special_offers_screen", return_value=True):
            with patch(
                "e4kbot.client.special_offers_close_point",
                return_value=(0.93, 0.04),
            ):
                with patch("e4kbot.client.CONTROL") as control:
                    control.sleep = Mock()
                    with patch("e4kbot.client.time.sleep"):
                        closed = engine._dismiss_special_offers_if_open(dummy)
                        engine._tap_norm(0.90, 0.20)
                        engine._tap_norm(0.88, 0.35)
        self.assertTrue(closed)
        self.assertEqual(engine.adb.tap.call_count, 1)
        self.assertEqual(engine.adb.tap.call_args.args[0], 837)
        self.assertEqual(engine.adb.tap.call_args.args[1], 64)

    def test_search_button_is_never_tapped(self) -> None:
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.layout = {"buttons": {"search": [0.90, 0.14]}}
        engine._plan_or_picker_open = Mock(return_value=False)
        engine._tap_norm = Mock()
        engine.tap_rel("search")
        engine._tap_norm.assert_not_called()

    def test_napadenie_footer_is_not_offer_rail(self) -> None:
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.adb = Mock()
        engine._size = Mock(return_value=(900, 1600))
        engine._image = Mock(return_value=Image.new("RGB", (900, 1600), (80, 50, 30)))
        with patch("e4kbot.client.time.sleep"):
            engine._tap_norm(0.85, 0.96)
        engine.adb.tap.assert_called()

    def test_tap_rel_close_redirects_from_offer_rail(self) -> None:
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.layout = {"buttons": {"close": [0.94, 0.034]}}
        engine._plan_or_picker_open = Mock(return_value=False)
        engine._tap_norm = Mock()
        engine._dismiss_special_offers_if_open = Mock()
        engine.tap_rel("close")
        engine._tap_norm.assert_not_called()
        engine._dismiss_special_offers_if_open.assert_called_once()

    def test_dismiss_popups_skips_blind_close_on_rail(self) -> None:
        green = Image.new("RGB", (900, 1600), (104, 151, 57))
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.layout = {"dismiss": [], "buttons": {"close": [0.94, 0.034]}}
        engine._dismiss_blocking_overlay = Mock(return_value=False)
        engine._tap_norm = Mock()
        engine.dismiss_popups()
        engine._dismiss_blocking_overlay.assert_called_once()
        engine._tap_norm.assert_not_called()


class HuntTests(unittest.TestCase):
    def test_hunt_quota_defaults_to_ten_commanders(self) -> None:
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.config = {}
        self.assertEqual(engine._hunt_quota(), 10)
        engine.config = {"max_commanders": 10}
        self.assertEqual(engine._hunt_quota(), 10)
        engine.config = {"army_slots": 7}
        self.assertEqual(engine._hunt_quota(), 7)

    def test_jump_to_coords_does_not_tap_search(self) -> None:
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.tap_rel = Mock()
        engine.adb = Mock()
        engine._jump_to_coords((607, 738))
        engine.tap_rel.assert_not_called()
        engine.adb.text.assert_not_called()

    def test_focus_hunt_skips_missing_target_without_search(self) -> None:
        green = Image.new("RGB", (900, 1600), (104, 151, 57))
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine._await_world_map = Mock(return_value=green)
        engine._dismiss_special_offers_if_open = Mock(return_value=False)
        engine._match_visible_target = Mock(return_value=None)
        engine._recenter_on_main_castle = Mock(return_value=green)
        engine._jump_to_coords = Mock()
        point = engine._focus_hunt_target(
            "baron", HuntTarget((0.42, 0.51), (609, 739))
        )
        self.assertIsNone(point)
        engine._jump_to_coords.assert_not_called()

    def test_recenter_does_not_jump_via_search(self) -> None:
        green = Image.new("RGB", (900, 1600), (104, 151, 57))
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine._read_map_coords = Mock(return_value=((607, 738), (620, 750)))
        engine._jump_to_coords = Mock()
        engine._pan_map = Mock()
        engine._image = Mock(return_value=green)
        with patch("e4kbot.client.find_main_castle_marker", return_value=None):
            result = engine._recenter_on_main_castle(green)
        engine._jump_to_coords.assert_not_called()
        engine._pan_map.assert_not_called()
        self.assertIs(result, green)

    def test_hunt_swipes_when_no_visible_target(self) -> None:
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.config = {"vision": {"map_scan_rings": 1, "map_scan_span": [0.3, 0.24]}}
        engine.store = Mock()
        engine.store.live = Mock()
        green = Image.new("RGB", (900, 1600), (104, 151, 57))
        engine._image = Mock(return_value=green)
        engine._recenter_on_main_castle = Mock(return_value=green)
        engine._select_visible_target = Mock(
            side_effect=[None, None, (0.42, 0.51)]
        )
        engine._pan_map = Mock()
        with patch("e4kbot.client.is_map_screen", return_value=True):
            with patch("e4kbot.client.CONTROL") as control:
                control.sleep = Mock()
                point = engine._hunt_robbers("baron")
        self.assertEqual(point, (0.42, 0.51))
        self.assertGreaterEqual(engine._pan_map.call_count, 1)
        engine.store.save.assert_called()

    def test_hunt_collects_quota_then_stops(self) -> None:
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.config = {"max_commanders": 2, "vision": {"map_scan_rings": 1}}
        engine.store = Mock()
        engine.store.live = Mock()
        green = Image.new("RGB", (900, 1600), (104, 151, 57))
        engine._image = Mock(return_value=green)
        engine._recenter_on_main_castle = Mock(return_value=green)
        engine._pan_map = Mock()
        engine._list_eligible_targets = Mock(
            return_value=[
                HuntTarget((0.40, 0.50), (601, 700)),
                HuntTarget((0.55, 0.52), (603, 701)),
                HuntTarget((0.70, 0.60), (605, 702)),
            ]
        )
        with patch("e4kbot.client.is_map_screen", return_value=True):
            batch = engine._collect_hunt_batch("baron")
        self.assertEqual(len(batch), 2)
        self.assertEqual(batch[0].coords, (601, 700))
        self.assertEqual(batch[1].coords, (603, 701))
        engine._pan_map.assert_not_called()

    def test_on_screen_attack_hunts_before_giving_up(self) -> None:
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.config = {"vision": {"popup_retries": 0}}
        engine._blocked_screen_targets = []
        engine._hunt_queue = []
        green = Image.new("RGB", (900, 1600), (104, 151, 57))
        engine._image = Mock(return_value=green)
        engine._plan_or_picker_open = Mock(return_value=False)
        engine._collect_hunt_batch = Mock(return_value=[])
        result = engine.on_screen_attack("baron")
        self.assertEqual(result, "no_targets")
        engine._collect_hunt_batch.assert_called_once_with("baron")

    def test_on_screen_attack_goes_napadenie_feather_then_send(self) -> None:
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.config = {"vision": {"popup_retries": 0}, "baron_attacks": {"kingdom": 0}}
        engine._blocked_screen_targets = []
        engine._hunt_queue = []
        engine._selected_target_coords = (607, 743)
        engine.store = Mock()
        engine.store.live = Mock()
        travel = Image.new("RGB", (900, 1600), (55, 35, 25))
        engine._collect_hunt_batch = Mock(
            return_value=[HuntTarget((0.42, 0.51), (607, 743))]
        )
        engine._focus_hunt_target = Mock(return_value=(0.42, 0.51))
        engine._open_formation = Mock(return_value=True)
        engine._prepare_single_center_wave = Mock(return_value=(True, ""))
        engine._plan_or_picker_open = Mock(return_value=False)
        engine.tap_rel = Mock()
        engine._wait_for = Mock(return_value=travel)
        engine._movement_option = Mock(return_value=("feather", 3))
        engine._image = Mock(return_value=travel)
        engine._read_march_time = Mock(return_value=188)
        engine._finish_attack = Mock(return_value="baron")
        with patch("e4kbot.client.time.sleep"):
            result = engine.on_screen_attack("baron")
        self.assertEqual(result, "baron")
        engine._tap_formation_attack = getattr(
            engine, "_tap_formation_attack", None
        )
        taps = [call.args[0] for call in engine.tap_rel.call_args_list]
        self.assertIn("formation_attack", taps)
        engine._movement_option.assert_called_once()
        engine._finish_attack.assert_called_once()
        self.assertEqual(engine._finish_attack.call_args.args[3], "feather")

    def test_second_attack_uses_stored_list_without_rescan(self) -> None:
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.config = {"vision": {"popup_retries": 0}, "baron_attacks": {"kingdom": 0}}
        engine._blocked_screen_targets = []
        engine._hunt_queue = []
        engine._selected_target_coords = (607, 743)
        engine.store = Mock()
        engine.store.live = Mock()
        travel = Image.new("RGB", (900, 1600), (55, 35, 25))
        engine._collect_hunt_batch = Mock(
            return_value=[
                HuntTarget((0.42, 0.51), (607, 743)),
                HuntTarget((0.58, 0.55), (609, 740)),
            ]
        )
        engine._focus_hunt_target = Mock(side_effect=[(0.42, 0.51), (0.58, 0.55)])
        engine._open_formation = Mock(return_value=True)
        engine._prepare_single_center_wave = Mock(return_value=(True, ""))
        engine._plan_or_picker_open = Mock(return_value=False)
        engine.tap_rel = Mock()
        engine._wait_for = Mock(return_value=travel)
        engine._movement_option = Mock(return_value=("feather", 3))
        engine._image = Mock(return_value=travel)
        engine._read_march_time = Mock(return_value=188)
        engine._finish_attack = Mock(return_value="baron")
        with patch("e4kbot.client.time.sleep"):
            first = engine.on_screen_attack("baron")
            second = engine.on_screen_attack("baron")
        self.assertEqual(first, "baron")
        self.assertEqual(second, "baron")
        engine._collect_hunt_batch.assert_called_once_with("baron")
        self.assertEqual(engine._focus_hunt_target.call_count, 2)
        self.assertEqual(engine._finish_attack.call_count, 2)
        self.assertEqual(engine._prepare_single_center_wave.call_count, 2)

    def test_formation_open_never_taps_red_x(self) -> None:
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.layout = {
            "buttons": {
                "formation_close": [0.94, 0.034],
                "close": [0.94, 0.034],
                "map": [0.12, 0.94],
                "formation_attack": [0.80, 0.96],
            }
        }
        engine._plan_or_picker_open = Mock(return_value=True)
        engine._tap_norm = Mock()
        engine.tap_rel("formation_close")
        engine.tap_rel("close")
        engine.tap_rel("map")
        engine._tap_norm.assert_not_called()
        engine.tap_rel("formation_attack")
        engine._tap_norm.assert_called_once()

    def test_failed_prepare_does_not_close_plan(self) -> None:
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.store = Mock()
        engine.store.live = Mock()
        engine.tap_rel = Mock()
        engine._dismiss_empty_wave_warning = Mock(return_value=False)
        engine._prepare_single_center_wave = Mock(
            return_value=(False, "unit_picker_fill_not_retained")
        )
        result = engine._execute_formation_attack("baron", (0.42, 0.51))
        self.assertEqual(result, "unsafe_formation")
        engine.tap_rel.assert_not_called()

    def test_open_plan_skips_hunt_and_reuses_first_attack_path(self) -> None:
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.config = {}
        engine._hunt_queue = [HuntTarget((0.42, 0.51), (607, 743))]
        engine._blocked_screen_targets = []
        engine._image = Mock(return_value=Image.new("RGB", (900, 1600), (80, 50, 30)))
        engine._dismiss_empty_wave_warning = Mock(return_value=False)
        engine._dismiss_no_commanders = Mock(return_value=False)
        engine._plan_or_picker_open = Mock(return_value=True)
        engine._collect_hunt_batch = Mock()
        engine._execute_formation_attack = Mock(return_value="baron")
        result = engine.on_screen_attack("baron")
        self.assertEqual(result, "baron")
        engine._collect_hunt_batch.assert_not_called()
        engine._execute_formation_attack.assert_called_once()

    def test_on_screen_attack_stops_on_no_commanders_inscription(self) -> None:
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.config = {}
        engine._hunt_queue = []
        engine._blocked_screen_targets = []
        engine._image = Mock(return_value=Image.new("RGB", (900, 1600), (80, 50, 30)))
        engine._dismiss_empty_wave_warning = Mock(return_value=False)
        engine._dismiss_no_commanders = Mock(return_value=True)
        engine._collect_hunt_batch = Mock()
        self.assertEqual(engine.on_screen_attack("baron"), "no_commanders")
        engine._collect_hunt_batch.assert_not_called()

    def test_second_prepare_still_clicks_confirm_like_first(self) -> None:
        dummy = Image.new("RGB", (900, 1600), (80, 50, 30))
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.config = {"vision": {"picker_max_actions": 1, "minimum_flank_fill": 0.70}}
        engine.telegram = Mock()
        engine.store = Mock()
        engine.store.live = Mock()
        engine._image = Mock(return_value=dummy)
        engine.tap_rel = Mock()
        engine._wait_for = Mock(return_value=dummy)
        engine._picker_overlay_open = Mock(return_value=False)
        engine._dump_picker_max = Mock(
            side_effect=lambda: setattr(engine, "_last_picker_fill", (10, 10)) or True
        )
        engine._dismiss_empty_wave_warning = Mock(return_value=False)
        engine._read_ratio_from_image = Mock(
            side_effect=lambda _image, key: {
                "picker_units": (10, 10),
                "formation_units": None,
                "formation_tools": None,
            }.get(key)
        )
        engine.diagnose_unit_picker_confirm = Mock(return_value=(True, "confirmed", dummy))
        first = engine._prepare_single_center_wave()
        second = engine._prepare_single_center_wave()
        self.assertEqual(first, (True, ""))
        self.assertEqual(second, (True, ""))
        self.assertEqual(engine.diagnose_unit_picker_confirm.call_count, 2)
        self.assertEqual(engine._dump_picker_max.call_count, 2)
        for call in engine.diagnose_unit_picker_confirm.call_args_list:
            self.assertTrue(call.kwargs.get("click"))
            self.assertEqual(call.kwargs.get("observed_fill"), (10, 10))

    def test_fourth_prepare_wave_always_max_and_confirm(self) -> None:
        dummy = Image.new("RGB", (900, 1600), (80, 50, 30))
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.config = {"vision": {"picker_max_actions": 1, "minimum_flank_fill": 0.70}}
        engine.telegram = Mock()
        engine.store = Mock()
        engine.store.live = Mock()
        engine._image = Mock(return_value=dummy)
        engine.tap_rel = Mock()
        engine._wait_for = Mock(return_value=dummy)
        engine._picker_overlay_open = Mock(return_value=False)
        engine._dump_picker_max = Mock(
            side_effect=lambda: setattr(engine, "_last_picker_fill", (10, 10)) or True
        )
        engine._dismiss_empty_wave_warning = Mock(return_value=False)
        engine._read_ratio_from_image = Mock(
            side_effect=lambda _image, key: {
                "picker_units": (10, 10),
                "formation_units": None,
                "formation_tools": None,
            }.get(key)
        )
        engine.diagnose_unit_picker_confirm = Mock(return_value=(True, "confirmed", dummy))
        for wave in range(4):
            ok, reason = engine._prepare_single_center_wave()
            self.assertEqual((ok, reason), (True, ""), msg=f"wave {wave + 1}")
        self.assertEqual(engine._dump_picker_max.call_count, 4)
        self.assertEqual(engine.diagnose_unit_picker_confirm.call_count, 4)
        for call in engine.diagnose_unit_picker_confirm.call_args_list:
            self.assertTrue(call.kwargs.get("click"))

    def test_open_picker_when_overlay_already_visible(self) -> None:
        dummy = Image.new("RGB", (900, 1600), (80, 50, 30))
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine._image = Mock(return_value=dummy)
        engine.tap_rel = Mock()
        engine._wait_for = Mock()
        engine._picker_overlay_open = Mock(return_value=True)
        picker, reason = engine._open_unit_picker("unit_slot")
        self.assertIs(picker, dummy)
        self.assertEqual(reason, "")
        engine.tap_rel.assert_not_called()
        engine._wait_for.assert_not_called()

    def test_select_best_picker_card_does_not_skip_on_stale_full_ocr(self) -> None:
        dummy = Image.new("RGB", (900, 1600), (80, 50, 30))
        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.config = {"vision": {"picker_timeout_seconds": 1}}
        engine._last_picker_fill = None
        engine._image = Mock(return_value=dummy)
        engine._tap_norm = Mock()
        engine._is_plain_formation = Mock(return_value=False)
        engine._read_ratio_from_image = Mock(
            side_effect=[
                (10, 10),
                (10, 10),
                (10, 10),
                (10, 10),
            ]
        )
        with patch("e4kbot.client.find_picker_cards", return_value=[]):
            self.assertFalse(engine._select_best_picker_card())
        engine._tap_norm.assert_not_called()


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
