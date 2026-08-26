from __future__ import annotations

import unittest

from PIL import Image

from e4kbot.attacks.registry import get_attack_module
from e4kbot.modes.catalog import MODE_BY_ID
from e4kbot.paths import ROOT
from e4kbot.vision import (
    find_navigation_button,
    find_world_list_sextants,
    is_world_list_open,
    parse_navigation_rows,
)
from e4kbot.worlds import (
    CONTINUE_ONE_WORLD_LINE,
    STORM_UNOPENED_LINE,
    looks_like_wrong_world_target,
    match_target_kind,
    match_world_id,
    scan_from_blob,
    unopened_report_text,
    world_fill_decision,
    write_world_attack_report,
)


NAV_SHOT = ROOT / "assets" / "nav_button.png"
LIST_SHOT = ROOT / "assets" / "world_list.png"
ROWS_SHOT = ROOT / "assets" / "world_list_rows.png"


class WorldOcrTests(unittest.TestCase):
    def test_garbled_list_ocr_detects_open_worlds_and_missing_storm(self) -> None:
        blob = (
            "3amokthirdabobbenukanvmnepnax605y736ganevbenukaavmnepuaxst4y744"
            "mopo3beyhoxonoghbinjlequukx659y676necknmbinarouwmemeckux677y656"
            "aparoorhehhblebepwinhblx636y587"
        )
        self.assertEqual(match_world_id("BeyHoxonogHbin JlequuK"), "everwinter")
        self.assertEqual(match_world_id("OrHeHHble BepwinHbl"), "fire_peaks")
        scan = scan_from_blob(blob)
        self.assertIn("everwinter", scan.open_world_ids)
        self.assertIn("burning_sands", scan.open_world_ids)
        self.assertIn("fire_peaks", scan.open_world_ids)
        self.assertNotIn("storm_islands", scan.open_world_ids)
        self.assertIn("storm_islands", scan.missing_world_ids)
        text = unopened_report_text(scan)
        self.assertIn(STORM_UNOPENED_LINE, text)
        self.assertIn(CONTINUE_ONE_WORLD_LINE, text)
        self.assertIn("Storm Islands", text)

    def test_target_titles(self) -> None:
        self.assertTrue(match_target_kind("Варварская башня", "barbarian_tower"))
        self.assertTrue(match_target_kind("Башня", "barbarian_tower"))
        self.assertTrue(match_target_kind("Башня в пустыне", "desert_tower"))
        self.assertTrue(match_target_kind("Башня культистов", "cultist_tower"))
        self.assertTrue(match_target_kind("Форт ураганов", "storm_fort"))
        self.assertFalse(match_target_kind("Замок TestBoot", "barbarian_tower"))
        self.assertTrue(looks_like_wrong_world_target("SaMoK PasGONHUNKOB"))
        self.assertTrue(looks_like_wrong_world_target("0630p Обзор"))
        self.assertFalse(looks_like_wrong_world_target("Варварская башня"))

    def test_castle_flag_cluster_vs_spread_tower(self) -> None:
        from e4kbot.worlds import world_hunt_hits_are_cluster

        self.assertTrue(
            world_hunt_hits_are_cluster(
                [(0.58, 0.60), (0.59, 0.61), (0.57, 0.60)],
                [(583, 741), (581, 745), (575, 746)],
            )
        )
        self.assertFalse(
            world_hunt_hits_are_cluster(
                [(0.577, 0.349), (0.302, 0.195)],
                [(583, 741), (583, 742)],
            )
        )

    def test_fill_policy_first_skip_fifth_wait(self) -> None:
        self.assertEqual(world_fill_decision(0, 5, 0, first_must_skip=True), "world_skip_empty")
        self.assertEqual(world_fill_decision(0, 5, 0, first_must_skip=False), "wait_return")
        self.assertEqual(world_fill_decision(4, 5, 2, first_must_skip=True), "wait_return")
        self.assertEqual(world_fill_decision(1, 5, 1, first_must_skip=True), "wait_return")
        self.assertEqual(
            world_fill_decision(4, 5, 2, first_must_skip=True, wait_last=False),
            "world_skip_empty",
        )

    def test_storm_does_not_wait_forever_on_first(self) -> None:
        self.assertEqual(world_fill_decision(0, 5, 0, first_must_skip=False), "wait_return")

    def test_report_written_to_disk(self) -> None:
        scan = scan_from_blob("beyhoxonog necku orhehh")
        payload = write_world_attack_report(scan)
        self.assertTrue((ROOT / "data" / "attack_report.json").exists())
        self.assertIn("storm_islands", payload["missing_world_ids"])
        self.assertIn(STORM_UNOPENED_LINE, payload["text"])
        self.assertIn("Storm Islands", payload["text"])


class WorldScreenshotTests(unittest.TestCase):
    def test_nav_button_is_bottom_left_not_ruby(self) -> None:
        if not NAV_SHOT.exists():
            self.skipTest("nav_button.png missing")
        image = Image.open(NAV_SHOT)
        point = find_navigation_button(image)
        self.assertIsNotNone(point)
        assert point is not None
        self.assertLess(point[0], 0.28)
        self.assertGreater(point[1], 0.78)
        self.assertLess(point[0], 0.78)

    def test_world_list_left_sextants_and_ocr(self) -> None:
        shot = LIST_SHOT if LIST_SHOT.exists() else ROWS_SHOT
        if not shot.exists():
            self.skipTest("world list screenshot missing")
        image = Image.open(shot)
        self.assertTrue(is_world_list_open(image))
        sextants = find_world_list_sextants(image)
        self.assertGreaterEqual(len(sextants), 4)
        for nx, _ny in sextants:
            self.assertLess(nx, 0.40)
        rows = parse_navigation_rows(image)
        world_ids = {row.get("world_id") for row in rows}
        self.assertTrue({"everwinter", "burning_sands", "fire_peaks"} & world_ids)
        self.assertNotIn("storm_islands", world_ids)

    def test_choose_place_list_detects_great_empire_row(self) -> None:
        shot = ROOT / "assets" / "choose_place_list.png"
        if not shot.exists():
            self.skipTest("choose_place_list.png missing")
        image = Image.open(shot)
        self.assertTrue(is_world_list_open(image))
        rows = parse_navigation_rows(image)
        world_ids = {row.get("world_id") for row in rows}
        self.assertIn("great_empire", world_ids)
        for row in rows:
            if row.get("sextant"):
                self.assertLess(row["sextant"][0], 0.40)

    def test_nav_submenu_select_place_is_middle(self) -> None:
        shot = ROOT / "assets" / "nav_submenu.png"
        if not shot.exists():
            self.skipTest("nav_submenu.png missing")
        from e4kbot.vision import find_select_place_button, is_navigation_submenu

        image = Image.open(shot)
        self.assertTrue(is_navigation_submenu(image))
        point = find_select_place_button(image)
        self.assertIsNotNone(point)
        assert point is not None
        self.assertLess(point[0], 0.40)
        self.assertGreater(point[1], 0.68)
        self.assertLess(point[1], 0.81)

    def test_choose_place_icon_is_middle_not_karta(self) -> None:
        shot = ROOT / "assets" / "nav_choose_place.png"
        if not shot.exists():
            self.skipTest("nav_choose_place.png missing")
        from e4kbot.vision import find_select_place_button, is_select_place_point

        image = Image.open(ROOT / "assets" / "nav_submenu.png")
        point = find_select_place_button(image)
        self.assertIsNotNone(point)
        assert point is not None
        self.assertTrue(is_select_place_point(*point))
        self.assertGreater(point[1], 0.70)
        self.assertLess(point[1], 0.79)

    def test_live_choose_place_list_is_exhausted_one_row(self) -> None:
        shot = ROOT / "assets" / "choose_place_list.png"
        if not shot.exists():
            self.skipTest("choose_place_list.png missing")
        from e4kbot.vision import is_place_list_exhausted

        image = Image.open(shot)
        self.assertTrue(is_place_list_exhausted(image))

    def test_live_modules_are_not_stubs(self) -> None:
        for mode_id in ("barbarian_towers", "desert_towers", "cultist_towers", "storm_forts"):
            self.assertEqual(MODE_BY_ID[mode_id].status, "live")
            module = get_attack_module(mode_id)
            self.assertFalse(getattr(module, "is_stub", True))


if __name__ == "__main__":
    unittest.main()
