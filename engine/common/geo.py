# -*- coding: utf-8 -*-
"""common.geo — WGS-84 大地测量 / ECI-ECEF 变换 / 太阳位置 / 观测方位高度角 基础库。

设计原则
--------
* **纯 numpy**，不依赖任何外部包（scipy/matplotlib 都不用），可直接被 sim/sky/map/viz 复用。
* 角度输入/输出统一为 **度**（函数名带 ``_rad`` 者除外）；长度单位在函数名或 docstring 中标注。
* 全部函数对标量、list、numpy 数组（含广播）尽量通用；返回 numpy 标量/数组。
* 不做 I/O，不读文件，不画图。

坐标与时间约定
--------------
* 坐标系：WGS-84（a = 6378137 m, f = 1/298.257223563）。
* 高程：**WGS-84 椭球高**（不是海拔正高；DEM 的 EGM96 正高与椭球高在本区域差约 -20 m，
  见 ``common/dem.py`` 说明）。
* 时间：``datetime.datetime``（naive 表示 **UTC**）；MET 秒由调用方换算。
  所有 ``*_ecef`` / ``*_eci`` 变换按 **GMST** 旋转，**忽略极移与章动**
  （极移 < 0.0002°，章动对赤经影响 < 0.005°；本任务 ~0.001° 精度足够）。

主要精度
--------
* ``geodetic_to_ecef`` / ``ecef_to_geodetic``：往返 < 1e-6 m（迭代到收敛）。
* ``gmst_rad``：IAU-82 多项式，1900–2100 内误差 ~0.001°（3.6"）。
* 太阳位置：Meeus《Astronomical Algorithms》第 25 章低精度公式，视黄经误差 ~0.01°，
  赤纬/时差误差 ~0.01°/0.1 min（优于任务要求的 0.1°）。
* 大圆：球面近似，半径取 IUGG 平均 6371.0088 km（与 WGS-84 测地线相差 < 0.5%）。
* WGS-84 测地线：Vincenty 反解/正解，mm 级（对 200 km 量级）。

参考
----
* NIMA TR8350.2 (WGS-84)
* Vallado, *Fundamentals of Astrodynamics and Applications*, GMST(IAU-82) 公式
* Meeus, *Astronomical Algorithms* 2nd ed., Ch.12 (GMST) / Ch.22 (obliquity) / Ch.25 (Sun) / Ch.28 (EoT)
* Vincenty, T. (1975), *Direct and Inverse Solutions of Geodesics on the Ellipsoid*
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np

__all__ = [
    # 常量
    "WGS84_A", "WGS84_F", "WGS84_B", "WGS84_E2", "WGS84_EP2", "EARTH_MEAN_R_KM",
    "J2000_JD", "OMEGA_EARTH",
    # 角度/数值工具
    "deg2rad", "rad2deg", "wrap360", "wrap180", "unit_vector",
    # 大地 ↔ ECEF
    "geodetic_to_ecef", "ecef_to_geodetic", "ecef_to_geodetic_fast",
    # 时间 / 恒星时
    "julian_date", "jd_to_datetime", "gmst_rad", "gmst_deg",
    "mean_obliquity_deg", "equation_of_time_min",
    # ECI/ECEF
    "ecef_to_eci", "eci_to_ecef", "eci_to_ecef_mat",
    # 太阳
    "sun_apparent", "sun_ra_dec_deg", "sun_declination_deg", "sun_eci_unit",
    "sun_ecef_unit", "sun_subpoint", "solar_elevation_deg", "solar_azimuth_deg",
    "solar_zenith_deg", "terminator_lat_for_lon",
    # 观测几何
    "enu_basis", "ecef_to_enu", "enu_to_ecef", "az_el_from_observer",
    "az_el_from_geodetic", "slant_range_km", "geodetic_to_enu_offset",
    # 距离 / 方位
    "great_circle", "great_circle_distance_km", "initial_bearing_deg",
    "geodesic_inverse", "geodesic_distance", "geodesic_direct",
    "downrange_crossrange",
]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
WGS84_A = 6378137.0                      # 长半轴 a [m]
WGS84_F = 1.0 / 298.257223563            # 扁率 f
WGS84_B = WGS84_A * (1.0 - WGS84_F)      # 短半轴 b [m] = 6356752.314245
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)     # 第一偏心率平方 e²
WGS84_EP2 = WGS84_E2 / (1.0 - WGS84_E2)  # 第二偏心率平方 e'²
EARTH_MEAN_R_KM = 6371.0088              # IUGG 平均地球半径 [km]
EARTH_MEAN_R_M = EARTH_MEAN_R_KM * 1000.0
J2000_JD = 2451545.0                     # J2000.0 儒略日 (2000-01-01T12:00:00 TT)
OMEGA_EARTH = 7.2921150e-5               # 地球自转角速度 [rad/s]（ECI↔ECEF 速度修正用）
DEG2RAD = math.pi / 180.0
RAD2DEG = 180.0 / math.pi


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def deg2rad(deg):
    """度 → 弧度（数组可用）。"""
    return np.deg2rad(deg)


def rad2deg(rad):
    """弧度 → 度（数组可用）。"""
    return np.rad2deg(rad)


def wrap360(deg):
    """角度归一到 [0, 360) 度。"""
    return np.mod(np.asarray(deg, dtype=float), 360.0)


def wrap180(deg):
    """角度归一到 (-180, 180] 度。"""
    x = np.mod(np.asarray(deg, dtype=float) + 180.0, 360.0) - 180.0
    return np.where(x <= -180.0, x + 360.0, x)


def unit_vector(v, axis=-1):
    """把向量（最后一维为 xyz）归一化为单位向量。零向量返回自身。"""
    v = np.asarray(v, dtype=float)
    n = np.linalg.norm(v, axis=axis, keepdims=True)
    n = np.where(n == 0.0, 1.0, n)
    return v / n


# --------------------------------------------------------------------------- #
# 大地坐标 ↔ ECEF
# --------------------------------------------------------------------------- #
def geodetic_to_ecef(lat_deg, lon_deg, h_m=0.0):
    """WGS-84 大地坐标 → ECEF 笛卡尔坐标。

    Parameters
    ----------
    lat_deg, lon_deg : float or array_like
        大地纬度 / 经度 [deg]。lat ∈ [-90, 90]。
    h_m : float or array_like
        椭球高 [m]（**不是**海拔正高）。

    Returns
    -------
    (N, 3) ndarray or (3,) ndarray
        ECEF 位置 [m]，分量顺序 (x, y, z)。输入为标量时返回 shape (3,)。
    """
    lat = np.deg2rad(np.asarray(lat_deg, dtype=float))
    lon = np.deg2rad(np.asarray(lon_deg, dtype=float))
    h = np.asarray(h_m, dtype=float)
    sin_lat, cos_lat = np.sin(lat), np.cos(lat)
    N = WGS84_A / np.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)   # 卯酉圈曲率半径
    x = (N + h) * cos_lat * np.cos(lon)
    y = (N + h) * cos_lat * np.sin(lon)
    z = (N * (1.0 - WGS84_E2) + h) * sin_lat
    out = np.stack(np.broadcast_arrays(x, y, z), axis=-1)
    return out


def ecef_to_geodetic(x, y, z, tol_m=1e-9, max_iter=30):
    """ECEF 笛卡尔坐标 → WGS-84 大地坐标（迭代法，往返误差 < 1e-6 m）。

    采用 Bowring 初值 + 纬度不动点迭代（Heikkinen/Bowring 混合），
    极点附近用解析式避免除零。

    Parameters
    ----------
    x, y, z : float or array_like
        ECEF 位置分量 [m]。
    tol_m : float
        纬度收敛阈值（以椭球高变化折算，单位 m），默认 1e-9 m。
    max_iter : int
        最大迭代次数。

    Returns
    -------
    (lat_deg, lon_deg, h_m) : tuple of ndarray
        大地纬度 [deg] ∈ [-90, 90]、经度 [deg] ∈ (-180, 180]、椭球高 [m]。
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    z = np.asarray(z, dtype=float)
    x, y, z = np.broadcast_arrays(x, y, z)

    lon = np.arctan2(y, x)                                   # 经度解析可得
    p = np.hypot(x, y)                                       # 到自转轴的距离
    e2, a, b = WGS84_E2, WGS84_A, WGS84_B

    # --- 初值：Bowring (1985) 单次近似 ---
    theta = np.arctan2(z * a, p * b)
    sin_t, cos_t = np.sin(theta), np.cos(theta)
    lat = np.arctan2(z + WGS84_EP2 * b * sin_t ** 3,
                     p - e2 * a * cos_t ** 3)

    # --- 不动点迭代：解 tanφ = (z + e²·N·sinφ)/p ---
    # 迭代映射的压缩因子约 e² ≈ 0.0067，6~8 次迭代即达 1e-9 m 量级。
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        for _ in range(max_iter):
            sin_lat = np.sin(lat)
            N = a / np.sqrt(1.0 - e2 * sin_lat * sin_lat)
            lat_new = np.arctan2(z + e2 * N * sin_lat, p)     # 极点 p=0 时也成立
            h_cur = np.where(np.abs(np.cos(lat)) > 0.5,
                             p / np.maximum(np.cos(lat), 1e-15) - N,
                             z / np.where(np.abs(sin_lat) < 1e-15, 1e-15, sin_lat)
                             - N * (1.0 - e2))
            dlat_m = np.abs(lat_new - lat) * (N + h_cur)      # 纬度差 → 弧长 [m]
            lat = lat_new
            if np.all(dlat_m < max(tol_m, 1e-12)):
                break

    sin_lat = np.sin(lat)
    cos_lat = np.cos(lat)
    N = a / np.sqrt(1.0 - e2 * sin_lat * sin_lat)
    # 高纬用 z 分量反算更稳，低纬用 p 分量反算更稳
    h_p = p / np.maximum(cos_lat, 1e-15) - N
    h_z = z / np.where(np.abs(sin_lat) < 1e-15, 1e-15, sin_lat) - N * (1.0 - e2)
    h = np.where(np.abs(cos_lat) > 0.5, h_p, h_z)

    lat_deg = np.rad2deg(lat)
    lon_deg = np.rad2deg(lon)
    # np.rad2deg(np.arctan2) 已在 (-180,180]；标量输入返回 0-d 数组，统一为 python/数组
    return lat_deg, lon_deg, h


def ecef_to_geodetic_fast(x, y, z):
    """ECEF → 大地坐标的快速版本（仅 Bowring 一次近似，误差 ~0.1 mm）。

    用于大批量网格点（如 DEM 采样）加速；单点查询请用 ``ecef_to_geodetic``。
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    z = np.asarray(z, dtype=float)
    lon = np.arctan2(y, x)
    p = np.hypot(x, y)
    theta = np.arctan2(z * WGS84_A, p * WGS84_B)
    lat = np.arctan2(z + WGS84_EP2 * WGS84_B * np.sin(theta) ** 3,
                     p - WGS84_E2 * WGS84_A * np.cos(theta) ** 3)
    sin_lat = np.sin(lat)
    N = WGS84_A / np.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
    h_p = p / np.maximum(np.cos(lat), 1e-15) - N
    h_z = z / np.where(np.abs(sin_lat) < 1e-15, 1e-15, sin_lat) - N * (1.0 - WGS84_E2)
    h = np.where(np.abs(np.cos(lat)) > 0.5, h_p, h_z)
    return np.rad2deg(lat), np.rad2deg(lon), h


# --------------------------------------------------------------------------- #
# 时间 / 恒星时
# --------------------------------------------------------------------------- #
def julian_date(dt):
    """UTC 时刻 → 儒略日 JD（格里高利历，UT 近似 TT，ΔT<70 s 对本任务可忽略）。

    Parameters
    ----------
    dt : datetime.datetime or datetime.date
        naive 视为 UTC；带 tzinfo 时自动换算为 UTC。

    Returns
    -------
    float
        儒略日 JD（J2000.0 = 2451545.0）。
    """
    if isinstance(dt, datetime) and dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    y = dt.year
    m = dt.month
    d = float(dt.day)
    if isinstance(dt, datetime):
        d += (dt.hour + (dt.minute + (dt.second + dt.microsecond * 1e-6) / 60.0) / 60.0) / 24.0
    if m <= 2:
        y -= 1
        m += 12
    A = y // 100
    B = 2 - A + A // 4
    return math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + d + B - 1524.5


def jd_to_datetime(jd):
    """儒略日 → UTC datetime（naive，UTC）。``julian_date`` 的逆运算。"""
    jd = float(jd)
    z = math.floor(jd + 0.5)
    f = (jd + 0.5) - z
    if z < 2299161:
        A = z
    else:
        alpha = math.floor((z - 1867216.25) / 36524.25)
        A = z + 1 + alpha - math.floor(alpha / 4)
    B = A + 1524
    C = math.floor((B - 122.1) / 365.25)
    D = math.floor(365.25 * C)
    E = math.floor((B - D) / 30.6001)
    day = B - D - math.floor(30.6001 * E) + f
    month = E - 1 if E < 14 else E - 13
    year = C - 4716 if month > 2 else C - 4715
    d_int = int(math.floor(day))
    frac = (day - d_int) * 24.0
    hh = int(math.floor(frac))
    mm = int(math.floor((frac - hh) * 60.0))
    ss = (frac - hh - mm / 60.0) * 3600.0
    base = datetime(year, month, d_int, hh, mm)
    return base + timedelta(seconds=min(ss, 59.999999))


def gmst_deg(dt):
    """UTC → 格林尼治平恒星时 GMST [deg]，[0, 360)。

    IAU-82 / Vallado 多项式（与 Meeus Ch.12 等价）：

    .. math::

        \\theta_G = 280.46061837 + 360.98564736629\\,(JD-2451545.0)
                    + 0.000387933\\,T^2 - T^3/38710000

    其中 T = (JD-2451545)/36525 [儒略世纪]。
    **这是平恒星时（mean sidereal time）**，未加章动改正（真恒星时差 ≤ 1.1 s ≈ 0.0046°），
    未考虑极移；对本任务 0.001° 量级精度足够。

    Parameters
    ----------
    dt : datetime.datetime
        UTC 时刻。

    Returns
    -------
    float
        GMST [deg] ∈ [0, 360)。
    """
    jd = julian_date(dt)
    T = (jd - J2000_JD) / 36525.0
    d = jd - J2000_JD
    theta = (280.46061837 + 360.98564736629 * d
             + 0.000387933 * T * T - T * T * T / 38710000.0)
    return float(np.mod(theta, 360.0))


def gmst_rad(dt):
    """UTC → GMST [rad]（见 :func:`gmst_deg`）。"""
    return math.radians(gmst_deg(dt))


def mean_obliquity_deg(dt):
    """黄赤交角 ε₀（平黄赤交角）[deg]，Meeus Ch.22（IAU-1980）。

    ε₀ = 23°26'21.448" - 46.8150"T - 0.00059"T² + 0.001813"T³，T 为儒略世纪。
    """
    T = (julian_date(dt) - J2000_JD) / 36525.0
    eps = (84381.448 - 46.8150 * T - 0.00059 * T * T + 0.001813 * T ** 3) / 3600.0
    return float(eps)


def equation_of_time_min(dt):
    """时差 EoT [min]（视太阳时 - 平太阳时），Meeus Ch.28。

    正值表示日晷快于平太阳（如 11 月初约 +16.4 min）。
    本函数忽略章动项（<0.02 s），精度 ~1 s。
    """
    sun = sun_apparent(dt)
    T = (julian_date(dt) - J2000_JD) / 36525.0
    L0 = 280.4664567 + 36000.76982779 * T + 0.0003032028 * T * T + T ** 3 / 49931.0
    # E = L0 - 20.495" - α（度），取 (-180,180] 分支后再换算为分钟（1° = 4 min）
    E = float(wrap180(L0 - 0.0057183 - sun["ra_deg"]))
    return float(E * 4.0)


# --------------------------------------------------------------------------- #
# ECI ↔ ECEF
# --------------------------------------------------------------------------- #
def _rot_z(theta_rad):
    """绕 z 轴旋转矩阵 R_z(θ)（右手，θ>0 为逆时针）。"""
    c, s = math.cos(theta_rad), math.sin(theta_rad)
    return np.array([[c, -s, 0.0],
                     [s, c, 0.0],
                     [0.0, 0.0, 1.0]])


def _apply_rot(R, v):
    """对 (3,) / (N,3) 数组应用旋转矩阵。"""
    v = np.asarray(v, dtype=float)
    if v.shape[-1] != 3:
        raise ValueError(f"期望最后一维为 3 (xyz)，得到 shape={v.shape}")
    return v @ R.T


def eci_to_ecef_mat(dt):
    """ECEF ← ECI 的旋转矩阵（3x3）：``r_ecef = R @ r_eci``，R = Rz(−GMST)。

    推导：GMST = θ 时，ECEF 的 x 轴（本初子午线）在 ECI 中的方向为
    ``[cos θ, sin θ, 0]``，故 ECEF 坐标是 ECI 坐标绕 z 轴旋转 −θ 的结果。
    忽略极移/章动（精度 ~0.001°）。
    """
    return _rot_z(-gmst_rad(dt))


def eci_to_ecef(r_eci, dt):
    """ECI（J2000 惯性系，仅按 GMST 旋转的"伪惯性系"）→ ECEF。

    Parameters
    ----------
    r_eci : (3,) or (N,3) array_like
        ECI 位置 [m]。
    dt : datetime.datetime
        UTC 时刻。

    Returns
    -------
    ndarray
        ECEF 位置 [m]，形状同 ``r_eci``。
    """
    return _apply_rot(eci_to_ecef_mat(dt), r_eci)


def ecef_to_eci(r_ecef, dt):
    """ECEF → ECI（:func:`eci_to_ecef` 的逆变换，R = Rz(+GMST)）。"""
    return _apply_rot(_rot_z(gmst_rad(dt)), r_ecef)


# --------------------------------------------------------------------------- #
# 太阳位置
# --------------------------------------------------------------------------- #
def sun_apparent(dt):
    """太阳视位置（Meeus Ch.25 低精度太阳坐标）。

    Parameters
    ----------
    dt : datetime.datetime
        UTC（用 UT 代替 TT，误差 < 0.001°）。

    Returns
    -------
    dict
        ``lambda_deg`` 视黄经 [deg]；``eps_deg`` 真黄赤交角 [deg]；
        ``ra_deg`` 视赤经 [deg] ∈ [0,360)；``dec_deg`` 视赤纬 [deg]；
        ``r_au`` 日地距离 [AU]；``L0_deg`` 几何平黄经 [deg]；``M_deg`` 平近点角 [deg]。

    Notes
    -----
    Meeus 第 25 章 "low accuracy" 方法：视黄经误差 ~0.01°，赤纬 ~0.01°（远优于任务 0.1°）。
    """
    jd = julian_date(dt)
    T = (jd - J2000_JD) / 36525.0

    # 几何平黄经
    L0 = 280.46646 + 36000.76983 * T + 0.0003032 * T * T
    # 平近点角
    M = 357.52911 + 35999.05029 * T - 0.0001537 * T * T
    # 地球轨道偏心率
    e = 0.016708634 - 0.000042037 * T - 0.0000001267 * T * T
    # 中心差
    Mr = math.radians(M % 360.0)
    C = ((1.914602 - 0.004817 * T - 0.000014 * T * T) * math.sin(Mr)
         + (0.019993 - 0.000101 * T) * math.sin(2.0 * Mr)
         + 0.000289 * math.sin(3.0 * Mr))
    # 真黄经 / 真近点角 / 日地距离
    true_long = L0 + C
    nu = M + C
    R = 1.000001018 * (1.0 - e * e) / (1.0 + e * math.cos(math.radians(nu)))
    # 视黄经（加章动 + 光行差近似修正；Ω 为月球升交点平经度）
    omega = 125.04 - 1934.136 * T
    lam = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    # 真黄赤交角
    eps0 = mean_obliquity_deg(dt)
    eps = eps0 + 0.00256 * math.cos(math.radians(omega))

    lam_r = math.radians(lam % 360.0)
    eps_r = math.radians(eps)
    sin_lam, cos_lam = math.sin(lam_r), math.cos(lam_r)
    # 视赤经 α: tan α = cos ε · sin λ / cos λ  →  atan2(cos ε sin λ, cos λ)
    ra = math.atan2(math.cos(eps_r) * sin_lam, cos_lam)
    ra = float(np.mod(ra, 2.0 * math.pi))
    dec = math.asin(math.sin(eps_r) * sin_lam)

    return {
        "lambda_deg": float(lam % 360.0),
        "eps_deg": float(eps),
        "ra_deg": float(np.rad2deg(ra)),
        "dec_deg": float(np.rad2deg(dec)),
        "r_au": float(R),
        "L0_deg": float(L0 % 360.0),
        "M_deg": float(M % 360.0),
    }


def sun_ra_dec_deg(dt):
    """太阳视赤经/赤纬 [deg] → ``(ra_deg, dec_deg)``，ra ∈ [0,360)。"""
    s = sun_apparent(dt)
    return s["ra_deg"], s["dec_deg"]


def sun_declination_deg(dt):
    """太阳视赤纬 [deg]（≈ 太阳直射点纬度）。"""
    return sun_apparent(dt)["dec_deg"]


def sun_eci_unit(dt):
    """太阳单位方向矢量（ECI，赤道坐标）→ (3,) ndarray。

    仅由赤经/赤纬构造，单位向量 [无量纲]；距离信息不含（太阳距离约 1 AU，
    对 3-DOF 上升段仿真无影响）。
    """
    ra, dec = sun_ra_dec_deg(dt)
    ra_r, dec_r = math.radians(ra), math.radians(dec)
    return np.array([math.cos(dec_r) * math.cos(ra_r),
                     math.cos(dec_r) * math.sin(ra_r),
                     math.sin(dec_r)])


def sun_ecef_unit(dt):
    """太阳单位方向矢量（ECEF）→ (3,) ndarray。``r_ecef = Rz(θ_G) r_eci``。"""
    return eci_to_ecef(sun_eci_unit(dt), dt)


def sun_subpoint(dt):
    """太阳直射点（subsolar point）经纬度 [deg] → ``(lat_deg, lon_deg)``。

    直射点纬度 = 太阳视赤纬 δ；经度 λ 满足 ECEF 中太阳方向 = 直射点位置矢量：
    λ = α - GMST（归一化到 (-180, 180]）。等价于"当地太阳时 12:00"的位置。

    Examples
    --------
    2026-10-25T02:00:00Z 约 (12.2°S, 146°E)。任务书给的 150°E 为忽略时差的
    平太阳近似值（2026-10-25 时差约 +15.5 min → 约 -3.9°）。
    """
    s = sun_apparent(dt)
    lon = float(wrap180(s["ra_deg"] - gmst_deg(dt)))
    return s["dec_deg"], lon


def solar_elevation_deg(lat_deg, lon_deg, dt):
    """太阳（几何）高度角 [deg] —— 支持 numpy 数组广播。

    .. math::

        \\sin h_\\odot = \\sin\\varphi\\sin\\delta
                        + \\cos\\varphi\\cos\\delta\\cos H,
        \\quad H = \\theta_G + \\lambda - \\alpha

    Parameters
    ----------
    lat_deg, lon_deg : float or array_like
        观测点纬度/经度 [deg]。可广播（如 lat 为 (M,1)、lon 为 (1,N)）。
    dt : datetime.datetime
        UTC 时刻。

    Returns
    -------
    ndarray or float
        **几何**高度角 [deg]，范围 [-90, 90]（大气折射未计入；地平附近约 +0.5°）。

    Notes
    -----
    精度优于 0.1°（Meeus 低精度太阳坐标 ~0.01°）。
    """
    ra, dec = sun_ra_dec_deg(dt)
    theta = gmst_deg(dt)
    lat = np.deg2rad(np.asarray(lat_deg, dtype=float))
    lon = np.deg2rad(np.asarray(lon_deg, dtype=float))
    H = np.deg2rad(theta) + lon - math.radians(ra)          # 时角
    sin_el = (np.sin(lat) * math.sin(math.radians(dec))
              + np.cos(lat) * math.cos(math.radians(dec)) * np.cos(H))
    return np.rad2deg(np.arcsin(np.clip(sin_el, -1.0, 1.0)))


def solar_azimuth_deg(lat_deg, lon_deg, dt):
    """太阳方位角 [deg]（正北 = 0，顺时针为正/向东），支持 numpy 广播。

    .. math::

        \\tan A = \\frac{-\\sin H\\cos\\delta}
                       {\\sin\\delta\\cos\\varphi - \\cos\\delta\\sin\\varphi\\cos H}

    Returns
    -------
    ndarray or float
        方位角 [deg] ∈ [0, 360)。
    """
    ra, dec = sun_ra_dec_deg(dt)
    theta = gmst_deg(dt)
    lat = np.deg2rad(np.asarray(lat_deg, dtype=float))
    lon = np.deg2rad(np.asarray(lon_deg, dtype=float))
    H = np.deg2rad(theta) + lon - math.radians(ra)
    dec_r = math.radians(dec)
    sin_az = -np.sin(H) * math.cos(dec_r)
    cos_az = math.sin(dec_r) * np.cos(lat) - math.cos(dec_r) * np.sin(lat) * np.cos(H)
    return np.mod(np.rad2deg(np.arctan2(sin_az, cos_az)), 360.0)


def solar_zenith_deg(lat_deg, lon_deg, dt):
    """太阳天顶角 [deg] = 90 - 几何高度角。"""
    return 90.0 - solar_elevation_deg(lat_deg, lon_deg, dt)


def terminator_lat_for_lon(dt, lon_deg):
    """给定时刻/经度，求晨昏线（太阳几何高度角 = 0°）上的纬度 [deg]。

    用于地图绘制晨昏线。由 sin φ sin δ + cos φ cos δ cos H = 0 得

    .. math:: \\tan\\varphi = -\\cos H / \\tan\\delta

    Parameters
    ----------
    dt : datetime.datetime
        时刻（UTC）。
    lon_deg : array_like
        经度 [deg] 数组。

    Returns
    -------
    ndarray
        对应纬度 [deg]。当 |δ| 很小时退化为 |H|=90°（沿经线），返回 ±90°。

    Notes
    -----
    * 若某经线整条都在白昼/黑夜（极昼极夜），本公式给出的解在数学上仍存在，
      但物理上该经线不穿过晨昏线；调用方应结合 :func:`solar_elevation_deg` 判断。
    * 忽略大气折射与太阳视半径（真实晨昏线约在几何高度角 -0.83° 处）。
    """
    dec, _lon_sub = sun_subpoint(dt)          # sun_subpoint 返回 (直射点纬度=δ, 经度)
    H = np.deg2rad(gmst_deg(dt) + np.asarray(lon_deg, dtype=float)
                   - (sun_apparent(dt)["ra_deg"]))
    if abs(dec) < 1e-9:
        # δ≈0（春分/秋分）：晨昏线沿经线，|H| = 90°
        return np.full_like(np.asarray(lon_deg, dtype=float),
                            90.0 * np.sign(np.sin(H)))
    # tanφ = -cos H / tan δ，取反三角函数主值分支 → φ ∈ (-90, 90)，正好是纬度范围
    return np.rad2deg(np.arctan(-np.cos(H) / math.tan(math.radians(dec))))


# --------------------------------------------------------------------------- #
# 观测几何：ENU / 方位角 / 高度角
# --------------------------------------------------------------------------- #
def enu_basis(lat_deg, lon_deg):
    """观测点局部 ENU（东-北-天）基矢量在 ECEF 中的表示。

    Parameters
    ----------
    lat_deg, lon_deg : float
        观测点大地经纬度 [deg]。

    Returns
    -------
    (3, 3) ndarray
        行向量为 E, N, U 三个单位矢量（ECEF 分量）。即
        ``enu = R @ d_ecef``，``R = enu_basis(lat, lon)``。
    """
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    sin_lat, cos_lat = math.sin(lat), math.cos(lat)
    sin_lon, cos_lon = math.sin(lon), math.cos(lon)
    E = np.array([-sin_lon, cos_lon, 0.0])
    N = np.array([-sin_lat * cos_lon, -sin_lat * sin_lon, cos_lat])
    U = np.array([cos_lat * cos_lon, cos_lat * sin_lon, sin_lat])
    return np.vstack([E, N, U])


def ecef_to_enu(d_ecef, lat_deg, lon_deg):
    """ECEF 位移矢量/相对矢量 → ENU。

    Parameters
    ----------
    d_ecef : (3,) or (N,3) or((N,3),(3,)) ndarray
        目标相对观测点的 ECEF 矢量 [m]（即 r_target - r_obs）。
    lat_deg, lon_deg : float
        观测点大地经纬度 [deg]。

    Returns
    -------
    ndarray
        ENU 分量 [m]，最后一维 = 3，顺序 (E, N, U)。
    """
    R = enu_basis(lat_deg, lon_deg)
    d = np.asarray(d_ecef, dtype=float)
    return d @ R.T


def enu_to_ecef(enu, lat_deg, lon_deg):
    """ENU 矢量 → ECEF 矢量（:func:`ecef_to_enu` 的逆运算）。"""
    R = enu_basis(lat_deg, lon_deg)
    v = np.asarray(enu, dtype=float)
    return v @ R


def az_el_from_observer(obs_lat_deg, obs_lon_deg, obs_h_m, tgt_ecef_xyz):
    """观测点 → 目标的方位角/高度角（真北起顺时针），含目标高度与地球曲率。

    计算方式为**严格的三维几何**：先求目标相对观测点的 ECEF 矢量，再转到观测点
    局部 ENU 系：

    .. math::

        \\text{az} = \\operatorname{atan2}(E, N), \\qquad
        \\text{el} = \\operatorname{atan2}\\left(U, \\sqrt{E^2+N^2}\\right)

    因此"地球曲率 + 目标高度"自动包含，无需额外修正项（在距离 d 处，
    地平线下降量 d²/2R 会自然体现在 U 分量为负）。

    Parameters
    ----------
    obs_lat_deg, obs_lon_deg : float
        观测点大地纬度/经度 [deg]。
    obs_h_m : float
        观测点椭球高 [m]（DEM 海拔 + 天线高；本任务取 DEM 正高近似）。
    tgt_ecef_xyz : (3,) or (N,3) array_like
        目标 ECEF 位置 [m]。

    Returns
    -------
    (az_deg, el_deg) : tuple of ndarray/float
        方位角 [deg] ∈ [0,360)（正北 0，正东 90，正南 180，正西 270）；
        高度角 [deg] ∈ [-90, 90]（地平线以上为正）。

    Notes
    -----
    目标恰好位于观测点时返回 (0, 90)（避免除零）。
    """
    obs = np.asarray(geodetic_to_ecef(obs_lat_deg, obs_lon_deg, obs_h_m), dtype=float)
    tgt = np.asarray(tgt_ecef_xyz, dtype=float)
    d = tgt - obs
    enu = ecef_to_enu(d, obs_lat_deg, obs_lon_deg)
    e, n, u = enu[..., 0], enu[..., 1], enu[..., 2]
    hor = np.hypot(e, n)
    az = np.mod(np.rad2deg(np.arctan2(e, n)), 360.0)
    el = np.rad2deg(np.arctan2(u, hor))
    # 目标与观测点重合（或极近）时 arctan2(0,0)=0 → 高度角记为 90°
    coincident = (hor < 1e-9) & (np.abs(u) < 1e-9)
    el = np.where(coincident, 90.0, el)
    return az, el


def az_el_from_geodetic(obs_lat_deg, obs_lon_deg, obs_h_m, tgt_lat_deg, tgt_lon_deg, tgt_h_m):
    """观测点 → 目标（大地坐标给出）的方位角/高度角 [deg]，见 :func:`az_el_from_observer`。"""
    tgt = geodetic_to_ecef(tgt_lat_deg, tgt_lon_deg, tgt_h_m)
    return az_el_from_observer(obs_lat_deg, obs_lon_deg, obs_h_m, tgt)


def slant_range_km(obs_lat_deg, obs_lon_deg, obs_h_m, tgt_ecef_xyz):
    """观测点 → 目标的直线（斜）距 [km]。"""
    obs = np.asarray(geodetic_to_ecef(obs_lat_deg, obs_lon_deg, obs_h_m), dtype=float)
    tgt = np.asarray(tgt_ecef_xyz, dtype=float)
    return np.linalg.norm(tgt - obs, axis=-1) / 1000.0


def geodetic_to_enu_offset(lat_deg, lon_deg, h_m, lat0_deg, lon0_deg, h0_m):
    """目标相对参考点的 ENU 偏移 [m]（大地坐标输入）。

    Returns
    -------
    (E, N, U) : tuple of ndarray  [m]
    """
    r = geodetic_to_ecef(lat_deg, lon_deg, h_m)
    r0 = geodetic_to_ecef(lat0_deg, lon0_deg, h0_m)
    enu = ecef_to_enu(np.asarray(r) - np.asarray(r0), lat0_deg, lon0_deg)
    return enu[..., 0], enu[..., 1], enu[..., 2]


# --------------------------------------------------------------------------- #
# 距离 / 方位角
# --------------------------------------------------------------------------- #
def great_circle(lat1, lon1, lat2, lon2):
    """球面大圆距离与初始方位角（球面近似，R = 6371.0088 km）。

    Parameters
    ----------
    lat1, lon1, lat2, lon2 : float or array_like
        起点/终点经纬度 [deg]，可广播。

    Returns
    -------
    (dist_km, az_deg) : tuple
        大圆距离 [km]（haversine）与起点处**初始方位角** [deg]（正北 0，顺时针）。

    Notes
    -----
    与 WGS-84 椭球测地线（:func:`geodesic_distance`）相比，在本任务尺度
    （100–3000 km）球面距离偏小约 0.1%–0.4%（低纬 0.1%~0.3%），方位角差 < 0.2°。
    需要精确值请用 Vincenty。
    """
    phi1 = np.deg2rad(np.asarray(lat1, dtype=float))
    phi2 = np.deg2rad(np.asarray(lat2, dtype=float))
    dphi = phi2 - phi1
    dlam = np.deg2rad(wrap180(np.asarray(lon2, dtype=float) - np.asarray(lon1, dtype=float)))
    a = np.sin(dphi / 2.0) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlam / 2.0) ** 2
    a = np.clip(a, 0.0, 1.0)
    c = 2.0 * np.arctan2(np.sqrt(a), np.sqrt(1.0 - a))
    dist_km = EARTH_MEAN_R_KM * c
    # 初始方位角
    y = np.sin(dlam) * np.cos(phi2)
    x = np.cos(phi1) * np.sin(phi2) - np.sin(phi1) * np.cos(phi2) * np.cos(dlam)
    az = np.mod(np.rad2deg(np.arctan2(y, x)), 360.0)
    return dist_km, az


def great_circle_distance_km(lat1, lon1, lat2, lon2):
    """球面大圆距离 [km]（只要距离时更简洁）。"""
    return great_circle(lat1, lon1, lat2, lon2)[0]


def initial_bearing_deg(lat1, lon1, lat2, lon2):
    """起点 → 终点的初始方位角 [deg]（正北 0，顺时针，球面公式）。"""
    return great_circle(lat1, lon1, lat2, lon2)[1]


def geodesic_inverse(lat1, lon1, lat2, lon2, max_iter=200, tol=1e-12):
    """WGS-84 椭球测地线反解（Vincenty 1975）：距离与两端方位角。

    Parameters
    ----------
    lat1, lon1, lat2, lon2 : float
        经纬度 [deg]（标量）。批量计算请循环或改用 :func:`geodesic_distance` 的数组包装。
    max_iter : int
        最大迭代次数；对近对跖点可能不收敛（此时抛 RuntimeError，请用大圆近似）。
    tol : float
        经度差收敛阈值 [rad]。

    Returns
    -------
    (dist_km, az12_deg, az21_deg) : tuple of float
        测地线距离 [km]，起点前方位角、终点后方位角 [deg]（正北 0，顺时针）。

    Notes
    -----
    Vincenty 反解在 |Δ| 极小时数值稳定；近对跖点（如 179.5°×179.5°）迭代不收敛，
    本实现会抛异常，调用方应回退大圆公式。
    """
    a, f, b = WGS84_A, WGS84_F, WGS84_B
    if lat1 == lat2 and wrap180(lon2 - lon1) == 0.0:
        return 0.0, 0.0, 0.0
    L = math.radians(float(wrap180(lon2 - lon1)))
    U1 = math.atan((1.0 - f) * math.tan(math.radians(lat1)))
    U2 = math.atan((1.0 - f) * math.tan(math.radians(lat2)))
    sinU1, cosU1 = math.sin(U1), math.cos(U1)
    sinU2, cosU2 = math.sin(U2), math.cos(U2)

    lam = L
    for _ in range(max_iter):
        sin_lam, cos_lam = math.sin(lam), math.cos(lam)
        sin_sigma = math.hypot(cosU2 * sin_lam,
                               cosU1 * sinU2 - sinU1 * cosU2 * cos_lam)
        if sin_sigma == 0.0:
            return 0.0, 0.0, 0.0
        cos_sigma = sinU1 * sinU2 + cosU1 * cosU2 * cos_lam
        sigma = math.atan2(sin_sigma, cos_sigma)
        sin_alpha = cosU1 * cosU2 * sin_lam / sin_sigma
        cos2_alpha = 1.0 - sin_alpha ** 2
        cos2_sigma_m = (cos_sigma - 2.0 * sinU1 * sinU2 / cos2_alpha
                        if cos2_alpha != 0.0 else 0.0)
        C = f / 16.0 * cos2_alpha * (4.0 + f * (4.0 - 3.0 * cos2_alpha))
        lam_prev = lam
        lam = L + (1.0 - C) * f * sin_alpha * (
            sigma + C * sin_sigma * (cos2_sigma_m + C * cos_sigma *
                                     (-1.0 + 2.0 * cos2_sigma_m ** 2)))
        if abs(lam - lam_prev) < tol:
            break
    else:
        raise RuntimeError("Vincenty 反解未收敛（近对跖点），请回退大圆公式")

    u2 = cos2_alpha * (a * a - b * b) / (b * b)
    A = 1.0 + u2 / 16384.0 * (4096.0 + u2 * (-768.0 + u2 * (320.0 - 175.0 * u2)))
    B = u2 / 1024.0 * (256.0 + u2 * (-128.0 + u2 * (74.0 - 47.0 * u2)))
    d_sigma = B * sin_sigma * (cos2_sigma_m + B / 4.0 * (
        cos_sigma * (-1.0 + 2.0 * cos2_sigma_m ** 2)
        - B / 6.0 * cos2_sigma_m * (-3.0 + 4.0 * sin_sigma ** 2)
        * (-3.0 + 4.0 * cos2_sigma_m ** 2)))
    s = b * A * (sigma - d_sigma)

    sin_lam, cos_lam = math.sin(lam), math.cos(lam)
    az12 = math.atan2(cosU2 * sin_lam,
                      cosU1 * sinU2 - sinU1 * cosU2 * cos_lam)
    az21 = math.atan2(cosU1 * sin_lam,
                      -sinU1 * cosU2 + cosU1 * sinU2 * cos_lam)
    return (s / 1000.0,
            float(np.mod(math.degrees(az12), 360.0)),
            float(np.mod(math.degrees(az21), 360.0)))


def geodesic_distance(lat1, lon1, lat2, lon2):
    """WGS-84 椭球测地线距离 [km]（Vincenty；近对跖点回退大圆）。

    与 :func:`great_circle` 的区别
    ------------------------------
    * 大圆：球面、平均半径 6371.0088 km，低纬与椭球差约 0.1%~0.3%，计算快、可向量化。
    * Vincenty：WGS-84 椭球，mm 级；适合落点/射程的正式数值（``sim/落点与射程.csv``）。
    两者在本任务 1000 km 量级差异约 1–3 km，报告中应注明用哪一个。
    """
    try:
        return geodesic_inverse(lat1, lon1, lat2, lon2)[0]
    except RuntimeError:
        return float(great_circle_distance_km(lat1, lon1, lat2, lon2))


def geodesic_direct(lat1, lon1, az_deg, dist_km, max_iter=100, tol=1e-12):
    """WGS-84 椭球测地线正解（Vincenty 1975）：给定起点、方位角、距离求终点。

    Parameters
    ----------
    lat1, lon1 : float
        起点经纬度 [deg]。
    az_deg : float
        初始方位角 [deg]（正北 0，顺时针）。
    dist_km : float
        测地线距离 [km]。

    Returns
    -------
    (lat2_deg, lon2_deg, az2_deg) : tuple of float
        终点经纬度 [deg]（lon 归一化到 (-180,180]）与终点处前方位角 [deg]。

    Notes
    -----
    地形遮挡计算沿方位角按步长取点时使用本函数（90 m 步长，200 km 累计误差 < 1 m）。
    """
    a, f, b = WGS84_A, WGS84_F, WGS84_B
    s = dist_km * 1000.0
    alpha1 = math.radians(az_deg)
    sin_a1, cos_a1 = math.sin(alpha1), math.cos(alpha1)
    tanU1 = (1.0 - f) * math.tan(math.radians(lat1))
    cosU1 = 1.0 / math.sqrt(1.0 + tanU1 ** 2)
    sinU1 = tanU1 * cosU1
    sigma1 = math.atan2(tanU1, cos_a1)
    sin_alpha = cosU1 * sin_a1
    cos2_alpha = 1.0 - sin_alpha ** 2
    u2 = cos2_alpha * (a * a - b * b) / (b * b)
    A = 1.0 + u2 / 16384.0 * (4096.0 + u2 * (-768.0 + u2 * (320.0 - 175.0 * u2)))
    B = u2 / 1024.0 * (256.0 + u2 * (-128.0 + u2 * (74.0 - 47.0 * u2)))

    sigma = s / (b * A)
    for _ in range(max_iter):
        cos2_sigma_m = math.cos(2.0 * sigma1 + sigma)
        sin_sigma, cos_sigma = math.sin(sigma), math.cos(sigma)
        d_sigma = B * sin_sigma * (cos2_sigma_m + B / 4.0 * (
            cos_sigma * (-1.0 + 2.0 * cos2_sigma_m ** 2)
            - B / 6.0 * cos2_sigma_m * (-3.0 + 4.0 * sin_sigma ** 2)
            * (-3.0 + 4.0 * cos2_sigma_m ** 2)))
        sigma_new = s / (b * A) + d_sigma
        if abs(sigma_new - sigma) < tol:
            sigma = sigma_new
            break
        sigma = sigma_new

    cos2_sigma_m = math.cos(2.0 * sigma1 + sigma)
    sin_sigma, cos_sigma = math.sin(sigma), math.cos(sigma)
    tmp = sinU1 * sin_sigma - cosU1 * cos_sigma * cos_a1
    lat2 = math.atan2(sinU1 * cos_sigma + cosU1 * sin_sigma * cos_a1,
                      (1.0 - f) * math.sqrt(sin_alpha ** 2 + tmp ** 2))
    lam = math.atan2(sin_sigma * sin_a1,
                     cosU1 * cos_sigma - sinU1 * sin_sigma * cos_a1)
    C = f / 16.0 * cos2_alpha * (4.0 + f * (4.0 - 3.0 * cos2_alpha))
    L = lam - (1.0 - C) * f * sin_alpha * (
        sigma + C * sin_sigma * (cos2_sigma_m + C * cos_sigma *
                                 (-1.0 + 2.0 * cos2_sigma_m ** 2)))
    lon2 = float(wrap180(lon1 + math.degrees(L)))
    az2 = math.atan2(sin_alpha, -tmp)
    return float(math.degrees(lat2)), lon2, float(np.mod(math.degrees(az2), 360.0))


def downrange_crossrange(lat0, lon0, az0_deg, lat, lon):
    """沿初始方位角的**沿航向距离 (downrange)** 与**横向距离 (crossrange)** [km]。

    球面近似（R = 6371.0088 km）：

    * 中心角 δ 与起点方位 θ 由 :func:`great_circle` 给出；
    * 横向距离  xt = R·asin( sin δ · sin(θ − az0) )
    * 沿航向距离 at = R·acos( cos δ / cos(xt/R) )

    Parameters
    ----------
    lat0, lon0 : float
        起点（发射台）经纬度 [deg]。
    az0_deg : float
        参考方位角（发射方位角）[deg]，正北 0 顺时针。
    lat, lon : float or array_like
        目标点经纬度 [deg]。

    Returns
    -------
    (downrange_km, crossrange_km) : tuple of ndarray/float
        crossrange > 0 表示目标在参考方位角**右侧**（顺时针方向），< 0 在左侧。
        downrange 取 [0, πR]（对超过 90% 地球周长的目标不适用，本任务无此情况）。

    Notes
    -----
    球面近似与椭球差异 < 0.5%（本任务 ~1000 km 时约 1–3 km）；需要精确射程请用
    :func:`geodesic_distance` 配合 :func:`geodesic_direct` 求交叉点。
    """
    dist_km, az_pt = great_circle(lat0, lon0, lat, lon)
    delta = np.asarray(dist_km, dtype=float) / EARTH_MEAN_R_KM      # 中心角 [rad]
    dtheta = np.deg2rad(np.asarray(az_pt, dtype=float) - float(az0_deg))
    sin_xt = np.sin(delta) * np.sin(dtheta)
    xt = np.arcsin(np.clip(sin_xt, -1.0, 1.0))
    cos_at = np.cos(delta) / np.maximum(np.cos(xt), 1e-15)
    at = np.arccos(np.clip(cos_at, -1.0, 1.0))
    return at * EARTH_MEAN_R_KM, xt * EARTH_MEAN_R_KM


# --------------------------------------------------------------------------- #
# 便捷常量导出（map/sky 可能直接用）
# --------------------------------------------------------------------------- #
CONSTANTS = {
    "WGS84_A": WGS84_A,
    "WGS84_F": WGS84_F,
    "WGS84_B": WGS84_B,
    "EARTH_MEAN_R_KM": EARTH_MEAN_R_KM,
}


if __name__ == "__main__":  # 简单自检：python -m common.geo 或直接运行
    _dt = datetime(2026, 10, 25, 2, 0, 0)
    print("JD           =", julian_date(_dt))
    print("GMST [deg]   =", gmst_deg(_dt))
    print("sun          =", sun_apparent(_dt))
    print("sun subpoint =", sun_subpoint(_dt))
    print("sanya el     =", solar_elevation_deg(18.2528, 109.5119, _dt))
    print("EoT [min]    =", equation_of_time_min(_dt))
