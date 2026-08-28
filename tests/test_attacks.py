from __future__ import annotations

import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from e4kbot.attacks.registry import ATTACK_MODULES, get_attack_module
from e4kbot.attacks.samurai_camps import SAMURAI_SESSION_QUOTA, merge_tool_bonus_pages, wave_has_units
from e4kbot.attacks.nomad_camps import (
    FRESH_CAMP_REMAINING,
    NOMAD_CAMP_LIMIT,
    NOMAD_SESSION_QUOTA,
)
from e4kbot.modes.catalog import MODE_BY_ID, MODES
from e4kbot.paths import ROOT
from e4kbot.state import StateStore
from e4kbot.vision import (
    APPLY_PRESET_ALL_TEMPLATE,
    AUTOSELECT_BUTTON_TEMPLATE,
    AUTOSELECT_DIALOG_TEMPLATE,
    DAIMYO_TEMPLATES,
    FEATHER_HORSE_MIN_X,
    FEATHER_HORSE_TEMPLATE,
    NOMAD_TEMPLATE,
    NOMAD_TOOL_BADGE_TEMPLATE,
    NOMAD_TOOL_TILE_TEMPLATE,
    NomadToolStock,
    PRESET_BUTTON_TEMPLATE,
    PRESETS_DIALOG_TEMPLATE,
    SAMURAI_FEATHER_HORSE_TEMPLATE,
    SAMURAI_TEMPLATE,
    SAVE_PRESET_BUTTON_TEMPLATE,
    SAVE_PRESET_CONFIRM_TEMPLATE,
    TOOL_BONUS_TEMPLATE,
    assign_flank_tools,
    find_apply_preset_all,
    find_autoselect_button,
    find_daimyo_candidates,
    find_feather_horse,
    find_in_stock_tool_plus,
    find_nomad_candidates,
    find_preset_button,
    find_preset_dialog_close,
    find_save_preset_button,
    find_samurai_candidates,
    find_template_center,
    find_tool_bonus_candidates,
    is_autoselect_dialog,
    is_feather_selected,
    is_info_plaque,
    is_presets_dialog,
    is_ruby_horse_selected,
    is_save_preset_dialog,
    parse_report_resources,
    parse_samurai_camp_level,
    plaque_text_is_player_keep,
    plaque_text_is_samurai_camp,
    remaining_attacks_from_level,
    remaining_attacks_from_nomad_level,
)


def _canvas_with(template_path: Path, at: tuple[float, float]) -> Image.Image:
    image = Image.new("RGB", (900, 1600), (72, 48, 28))
    tmpl = Image.open(template_path).convert("RGB")
    x = max(0, int(at[0] * 900 - tmpl.width / 2))
    y = max(0, int(at[1] * 1600 - tmpl.height / 2))
    image.paste(tmpl, (x, y))
    return image


class AttackModuleRegistryTests(unittest.TestCase):
    def test_every_catalog_mode_has_a_module(self) -> None:
        self.assertEqual(set(ATTACK_MODULES), {mode.id for mode in MODES})
        robber = get_attack_module("robber_barons")
        samurai = get_attack_module("samurai_camps")
        nomad = get_attack_module("nomad_camps")
        dragons = get_attack_module("dragons")
        self.assertEqual(robber.spec_id, "robber_barons")
        self.assertFalse(getattr(robber, "is_stub", False))
        self.assertEqual(samurai.spec_id, "samurai_camps")
        self.assertFalse(getattr(samurai, "is_stub", False))
        self.assertEqual(nomad.spec_id, "nomad_camps")
        self.assertFalse(getattr(nomad, "is_stub", False))
        self.assertEqual(dragons.run_cycle(), "stub:dragons")
        self.assertEqual(MODE_BY_ID["samurai_camps"].status, "live")
        self.assertEqual(MODE_BY_ID["samurai_camps"].default_quota, 44)
        self.assertEqual(MODE_BY_ID["nomad_camps"].status, "live")
        self.assertEqual(MODE_BY_ID["nomad_camps"].default_quota, 44)
        self.assertEqual(SAMURAI_SESSION_QUOTA, 44)


class SamuraiLimitsTests(unittest.TestCase):
    def test_remaining_from_level_last_digit(self) -> None:
        self.assertEqual(remaining_attacks_from_level(41), 10)
        self.assertEqual(remaining_attacks_from_level(31), 10)
        self.assertEqual(remaining_attacks_from_level(101), 10)
        self.assertEqual(remaining_attacks_from_level(42), 9)
        self.assertEqual(remaining_attacks_from_level(49), 2)
        self.assertEqual(remaining_attacks_from_level(50), 1)
        self.assertEqual(parse_samurai_camp_level("Лагерь самураев Ур. 41"), 41)
        self.assertEqual(parse_samurai_camp_level("lvl 21"), 21)

    def test_plaque_text_rejects_player_keep_and_accepts_samurai_camp(self) -> None:
        self.assertTrue(plaque_text_is_samurai_camp("Лагерь самураев Ур. 41"))
        self.assertTrue(plaque_text_is_samurai_camp("Samurai camp lvl 31"))
        self.assertFalse(plaque_text_is_player_keep("Лагерь самураев Ур. 41"))
        self.assertTrue(plaque_text_is_player_keep("Замок Abozaval"))
        self.assertTrue(plaque_text_is_player_keep("Castle ThirdAbob"))
        self.assertTrue(plaque_text_is_player_keep("Даймё крепость"))
        self.assertFalse(plaque_text_is_samurai_camp("Замок Abozaval"))
        self.assertFalse(plaque_text_is_samurai_camp("Va ng moka davon Abozaval"))

    def test_ten_remaining_then_camp_rests_without_three_hour_cd(self) -> None:
        store = StateStore(path=self._tmp("state.json"))
        store.set_samurai_remaining((100, 200), 10)
        for index in range(9):
            march = store.register_march(index + 1, index + 1, "samurai", 0, 100, 200, 30)
            self.assertEqual(march.cooldown_until, 0.0)
            self.assertEqual(store.samurai_remaining_for((100, 200)), 9 - index)
            self.assertTrue(store.target_available("samurai", 0, 100, 200))
        last = store.register_march(10, 10, "samurai", 0, 100, 200, 30)
        self.assertGreater(last.cooldown_until, 0)
        self.assertEqual(store.samurai_remaining_for((100, 200)), 0)
        self.assertFalse(store.target_available("samurai", 0, 100, 200))

    def test_barons_keep_three_hour_cooldown(self) -> None:
        store = StateStore(path=self._tmp("state.json"))
        march = store.register_march(1, 1, "baron", 0, 10, 20, 40)
        self.assertGreater(march.cooldown_until, march.sent_at + 3 * 60 * 60 - 1)

    def test_does_not_save_preset_when_wave_has_units(self) -> None:
        self.assertTrue(wave_has_units((12, 130)))
        self.assertFalse(wave_has_units((0, 130)))
        self.assertFalse(wave_has_units(None))

    def test_no_tools_does_not_press_attack(self) -> None:
        from e4kbot.attacks.samurai_camps import SamuraiCampsModule

        module = SamuraiCampsModule()
        calls: list[str] = []

        class _Driver:
            layout = {"buttons": {}, "regions": {}}

            def _image(self):
                return Image.new("RGB", (900, 1600), (40, 28, 20))

            def _read_ratio_from_image(self, _image, name):
                if name == "formation_units":
                    return (0, 297)
                if name == "formation_tools":
                    return (0, 40)
                return None

        module._fill_support_tools = lambda _d: calls.append("fill") or False  # type: ignore[method-assign]
        module._first_preset_flow = lambda _d: calls.append("preset") or True  # type: ignore[method-assign]
        module._run_autoselect = lambda _d: calls.append("autoselect") or True  # type: ignore[method-assign]

        with patch(
            "e4kbot.attacks.samurai_camps.is_formation_screen",
            return_value=True,
        ):
            ok, reason = module._prepare_waves(_Driver())
        self.assertFalse(ok)
        self.assertEqual(reason, "tools_not_filled")
        self.assertEqual(calls, ["fill"])
        self.assertFalse(module._preset_ready)

    def test_first_attack_saves_preset_then_autoselect(self) -> None:
        from e4kbot.attacks.samurai_camps import SamuraiCampsModule

        module = SamuraiCampsModule()
        calls: list[str] = []

        class _Driver:
            layout = {"buttons": {}, "regions": {}}

            def _image(self):
                return Image.new("RGB", (900, 1600), (40, 28, 20))

            def _read_ratio_from_image(self, _image, name):
                if name == "formation_units":
                    return (0, 297) if "autoselect" not in calls else (297, 297)
                if name == "formation_tools":
                    return (40, 40) if "fill" in calls else (0, 40)
                return None

        module._fill_support_tools = lambda _d: calls.append("fill") or True  # type: ignore[method-assign]
        module._first_preset_flow = lambda _d: calls.append("preset") or True  # type: ignore[method-assign]
        module._run_autoselect = lambda _d: calls.append("autoselect") or True  # type: ignore[method-assign]

        with patch(
            "e4kbot.attacks.samurai_camps.is_formation_screen",
            return_value=True,
        ):
            ok, reason = module._prepare_waves(_Driver())
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        self.assertEqual(calls, ["fill", "preset", "autoselect"])
        self.assertTrue(module._preset_ready)

    def test_later_attack_applies_preset_and_checks_front_tools(self) -> None:
        from e4kbot.attacks.samurai_camps import SamuraiCampsModule

        module = SamuraiCampsModule()
        module._preset_ready = True
        calls: list[str] = []

        class _Driver:
            layout = {"buttons": {"flank_2": [0.14, 0.51]}, "regions": {}}

            def _image(self):
                return Image.new("RGB", (900, 1600), (40, 28, 20))

            def _read_ratio_from_image(self, _image, name):
                if name == "formation_units":
                    return (0, 297)
                if name == "formation_tools":
                    return (40, 40) if "apply" in calls else (0, 40)
                return None

            def _tap_norm_exact(self, *_args):
                calls.append("front")

        module._apply_preset_only = lambda _d: calls.append("apply") or True  # type: ignore[method-assign]
        module._fill_support_tools = lambda _d: calls.append("fill") or True  # type: ignore[method-assign]
        module._run_autoselect = lambda _d: calls.append("autoselect") or True  # type: ignore[method-assign]

        with patch(
            "e4kbot.attacks.samurai_camps.is_formation_screen",
            return_value=True,
        ):
            ok, reason = module._prepare_waves(_Driver())
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        self.assertEqual(calls, ["apply", "front", "autoselect"])
        self.assertNotIn("fill", calls)

    def test_later_attack_refills_when_preset_tools_not_max(self) -> None:
        from e4kbot.attacks.samurai_camps import SamuraiCampsModule

        module = SamuraiCampsModule()
        module._preset_ready = True
        calls: list[str] = []

        class _Driver:
            layout = {"buttons": {"flank_2": [0.14, 0.51]}, "regions": {}}

            def _image(self):
                return Image.new("RGB", (900, 1600), (40, 28, 20))

            def _read_ratio_from_image(self, _image, name):
                if name == "formation_units":
                    return (0, 297)
                if name == "formation_tools":
                    return (40, 40) if "fill" in calls else (12, 40)
                return None

            def _tap_norm_exact(self, *_args):
                return None

        module._apply_preset_only = lambda _d: calls.append("apply") or True  # type: ignore[method-assign]
        module._fill_support_tools = lambda _d: calls.append("fill") or True  # type: ignore[method-assign]
        module._save_preset_safe = lambda _d: calls.append("save") or True  # type: ignore[method-assign]
        module._run_autoselect = lambda _d: calls.append("autoselect") or True  # type: ignore[method-assign]

        with patch(
            "e4kbot.attacks.samurai_camps.is_formation_screen",
            return_value=True,
        ):
            ok, reason = module._prepare_waves(_Driver())
        self.assertTrue(ok)
        self.assertEqual(calls, ["apply", "fill", "save", "autoselect"])

    def test_junk_front_ocr_does_not_count_as_empty(self) -> None:
        from e4kbot.attacks.samurai_camps import SamuraiCampsModule

        module = SamuraiCampsModule()
        self.assertFalse(module._tools_full((0, 4)))
        self.assertTrue(module._tools_full((40, 40)))

        class _Driver:
            def _image(self):
                return Image.new("RGB", (900, 1600), (40, 28, 20))

            def _read_ratio_from_image(self, _image, name):
                if name == "formation_tools":
                    return (0, 4)
                return None

        with patch(
            "e4kbot.attacks.samurai_camps.find_formation_tool_slots",
            return_value=[(0.62, 0.70), (0.70, 0.70)],
        ):
            self.assertTrue(module._tools_ready(_Driver()))
        with patch(
            "e4kbot.attacks.samurai_camps.find_formation_tool_slots",
            return_value=[(0.54, 0.70), (0.62, 0.70), (0.70, 0.70)],
        ):
            self.assertFalse(module._tools_ready(_Driver()))

    def test_fill_support_tools_switches_front_left_right(self) -> None:
        from e4kbot.attacks.samurai_camps import SamuraiCampsModule

        taps: list[tuple[float, float]] = []
        fills: list[str] = []

        class _Driver:
            layout = {
                "buttons": {
                    "flank_1": [0.052, 0.51],
                    "flank_2": [0.138, 0.51],
                    "flank_3": [0.218, 0.51],
                },
                "regions": {},
            }

            def _image(self):
                return Image.new("RGB", (900, 1600), (40, 28, 20))

            def _tap_norm_exact(self, x, y):
                taps.append((round(float(x), 3), round(float(y), 3)))

            def _read_ratio_from_image(self, _image, name):
                return (40, 40)

            def _wait_for(self, *_args, **_kwargs):
                return self._image()

        module = SamuraiCampsModule()
        module._fill_flank_tools = lambda _d, name: fills.append(name) or True  # type: ignore[method-assign]
        module._dismiss_tool_picker = lambda _d: None  # type: ignore[method-assign]

        with patch("e4kbot.attacks.samurai_camps.is_formation_screen", return_value=True), patch(
            "e4kbot.attacks.samurai_camps.find_picker_confirm_button", return_value=None
        ), patch(
            "e4kbot.attacks.samurai_camps.is_tool_catalog", return_value=False
        ), patch(
            "e4kbot.attacks.samurai_camps.CONTROL.sleep"
        ):
            ok = module._fill_support_tools(_Driver())
        self.assertTrue(ok)
        self.assertEqual(fills, ["фронт", "левый", "правый"])
        flank_taps = [item for item in taps if item[1] == 0.51]
        self.assertEqual(
            flank_taps,
            [(0.138, 0.51), (0.052, 0.51), (0.218, 0.51), (0.138, 0.51)],
        )

    def test_fill_support_tools_requires_all_three_flanks(self) -> None:
        from e4kbot.attacks.samurai_camps import SamuraiCampsModule

        fills: list[str] = []

        class _Driver:
            layout = {
                "buttons": {
                    "flank_1": [0.052, 0.51],
                    "flank_2": [0.138, 0.51],
                    "flank_3": [0.218, 0.51],
                },
                "regions": {},
            }

            def _image(self):
                return Image.new("RGB", (900, 1600), (40, 28, 20))

            def _tap_norm_exact(self, *_args):
                return None

            def _read_ratio_from_image(self, _image, name):
                return (40, 40) if name == "formation_tools" else (0, 297)

            def _wait_for(self, *_args, **_kwargs):
                return self._image()

        module = SamuraiCampsModule()
        module._fill_flank_tools = lambda _d, name: fills.append(name) or (name != "правый")  # type: ignore[method-assign]
        module._dismiss_tool_picker = lambda _d: None  # type: ignore[method-assign]

        with patch("e4kbot.attacks.samurai_camps.is_formation_screen", return_value=True), patch(
            "e4kbot.attacks.samurai_camps.find_picker_confirm_button", return_value=None
        ), patch(
            "e4kbot.attacks.samurai_camps.is_tool_catalog", return_value=False
        ), patch(
            "e4kbot.attacks.samurai_camps.CONTROL.sleep"
        ):
            ok = module._fill_support_tools(_Driver())
        self.assertFalse(ok)
        self.assertEqual(fills, ["фронт", "левый", "правый"])

    def test_fill_flank_tools_opens_slot_even_if_previous_flank_looks_full(self) -> None:
        from e4kbot.attacks.samurai_camps import SamuraiCampsModule

        taps: list[tuple[float, float]] = []

        class _Driver:
            layout = {"buttons": {"tool_slot": [0.62, 0.70]}, "regions": {}}

            def _image(self):
                return Image.new("RGB", (900, 1600), (40, 28, 20))

            def _tap_norm_exact(self, x, y):
                taps.append((round(float(x), 3), round(float(y), 3)))

            def _read_ratio_from_image(self, _image, name):
                return (40, 40)

            def _wait_for(self, *_args, **_kwargs):
                return self._image()

        module = SamuraiCampsModule()
        with patch("e4kbot.attacks.samurai_camps.is_formation_screen", return_value=True), patch(
            "e4kbot.attacks.samurai_camps.find_picker_confirm_button", return_value=None
        ), patch(
            "e4kbot.attacks.samurai_camps.is_tool_catalog", return_value=False
        ), patch(
            "e4kbot.attacks.samurai_camps.find_formation_tool_slots",
            return_value=[(0.598, 0.697)],
        ), patch(
            "e4kbot.attacks.samurai_camps.CONTROL.sleep"
        ), patch(
            "e4kbot.attacks.samurai_camps.save_shot"
        ):
            ok = module._fill_flank_tools(_Driver(), "левый")
        self.assertTrue(ok)
        self.assertEqual(taps[0], (0.598, 0.697))

    def _tmp(self, name: str):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return Path(folder.name) / name


class SamuraiVisionTests(unittest.TestCase):
    def test_templates_exist(self) -> None:
        for path in (
            TOOL_BONUS_TEMPLATE,
            PRESET_BUTTON_TEMPLATE,
            APPLY_PRESET_ALL_TEMPLATE,
            PRESETS_DIALOG_TEMPLATE,
            AUTOSELECT_BUTTON_TEMPLATE,
            AUTOSELECT_DIALOG_TEMPLATE,
            SAMURAI_TEMPLATE,
            SAVE_PRESET_BUTTON_TEMPLATE,
            SAVE_PRESET_CONFIRM_TEMPLATE,
            FEATHER_HORSE_TEMPLATE,
            SAMURAI_FEATHER_HORSE_TEMPLATE,
            *DAIMYO_TEMPLATES,
        ):
            self.assertTrue(path.exists(), path.name)

    def test_finds_daimyo_on_green_map(self) -> None:
        sprite = DAIMYO_TEMPLATES[0]
        image = Image.new("RGB", (900, 1600), (62, 128, 48))
        tmpl = Image.open(sprite).convert("RGB")
        image.paste(tmpl, (360, 640))
        daimyo = find_daimyo_candidates(image)
        self.assertGreaterEqual(len(daimyo), 1)

    def test_finds_preset_and_autoselect_on_planning_bar(self) -> None:
        image = _canvas_with(PRESET_BUTTON_TEMPLATE, (0.22, 0.93))
        found = find_preset_button(image)
        self.assertIsNotNone(found)
        self.assertGreater(found[1], 0.82)

        image = _canvas_with(AUTOSELECT_BUTTON_TEMPLATE, (0.08, 0.93))
        found = find_autoselect_button(image)
        self.assertIsNotNone(found)
        self.assertLess(found[0], 0.45)

    def test_apply_all_and_preset_close_are_not_plan_close(self) -> None:
        dialog = Image.open(PRESETS_DIALOG_TEMPLATE).convert("RGB")
        self.assertTrue(is_presets_dialog(dialog))
        apply = find_apply_preset_all(dialog)
        self.assertIsNotNone(apply)
        close = find_preset_dialog_close(dialog)
        self.assertIsNotNone(close)
        self.assertGreater(close[1], 0.05)
        self.assertNotAlmostEqual(close[1], 0.034, places=2)
        self.assertGreater(close[0], 0.55)

    def test_autoselect_dialog_is_detected(self) -> None:
        dialog = Image.open(AUTOSELECT_DIALOG_TEMPLATE).convert("RGB")
        self.assertTrue(is_autoselect_dialog(dialog))

    def test_save_preset_confirm_is_detected(self) -> None:
        image = Image.new("RGB", (900, 1600), (48, 32, 22))
        dialog = Image.open(SAVE_PRESET_CONFIRM_TEMPLATE).convert("RGB")
        image.paste(dialog, (228, 604))
        self.assertTrue(is_save_preset_dialog(image))

    def test_save_preset_button_matches_floppy(self) -> None:
        image = _canvas_with(SAVE_PRESET_BUTTON_TEMPLATE, (0.18, 0.58))
        found = find_save_preset_button(image)
        self.assertIsNotNone(found)
        self.assertLess(found[0], 0.45)

    def test_first_in_stock_tool_plus_skips_zero_stock(self) -> None:
        crop = Image.open(ROOT / "assets" / "samurai" / "tools_ladders.png").convert("RGB")
        image = Image.new("RGB", (900, 1600), (72, 48, 28))
        image.paste(crop, (400, 480))
        plus = find_in_stock_tool_plus(image)
        self.assertIsNotNone(plus)
        self.assertGreater(plus[0], 0.70)
        self.assertGreater(plus[1], 0.38)

    def test_tool_bonus_template_matches_itself(self) -> None:
        image = _canvas_with(TOOL_BONUS_TEMPLATE, (0.62, 0.55))
        found = find_template_center(image, TOOL_BONUS_TEMPLATE, threshold=0.8)
        self.assertIsNotNone(found)

    def test_parse_bonus_percent_ignores_wall_minus(self) -> None:
        from e4kbot.vision import parse_bonus_percent

        self.assertEqual(parse_bonus_percent("+3%"), 3)
        self.assertEqual(parse_bonus_percent("+5%"), 5)
        self.assertIsNone(parse_bonus_percent("-25%"))
        self.assertIsNone(parse_bonus_percent("Купить сейчас 20"))

    def test_merge_tool_bonus_pages_keeps_lowest_percent(self) -> None:
        merged = merge_tool_bonus_pages(
            [
                [(5, 0.42, 0.50)],
                [(3, 0.42, 0.48), (5, 0.42, 0.62)],
            ]
        )
        self.assertEqual([item[0] for item in merged], [3, 5])
        self.assertEqual(merged[0][2], 0.48)

    def test_live_tool_picker_prefers_lower_percent(self) -> None:
        path = ROOT / "shots" / "unit-picker-confirm-before.png"
        if not path.exists():
            self.skipTest("no live tool picker screenshot")
        found = find_tool_bonus_candidates(Image.open(path).convert("RGB"))
        percents = [item[0] for item in found]
        if not percents or min(percents) != 3:
            self.skipTest("live picker screenshot no longer shows +3%")
        self.assertEqual(min(percents), 3)

    def test_samurai_camp_template_matches_on_map_canvas(self) -> None:
        self.assertTrue(SAMURAI_TEMPLATE.exists())
        image = Image.new("RGB", (900, 1600), (104, 151, 57))
        tmpl = Image.open(SAMURAI_TEMPLATE).convert("RGB")
        image.paste(tmpl, (360, 400))
        found = find_samurai_candidates(image, 0.65, max_hits=4)
        self.assertGreaterEqual(len(found), 1)
        self.assertLessEqual(len(found), 4)

    def test_live_map_shot_finds_up_to_four_camps(self) -> None:
        path = ROOT / "shots" / "samurai_live_now.png"
        if not path.exists():
            self.skipTest("no live map screenshot")
        found = find_samurai_candidates(Image.open(path).convert("RGB"), 0.65, max_hits=4)
        if len(found) < 3:
            self.skipTest("live screenshot is not a camp map")
        self.assertGreaterEqual(len(found), 3)
        self.assertLessEqual(len(found), 4)

    def test_samurai_finder_does_not_use_color_rings(self) -> None:
        from PIL import ImageDraw

        image = Image.new("RGB", (900, 1600), (104, 151, 57))
        draw = ImageDraw.Draw(image)
        draw.ellipse((300, 500, 360, 560), outline=(255, 80, 0), width=6)
        draw.ellipse((500, 700, 560, 760), outline=(255, 80, 0), width=6)
        self.assertEqual(find_samurai_candidates(image, 0.65), [])

    def test_live_send_shot_does_not_tap_ruby_warhorse(self) -> None:
        path = ROOT / "shots" / "samurai_605_734_1787940203.png"
        if not path.exists():
            self.skipTest("no live ruby-horse send screenshot")
        image = Image.open(path).convert("RGB")
        point = find_feather_horse(image)
        self.assertIsNotNone(point)
        self.assertGreaterEqual(point[0], FEATHER_HORSE_MIN_X)
        self.assertGreater(point[0], 0.72)
        self.assertNotAlmostEqual(point[0], 0.662, places=2)
        self.assertTrue(is_ruby_horse_selected(image))
        self.assertFalse(is_feather_selected(image))

    def test_movement_dialog_feather_is_rightmost_not_ruby(self) -> None:
        path = ROOT / "shots" / "movement-confirm-before.png"
        if not path.exists():
            self.skipTest("no movement-confirm-before screenshot")
        image = Image.open(path).convert("RGB")
        point = find_feather_horse(image)
        self.assertIsNotNone(point)
        self.assertGreaterEqual(point[0], FEATHER_HORSE_MIN_X)
        self.assertGreater(point[0], 0.74)
        self.assertNotAlmostEqual(point[0], 0.662, places=2)
        self.assertTrue(is_ruby_horse_selected(image))
        self.assertFalse(is_feather_selected(image))

    def test_pick_feather_cancels_when_ruby_stays_selected(self) -> None:
        from e4kbot.attacks.samurai_camps import SamuraiCampsModule

        path = ROOT / "shots" / "samurai_605_734_1787940203.png"
        if not path.exists():
            self.skipTest("no live ruby-horse send screenshot")
        image = Image.open(path).convert("RGB")
        taps: list[tuple[float, float]] = []

        class _Driver:
            layout = {"buttons": {"travel_cancel": [0.23, 0.815]}}

            def _image(self):
                return image

            def _tap_norm_exact(self, x, y):
                taps.append((float(x), float(y)))

        module = SamuraiCampsModule()
        with patch("e4kbot.attacks.samurai_camps.CONTROL.sleep"), patch(
            "e4kbot.attacks.samurai_camps.save_shot"
        ):
            result = module._pick_feather(_Driver(), image)
        self.assertEqual(result, "ruby_movement_refused")
        horse_taps = [item for item in taps if item[0] >= FEATHER_HORSE_MIN_X]
        self.assertGreaterEqual(len(horse_taps), 1)
        self.assertTrue(all(item[0] >= FEATHER_HORSE_MIN_X for item in horse_taps))
        self.assertTrue(any(item[0] < 0.40 for item in taps))


class NomadCampsTests(unittest.TestCase):
    def test_templates_exist(self) -> None:
        self.assertTrue(NOMAD_TEMPLATE.exists())
        self.assertTrue(NOMAD_TOOL_BADGE_TEMPLATE.exists())
        self.assertTrue(NOMAD_TOOL_TILE_TEMPLATE.exists())
        self.assertEqual(NOMAD_SESSION_QUOTA, 44)
        self.assertEqual(FRESH_CAMP_REMAINING, 11)
        self.assertEqual(NOMAD_CAMP_LIMIT, 11)

    def test_nomad_camp_template_matches_on_map_canvas(self) -> None:
        image = Image.new("RGB", (900, 1600), (104, 151, 57))
        tmpl = Image.open(NOMAD_TEMPLATE).convert("RGB")
        image.paste(tmpl, (360, 500))
        found = find_nomad_candidates(image, 0.65)
        self.assertGreaterEqual(len(found), 1)
        self.assertLessEqual(len(found), 4)

    def test_nomad_finder_does_not_use_color_rings(self) -> None:
        from PIL import ImageDraw

        image = Image.new("RGB", (900, 1600), (104, 151, 57))
        draw = ImageDraw.Draw(image)
        draw.ellipse((300, 500, 360, 560), outline=(255, 200, 40), width=8)
        draw.ellipse((500, 700, 560, 760), outline=(80, 180, 255), width=6)
        self.assertEqual(find_nomad_candidates(image, 0.65), [])

    def test_info_plaque_is_parchment_not_grass(self) -> None:
        from PIL import ImageDraw

        grass = Image.new("RGB", (900, 1600), (104, 151, 57))
        self.assertFalse(is_info_plaque(grass))
        parchment = Image.new("RGB", (900, 1600), (104, 151, 57))
        draw = ImageDraw.Draw(parchment)
        draw.rectangle((160, 280, 740, 1080), fill=(118, 78, 38))
        self.assertTrue(is_info_plaque(parchment))

    def test_compact_start_attack_seals_are_gate_not_travel(self) -> None:
        from PIL import ImageDraw

        from e4kbot.vision import (
            find_start_attack_gate_seals,
            find_travel_seal_pair,
            is_start_attack_gate,
            is_travel_dialog,
            movement_confirm_diagnostics,
        )

        image = Image.new("RGB", (900, 1600), (104, 151, 57))
        draw = ImageDraw.Draw(image)
        draw.rectangle((80, 220, 820, 1180), fill=(210, 190, 150))
        draw.ellipse((180, 920, 310, 1050), fill=(200, 40, 30))
        draw.ellipse((620, 920, 750, 1050), fill=(40, 170, 55))
        self.assertTrue(is_start_attack_gate(image))
        self.assertFalse(is_travel_dialog(image))
        self.assertIsNone(find_travel_seal_pair(image))
        pair = find_start_attack_gate_seals(image)
        self.assertIsNotNone(pair)
        green, red = pair
        self.assertGreater(green[0], 0.65)
        self.assertLess(red[0], 0.40)
        self.assertAlmostEqual(green[1], 0.616, delta=0.04)
        self.assertLess(green[1], 0.72)
        diagnostic = movement_confirm_diagnostics(image)
        self.assertFalse(diagnostic["valid"])
        if diagnostic["point"] is not None:
            self.assertGreaterEqual(diagnostic["point"][1], 0.74)
        self.assertEqual(find_nomad_candidates(image, 0.65), [])

    def test_bottom_travel_seals_are_the_real_send(self) -> None:
        from PIL import ImageDraw

        from e4kbot.vision import (
            find_start_attack_gate_seals,
            find_travel_seal_pair,
            is_start_attack_gate,
            is_travel_dialog,
            movement_confirm_diagnostics,
        )

        image = Image.new("RGB", (900, 1600), (104, 151, 57))
        draw = ImageDraw.Draw(image)
        draw.rectangle((80, 180, 820, 1420), fill=(210, 190, 150))
        draw.ellipse((142, 1241, 272, 1371), fill=(200, 40, 30))
        draw.ellipse((626, 1241, 756, 1371), fill=(40, 170, 55))
        self.assertFalse(is_start_attack_gate(image))
        self.assertIsNone(find_start_attack_gate_seals(image))
        self.assertTrue(is_travel_dialog(image))
        pair = find_travel_seal_pair(image)
        self.assertIsNotNone(pair)
        green, red = pair
        self.assertGreater(green[0], 0.65)
        self.assertLess(red[0], 0.40)
        self.assertAlmostEqual(green[1], 0.816, delta=0.04)
        self.assertGreaterEqual(green[1], 0.74)
        diagnostic = movement_confirm_diagnostics(image)
        self.assertTrue(diagnostic["valid"])
        self.assertGreater(diagnostic["point"][0], 0.65)
        self.assertGreaterEqual(diagnostic["point"][1], 0.74)
        self.assertAlmostEqual(diagnostic["point"][1], 0.816, delta=0.04)

    def test_movement_diagnostics_prefer_bottom_seal_over_gate(self) -> None:
        from PIL import ImageDraw

        from e4kbot.vision import movement_confirm_diagnostics

        image = Image.new("RGB", (900, 1600), (104, 151, 57))
        draw = ImageDraw.Draw(image)
        draw.rectangle((80, 180, 820, 1420), fill=(210, 190, 150))
        draw.ellipse((180, 920, 310, 1050), fill=(200, 40, 30))
        draw.ellipse((620, 920, 750, 1050), fill=(40, 170, 55))
        draw.ellipse((142, 1241, 272, 1371), fill=(200, 40, 30))
        draw.ellipse((626, 1241, 756, 1371), fill=(40, 170, 55))
        diagnostic = movement_confirm_diagnostics(image)
        self.assertTrue(diagnostic["valid"])
        self.assertGreaterEqual(diagnostic["point"][1], 0.74)
        self.assertAlmostEqual(diagnostic["point"][1], 0.816, delta=0.04)

    def test_grass_map_is_not_a_travel_dialog(self) -> None:
        from e4kbot.vision import is_travel_dialog

        grass = Image.new("RGB", (900, 1600), (104, 151, 57))
        self.assertFalse(is_travel_dialog(grass))

    def test_map_green_seal_is_not_a_reward_claim(self) -> None:
        from PIL import ImageDraw

        from e4kbot.vision import find_reward_confirm

        grass = Image.new("RGB", (900, 1600), (104, 151, 57))
        draw = ImageDraw.Draw(grass)
        draw.ellipse((620, 980, 750, 1110), fill=(40, 170, 55))
        self.assertIsNone(find_reward_confirm(grass))

    def test_nomad_finder_skips_parchment_overlay(self) -> None:
        from PIL import ImageDraw

        image = Image.new("RGB", (900, 1600), (104, 151, 57))
        tmpl = Image.open(NOMAD_TEMPLATE).convert("RGB")
        image.paste(tmpl, (360, 500))
        self.assertGreaterEqual(len(find_nomad_candidates(image, 0.65)), 1)
        draw = ImageDraw.Draw(image)
        draw.rectangle((160, 280, 740, 1080), fill=(118, 78, 38))
        self.assertTrue(is_info_plaque(image))
        self.assertEqual(find_nomad_candidates(image, 0.65), [])

    def test_overview_plaque_is_not_a_nomad_camp(self) -> None:
        from e4kbot.vision import is_overview_plaque

        shot = ROOT / "shots" / "nomad_debug_map.png"
        if not shot.exists():
            self.skipTest("no live overview screenshot")
        image = Image.open(shot)
        self.assertTrue(is_overview_plaque(image))
        self.assertEqual(find_nomad_candidates(image, 0.65), [])
        image = Image.new("RGB", (900, 1600), (104, 151, 57))
        tmpl = Image.open(NOMAD_TEMPLATE).convert("RGB")
        image.paste(tmpl, (360, 40))
        found = find_nomad_candidates(image, 0.65)
        self.assertTrue(all(item[1] > 0.30 for item in found))

    def test_ruby_shop_is_not_a_nomad_camp(self) -> None:
        from e4kbot.vision import is_ruby_shop

        shot = ROOT / "shots" / "nomad_debug_now2.png"
        if not shot.exists():
            self.skipTest("no live ruby shop screenshot")
        image = Image.open(shot)
        self.assertTrue(is_ruby_shop(image))
        self.assertEqual(find_nomad_candidates(image, 0.65), [])

    def test_keep_current_camp_queued_after_send(self) -> None:
        from e4kbot.attacks.nomad_camps import NomadCampsModule
        from e4kbot.client import HuntTarget

        store = StateStore(path=self._tmp("keep.json"))
        store.set_nomad_remaining((598, 735), 10)
        module = NomadCampsModule()
        driver = type("D", (), {})()
        driver.store = store
        driver._selected_target_coords = (598, 735)
        driver._hunt_queue = [HuntTarget((0.4, 0.4), (604, 731))]
        module._keep_current_camp_queued(driver)
        self.assertEqual(driver._hunt_queue[0].coords, (598, 735))
        self.assertEqual(len(driver._hunt_queue), 2)
        driver._last_nomad_point = (0.39, 0.41)
        module._keep_current_camp_queued(driver)
        self.assertEqual(driver._hunt_queue[0].point, (0.39, 0.41))
        self.assertEqual(driver._hunt_queue[0].coords, (598, 735))
        self.assertEqual(store.nomad_farm_xy(), (598, 735))

    def test_canonicalize_nomad_coords_merges_jitter(self) -> None:
        store = StateStore(path=self._tmp("canon.json"))
        store.register_march(1, 1, "nomad", 0, 605, 732, 30)
        self.assertEqual(store.canonicalize_nomad_coords((602, 732)), (605, 732))
        self.assertEqual(store.target_hits("nomad", (602, 734)), 1)
        store.register_march(2, 2, "nomad", 0, 603, 733, 30)
        self.assertEqual(store.target_hits("nomad", (605, 732)), 2)
        self.assertEqual(store.nomad_farm_xy(), (605, 732))

    def test_restore_farm_queue_after_restart(self) -> None:
        from e4kbot.attacks.nomad_camps import NomadCampsModule

        store = StateStore(path=self._tmp("farm.json"))
        store.set_nomad_remaining((605, 732), 10)
        store.live.target_hits["nomad:605:732"] = 1
        store.live.last_screenshot = r"C:\shots\nomad_605_732_1.png"
        module = NomadCampsModule()
        driver = type("D", (), {})()
        driver.store = store
        driver._hunt_queue = []
        driver._last_nomad_point = None
        driver._selected_target_coords = None
        module._restore_farm_queue(driver)
        self.assertEqual(driver._hunt_queue[0].coords, (605, 732))
        self.assertTrue(driver._nomad_recenter_next)

    def test_badge_template_matches_itself(self) -> None:
        image = _canvas_with(NOMAD_TOOL_BADGE_TEMPLATE, (0.62, 0.55))
        found = find_template_center(image, NOMAD_TOOL_BADGE_TEMPLATE, threshold=0.72)
        self.assertIsNotNone(found)

    def test_assign_different_tools_per_flank(self) -> None:
        a = NomadToolStock("3:1:2:3", 3, 40, (0.2, 0.4), (0.6, 0.4))
        b = NomadToolStock("5:4:5:6", 5, 12, (0.2, 0.5), (0.6, 0.5))
        c = NomadToolStock("3:7:8:9", 3, 8, (0.2, 0.6), (0.6, 0.6))
        plan = assign_flank_tools([a, b, c], 3)
        self.assertEqual([item.fingerprint if item else None for item in plan], ["3:1:2:3", "5:4:5:6", "3:7:8:9"])
        reuse = assign_flank_tools([a], 3)
        self.assertEqual({item.fingerprint for item in reuse if item}, {"3:1:2:3"})
        self.assertEqual(assign_flank_tools([], 3), [None, None, None])

    def test_no_three_hour_cooldown_and_remaining(self) -> None:
        store = StateStore(path=self._tmp("state.json"))
        store.set_nomad_remaining((110, 220), 11)
        for index in range(10):
            march = store.register_march(index + 1, index + 1, "nomad", 0, 110, 220, 30)
            self.assertEqual(march.cooldown_until, 0.0)
            self.assertEqual(store.nomad_remaining_for((110, 220)), 10 - index)
            self.assertTrue(store.target_available("nomad", 0, 110, 220))
        last = store.register_march(11, 11, "nomad", 0, 110, 220, 30)
        self.assertGreater(last.cooldown_until, 0)
        self.assertEqual(store.nomad_remaining_for((110, 220)), 0)
        self.assertFalse(store.target_available("nomad", 0, 110, 220))

    def test_ocr_cannot_reset_a_finished_camp(self) -> None:
        store = StateStore(path=self._tmp("ocr.json"))
        store.set_nomad_remaining((50, 60), 11)
        for index in range(11):
            store.register_march(index + 1, index + 1, "nomad", 0, 50, 60, 20)
        self.assertEqual(store.apply_nomad_ocr_remaining((50, 60), 11), 0)
        self.assertFalse(store.camp_has_nomad_budget((50, 60)))

    def test_ocr_does_not_increase_remaining_after_hits(self) -> None:
        store = StateStore(path=self._tmp("ocr2.json"))
        store.set_nomad_remaining((7, 8), 11)
        store.register_march(1, 1, "nomad", 0, 7, 8, 20)
        self.assertEqual(store.nomad_remaining_for((7, 8)), 10)
        self.assertEqual(store.apply_nomad_ocr_remaining((7, 8), 11), 10)

    def test_ocr_can_fix_seeded_ten_before_first_hit(self) -> None:
        store = StateStore(path=self._tmp("ocr3.json"))
        store.set_nomad_remaining((9, 9), 10)
        self.assertEqual(store.apply_nomad_ocr_remaining((9, 9), 11), 11)

    def test_after_eleven_hits_queue_drops_camp(self) -> None:
        from e4kbot.attacks.nomad_camps import NomadCampsModule
        from e4kbot.client import HuntTarget

        store = StateStore(path=self._tmp("queue.json"))
        store.set_nomad_remaining((100, 200), 1)
        store.set_nomad_remaining((300, 400), 11)
        store.register_march(1, 1, "nomad", 0, 100, 200, 20)
        module = NomadCampsModule()
        driver = type("D", (), {})()
        driver.store = store
        driver._selected_target_coords = (100, 200)
        driver._hunt_queue = [HuntTarget((0.2, 0.2), (100, 200)), HuntTarget((0.4, 0.4), (300, 400))]
        module._drop_exhausted_camps(driver)
        self.assertEqual(len(driver._hunt_queue), 1)
        self.assertEqual(driver._hunt_queue[0].coords, (300, 400))

    def test_eleven_sends_write_progress_report(self) -> None:
        from e4kbot.attacks.nomad_camps import NomadCampsModule
        from e4kbot.paths import DATA_DIR

        store = StateStore(path=self._tmp("eleven.json"))
        store.live.session_by_mode["nomad_camps"] = 11
        store.live.nomad_remaining["nomad:100:200"] = 0
        store.live.target_hits["nomad:100:200"] = 11
        module = NomadCampsModule()
        sent_texts: list[str] = []

        class _Telegram:
            def send_text(self, text, kind=None):
                sent_texts.append(text)
                return False

        driver = type("D", (), {})()
        driver.store = store
        driver.telegram = _Telegram()
        driver._selected_target_coords = (100, 200)
        module._maybe_report_eleven(driver, 11)
        report = DATA_DIR / "nomad_eleven_report.json"
        self.assertTrue(report.exists())
        self.assertIn("11 атак", sent_texts[0])
        module._maybe_report_eleven(driver, 12)
        self.assertEqual(len(sent_texts), 1)

    def test_nomad_remaining_from_level_is_eleven_hits(self) -> None:
        self.assertEqual(remaining_attacks_from_nomad_level(11), 11)
        self.assertEqual(remaining_attacks_from_nomad_level(21), 11)
        self.assertEqual(remaining_attacks_from_nomad_level(12), 10)
        self.assertEqual(remaining_attacks_from_nomad_level(19), 3)
        self.assertEqual(remaining_attacks_from_nomad_level(20), 2)

    def test_parse_report_resources(self) -> None:
        loot = parse_report_resources("Дерево 1200 Камень 800 Еда 450 Золото 90 Рубины 2")
        self.assertEqual(loot["wood"], 1200)
        self.assertEqual(loot["stone"], 800)
        self.assertEqual(loot["food"], 450)
        self.assertEqual(loot["gold"], 90)
        self.assertEqual(loot["rubies"], 2)

    def test_no_tools_skips_preset_and_goes_to_autoselect(self) -> None:
        from e4kbot.attacks.nomad_camps import NomadCampsModule

        module = NomadCampsModule()
        calls: list[str] = []

        class _Driver:
            layout = {"buttons": {}, "regions": {}}

            def _image(self):
                return Image.new("RGB", (900, 1600), (40, 28, 20))

            def _read_ratio_from_image(self, _image, name):
                if name == "formation_units":
                    return (0, 130)
                if name == "formation_tools":
                    return (0, 20)
                return None

        module._fill_support_tools = lambda _d: calls.append("fill") or False  # type: ignore[method-assign]
        module._first_preset_flow = lambda _d: calls.append("preset") or True  # type: ignore[method-assign]
        module._run_autoselect = lambda _d: calls.append("autoselect") or True  # type: ignore[method-assign]
        module._dismiss_tool_picker = lambda _d: calls.append("dismiss")  # type: ignore[method-assign]

        with patch(
            "e4kbot.attacks.nomad_camps.is_formation_screen",
            return_value=True,
        ):
            ok, reason = module._prepare_waves(_Driver())
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        self.assertEqual(calls, ["fill", "dismiss", "autoselect"])
        self.assertFalse(module._preset_ready)
        self.assertTrue(module._tools_unavailable)

    def _tmp(self, name: str):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return Path(folder.name) / name


if __name__ == "__main__":
    unittest.main()
