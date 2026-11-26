# -*- coding: utf-8 -*-
"""v3.5 应用层单测：仅返回任务不要求发射场/火箭/上升段。"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from app.core import config_build as CB  # noqa: E402
from app.core.model import MissionInput  # noqa: E402


class TestReturnOnlyV35(unittest.TestCase):
    def _inp(self):
        return MissionInput(
            name="返回任务测试", return_only=True, return_enabled=True,
            t0_text="2026-11-26 21:20", station_enabled=True,
            return_landing_name="四子王旗着陆场",
            return_landing_lat=42.27, return_landing_lon=111.70,
            return_landing_radius_km=50.0,
            return_search_start="2026-11-26T19:00:00Z",
            return_search_end="2026-11-26T21:00:00Z")

    def test_no_launch_site_or_vehicle(self):
        r = CB.build_config(self._inp(), write=False)
        cfg = r.config
        self.assertTrue(cfg.get("return_only"))
        self.assertEqual((cfg.get("mission") or {}).get("profile"), "return")
        self.assertNotIn("launch_site", cfg)
        self.assertNotIn("vehicle", cfg)
        self.assertNotIn("guidance", cfg)
        self.assertFalse((cfg.get("_v3") or {}).get("modules", {}).get("export"))
        self.assertTrue((cfg.get("_v3") or {}).get("modules", {}).get("return"))

    def test_roundtrip_return_only(self):
        r = CB.build_config(self._inp(), write=False)
        inp = CB.mission_input_from_config(r.config)
        self.assertTrue(inp.return_only)
        self.assertTrue(inp.return_enabled)
        self.assertAlmostEqual(float(inp.return_landing_lat), 42.27, places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
