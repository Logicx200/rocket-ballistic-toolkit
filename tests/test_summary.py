# -*- coding: utf-8 -*-
"""v4 通用成果（summary）单测：关键节点 / 飞行阶段 / 轨迹统计 / 摘要 / 成果清单。

要点
----
* 三类任务都要能产出同一套"通用成果"；
* 动压必须用 ECEF 相对速度（不能用含地球自转的惯性速度）；
* 返回任务的关键节点要含"着陆"，阶段来自 返回舱阶段.csv；
* 成果清单要能做"应产出项"核对并列出缺失项。
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.core import summary as S  # noqa: E402


def _write_csv(path, fields, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class _Workdir(unittest.TestCase):
    def setUp(self):
        self.wd = tempfile.mkdtemp(prefix="rk_summary_")

    def tearDown(self):
        shutil.rmtree(self.wd, ignore_errors=True)

    def _launch(self):
        cfg = {
            "mission": {
                "name": "常规发射单测", "rocket": "长征二号丁", "serial": "遥1",
                "mission_type": "standard", "profile": "standard",
                "t0_utc": "2026-11-18T22:50:00Z",
                "t0_note": "用户给定", "disclaimer": "仅供测试",
                "launch_site": {"name": "太原 LC-9", "lat_deg": 38.86,
                                "lon_deg": 111.59, "alt_m_ellipsoid": 1459.0},
                "reference_orbit": {"alt_km": 505.0, "i_deg": 97.45},
                "_provenance": [{"field": "mission.t0_utc", "label": "T0",
                                 "value": "2026-11-18T22:50:00Z", "source": "user",
                                 "confidence": "高", "note": ""}],
            },
            "notams": [],
            "observers": [{"id": "obs1", "name": "太原", "lat_deg": 37.87,
                           "lon_deg": 112.55, "alt_m": 800.0}],
            "station": {"enabled": False},
            "return_capsule": {"enabled": False},
            "launch_sequence": {"custom": False, "values": {"seco_s": 723.8}},
            "_v3": {"modules": {"export": True, "sky": True, "map": True,
                                "summary": True, "report": True, "qc": True},
                    "warnings": ["示例提示"]},
        }
        _write_json(os.path.join(self.wd, "config.json"), cfg)
        # 轨迹：第 1 行故意给"惯性速度 400 m/s、ECEF 速度 0"——动压必须是 0
        _write_csv(
            os.path.join(self.wd, "sim", "trajectory.csv"),
            ["t_met_s", "t_utc", "lat_deg", "lon_deg", "alt_m", "vx_ecef", "vy_ecef",
             "vz_ecef", "speed_m_s", "mass_kg", "phase"],
            [
                {"t_met_s": 0.0, "t_utc": "2026-11-18T22:50:00Z", "lat_deg": 38.8632,
                 "lon_deg": 111.5896, "alt_m": 1459.0, "vx_ecef": 0, "vy_ecef": 0,
                 "vz_ecef": 0, "speed_m_s": 400.0, "mass_kg": 250660, "phase": "S1"},
                {"t_met_s": 60.0, "t_utc": "2026-11-18T22:51:00Z", "lat_deg": 38.5,
                 "lon_deg": 111.5, "alt_m": 12000.0, "vx_ecef": 300, "vy_ecef": 200,
                 "vz_ecef": 100, "speed_m_s": 800.0, "mass_kg": 200000, "phase": "S1"},
                {"t_met_s": 156.7, "t_utc": "2026-11-18T22:52:36Z", "lat_deg": 38.3,
                 "lon_deg": 111.49, "alt_m": 68900.0, "vx_ecef": 2000, "vy_ecef": 700,
                 "vz_ecef": 200, "speed_m_s": 2127.0, "mass_kg": 58660, "phase": "S2"},
                {"t_met_s": 723.8, "t_utc": "2026-11-18T23:02:03Z", "lat_deg": 11.5,
                 "lon_deg": 107.3, "alt_m": 506854.0, "vx_ecef": 7500, "vy_ecef": 1000,
                 "vz_ecef": 500, "speed_m_s": 7608.0, "mass_kg": 6159, "phase": "ORBIT"},
            ])
        _write_csv(
            os.path.join(self.wd, "sim", "弹道事件表.csv"),
            ["event", "t_met_s", "t_utc", "lat_deg", "lon_deg", "alt_m",
             "speed_m_s", "mass_kg", "note"],
            [
                {"event": "起飞", "t_met_s": 0.0, "t_utc": "2026-11-18T22:50:00Z",
                 "lat_deg": 38.86, "lon_deg": 111.59, "alt_m": 1459.0,
                 "speed_m_s": 400.0, "mass_kg": 250660.0, "note": "点火起飞"},
                {"event": "一级关机/一二级分离", "t_met_s": 156.7,
                 "t_utc": "2026-11-18T22:52:36Z", "lat_deg": 38.3, "lon_deg": 111.49,
                 "alt_m": 68900.0, "speed_m_s": 2127.0, "mass_kg": 58660.0, "note": ""},
                {"event": "整流罩分离", "t_met_s": 206.0,
                 "t_utc": "2026-11-18T22:53:26Z", "lat_deg": 37.4, "lon_deg": 111.34,
                 "alt_m": 128527.0, "speed_m_s": 2618.0, "mass_kg": 44552.0, "note": ""},
                {"event": "二级关机(SECO)", "t_met_s": 723.8,
                 "t_utc": "2026-11-18T23:02:03Z", "lat_deg": 11.5, "lon_deg": 107.3,
                 "alt_m": 506854.0, "speed_m_s": 7608.0, "mass_kg": 6159.0, "note": ""},
            ])
        _write_csv(
            os.path.join(self.wd, "sim", "落点与射程.csv"),
            ["object", "impact_t_met_s", "impact_t_utc", "lat_deg", "lon_deg",
             "great_circle_km", "downrange_km", "crossrange_km", "zone_id",
             "inside_zone", "dist_to_boundary_km", "nearest_zone_id",
             "nearest_dist_km", "note"],
            [{"object": "core_stage", "impact_t_met_s": 600.0,
              "impact_t_utc": "2026-11-18T23:00:00Z", "lat_deg": 33.4,
              "lon_deg": 110.6, "great_circle_km": 650.0, "downrange_km": 640.0,
              "crossrange_km": 20.0, "zone_id": "A1", "inside_zone": "true",
              "dist_to_boundary_km": "10.0", "nearest_zone_id": "A1",
              "nearest_dist_km": "10.0", "note": "一级"}])
        _write_json(os.path.join(self.wd, "sim", "入轨根数.json"),
                    {"a_km": 6883.1, "e": 0.0143, "i_deg": 94.18,
                     "perigee_km": 406.0, "apogee_km": 604.0, "period_s": 5683.0,
                     "raan_deg": 330.0, "argp_deg": 77.0,
                     "insertion": {"t_met_s": 723.8, "alt_km": 506.8,
                                   "speed_m_s": 7608.7}})
        _write_json(os.path.join(self.wd, "report", "发射窗口计算.json"),
                    {"practical_window": {"start_utc": "2026-11-18T22:35:14Z",
                                          "end_utc": "2026-11-18T22:54:35Z",
                                          "duration_min": 19.4},
                     "orbit_window": {"available": True, "n_windows": 5,
                                      "best_near_t0": {"best_utc":
                                                       "2026-11-18T22:50:00Z"}}})
        return cfg

    def _ret(self):
        cfg = {
            "mission": {"name": "返回单测", "mission_type": "return",
                        "profile": "return", "t0_utc": "2026-11-26T21:20:00Z",
                        "disclaimer": "仅供测试"},
            "return_only": True, "notams": [],
            "station": {"enabled": True},
            "return_capsule": {"enabled": True},
            "_v3": {"modules": {"return": True, "summary": True, "report": True},
                    "warnings": []},
        }
        _write_json(os.path.join(self.wd, "config.json"), cfg)
        _write_json(os.path.join(self.wd, "sim", "返回舱返回.json"), {
            "enabled": True, "feasible": True, "n_windows": 1,
            "landing_site": {"name": "四子王旗", "lat_deg": 42.27, "lon_deg": 111.7,
                             "radius_km": 50.0},
            "station": {"name": "天宫空间站", "source": "OEM", "kind": "oem"},
            "selected": {
                "t_deorbit_utc": "2026-11-26T20:07:43Z",
                "t_landing_utc": "2026-11-26T21:18:23Z",
                "lat_deg": 42.07, "lon_deg": 111.81, "flight_time_s": 4239.4,
                "inside_zone": True, "zone_id": "R1", "dist_to_zone_km": 23.7,
                "deorbit_dv_mps": 100.0, "bank_deg": -70.0,
                "peaks": {"q_pa": 27972.0, "g": 4.75, "heat_w_cm2": 102.6},
                "landing": {"t_utc": "2026-11-26T21:18:23Z", "lat_deg": 42.07,
                            "lon_deg": 111.81, "speed_m_s": 4.3,
                            "flight_time_s": 4239.4},
            },
            "timeline": [{"event": "再入界面", "t_met_s": 1858.0,
                          "t_utc": "2026-11-26T20:38:41Z", "alt_km": 119.8},
                         {"event": "主伞开伞", "t_met_s": 3100.5,
                          "t_utc": "2026-11-26T20:59:23Z", "alt_km": 6.0}],
            "phase_timeline": [{"event": "船站分离", "phase": "分离",
                                "t_utc": "2026-11-26T19:07:43Z", "t_met_s": -3600.0},
                               {"event": "制动开始", "phase": "制动",
                                "t_utc": "2026-11-26T20:07:43Z", "t_met_s": 0.0}],
            "phases": [{"phase": "制动", "start_utc": "2026-11-26T20:07:43Z",
                        "end_utc": "2026-11-26T20:10:13Z", "duration_s": 150.0,
                        "note": "反向制动"},
                       {"phase": "再入", "start_utc": "2026-11-26T20:38:41Z",
                        "end_utc": "2026-11-26T20:50:43Z", "duration_s": 722.0,
                        "note": ""}],
        })
        _write_csv(os.path.join(self.wd, "sim", "返回舱阶段.csv"),
                   ["phase", "start_utc", "end_utc", "duration_s", "note"],
                   [{"phase": "制动", "start_utc": "2026-11-26T20:07:43Z",
                     "end_utc": "2026-11-26T20:10:13Z", "duration_s": 150.0,
                     "note": "反向制动"},
                    {"phase": "再入", "start_utc": "2026-11-26T20:38:41Z",
                     "end_utc": "2026-11-26T20:50:43Z", "duration_s": 722.0,
                     "note": ""}])
        return cfg


class TestLaunchCommon(_Workdir):
    def test_generate_all(self):
        cfg = self._launch()
        steps = [{"name": "仿真与数据契约", "state": "完成", "seconds": 0.3},
                 {"name": "天空轨迹", "state": "跳过", "seconds": 0.0,
                  "reason": "无观测点"}]
        res = S.generate_all(self.wd, cfg, steps=steps)
        self.assertTrue(res["ok"], res.get("errors"))
        for rel in ("sim/关键节点.csv", "sim/飞行阶段.csv", "sim/飞行环境.csv",
                    "sim/轨迹统计.json", "report/任务摘要.md", "report/任务摘要.json",
                    "report/成果清单.md", "report/成果清单.json",
                    "sim/飞行环境关注项.json"):
            self.assertTrue(os.path.exists(os.path.join(self.wd, rel)), rel)

    def test_key_nodes(self):
        cfg = self._launch()
        S.generate_all(self.wd, cfg)
        rows = S._read_csv(os.path.join(self.wd, "sim", "关键节点.csv"))
        names = [r.get("节点") for r in rows]
        self.assertIn("起飞", names)
        self.assertIn("级间分离", names)
        self.assertIn("整流罩分离", names)
        self.assertIn("末级关机/入轨", names)
        self.assertEqual(rows[0]["节点"], "起飞", "应按 T+ 排序")

    def test_phases(self):
        cfg = self._launch()
        S.generate_all(self.wd, cfg)
        rows = S._read_csv(os.path.join(self.wd, "sim", "飞行阶段.csv"))
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["阶段"] for r in rows], ["一级", "二级", "入轨段"])
        self.assertGreater(float(rows[0]["时长(s)"]), 0)

    def test_q_uses_air_relative_speed(self):
        cfg = self._launch()
        S.generate_all(self.wd, cfg)
        env = S._read_csv(os.path.join(self.wd, "sim", "飞行环境.csv"))
        # 第 1 行 ECEF 速度为 0（惯性速度 400 m/s）→ 动压必须接近 0
        self.assertLess(float(env[0]["q_pa"]), 5.0,
                        "动压不能用惯性速度（会算出 ~100 kPa 的假值）")
        stats = S._read_json(os.path.join(self.wd, "sim", "轨迹统计.json"))
        self.assertGreater(stats["max_q"]["pa"], 0)
        self.assertEqual(stats["trajectory"]["max_altitude_km"], 506.85)
        self.assertEqual(stats["orbit"]["perigee_km"], 406.0)
        self.assertEqual(stats["impacts"][0]["inside_zone"], True)

    def test_summary_markdown_and_inventory(self):
        cfg = self._launch()
        steps = [{"name": "仿真与数据契约", "state": "完成", "seconds": 0.3},
                 {"name": "天空轨迹", "state": "跳过", "seconds": 0.0,
                  "reason": "无观测点"},
                 {"name": "返回舱返回", "state": "不适用", "seconds": 0.0,
                  "reason": "未勾选"}]
        res = S.generate_all(self.wd, cfg, steps=steps)
        with open(os.path.join(self.wd, "report", "任务摘要.md"),
                  encoding="utf-8") as f:
            md = f.read()
        self.assertIn("关键数字", md)
        self.assertIn("关键节点", md)
        self.assertIn("成果与输出完整性", md)
        self.assertIn("常规发射弹道", md)
        with open(os.path.join(self.wd, "report", "成果清单.json"),
                  encoding="utf-8") as f:
            inv = json.load(f)
        strings = {e["path"] for e in inv["expected"]}
        self.assertIn("sim/关键节点.csv", strings)
        self.assertIn("任务输入.json", {m["path"] for m in inv["missing"]},
                      "没写任务输入.json 时应列为缺失项")
        self.assertTrue(res["missing"])
        with open(os.path.join(self.wd, "report", "成果清单.md"),
                  encoding="utf-8") as f:
            md2 = f.read()
        self.assertIn("步骤台账", md2)
        self.assertIn("跳过", md2)

    def test_expected_outputs_follow_modules(self):
        cfg = self._launch()
        cfg["_v3"]["modules"]["sky"] = False
        cfg["_v3"]["modules"]["appear"] = False
        cfg["_v3"]["modules"]["map"] = False
        paths = {p for p, _d, _g in S.expected_outputs(cfg)}
        self.assertNotIn("sky/sky_summary.json", paths)
        self.assertIn("sim/关键节点.csv", paths)
        # v4：关掉 window 模块时不应再把「发射窗口」当成应产出项
        self.assertNotIn("report/发射窗口计算.json", paths)


class TestReturnCommon(_Workdir):
    def test_return_nodes_and_phases(self):
        cfg = self._ret()
        res = S.generate_all(self.wd, cfg)
        self.assertTrue(res["ok"], res.get("errors"))
        rows = S._read_csv(os.path.join(self.wd, "sim", "关键节点.csv"))
        names = [r.get("节点") for r in rows]
        self.assertIn("着陆", names)
        self.assertIn("制动开始", names)
        phases = S._read_csv(os.path.join(self.wd, "sim", "飞行阶段.csv"))
        self.assertEqual([p["阶段"] for p in phases], ["制动", "再入"])
        stats = S._read_json(os.path.join(self.wd, "sim", "轨迹统计.json"))
        self.assertEqual(stats["mission_type"], "return")
        self.assertTrue(stats["return"]["selected"]["inside_zone"])
        with open(os.path.join(self.wd, "report", "任务摘要.md"),
                  encoding="utf-8") as f:
            md = f.read()
        self.assertIn("返回舱返回弹道", md)
        # 返回任务不应生成 飞行环境.csv（无上升段）
        self.assertFalse(os.path.exists(os.path.join(self.wd, "sim", "飞行环境.csv")))

    def test_return_expected_outputs(self):
        cfg = self._ret()
        paths = {p for p, _d, _g in S.expected_outputs(cfg)}
        self.assertIn("sim/返回舱返回.json", paths)
        self.assertNotIn("sim/轨迹统计.json", paths - {p for p in paths})


if __name__ == "__main__":
    unittest.main(verbosity=2)
