# -*- coding: utf-8 -*-
"""CZ-2F 专用：入轨 → 相位调整 → 转移 → 最终逼近 → 对接 的详细方案。

输入
----
* ``sim/trajectory.csv`` / ``sim/入轨根数.json``：入轨点状态；
* 空间站轨道（官方 OEM 快照 / 用户 TLE / 在线）：经 ``common.station`` 统一解析；
* ``report/发射窗口计算.json``：优先使用平面窗口内的可行 T0。

模型
----
共面圆轨道、三次脉冲 + 最终逼近：
1. 入轨（SECO，约 200 km）；
2. （可选）相位轨道绕 k 圈，用相位角与目标对齐；
3. 半圈 Hohmann 转移至目标高度；
4. 圆化/制动；
5. 最终逼近（默认 30 min、25 m/s 量级）后对接。

对时间线做二体 + J2 的 RK4 递推，末端与官方星历对比，给出残余距离/速度，
用于判断"窗口—对接"是否自洽。**不是飞行程序，仅供公开信息分析。**

输出
----
* ``sim/交会对接.json``
* ``sim/交会对接_时序.csv``
* ``sim/交会轨迹.csv``：入轨 → 相位 → 转移 → 最终逼近的密集轨迹（30 s 采样），
  供「交会全流程」三维/动画与星下点图使用（v4.2）。
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

_ENG = os.environ.get("RK_ENGINE")
if _ENG:
    for _p in (_ENG, os.path.join(_ENG, "scripts")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import numpy as np
from common import orbit as O

try:
    from common import station as ST
except Exception:  # noqa: BLE001
    ST = None

import window as W


def _unit(v):
    v = np.asarray(v, dtype=float)
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-12 else v


def _burn(v, dv_mps, prograde=True):
    d = _unit(v) * (float(dv_mps) / 1000.0)
    return np.asarray(v, dtype=float) + (d if prograde else -d)


def _plane_angle(i1, raan1, i2, raan2):
    """两个轨道面的夹角 [deg]。"""
    def n(i, r):
        i, r = math.radians(float(i)), math.radians(float(r))
        return np.array([math.sin(i) * math.sin(r),
                         -math.sin(i) * math.cos(r), math.cos(i)])
    try:
        v = float(n(i1, raan1) @ n(i2, raan2))
        return math.degrees(math.acos(max(-1.0, min(1.0, v))))
    except Exception:  # noqa: BLE001
        return 0.0


def _track_point(dt, t_met, phase, r):
    lat, lon, alt = O.subpoint(dt, r)
    return dict(t_utc=O.utc_text(dt), t_met_s=round(float(t_met), 3), phase=phase,
                lat_deg=round(float(lat), 6), lon_deg=round(float(lon), 6),
                alt_km=round(float(alt), 3))


def _state_row(tag, dt, t_met, r, v, dv_mps=0.0, dv_note="", station=None,
               note=""):
    """构造时间线一行。"""
    el = O.elements_from_state(r, v)
    la, lo, al = O.subpoint(dt, r)
    rng = rv = None
    if station is not None:
        try:
            r_st, v_st = station.state(dt)
            rng = float(np.linalg.norm(np.asarray(r) - r_st))
            rv = float(np.linalg.norm(np.asarray(v) - v_st))
        except Exception:  # noqa: BLE001
            pass
    return dict(phase=tag, t_met_s=round(float(t_met), 3), t_utc=O.utc_text(dt),
                duration_s=0.0, dv_mps=round(float(dv_mps), 3),
                dv_note=dv_note,
                lat_deg=round(la, 5), lon_deg=round(lo, 5), alt_km=round(al, 2),
                range_to_station_km=(round(rng, 3) if rng is not None else None),
                rel_vel_mps=(round(rv, 3) if rv is not None else None),
                orbit_a_km=round(el["a_km"], 3), orbit_e=round(el["e"], 6),
                orbit_i_deg=round(el["i_deg"], 4), orbit_u_deg=round(el["u_deg"], 3),
                note=note)


def _fmt_t(dt):
    return O.utc_text(dt)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--allow-network", action="store_true")
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))
    sim_dir = os.path.join(ROOT, "sim")
    os.makedirs(sim_dir, exist_ok=True)
    t0_nom = O.parse_iso(cfg["mission"]["t0_utc"])
    allow_net = bool(args.allow_network or os.environ.get("RK_ALLOW_NETWORK") == "1")

    out = dict(enabled=False, t0_nominal_utc=_fmt_t(t0_nom),
               generated_utc=O.utc_text(datetime.now(timezone.utc)))

    st_cfg = dict(cfg.get("station") or {})
    if not st_cfg.get("enabled"):
        out["reason"] = "未启用空间站交会模块（config.station.enabled=false）"
    elif ST is None:
        out["reason"] = "空间站模块不可用（engine/common/station.py 缺失）"
    else:
        try:
            src = ST.resolve(cfg, t_hint=t0_nom, allow_network=allow_net,
                             logger=lambda s: print(s))
        except Exception as exc:  # noqa: BLE001
            src = None
            out["reason"] = "空间站轨道数据不可用：%s" % exc

        ins = W.insertion_state(cfg, sim_dir, t0_nom) or W._fallback_insertion(cfg, t0_nom)
        if src is None:
            pass
        elif ins is None:
            out["reason"] = "没有可用的入轨点状态（缺 sim/trajectory.csv / 入轨根数.json）"
        else:
            # v4.3：只用 config 的 T0（弹道就是在这个 T0 飞出来的）。
            # 窗口 JSON 仅作附注；绝不再自行平移 T0（旧逻辑会让交会与弹道用两个 T0，
            # 窗口文件过期时结果会飘到几分钟之外）。
            t0_used = t0_nom
            win = {}
            try:
                with open(os.path.join(ROOT, "report", "发射窗口计算.json"),
                          encoding="utf-8") as f:
                    win = json.load(f)
            except Exception:  # noqa: BLE001
                win = {}
            ow = win.get("orbit_window") or {}
            wins = ow.get("windows") or []
            t0_reason = "按 config T0 计算（弹道同一时刻）"
            try:
                _inside = None
                for _w in wins:
                    _a = O.parse_iso(_w.get("start_dogleg_utc") or _w["start_utc"])
                    _b = O.parse_iso(_w.get("end_dogleg_utc") or _w["end_utc"])
                    if _a <= t0_nom <= _b:
                        _inside = _w
                        break
                if _inside is not None:
                    _best = O.parse_iso(_inside["best_utc"])
                    _need = abs((t0_nom - _best).total_seconds()) / 3600.0 * 15.04
                    t0_reason = ("按 config T0 计算；该 T0 在 dogleg 可达窗口内"
                                 "（穿越面最佳 %s，需平面修正约 %.2f°）"
                                 % (_inside["best_utc"], _need))
            except Exception:  # noqa: BLE001
                pass

            dt_ins = t0_used + timedelta(seconds=float(ins["t_met_s"]))
            # v4.2：用**实测入轨状态**。标称 T0 与计算 T0 不同时，整条入轨状态
            # 随地球自转旋转到 t0_used（10 min 上升段的平面旋转近似线性）。
            dth = math.radians(O.OMEGA_EARTH_DEG_DAY / 86400.0 *
                               (t0_used - t0_nom).total_seconds())
            cd, sd = math.cos(dth), math.sin(dth)
            _Rz = np.array([[cd, -sd, 0.0], [sd, cd, 0.0], [0.0, 0.0, 1.0]])
            r_sc = _Rz @ np.asarray(ins["r_eci"], dtype=float)
            v_sc = _Rz @ np.asarray(ins["v_eci"], dtype=float)
            el_used = O.elements_from_state(r_sc, v_sc)
            el_plane = {"raan_deg": float(el_used["raan_deg"]),
                        "i_deg": float(el_used["i_deg"]),
                        "a_km": float(el_used["a_km"]),
                        "e": float(el_used.get("e") or 0.0)}
            az = float(ins["az_deg"])
            r_st, v_st = src.state(dt_ins)
            phase = O.relative_phase_deg(r_sc, v_sc, r_st, v_st)
            rd = ST.rendezvous_defaults(cfg)
            budget = float(rd.get("dv_budget_mps") or 250.0)
            final_s = float(rd.get("final_approach_s") or 1800.0)
            final_dv_cap = float(rd.get("final_approach_dv_mps") or 25.0)
            plan = O.phasing_plan_best(
                float(np.linalg.norm(r_sc)), float(np.linalg.norm(r_st)), phase,
                max_orbits=int(rd.get("max_phasing_orbits") or 10),
                min_orbits=int(rd.get("min_phasing_orbits") or 1),
                preferred_s=float(rd.get("target_time_h") or 6.5) * 3600.0,
                max_dv_mps=budget,
                max_total_s=float(rd.get("max_phasing_h") or 12.0) * 3600.0,
                final_dv_cap_mps=final_dv_cap,
                v_sc_kms=float(np.linalg.norm(v_sc)))

            out.update(enabled=True, station=src.info(), t0_used_utc=_fmt_t(t0_used),
                       t0_reason=t0_reason,
                       insertion=dict(t_utc=_fmt_t(dt_ins),
                                      t_met_s=float(ins["t_met_s"]),
                                      lat_deg=round(ins["lat"], 5),
                                      lon_deg=round(ins["lon"], 5),
                                      alt_km=round(ins["alt_km"], 2),
                                      raan_deg=round(el_plane["raan_deg"], 4),
                                      i_deg=round(el_plane["i_deg"], 4),
                                      az_deg=round(az, 3),
                                      plane_note="使用实测入轨状态（不再用理想平面替代）"),
                       station_at_insertion=dict(
                           raan_deg=round(src.elements(dt_ins)["raan_deg"], 4),
                           i_deg=round(src.elements(dt_ins)["i_deg"], 4),
                           u_deg=round(src.elements(dt_ins)["u_deg"], 4),
                           alt_km=round(src.elements(dt_ins)["a_km"] - O.RE_EQ, 2)),
                       plane=dict(
                           residual_deg=round(_plane_angle(
                               el_plane["i_deg"], el_plane["raan_deg"],
                               src.elements(dt_ins)["i_deg"],
                               src.elements(dt_ins)["raan_deg"]), 4),
                           achieved_i_deg=round(el_plane["i_deg"], 4),
                           achieved_raan_deg=round(el_plane["raan_deg"], 4),
                           target_i_deg=round(src.elements(dt_ins)["i_deg"], 4),
                           target_raan_deg=round(src.elements(dt_ins)["raan_deg"], 4),
                           note="入轨面与空间站面的夹角（实测轨迹）"),
                       phase=dict(lead_deg=round(phase, 4),
                                  target_lead_deg=(round(plan["target_lead_deg"], 4)
                                                   if plan else None)),
                       budget_mps=budget, feasible=bool(plan))

            if plan is None:
                out["reason"] = ("相位不可行：没有满足 ΔV≤%.0f m/s、时长≤%.1f h 的方案"
                                 % (budget, float(rd.get("max_phasing_h") or 12.0)))
            else:
                # ---- 时间线（三次脉冲 + 最终逼近） ----
                timeline = []
                track = []
                station = src
                r = r_sc.copy()
                v = v_sc.copy()
                t = dt_ins

                def _rec_samples(samples, seg_start, t_met0, phase):
                    for off, rr, _vv in samples:
                        track.append(_track_point(
                            seg_start + timedelta(seconds=float(off)),
                            t_met0 + float(off), phase, rr))

                timeline.append(_state_row("入轨（SECO）", t, ins["t_met_s"], r, v,
                                          0.0, "", station,
                                          "CZ-2F 入轨；%.1f km × %.4f°" % (
                                              np.linalg.norm(r) - O.RE_EQ,
                                              el_plane["i_deg"])))
                dv1 = float(plan["dv_phase_signed_mps"])
                if plan["k"] > 0:
                    v = _burn(v, dv1)
                    timeline.append(_state_row("相位调整点火", t, ins["t_met_s"], r, v,
                                              abs(dv1), "沿速度方向" if dv1 >= 0 else "逆速度方向",
                                              station,
                                              "进入相位轨道，绕 %d 圈" % plan["k"]))
                    dur = float(plan["phasing_s"])
                    r, v, _samp = O.propagate_rk4_track(r, v, dur, step_s=30.0)
                    _rec_samples(_samp, t, float(ins["t_met_s"]), "相位段")
                    t = t + timedelta(seconds=dur)
                    timeline.append(_state_row("相位段结束", t, ins["t_met_s"] + dur,
                                              r, v, 0.0, "", station,
                                              "相位轨道 %.1f km" % (
                                                  plan["phasing_a_km"] - O.RE_EQ)))
                # 转移点火：按“当前实际状态 + 到达时刻空间站半径”自适应求脉冲，
                # 消除 J2 漂移导致的拱点半径偏差。
                rn = float(np.linalg.norm(r))
                tof = float(plan["transfer_s"])
                r_tgt = rn
                a_t = 0.5 * (rn + r_tgt)
                for _ in range(4):
                    try:
                        r_tgt = float(np.linalg.norm(
                            station.state(t + timedelta(seconds=tof))[0]))
                    except Exception:  # noqa: BLE001
                        r_tgt = float(np.linalg.norm(station.state(t)[0]))
                    a_t = 0.5 * (rn + r_tgt)
                    tof = math.pi * math.sqrt(a_t ** 3 / O.MU_EARTH)
                v_t1 = math.sqrt(O.MU_EARTH * (2.0 / rn - 1.0 / a_t))
                dv2 = v_t1 - float(np.linalg.norm(v))
                dv2_mps = abs(dv2) * 1000.0
                v = _burn(v, dv2 * 1000.0)
                timeline.append(_state_row("转移点火（Hohmann）", t,
                                          ins["t_met_s"] + float(plan["phasing_s"]),
                                          r, v, abs(dv2) * 1000.0,
                                          "沿速度方向" if dv2 >= 0 else "逆速度方向",
                                          station,
                                          "半圈椭圆转移，目标半径 %.2f km"
                                          % (r_tgt - O.RE_EQ)))
                _t_transfer0 = t
                r, v, _samp = O.propagate_rk4_track(r, v, tof, step_s=30.0)
                _rec_samples(_samp, _t_transfer0,
                             float(ins["t_met_s"]) + float(plan["phasing_s"]),
                             "转移段")
                t = t + timedelta(seconds=tof)
                t_transfer_end = ins["t_met_s"] + float(plan["phasing_s"]) + tof
                timeline.append(_state_row("到达目标高度", t, t_transfer_end,
                                          r, v, 0.0, "", station, "转移段结束"))
                # 圆化：按实际半径与速度求脉冲
                rn2 = float(np.linalg.norm(r))
                v_circ = math.sqrt(O.MU_EARTH / rn2)
                dv3 = v_circ - float(np.linalg.norm(v))
                dv3_mps = abs(dv3) * 1000.0
                v = _burn(v, dv3 * 1000.0)
                timeline.append(_state_row("圆化/制动点火", t, t_transfer_end,
                                          r, v, abs(dv3) * 1000.0,
                                          "沿速度方向" if dv3 >= 0 else "逆速度方向",
                                          station, "进入目标轨道"))
                # ---- 最终逼近：二体 Lambert 两脉冲（末端位置精确匹配） ----
                # 说明：本段用二体解（不含 J2）以保证"到达空间站位置"的几何闭合；
                # 实际任务由相对导航/末制导修正 J2 与摄动残差。
                r_st_arr, v_st_arr = station.state(t)
                r_rel = r - r_st_arr
                v_rel = v - v_st_arr
                range0 = float(np.linalg.norm(r_rel))
                rv0 = float(np.linalg.norm(v_rel))
                t_dock = t + timedelta(seconds=final_s)
                r_st_dock, v_st_dock = station.state(t_dock)
                dv_lam1 = dv_dock = 0.0
                lam_ok = False
                try:
                    res_lam = O.lambert(r, r_st_dock, final_s, O.MU_EARTH,
                                        prograde=True)
                    if res_lam is not None:
                        v1_lam, v2_lam = res_lam
                        dv_lam1 = float(np.linalg.norm(v1_lam - v)) * 1000.0
                        dv_dock = float(np.linalg.norm(v_st_dock - v2_lam)) * 1000.0
                        v = v1_lam
                        lam_ok = True
                except Exception:  # noqa: BLE001
                    lam_ok = False
                if not lam_ok:
                    dv_vec = -(r_rel / max(60.0, final_s)) - v_rel
                    dv_lam1 = float(np.linalg.norm(dv_vec)) * 1000.0
                    v = v + dv_vec
                guide = "Lambert 两脉冲" if lam_ok else "相对导航"
                t_base = float(ins["t_met_s"]) + (t - dt_ins).total_seconds()
                timeline.append(_state_row("最终逼近（%s 引导）" % guide, t, t_base,
                                          r, v, round(dv_lam1, 3),
                                          "相对导航双脉冲", station,
                                          "残余距离 %.1f km，相对速度 %.1f m/s" % (
                                              range0, rv0)))
                # 二体递推（与 Lambert 假设一致；避免 J2 把几何打偏）
                _t_final0 = t
                r, v, _samp = O.propagate_rk4_track(r, v, final_s, step_s=30.0, j2=False)
                _rec_samples(_samp, _t_final0, t_base, "最终逼近")
                t = t_dock
                r_st_d, v_st_d = station.state(t)
                residual = float(np.linalg.norm(r - r_st_d))
                rel_v_before = float(np.linalg.norm(v - v_st_d))
                if not lam_ok:
                    dv_dock = rel_v_before * 1000.0
                v = v_st_d.copy()
                timeline.append(_state_row("对接（速度匹配）", t, t_base + final_s,
                                          r, v, round(dv_dock, 3), "制动至与空间站同速",
                                          station,
                                          "残余 %.3f km / 制动前相对速度 %.2f m/s" % (
                                              residual, rel_v_before)))
                # 累计时长/ΔV
                total_dv = (abs(dv1) + dv2_mps + dv3_mps + dv_lam1 + dv_dock)
                for i, row in enumerate(timeline):
                    row["duration_s"] = round(
                        (O.parse_iso(row["t_utc"]) - O.parse_iso(timeline[0]["t_utc"]))
                        .total_seconds(), 1)
                    row["cum_dv_mps"] = round(sum(x["dv_mps"] for x in timeline[:i + 1]), 3)
                dock = timeline[-1]
                feasible = bool(total_dv <= budget and residual <= 10.0)
                out.update(
                    timeline=timeline,
                    plan=dict(mode=("phasing+hohmann" if plan["k"] > 0 else "direct+hohmann"),
                              phasing_orbits=int(plan["k"]),
                              phasing_a_km=round(plan["phasing_a_km"], 2),
                              dv_phase_mps=round(plan["dv_phase_mps"], 2),
                              dv_transfer_mps=round(dv2_mps, 2),
                              dv_circular_mps=round(dv3_mps, 2),
                              dv_final_mps=round(dv_lam1, 2),
                              dv_dock_mps=round(dv_dock, 2),
                              total_dv_mps=round(total_dv, 2),
                              duration_h=round((t - dt_ins).total_seconds() / 3600.0, 3),
                              within_target_time=bool(plan.get("within_time", True))),
                    docking=dict(
                        t_met_s=round(t_base + final_s, 3),
                        t_utc=dock["t_utc"], t_bjt=O.bjt_text(O.parse_iso(dock["t_utc"])),
                        station_lat_deg=dock["lat_deg"], station_lon_deg=dock["lon_deg"],
                        alt_km=dock.get("alt_km"),
                        # 对接点是"对接瞬间空间站星下点"（交会段末端两者重合）
                        subpoint_note="对接瞬间空间站/飞船共同星下点（WGS84，含地形无关的椭球高度）",
                        residual_range_km=round(residual, 3),
                        residual_rel_vel_mps=0.0,
                        rel_vel_before_dock_mps=round(rel_v_before, 3),
                        note="按共面圆轨道相位模型 + J2 递推，末端残差用于评估自洽性"),
                    feasible=feasible,
                    total_dv_mps=round(total_dv, 2),
                    notes=([] if feasible else
                           ["总 ΔV %.1f m/s / 末端残余 %.1f km 超出自洽阈值"
                            % (total_dv, residual)]),
                    assumptions=[
                        "共面圆轨道 + 三次脉冲（相位调整/Hohmann/圆化）+ 最终逼近；"
                        "未计大气、姿控、测控与真实制导。",
                        "相位模型用平均角速度；时间线用二体+J2 RK4 递推，"
                        "末端残差说明模型与官方星历的差异。",
                        "标称 T0 与所选 T0 的关系见 t0_reason；若标称 T0 不在窗口内，"
                        "说明该任务按真实交会窗口需要调整发射时刻。",
                    ])
                # ---- v3.5：任务前后各 30 min 空间站轨道（CZ-2F 上下文） ----
                try:
                    _ctx_start = t0_used - timedelta(seconds=1800.0)
                    _dock_dt = O.parse_iso(dock["t_utc"])
                    _ctx_end = _dock_dt + timedelta(seconds=1800.0)
                    _rows = []
                    _t = _ctx_start
                    while _t <= _ctx_end:
                        _r, _v = src.state(_t)
                        _la, _lo, _al = O.subpoint(_t, _r)
                        _rows.append([O.utc_text(_t), round(float(_la), 5),
                                      round(float(_lo), 5), round(float(_al), 3)])
                        _t += timedelta(seconds=30.0)
                    _cp = os.path.join(sim_dir, "CZ2F_任务前后30min_空间站.csv")
                    os.makedirs(sim_dir, exist_ok=True)
                    with open(_cp, "w", encoding="utf-8-sig", newline="") as _f:
                        import csv as _csv
                        _w = _csv.writer(_f)
                        _w.writerow(["t_utc", "lat_deg", "lon_deg", "alt_km"])
                        _w.writerows(_rows)
                    out["task_context"] = dict(
                        start_utc=O.utc_text(_ctx_start),
                        end_utc=O.utc_text(_ctx_end),
                        n=len(_rows), file="sim/CZ2F_任务前后30min_空间站.csv",
                        note="任务前后各延长 30 min 的空间站星下点，用于上下文/三维叠加")
                except Exception as _exc:  # noqa: BLE001
                    out["task_context"] = dict(error=str(_exc))
                # CSV
                cols = ["phase", "t_met_s", "t_utc", "duration_s", "dv_mps",
                        "cum_dv_mps", "dv_note", "lat_deg", "lon_deg", "alt_km",
                        "range_to_station_km", "rel_vel_mps", "orbit_a_km",
                        "orbit_e", "orbit_i_deg", "orbit_u_deg", "note"]
                path_csv = os.path.join(sim_dir, "交会对接_时序.csv")
                with open(path_csv, "w", encoding="utf-8-sig", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
                    w.writeheader()
                    for row in timeline:
                        w.writerow(row)
                out["timeline_csv"] = "sim/交会对接_时序.csv"
                # v4.2：密集轨迹（全流程，供三维/动画/星下点图）
                try:
                    _tp = os.path.join(sim_dir, "交会轨迹.csv")
                    with open(_tp, "w", encoding="utf-8-sig", newline="") as _f:
                        _w = csv.DictWriter(
                            _f, fieldnames=["t_utc", "t_met_s", "phase",
                                            "lat_deg", "lon_deg", "alt_km"])
                        _w.writeheader()
                        for _row in track:
                            _w.writerow(_row)
                    out["trajectory_csv"] = "sim/交会轨迹.csv"
                    out["trajectory_n"] = len(track)
                    out["trajectory_note"] = (
                        "入轨→相位→转移→最终逼近的 30 s 采样（实测轨迹递推）；"
                        "发射→入轨段见 sim/trajectory.csv，三维里合并为一条全流程轨迹")
                except Exception as _exc:  # noqa: BLE001
                    out["trajectory_error"] = str(_exc)

    path = os.path.join(sim_dir, "交会对接.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    if out.get("enabled"):
        d = out.get("docking") or {}
        p = out.get("plan") or {}
        print("[rendezvous] T0 用于计算：%s（%s）" % (out.get("t0_used_utc"),
                                                    out.get("t0_reason")))
        print("[rendezvous] 入轨 %.1f km → 相位 %d 圈 → 转移 → 对接：%s" % (
            float((out.get("insertion") or {}).get("alt_km") or 0),
            int(p.get("phasing_orbits") or 0), d.get("t_utc", "?")))
        print("[rendezvous] 总 ΔV %.1f m/s，时长 %.2f h，末端残余 %.2f km / %.2f m/s；%s" % (
            float(out.get("total_dv_mps") or 0),
            float(p.get("duration_h") or 0),
            float(d.get("residual_range_km") or 0),
            float(d.get("residual_rel_vel_mps") or 0),
            "可行" if out.get("feasible") else "不可行"))
    else:
        print("[rendezvous] 未计算：%s" % (out.get("reason") or "未知原因"))
    print("[rendezvous] → %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
