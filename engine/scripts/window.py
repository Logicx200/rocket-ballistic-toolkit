# -*- coding: utf-8 -*-
"""发射窗口计算（v3.4）：航警时窗 + 轨道平面窗口 + CZ-2F 交会窗口。

三部分
------
1. **航警时窗**（v3.0 原有）：由航警 B)/C) 与各残骸到达时刻反推允许的 T0 区间，
   所有区取交集，再扣除发射准备裕度（``config.window.margin_s``）。
2. **轨道平面窗口**（v3.4 新增）：已知目标轨道（RAAN 或太阳同步 LTAN）或已知交会
   目标（空间站）时，用「入轨点相对速度 + 地球自转」构造入轨轨道面，求
   ``|RAAN_achieved(T0) − RAAN_target(T0)| ≤ tolerance`` 的时间窗。
3. **CZ-2F 交会窗口**：在平面窗口内再检查相位可行性——由入轨时刻空间站与飞船的
   纬度幅角差，用「相位轨道 + Hohmann」模型求交会方案；总 ΔV ≤ 预算且总时长
   ≤ 上限时窗口可用，并给出预计对接时刻。

输出：``report/发射窗口计算.json``（保持 v3.0 的 model_window / practical_window
字段不变，新增 ``orbit_window``），控制台打印摘要。

用法：python scripts/window.py --config config.json
"""
import argparse
import csv
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("RK_ROOT") or os.path.dirname(HERE)

# --- v3 引擎/工作区解耦（自动补丁） ---
_ENG = os.environ.get("RK_ENGINE")
if _ENG:
    for _p in (_ENG, os.path.join(_ENG, "scripts")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
# --- end ---
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)
import numpy as np

import dynamics as D
from common import geo, notam as N
from common import orbit as O

try:
    from common import station as ST
except Exception:  # noqa: BLE001
    ST = None


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def utc(dt):
    return O.utc_text(dt)


def bjt(dt):
    return O.bjt_text(dt)


def _wrap180(x):
    return O.wrap180(x)


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default if default is not None else {}


def _read_csv(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    except Exception:  # noqa: BLE001
        return []


def _sun_el(dt, lat, lon):
    try:
        return float(geo.solar_elevation_deg(lat, lon, dt))
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# 1. 航警时窗（保持 v3.0 口径）
# --------------------------------------------------------------------------- #
def notam_window(cfg, t0_nom, sim_dir):
    """返回 (result_dict|None, note) —— 结构与 v3.0 完全兼容。"""
    imp_path = os.path.join(sim_dir, "落点与射程.csv")
    imps = _read_csv(imp_path)
    if not imps or not (cfg.get("notams") or []):
        return None, "无航警/无落点表，跳过航警时窗"
    margin = float((cfg.get("window") or {}).get("margin_s", 60.0))
    stage_names = [s.get("name", "") for s in cfg["vehicle"]["stages"]]
    obj_zone = {}
    for z in cfg.get("notams", []):
        hint = z.get("stage_hint", "")
        obj = z.get("object_hint", "")
        if not hint and not obj:
            continue
        targets = (N.object_hint_targets(obj, stage_names) if obj
                   else N.hint_object_targets(hint, stage_names))
        if not targets:
            print("[window] 警告：航警 %s 的残骸类型「%s」无法识别，该区不进窗口约束"
                  % (z.get("id", "?"), hint))
            continue
        for t in targets:
            obj_zone[t] = z["id"]

    def _zone_of(obj_name):
        if obj_name in obj_zone:
            return obj_zone[obj_name]
        for k, v in obj_zone.items():
            if str(obj_name).startswith(k):
                return v
        return None

    lo = None
    hi = None
    detail = []
    for im in imps:
        zid = im.get("zone_id") or _zone_of(im["object"])
        z = next((x for x in cfg.get("notams", []) if x["id"] == zid), None)
        if z is None:
            continue
        t_arr = float(im["impact_t_met_s"])
        b = datetime.fromisoformat(z["b_utc"].replace("Z", "+00:00"))
        c = datetime.fromisoformat(z["c_utc"].replace("Z", "+00:00"))
        cand_lo = b - timedelta(seconds=t_arr)
        cand_hi = c - timedelta(seconds=t_arr)
        lo = cand_lo if lo is None else max(lo, cand_lo)
        hi = cand_hi if hi is None else min(hi, cand_hi)
        detail.append(dict(object=im["object"], zone_id=zid, t_arrival_met_s=t_arr,
                           t_arrival_utc=utc(t0_nom + timedelta(seconds=t_arr)),
                           allowed_t0_from=utc(cand_lo), allowed_t0_to=utc(cand_hi)))
    if lo is None or hi is None:
        return None, "没有可与落点配对的航警区"
    if hi <= lo:
        return dict(t0_nominal_utc=cfg["mission"]["t0_utc"], margin_s=margin,
                    model_window=None, practical_window=None,
                    constraints=detail, feasible=False,
                    reason="航警有效期与残骸到达时刻互斥（交集为空）",
                    notam_offsets={z["id"]: dict(
                        b_minus_t0_min=round((datetime.fromisoformat(
                            z["b_utc"].replace("Z", "+00:00")) - t0_nom).total_seconds() / 60.0, 2),
                        c_minus_t0_min=round((datetime.fromisoformat(
                            z["c_utc"].replace("Z", "+00:00")) - t0_nom).total_seconds() / 60.0, 2))
                        for z in cfg.get("notams", [])},
                    observer_daylight=None,
                    note="窗口 = 航警有效期与各残骸到达时刻反推的交集；本任务交集为空。"), \
            "航警时窗无解"
    lo_m = lo + timedelta(seconds=margin)
    hi_m = hi - timedelta(seconds=margin)
    dur = (hi - lo).total_seconds()
    dur_m = (hi_m - lo_m).total_seconds()

    obs_info = None
    obs_list = cfg.get("observers") or []
    if obs_list:
        o = obs_list[0]
        try:
            el = geo.solar_elevation_deg(t0_nom, o["lat_deg"], o["lon_deg"])
            obs_info = dict(observer=o["id"], sun_el_t0_deg=round(float(el), 2))
        except Exception:  # noqa: BLE001
            obs_info = None

    return dict(
        t0_nominal_utc=cfg["mission"]["t0_utc"], margin_s=margin,
        model_window=dict(start_utc=utc(lo), end_utc=utc(hi),
                          duration_min=round(dur / 60.0, 2),
                          start_bjt=bjt(lo), end_bjt=bjt(hi)),
        practical_window=dict(start_utc=utc(lo_m), end_utc=utc(hi_m),
                              duration_min=round(dur_m / 60.0, 2),
                              start_bjt=bjt(lo_m), end_bjt=bjt(hi_m)),
        constraints=detail,
        notam_offsets={z["id"]: dict(
            b_minus_t0_min=round((datetime.fromisoformat(
                z["b_utc"].replace("Z", "+00:00")) - t0_nom).total_seconds() / 60.0, 2),
            c_minus_t0_min=round((datetime.fromisoformat(
                z["c_utc"].replace("Z", "+00:00")) - t0_nom).total_seconds() / 60.0, 2))
            for z in cfg.get("notams", [])},
        observer_daylight=obs_info,
        feasible=True,
        note="窗口 = 航警有效期与各残骸到达时刻反推的交集；practical = model ∓ margin。"
             "轨道平面/交会约束见 orbit_window。"), "航警时窗"


# --------------------------------------------------------------------------- #
# 2. 入轨点状态（从 sim/trajectory.csv 反算）
# --------------------------------------------------------------------------- #
def insertion_state(cfg, sim_dir, t0_nom):
    """从 sim 产物恢复入轨点惯性状态；失败返回 None。"""
    orb = _read_json(os.path.join(sim_dir, "入轨根数.json"), {})
    meta = _read_json(os.path.join(sim_dir, "trajectory_meta.json"), {})
    t_ins = None
    if orb.get("insertion"):
        t_ins = orb["insertion"].get("t_met_s")
    if t_ins is None:
        t_ins = meta.get("insertion_t_met_s")
    rows = _read_csv(os.path.join(sim_dir, "trajectory.csv"))
    if not rows or t_ins is None:
        return None
    row = min(rows, key=lambda r: abs(float(r.get("t_met_s") or 0.0) - float(t_ins)))
    try:
        t_met = float(row["t_met_s"])
        lat = float(row["lat_deg"])
        lon = float(row["lon_deg"])
        alt_km = float(row["alt_m"]) / 1000.0
        vrel = np.array([float(row["vx_ecef"]), float(row["vy_ecef"]),
                         float(row["vz_ecef"])], dtype=float) / 1000.0
    except (KeyError, TypeError, ValueError):
        return None
    dt = t0_nom + timedelta(seconds=t_met)
    r_ecef = np.asarray(geo.geodetic_to_ecef(lat, lon, alt_km * 1000.0),
                        dtype=float) / 1000.0
    omega = 2.0 * math.pi / 86164.0905
    v_abs_ecef = vrel + omega * np.array([-r_ecef[1], r_ecef[0], 0.0])
    th = math.radians(geo.gmst_deg(dt))
    c, s = math.cos(th), math.sin(th)
    R = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
    r_eci = R @ r_ecef
    v_eci = R @ v_abs_ecef
    el = O.elements_from_state(r_eci, v_eci)
    # 入轨点当地 ENU 下的相对速度方向（方位角/飞行路径角）
    east, north, up = O._site_enu(lat, lon)
    v_n = float(np.dot(vrel, north))
    v_e = float(np.dot(vrel, east))
    v_u = float(np.dot(vrel, up))
    az = math.degrees(math.atan2(v_e, v_n)) % 360.0
    fpa = math.degrees(math.asin(max(-1.0, min(1.0, v_u / max(1e-9, np.linalg.norm(vrel))))))
    return dict(t_met_s=t_met, dt=dt, lat=lat, lon=lon, alt_km=alt_km,
                vrel_kms=float(np.linalg.norm(vrel)), az_deg=az, fpa_deg=fpa,
                r_eci=r_eci, v_eci=v_eci, elements=el,
                source="sim/trajectory.csv 入轨点（相对速度 + 地球自转）")


def _fallback_insertion(cfg, t0_nom):
    """无 sim 时用发射场 + 参考轨道近似（低置信度）。"""
    m = cfg.get("mission") or {}
    site = m.get("launch_site") or {}
    ref = m.get("reference_orbit") or {}
    alt = ref.get("alt_km")
    if alt is None:
        return None
    try:
        t_ins = float((cfg.get("guidance") or {}).get("t_seco") or 580.0)
    except (TypeError, ValueError):
        t_ins = 580.0
    speed = math.sqrt(O.MU_EARTH / (O.RE_EQ + float(alt)))
    az = (cfg.get("guidance") or {}).get("azimuth_deg")
    if az is None:
        return None
    dt = t0_nom + timedelta(seconds=t_ins)
    el = O.plane_from_launch(site["lat_deg"], site["lon_deg"], float(alt), float(az),
                             speed, dt, 0.0)
    return dict(t_met_s=t_ins, dt=dt, lat=site["lat_deg"], lon=site["lon_deg"],
                alt_km=float(alt), vrel_kms=speed, az_deg=float(az), fpa_deg=0.0,
                r_eci=el["r_eci"], v_eci=el["v_eci"], elements=el,
                source="发射场 + 参考轨道近似（无 sim 弹道，低置信度）")


# --------------------------------------------------------------------------- #
# 3. 目标轨道/交会目标
# --------------------------------------------------------------------------- #
def resolve_target(cfg, ins, t0_nom, allow_network=True):
    """返回目标描述 dict；无平面约束时 kind='free'。"""
    lw = dict(cfg.get("launch_window") or {})
    m = cfg.get("mission") or {}
    ref = dict(m.get("reference_orbit") or {})
    mode = str(lw.get("mode") or "auto").lower()
    st_cfg = dict(cfg.get("station") or {})
    if st_cfg.get("enabled") and mode in ("auto", "station", "cz2f"):
        if ST is None:
            return dict(kind="station", available=False,
                        reason="空间站模块不可用（engine/common/station.py 缺失）")
        try:
            src = ST.resolve(cfg, t_hint=t0_nom, allow_network=allow_network,
                             logger=lambda s: print(s))
            t_ins = ins["dt"] if ins else t0_nom
            el = src.elements(t_ins)
            return dict(kind="station", available=True, name=src.name,
                        norad_id=src.norad_id, source=src.info(),
                        i_deg=el["i_deg"], source_obj=src,
                        tolerance_deg=float(lw.get("tolerance_deg") or 0.2))
        except Exception as exc:  # noqa: BLE001
            return dict(kind="station", available=False, reason=str(exc))
    # 显式 RAAN
    if lw.get("raan_deg") is not None and mode in ("auto", "raan"):
        epoch = lw.get("raan_epoch_utc") or m.get("t0_utc")
        return dict(kind="raan", available=True,
                    raan_deg=float(lw["raan_deg"]),
                    epoch=O.parse_iso(epoch) if epoch else t0_nom,
                    i_deg=float(lw.get("i_deg") or ref.get("i_deg") or 0.0),
                    alt_km=float(lw.get("alt_km") or ref.get("alt_km") or 0.0),
                    tolerance_deg=float(lw.get("tolerance_deg") or 0.2),
                    source="用户给定 RAAN")
    # 显式 LTAN
    if lw.get("ltan_h") is not None and mode in ("auto", "ltan"):
        return dict(kind="ltan", available=True, ltan_h=float(lw["ltan_h"]),
                    i_deg=float(lw.get("i_deg") or ref.get("i_deg") or 0.0),
                    alt_km=float(lw.get("alt_km") or ref.get("alt_km") or 0.0),
                    tolerance_deg=float(lw.get("tolerance_deg") or 0.2),
                    source="用户给定太阳同步 LTAN")
    # 太阳同步轨道 → 由标称 T0 反推 LTAN
    i_ref = ref.get("i_deg")
    if i_ref is not None and 95.0 <= float(i_ref) <= 105.0 and ins is not None:
        ltan = O.ltan_from_raan(ins["dt"], ins["elements"]["raan_deg"])
        return dict(kind="ltan", available=True, ltan_h=float(ltan),
                    i_deg=float(i_ref),
                    alt_km=float(ref.get("alt_km") or ins["alt_km"]),
                    tolerance_deg=float(lw.get("tolerance_deg") or 0.2),
                    source="由标称 T0 的入轨轨道面反推 LTAN")
    return dict(kind="free", available=False,
                reason="目标轨道只给了高度/倾角，没有 RAAN、LTAN 或交会目标："
                       "轨道面可取任意方向，不构成平面窗口约束；窗口由航警/光照/准备条件决定。")


def target_raan(target, dt_ins):
    """给定入轨时刻，返回 (目标 RAAN, 目标倾角, 附加信息)。"""
    kind = target.get("kind")
    if kind == "station":
        el = target["source_obj"].elements(dt_ins)
        return el["raan_deg"], el["i_deg"], dict(u_deg=el["u_deg"],
                                                 alt_km=el["a_km"] - O.RE_EQ,
                                                 station_raan_deg=el["raan_deg"])
    if kind == "raan":
        epoch = target["epoch"]
        a = None
        try:
            a = (O.RE_EQ + float(target.get("alt_km") or 0.0)) if target.get("alt_km") else None
        except (TypeError, ValueError):
            a = None
        rate = O.j2_raan_rate_deg_day(a, 0.0, float(target["i_deg"])) if a else 0.0
        dt_days = (dt_ins - epoch).total_seconds() / 86400.0
        return (O.wrap360(float(target["raan_deg"]) + rate * dt_days),
                float(target["i_deg"]), dict(j2_raan_rate_deg_day=rate))
    if kind == "ltan":
        raan = O.raan_for_ltan(dt_ins, float(target["ltan_h"]))
        return raan, float(target["i_deg"]), dict(ltan_h=target["ltan_h"])
    raise ValueError("无平面约束目标")


# --------------------------------------------------------------------------- #
# 4. 平面窗口搜索
# --------------------------------------------------------------------------- #
def _golden_min(f, a, b, iters=40):
    """黄金分割求 |f| 极小点（a<b）。"""
    gr = (math.sqrt(5.0) - 1.0) / 2.0
    c = b - gr * (b - a)
    d = a + gr * (b - a)
    fc, fd = abs(f(c)), abs(f(d))
    for _ in range(iters):
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - gr * (b - a)
            fc = abs(f(c))
        else:
            a, c, fc = c, d, fd
            d = a + gr * (b - a)
            fd = abs(f(d))
    return a + (b - a) / 2.0


def _bisect_threshold(g, a, b, iters=50):
    """g(a)、g(b) 异号时求零点（用于窗口边界 |delta|-tol）。"""
    ga = g(a)
    gb = g(b)
    if ga == 0:
        return a
    if gb == 0:
        return b
    if ga * gb > 0:
        return a + (b - a) / 2.0
    for _ in range(iters):
        mid = a + (b - a) / 2.0
        gm = g(mid)
        if ga * gm <= 0:
            b, gb = mid, gm
        else:
            a, ga = mid, gm
    return a + (b - a) / 2.0


def search_plane_windows(delta_func, t_start, t_end, step_s, tol_deg):
    """在时间区间内找 ``|delta(t)| <= tol`` 的连续窗口，返回列表。

    delta_func 返回角差 [deg]（已 wrap 到 ±180）。
    """
    step = max(2.0, float(step_s))
    n = max(2, int((t_end - t_start).total_seconds() / step) + 1)
    times = [t_start + timedelta(seconds=step * i) for i in range(n)]
    vals = [float(delta_func(t)) for t in times]
    mask = [abs(v) <= tol_deg for v in vals]
    windows = []
    i = 0
    while i < len(mask):
        if not mask[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(mask) and mask[j + 1]:
            j += 1
        # 边界细化
        t_lo = times[i] if i == 0 else _bisect_threshold(
            lambda t: abs(float(delta_func(t))) - tol_deg, times[i - 1], times[i])
        t_hi = times[j] if j == len(mask) - 1 else _bisect_threshold(
            lambda t: abs(float(delta_func(t))) - tol_deg, times[j], times[j + 1])
        k = min(range(i, j + 1), key=lambda q: abs(vals[q]))
        a = times[max(0, k - 1)]
        b = times[min(len(times) - 1, k + 1)]
        t_best = _golden_min(lambda t: float(delta_func(t)), a, b) if b > a else times[k]
        windows.append(dict(start=t_lo, end=t_hi, best=t_best,
                            truncated_left=(i == 0), truncated_right=(j == len(mask) - 1),
                            d_raan_deg=float(delta_func(t_best))))
        i = j + 1
    return windows


# --------------------------------------------------------------------------- #
# 5. CZ-2F 交会可行性（相位 + 时长 + ΔV）
# --------------------------------------------------------------------------- #
def _rendezvous_feasibility(cfg, target, ins, t0, r_sc, v_sc):
    """在给定 T0 下计算交会方案；返回 dict 或 None。"""
    if target.get("kind") != "station":
        return None
    try:
        st = target["source_obj"]
        dt_ins = t0 + timedelta(seconds=float(ins["t_met_s"]))
        r_st, v_st = st.state(dt_ins)
        phase = O.relative_phase_deg(r_sc, v_sc, r_st, v_st)
        r_sc_n = float(np.linalg.norm(r_sc))
        r_st_n = float(np.linalg.norm(r_st))
        rd = ST.rendezvous_defaults(cfg)
        pref = float(rd.get("target_time_h") or 6.5) * 3600.0
        tol_t = max(600.0, 0.25 * pref)
        max_t = float(rd.get("max_phasing_h") or 12.0) * 3600.0
        budget = float(rd.get("dv_budget_mps") or 250.0)
        final_s = float(rd.get("final_approach_s") or 1800.0)
        final_dv = float(rd.get("final_approach_dv_mps") or 25.0)
        plan = O.phasing_plan_best(r_sc_n, r_st_n, phase,
                              max_orbits=int(rd.get("max_phasing_orbits") or 10),
                              min_orbits=int(rd.get("min_phasing_orbits") or 1),
                              preferred_s=pref, time_tol_s=tol_t,
                              max_dv_mps=budget, max_total_s=max_t,
                              final_dv_cap_mps=final_dv,
                              v_sc_kms=float(np.linalg.norm(v_sc)))
        if plan is None:
            return dict(feasible=False, phase_lead_deg=phase,
                        reason="没有满足 ΔV≤%.0f m/s、时长≤%.1f h 的相位调整方案"
                               % (budget, max_t / 3600.0))
        dock_t = dt_ins + timedelta(seconds=plan["total_s"] + final_s)
        # 可行性：ΔV 预算与最大相位时长（max_total_s 已在 phasing_plan 内过滤）。
        # within_target_time 只作信息标注——若最小 ΔV 方案比任务期望的 6.5 h 更短，
        # 说明相位条件更好，不应判为不可行。
        feasible = bool((plan["dv_total_mps"] + final_dv) <= budget)
        return dict(feasible=feasible, phase_lead_deg=round(phase, 3),
                    phasing_orbits=int(plan["k"]),
                    phasing_a_km=round(plan["phasing_a_km"], 2),
                    dv_total_mps=round(plan["dv_total_mps"] + final_dv, 1),
                    dv_phase_mps=round(plan["dv_phase_mps"], 1),
                    dv_transfer_mps=round(plan["dv_transfer_mps"], 1),
                    dv_circular_mps=round(plan["dv_circular_mps"], 1),
                    dv_final_mps=final_dv,
                    duration_h=round((plan["total_s"] + final_s) / 3600.0, 2),
                    within_target_time=bool(plan.get("within_time", True)),
                    dock_utc=utc(dock_t), dock_bjt=bjt(dock_t),
                    budget_mps=budget,
                    reason="" if feasible else "超出 ΔV 预算 %.0f m/s" % budget)
    except Exception as exc:  # noqa: BLE001
        return dict(feasible=False, reason="交会可行性计算失败：%s" % exc)


# --------------------------------------------------------------------------- #
# 6. 轨道窗口主流程
# --------------------------------------------------------------------------- #
def orbit_window(cfg, ins, t0_nom, allow_network=True):
    lw = dict(cfg.get("launch_window") or {})
    target = resolve_target(cfg, ins, t0_nom, allow_network=allow_network)
    tol = float(lw.get("tolerance_deg") or target.get("tolerance_deg") or 0.2)
    search_h = float(lw.get("search_hours") or 48.0)
    step_s = float(lw.get("step_s") or 20.0)
    base = dict(mode=str(lw.get("mode") or "auto"), tolerance_deg=tol,
                search_hours=search_h, step_s=step_s, target=target)
    # 输出前去掉不可 JSON 序列化的内部对象
    base["target"] = {k: v for k, v in target.items() if k != "source_obj"}
    if ins is None:
        base.update(available=False, reason="没有可用的入轨点状态（缺 sim/trajectory.csv）")
        return base
    if not target.get("available"):
        base.update(available=False, reason=target.get("reason") or "无平面约束")
        return base

    # v4.2：直接用**实测入轨平面**（sim 里由 export 递推得到），
    # 不再用"按目标倾角反解方位角"的理想平面替换——那会把制导平面残差
    # 吸收进 T0，导致窗口时刻与真实弹道不一致（v4.1 的 195 s 偏差即此）。
    i_t = float(target.get("i_deg") or 0.0)
    el_plane = dict(ins["elements"])
    raan0 = float(el_plane["raan_deg"])
    i_ach = float(el_plane["i_deg"])
    az_used = float(ins["az_deg"])
    ins_plane = dict(az_deg=az_used, elements=el_plane,
                     azimuth_corrected=False,
                     note="实测入轨平面（不替换为理想平面）")
    omega_deg_s = O.OMEGA_EARTH_DEG_DAY / 86400.0
    # 太阳同步任务：若 LTAN 是由标称 T0 反推的，改用“修正倾角后的轨道面”重算，
    # 避免仿真制导倾角偏差导致 LTAN 与最终平面不自洽。
    if target.get("kind") == "ltan" and "反推" in str(target.get("source") or ""):
        try:
            target["ltan_h"] = O.ltan_from_raan(ins["dt"], raan0)
        except Exception:  # noqa: BLE001
            pass

    def delta(t0):
        dt_ins = t0 + timedelta(seconds=float(ins["t_met_s"]))
        raan_ach = O.wrap360(raan0 + omega_deg_s * (t0 - t0_nom).total_seconds())
        raan_tgt, _i_tgt, _extra = target_raan(target, dt_ins)
        return O.wrap180(raan_ach - raan_tgt)

    t_start = t0_nom - timedelta(hours=search_h)
    t_end = t0_nom + timedelta(hours=search_h)
    # 空间站 OEM/TLE 覆盖有限：把搜索区间裁剪到数据覆盖范围内，避免越界失败
    try:
        if target.get("kind") == "station" and target.get("source_obj") is not None:
            cov = target["source_obj"].coverage()
            if cov:
                lead_s = float(ins.get("t_met_s") or 0.0)
                t_start = max(t_start, cov[0] - timedelta(seconds=lead_s) + timedelta(seconds=1))
                t_end = min(t_end, cov[1] - timedelta(seconds=lead_s) - timedelta(seconds=1))
    except Exception:  # noqa: BLE001
        pass
    if t_end <= t_start:
        base.update(available=False,
                    reason="搜索区间与空间站轨道数据覆盖区间不相交（%s ~ %s）"
                           % (utc(t_start), utc(t_end)))
        return base
    try:
        wins = search_plane_windows(delta, t_start, t_end, step_s, tol)
    except Exception as exc:  # noqa: BLE001
        base.update(available=False, reason="平面窗口搜索失败：%s" % exc)
        return base

    # v4.2：实测入轨倾角与目标的偏差不再直接判"不可达"——
    #   ≤0.5°：认为一致；
    #   0.5°~3°：记入 plane_residual_deg，窗口仍按 RAAN 对齐计算，倾角残差需末级修正；
    #   >3°：确实不可达。
    plane_residual = None
    if i_t > 0:
        plane_residual = abs(float(i_ach) - float(i_t))
        base["plane_residual_deg"] = round(plane_residual, 4)
        if plane_residual > 3.0:
            base.update(available=False,
                        reason="由入轨点无法达到目标倾角 %.3f°（可达 %.3f°，差 %.3f°）"
                               % (i_t, i_ach, plane_residual))
            return base
        if plane_residual > 0.5:
            base["plane_note"] = (
                "实测入轨倾角 %.3f° 与目标 %.3f° 相差 %.3f°；窗口按 RAAN 对齐计算，"
                "倾角残差需末级平面修正或反演处理（见报告 §4.4/§5.3）"
                % (i_ach, i_t, plane_residual))

    detailed = []
    for w in wins:
        t_best = w["best"]
        dt_ins = t_best + timedelta(seconds=float(ins["t_met_s"]))
        raan_tgt, i_tgt, extra = target_raan(target, dt_ins)
        raan_ach = O.wrap360(raan0 + omega_deg_s * (t_best - t0_nom).total_seconds())
        # 用修正方位角重算入轨点（RAAN 随 T0 平移，位置不变）
        el_best = dict(el_plane)
        el_best["raan_deg"] = raan_ach
        # 交会可行性
        r_sc = np.asarray(ins["r_eci"], dtype=float)
        v_sc = np.asarray(ins["v_eci"], dtype=float)
        # 把状态按 T0 平移（惯性系中整条弹道随地球自转旋转）
        # v4.2：旋转量按「候选 T0 − 仿真 T0」；旧代码用 − 入轨时刻，多转了 t_met（≈2.4°），
        # 会把每个窗口的相位/ΔV 判据整体偏掉。
        dth = math.radians(omega_deg_s * (t_best - t0_nom).total_seconds())
        c, s = math.cos(dth), math.sin(dth)
        Rz = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
        r_sc_t = Rz @ r_sc
        v_sc_t = Rz @ v_sc
        rv = _rendezvous_feasibility(cfg, target, ins, t_best, r_sc_t, v_sc_t) \
            if target.get("kind") == "station" else None
        # 航警/光照
        notam_ok = True
        for z in cfg.get("notams") or []:
            try:
                b = datetime.fromisoformat(z["b_utc"].replace("Z", "+00:00"))
                c2 = datetime.fromisoformat(z["c_utc"].replace("Z", "+00:00"))
                if not (b <= t_best <= c2):
                    notam_ok = False
                    break
            except Exception:  # noqa: BLE001
                continue
        site = (cfg.get("mission") or {}).get("launch_site") or {}
        sun_el = _sun_el(t_best, site.get("lat_deg", 0.0), site.get("lon_deg", 0.0)) \
            if site else None
        detailed.append(dict(
            start_utc=utc(w["start"]), end_utc=utc(w["end"]),
            start_bjt=bjt(w["start"]), end_bjt=bjt(w["end"]),
            duration_s=round((w["end"] - w["start"]).total_seconds(), 1),
            best_utc=utc(t_best), best_bjt=bjt(t_best),
            d_raan_deg=round(w["d_raan_deg"], 4),
            raan_ach_deg=round(raan_ach, 3),
            raan_target_deg=round(raan_tgt, 3),
            i_ach_deg=round(i_ach, 3), i_target_deg=round(i_tgt, 3),
            ltan_ach_h=round(O.ltan_from_raan(dt_ins, raan_ach), 3),
            ltan_target_h=round(O.ltan_from_raan(dt_ins, raan_tgt), 3),
            notam_ok=bool(notam_ok),
            launch_sun_el_deg=(round(sun_el, 2) if sun_el is not None else None),
            truncated_left=bool(w.get("truncated_left")),
            truncated_right=bool(w.get("truncated_right")),
            rendezvous=rv,
        ))
    base.update(available=bool(detailed), windows=detailed, n_windows=len(detailed),
                insertion=dict(t_met_s=ins["t_met_s"], utc=utc(ins["dt"]),
                               lat=round(ins["lat"], 5), lon=round(ins["lon"], 5),
                               alt_km=round(ins["alt_km"], 2),
                               v_rel_kms=round(ins["vrel_kms"], 4),
                               az_deg=round(az_used, 3),
                               az_sim_deg=round(ins["az_deg"], 3),
                               azimuth_corrected=bool(ins_plane["azimuth_corrected"]),
                               source=ins["source"]),
                achieved=dict(raan_deg=round(raan0, 3), i_deg=round(i_ach, 3),
                              r_eci=[round(float(x), 3) for x in ins["r_eci"]],
                              v_eci=[round(float(x), 3) for x in ins["v_eci"]]),
                assumptions=[
                    "入轨点取 sim 弹道 SECO 时刻状态；不同 T0 假设弹道相对地球固连、"
                    "仅整条轨迹随地球自转旋转。",
                    "目标倾角由入轨点方位角反解（含地球自转影响），以消除仿真制导倾角偏差。",
                    "平面窗口只约束 RAAN；实际任务还受航警、光照、测控、准备窗口约束。",
                ])
    if not detailed:
        base["reason"] = ("在 %+g h 搜索范围内没有满足 |ΔRAAN| ≤ %.2f° 的窗口"
                          % (search_h, tol))
    base["best_near_t0"] = min(
        detailed, key=lambda w: abs((O.parse_iso(w["best_utc"]) - t0_nom).total_seconds())
    ) if detailed else None
    return base


# --------------------------------------------------------------------------- #
# 主函数
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--allow-network", action="store_true")
    args = ap.parse_args()
    cfg = json.load(open(args.config, encoding="utf-8"))
    sim_dir = os.path.join(ROOT, "sim")
    t0_nom = datetime.fromisoformat(cfg["mission"]["t0_utc"].replace("Z", "+00:00"))
    allow_net = bool(args.allow_network or os.environ.get("RK_ALLOW_NETWORK") == "1")

    # ---- 航警时窗 ----
    nw, nw_note = notam_window(cfg, t0_nom, sim_dir)
    # ---- 轨道/交会窗口 ----
    ins = insertion_state(cfg, sim_dir, t0_nom) or _fallback_insertion(cfg, t0_nom)
    ow = orbit_window(cfg, ins, t0_nom, allow_network=allow_net)

    res = {}
    if nw:
        res.update(nw)
    else:
        res.update(t0_nominal_utc=cfg["mission"]["t0_utc"],
                   model_window=None, practical_window=None, constraints=[],
                   notam_offsets={}, observer_daylight=None,
                   note=nw_note)
    res["orbit_window"] = ow
    res["generated_utc"] = O.utc_text(datetime.now(timezone.utc))
    res["app_version"] = "3.4.0"
    res["note"] = (res.get("note") or "") + \
        " 轨道平面窗口与交会窗口见 orbit_window；仅为公开信息分析，不是飞行程序。"

    out_dir = os.path.join(ROOT, "report")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "发射窗口计算.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)

    # ---- 摘要 ----
    pw = res.get("practical_window")
    if pw:
        print("[window] 航警时窗 %s → %s UTC（%.1f min，裕度 %.0f s）" % (
            pw["start_utc"], pw["end_utc"], pw["duration_min"],
            float(res.get("margin_s") or 0)))
    elif nw:
        print("[window] 航警时窗：%s" % (res.get("reason") or "无可行区间"))
    else:
        print("[window] 航警时窗：%s" % nw_note)

    if ow.get("available"):
        print("[window] 轨道平面窗口：%d 个（目标 %s，容差 %.2f°）" % (
            ow.get("n_windows", 0),
            (ow.get("target") or {}).get("kind", "?"), ow.get("tolerance_deg", 0)))
        for k, w in enumerate(ow.get("windows") or [], 1):
            extra = ""
            rv = w.get("rendezvous")
            if rv:
                extra = "；交会 %s（ΔV %.0f m/s，%.1f h）" % (
                    "可行" if rv.get("feasible") else "不可行",
                    float(rv.get("dv_total_mps") or 0),
                    float(rv.get("duration_h") or 0))
            print("  %d) %s ~ %s UTC（%.0f s）最佳 %s，ΔRAAN %+.3f°%s" % (
                k, w["start_utc"][5:19].replace("T", " "),
                w["end_utc"][11:19], w["duration_s"],
                w["best_utc"], w["d_raan_deg"], extra))
        best = ow.get("best_near_t0")
        if best:
            dt_s = (O.parse_iso(best["best_utc"]) - t0_nom).total_seconds()
            print("[window] 距标称 T0 最近的窗口：%s UTC（%+.0f s）" % (
                best["best_utc"], dt_s))
    else:
        print("[window] 轨道平面窗口：%s" % (ow.get("reason") or "未计算"))
    print("[window] → %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
