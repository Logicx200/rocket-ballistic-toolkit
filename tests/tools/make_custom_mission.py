# -*- coding: utf-8 -*-
"""生成 v4 自定义功能测试用任务 JSON（自定义箭型 + 自定义参考轨道 + 自定义时序）。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import userdb as U  # noqa: E402

rocket = U.blank_rocket("测试号甲（自定义两级液体）", "medium_liquid")
# 只改动“用户能感知的差异项”，其余沿用已率定数值——这也是编辑器推荐用法：
# 默认克隆内置箭型，然后改载荷/整流罩等少数参数，而不是从空白模板重写全部质量推力。
rocket["vehicle"]["payload_kg"] = 1100.0
rocket["vehicle"]["fairing"]["mass_kg"] = 900.0
rocket["vehicle"]["fairing"]["sep_met"] = 210.0
rocket["guidance_defaults"]["t_fairing"] = 210.0
rocket["default_payload_kg"] = 1100.0
rocket["confidence"] = "中"
rocket["note"] = "v4 自定义箭型测试：克隆长二丁后改载荷 1100 kg、整流罩 900 kg/抛罩 210 s"

notams = (
    "A5897/26 NOTAMN\n"
    "Q)ZXXX/QRDCA/IV/BO/W/000/999/3322N11036E013\n"
    "A)ZXXX B)2611182243 C)2611182305\n"
    "E) DANGER AREA ESTABLISHED BOUNDED BY: N333400E1103100-N333200E1104400-"
    "N331000E1104100-N331100E1102700. VERTICAL LIMITS:SFC-UNL.\n\n"
    "A5898/26 NOTAMN\n"
    "Q)ZHWH/QRDCA/IV/BO/W/000/999/3015N11004E022\n"
    "A)ZHWH B)2611182244 C)2611182310\n"
    "E) DANGER AREA ESTABLISHED BOUNDED BY: N305300E1091600-N304900E1102000-"
    "N295800E1100700-N300300E1090200. VERTICAL LIMITS:SFC-UNL.\n"
)

mission = {
    "name": "v4-自定义功能测试：自定义箭型 + 自定义参考轨道 + 自定义时序",
    "mission_type": "standard",
    "rocket_label": "",
    "custom_rocket": rocket,
    "custom_rocket_mode": "session",
    "site_label": "太原 LC-9",
    "t0_text": "2026-11-19 06:50",
    "payload_name": "遥感三十九号",
    "payload_kg": 1000.0,
    "ref_orbit_mode": "custom",
    "ref_orbit": {"name": "我的 SSO 500（手填）", "alt_km": 500.0, "i_deg": 97.4,
                  "e": 0.001, "ltan_h": 10.5, "type": "SSO"},
    "notams_text": notams,
    # 只改抛罩时刻（T+ 写法），SECO 留空=箭型默认——演示“留空不影响结果”
    "launch_sequence": {"custom": True, "fairing_sep_s": "T+212"},
    "observers": [{"id": "obs1", "name": "太原（自定义测试点）",
                   "lat_deg": 37.87, "lon_deg": 112.55, "alt_m": 800.0, "note": ""}],
    "modules": {"dem": False, "invert": True, "export": True, "window": True,
                "station": False, "docking": False, "return": False, "sky": False,
                "appear": False, "map": False, "mc": False, "kml": False,
                "summary": True, "qc": True, "report": True},
    "mode": "accurate",
}

out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "_tools", "t4_mission.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(mission, f, ensure_ascii=False, indent=2)
print("written:", out)
