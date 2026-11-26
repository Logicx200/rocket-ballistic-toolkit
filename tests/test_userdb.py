# -*- coding: utf-8 -*-
"""v4 用户库（自定义箭型 / 参考轨道 / 时序模板）单测。

覆盖：模板来源、归一化与校验、指标估算、用户库 CRUD、与内置库合并（覆盖标注）、
数据包导入导出、轨道与时序模板。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.core import paths as P  # noqa: E402
from app.core import userdb as U  # noqa: E402


class _TmpUserData(unittest.TestCase):
    """把用户库目录重定向到临时目录，避免污染真实 user_data/。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="rk_userdb_")
        self._orig = P.runtime_base
        P.runtime_base = lambda: Path(self._tmp)  # type: ignore[assignment]
        U._BUILTIN_CACHE = None

    def tearDown(self):
        P.runtime_base = self._orig  # type: ignore[assignment]
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestTemplates(_TmpUserData):
    def test_blank_rocket_derived_from_builtin(self):
        rec = U.blank_rocket("测试甲", "medium_liquid")
        self.assertTrue(rec.get("_user"))
        self.assertEqual(rec.get("name"), "测试甲")
        self.assertEqual(rec.get("class_key"), "medium_liquid")
        self.assertTrue(rec.get("vehicle", {}).get("stages"), "模板必须带分级")
        # 默认克隆内置箭型（比手写模板可靠）：_derived_from 应有值
        self.assertTrue(rec.get("_derived_from"), "应记录来源箭型")
        self.assertEqual(rec.get("launch_sites"), [], "不应继承原箭型的典型工位")

    def test_four_class_templates(self):
        for key in U.CLASS_KEYS:
            rec = U.blank_rocket("模板-%s" % key, key)
            self.assertEqual(rec["class_key"], key)
            stages = rec["vehicle"]["stages"]
            self.assertTrue(stages)
            issues = U.validate_rocket(rec)
            errors = [m for lv, _f, m in issues if lv == "错误"]
            self.assertEqual(errors, [], "内置来源的模板不应有错误：%s" % errors)

    def test_class_key_of_from_chinese_class(self):
        self.assertEqual(U.class_key_of({"class": "载人中型两级"}), "crewed")
        self.assertEqual(U.class_key_of({"class": "小型四级固体"}), "small_solid")
        self.assertEqual(U.class_key_of({"class": "大型三级半"}), "large_liquid")
        self.assertEqual(U.class_key_of({"class": "中型两级"}), "medium_liquid")
        self.assertEqual(U.class_key_of({"liftoff_mass_kg": 40000}), "small_solid")
        self.assertEqual(U.class_key_of({"class_key": "crewed", "class": "小型固体"}),
                         "crewed")


class TestNormalizeAndValidate(_TmpUserData):
    def test_normalize_fills_defaults(self):
        rec = U.normalize_rocket({"name": " 简易火箭 ", "vehicle": {"stages": [
            {"name": "一级", "dry_kg": "1000", "prop_kg": "9000"},
            {"name": "二级", "dry_kg": 500, "prop_kg": 2000,
             "phases": [{"thrusters": [{"thrust_vac_n": 30000, "isp_vac_s": 290}]}]},
        ]}})
        self.assertEqual(rec["name"], "简易火箭")
        self.assertTrue(rec["id"])
        st = rec["vehicle"]["stages"]
        self.assertEqual(st[0]["dry_kg"], 1000.0)
        self.assertEqual(st[0]["pitch_end_deg"], 0.0)
        self.assertEqual(rec["vehicle"]["payload_kg"], 1000.0)  # 默认载荷
        self.assertIn("guidance_defaults", rec)
        self.assertEqual(st[1]["phases"][0]["thrusters"][0]["n"], 1)

    def test_validate_catches_missing_mass_and_thrust(self):
        rec = U.blank_rocket("坏火箭", "medium_liquid")
        del rec["vehicle"]["stages"][0]["thrust_sl_n"]
        rec["vehicle"]["stages"][0]["thrust_vac_n"] = 0
        rec["vehicle"]["stages"][0].pop("engines", None)
        issues = U.validate_rocket(rec)
        msgs = " ".join(m for _l, _f, m in issues)
        self.assertIn("推力", msgs)

    def test_validate_warns_on_low_twr_and_isp(self):
        rec = U.blank_rocket("低推重比", "medium_liquid")
        rec["vehicle"]["stages"][0]["thrust_sl_n"] = 20000.0
        rec["vehicle"]["stages"][0]["engines"] = 1
        issues = U.validate_rocket(rec)
        levels = {lv for lv, _f, _m in issues}
        self.assertIn("提示", levels)
        m = U.rocket_metrics(rec)
        self.assertLess(m["liftoff_twr"], 1.0)
        self.assertTrue(m["warnings"], "推重比过低应给警告")

    def test_metrics_reasonable_for_builtin_clone(self):
        rec = U.blank_rocket("指标", "medium_liquid")
        m = U.rocket_metrics(rec)
        self.assertGreater(m["liftoff_mass_kg"], 100000)
        self.assertGreater(m["liftoff_twr"], 1.05)
        self.assertGreater(m["dv_ideal_mps"], 7000)
        self.assertTrue(m["burns"])
        self.assertGreater(m["burn_total_s"], 100)


class TestRocketCRUD(_TmpUserData):
    def test_save_list_delete(self):
        rec = U.save_rocket(U.blank_rocket("我的甲", "medium_liquid"))
        self.assertTrue(rec["id"].startswith("user"))
        items = U.list_rockets()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "我的甲")
        # 更新（同 id）
        rec["note"] = "改过"
        U.save_rocket(rec)
        self.assertEqual(U.list_rockets()[0]["note"], "改过")
        self.assertTrue(U.delete_rocket(str(rec["id"])))
        self.assertEqual(U.list_rockets(), [])

    def test_duplicate_name_gets_unique_id(self):
        a = U.save_rocket(U.blank_rocket("同名", "medium_liquid"))
        b = U.blank_rocket("同名", "medium_liquid")
        b = U.save_rocket(b, new=True)
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(len(U.list_rockets()), 2)

    def test_merge_shadows_builtin_same_id(self):
        builtin = [{"id": "cz2d", "name": "内置长二丁"}, {"id": "other", "name": "其它"}]
        rec = U.blank_rocket("覆盖长二丁", "medium_liquid")
        rec["id"] = "cz2d"
        U.save_rocket(rec)
        merged = U.merge_rockets(builtin)
        hit = next(m for m in merged if m["id"] == "cz2d")
        self.assertEqual(hit["name"], "覆盖长二丁")
        self.assertTrue(hit.get("_shadow_builtin"))
        self.assertEqual([m for m in merged if m["id"] == "other"], [{"id": "other",
                                                                      "name": "其它"}])


class TestOrbits(_TmpUserData):
    def test_orbit_crud_and_validation(self):
        o = U.save_orbit({"name": "我的 SSO", "alt_km": 500, "i_deg": 97.4,
                          "e": 0.001, "ltan_h": 10.5, "type": "SSO"})
        self.assertTrue(o["id"])
        self.assertEqual(len(U.list_orbits()), 1)
        self.assertEqual(U.list_orbits()[0]["ltan_h"], 10.5)
        issues = U.validate_orbit({"name": "坏轨道", "alt_km": 5000, "i_deg": 200})
        self.assertTrue(any(lv == "错误" for lv, _f, _m in issues))
        issues2 = U.validate_orbit({"name": "SSO", "alt_km": 500, "i_deg": 97.4})
        self.assertTrue(any("LTAN" in m for _lv, _f, m in issues2))
        self.assertTrue(U.delete_orbit(str(o["id"])))

    def test_merge_orbits(self):
        builtin = [{"id": "sso_500", "name": "内置 SSO"}, {"id": "leo_400", "name": "LEO"}]
        U.save_orbit({"name": "用户轨道", "alt_km": 600, "i_deg": 98})
        merged = U.merge_orbits(builtin)
        self.assertEqual(len(merged), 3)
        self.assertEqual(merged[-1]["name"], "用户轨道")


class TestSequencesAndBundle(_TmpUserData):
    def test_sequence_template(self):
        s = U.save_sequence({"name": "我的时序", "rocket_id": "cz2d",
                             "values": {"fairing_sep_s": 210, "seco_s": 730,
                                        "stage_sep_s": [156]}})
        self.assertTrue(s["id"])
        self.assertEqual(len(U.list_sequences()), 1)
        self.assertTrue(U.delete_sequence(str(s["id"])))

    def test_bundle_roundtrip(self):
        U.save_rocket(U.blank_rocket("包内箭型", "small_solid"))
        U.save_orbit({"name": "包内轨道", "alt_km": 400, "i_deg": 51.6})
        U.save_sequence({"name": "包内时序", "values": {"seco_s": 700}})
        path = os.path.join(self._tmp, "bundle.json")
        n = U.export_bundle(path)
        self.assertEqual(n, 3)
        # 清空后导入
        U.delete_rocket(str(U.list_rockets()[0]["id"]))
        U.delete_orbit(str(U.list_orbits()[0]["id"]))
        U.delete_sequence(str(U.list_sequences()[0]["id"]))
        stats = U.import_bundle(path)
        self.assertEqual(stats["rockets"], 1)
        self.assertEqual(stats["orbits"], 1)
        self.assertEqual(stats["sequences"], 1)

    def test_tolerant_read_of_bom_file(self):
        p = U.user_dir() / "rockets.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = U.blank_rocket("BOM 箭型", "medium_liquid")
        import json
        text = json.dumps({"schema": "rocketkit.user_db.v1", "rockets": [rec]},
                          ensure_ascii=False, indent=2)
        p.write_text(text, encoding="utf-8-sig")   # 模拟记事本另存为（带 BOM）
        self.assertEqual(len(U.list_rockets()), 1)

    def test_broken_file_is_backed_up(self):
        p = U.user_dir() / "rockets.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{ this is not json", encoding="utf-8")
        self.assertEqual(U.list_rockets(), [])
        self.assertTrue(any(f.suffix.startswith(".bad") for f in p.parent.iterdir()),
                        "坏文件应被改名备份")


if __name__ == "__main__":
    unittest.main(verbosity=2)
