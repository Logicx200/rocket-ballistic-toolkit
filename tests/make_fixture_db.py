# -*- coding: utf-8 -*-
"""从三个真实历史任务 config.json 生成**测试用**迷你数据库（tests/fixtures/db）。

用途：在正式 app/data/*.json（由 db-research 产出）尚未就绪时，用真实算例端到端跑通
「数据体检 → config → 流水线」，并作为回归基准。这些数值来自已交付算例，可靠性高。
"""
from __future__ import annotations

import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "fixtures", "db")

SOURCES = [
    dict(path=r"F:\DeepSeek工作区\火箭弹道工具箱_v2.0\config.json",
         rid="cz2d", name="长征二号丁", aliases=["CZ-2D", "长二丁", "长征二号丁", "cz2d"],
         cls="medium_liquid", sites=["tslc_lc9"]),
    dict(path=r"F:\DeepSeek工作区\弹道仿真工作区\2026.11.25 CZ2-F\config.json",
         rid="cz2f", name="长征二号F/G", aliases=["CZ-2F", "长二F", "长征二号F", "载人火箭", "cz2f"],
         cls="crewed", sites=["jq_921"]),
    dict(path=r"F:\DeepSeek工作区\弹道仿真工作区\2026.11.27 CZ6-A\config.json",
         rid="cz6a", name="长征六号甲", aliases=["CZ-6A", "长六甲", "长征六号甲", "cz6a"],
         cls="medium_liquid", sites=["tslc_lc9a"]),
]

SITE_IDS = {
    "tslc_lc9": dict(name="太原卫星发射中心 九号工位（TSLC LC-9）", region="山西"),
    "jq_921": dict(name="酒泉卫星发射中心 921 工位（LC-91 / SLS-1）", region="甘肃/内蒙古"),
    "tslc_lc9a": dict(name="太原卫星发射中心 9A 工位（TSLC LC-9A）", region="山西"),
}

CITIES = [
    ("北京", "北京", 39.9042, 116.4074, 44),
    ("太原", "山西", 37.8706, 112.5489, 800),
    ("晋中", "山西", 37.6870, 112.7528, 800),
    ("西安", "陕西", 34.3416, 108.9398, 405),
    ("兰州", "甘肃", 36.0611, 103.8343, 1520),
    ("酒泉", "甘肃", 39.7326, 98.4941, 1480),
    ("上海", "上海", 31.2304, 121.4737, 4),
    ("广州", "广东", 23.1291, 113.2644, 21),
    ("成都", "四川", 30.5728, 104.0668, 500),
    ("武汉", "湖北", 30.5928, 114.3055, 23),
    ("文昌", "海南", 19.5438, 110.7962, 10),
    ("西昌", "四川", 27.8947, 102.2636, 1500),
]

ORBITS = [
    dict(id="sso500", name="太阳同步轨道 500 km", alt_km=500.0, i_deg=97.4, type="SSO"),
    dict(id="sso700", name="太阳同步轨道 700 km", alt_km=700.0, i_deg=98.2, type="SSO"),
    dict(id="leo200", name="近地轨道 200 km", alt_km=200.0, i_deg=41.5, type="LEO"),
    dict(id="leo400", name="近地轨道 400 km", alt_km=400.0, i_deg=51.6, type="LEO"),
]

PAYLOADS = [
    dict(id="shenzhou", name="神舟载人飞船", aliases=["神舟", "Shenzhou"], mass_kg=8100.0,
         min_kg=7800.0, max_kg=8500.0, alt_km=200.0, i_deg=41.5, confidence="中"),
    dict(id="tianzhou", name="天舟货运飞船", aliases=["天舟"], mass_kg=13000.0,
         min_kg=12000.0, max_kg=14000.0, alt_km=380.0, i_deg=41.5, confidence="中"),
    dict(id="yaogan", name="遥感卫星（单星）", aliases=["遥感", "YG"], mass_kg=3000.0,
         min_kg=1000.0, max_kg=3500.0, alt_km=800.0, i_deg=98.0, confidence="低"),
    dict(id="gaofen", name="高分卫星", aliases=["高分", "GF"], mass_kg=2500.0,
         min_kg=1000.0, max_kg=5000.0, alt_km=600.0, i_deg=97.8, confidence="低"),
]


def _liftoff_mass(veh):
    m = 0.0
    for st in veh.get("stages") or []:
        m += float(st.get("dry_kg") or 0) + float(st.get("prop_kg") or 0)
    bo = veh.get("boosters")
    if bo:
        m += (float(bo.get("dry_kg_each") or 0) + float(bo.get("prop_kg_each") or 0)) * \
            float(bo.get("count") or 1)
    m += float(veh.get("fairing", {}).get("mass_kg") or 0) if veh.get("fairing") else 0.0
    m += float(veh.get("payload_kg") or 0) + float(veh.get("adapter_kg") or 0)
    return round(m, 0)


def main():
    os.makedirs(OUT, exist_ok=True)
    rockets, sites = [], []
    for s in SOURCES:
        cfg = json.load(open(s["path"], encoding="utf-8"))
        m = cfg["mission"]
        veh = copy.deepcopy(cfg["vehicle"])
        gd = copy.deepcopy(cfg.get("guidance") or {})
        gd.pop("azimuth_deg", None)
        gd.pop("dt", None)
        gd.pop("debris_dt", None)
        for k in ("mode", "t_v", "t_p", "pitch_power", "t_fairing", "fairing_divert_mps",
                  "plane_keeping", "yaw_tau", "i_target_deg", "post_seco_s", "flow_scale"):
            gd.setdefault(k, cfg.get("guidance", {}).get(k))
        ref = m.get("reference_orbit") or {}
        site = m["launch_site"]
        rockets.append(dict(
            id=s["rid"], name=s["name"], aliases=s["aliases"],
            **{"class": s["cls"]},
            liftoff_mass_kg=_liftoff_mass(veh), leo_kg=None, sso_kg=None,
            default_payload_kg=veh.get("payload_kg"),
            typical_orbits=[dict(name="参考轨道", alt_km=ref.get("alt_km"),
                                 i_deg=ref.get("i_deg"))] if ref else [],
            launch_sites=s["sites"], guidance_defaults=gd, vehicle=veh,
            numerics={"mode": "accurate"}, confidence="高",
            note="由已交付历史算例 %s 的 config.json 复制（参数来源见该算例 t0_note/coord_note 与 docs/验证_回归_CZ2D.md）" % s["path"],
            sources=[{"url": s["path"], "accessed": "2026-11-23", "what": "已交付算例配置"}],
        ))
        sid = s["sites"][0]
        sites.append(dict(
            id=sid, name=SITE_IDS[sid]["name"], aliases=[site["name"]],
            lat_deg=site["lat_deg"], lon_deg=site["lon_deg"],
            alt_m_ellipsoid=site.get("alt_m_ellipsoid"), country="CN",
            region=SITE_IDS[sid]["region"], vehicles=[s["rid"]], confidence="高",
            coord_note=site.get("coord_note", ""),
            sources=[{"url": s["path"], "accessed": "2026-11-23", "what": "已交付算例配置"}],
        ))
    json.dump({"_schema": "rocketkit.rockets.v1", "updated": "2026-11-23", "rockets": rockets},
              open(os.path.join(OUT, "rockets.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    json.dump({"_schema": "rocketkit.sites.v1", "updated": "2026-11-23", "sites": sites},
              open(os.path.join(OUT, "launch_sites.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    json.dump({"_schema": "rocketkit.cities.v1", "updated": "2026-11-23",
               "cities": [dict(name=c[0], province=c[1], lat_deg=c[2], lon_deg=c[3],
                               alt_m=c[4], aliases=[]) for c in CITIES]},
              open(os.path.join(OUT, "cities.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    json.dump({"_schema": "rocketkit.orbits.v1", "updated": "2026-11-23", "orbits": ORBITS},
              open(os.path.join(OUT, "orbits.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    json.dump({"_schema": "rocketkit.payloads.v1", "updated": "2026-11-23", "payloads": PAYLOADS},
              open(os.path.join(OUT, "payloads.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("wrote fixtures to", OUT)
    for f in os.listdir(OUT):
        print("  ", f, os.path.getsize(os.path.join(OUT, f)), "bytes")


if __name__ == "__main__":
    sys.exit(main())
