# -*- coding: utf-8 -*-
"""生成 README/主页展示用的「北京示例」任务输入（虚构示例，非真实发射）。

用途
----
* README 与个人主页的展示图统一用北京作为示例地点：
  发射场设在北京北部（自定义坐标），观测点设在北京天安门，航警区设在东北方向；
* 载荷 1600 kg、目标轨道 480 km / 44°，使「长征二号丁」从北京向东发射时接近圆轨道
  （单纯换经纬度会因地球自转加成让入轨偏心；这里是按标定结果调的展示参数）；
* 该任务**不是真实发射任务**，只用于演示软件能力与出图效果。

用法::

    python tests/tools/make_beijing_demo.py            # 写入 examples/北京示例_任务输入.json
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
OUT = os.path.join(ROOT, "examples", "北京示例_任务输入.json")

#: 示意发射场：北京北部（40.35°N, 116.20°E，海拔 60 m）
SITE_LAT, SITE_LON, SITE_ALT = 40.3500, 116.2000, 60.0
#: 示例观测点：北京天安门（39.9042°N, 116.4074°E，海拔 44 m）
OBS_LAT, OBS_LON, OBS_ALT = 39.9042, 116.4074, 44.0

#: 两条航警区（一级残骸 / 整流罩），位置取自本任务标定后的自然落点附近
NOTAMS = (
    "A1101/26 NOTAMN\n"
    "Q)ZBPE/QRDCA/IV/BO/W/000/999/4150N12200E040\n"
    "A)ZBPE B)2611182243 C)2611182305\n"
    "E) DANGER AREA ESTABLISHED BOUNDED BY: N421000E1215000-N420500E1223000-"
    "N414500E1220000-N415500E1212000. VERTICAL LIMITS:SFC-UNL.\n\n"
    "A1102/26 NOTAMN\n"
    "Q)ZBPE/QRDCA/IV/BO/W/000/999/4235N12540E045\n"
    "A)ZBPE B)2611182241 C)2611182310\n"
    "E) DANGER AREA ESTABLISHED BOUNDED BY: N425500E1254000-N424500E1262000-"
    "N422000E1254000-N423500E1245000. VERTICAL LIMITS:SFC-UNL.\n"
)

MISSION = {
    "name": "示例：北京 · 长征二号丁（虚构示意弹道）",
    "mission_type": "auto",
    "rocket_label": "长征二号丁",
    "rocket_id": "auto",
    "site_id": "custom",
    "site_label": "北京（示意发射场）",
    "site_lat": SITE_LAT,
    "site_lon": SITE_LON,
    "site_alt": SITE_ALT,
    "t0_text": "2026-11-19 06:50",
    "tz_hint": "auto",
    "serial": "示例",
    "payload_name": "示例载荷",
    "payload_kg": 1600.0,
    "orbit_alt_km": 480.0,
    "orbit_i_deg": 44.0,
    "notams_text": NOTAMS,
    "observers": [
        {"id": "beijing", "name": "北京（天安门示例点）", "lat_deg": OBS_LAT,
         "lon_deg": OBS_LON, "alt_m": OBS_ALT, "note": "示例观测点"},
    ],
    "mode": "accurate",
    "mc_n": 0,
    "modules": {
        "dem": False, "invert": True, "export": True, "window": True,
        "station": False, "docking": False, "return": False,
        "sky": True, "appear": True, "map": True, "mc": False, "kml": True,
        "summary": True, "qc": True, "report": True,
    },
}


def main() -> int:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(MISSION, fh, ensure_ascii=False, indent=2)
    print("written:", OUT)
    print("hint: python -m app.main --cli --mission \"%s\" --workdir \"作品\\示例_北京_2026-11-19\""
          % os.path.relpath(OUT, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
