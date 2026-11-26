# -*- coding: utf-8 -*-
"""返回舱再入模型单测（v3.5）：大气密度、着陆速度、事件顺序、散布椭圆。"""
import math
import os
import sys
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "engine"))

from common import orbit as O  # noqa: E402
from common import reentry as R  # noqa: E402


class TestReentryV35(unittest.TestCase):
    def test_density_sea_level(self):
        self.assertAlmostEqual(R.density(0.0), 1.225, delta=0.03)

    def test_return_from_circular_orbit(self):
        # 380 km 圆轨道（ECI, km/km/s），反向制动 120 m/s
        r = (6378.137 + 380.0, 0.0, 0.0)
        v = (0.0, math.sqrt(O.MU_EARTH / r[0]), 0.0)
        t0 = datetime(2026, 11, 26, 0, 0, tzinfo=timezone.utc)
        res = R.simulate_return(r, v, t0, R.CapsuleParams(ld_ratio=0.2),
                                deorbit_dv_mps=120.0, record=False)
        self.assertTrue(res["ok"], res.get("reason"))
        L = res["landing"]
        self.assertGreaterEqual(L["lat_deg"], -90.0)
        self.assertLessEqual(L["lat_deg"], 90.0)
        self.assertGreater(L["flight_time_s"], 1500.0)
        self.assertLess(L["flight_time_s"], 6000.0)
        # 伞降后接地速度应是小量级（不是惯性速度 ~7 km/s）
        self.assertLess(L["speed_m_s"], 20.0)
        events = [e["event"] for e in res["events"]]
        self.assertIn("再入界面", events)
        self.assertIn("着陆", events)
        self.assertLess(events.index("再入界面"), events.index("着陆"))

    def test_dispersion_ellipse(self):
        pts = [(40.0 + 0.01 * i, 110.0 + 0.005 * (i % 3)) for i in range(9)]
        ell = R.dispersion_ellipse(pts)
        self.assertIsNotNone(ell)
        self.assertGreaterEqual(ell["semi_major_km"], ell["semi_minor_km"])
        self.assertGreater(ell["semi_major_km"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
