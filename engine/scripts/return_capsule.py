# -*- coding: utf-8 -*-
"""返回舱返回窗口与弹道（CZ-2F/神舟返回，v3.5）。

输入
----
* 空间站轨道（``config.station``，官方 OEM / 用户 TLE / 内置快照，复用
  :mod:`common.station`）；
* 返回航警（``config.notams`` 中带「返回/返回舱/回收/着陆/landing/capsule」hint 的区，
  或 ``return_capsule.use_current_notams=true`` 时用全部区）；
* 返回舱气动参数与着陆场（``config.return_capsule``）。

模型
----
1. 在候选制动时刻取空间站 ECI 状态，施加反向制动 ΔV；
2. :mod:`common.reentry` 积分「滑行→再入→减速伞→主伞→着陆」（USSA 大气、阻力、
   常值 L/D + 倾侧角、J2）；
3. 对候选时刻扫描，检查落点是否在航警着陆区内、落地时刻是否在航警 B)/C) 内；
4. 对选中的机会做气动参数/制动偏差的蒙特卡洛，给出 1σ 落点椭圆（"大致范围"）。

性能策略
--------
先用「空间站在 ``t + T_nominal`` 时刻的星下点是否靠近着陆区」做**廉价预筛**，
只对候选走廊做完整 3-DOF 积分，避免对整段时间逐秒仿真。

输出
----
* ``sim/返回舱返回.json``：窗口、选中机会、事件、落点/椭圆、航警匹配；
* ``sim/返回舱_时序.csv``：选中机会的完整时序。

用法：python scripts/return_capsule.py --config config.json [--allow-network]
"""
import argparse
import csv
import json
import math
import os
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("RK_ROOT") or os.path.dirname(HERE)
_ENG = os.environ.get("RK_ENGINE")
if _ENG:
    for _p in (_ENG, os.path.join(_ENG, "scripts")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np
from common import geo
from common import notam as N
from common import orbit as O
from common import reentry as R

try:
    from common import station as ST
except Exception:  # noqa: BLE001
    ST = None

DEFAULT_SITE = dict(name="四子王旗着陆场（默认）", lat_deg=42.27, lon_deg=111.70,
                    radius_km=30.0)
RETURN_KEYS = ("返回", "返回舱", "回收", "着陆", "落区", "landing", "return",
               "capsule", "recovery")


def _log(msg: str) -> None:
    print(msg, flush=True)


def _write_csv(path, header, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)


def _parse_iso(text, default=None):
    try:
        return O.parse_iso(text)
    except Exception:  # noqa: BLE001
        return default


def _utc_text(dt):
    return O.utc_text(dt) if dt else ""


def _zone_return_like(z):
    txt = " ".join(str(z.get(k) or "") for k in
                   ("stage_hint", "object_hint", "raw_text", "q_line"))
    low = txt.lower()
    return any(k.lower() in low for k in RETURN_KEYS)


def _zone_center(z):
    poly = z.get("polygon_latlon") or []
    if len(poly) >= 3:
        try:
            c = N.polygon_centroid(poly)
            if isinstance(c, dict):
                return float(c.get("lat_deg") or c.get("lat")), float(
                    c.get("lon_deg") or c.get("lon"))
            return float(c[0]), float(c[1])
        except Exception:  # noqa: BLE001
            pass
    # 退化：多边形均值
    if poly:
        return (float(np.mean([p[0] for p in poly])),
                float(np.mean([p[1] for p in poly])))
    q = str(z.get("q_line") or "")
    return None


def resolve_landing(cfg):
    rc = dict(cfg.get("return_capsule") or {})
    site = dict(rc.get("landing_site") or {})
    zones = []
    for z in cfg.get("notams") or []:
        if _zone_return_like(z) or rc.get("use_current_notams"):
            zones.append(z)
    if site.get("lat_deg") is None or site.get("lon_deg") is None:
        if zones:
            c = _zone_center(zones[0])
            if c:
                site = dict(name="航警着陆区 %s" % zones[0].get("id", "?"),
                            lat_deg=c[0], lon_deg=c[1], radius_km=30.0,
                            from_notam=zones[0].get("id", ""))
        if site.get("lat_deg") is None:
            site = dict(DEFAULT_SITE)
    site.setdefault("radius_km", 30.0)
    return site, zones


def _inside_any(lat, lon, zones):
    for z in zones:
        poly = z.get("polygon_latlon") or []
        if len(poly) >= 3 and N.point_in_polygon(lat, lon, poly):
            return z
    return None


def _zone_dist(lat, lon, z):
    poly = z.get("polygon_latlon") or []
    if len(poly) < 3:
        return float("nan")
    try:
        return float(N.distance_to_polygon_edge_km(lat, lon, poly))
    except Exception:  # noqa: BLE001
        return float("nan")


def _time_in_zone(landing_dt, z):
    b = _parse_iso(z.get("b_utc"))
    c = _parse_iso(z.get("c_utc"))
    if b is None or c is None:
        return True, None
    return (b <= landing_dt <= c), (b, c)


def _target_distance(lat, lon, site, zones):
    ds = []
    for z in zones:
        c = _zone_center(z)
        if c:
            ds.append(R.ground_range_km(lat, lon, *c))
    ds.append(R.ground_range_km(lat, lon, site["lat_deg"], site["lon_deg"]))
    return min(ds) if ds else float("inf")


def _eval_opportunity(src, t_d, params, dv, bank_deg, site, zones):
    """完整仿真一次并判定。"""
    try:
        r, v = src.state(t_d)
        p = replace(params)
        p.bank_deg = float(bank_deg)
        res = R.simulate_return(r, v, t_d, p, deorbit_dv_mps=dv, record=False)
    except Exception as exc:  # noqa: BLE001
        return dict(t_deorbit_utc=_utc_text(t_d), ok=False, reason=str(exc))
    if not res.get("ok"):
        return dict(t_deorbit_utc=_utc_text(t_d), ok=False,
                    reason=res.get("reason") or "仿真失败")
    L = res["landing"]
    lt = _parse_iso(L["t_utc"])
    zone = _inside_any(L["lat_deg"], L["lon_deg"], zones) if zones else None
    time_ok = True
    if zone is not None:
        time_ok, _bc = _time_in_zone(lt, zone)
        inside = True
    else:
        d_site = R.ground_range_km(L["lat_deg"], L["lon_deg"],
                                   site["lat_deg"], site["lon_deg"])
        inside = d_site <= float(site.get("radius_km") or 30.0)
        d_site_km = d_site
    if zones:
        # 不在任何区内时，找最近区边界
        if zone is not None:
            dist = _zone_dist(L["lat_deg"], L["lon_deg"], zone)
        else:
            dist = min((_zone_dist(L["lat_deg"], L["lon_deg"], z) for z in zones
                        if len(z.get("polygon_latlon") or []) >= 3),
                       default=float("nan"))
    else:
        dist = (d_site_km if "d_site_km" in locals() else float("nan"))
    feasible = bool(inside and time_ok)
    return dict(
        t_deorbit_utc=_utc_text(t_d), t_deorbit_bjt=O.bjt_text(t_d), ok=True,
        t_landing_utc=L["t_utc"], t_landing_bjt=O.bjt_text(lt),
        lat_deg=round(float(L["lat_deg"]), 5), lon_deg=round(float(L["lon_deg"]), 5),
        flight_time_s=round(float(L["flight_time_s"]), 1),
        ground_speed_mps=round(float(L["speed_m_s"]), 2),
        zone_id=(zone or {}).get("id", ""), inside_zone=bool(inside),
        notam_time_ok=bool(time_ok), feasible=feasible,
        dist_to_zone_km=(round(float(dist), 2) if dist == dist else None),
        deorbit_dv_mps=float(dv), bank_deg=float(bank_deg),
        target_distance_km=round(_target_distance(L["lat_deg"], L["lon_deg"],
                                                  site, zones), 1),
        events=res.get("events") or [],
        peaks=res.get("peaks") or {},
        landing=dict(lat_deg=float(L["lat_deg"]), lon_deg=float(L["lon_deg"]),
                     t_utc=L["t_utc"], speed_m_s=float(L["speed_m_s"]),
                     flight_time_s=float(L["flight_time_s"])),
    )


def _merge_intervals(points, gap_s=120.0):
    if not points:
        return []
    pts = sorted(points)
    out = [[pts[0][0], pts[0][1]]]
    for t0, t1 in pts[1:]:
        if (t0 - out[-1][1]).total_seconds() <= gap_s:
            out[-1][1] = max(out[-1][1], t1)
        else:
            out.append([t0, t1])
    return [(a, b) for a, b in out]


def _bisect_boundary(eval_fn, t_in, t_out, iters=16):
    """在可行/不可行之间二分窗口边界（t_in 可行，t_out 不可行）。"""
    for _ in range(iters):
        mid = t_in + (t_out - t_in) / 2.0
        ev = eval_fn(mid)
        if ev.get("feasible") == ev.get("feasible") and ev.get("feasible"):
            t_in = mid
        else:
            t_out = mid
    return t_in


def _make_params(rc):
    def f(key, default):
        v = rc.get(key)
        return default if v is None else type(default)(v)
    return R.CapsuleParams(
        mass_kg=f("mass_kg", 3000.0),
        cd_area_m2=f("cd_area_m2", 5.0),
        ld_ratio=f("ld_ratio", 0.2),
        bank_deg=f("bank_deg", 0.0),
        drogue_cd_area_m2=f("drogue_cd_area_m2", 20.0),
        chute_cd_area_m2=f("chute_cd_area_m2", 1200.0),
        drogue_deploy_km=f("drogue_deploy_km", 10.0),
        main_deploy_km=f("main_deploy_km", 6.0),
        main_deploy_speed_mps=f("main_deploy_speed_mps", 80.0),
        entry_interface_km=f("entry_interface_km", 120.0),
        dt_coast_s=2.0, dt_entry_s=2.0, dt_low_s=1.0, dt_chute_s=0.5,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--allow-network", action="store_true")
    args = ap.parse_args()
    cfg = json.load(open(args.config, encoding="utf-8-sig"))
    rc = dict(cfg.get("return_capsule") or {})
    sim_dir = os.path.join(ROOT, "sim")
    os.makedirs(sim_dir, exist_ok=True)
    out = dict(enabled=False, generated_utc=O.utc_text(datetime.now(timezone.utc)))
    if not rc.get("enabled"):
        out["reason"] = "未启用返回舱返回模块（config.return_capsule.enabled=false）"
        json.dump(out, open(os.path.join(sim_dir, "返回舱返回.json"), "w",
                            encoding="utf-8"), ensure_ascii=False, indent=2)
        _log("[return] 未启用，跳过")
        return 0
    if ST is None:
        out["reason"] = "空间站模块不可用"
        return _finish(out, sim_dir, 2)

    t0_nom = _parse_iso((cfg.get("mission") or {}).get("t0_utc"),
                        datetime.now(timezone.utc))
    site, zones = resolve_landing(cfg)
    out.update(landing_site=site,
               notam_zones=[z.get("id") for z in zones],
               use_current_notams=bool(rc.get("use_current_notams")))
    _log("[return] 着陆场：%s（%.4fN, %.4fE, R=%.0f km）；返回航警区 %s"
         % (site.get("name"), site["lat_deg"], site["lon_deg"],
            float(site.get("radius_km") or 30), out["notam_zones"] or "无"))

    try:
        src = ST.resolve(cfg, t_hint=t0_nom, allow_network=bool(args.allow_network),
                         logger=_log)
    except Exception as exc:  # noqa: BLE001
        out["reason"] = "空间站轨道不可用：%s" % exc
        return _finish(out, sim_dir, 2)
    out.update(enabled=True, station=src.info())

    params = _make_params(rc)
    dv0 = float(rc.get("deorbit_dv_mps") or 120.0)
    bank0 = float(rc.get("bank_deg") or 0.0)
    auto_bank = bool(rc.get("auto_bank", True))
    auto_dv = bool(rc.get("auto_dv", True))
    out["model"] = dict(
        mass_kg=params.mass_kg, cd_area_m2=params.cd_area_m2,
        ld_ratio=params.ld_ratio, bank_deg=params.bank_deg,
        drogue_cd_area_m2=params.drogue_cd_area_m2,
        chute_cd_area_m2=params.chute_cd_area_m2,
        drogue_deploy_km=params.drogue_deploy_km,
        main_deploy_km=params.main_deploy_km,
        entry_interface_km=params.entry_interface_km,
        deorbit_dv_mps=dv0, auto_dv=auto_dv, auto_bank=auto_bank,
        note="3-DOF 简化返回舱模型；USSA 大气、阻力、常值 L/D+倾侧角、J2；"
             "落点精度约几十 km，仅供公开信息分析与教学")

    # ---- 搜索区间 ----
    ss = _parse_iso(rc.get("search_start_utc"))
    se = _parse_iso(rc.get("search_end_utc"))
    if ss is None or se is None:
        if zones:
            bs = [b for b in (_parse_iso(z.get("b_utc")) for z in zones) if b]
            cs = [c for c in (_parse_iso(z.get("c_utc")) for z in zones) if c]
            if bs and cs:
                ss = min(bs) - timedelta(hours=3)
                se = max(cs) + timedelta(hours=1)
        if ss is None or se is None:
            ss = t0_nom - timedelta(hours=6)
            se = t0_nom + timedelta(hours=6)
    cov = src.coverage()
    if cov:
        ss = max(ss, cov[0])
        se = min(se, cov[1] - timedelta(seconds=4000))
    if se <= ss:
        out["reason"] = "搜索区间无效（%s ~ %s）" % (_utc_text(ss), _utc_text(se))
        return _finish(out, sim_dir, 2)
    out["search"] = dict(start_utc=_utc_text(ss), end_utc=_utc_text(se),
                         coarse_step_s=float(rc.get("coarse_step_s") or 180.0),
                         scan_step_s=float(rc.get("scan_step_s") or 5.0))

    # ---- 标称飞行时间（用于确定搜索/展示） ----
    t_ref = ss + (se - ss) / 2.0
    try:
        r_ref, v_ref = src.state(t_ref)
        res_ref = R.simulate_return(r_ref, v_ref, t_ref, params,
                                    deorbit_dv_mps=dv0, record=False)
        t_nom = float(res_ref["landing"]["flight_time_s"]) if res_ref.get("ok") else 3900.0
    except Exception:  # noqa: BLE001
        t_nom = 3900.0
    out["nominal_flight_s"] = round(t_nom, 1)
    _log("[return] 标称飞行时间 %.0f s；粗扫…" % t_nom)

    def evaluate(td, dv, bank):
        return _eval_opportunity(src, td, params, dv, bank, site, zones)

    # ---- 粗扫：直接对制动时刻做完整仿真（不依赖「星下点=落点」的错误近似） ----
    # 落点对 bank 很敏感（同一时刻不同 bank 可差上千 km），粗扫必须带 bank 维，
    # 否则会在 bank=0 的“假极小”上停住。
    coarse_step = float(rc.get("coarse_step_s") or 300.0)
    bank_scan = [bank0] if not auto_bank else [-70.0, -40.0, 0.0, 40.0, 70.0]
    coarse = []
    t = ss
    while t <= se:
        best = None
        for bk in bank_scan:
            ev = evaluate(t, dv0, bk)
            if not ev.get("ok"):
                continue
            d = ev.get("target_distance_km") or 1e9
            if best is None or d < best[0]:
                best = (d, bk, ev)
        if best is not None:
            ev = best[2]
            ev["_t_dt"] = t
            coarse.append(ev)
        t += timedelta(seconds=coarse_step)
    if not coarse:
        out["reason"] = "搜索区间内没有可用的返回弹道"
        return _finish(out, sim_dir, 2)
    # 距离局部最小（或直接取最小的几个点）
    ds = [e.get("target_distance_km") or 1e9 for e in coarse]
    cand = []
    for i in range(1, len(coarse) - 1):
        if ds[i] <= ds[i - 1] and ds[i] <= ds[i + 1] and ds[i] < float(
                rc.get("refine_km") or 1500.0):
            cand.append(i)
    if not cand:
        cand = sorted(range(len(coarse)), key=lambda i: ds[i])[:3]
    # 局部极小可能只是“假谷”（距离函数在 bank 维有窄谷）；再并上几个全局最小点
    top = sorted(range(len(coarse)), key=lambda i: ds[i])[:5]
    cand = sorted(set(list(cand) + top), key=lambda i: ds[i])[:4]
    _log("[return] 粗扫 %d 点（含 bank 扫描）；候选极小 %d 个（最近 %.0f km）"
         % (len(coarse), len(cand), min(ds)))

    all_evals = []
    cand_best = []
    for i in cand:
        t_best = coarse[i]["_t_dt"]
        b_best = float(coarse[i].get("bank_deg") or bank0)
        lo = max(ss, t_best - timedelta(seconds=coarse_step))
        hi = min(se, t_best + timedelta(seconds=coarse_step))
        # 每个候选点附近选一组 (ΔV, bank)；只在该点做小网格
        dvs = [dv0] if not auto_dv else [100.0, 120.0, 140.0]
        banks = [b_best] if not auto_bank else sorted(set(
            [b_best, b_best - 10.0, b_best + 10.0, -70.0, 0.0, 70.0]))
        best = None
        for dv in dvs:
            for bk in banks:
                ev = evaluate(t_best, dv, bk)
                if not ev.get("ok"):
                    continue
                key = ev.get("target_distance_km") or 1e9
                if best is None or key < best[0]:
                    best = (key, dv, bk)
        _dv, _bk = best[1], best[2]
        _log("[return] 候选 %s：Δv=%.0f m/s、bank=%.0f°，最近 %.0f km"
             % (_utc_text(t_best)[11:19], _dv, _bk, float(best[0])))
        # 在 ±coarse_step 内用三分法细化最近点（距离函数局部近似单峰）
        def _dist(td):
            ev = evaluate(td, _dv, _bk)
            return (ev.get("target_distance_km") or 1e9), ev
        a, b = lo, hi
        for _ in range(12):
            m1 = a + (b - a) / 3.0
            m2 = b - (b - a) / 3.0
            d1, _ = _dist(m1)
            d2, _ = _dist(m2)
            if d1 <= d2:
                b = m2
            else:
                a = m1
        t_min = a + (b - a) / 2.0
        _rd, _ = _dist(t_min)
        cand_best.append((float(_rd), t_min, _dv, _bk))
    if not cand_best:
        out["reason"] = "候选点附近没有可用的返回弹道"
        return _finish(out, sim_dir, 2)
    # 只对全局最好的候选做细扫（其余候选只用于选 ΔV/bank/时刻）
    _bd, t_min, _dv, _bk = min(cand_best, key=lambda x: x[0])
    _log("[return] 全局最近候选 %s：Δv=%.0f m/s、bank=%.0f°，%.0f km；细扫…"
         % (_utc_text(t_min)[11:19], _dv, _bk, _bd))
    all_evals = []
    scan_step = float(rc.get("scan_step_s") or 5.0)
    pad = timedelta(seconds=coarse_step + 180.0)
    t = max(ss, t_min - pad)
    t_end = min(se, t_min + pad)
    while t <= t_end:
        ev = evaluate(t, _dv, _bk)
        if ev.get("ok"):
            ev["_t_dt"] = t
            all_evals.append(ev)
        t += timedelta(seconds=scan_step)
    if not all_evals:
        out["reason"] = "候选点附近没有可用的返回弹道"
        return _finish(out, sim_dir, 2)

    # ---- 可行窗口 ----
    all_evals.sort(key=lambda e: e.get("t_deorbit_utc", ""))
    windows = []
    cur = None
    for ev in all_evals:
        if ev.get("feasible"):
            if cur is None:
                cur = dict(start=ev.get("t_deorbit_utc"), end=ev.get("t_deorbit_utc"),
                           items=[ev])
            else:
                cur["end"] = ev.get("t_deorbit_utc")
                cur["items"].append(ev)
        else:
            if cur is not None:
                windows.append(cur)
                cur = None
    if cur is not None:
        windows.append(cur)
    # 边界细化：扫描步长 5 s 会把窗口截成单点；向两侧逐 2 s 探到不可行，再二分到 1 s。
    for w in windows:
        _e0 = w["items"][0]
        _dv, _bk = float(_e0["deorbit_dv_mps"]), float(_e0["bank_deg"])
        t_start = _parse_iso(w["start"])
        t_end = _parse_iso(w["end"])
        for side in ("start", "end"):
            if side == "start":
                t_in, direction = t_start, -1
            else:
                t_in, direction = t_end, 1
            t_out = t_in + timedelta(seconds=2.0 * direction)
            found = False
            for _ in range(15):
                ev = evaluate(t_out, _dv, _bk)
                if ev.get("ok") and ev.get("feasible"):
                    t_in = t_out
                    t_out = t_out + timedelta(seconds=2.0 * direction)
                else:
                    found = True
                    break
            if found:
                for _ in range(6):
                    mid = t_in + (t_out - t_in) / 2.0
                    ev = evaluate(mid, _dv, _bk)
                    if ev.get("ok") and ev.get("feasible"):
                        t_in = mid
                    else:
                        t_out = mid
            if side == "start":
                t_start = t_in
            else:
                t_end = t_in
        w["start"] = _utc_text(t_start)
        w["end"] = _utc_text(t_end)
    wrows = []
    for w in windows:
        items = w["items"]
        best = min(items, key=lambda e: (e.get("dist_to_zone_km")
                                         if e.get("dist_to_zone_km") is not None else 0))
        wrows.append(dict(start_utc=w["start"], end_utc=w["end"],
                          start_bjt=O.bjt_text(_parse_iso(w["start"])),
                          end_bjt=O.bjt_text(_parse_iso(w["end"])),
                          duration_s=round((_parse_iso(w["end"]) -
                                            _parse_iso(w["start"])).total_seconds(), 1),
                          best_deorbit_utc=best.get("t_deorbit_utc"),
                          best_deorbit_bjt=best.get("t_deorbit_bjt"),
                          best_landing_utc=best.get("t_landing_utc"),
                          landing_lat_deg=best.get("lat_deg"),
                          landing_lon_deg=best.get("lon_deg"),
                          zone_id=best.get("zone_id"),
                          inside_zone=best.get("inside_zone"),
                          notam_time_ok=best.get("notam_time_ok"),
                          dist_to_zone_km=best.get("dist_to_zone_km"),
                          deorbit_dv_mps=best.get("deorbit_dv_mps"),
                          bank_deg=best.get("bank_deg"),
                          flight_time_s=best.get("flight_time_s")))
    # 最佳/最近机会
    feas = [e for e in all_evals if e.get("feasible")]
    if feas:
        selected = min(feas, key=lambda e: abs(
            (_parse_iso(e["t_landing_utc"]) - t0_nom).total_seconds()))
        feasible = True
    else:
        selected = min(all_evals, key=lambda e: e.get("target_distance_km") or 1e9)
        feasible = False
    out.update(windows=wrows, n_windows=len(wrows), feasible=feasible,
               selected=_public_eval(selected),
               selection_note=("可行窗口内、距标称 T0 最近" if feasible
                               else "无可行窗口；给出距着陆区最近的一次机会（仅示意）"))

    # ---- 选中机会的详细时序 ----
    try:
        td = _parse_iso(selected["t_deorbit_utc"])
        r, v = src.state(td)
        p = replace(params)
        p.bank_deg = float(selected["bank_deg"])
        det = R.simulate_return(r, v, td, p,
                                deorbit_dv_mps=float(selected["deorbit_dv_mps"]),
                                record=True)
        out["timeline"] = det.get("events") or []
        rows = []
        for x in det.get("trajectory") or []:
            rows.append([x.get("t_utc"), x.get("t_met_s"), x.get("lat_deg"),
                         x.get("lon_deg"), x.get("alt_km"), x.get("speed_km_s"),
                         x.get("q_pa"), x.get("phase")])
        _write_csv(os.path.join(sim_dir, "返回舱_时序.csv"),
                   ["t_utc", "t_met_s", "lat_deg", "lon_deg", "alt_km",
                    "ground_speed_km_s", "q_pa", "phase"], rows)
        out["timeline_csv"] = "sim/返回舱_时序.csv"
        out["trajectory_points"] = len(rows)
        _phase_ev, _seps, _phases = _build_return_phases(src, td, det, rc)
        out["phase_timeline"] = _phase_ev
        out["separations"] = _seps
        out["phases"] = _phases
        if _phase_ev:
            _write_phase_csv(os.path.join(sim_dir, "返回舱事件与分离点.csv"), _phase_ev)
            out["phase_csv"] = "sim/返回舱事件与分离点.csv"
        if _phases:
            _write_phases_csv(os.path.join(sim_dir, "返回舱阶段.csv"), _phases)
            out["phase_summary_csv"] = "sim/返回舱阶段.csv"
        _ctx, _ctx_err = _pre_return_context(src, td, rc)
        if _ctx:
            _write_context_csv(os.path.join(sim_dir, "返回前30分钟_空间站与神舟.csv"), _ctx)
            out["pre_context_csv"] = "sim/返回前30分钟_空间站与神舟.csv"
            out["pre_context"] = dict(points=len(_ctx),
                                      start_utc=_ctx[0].get("t_utc"),
                                      end_utc=_ctx[-1].get("t_utc"),
                                      objects=["空间站", "神舟飞船（分离后）"])
        if _ctx_err:
            out["pre_context_warning"] = _ctx_err
        _png = os.path.join(sim_dir, "返回舱弹道.png")
        _err = _plot_return(det, site, zones, _png)
        if not _err:
            out["plot"] = "sim/返回舱弹道.png"
        else:
            out["plot_warning"] = _err
        try:
            _kml, _kmz = _write_return_kml(os.path.join(ROOT, "viz"), det, site, zones,
                                          _seps, _ctx)
            if _kml:
                out["kml"] = "viz/返回舱弹道.kml"
                out["kmz"] = "viz/返回舱弹道.kmz"
        except Exception as exc:  # noqa: BLE001
            out["kml_warning"] = str(exc)
        _write_return_windows(os.path.join(sim_dir, "返回舱窗口.csv"),
                              out.get("windows") or [])
        out["windows_csv"] = "sim/返回舱窗口.csv"
    except Exception as exc:  # noqa: BLE001
        out["timeline_warning"] = str(exc)

    # ---- 落点散布（1σ 椭圆） ----
    try:
        base_lat = selected.get("lat_deg")
        base_lon = selected.get("lon_deg")
        pts = [(base_lat, base_lon)]
        td = _parse_iso(selected["t_deorbit_utc"])
        r, v = src.state(td)
        cases = []
        for ddv in (-0.5, 0.5, -0.25, 0.25):
            cases.append(dict(dv=selected["deorbit_dv_mps"] + ddv,
                              bank=selected["bank_deg"], mass=params.mass_kg))
        for dbk in (-0.5, 0.5):
            cases.append(dict(dv=selected["deorbit_dv_mps"],
                              bank=selected["bank_deg"] + dbk, mass=params.mass_kg))
        for dm in (-0.01, 0.01):
            cases.append(dict(dv=selected["deorbit_dv_mps"],
                              bank=selected["bank_deg"],
                              mass=params.mass_kg * (1 + dm)))
        for c in cases:
            p = replace(params)
            p.bank_deg = float(c["bank"])
            p.mass_kg = float(c["mass"])
            rr = R.simulate_return(r, v, td, p, deorbit_dv_mps=float(c["dv"]),
                                   record=False)
            if rr.get("ok"):
                pts.append((rr["landing"]["lat_deg"], rr["landing"]["lon_deg"]))
        ell = R.dispersion_ellipse(pts)
        if ell:
            out["dispersion"] = ell
            out["dispersion_note"] = ("对 ΔV ±0.5 m/s、倾侧角 ±0.5°、质量 ±1% 扰动后的落点样本"
                                      "做 1σ 协方差椭圆；不是官方残骸区")
    except Exception as exc:  # noqa: BLE001
        out["dispersion_warning"] = str(exc)

    try:
        _map = os.path.join(ROOT, "map", "返回舱落区.png")
        _err = _plot_landing_map(det, site, zones, out.get("dispersion"), _map)
        if not _err:
            out["landing_map"] = "map/返回舱落区.png"
        else:
            out["landing_map_warning"] = _err
    except Exception as exc:  # noqa: BLE001
        out["landing_map_warning"] = str(exc)

    _log("[return] 可行窗口 %d 个；%s" % (
        len(wrows), ("选中制动 %s → 落地 %s（%.2fN, %.2fE，%s）"
                     % (selected.get("t_deorbit_utc"), selected.get("t_landing_utc"),
                        selected.get("lat_deg") or 0, selected.get("lon_deg") or 0,
                        "区内" if selected.get("inside_zone") else "区外"))
        if selected else "无"))
    return _finish(out, sim_dir, 0)


def _pre_return_context(src, t_deorbit, rc, seconds=1800.0, step_s=10.0):
    """返回前 30 min 的空间站 + 神舟飞船轨道（分离后按二体+J2 递推）。"""
    from datetime import timedelta as _td
    end = t_deorbit
    start = end - _td(seconds=float(seconds))
    undock_lead = float(rc.get("undock_lead_s") or 3600.0)
    base_t = end - _td(seconds=undock_lead)
    if base_t > start:
        base_t = start
    rows = []
    try:
        r_s, v_s = src.state(base_t)
        # 先把飞船状态递推到窗口起点
        if (start - base_t).total_seconds() > 0:
            r_s, v_s = O.propagate_rk4(r_s, v_s, (start - base_t).total_seconds(),
                                       step_s=30.0, j2=True)
        t = start
        while t <= end + _td(seconds=0.1):
            r_st, _v_st = src.state(t)
            la, lo, al = O.subpoint(t, r_st)
            rows.append(dict(object="空间站", t_utc=_utc_text(t),
                             lat_deg=round(float(la), 5), lon_deg=round(float(lo), 5),
                             alt_km=round(float(al), 3)))
            la2, lo2, al2 = O.subpoint(t, r_s)
            rows.append(dict(object="神舟飞船（分离后）", t_utc=_utc_text(t),
                             lat_deg=round(float(la2), 5), lon_deg=round(float(lo2), 5),
                             alt_km=round(float(al2), 3)))
            t += _td(seconds=step_s)
            if t <= end:
                r_s, v_s = O.propagate_rk4(r_s, v_s, step_s, step_s=30.0, j2=True)
    except Exception as exc:  # noqa: BLE001
        return rows, str(exc)
    return rows, ""


def _write_context_csv(path, rows):
    _write_csv(path, ["object", "t_utc", "lat_deg", "lon_deg", "alt_km"],
               [[r.get("object"), r.get("t_utc"), r.get("lat_deg"),
                 r.get("lon_deg"), r.get("alt_km")] for r in rows])


def _write_return_kml(out_dir, det, site, zones, separations=None, context=None):
    """写返回舱 KML/KMZ：星下点、3D 轨迹、关键事件、分离点、着陆场与航警区。"""
    import html
    import zipfile
    os.makedirs(out_dir, exist_ok=True)
    tr = det.get("trajectory") or []
    if len(tr) < 2:
        return None, None
    evs = det.get("events") or []
    landing = det.get("landing") or {}

    def _pos_at(tsec):
        best = min(tr, key=lambda x: abs(float(x.get("t_met_s") or 0) - tsec))
        return best

    def _coord(lon, lat, alt_m=0.0):
        return "%.6f,%.6f,%.1f" % (float(lon), float(lat), float(alt_m))

    ground = "\n".join(_coord(x["lon_deg"], x["lat_deg"], 0.0) for x in tr)
    air = "\n".join(_coord(x["lon_deg"], x["lat_deg"],
                           float(x.get("alt_km") or 0.0) * 1000.0) for x in tr)
    pm = []
    pm.append("""<Placemark><name>返回舱星下点</name><styleUrl>#ground</styleUrl>
<LineString><tessellate>1</tessellate><altitudeMode>clampToGround</altitudeMode>
<coordinates>%s</coordinates></LineString></Placemark>""" % ground)
    pm.append("""<Placemark><name>返回舱 3D 轨迹</name><styleUrl>#air</styleUrl>
<LineString><tessellate>1</tessellate><altitudeMode>absolute</altitudeMode>
<coordinates>%s</coordinates></LineString></Placemark>""" % air)
    # 返回前 30 min：空间站 + 神舟飞船
    for _obj, _style, _label in (("空间站", "station", "空间站（返回前30min）"),
                                ("神舟飞船（分离后）", "spacecraft",
                                 "神舟飞船（返回前30min，分离后）")):
        _cr = [x for x in (context or []) if x.get("object") == _obj]
        if len(_cr) >= 2:
            _coords = "\n".join(_coord(x["lon_deg"], x["lat_deg"],
                                       float(x.get("alt_km") or 0.0) * 1000.0)
                                for x in _cr)
            pm.append("""<Placemark><name>%s</name><styleUrl>#%s</styleUrl>
<LineString><tessellate>1</tessellate><altitudeMode>absolute</altitudeMode>
<coordinates>%s</coordinates></LineString></Placemark>""" % (_label, _style, _coords))
    # 关键事件点
    for e in evs:
        t = float(e.get("t_met_s") or 0)
        pos = _pos_at(t)
        lat = e.get("lat", pos.get("lat_deg"))
        lon = e.get("lon", pos.get("lon_deg"))
        alt = float(e.get("alt_km") or pos.get("alt_km") or 0.0) * 1000.0
        name = html.escape(str(e.get("event") or "事件"))
        desc = html.escape("MET %.1f s / UTC %s / 速度 %s m/s" % (
            t, e.get("t_utc") or "", e.get("speed_m_s") or "—"))
        pm.append("""<Placemark><name>%s</name><description>%s</description>
<styleUrl>#event</styleUrl><Point><altitudeMode>absolute</altitudeMode>
<coordinates>%s</coordinates></Point></Placemark>""" % (
            name, desc, _coord(lon, lat, alt)))
    # 分离点（船站/轨道舱/推进舱分离）
    for e in separations or []:
        lat = e.get("lat_deg")
        lon = e.get("lon_deg")
        if lat is None or lon is None:
            continue
        alt = float(e.get("alt_km") or 0.0) * 1000.0
        name = html.escape(str(e.get("event") or "分离点"))
        desc = html.escape("阶段：%s\nUTC：%s\n%s" % (
            e.get("phase") or "", e.get("t_utc") or "", e.get("note") or ""))
        pm.append("""<Placemark><name>%s</name><description>%s</description>
<styleUrl>#sep</styleUrl><Point><altitudeMode>absolute</altitudeMode>
<coordinates>%s</coordinates></Point></Placemark>""" % (
            name, desc, _coord(lon, lat, alt)))
    # 着陆场
    if site:
        pm.append("""<Placemark><name>%s</name><styleUrl>#site</styleUrl>
<Point><coordinates>%s</coordinates></Point></Placemark>""" % (
            html.escape(str(site.get("name") or "着陆场")),
            _coord(site["lon_deg"], site["lat_deg"], 0.0)))
        if site.get("radius_km"):
            ring = []
            for k in range(73):
                la, lo, _az = geo.geodesic_direct(float(site["lat_deg"]),
                                                  float(site["lon_deg"]),
                                                  k * 5.0, float(site["radius_km"]))
                ring.append(_coord(lo, la, 0.0))
            pm.append("""<Placemark><name>着陆场范围 R=%s km</name><styleUrl>#zone</styleUrl>
<Polygon><tessellate>1</tessellate><altitudeMode>clampToGround</altitudeMode>
<outerBoundaryIs><LinearRing><coordinates>%s</coordinates></LinearRing></outerBoundaryIs>
</Polygon></Placemark>""" % (_fmt_num(site.get("radius_km")), "\n".join(ring)))
    # 返回航警区
    for z in zones or []:
        poly = z.get("polygon_latlon") or []
        if len(poly) < 3:
            continue
        ring = [_coord(p[1], p[0], 0.0) for p in poly]
        ring.append(ring[0])
        pm.append("""<Placemark><name>%s（返回航警）</name><styleUrl>#zone</styleUrl>
<description>B) %s / C) %s</description>
<Polygon><tessellate>1</tessellate><altitudeMode>clampToGround</altitudeMode>
<outerBoundaryIs><LinearRing><coordinates>%s</coordinates></LinearRing></outerBoundaryIs>
</Polygon></Placemark>""" % (html.escape(str(z.get("id") or "航警区")),
                            html.escape(str(z.get("b_utc") or "")),
                            html.escape(str(z.get("c_utc") or "")), "\n".join(ring)))
    kml = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
<name>返回舱返回弹道</name>
<Style id="ground"><LineStyle><color>ff00ffff</color><width>3</width></LineStyle></Style>
<Style id="air"><LineStyle><color>ff00aaff</color><width>4</width></LineStyle></Style>
<Style id="station"><LineStyle><color>ff00ff00</color><width>2</width></LineStyle></Style>
<Style id="spacecraft"><LineStyle><color>ffff00ff</color><width>2</width></LineStyle></Style>
<Style id="event"><IconStyle><color>ff0000ff</color><scale>0.8</scale></IconStyle></Style>
<Style id="sep"><IconStyle><color>ffff00ff</color><scale>1.0</scale></IconStyle></Style>
<Style id="site"><IconStyle><color>ff00ff00</color><scale>1.2</scale></IconStyle></Style>
<Style id="zone"><LineStyle><color>ff0000ff</color><width>2</width></LineStyle>
<PolyStyle><color>330000ff</color></PolyStyle></Style>
%s
</Document></kml>
""" % "\n".join(pm)
    kml_path = os.path.join(out_dir, "返回舱弹道.kml")
    kmz_path = os.path.join(out_dir, "返回舱弹道.kmz")
    with open(kml_path, "w", encoding="utf-8") as f:
        f.write(kml)
    with zipfile.ZipFile(kmz_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", kml)
    return kml_path, kmz_path


def _fmt_num(v):
    try:
        return ("%g" % float(v))
    except Exception:  # noqa: BLE001
        return str(v)


def _plot_landing_map(det, site, zones, disp, out_png):
    """落区图：返回航警区/着陆场/落点/1σ 椭圆 + 末段星下点。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from common import plotstyle
        plotstyle.setup()
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    tr = det.get("trajectory") or []
    if not tr:
        return "无轨迹"
    fig, ax = plt.subplots(figsize=(12.4, 10.2))
    # 末段星下点（开伞后高度 < 30 km）
    seg = [x for x in tr if float(x.get("alt_km") or 0.0) <= 30.0]
    if len(seg) > 2:
        ax.plot([x["lon_deg"] for x in seg], [x["lat_deg"] for x in seg],
                color="#555555", lw=1.0, alpha=0.7, label="末段星下点")
    landing = det.get("landing") or {}
    if landing:
        ax.plot([landing["lon_deg"]], [landing["lat_deg"]], marker="*", ms=20,
                mfc="#e60000", mec="white", mew=1.0, ls="none", zorder=8,
                label="预测落点")
    if site:
        ax.plot([site["lon_deg"]], [site["lat_deg"]], marker="+", ms=14,
                color="#0a8f2d", mew=2.0, ls="none", zorder=8,
                label="着陆场中心")
        if site.get("radius_km"):
            ring = []
            for k in range(73):
                la, lo, _az = geo.geodesic_direct(float(site["lat_deg"]),
                                                  float(site["lon_deg"]),
                                                  k * 5.0, float(site["radius_km"]))
                ring.append((lo, la))
            ax.plot([p[0] for p in ring], [p[1] for p in ring],
                    color="#0a8f2d", lw=1.2, ls="--", zorder=6,
                    label="着陆场 R=%s km" % _fmt_num(site.get("radius_km")))
    for z in zones or []:
        poly = z.get("polygon_latlon") or []
        if len(poly) < 3:
            continue
        xs = [p[1] for p in poly] + [poly[0][1]]
        ys = [p[0] for p in poly] + [poly[0][0]]
        ax.plot(xs, ys, color="#7a3b00", lw=1.8, zorder=5,
                label="返回航警 %s" % (z.get("id") or ""))
        ax.fill(xs, ys, color="#7a3b00", alpha=0.10, zorder=4)
    if disp:
        lat0 = float(disp.get("center_lat") or 0.0)
        lon0 = float(disp.get("center_lon") or 0.0)
        a = float(disp.get("semi_major_km") or 0.0)
        b = float(disp.get("semi_minor_km") or 0.0)
        ang = math.radians(float(disp.get("orientation_deg") or 0.0))
        kx = 111.32 * max(0.2, math.cos(math.radians(lat0)))
        th = np.linspace(0, 2 * math.pi, 180)
        ex = a * np.cos(th) * math.cos(ang) - b * np.sin(th) * math.sin(ang)
        ey = a * np.cos(th) * math.sin(ang) + b * np.sin(th) * math.cos(ang)
        ax.plot(lon0 + ex / kx, lat0 + ey / 111.32, color="#d17a00", lw=1.6,
                zorder=7, label="1σ 落点椭圆")
    if landing:
        ax.set_xlim(float(landing["lon_deg"]) - 1.8, float(landing["lon_deg"]) + 1.8)
        ax.set_ylim(float(landing["lat_deg"]) - 1.4, float(landing["lat_deg"]) + 1.4)
    ax.set_xlabel("经度 (°E)")
    ax.set_ylabel("纬度 (°N)")
    ax.set_title("返回舱落区：落点、着陆场与返回航警区")
    ax.grid(True, ls=":", lw=0.5, alpha=0.6)
    ax.legend(fontsize=7.5, loc="best")
    ax.set_aspect(1.0 / max(0.2, math.cos(math.radians(
        float(landing.get("lat_deg") or 42.0)))))
    fig.tight_layout()
    _old_bbox = matplotlib.rcParams.get("savefig.bbox")
    matplotlib.rcParams["savefig.bbox"] = "standard"
    try:
        fig.savefig(out_png, dpi=150, facecolor="white")
    finally:
        if _old_bbox is not None:
            matplotlib.rcParams["savefig.bbox"] = _old_bbox
    plt.close(fig)
    return ""


def _write_return_windows(path, windows):
    rows = []
    for w in windows or []:
        rows.append([w.get("start_utc"), w.get("end_utc"), w.get("duration_s"),
                     w.get("best_deorbit_utc"), w.get("best_landing_utc"),
                     w.get("landing_lat_deg"), w.get("landing_lon_deg"),
                     w.get("zone_id"), w.get("inside_zone"), w.get("notam_time_ok"),
                     w.get("dist_to_zone_km"), w.get("deorbit_dv_mps"),
                     w.get("bank_deg"), w.get("flight_time_s")])
    _write_csv(path, ["start_utc", "end_utc", "duration_s", "best_deorbit_utc",
                      "best_landing_utc", "landing_lat_deg", "landing_lon_deg",
                      "zone_id", "inside_zone", "notam_time_ok", "dist_to_zone_km",
                      "deorbit_dv_mps", "bank_deg", "flight_time_s"], rows)


def _event_at_station(src, t, label, phase, note="", separation=False, t_ref=None):
    """用空间站星下点生成分离/相位事件。"""
    r, v = src.state(t)
    la, lo, _al = O.subpoint(t, r)
    t_met = (t - t_ref).total_seconds() if t_ref else 0.0
    return dict(event=label, phase=phase, t_utc=_utc_text(t), t_met_s=round(t_met, 2),
                lat_deg=float(la), lon_deg=float(lo),
                alt_km=float(np.linalg.norm(r)) - 6378.137,
                speed_m_s=float(np.linalg.norm(v)) * 1000.0,
                separation=bool(separation), note=note)


def _event_from_track(det, t_utc, label, phase, note="", separation=False):
    """从再入时序里找最接近 t_utc 的点，生成分离/相位事件。"""
    tr = det.get("trajectory") or []
    if not tr:
        return None
    t_target = _parse_iso(t_utc)
    if t_target is None:
        return None
    best = min(tr, key=lambda x: abs((_parse_iso(x.get("t_utc")) or t_target) - t_target))
    return dict(event=label, phase=phase, t_utc=best.get("t_utc"),
                t_met_s=float(best.get("t_met_s") or 0.0),
                lat_deg=float(best.get("lat_deg") or 0.0),
                lon_deg=float(best.get("lon_deg") or 0.0),
                alt_km=float(best.get("alt_km") or 0.0),
                speed_m_s=float(best.get("speed_km_s") or 0.0) * 1000.0,
                separation=bool(separation), note=note)


def _build_return_phases(src, t_deorbit, det, rc):
    """返回任务阶段与分离点（船站分离/轨道舱分离/制动/推进舱分离/再入/开伞/着陆）。

    说明：仿真从制动时刻开始；船站分离、轨道舱分离早于制动，按空间站轨道给出星下点；
    推进舱分离在制动后滑行段，按再入时序插值给出位置。分离时刻是工程阶段标记，
    不是精确的分离动力学。
    """
    undock_lead = float(rc.get("undock_lead_s") or 3600.0)
    orb_lead = float(rc.get("orbital_sep_lead_s") or 600.0)
    burn_s = float(rc.get("burn_duration_s") or 150.0)
    prop_delay = float(rc.get("prop_sep_delay_s") or 300.0)
    undock_t = t_deorbit - timedelta(seconds=undock_lead)
    orb_t = t_deorbit - timedelta(seconds=orb_lead)
    if orb_t <= undock_t:
        orb_t = undock_t + timedelta(seconds=60)
    burn_end = t_deorbit + timedelta(seconds=burn_s)
    prop_t = t_deorbit + timedelta(seconds=prop_delay)
    ev = []
    try:
        ev.append(_event_at_station(src, undock_t, "船站分离", "分离",
                                    "与空间站分离，进入独立飞行", True,
                                    t_ref=t_deorbit))
        ev.append(_event_at_station(src, orb_t, "轨道舱分离", "分离",
                                    "轨道舱与返回舱/推进舱分离（按空间站轨道近似）", True,
                                    t_ref=t_deorbit))
        ev.append(_event_at_station(src, t_deorbit, "制动开始", "制动",
                                    "反向制动 ΔV（模型按瞬时制动）", False,
                                    t_ref=t_deorbit))
        _be = _event_at_station(src, burn_end, "制动结束", "制动",
                                "阶段标记；模型未积分有限推力弧段", False,
                                t_ref=t_deorbit)
        ev.append(_be)
        _pe = _event_from_track(det, _utc_text(prop_t), "推进舱分离", "分离",
                                "推进舱分离，返回舱单独再入（位置按滑行段轨迹插值）", True)
        if _pe:
            ev.append(_pe)
    except Exception as exc:  # noqa: BLE001
        return [], [], str(exc)
    # 再入/开伞/着陆来自完整仿真
    for e in det.get("events") or []:
        ev.append(dict(event=e.get("event"), phase=("再入" if "再入" in str(e.get("event"))
                                                   else ("伞降" if "伞" in str(e.get("event"))
                                                         else "着陆")),
                      t_utc=e.get("t_utc"), t_met_s=float(e.get("t_met_s") or 0.0),
                      lat_deg=e.get("lat"), lon_deg=e.get("lon"),
                      alt_km=e.get("alt_km"), speed_m_s=e.get("speed_m_s"),
                      separation=False, note=""))
    # 去重/按时间排序
    ev = [x for x in ev if x.get("t_utc")]
    ev.sort(key=lambda x: x.get("t_utc") or "")
    seps = [x for x in ev if x.get("separation")]
    # 阶段表
    phases = []
    marks = [("停泊/分离准备", undock_t, orb_t, "船站分离→轨道舱分离"),
             ("独立飞行/制动准备", orb_t, t_deorbit, "轨道舱分离→制动开始"),
             ("制动", t_deorbit, burn_end, "反向制动 ΔV"),
             ("自由滑行", burn_end, _parse_iso((det.get("events") or [{}])[0].get("t_utc"))
              or burn_end, "制动结束→再入界面"),
             ("再入", _parse_iso((det.get("events") or [{}])[0].get("t_utc")) or burn_end,
              _parse_iso((det.get("events") or [{}])[1].get("t_utc")) if len(
                  det.get("events") or []) > 1 else None, "再入界面→减速伞"),
             ("伞降", _parse_iso((det.get("events") or [{}])[1].get("t_utc")) if len(
                 det.get("events") or []) > 1 else None,
              _parse_iso((det.get("events") or [{}])[-1].get("t_utc")) if det.get("events")
              else None, "减速伞/主伞→着陆"),
             ("着陆", _parse_iso((det.get("events") or [{}])[-1].get("t_utc")) if det.get("events")
              else None, None, "着陆后")]
    for name, st, en, note in marks:
        if st is None:
            continue
        dur = ((en - st).total_seconds() if en else None)
        phases.append(dict(phase=name, start_utc=_utc_text(st),
                           end_utc=_utc_text(en) if en else "",
                           duration_s=round(dur, 1) if dur is not None else None,
                           note=note))
    return ev, seps, phases


def _write_phase_csv(path, events):
    _write_csv(path, ["event", "phase", "t_utc", "t_met_s", "lat_deg", "lon_deg",
                      "alt_km", "speed_m_s", "separation", "note"],
               [[e.get("event"), e.get("phase"), e.get("t_utc"), e.get("t_met_s"),
                 e.get("lat_deg"), e.get("lon_deg"), e.get("alt_km"),
                 e.get("speed_m_s"), e.get("separation"), e.get("note")] for e in events])


def _write_phases_csv(path, phases):
    _write_csv(path, ["phase", "start_utc", "end_utc", "duration_s", "note"],
               [[p.get("phase"), p.get("start_utc"), p.get("end_utc"),
                 p.get("duration_s"), p.get("note")] for p in phases])


def _public_eval(ev):
    return {k: v for k, v in ev.items() if k not in ("events", "_t_dt")}


def _plot_return(det, site, zones, out_png):
    """返回弹道图：左=星下点地面轨迹，右=高度-时间。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from common import plotstyle
        plotstyle.setup()
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    tr = det.get("trajectory") or []
    if len(tr) < 2:
        return "轨迹点不足"
    lat = np.array([x["lat_deg"] for x in tr], dtype=float)
    lon = np.array([x["lon_deg"] for x in tr], dtype=float)
    alt = np.array([x["alt_km"] for x in tr], dtype=float)
    tt = np.array([x["t_met_s"] for x in tr], dtype=float)
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13.2, 6.2),
                                   gridspec_kw={"width_ratios": [1.05, 1.0]})
    # 地面轨迹（经度做 360° 展开，避免跨日界线折返）
    x = lon.copy()
    for i in range(1, len(x)):
        while x[i] - x[i - 1] > 180:
            x[i] -= 360
        while x[i] - x[i - 1] < -180:
            x[i] += 360
    sc = ax1.scatter(x, lat, c=tt, s=9, cmap="turbo", zorder=4)
    ax1.plot(x, lat, color="#444444", lw=0.8, alpha=0.6, zorder=3)
    cb = fig.colorbar(sc, ax=ax1, pad=0.02, fraction=0.04)
    cb.set_label("MET (s)", fontsize=9)
    ax1.plot([site["lon_deg"]], [site["lat_deg"]], marker="*", ms=18,
             mfc="#e60000", mec="white", ls="none", zorder=8,
             label="着陆场 %s" % site.get("name", ""))
    if site.get("radius_km"):
        # 纬度圈近似：1° lat ≈ 111.32 km
        rlat = float(site["radius_km"]) / 111.32
        rlon = rlat / max(0.2, math.cos(math.radians(site["lat_deg"])))
        th = np.linspace(0, 2 * math.pi, 120)
        ax1.plot(site["lon_deg"] + rlon * np.cos(th),
                 site["lat_deg"] + rlat * np.sin(th),
                 color="#e60000", lw=1.2, ls="--", zorder=7)
    for z in zones or []:
        poly = z.get("polygon_latlon") or []
        if len(poly) >= 3:
            pp = np.asarray(poly, dtype=float)
            ax1.plot(np.r_[pp[:, 1], pp[0, 1]], np.r_[pp[:, 0], pp[0, 0]],
                     color="#7a3b00", lw=1.6, zorder=7)
            ax1.annotate(str(z.get("id", "航警区")), (float(pp[0, 1]), float(pp[0, 0])),
                         fontsize=8, color="#7a3b00")
    for e in det.get("events") or []:
        if e.get("lat") is not None and e.get("lon") is not None:
            ax1.plot([e["lon"]], [e["lat"]], "o", ms=6, mfc="white",
                     mec="#222222", mew=1.0, zorder=9)
            ax1.annotate("%s\nT+%.0f s" % (e.get("event", ""), e.get("t_met_s", 0)),
                         (e["lon"], e["lat"]), textcoords="offset points",
                         xytext=(5, 5), fontsize=7.2, zorder=9)
    ax1.set_xlabel("经度 (°E)")
    ax1.set_ylabel("纬度 (°N)")
    ax1.set_title("返回舱星下点地面轨迹（颜色=MET）")
    ax1.grid(True, ls=":", lw=0.5, alpha=0.6)
    ax1.legend(fontsize=8, loc="best")
    ax2.plot(tt, alt, color="#8B4513", lw=1.8)
    for e in det.get("events") or []:
        ax2.axvline(float(e.get("t_met_s") or 0), color="#888888", lw=0.8, ls=":")
        ax2.annotate(e.get("event", ""), (float(e.get("t_met_s") or 0), 0),
                     rotation=90, fontsize=7, va="bottom", ha="right")
    ax2.set_xlabel("MET (s)")
    ax2.set_ylabel("高度 (km)")
    ax2.set_title("返回舱高度-时间（制动→再入→开伞→着陆）")
    ax2.grid(True, ls=":", lw=0.5, alpha=0.6)
    fig.tight_layout()
    plotstyle.save(fig, out_png, dpi=150)
    plt.close(fig)
    return ""


def _finish(out, sim_dir, code):
    out["generated_utc"] = O.utc_text(datetime.now(timezone.utc))
    path = os.path.join(sim_dir, "返回舱返回.json")
    os.makedirs(sim_dir, exist_ok=True)
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2,
              default=str)
    _log("[return] → %s" % path)
    return code


if __name__ == "__main__":
    sys.exit(main())
