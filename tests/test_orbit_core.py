# -*- coding: utf-8 -*-
"""轨道内核单测：TLE/OEM、坐标变换、根数、Lambert、相位方案。

运行：python tests\test_orbit_core.py
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "engine"))
os.environ.setdefault("MPLBACKEND", "Agg")

import numpy as np

from common import orbit as O

TLE = ("1 48274U 21035A   26323.81256295  .00021071  00000+0  24899-3 0  9995",
       "2 48274  41.4687  96.2271 0004931 264.7589  95.2687 15.60787252317474")
CSS_OEM = os.path.join(ROOT, "app", "data", "station",
                       "CSS_OEM_20261120003641_0001.dat")
T0 = O.parse_iso("2026-11-25T00:18:00Z")


class TestOrbitCore(unittest.TestCase):
    def test_tle_parse_and_state(self):
        recs = O.parse_tle("\n".join(TLE))
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec["norad_id"], 48274)
        self.assertAlmostEqual(rec["inc_deg"], 41.4687, places=4)
        self.assertTrue(rec["epoch_dt"].year == 2026)
        if not O.HAVE_SGP4:
            self.skipTest("sgp4 未安装")
        sat = O.TleSatellite(*TLE)
        r, v = sat.state(T0)
        self.assertAlmostEqual(float(np.linalg.norm(r)), 6760.0, delta=80.0)
        self.assertAlmostEqual(float(np.linalg.norm(v)), 7.68, delta=0.2)
        el = O.elements_from_state(r, v)
        self.assertTrue(41.0 < el["i_deg"] < 42.0)
        self.assertGreater(el["period_s"], 5000)

    def test_elements_roundtrip(self):
        a, e, i, raan, argp, nu = 6578.0, 0.001, 41.47, 64.7, 45.0, 90.0
        r, v = O.state_from_elements(a, e, i, raan, argp, nu)
        el = O.elements_from_state(r, v)
        self.assertAlmostEqual(el["a_km"], a, places=6)
        self.assertAlmostEqual(el["e"], e, places=9)
        self.assertAlmostEqual(el["i_deg"], i, places=7)
        self.assertAlmostEqual(el["raan_deg"], raan, places=6)
        self.assertAlmostEqual(el["u_deg"], (argp + nu) % 360.0, places=6)

    def test_ltan_raan_roundtrip(self):
        t = O.parse_iso("2026-11-25T00:18:00Z")
        for ltan in (0.0, 6.5, 13.5, 23.9):
            raan = O.raan_for_ltan(t, ltan)
            got = O.ltan_from_raan(t, raan)
            d = abs((got - ltan + 12.0) % 24.0 - 12.0)
            self.assertLess(d, 1e-9)

    def test_oem_parse_interp(self):
        if not os.path.isfile(CSS_OEM):
            self.skipTest("内置 CSS OEM 不存在")
        oem = O.OemEphemeris(CSS_OEM)
        info = oem.info()
        self.assertEqual(info["frame"], "EME2000")
        self.assertEqual(info["n_points"], 2521)
        self.assertLess(info["step_s"], 300)
        r, v = oem.state(T0)
        self.assertAlmostEqual(float(np.linalg.norm(r)), 6765.0, delta=40.0)
        el = oem.elements(T0)
        self.assertTrue(41.2 < el["i_deg"] < 41.7)
        # 区间外必须抛错（上层据此回退 TLE）
        with self.assertRaises(ValueError):
            oem.state(oem.t1 + timedelta(hours=1))

    def test_frame_conversion_astropy(self):
        try:
            import warnings
            warnings.filterwarnings("ignore")
            from astropy.utils import iers
            iers.conf.auto_download = False
            iers.conf.auto_max_age = None
            from astropy.time import Time
            from astropy.coordinates import TEME, GCRS, CartesianRepresentation
            import astropy.units as u
        except Exception:  # noqa: BLE001
            self.skipTest("astropy 不可用")
        t = O.parse_iso("2026-11-25T00:18:00Z")
        at = Time(t.strftime("%Y-%m-%dT%H:%M:%S"), scale="utc")
        r = np.array([-5622.3355, 469.4826, 3735.1961])
        teme = TEME(CartesianRepresentation(r[0] * u.km, r[1] * u.km, r[2] * u.km),
                    obstime=at)
        want = teme.transform_to(GCRS(obstime=at)).cartesian.xyz.value
        got = O.vector_teme_to_gcrs(r, t)
        self.assertLess(float(np.linalg.norm(want - got)), 0.2)  # < 200 m

    def test_azimuth_for_inclination(self):
        dt = T0 + timedelta(seconds=584)
        az = O.azimuth_for_inclination(34.937, 204.66, 7.4203, 41.47, dt,
                                       near_az_deg=114.6)
        self.assertIsNotNone(az)
        el = O.plane_from_launch(34.937, 122.695, 204.66, az, 7.4203, dt)
        self.assertAlmostEqual(el["i_deg"], 41.47, places=4)
        # 太阳同步任务（西南射向，>180°）也要能解
        az2 = O.azimuth_for_inclination(38.9, 500.0, 7.6, 97.45, dt, near_az_deg=190.0)
        self.assertIsNotNone(az2)
        el2 = O.plane_from_launch(38.9, 0.0, 500.0, az2, 7.6, dt)
        self.assertAlmostEqual(el2["i_deg"], 97.45, places=4)

    def test_hohmann_and_lambert(self):
        dv1, dv2, tof = O.hohmann_dv(6578.0, 6758.0)
        self.assertAlmostEqual(dv1 * 1000, 52.4, delta=1.0)
        self.assertAlmostEqual(dv2 * 1000, 52.0, delta=1.0)
        a = np.radians(179.9)
        r1 = np.array([6578.0, 0, 0])
        r2 = 6758.0 * np.array([np.cos(a), np.sin(a), 0])
        res = O.lambert(r1, r2, tof)
        self.assertIsNotNone(res)
        v1, v2 = res
        rr, _vv = O.propagate_rk4(r1, v1, tof, step_s=5, j2=False)
        self.assertLess(float(np.linalg.norm(rr - r2)), 0.05)
        self.assertAlmostEqual(float(np.linalg.norm(v1)), 7.837, delta=0.01)
        self.assertAlmostEqual(float(np.linalg.norm(v2)), 7.628, delta=0.01)

    def test_phasing_plan(self):
        r_sc, r_st = 6578.0, 6758.0
        p = O.phasing_plan(r_sc, r_st, 60.0, max_orbits=8, min_orbits=1,
                           preferred_s=6.5 * 3600, max_dv_mps=400,
                           max_total_s=13 * 3600, v_sc_kms=7.78)
        self.assertIsNotNone(p)
        self.assertGreaterEqual(p["k"], 0)
        self.assertTrue(0 < p["dv_total_mps"] < 400)
        self.assertGreater(p["total_s"], 0)
        cw = O.cw_rendezvous(
            np.array([6765.0, 14.0, 0.0]), np.array([0.0, 7.68, 0.0]),
            np.array([6765.0, 0.0, 0.0]), np.array([0.0, 7.68, 0.0]), 1800.0)
        self.assertIsNotNone(cw)
        self.assertTrue(5.0 < cw["dv1_mps"] < 15.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
