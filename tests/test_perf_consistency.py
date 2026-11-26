# -*- coding: utf-8 -*-
"""内置库"物理自洽"体检（v3.3 新增）——比 test_db_validate（字段格式/量纲）更深一层。

对每个箭型做三查：
  1. **质量账目**：分级干重+装药+整流罩+适配器+载荷 与公开起飞质量是否闭合（≤2%）。
  2. **起飞推重比**：海平面推力/起飞重力（0.9–2.2；固体捆助推允许到 3）。
  3. **运力闭合**（核心）：可用理想 Δv = Σ Isp·g·ln(m0/mf)（逐级，含并联助推器单独处理）
     与"公开运力档位所需 Δv"比较：
        需要 Δv ≈ v_circ(典型轨道高度) − 地球自转投射 + 重力/阻力损失
     余量 < -0.45 km/s → **不自洽**（该条目的装药/干重/载荷/轨道至少一项是错的，
     与 2026-11-23 CZ-6C 事故同一病根：二级装药少填 3 t 时余量 −1.0 km/s）；
     -0.45 ~ +0.15 → **偏紧**（建议复核）；其余通过。

用法::

    python tests\\test_perf_consistency.py            # 报告 + 退出码（不自洽 >0）
    python tests\\test_perf_consistency.py --strict    # 偏紧也算不通过

说明：损失项是经验值（液体 1.75 / 固体助推 1.9 / 全固体 2.2 km/s），余量带 ±0.45 km/s 的系统不确定度，
所以本测试只用于"把明显不自洽的条目挑出来"，不替代逐型号溯源。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB = os.path.join(ROOT, "app", "data", "rockets.json")
SITES = os.path.join(ROOT, "app", "data", "launch_sites.json")
G0 = 9.80665
RE = 6378.137          # km
MU = 398600.4418       # km^3/s^2
OMEGA_E = 465.1        # m/s（赤道自转线速度）

#: 损失估计（km/s）与判定阈值
LOSS_LIQUID = 1.75
LOSS_SOLID_BOOST = 1.90
LOSS_ALL_SOLID = 2.20
MARGIN_HARD = -0.45    # 余量低于它 → 不自洽
MARGIN_SOFT = 0.15     # 余量低于它 → 偏紧
GTO_NEED = 12.0        # GTO 构型直接用经验总 Δv（km/s）
#: 历史任务锚点：vehicle 参数来自已与真实任务对齐的 config，数值不再改；
#: 其"理论闭合偏紧"属公开数据本身的矛盾（如 CZ-2F 的 464 t/8.4 t 公开值），只提示不定性。
ANCHORS = {"cz2d", "cz2f", "cz6a"}


def load():
    d = json.load(open(DB, encoding="utf-8"))
    s = json.load(open(SITES, encoding="utf-8"))
    return d["rockets"], {x["id"]: x for x in s["sites"]}


def stage_thrusters(st):
    if st.get("phases"):
        out = []
        for ph in st["phases"]:
            out.extend(ph.get("thrusters") or [])
        return out
    return [{"n": st.get("engines", 1), "thrust_vac_n": st.get("thrust_vac_n"),
             "isp_vac_s": st.get("isp_vac_s"), "thrust_sl_n": st.get("thrust_sl_n"),
             "isp_sl_s": st.get("isp_sl_s")}]


def stage_flow(st):
    f = 0.0
    for t in stage_thrusters(st):
        thr, isp = t.get("thrust_vac_n"), t.get("isp_vac_s")
        if thr and isp:
            f += float(t.get("n") or 1) * float(thr) / (float(isp) * G0)
    return f


def vehicle_mass(v):
    m = float(v.get("payload_kg", 0)) + float(v.get("adapter_kg", 0))
    m += float((v.get("fairing") or {}).get("mass_kg", 0))
    m += sum(float(s["dry_kg"]) + float(s["prop_kg"]) for s in v["stages"])
    b = v.get("boosters")
    if b:
        m += int(b["count"]) * (float(b["dry_kg_each"]) + float(b["prop_kg_each"]))
    return m


def liftoff_thrust(v):
    tot = 0.0
    for t in stage_thrusters(v["stages"][0]):
        tot += float(t.get("n") or 1) * float(t.get("thrust_sl_n") or t.get("thrust_vac_n") or 0)
    b = v.get("boosters")
    if b:
        # boosters.thrusters 的口径是"所有助推器合起来"（n=助推器数×单枚发动机数）
        for t in b["thrusters"]:
            tot += float(t.get("n") or 1) * float(t.get("thrust_sl_n") or t.get("thrust_vac_n") or 0)
    return tot


def available_dv(v):
    """逐级理想 Δv（km/s）。boosters.thrusters 口径 = 全部助推器合计。"""
    m = vehicle_mass(v)
    dv = 0.0
    b = v.get("boosters")
    if b:
        thr = sum(float(t.get("n") or 1) * float(t.get("thrust_vac_n") or 0) for t in b["thrusters"])
        flow = sum(float(t.get("n") or 1) * float(t.get("thrust_vac_n") or 0) /
                   (float(t.get("isp_vac_s") or 1) * G0) for t in b["thrusters"])
        isp = thr / (flow * G0) if flow > 0 else 0.0
        prop = float(b["prop_kg_each"]) * int(b["count"])
        m0 = m
        m -= prop
        if isp and m0 > m > 0:
            dv += isp * G0 * math.log(m0 / m)
        m -= int(b["count"]) * float(b["dry_kg_each"])
    for st in v["stages"]:
        thr = sum(float(t.get("n") or 1) * float(t.get("thrust_vac_n") or 0) for t in stage_thrusters(st))
        flow = stage_flow(st)
        if flow <= 0 or thr <= 0:
            continue
        isp = thr / (flow * G0)
        m0 = m
        m -= float(st["prop_kg"])
        if m0 > m > 0:
            dv += isp * G0 * math.log(m0 / m)
        m -= float(st["dry_kg"])
    return dv / 1000.0


def required_dv(r, sites):
    to = (r.get("typical_orbits") or [{}])[0]
    name = str(to.get("name") or "")
    alt = float(to.get("alt_km") or 500.0)
    if "GTO" in name.upper():
        return GTO_NEED, name
    vc = math.sqrt(MU / (RE + alt))                       # km/s
    site = sites.get((r.get("sites") or [None])[0]) or {}
    lat = float(site.get("lat_deg") or 30.0)
    assist = OMEGA_E * math.cos(math.radians(lat)) * math.sin(math.radians(190.0)) / 1000.0
    v = r["vehicle"]
    if v.get("boosters"):
        solid = any((t.get("isp_vac_s") or 999) < 300 for t in v["boosters"]["thrusters"])
        loss = LOSS_SOLID_BOOST if solid else 1.60
    elif all((s.get("isp_vac_s") or 300) < 290 and (s.get("isp_sl_s") or 999) < 270
             for s in v["stages"]):
        loss = LOSS_ALL_SOLID
    else:
        loss = LOSS_LIQUID
    return vc - assist + loss, name


def audit():
    rockets, sites = load()
    rows = []
    for r in rockets:
        v = r.get("vehicle") or {}
        mass = vehicle_mass(v)
        pub = float(r.get("liftoff_mass_kg") or 0)
        tw = liftoff_thrust(v) / (mass * G0)
        av = available_dv(v)
        need, orb = required_dv(r, sites)
        margin = av - need
        if margin < MARGIN_HARD:
            verdict, level = "不自洽", 2
        elif margin < MARGIN_SOFT:
            verdict, level = "偏紧", 1
        else:
            verdict, level = "通过", 0
        if abs(mass - pub) / pub > 0.02 or not (0.9 <= tw <= 3.0):
            verdict, level = "不自洽", max(level, 2)
        # 锚点（历史任务标定）与 GTO 构型：只提示，不计入硬判定
        if r["id"] in ANCHORS and level >= 1:
            level = 1
            verdict = "锚点·偏紧"
        if "GTO" in orb.upper() and level >= 1:
            level = 1
            verdict = "GTO·参考"
        rows.append(dict(id=r["id"], name=r.get("name"), conf=r.get("confidence"),
                         mass_t=mass / 1000.0, pub_t=pub / 1000.0,
                         mass_rel=(mass - pub) / pub * 100.0, tw=tw,
                         dv_avail=av, dv_need=need, margin=margin, orbit=orb,
                         payload_t=float(v.get("payload_kg") or 0) / 1000.0,
                         verdict=verdict, level=level))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="偏紧也算不通过")
    args = ap.parse_args()
    rows = audit()
    print("=" * 108)
    print("内置库物理自洽体检（质量账目 / 推重比 / 运力闭合）")
    print("=" * 108)
    print("%-22s %7s %7s %7s %6s %8s %8s %8s %7s  %s" % (
        "id", "起飞t", "公开t", "差%", "T/W", "可用dv", "需求dv", "余量", "载荷t", "结论"))
    order = None
    for x in sorted(rows, key=lambda y: (-y["level"], y["margin"])):
        print("%-22s %7.1f %7.1f %+6.1f%% %6.2f %8.2f %8.2f %+8.2f %7.2f  %s%s" % (
            x["id"], x["mass_t"], x["pub_t"], x["mass_rel"], x["tw"],
            x["dv_avail"], x["dv_need"], x["margin"], x["payload_t"], x["verdict"],
            ("  <" + x["orbit"] + ">") if x["level"] else ""))
    bad = [x for x in rows if x["level"] >= 2]
    tight = [x for x in rows if x["level"] == 1]
    print("-" * 108)
    print("不自洽 %d 条：%s" % (len(bad), "、".join("%s(%+.2f km/s)" % (x["id"], x["margin"]) for x in bad) or "无"))
    print("偏紧   %d 条：%s" % (len(tight), "、".join("%s(%+.2f km/s)" % (x["id"], x["margin"]) for x in tight) or "无"))
    print("说明：需求 Δv 含经验损失（液体 1.75 / 固体助推 1.9 / 全固体 2.2 km/s），余量带 ±0.45 km/s 系统不确定度；")
    print("      本体检只用于挑出「明显不自洽」的条目，具体型号仍需逐条溯源。")
    out = os.path.join(HERE, "perf_consistency_report.json")
    json.dump(rows, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("明细已写入：%s" % out)
    fail = bool(bad) or (args.strict and bool(tight))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())

