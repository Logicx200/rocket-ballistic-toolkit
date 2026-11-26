# -*- coding: utf-8 -*-
"""应用数据模型：用户输入、数据体检记录、构建结果。无 GUI / 无 numpy 依赖。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

#: 默认运行模块开关（GUI 复选框初值）
DEFAULT_MODULES = {
    "dem": True,       # DEM 瓦片准备（在线补全/本地导入）
    "invert": True,    # 制导/箭型参数反演（自动生成边界）
    "export": True,    # 仿真 + 数据契约（必选）
    "window": True,    # 发射窗口（航警时窗 + 轨道平面/交会窗口）
    "station": True,   # 空间站轨道准备（CZ-2F/载人任务；自动跳过非载人）
    "docking": True,   # 入轨+交会对接方案（CZ-2F/载人任务；自动跳过）
    "return": True,    # 返回舱返回窗口/弹道（需显式启用 return_capsule）
    "sky": True,       # 天空轨迹（详细版 + 参考样式 + 地形判读）
    "appear": True,    # 出现方位地形判读图
    "map": True,       # 全国最大高度角地图
    "mc": False,       # 蒙特卡洛
    "kml": True,       # 三维 KMZ/KML
    "summary": True,   # 通用成果汇总（摘要/关键节点/阶段/清单）
    "qc": True,        # 交付自检
    "report": True,    # 自动技术报告
}

#: 任务类型（v4）：决定模块可用性与额外成果
MISSION_TYPES = ("auto", "standard", "crewed", "return")
MISSION_TYPE_CN = {
    "auto": "自动识别（按箭型 / 返回开关）",
    "standard": "常规发射弹道（无人）",
    "crewed": "载人发射弹道（含空间站交会）",
    "return": "返回舱返回弹道（无需发射场/上升段）",
}

#: 参考轨道来源（v4）
REF_ORBIT_MODES = ("auto", "template", "user", "custom", "tle")
REF_ORBIT_MODE_CN = {
    "auto": "自动推断（按箭型典型轨道 + 发射方位角）",
    "template": "内置轨道模板",
    "user": "我的轨道库",
    "custom": "手动填写（高度/倾角/偏心率/RAAN/LTAN）",
    "tle": "从 TLE 导入",
}


@dataclass
class NotamInput:
    """一条航警（NOTAM）危险区。多边形顺序 [lat, lon]。"""
    id: str = ""
    b_utc: str = ""
    c_utc: str = ""
    q_line: str = ""
    fir: str = ""
    polygon_latlon: List[List[float]] = field(default_factory=list)
    stage_hint: str = ""
    raw_text: str = ""
    verified: str = ""

    def as_config(self) -> Dict[str, Any]:
        return {
            "id": self.id, "b_utc": self.b_utc, "c_utc": self.c_utc,
            "q_line": self.q_line, "fir": self.fir,
            "polygon_latlon": [[float(a), float(b)] for a, b in self.polygon_latlon],
            "stage_hint": self.stage_hint, "verified": self.verified,
            "raw_text": self.raw_text,
        }

    def is_usable(self) -> bool:
        return bool(self.id and self.b_utc and self.c_utc and len(self.polygon_latlon) >= 3)


@dataclass
class ObserverInput:
    """观测点。alt_m 可空（由 DEM/城市库补全）。"""
    id: str = "obs1"
    name: str = ""
    lat_deg: Optional[float] = None
    lon_deg: Optional[float] = None
    alt_m: Optional[float] = None
    note: str = ""

    def as_config(self, alt_m: Optional[float] = None) -> Dict[str, Any]:
        alt = self.alt_m if self.alt_m is not None else alt_m
        return {"id": self.id, "name": self.name, "lat_deg": float(self.lat_deg),
                "lon_deg": float(self.lon_deg), "alt_m": float(alt if alt is not None else 0.0),
                "note": self.note}


@dataclass
class MissionInput:
    """一次任务的全部用户输入。GUI 只需要填这个对象。"""
    name: str = ""
    rocket_id: str = "auto"
    rocket_label: str = ""
    site_id: str = "auto"
    site_label: str = ""
    site_lat: Optional[float] = None
    site_lon: Optional[float] = None
    site_alt: Optional[float] = None
    t0_text: str = ""
    tz_hint: str = "auto"
    serial: str = ""
    payload_name: str = ""
    payload_kg: Optional[float] = None
    orbit_alt_km: Optional[float] = None
    orbit_i_deg: Optional[float] = None
    notams_text: str = ""
    notams: List[NotamInput] = field(default_factory=list)
    notams_mode: str = "auto"           # auto / provided / predict
    observers: List[ObserverInput] = field(default_factory=list)
    mode: str = "accurate"
    modules: Dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_MODULES))
    mc_n: int = 0
    jobs: int = 0
    dem_radius_km: float = 200.0
    vehicle_overrides: Dict[str, Any] = field(default_factory=dict)
    #: 制导参数覆盖（点号路径，如 t_v / t_p / pitch_power / azimuth_deg / i_target_deg；
    #: 留空=由箭型默认+自动反演决定）
    guidance_overrides: Dict[str, Any] = field(default_factory=dict)
    inversion_maxiter: int = 0
    inversion_popsize: int = 0
    notes: str = ""
    #: 发射时序自定义（空 dict = 完全用箭型库默认）；结构见 config.launch_sequence
    launch_sequence: Dict[str, Any] = field(default_factory=dict)

    # ---- v3.4：轨道发射窗口 / CZ-2F 空间站交会 ----
    profile: str = "auto"              # auto / standard / cz2f（兼容 v3；v4 用 mission_type）
    station_enabled: bool = True       # 空间站交会模块（CZ-2F/载人任务生效）
    station_source: str = "auto"       # auto / bundled / online / tle / oem
    station_name: str = "天宫空间站"
    station_norad: int = 48274
    station_tle_text: str = ""         # 用户粘贴的两行 TLE
    station_oem_path: str = ""         # 用户导入的 OEM 文件
    rendezvous_h: float = 6.5          # 期望交会总时长 [h]
    dv_budget_mps: float = 300.0       # 交会 ΔV 预算 [m/s]
    window_tolerance_deg: float = 0.2  # 轨道平面窗口容差 [deg]
    window_search_hours: float = 48.0  # 窗口搜索半宽 [h]
    window_ltan_h: Optional[float] = None   # 太阳同步 LTAN（可选）
    window_raan_deg: Optional[float] = None  # 目标 RAAN（可选）

    # ---- v4：任务类型 / 自定义箭型 / 自定义参考轨道 ----
    mission_type: str = "auto"         # auto / standard / crewed / return
    #: 完整自定义箭型记录（空 dict = 使用内置/用户库匹配）。GUI 编辑器写入。
    custom_rocket: Dict[str, Any] = field(default_factory=dict)
    #: 自定义箭型来源说明："" / "user_db"（已存用户库）/ "session"（仅本次任务）
    custom_rocket_mode: str = ""
    #: 参考轨道来源：auto / template / user / custom / tle
    ref_orbit_mode: str = "auto"
    ref_orbit_name: str = ""
    #: 手填轨道的完整参数：{alt_km, i_deg, e, raan_deg, argp_deg, ltan_h, epoch_utc, type}
    ref_orbit: Dict[str, Any] = field(default_factory=dict)
    ref_orbit_tle: str = ""
    #: 未指定观测点时，是否用发射场作为观测点（保证天空类成果不因留空而丢失）
    auto_observer: bool = True
    #: 起飞时刻的来源策略（v4.1）：
    #: ``auto`` = 用户给了就保留、没给就按交会窗口反推；
    #: ``rendezvous`` = 强制按交会发射窗口反推 T0（覆盖手填，仅载人/交会任务生效）
    t0_mode: str = "auto"

    # ---- v3.5：返回舱返回（空间站轨道 + 返回航警） ----
    return_only: bool = False           # 仅返回任务：不需要发射场/火箭/上升段
    return_enabled: bool = False        # 是否计算返回舱返回
    return_use_notams: bool = False     # 是否把当前航警区当作着陆区
    return_landing_name: str = "四子王旗着陆场"
    return_landing_lat: Optional[float] = 42.27
    return_landing_lon: Optional[float] = 111.70
    return_landing_radius_km: float = 50.0
    #: 留空 = 有返回航警时取 [B−3h, C+1h]，否则 T0±6h（见 engine/scripts/return_capsule.py）
    return_search_start: str = ""
    return_search_end: str = ""
    return_dv_mps: float = 120.0        # 标称制动 ΔV
    return_ld_ratio: float = 0.30       # 升阻比
    return_bank_deg: Optional[float] = None  # None = 自动扫描
    return_auto_dv: bool = True
    return_auto_bank: bool = True
    return_coarse_step_s: float = 300.0
    return_scan_step_s: float = 10.0

    # ------------------------------------------------------------------ #
    def effective_notams(self) -> List[NotamInput]:
        """可用的航警列表（已解析且字段完整）。"""
        return [z for z in self.notams if z.is_usable()]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MissionInput":
        d = dict(d or {})
        notams = [NotamInput(**{k: v for k, v in z.items() if k in NotamInput.__annotations__})
                  for z in d.pop("notams", []) or []]
        obs = [ObserverInput(**{k: v for k, v in o.items() if k in ObserverInput.__annotations__})
               for o in d.pop("observers", []) or []]
        mods = dict(DEFAULT_MODULES)
        mods.update(d.pop("modules", {}) or {})
        mi = cls(**{k: v for k, v in d.items() if k in cls.__annotations__})
        mi.notams, mi.observers, mi.modules = notams, obs, mods
        return mi


@dataclass
class Resolution:
    """数据体检单行：这个字段最后用了什么值、从哪来、多可信。"""
    field: str = ""
    label: str = ""
    value: str = ""
    source: str = "user"     # user / db / infer / default / derived
    confidence: str = "高"    # 高 / 中 / 低
    note: str = ""

    SOURCE_CN = {"user": "用户给定", "db": "内置库", "infer": "自动推断",
                 "default": "默认值", "derived": "计算得出"}

    def source_cn(self) -> str:
        return self.SOURCE_CN.get(self.source, self.source)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def line(self) -> str:
        return "[%s/%s] %s = %s%s" % (self.source_cn(), self.confidence, self.label,
                                      self.value, ("  # " + self.note) if self.note else "")


@dataclass
class BuildResult:
    """MissionInput → config.json 的构建结果。"""
    config: Dict[str, Any] = field(default_factory=dict)
    path: str = ""
    resolutions: List[Resolution] = field(default_factory=list)
    gaps: List[Resolution] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    needs_refine_t0: bool = False
    summary: str = ""

    def lines(self) -> List[str]:
        return [r.line() for r in self.resolutions]


def fmt_num(v: Any, nd: int = 3) -> str:
    """数字格式化（用于体检表显示）。"""
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, (int,)):
        return str(v)
    if isinstance(v, float):
        if v == int(v) and abs(v) < 1e9:
            return str(int(v))
        return ("%." + str(nd) + "f") % v
    return str(v)
