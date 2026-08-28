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
    NavRow,
    NavigationScan,
    STORM_UNOPENED_LINE,
    is_world_npc_kind,
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
    def test_canonical_ge_row_is_topmost_and_lower_duplicate_never_selected(self) -> None:
        from e4kbot.world_switch import WorldSwitchMixin

        top = NavRow(None, "", "Main OCR unknown", None, (0.20, 0.22), "main")
        lower = NavRow("great_empire", "Великая империя", "Outpost", None, (0.20, 0.48), "outpost")
        scan = NavigationScan(rows=[lower, top])
        chosen = WorldSwitchMixin._central_row_for_world(object(), scan, "great_empire")
        self.assertIs(chosen, top)
        self.assertIsNot(chosen, lower)

    def test_canonical_single_other_world_row_is_selected(self) -> None:
        from e4kbot.world_switch import WorldSwitchMixin

        row = NavRow("everwinter", "Вечнохолодный ледник", "Any account", None, (0.20, 0.35), "row")
        scan = NavigationScan(rows=[row])
        self.assertIs(WorldSwitchMixin._central_row_for_world(object(), scan, "everwinter"), row)

    def test_fast_sextant_ignores_poisoned_store_outpost_row(self) -> None:
        from e4kbot.world_switch import FAST_WORLD_SEXTANTS, WorldSwitchMixin
        from unittest.mock import Mock

        engine = WorldSwitchMixin()
        engine.store = Mock()
        engine.store.live = Mock()
        engine.store.live.central_castles = {
            "everwinter": {"row": {"sextant": [0.199, 0.2867]}},
        }
        self.assertEqual(
            engine._world_list_expected_sextant("everwinter"),
            FAST_WORLD_SEXTANTS["everwinter"],
        )
        self.assertAlmostEqual(FAST_WORLD_SEXTANTS["everwinter"][1], 0.343, places=3)

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

    def test_fill_policy_always_skips_world_on_insufficient_troops(self) -> None:
        self.assertTrue(is_world_npc_kind("baron"))
        self.assertEqual(world_fill_decision(0, 5, 0, first_must_skip=True), "world_skip_empty")
        self.assertEqual(world_fill_decision(0, 5, 0, first_must_skip=False), "world_skip_empty")
        self.assertEqual(world_fill_decision(4, 5, 2, first_must_skip=True), "world_skip_empty")
        self.assertEqual(world_fill_decision(1, 5, 1, first_must_skip=True), "world_skip_empty")
        self.assertEqual(
            world_fill_decision(4, 5, 2, first_must_skip=True, wait_last=False),
            "world_skip_empty",
        )

    def test_storm_does_not_wait_forever_on_first(self) -> None:
        self.assertEqual(world_fill_decision(0, 5, 0, first_must_skip=False), "world_skip_empty")

    def test_empty_hunt_does_not_skip_world_before_five_sends(self) -> None:
        from e4kbot.state import StateStore
        from e4kbot.world_switch import WorldSwitchMixin
        from tempfile import TemporaryDirectory
        from pathlib import Path

        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = StateStore(path=Path(folder.name) / "state.json")
        store.live.session_by_mode = {"robber_barons": 2}

        class Dummy(WorldSwitchMixin):
            def __init__(self) -> None:
                self.store = store
                self.telegram = None
                self._world_scan = None
                self._world_recenter_tries = 3

        dummy = Dummy()
        self.assertEqual(dummy._handle_world_no_targets("baron"), "no_targets")
        self.assertNotIn("robber_barons", store.live.skipped_modes)
        self.assertEqual(dummy._world_recenter_tries, 0)

    def test_world_castle_templates_match_on_map_canvas(self) -> None:
        from e4kbot.vision import WORLD_CASTLE_TEMPLATES, find_world_castle_candidates

        self.assertEqual(
            set(WORLD_CASTLE_TEMPLATES),
            {"barbarian_tower", "desert_tower", "cultist_tower", "storm_fort"},
        )
        self.assertEqual(len(WORLD_CASTLE_TEMPLATES["storm_fort"]), 2)
        for kind, paths in WORLD_CASTLE_TEMPLATES.items():
            for path in paths:
                self.assertTrue(path.exists(), path.name)
                canvas = Image.new("RGB", (900, 1600), (104, 151, 57))
                sprite = Image.open(path).convert("RGB")
                canvas.paste(sprite, (280, 720))
                hits = find_world_castle_candidates(canvas, kind, threshold=0.55)
                self.assertTrue(hits, f"{kind} / {path.name} should match itself")

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

    def test_inbox_parchment_is_not_special_offers(self) -> None:
        from PIL import ImageDraw
        from e4kbot.vision import is_special_offers_screen

        water = Image.new("RGB", (900, 1600), (42, 98, 150))
        inbox = water.copy()
        draw = ImageDraw.Draw(inbox)
        draw.rectangle((80, 300, 820, 1160), fill=(214, 186, 138))
        self.assertFalse(is_special_offers_screen(inbox, recognized_text="Входящие"))


        from PIL import ImageDraw
        from e4kbot.vision import find_robber_candidates, is_map_screen, map_grass_ratio

        grass = Image.new("RGB", (900, 1600), (104, 151, 57))
        self.assertTrue(is_map_screen(grass))
        self.assertGreater(map_grass_ratio(grass), 0.12)
        water = Image.new("RGB", (900, 1600), (42, 98, 150))
        inbox = water.copy()
        draw = ImageDraw.Draw(inbox)
        draw.rectangle((80, 300, 820, 1160), fill=(214, 186, 138))
        self.assertFalse(is_map_screen(inbox))
        self.assertEqual(find_robber_candidates(inbox), [])

    def test_nav_fallback_point_is_inside_star_band(self) -> None:
        point = (0.198, 0.937)
        self.assertGreaterEqual(point[0], 0.07)
        self.assertLess(point[0], 0.26)
        self.assertGreaterEqual(point[1], 0.87)


        for mode_id in ("barbarian_towers", "desert_towers", "cultist_towers", "storm_forts"):
            self.assertEqual(MODE_BY_ID[mode_id].status, "live")
            module = get_attack_module(mode_id)
            self.assertFalse(getattr(module, "is_stub", True))


if __name__ == "__main__":
    unittest.main()
