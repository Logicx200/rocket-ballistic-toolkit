# -*- coding: utf-8 -*-
"""
通用 3-DOF 上升段弹道动力学模型（多级串联 + 可选并联助推器）—— **v2.0 数值内核**

> 仅供公开信息分析、教学与可视化使用，非实际发射弹道。

v2.0 相对 v1.0 的改动（物理方程逐项不变，只改数值实现与事件处理）
------------------------------------------------------------------
1. **更快的右端函数**：USSA76 大气改为导入期预计算的分层常数 + ``bisect`` 定位；
   WGS-84 大地坐标换算改为 Bowring 闭式 + 1 次 Newton 精化（实测与迭代解差 < 5e-5 m）；
   重力/J2、俯仰程序、平面导引均内联为闭包局部变量，去掉每步重复的函数查找与对象分配。
2. **事件对齐步进（accurate 模式）**：把「整流罩分离 / 推力段切换 / 级分离 / SECO /
   助推器分离 / 采样时刻 / 推进剂耗尽」作为步长断点，逐步精确落在事件时刻上，
   事件时刻不再被量化到 dt；同时推进剂与质量按 RK4 同步积分（一阶 → 四阶）。
   结果与 dt 无关（dt=0.5…0.02 的轨迹差 < 1 m），而 v1 在 dt=0.02 时的事件量化
   误差可达 ~2.4 km（见 docs/精度验证_v2.md）。dt 因此可以放大到 0.2~0.5 s，
   整体仿真时间下降 20~50×。
3. **legacy 模式**：逐点保留 v1.0 的循环语义（步末质量、事件只在步边界判定、
   固定 dt），因此结果与 v1.0 **逐位等价**（实测轨迹差 < 1e-3 m、事件时刻完全相同），
   用于复现已交付成果。
4. **残骸弹道**：去除每步重复的 GMST/儒略日计算（v1 每步调用 ``gmst_rad``）；
   accurate 模式下用「高度过零二分」定位落点（v1 落点被 0.25 s 步长量化，
   典型误差 50~75 m），并把 ``debris_dt`` 放大到 1 s 仍保持 < 1 m 落点差。
5. **兼容性**：``simulate(veh, p, collect_debris)`` 签名不变，输出字典键不变
   （仅新增 ``numerics`` / ``stats`` 等附加键）；下游 export/mc/invert 无需改动即可运行。

单位：SI（m, kg, s, N, m/s）；角度度；时间 UTC。
"""
from __future__ import annotations

import math
import os
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

MU = 3.986004418e14
RE = 6378137.0
FLAT = 1.0 / 298.257223563
E2 = FLAT * (2.0 - FLAT)
OMEGA = 7.292115e-5
G0 = 9.80665
J2 = 1.08262668e-3
P0 = 101325.0
R_AIR = 287.05287

# WGS-84 辅助量（v2 新增，纯常量）
_B_SEMI = RE * (1.0 - FLAT)
_EP2 = (RE * RE - _B_SEMI * _B_SEMI) / (_B_SEMI * _B_SEMI)
_TWO_PI = 2.0 * math.pi


# ---------------------------------------------------------------- time / frames
def julian_date(dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    y, m = dt.year, dt.month
    d = dt.day + (dt.hour + (dt.minute + (dt.second + dt.microsecond / 1e6) / 60.0) / 60.0) / 24.0
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return float(math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + d + b - 1524.5)


def gmst_rad(dt: datetime) -> float:
    d = julian_date(dt) - 2451545.0
    T = d / 36525.0
    return math.radians((280.46061837 + 360.98564736629 * d
                         + 0.000387933 * T * T - T * T * T / 38710000.0) % 360.0)


def geodetic_to_ecef(lat_deg: float, lon_deg: float, h_m: float):
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    n = RE / math.sqrt(1.0 - E2 * math.sin(lat) ** 2)
    return ((n + h_m) * math.cos(lat) * math.cos(lon),
            (n + h_m) * math.cos(lat) * math.sin(lon),
            (n * (1.0 - E2) + h_m) * math.sin(lat))


def ecef_to_geodetic(x: float, y: float, z: float):
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    if p < 1e-9:
        return (90.0 if z >= 0 else -90.0), 0.0, abs(z) - RE * (1.0 - FLAT)
    lat = math.atan2(z, p * (1.0 - E2))
    for _ in range(4):
        n = RE / math.sqrt(1.0 - E2 * math.sin(lat) ** 2)
        h = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1.0 - E2 * n / (n + h)))
    n = RE / math.sqrt(1.0 - E2 * math.sin(lat) ** 2)
    h = p / math.cos(lat) - n
    return math.degrees(lat), math.degrees(lon), h


def eci_to_ecef(r, dt):
    th = gmst_rad(dt)
    c, s = math.cos(th), math.sin(th)
    x, y, z = r
    return np.array([c * x + s * y, -s * x + c * y, z])


def ecef_to_eci(r, dt):
    th = gmst_rad(dt)
    c, s = math.cos(th), math.sin(th)
    x, y, z = r
    return np.array([c * x - s * y, s * x + c * y, z])


# ---------------------------------------------------------------- atmosphere
# v1 的分层常数 + 每层底压比（导入期一次算好，避免每步递归/缓存查找）
_USSA_H = (0.0, 11.0, 20.0, 32.0, 47.0, 51.0, 71.0, 84.852)
_USSA_T = (288.15, 216.65, 216.65, 228.65, 270.65, 270.65, 214.65, 186.946)
_USSA_L = (-6.5, 0.0, 1.0, 2.8, 0.0, -2.8, -2.0, 0.0)

_H_HI = (86.0, 90.0, 100.0, 110.0, 120.0, 150.0, 200.0, 300.0, 500.0)
_LNRHO = tuple(math.log(v) for v in
               (7.0e-6, 3.4e-6, 5.6e-7, 1.0e-7, 2.2e-8, 2.0e-9, 2.5e-10, 2.0e-11, 5.0e-13))
_LNP = tuple(math.log(v) for v in
             (0.37, 0.15, 3.2e-2, 7.1e-3, 2.0e-3, 4.0e-4, 2.5e-5, 1.0e-6, 3.0e-9))
_T_HI = (186.9, 186.9, 195.0, 240.0, 360.0, 640.0, 860.0, 1000.0, 1000.0)


def _layer_ratio(hb: float, Tb: float, Lb_km: float, H_km: float) -> float:
    if abs(Lb_km) < 1e-12:
        return math.exp(-G0 * H_km * 1000.0 / (R_AIR * Tb))
    T = Tb + Lb_km * H_km
    return (T / Tb) ** (-G0 / ((Lb_km / 1000.0) * R_AIR))


# 每层底压比（与 v1 _p_ratio() 的递归连乘逐位一致：同样按层序累乘）
_P_BASE = [1.0]
for _i in range(1, len(_USSA_H)):
    _P_BASE.append(_P_BASE[-1] * _layer_ratio(_USSA_H[_i - 1], _USSA_T[_i - 1],
                                              _USSA_L[_i - 1], _USSA_H[_i] - _USSA_H[_i - 1]))
_P_BASE = tuple(_P_BASE)


def _p_ratio(hb_km: float, _cache={}) -> float:
    """兼容 v1 的旧接口（只用于外部/自检；内部不再调用）。"""
    if hb_km <= 0.0:
        return 1.0
    key = round(hb_km, 3)
    if key in _cache:
        return _cache[key]
    for i, layer in enumerate(_USSA_H):
        if abs(layer - hb_km) < 1e-6:
            hb, Tb, Lb_km = _USSA_H[i - 1], _USSA_T[i - 1], _USSA_L[i - 1]
            H = hb_km - hb
            ratio = _layer_ratio(hb, Tb, Lb_km, H)
            _cache[key] = _p_ratio(hb) * ratio
            return _cache[key]
    raise ValueError(hb_km)


def atmosphere(h_m: float):
    """返回 (rho, p, T)，与 v1.0 逐位一致。"""
    if h_m > 86000.0:
        hk = min(h_m / 1000.0, 500.0)
        for i in range(len(_H_HI) - 1):
            if hk <= _H_HI[i + 1]:
                x = (hk - _H_HI[i]) / (_H_HI[i + 1] - _H_HI[i])
                return (math.exp(_LNRHO[i] + x * (_LNRHO[i + 1] - _LNRHO[i])),
                        math.exp(_LNP[i] + x * (_LNP[i + 1] - _LNP[i])),
                        _T_HI[i] + x * (_T_HI[i + 1] - _T_HI[i]))
        return math.exp(_LNRHO[-1]), math.exp(_LNP[-1]), _T_HI[-1]
    hk = h_m / 1000.0
    if hk < 0.0:
        hk = 0.0
    i = bisect_right(_USSA_H, hk) - 1
    if i < 0:
        i = 0
    hb = _USSA_H[i]
    Tb = _USSA_T[i]
    Lb = _USSA_L[i]
    dh = hk - hb
    base = _P_BASE[i]
    if abs(Lb) < 1e-9:
        T = Tb
        p = P0 * base * math.exp(-G0 * dh * 1000.0 / (R_AIR * Tb))
    else:
        T = Tb + Lb * dh
        p = P0 * base * (T / Tb) ** (-G0 / ((Lb / 1000.0) * R_AIR))
    return p / (R_AIR * T), p, T


def density(h_m: float) -> float:
    """大气密度 [kg/m^3]；与 v1.0 逐位一致。"""
    if h_m > 86000.0:
        return atmosphere(h_m)[0]
    hk = h_m / 1000.0
    if hk < 0.0:
        hk = 0.0
    i = bisect_right(_USSA_H, hk) - 1
    if i < 0:
        i = 0
    Tb = _USSA_T[i]
    Lb = _USSA_L[i]
    dh = hk - _USSA_H[i]
    T = Tb + Lb * dh if abs(Lb) > 1e-9 else Tb
    base = _P_BASE[i]
    if abs(Lb) < 1e-9:
        p = P0 * base * math.exp(-G0 * dh * 1000.0 / (R_AIR * Tb))
    else:
        p = P0 * base * (T / Tb) ** (-G0 / ((Lb / 1000.0) * R_AIR))
    return p / (R_AIR * T)


# ---------------------------------------------------------------- dynamics helpers
def _lat_lon_alt(x, y, z):
    """v2 快速大地坐标：Bowring 闭式 + 1 次 Newton（与 v1 迭代解差 < 5e-5 m）。

    返回 (lat_rad, lon_rad, alt_m)。
    """
    p = math.hypot(x, y)
    lon = math.atan2(y, x)
    if p < 1e-6:
        return (0.5 * math.pi if z >= 0.0 else -0.5 * math.pi), lon, abs(z) - _B_SEMI
    th = math.atan2(z * RE, p * _B_SEMI)
    st = math.sin(th)
    ct = math.cos(th)
    lat = math.atan2(z + _EP2 * _B_SEMI * st * st * st, p - E2 * RE * ct * ct * ct)
    sl = math.sin(lat)
    n = RE / math.sqrt(1.0 - E2 * sl * sl)
    h = p / math.cos(lat) - n
    # 1 次 Newton 精化（把 1 cm 级残差压到 5e-5 m）
    lat = math.atan2(z, p * (1.0 - E2 * n / (n + h)))
    sl = math.sin(lat)
    n = RE / math.sqrt(1.0 - E2 * sl * sl)
    return lat, lon, p / math.cos(lat) - n


def _local_frame(x, y, z, theta):
    """v1 兼容接口：返回 (lat, lam, h)，lat/lam 为**弧度**。"""
    lat, lon, h = _lat_lon_alt(x, y, z)
    return lat, lon - theta, h


def gravity_xyz(x, y, z):
    r2 = x * x + y * y + z * z
    rn = math.sqrt(r2)
    r3 = r2 * rn
    k = -MU / r3
    zr2 = (z * z) / r2
    fac = 1.5 * J2 * (RE / rn) ** 2
    return (k * x * (1.0 - fac * (5.0 * zr2 - 1.0)),
            k * y * (1.0 - fac * (5.0 * zr2 - 1.0)),
            k * z * (1.0 - fac * (5.0 * zr2 - 3.0)))


def _gravity_xyz_fast(x, y, z):
    """与 gravity_xyz 等价，去掉函数调用层级（保留给内核使用）。"""
    r2 = x * x + y * y + z * z
    rn = math.sqrt(r2)
    r3 = r2 * rn
    k = -MU / r3
    zr2 = (z * z) / r2
    fac = 1.5 * J2 * (RE / rn) ** 2
    f1 = 1.0 - fac * (5.0 * zr2 - 1.0)
    f2 = 1.0 - fac * (5.0 * zr2 - 3.0)
    return k * x * f1, k * y * f1, k * z * f2


# ---------------------------------------------------------------- vehicle model
@dataclass
class Thruster:
    """同型发动机 × n（推力/Isp 为单台值；sl 缺省时按真空值处理）。"""
    thrust_vac_n: float
    isp_vac_s: float
    n: int = 1
    thrust_sl_n: float | None = None
    isp_sl_s: float | None = None

    def flow(self, p_amb: float, flow_scale: float = 1.0):
        tv = self.thrust_vac_n * self.n * flow_scale
        ts = (self.thrust_sl_n if self.thrust_sl_n is not None else self.thrust_vac_n) * self.n * flow_scale
        iv = self.isp_vac_s
        isl = self.isp_sl_s if self.isp_sl_s is not None else self.isp_vac_s
        pr = 1.0 if p_amb >= P0 else (0.0 if p_amb <= 0.0 else p_amb / P0)
        thrust = ts + (tv - ts) * (1.0 - pr)
        isp = iv - (iv - isl) * pr
        return thrust, (thrust / (isp * G0) if isp > 0 else 0.0)


@dataclass
class Phase:
    """级内推力段：多台发动机组合；结束条件 = end_met（绝对 MET）或推进剂耗尽。"""
    thrusters: list[Thruster]
    name: str = ""
    end_met: float | None = None        # 绝对 MET 结束（None = 该级推进剂耗尽）
    emit_event: bool = False            # 是否产生"XX关机"事件（如二级主机关机）


@dataclass
class Stage:
    name: str
    dry_kg: float
    prop_kg: float
    phases: list[Phase] = field(default_factory=list)
    pitch_end_deg: float | None = None      # 该级燃烧结束时的俯仰角（度，相对当地水平）
    pitch_duration_s: float | None = None   # 该级俯仰线性过渡时长（None=用该级燃烧时长）
    seco_met: float | None = None           # 末级：程序关机时刻（None=推进剂耗尽）
    sep_met: float | None = None            # 非末级：程序分离时刻（None=自然耗尽后分离）
    # 末级关机方式（v3.3）：
    #   None / "depletion" = 推进剂耗尽关机（历史行为）
    #   "target_orbit"     = 目标轨道关机：当瞬时根数落在
    #                        |a - a_target| <= seco_band_km 且 e <= seco_e_max 时关机
    #                        （用于二级推进剂有明显余量的任务，靠耗尽关机必然超能量）
    seco_mode: str | None = None
    cda_attached_m2: float = 0.0            # 该级在箭时的上升段 CdA（m²）


@dataclass
class Booster:
    """并联助推器：M 台同型；独立推进剂池，耗尽或到 jettison_met 分离。"""
    name: str = "助推器"
    count: int = 2
    dry_kg_each: float = 0.0
    prop_kg_each: float = 0.0
    thrusters: list[Thruster] = field(default_factory=list)
    jettison_met: float | None = None
    pitch_end_deg: float | None = None      # 助推器关机时的俯仰角（默认用一级的）


@dataclass
class Vehicle:
    stages: list[Stage]
    boosters: Booster | None = None
    fairing_mass_kg: float = 0.0
    fairing_halves: int = 2
    adapter_kg: float = 0.0
    payload_kg: float = 0.0
    debris: dict = field(default_factory=dict)
    fairing_cda_m2: float = 0.0
    cda_ascent_m2: float = 0.0
    ascent_cda_scale: float = 1.0

    @property
    def liftoff_mass(self) -> float:
        m = self.payload_kg + self.adapter_kg + self.fairing_mass_kg
        m += sum(s.dry_kg + s.prop_kg for s in self.stages)
        if self.boosters is not None:
            m += self.boosters.count * (self.boosters.dry_kg_each + self.boosters.prop_kg_each)
        return m

    def debris_spec(self, key: str, default_cda: float = 5.0) -> dict:
        d = dict(self.debris.get(key, {}))
        d.setdefault("cda", default_cda)
        d.setdefault("extra_mass_kg", 0.0)
        return d


@dataclass
class AscentParams:
    lat0: float
    lon0: float
    alt0: float
    azimuth_deg: float
    t0_utc: datetime
    # 俯仰程序
    t_v: float = 10.0
    t_p: float = 150.0
    pitch_power: float = 1.5
    # 制导
    guidance: str = "shooting_plane"     # shooting_plane | inertial_plane
    plane_keeping: bool = False
    yaw_tau: float = 150.0
    # 整流罩
    t_fairing: float | None = None
    fairing_divert_mps: float = 4.0
    # 末级平面修正（dogleg）
    i_target_deg: float | None = None
    raan_target_deg: float | None = None
    yaw2_tau: float = 200.0
    # 末级"目标轨道关机"（seco_mode="target_orbit"）用的目标高度与判据带宽
    target_alt_km: float | None = None
    seco_band_km: float = 12.0          # |a - a_target| 许可带 [km]
    seco_e_max: float = 0.006           # 许可偏心率
    # 可选：整条俯仰程序折线 [[t_s, pitch_deg], ...]（缺省 None = 用 t_v/t_p/各级 pitch_end）
    pitch_schedule: list | None = None
    # 数值（v2）
    numerics: str = "legacy"            # "legacy"=逐位复现 v1；"accurate"=事件对齐
    dt: float = 0.02
    dt_legacy: float = 0.02             # legacy 口径默认步长（v1 guidance.dt）
    dt_accurate: float = 0.2            # accurate 口径默认步长
    sample_dt: float = 0.5
    debris_dt: float = 0.25
    debris_numerics: str | None = None  # None=跟随 numerics
    t_max: float = 6000.0
    post_seco_s: float = 60.0
    stop_met: float | None = None
    flow_scale: float = 1.0
    # 残骸推进上限（s）
    debris_t_max: float = 6000.0


# ---------------------------------------------------------------- pitch program
class _Pitch:
    """分段开环俯仰程序（相对当地水平）；预排序常数，避免每步遍历级表。

    v3.3 新增：若 ``AscentParams.pitch_schedule`` 非空（``[[t_s, 俯仰角_deg], ...]``，按时间
    升序、首点 t=0），则直接按该折线插值——用于低推重比二级等"线性俯仰程序描述不够用"的
    火箭（可由任务给出一条标称俯仰剖面）；缺省仍走 t_v/t_p/pitch_power/各级 pitch_end 的老口径。
    """

    __slots__ = ("t_v", "t_p", "pw", "starts", "ends", "durs", "n", "sched_t", "sched_a")

    def __init__(self, p: AscentParams, stage_starts, pitch_ends, pitch_durs):
        self.t_v = p.t_v
        self.t_p = max(p.t_p, 1e-6)
        self.pw = p.pitch_power
        self.starts = stage_starts          # 保持引用：级起始时刻在飞行中原地更新
        self.ends = list(pitch_ends)
        self.durs = list(pitch_durs)
        self.n = len(self.ends)
        sched = getattr(p, "pitch_schedule", None)
        if sched:
            pts = sorted((float(a), float(b)) for a, b in sched)
            self.sched_t = [a for a, _b in pts]
            self.sched_a = [b for _a, b in pts]
        else:
            self.sched_t = None
            self.sched_a = None

    def __call__(self, t: float) -> float:
        if self.sched_t is not None:
            ts, ang = self.sched_t, self.sched_a
            if t <= ts[0]:
                return ang[0]
            if t >= ts[-1]:
                return ang[-1]
            for i in range(1, len(ts)):
                if t <= ts[i]:
                    f = (t - ts[i - 1]) / (ts[i] - ts[i - 1])
                    return ang[i - 1] + f * (ang[i] - ang[i - 1])
            return ang[-1]
        starts = self.starts
        si = 0
        for i in range(self.n - 1, -1, -1):
            if t >= starts[i]:
                si = i
                break
        if si == 0:
            if t < self.t_v:
                return 90.0
            x = (t - self.t_v) / self.t_p
            if x >= 1.0:
                return self.ends[0]
            return 90.0 - (90.0 - self.ends[0]) * (x ** self.pw)
        start = self.ends[si - 1]
        end = self.ends[si]
        dur = self.durs[si] if si < len(self.durs) else None
        if dur is None or dur <= 0:
            return end
        x = (t - starts[si]) / dur
        if x >= 1.0:
            return end
        return start + (end - start) * x


def _pitch_deg(t: float, p: AscentParams, stage_starts: list[float],
               pitch_ends: list[float], pitch_durs: list[float]) -> float:
    """v1 兼容接口（导出/回归用）。"""
    return _Pitch(p, stage_starts, pitch_ends, pitch_durs)(t)


# ---------------------------------------------------------------- acceleration
class _Accel:
    """右端加速度。构造一次，RK4 四阶段复用（v1 每步重建闭包）。

    方程与 v1.0 逐项相同：
      g = 二体 + J2；阻力用**地球相对速度** v + ω×r；推力沿导引方向；
      shooting_plane 用当地水平面方位基；inertial_plane 用惯性铅垂面法向。
    """

    __slots__ = ("theta0", "cos_az", "sin_az", "plane_normal", "plane2", "yaw_tau",
                 "yaw2_tau", "plane_keeping", "cda_scale", "pitch", "om2")

    def __init__(self, theta0, azimuth_rad, guidance, plane_normal, yaw_tau, yaw2_tau,
                 plane_keeping, cda_scale, pitch):
        self.theta0 = theta0
        self.cos_az = math.cos(azimuth_rad)
        self.sin_az = math.sin(azimuth_rad)
        self.plane_normal = plane_normal          # tuple | None
        self.plane2 = None                        # dogleg 平面法向（list[0] 可写）
        self.yaw_tau = max(10.0, yaw_tau)
        self.yaw2_tau = max(20.0, yaw2_tau)
        self.plane_keeping = bool(plane_keeping)
        self.cda_scale = cda_scale
        self.pitch = pitch
        self.om2 = None

    def __call__(self, t, x, y, z, vx, vy, vz, m, thrust_tot, cda_now, geo=None):
        r2 = x * x + y * y + z * z
        rn = math.sqrt(r2)
        r3 = r2 * rn
        k = -MU / r3
        zr2 = (z * z) / r2
        fac = 1.5 * J2 * (RE / rn) ** 2
        f1 = 1.0 - fac * (5.0 * zr2 - 1.0)
        f2 = 1.0 - fac * (5.0 * zr2 - 3.0)
        ax = k * x * f1
        ay = k * y * f1
        az = k * z * f2

        geo_given = geo is not None
        if not geo_given:
            lat, lon, alt = _lat_lon_alt(x, y, z)
        else:
            lat, lon, alt = geo
        pn = self.plane_normal
        pitch = self.pitch(t)
        pit = math.radians(pitch)
        sp = math.sin(pit)
        cp = math.cos(pit)
        if pn is not None:
            rrx, rry, rrz = x / rn, y / rn, z / rn
            nx_, ny_, nz_ = pn
            hx = ny_ * rrz - nz_ * rry
            hy = nz_ * rrx - nx_ * rrz
            hz = nx_ * rry - ny_ * rrx
            hn = math.sqrt(hx * hx + hy * hy + hz * hz)
            hx /= hn
            hy /= hn
            hz /= hn
            ux_t = sp * rrx + cp * hx
            uy_t = sp * rry + cp * hy
            uz_t = sp * rrz + cp * hz
        else:
            c2 = math.cos(lat)
            s2 = math.sin(lat)
            # lon 已是 ECI 赤经（= v1 的 lam + theta），here 直接使用
            cE = math.cos(lon)
            sE = math.sin(lon)
            ux = c2 * cE
            uy = c2 * sE
            uz = s2
            ex = -sE
            ey = cE
            nx = -s2 * cE
            ny = -s2 * sE
            nz = c2
            ca = self.cos_az
            sa = self.sin_az
            hx = ca * nx + sa * ex
            hy = ca * ny + sa * ey
            hz = ca * nz
            ux_t = sp * ux + cp * hx
            uy_t = sp * uy + cp * hy
            uz_t = sp * uz + cp * hz

        # 地球相对速度（v1 同名变量 rvx/rvy/uzz）
        rvx = vx + OMEGA * y
        rvy = vy - OMEGA * x
        spd = math.sqrt(rvx * rvx + rvy * rvy + vz * vz)
        if spd > 1e-6 and m > 1e-6:
            rho = density(alt)
            f = -0.5 * rho * cda_now * self.cda_scale * spd / m
            ax += f * rvx
            ay += f * rvy
            az += f * vz
        if thrust_tot > 0.0 and m > 1e-6:
            f = thrust_tot / m
            ax += f * ux_t
            ay += f * uy_t
            az += f * uz_t

        if pn is not None and self.plane_keeping:
            nx_, ny_, nz_ = pn
            d_out = x * nx_ + y * ny_ + z * nz_
            v_out = vx * nx_ + vy * ny_ + vz * nz_
            om = 1.0 / self.yaw_tau
            a_lat = -2.0 * om * v_out - om * om * d_out
            ax += a_lat * nx_
            ay += a_lat * ny_
            az += a_lat * nz_
        p2 = self.plane2
        if p2 is not None:
            nx_, ny_, nz_ = p2
            d_out = x * nx_ + y * ny_ + z * nz_
            v_out = vx * nx_ + vy * ny_ + vz * nz_
            om = 1.0 / self.yaw2_tau
            a_lat = -2.0 * om * v_out - om * om * d_out
            ax += a_lat * nx_
            ay += a_lat * ny_
            az += a_lat * nz_
        return ax, ay, az


# ---------------------------------------------------------------- 推力段工具
def _active_thrusters(stages, si, pi, seco):
    """当前推力段的多台发动机列表（与 v1 cur_thrusters 语义一致）。"""
    if seco or si >= len(stages):
        return None
    ph = stages[si].phases
    if pi >= len(ph):
        return None
    return ph[pi].thrusters


def _flow_sum(thrusters, p_amb, flow_scale):
    """多台发动机总推力/总流量；与 v1 th_flow（逐台 Thruster.flow 相加）逐位一致。"""
    thr = 0.0
    mdot = 0.0
    for th in thrusters:
        tv = th.thrust_vac_n * th.n * flow_scale
        ts = (th.thrust_sl_n if th.thrust_sl_n is not None else th.thrust_vac_n) * th.n * flow_scale
        iv = th.isp_vac_s
        isl = th.isp_sl_s if th.isp_sl_s is not None else th.isp_vac_s
        pr = 1.0 if p_amb >= P0 else (0.0 if p_amb <= 0.0 else p_amb / P0)
        thrust = ts + (tv - ts) * (1.0 - pr)
        isp = iv - (iv - isl) * pr
        thr += thrust
        if isp > 0:
            mdot += thrust / (isp * G0)
    return thr, mdot


def _rva(thr_tot, cda_now, veh, scale):
    return thr_tot, cda_now * scale


# ---------------------------------------------------------------- simulation
def _init_state(veh: Vehicle, p: AscentParams):
    theta0 = gmst_rad(p.t0_utc)
    x0, y0, z0 = geodetic_to_ecef(p.lat0, p.lon0, p.alt0)
    c, s = math.cos(theta0), math.sin(theta0)
    x, y, z = c * x0 - s * y0, s * x0 + c * y0, z0
    return theta0, x, y, z, -OMEGA * y, OMEGA * x, 0.0


def _guidance_plane(p, theta0, x, y, z, azimuth_rad):
    """inertial_plane 的法向（与 v1 相同）。"""
    if p.guidance != "inertial_plane":
        return None
    lat0r, lon0r = math.radians(p.lat0), math.radians(p.lon0)
    lam0 = lon0r + theta0
    cl, sl = math.cos(lam0), math.sin(lam0)
    cphi, sphi = math.cos(lat0r), math.sin(lat0r)
    north0 = np.array([-sphi * cl, -sphi * sl, cphi])
    east0 = np.array([-sl, cl, 0.0])
    hd0 = math.cos(azimuth_rad) * north0 + math.sin(azimuth_rad) * east0
    r0_hat = np.array([x, y, z]) / math.sqrt(x * x + y * y + z * z)
    n_hat = np.cross(r0_hat, hd0)
    return tuple(float(v) for v in n_hat / np.linalg.norm(n_hat))


def simulate(veh: Vehicle, p: AscentParams, collect_debris: bool = True) -> dict:
    """弹道仿真主入口（v2）：按 ``p.numerics`` 选择 legacy/accurate 内核。"""
    if p.numerics == "accurate":
        return _simulate_accurate(veh, p, collect_debris)
    return _simulate_legacy(veh, p, collect_debris)


def _finalize(veh, p, theta0, samples_t, samples_r, samples_v, samples_m, samples_ph,
              events, debris_states, failures, t_seco, seco, numerics, t_hist,
              collect_debris=True):
    out = dict(t=np.array(samples_t), r_eci=np.array(samples_r), v_eci=np.array(samples_v),
               mass=np.array(samples_m), phase=samples_ph, events=[], debris=[],
               t_seco=t_seco, failures=failures, vehicle=veh, params=p, numerics=numerics)
    n = len(samples_t)
    lat = np.empty(n)
    lon = np.empty(n)
    alt = np.empty(n)
    t0 = p.t0_utc
    for i in range(n):
        ts = samples_t[i]
        th = gmst_rad(t0 + timedelta(seconds=float(ts)))
        c, s = math.cos(th), math.sin(th)
        xx, yy, zz = samples_r[i]
        la, lo, al = ecef_to_geodetic(c * xx + s * yy, -s * xx + c * yy, zz)
        lat[i] = la
        lon[i] = lo
        alt[i] = al
    out.update(lat=lat, lon=lon, alt=alt, speed=np.linalg.norm(out["v_eci"], axis=1))
    ev_out = []
    for e in events:
        ts = e["t_met_s"]
        th = gmst_rad(t0 + timedelta(seconds=float(ts)))
        c, s = math.cos(th), math.sin(th)
        xx, yy, zz = e["r"]
        la, lo, al = ecef_to_geodetic(c * xx + s * yy, -s * xx + c * yy, zz)
        vv = e["v"]
        ev_out.append(dict(event=e["event"], t_met_s=ts, lat=la, lon=lo, alt_m=al,
                           speed_m_s=math.sqrt(vv[0] ** 2 + vv[1] ** 2 + vv[2] ** 2),
                           mass_kg=e["mass_kg"], note=e["note"],
                           r_eci=np.array(e["r"]), v_eci=np.array(e["v"])))
    out["events"] = ev_out
    if seco and t_seco is not None:
        idx = min(int(np.searchsorted(out["t"], t_seco)), len(out["t"]) - 1)
        out["orbit_seco"] = elements(out["r_eci"][idx], out["v_eci"][idx])
        out["insertion_state"] = dict(t_met_s=float(out["t"][idx]), lat=float(lat[idx]),
                                      lon=float(lon[idx]), alt_km=float(alt[idx] / 1000.0),
                                      speed_m_s=float(out["speed"][idx]),
                                      fpa_deg=flight_path_angle(out["r_eci"][idx], out["v_eci"][idx]),
                                      i_deg=out["orbit_seco"]["i_deg"])
    else:
        out["orbit_seco"] = None
        out["insertion_state"] = None
    if collect_debris:
        if (p.debris_numerics or p.numerics) == "accurate":
            out["debris"] = _propagate_debris_accurate(debris_states, p)
        else:
            out["debris"] = propagate_debris(debris_states, p)
    else:
        out["debris"] = []
    out["stats"] = dict(n_steps=int(t_hist), numerics=numerics, dt=p.dt)
    return out


def _stage_nominal_end(stages, si, pi):
    """该级「最后一段到点」判定（v1 stage_nominal_done 语义）。"""
    ph = stages[si].phases
    if pi >= len(ph):
        return False
    return ph[pi].end_met is not None


# ================================================================ legacy 内核
def _simulate_legacy(veh: Vehicle, p: AscentParams, collect_debris: bool) -> dict:
    """v1.0 循环语义逐点保留 + v2 快速右端函数。

    * 质量在**步首**按步首流量扣减（并使用扣减后的质量做整步 RK4）——与 v1 相同；
    * 事件只在步边界判定（步长量化）——与 v1 相同；
    * 因此结果与 v1 逐位可比（仅浮点重排带来的 ~1e-9 相对差）。
    """
    dt = p.dt
    theta0, x, y, z, vx, vy, vz = _init_state(veh, p)
    az = math.radians(p.azimuth_deg)
    plane_normal = _guidance_plane(p, theta0, x, y, z, az)
    yaw_tau = max(10.0, p.yaw_tau)
    yaw2_tau = max(20.0, p.yaw2_tau)

    stages = veh.stages
    n_st = len(stages)
    stage_prop = [s.prop_kg for s in stages]
    stage_starts = [0.0] + [float("inf")] * (n_st - 1)
    pitch_ends = [s.pitch_end_deg if s.pitch_end_deg is not None else 10.0 for s in stages]
    pitch_durs = [s.pitch_duration_s for s in stages]
    pitch = _Pitch(p, stage_starts, pitch_ends, pitch_durs)
    acc = _Accel(theta0, az, p.guidance, plane_normal, p.yaw_tau, p.yaw2_tau,
                 p.plane_keeping, veh.ascent_cda_scale, pitch)
    acc.plane2 = None

    boost = veh.boosters
    boost_prop = boost.prop_kg_each * boost.count if boost else 0.0
    boost_att = boost is not None

    mass = veh.liftoff_mass
    fairing_att = veh.fairing_mass_kg > 0.0
    si = 0
    pi = 0
    seco = False
    t_seco = None
    t_stage_sep = [None] * n_st
    events: list[dict] = []
    debris_states: list[tuple] = []
    failures: list[str] = []

    t = 0.0
    samples_t, samples_r, samples_v, samples_m, samples_ph = [], [], [], [], []
    next_sample = 0.0
    events.append(dict(event="起飞", t_met_s=0.0, r=(x, y, z), v=(vx, vy, vz),
                       mass_kg=mass, note="点火起飞（T0）"))

    def cur_thrusters():
        return _active_thrusters(stages, si, pi, seco)

    steps = 0
    while t < p.t_max:
        steps += 1
        lat_c, lam_c, alt_c = _local_frame(x, y, z, theta0 + OMEGA * t)
        if t >= next_sample - 1e-9:
            samples_t.append(t)
            samples_r.append((x, y, z))
            samples_v.append((vx, vy, vz))
            samples_m.append(mass)
            samples_ph.append("S%d" % (si + 1) if not seco else "ORBIT")
            next_sample += p.sample_dt
        if fairing_att and p.t_fairing is not None and t >= p.t_fairing:
            fairing_att = False
            mass -= veh.fairing_mass_kg
            events.append(dict(event="整流罩分离", t_met_s=t, r=(x, y, z), v=(vx, vy, vz),
                               mass_kg=mass,
                               note="%d 半整流罩抛离（%.2f t）" % (veh.fairing_halves,
                                                                 veh.fairing_mass_kg / 1000)))
            if collect_debris:
                debris_states.extend(_make_fairing(t, (x, y, z), (vx, vy, vz), p, theta0, veh))
        ph = stages[si].phases if (not seco and 0 <= si < n_st) else []
        thr = None
        stage_nominal_done = False
        if ph:
            while pi < len(ph) - 1 and ph[pi].end_met is not None and t >= ph[pi].end_met:
                if ph[pi].emit_event:
                    events.append(dict(event="%s%s关机" % (stages[si].name, ph[pi].name),
                                       t_met_s=t, r=(x, y, z), v=(vx, vy, vz), mass_kg=mass,
                                       note="程序关机（%s）" % ph[pi].name))
                pi += 1
            if ph[pi].end_met is not None and t >= ph[pi].end_met:
                stage_nominal_done = True
            else:
                thr = ph[pi].thrusters
        cda_now = veh.fairing_cda_m2 if fairing_att else 0.0
        for _st in stages[si:]:
            cda_now += _st.cda_attached_m2
        if cda_now <= 0.0:
            cda_now = veh.cda_ascent_m2
        rho_c, p_amb, _T = atmosphere(alt_c)
        thrust_tot = 0.0
        stage_mdot = 0.0
        boost_mdot = 0.0
        if thr:
            a_, stage_mdot = _flow_sum(thr, p_amb, p.flow_scale)
            thrust_tot += a_
        if boost_att:
            a_, boost_mdot = _flow_sum(boost.thrusters, p_amb, p.flow_scale)
            thrust_tot += a_
        mass = max(mass - (stage_mdot + boost_mdot) * dt, 1e-6)
        if si < n_st and stage_mdot > 0.0:
            stage_prop[si] = max(0.0, stage_prop[si] - stage_mdot * dt)
        if boost_att and boost_mdot > 0.0:
            boost_prop = max(0.0, boost_prop - boost_mdot * dt)

        if boost_att and ((boost_prop <= 1e-6) or
                          (boost.jettison_met is not None and t >= boost.jettison_met)):
            boost_att = False
            _bres = max(0.0, float(boost_prop))
            mass -= boost.count * boost.dry_kg_each + _bres
            events.append(dict(event="%s分离" % boost.name, t_met_s=t, r=(x, y, z),
                               v=(vx, vy, vz), mass_kg=mass,
                               note="%d 台%s分离（剩余推进剂 %.0f kg）"
                                    % (boost.count, boost.name, _bres)))
            if collect_debris:
                debris_states.extend(_make_booster(t, (x, y, z), (vx, vy, vz), p, theta0, veh))
        if not seco and si < n_st:
            phase_done = stage_nominal_done or (stage_prop[si] <= 1e-6)
            if stages[si].sep_met is not None and t >= stages[si].sep_met:
                phase_done = True
            if si == n_st - 1 and stages[si].seco_met is not None and t >= stages[si].seco_met:
                phase_done = True
            if phase_done:
                if si == n_st - 1:
                    seco = True
                    t_seco = t
                    acc.plane2 = None      # v1 s2_active 语义：SECO 后不再做平面修正
                    events.append(dict(event="%s关机(SECO)" % stages[si].name, t_met_s=t,
                                       r=(x, y, z), v=(vx, vy, vz), mass_kg=mass,
                                       note="末级程序/耗尽关机（入轨）"))
                else:
                    t_stage_sep[si] = t
                    _residual = max(0.0, float(stage_prop[si]))
                    mass -= stages[si].dry_kg + _residual
                    events.append(dict(event="%s关机/分离" % stages[si].name, t_met_s=t,
                                       r=(x, y, z), v=(vx, vy, vz), mass_kg=mass,
                                       note="%s分离（剩余推进剂 %.0f kg 随级抛离）"
                                            % (stages[si].name, _residual)))
                    if collect_debris:
                        debris_states.extend(_make_stage(t, (x, y, z), (vx, vy, vz), p, theta0,
                                                         veh, si, stages[si]))
                    si += 1
                    pi = 0
                    if si < n_st:
                        stage_starts[si] = t
                        events.append(dict(event="%s点火" % stages[si].name, t_met_s=t,
                                           r=(x, y, z), v=(vx, vy, vz), mass_kg=mass,
                                           note="%s主机点火" % stages[si].name))
                        if p.i_target_deg is not None and si == n_st - 1:
                            if getattr(p, "raan_target_deg", None) is not None:
                                _set_plane2(acc, _plane_for_target(
                                (x, y, z), (vx, vy, vz), p.i_target_deg,
                                p.raan_target_deg, events, t, mass))
                            else:
                                _set_plane2(acc, _plane_for_inclination(
                                (x, y, z), (vx, vy, vz), p.i_target_deg, events, t, mass))
        if seco and t_seco is not None and t >= t_seco + p.post_seco_s:
            break
        if p.stop_met is not None and t >= p.stop_met:
            break

        if _DEBUG_T > 0 and abs(t - _DEBUG_T) < dt * 0.6:
            print("[KIT] t=%.2f alt=%.2f thr=%.6f mdot=%.4f m=%.4f pitch=%.4f cda=%.3f" % (
                t, alt_c / 1000.0, thrust_tot / 1e6, stage_mdot + boost_mdot, mass,
                pitch(t), cda_now))
        # ---- RK4（推力/质量/气动在一整步内视为常数：与 v1 相同）----
        a1 = acc(t, x, y, z, vx, vy, vz, mass, thrust_tot, cda_now)
        h2 = dt * 0.5
        a2 = acc(t + h2, x + h2 * vx, y + h2 * vy, z + h2 * vz,
                 vx + h2 * a1[0], vy + h2 * a1[1], vz + h2 * a1[2], mass, thrust_tot, cda_now)
        a3 = acc(t + h2, x + h2 * (vx + h2 * a1[0]), y + h2 * (vy + h2 * a1[1]),
                 z + h2 * (vz + h2 * a1[2]),
                 vx + h2 * a2[0], vy + h2 * a2[1], vz + h2 * a2[2], mass, thrust_tot, cda_now)
        a4 = acc(t + dt, x + dt * (vx + h2 * a2[0]), y + dt * (vy + h2 * a2[1]),
                 z + dt * (vz + h2 * a2[2]),
                 vx + dt * a3[0], vy + dt * a3[1], vz + dt * a3[2], mass, thrust_tot, cda_now)
        x += dt / 6.0 * (vx + 2 * (vx + h2 * a1[0]) + 2 * (vx + h2 * a2[0]) + (vx + dt * a3[0]))
        y += dt / 6.0 * (vy + 2 * (vy + h2 * a1[1]) + 2 * (vy + h2 * a2[1]) + (vy + dt * a3[1]))
        z += dt / 6.0 * (vz + 2 * (vz + h2 * a1[2]) + 2 * (vz + h2 * a2[2]) + (vz + dt * a3[2]))
        vx += dt / 6.0 * (a1[0] + 2 * a2[0] + 2 * a3[0] + a4[0])
        vy += dt / 6.0 * (a1[1] + 2 * a2[1] + 2 * a3[1] + a4[1])
        vz += dt / 6.0 * (a1[2] + 2 * a2[2] + 2 * a3[2] + a4[2])
        t += dt
        if t > 1.0 and _lat_lon_alt(x, y, z)[2] < -100.0 and not seco:
            failures.append("非预期落地 t=%.1f s" % t)
            break
    return _finalize(veh, p, theta0, samples_t, samples_r, samples_v, samples_m, samples_ph,
                     events, debris_states, failures, t_seco, seco, "legacy", steps,
                     collect_debris)


# ================================================================ accurate 内核
def _simulate_accurate(veh: Vehicle, p: AscentParams, collect_debris: bool) -> dict:
    """事件对齐 + 质量/推进剂随 RK4 同步积分的收敛内核。

    步长断点 = min(dt, 下一事件时刻, 下一采样时刻, 推进剂耗尽预测时刻)；
    事件时刻精确落在断点上，因此结果与 dt 无关。
    """
    dt = p.dt
    theta0, x, y, z, vx, vy, vz = _init_state(veh, p)
    az = math.radians(p.azimuth_deg)
    plane_normal = _guidance_plane(p, theta0, x, y, z, az)

    stages = veh.stages
    n_st = len(stages)
    stage_starts = [0.0] + [float("inf")] * (n_st - 1)
    pitch_ends = [s.pitch_end_deg if s.pitch_end_deg is not None else 10.0 for s in stages]
    pitch_durs = [s.pitch_duration_s for s in stages]
    pitch = _Pitch(p, stage_starts, pitch_ends, pitch_durs)
    acc = _Accel(theta0, az, p.guidance, plane_normal, p.yaw_tau, p.yaw2_tau,
                 p.plane_keeping, veh.ascent_cda_scale, pitch)

    boost = veh.boosters
    boost_prop = boost.prop_kg_each * boost.count if boost else 0.0
    boost_att = boost is not None
    boost_mdot0 = 0.0

    mass = veh.liftoff_mass
    prop_cur = stages[0].prop_kg
    fairing_att = veh.fairing_mass_kg > 0.0
    si = 0
    pi = 0
    seco = False
    t_seco = None
    stage_active = True
    events: list[dict] = []
    debris_states: list[tuple] = []
    failures: list[str] = []

    t = 0.0
    samples_t, samples_r, samples_v, samples_m, samples_ph = [], [], [], [], []
    next_sample = 0.0
    events.append(dict(event="起飞", t_met_s=0.0, r=(x, y, z), v=(vx, vy, vz),
                       mass_kg=mass, note="点火起飞（T0）"))
    steps = 0
    last_alt = p.alt0

    def record_sample(tt):
        samples_t.append(tt)
        samples_r.append((x, y, z))
        samples_v.append((vx, vy, vz))
        samples_m.append(mass)
        samples_ph.append("S%d" % (si + 1) if not seco else "ORBIT")

    record_sample(0.0)
    next_sample = p.sample_dt

    # ---- RK4 右端（闭包只建一次；thr/cda_now/boost_att 由外层循环更新）----
    def deriv(tt, xs, ys, zs, vxs, vys, vzs, mm):
        geo = _lat_lon_alt(xs, ys, zs)
        p_amb_ = atmosphere(geo[2])[1]
        thr_tot = 0.0
        md_s = 0.0
        if thr is not None:
            thr_tot, md_s = _flow_sum(thr, p_amb_, p.flow_scale)
        md_b = 0.0
        if boost_att:
            a_b, md_b = _flow_sum(boost.thrusters, p_amb_, p.flow_scale)
            thr_tot += a_b
        ax_, ay_, az_ = acc(tt, xs, ys, zs, vxs, vys, vzs, mm, thr_tot, cda_now, geo)
        return ax_, ay_, az_, md_s, md_b

    # ---- 事件状态机 ----
    while t < p.t_max:
        # (a) 处理「到达 t」的已知事件（只处理一次）
        if fairing_att and p.t_fairing is not None and t >= p.t_fairing - 1e-12:
            fairing_att = False
            mass -= veh.fairing_mass_kg
            events.append(dict(event="整流罩分离", t_met_s=t, r=(x, y, z), v=(vx, vy, vz),
                               mass_kg=mass,
                               note="%d 半整流罩抛离（%.2f t）" % (veh.fairing_halves,
                                                                 veh.fairing_mass_kg / 1000)))
            if collect_debris:
                debris_states.extend(_make_fairing(t, (x, y, z), (vx, vy, vz), p, theta0, veh))
        # 当前级推力段切换（end_met 精确落在断点上）
        while (not seco and si < n_st and pi < len(stages[si].phases) - 1
               and stages[si].phases[pi].end_met is not None
               and t >= stages[si].phases[pi].end_met - 1e-12):
            ph = stages[si].phases[pi]
            if ph.emit_event:
                events.append(dict(event="%s%s关机" % (stages[si].name, ph.name),
                                   t_met_s=t, r=(x, y, z), v=(vx, vy, vz), mass_kg=mass,
                                   note="程序关机（%s）" % ph.name))
            pi += 1
        # 推进剂耗尽 / 程序关机 → 级分离或 SECO
        if not seco and si < n_st:
            ph = stages[si].phases[pi] if pi < len(stages[si].phases) else None
            prop_empty = stage_active and prop_cur <= 1e-9
            nominal_end = (ph is not None and ph.end_met is not None
                           and t >= ph.end_met - 1e-12)
            seco_now = (si == n_st - 1 and stages[si].seco_met is not None
                        and t >= stages[si].seco_met - 1e-12)
            sep_now = (stages[si].sep_met is not None
                       and t >= stages[si].sep_met - 1e-12)
            if prop_empty or nominal_end or seco_now or sep_now:
                if si == n_st - 1:
                    seco = True
                    t_seco = t
                    stage_active = False
                    acc.plane2 = None      # v1 s2_active 语义：SECO 后不再做平面修正
                    events.append(dict(event="%s关机(SECO)" % stages[si].name, t_met_s=t,
                                       r=(x, y, z), v=(vx, vy, vz), mass_kg=mass,
                                       note="末级程序/耗尽关机（入轨）"))
                else:
                    _residual = max(0.0, float(prop_cur))
                    mass -= stages[si].dry_kg + _residual
                    events.append(dict(event="%s关机/分离" % stages[si].name, t_met_s=t,
                                       r=(x, y, z), v=(vx, vy, vz), mass_kg=mass,
                                       note="%s分离（剩余推进剂 %.0f kg 随级抛离）"
                                            % (stages[si].name, _residual)))
                    if collect_debris:
                        debris_states.extend(_make_stage(t, (x, y, z), (vx, vy, vz), p, theta0,
                                                         veh, si, stages[si]))
                    si += 1
                    pi = 0
                    stage_active = True
                    prop_cur = stages[si].prop_kg
                    stage_starts[si] = t
                    events.append(dict(event="%s点火" % stages[si].name, t_met_s=t,
                                       r=(x, y, z), v=(vx, vy, vz), mass_kg=mass,
                                       note="%s主机点火" % stages[si].name))
                    if p.i_target_deg is not None and si == n_st - 1:
                        if getattr(p, "raan_target_deg", None) is not None:
                            _set_plane2(acc, _plane_for_target(
                            (x, y, z), (vx, vy, vz), p.i_target_deg,
                            p.raan_target_deg, events, t, mass))
                        else:
                            _set_plane2(acc, _plane_for_inclination(
                            (x, y, z), (vx, vy, vz), p.i_target_deg, events, t, mass))
        # v3.3：末级「目标轨道关机」—— 瞬时根数进入目标带即关机。
        # 仅当该级 seco_mode="target_orbit" 且 config 给了 reference_orbit.alt_km 时启用；
        # 典型用途：二级推进剂对本次任务明显有余量（耗尽关机必然超能量、解出大椭圆/双曲线）。
        # 判据：|a - (Re+h_target)| <= seco_band_km 且 e <= seco_e_max（默认 12 km / 0.006，
        # 对应近远地点各偏离目标 ~±40 km 以内）。不会提前于 60 km 高度触发。
        if (not seco and si == n_st - 1 and stage_active
                and stages[si].seco_mode == "target_orbit"
                and p.target_alt_km is not None):
            _alt_now = _lat_lon_alt(x, y, z)[2]
            if _alt_now > 60e3:
                _el = elements((x, y, z), (vx, vy, vz))
                _a_t = (RE + p.target_alt_km * 1000.0) / 1000.0
                if (_el.get("a_km") is not None
                        and abs(_el["a_km"] - _a_t) <= p.seco_band_km
                        and float(_el.get("e") or 1.0) <= p.seco_e_max):
                    seco = True
                    t_seco = t
                    stage_active = False
                    acc.plane2 = None
                    events.append(dict(event="%s关机(SECO)" % stages[si].name, t_met_s=t,
                                       r=(x, y, z), v=(vx, vy, vz), mass_kg=mass,
                                       note="目标轨道关机（a=%.1f km、e=%.4f 进入 %.0f km 目标带）"
                                            % (_el["a_km"], float(_el.get("e") or 0.0),
                                               p.target_alt_km)))
        if boost_att and ((boost_prop <= 1e-9) or
                          (boost.jettison_met is not None and t >= boost.jettison_met - 1e-12)):
            boost_att = False
            _bres = max(0.0, float(boost_prop))
            mass -= boost.count * boost.dry_kg_each + _bres
            events.append(dict(event="%s分离" % boost.name, t_met_s=t, r=(x, y, z),
                               v=(vx, vy, vz), mass_kg=mass,
                               note="%d 台%s分离（剩余推进剂 %.0f kg）"
                                    % (boost.count, boost.name, _bres)))
            if collect_debris:
                debris_states.extend(_make_booster(t, (x, y, z), (vx, vy, vz), p, theta0, veh))

        if seco and t_seco is not None and t >= t_seco + p.post_seco_s - 1e-12:
            break
        if p.stop_met is not None and t >= p.stop_met - 1e-12:
            break

        # (b) 该区间内的推力/气动
        thr = None
        if stage_active and not seco and si < n_st:
            ph = stages[si].phases[pi] if pi < len(stages[si].phases) else None
            if ph is not None and not (ph.end_met is not None and t >= ph.end_met - 1e-12):
                thr = ph.thrusters
        cda_now = veh.fairing_cda_m2 if fairing_att else 0.0
        for _st in stages[si:]:
            cda_now += _st.cda_attached_m2
        if cda_now <= 0.0:
            cda_now = veh.cda_ascent_m2

        # (c) 步长：受 dt / 事件 / 采样 / 推进剂耗尽预测约束
        h = dt
        if next_sample > t:
            h = min(h, next_sample - t)
        for tt in _next_event_times(p, stages, si, pi, seco, stage_active, boost_att, boost):
            if tt > t:
                d_ev = tt - t
                if d_ev < h:
                    h = d_ev
        geo_now = _lat_lon_alt(x, y, z)
        p_amb_now = atmosphere(geo_now[2])[1]
        if thr:
            _, mdot_s = _flow_sum(thr, p_amb_now, p.flow_scale)
            if mdot_s > 0.0:
                h = min(h, prop_cur / mdot_s)
        if boost_att:
            _, boost_mdot0 = _flow_sum(boost.thrusters, p_amb_now, p.flow_scale)
            if boost_mdot0 > 0.0:
                h = min(h, boost_prop / boost_mdot0)
        if h < 1e-9:
            h = 1e-9

        # (d) RK4：质量与推进剂作为**状态**同步积分（推力/阻力按各阶段质量求值）
        h2 = 0.5 * h
        ax1, ay1, az1, ms1, mb1 = deriv(t, x, y, z, vx, vy, vz, mass)
        m2 = mass - h2 * (ms1 + mb1)
        ax2, ay2, az2, ms2, mb2 = deriv(
            t + h2, x + h2 * vx, y + h2 * vy, z + h2 * vz,
            vx + h2 * ax1, vy + h2 * ay1, vz + h2 * az1, m2)
        m3 = mass - h2 * (ms2 + mb2)
        ax3, ay3, az3, ms3, mb3 = deriv(
            t + h2, x + h2 * (vx + h2 * ax1), y + h2 * (vy + h2 * ay1), z + h2 * (vz + h2 * az1),
            vx + h2 * ax2, vy + h2 * ay2, vz + h2 * az2, m3)
        m4 = mass - h * (ms3 + mb3)
        ax4, ay4, az4, ms4, mb4 = deriv(
            t + h, x + h * (vx + h2 * ax2), y + h * (vy + h2 * ay2), z + h * (vz + h2 * az2),
            vx + h * ax3, vy + h * ay3, vz + h * az3, m4)
        x += h / 6.0 * (vx + 2 * (vx + h2 * ax1) + 2 * (vx + h2 * ax2) + (vx + h * ax3))
        y += h / 6.0 * (vy + 2 * (vy + h2 * ay1) + 2 * (vy + h2 * ay2) + (vy + h * ay3))
        z += h / 6.0 * (vz + 2 * (vz + h2 * az1) + 2 * (vz + h2 * az2) + (vz + h * az3))
        vx += h / 6.0 * (ax1 + 2 * ax2 + 2 * ax3 + ax4)
        vy += h / 6.0 * (ay1 + 2 * ay2 + 2 * ay3 + ay4)
        vz += h / 6.0 * (az1 + 2 * az2 + 2 * az3 + az4)
        dm = h / 6.0 * (ms1 + 2 * ms2 + 2 * ms3 + ms4)
        mass -= dm + h / 6.0 * (mb1 + 2 * mb2 + 2 * mb3 + mb4)
        if dm > 0.0:
            prop_cur -= dm
        if boost_att:
            boost_prop -= h / 6.0 * (mb1 + 2 * mb2 + 2 * mb3 + mb4)
        t += h
        steps += 1
        if t > 1.0:
            if _lat_lon_alt(x, y, z)[2] < -100.0 and not seco:
                failures.append("非预期落地 t=%.1f s" % t)
                break
        if t >= next_sample - 1e-9:
            record_sample(t)
            while next_sample <= t + 1e-12:
                next_sample += p.sample_dt
    return _finalize(veh, p, theta0, samples_t, samples_r, samples_v, samples_m, samples_ph,
                     events, debris_states, failures, t_seco, seco, "accurate", steps,
                     collect_debris)


def _next_event_times(p, stages, si, pi, seco, stage_active, boost_att, boost):
    """当前时刻之后的**已知时刻**断点（推进剂耗尽由调用方按流量预测）。"""
    ts = []
    if p.t_fairing is not None:
        ts.append(p.t_fairing)
    if not seco and si < len(stages):
        ph = stages[si].phases[pi] if pi < len(stages[si].phases) else None
        if ph is not None and ph.end_met is not None:
            ts.append(ph.end_met)
        if stages[si].seco_met is not None:
            ts.append(stages[si].seco_met)
    if boost_att and boost is not None and boost.jettison_met is not None:
        ts.append(boost.jettison_met)
    if p.stop_met is not None:
        ts.append(p.stop_met)
    return ts


def _set_plane2(acc, normal):
    acc.plane2 = normal


# ---------------------------------------------------------------- 残骸
def _lateral_unit(x, y, z, t, p, theta0):
    lat, lam, alt = _local_frame(x, y, z, theta0 + OMEGA * t)
    th = theta0 + OMEGA * t
    cl, sl = math.cos(lam + th), math.sin(lam + th)
    c2, s2 = math.cos(lat), math.sin(lat)
    ux, uy, uz = c2 * cl, c2 * sl, s2
    ex, ey = -sl, cl
    a = math.radians(p.azimuth_deg)
    hx = math.cos(a) * (-s2 * cl) + math.sin(a) * ex
    hy = math.cos(a) * (-s2 * sl) + math.sin(a) * ey
    hz = math.cos(a) * c2
    lx, ly, lz = uy * hz - uz * hy, uz * hx - ux * hz, ux * hy - uy * hx
    nn = math.sqrt(lx * lx + ly * ly + lz * lz)
    return lx / nn, ly / nn, lz / nn


def _make_stage(t, r, v, p, theta0, veh, si, stage):
    d = veh.debris_spec("stage_%d" % si, default_cda=5.0)
    return [("%s" % stage.name, stage.name, "#8e44ad", stage.dry_kg + d["extra_mass_kg"],
             float(d["cda"]), t, r, v)]


def _make_booster(t, r, v, p, theta0, veh):
    b = veh.boosters
    d = veh.debris_spec("boosters", default_cda=8.0)
    lx, ly, lz = _lateral_unit(*r, t, p, theta0)
    out = []
    for k in range(b.count):
        sign = 1.0 if k % 2 == 0 else -1.0
        out.append(("booster_%d" % (k + 1), "%s%d" % (b.name, k + 1), "#e67e22",
                    b.dry_kg_each + d["extra_mass_kg"], float(d["cda"]), t, r,
                    (v[0] + sign * d.get("divert_mps", 6.0) * lx,
                     v[1] + sign * d.get("divert_mps", 6.0) * ly,
                     v[2] + sign * d.get("divert_mps", 6.0) * lz)))
    return out


def _make_fairing(t, r, v, p, theta0, veh):
    d = veh.debris_spec("fairing", default_cda=10.0)
    lx, ly, lz = _lateral_unit(*r, t, p, theta0)
    m_half = veh.fairing_mass_kg / max(veh.fairing_halves, 1)
    out = []
    for k in range(veh.fairing_halves):
        sign = 1.0 if k % 2 == 0 else -1.0
        out.append(("fairing_%s" % ("L" if sign > 0 else "R"),
                    "整流罩%s半" % ("左" if sign > 0 else "右"), "#f39c12",
                    m_half + d["extra_mass_kg"], float(d["cda"]), t, r,
                    (v[0] + sign * p.fairing_divert_mps * lx,
                     v[1] + sign * p.fairing_divert_mps * ly,
                     v[2] + sign * p.fairing_divert_mps * lz)))
    return out


def _debris_rhs_factory(theta0):
    """残骸右端：只依赖状态，GMST 常数只算一次（v1 每步调用 gmst_rad）。"""

    def acc(t, x, y, z, vx, vy, vz, cda_m):
        r2 = x * x + y * y + z * z
        rn = math.sqrt(r2)
        r3 = r2 * rn
        k = -MU / r3
        zr2 = (z * z) / r2
        fac = 1.5 * J2 * (RE / rn) ** 2
        f1 = 1.0 - fac * (5.0 * zr2 - 1.0)
        f2 = 1.0 - fac * (5.0 * zr2 - 3.0)
        ax = k * x * f1
        ay = k * y * f1
        az = k * z * f2
        alt = _lat_lon_alt(x, y, z)[2]
        rvx = vx + OMEGA * y
        rvy = vy - OMEGA * x
        spd = math.sqrt(rvx * rvx + rvy * rvy + vz * vz)
        if spd > 1e-6:
            f = -0.5 * density(alt) * cda_m * spd
            ax += f * rvx
            ay += f * rvy
            az += f * vz
        return ax, ay, az

    return acc


def propagate_debris(debris_states, p: AscentParams):
    """v1 兼容的残骸弹道（固定 debris_dt，落点按步末判定）。"""
    acc = _debris_rhs_factory(gmst_rad(p.t0_utc))
    out = []
    for obj, name_cn, color, mass, cda, t_sep, r0, v0 in debris_states:
        k_bal = cda / mass if mass > 0 else 0.0
        x, y, z = r0
        vx, vy, vz = v0
        t = t_sep
        ts, rs = [t], [(x, y, z)]
        impact = None
        ddt = p.debris_dt
        while t < t_sep + p.debris_t_max:
            lat, lon, alt = ecef_to_geodetic(
                *_eci_to_ecef_scalar(x, y, z, p.t0_utc, t))
            if alt <= 0.0:
                impact = dict(t_met_s=t, lat=lat, lon=lon, alt_m=alt,
                              speed_m_s=math.sqrt(vx * vx + vy * vy + vz * vz))
                break
            a1 = acc(t, x, y, z, vx, vy, vz, k_bal)
            h2 = ddt * 0.5
            a2 = acc(t + h2, x + h2 * vx, y + h2 * vy, z + h2 * vz,
                     vx + h2 * a1[0], vy + h2 * a1[1], vz + h2 * a1[2], k_bal)
            a3 = acc(t + h2, x + h2 * (vx + h2 * a1[0]), y + h2 * (vy + h2 * a1[1]),
                     z + h2 * (vz + h2 * a1[2]),
                     vx + h2 * a2[0], vy + h2 * a2[1], vz + h2 * a2[2], k_bal)
            a4 = acc(t + ddt, x + ddt * (vx + h2 * a2[0]), y + ddt * (vy + h2 * a2[1]),
                     z + ddt * (vz + h2 * a2[2]),
                     vx + ddt * a3[0], vy + ddt * a3[1], vz + ddt * a3[2], k_bal)
            x += ddt / 6.0 * (vx + 2 * (vx + h2 * a1[0]) + 2 * (vx + h2 * a2[0]) + (vx + ddt * a3[0]))
            y += ddt / 6.0 * (vy + 2 * (vy + h2 * a1[1]) + 2 * (vy + h2 * a2[1]) + (vy + ddt * a3[1]))
            z += ddt / 6.0 * (vz + 2 * (vz + h2 * a1[2]) + 2 * (vz + h2 * a2[2]) + (vz + ddt * a3[2]))
            vx += ddt / 6.0 * (a1[0] + 2 * a2[0] + 2 * a3[0] + a4[0])
            vy += ddt / 6.0 * (a1[1] + 2 * a2[1] + 2 * a3[1] + a4[1])
            vz += ddt / 6.0 * (a1[2] + 2 * a2[2] + 2 * a3[2] + a4[2])
            t += ddt
            ts.append(t)
            rs.append((x, y, z))
        if impact is None:
            lat, lon, alt = ecef_to_geodetic(*_eci_to_ecef_scalar(x, y, z, p.t0_utc, t))
            impact = dict(t_met_s=t, lat=lat, lon=lon, alt_m=alt,
                          speed_m_s=math.sqrt(vx * vx + vy * vy + vz * vz), note="未落地(超时)")
        out.append(dict(obj=obj, name_cn=name_cn, color=color, mass=mass, t_sep=t_sep,
                        t=np.array(ts), r_eci=np.array(rs), impact=impact))
    return out


def _eci_to_ecef_scalar(x, y, z, t0, t):
    th = gmst_rad(t0 + timedelta(seconds=float(t)))
    c, s = math.cos(th), math.sin(th)
    return c * x + s * y, -s * x + c * y, z


def _propagate_debris_accurate(debris_states, p: AscentParams):
    """残骸弹道（accurate）：RK4 + 落点二分（消除 0.25 s 落点量化）。"""
    acc = _debris_rhs_factory(gmst_rad(p.t0_utc))
    ddt = max(p.debris_dt, 0.05)
    out = []
    for obj, name_cn, color, mass, cda, t_sep, r0, v0 in debris_states:
        k_bal = cda / mass if mass > 0 else 0.0
        t = t_sep
        st = [r0[0], r0[1], r0[2], v0[0], v0[1], v0[2]]
        ts = [t]
        rs = [tuple(st[:3])]
        impact = None
        while t < t_sep + p.debris_t_max:
            alt = _alt_of(st)
            if alt <= 0.0:
                break
            s2 = _rk4_step_debris(acc, t, st, ddt, k_bal)
            alt2 = _alt_of(s2)
            if alt2 <= 0.0:
                # 二分定位落地时刻（高度是时间的单调函数，近地段）
                lo, hi = 0.0, ddt
                for _ in range(60):
                    mid = 0.5 * (lo + hi)
                    sm = _rk4_step_debris(acc, t, st, mid, k_bal)
                    if _alt_of(sm) > 0.0:
                        lo = mid
                    else:
                        hi = mid
                    if hi - lo < 1e-7:
                        break
                tm = t + 0.5 * (lo + hi)
                st = _rk4_step_debris(acc, t, st, 0.5 * (lo + hi), k_bal)
                t = tm
                ts.append(t)
                rs.append(tuple(st[:3]))
                lat, lon, altf = _geo_of(st, p, t)
                impact = dict(t_met_s=t, lat=lat, lon=lon, alt_m=0.0,
                              speed_m_s=math.sqrt(st[3] ** 2 + st[4] ** 2 + st[5] ** 2))
                break
            st = s2
            t += ddt
            ts.append(t)
            rs.append(tuple(st[:3]))
        if impact is None:
            lat, lon, altf = _geo_of(st, p, t)
            impact = dict(t_met_s=t, lat=lat, lon=lon, alt_m=altf,
                          speed_m_s=math.sqrt(st[3] ** 2 + st[4] ** 2 + st[5] ** 2),
                          note="未落地(超时)")
        out.append(dict(obj=obj, name_cn=name_cn, color=color, mass=mass, t_sep=t_sep,
                        t=np.array(ts), r_eci=np.array(rs), impact=impact))
    return out


def _alt_of(st):
    return _lat_lon_alt(st[0], st[1], st[2])[2]


def _geo_of(st, p, t):
    return ecef_to_geodetic(*_eci_to_ecef_scalar(st[0], st[1], st[2], p.t0_utc, t))


def _rk4_step_debris(acc, t, st, h, k_bal):
    x, y, z, vx, vy, vz = st
    a1 = acc(t, x, y, z, vx, vy, vz, k_bal)
    h2 = 0.5 * h
    a2 = acc(t + h2, x + h2 * vx, y + h2 * vy, z + h2 * vz,
             vx + h2 * a1[0], vy + h2 * a1[1], vz + h2 * a1[2], k_bal)
    a3 = acc(t + h2, x + h2 * (vx + h2 * a1[0]), y + h2 * (vy + h2 * a1[1]),
             z + h2 * (vz + h2 * a1[2]),
             vx + h2 * a2[0], vy + h2 * a2[1], vz + h2 * a2[2], k_bal)
    a4 = acc(t + h, x + h * (vx + h2 * a2[0]), y + h * (vy + h2 * a2[1]),
             z + h * (vz + h2 * a2[2]),
             vx + h * a3[0], vy + h * a3[1], vz + h * a3[2], k_bal)
    return [x + h / 6.0 * (vx + 2 * (vx + h2 * a1[0]) + 2 * (vx + h2 * a2[0]) + (vx + h * a3[0])),
            y + h / 6.0 * (vy + 2 * (vy + h2 * a1[1]) + 2 * (vy + h2 * a2[1]) + (vy + h * a3[1])),
            z + h / 6.0 * (vz + 2 * (vz + h2 * a1[2]) + 2 * (vz + h2 * a2[2]) + (vz + h * a3[2])),
            vx + h / 6.0 * (a1[0] + 2 * a2[0] + 2 * a3[0] + a4[0]),
            vy + h / 6.0 * (a1[1] + 2 * a2[1] + 2 * a3[1] + a4[1]),
            vz + h / 6.0 * (a1[2] + 2 * a2[2] + 2 * a3[2] + a4[2])]


def _plane_for_inclination(r, v, i_target_deg, events, t, mass):
    rn = math.sqrt(r[0] ** 2 + r[1] ** 2 + r[2] ** 2)
    rv = np.array(r) / rn
    c0 = rv[2]
    den = 1.0 - c0 * c0
    ci = math.cos(math.radians(i_target_deg))
    if den <= 1e-9 or abs(ci) > math.sqrt(den):
        return None
    cross = np.cross(rv, [0.0, 0.0, 1.0])
    cross = cross / np.linalg.norm(cross)
    b = ci / den
    a = -c0 * b
    cc = math.sqrt(max(0.0, (den - ci * ci) / den))
    n1 = a * rv + b * np.array([0.0, 0.0, 1.0]) + cc * cross
    n2 = a * rv + b * np.array([0.0, 0.0, 1.0]) - cc * cross
    h_now = np.cross(rv, np.array(v))
    h_now = h_now / np.linalg.norm(h_now)
    n2_sel = n1 if abs(float(np.dot(n1, h_now))) >= abs(float(np.dot(n2, h_now))) else n2
    events.append(dict(event="末级平面修正(dogleg)开始", t_met_s=t, r=tuple(r), v=tuple(v),
                       mass_kg=mass, note="目标倾角 %.2f°" % i_target_deg))
    return tuple(float(x) for x in n2_sel)


def _plane_for_target(r, v, i_target_deg, raan_target_deg, events, t, mass):
    """由目标倾角 + 目标 RAAN 构造平面法向（完整平面，同时修正倾角与 RAAN）。"""
    i = math.radians(float(i_target_deg))
    O = math.radians(float(raan_target_deg))
    n = np.array([math.sin(i) * math.sin(O),
                  -math.sin(i) * math.cos(O),
                  math.cos(i)])
    h = np.cross(np.asarray(r, dtype=float), np.asarray(v, dtype=float))
    if float(np.dot(n, h)) < 0.0:
        n = -n
    events.append(dict(event="末级平面修正(dogleg)开始", t_met_s=t,
                       r=tuple(r), v=tuple(v), mass_kg=mass,
                       note="目标倾角 %.2f° / RAAN %.2f°"
                            % (float(i_target_deg), float(raan_target_deg))))
    return tuple(float(x) for x in n)


# ---------------------------------------------------------------- 轨道根数
def elements(r_eci, v_eci) -> dict:
    r = np.asarray(r_eci, dtype=float)
    v = np.asarray(v_eci, dtype=float)
    rn, vn = float(np.linalg.norm(r)), float(np.linalg.norm(v))
    h = np.cross(r, v)
    hn = float(np.linalg.norm(h))
    n = np.cross([0.0, 0.0, 1.0], h)
    nn = float(np.linalg.norm(n))
    e_vec = ((vn * vn - MU / rn) * r - float(np.dot(r, v)) * v) / MU
    e = float(np.linalg.norm(e_vec))
    energy = vn * vn / 2.0 - MU / rn
    a = -MU / (2.0 * energy) if abs(energy) > 1e-12 else float("inf")
    i = math.degrees(math.acos(max(-1.0, min(1.0, h[2] / hn))))
    raan = argp = nu = 0.0
    if nn > 1e-12:
        raan = math.degrees(math.atan2(n[1], n[0])) % 360.0
        argp = math.degrees(math.acos(max(-1.0, min(1.0, float(np.dot(n, e_vec)) / (nn * e)))))
        if e_vec[2] < 0:
            argp = 360.0 - argp
        nu = math.degrees(math.acos(max(-1.0, min(1.0, float(np.dot(e_vec, r)) / (e * rn)))))
        if float(np.dot(r, v)) < 0:
            nu = 360.0 - nu
    return dict(a_km=float(a / 1000.0) if math.isfinite(a) else None, e=e, i_deg=i,
                raan_deg=raan, argp_deg=argp, nu_deg=nu,
                perigee_km=float((a * (1 - e) - RE) / 1000.0) if math.isfinite(a) else None,
                apogee_km=float((a * (1 + e) - RE) / 1000.0) if math.isfinite(a) else None,
                period_s=float(2 * math.pi * math.sqrt(a ** 3 / MU)) if math.isfinite(a) and a > 0 else None)


def flight_path_angle(r_eci, v_eci) -> float:
    r = np.asarray(r_eci, dtype=float)
    v = np.asarray(v_eci, dtype=float)
    return math.degrees(math.asin(max(-1.0, min(1.0,
                                                  float(np.dot(r, v)) / (np.linalg.norm(r) * np.linalg.norm(v))))))


def bearing_from_to(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


# ---------------------------------------------------------------- 从 config 构建
def vehicle_from_config(cfg: dict) -> Vehicle:
    vc = cfg["vehicle"]
    stages = []
    for s in vc["stages"]:
        phases = []
        for ph in s.get("phases") or []:
            ths = [Thruster(thrust_vac_n=t["thrust_vac_n"], isp_vac_s=t["isp_vac_s"],
                            n=int(t.get("n", 1)),
                            thrust_sl_n=t.get("thrust_sl_n"),
                            isp_sl_s=t.get("isp_sl_s")) for t in ph["thrusters"]]
            phases.append(Phase(thrusters=ths, name=ph.get("name", ""),
                                end_met=ph.get("end_met"), emit_event=bool(ph.get("emit_event", False))))
        if not phases:
            phases = [Phase(thrusters=[Thruster(thrust_vac_n=s["thrust_vac_n"],
                                                isp_vac_s=s["isp_vac_s"],
                                                n=int(s.get("engines", 1)),
                                                thrust_sl_n=s.get("thrust_sl_n"),
                                                isp_sl_s=s.get("isp_sl_s"))])]
        stages.append(Stage(name=s["name"], dry_kg=float(s["dry_kg"]),
                            prop_kg=float(s["prop_kg"]), phases=phases,
                            pitch_end_deg=s.get("pitch_end_deg"),
                            pitch_duration_s=s.get("pitch_duration_s"),
                            seco_met=s.get("seco_met"),
                            sep_met=s.get("sep_met"),
                            seco_mode=s.get("seco_mode"),
                            cda_attached_m2=float(s.get("cda_attached_m2", 0.0))))
    boost = None
    if vc.get("boosters"):
        b = vc["boosters"]
        ths = [Thruster(thrust_vac_n=t["thrust_vac_n"], isp_vac_s=t["isp_vac_s"],
                        n=int(t.get("n", 1)), thrust_sl_n=t.get("thrust_sl_n"),
                        isp_sl_s=t.get("isp_sl_s")) for t in b["thrusters"]]
        boost = Booster(name=b.get("name", "助推器"), count=int(b.get("count", 2)),
                        dry_kg_each=float(b.get("dry_kg_each", 0)),
                        prop_kg_each=float(b.get("prop_kg_each", 0)), thrusters=ths,
                        jettison_met=b.get("jettison_met"))
    f = vc.get("fairing", {})
    return Vehicle(stages=stages, boosters=boost,
                   fairing_mass_kg=float(f.get("mass_kg", 0.0)),
                   fairing_halves=int(f.get("halves", 2)),
                   adapter_kg=float(vc.get("adapter_kg", 0.0)),
                   payload_kg=float(vc.get("payload_kg", 0.0)),
                   debris=vc.get("debris", {}),
                   fairing_cda_m2=float(f.get("cda_m2", 0.0)),
                   cda_ascent_m2=float(vc.get("cda_ascent_m2", 0.0)),
                   ascent_cda_scale=float(vc.get("ascent_cda_scale", 1.0)))


def params_from_config(cfg: dict) -> AscentParams:
    g = cfg.get("guidance", {})
    num = cfg.get("numerics", {})
    t0 = cfg["mission"]["t0_utc"].replace("Z", "+00:00")
    site = cfg["mission"]["launch_site"]
    mode = str(num.get("mode", g.get("numerics", "legacy")))
    # legacy 口径默认沿用 v1 的 guidance.dt（0.02），避免误用 accurate 的大步长
    if mode == "legacy":
        dt_default = num.get("dt_legacy", g.get("dt", 0.02))
    else:
        dt_default = num.get("dt", g.get("dt", 0.02))
    # 抛罩时刻：guidance.t_fairing 优先；兼容只填 vehicle.fairing.sep_met 的配置（v2 修正）
    t_fair = g.get("t_fairing")
    if t_fair is None:
        t_fair = ((cfg.get("vehicle") or {}).get("fairing") or {}).get("sep_met")
    _ref = (cfg.get("mission") or {}).get("reference_orbit") or {}
    return AscentParams(
        lat0=float(site["lat_deg"]), lon0=float(site["lon_deg"]),
        alt0=float(site.get("alt_m_ellipsoid", 0.0)),
        azimuth_deg=float(g.get("azimuth_deg", 180.0)),
        t0_utc=datetime.fromisoformat(t0),
        t_v=float(g.get("t_v", 10.0)), t_p=float(g.get("t_p", 150.0)),
        pitch_power=float(g.get("pitch_power", 1.5)),
        guidance=g.get("mode", "shooting_plane"),
        plane_keeping=bool(g.get("plane_keeping", False)),
        yaw_tau=float(g.get("yaw_tau", 150.0)),
        t_fairing=t_fair,
        fairing_divert_mps=float(g.get("fairing_divert_mps", 4.0)),
        i_target_deg=g.get("i_target_deg"),
        raan_target_deg=g.get("raan_target_deg"),
        yaw2_tau=float(g.get("yaw2_tau", 200.0)),
        target_alt_km=(float(_ref["alt_km"]) if _ref.get("alt_km") else None),
        seco_band_km=float(g.get("seco_band_km", 12.0)),
        seco_e_max=float(g.get("seco_e_max", 0.006)),
        pitch_schedule=(g.get("pitch_schedule") or None),
        numerics=mode,
        dt=float(dt_default),
        dt_legacy=float(num.get("dt_legacy", g.get("dt", 0.02))),
        dt_accurate=float(num.get("dt", g.get("dt", 0.02))),
        sample_dt=float(num.get("sample_dt", g.get("sample_dt", 0.5))),
        debris_dt=float(num.get("debris_dt", g.get("debris_dt", 0.25))),
        debris_numerics=num.get("debris_mode"),
        post_seco_s=float(g.get("post_seco_s", 60.0)),
        flow_scale=float(g.get("flow_scale", 1.0)),
    )


_DEBUG_T = float(os.environ.get("RK_DEBUG_T", "-1"))


def apply_mode(p: AscentParams, mode: str, dt: float | None = None) -> AscentParams:
    """切换数值口径并同步默认步长（v2 新增）。

    同时切 ``p.numerics`` 与 ``p.dt``：legacy 用 v1 的 guidance.dt（0.02），
    accurate 用 numerics.dt（0.2）。``dt`` 显式给出时以它为准。
    """
    p.numerics = mode
    p.dt = float(dt) if dt else (p.dt_legacy if mode == "legacy" else p.dt_accurate)
    if mode == "accurate":
        p.debris_numerics = "accurate"
    return p
