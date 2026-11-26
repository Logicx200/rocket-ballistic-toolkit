# -*- coding: utf-8 -*-
"""v4.1 补丁回归：参考轨道置信度按射向敏感度定量，不再反复报"侧向机动未计算"。

覆盖：
* 正常任务（CZ-2D 太原）→ 采用库内典型轨道，置信度中；
* 与典型轨道偏离 >5° 的场景 → 仍为中（数值可信），另发一条**可执行提示**，不进低置信度项；
* 说明文本不再包含"侧向机动"；
* 射向不确定度放大（az_sigma=4°）→ σ_i>2.5° 时才是低置信度；
* 报告 §1.1 把"低置信度字段"与"推断字段"分开列出。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.core import config_build as CB  # noqa: E402
from app.core import infer as I  # noqa: E402
from app.core import report as R  # noqa: E402
from app.core.model import MissionInput  # noqa: E402

NOTAMS = (
    "A5897/26 NOTAMN\n"
    "Q)ZXXX/QRDCA/IV/BO/W/000/999/3322N11036E013\n"
    "A)ZXXX B)2611182243 C)2611182305\n"
    "E) DANGER AREA ESTABLISHED BOUNDED BY: N333400E1103100-N333200E1104400-"
    "N331000E1104100-N331100E1102700. VERTICAL LIMITS:SFC-UNL.\n"
)


def _inp(rocket: str, site: str, notams: str = NOTAMS) -> MissionInput:
    return MissionInput(rocket_label=rocket, site_label=site,
                        t0_text="2026-11-19 06:50", notams_text=notams)


class TestOrbitConfidence(unittest.TestCase):
    def test_normal_case_adopts_typical_orbit(self):
        br = CB.build_config(_inp("长征二号丁", "太原 LC-9"), write=False)
        ref = br.config["mission"]["reference_orbit"]
        self.assertEqual(ref["confidence"], "中")
        self.assertAlmostEqual(ref["i_deg"], 97.45)
        self.assertIn("i_sigma_deg", ref)
        self.assertLessEqual(ref["i_sigma_deg"], 2.5)
        note = next(r.note for r in br.resolutions
                    if r.field == "mission.reference_orbit")
        self.assertNotIn("侧向机动", note)
        self.assertIn("采用典型轨道", note)

    def test_mismatch_gives_hint_not_low_confidence(self):
        # 文昌 + 太原的航警区：射向反算 i=91°，与库内 SSO 700（98.2°）差 7°
        br = CB.build_config(_inp("长征五号B", "文昌 LC-101"), write=False)
        ref = br.config["mission"]["reference_orbit"]
        self.assertEqual(ref["confidence"], "中",
                         "射向本身可信时不应标低置信度")
        self.assertAlmostEqual(ref.get("mismatch_deg"), 7.04, places=1)
        self.assertFalse([g for g in br.gaps if "reference_orbit" in g.field],
                         "不应把参考轨道放进低置信度项：%s" % [g.field for g in br.gaps])
        joined = " ".join(br.warnings)
        self.assertIn("显式选择目标轨道", joined, "应给可执行提示：%s" % joined)
        note = next(r.note for r in br.resolutions
                    if r.field == "mission.reference_orbit")
        self.assertNotIn("侧向机动", note)
        self.assertIn("±", note, "说明里应给出置信区间")

    def test_user_orbit_is_high_confidence_and_no_hint(self):
        cfg = CB.build_config(_inp("长征五号B", "文昌 LC-101").__class__(
            rocket_label="长征五号B", site_label="文昌 LC-101",
            t0_text="2026-11-19 06:50", notams_text=NOTAMS,
            ref_orbit_mode="template", ref_orbit_name="太阳同步轨道 700 km"),
            write=False).config
        ref = cfg["mission"]["reference_orbit"]
        self.assertIn(ref["confidence"], ("中", "高"))   # 内置模板 → 中/高
        self.assertAlmostEqual(ref["i_deg"], 98.2)

    def test_large_azimuth_sigma_becomes_low(self):
        # 近赤道场址 + 射向本身很不确定：σ_i≈4° → 几何上真的推不出倾角 → 低
        orb, src, conf, note = I.infer_reference_orbit(
            25.0, 5.0, I.match_rocket("长征二号丁")[0], None, None,
            az_sigma_deg=4.0)
        self.assertIsNotNone(orb)
        self.assertEqual(conf, "低")
        self.assertIn("±", note)
        self.assertNotIn("侧向机动", note)

    def test_sigma_is_bounded_for_typical_geometry(self):
        # |di/dA| ≤ 1（解析上界），因此 2° 射向误差最多带来 2° 倾角误差
        for lat in (0.0, 19.6, 38.86, 60.0, 80.0):
            for az in (10.0, 90.0, 150.0, 188.0, 260.0, 350.0):
                i_est = I.orbit_from_azimuth(az, lat)
                sig = I.inclination_sigma_deg(az, lat, i_est)
                self.assertLessEqual(sig, 1.05,
                                     "lat=%s az=%s i=%s sigma=%s" % (lat, az, i_est, sig))


class TestReportSections(unittest.TestCase):
    def test_low_and_inferred_are_separated(self):
        wd = tempfile.mkdtemp(prefix="rk_rep_")
        cfg = {
            "mission": {
                "name": "报告分区测试", "t0_utc": "2026-11-18T22:50:00Z",
                "launch_site": {"name": "测试场", "lat_deg": 38.86, "lon_deg": 111.59,
                                "alt_m_ellipsoid": 1000.0},
                "reference_orbit": {"alt_km": 500.0, "i_deg": 97.4, "confidence": "中"},
                "disclaimer": "仅供测试",
                "_provenance": [
                    {"field": "mission.t0_utc", "label": "T0", "value": "…",
                     "source": "infer", "confidence": "中", "note": "由航警推断"},
                    {"field": "vehicle.payload_kg", "label": "载荷质量", "value": "500",
                     "source": "infer", "confidence": "低", "note": "按箭型量级估"},
                ],
            },
            "vehicle": {"stages": [{"name": "一级"}]},
            "guidance": {}, "numerics": {}, "notams": [],
            "_v3": {"modules": {}, "warnings": []},
        }
        try:
            with open(os.path.join(wd, "config.json"), "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False)
            path = R.generate_report(wd, cfg)
            with open(path, encoding="utf-8") as f:
                md = f.read()
            self.assertIn("低置信度字段（1 项", md)
            self.assertIn("推断字段（1 项", md)
            self.assertNotIn("低置信度/推断字段", md)
            self.assertIn("3-DOF 模型：末级平面修正按简化几何 dogleg", md)
            self.assertNotIn("未考虑侧向机动", md)
        finally:
            import shutil
            shutil.rmtree(wd, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
