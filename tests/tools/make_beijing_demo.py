# -*- coding: utf-8 -*-
"""生成 README/主页展示用的「北京观测」示例任务输入。

设定
----
* **发射工位用真实的太原卫星发射中心 LC-9**（内置库，38.8632°N, 111.5896°E）；
* **观测点改成北京天安门**（39.9042°N, 116.4074°E，海拔 44 m）——展示“异地观测”；
* 航警用内置示例的两条禁区（真实任务格式）；这是一次**示例任务**，不是真实发射。

用法::

    python tests/tools/make_beijing_demo.py     # 写入 examples/北京观测示例_任务输入.json
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
OUT = os.path.join(ROOT, "examples", "北京观测示例_任务输入.json")

#: 观测点：北京天安门（示例观测地点）
OBS_LAT, OBS_LON, OBS_ALT = 39.9042, 116.4074, 44.0

#: 内置示例的两条航警区（与 app/main.py 的 DEMO_CZ2D 一致）
NOTAMS = (
    "A5897/26 NOTAMN\n"
    "Q)ZXXX/QRDCA/IV/BO/W/000/999/3322N11036E013\n"
    "A)ZXXX B)2611182243 C)2611182305\n"
    "E) DANGER AREA ESTABLISHED BOUNDED BY: N333400E1103100-N333200E1104400-"
    "N331000E1104100-N331100E1102700. VERTICAL LIMITS:SFC-UNL.\n\n"
    "A5898/26 NOTAMN\n"
    "Q)ZHWH/QRDCA/IV/BO/W/000/999/3015N11004E022\n"
    "A)ZHWH B)2611182244 C)2611182310\n"
    "E) DANGER AREA ESTABLISHED BOUNDED BY: N303500E1095700-N303300E1101900-"
    "N295400E1101300-N295700E1095000. VERTICAL LIMITS:SFC-UNL.\n"
)

MISSION = {
    "name": "示例：太原 LC-9 发射 · 北京观测（示意）",
    "mission_type": "auto",
    "rocket_label": "长征二号丁",
    "rocket_id": "auto",
    "site_id": "auto",
    "site_label": "太原 LC-9",
    "t0_text": "2026-11-19 06:50",
    "tz_hint": "auto",
    "serial": "遥106",
    "payload_name": "示例载荷",
    "payload_kg": 1000.0,
    "orbit_alt_km": 505.0,
    "orbit_i_deg": 97.45,
    "notams_text": NOTAMS,
    "observers": [
        {"id": "beijing", "name": "北京（天安门观测点）", "lat_deg": OBS_LAT,
         "lon_deg": OBS_LON, "alt_m": OBS_ALT, "note": "示例观测地点"},
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
    print("hint: python -m app.main --cli --mission \"%s\" --workdir \"作品\\示例_太原发射_北京观测\""
          % os.path.relpath(OUT, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
