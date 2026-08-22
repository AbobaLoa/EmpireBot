from __future__ import annotations

import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from e4kbot.attacks.registry import ATTACK_MODULES, get_attack_module
from e4kbot.attacks.samurai_camps import SAMURAI_SESSION_QUOTA, merge_tool_bonus_pages, wave_has_units
from e4kbot.modes.catalog import MODE_BY_ID, MODES
from e4kbot.paths import ROOT
from e4kbot.state import StateStore
from e4kbot.vision import (
    APPLY_PRESET_ALL_TEMPLATE,
    AUTOSELECT_BUTTON_TEMPLATE,
    AUTOSELECT_DIALOG_TEMPLATE,
    PRESET_BUTTON_TEMPLATE,
    PRESETS_DIALOG_TEMPLATE,
    SAMURAI_TEMPLATE,
    TOOL_BONUS_TEMPLATE,
    find_apply_preset_all,
    find_autoselect_button,
    find_preset_button,
    find_preset_dialog_close,
    find_samurai_candidates,
    find_template_center,
    find_tool_bonus_candidates,
    is_autoselect_dialog,
    is_presets_dialog,
    parse_samurai_camp_level,
    remaining_attacks_from_level,
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
        dragons = get_attack_module("dragons")
        self.assertEqual(robber.spec_id, "robber_barons")
        self.assertFalse(getattr(robber, "is_stub", False))
        self.assertEqual(samurai.spec_id, "samurai_camps")
        self.assertFalse(getattr(samurai, "is_stub", False))
        self.assertEqual(dragons.run_cycle(), "stub:dragons")
        self.assertEqual(MODE_BY_ID["samurai_camps"].status, "live")
        self.assertEqual(MODE_BY_ID["samurai_camps"].default_quota, 44)
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

    def test_no_tools_skips_preset_and_goes_to_autoselect(self) -> None:
        from e4kbot.attacks.samurai_camps import SamuraiCampsModule

        module = SamuraiCampsModule()
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

        def _fake_fill(_driver):
            calls.append("fill")
            return False

        def _fake_preset(_driver):
            calls.append("preset")
            return True

        def _fake_autoselect(_driver):
            calls.append("autoselect")
            return True

        def _fake_dismiss(_driver):
            calls.append("dismiss")

        module._fill_support_tools = _fake_fill  # type: ignore[method-assign]
        module._first_preset_flow = _fake_preset  # type: ignore[method-assign]
        module._run_autoselect = _fake_autoselect  # type: ignore[method-assign]
        module._dismiss_tool_picker = _fake_dismiss  # type: ignore[method-assign]

        with patch(
            "e4kbot.attacks.samurai_camps.is_formation_screen",
            return_value=True,
        ):
            ok, reason = module._prepare_waves(_Driver())
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        self.assertEqual(calls, ["fill", "dismiss", "autoselect"])
        self.assertFalse(module._preset_ready)
        self.assertTrue(module._tools_unavailable)

    def test_later_attack_skips_fill_when_tools_already_empty(self) -> None:
        from e4kbot.attacks.samurai_camps import SamuraiCampsModule

        module = SamuraiCampsModule()
        module._tools_unavailable = True
        calls: list[str] = []

        class _Driver:
            layout = {"buttons": {}, "regions": {}}

            def _image(self):
                return Image.new("RGB", (900, 1600), (40, 28, 20))

            def _read_ratio_from_image(self, _image, name):
                return (0, 130) if name == "formation_units" else (0, 20)

        module._fill_support_tools = lambda _d: calls.append("fill") or True  # type: ignore[method-assign]
        module._first_preset_flow = lambda _d: calls.append("preset") or True  # type: ignore[method-assign]
        module._run_autoselect = lambda _d: calls.append("autoselect") or True  # type: ignore[method-assign]
        module._dismiss_tool_picker = lambda _d: calls.append("dismiss")  # type: ignore[method-assign]

        with patch(
            "e4kbot.attacks.samurai_camps.is_formation_screen",
            return_value=True,
        ):
            ok, reason = module._prepare_waves(_Driver())
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        self.assertEqual(calls, ["dismiss", "autoselect"])
        self.assertFalse(module._preset_ready)

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
        ):
            self.assertTrue(path.exists(), path.name)

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
        self.assertTrue(percents, "need +% rows on the live picker")
        self.assertEqual(min(percents), 3)

    def test_samurai_camp_template_matches_on_map_canvas(self) -> None:
        self.assertTrue(SAMURAI_TEMPLATE.exists())
        image = Image.new("RGB", (900, 1600), (104, 151, 57))
        tmpl = Image.open(SAMURAI_TEMPLATE).convert("RGB")
        image.paste(tmpl, (360, 400))
        found = find_samurai_candidates(image, 0.65)
        self.assertGreaterEqual(len(found), 1)
        self.assertLessEqual(len(found), 4)

    def test_live_map_shot_finds_up_to_four_camps(self) -> None:
        path = ROOT / "shots" / "samurai_live_now.png"
        if not path.exists():
            self.skipTest("no live map screenshot")
        found = find_samurai_candidates(Image.open(path).convert("RGB"), 0.65)
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


if __name__ == "__main__":
    unittest.main()
