# -*- coding: utf-8 -*-
"""返回舱再入/着陆的 3-DOF 简化模型（无 GUI 依赖）。

用途
----
给定空间站在某一时刻的 ECI 状态（km / km/s），施加反向制动 ΔV，积分
「制动 → 大气外滑行 → 再入 → 开伞 → 着陆」全过程，回答：

* 返回舱大概什么时候、落在哪里；
* 再入过载/动压/热流峰值；
* 与给定着陆区/NOTAM 的相对位置。

模型假设（工程估算，不是飞行程序）
--------------------------------
* 球形地球重力 + J2；大气按 US Standard Atmosphere 密度（随高度）；
* 大气随地球共转，阻力按相对速度 ``v - ω×r``；
* 升力 = (L/D) × 阻力，方向由常值倾侧角（bank）在「当地垂直-速度平面」内旋转；
* 开伞高度以下把配平升力设为 0，阻力面积切换到主伞；
* 不计风、开伞过程、控制死区、气动参数随马赫变化、烧蚀等。

精度量级：落点几十 km（取决于 L/D、开伞高度、大气模型），满足"大致范围"；
不能作为回收/安全依据。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import geo
from . import orbit as O

# --------------------------------------------------------------------------- #
# 大气密度（US Standard Atmosphere 1976 分段；与 engine/scripts/dynamics.py 同口径）
# --------------------------------------------------------------------------- #
_USSA_H = (0.0, 11.0, 20.0, 32.0, 47.0, 51.0, 71.0, 84.852)
_USSA_T = (288.15, 216.65, 216.65, 228.65, 270.65, 270.65, 214.65, 186.946)
_USSA_L = (-6.5, 0.0, 1.0, 2.8, 0.0, -2.8, -2.0, 0.0)
_H_HI = (86.0, 90.0, 100.0, 110.0, 120.0, 150.0, 200.0, 300.0, 500.0)
_LNRHO = tuple(math.log(v) for v in
               (7.0e-6, 3.4e-6, 5.6e-7, 1.0e-7, 2.2e-8, 2.0e-9, 2.5e-10,
                2.0e-11, 5.0e-13))
_G0, _R_AIR, _P0 = 9.80665, 287.053, 101325.0


def _layer_ratio(hb: float, tb: float, lb: float, dh: float) -> float:
    if abs(lb) < 1e-12:
        return math.exp(-_G0 * dh * 1000.0 / (_R_AIR * tb))
    t = tb + lb * dh
    return (t / tb) ** (-_G0 / ((lb / 1000.0) * _R_AIR))


_P_BASE = [1.0]
for _i in range(1, len(_USSA_H)):
    _P_BASE.append(_P_BASE[-1] * _layer_ratio(_USSA_H[_i - 1], _USSA_T[_i - 1],
                                              _USSA_L[_i - 1],
                                              _USSA_H[_i] - _USSA_H[_i - 1]))


def density(h_m: float) -> float:
    """USSA 密度 [kg/m^3]；86 km 以上按指数插值。"""
    if h_m > 86000.0:
        hk = min(h_m / 1000.0, 500.0)
        for i in range(len(_H_HI) - 1):
            if hk <= _H_HI[i + 1]:
                x = (hk - _H_HI[i]) / (_H_HI[i + 1] - _H_HI[i])
                return math.exp(_LNRHO[i] + x * (_LNRHO[i + 1] - _LNRHO[i]))
        return math.exp(_LNRHO[-1])
    hk = max(0.0, h_m / 1000.0)
    i = 0
    for k in range(len(_USSA_H) - 1):
        if hk >= _USSA_H[k]:
            i = k
    tb, lb = _USSA_T[i], _USSA_L[i]
    dh = hk - _USSA_H[i]
    t = tb + lb * dh if abs(lb) > 1e-12 else tb
    p = _P0 * _P_BASE[i] * math.exp(-_G0 * dh * 1000.0 / (_R_AIR * t)) \
        if abs(lb) < 1e-12 else _P0 * _P_BASE[i] * (t / tb) ** (
            -_G0 / ((lb / 1000.0) * _R_AIR))
    return p / (_R_AIR * t)


@dataclass
class CapsuleParams:
    """返回舱气动/质量参数（默认量级取自神舟返回舱公开数据，工程估值）。"""
    mass_kg: float = 3000.0
    cd_area_m2: float = 5.0          # Cd·A
    ld_ratio: float = 0.30           # 升阻比
    bank_deg: float = 0.0            # 倾侧角（0=升力朝上；±=横向）
    drogue_cd_area_m2: float = 20.0  # 减速伞 Cd·A
    chute_cd_area_m2: float = 1200.0  # 主伞 Cd·A
    drogue_deploy_km: float = 10.0
    main_deploy_km: float = 6.0
    main_deploy_speed_mps: float = 80.0
    chute_open_s: float = 5.0        # 主伞充气/收口过程（线性放大阻力面积）
    entry_interface_km: float = 120.0
    dt_coast_s: float = 2.0
    dt_entry_s: float = 0.5
    dt_low_s: float = 0.2
    dt_chute_s: float = 0.5
    max_time_s: float = 7200.0
    record_step_s: float = 5.0


def _gravity(r: np.ndarray) -> np.ndarray:
    rn = float(np.linalg.norm(r))
    a = -O.MU_EARTH * r / rn ** 3
    z2 = (r[2] / rn) ** 2
    fac = 1.5 * O.J2 * O.MU_EARTH * O.RE_EQ ** 2 / rn ** 5
    return a + fac * np.array([r[0] * (5.0 * z2 - 1.0),
                               r[1] * (5.0 * z2 - 1.0),
                               r[2] * (5.0 * z2 - 3.0)])


def _accel(r: np.ndarray, v: np.ndarray, t: float, dt0: datetime,
           p: CapsuleParams, stage: int) -> Tuple[np.ndarray, Dict[str, float]]:
    """返回 (a_eci km/s^2, 诊断量)。``stage`` 0=气动外形,1=减速伞,2=主伞。"""
    rn = float(np.linalg.norm(r))
    h_m = rn * 1000.0 - 6378137.0
    # 地球自转（ECI 中的大气速度）
    omega = np.array([0.0, 0.0, 7.292115e-5])
    v_rel = v - np.cross(omega, r)
    vn = float(np.linalg.norm(v_rel))
    v_ms = vn * 1000.0
    a = _gravity(r)
    rho = density(h_m)
    q = 0.5 * rho * v_ms * v_ms
    # Sutton-Graves  stagnation heat flux（R_n≈1 m），W/m^2 → W/cm^2
    heat = 1.83e-4 * math.sqrt(max(rho, 1e-30)) * v_ms ** 3 / 1.0e4
    g_load = 0.0
    if vn > 1e-9 and h_m < 200000.0:
        cd_a = (p.cd_area_m2 if stage == 0 else
                (p.drogue_cd_area_m2 if stage == 1 else p.chute_cd_area_m2))
        if stage == 2:
            t2 = getattr(p, "_stage2_t", t)
            frac = max(0.0, min(1.0, (t - t2) / max(0.1, p.chute_open_s)))
            cd_a = p.drogue_cd_area_m2 + frac * (p.chute_cd_area_m2 -
                                                 p.drogue_cd_area_m2)
        # m/s^2 → km/s^2
        a_d_mps2 = 0.5 * rho * v_ms * v_ms * cd_a / max(1.0, p.mass_kg)
        a_d = a_d_mps2 / 1000.0
        g_load = a_d_mps2 / 9.80665
        vhat = v_rel / vn
        a = a - a_d * vhat
        if stage == 0 and p.ld_ratio > 0.0:
            up = r / rn
            lift0 = up - float(np.dot(up, vhat)) * vhat
            n0 = float(np.linalg.norm(lift0))
            if n0 > 1e-9:
                lift0 = lift0 / n0
                side = np.cross(vhat, lift0)
                bank = math.radians(p.bank_deg)
                ldir = math.cos(bank) * lift0 + math.sin(bank) * side
                a = a + (p.ld_ratio * a_d) * ldir
    return a, dict(rho=rho, q=q, heat=heat, h_km=h_m / 1000.0, v_rel=vn,
                   g_load=g_load)


def _rk4(r, v, t, h, dt0, p, stage):
    a1, d1 = _accel(r, v, t, dt0, p, stage)
    a2, _ = _accel(r + 0.5 * h * v, v + 0.5 * h * a1, t + 0.5 * h, dt0, p, stage)
    a3, _ = _accel(r + 0.5 * h * (v + 0.5 * h * a1), v + 0.5 * h * a2,
                   t + 0.5 * h, dt0, p, stage)
    a4, _ = _accel(r + h * (v + 0.5 * h * a2), v + h * a3, t + h, dt0, p, stage)
    r2 = r + (h / 6.0) * (v + 2 * (v + 0.5 * h * a1) + 2 * (v + 0.5 * h * a2)
                          + (v + h * a3))
    v2 = v + (h / 6.0) * (a1 + 2 * a2 + 2 * a3 + a4)
    return r2, v2, d1


def _chute_step(r, v, t, dt, p):
    """伞降段半隐式积分（避免大阻力 + 固定步长 RK4 的刚性震荡）。

    重力显式、阻力对相对速度做隐式衰减：``v_rel /= (1 + k·|v_rel|·dt)``。
    dt≈0.5 s 即可稳定；落点/落速对步长不敏感（终端速度 ~6–10 m/s）。
    """
    omega = np.array([0.0, 0.0, 7.292115e-5])
    rho = density(float(np.linalg.norm(r)) * 1000.0 - 6378137.0)
    t2 = getattr(p, "_stage2_t", t)
    frac = 1.0 if not hasattr(p, "_stage2_t") else max(
        0.0, min(1.0, (t - t2) / max(0.1, p.chute_open_s)))
    cd_a = p.drogue_cd_area_m2 + frac * (p.chute_cd_area_m2 - p.drogue_cd_area_m2)
    k = 0.5 * rho * cd_a / max(1.0, p.mass_kg)
    v = v + _gravity(r) * dt
    v_rel = v - np.cross(omega, r)
    vn = float(np.linalg.norm(v_rel))
    vn_ms = vn * 1000.0
    v_rel = v_rel / (1.0 + k * vn_ms * dt)
    v = v_rel + np.cross(omega, r)
    r = r + v * dt
    q = 0.5 * rho * vn_ms * vn_ms
    return r, v, dict(rho=rho, q=q, heat=0.0, h_km=float(np.linalg.norm(r)) - 6378.137,
                      v_rel=float(np.linalg.norm(v_rel)), g_load=0.0)


def apply_deorbit(r, v, dv_mps: float, retrograde: bool = True):
    """在给定 ECI 状态施加制动 ΔV；返回新速度（km/s）。"""
    v = np.asarray(v, dtype=float)
    vhat = v / max(1e-12, float(np.linalg.norm(v)))
    return v - (float(dv_mps) / 1000.0) * vhat if retrograde else v + \
        (float(dv_mps) / 1000.0) * vhat


def simulate_return(r0: Sequence[float], v0: Sequence[float], t0: datetime,
                    p: Optional[CapsuleParams] = None,
                    deorbit_dv_mps: float = 0.0,
                    record: bool = True) -> Dict[str, Any]:
    """从空间站状态出发，积分返回舱再入到着陆。

    Parameters
    ----------
    r0, v0 : ECI km / km/s（施加制动**前**的状态）。
    t0 : 制动时刻（UTC）。
    deorbit_dv_mps : 反向制动 ΔV（0 = 直接再入，仅用于测试）。
    record : 是否记录完整时序（窗口扫描时可 False 提速）。

    Returns
    -------
    dict
        ``landing``（lat/lon/alt/time/speed）、``events``、``trajectory``、
        ``peaks``、``params``、``deorbit_dv_mps``、``entry``、``ok``。
    """
    p = p or CapsuleParams()
    try:
        del p._stage2_t
    except AttributeError:
        pass
    t0 = O.ensure_utc(t0)
    r = np.asarray(r0, dtype=float).copy()
    v = np.asarray(v0, dtype=float).copy()
    if deorbit_dv_mps:
        v = apply_deorbit(r, v, deorbit_dv_mps)
    t = 0.0
    traj: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    peaks = dict(q_pa=0.0, g=0.0, heat_w_cm2=0.0, v_max=float(np.linalg.norm(v)))
    next_rec = 0.0
    stage = 0          # 0=气动外形,1=减速伞,2=主伞
    entry_t = None
    chute_t = None
    landing = None
    prev = None
    last_d = None
    while t < p.max_time_s:
        rn = float(np.linalg.norm(r))
        h_km = rn - 6378.137
        v_now = float(np.linalg.norm(v))
        v_rel_now = _rel_speed_km_s(r, v)
        # 事件：再入界面
        if entry_t is None and h_km <= p.entry_interface_km:
            entry_t = t
            _la, _lo, _al = _state_geo(r, t0 + timedelta(seconds=t))
            events.append(dict(event="再入界面", t_met_s=t,
                               t_utc=O.utc_text(t0 + timedelta(seconds=t)),
                               lat=_la, lon=_lo, alt_km=h_km,
                               speed_km_s=v_rel_now))
        if stage == 0 and h_km <= p.drogue_deploy_km:
            stage = 1
            chute_t = t
            events.append(dict(event="减速伞开伞", t_met_s=t,
                               t_utc=O.utc_text(t0 + timedelta(seconds=t)),
                               alt_km=h_km, speed_m_s=v_rel_now * 1000.0))
        if stage == 1 and h_km <= p.main_deploy_km:
            stage = 2
            p._stage2_t = t
            events.append(dict(event="主伞开伞", t_met_s=t,
                               t_utc=O.utc_text(t0 + timedelta(seconds=t)),
                               alt_km=h_km, speed_m_s=v_rel_now * 1000.0))
        if stage >= 1:
            r2, v2, d = _chute_step(r, v, t, p.dt_chute_s, p)
            dt = p.dt_chute_s
        else:
            dt = p.dt_coast_s if h_km > p.entry_interface_km else (
                p.dt_entry_s if h_km > 20.0 else p.dt_low_s)
            r2, v2, d = _rk4(r, v, t, dt, t0, p, stage)
        h2 = float(np.linalg.norm(r2)) - 6378.137
        # 峰值
        if d:
            peaks["q_pa"] = max(peaks["q_pa"], d["q"])
            peaks["heat_w_cm2"] = max(peaks["heat_w_cm2"], d["heat"])
            peaks["g"] = max(peaks["g"], d["g_load"])
        if record and t + dt >= next_rec:
            lat, lon, alt_km = _state_geo(r2, t0 + timedelta(seconds=t + dt))
            traj.append(dict(t_met_s=round(t + dt, 2),
                             t_utc=O.utc_text(t0 + timedelta(seconds=t + dt)),
                             lat_deg=round(float(lat), 5), lon_deg=round(float(lon), 5),
                             alt_km=round(float(alt_km), 3),
                             speed_km_s=round(_rel_speed_km_s(r2, v2), 4),
                             q_pa=round(float(d["q"]) if d else 0.0, 2),
                             phase=("主伞" if stage >= 2 else
                                    ("减速伞" if stage == 1 else
                                     ("再入" if h2 <= p.entry_interface_km else "滑行")))))
            next_rec += p.record_step_s
        prev = (r, v, t)
        r, v, t = r2, v2, t + dt
        last_d = d
        if h2 <= 0.0:
            # 线性插值落点/时刻
            frac = float(np.linalg.norm(prev[0]) - 6378.137) / max(
                1e-6, float(np.linalg.norm(prev[0]) - np.linalg.norm(r)))
            frac = max(0.0, min(1.0, frac))
            t_land = prev[2] + frac * dt
            rl = prev[0] + frac * (r - prev[0])
            vl = prev[1] + frac * (v - prev[1])
            lat, lon, alt_m = _state_geo(rl, t0 + timedelta(seconds=t_land))
            landing = dict(t_met_s=round(t_land, 3),
                           t_utc=O.utc_text(t0 + timedelta(seconds=t_land)),
                           lat_deg=float(lat), lon_deg=float(lon),
                           alt_m=float(alt_m) * 1000.0,
                           speed_m_s=_rel_speed_km_s(rl, vl) * 1000.0,
                           flight_time_s=round(t_land, 2))
            events.append(dict(event="着陆", t_met_s=round(t_land, 3),
                               t_utc=landing["t_utc"], lat=float(lat), lon=float(lon),
                               alt_m=float(alt_m) * 1000.0,
                               speed_m_s=landing["speed_m_s"]))
            break
    if landing is None:
        return dict(ok=False, reason="积分超时未着陆（检查制动 ΔV/气动参数）",
                    params=p, deorbit_dv_mps=deorbit_dv_mps,
                    events=events, trajectory=traj, peaks=peaks)
    # 记录着陆点
    if record:
        traj.append(dict(t_met_s=landing["t_met_s"], t_utc=landing["t_utc"],
                         lat_deg=round(landing["lat_deg"], 5),
                         lon_deg=round(landing["lon_deg"], 5),
                         alt_km=round(landing["alt_m"] / 1000.0, 4),
                         speed_km_s=round(landing["speed_m_s"] / 1000.0, 4),
                         q_pa=0.0, phase="着陆"))
    return dict(ok=True, landing=landing, events=events, trajectory=traj,
                peaks=peaks, params=p, deorbit_dv_mps=deorbit_dv_mps,
                entry_interface_km=p.entry_interface_km,
                t0_utc=O.utc_text(t0))


def ground_range_km(lat1, lon1, lat2, lon2) -> float:
    return float(geo.geodesic_distance(float(lat1), float(lon1),
                                       float(lat2), float(lon2)))


def _state_geo(r_eci, dt):
    """ECI 位置 → (lat, lon, alt_km)。注意 geo 接口用米。"""
    x, y, z = geo.eci_to_ecef(np.asarray(r_eci, dtype=float), dt)
    la, lo, alt_m = geo.ecef_to_geodetic(float(x) * 1000.0, float(y) * 1000.0,
                                         float(z) * 1000.0)
    return float(la), float(lo), float(alt_m) / 1000.0


_OMEGA = np.array([0.0, 0.0, 7.292115e-5])


def _rel_speed_km_s(r, v) -> float:
    """相对旋转大气的速度 [km/s]（报告落速/开伞速度用这个，而不是惯性速度）。"""
    return float(np.linalg.norm(np.asarray(v) - np.cross(_OMEGA, np.asarray(r))))


def dispersion_ellipse(points: Sequence[Sequence[float]]) -> Optional[Dict[str, Any]]:
    """由落点样本 (lat, lon) 估计 1σ 误差椭圆（km）。"""
    pts = np.asarray(points, dtype=float)
    if pts.shape[0] < 3:
        return None
    lat0 = float(np.mean(pts[:, 0]))
    lon0 = float(np.mean(pts[:, 1]))
    # 局部 km 坐标（等距圆柱近似，落点尺度 <100 km）
    km_per_deg_lat = 111.32
    km_per_deg_lon = 111.32 * math.cos(math.radians(lat0))
    x = (pts[:, 1] - lon0) * km_per_deg_lon
    y = (pts[:, 0] - lat0) * km_per_deg_lat
    cov = np.cov(np.column_stack([x, y]), rowvar=False)
    vals, vecs = np.linalg.eigh(cov)
    order = np.argsort(vals)[::-1]
    vals, vecs = vals[order], vecs[:, order]
    semi_major = math.sqrt(max(0.0, float(vals[0])))
    semi_minor = math.sqrt(max(0.0, float(vals[1])))
    ang = math.degrees(math.atan2(float(vecs[1, 0]), float(vecs[0, 0])))
    return dict(center_lat=lat0, center_lon=lon0,
                semi_major_km=round(semi_major, 2),
                semi_minor_km=round(semi_minor, 2),
                orientation_deg=round(ang, 1), n=int(pts.shape[0]),
                method="样本协方差 1σ（局部 ENU 近似）")


__all__ = ["CapsuleParams", "density", "apply_deorbit", "simulate_return",
           "ground_range_km", "dispersion_ellipse"]
