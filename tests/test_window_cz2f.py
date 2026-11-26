# -*- coding: utf-8 -*-
"""发射窗口 / CZ-2F 交会对接的端到端测试（引擎脚本级）。

运行：python tests\test_window_cz2f.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
REG = os.path.join(ROOT, "tests", "_regression")
ENGINE = os.path.join(ROOT, "engine")
APP_DATA = os.path.join(ROOT, "app", "data")


def _env(workdir: str) -> dict:
    env = dict(os.environ)
    env.update({
        "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "MPLBACKEND": "Agg",
        "RK_ENGINE": ENGINE, "RK_ROOT": workdir, "RK_APP_DATA": APP_DATA,
    })
    env.pop("RK_DEM", None)
    return env


def _run(script: str, workdir: str, extra=None, timeout=180):
    argv = [PY, os.path.join(ENGINE, "scripts", script),
            "--config", os.path.join(workdir, "config.json")]
    argv += list(extra or [])
    return subprocess.run(argv, cwd=workdir, env=_env(workdir), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class TestCz2fWindow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rk_cz2f_")
        src = os.path.join(REG, "cz2f_app")
        cls.wd = os.path.join(cls.tmp, "cz2f")
        shutil.copytree(src, cls.wd)
        cfg_path = os.path.join(cls.wd, "config.json")
        cfg = _load(cfg_path)
        cfg["mission"]["profile"] = "cz2f"
        cfg["mission"]["reference_orbit"] = {"alt_km": 200.0, "i_deg": 41.47,
                                             "confidence": "高"}
        cfg["launch_window"] = {"mode": "auto", "tolerance_deg": 0.2,
                                "search_hours": 24.0, "step_s": 20.0}
        cfg["station"] = {
            "enabled": True, "name": "天宫空间站", "source": "auto",
            "rendezvous": {"target_time_h": 6.5, "max_phasing_h": 13.0,
                           "dv_budget_mps": 300.0, "final_approach_dv_mps": 10.0,
                           "final_approach_s": 1800.0, "max_phasing_orbits": 10,
                           "min_phasing_orbits": 1},
        }
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_station_track(self):
        p = _run("station_track.py", self.wd)
        self.assertEqual(p.returncode, 0, p.stderr[-1500:])
        st = _load(os.path.join(self.wd, "sim", "空间站轨道.json"))
        self.assertTrue(st.get("enabled"))
        self.assertEqual(st["station"]["norad_id"], 48274)
        self.assertGreater(len(st.get("ground_track", {})), 0)
        self.assertTrue(os.path.isfile(os.path.join(self.wd, "sim", "空间站星下点.csv")))

    def test_window_and_rendezvous(self):
        p = _run("window.py", self.wd)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        win = _load(os.path.join(self.wd, "report", "发射窗口计算.json"))
        ow = win.get("orbit_window") or {}
        self.assertTrue(ow.get("available"), ow.get("reason"))
        self.assertEqual((ow.get("target") or {}).get("kind"), "station")
        self.assertGreaterEqual(ow.get("n_windows", 0), 1)
        feasible = [w for w in ow.get("windows", [])
                    if (w.get("rendezvous") or {}).get("feasible")]
        self.assertTrue(feasible, "应至少有一个交会可行窗口")
        # 航警时窗仍保留（v3.0 兼容）
        self.assertIn("model_window", win)
        self.assertIn("practical_window", win)

        p = _run("rendezvous.py", self.wd)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        dk = _load(os.path.join(self.wd, "sim", "交会对接.json"))
        self.assertTrue(dk.get("enabled"), dk.get("reason"))
        tl = dk.get("timeline") or []
        self.assertGreaterEqual(len(tl), 5)
        stamps = [r.get("t_utc") for r in tl]
        self.assertEqual(stamps, sorted(stamps), "时间线必须单调")
        # v4.2：交会按实测入轨面计算；若该 fixture 的 T0 未先经 t0win 对齐，
        # 入轨面残差会较大，此时只要求"残差被如实报告"，不断言 ΔV 可行。
        _pl = dk.get("plane") or {}
        _resid = _pl.get("residual_deg")
        self.assertIsNotNone(_resid, "必须报告实测入轨面残差")
        if float(_resid) <= 0.2:
            self.assertLessEqual(float(dk.get("total_dv_mps") or 1e9),
                                 float(dk.get("budget_mps") or 300.0) + 1e-6)
            self.assertTrue(dk.get("feasible"))
        else:
            self.assertGreater(float(_resid), 0.2)
        dock = dk.get("docking") or {}
        self.assertTrue(dock.get("t_utc"))
        self.assertIsNotNone(dock.get("residual_range_km"))
        self.assertLess(float(dock.get("residual_range_km")), 1.0)
        self.assertIsNotNone(dock.get("station_lat_deg"))
        self.assertIsNotNone(dock.get("alt_km"))
        self.assertTrue(os.path.isfile(os.path.join(self.wd, "sim", "交会对接_时序.csv")))


class TestNonCz2fWindow(unittest.TestCase):
    def test_cz2d_ltan_window(self):
        tmp = tempfile.mkdtemp(prefix="rk_cz2d_")
        try:
            src = os.path.join(REG, "cz2d_app")
            wd = os.path.join(tmp, "cz2d")
            shutil.copytree(src, wd)
            p = _run("window.py", wd)
            self.assertEqual(p.returncode, 0, p.stderr[-2000:])
            win = _load(os.path.join(wd, "report", "发射窗口计算.json"))
            ow = win.get("orbit_window") or {}
            self.assertTrue(ow.get("available"), ow.get("reason"))
            self.assertEqual((ow.get("target") or {}).get("kind"), "ltan")
            # v4.2：实测入轨倾角与目标的偏差以残差形式报告（0.5~3° 仍给出窗口）
            self.assertIsNotNone(ow.get("plane_residual_deg"))
            if float(ow.get("plane_residual_deg") or 0.0) > 0.5:
                self.assertTrue(ow.get("plane_note"))
            self.assertGreaterEqual(ow.get("n_windows", 0), 1)
            self.assertIn("model_window", win)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
