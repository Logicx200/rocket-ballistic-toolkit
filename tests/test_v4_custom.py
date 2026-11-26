# -*- coding: utf-8 -*-
"""v4 配置构建单测：自定义箭型 / 自定义参考轨道 / 制导覆盖 / 自动观测点 / 时序 / 三类任务。

这些是 v4 的核心新能力，必须保证「界面上填的 = 进 config 的 = 进仿真的」。
"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.core import config_build as CB  # noqa: E402
from app.core import userdb as U  # noqa: E402
from app.core.model import MissionInput  # noqa: E402

TLE_TEXT = """TEST-SAT (500 km SSO)
1 99999U 26001A   26320.50000000  .00000000  00000-0  00000-0 0  0000
2 99999  97.4000 120.0000 0001000  90.0000 200.0000 15.20000000  0000
"""


def _base(**kw) -> MissionInput:
    d = dict(name="v4 单测任务", rocket_label="长征二号丁", site_label="太原 LC-9",
             t0_text="2026-11-19 06:50")
    d.update(kw)
    return MissionInput(**d)


def _custom_rocket(name: str = "单测自定义箭型") -> dict:
    rec = U.blank_rocket(name, "medium_liquid")
    rec["vehicle"]["stages"][0]["prop_kg"] = 180000.0
    return rec


class TestCustomRocket(unittest.TestCase):
    def test_custom_rocket_used_and_recorded(self):
        rec = _custom_rocket()
        cfg = CB.build_config(_base(custom_rocket=rec,
                                    custom_rocket_mode="session"), write=False).config
        self.assertEqual(cfg["mission"]["custom_rocket"]["id"], rec["id"])
        self.assertEqual(len(cfg["vehicle"]["stages"]), len(rec["vehicle"]["stages"]))
        self.assertAlmostEqual(cfg["vehicle"]["stages"][0]["prop_kg"], 180000.0)
        prov = {p["field"]: p for p in cfg["mission"]["_provenance"]}
        self.assertIn("mission.custom_rocket", prov)
        self.assertEqual(prov["mission.custom_rocket"]["source"], "user")

    def test_invalid_custom_rocket_raises(self):
        rec = _custom_rocket()
        rec["vehicle"]["stages"][0]["prop_kg"] = 0
        with self.assertRaises(ValueError) as ctx:
            CB.build_config(_base(custom_rocket=rec), write=False)
        self.assertIn("推进剂", str(ctx.exception))

    def test_low_thrust_warns_but_builds(self):
        rec = _custom_rocket()
        st = rec["vehicle"]["stages"][0]
        st["thrust_sl_n"] = 20000.0
        st["thrust_vac_n"] = 22000.0
        st["engines"] = 1
        br = CB.build_config(_base(custom_rocket=rec), write=False)
        joined = " ".join(br.warnings)
        self.assertIn("推重比", joined)


class TestMissionType(unittest.TestCase):
    def test_standard_default(self):
        cfg = CB.build_config(_base(), write=False).config
        self.assertEqual(cfg["mission"]["mission_type"], "standard")
        self.assertEqual(cfg["mission"]["profile"], "standard")
        self.assertFalse((cfg.get("station") or {}).get("enabled"))

    def test_crewed_enables_station(self):
        cfg = CB.build_config(_base(mission_type="crewed", station_enabled=True),
                              write=False).config
        self.assertEqual(cfg["mission"]["mission_type"], "crewed")
        self.assertEqual(cfg["mission"]["profile"], "cz2f")
        self.assertTrue(cfg["station"]["enabled"])
        mods = cfg["_v3"]["modules"]
        self.assertTrue(mods["station"] and mods["docking"])

    def test_return_routes_to_return_only(self):
        br = CB.build_config(_base(mission_type="return", return_enabled=True,
                                   t0_text="2026-11-26 21:20",
                                   return_landing_lat=42.27,
                                   return_landing_lon=111.70), write=False)
        cfg = br.config
        self.assertTrue(cfg.get("return_only"))
        self.assertEqual(cfg["mission"]["mission_type"], "return")
        self.assertNotIn("vehicle", cfg)
        mods = cfg["_v3"]["modules"]
        self.assertFalse(mods["export"])
        self.assertTrue(mods["station"] and mods["return"])
        self.assertTrue(mods["summary"])


class TestReferenceOrbit(unittest.TestCase):
    def test_custom_orbit_with_raan_creates_window_constraint(self):
        cfg = CB.build_config(_base(
            ref_orbit_mode="custom",
            ref_orbit={"name": "手填轨道", "alt_km": 505.0, "i_deg": 97.45,
                       "e": 0.0, "raan_deg": 123.4}), write=False).config
        self.assertAlmostEqual(cfg["mission"]["reference_orbit"]["alt_km"], 505.0)
        self.assertAlmostEqual(cfg["launch_window"]["raan_deg"], 123.4)
        self.assertTrue(cfg["_v3"]["modules"]["window"],
                        "有 RAAN 平面约束时应计算发射窗口")
        prov = {p["field"]: p for p in cfg["mission"]["_provenance"]}
        self.assertEqual(prov["mission.reference_orbit"]["source"], "user")

    def test_custom_orbit_ltan(self):
        cfg = CB.build_config(_base(
            ref_orbit_mode="custom",
            ref_orbit={"name": "SSO", "alt_km": 500.0, "i_deg": 97.4,
                       "ltan_h": 10.5}), write=False).config
        self.assertAlmostEqual(cfg["launch_window"]["ltan_h"], 10.5)

    def test_template_orbit(self):
        cfg = CB.build_config(_base(ref_orbit_mode="template",
                                    ref_orbit_name="太阳同步轨道 500 km"),
                              write=False).config
        ref = cfg["mission"]["reference_orbit"]
        self.assertAlmostEqual(ref["alt_km"], 500.0)
        self.assertAlmostEqual(ref["i_deg"], 97.4)

    def test_template_orbit_not_found_falls_back(self):
        br = CB.build_config(_base(ref_orbit_mode="template",
                                   ref_orbit_name="不存在的轨道名"), write=False)
        self.assertTrue(any("未找到" in w or "回退" in w for w in br.warnings))
        self.assertIn("reference_orbit", br.config["mission"])

    def test_tle_orbit(self):
        cfg = CB.build_config(_base(ref_orbit_mode="tle",
                                    ref_orbit_tle=TLE_TEXT), write=False).config
        ref = cfg["mission"]["reference_orbit"]
        self.assertAlmostEqual(ref["i_deg"], 97.4, places=3)
        self.assertGreater(ref["alt_km"], 480)
        self.assertLess(ref["alt_km"], 540)
        self.assertAlmostEqual(ref["raan_deg"], 120.0, places=3)


class TestGuidanceAndSequence(unittest.TestCase):
    def test_guidance_overrides_applied_and_locked(self):
        cfg = CB.build_config(_base(guidance_overrides={
            "t_v": 9.5, "t_p": 150.0, "pitch_power": 1.2,
            "azimuth_deg": 190.0}), write=False).config
        g = cfg["guidance"]
        self.assertAlmostEqual(g["t_v"], 9.5)
        self.assertAlmostEqual(g["t_p"], 150.0)
        self.assertAlmostEqual(g["pitch_power"], 1.2)
        self.assertAlmostEqual(g["azimuth_deg"], 190.0)
        inv = cfg.get("inversion") or {}
        paths = {p["path"] for p in (inv.get("parameters") or [])}
        # 方位角被用户固定后不应再进反演
        self.assertNotIn("guidance.azimuth_deg", paths)

    def test_guidance_lock_removes_params_from_inversion(self):
        cfg = CB.build_config(_base(guidance_overrides={"t_v": 9.0}), write=False).config
        paths = {p["path"] for p in ((cfg.get("inversion") or {}).get("parameters") or [])}
        self.assertNotIn("guidance.t_v", paths, "用户固定的 t_v 不应再进反演")
        self.assertEqual((cfg.get("_v3") or {}).get("guidance_locked"), ["t_v"])

    def test_all_guidance_params_locked_keeps_vehicle_params(self):
        """把制导参数全固定后，反演仍可用（只微调箭型 pitch_end 等），但不应再出现 guidance.* 参数。"""
        br = CB.build_config(_base(guidance_overrides={
            "t_v": 9.0, "t_p": 150.0, "pitch_power": 1.1,
            "azimuth_deg": 190.0}), write=False)
        inv = br.config.get("inversion") or {}
        paths = {p["path"] for p in (inv.get("parameters") or [])}
        self.assertFalse([p for p in paths if p.startswith("guidance.")],
                         "guidance.* 都被固定了，不应进反演：%s" % paths)
        self.assertTrue([p for p in paths if p.startswith("vehicle.")],
                        "箭型 pitch_end 仍应可反演")
        self.assertEqual(sorted(br.config["_v3"]["guidance_locked"]),
                         ["azimuth_deg", "pitch_power", "t_p", "t_v"])

    def test_sequence_four_input_formats(self):
        cfg = CB.build_config(_base(launch_sequence={
            "custom": True, "fairing_sep_s": "T+212",
            "seco_s": "2026-11-18T23:02:00Z"}), write=False).config
        val = cfg["launch_sequence"]["values"]
        self.assertAlmostEqual(val["fairing_sep_s"], 212.0)
        # T0 = 2026-11-18T22:50:00Z → 绝对时刻 23:02:00 即 T+720 s
        self.assertAlmostEqual(val["seco_s"], 720.0)
        self.assertEqual(cfg["guidance"]["t_fairing"], 212.0)

    def test_sequence_nested_values_supported(self):
        """v3 从历史 config 读回时是嵌套结构；v4 两种结构都要认。"""
        cfg = CB.build_config(_base(launch_sequence={
            "custom": True,
            "values": {"stage_sep_s": [{"x": 1}], "seco_s": 700}}),
            write=False)
        # 上面故意给了一个非法 stage 值：应给警告而不是崩
        self.assertTrue(any("分离时刻" in w for w in cfg.warnings))
        self.assertAlmostEqual(cfg.config["launch_sequence"]["values"]["seco_s"], 700.0)

    def test_sequence_nested_real(self):
        cfg = CB.build_config(_base(launch_sequence={
            "custom": True,
            "values": {"stage_sep_s": [140], "fairing_sep_s": 205, "seco_s": 700}}),
            write=False).config
        self.assertEqual(cfg["vehicle"]["stages"][0]["sep_met"], 140.0)
        self.assertEqual(cfg["vehicle"]["fairing"]["sep_met"], 205.0)
        self.assertEqual(cfg["vehicle"]["stages"][-1]["seco_met"], 700.0)

    def test_sequence_deviation_warning(self):
        br = CB.build_config(_base(launch_sequence={"custom": True, "seco_s": 900}),
                             write=False)
        self.assertTrue(any("相差" in w for w in br.warnings),
                        "SECO 与默认差太多时应提醒：%s" % br.warnings)

    def test_parse_seq_time(self):
        self.assertEqual(CB.parse_seq_time("206"), 206.0)
        self.assertEqual(CB.parse_seq_time(" T+206.5 "), 206.5)
        self.assertEqual(CB.parse_seq_time("206s"), 206.0)
        self.assertEqual(CB.parse_seq_time("206 秒"), 206.0)
        self.assertIsNone(CB.parse_seq_time(""))
        self.assertIsNone(CB.parse_seq_time("abc"))
        self.assertIsNone(CB.parse_seq_time("2026-11-19T00:00:00Z", ""))
        self.assertAlmostEqual(
            CB.parse_seq_time("2026-11-19T07:10:00Z", "2026-11-18T22:50:00Z"),
            30000.0)   # 8 h 20 min


class TestAutoObserver(unittest.TestCase):
    def test_default_observer_at_site(self):
        cfg = CB.build_config(_base(), write=False).config
        obs = cfg["observers"]
        self.assertEqual(len(obs), 1)
        self.assertIn("发射场", obs[0]["name"])
        self.assertTrue(cfg["mission"].get("auto_observer_used"))
        self.assertTrue(cfg["_v3"].get("auto_observer"), "用户设置应被记录")
        self.assertTrue(cfg["_v3"]["modules"]["sky"], "天空轨迹模块不应因缺观测点被关闭")

    def test_disable_auto_observer(self):
        cfg = CB.build_config(_base(auto_observer=False), write=False).config
        self.assertEqual(cfg["observers"], [])
        # 模块仍保持开启（由 runner 记录"跳过+原因"，而不是在构建阶段静默关闭）
        self.assertTrue(cfg["_v3"]["modules"]["sky"])


class TestRoundTrip(unittest.TestCase):
    def test_roundtrip_keeps_v4_fields(self):
        rec = _custom_rocket("往返测试箭型")
        cfg = CB.build_config(_base(
            mission_type="standard",
            custom_rocket=rec, custom_rocket_mode="session",
            ref_orbit_mode="custom",
            ref_orbit={"name": "往返轨道", "alt_km": 480.0, "i_deg": 97.2,
                       "ltan_h": 13.5},
            guidance_overrides={"t_p": 170.0},
            launch_sequence={"custom": True, "seco_s": 700},
            auto_observer=False), write=False).config
        inp = CB.mission_input_from_config(cfg)
        self.assertEqual(inp.mission_type, "standard")
        self.assertEqual(inp.custom_rocket.get("id"), rec["id"])
        self.assertEqual(inp.ref_orbit_mode, "custom")
        self.assertAlmostEqual(inp.ref_orbit.get("alt_km"), 480.0)
        self.assertAlmostEqual(inp.guidance_overrides.get("t_p"), 170.0)
        self.assertTrue(inp.launch_sequence.get("custom"))
        self.assertFalse(inp.auto_observer)

    def test_roundtrip_return_only(self):
        br = CB.build_config(_base(mission_type="return", return_enabled=True,
                                   t0_text="2026-11-26 21:20"), write=False)
        inp = CB.mission_input_from_config(br.config)
        self.assertTrue(inp.return_only)
        self.assertEqual(inp.mission_type, "return")


if __name__ == "__main__":
    unittest.main(verbosity=2)
