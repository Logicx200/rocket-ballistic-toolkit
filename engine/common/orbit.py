# -*- coding: utf-8 -*-
"""轨道力学公共内核（无 GUI 依赖、无 astropy/skyfield 依赖）。

提供工具箱新增功能所需的全部轨道计算：

* TLE 解析 + SGP4 传播（可选依赖 :mod:`sgp4`；缺失时给出中文提示）；
* CCSDS OEM 精密星历解析 + 三次 Hermite 插值（官方 CMSE 快照即此格式）；
* J2000(EME2000/GCRS) ↔ TEME 坐标变换（IAU 1976 岁差 + IAU 1980 章动主项）；
  工具箱上升段动力学用的 ECI 即「GMST 旋转」的 TEME 口径，因此两者可直接比较 RAAN；
* 轨道根数、升交点赤经 RAAN、纬度幅角、J2 长期漂移；
* 太阳同步轨道 LTAN（降/升交点地方时）与 RAAN 的互换；
* 二体 + J2 RK4 递推、Hohmann 转移、相位调整（phasing）方案求解。

约定
----
* 距离 km、速度 km/s、角度 deg、时间 UTC（``datetime`` 带 tzinfo）。
* 坐标系：``teme``（与工具 ECI 一致）；OEM 默认 ``eme2000``，读取时自动转 TEME。
* 所有函数只依赖标准库 + numpy（SGP4 为可选依赖）。

仅供公开信息分析与教学使用，非实际飞行程序。
"""
from __future__ import annotations

import math
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from . import geo

# --------------------------------------------------------------------------- #
# 常数
# --------------------------------------------------------------------------- #
MU_EARTH = 398600.4418          # km^3/s^2（WGS84/EGM 量级）
RE_EQ = 6378.137                # km，WGS84 赤道半径
J2 = 1.08262668e-3
FLAT = 1.0 / 298.257223563
OMEGA_EARTH_DEG_DAY = 360.98564736629     # 地球自转角速度（相对惯性系）
DAY_S = 86400.0
J2000_JD = 2451545.0
AU_KM = 149597870.7

try:  # 可选依赖：sgp4 提供标准 SGP4/SDP4 传播
    from sgp4.api import Satrec as _Satrec
    from sgp4.api import jday as _sgp4_jday
    HAVE_SGP4 = True
except Exception:  # noqa: BLE001
    _Satrec = None  # type: ignore
    _sgp4_jday = None  # type: ignore
    HAVE_SGP4 = False


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def ensure_utc(dt: datetime) -> datetime:
    """naive 视为 UTC；带时区则换算到 UTC。"""
    if not isinstance(dt, datetime):
        raise TypeError("需要 datetime，得到 %r" % (dt,))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_iso(text: str) -> datetime:
    """解析 ``2026-11-25T00:18:00Z`` / 带微秒 / 带偏移的 ISO 时刻。"""
    t = str(text).strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    dt = datetime.fromisoformat(t)
    return ensure_utc(dt)


def utc_text(dt: datetime) -> str:
    return ensure_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def bjt_text(dt: datetime) -> str:
    return (ensure_utc(dt) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def wrap360(x: float) -> float:
    return float(x) % 360.0


def wrap180(x: float) -> float:
    v = (float(x) + 180.0) % 360.0 - 180.0
    return v


def julian_date(dt: datetime) -> float:
    return float(geo.julian_date(ensure_utc(dt)))


def gmst_deg(dt: datetime) -> float:
    return float(geo.gmst_deg(ensure_utc(dt)))


# --------------------------------------------------------------------------- #
# 坐标系：J2000(EME2000/GCRS) ↔ 真赤道真春分点(TOD) ↔ TEME
# --------------------------------------------------------------------------- #
_ARCSEC = 1.0 / 3600.0


def _r1(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1.0, 0.0, 0.0], [0.0, c, s], [0.0, -s, c]], dtype=float)


def _r2(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0.0, -s], [0.0, 1.0, 0.0], [s, 0.0, c]], dtype=float)


def _r3(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, s, 0.0], [-s, c, 0.0], [0.0, 0.0, 1.0]], dtype=float)


#: IAU 1980 章动主项：(D, M, M', F, Ω, Δψ系数[0.0001"], Δψ_T, Δε系数[0.0001"], Δε_T)
_NUT_TERMS = (
    (0, 0, 0, 0, 1, -171996.0, -174.2, 92025.0, 8.9),
    (0, 0, 0, 0, 2, -13187.0, -1.6, 5736.0, -3.1),
    (-2, 0, 2, 0, 1, -2274.0, -0.2, 977.0, -0.5),
    (2, 0, -2, 0, 0, 2062.0, 0.2, -895.0, 0.5),
    (-2, 0, 2, 0, 2, 1426.0, -3.4, 54.0, -0.1),
    (0, 0, 2, 0, 2, 712.0, 0.1, -7.0, 0.0),
    (0, 0, 0, 2, 2, -517.0, 1.2, 224.0, -0.6),
    (-2, 0, 2, 2, 1, -386.0, -0.4, 200.0, 0.0),
    (2, 0, -2, 2, 0, -301.0, 0.0, 129.0, -0.1),
    (0, 0, 2, 2, 2, 217.0, -0.5, -95.0, 0.3),
    (0, 0, 0, 2, 1, 129.0, 0.1, -70.0, 0.0),
    (0, 0, 2, 0, 1, 123.0, 0.0, -53.0, 0.0),
)


def nutation_arcsec(jd_tt: float) -> Tuple[float, float, float]:
    """IAU 1980 章动主项（12 项）→ ``(Δψ", Δε", ε0")``，精度约 0.01″。"""
    T = (jd_tt - J2000_JD) / 36525.0
    # Meeus 第 22 章基本幅角（角秒）
    l = math.radians((485866.733 + 1717915922.633 * T + 31.310 * T * T
                      + 0.064 * T ** 3) * _ARCSEC % 360.0)
    lp = math.radians((1287099.804 + 129596581.224 * T - 0.577 * T * T
                       - 0.012 * T ** 3) * _ARCSEC % 360.0)
    F = math.radians((335778.877 + 1739527263.137 * T - 13.257 * T * T
                      + 0.011 * T ** 3) * _ARCSEC % 360.0)
    D = math.radians((1072261.307 + 1602961601.328 * T - 6.891 * T * T
                      + 0.019 * T ** 3) * _ARCSEC % 360.0)
    Om = math.radians((450160.280 - 6962890.539 * T + 7.455 * T * T
                       + 0.008 * T ** 3) * _ARCSEC % 360.0)
    dpsi = 0.0
    deps = 0.0
    for cd, cm, cmp, cf, com, ps, pst, ep, ept in _NUT_TERMS:
        arg = cd * D + cm * l + cmp * lp + cf * F + com * Om
        dpsi += (ps + pst * T) * math.sin(arg)
        deps += (ep + ept * T) * math.cos(arg)
    eps0 = 84381.448 - 46.8150 * T - 0.00059 * T * T + 0.001813 * T ** 3
    return dpsi * 1e-4, deps * 1e-4, eps0


def precession_nutation_matrix(jd_tt: float) -> np.ndarray:
    """J2000 平赤道 → 当日真赤道真春分点（TOD）的旋转矩阵。"""
    T = (jd_tt - J2000_JD) / 36525.0
    zeta = math.radians((2306.2181 * T + 0.30188 * T * T + 0.017998 * T ** 3) * _ARCSEC)
    z = math.radians((2306.2181 * T + 1.09468 * T * T + 0.018203 * T ** 3) * _ARCSEC)
    theta = math.radians((2004.3109 * T - 0.42665 * T * T - 0.041833 * T ** 3) * _ARCSEC)
    P = _r3(-z) @ _r2(theta) @ _r3(-zeta)
    dpsi, deps, eps0 = nutation_arcsec(jd_tt)
    eps = math.radians((eps0 + deps) * _ARCSEC)
    dpsi_r = math.radians(dpsi * _ARCSEC)
    eps0_r = math.radians(eps0 * _ARCSEC)
    N = _r1(-eps) @ _r3(-dpsi_r) @ _r1(eps0_r)
    return N @ P


def equation_of_equinoxes_arcsec(jd_tt: float) -> float:
    """分点差（GAST−GMST）[角秒]，含 IAU 1994 补充项。"""
    dpsi, deps, eps0 = nutation_arcsec(jd_tt)
    T = (jd_tt - J2000_JD) / 36525.0
    Om = math.radians((450160.280 - 6962890.539 * T) * _ARCSEC % 360.0)
    return (dpsi * math.cos(math.radians((eps0 + deps) * _ARCSEC))
            + 0.00264 * math.sin(Om) + 0.000063 * math.sin(2.0 * Om))


def gcrs_to_teme_matrix(dt: datetime) -> np.ndarray:
    """J2000/EME2000/GCRS → TEME（工具 ECI 口径）旋转矩阵。

    说明：TEME 使用「当日真赤道、平春分点」，与 TOD 相差一个分点差转角；
    工具箱的 ECI 用 GMST 从 ECEF 反旋得到，正比于 TEME（忽略极移）。
    """
    jd_tt = julian_date(dt)  # UT≈TT，对本用途 <70 s 差异对应 <0.001″
    P = precession_nutation_matrix(jd_tt)
    eqeq = math.radians(equation_of_equinoxes_arcsec(jd_tt) * _ARCSEC)
    # 与 astropy/ERFA 一致：TEME = R3(+EqEq) · TOD（均值春分点在真春分点以西）
    return _r3(eqeq) @ P


def teme_to_gcrs_matrix(dt: datetime) -> np.ndarray:
    return gcrs_to_teme_matrix(dt).T


def vector_gcrs_to_teme(vec: Iterable[float], dt: datetime) -> np.ndarray:
    return gcrs_to_teme_matrix(dt) @ np.asarray(vec, dtype=float)


def vector_teme_to_gcrs(vec: Iterable[float], dt: datetime) -> np.ndarray:
    return teme_to_gcrs_matrix(dt) @ np.asarray(vec, dtype=float)


# --------------------------------------------------------------------------- #
# 轨道根数
# --------------------------------------------------------------------------- #
def elements_from_state(r: Sequence[float], v: Sequence[float],
                        mu: float = MU_EARTH) -> Dict[str, float]:
    """惯性系状态 → 经典轨道根数（deg / km / s）。"""
    r = np.asarray(r, dtype=float)
    v = np.asarray(v, dtype=float)
    rn = float(np.linalg.norm(r))
    vn = float(np.linalg.norm(v))
    if rn <= 0.0 or vn <= 0.0 or not np.all(np.isfinite(r)) or not np.all(np.isfinite(v)):
        raise ValueError("状态矢量无效（零或非有限值）")
    h = np.cross(r, v)
    hn = float(np.linalg.norm(h))
    if hn <= 0.0:
        raise ValueError("角动量为零，无法定轨")
    n = np.cross(np.array([0.0, 0.0, 1.0]), h)
    nn = float(np.linalg.norm(n))
    e_vec = np.cross(v, h) / mu - r / rn
    e = float(np.linalg.norm(e_vec))
    energy = vn * vn / 2.0 - mu / rn
    a = -mu / (2.0 * energy) if abs(energy) > 1e-12 else float("inf")
    i = math.degrees(math.acos(max(-1.0, min(1.0, h[2] / hn))))
    raan = math.degrees(math.atan2(n[1], n[0])) % 360.0 if nn > 1e-12 else 0.0
    argp = 0.0
    nu = 0.0
    u = 0.0
    if nn > 1e-12:
        node_hat = n / nn
        h_hat = h / hn
        u = math.degrees(math.atan2(float(np.dot(r, np.cross(h_hat, node_hat))),
                                    float(np.dot(r, node_hat)))) % 360.0
    if e > 1e-9 and nn > 1e-12:
        argp = math.degrees(math.acos(max(-1.0, min(1.0, float(np.dot(n, e_vec)) / (nn * e)))))
        if e_vec[2] < 0.0:
            argp = 360.0 - argp
        nu = math.degrees(math.acos(max(-1.0, min(1.0, float(np.dot(e_vec, r)) / (e * rn)))))
        if float(np.dot(r, v)) < 0.0:
            nu = 360.0 - nu
    elif e > 1e-12:
        nu = u  # 近圆轨道用纬度幅角代替
    period = 2.0 * math.pi * math.sqrt(a ** 3 / mu) if a > 0 and math.isfinite(a) else float("inf")
    return dict(a_km=a, e=e, i_deg=i, raan_deg=raan, argp_deg=argp % 360.0,
                nu_deg=nu % 360.0, u_deg=u, period_s=period,
                perigee_km=a * (1.0 - e) - RE_EQ if a > 0 else float("nan"),
                apogee_km=a * (1.0 + e) - RE_EQ if a > 0 else float("nan"))


def raan_deg_of_state(r: Sequence[float], v: Sequence[float]) -> float:
    h = np.cross(np.asarray(r, dtype=float), np.asarray(v, dtype=float))
    n = np.cross(np.array([0.0, 0.0, 1.0]), h)
    if float(np.linalg.norm(n)) < 1e-15:
        return 0.0
    return math.degrees(math.atan2(n[1], n[0])) % 360.0


def inclination_deg_of_state(r: Sequence[float], v: Sequence[float]) -> float:
    h = np.cross(np.asarray(r, dtype=float), np.asarray(v, dtype=float))
    hn = float(np.linalg.norm(h))
    if hn < 1e-15:
        return 0.0
    return math.degrees(math.acos(max(-1.0, min(1.0, h[2] / hn))))


def argument_of_latitude_deg(r: Sequence[float], v: Sequence[float]) -> float:
    r = np.asarray(r, dtype=float)
    h = np.cross(r, np.asarray(v, dtype=float))
    hn = float(np.linalg.norm(h))
    n = np.cross(np.array([0.0, 0.0, 1.0]), h)
    nn = float(np.linalg.norm(n))
    if hn < 1e-15 or nn < 1e-15:
        return 0.0
    h_hat = h / hn
    node_hat = n / nn
    return math.degrees(math.atan2(float(np.dot(r, np.cross(h_hat, node_hat))),
                                   float(np.dot(r, node_hat)))) % 360.0


def state_from_elements(a_km: float, e: float, i_deg: float, raan_deg: float,
                        argp_deg: float = 0.0, nu_deg: float = 0.0,
                        mu: float = MU_EARTH) -> Tuple[np.ndarray, np.ndarray]:
    """经典根数 → 惯性系状态（用于画星下点、验证）。"""
    a = float(a_km)
    e = float(e)
    inc = math.radians(i_deg)
    O = math.radians(raan_deg)
    w = math.radians(argp_deg)
    nu = math.radians(nu_deg)
    p = a * (1.0 - e * e)
    r_pf = p / (1.0 + e * math.cos(nu))
    r = np.array([r_pf * math.cos(nu), r_pf * math.sin(nu), 0.0])
    v_pf = math.sqrt(mu / p) * np.array([-math.sin(nu), e + math.cos(nu), 0.0])
    cO, sO = math.cos(O), math.sin(O)
    cw, sw = math.cos(w), math.sin(w)
    ci, si = math.cos(inc), math.sin(inc)
    R = np.array([
        [cO * cw - sO * sw * ci, -cO * sw - sO * cw * ci, sO * si],
        [sO * cw + cO * sw * ci, -sO * sw + cO * cw * ci, -cO * si],
        [sw * si, cw * si, ci],
    ])
    return R @ r, R @ v_pf


def j2_raan_rate_deg_day(a_km: float, e: float, i_deg: float) -> float:
    """J2 引起的升交点赤经长期漂移 [deg/day]。"""
    n = math.sqrt(MU_EARTH / a_km ** 3)
    p = a_km * (1.0 - e * e)
    rate = -1.5 * n * J2 * (RE_EQ / p) ** 2 * math.cos(math.radians(i_deg))
    return math.degrees(rate) * DAY_S


def sun_sync_inclination_deg(a_km: float, e: float = 0.0,
                             raan_rate_deg_day: float = 0.985647) -> float:
    """太阳同步轨道所需倾角（给定半长轴）。"""
    n = math.sqrt(MU_EARTH / a_km ** 3)
    p = a_km * (1.0 - e * e)
    target = math.radians(raan_rate_deg_day) / DAY_S
    cosi = -target / (1.5 * n * J2 * (RE_EQ / p) ** 2)
    if abs(cosi) > 1.0:
        return float("nan")
    return math.degrees(math.acos(cosi))


def sun_ra_deg(dt: datetime) -> float:
    """太阳视赤经 [deg]（Meeus 低精度，误差 ~0.01°）。"""
    ra, _dec = geo.sun_ra_dec_deg(ensure_utc(dt))
    return float(ra)


# --------------------------------------------------------------------------- #
# 发射几何：地面方位角 / 入轨点 / 轨道面
# --------------------------------------------------------------------------- #
def _site_enu(lat_deg: float, lon_deg: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    east = np.array([-math.sin(lon), math.cos(lon), 0.0])
    north = np.array([-math.sin(lat) * math.cos(lon),
                      -math.sin(lat) * math.sin(lon), math.cos(lat)])
    up = np.array([math.cos(lat) * math.cos(lon),
                   math.cos(lat) * math.sin(lon), math.sin(lat)])
    return east, north, up


def plane_from_launch(site_lat_deg: float, site_lon_deg: float,
                      alt_km: float, az_deg: float, speed_km_s: float,
                      dt: datetime, fpa_deg: float = 0.0) -> Dict[str, Any]:
    """由发射点、方位角与入轨速度直接构造入轨点状态与轨道面（TEME）。

    Parameters
    ----------
    site_lat_deg, site_lon_deg : float
        发射场（或入轨点星下点）经纬度 [deg]。
    alt_km : float
        入轨点高度 [km]。
    az_deg : float
        发射方位角 [deg]（自北顺时针，地面相对速度方向）。
    speed_km_s : float
        入轨点相对地球速度 [km/s]。
    dt : datetime
        入轨时刻（UTC）。
    fpa_deg : float
        飞行路径角 [deg]（相对当地水平，向上为正）。

    Returns
    -------
    dict
        ``r_eci, v_eci, a_km, e, i_deg, raan_deg, argp_deg, nu_deg, u_deg,
        period_s``。速度已加入地球自转（惯性系）。
    """
    lat = math.radians(float(site_lat_deg))
    az = math.radians(float(az_deg))
    fpa = math.radians(float(fpa_deg))
    r_ecef = np.asarray(geo.geodetic_to_ecef(float(site_lat_deg), float(site_lon_deg),
                                             float(alt_km) * 1000.0), dtype=float) / 1000.0
    east, north, up = _site_enu(lat_deg=float(site_lat_deg), lon_deg=float(site_lon_deg))
    hdir = math.cos(az) * north + math.sin(az) * east
    v_rel_ecef = float(speed_km_s) * (math.cos(fpa) * hdir + math.sin(fpa) * up)
    omega = 2.0 * math.pi / 86164.0905      # rad/s，恒星日
    v_abs_ecef = v_rel_ecef + omega * np.array([-r_ecef[1], r_ecef[0], 0.0])
    th = math.radians(gmst_deg(dt))
    c, s = math.cos(th), math.sin(th)
    R = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
    r_eci = R @ r_ecef
    v_eci = R @ v_abs_ecef
    el = elements_from_state(r_eci, v_eci)
    el.update(r_eci=r_eci, v_eci=v_eci)
    return el


def azimuth_for_inclination(site_lat_deg: float, alt_km: float, speed_km_s: float,
                            target_i_deg: float, dt: datetime,
                            fpa_deg: float = 0.0,
                            near_az_deg: Optional[float] = None) -> Optional[float]:
    """反解能命中目标倾角的发射方位角（两个解中取最接近 ``near_az_deg`` 者）。

    地面相对速度方位角；包含地球自转影响。无解（目标倾角低于该纬度可达最小值）
    返回 None。
    """
    target = float(target_i_deg)
    cands: List[float] = []
    # 在 [0,360] 上粗扫 + 二分：倾角曲线在 90°/270° 附近有极小值，两侧各有解；
    # 太阳同步/极轨任务常用 180°~200° 的西南射向，不能只扫 0~180°。
    grid = np.linspace(0.0, 360.0, 361)
    vals = []
    for a in grid:
        try:
            el = plane_from_launch(site_lat_deg, 0.0, alt_km, float(a), speed_km_s,
                                   dt, fpa_deg)
            vals.append(float(el["i_deg"]))
        except Exception:  # noqa: BLE001
            vals.append(float("nan"))
    vals_arr = np.asarray(vals)

    def bisect(lo: float, hi: float) -> Optional[float]:
        f_lo = vals_arr[int(round(lo))] - target
        f_hi = vals_arr[int(round(hi))] - target
        for _ in range(60):
            mid = 0.5 * (lo + hi)
            el = plane_from_launch(site_lat_deg, 0.0, alt_km, mid, speed_km_s, dt, fpa_deg)
            f_mid = float(el["i_deg"]) - target
            if abs(f_mid) < 1e-7:
                return mid
            if (f_lo < 0) == (f_mid < 0):
                lo, f_lo = mid, f_mid
            else:
                hi, f_hi = mid, f_mid
        return 0.5 * (lo + hi)

    for k in range(360):
        a0, a1 = float(grid[k]), float(grid[k + 1])
        v0, v1 = vals_arr[k], vals_arr[k + 1]
        if not (np.isfinite(v0) and np.isfinite(v1)):
            continue
        if (v0 - target) * (v1 - target) <= 0.0 and abs(v1 - v0) < 30.0:
            root = bisect(a0, a1)
            if root is not None:
                cands.append(float(root))
    if not cands:
        return None
    if near_az_deg is not None:
        return min(cands, key=lambda a: abs(wrap180(a - float(near_az_deg))))
    return cands[0]


def raan_for_ltan(dt: datetime, ltan_h: float) -> float:
    """太阳同步轨道：给定降/升交点地方时 LTAN(h) → 当日所需 RAAN [deg]。

    定义：当地平太阳时 = 12h + (RAAN − α_sun)/15°；因此
    RAAN = α_sun + 15°·(LTAN − 12)。
    """
    return wrap360(sun_ra_deg(dt) + 15.0 * (float(ltan_h) - 12.0))


def ltan_from_raan(dt: datetime, raan_deg: float) -> float:
    """RAAN [deg] → 该日升交点地方时 [h] ∈ [0,24)。"""
    return (12.0 + wrap180(float(raan_deg) - sun_ra_deg(dt)) / 15.0) % 24.0


# --------------------------------------------------------------------------- #
# TLE / SGP4
# --------------------------------------------------------------------------- #
def tle_epoch_from_line1(line1: str) -> datetime:
    """从 TLE 第一行解析历元（UTC）。"""
    s = str(line1)
    if len(s) < 32:
        raise ValueError("TLE 第一行长度不足：%r" % s)
    yy = int(s[18:20])
    day = float(s[20:32])
    year = 2000 + yy if yy < 57 else 1900 + yy
    t0 = datetime(year, 1, 1, tzinfo=timezone.utc)
    return t0 + timedelta(days=day - 1.0)


def parse_tle(text: str) -> List[Dict[str, Any]]:
    """解析一段或多段 TLE 文本 → ``[{name,line1,line2,norad_id,epoch_utc,...}]``。

    支持：带名称行的两行格式、连续两行、CRLF/LF。格式错误抛 ``ValueError``。
    """
    lines = [ln.strip() for ln in str(text or "").replace("\r", "").splitlines()]
    lines = [ln for ln in lines if ln]
    out: List[Dict[str, Any]] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("1 ") and i + 1 < len(lines) and lines[i + 1].startswith("2 "):
            name = ""
            l1, l2 = ln, lines[i + 1]
            i += 2
        elif (i + 2 < len(lines) and not ln.startswith("1 ") and not ln.startswith("2 ")
              and lines[i + 1].startswith("1 ") and lines[i + 2].startswith("2 ")):
            name, l1, l2 = ln, lines[i + 1], lines[i + 2]
            i += 3
        else:
            i += 1
            continue
        if len(l1) < 60 or len(l2) < 60:
            raise ValueError("TLE 行长度不足（应 ≥69 字符）：%r / %r" % (l1[:20], l2[:20]))
        norad = int(l1[2:7])
        epoch = tle_epoch_from_line1(l1)
        inc = float(l2[8:16])
        raan = float(l2[17:25])
        ecc = float("0." + l2[26:33].strip())
        argp = float(l2[34:42])
        ma = float(l2[43:51])
        n = float(l2[52:63])
        out.append(dict(name=name, line1=l1, line2=l2, norad_id=norad,
                        epoch_utc=utc_text(epoch), epoch_dt=epoch,
                        inc_deg=inc, raan_deg=raan, ecc=ecc, argp_deg=argp,
                        ma_deg=ma, mean_motion_rev_day=n,
                        a_km=(MU_EARTH / (n * 2.0 * math.pi / DAY_S) ** 2) ** (1.0 / 3.0)))
    if not out:
        raise ValueError("未找到有效 TLE（需要成对的 '1 ' / '2 ' 行）")
    return out


class TleSatellite:
    """一条 TLE 的 SGP4 传播器（输出 TEME 状态）。"""

    def __init__(self, line1: str, line2: str, name: str = ""):
        if not HAVE_SGP4:
            raise RuntimeError(
                "缺少 sgp4 依赖：无法用 TLE 传播空间站轨道。"
                "请在联网环境执行 `python -m pip install sgp4`，"
                "或改用官方 OEM 精密星历文件（.dat/.oem）。")
        self.line1, self.line2, self.name = line1, line2, name
        self.sat = _Satrec.twoline2rv(line1, line2)
        self.epoch = tle_epoch_from_line1(line1)
        self.norad_id = int(line1[2:7])

    def state(self, dt: datetime) -> Tuple[np.ndarray, np.ndarray]:
        """UTC → (r, v) [km, km/s]（TEME）。"""
        dt = ensure_utc(dt)
        jd, fr = _sgp4_jday(dt.year, dt.month, dt.day, dt.hour, dt.minute,
                            dt.second + dt.microsecond * 1e-6)
        err, r, v = self.sat.sgp4(jd, fr)
        if err != 0:
            raise RuntimeError("SGP4 传播失败（错误码 %d，历元 %s）" % (err, self.epoch))
        return np.asarray(r, dtype=float), np.asarray(v, dtype=float)

    def elements(self, dt: datetime) -> Dict[str, float]:
        r, v = self.state(dt)
        return elements_from_state(r, v)


# --------------------------------------------------------------------------- #
# CCSDS OEM 精密星历
# --------------------------------------------------------------------------- #
_META_KEYS = ("CREATION_DATE", "ORIGINATOR", "OBJECT_NAME", "OBJECT_ID",
              "CENTER_NAME", "REF_FRAME", "TIME_SYSTEM", "START_TIME", "STOP_TIME")


class OemEphemeris:
    """CCSDS OEM（位置+速度，等间隔或不等间隔）三次 Hermite 插值星历。

    默认按 ``REF_FRAME=EME2000`` 读取，``state(dt, frame="teme")`` 时自动转到
    工具 ECI（TEME）口径；``frame="eme2000"`` 返回原始口径。
    """

    def __init__(self, path: Optional[str] = None, text: Optional[str] = None,
                 source: str = ""):
        if text is None:
            if not path:
                raise ValueError("OemEphemeris 需要 path 或 text")
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        self.source = source or (path or "<text>")
        self.meta: Dict[str, str] = {}
        for key in _META_KEYS:
            m = re.search(r"%s\s*=\s*(\S+)" % key, text)
            self.meta[key] = m.group(1) if m else ""
        times: List[datetime] = []
        states: List[Tuple[float, float, float, float, float, float]] = []
        for line in text.replace("\r", "").splitlines():
            s = line.strip()
            if not s or not s[0].isdigit():
                continue
            parts = s.split()
            if len(parts) < 7:
                continue
            try:
                dt = parse_iso(parts[0])
                vals = tuple(float(x) for x in parts[1:7])
            except (ValueError, TypeError):
                continue
            times.append(dt)
            states.append(vals)  # type: ignore[arg-type]
        if len(times) < 2:
            raise ValueError("OEM 星历有效数据点不足（%d）；请检查文件格式" % len(times))
        order = sorted(range(len(times)), key=lambda k: times[k])
        self.times = [times[k] for k in order]
        self.states = np.asarray([states[k] for k in order], dtype=float)
        self.t0 = self.times[0]
        self.t1 = self.times[-1]
        self.sec = np.asarray([(t - self.t0).total_seconds() for t in self.times], dtype=float)
        self.frame_name = (self.meta.get("REF_FRAME") or "EME2000").upper()
        if "EME" not in self.frame_name and "J2000" not in self.frame_name \
                and "GCRS" not in self.frame_name:
            raise ValueError("OEM 参考系「%s」不受支持（需要 EME2000/J2000/GCRS）"
                             % self.frame_name)

    # -- 元信息 ------------------------------------------------------------ #
    @property
    def coverage(self) -> Tuple[datetime, datetime]:
        return self.t0, self.t1

    def covers(self, dt: datetime) -> bool:
        dt = ensure_utc(dt)
        return self.t0 <= dt <= self.t1

    def info(self) -> Dict[str, Any]:
        return dict(object_name=self.meta.get("OBJECT_NAME", ""),
                    object_id=self.meta.get("OBJECT_ID", ""),
                    originator=self.meta.get("ORIGINATOR", ""),
                    frame=self.frame_name,
                    start_utc=utc_text(self.t0), stop_utc=utc_text(self.t1),
                    n_points=int(len(self.times)),
                    step_s=float(np.median(np.diff(self.sec))) if len(self.sec) > 1 else 0.0,
                    source=self.source)

    # -- 插值 -------------------------------------------------------------- #
    def _bracket(self, dt: datetime) -> Tuple[int, float]:
        dt = ensure_utc(dt)
        if not self.covers(dt):
            raise ValueError("时刻 %s 超出 OEM 星历覆盖区间 %s ~ %s"
                             % (utc_text(dt), utc_text(self.t0), utc_text(self.t1)))
        s = (dt - self.t0).total_seconds()
        i = int(np.searchsorted(self.sec, s, side="right") - 1)
        i = max(0, min(i, len(self.sec) - 2))
        h = float(self.sec[i + 1] - self.sec[i])
        return i, (s - float(self.sec[i])) / h

    def state(self, dt: datetime, frame: str = "teme") -> Tuple[np.ndarray, np.ndarray]:
        """插值得到 ``(r, v)``；``frame`` ∈ {'teme','eme2000'}。"""
        i, tau = self._bracket(dt)
        h = float(self.sec[i + 1] - self.sec[i])
        r0, v0 = self.states[i, 0:3], self.states[i, 3:6]
        r1, v1 = self.states[i + 1, 0:3], self.states[i + 1, 3:6]
        t2, t3 = tau * tau, tau * tau * tau
        h00 = 2 * t3 - 3 * t2 + 1
        h10 = t3 - 2 * t2 + tau
        h01 = -2 * t3 + 3 * t2
        h11 = t3 - t2
        dh00 = 6 * t2 - 6 * tau
        dh10 = 3 * t2 - 4 * tau + 1
        dh01 = -6 * t2 + 6 * tau
        dh11 = 3 * t2 - 2 * tau
        r = h00 * r0 + h10 * h * v0 + h01 * r1 + h11 * h * v1
        v = (dh00 / h) * r0 + dh10 * v0 + (dh01 / h) * r1 + dh11 * v1
        if str(frame).lower() in ("teme", "eci"):
            M = gcrs_to_teme_matrix(dt)
            r = M @ r
            v = M @ v
        elif str(frame).lower() not in ("eme2000", "j2000", "gcrs"):
            raise ValueError("未知坐标口径：%s" % frame)
        return np.asarray(r, dtype=float), np.asarray(v, dtype=float)

    def elements(self, dt: datetime, frame: str = "teme") -> Dict[str, float]:
        r, v = self.state(dt, frame=frame)
        return elements_from_state(r, v)


# --------------------------------------------------------------------------- #
# 传播 / 转移
# --------------------------------------------------------------------------- #
def _accel_j2(r: np.ndarray, mu: float = MU_EARTH) -> np.ndarray:
    rn = float(np.linalg.norm(r))
    if rn <= 0.0:
        return np.zeros(3)
    a = -mu * r / rn ** 3
    z2 = (r[2] / rn) ** 2
    fac = 1.5 * J2 * mu * RE_EQ ** 2 / rn ** 5
    a = a + fac * np.array([r[0] * (5.0 * z2 - 1.0),
                            r[1] * (5.0 * z2 - 1.0),
                            r[2] * (5.0 * z2 - 3.0)])
    return a


def propagate_rk4(r: Sequence[float], v: Sequence[float], dt_s: float,
                  step_s: float = 10.0, j2: bool = True,
                  mu: float = MU_EARTH) -> Tuple[np.ndarray, np.ndarray]:
    """二体(+J2) RK4 递推；``dt_s`` 可正可负。"""
    r = np.asarray(r, dtype=float).copy()
    v = np.asarray(v, dtype=float).copy()
    remain = float(dt_s)
    sign = 1.0 if remain >= 0 else -1.0
    step_s = max(1e-3, abs(float(step_s)))
    while abs(remain) > 1e-9:
        h = sign * min(step_s, abs(remain))

        def f(rr, vv):
            a = _accel_j2(rr, mu) if j2 else -mu * rr / np.linalg.norm(rr) ** 3
            return vv, a

        k1r, k1v = f(r, v)
        k2r, k2v = f(r + 0.5 * h * k1r, v + 0.5 * h * k1v)
        k3r, k3v = f(r + 0.5 * h * k2r, v + 0.5 * h * k2v)
        k4r, k4v = f(r + h * k3r, v + h * k3v)
        r = r + (h / 6.0) * (k1r + 2 * k2r + 2 * k3r + k4r)
        v = v + (h / 6.0) * (k1v + 2 * k2v + 2 * k3v + k4v)
        remain -= h
    return r, v


def propagate_rk4_track(r: Sequence[float], v: Sequence[float], dt_s: float,
                        step_s: float = 30.0, j2: bool = True,
                        mu: float = MU_EARTH) -> Tuple[np.ndarray, np.ndarray, list]:
    """与 :func:`propagate_rk4` 相同的二体(+J2) RK4，但返回中间采样。

    返回 ``(r, v, samples)``，``samples`` 为 ``[(t_offset_s, r, v), ...]``，
    包含起点（t=0）与终点，供画全流程轨迹/动画使用。
    """
    r = np.asarray(r, dtype=float).copy()
    v = np.asarray(v, dtype=float).copy()
    remain = float(dt_s)
    sign = 1.0 if remain >= 0 else -1.0
    step_s = max(1e-3, abs(float(step_s)))
    samples = [(0.0, r.copy(), v.copy())]
    done = 0.0
    while abs(remain) > 1e-9:
        h = sign * min(step_s, abs(remain))

        def f(rr, vv):
            a = _accel_j2(rr, mu) if j2 else -mu * rr / np.linalg.norm(rr) ** 3
            return vv, a

        k1r, k1v = f(r, v)
        k2r, k2v = f(r + 0.5 * h * k1r, v + 0.5 * h * k1v)
        k3r, k3v = f(r + 0.5 * h * k2r, v + 0.5 * h * k2v)
        k4r, k4v = f(r + h * k3r, v + h * k3v)
        r = r + (h / 6.0) * (k1r + 2 * k2r + 2 * k3r + k4r)
        v = v + (h / 6.0) * (k1v + 2 * k2v + 2 * k3v + k4v)
        remain -= h
        done += h
        samples.append((done, r.copy(), v.copy()))
    return r, v, samples


def hohmann_dv(r1_km: float, r2_km: float, mu: float = MU_EARTH) -> Tuple[float, float, float]:
    """圆轨道 r1 → r2 的 Hohmann 双脉冲 ``(Δv1, Δv2, 转移时间 s)``。"""
    a_t = 0.5 * (r1_km + r2_km)
    v1 = math.sqrt(mu / r1_km)
    v2 = math.sqrt(mu / r2_km)
    vp = math.sqrt(mu * (2.0 / r1_km - 1.0 / a_t))
    va = math.sqrt(mu * (2.0 / r2_km - 1.0 / a_t))
    tof = math.pi * math.sqrt(a_t ** 3 / mu)
    return abs(vp - v1), abs(v2 - va), tof


# --------------------------------------------------------------------------- #
# Lambert 转移（universal variables，Vallado 算法）
# --------------------------------------------------------------------------- #
def _stumpff_c2c3(z: float) -> Tuple[float, float]:
    if z > 1e-8:
        sz = math.sqrt(z)
        c2 = (1.0 - math.cos(sz)) / z
        c3 = (sz - math.sin(sz)) / (sz ** 3)
    elif z < -1e-8:
        sz = math.sqrt(-z)
        c2 = (math.cosh(sz) - 1.0) / (-z)
        c3 = (math.sinh(sz) - sz) / (sz ** 3)
    else:
        c2 = 0.5
        c3 = 1.0 / 6.0
    return c2, c3


def lambert(r1: Sequence[float], r2: Sequence[float], tof_s: float,
            mu: float = MU_EARTH, prograde: bool = True
            ) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    """Lambert 问题：给定两端位置与飞行时间，求初/末速度（km/s）。

    返回 ``(v1, v2)``；无解（几何退化/时间非正）返回 None。
    使用通用变量 + 二分求解 z，适用于椭圆/双曲转移。
    """
    r1 = np.asarray(r1, dtype=float)
    r2 = np.asarray(r2, dtype=float)
    r1n = float(np.linalg.norm(r1))
    r2n = float(np.linalg.norm(r2))
    tof_s = float(tof_s)
    if r1n < 1.0 or r2n < 1.0 or tof_s <= 0.0:
        return None
    cross = np.cross(r1, r2)
    cosd = float(np.dot(r1, r2) / (r1n * r2n))
    cosd = max(-1.0, min(1.0, cosd))
    dtheta = math.acos(cosd)
    if prograde and cross[2] < 0.0:
        dtheta = 2.0 * math.pi - dtheta
    elif (not prograde) and cross[2] > 0.0:
        dtheta = 2.0 * math.pi - dtheta
    sin_d = math.sin(dtheta)
    if abs(sin_d) < 1e-10:
        return None
    A = sin_d * math.sqrt(r1n * r2n / (1.0 - cosd))
    if abs(A) < 1e-12:
        return None

    def y_of(z: float) -> float:
        c2, c3 = _stumpff_c2c3(z)
        if c2 <= 1e-14:
            return -1.0
        return r1n + r2n + A * (z * c3 - 1.0) / math.sqrt(c2)

    def F(z: float) -> float:
        y = y_of(z)
        if y < 0.0:
            return float("nan")
        c2, c3 = _stumpff_c2c3(z)
        return ((y / c2) ** 1.5) * c3 + A * math.sqrt(y) - math.sqrt(mu) * tof_s

    # 搜索使 F(z)=0 的 z：F 单调递增；先找一个包含根的小区间
    z_lo, z_hi = -4.0 * math.pi ** 2, 4.0 * math.pi ** 2 - 1e-2
    f_lo, f_hi = F(z_lo), F(z_hi)
    tries = 0
    while (not np.isfinite(f_lo) or f_lo > 0) and tries < 40:
        z_lo *= 0.5
        f_lo = F(z_lo)
        tries += 1
    tries = 0
    while (not np.isfinite(f_hi) or f_hi < 0) and tries < 60:
        z_hi *= 0.5
        f_hi = F(z_hi)
        tries += 1
    if not (np.isfinite(f_lo) and np.isfinite(f_hi)) or f_lo > 0 or f_hi < 0:
        return None
    for _ in range(200):
        z_mid = 0.5 * (z_lo + z_hi)
        f_mid = F(z_mid)
        if not np.isfinite(f_mid):
            z_hi = z_mid
            continue
        if f_mid < 0:
            z_lo = z_mid
        else:
            z_hi = z_mid
        if abs(z_hi - z_lo) < 1e-10:
            break
    z = 0.5 * (z_lo + z_hi)
    y = y_of(z)
    if y <= 0.0:
        return None
    f = 1.0 - y / r1n
    g = A * math.sqrt(y / mu)
    gdot = 1.0 - y / r2n
    if abs(g) < 1e-12:
        return None
    v1 = (r2 - f * r1) / g
    v2 = (gdot * r2 - r1) / g
    return np.asarray(v1, dtype=float), np.asarray(v2, dtype=float)


def phasing_plan(r_sc_km: float, r_st_km: float, phase_lead_deg: float,
                 max_orbits: int = 10, min_orbits: int = 1,
                 preferred_s: Optional[float] = None,
                 time_tol_s: Optional[float] = None,
                 max_dv_mps: Optional[float] = None,
                 max_total_s: Optional[float] = None,
                 v_sc_kms: Optional[float] = None,
                 mu: float = MU_EARTH) -> Optional[Dict[str, Any]]:
    """追及式相位调整 + Hohmann 交会方案（CZ-2F/神舟类任务用）。

    模型（共面圆轨道，工程估算）
    ----------------------------
    航天器在半径 ``r_sc`` 圆轨道入轨，目标在 ``r_st`` 圆轨道、初始领先
    ``phase_lead_deg``。方案：先在近圆「相位轨道」绕 ``k`` 圈（k≥1）调整相位，
    再点火进入半个 Hohmann 椭圆到达目标高度并圆化；``k=0`` 表示不绕相位圈、
    直接 Hohmann（仅当初始相位已接近交会几何时成立）。

    相位段结束时的几何条件：航天器将走完 180° 的转移弧，目标在此期间走
    ``n_st·T_H``；因此点火时目标只需领先 ``180° − n_st·T_H``（200→380 km
    约 3.6°）。给定 k 可反解相位轨道平均角速度与半长轴。

    Parameters
    ----------
    r_sc_km, r_st_km : float
        航天器入轨半径 / 目标轨道半径（地心距）。
    phase_lead_deg : float
        入轨时刻目标相对航天器的纬度幅角差（目标在前为正）。
    max_orbits, min_orbits : int
        相位段绕行圈数范围。
    preferred_s : float, optional
        期望交会总时长（秒）；给了会优先选择时长接近的方案。
    time_tol_s : float, optional
        允许的时长偏差（秒），默认 ``0.25*preferred_s``。
    max_dv_mps : float, optional
        总 ΔV 上限（m/s）；超限方案不返回。

    Returns
    -------
    dict or None
        ``{k, phasing_a_km, dv_phase_mps, dv_transfer_mps, dv_circular_mps,
        dv_total_mps, phasing_s, transfer_s, total_s, phase_lead_deg,
        target_lead_deg, within_time}``；无可行解返回 None。
    """
    if r_sc_km <= RE_EQ or r_st_km <= RE_EQ or phase_lead_deg is None:
        return None
    n_st = math.sqrt(mu / r_st_km ** 3)
    a_t = 0.5 * (r_sc_km + r_st_km)
    tof = math.pi * math.sqrt(a_t ** 3 / mu)
    target_lead = math.pi - n_st * tof
    delta = math.remainder(math.radians(float(phase_lead_deg)) - target_lead,
                           2.0 * math.pi)
    v_before = float(v_sc_kms) if v_sc_kms else math.sqrt(mu / r_sc_km)
    v_t1 = math.sqrt(mu * (2.0 / r_sc_km - 1.0 / a_t))
    v_t2 = math.sqrt(mu * (2.0 / r_st_km - 1.0 / a_t))
    v_st = math.sqrt(mu / r_st_km)
    dv_circ = abs(v_st - v_t2)

    candidates: List[Dict[str, Any]] = []

    # k=0：不入相位轨道，直接 Hohmann（仅当初始相位已接近目标几何）
    if abs(delta) <= math.radians(0.5):
        dv_first = v_t1 - v_before
        total = abs(dv_first) + dv_circ
        candidates.append(dict(
            k=0, phasing_a_km=r_sc_km, dv_phase_mps=0.0,
            dv_transfer_mps=abs(dv_first) * 1000.0,
            dv_circular_mps=dv_circ * 1000.0,
            dv_phase_signed_mps=0.0,
            dv_transfer_signed_mps=dv_first * 1000.0,
            dv_circular_signed_mps=(v_st - v_t2) * 1000.0,
            dv_total_mps=total * 1000.0,
            phasing_s=0.0, transfer_s=tof, total_s=tof,
            phase_lead_deg=float(phase_lead_deg),
            target_lead_deg=math.degrees(target_lead)))

    for k in range(max(1, int(min_orbits)), max(0, int(max_orbits)) + 1):
        denom = 1.0 - delta / (2.0 * math.pi * k)
        if abs(denom) < 1e-6:
            continue
        n_p = n_st / denom
        if n_p <= 0:
            continue
        a_p = (mu / n_p ** 2) ** (1.0 / 3.0)
        # 在 r_sc 处沿切向点火后，新轨道半长轴为 a_p、经过 r_sc。
        # 其速度由活力公式给出；r_sc 不一定是新轨道的拱点，因此必须用
        # 能量/角动量反算近地点，避免"相位轨道穿入大气层"。
        v_after = math.sqrt(mu * (2.0 / r_sc_km - 1.0 / a_p))
        energy = v_after * v_after / 2.0 - mu / r_sc_km
        h_ang = r_sc_km * v_after
        ecc = math.sqrt(max(0.0, 1.0 + 2.0 * energy * h_ang * h_ang / (mu * mu)))
        r_peri = a_p * (1.0 - ecc)
        if r_peri <= RE_EQ + 80.0 or a_p <= RE_EQ + 90.0:
            continue
        dv_phase = abs(v_after - v_before)
        dv_transfer = abs(v_t1 - v_after)
        phasing_s = 2.0 * math.pi * k / n_p
        total = dv_phase + dv_transfer + dv_circ
        candidates.append(dict(
            k=k, phasing_a_km=a_p, dv_phase_mps=dv_phase * 1000.0,
            dv_transfer_mps=dv_transfer * 1000.0,
            dv_circular_mps=dv_circ * 1000.0,
            dv_phase_signed_mps=(v_after - v_before) * 1000.0,
            dv_transfer_signed_mps=(v_t1 - v_after) * 1000.0,
            dv_circular_signed_mps=(v_st - v_t2) * 1000.0,
            dv_total_mps=total * 1000.0,
            phasing_s=phasing_s, transfer_s=tof,
            total_s=phasing_s + tof,
            phase_lead_deg=float(phase_lead_deg),
            target_lead_deg=math.degrees(target_lead)))

    if not candidates:
        return None
    if max_dv_mps is not None:
        candidates = [c for c in candidates if c["dv_total_mps"] <= float(max_dv_mps)]
    if max_total_s is not None:
        candidates = [c for c in candidates if c["total_s"] <= float(max_total_s)]
    if not candidates:
        return None
    for c in candidates:
        c["within_time"] = True
    if preferred_s:
        pref = float(preferred_s)
        tol = float(time_tol_s) if time_tol_s is not None else 0.25 * pref
        for c in candidates:
            c["within_time"] = abs(c["total_s"] - pref) <= tol
        near = [c for c in candidates if c["within_time"]]
    else:
        near = []
    # 优先选择落在任务期望交会时间窗内的方案（其中取 ΔV 最小）；
    # 若没有，则在全部可行方案中取 ΔV 最小。
    best = min(near or candidates, key=lambda c: c["dv_total_mps"])
    if not near:
        best["within_time"] = False
    best["n_candidates"] = len(candidates)
    return best


def phasing_plan_best(r_sc_km: float, r_st_km: float, phase_lead_deg: float,
                      *, max_orbits: int = 10, min_orbits: int = 1,
                      preferred_s: float = 23400.0, time_tol_s: float = 5850.0,
                      max_dv_mps: float = 250.0, max_total_s: float = 43200.0,
                      final_dv_cap_mps: float = 25.0,
                      v_sc_kms: float | None = None) -> dict | None:
    """在相位圈数 k 上做选择，目标是「含末段逼近预算的总 ΔV 最小」。

    为什么需要：:func:`phasing_plan` 只按相位/转移/圆化的 ΔV 选圈数，
    末段 Lambert 还要花 10~20 m/s；k=4 的相位方案可能 304 m/s（超 300 预算），
    而 k=5/6 的总 ΔV 反而更低。这里把末段预算 ``final_dv_cap_mps`` 计入后
    逐个 k 试算，按「可行 → 接近期望时长 → 总 ΔV」排序取优。
    """
    cands = []
    for k in range(max(1, int(min_orbits)), int(max_orbits) + 1):
        try:
            p = phasing_plan(r_sc_km, r_st_km, phase_lead_deg,
                             max_orbits=k, min_orbits=k,
                             preferred_s=preferred_s, time_tol_s=time_tol_s,
                             max_dv_mps=max_dv_mps, max_total_s=max_total_s,
                             v_sc_kms=v_sc_kms)
        except Exception:  # noqa: BLE001
            p = None
        if not p:
            continue
        # 末段逼近之外还要一次"对接制动"（量级与末段相当），按 2.2× 末段预算计入，
        # 否则会挑到"相位 ΔV 低、但含末段后超预算"的圈数。
        est = (float(p.get("dv_phase_mps") or 0.0)
               + float(p.get("dv_transfer_mps") or 0.0)
               + float(p.get("dv_circular_mps") or 0.0)
               + 2.2 * float(final_dv_cap_mps))
        dur_dev = abs(float(p.get("total_s") or 0.0) - float(preferred_s))
        cands.append((0 if est <= float(max_dv_mps) else 1,
                      round(dur_dev / 60.0, 1), round(est, 2), p))
    if not cands:
        return None
    cands.sort(key=lambda x: x[:3])
    return cands[0][3]


def relative_phase_deg(r_sc: Sequence[float], v_sc: Sequence[float],
                       r_st: Sequence[float], v_st: Sequence[float]) -> float:
    """目标相对航天器的纬度幅角差（目标在前为正，[-180,180)）。"""
    u_sc = argument_of_latitude_deg(r_sc, v_sc)
    u_st = argument_of_latitude_deg(r_st, v_st)
    return wrap180(u_st - u_sc)


def _lvlh_basis(r_st: Sequence[float], v_st: Sequence[float]
                ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    r = np.asarray(r_st, dtype=float)
    v = np.asarray(v_st, dtype=float)
    er = _unit_vec(r)
    h = np.cross(r, v)
    eh = _unit_vec(h)
    et = _unit_vec(np.cross(eh, er))
    return er, et, eh


def _unit_vec(v: Sequence[float]) -> np.ndarray:
    a = np.asarray(v, dtype=float)
    n = float(np.linalg.norm(a))
    return a / n if n > 1e-12 else a


def cw_rendezvous(r_sc: Sequence[float], v_sc: Sequence[float],
                  r_st: Sequence[float], v_st: Sequence[float],
                  tof_s: float) -> Optional[Dict[str, Any]]:
    """Clohessy–Wiltshire 两脉冲近距交会（相对导航段用）。

    在目标 LVLH 系内，给定当前相对位置/速度，求初始相对速度使
    ``tof_s`` 后相对位置归零；返回两段 ECI 速度增量与预测末端相对速度。

    Returns
    -------
    dict or None
        ``dv1_vec, dv2_vec (ECI, km/s), dv1_mps, dv2_mps, rel_pos0_km,
        rel_vel0_mps, tof_s``。
    """
    tof_s = float(tof_s)
    if tof_s <= 0.0:
        return None
    r_st = np.asarray(r_st, dtype=float)
    v_st = np.asarray(v_st, dtype=float)
    r_sc = np.asarray(r_sc, dtype=float)
    v_sc = np.asarray(v_sc, dtype=float)
    rn = float(np.linalg.norm(r_st))
    n = math.sqrt(MU_EARTH / rn ** 3)
    er, et, eh = _lvlh_basis(r_st, v_st)
    dr = r_sc - r_st
    dv = v_sc - v_st
    x, y, z = float(dr @ er), float(dr @ et), float(dr @ eh)
    vx, vy, vz = float(dv @ er), float(dv @ et), float(dv @ eh)
    c, s = math.cos(n * tof_s), math.sin(n * tof_s)
    # 求初始相对速度 (vx0, vy0) 使 x(T)=y(T)=0
    A = np.array([[s / n, 2.0 * (1.0 - c) / n],
                  [2.0 * (c - 1.0) / n, (4.0 * s - 3.0 * n * tof_s) / n]], dtype=float)
    b = -np.array([(4.0 - 3.0 * c) * x,
                   y + 6.0 * (s - n * tof_s) * x], dtype=float)
    try:
        vxy0 = np.linalg.solve(A, b)
    except np.linalg.LinAlgError:
        return None
    vx0, vy0 = float(vxy0[0]), float(vxy0[1])
    vz0 = -n * z * c / s if abs(s) > 1e-8 else 0.0
    # 预测末端相对速度
    vxT = 3.0 * n * s * x + c * vx0 + 2.0 * s * vy0
    vyT = 6.0 * n * (c - 1.0) * x - 2.0 * s * vx0 + (4.0 * c - 3.0) * vy0
    vzT = -n * s * z + c * vz0
    dv1_l = np.array([vx0 - vx, vy0 - vy, vz0 - vz])
    dv2_l = -np.array([vxT, vyT, vzT])
    B = np.column_stack([er, et, eh])
    dv1_eci = B @ dv1_l
    dv2_eci = B @ dv2_l
    return dict(dv1_vec=dv1_eci, dv2_vec=dv2_eci,
                dv1_mps=float(np.linalg.norm(dv1_eci)) * 1000.0,
                dv2_mps=float(np.linalg.norm(dv2_eci)) * 1000.0,
                rel_pos0_km=float(math.sqrt(x * x + y * y + z * z)),
                rel_vel0_mps=float(math.sqrt(vx * vx + vy * vy + vz * vz)) * 1000.0,
                tof_s=tof_s)


# --------------------------------------------------------------------------- #
# 辅助：星下点
# --------------------------------------------------------------------------- #
def subpoint(dt: datetime, r_teme: Sequence[float]) -> Tuple[float, float, float]:
    """TEME 位置 → 星下点 ``(lat_deg, lon_deg, alt_km)``。"""
    r_ecef_km = geo.eci_to_ecef(np.asarray(r_teme, dtype=float), ensure_utc(dt))
    lat, lon, alt_m = geo.ecef_to_geodetic(float(r_ecef_km[0] * 1000.0),
                                           float(r_ecef_km[1] * 1000.0),
                                           float(r_ecef_km[2] * 1000.0))
    return float(lat), float(lon), float(alt_m) / 1000.0


def sunlit(dt: datetime, r_teme: Sequence[float]) -> bool:
    """卫星是否处于地球本影外（简化圆柱本影，太阳视为无穷远）。"""
    r = np.asarray(r_teme, dtype=float)
    s = geo.sun_eci_unit(ensure_utc(dt))
    # 太阳方向与位置矢量夹角 > 90+arcsin(Re/r) 时进入本影
    cos_sep = float(np.dot(r, s) / np.linalg.norm(r))
    sep = math.acos(max(-1.0, min(1.0, cos_sep)))
    earth_ang = math.asin(min(1.0, RE_EQ / np.linalg.norm(r)))
    return sep > (math.pi / 2.0 + earth_ang)


def access_from_site(dt: datetime, r_teme: Sequence[float],
                     lat_deg: float, lon_deg: float, alt_m: float = 0.0,
                     min_el_deg: float = 0.0) -> Dict[str, float]:
    """地面站 → 卫星的方位/仰角/斜距（TEME 输入）。"""
    r_km = geo.eci_to_ecef(np.asarray(r_teme, dtype=float), ensure_utc(dt))
    r_ecef_m = (float(r_km[0]) * 1000.0, float(r_km[1]) * 1000.0, float(r_km[2]) * 1000.0)
    az, el = geo.az_el_from_observer(lat_deg, lon_deg, alt_m, r_ecef_m)
    rng = geo.slant_range_km(lat_deg, lon_deg, alt_m, r_ecef_m)
    return dict(az_deg=float(az), el_deg=float(el), range_km=float(rng),
                visible=bool(el >= min_el_deg))


__all__ = [
    "MU_EARTH", "RE_EQ", "J2", "HAVE_SGP4",
    "ensure_utc", "parse_iso", "utc_text", "bjt_text", "wrap360", "wrap180",
    "julian_date", "gmst_deg",
    "nutation_arcsec", "precession_nutation_matrix", "gcrs_to_teme_matrix",
    "teme_to_gcrs_matrix", "vector_gcrs_to_teme", "vector_teme_to_gcrs",
    "elements_from_state", "state_from_elements", "raan_deg_of_state",
    "inclination_deg_of_state", "argument_of_latitude_deg",
    "j2_raan_rate_deg_day", "sun_sync_inclination_deg",
    "sun_ra_deg", "raan_for_ltan", "ltan_from_raan",
    "parse_tle", "tle_epoch_from_line1", "TleSatellite", "OemEphemeris",
    "propagate_rk4", "hohmann_dv", "phasing_plan", "relative_phase_deg",
    "subpoint", "sunlit", "access_from_site",
]
