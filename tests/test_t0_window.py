# -*- coding: utf-8 -*-
"""v4.1 交会发射窗口 / T0 反推 的回归测试。

* 单元：``t0_mode`` 写入 config 与读回；非载人任务不启用；
* 集成：用 `作品/示例_3_载人交会_CZ2F酒泉` 的任务输入（t0_mode=rendezvous）跑一遍，
  断言 T0 被交会窗口反推、报告 §4.4 与摘要里的对接点坐标存在。

集成测试需要内置空间站 OEM（app/data/station）覆盖任务时刻；缺失时自动跳过。
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.core import config_build as CB  # noqa: E402
from app.core.model import MissionInput  # noqa: E402

EXAMPLE_GLOB = os.path.join(ROOT, "作品", "示例_3_载人交会_CZ2F酒泉")


def _find_example() -> str:
    if os.path.isdir(EXAMPLE_GLOB):
        return EXAMPLE_GLOB
    hits = glob.glob(os.path.join(ROOT, "作品", "*载人交会*"))
    return hits[0] if hits else ""


class TestT0ModeConfig(unittest.TestCase):
    def test_rendezvous_mode_recorded_and_roundtrip(self):
        inp = MissionInput(
            name="T0 反推单测", rocket_label="长征二号F", site_label="酒泉 921工位",
            t0_text="2026-11-25T00:18:00Z", mission_type="crewed",
            t0_mode="rendezvous")
        cfg = CB.build_config(inp, write=False).config
        self.assertEqual(cfg["_v3"]["t0_mode"], "rendezvous")
        self.assertTrue(cfg["_v3"]["t0_user_given"])
        back = CB.mission_input_from_config(cfg)
        self.assertEqual(back.t0_mode, "rendezvous")

    def test_auto_mode_default(self):
        cfg = CB.build_config(MissionInput(rocket_label="长征二号丁",
                                           site_label="太原 LC-9",
                                           t0_text="2026-11-19 06:50"),
                              write=False).config
        self.assertEqual(cfg["_v3"]["t0_mode"], "auto")
        self.assertTrue(cfg["_v3"]["t0_user_given"])

    def test_rendezvous_allows_missing_t0_for_crewed(self):
        inp = MissionInput(name="无 T0 的载人任务", rocket_label="长征二号F",
                           site_label="酒泉 921工位", mission_type="crewed",
                           t0_text="", t0_mode="rendezvous")
        br = CB.build_config(inp, write=False)
        self.assertTrue(br.config["mission"]["t0_utc"],
                        "rendezvous 模式下应允许无 T0（用当前时刻作搜索中心）")
        self.assertTrue(any("搜索中心" in w for w in br.warnings))


class TestT0WindowIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.example = _find_example()
        if not cls.example:
            raise unittest.SkipTest("找不到载人示例工作区")
        mission = os.path.join(cls.example, "任务输入.json")
        if not os.path.isfile(mission):
            raise unittest.SkipTest("示例缺少 任务输入.json")
        station_dir = os.path.join(ROOT, "app", "data", "station")
        if not os.path.isdir(station_dir):
            raise unittest.SkipTest("缺少内置空间站数据目录")

    def test_t0_backsolved_from_rendezvous_window(self):
        tmp = tempfile.mkdtemp(prefix="rk_t0win_")
        try:
            mission = os.path.join(self.example, "任务输入.json")
            with open(mission, encoding="utf-8") as f:
                raw = json.load(f)
            raw["t0_mode"] = "rendezvous"
            mpath = os.path.join(tmp, "mission.json")
            with open(mpath, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False)
            cmd = [sys.executable, "-m", "app.main", "--cli", "--mission", mpath,
                   "--workdir", os.path.join(tmp, "work"),
                   "--modules", "export,station,window,docking,summary,report,qc"]
            env = dict(os.environ, PYTHONUTF8="1")
            proc = subprocess.run(cmd, cwd=ROOT, env=env, timeout=420,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            out = proc.stdout.decode("utf-8", "replace")
            self.assertEqual(proc.returncode, 0,
                             "端到端失败，尾部日志：\n%s" % out[-2500:])
            work = os.path.join(tmp, "work")
            with open(os.path.join(work, "config.json"), encoding="utf-8-sig") as f:
                cfg = json.load(f)
            win = cfg["mission"].get("t0_window") or {}
            # v4.3：示例给定 T0（00:18:00）落在 dogleg 可达窗口内 → 保留，
            # 只按目标平面飞（不再强行平移）。
            self.assertEqual(cfg["mission"]["t0_utc"], "2026-11-25T00:18:00Z")
            self.assertFalse(win.get("applied"))
            self.assertAlmostEqual(float(win.get("shift_s") or 0.0), 0.0, delta=5.0,
                                   msg="T0 不应被平移：%s" % win.get("shift_s"))
            self.assertTrue(os.path.isfile(
                os.path.join(work, "report", "交会发射窗口.json")))
            with open(os.path.join(work, "report", "交会发射窗口.json"),
                      encoding="utf-8-sig") as f:
                tw = json.load(f)
            self.assertTrue(tw.get("enabled") and tw.get("available"))
            self.assertGreaterEqual(int(tw.get("feasible_n") or 0), 1)
            # 报告 §4.4
            with open(os.path.join(work, "report", "弹道仿真报告.md"),
                      encoding="utf-8") as f:
                md = f.read()
            self.assertIn("### 4.4 交会发射窗口与 T0 反推", md)
            self.assertIn("是否已写回配置", md)
            self.assertIn("入轨面残差", md)
            self.assertIn("几何窗口", md)
            self.assertIn("全流程轨迹", md)
            # 摘要里的对接点坐标
            with open(os.path.join(work, "report", "任务摘要.md"),
                      encoding="utf-8") as f:
                summ = f.read()
            self.assertIn("对接点", summ)
            # 交会 JSON 的对接位置
            with open(os.path.join(work, "sim", "交会对接.json"),
                      encoding="utf-8-sig") as f:
                rdv = json.load(f)
            dk = rdv.get("docking") or {}
            self.assertIsNotNone(dk.get("station_lat_deg"))
            self.assertIsNotNone(dk.get("station_lon_deg"))
            self.assertIsNotNone(dk.get("alt_km"))
            self.assertIn("dogleg 可达窗口", str(rdv.get("t0_reason")))
            # v4.2：实测入轨面残差应 < 0.05°，且交会方案在预算内可行
            plane = rdv.get("plane") or {}
            self.assertIsNotNone(plane.get("residual_deg"))
            self.assertLess(float(plane["residual_deg"]), 0.05,
                            "入轨面残差过大：%s" % plane)
            self.assertTrue(rdv.get("feasible"), "交会方案应可行：%s"
                            % (rdv.get("plan") or {}))
            self.assertLessEqual(float((rdv.get("plan") or {}).get("total_dv_mps") or 1e9),
                                 300.0)
            self.assertEqual(int((rdv.get("plan") or {}).get("phasing_orbits") or 0), 4)
            # v4.2：全流程轨迹 CSV（相位/转移/最终逼近）
            tpath = os.path.join(work, "sim", "交会轨迹.csv")
            self.assertTrue(os.path.isfile(tpath), "缺少 sim/交会轨迹.csv")
            with open(tpath, encoding="utf-8-sig") as f:
                content = f.read()
            self.assertGreater(len(content.splitlines()), 100)
            for _phase in ("相位段", "转移段", "最终逼近"):
                self.assertIn(_phase, content)
            # KMZ 里应有全流程轨迹/动画
            kmz = os.path.join(work, "viz", "弹道三维_含残骸坠落.kmz")
            if os.path.isfile(kmz):
                import zipfile
                with zipfile.ZipFile(kmz) as z:
                    name = [n for n in z.namelist() if n.endswith(".kml")][0]
                    kml = z.read(name).decode("utf-8")
                self.assertIn("全流程轨迹", kml)
                self.assertIn("全流程时间动画", kml)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
