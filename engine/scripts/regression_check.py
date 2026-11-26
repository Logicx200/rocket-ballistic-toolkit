# -*- coding: utf-8 -*-
"""回归校验：通用 N 级动力学模型 vs 参考工程数据（默认 CZ-2D 20261119）。

用法：
  python scripts/regression_check.py --config templates/config_cz2d.json \
      --reference D:\\Users\\rocket_launching\\20261119CZ2D\\sim
输出：控制台表格 + 退出码（0=通过）。判定阈值见 PASS_CRITERIA。
"""
import sys, os, json, math, argparse, csv
HERE = os.path.dirname(os.path.abspath(__file__))

# --- v3 引擎/工作区解耦（自动补丁） ---
_ENG = os.environ.get("RK_ENGINE")
if _ENG:
    for _p in (_ENG, os.path.join(_ENG, "scripts")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
# --- end ---
sys.path.insert(0, os.path.dirname(HERE))     # 工作区根
sys.path.insert(0, HERE)
import numpy as np
import dynamics as D
from common import notam as N

# 判定阈值：通用重实现 vs 原专用模型（CZ-2D 780 s 上升段）。
# 差异来源：分离/SECO 的单步时序、参数取整（配置中已用反演精确值）。
PASS_CRITERIA = dict(
    event_dt_s=0.02,          # 事件时刻差（1 个积分步）
    impact_km=0.10,           # 残骸落点差
    orbit_a_km=1.50,          # 半长轴差（约 2e-4 相对）
    orbit_e=1.0e-5,           # 偏心率差
    orbit_i_deg=0.002,        # 倾角差
    traj_alt_m=150.0,         # 轨迹高度差
    traj_horiz_m=200.0,       # 水平位置差
)


def load_reference(ref_dir):
    sim = ref_dir
    traj = list(csv.DictReader(open(os.path.join(sim, "trajectory.csv"), encoding="utf-8-sig")))
    ev = list(csv.DictReader(open(os.path.join(sim, "弹道事件表.csv"), encoding="utf-8-sig")))
    imp = list(csv.DictReader(open(os.path.join(sim, "落点与射程.csv"), encoding="utf-8-sig")))
    orb = json.load(open(os.path.join(sim, "入轨根数.json"), encoding="utf-8"))
    return traj, ev, imp, orb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(os.path.dirname(HERE), "templates", "config_cz2d.json"))
    ap.add_argument("--reference", required=True, help="参考工程 sim/ 目录")
    ap.add_argument("--accurate", action="store_true",
                    help="用 v2 accurate 口径比对（默认 legacy：与 v1 参考逐位同口径）")
    args = ap.parse_args()
    cfg = json.load(open(args.config, encoding="utf-8"))
    veh = D.vehicle_from_config(cfg)
    p = D.params_from_config(cfg)
    if not args.accurate:
        # v1 参考结果是在 legacy 口径（事件量化到 dt 步边界、dt=0.02）下产生的；
        # 默认用同口径复现，apples-to-apples。
        p.numerics = "legacy"
        p.dt = float((cfg.get("guidance") or {}).get("dt", 0.02))
    print("[reg] 口径 %s dt=%.3f" % (p.numerics, p.dt))
    print("[reg] liftoff mass %.0f kg（%d 级%s）" % (
        veh.liftoff_mass, len(veh.stages), "，带 %d 台助推器" % veh.boosters.count if veh.boosters else "，无助推器"))
    r = D.simulate(veh, p, collect_debris=True)
    if r["failures"]:
        print("[reg] FAIL 仿真失败：", r["failures"])
        return 1
    traj, ev_ref, imp_ref, orb_ref = load_reference(args.reference)

    rows, ok = [], True

    # ---- 1. 事件 ----
    ref_ev = {e["event"]: float(e["t_met_s"]) for e in ev_ref}
    for e in r["events"]:
        name = e["event"]
        key = {"一级关机/分离": "一级关机/一二级分离", "二级关机(SECO)": "二级关机(SECO)",
               "二级点火": "二级点火", "整流罩分离": "整流罩分离"}.get(name, name)
        if key not in ref_ev:
            continue
        dt = e["t_met_s"] - ref_ev[key]
        bad = abs(dt) > PASS_CRITERIA["event_dt_s"]
        ok = ok and not bad
        rows.append(("事件 %s" % key, "T+%.3f / %.3f s" % (e["t_met_s"], ref_ev[key]),
                     "Δt=%+.3f s" % dt, "PASS" if not bad else "FAIL"))

    # ---- 2. 落点 ----
    ref_imp = {x["object"]: x for x in imp_ref}
    for d in r["debris"]:
        obj = d["obj"]
        ref_key = {"一级": "core_stage", "二级": "fairing_L"}.get(obj, obj)
        # 整流罩左右半按方位配准
        if obj.startswith("fairing"):
            ref_key = obj
        if ref_key not in ref_imp:
            continue
        ref = ref_imp[ref_key]
        dlat = (d["impact"]["lat"] - float(ref["lat_deg"])) * 111.19
        dlon = (d["impact"]["lon"] - float(ref["lon_deg"])) * 111.19 * math.cos(math.radians(
            float(ref["lat_deg"])))
        dist = math.hypot(dlat, dlon)
        dt = d["impact"]["t_met_s"] - float(ref["impact_t_met_s"])
        bad = dist > PASS_CRITERIA["impact_km"]
        ok = ok and not bad
        rows.append(("落点 %s" % obj, "(%.4f, %.4f) / (%.4f, %.4f)" % (
            d["impact"]["lat"], d["impact"]["lon"], float(ref["lat_deg"]), float(ref["lon_deg"])),
            "Δ=%.3f km, Δt=%+.1f s" % (dist, dt), "PASS" if not bad else "FAIL"))

    # ---- 3. 入轨根数 ----
    o = r["orbit_seco"]
    for k, tol in (("a_km", "orbit_a_km"), ("e", "orbit_e"), ("i_deg", "orbit_i_deg")):
        dv = o[k] - float(orb_ref[k])
        bad = abs(dv) > PASS_CRITERIA[tol]
        ok = ok and not bad
        rows.append(("根数 %s" % k, "%.6f / %.6f" % (o[k], float(orb_ref[k])),
                     "Δ=%+.6f" % dv, "PASS" if not bad else "FAIL"))

    # ---- 4. 轨迹（同 MET 对比）----
    t_ref = np.array([float(x["t_met_s"]) for x in traj])
    lat_ref = np.array([float(x["lat_deg"]) for x in traj])
    lon_ref = np.array([float(x["lon_deg"]) for x in traj])
    alt_ref = np.array([float(x["alt_m"]) for x in traj])
    max_alt = max_h = 0.0
    for i in range(0, len(r["t"]), 2):
        ts = float(r["t"][i])
        if ts > t_ref[-1]:
            break
        la = float(np.interp(ts, t_ref, lat_ref))
        lo = float(np.interp(ts, t_ref, lon_ref))
        al = float(np.interp(ts, t_ref, alt_ref))
        dh = math.hypot((r["lat"][i] - la) * 111190.0,
                        (r["lon"][i] - lo) * 111190.0 * math.cos(math.radians(la)))
        da = abs(r["alt"][i] - al)
        max_h = max(max_h, dh)
        max_alt = max(max_alt, da)
    bad = max_h > PASS_CRITERIA["traj_horiz_m"] or max_alt > PASS_CRITERIA["traj_alt_m"]
    ok = ok and not bad
    rows.append(("轨迹最大偏差（水平/高度）", "-", "%.1f m / %.1f m" % (max_h, max_alt),
                 "PASS" if not bad else "FAIL"))

    # ---- 输出 ----
    print("\n%-26s %-46s %-30s %s" % ("项目", "本模型 / 参考", "差", "判定"))
    print("-" * 116)
    for a, b, c, d in rows:
        print("%-26s %-46s %-30s %s" % (a, b, c, d))
    print("-" * 116)
    print("总判定：%s（阈值：事件 %.2f s、落点 %.2f km、a %.2f km、e %.1e、i %.2f°、轨迹 %.0f m/%.0f m）"
          % ("PASS" if ok else "FAIL", PASS_CRITERIA["event_dt_s"], PASS_CRITERIA["impact_km"],
             PASS_CRITERIA["orbit_a_km"], PASS_CRITERIA["orbit_e"], PASS_CRITERIA["orbit_i_deg"],
             PASS_CRITERIA["traj_horiz_m"], PASS_CRITERIA["traj_alt_m"]))
    res = dict(passed=ok, criteria=PASS_CRITERIA, rows=[
        dict(item=a, values=b, diff=c, verdict=d) for a, b, c, d in rows],
        model=dict(liftoff_mass_kg=veh.liftoff_mass, stages=len(veh.stages),
                   boosters=(veh.boosters.count if veh.boosters else 0),
                   insertion=r["insertion_state"], orbit=o))
    json.dump(res, open(os.path.join(HERE, "..", "docs", "回归结果_CZ2D.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
