# -*- coding: utf-8 -*-
"""v3.4 应用层单测：任务类型识别 / station/launch_window 配置 / 离线高程导入。

运行：python tests\test_app_v34.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("MPLBACKEND", "Agg")
os.environ.setdefault("PYTHONUTF8", "1")

import numpy as np

from app.core import paths as P
from app.core.config_build import build_config
from app.core.model import MissionInput, ObserverInput, NotamInput


def _base_input(**kw):
    d = dict(
        name="单测任务",
        rocket_label="长征二号F",
        site_label="酒泉卫星发射中心",
        t0_text="2026-11-25T00:18:00Z",
        payload_name="神舟载人飞船",
        payload_kg=8100.0,
        observers=[ObserverInput(id="obs1", lat_deg=37.761478, lon_deg=112.725208,
                                 alt_m=800.0)],
        modules={"dem": False, "invert": False, "export": True, "window": True,
                 "station": True, "docking": True, "sky": False, "appear": False,
                 "map": False, "mc": False, "kml": False, "qc": False, "report": False},
    )
    d.update(kw)
    return MissionInput(**d)


class TestConfigBuildV34(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        P.setup_env()
        P.ensure_syspath()

    def test_cz2f_profile(self):
        br = build_config(_base_input(), write=False)
        cfg = br.config
        self.assertEqual(cfg["mission"]["profile"], "cz2f")
        self.assertTrue(cfg["station"]["enabled"])
        self.assertTrue(cfg["_v3"]["modules"]["station"])
        self.assertTrue(cfg["_v3"]["modules"]["docking"])
        self.assertIn("launch_window", cfg)
        self.assertEqual(cfg["launch_window"]["tolerance_deg"], 0.2)
        self.assertIn("reference_orbit", cfg["mission"])

    def test_standard_profile(self):
        br = build_config(_base_input(rocket_label="长征二号丁",
                                      payload_name="遥感卫星",
                                      site_label="太原 LC-9"), write=False)
        cfg = br.config
        self.assertEqual(cfg["mission"]["profile"], "standard")
        self.assertFalse(cfg["station"]["enabled"])
        self.assertFalse(cfg["_v3"]["modules"]["station"])
        self.assertFalse(cfg["_v3"]["modules"]["docking"])

    def test_explicit_tle_and_window(self):
        tle = ("1 48274U 21035A   26323.81256295  .00021071  00000+0  24899-3 0  9995\n"
               "2 48274  41.4687  96.2271 0004931 264.7589  95.2687 15.60787252317474")
        br = build_config(_base_input(station_tle_text=tle, window_ltan_h=13.5,
                                      window_tolerance_deg=0.3,
                                      window_search_hours=24.0), write=False)
        cfg = br.config
        self.assertEqual(len(cfg["station"]["tle"]), 2)
        self.assertAlmostEqual(cfg["launch_window"]["ltan_h"], 13.5)
        self.assertAlmostEqual(cfg["launch_window"]["tolerance_deg"], 0.3)
        self.assertAlmostEqual(cfg["launch_window"]["search_hours"], 24.0)


class TestDemImportV34(unittest.TestCase):
    def test_hgt_import(self):
        tmp = tempfile.mkdtemp(prefix="rk_dem_app_")
        try:
            hgt = os.path.join(tmp, "N37E112.hgt")
            arr = np.zeros((3601, 3601), dtype=">i2")
            arr[1800, 1800] = 1234
            arr.tofile(hgt)
            from app.core.dem_manager import DemManager
            dm = DemManager(tmp)
            info = dm.import_dem(hgt)
            self.assertEqual(info.get("ok"), 1)
            self.assertEqual(info.get("failed"), [])
            self.assertIn("N37E112", info.get("tiles") or [])
            st = dm.status()
            self.assertIn("N37E112", st.get("tiles") or [])
            # 引擎侧读取（自定义索引在临时 DEM 根下）
            os.environ["RK_DEM"] = tmp
            import importlib
            import common.dem as D
            importlib.reload(D)
            self.assertAlmostEqual(float(D.sample(37.5, 112.5)), 1234.0, delta=1.0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_geotiff_reference_import(self):
        shanxi = r"D:\BaiduNetdiskDownload\山西\山西WGS84.tif"
        if not os.path.isfile(shanxi):
            self.skipTest("山西 GeoTIFF 不存在")
        tmp = tempfile.mkdtemp(prefix="rk_dem_app_")
        try:
            from app.core.dem_manager import DemManager
            dm = DemManager(tmp)
            info = dm.import_dem(shanxi, copy=False)
            self.assertEqual(info.get("ok"), 1)
            srcs = dm.custom_sources()
            self.assertEqual(len(srcs), 1)
            self.assertTrue(srcs[0]["valid"])
            cfg = {"mission": {"launch_site": {"lat_deg": 37.7615, "lon_deg": 112.7252}},
                   "observers": [{"lat_deg": 37.7615, "lon_deg": 112.7252}],
                   "notams": []}
            plan = dm.custom_plan(cfg)
            self.assertTrue(plan["ready"])
            self.assertGreaterEqual(plan["coverage"]["total"]["ratio"], 0.99)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
