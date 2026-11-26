# -*- coding: utf-8 -*-
"""v3.6 发射时序自定义单测：默认不覆盖；自定义写入 vehicle/guidance 并生效。"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "engine", "scripts"))

from app.core import config_build as CB  # noqa: E402
from app.core.model import MissionInput  # noqa: E402
import dynamics as D  # noqa: E402


def _inp(**kw):
    base = dict(name="时序测试", rocket_label="长征二号丙",
                site_label="太原卫星发射中心", t0_text="2026-11-19 06:50")
    base.update(kw)
    return MissionInput(**base)


class TestLaunchSequenceV36(unittest.TestCase):
    def test_default_not_overridden(self):
        cfg = CB.build_config(_inp(), write=False).config
        seq = cfg.get("launch_sequence") or {}
        self.assertFalse(seq.get("custom"))
        self.assertEqual(seq.get("source"), "default")
        self.assertEqual(cfg["vehicle"]["fairing"]["sep_met"], 200.0)
        self.assertIsNone(cfg["vehicle"]["stages"][0].get("sep_met"))

    def test_custom_applied(self):
        seq = {"custom": True, "stage_sep_s": [130], "fairing_sep_s": 215,
               "seco_s": 340}
        cfg = CB.build_config(_inp(launch_sequence=seq), write=False).config
        self.assertTrue(cfg["launch_sequence"]["custom"])
        self.assertEqual(cfg["vehicle"]["stages"][0]["sep_met"], 130.0)
        self.assertEqual(cfg["vehicle"]["fairing"]["sep_met"], 215.0)
        self.assertEqual(cfg["guidance"]["t_fairing"], 215.0)
        self.assertEqual(cfg["vehicle"]["stages"][-1]["seco_met"], 340.0)

    def test_custom_takes_effect_in_simulation(self):
        seq = {"custom": True, "stage_sep_s": [130], "fairing_sep_s": 210,
               "seco_s": 340}
        cfg = CB.build_config(_inp(launch_sequence=seq), write=False).config
        res = D.simulate(D.vehicle_from_config(cfg), D.params_from_config(cfg),
                         collect_debris=False)
        ev = {e["event"]: float(e["t_met_s"]) for e in res["events"]}
        self.assertAlmostEqual(ev.get("一级关机/分离"), 130.0, places=2)
        self.assertAlmostEqual(ev.get("整流罩分离"), 210.0, places=2)
        self.assertAlmostEqual(ev.get("二级关机(SECO)"), 340.0, places=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
