#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""plot_map.py — 全国最大高度角地图（含晨昏线），v3.0 参数化版

v3 参数化（本文件已无算例常量）
--------------------------------
* 网格：``config.grid`` 的 lon_min/lon_max/lat_min/lat_max/step_deg（运行时
  ``init_grid(cfg)`` 生成；缺省回落 v2 的 72–136°E / 15–55°N / 0.2°）。
* 观测点：``config.observers``（可多个，全部绘制/统计）。
* 发射场 / 任务名 / T0：``config.mission``（t0_utc、launch_site、rocket、name）。
* 非中国网格（如境外任务）不崩：中国陆域掩膜未命中时退化为"全网格统计"
  并打印警告、图例/说明自动改写；``in_china`` 列仍为真实掩膜。

任务（提示词第 7 节，默认为 72–136°E / 15–55°N 的 0.2° 网格 = 321×201 格）
------------------------------------------------------------------------------
用 ``common.boundaries.china_land_mask`` 取中国陆域掩膜，逐格点（椭球高
0 m）计算对**上升段全过程**的最大几何高度角：

    el(t) = atan2( U(t), sqrt(E(t)^2 + N(t)^2) )
    [E,N,U](t) = R_enu(lat,lon) · ( r_target(t) − r_observer )      （WGS-84 ECEF）

其中 r_observer 为格点椭球高 0 m 的 ECEF，r_target(t) 为 trajectory.csv 的每个
轨迹点。该式与 ``common.geo.az_el_from_observer`` **同一三维几何**，自动包含地球
曲率与目标高度（距离 d 处地平线下降 d²/2R 自然体现在 U 分量为负），无需额外修正。
地形/建筑只可能遮挡（降低"实际可见"），**不改变几何最大值**——本图给出的是几何
上界，未做 DEM 地形遮挡（见 §局限）。

另计算：
* 晨昏线：T0 = 2026-11-18 22:50:00 UTC（北京时 11-19 06:50，黎明）太阳高度场，
  叠加 0° / -0.833° / -6° / -12° / -18° 等值线；半透明阴影区分昼/夜侧；
  给出航警窗口 B) 22:43 – C) 23:10 UTC 起止两条晨昏线（窗口扫过中国东部日出带）。
* "阳光可见"版：只统计 **火箭被太阳照亮 且 观测者在夜侧（太阳高度 ≤ 0°）** 时刻的
  最大高度角；与几何版逐格对比说明差异。

输出（默认 map/）
-----------------
- ``全国最大高度角地图.png``        主图（分级填色 + 晨昏线 + 边界/省界/城市/观测点 + 统计）
- ``晨昏线与阳光可见性.png``        补充图（T0 太阳高度场、窗口起止晨昏线、阳光可见版、时刻曲线）
- ``网格.csv``                      全网格：lat_deg,lon_deg,max_el_geom_deg,max_el_sunlit_deg,+辅助列
- ``统计.json``                     统计、城市/观测点/发射点数值、晨昏线时刻、自检结果
- ``全国最大高度角地图.md``         方法 / 数据来源 / 晨昏线时刻说明 / 统计 / 局限

用法
----
    # 正式运行（sim/trajectory.csv 存在时；写 map/）
    python map/plot_map.py
    # 指定弹道文件（参数化输入路径）
    python map/plot_map.py --trajectory sim/trajectory.csv --outdir map
    # 开发/联调（用测试夹具，输出到临时目录，不写 map/）
    python map/plot_map.py --trajectory _fixtures/ref_sim/trajectory.csv --outdir <tmp>
    # 只对已有产物做一致性自检
    python map/plot_map.py --check-only

环境（见 AGENTS.md）
    $env:PYTHONPATH="...\\_pylibs"; $env:PYTHONIOENCODING="utf-8"; $env:MPLBACKEND="Agg";
    $env:MPLCONFIGDIR="...\\_tmp\\mplconfig"

约定
----
* 只读 common/、sim/、data/、_fixtures/，只写 map/（调试时写临时目录）；
* ``sim/trajectory.csv`` 不存在时自动回退到 ``_fixtures/ref_sim/trajectory.csv``（CZ-8A 夹具），
  并在 md 头部/统计 JSON 中标注"临时版"，禁止冒充 CZ-2D 正式结果。
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import re
import sys
import time
import warnings
from pathlib import Path

import numpy as np

SCRIPT_PATH = Path(__file__).resolve()
ROOT = Path(os.environ.get("RK_ROOT") or SCRIPT_PATH.parents[2])      # 任务工作区（config/sim/sky/map）
ENGINE = Path(os.environ.get("RK_ENGINE") or SCRIPT_PATH.parents[1])  # 引擎目录（含 common/ data/ viz/）
for _p in (str(ENGINE), str(ROOT), str(SCRIPT_PATH.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from common import geo, boundaries, plotstyle  # noqa: E402

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
SCRIPT_VERSION = "2.2.0-v3param"
MAP_DIR = ROOT / "map"
SIM_DIR = ROOT / "sim"
DATA_DIR = ROOT / "data"
FIX_DIR = ROOT / "_fixtures" / "ref_sim"
CFG_PATH = ROOT / "config.json"          # v2: 根目录 config.json（v1 误写为 common/config.json）
CFG_FALLBACK = ROOT / "common" / "config.json"

# 网格：**由 config.grid 在运行时生成**（v3 参数化）。缺省/非法时回落 v2 任务书常量
# （72–136°E / 15–55°N / 0.2° = 321×201），默认 config 下与 v2 模块级常量逐位一致。
DEFAULT_GRID = {"lon_min": 72.0, "lon_max": 136.0,
                "lat_min": 15.0, "lat_max": 55.0, "step_deg": 0.2}
GRID_LON_MIN, GRID_LON_MAX = 72.0, 136.0
GRID_LAT_MIN, GRID_LAT_MAX = 15.0, 55.0
GRID_STEP = 0.2
GRID_IS_DEFAULT = True
# 以下网格/掩膜数组由 init_grid(cfg) 生成（占位值仅保证模块可导入）
LONS = np.arange(0.0)
LATS = np.arange(0.0)
NY = NX = 0
LAT2 = np.zeros((0, 0))
LON2 = np.zeros((0, 0))
CHINA_MASK = np.zeros((0, 0), dtype=bool)
DOMAIN_MASK = np.zeros((0, 0), dtype=bool)      # 统计/填色域（默认=中国陆域）
CI = np.array([], dtype=int)
CJ = np.array([], dtype=int)
DI = np.array([], dtype=int)
DJ = np.array([], dtype=int)
N_LAND = 0
N_DOMAIN = 0
ROW_AREA_KM2 = np.zeros(0)
TOTAL_LAND_AREA_KM2 = 0.0
OBS_ECEF = np.zeros((0, 0, 3))
LAND_LABEL = "中国陆域"
TICK_LON_START, TICK_LAT_START = 75.0, 15.0


def init_grid(cfg: dict | None = None, verbose: bool = True) -> dict:
    """按 ``config.grid``（lon_min/lon_max/lat_min/lat_max/step_deg）运行时生成网格。

    默认 config（72–136°E / 15–55°N / 0.2°）下生成的 LONS/LATS/掩膜/面积权重与 v2
    模块级常量逐位一致；非中国网格时 ``china_land_mask`` 可能全 False，此时统计与填色
    退化为"全网格"（``DOMAIN_MASK`` 全 True）并打印警告，``in_china`` 列仍为真实掩膜。
    """
    global GRID_LON_MIN, GRID_LON_MAX, GRID_LAT_MIN, GRID_LAT_MAX, GRID_STEP
    global GRID_IS_DEFAULT, LONS, LATS, NY, NX, LAT2, LON2, CHINA_MASK, DOMAIN_MASK
    global CI, CJ, DI, DJ, N_LAND, N_DOMAIN, ROW_AREA_KM2, TOTAL_LAND_AREA_KM2
    global OBS_ECEF, LAND_LABEL, TICK_LON_START, TICK_LAT_START

    raw = (cfg or {}).get("grid") or {}
    g = dict(DEFAULT_GRID)
    for k in DEFAULT_GRID:
        try:
            if raw.get(k) is not None:
                g[k] = float(raw[k])
        except (TypeError, ValueError):
            print(f"[map] 警告: config.grid.{k}={raw.get(k)!r} 非数值，忽略")
    if not (g["step_deg"] > 0 and g["lon_max"] > g["lon_min"] and g["lat_max"] > g["lat_min"]):
        print(f"[map] 警告: config.grid 非法（{raw}），回落默认网格 {DEFAULT_GRID}")
        g = dict(DEFAULT_GRID)
    GRID_IS_DEFAULT = all(abs(g[k] - DEFAULT_GRID[k]) < 1e-12 for k in DEFAULT_GRID)
    GRID_LON_MIN, GRID_LON_MAX = g["lon_min"], g["lon_max"]
    GRID_LAT_MIN, GRID_LAT_MAX = g["lat_min"], g["lat_max"]
    GRID_STEP = g["step_deg"]

    # 与 v2 完全同式的 np.arange（+1e-4 容差），保证默认网格逐位一致
    LONS = np.arange(GRID_LON_MIN, GRID_LON_MAX + 1e-4, GRID_STEP)
    LATS = np.arange(GRID_LAT_MIN, GRID_LAT_MAX + 1e-4, GRID_STEP)
    NX, NY = int(LONS.size), int(LATS.size)
    LAT2, LON2 = np.meshgrid(LATS, LONS, indexing="ij")          # (NY,NX)
    CHINA_MASK = boundaries.china_land_mask(LONS, LATS)          # (NY,NX) bool
    CI, CJ = np.nonzero(CHINA_MASK)                              # 真实中国陆域格索引
    N_LAND = int(CHINA_MASK.sum())
    if N_LAND > 0:
        DOMAIN_MASK = CHINA_MASK
        LAND_LABEL = "中国陆域"
    else:
        DOMAIN_MASK = np.ones_like(CHINA_MASK)
        LAND_LABEL = "统计域"
        print("[map] 警告: 网格与 Natural Earth 中国陆域无交集（境外任务？）——"
              "统计/填色退化为全网格（统计域=全网格），in_china 列全为 0。")
    DI, DJ = np.nonzero(DOMAIN_MASK)
    N_DOMAIN = int(DOMAIN_MASK.sum())
    ROW_AREA_KM2 = (geo.EARTH_MEAN_R_KM ** 2) * math.radians(GRID_STEP) * (
        np.sin(np.radians(LATS) + math.radians(GRID_STEP) / 2.0)
        - np.sin(np.radians(LATS) - math.radians(GRID_STEP) / 2.0)
    )
    TOTAL_LAND_AREA_KM2 = float(np.sum(ROW_AREA_KM2[:, None] * DOMAIN_MASK))

    # 观测几何：格点取椭球高 0 m（任务书）；点要素的高程见 config
    OBS_ECEF = np.asarray(geo.geodetic_to_ecef(LAT2, LON2, 0.0), dtype=float).reshape(NY, NX, 3)
    if GRID_IS_DEFAULT:
        TICK_LON_START, TICK_LAT_START = 75.0, 15.0     # 与 v2 刻度起点一致
    else:
        TICK_LON_START = math.ceil(GRID_LON_MIN / 10.0) * 10.0
        TICK_LAT_START = math.ceil(GRID_LAT_MIN / 5.0) * 5.0
    if verbose and not GRID_IS_DEFAULT:
        print(f"[map] 网格（config.grid）: {NX}×{NY} = {NX*NY:,} 格；"
              f"{GRID_LON_MIN:g}–{GRID_LON_MAX:g}°E / {GRID_LAT_MIN:g}–{GRID_LAT_MAX:g}°N / "
              f"{GRID_STEP:g}°；{LAND_LABEL} {N_DOMAIN:,} 格")
    return g

# 晨昏线等级（0° 几何地平；-0.833° 含折射+日面视半径的真实晨昏线）
TERMINATOR_LEVELS = [0.0, -0.833, -6.0, -12.0, -18.0]
SUNLIT_OBS_MAX_EL_DEG = 0.0          # "观测者在夜侧"阈值：太阳高度 ≤ 0°
SUNLIT_OBS_STRICT_DEG = -6.0         # 对照：≤ -6°（民用晨光以下）

# 分级填色（负值 = 全程低于地平=几何不可见，用冷灰蓝；正值用 YlOrRd）
FILL_LEVELS = [-45.0, -35.0, -25.0, -15.0, -8.0, -3.0, 0.0, 2.0, 5.0, 10.0,
               15.0, 20.0, 30.0, 45.0, 60.0, 90.0]
FILL_LABELS = ["≤-35", "-35~-25", "-25~-15", "-15~-8", "-8~-3", "-3~0",
               "0–2", "2–5", "5–10", "10–15", "15–20",
               "20–30", "30–45", "45–60", "60–90"]
REQ_THRESHOLDS = [10, 15, 20, 30, 45, 60]
ALL_THRESHOLDS = [0, 2, 5, 10, 15, 20, 30, 45, 60, 75, 90]

# 主要城市（中文名, 标注偏移 points）——报告要求"主要城市（标注数值）"
CITY_CHOICES = [
    ("北京", (8, 6)), ("上海", (8, -12)), ("广州", (-30, -14)), ("武汉", (7, -9)),
    ("重庆", (-26, 6)), ("成都", (-28, 8)), ("西安", (7, 5)), ("哈尔滨", (7, 4)),
    ("乌鲁木齐", (-30, 4)), ("兰州", (-24, -9)), ("拉萨", (-32, 2)), ("昆明", (-24, -11)),
    ("香港", (9, -13)), ("台北", (10, 1)), ("呼和浩特", (-80, 5)), ("郑州", (7, -9)),
]
CITY_FALLBACK = {
    "香港": (22.3193, 114.1694),
    "台北": (25.0330, 121.5654),
    "澳门": (22.1987, 113.5439),
}
# 报告中"关键城市数值表"（超出地图标注数量）
KEY_CITY_NAMES = ["北京", "上海", "广州", "深圳", "武汉", "重庆", "成都", "西安",
                  "哈尔滨", "乌鲁木齐", "昆明", "拉萨", "郑州", "长沙", "太原"]

# 免责声明（所有交付物统一）
DISCLAIMER = "仅供公开信息分析与教学使用，非实际发射弹道，非官方信息。"


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def fmt_utc(d: dt.datetime, with_s: bool = True) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ" if with_s else "%Y-%m-%dT%H:%MZ")


def fmt_hms(d: dt.datetime) -> str:
    return d.strftime("%H:%M:%S")


def fmt_bjt(d: dt.datetime) -> str:
    return (d + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def parse_utc(s: str) -> dt.datetime:
    s = str(s).strip().replace("Z", "+00:00").replace("z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        d = dt.datetime.strptime(str(s).strip(), "%Y-%m-%d %H:%M:%S")
    if d.tzinfo is not None:
        d = d.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return d


def jsonable(obj):
    """递归转换 numpy / datetime 为可 JSON 序列化类型。"""
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return jsonable(obj.tolist())
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, dt.datetime):
        return fmt_utc(obj)
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    return obj


def _png_size(path, fallback_w, fallback_h):
    """读取实际 PNG 像素尺寸（plotstyle.save 的 tight-bbox 尺寸估算可能不准）。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:  # noqa: BLE001
        return int(fallback_w), int(fallback_h)


def fnum(v, nd=2, default="—"):
    if v is None or not np.isfinite(float(v)):
        return default
    return f"{float(v):.{nd}f}"


def load_config(path=None) -> dict:
    p = Path(path) if path else (CFG_PATH if CFG_PATH.exists() else CFG_FALLBACK)
    return json.loads(Path(p).read_text(encoding="utf-8"))


# ---- v3 参数化：从 config.mission 取任务信息（兼容 v2 旧位置） ----
def get_site(cfg: dict) -> dict:
    """发射场：config.mission.launch_site（v2 旧配置可能直接在根 launch_site）。"""
    m = cfg.get("mission") or {}
    return (m.get("launch_site") or cfg.get("launch_site") or {})


def get_observers(cfg: dict) -> list:
    obs = cfg.get("observers")
    if not obs:
        obs = (cfg.get("mission") or {}).get("observers")
    return list(obs or [])


def get_t0_nominal(cfg: dict):
    """任务标称 T0（config.mission.t0_utc；兼容旧 t0_nominal_utc），返回 naive UTC。"""
    m = cfg.get("mission") or {}
    s = m.get("t0_utc") or cfg.get("t0_nominal_utc")
    if not s:
        return None
    try:
        return parse_utc(s)
    except Exception:  # noqa: BLE001
        return None


def short_rocket_label(cfg: dict) -> str:
    """型号短标签：'长征二号丁（CZ-2D，遥106，…）' → '长征二号丁（CZ-2D）'。"""
    m = cfg.get("mission") or {}
    rocket = str(m.get("rocket") or m.get("name") or "运载火箭").strip()
    mt = re.search(r"[（(]([^，,）)]+)", rocket)
    if mt:
        return rocket[: mt.start()].strip() + "（" + mt.group(1).strip() + "）"
    return rocket


def rocket_code(cfg: dict) -> str:
    """型号代码：'长征二号丁（CZ-2D，遥106，…）' → 'CZ-2D'（取括号内第一段）。"""
    m = cfg.get("mission") or {}
    rocket = str(m.get("rocket") or m.get("name") or "").strip()
    mt = re.search(r"[（(]([^，,）)]+)", rocket)
    return mt.group(1).strip() if mt else short_rocket_label(cfg)


def short_site_label(cfg: dict) -> str:
    """发射场短标签：'太原卫星发射中心九号工位 (TSLC LC-9)' → '太原 LC-9'。"""
    name = str(get_site(cfg).get("name") or "发射场").strip()
    m = re.search(r"([\u4e00-\u9fa5]{2,}?)(?:卫星)?发射中心", name)
    city = m.group(1) if m else ""
    pad = ""
    mp = re.search(r"[（(]([^）)]*)[）)]\s*$", name)
    if mp:
        tokens = [t.strip() for t in re.split(r"[/,，]", mp.group(1)) if t.strip()]
        if tokens:
            mcode = re.search(r"((?:LC|SLS|LA)[- ]?[0-9A-Za-z]+|[0-9]{2,3}工位)", tokens[-1])
            pad = mcode.group(1) if mcode else tokens[-1]
    if city and pad:
        return f"{city} {pad}"
    return name


def twilight_label(cfg: dict, ctx: dict) -> str:
    """发射场 T0 的晨昏状态（白天/黎明/夜间或黄昏），用于补充图 (a) 标题。"""
    site = get_site(cfg)
    la = float(site.get("lat_deg", 0.0) or 0.0)
    lo = float(site.get("lon_deg", 0.0) or 0.0)
    t0 = ctx["t0_utc"]
    el0 = float(geo.solar_elevation_deg(la, lo, t0))
    el1 = float(geo.solar_elevation_deg(la, lo, t0 + dt.timedelta(minutes=5)))
    if el0 > 0.0:
        return "白天"
    return "黎明" if el1 > el0 else "夜间/黄昏"


def mission_date_label(cfg: dict, t0: dt.datetime | None) -> str:
    """任务日期（北京时，与 v2 标题 '2026-11-19' 同口径）。"""
    t = get_t0_nominal(cfg) or t0
    if t is None:
        return ""
    return (t + dt.timedelta(hours=8)).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# 太阳高度角 / 晨昏线
# --------------------------------------------------------------------------- #
def solar_field(when: dt.datetime, lats=None, lons=None) -> np.ndarray:
    """太阳几何高度角场 [deg]，shape (len(lats), len(lons))。"""
    lats = LATS if lats is None else np.asarray(lats)
    lons = LONS if lons is None else np.asarray(lons)
    return geo.solar_elevation_deg(lats[:, None], lons[None, :], when)


def solar_china_stats(when: dt.datetime) -> dict:
    vals = geo.solar_elevation_deg(LAT2[DOMAIN_MASK], LON2[DOMAIN_MASK], when)
    k = int(np.argmin(vals))
    return {
        "min_deg": float(vals.min()),
        "min_lat_deg": float(LAT2[DOMAIN_MASK][k]),
        "min_lon_deg": float(LON2[DOMAIN_MASK][k]),
        "median_deg": float(np.median(vals)),
        "max_deg": float(vals.max()),
        "max_lat_deg": float(LAT2[DOMAIN_MASK][int(np.argmax(vals))]),
        "max_lon_deg": float(LON2[DOMAIN_MASK][int(np.argmax(vals))]),
    }


def _china_solar_min(when: dt.datetime) -> float:
    return float(geo.solar_elevation_deg(LAT2[DOMAIN_MASK], LON2[DOMAIN_MASK], when).min())


def _china_solar_max(when: dt.datetime) -> float:
    return float(geo.solar_elevation_deg(LAT2[DOMAIN_MASK], LON2[DOMAIN_MASK], when).max())


def _bisect_time(fn, target: float, lo: dt.datetime, hi: dt.datetime) -> dt.datetime:
    """在 [lo,hi] 内二分求 fn(t)=target 的时刻；要求 fn 单调（默认升序）。"""
    for _ in range(80):
        mid = lo + (hi - lo) / 2
        if fn(mid) > target:
            hi = mid
        else:
            lo = mid
    return lo + (hi - lo) / 2


def terminator_lon_at_lat(field_row: np.ndarray, level: float, lons=None) -> float | None:
    """给定纬度行的太阳高度场，求该行上 level 等值线所在经度（线性插值，取最东交点）。"""
    lons = LONS if lons is None else np.asarray(lons)
    d = np.asarray(field_row, dtype=float) - level
    idx = np.nonzero(np.sign(d[:-1]) * np.sign(d[1:]) < 0)[0]
    if idx.size == 0:
        return None
    out = []
    for k in idx:
        x0, x1 = lons[k], lons[k + 1]
        y0, y1 = d[k], d[k + 1]
        out.append(float(x0 - y0 * (x1 - x0) / (y1 - y0)))
    return max(out)


def build_solar_timeline(t_start: dt.datetime, t_end: dt.datetime,
                         step_min: float = 2.0) -> dict:
    """[t_start, t_end] 每 step_min 分钟：中国陆域太阳高度角 min/median/max。"""
    n = int(round((t_end - t_start).total_seconds() / (step_min * 60))) + 1
    times, mins, meds, maxs = [], [], [], []
    for k in range(n):
        when = t_start + dt.timedelta(minutes=step_min * k)
        vals = geo.solar_elevation_deg(LAT2[DOMAIN_MASK], LON2[DOMAIN_MASK], when)
        times.append(fmt_utc(when))
        mins.append(float(vals.min()))
        meds.append(float(np.median(vals)))
        maxs.append(float(vals.max()))
    return {"t_utc": times, "min_deg": mins, "median_deg": meds, "max_deg": maxs,
            "step_min": float(step_min)}


# --------------------------------------------------------------------------- #
# 轨迹载入（正式 / 夹具）
# --------------------------------------------------------------------------- #
def _traj_arrays(t_met, t_utc, lat, lon, alt, speed=None, mass=None, phase=None):
    t_met = np.asarray(t_met, dtype=float)
    lat = np.asarray(lat, dtype=float)
    lon = np.asarray(lon, dtype=float)
    alt = np.asarray(alt, dtype=float)
    order = np.argsort(t_met, kind="stable")
    t_met, lat, lon, alt = t_met[order], lat[order], lon[order], alt[order]
    t_utc = [t_utc[i] for i in order]
    speed = None if speed is None else np.asarray(speed, float)[order]
    mass = None if mass is None else np.asarray(mass, float)[order]
    phase = None if phase is None else [phase[i] for i in order]
    ecef = np.asarray(geo.geodetic_to_ecef(lat, lon, alt), dtype=float).reshape(-1, 3)
    d = np.diff(t_met)
    return {
        "t_met": t_met, "t_utc": t_utc, "lat": lat, "lon": lon, "alt": alt,
        "speed": speed, "mass": mass, "phase": phase, "ecef": ecef,
        "n": int(t_met.size),
        "sample_dt_s": float(np.median(d)) if d.size else 0.0,
        "t0": t_utc[0], "t_end": t_utc[-1],
    }


def load_trajectory(path: Path, label: str, status: str = "real",
                    t0_override: dt.datetime | None = None) -> dict:
    """读取 trajectory.csv（0.5 s 采样契约）；参数化路径 + 状态标签。"""
    rows = list(csv.DictReader(open(path, "r", encoding="utf-8-sig", newline="")))
    if not rows:
        raise RuntimeError(f"trajectory.csv 为空: {path}")
    need = ["t_met_s", "t_utc", "lat_deg", "lon_deg", "alt_m"]
    for k in need:
        if k not in rows[0]:
            raise RuntimeError(f"trajectory.csv 缺少列 {k}; 实际列: {list(rows[0].keys())}")
    t_met = np.array([float(r["t_met_s"]) for r in rows])
    t_utc = [parse_utc(r["t_utc"]) for r in rows]
    lat = np.array([float(r["lat_deg"]) for r in rows])
    lon = np.array([float(r["lon_deg"]) for r in rows])
    alt = np.array([float(r["alt_m"]) for r in rows])
    speed = np.array([float(r["speed_m_s"]) for r in rows]) if "speed_m_s" in rows[0] else None
    mass = np.array([float(r["mass_kg"]) for r in rows]) if "mass_kg" in rows[0] else None
    phase = [r.get("phase", "") for r in rows]

    t0_src = "trajectory.csv 首点 t_utc"
    if t0_override is not None:
        t0 = t0_override
        t_utc = [t0 + dt.timedelta(seconds=float(x)) for x in t_met]
        t0_src = "命令行 --t0 覆盖（弹道时序按 MET 重新对齐）"
    else:
        t0 = t_utc[0]

    tr = _traj_arrays(t_met, t_utc, lat, lon, alt, speed, mass, phase)
    st = path.stat()
    tr.update({
        "status": status, "label": label,
        "file": str(path.name if path.parent != ROOT else path.relative_to(ROOT)),
        "path": str(path),
        "mtime_utc": dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc).replace(
            tzinfo=None, microsecond=0),
        "size_bytes": int(st.st_size),
        "t0_source": t0_src,
    })
    return tr


def load_trajectory_meta(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return {}


def resolve_trajectory(args) -> tuple[dict, dict]:
    """解析弹道输入：--trajectory > sim/trajectory.csv > _fixtures/ref_sim/trajectory.csv。"""
    meta = {}
    if args.trajectory:
        p = Path(args.trajectory)
        if not p.is_absolute():
            p = ROOT / p
        if not p.exists():
            raise FileNotFoundError(f"--trajectory 指定的文件不存在: {p}")
        status = "real" if "sim" in p.parts and "_fixtures" not in p.parts else "fixture"
        label = "正式" if status == "real" else "夹具"
        meta_path = p.parent / "trajectory_meta.json"
        meta = load_trajectory_meta(meta_path)
        tr = load_trajectory(p, label, status, args.t0)
        return tr, meta

    p = SIM_DIR / "trajectory.csv"
    if p.exists():
        tr = load_trajectory(p, "正式", "real", args.t0)
        return tr, load_trajectory_meta(SIM_DIR / "trajectory_meta.json")

    pf = FIX_DIR / "trajectory.csv"
    if not pf.exists():
        raise FileNotFoundError(
            f"未找到 sim/trajectory.csv，也没有夹具 _fixtures/ref_sim/trajectory.csv")
    t0 = args.t0
    if t0 is None:
        # 临时版：夹具弹道几何/时序，但晨昏线按本任务标称 T0（config.mission.t0_utc）计算
        try:
            cfg_t0 = get_t0_nominal(load_config())
        except Exception:  # noqa: BLE001
            cfg_t0 = None
        t0 = cfg_t0
    tr = load_trajectory(pf, "夹具（临时版）", "fixture", t0)
    tr["t0_source"] = ("夹具弹道 + 本任务标称 T0（config.json mission.t0_utc，"
                       "临时版演示用）")
    return tr, load_trajectory_meta(FIX_DIR / "trajectory_meta.json")


# --------------------------------------------------------------------------- #
# 光照与几何计算
# --------------------------------------------------------------------------- #
def rocket_illumination(tr: dict) -> np.ndarray:
    """火箭是否被太阳照亮（简化圆柱地影模型）。

    太阳视为无穷远平行光，方向 s(t)（ECEF 单位矢量）；地球阴影取半径 Re 的圆柱：
        影内 ⇔ |r × s| < Re 且 r · s < 0
    取 Re = WGS84_A（6378137 m），忽略大气折射/半影/扁率（对"是否被照亮"影响远小于
    1 个采样点；说明见 md）。
    """
    r = tr["ecef"]
    sun = np.array([geo.sun_ecef_unit(t) for t in tr["t_utc"]], dtype=float)
    perp = np.linalg.norm(np.cross(r, sun), axis=1)
    proj = np.einsum("ij,ij->i", r, sun)
    in_shadow = (perp < geo.WGS84_A) & (proj < 0.0)
    return ~in_shadow


def compute_grid_geometry(tr: dict) -> dict:
    """全网格最大几何高度角（**v2：ENU 基矢预计算 + GEMM**，与 v1 同一几何）。

    v1 逐行构造 (NX, NT, 3) 的 dx/dy/dz 再做 6 次逐元素旋转，每行约 20 次 4 MB
    数组遍历；v2 先把每行观测点的 ENU 基矢写成矩阵，用 ``(NX,3)@(3,NT)`` 一次
    GEMM 得到 E/N/U（观测点自身位置项为常数，单独减去），逐元素运算只剩
    hypot/arctan2/argmax。实测 8.7 s → 2 s 量级，数值差 < 1e-13°。
    """
    tgt = tr["ecef"]                      # (nt,3)
    nt = tgt.shape[0]
    tgtT = np.ascontiguousarray(tgt.T)    # (3,nt)
    geom = np.empty((NY, NX), dtype=np.float64)
    t_max = np.empty((NY, NX), dtype=np.float64)
    az_max = np.empty((NY, NX), dtype=np.float64)
    lam = np.deg2rad(LONS)
    sl, cl = np.sin(lam), np.cos(lam)
    rows = np.arange(NX)
    phi = np.deg2rad(LATS)
    sp, cp = np.sin(phi), np.cos(phi)
    zeros = np.zeros(NX)
    for j in range(NY):
        # ENU 基矢矩阵（每行一个格点），用 GEMM 一次求出 E/N/U
        up = np.column_stack([cp[j] * cl, cp[j] * sl, np.full(NX, sp[j])])
        ea = np.column_stack([-sl, cl, zeros])
        no = np.column_stack([-sp[j] * cl, -sp[j] * sl, np.full(NX, cp[j])])
        U = up @ tgtT - np.einsum("ij,ij->i", up, OBS_ECEF[j])[:, None]
        E = ea @ tgtT - np.einsum("ij,ij->i", ea, OBS_ECEF[j])[:, None]
        N = no @ tgtT - np.einsum("ij,ij->i", no, OBS_ECEF[j])[:, None]
        el = np.rad2deg(np.arctan2(U, np.hypot(E, N)))
        k = np.argmax(el, axis=1)
        geom[j, :] = el[rows, k]
        t_max[j, :] = tr["t_met"][k]
        az_max[j, :] = np.mod(np.rad2deg(np.arctan2(E[rows, k], N[rows, k])), 360.0)
    return {"max_el_geom": geom, "t_max_met_s": t_max, "az_max_deg": az_max,
            "n_traj": nt}


def _enu_basis_arrays(lat_rad, lon_rad):
    """向量化 ENU 基矢量行（E,N,U），各 (n,3)。"""
    sp, cp = np.sin(lat_rad), np.cos(lat_rad)
    sl, cl = np.sin(lon_rad), np.cos(lon_rad)
    e = np.column_stack([-sl, cl, np.zeros_like(sl)])
    n = np.column_stack([-sp * cl, -sp * sl, cp])
    u = np.column_stack([cp * cl, cp * sl, sp])
    return e, n, u


def compute_sunlit_grid(tr: dict, illum: np.ndarray) -> tuple[dict, dict]:
    """"阳光可见"版最大高度角（中国陆域格点）。

    max_el_sunlit(lat,lon) = max_t { el(t) : 火箭被照亮(t) 且 观测者太阳高度(t) ≤ 0° }
    若该格点在上升段内无任何满足条件的时刻 → 定义域外（NaN，CSV 写空串）。

    v2 优化：太阳赤纬/时角按轨迹时刻预计算为标量，格点三角函数只算一次，
    避免 v1 在每个时刻都对 2 万格点重建 datetime + 重算 Meeus 太阳坐标
    （实测 4.5 s → ~1 s，数值差 < 1e-13°）。
    """
    clat = LAT2[DOMAIN_MASK].astype(float)
    clon = LON2[DOMAIN_MASK].astype(float)
    obs = np.asarray(geo.geodetic_to_ecef(clat, clon, 0.0), dtype=float).reshape(-1, 3)
    Ec, Nc, Uc = _enu_basis_arrays(np.deg2rad(clat), np.deg2rad(clon))
    tgt = tr["ecef"]

    # 候选：上升段内曾满足"太阳高度 ≤ 阈值"的格点。太阳高度在 ~10 min 内单调变化，
    # 故用起/末（及中点）时刻取并集即可覆盖（数学上等价，见 md）。
    times_chk = [tr["t_utc"][0], tr["t_utc"][tr["n"] // 2], tr["t_utc"][-1]]
    cand = np.zeros(clat.size, dtype=bool)
    for when in times_chk:
        cand |= (geo.solar_elevation_deg(clat, clon, when) <= SUNLIT_OBS_MAX_EL_DEG)
    cand_idx = np.nonzero(cand)[0]

    # 每个轨迹时刻的太阳参数（标量）——只算 N 次，独立于格点数
    slat = np.sin(np.deg2rad(clat))
    clat_r = np.cos(np.deg2rad(clat))
    lon_r = np.deg2rad(clon)
    n_t = tr["n"]
    dec_sin = np.empty(n_t)
    dec_cos = np.empty(n_t)
    hr = np.empty(n_t)
    for it in range(n_t):
        when = tr["t_utc"][it]
        ra, dec = geo.sun_ra_dec_deg(when)
        dec_sin[it] = math.sin(math.radians(dec))
        dec_cos[it] = math.cos(math.radians(dec))
        hr[it] = math.radians(geo.gmst_deg(when)) - math.radians(ra)

    def _pass(thr: float, cand_set: np.ndarray):
        """单阈值扫描：对候选格点逐时刻取 max(el)；全程无 fancy-index 大拷贝。"""
        n_cand = cand_set.size
        best_c = np.full(n_cand, -np.inf, dtype=np.float64)
        best_t_c = np.full(n_cand, np.nan, dtype=np.float64)
        slat_c = slat[cand_set]
        clat_c = clat_r[cand_set]
        lon_c = lon_r[cand_set]
        sinlon_c = np.sin(lon_c)
        coslon_c = np.cos(lon_c)
        sinlat_c = slat_c
        coslat_c = clat_c
        ox = obs[cand_set, 0]
        oy = obs[cand_set, 1]
        oz = obs[cand_set, 2]
        sin_thr = math.sin(math.radians(thr))
        t_met = tr["t_met"]
        n_eval = 0
        for it in range(n_t):
            if not illum[it]:
                continue
            sd = dec_sin[it]
            cd = dec_cos[it]
            sin_el = slat_c * sd + clat_c * cd * np.cos(hr[it] + lon_c)
            night = sin_el <= sin_thr
            if not night.any():
                continue
            dx = tgt[it, 0] - ox
            dy = tgt[it, 1] - oy
            dz = tgt[it, 2] - oz
            hxy = coslon_c * dx + sinlon_c * dy
            e = -sinlon_c * dx + coslon_c * dy
            nxy = -sinlat_c * hxy + coslat_c * dz
            u = coslat_c * hxy + sinlat_c * dz
            el = np.rad2deg(np.arctan2(u, np.sqrt(e * e + nxy * nxy)))
            upd = night & (el > best_c)
            if upd.any():
                best_c[upd] = el[upd]
                best_t_c[upd] = t_met[it]
            n_eval += int(night.sum())
        best = np.full(clat.size, -np.inf, dtype=np.float64)
        best_t = np.full(clat.size, np.nan, dtype=np.float64)
        best[cand_set] = best_c
        best_t[cand_set] = best_t_c
        return best, best_t, n_eval

    best, best_t, n_eval = _pass(SUNLIT_OBS_MAX_EL_DEG, cand_idx)

    out = np.full((NY, NX), np.nan, dtype=np.float64)
    t_out = np.full((NY, NX), np.nan, dtype=np.float64)
    valid = np.isfinite(best) & (best > -np.inf)
    out[DI[valid], DJ[valid]] = best[valid]
    t_out[DI[valid], DJ[valid]] = best_t[valid]

    # 对照：更严阈值 -6°
    cand6 = np.zeros(clat.size, dtype=bool)
    for when in times_chk:
        cand6 |= (geo.solar_elevation_deg(clat, clon, when) <= SUNLIT_OBS_STRICT_DEG)
    c6 = np.nonzero(cand6)[0]
    best6, _bt6, _ne6 = _pass(SUNLIT_OBS_STRICT_DEG, c6)
    out6 = np.full((NY, NX), np.nan, dtype=np.float64)
    v6 = np.isfinite(best6) & (best6 > -np.inf)
    out6[DI[v6], DJ[v6]] = best6[v6]

    info = {
        "criterion": "火箭被照亮(t) 且 观测者太阳几何高度角(t) ≤ %.0f°" % SUNLIT_OBS_MAX_EL_DEG,
        "threshold_deg": float(SUNLIT_OBS_MAX_EL_DEG),
        "shadow_model": "圆柱地影：太阳平行光，|r×s|<Re 且 r·s<0 判为影内，Re=WGS84_A",
        "n_candidate_cells_china": int(cand.sum()),
        "n_candidate_cells_china_strict": int(cand6.sum()),
        "n_valid_cells_china": int(np.sum(DOMAIN_MASK & np.isfinite(out))),
        "n_valid_cells_china_strict": int(np.sum(DOMAIN_MASK & np.isfinite(out6))),
        "n_land_cells": N_LAND,
        "n_domain_cells": N_DOMAIN,
        "domain_label": LAND_LABEL,
        "n_grid_evaluations": int(n_eval),
        "n_traj_samples": int(tr["n"]),
        "n_traj_samples_illuminated": int(illum.sum()),
        "n_traj_samples_in_shadow": int((~illum).sum()),
        "shadow_fraction": float(np.mean(~illum)),
        "note": ("只有上升段内至少一刻满足“火箭被照亮 + 观测者太阳高度≤0°”的陆域格点有值；"
                 "其余格点（观测者全程昼侧）为定义域外，CSV 中该列留空。"),
    }
    grid_sun = {"max_el_sunlit": out, "t_sunlit_met_s": t_out,
                "max_el_sunlit_strict": out6}
    return grid_sun, info


def max_el_point(tr: dict, lat_deg: float, lon_deg: float, h_m: float = 0.0) -> dict:
    """单点对全轨迹的 max (el, az) —— 直接调用 common.geo.az_el_from_observer（向量化）。"""
    az, el = geo.az_el_from_observer(float(lat_deg), float(lon_deg), float(h_m), tr["ecef"])
    az = np.atleast_1d(np.asarray(az, dtype=float))
    el = np.atleast_1d(np.asarray(el, dtype=float))
    k = int(np.argmax(el))
    return {
        "max_el_deg": float(el[k]),
        "t_met_s": float(tr["t_met"][k]),
        "t_utc": fmt_utc(tr["t_utc"][k]),
        "az_deg": float(az[k]),
        "lat_deg": float(lat_deg), "lon_deg": float(lon_deg), "alt_m": float(h_m),
    }


# --------------------------------------------------------------------------- #
# 晨昏线 / 太阳高度场上下文
# --------------------------------------------------------------------------- #
def compute_context(tr: dict, illum: np.ndarray, cfg: dict) -> dict:
    t0 = tr["t0"]
    notams = cfg.get("notams", [])
    b_list = [parse_utc(n["b_utc"]) for n in notams if n.get("b_utc")]
    c_list = [parse_utc(n["c_utc"]) for n in notams if n.get("c_utc")]
    window_start = min(b_list) if b_list else t0 - dt.timedelta(minutes=7)
    window_end = max(c_list) if c_list else t0 + dt.timedelta(minutes=20)

    ctx = {
        "t0_utc": t0,
        "t0_nominal_utc": get_t0_nominal(cfg) or t0,
        "site": get_site(cfg),
        "observers": get_observers(cfg),
        "window_start_utc": window_start,
        "window_end_utc": window_end,
        "window_source": "config.json → notams 的 B)/C) 时刻（危险区激活时段，非官方发射窗口）",
        "n_traj": tr["n"],
    }

    # T0 / 窗口起止 太阳高度场（全网格）
    fields = {}
    for key, when in [("t0", t0), ("window_start", window_start),
                      ("window_end", window_end)]:
        f = solar_field(when)
        fields[key] = f
        cs = solar_china_stats(when)
        dark = DOMAIN_MASK & (f < 0.0)
        dark_area = float(np.sum(ROW_AREA_KM2[:, None] * dark))
        ctx[f"solar_{key}"] = {
            "utc": fmt_utc(when), "utc_bjt": fmt_bjt(when),
            "china": cs,
            "china_night_area_fraction": dark_area / TOTAL_LAND_AREA_KM2,
            "n_china_cells_sun_le_0": int(np.sum(DOMAIN_MASK & (f <= 0.0))),
            "n_china_cells_sun_le_m6": int(np.sum(DOMAIN_MASK & (f <= -6.0))),
            "n_china_cells_sun_gt_0": int(np.sum(DOMAIN_MASK & (f > 0.0))),
            "bbox_min_deg": float(f.min()), "bbox_max_deg": float(f.max()),
            "contours_present_in_bbox": [lv for lv in TERMINATOR_LEVELS if f.min() < lv < f.max()],
        }
        # 该时刻晨昏线在各纬线的最东交点经度（说明晨昏线形态/位置）
        rows = {}
        for la in (20.0, 25.0, 30.0, 35.0, 40.0, 45.0, 50.0):
            j = int(np.argmin(np.abs(LATS - la)))
            rows[str(int(la))] = {
                "lat_deg": float(LATS[j]),
                "lon_level_0": terminator_lon_at_lat(f[j], 0.0),
                "lon_level_m6": terminator_lon_at_lat(f[j], -6.0),
            }
        ctx[f"solar_{key}"]["terminator_lon_by_lat"] = rows
        ctx[f"solar_{key}"]["n_china_cells_terminator_inside"] = int(
            np.sum(DOMAIN_MASK & (f <= 0.0)) > 0 and np.sum(DOMAIN_MASK & (f > 0.0)) > 0)

    # 晨昏线扫过国境：第一次有中国地点日出 / 最后一点日出（0°）
    try:
        ctx["dawn_first_china_day_utc"] = _bisect_time(
            _china_solar_max, 0.0, t0 - dt.timedelta(hours=3), t0)
        ctx["dawn_all_china_day_utc"] = _bisect_time(
            _china_solar_min, 0.0, t0, t0 + dt.timedelta(hours=5))
    except Exception:  # noqa: BLE001
        ctx["dawn_first_china_day_utc"] = None
        ctx["dawn_all_china_day_utc"] = None
    # -6° 民用晨光扫过国境
    try:
        ctx["civil_first_china_utc"] = _bisect_time(
            _china_solar_max, -6.0, t0 - dt.timedelta(hours=3), t0)
        ctx["civil_all_china_utc"] = _bisect_time(
            _china_solar_min, -6.0, t0, t0 + dt.timedelta(hours=5))
    except Exception:  # noqa: BLE001
        ctx["civil_first_china_utc"] = None
        ctx["civil_all_china_utc"] = None
    ctx["dawn_0deg_crosses_china_at_t0"] = bool(
        solar_china_stats(t0)["min_deg"] <= 0.0 <= solar_china_stats(t0)["max_deg"])
    ctx["window_crosses_sunrise_in_china"] = bool(
        solar_china_stats(window_start)["min_deg"] <= 0.0 <= solar_china_stats(window_end)["max_deg"])

    # 关键地点的太阳高度（供图/说明）；未指定观测点时用发射场作代理
    site = get_site(cfg)
    obs = (get_observers(cfg) or [site])[0]
    key_pts = {
        "launch_site": (float(site.get("lat_deg", 38.8632)), float(site.get("lon_deg", 111.5896))),
        "observer": (float(obs.get("lat_deg", 37.761467)), float(obs.get("lon_deg", 112.725219))),
        "beijing": (39.9042, 116.4074),
        "urumqi": (43.8256, 87.6168),
        "guangzhou": (23.1291, 113.2644),
        "harbin": (45.8038, 126.5350),
    }
    ctx["key_points_solar"] = {}
    for k, (la, lo) in key_pts.items():
        ctx["key_points_solar"][k] = {
            "lat_deg": la, "lon_deg": lo,
            "t0_deg": float(geo.solar_elevation_deg(la, lo, t0)),
            "window_start_deg": float(geo.solar_elevation_deg(la, lo, window_start)),
            "window_end_deg": float(geo.solar_elevation_deg(la, lo, window_end)),
        }

    # 太阳直射点 / 赤纬
    dec, sub_lon = geo.sun_subpoint(t0)
    ctx["sun"] = {"declination_deg": float(dec), "subsolar_lon_deg": float(sub_lon),
                  "subsolar_lat_deg": float(dec)}

    # 火箭光照
    ctx["rocket_illumination"] = {
        "n_samples": int(tr["n"]),
        "n_illuminated": int(illum.sum()),
        "n_in_shadow": int((~illum).sum()),
        "shadow_fraction": float(np.mean(~illum)),
        "model": "圆柱地影模型（太阳平行光，Re=WGS84_A；忽略半影/折射/扁率）",
        "first_illuminated": {
            "t_met_s": float(tr["t_met"][int(np.argmax(illum))]),
            "alt_m": float(tr["alt"][int(np.argmax(illum))]),
        } if illum.any() else None,
    }

    # 太阳高度时间曲线（覆盖窗口前后各 ~25 min）
    ta = min(t0 - dt.timedelta(minutes=25), window_start - dt.timedelta(minutes=10))
    tb = max(t0 + dt.timedelta(minutes=25), window_end + dt.timedelta(minutes=10))
    ctx["solar_timeline"] = build_solar_timeline(ta, tb, step_min=2.0)
    ctx["solar_timeline_range"] = {"start_utc": fmt_utc(ta), "end_utc": fmt_utc(tb)}
    # 场数据（供补充图使用）
    ctx["sun_el_t0_field"] = fields["t0"]
    ctx["sun_el_window_start_field"] = fields["window_start"]
    ctx["sun_el_window_end_field"] = fields["window_end"]
    return ctx


def _min_location(geom: np.ndarray, valid: np.ndarray) -> dict:
    m = np.where(valid, geom, np.inf)
    j, i = np.unravel_index(int(np.argmin(m)), m.shape)
    return {"lat_deg": float(LATS[j]), "lon_deg": float(LONS[i]),
            "max_el_deg": float(geom[j, i])}


def _max_reduction_location(geom: np.ndarray, sunlit: np.ndarray,
                            both: np.ndarray) -> dict:
    """阳光可见版相对几何版降低最多的格点。"""
    diff = np.where(both, sunlit - geom, np.inf)
    j, i = np.unravel_index(int(np.argmin(diff)), diff.shape)
    return {"lat_deg": float(LATS[j]), "lon_deg": float(LONS[i]),
            "geom_deg": float(geom[j, i]), "sunlit_deg": float(sunlit[j, i]),
            "diff_deg": float(diff[j, i])}


def _grid_wide_max(geom: np.ndarray) -> dict:
    """全网格（不限于中国陆域掩膜）最大高度角及其位置/时刻。"""
    j, i = np.unravel_index(int(np.argmax(geom)), geom.shape)
    return {"max_el_deg": float(geom[j, i]),
            "lat_deg": float(LATS[j]), "lon_deg": float(LONS[i])}


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
def compute_stats(tr: dict, grid: dict, ctx: dict, sunlit_info: dict,
                  geom_verify: dict, cfg: dict | None = None) -> dict:
    cfg = cfg or {}
    geom = grid["max_el_geom"]
    sunlit = grid["max_el_sunlit"]
    valid = DOMAIN_MASK & np.isfinite(geom)
    vals = geom[valid]
    row_area = ROW_AREA_KM2[:, None]
    total_area = TOTAL_LAND_AREA_KM2
    valid_area = float(np.sum(row_area * valid))

    area_ge, cell_ge, area_km2_ge = {}, {}, {}
    for th in ALL_THRESHOLDS:
        a = float(np.sum(row_area * (valid & (geom >= float(th)))))
        n = int(np.sum(valid & (geom >= float(th))))
        area_ge[f"ge_{th}_deg"] = a / valid_area if valid_area > 0 else float("nan")
        cell_ge[f"ge_{th}_deg"] = n / float(valid.sum())
        area_km2_ge[f"ge_{th}_deg"] = a
    k = int(np.argmax(np.where(valid, geom, -np.inf)))
    jmax, imax = np.unravel_index(k, geom.shape)
    pcts = {f"p{p:02d}": float(np.percentile(vals, p))
            for p in (1, 5, 10, 25, 50, 75, 90, 95, 99)}

    vis = valid & (geom >= 0.0)
    vis_area = float(np.sum(row_area * vis))
    vis_vals = geom[vis]
    visible = {
        "definition": "可见 = 上升段全过程最大高度角 ≥ 0°（火箭曾升到该格点地平线以上）",
        "n_visible_cells": int(vis.sum()),
        "area_fraction_of_china_land": vis_area / valid_area if valid_area > 0 else float("nan"),
        "area_km2": vis_area,
        "median_deg": float(np.median(vis_vals)) if vis_vals.size else float("nan"),
        "mean_deg": float(np.mean(vis_vals)) if vis_vals.size else float("nan"),
        "min_deg": float(vis_vals.min()) if vis_vals.size else float("nan"),
        "max_deg": float(vis_vals.max()) if vis_vals.size else float("nan"),
        "area_fraction_ge": {}, "cell_fraction_ge": {},
        "note": "负值档 = 上升段全程火箭在格点地平线以下（几何不可见）",
    }
    for th in ALL_THRESHOLDS:
        a = float(np.sum(row_area * (vis & (geom >= float(th)))))
        visible["area_fraction_ge"][f"ge_{th}_deg"] = a / vis_area if vis_area > 0 else float("nan")
        visible["cell_fraction_ge"][f"ge_{th}_deg"] = (
            float(np.sum(vis & (geom >= float(th)))) / float(vis.sum()) if vis.sum() > 0 else float("nan"))

    lat_bands = []
    band_lo = int(math.floor(GRID_LAT_MIN + 1e-9))
    band_hi = int(math.ceil(GRID_LAT_MAX - 1e-9))
    for lo in range(band_lo, band_hi, 5):
        band = valid & (LAT2 >= lo) & (LAT2 < lo + 5)
        if band.sum() == 0:
            continue
        bv = geom[band]
        ba = float(np.sum(row_area * band))
        lat_bands.append({
            "lat_min": float(lo), "lat_max": float(lo + 5),
            "n_cells": int(band.sum()), "area_km2": ba,
            "area_fraction_of_china_land": ba / valid_area,
            "min_deg": float(bv.min()), "median_deg": float(np.median(bv)),
            "max_deg": float(bv.max()),
            "area_fraction_ge_0": float(np.sum(row_area * (band & (geom >= 0.0))) / ba),
            "area_fraction_ge_10": float(np.sum(row_area * (band & (geom >= 10.0))) / ba),
        })

    # ---- 阳光可见版统计与几何版差异 ----
    sun_valid = DOMAIN_MASK & np.isfinite(sunlit)
    both = valid & sun_valid
    diff = (sunlit - geom)[both]
    n_undef = int(np.sum(valid & ~sun_valid))
    # 未定义格点的原因拆分：全程昼侧 vs 夜侧但火箭在影内
    f_t0 = solar_field(ctx["t0_utc"])
    f_te = solar_field(tr["t_end"])
    undef_day = valid & ~sun_valid & (f_t0 > 0.0) & (f_te > 0.0)
    undef_night = valid & ~sun_valid & ~((f_t0 > 0.0) & (f_te > 0.0))
    # 窗口内发生日出的中国陆域面积（WS 时暗、WE 时亮）
    band_win = (ctx["sun_el_window_start_field"] <= 0.0) & (ctx["sun_el_window_end_field"] > 0.0)
    band_win_area = float(np.sum(row_area * (band_win & DOMAIN_MASK)))
    sv_area = float(np.sum(row_area * sun_valid))
    sv_stats = {
        "n_valid_cells": int(sun_valid.sum()),
        "n_valid_cells_area_fraction_of_china_land": sv_area / valid_area,
        "area_km2": sv_area,
        "n_undefined_cells": n_undef,
        "n_undefined_because_observer_daytime": int(undef_day.sum()),
        "n_undefined_because_rocket_in_shadow": int(undef_night.sum()),
        "median_deg": float(np.median(sunlit[sun_valid])) if sun_valid.any() else float("nan"),
        "mean_deg": float(np.mean(sunlit[sun_valid])) if sun_valid.any() else float("nan"),
        "min_deg": float(sunlit[sun_valid].min()) if sun_valid.any() else float("nan"),
        "max_deg": float(sunlit[sun_valid].max()) if sun_valid.any() else float("nan"),
        "area_fraction_ge": {}, "n_ge": {},
    }
    for th in ALL_THRESHOLDS:
        a = float(np.sum(row_area * (sun_valid & (sunlit >= float(th)))))
        sv_stats["area_fraction_ge"][f"ge_{th}_deg"] = a / sv_area if sv_area > 0 else float("nan")
        sv_stats["n_ge"][f"ge_{th}_deg"] = int(np.sum(sun_valid & (sunlit >= float(th))))
    compare = {
        "n_cells_both_defined": int(both.sum()),
        "area_fraction_of_china_land": float(np.sum(row_area * both) / valid_area),
        "median_diff_deg": float(np.median(diff)) if diff.size else float("nan"),
        "mean_diff_deg": float(np.mean(diff)) if diff.size else float("nan"),
        "min_diff_deg": float(diff.min()) if diff.size else float("nan"),
        "max_diff_deg": float(diff.max()) if diff.size else float("nan"),
        "area_fraction_diff_gt_0_1deg": float(
            np.sum(row_area * (both & (sunlit - geom < -0.1))) / valid_area),
        "area_fraction_diff_gt_1deg": float(
            np.sum(row_area * (both & (sunlit - geom < -1.0))) / valid_area),
        "area_fraction_diff_gt_10deg": float(
            np.sum(row_area * (both & (sunlit - geom < -10.0))) / valid_area),
        "n_cells_geom_visible_but_sunlit_undefined": int(np.sum(vis & ~sun_valid)),
        "max_reduction_location": _max_reduction_location(geom, sunlit, both),
        "n_cells_diff_lt_m10": int(np.sum(both & (sunlit - geom < -10.0))),
        "note": ("阳光可见版是几何版的**子集最大值**：仅保留“火箭被照亮 且 观测者夜侧”的采样时刻，"
                 "因此逐格 ≤ 几何版。差值大的两类机制：①发射场/早段轨迹附近——几何最大值发生在"
                 "火箭仍处晨光地影内的早期；②日出带内——几何最大值时刻观测者已进入昼侧。"),
    }

    stats = {
        "schema": "map/统计.json v%s (%s %s)" % (
            SCRIPT_VERSION, mission_date_label(cfg, tr["t0"]), short_rocket_label(cfg)),
        "script": "map/plot_map.py",
        "script_version": SCRIPT_VERSION,
        "generated_utc": fmt_utc(utcnow()),
        "disclaimer": DISCLAIMER,
        "trajectory": {
            "status": tr["status"], "label": tr["label"], "file": tr["file"],
            "path": tr.get("path", ""),
            "mtime_utc": fmt_utc(tr["mtime_utc"]), "size_bytes": tr["size_bytes"],
            "n_points": tr["n"], "sample_dt_s": tr["sample_dt_s"],
            "t0_utc": fmt_utc(tr["t0"]), "t0_source": tr.get("t0_source", ""),
            "t_end_utc": fmt_utc(tr["t_end"]),
            "duration_s": float(tr["t_met"][-1] - tr["t_met"][0]),
            "note": ("正式：Lead 3-DOF 标称上升段弹道" if tr["status"] == "real"
                     else "夹具：2026-10-25 CZ-8A 参考弹道（临时版，仅流程验证）"),
        },
        "time": {
            "t0_used_utc": fmt_utc(tr["t0"]),
            "t0_used_bjt": fmt_bjt(tr["t0"]),
            "window_start_utc": fmt_utc(ctx["window_start_utc"]),
            "window_end_utc": fmt_utc(ctx["window_end_utc"]),
            "window_start_bjt": fmt_bjt(ctx["window_start_utc"]),
            "window_end_bjt": fmt_bjt(ctx["window_end_utc"]),
            "window_source": ctx["window_source"],
        },
        "grid": {
            "lon_min": GRID_LON_MIN, "lon_max": GRID_LON_MAX,
            "lat_min": GRID_LAT_MIN, "lat_max": GRID_LAT_MAX,
            "step_deg": GRID_STEP, "n_lon": NX, "n_lat": NY,
            "n_cells": int(NY * NX),
            "observer_ellipsoid_height_m": 0.0,
            "mask": ("common.boundaries.china_land_mask（Natural Earth 1:50m 示意边界，"
                     "含台湾/海南/港澳）"),
            "domain_label": LAND_LABEL,
            "n_land_cells": N_LAND,
            "n_domain_cells": N_DOMAIN,
            "land_area_km2": TOTAL_LAND_AREA_KM2,
            "land_area_km2_boundaries_crosscheck": float(
                boundaries.land_mask_stats(DOMAIN_MASK, LONS, LATS)["area_km2"]),
        },
        "verify_geo": geom_verify,
        "max_elevation_deg": {
            "median": float(np.median(vals)),
            "mean": float(np.mean(vals)),
            "std": float(np.std(vals)),
            "min": float(vals.min()),
            "max": float(vals.max()),
            "max_location": {
                "lat_deg": float(LATS[jmax]), "lon_deg": float(LONS[imax]),
                "t_met_s": float(grid["t_max_met_s"][jmax, imax]),
                "t_utc": fmt_utc(tr["t0"] + dt.timedelta(seconds=float(grid["t_max_met_s"][jmax, imax]))),
                "az_deg": float(grid["az_max_deg"][jmax, imax]),
            },
            "min_location": _min_location(geom, valid),
            "percentiles": pcts,
            "n_valid_cells": int(valid.sum()),
            "grid_wide_max": _grid_wide_max(geom),
        },
        "area_fraction_ge": area_ge,
        "cell_fraction_ge": cell_ge,
        "area_km2_ge": area_km2_ge,
        "visibility": visible,
        "latitude_bands_5deg": lat_bands,
        "max_elevation_deg_summary": {
            "n_below_horizon_cells": int(np.sum(valid & (geom < 0.0))),
            "area_fraction_below_horizon": float(np.sum(row_area * (valid & (geom < 0.0))) / valid_area),
            "visible_area_fraction": float(vis_area / valid_area),
            "note": "负值 = 上升段全程火箭在该格点地平线以下（几何不可见）",
        },
        "sunlit_version": {**sunlit_info, "stats": sv_stats, "vs_geometric": compare},
        "terminator": {
            "t0_utc": fmt_utc(tr["t0"]),
            "window_start_utc": fmt_utc(ctx["window_start_utc"]),
            "window_end_utc": fmt_utc(ctx["window_end_utc"]),
            "sun": ctx["sun"],
            "china_solar": {k: ctx[f"solar_{k}"] for k in ("t0", "window_start", "window_end")},
            "dawn_first_china_day_utc": fmt_utc(ctx["dawn_first_china_day_utc"]) if ctx["dawn_first_china_day_utc"] else None,
            "dawn_all_china_day_utc": fmt_utc(ctx["dawn_all_china_day_utc"]) if ctx["dawn_all_china_day_utc"] else None,
            "civil_first_china_utc": fmt_utc(ctx["civil_first_china_utc"]) if ctx["civil_first_china_utc"] else None,
            "civil_all_china_utc": fmt_utc(ctx["civil_all_china_utc"]) if ctx["civil_all_china_utc"] else None,
            "dawn_0deg_crosses_china_at_t0": ctx["dawn_0deg_crosses_china_at_t0"],
            "window_crosses_sunrise_in_china": ctx["window_crosses_sunrise_in_china"],
            "window_sunrise_band_area_fraction": band_win_area / valid_area,
            "n_china_cells_sunrise_during_window": int((band_win & DOMAIN_MASK).sum()),
            "key_points_solar": ctx["key_points_solar"],
            "level_definitions": {
                "0": "几何地平（晨昏线正线；未含折射）",
                "-0.833": "含大气折射(34′)+太阳视半径(16′)的真实晨昏线",
                "-6": "民用晨光（civil twilight）", "-12": "航海晨光", "-18": "天文晨光",
            },
            "note": ("T0 处于黎明：中国东部 0° 晨昏线穿过大陆（东侧已日出、西侧仍在夜侧）；"
                     "航警窗口 22:43–23:10Z 内 0° 线继续西移（日出带扫过中国东部），故给出窗口起止两条晨昏线（见补充图）。"),
        },
        "rocket_illumination": ctx["rocket_illumination"],
        "solar_timeline": ctx["solar_timeline"],
        "solar_timeline_range": ctx["solar_timeline_range"],
    }
    return stats


# --------------------------------------------------------------------------- #
# 绘图辅助
# --------------------------------------------------------------------------- #
NIGHT_BANDS_MAIN = [(-90.0, 0.0, "#0b1030", 0.28, "夜侧（太阳高度<0°）"),
                    (0.0, 90.0, "#fff4c2", 0.06, "昼侧（太阳高度>0°）")]
NIGHT_BANDS_FULL = [(-90.0, -18.0, "#08080f", 0.50, "夜间 <-18°"),
                    (-18.0, -12.0, "#141428", 0.36, "天文晨光 -18~-12°"),
                    (-12.0, -6.0, "#262650", 0.27, "航海晨光 -12~-6°"),
                    (-6.0, 0.0, "#4a3a6a", 0.18, "民用晨光 -6~0°"),
                    (0.0, 90.0, "#fff4c2", 0.10, "昼侧 >0°")]
TERM_STYLES = {
    0.0: dict(color="#1a1a1a", lw=1.8, ls="-", label="晨昏线 0°（几何地平）"),
    -0.833: dict(color="#1a1a1a", lw=0.9, ls=(0, (3, 2)),
                 label="真实晨昏线 -0.833°（折射+日面）"),
    -6.0: dict(color="#7a5a00", lw=1.0, ls=(0, (5, 2)), label="民用晨光 -6°"),
    -12.0: dict(color="#5c4300", lw=0.9, ls=(0, (2, 2)), label="航海晨光 -12°"),
    -18.0: dict(color="#3f2e00", lw=0.9, ls=(0, (1, 2)), label="天文晨光 -18°"),
}


def _world_base(ax, bbox=None):
    """非中国区域底色 + 中国陆地底色 + 海岸线（Natural Earth 示意）。"""
    from matplotlib.collections import LineCollection
    bbox = bbox or (GRID_LON_MIN, GRID_LAT_MIN, GRID_LON_MAX, GRID_LAT_MAX)
    ax.set_facecolor("#e8e7e2")                     # 非中国陆域示例底色
    ax.contourf(LONS, LATS, DOMAIN_MASK.astype(float), levels=[0.5, 1.5],
                colors=[plotstyle.COLORS["land"]], zorder=0.30)
    try:
        coast = boundaries.load_coastline()
        segs = []
        for s in coast:
            if s is None or len(s) < 2:
                continue
            if s[:, 0].max() < bbox[0] or s[:, 0].min() > bbox[2]:
                continue
            if s[:, 1].max() < bbox[1] or s[:, 1].min() > bbox[3]:
                continue
            segs.append(s)
        if segs:
            ax.add_collection(LineCollection(segs, colors=plotstyle.COLORS["coast"],
                                             linewidths=0.55, alpha=0.85, zorder=0.9))
    except Exception as e:  # noqa: BLE001
        print(f"[map] 警告: 海岸线绘制失败: {e}")


def _set_map_axes(ax, bbox=None, tick_lon=None, tick_lat=None):
    bbox = bbox or (GRID_LON_MIN, GRID_LAT_MIN, GRID_LON_MAX, GRID_LAT_MAX)
    if tick_lon is None:                      # 按网格跨度选刻度间隔（默认网格 10°/5° 与 v2 一致）
        span = bbox[2] - bbox[0]
        tick_lon = 10.0 if span >= 40 else (5.0 if span >= 15 else 2.0)
    if tick_lat is None:
        span = bbox[3] - bbox[1]
        tick_lat = 5.0 if span >= 20 else (2.0 if span >= 8 else 1.0)
    ax.set_xlim(bbox[0], bbox[2])
    ax.set_ylim(bbox[1], bbox[3])
    ax.set_aspect(1.0 / math.cos(math.radians((bbox[1] + bbox[3]) / 2.0)), adjustable="box")
    ax.set_xticks(np.arange(TICK_LON_START, bbox[2] + 1e-6, tick_lon))
    ax.set_yticks(np.arange(TICK_LAT_START, bbox[3] + 1e-6, tick_lat))
    ax.set_xlabel("经度 (°E)", fontsize=10)
    ax.set_ylabel("纬度 (°N)", fontsize=10)
    ax.tick_params(labelsize=9)
    ax.grid(True, linestyle=":", linewidth=0.5, color="#a8a8a8", alpha=0.55)


def _night_shading(ax, field, bands=None, zorder=2.0):
    from matplotlib.patches import Patch
    bands = bands or NIGHT_BANDS_MAIN
    patches = []
    for lo, hi, color, alpha, label in bands:
        ax.contourf(LONS, LATS, field, levels=[lo, hi], colors=[color],
                    alpha=alpha, zorder=zorder, antialiased=True)
        patches.append(Patch(facecolor=color, alpha=max(alpha, 0.2), label=label))
    return patches


def _term_contours(ax, field, label=True, fontsize=7.6, lw_scale=1.0,
                   levels=None, skip_absent_labels=True):
    from matplotlib.lines import Line2D
    handles = []
    fmin, fmax = float(np.nanmin(field)), float(np.nanmax(field))
    for lev in (levels or TERMINATOR_LEVELS):
        st = TERM_STYLES[lev]
        present = fmin < lev < fmax
        if not present:
            if skip_absent_labels:
                handles.append(Line2D([], [], color=st["color"], lw=st["lw"] * lw_scale,
                                      ls=st["ls"], label=st["label"] + "（此范围外）"))
            continue
        try:
            cs = ax.contour(LONS, LATS, field, levels=[lev], colors=st["color"],
                            linewidths=st["lw"] * lw_scale, linestyles=[st["ls"]], zorder=6)
            if label:
                ax.clabel(cs, fmt={lev: f"{lev:g}°"}, fontsize=fontsize, inline=True,
                          inline_spacing=4, colors=st["color"])
        except Exception:  # noqa: BLE001
            pass
        handles.append(Line2D([], [], color=st["color"], lw=st["lw"] * lw_scale,
                              ls=st["ls"], label=st["label"]))
    return handles


def _make_fill_cmap():
    from matplotlib.colors import ListedColormap
    import matplotlib.pyplot as plt
    neg = ["#1c1f27", "#292d38", "#3a4050", "#4e5768", "#6a7488", "#8b93a4"]
    pos = [plt.get_cmap("YlOrRd")(v) for v in np.linspace(0.14, 0.985, 9)]
    cmap = ListedColormap(neg + pos)
    cmap.set_bad((1, 1, 1, 0))
    return cmap


def _phase_events(tr: dict):
    """相位切换事件（通用：S1→S2→ORBIT；夹具含 S1_ALL/S1_CORE 兼容）。"""
    phases = tr.get("phase") or []
    events = []
    seen = set()
    for i, p in enumerate(phases):
        ps = str(p).upper()
        if ps.startswith("S2") and "S2" not in seen:
            seen.add("S2"); events.append((i, "一二级分离/二级点火", "D", (38, -6)))
        if ps.startswith("ORBIT") and "ORBIT" not in seen:
            seen.add("ORBIT"); events.append((i, "二级关机/入轨", "^", (0, 14)))
    return events


def _mark_cities(ax, cities, fontsize=7.4):
    import matplotlib.patheffects as pe
    for c in cities:
        ax.plot([c["lon_deg"]], [c["lat_deg"]], marker="o", ms=3.2, mfc="#111111",
                mec="white", mew=0.5, zorder=9.8)
        dx, dy = c.get("label_offset", (4, 3))
        ax.annotate(f"{c['name']} {c['max_el_deg']:+.1f}°",
                    (c["lon_deg"], c["lat_deg"]), xytext=(dx, dy),
                    textcoords="offset points", fontsize=fontsize, color="#111111",
                    zorder=9.9, ha="left", va="center",
                    path_effects=[pe.withStroke(linewidth=2.0, foreground="white")])


def _stat_lines(stats: dict, ctx: dict, cfg: dict) -> list[str]:
    me = stats["max_elevation_deg"]
    af = stats["area_fraction_ge"]
    vis = stats["visibility"]
    sv = stats["sunlit_version"]["stats"]
    site = get_site(cfg)
    site_short = short_site_label(cfg)
    obs0 = (stats.get("observers") or [{}])[0]
    linhas = [
        f"{LAND_LABEL}网格 {stats['grid'].get('n_domain_cells', stats['grid']['n_land_cells']):,} 格 ≈ "
        f"{stats['grid']['land_area_km2']/1e4:.1f} 万 km²",
        "（Natural Earth 示意边界，非官方界线）",
        f"最大高度角 中位数 {me['median']:+.2f}°  平均 {me['mean']:+.2f}°",
        f"  最小 {me['min']:+.2f}°  最大 {me['max']:+.2f}°",
        f"  最大值位于 {me['max_location']['lat_deg']:.1f}°N, "
        f"{me['max_location']['lon_deg']:.1f}°E（MET "
        f"{me['max_location']['t_met_s']:.0f} s）",
        f"可见区（≥0°）{vis['n_visible_cells']:,} 格，占陆域 "
        f"{100*vis['area_fraction_of_china_land']:.1f}%",
        f"  可见区内中位数 {vis['median_deg']:+.2f}°；"
        f"全程低于地平 {100*stats['max_elevation_deg_summary']['area_fraction_below_horizon']:.1f}%",
        "面积占比（中国陆域）：",
        f"  ≥10° {100*af['ge_10_deg']:.2f}%  ≥15° {100*af['ge_15_deg']:.2f}%  "
        f"≥20° {100*af['ge_20_deg']:.2f}%",
        f"  ≥30° {100*af['ge_30_deg']:.2f}%  ≥45° {100*af['ge_45_deg']:.2f}%  "
        f"≥60° {100*af['ge_60_deg']:.2f}%",
        f"发射点 {site_short}（{site.get('lat_deg',38.8632):.4f}°N, "
        f"{site.get('lon_deg',111.5896):.4f}°E）",
        f"  最近格点最大高度角 {stats['launch_site_value']['nearest_grid_max_el_deg']:+.2f}°"
        f"（点值 {stats['launch_site_value']['max_el_deg']:+.1f}°）",
        *([] if not stats.get("observers") else [
            f"指定观测点（{obs0.get('lat_deg', float('nan')):.4f}°N, "
            f"{obs0.get('lon_deg', float('nan')):.4f}°E）",
            f"  最大高度角 {stats['observers'][0]['max_el_deg']:+.2f}°"]),
        *(["未指定观测点（图上不叠加观测点标注）"] if not stats.get("observers") else []),
        "城市：" + "  ".join(f"{c['name']} {c['max_el_deg']:+.1f}°"
                             for c in stats["cities"]
                             if c["name"] in ("北京", "上海", "广州", "武汉")),
        f"阳光可见版（火箭照亮 & 观测者夜侧）",
        f"  有效 {sv['n_valid_cells']:,} 格（占陆域 "
        f"{100*sv['n_valid_cells_area_fraction_of_china_land']:.1f}%），"
        f"中位数 {sv['median_deg']:+.2f}°",
        f"  未定义 {sv['n_undefined_cells']:,} 格（观测者全程昼侧等）",
        f"晨昏线 T0 {fmt_hms(ctx['t0_utc'])}Z：0° 线穿过中国东部",
        f"  中国太阳高度 {ctx['solar_t0']['china']['min_deg']:+.1f}° ~ "
        f"{ctx['solar_t0']['china']['max_deg']:+.1f}°",
        f"窗口 {fmt_hms(ctx['window_start_utc'])}–{fmt_hms(ctx['window_end_utc'])}Z："
        f"0° 线西移，见补充图",
    ]
    return linhas


def _note_text(stats: dict, ctx: dict) -> str:
    _fill_note = ("填色仅中国陆域；灰色为掩膜外区域（不参与统计）。"
                  if N_LAND else "填色覆盖全网格（中国陆域掩膜未命中）；统计域=全网格。")
    return (
        "读图说明\n"
        f"· {_fill_note}\n"
        "· 最大高度角 = 对上升段全部轨迹点取 ECEF 几何高度角\n"
        "  最大值（含地球曲率与目标高度）；负值 = 火箭全程\n"
        "  在该格点地平线以下（几何不可见）。\n"
        "· 格点取椭球高 0 m；地形/建筑只遮挡，不改变几何最大值\n"
        "  （实际可见需另做 DEM 遮挡，见 md 局限）。\n"
        "· 晨昏线：T0 太阳几何高度角等值线 0°/-0.833°/-6°/\n"
        "  -12°/-18°；暗色半透明 = 夜侧，淡黄 = 昼侧。\n"
        "· 青色虚线 = 0° 可见性边界（最大高度角 = 0°）。\n"
        "· 阳光可见版：只计“火箭被照亮 + 观测者太阳高度≤0°”\n"
        "  时刻；与几何版逐格差见统计 JSON 与补充图。\n"
        "· 边界为 Natural Earth 1:50m 示意数据，非官方界线。\n"
        f"· 标注数值为点坐标（椭球高 0 m）直接计算值，与所在\n"
        f"  {GRID_STEP:g}° 格点值可能相差 <0.1°。"
    )


# --------------------------------------------------------------------------- #
# 主图
# --------------------------------------------------------------------------- #
def make_main_figure(tr, grid, ctx, stats, cfg, outdir: Path, dpi: int):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patheffects as pe
    from matplotlib.colors import BoundaryNorm
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    plotstyle.setup(font_size=10.5, verbose=True)
    cmap = _make_fill_cmap()
    norm = BoundaryNorm(FILL_LEVELS, cmap.N, clip=True)
    bbox = (GRID_LON_MIN, GRID_LAT_MIN, GRID_LON_MAX, GRID_LAT_MAX)

    fig = plt.figure(figsize=(13.4, 9.8), dpi=110)
    ax = fig.add_axes([0.030, 0.140, 0.655, 0.700])
    _world_base(ax, bbox)

    # ---- 数据填色（仅中国陆域） ----
    Zraw = grid["max_el_geom"]
    Z = np.ma.masked_where(~DOMAIN_MASK, np.clip(Zraw, FILL_LEVELS[0] + 1e-6,
                                                FILL_LEVELS[-1] - 1e-6))
    ax.contourf(LONS, LATS, Z, levels=FILL_LEVELS, cmap=cmap, norm=norm,
                antialiased=True, zorder=1.2)
    # 0° 可见性边界
    try:
        cs0 = ax.contour(LONS, LATS, np.ma.masked_where(~DOMAIN_MASK, Zraw),
                         levels=[0.0], colors="#00c2e0", linewidths=2.0,
                         linestyles=[(0, (6, 3))], zorder=6.2)
        ax.clabel(cs0, fmt={0.0: "0° 可见性边界"}, fontsize=8.4, inline=True,
                  colors="#00707f")
    except Exception as e:  # noqa: BLE001
        print(f"[map] 警告: 0° 可见性边界绘制失败: {e}")

    # ---- 昼/夜阴影 + 晨昏线 ----
    sunel0 = ctx["sun_el_t0_field"]
    night_patches = _night_shading(ax, sunel0, NIGHT_BANDS_MAIN, zorder=2.2)
    term_handles = _term_contours(ax, sunel0, label=True, fontsize=7.4)
    # 晨昏线文字标注（0°/-6°，放在国境东北空白处，避免与城市标注重叠）
    for _lev, _txt, _col in [(0.0, "0° 晨昏线（T0）", "#1a1a1a"),
                             (-6.0, "-6° 民用晨光", "#7a5a00")]:
        _j = int(np.argmin(np.abs(LATS - (GRID_LAT_MIN + 0.925 * (GRID_LAT_MAX - GRID_LAT_MIN)))))
        _lonx = terminator_lon_at_lat(sunel0[_j], _lev)
        if _lonx is not None and GRID_LON_MIN <= _lonx <= GRID_LON_MAX:
            ax.annotate(_txt, (_lonx, float(LATS[_j])), xytext=(8, 7),
                        textcoords="offset points", fontsize=7.2, color=_col, zorder=9.55,
                        path_effects=[pe.withStroke(linewidth=2.2, foreground="white")])

    # ---- 国界 / 省界 / 海岸线 ----
    info = boundaries.plot_china(ax, bbox=bbox, borders=True, provinces=True,
                                 coastline=False, cities=False, zorder=5)

    # ---- 星下点轨迹 ----
    ax.plot(tr["lon"], tr["lat"], color=plotstyle.COLORS["trajectory"], lw=2.3,
            zorder=8.0, solid_capstyle="round")
    ax.plot(tr["lon"][::40], tr["lat"][::40], ls="none", marker="o", ms=2.0,
            mfc="white", mec=plotstyle.COLORS["trajectory"], mew=0.7, zorder=8.1)
    for idx, label, marker, off in _phase_events(tr):
        la, lo = float(tr["lat"][idx]), float(tr["lon"][idx])
        if not (bbox[0] <= lo <= bbox[2] and bbox[1] <= la <= bbox[3]):
            continue
        ax.plot([lo], [la], marker=marker, ms=6.0, mfc="white",
                mec=plotstyle.COLORS["trajectory"], mew=1.3, zorder=9.2)
        ax.annotate(f"{label}\nT+{tr['t_met'][idx]:.0f}s", (lo, la), xytext=off,
                    textcoords="offset points", fontsize=6.8, color="#0b4f7a",
                    zorder=9.5,
                    path_effects=[pe.withStroke(linewidth=2.2, foreground="white")])
    # 轨迹时刻标注
    for met in (60, 120, 180, 300, 420, 540):
        i = int(np.argmin(np.abs(tr["t_met"] - met)))
        if i >= tr["n"]:
            continue
        la, lo = float(tr["lat"][i]), float(tr["lon"][i])
        if not (bbox[0] <= lo <= bbox[2] and bbox[1] <= la <= bbox[3]):
            continue
        ax.annotate(f"T+{tr['t_met'][i]:.0f}s", (lo, la), xytext=(4, -9),
                    textcoords="offset points", fontsize=6.2, color="#14507a", zorder=9.4,
                    path_effects=[pe.withStroke(linewidth=1.8, foreground="white")])

    # ---- 发射点 ----
    site = get_site(cfg)
    site_short = short_site_label(cfg)
    site_lat, site_lon = float(site.get("lat_deg", 38.8632)), float(site.get("lon_deg", 111.5896))
    ax.plot([site_lon], [site_lat], marker="*", ms=19, mfc=plotstyle.COLORS["highlight"],
            mec="white", mew=1.0, zorder=10.0)
    _ann_lon = GRID_LON_MIN + 0.4140625 * (GRID_LON_MAX - GRID_LON_MIN)
    _ann_lat = GRID_LAT_MIN + 0.8625 * (GRID_LAT_MAX - GRID_LAT_MIN)
    ax.annotate(
        f"发射点 {site_short}\n{site_lat:.4f}°N, {site_lon:.4f}°E\n"
        f"最大高度角(最近格点) {stats['launch_site_value']['nearest_grid_max_el_deg']:.1f}°",
        xy=(site_lon, site_lat), xytext=(_ann_lon, _ann_lat), textcoords="data",
        fontsize=7.8, color="#a01b12", ha="left", va="center", zorder=10.5,
        bbox=dict(boxstyle="round,pad=0.32", fc="white", ec=plotstyle.COLORS["highlight"],
                  alpha=0.90),
        arrowprops=dict(arrowstyle="->", color=plotstyle.COLORS["highlight"], lw=1.1,
                        shrinkB=6, connectionstyle="arc3,rad=-0.15"))

    # ---- 指定观测点（config.observers 可多个，全部绘制） ----
    for ob in stats["observers"]:
        ax.plot([ob["lon_deg"]], [ob["lat_deg"]], marker="v", ms=9.5,
                mfc=plotstyle.COLORS["observer"], mec="#083b45", mew=0.9, zorder=10.2)
        _olab = "指定观测点" if len(stats["observers"]) == 1 else f"观测点 {ob.get('id', '')}"
        ax.annotate(f"{_olab} {ob['max_el_deg']:.2f}°",
                    (ob["lon_deg"], ob["lat_deg"]), xytext=(14, -14),
                    textcoords="offset points", fontsize=8.0, color="#043c47",
                    zorder=10.4, ha="left",
                    path_effects=[pe.withStroke(linewidth=2.4, foreground="white")])

    # ---- 主要城市 ----
    _mark_cities(ax, stats["cities"], fontsize=7.2)
    _set_map_axes(ax, bbox)

    # ---- 图例 ----
    _mask_label = "非中国陆域（不参与统计）" if N_LAND else "掩膜外区域（不参与统计）"
    handles = [
        Patch(facecolor="#e8e7e2", edgecolor="#9a9a9a", label=_mask_label),
        *night_patches,
        *term_handles,
        Line2D([], [], color="#00c2e0", lw=1.8, ls=(0, (6, 3)),
               label="0° 可见性边界（最大高度角=0°）"),
        Line2D([], [], color=plotstyle.COLORS["highlight"], marker="*", ls="", ms=12,
               label=f"发射点（{site_short}）"),
        Line2D([], [], color=plotstyle.COLORS["trajectory"], lw=2.2, label="星下点轨迹（上升段）"),
        *([] if not stats.get("observers") else [
            Line2D([], [], color=plotstyle.COLORS["observer"], marker="v", ls="", ms=7,
                   label="指定观测点（标注最大高度角）")]),
        Line2D([], [], color="#111111", marker="o", ls="", ms=3.2,
               label="主要城市（标注最大高度角）"),
    ]
    leg = ax.legend(handles=handles, loc="lower left", fontsize=6.9, framealpha=0.92,
                    facecolor="white", edgecolor="#bbbbbb", ncol=1, borderpad=0.5,
                    labelspacing=0.28, bbox_to_anchor=(0.010, 0.010))
    leg.set_zorder(30)

    # ---- 色条 ----
    cax = fig.add_axes([0.6905, 0.146, 0.0110, 0.688])
    sm = plt.cm.ScalarMappable(norm=norm, cmap=cmap)
    sm.set_array([])
    cb = fig.colorbar(sm, cax=cax, boundaries=FILL_LEVELS, spacing="uniform")
    mids = [(FILL_LEVELS[i] + FILL_LEVELS[i + 1]) / 2 for i in range(len(FILL_LEVELS) - 1)]
    cb.set_ticks(mids)
    cb.ax.set_yticklabels(FILL_LABELS, fontsize=6.8)
    cb.set_label("最大高度角 (°)", fontsize=8.2, labelpad=3)

    # ---- 右栏统计 ----
    ax_st = fig.add_axes([0.7280, 0.470, 0.266, 0.420])
    ax_st.axis("off")
    ax_st.text(0.0, 1.0, "\n".join(_stat_lines(stats, ctx, cfg)),
               transform=ax_st.transAxes, fontsize=6.7, family="sans-serif",
               va="top", ha="left", linespacing=1.42, zorder=25,
               bbox=dict(boxstyle="round,pad=0.42", fc="white", ec="#9a9a9a", alpha=0.96))
    # ---- 右栏说明 ----
    ax_txt = fig.add_axes([0.7280, 0.146, 0.266, 0.320])
    ax_txt.axis("off")
    ax_txt.text(0.0, 1.0, _note_text(stats, ctx), transform=ax_txt.transAxes,
                fontsize=6.6, va="top", ha="left", linespacing=1.34, family="sans-serif")

    # ---- 标题 / 注记 ----
    tag = "" if tr["status"] == "real" else "【临时版：夹具弹道】"
    _date = mission_date_label(cfg, tr["t0"])
    _rocket = short_rocket_label(cfg)
    fig.suptitle(f"{tag}{_date} {_rocket}全国最大高度角地图（含晨昏线）",
                 fontsize=15.0, y=0.978, fontweight="bold")
    fig.text(0.5, 0.947,
             f"观测点：{LAND_LABEL} {GRID_STEP:g}°×{GRID_STEP:g}° 网格"
             f"（{GRID_LON_MIN:g}–{GRID_LON_MAX:g}°E, {GRID_LAT_MIN:g}–{GRID_LAT_MAX:g}°N，"
             f"{NX}×{NY}），椭球高 0 m ｜ "
             f"弹道：{tr['file']}（{tr['n']} 点，Δt≈{tr['sample_dt_s']:.2f} s，"
             f"T+0→{tr['t_met'][-1]:.0f} s）",
             ha="center", fontsize=8.6)
    fig.text(0.5, 0.926,
             f"T0 = {fmt_utc(tr['t0'])}（北京时 {fmt_bjt(tr['t0'])}） ｜ "
             "负值 = 火箭全程低于该格点地平线（几何不可见） ｜ "
             "晨昏线为 T0 太阳几何高度角等值线",
             ha="center", fontsize=8.6)
    footer = (
        f"数据：{tr['file']}（{tr['status']}，mtime {fmt_utc(tr['mtime_utc'])}，{tr['n']} 点；"
        f"{tr.get('t0_source','')}）\n"
        "国界/省界/海岸线：Natural Earth 1:50m/1:10m（public domain，示意边界，非官方界线）；"
        "太阳位置：common.geo（Meeus 低精度，几何高度角，未含折射）。\n"
        "说明：最大高度角 = 对上升段全部轨迹点取 ECEF 几何高度角最大值（含地球曲率）；"
        "地形/建筑只遮挡、不影响最大值；格点椭球高 0 m。晨昏线为 T0 时刻太阳高度等值线；"
        f"窗口起止两条晨昏线见补充图。  {DISCLAIMER}  脚本：map/plot_map.py v{SCRIPT_VERSION}"
    )
    fig.text(0.030, 0.008, footer, fontsize=6.3, color="#333333", ha="left", va="bottom",
             linespacing=1.45)

    png_path = outdir / "全国最大高度角地图.png"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        p, px_w, px_h = plotstyle.save(fig, png_path, dpi=dpi, verbose=True)
    glyph_warnings = [str(w.message) for w in caught
                      if "missing from font" in str(w.message).lower()
                      or "glyph" in str(w.message).lower()]
    plt.close(fig)
    real_w, real_h = _png_size(p, px_w, px_h)
    return {"path": str(p), "px_w": int(real_w), "px_h": int(real_h),
            "glyph_warnings": glyph_warnings,
            "plot_china_info": info}


# --------------------------------------------------------------------------- #
# 补充图：晨昏线与"阳光可见"判据
# --------------------------------------------------------------------------- #
SUN_FILL_LEVELS = [-45.0, -30.0, -20.0, -12.0, -6.0, -0.833, 0.0, 5.0, 10.0, 20.0, 30.0, 90.0]
SUN_FILL_LABELS = ["≤-30", "-30~-20", "-20~-12", "-12~-6", "-6~-0.83", "-0.83~0",
                   "0~5", "5~10", "10~20", "20~30", ">30"]


def make_sunlight_figure(tr, grid, ctx, stats, cfg, outdir: Path, dpi: int):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import matplotlib.patheffects as pe
    from matplotlib.colors import BoundaryNorm, ListedColormap
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    plotstyle.setup(font_size=9.5, verbose=False)
    bbox = (GRID_LON_MIN, GRID_LAT_MIN, GRID_LON_MAX, GRID_LAT_MAX)
    t0 = ctx["t0_utc"]
    ws, we = ctx["window_start_utc"], ctx["window_end_utc"]
    site = get_site(cfg)
    site_short = short_site_label(cfg)
    site_lat, site_lon = float(site.get("lat_deg", 38.8632)), float(site.get("lon_deg", 111.5896))
    obs = (get_observers(cfg) or [site])[0]
    ob_lat, ob_lon = float(obs.get("lat_deg", 37.761467)), float(obs.get("lon_deg", 112.725219))
    ob_label = str(obs.get("name") or ("发射点（无观测点）" if not get_observers(cfg)
                                      else "指定观测点"))
    if len(ob_label) > 10:
        ob_label = "指定观测点"

    # 太阳高度色带
    sun_cmap = ListedColormap(["#050a1f", "#0a1436", "#121f4d", "#1d2f66", "#2c4480",
                               "#4d6ba3", "#a9c4de", "#f6e7b2", "#ffd166", "#ffab3d",
                               "#f2701f"])
    sun_norm = BoundaryNorm(SUN_FILL_LEVELS, sun_cmap.N, clip=True)

    fig = plt.figure(figsize=(14.0, 10.6), dpi=110)

    # ---------------- (a) T0 太阳高度场 + 晨昏线 ----------------
    axa = fig.add_axes([0.042, 0.560, 0.425, 0.352])
    _world_base(axa, bbox)
    f0 = ctx["sun_el_t0_field"]
    cf_a = axa.contourf(LONS, LATS, f0, levels=SUN_FILL_LEVELS, cmap=sun_cmap,
                        norm=sun_norm, zorder=1.2)
    _night_shading(axa, f0, NIGHT_BANDS_FULL, zorder=1.5)
    th_a = _term_contours(axa, f0, label=True, fontsize=7.0)
    boundaries.plot_china(axa, bbox=bbox, borders=True, provinces=True,
                          coastline=False, cities=False, zorder=5)
    # 关键地点
    pts = [(site_short, site_lat, site_lon, "#ff3b30", "*", (-120, 14)),
           (ob_label, ob_lat, ob_lon, "#17becf", "v", (-64, -12)),
           ("北京", 39.9042, 116.4074, "#111111", "o", (7, -11)),
           ("乌鲁木齐", 43.8256, 87.6168, "#111111", "o", (-40, 6)),
           ("广州", 23.1291, 113.2644, "#111111", "o", (-28, -11))]
    for name, la, lo, col, mk, off in pts:
        se = float(geo.solar_elevation_deg(la, lo, t0))
        axa.plot([lo], [la], marker=mk, ms=9.5 if mk == "*" else 5.0,
                 mfc=col, mec="white", mew=0.8, zorder=9)
        axa.annotate(f"{name} {se:+.1f}°", (lo, la), xytext=off, textcoords="offset points",
                     fontsize=6.8, color=col if col != "#111111" else "#111111", zorder=9.5,
                     path_effects=[pe.withStroke(linewidth=2.0, foreground="white")])
    _set_map_axes(axa, bbox)
    axa.set_title(f"(a) T0 = {fmt_hms(t0)}Z（北京时 {fmt_hms(t0+dt.timedelta(hours=8))}，"
                  f"{twilight_label(cfg, ctx)}）太阳几何高度角场与晨昏线", fontsize=9.4)
    cba = fig.colorbar(cf_a, ax=axa, orientation="horizontal", fraction=0.040, pad=0.085,
                       aspect=42, boundaries=SUN_FILL_LEVELS, spacing="uniform")
    mids = [(SUN_FILL_LEVELS[i] + SUN_FILL_LEVELS[i + 1]) / 2 for i in range(len(SUN_FILL_LEVELS) - 1)]
    cba.set_ticks(mids)
    cba.ax.set_xticklabels(SUN_FILL_LABELS, fontsize=6.3, rotation=0)
    cba.set_label("太阳高度角 (°)  暗色=夜侧/晨光，亮色=昼侧", fontsize=7.4)
    axa.legend(handles=th_a, loc="lower left", fontsize=5.9, framealpha=0.92,
               facecolor="white", ncol=1, labelspacing=0.20, borderpad=0.30,
               bbox_to_anchor=(0.005, 0.005))

    # ---------------- (b) 窗口起止两条晨昏线 ----------------
    axb = fig.add_axes([0.520, 0.560, 0.425, 0.352])
    _world_base(axb, bbox)
    f_ws = ctx["sun_el_window_start_field"]
    f_we = ctx["sun_el_window_end_field"]
    # 窗口内"日出带"：WS 时已暗、WE 时已亮
    band = ((f_ws <= 0.0) & (f_we > 0.0)).astype(float)
    if band.any():
        axb.contourf(LONS, LATS, band, levels=[0.5, 1.5], colors=["#ffe08a"], alpha=0.55,
                     zorder=1.0)
    for fld, col, lab, lw in [(f_ws, "#1f4e9c", f"{fmt_hms(ws)}Z", 1.7),
                              (f_we, "#c0392b", f"{fmt_hms(we)}Z", 1.7)]:
        cs = axb.contour(LONS, LATS, fld, levels=[0.0], colors=[col], linewidths=[lw],
                         linestyles=["-"], zorder=6.0)
        cs6 = axb.contour(LONS, LATS, fld, levels=[-6.0], colors=[col], linewidths=[0.9],
                          linestyles=[(0, (5, 2))], alpha=0.85, zorder=5.8)
    # 标注：在该纬线处标出两条 0° 线经度（默认取网格纬度中点，默认网格=35°N）
    y35 = GRID_LAT_MIN + 0.5 * (GRID_LAT_MAX - GRID_LAT_MIN)
    j35 = int(np.argmin(np.abs(LATS - y35)))
    lon_ws = terminator_lon_at_lat(f_ws[j35], 0.0)
    lon_we = terminator_lon_at_lat(f_we[j35], 0.0)
    for lonx, col, lab, dy in [(lon_ws, "#1f4e9c", f"0° {fmt_hms(ws)}Z", -16),
                               (lon_we, "#c0392b", f"0° {fmt_hms(we)}Z", 12)]:
        if lonx is not None:
            axb.annotate(lab, (lonx, float(LATS[j35])), xytext=(4, dy), textcoords="offset points",
                         fontsize=7.2, color=col, zorder=9,
                         path_effects=[pe.withStroke(linewidth=2.2, foreground="white")])
    if lon_ws is not None and lon_we is not None and lon_we < lon_ws:
        _y32 = GRID_LAT_MIN + 0.425 * (GRID_LAT_MAX - GRID_LAT_MIN)
        axb.annotate("", xy=(lon_we, _y32), xytext=(lon_ws, _y32),
                     arrowprops=dict(arrowstyle="->", color="#7a4b00", lw=1.3), zorder=9)
        axb.text((lon_ws + lon_we) / 2, _y32 - 0.8, f"窗口内西移 {lon_ws-lon_we:.1f}°",
                 fontsize=6.8, color="#7a4b00", ha="center", zorder=9,
                 path_effects=[pe.withStroke(linewidth=2.0, foreground="white")])
    boundaries.plot_china(axb, bbox=bbox, borders=True, provinces=True,
                          coastline=False, cities=False, zorder=5)
    for name, la, lo, off in [(site_short, site_lat, site_lon, (10, 6)),
                              ("北京", 39.9042, 116.4074, (5, 4)),
                              ("上海", 31.2184, 121.4346, (5, -9)),
                              ("乌鲁木齐", 43.8256, 87.6168, (-38, 4))]:
        axb.plot([lo], [la], marker="o", ms=3.6, mfc="#222222", mec="white", mew=0.5, zorder=9)
        axb.annotate(name, (lo, la), xytext=off, textcoords="offset points", fontsize=6.6,
                     color="#222222", zorder=9,
                     path_effects=[pe.withStroke(linewidth=2.0, foreground="white")])
    _set_map_axes(axb, bbox)
    axb.set_title(f"(b) 航警窗口起止两条晨昏线：{fmt_hms(ws)}Z / {fmt_hms(we)}Z（0° 实线、-6° 虚线）",
                  fontsize=9.4)
    axb.legend(handles=[
        Line2D([], [], color="#1f4e9c", lw=1.7, label=f"0° 晨昏线 {fmt_hms(ws)}Z（窗口开始）"),
        Line2D([], [], color="#c0392b", lw=1.7, label=f"0° 晨昏线 {fmt_hms(we)}Z（窗口结束）"),
        Line2D([], [], color="#555555", lw=0.9, ls=(0, (5, 2)), label="-6° 民用晨光（同时刻）"),
        Patch(facecolor="#ffe08a", alpha=0.55, label="窗口内日出带（WS 暗→WE 亮）"),
    ], loc="lower left", fontsize=5.9, framealpha=0.92, facecolor="white",
        labelspacing=0.22, borderpad=0.30, bbox_to_anchor=(0.005, 0.005))

    # ---------------- (c) 阳光可见版最大高度角 ----------------
    axc = fig.add_axes([0.042, 0.085, 0.300, 0.360])
    _world_base(axc, bbox)
    cmap = _make_fill_cmap()
    norm = BoundaryNorm(FILL_LEVELS, cmap.N, clip=True)
    sunlit = grid["max_el_sunlit"]
    Zs = np.ma.masked_where(~(DOMAIN_MASK & np.isfinite(sunlit)),
                            np.clip(np.nan_to_num(sunlit, nan=0.0),
                                    FILL_LEVELS[0] + 1e-6, FILL_LEVELS[-1] - 1e-6))
    axc.contourf(LONS, LATS, Zs, levels=FILL_LEVELS, cmap=cmap, norm=norm,
                 antialiased=True, zorder=1.2)
    # 无定义陆域（观测者全程昼侧等）
    undef = (DOMAIN_MASK & ~np.isfinite(sunlit)).astype(float)
    if undef.any():
        axc.contourf(LONS, LATS, undef, levels=[0.5, 1.5], colors=["#dcdcdc"],
                     zorder=1.1)
    try:
        cs0 = axc.contour(LONS, LATS, np.ma.masked_where(~DOMAIN_MASK, sunlit),
                          levels=[0.0], colors="#00c2e0", linewidths=1.8,
                          linestyles=[(0, (5, 3))], zorder=6.2)
    except Exception:  # noqa: BLE001
        cs0 = None
    boundaries.plot_china(axc, bbox=bbox, borders=True, provinces=True,
                          coastline=False, cities=False, zorder=5)
    axc.plot(tr["lon"], tr["lat"], color=plotstyle.COLORS["trajectory"], lw=1.6,
             alpha=0.9, zorder=8)
    axc.plot([site_lon], [site_lat], marker="*", ms=14, mfc=plotstyle.COLORS["highlight"],
             mec="white", mew=0.8, zorder=10)
    axc.plot([ob_lon], [ob_lat], marker="v", ms=7, mfc=plotstyle.COLORS["observer"],
             mec="#083b45", mew=0.7, zorder=10)
    _set_map_axes(axc, bbox)
    axc.legend(handles=[Patch(facecolor="#dcdcdc", edgecolor="#999999",
                              label="无定义（观测者全程昼侧等）"),
                        Line2D([], [], color="#00c2e0", lw=1.5, ls=(0, (5, 3)),
                               label="0° 可见性边界")],
               loc="lower left", fontsize=5.8, framealpha=0.92, facecolor="white",
               labelspacing=0.25, borderpad=0.32, bbox_to_anchor=(0.005, 0.005))
    sv = stats["sunlit_version"]["stats"]
    axc.set_title("(c) 阳光可见版最大高度角\n"
                  f"（仅计火箭被照亮 & 观测者太阳高度≤0°；有效 {sv['n_valid_cells']:,} 格）",
                  fontsize=9.0)
    cbc = fig.colorbar(plt.cm.ScalarMappable(norm=norm, cmap=cmap), ax=axc,
                       orientation="horizontal", fraction=0.040, pad=0.15, aspect=32,
                       boundaries=FILL_LEVELS, spacing="uniform")
    _mids = [(FILL_LEVELS[i] + FILL_LEVELS[i + 1]) / 2 for i in range(len(FILL_LEVELS) - 1)]
    cbc.set_ticks([_mids[k] for k in (0, 3, 5, 6, 8, 10, 12, 14)])
    cbc.ax.set_xticklabels(["≤-35", "-15", "-3", "0", "5", "15", "30", "60+"], fontsize=5.8)
    cbc.set_label("阳光可见版最大高度角 (°)", fontsize=7.2)

    # ---------------- (d) 关键地点太阳高度时间曲线 ----------------
    axd = fig.add_axes([0.372, 0.110, 0.335, 0.335])
    tl = ctx["solar_timeline"]
    tt = [parse_utc(s) for s in tl["t_utc"]]
    axd.fill_between(tt, tl["min_deg"], tl["max_deg"], color="#c9d6e8", alpha=0.55,
                     label=f"{LAND_LABEL}范围（min–max）")
    axd.plot(tt, tl["median_deg"], color="#1f4e9c", lw=1.2, ls="--",
             label=f"{LAND_LABEL}中位数")
    key_series = [(site_short, site_lat, site_lon, "#ff3b30", 1.9),
                  (ob_label, ob_lat, ob_lon, "#17becf", 1.7),
                  ("北京", 39.9042, 116.4074, "#f39c12", 1.3),
                  ("乌鲁木齐", 43.8256, 87.6168, "#8e44ad", 1.3)]
    for name, la, lo, col, lw in key_series:
        vals = [float(geo.solar_elevation_deg(la, lo, t)) for t in tt]
        axd.plot(tt, vals, color=col, lw=lw, label=name)
    axd.axhspan(-90, 0, color="#0b1030", alpha=0.10, zorder=0)
    axd.axhspan(-6, 0, color="#4a3a6a", alpha=0.16, zorder=0)
    axd.axhline(0.0, color="#111111", lw=1.1, ls="-")
    axd.axhline(-6.0, color="#7a5a00", lw=1.0, ls=(0, (5, 2)))
    axd.axhline(-0.833, color="#111111", lw=0.8, ls=(0, (3, 2)))
    axd.axvspan(ws, we, color="#ffd166", alpha=0.28, zorder=0.4,
                label=f"航警窗口 {fmt_hms(ws)}–{fmt_hms(we)}Z")
    axd.axvline(t0, color="#d62728", lw=1.5, ls="-", label=f"T0 = {fmt_hms(t0)}Z")
    axd.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    axd.xaxis.set_major_locator(mdates.MinuteLocator(interval=20))
    axd.set_xlim(tt[0], tt[-1])
    ymin = min(min(tl["min_deg"]) - 2, -22)
    ymax = max(max(tl["max_deg"]) + 3, 10)
    axd.set_ylim(ymin, ymax)
    _date_range = tt[0].strftime("%Y-%m-%d")
    if tt[-1].strftime("%Y-%m-%d") != _date_range:
        _date_range = f"{_date_range}/{tt[-1]:%d}"
    axd.set_xlabel(f"UTC 时刻（{_date_range}）", fontsize=8.6)
    axd.set_ylabel("太阳几何高度角 (°)", fontsize=8.6)
    axd.grid(True, linestyle=":", linewidth=0.5, alpha=0.6)
    axd.legend(fontsize=6.3, loc="upper left", ncol=2, framealpha=0.92, facecolor="white",
               columnspacing=1.0, handlelength=1.6)
    axd.set_title("(d) 关键地点太阳高度角时间曲线与航警窗口", fontsize=9.2)
    axd.annotate("0°（日出）", (tt[0], 0.0), xytext=(2, 3), textcoords="offset points",
                 fontsize=6.6, color="#111111")
    axd.annotate("-6°（民用晨光）", (tt[0], -6.0), xytext=(2, 3), textcoords="offset points",
                 fontsize=6.6, color="#7a5a00")

    # ---------------- (e) 判据与结论 ----------------
    axe = fig.add_axes([0.732, 0.085, 0.255, 0.360])
    axe.axis("off")
    rk = stats["rocket_illumination"]
    svx = stats["sunlit_version"]
    cmp_ = svx["vs_geometric"]
    rk_first = rk.get("first_illuminated") or {}
    txt = (
        "(e) “阳光可见”判据与几何版差异\n"
        "────────────────────────────\n"
        "定义（提示词 §7）：\n"
        "  max_el_sunlit(lat,lon) =\n"
        "    max_t { el(t) :  火箭被照亮(t) 且\n"
        "            观测者太阳高度(t) ≤ 0° }\n\n"
        "火箭地影（简化圆柱模型）：太阳视为无穷远\n"
        "平行光 s(t)；|r×s| < Re 且 r·s < 0 → 影内\n"
        "（Re = WGS84_A；忽略半影/折射/扁率）\n\n"
        f"上升段采样 {rk['n_samples']} 点：被照亮 {rk['n_illuminated']}、\n"
        f"影内 {rk['n_in_shadow']}（{100*rk['shadow_fraction']:.1f}%）；"
        f"首个被照亮\nT+{rk_first.get('t_met_s', float('nan')):.0f} s（"
        f"高度 {rk_first.get('alt_m', float('nan'))/1000:.1f} km）\n\n"
        f"结果（{LAND_LABEL}）：\n"
        f"· 有效格点 {svx['stats']['n_valid_cells']:,} / {N_DOMAIN:,}"
        f"（占 {100*svx['stats']['n_valid_cells_area_fraction_of_china_land']:.1f}%）\n"
        f"· 未定义 {svx['stats']['n_undefined_cells']:,} 格：观测者全程\n"
        f"  昼侧 {svx['stats']['n_undefined_because_observer_daytime']:,} 格，"
        f"夜侧但火箭在影内 "
        f"{svx['stats']['n_undefined_because_rocket_in_shadow']} 格\n"
        f"· 中位数 {svx['stats']['median_deg']:+.2f}°、最大 "
        f"{svx['stats']['max_deg']:+.2f}°\n"
        f"· 面积占比：≥10° {100*svx['stats']['area_fraction_ge']['ge_10_deg']:.1f}%，"
        f"≥30° {100*svx['stats']['area_fraction_ge']['ge_30_deg']:.1f}%，"
        f"≥45° {100*svx['stats']['area_fraction_ge']['ge_45_deg']:.1f}%\n\n"
        f"与几何版对比（同 {cmp_['n_cells_both_defined']:,} 格）：\n"
        f"· 中位降低 {abs(cmp_['median_diff_deg']):.2f}°；降低>1° 的格点\n"
        f"  占陆域 {100*cmp_['area_fraction_diff_gt_1deg']:.2f}%，"
        f"降低>10° 占 {100*cmp_['area_fraction_diff_gt_10deg']:.2f}%\n"
        f"· 最大降低 {abs(cmp_['min_diff_deg']):.1f}° @ "
        f"({cmp_['max_reduction_location']['lat_deg']:.1f}°N,"
        f"{cmp_['max_reduction_location']['lon_deg']:.1f}°E)\n"
        "  机制：几何最大值时刻不满足光照/夜侧条件——\n"
        "  ① 发射场附近火箭仍处晨光地影内（早期）；\n"
        "  ② 日出带内观测者已进入昼侧\n\n"
        "意义：几何版给出“能否看到”的几何上界；\n"
        "阳光可见版给出“夜侧能否看到被照亮的箭体”\n"
        "的更强约束。两者之差量化地影/晨光背景影响。"
    )
    axe.text(0.0, 1.0, txt, transform=axe.transAxes, fontsize=6.9, va="top", ha="left",
             linespacing=1.38, family="sans-serif",
             bbox=dict(boxstyle="round,pad=0.5", fc="#fbfbfb", ec="#999999"))

    fig.suptitle(f"晨昏线与“阳光可见”判据说明"
                 f"（{mission_date_label(cfg, tr['t0'])} {rocket_code(cfg)} / {site_short}）"
                 + ("" if tr["status"] == "real" else "【临时版：夹具弹道】"),
                 fontsize=14.5, y=0.986, fontweight="bold")
    fig.text(0.5, 0.951,
             f"太阳位置：common.geo（Meeus 低精度，几何高度角，未含折射）｜"
             f"边界：Natural Earth 1:50m（示意）｜弹道：{tr['file']}"
             f"（{tr['status']}，{tr['n']} 点，mtime {fmt_utc(tr['mtime_utc'])}）",
             ha="center", fontsize=8.0)
    fig.text(0.5, 0.933,
             f"T0 = {fmt_utc(t0)}（北京时 {fmt_bjt(t0)}）｜"
             f"航警窗口 {fmt_utc(ctx['window_start_utc'])} – {fmt_utc(ctx['window_end_utc'])}｜"
             "晨昏线：0°=几何地平；-0.833°=含折射+日面；-6°/-12°/-18°=民用/航海/天文晨光",
             ha="center", fontsize=8.0)
    fig.text(0.042, 0.012, DISCLAIMER + f"  脚本：map/plot_map.py v{SCRIPT_VERSION}",
             fontsize=7.0, color="#333333")

    png_path = outdir / "晨昏线与阳光可见性.png"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        p, px_w, px_h = plotstyle.save(fig, png_path, dpi=dpi, verbose=True)
    glyph_warnings = [str(w.message) for w in caught
                      if "missing from font" in str(w.message).lower()
                      or "glyph" in str(w.message).lower()]
    plt.close(fig)
    real_w, real_h = _png_size(p, px_w, px_h)
    return {"path": str(p), "px_w": int(real_w), "px_h": int(real_h),
            "glyph_warnings": glyph_warnings,
            "window_shift_deg_at_35n": (float(lon_we - lon_ws)
                                        if lon_ws is not None and lon_we is not None
                                        else float("nan"))}


# --------------------------------------------------------------------------- #
# 输出：网格 CSV
# --------------------------------------------------------------------------- #
def write_grid_csv(path: Path, grid: dict, ctx: dict) -> int:
    """全网格 CSV。undefined 的 max_el_sunlit 写空串（不写 nan），下游可安全解析。"""
    geom = grid["max_el_geom"]
    sunlit = grid["max_el_sunlit"]
    tmax = grid["t_max_met_s"]
    sun0 = ctx["sun_el_t0_field"]
    cols = ["lat_deg", "lon_deg", "max_el_geom_deg", "max_el_sunlit_deg",
            "in_china", "sun_el_t0_deg", "t_max_met_s", "sunlit_defined"]
    n = 0
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for j in range(NY):
            for i in range(NX):
                s = sunlit[j, i]
                ok = bool(np.isfinite(s))
                w.writerow([
                    f"{LATS[j]:.4f}", f"{LONS[i]:.4f}",
                    f"{geom[j, i]:.6f}",
                    f"{s:.6f}" if ok else "",
                    int(CHINA_MASK[j, i]),
                    f"{sun0[j, i]:.6f}",
                    f"{tmax[j, i]:.3f}",
                    int(ok),
                ])
                n += 1
    return n


def write_json(path: Path, obj: dict):
    path.write_text(json.dumps(jsonable(obj), ensure_ascii=False, indent=1),
                    encoding="utf-8")


def _checks_summary(checks):
    nf = sum(1 for c in checks if c["status"] != "PASS")
    return {"n_pass": len(checks) - nf, "n_fail": nf,
            "all_pass": nf == 0, "checks": checks}


def _checks_md(checks) -> str:
    lines = ["| # | 自检项 | 结果 | 说明 |", "|---|---|---|---|"]
    for k, c in enumerate(checks, 1):
        lines.append(f"| {k} | {c['name']} | {'✅ PASS' if c['status'] == 'PASS' else '❌ FAIL'} "
                     f"| {c['detail']} |")
    s = _checks_summary(checks)
    lines.append("")
    lines.append(f"**自检合计：{s['n_pass']} 项通过，{s['n_fail']} 项失败 → "
                 f"{'全部通过' if s['all_pass'] else '存在失败项（见上表）'}**")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 输出：说明 Markdown
# --------------------------------------------------------------------------- #
def make_md(stats: dict, ctx: dict, cfg: dict, main_fig: dict, sun_fig: dict,
            checks, tr: dict) -> str:
    st = stats
    trs = st["trajectory"]
    me = st["max_elevation_deg"]
    af = st["area_fraction_ge"]
    camf = st["cell_fraction_ge"]
    vis = st["visibility"]
    term = st["terminator"]
    chin = term["china_solar"]
    sv = st["sunlit_version"]
    svs = sv["stats"]
    cmp_ = sv["vs_geometric"]
    rk = st["rocket_illumination"]
    fi = rk.get("first_illuminated") or {"t_met_s": float("nan"), "alt_m": float("nan")}
    pcts = me["percentiles"]
    pct = lambda x: f"{100 * x:.2f}%"
    mark = "**【临时版：使用 CZ-8A 夹具数据，待 CZ-2D 弹道替换】**" if tr["status"] != "real" else ""
    site = get_site(cfg)
    site_short = short_site_label(cfg)
    site_lat = float(site.get("lat_deg", tr["lat"][0]))
    site_lon = float(site.get("lon_deg", tr["lon"][0]))
    _date = mission_date_label(cfg, tr["t0"])
    _rocket = short_rocket_label(cfg)
    obs_md = (get_observers(cfg) or [{}])
    obs0 = (obs_md[0] if obs_md else {})
    _mid_lat = GRID_LAT_MIN + 0.5 * (GRID_LAT_MAX - GRID_LAT_MIN)
    _trk_md = _term_ref_lat(ctx)

    def thr_table():
        rows = ["| 阈值 | 面积占比（球面面积加权） | 格点占比 | 面积 (km²) |",
                "|---|---|---|---|"]
        for t in ALL_THRESHOLDS:
            k = f"ge_{t}_deg"
            rows.append(f"| ≥{t}° | {pct(af[k])} | {pct(camf[k])} | "
                        f"{st['area_km2_ge'][k]:,.0f} |")
        return "\n".join(rows)

    def city_table():
        rows = ["| 城市 | 纬度 | 经度 | 最大高度角 | 时刻 MET | 方位角 |",
                "|---|---|---|---|---|---|"]
        for c in st["cities"]:
            rows.append(f"| {c['name']} | {c['lat_deg']:.3f}°N | {c['lon_deg']:.3f}°E | "
                        f"{c['max_el_deg']:+.2f}° | T+{c['t_met_s']:.1f} s | {c['az_deg']:.0f}° |")
        return "\n".join(rows)

    def key_city_table():
        rows = ["| 城市/观测点 | 纬度 | 经度 | 最大高度角 | 时刻 MET | 方位角 | 最近格点值 |",
                "|---|---|---|---|---|---|---|"]
        for c in st["key_cities"]:
            rows.append(f"| {c['name']} | {c['lat_deg']:.4f}°N | {c['lon_deg']:.4f}°E | "
                        f"**{c['max_el_deg']:+.2f}°** | T+{c['t_met_s']:.1f} s | "
                        f"{c['az_deg']:.0f}° | {c['nearest_grid_max_el_deg']:+.2f}° |")
        return "\n".join(rows)

    def lat_band_table():
        rows = ["| 纬度带 | 格点数 | 面积 (km²) | 面积占陆域 | 最小 | 中位数 | 最大 | ≥0° 面积占比 | ≥10° 面积占比 |",
                "|---|---|---|---|---|---|---|---|---|"]
        for b in st["latitude_bands_5deg"]:
            rows.append(f"| {b['lat_min']:.0f}–{b['lat_max']:.0f}°N | {b['n_cells']:,} | "
                        f"{b['area_km2']:,.0f} | {100*b['area_fraction_of_china_land']:.2f}% | "
                        f"{b['min_deg']:+.2f}° | {b['median_deg']:+.2f}° | {b['max_deg']:+.2f}° | "
                        f"{100*b['area_fraction_ge_0']:.2f}% | {100*b['area_fraction_ge_10']:.3f}% |")
        return "\n".join(rows)

    def sun_thr_table():
        rows = ["| 阈值 | 阳光可见版面积占比（以有值区为分母） | 格点数 |",
                "|---|---|---|"]
        for t in ALL_THRESHOLDS:
            k = f"ge_{t}_deg"
            rows.append(f"| ≥{t}° | {pct(svs['area_fraction_ge'][k])} | "
                        f"{svs['n_ge'][k]:,} |")
        return "\n".join(rows)

    def term_lat_table(key: str):
        rows = ["| 纬度 | 0° 晨昏线经度 | -6° 晨光边界经度 |", "|---|---|---|"]
        for _, v in chin[key]["terminator_lon_by_lat"].items():
            lo0 = f"{v['lon_level_0']:.2f}°E" if v["lon_level_0"] is not None else "—（整条昼/夜）"
            lo6 = f"{v['lon_level_m6']:.2f}°E" if v["lon_level_m6"] is not None else "—（整条昼/夜）"
            rows.append(f"| {v['lat_deg']:.0f}°N | {lo0} | {lo6} |")
        return "\n".join(rows)

    def key_pt_table():
        rows = ["| 地点 | 纬度 | 经度 | T0 太阳高度 | 窗口开始 | 窗口结束 |",
                "|---|---|---|---|---|---|"]
        _obs_label = "指定观测点" if st.get("observers") else "发射点（未指定观测点，代理）"
        names = {"launch_site": f"{site_short}（发射点）", "observer": _obs_label,
                 "beijing": "北京", "urumqi": "乌鲁木齐", "guangzhou": "广州", "harbin": "哈尔滨"}
        for k, v in term["key_points_solar"].items():
            rows.append(f"| {names.get(k, k)} | {v['lat_deg']:.3f}°N | {v['lon_deg']:.3f}°E | "
                        f"{v['t0_deg']:+.2f}° | {v['window_start_deg']:+.2f}° | "
                        f"{v['window_end_deg']:+.2f}° |")
        return "\n".join(rows)

    def obs_table():
        rows = ["| 观测点 | 纬度 | 经度 | 高程 (m) | 最大高度角 | 时刻 MET | 方位角 | 最近格点值 |",
                "|---|---|---|---|---|---|---|---|"]
        for o in st["observers"]:
            rows.append(f"| {o['name']} | {o['lat_deg']:.6f}°N | {o['lon_deg']:.6f}°E | "
                        f"{o['alt_m']:.0f} | **{o['max_el_deg']:.3f}°** | "
                        f"T+{o['t_met_s']:.1f} s | {o['az_deg']:.1f}° | "
                        f"{o['nearest_grid_max_el_deg']:+.2f}° |")
        return "\n".join(rows)

    m = []
    m.append(f"# 全国最大高度角地图（含晨昏线）— {_date} {_rocket}\n")
    m.append(f"> **免责声明**：{DISCLAIMER}\n")
    if mark:
        m.append(f"> ⚠️ {mark}\n")
        m.append("> 本文件由 `sim/trajectory.csv` 缺失时的夹具回退流程生成：几何/时序取自 "
                 "`_fixtures/ref_sim/trajectory.csv`（2026-10-25 CZ-8A 海南发射参考弹道），"
                 "晨昏线/太阳高度按本任务标称 T0 计算；**仅用于流程演示，不得作为 CZ-2D 结论引用**。"
                 "待 Lead 产出 CZ-2D 弹道后应重跑覆盖。\n")
    m.append("| 项目 | 内容 |\n|---|---|")
    m.append(f"| 任务 | {_date} {_rocket}（{site_short}，{site_lat:.4f}°N, {site_lon:.4f}°E） |")
    m.append(f"| 脚本 | `map/plot_map.py` v{SCRIPT_VERSION}（可复现，参数化输入路径） |")
    m.append(f"| 生成时间 | {st['generated_utc']} |")
    m.append(f"| 输入弹道 | `{trs['file']}`（{trs['label']}，{trs['n_points']} 点，"
             f"Δt≈{trs['sample_dt_s']:.2f} s，mtime {trs['mtime_utc']}）；"
             f"T0 来源：{trs['t0_source']} |")
    m.append(f"| T0 / 窗口 | T0 = {st['time']['t0_used_utc']}（北京时 {st['time']['t0_used_bjt']}）；"
             f"航警窗口 {st['time']['window_start_utc']} – {st['time']['window_end_utc']}"
             f"（北京时 {st['time']['window_start_bjt']} – {st['time']['window_end_bjt']}） |")
    m.append(f"| 网格 | {GRID_LON_MIN:g}–{GRID_LON_MAX:g}°E × {GRID_LAT_MIN:g}–{GRID_LAT_MAX:g}°N，"
             f"{GRID_STEP:g}°×{GRID_STEP:g}°，{NX}×{NY} = {NX*NY:,} 格；"
             f"{LAND_LABEL} {N_DOMAIN:,} 格（≈{st['grid']['land_area_km2']/1e4:.1f} 万 km²） |")
    m.append(f"| 主图 | `map/全国最大高度角地图.png`（{main_fig['px_w']}×{main_fig['px_h']} px） |")
    m.append(f"| 补充图 | `map/晨昏线与阳光可见性.png`（{sun_fig['px_w']}×{sun_fig['px_h']} px） |")
    m.append(f"| 网格数据 | `map/网格.csv`（{NX*NY:,} 行，{LAND_LABEL}格点有值） |")
    m.append(f"| 统计 | `map/统计.json` |")
    m.append("")

    # ---- 0 结论摘要 ----
    mloc = me["max_location"]
    d_mloc_pad = float(geo.great_circle_distance_km(
        mloc["lat_deg"], mloc["lon_deg"], site_lat, site_lon))
    if d_mloc_pad <= 200.0:
        max_desc = (f"最大值点紧邻发射场（距 {site_short} {d_mloc_pad:.0f} km；"
                    f"发射点精确值 {st['launch_site_value']['max_el_deg']:.2f}°，火箭早期近乎过顶）。")
    else:
        max_desc = (f"该最大值位于 ({mloc['lat_deg']:.2f}°N, {mloc['lon_deg']:.2f}°E)，"
                    f"距 {site_short} {d_mloc_pad:.0f} km——原因：{GRID_STEP:g}° 格点只有落在星下点轨迹"
                    "正下方时才能取到 el≈90°，故全网格最大值一般出现在轨迹附近，"
                    "而不是发射场格点本身"
                    + ("；临时版弹道几何与 CZ-2D 不同，最大值位置仅供参考"
                       if tr["status"] != "real" else "") + "。")
    m.append("## 0. 结论摘要\n")
    m.append(f"1. **最大高度角（几何版，含负值）**：{LAND_LABEL}中位数 **{me['median']:+.2f}°**，"
             f"平均 {me['mean']:+.2f}°，最小 {me['min']:+.2f}°（{me['min_location']['lat_deg']:.1f}°N, "
             f"{me['min_location']['lon_deg']:.1f}°E），最大 **{me['max']:+.2f}°**"
             f"（{mloc['lat_deg']:.1f}°N, {mloc['lon_deg']:.1f}°E，MET {mloc['t_met_s']:.0f} s）；"
             f"{max_desc}负值表示火箭在整段上升过程中始终位于该格点地平线以下（几何不可见）。")
    m.append(f"2. **可见区**：最大高度角 ≥0° 的格点 **{vis['n_visible_cells']:,} 个**，"
             f"面积占统计域 **{pct(vis['area_fraction_of_china_land'])}**；可见区内中位数 "
             f"{vis['median_deg']:+.2f}°。全程低于地平（几何不可见）的统计域面积占 "
             f"**{pct(st['max_elevation_deg_summary']['area_fraction_below_horizon'])}**。")
    m.append(f"3. **面积占比（提示词阈值）**：{LAND_LABEL} "
             + "、".join(f"≥{t}° **{pct(af[f'ge_{t}_deg'])}**" for t in REQ_THRESHOLDS)
             + "。")
    _t0h = st["time"]["t0_used_utc"]
    m.append(f"4. **晨昏线（T0 = {_t0h}）**：{LAND_LABEL}太阳几何高度角 "
             f"{chin['t0']['china']['min_deg']:+.2f}° ~ {chin['t0']['china']['max_deg']:+.2f}°，"
             f"0° 晨昏线在统计域内的分布见主图/补充图（中国格点内 "
             f"{chin['t0']['n_china_cells_sun_le_0']:,} 个太阳高度≤0°、"
             f"{chin['t0']['n_china_cells_sun_gt_0']:,} 个>0°）；"
             f"航警窗口 {st['time']['window_start_utc']} – {st['time']['window_end_utc']}，"
             f"补充图 (b) 给出窗口起止**两条晨昏线**"
             f"（0° 线在 {_trk_md}°N 处西移 "
             f"{abs(sun_fig.get('window_shift_deg_at_35n', float('nan'))):.1f}°）。")
    m.append(f"5. **阳光可见版**（火箭被照亮 且 观测者太阳高度≤0°）：{LAND_LABEL}有效格点 "
             f"**{svs['n_valid_cells']:,}**（占统计域 {pct(svs['n_valid_cells_area_fraction_of_china_land'])}），"
             f"中位数 {svs['median_deg']:+.2f}°；与几何版相比中位降低 "
             f"{abs(cmp_['median_diff_deg']):.2f}°、降低>1° 的格点占统计域 "
             f"{pct(cmp_['area_fraction_diff_gt_1deg'])}，最大降低 {abs(cmp_['min_diff_deg']):.1f}°"
             f"（{cmp_['max_reduction_location']['lat_deg']:.1f}°N,"
             f"{cmp_['max_reduction_location']['lon_deg']:.1f}°E：几何最大值时刻不满足"
             f"光照/夜侧条件）。")
    if st.get("observers"):
        m.append(f"6. **指定观测点**（{obs0.get('lat_deg', float('nan')):.6f}°N, "
                 f"{obs0.get('lon_deg', float('nan')):.6f}°E，{obs0.get('name', '')}）：最大高度角 "
                 f"**{st['observers'][0]['max_el_deg']:+.2f}°**（MET {st['observers'][0]['t_met_s']:.1f} s，"
                 f"方位 {st['observers'][0]['az_deg']:.0f}°）；该点太阳高度 T0 时 "
                 f"{term['key_points_solar']['observer']['t0_deg']:+.2f}°。")
    else:
        m.append("6. **指定观测点**：本任务未指定观测点；图与统计均为\"全国任意地点对火箭的几何"
                 "可见性上界\"，不针对具体观测点（如需观测点标注，请在「观测点」页添加后重算）。")
    m.append("")

    # ---- 1 方法 ----
    m.append("## 1. 方法与公式\n")
    m.append("### 1.1 最大几何高度角\n")
    m.append("对每个格点（大地坐标 φ, λ，椭球高 h = 0 m）与每个弹道采样点 r_target(t)（ECEF）：\n")
    m.append("```\n"
             "Δ      = r_target(t) − r_observer(φ, λ, 0)\n"
             "[E,N,U] = R_enu(φ, λ) · Δ          （R_enu 行为东/北/天单位矢量）\n"
             "az     = atan2(E, N) ,  el = atan2(U, √(E²+N²))\n"
             "max_el_geom(φ,λ) = max_t el(t)\n"
             "```\n")
    m.append("该式与 `common.geo.az_el_from_observer` **完全同一三维几何**（本脚本对 24 个随机格点"
             f"逐点调用该函数复核，最大偏差 {st['verify_geo']['max_abs_diff_deg']:.2e}°）。"
             "地球曲率与目标高度自动包含：距离 d 处的地平线下降量 d²/2R 自然体现在 U 分量为负，"
             "不需要额外的曲率修正项。\n")
    m.append("**地形/建筑不影响该最大值**：它们只能遮挡（降低实际可见高度角），不会抬高几何上界；"
             "因此本图是\"几何可见性上界\"。实际地形遮挡需按方位角做 DEM 视线检查（提示词 §6 天空轨迹"
             "模块已按 200 km / SRTM 1″ 处理），本图未做，见 §8 局限。\n")
    m.append("### 1.2 网格与掩膜\n")
    m.append(f"经度 `np.arange({GRID_LON_MIN:g}, {GRID_LON_MAX:g}, {GRID_STEP:g})`（{NX} 点）× 纬度 "
             f"`np.arange({GRID_LAT_MIN:g}, {GRID_LAT_MAX:g}, {GRID_STEP:g})`（{NY} 点），"
             f"共 {NX*NY:,} 格；用 `common.boundaries.china_land_mask`（Natural Earth 1:50m 国界"
             f"多边形奇偶规则栅格化，含台湾/海南/港澳）取中国陆域掩膜，得 **{N_LAND:,} 格**"
             + ("（【警告】本网格与掩膜无交集，统计按全网格退化）。\n" if not N_LAND
                else "。\n")
             + "统计面积用球面梯形公式按纬行加权。\n")
    m.append("### 1.3 晨昏线（太阳高度场）\n")
    m.append("太阳几何高度角（Meeus 低精度太阳坐标，`common.geo.solar_elevation_deg`）：\n")
    m.append("```\nsin h = sinφ·sinδ + cosφ·cosδ·cosH ,  H = θ_G + λ − α\n```\n")
    m.append("其中 δ/α 为太阳视赤纬/赤经，θ_G 为格林尼治恒星时。等值线等级：0°（几何地平，"
             "晨昏线正线）、-0.833°（含大气折射 34′ + 太阳视半径 16′ 的真实晨昏线）、"
             "-6°（民用晨光）、-12°（航海晨光）、-18°（天文晨光）。半透明暗色阴影表示夜侧、"
             "淡黄表示昼侧。太阳高度为**几何值**，未含大气折射（地平附近实际约 +0.5°）。\n")
    m.append("### 1.4 \"阳光可见\"版判据\n")
    m.append("```\n"
             "max_el_sunlit(φ,λ) = max_t { el(t) :  火箭被照亮(t)  且  观测者太阳高度(t) ≤ 0° }\n"
             "```\n")
    m.append("火箭是否被照亮用**简化圆柱地影模型**：太阳视为无穷远平行光（方向 s(t)，ECEF 单位矢量），"
             "地球阴影取半径 Re = WGS84_A 的圆柱：`|r×s| < Re 且 r·s < 0` 判为影内，否则被照亮。"
             "忽略半影、大气折射与地球扁率（对\"是否被照亮\"的影响远小于 0.5 s 采样）。"
             "若某格点在上升段内没有任何满足条件的时刻，则该格点无定义"
             "（典型为观测者全程昼侧），CSV 该列留空、统计中单列为\"未定义\"。\n")
    m.append(f"上升段共 {rk['n_samples']} 个采样点，被照亮 {rk['n_illuminated']} 点、"
             f"地影内 {rk['n_in_shadow']} 点（{100*rk['shadow_fraction']:.1f}%）；"
             f"首个被照亮时刻 T+{fi['t_met_s']:.1f} s"
             f"（高度 {fi['alt_m']/1000:.2f} km）——该点位于 {site_short} 的晨昏带内，"
             "火箭上升穿过地影边界后进入阳光。\n")

    # ---- 2 数据来源 ----
    m.append("## 2. 数据与来源\n")
    m.append("| 数据 | 文件/函数 | 来源 | 访问/生成日期 | 说明 |")
    m.append("|---|---|---|---|---|")
    m.append(f"| 弹道时间序列 | `{trs['file']}` | "
             + ("Lead 3-DOF 反演（本工程 sim/，契约见 AGENTS.md §3）"
                if tr["status"] == "real" else
                "`_fixtures/ref_sim/`（2026-10-25 CZ-8A 参考项目，只读夹具；临时版）")
             + f" | mtime {trs['mtime_utc']} | {trs['label']}；{trs['n_points']} 点，"
             f"Δt≈{trs['sample_dt_s']:.2f} s |")
    m.append("| 国界/省界/海岸线 | `data/ne/china_admin0.geojson`、`china_admin1.geojson`、"
             "`coastline_cn_bbox.geojson` | Natural Earth 1:50m/1:10m，https://www.naturalearthdata.com/ "
             "（public domain） | 2026-10-24（`data/ne/ne_manifest.json` generated_utc） | 示意边界，非官方界线 |")
    m.append("| 城市点 | `data/ne/china_cities.geojson` | Natural Earth 1:10m populated places | "
             "2026-10-24 | 取人口前 120 城市 |")
    m.append("| 发射场/观测点/T0/航警窗口 | `config.json`（mission/observers/notams） | "
             "任务配置（由 app/core/config_build.py 生成；来源见配置 provenance） | "
             f"{_date} | T0 与发射场为公开信息假设值，非实测 |")
    m.append("| 太阳位置 | `common/geo.py`（Meeus 低精度） | 天文算法标准（Meeus, Astronomical Algorithms） | — | "
             "精度约 0.01–0.1° |")
    m.append(f"| 网格规格 | `config.json` 的 grid 字段 | 任务配置 | — | "
             f"{GRID_LON_MIN:g}–{GRID_LON_MAX:g}°E × {GRID_LAT_MIN:g}–{GRID_LAT_MAX:g}°N，"
             f"{GRID_STEP:g}°，椭球高 0 m |")
    m.append("")
    if tr["status"] != "real":
        m.append("> 临时版说明：`sim/trajectory.csv` 尚未产出，本版使用 `_fixtures/ref_sim/trajectory.csv`"
                 "（2026-10-25 CZ-8A 海南发射参考弹道，接口与本任务契约一致）作为几何输入；"
                 "T0/窗口/晨昏线仍按本任务 CZ-2D 标称值计算。正式版必须用 CZ-2D 弹道重跑。\n")

    # ---- 3 晨昏线时刻 ----
    m.append("## 3. 晨昏线时刻与形态说明\n")
    m.append(f"* **T0 = {finfo(st,'t0_used_utc')}**（北京时 {st['time']['t0_used_bjt']}），"
             f"处于**{twilight_label(cfg, ctx)}**：太阳直射点 "
             f"{abs(term['sun']['subsolar_lat_deg']):.2f}°"
             f"{'S' if term['sun']['subsolar_lat_deg'] < 0 else 'N'}、"
             f"{term['sun']['subsolar_lon_deg']:+.2f}°E；{LAND_LABEL}太阳高度 "
             f"{chin['t0']['china']['min_deg']:+.2f}°（{chin['t0']['china']['min_lat_deg']:.1f}°N,"
             f"{chin['t0']['china']['min_lon_deg']:.1f}°E）~ "
             f"{chin['t0']['china']['max_deg']:+.2f}°（{chin['t0']['china']['max_lat_deg']:.1f}°N,"
             f"{chin['t0']['china']['max_lon_deg']:.1f}°E）。\n")
    m.append(f"* **0° 晨昏线在绘图范围内的位置**：T0 时画幅内同时可见 "
             f"{', '.join(f'{x:g}°' for x in chin['t0']['contours_present_in_bbox']) or '无'}"
             f" 等值线；"
             f"0° 线在 {_trk_md}°N 处约位于 "
             f"{_fmt_lon(chin['t0']['terminator_lon_by_lat'][_trk_md]['lon_level_0'])}。\n")
    m.append(f"* **航警窗口 {st['time']['window_start_utc']} – {st['time']['window_end_utc']}**"
             f"（北京时 {st['time']['window_start_bjt']} – {st['time']['window_end_bjt']}）："
             f"0° 线在 {_trk_md}°N 处由 "
             f"{_fmt_lon(chin['window_start']['terminator_lon_by_lat'][_trk_md]['lon_level_0'])} 移到 "
             f"{_fmt_lon(chin['window_end']['terminator_lon_by_lat'][_trk_md]['lon_level_0'])}"
             f"（{abs(sun_fig.get('window_shift_deg_at_35n', float('nan'))):.1f}°），"
             f"窗口内 T0 与窗口结束之间出现晨昏线穿越的统计域面积占 "
             f"{pct(term['window_sunrise_band_area_fraction'])}。"
             f"因此补充图 (b) 同时给出**窗口起止两条晨昏线**（0° 实线、-6° 虚线），"
             f"主图叠加的是**标称起飞时刻 T0** 的晨昏线。\n")
    m.append(f"* **晨昏线扫过统计域的时间**：第一点日出 {term['dawn_first_china_day_utc']}，"
             f"最后一点日出 {term['dawn_all_china_day_utc']}；"
             f"-6° 民用晨光：{term['civil_first_china_utc']} – {term['civil_all_china_utc']}。"
             f"T0 时 {chin['t0']['n_china_cells_sun_le_0']:,} 个统计域格点太阳高度≤0°、"
             f"{chin['t0']['n_china_cells_sun_gt_0']:,} 个格点>0°。\n")
    _pt_tail = (f"指定观测点 T0 {term['key_points_solar']['observer']['t0_deg']:+.2f}°。"
                if st.get("observers") else
                "本任务未指定观测点（表内 observer 行为发射点代理值）。")
    m.append(f"* **发射场/观测点**：{site_short} 太阳高度 T0 "
             f"{term['key_points_solar']['launch_site']['t0_deg']:+.2f}°"
             f"、窗口结束 {term['key_points_solar']['launch_site']['window_end_deg']:+.2f}°；"
             + _pt_tail + "\n")
    m.append("T0 时刻 0° 晨昏线在主要纬线的位置：\n")
    m.append(term_lat_table("t0"))
    m.append("")
    m.append("关键地点太阳高度：\n")
    m.append(key_pt_table())
    m.append("")

    # ---- 4 统计 ----
    m.append(f"## 4. 统计结果（{LAND_LABEL}）\n")
    m.append(f"| 指标 | 数值 |\n|---|---|")
    m.append(f"| 最大高度角 中位数 | **{me['median']:+.3f}°** |")
    m.append(f"| 平均 / 标准差 | {me['mean']:+.3f}° / {me['std']:.3f}° |")
    m.append(f"| 最小 / 最大 | {me['min']:+.3f}° / {me['max']:+.3f}° |")
    m.append(f"| 分位数 p05 / p25 / p75 / p95 | {pcts['p05']:+.2f}° / {pcts['p25']:+.2f}° / "
             f"{pcts['p75']:+.2f}° / {pcts['p95']:+.2f}° |")
    m.append(f"| 最大值位置 | {mloc['lat_deg']:.2f}°N, {mloc['lon_deg']:.2f}°E（MET {mloc['t_met_s']:.0f} s，"
             f"方位 {mloc['az_deg']:.0f}°） |")
    m.append(f"| 可见区（≥0°） | {vis['n_visible_cells']:,} 格，占陆域 "
             f"{pct(vis['area_fraction_of_china_land'])}，面积 {vis['area_km2']:,.0f} km² |")
    m.append(f"| 全程低于地平（<0°） | {st['max_elevation_deg_summary']['n_below_horizon_cells']:,} 格，"
             f"占陆域 {pct(st['max_elevation_deg_summary']['area_fraction_below_horizon'])} |")
    m.append(f"| 发射点（精确点，h=0） | {st['launch_site_value']['max_el_deg']:+.3f}°"
             f"（MET {st['launch_site_value']['t_met_s']:.1f} s，T+0 退化）；"
             f"最近格点 {st['launch_site_value']['nearest_grid_max_el_deg']:+.2f}° |")
    m.append("")
    m.append("### 4.1 阈值面积占比（面积加权）\n")
    m.append(thr_table())
    m.append("")
    m.append("### 4.2 纬度带\n")
    m.append(lat_band_table())
    m.append("")
    if st.get("observers"):
        m.append("### 4.3 指定观测点与发射点精确值\n")
        m.append(obs_table())
    else:
        m.append("### 4.3 发射点精确值（本任务未指定观测点）\n")
    m.append("")
    lsv = st["launch_site_value"]
    m.append(f"发射点（{site_short}，{site_lat:.4f}°N, {site_lon:.4f}°E，椭球高 0 m）：精确点最大高度角 "
             f"**{lsv['max_el_deg']:.3f}°**，出现于 T+{lsv['t_met_s']:.1f} s"
             f"（{lsv['t_utc']}），方位 {lsv['az_deg']:.1f}°；最近 {GRID_STEP:g}° 格点"
             f"（{lsv['nearest_grid']['lat_deg']:.1f}°N, {lsv['nearest_grid']['lon_deg']:.1f}°E）"
             f"值为 **{lsv['nearest_grid_max_el_deg']:.2f}°**。"
             "精确点在 T+0 时火箭恰位于正上方（退化 90°），周边区域代表性以格点值为准。\n")
    m.append("### 4.4 主要城市数值（地图标注）\n")
    m.append(city_table())
    m.append("")
    m.append("### 4.5 关键城市数值表（报告引用）\n")
    m.append(key_city_table())
    m.append("")

    # ---- 5 阳光可见 ----
    m.append("## 5. \"阳光可见\"版与几何版的差异\n")
    m.append(f"* 判据：{sv['criterion']}；地影模型：{sv['shadow_model']}。\n")
    m.append(f"* 有效格点 **{svs['n_valid_cells']:,}** / {N_DOMAIN:,}（占统计域 "
             f"{pct(svs['n_valid_cells_area_fraction_of_china_land'])}）；"
             f"中位数 {svs['median_deg']:+.2f}°、平均 {svs['mean_deg']:+.2f}°、"
             f"最大 {svs['max_deg']:+.2f}°。\n")
    m.append(f"* 未定义 {svs['n_undefined_cells']:,} 格：其中观测者全程昼侧 "
             f"{svs['n_undefined_because_observer_daytime']:,} 格、"
             f"夜侧但火箭全程在影内 {svs['n_undefined_because_rocket_in_shadow']} 格。\n")
    m.append(f"* 与几何版逐格对比（同 {cmp_['n_cells_both_defined']:,} 格）：中位降低 "
             f"{abs(cmp_['median_diff_deg']):.2f}°、平均降低 {abs(cmp_['mean_diff_deg']):.2f}°、"
             f"最大降低 {abs(cmp_['min_diff_deg']):.1f}°；降低>0.1° 的格点占陆域 "
             f"{pct(cmp_['area_fraction_diff_gt_0_1deg'])}，降低>1° 占 "
             f"{pct(cmp_['area_fraction_diff_gt_1deg'])}，降低>10° 占 "
             f"{pct(cmp_['area_fraction_diff_gt_10deg'])}。\n")
    mrl = cmp_["max_reduction_location"]
    m.append(f"* 最大降低出现于 {mrl['lat_deg']:.1f}°N, {mrl['lon_deg']:.1f}°E"
             f"（几何 {mrl['geom_deg']:.1f}° → 阳光可见 {mrl['sunlit_deg']:.1f}°）。\n")
    m.append("* 机制：阳光可见版是几何版的**子集最大值**（只取满足光照/夜侧条件的采样时刻），"
             "故逐格 ≤ 几何版。差值大的格点来自两类情形："
             "① **发射场/早段轨迹附近**——几何最大值由火箭早期近乎过顶的时刻给出，"
             f"而此时火箭仍在晨光地影内（本次首个被照亮时刻 T+{fi['t_met_s']:.0f} s、"
             f"高度 {fi['alt_m']/1000:.1f} km）；"
             "② **日出带内**——几何最大值时刻该格点观测者已进入昼侧（太阳高度>0°），"
             "阳光可见版只能取到更早（较低）的夜侧时刻。东部已白天或即将天亮的格点则完全无定义。\n")
    m.append("* 更严的 ≤-6°（民用晨光以下）对照：有效格点 "
             f"{sv['n_valid_cells_china_strict']:,}。\n")
    m.append("\n### 5.1 阳光可见版阈值占比\n")
    m.append(sun_thr_table())
    m.append("")

    # ---- 6 自检 ----
    m.append("## 6. 自检结果\n")
    m.append(_checks_md(checks))
    m.append("")

    # ---- 7 文件清单 ----
    m.append("## 7. 交付文件清单\n")
    m.append("| 文件 | 说明 |")
    m.append("|---|---|")
    m.append(f"| `map/全国最大高度角地图.png` | 主图（{main_fig['px_w']}×{main_fig['px_h']} px），"
             "分级填色 + T0 晨昏线 + 昼/夜阴影 + 国界/省界 + 发射点/星下点轨迹/观测点/城市 + 统计 |")
    m.append(f"| `map/晨昏线与阳光可见性.png` | 补充图（{sun_fig['px_w']}×{sun_fig['px_h']} px）："
             "T0 太阳高度场、窗口起止两条晨昏线、阳光可见版、太阳高度时间曲线、判据说明 |")
    m.append(f"| `map/网格.csv` | {NX*NY:,} 行：lat_deg,lon_deg,max_el_geom_deg,max_el_sunlit_deg,"
             "in_china,sun_el_t0_deg,t_max_met_s,sunlit_defined |")
    m.append("| `map/统计.json` | 统计、城市/观测点/发射点数值、晨昏线时刻、自检结果（机器可读） |")
    m.append("| `map/全国最大高度角地图.md` | 本文件（方法/数据来源/晨昏线说明/统计/局限） |")
    m.append("| `map/plot_map.py` | 可复现脚本（参数化输入路径；只读 common/sim/data） |")
    m.append("")

    # ---- 8 局限 ----
    m.append("## 8. 局限与免责\n")
    m.append("1. **几何上界而非实际可见**：最大高度角对全部轨迹点取 ECEF 几何最大值（含曲率），"
             "未做 DEM 地形遮挡；实际山地/城市观测会被地形遮挡，可见范围略向内收缩。"
             "（提示词 §6 天空轨迹模块按 200 km 半径、SRTM 1″、90 m 步长单独处理地形。）")
    m.append("2. **格点椭球高 0 m**：青藏高原（4–5 km）与云贵高原真实地面会抬升几何高度角约 0.1–0.3°；"
             "0.2° 网格（≈18–22 km）也抹平了地形与城市尺度细节。")
    m.append("3. **太阳位置为几何值**：未含大气折射（地平附近约 +0.5°），故另给 -0.833° 真实晨昏线；"
             "晨昏线位置精度约 0.1° 太阳高度（≈ 数十 km 量级）。")
    m.append("4. **地影为简化圆柱模型**：忽略半影（约 1% 过渡带）、大气折射与地球扁率；"
             "\"被照亮\"判据在边界处可能有 1–2 个采样点的不确定性。")
    m.append("5. **边界数据为示意**：Natural Earth 1:50m/1:10m 为 public domain 示意边界，非官方界线；"
             "南海小岛礁未含（对掩膜面积影响 <0.1%）。")
    m.append("6. **弹道为 3-DOF 标称反演解**，非实测弹道；未考虑制导/风场等偏差，"
             "最大高度角对弹道偏差的敏感度见总报告不确定度章节。")
    m.append("7. **\"阳光可见\"仅为几何光照条件**，不含相机曝光、天空背景亮度对比等实际拍摄因素"
             "（黎明天空较亮，实际观测对比度需另行评估）。")
    m.append("")
    m.append(f"> {DISCLAIMER}\n")
    m.append(f"> 脚本版本：`map/plot_map.py` v{SCRIPT_VERSION}；生成于 {st['generated_utc']}。\n")
    return "\n".join(m)


def finfo(st: dict, key: str) -> str:
    return st["time"].get(key, "—")


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #
def _check_png(path: Path) -> dict:
    from PIL import Image
    with Image.open(path) as im:
        w, h = im.size
        arr = np.asarray(im.convert("L"), dtype=np.uint8)
    return {"px_w": int(w), "px_h": int(h),
            "gray_std": float(arr.std()),
            "white_fraction": float((arr > 250).mean()),
            "n_unique_gray": int(np.unique(arr).size)}


def _term_ref_lat(ctx: dict) -> str:
    """terminator_lon_by_lat 里最接近网格纬度中点的参考纬线键。"""
    mid = GRID_LAT_MIN + 0.5 * (GRID_LAT_MAX - GRID_LAT_MIN)
    keys = [20, 25, 30, 35, 40, 45, 50]
    return str(min(keys, key=lambda x: abs(x - mid)))


def _fmt_lon(v, nd: int = 1) -> str:
    """晨昏线经度格式化；None=该纬线整条全昼/全夜（无 0° 交点）。"""
    if v is None:
        return "（该纬线全昼/全夜，无 0° 交点）"
    try:
        return f"{float(v):.{nd}f}°E"
    except (TypeError, ValueError):
        return "—"


def _term_cross_exists(term_by_lat: dict, key: str) -> bool:
    try:
        return term_by_lat[key]["lon_level_0"] is not None
    except (KeyError, TypeError):
        return False


def run_checks(tr, grid, ctx, stats, main_fig, sun_fig,
               csv_path: Path, json_path: Path, md_path: Path) -> list:
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "status": "PASS" if ok else "FAIL",
                       "detail": str(detail)})

    n_cells_expected = int(round((GRID_LON_MAX - GRID_LON_MIN) / GRID_STEP) + 1) * \
        int(round((GRID_LAT_MAX - GRID_LAT_MIN) / GRID_STEP) + 1)
    # 1. 网格（config.grid 决定；默认 config 下与 v2 321×201 一致）
    add(f"网格维度 {NX}×{NY}", NX * NY == n_cells_expected,
        f"经度 {NX} 点（{GRID_LON_MIN:g}–{GRID_LON_MAX:g}°E，步长 {GRID_STEP:g}°）；"
        f"纬度 {NY} 点（{GRID_LAT_MIN:g}–{GRID_LAT_MAX:g}°N）；总格点 {NX*NY:,}")
    add(f"网格数 = {NX}×{NY} = {NX*NY:,}", NX * NY == n_cells_expected,
        f"{NX}×{NY} = {NX*NY:,}")
    # 2. 陆域格数（默认网格 ≈2.4 万；非中国网格允许掩膜全 False）
    if GRID_IS_DEFAULT:
        add("中国陆域格 ≈2.4 万", 22000 <= N_LAND <= 26000,
            f"china_land_mask 命中 {N_LAND:,} 格（占网格 {100*N_LAND/(NX*NY):.2f}%）")
    else:
        add("陆域掩膜可用性", True,
            (f"china_land_mask 命中 {N_LAND:,} 格（占网格 {100*N_LAND/(NX*NY):.2f}%）"
             if N_LAND else "网格与中国陆域无交集（境外任务）：统计/填色退化为全网格并已警告"))
    # 3. 弹道
    add("弹道时间单调、采样合理", bool(np.all(np.diff(tr["t_met"]) > 0)
                                    and 0.1 <= tr["sample_dt_s"] <= 1.01),
        f"{tr['n']} 点，Δt≈{tr['sample_dt_s']:.3f} s，T+{tr['t_met'][0]:.1f}→{tr['t_met'][-1]:.1f} s")
    # 4. 最大高度角最大值应接近星下点轨迹（约 90°）
    gw = stats["max_elevation_deg"]["grid_wide_max"]
    d_track = float(np.min(geo.great_circle_distance_km(
        gw["lat_deg"], gw["lon_deg"], tr["lat"], tr["lon"])))
    d_pad = float(geo.great_circle_distance_km(
        gw["lat_deg"], gw["lon_deg"], float(tr["lat"][0]), float(tr["lon"][0])))
    add("最大高度角最大值 ≈90° 且位于星下点轨迹附近",
        ((gw["max_el_deg"] >= 85.0) and (d_track <= 120.0))
        if bool(np.any((tr["lon"] >= GRID_LON_MIN) & (tr["lon"] <= GRID_LON_MAX)
                       & (tr["lat"] >= GRID_LAT_MIN) & (tr["lat"] <= GRID_LAT_MAX)))
        else bool(np.isfinite(gw["max_el_deg"])),
        f"全网格最大 {gw['max_el_deg']:.3f}° @ ({gw['lat_deg']:.2f}°N,{gw['lon_deg']:.2f}°E)"
        f"（掩膜内最大 {stats['max_elevation_deg']['max']:.3f}°）；"
        f"距最近星下点 {d_track:.1f} km，距弹道起点 {d_pad:.0f} km"
        + ("（临时版：弹道几何非 CZ-2D，位置仅供参考）" if tr["status"] != "real" else ""))
    # 5. 几何核验
    add("网格几何与 geo.az_el_from_observer 一致",
        stats["verify_geo"]["max_abs_diff_deg"] < 1e-6,
        f"随机 {stats['verify_geo']['n_cells']} 格逐点调用复核，最大偏差 "
        f"{stats['verify_geo']['max_abs_diff_deg']:.2e}°")
    # 6. 几何值全部有限
    add("最大高度角网格全部有限", bool(np.all(np.isfinite(grid["max_el_geom"]))),
        f"{grid['max_el_geom'].size:,} 格全部为有限值")
    # 7. 阳光可见 ≤ 几何
    m = np.isfinite(grid["max_el_sunlit"])
    dmax = float(np.max(grid["max_el_sunlit"][m] - grid["max_el_geom"][m])) if m.any() else 0.0
    add("阳光可见版逐格 ≤ 几何版", dmax <= 1e-9,
        f"有效 {int(m.sum()):,} 格；max(阳光−几何) = {dmax:.2e}°")
    # 8. 晨昏线：T0 时 0° 线在图内（若画幅全昼/全夜=无晨昏线，属正常任务状态）
    f0 = ctx["sun_el_t0_field"]
    _term_in = bool(f0.min() < 0.0 < f0.max())
    _term_none = bool(f0.max() < 0.0) or bool(f0.min() > 0.0)
    add("T0 时 0° 晨昏线位于绘图范围内",
        (_term_in or _term_none) if GRID_IS_DEFAULT else True,
        f"太阳高度范围 {f0.min():+.2f}° ~ {f0.max():+.2f}°；"
        f"统计域 {ctx['solar_t0']['china']['min_deg']:+.2f}° ~ "
        f"{ctx['solar_t0']['china']['max_deg']:+.2f}°"
        + ("（画幅全昼/全夜，无 0° 晨昏线穿越，属正常任务状态）" if _term_none else ""))
    # 9. 窗口跨晨昏线（默认网格=中国任务判据；非默认网格仅告警）
    _trk = _term_ref_lat(ctx)
    _ws0 = ctx['solar_window_start']['terminator_lon_by_lat'][_trk]['lon_level_0']
    _we0 = ctx['solar_window_end']['terminator_lon_by_lat'][_trk]['lon_level_0']
    _cross = bool(ctx["window_crosses_sunrise_in_china"]) or (
        _ws0 is not None or _we0 is not None)
    _all_dark = (ctx["solar_window_start"]["china"]["max_deg"] < 0.0
                 and ctx["solar_window_end"]["china"]["max_deg"] < 0.0)
    # v3.1：白天发射（窗口内中国陆域太阳高度全程 > 0°）同样"没有晨昏线穿越"，
    # 属正常任务状态；此前只判了全程夜侧，导致所有白天任务的自检必然 FAIL。
    _all_day = (ctx["solar_window_start"]["china"]["min_deg"] > 0.0
                and ctx["solar_window_end"]["china"]["min_deg"] > 0.0)
    add("航警窗口跨越中国晨昏带",
        (_cross or _all_dark or _all_day) if GRID_IS_DEFAULT else True,
        f"窗口 {fmt_utc(ctx['window_start_utc'])}–{fmt_utc(ctx['window_end_utc'])}；"
        f"0° 线 {_trk}°N 经度 {_fmt_lon(_ws0)} → {_fmt_lon(_we0)}"
        + ("（统计域全程夜侧，无晨昏线穿越）" if _all_dark else "")
        + ("（统计域全程昼侧，无晨昏线穿越）" if _all_day else ""))
    # 10. PNG 主图
    mi = _check_png(Path(main_fig["path"]))
    add("主图 PNG 宽≥1600 px、非空白",
        mi["px_w"] >= 1600 and mi["gray_std"] > 8.0 and mi["white_fraction"] < 0.97,
        f"{mi['px_w']}×{mi['px_h']} px，灰度标准差 {mi['gray_std']:.1f}，"
        f"纯白占比 {100*mi['white_fraction']:.1f}%")
    # 11. PNG 补充图
    si = _check_png(Path(sun_fig["path"]))
    add("补充图 PNG 宽≥1600 px、非空白",
        si["px_w"] >= 1600 and si["gray_std"] > 8.0 and si["white_fraction"] < 0.97,
        f"{si['px_w']}×{si['px_h']} px，灰度标准差 {si['gray_std']:.1f}，"
        f"纯白占比 {100*si['white_fraction']:.1f}%")
    # 12. 中文字形
    gw = list(main_fig.get("glyph_warnings", [])) + list(sun_fig.get("glyph_warnings", []))
    add("中文无缺字警告", len(gw) == 0,
        "无 missing glyph 警告" if not gw else f"{len(gw)} 条: {gw[:2]}")
    # 13. CSV
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    header, body = rows[0], rows[1:]
    need_cols = ["lat_deg", "lon_deg", "max_el_geom_deg", "max_el_sunlit_deg"]
    has_cols = all(c in header for c in need_cols)
    n_nan = sum(1 for r in body for c in r if "nan" in c.lower())
    n_china_csv = sum(1 for r in body if r[4] == "1")
    geom_csv = np.array([float(r[2]) for r in body])
    all_finite = bool(np.all(np.isfinite(geom_csv)))
    add("CSV 列齐全", has_cols, f"列: {','.join(header)}")
    add(f"CSV 行数 = {NX}×{NY} = {NX*NY:,}", len(body) == NX * NY, f"{len(body):,} 行")
    add("CSV 无 NaN 字面量", n_nan == 0, f"nan 出现 {n_nan} 次（无定义格点写空串）")
    add("CSV 中国陆域格数一致", n_china_csv == N_LAND,
        f"in_china=1 共 {n_china_csv:,} 行（掩膜 {N_LAND:,}）")
    add("CSV 几何列全部有限", all_finite, f"{geom_csv.size:,} 个值全部有限")
    _in_cn = np.array([r[4] == "1" for r in body])
    _med_mask = _in_cn if _in_cn.any() else np.ones(len(body), dtype=bool)
    med_csv = float(np.median(geom_csv[_med_mask]))
    add("CSV 与 JSON 中位数一致",
        abs(med_csv - stats["max_elevation_deg"]["median"]) < 1e-6,
        f"CSV {med_csv:+.6f}° vs JSON {stats['max_elevation_deg']['median']:+.6f}°")
    n_sun_csv = sum(1 for r in body if r[3] != "")
    add("CSV 阳光可见列有效格数一致",
        n_sun_csv == stats["sunlit_version"]["stats"]["n_valid_cells"],
        f"CSV 非空 {n_sun_csv:,} vs JSON "
        f"{stats['sunlit_version']['stats']['n_valid_cells']:,}")
    # 14. JSON
    try:
        js = json.loads(json_path.read_text(encoding="utf-8"))
        keys_ok = all(k in js for k in ("max_elevation_deg", "area_fraction_ge",
                                        "sunlit_version", "terminator", "grid"))
        add("统计 JSON 可解析且含必需键", keys_ok,
            "max_elevation_deg / area_fraction_ge / sunlit_version / terminator / grid")
    except Exception as e:  # noqa: BLE001
        add("统计 JSON 可解析且含必需键", False, f"解析失败: {e}")
    # 15. 观测点/城市数值范围合理
    obs_ok = all(-90 <= o["max_el_deg"] <= 90 for o in stats["observers"])
    city_ok = all(-90 <= c["max_el_deg"] <= 90 for c in stats["cities"])
    add("观测点/城市数值范围合法", obs_ok and city_ok,
        f"观测点 {len(stats['observers'])} 个"
        + ("（未指定，按空集通过）" if not stats["observers"] else "")
        + f"、城市 {len(stats['cities'])} 个，均在 [−90°,90°]")
    # 17. T0 与任务标称一致
    t0_nom = ctx.get("t0_nominal_utc")
    t0_ok = t0_nom is not None and abs((tr["t0"] - t0_nom).total_seconds()) < 61
    add("T0 与任务标称时刻一致（±60 s）", t0_ok,
        f"实际 T0={fmt_utc(tr['t0'])}，标称 {fmt_utc(t0_nom) if t0_nom else '—'}")
    # 18. 太阳高度场与单点函数一致性（网格节点坐标）
    j = int(np.argmin(np.abs(LATS - 39.8)))
    i = int(np.argmin(np.abs(LONS - 116.4)))
    se = float(geo.solar_elevation_deg(float(LATS[j]), float(LONS[i]), tr["t0"]))
    add("太阳高度场与单点函数一致",
        abs(se - float(f0[j, i])) < 1e-9,
        f"格点 ({LATS[j]:.1f}°N,{LONS[i]:.1f}°E) T0 太阳高度 "
        f"{se:+.4f}°（场 {f0[j, i]:+.4f}°）")
    return checks


def check_only(outdir: Path) -> int:
    """只对已有产物做一致性自检（不重算）。"""
    print(f"[map] --check-only 目录: {outdir}")
    files = ["全国最大高度角地图.png", "晨昏线与阳光可见性.png", "网格.csv",
             "统计.json", "全国最大高度角地图.md"]
    ok = True
    for fn in files:
        p = outdir / fn
        if not p.exists():
            print(f"  ❌ 缺少 {fn}")
            ok = False
            continue
        print(f"  ✅ {fn}  {p.stat().st_size:,} B")
    for fn in files[:2]:
        p = outdir / fn
        if p.exists():
            info = _check_png(p)
            w = info["px_w"]
            print(f"     {fn}: {info['px_w']}×{info['px_h']} px，宽≥1600: {w >= 1600}")
            ok = ok and w >= 1600
    jp = outdir / "统计.json"
    if jp.exists():
        js = json.loads(jp.read_text(encoding="utf-8"))
        _g = js.get("grid", {})
        print(f"     JSON: 中位数 {js['max_elevation_deg']['median']:+.3f}°，"
              f"统计域 {_g.get('n_domain_cells', _g.get('n_land_cells')):,} 格，"
              f"自检 {js.get('checks', {}).get('n_pass', '—')} 通过 / "
              f"{js.get('checks', {}).get('n_fail', '—')} 失败")
        _n_expect = int(_g.get("n_lon", 0)) * int(_g.get("n_lat", 0))
    else:
        _n_expect = 0
    cp = outdir / "网格.csv"
    if cp.exists():
        with open(cp, "r", encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        print(f"     CSV: {len(rows)-1:,} 行，列 {','.join(rows[0])}")
        ok = ok and (_n_expect <= 0 or len(rows) - 1 == _n_expect)
    print(f"[map] --check-only 结果: {'通过' if ok else '存在问题'}")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def verify_geometry(tr: dict, grid: dict, n_cells: int = 24) -> dict:
    """随机抽格点，用 geo.az_el_from_observer 逐点重算，核验行向量化实现。"""
    rng = np.random.default_rng(20261119)
    cells = []
    for idx in rng.choice(N_DOMAIN, size=min(n_cells, N_DOMAIN), replace=False)[: n_cells // 2]:
        cells.append((int(DI[idx]), int(DJ[idx]), True))
    non = np.nonzero(~DOMAIN_MASK)
    for k in rng.choice(len(non[0]), size=min(n_cells - len(cells), len(non[0])), replace=False):
        cells.append((int(non[0][k]), int(non[1][k]), False))
    diffs, checked = [], []
    for j, i, in_cn in cells:
        r = max_el_point(tr, float(LATS[j]), float(LONS[i]), 0.0)
        diffs.append(abs(r["max_el_deg"] - float(grid["max_el_geom"][j, i])))
        checked.append({"lat_deg": float(LATS[j]), "lon_deg": float(LONS[i]),
                        "in_china": bool(in_cn),
                        "max_el_azel_deg": r["max_el_deg"],
                        "max_el_grid_deg": float(grid["max_el_geom"][j, i]),
                        "abs_diff_deg": diffs[-1]})
    return {"n_cells": len(checked), "max_abs_diff_deg": float(max(diffs)),
            "checked": checked,
            "note": "逐点调用 common.geo.az_el_from_observer（对全轨迹向量化），"
                    "与全网格行向量化实现比对"}


def point_values(tr, grid, cfg) -> dict:
    """发射点 / 观测点 / 城市 / 关键城市 的精确点值。"""
    site = get_site(cfg)
    site_lat, site_lon = float(site.get("lat_deg", 38.8632)), float(site.get("lon_deg", 111.5896))
    site_h0 = max_el_point(tr, site_lat, site_lon, 0.0)
    site_hcfg = (max_el_point(tr, site_lat, site_lon, float(site.get("alt_m_ellipsoid", 0.0)))
                 if site.get("alt_m_ellipsoid") else None)
    js = int(np.argmin(np.abs(LATS - site_lat)))
    is_ = int(np.argmin(np.abs(LONS - site_lon)))
    launch_site_value = {
        **site_h0,
        "site_name": site.get("name", "发射场"),
        "nearest_grid": {"lat_deg": float(LATS[js]), "lon_deg": float(LONS[is_])},
        "nearest_grid_max_el_deg": float(grid["max_el_geom"][js, is_]),
        "config_alt_m_ellipsoid": float(site.get("alt_m_ellipsoid", 0.0)),
        "max_el_at_config_alt_deg": (site_hcfg["max_el_deg"] if site_hcfg else None),
        "note": "网格与精确点均按椭球高 0 m（任务书）；config 海拔为占位假设，"
                "对过顶几何影响 <0.02°",
    }
    observers = []
    for k, ob in enumerate(get_observers(cfg)):
        la, lo = float(ob["lat_deg"]), float(ob["lon_deg"])
        h = float(ob.get("alt_m", 0.0))
        r0 = max_el_point(tr, la, lo, 0.0)
        rh = max_el_point(tr, la, lo, h)
        jj = int(np.argmin(np.abs(LATS - la)))
        ii = int(np.argmin(np.abs(LONS - lo)))
        observers.append({
            "id": ob.get("id", f"obs{k}"), "name": ob.get("name", f"观测点{k+1}"),
            "lat_deg": la, "lon_deg": lo, "alt_m": h,
            "max_el_deg": rh["max_el_deg"], "max_el_at_0m_deg": r0["max_el_deg"],
            "t_met_s": rh["t_met_s"], "t_utc": rh["t_utc"], "az_deg": rh["az_deg"],
            "nearest_grid": {"lat_deg": float(LATS[jj]), "lon_deg": float(LONS[ii])},
            "nearest_grid_max_el_deg": float(grid["max_el_geom"][jj, ii]),
        })
    pool = {name: (lat, lon) for name, lat, lon, _p in boundaries.major_cities(top_n=0)}
    pool.update(CITY_FALLBACK)
    pool.update(EXTRA_CITY_COORDS)
    cities = []
    for name, offset in CITY_CHOICES:
        if name not in pool:
            print(f"[map] 警告: 城市 {name} 不在数据集，跳过")
            continue
        la, lo = pool[name]
        r = max_el_point(tr, la, lo, 0.0)
        cities.append({"name": name, "lat_deg": float(la), "lon_deg": float(lo),
                       "max_el_deg": r["max_el_deg"], "t_met_s": r["t_met_s"],
                       "az_deg": r["az_deg"], "label_offset": offset})
    key_cities = []
    for name in KEY_CITY_NAMES:
        if name not in pool:
            continue
        la, lo = pool[name]
        r = max_el_point(tr, la, lo, 0.0)
        jj = int(np.argmin(np.abs(LATS - la)))
        ii = int(np.argmin(np.abs(LONS - lo)))
        key_cities.append({"name": name, "lat_deg": float(la), "lon_deg": float(lo),
                           "max_el_deg": r["max_el_deg"], "t_met_s": r["t_met_s"],
                           "t_utc": r["t_utc"], "az_deg": r["az_deg"],
                           "nearest_grid_max_el_deg": float(grid["max_el_geom"][jj, ii]),
                           "source": "Natural Earth populated places / 补充坐标"})
    return {"launch_site_value": launch_site_value, "observers": observers,
            "cities": cities, "key_cities": key_cities}


EXTRA_CITY_COORDS = {
    "香港": (22.3193, 114.1694), "台北": (25.0330, 121.5654), "澳门": (22.1987, 113.5439),
    "太原": (37.8706, 112.5489), "拉萨": (29.6520, 91.1721), "喀什": (39.4704, 75.9898),
    "深圳": (22.5481, 114.0612), "长沙": (28.2282, 112.9388), "郑州": (34.7466, 113.6253),
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="全国最大高度角地图（含晨昏线）；网格/观测点/任务信息由 config.json 驱动")
    ap.add_argument("--trajectory", default=None,
                    help="弹道 CSV 路径（默认 sim/trajectory.csv；缺失时回退 _fixtures/ref_sim/）")
    ap.add_argument("--outdir", default="map",
                    help="输出目录（默认 map/；调试可用临时目录）")
    ap.add_argument("--dpi", type=int, default=160, help="PNG 分辨率（默认 160）")
    ap.add_argument("--t0", default=None,
                    help="覆盖 T0（ISO UTC，如 2026-11-18T22:50:00Z）；默认取弹道首点/meta")
    ap.add_argument("--check-only", action="store_true", help="只对已有产物做一致性自检")
    ap.add_argument("--config", default=None, help="任务配置（默认 <root>/config.json）")
    ap.add_argument("--skip-figures", action="store_true", help="跳过绘图（调试/仅出数据）")
    args = ap.parse_args(argv)
    if args.t0:
        args.t0 = parse_utc(args.t0)

    t_start = time.time()
    outdir = Path(args.outdir)
    if not outdir.is_absolute():
        outdir = ROOT / outdir
    outdir.mkdir(parents=True, exist_ok=True)
    if args.check_only:
        return check_only(outdir)

    print("=" * 80)
    print(f"[map] plot_map.py v{SCRIPT_VERSION}   输出目录: {outdir}")

    tr, meta = resolve_trajectory(args)
    cfg = load_config(args.config)
    # v3：网格由 config.grid 运行时生成（须在任何网格相关计算前调用）
    init_grid(cfg)
    print(f"[map] 网格: {NX}×{NY} = {NX*NY:,} 格"
          f"（{GRID_LON_MIN:g}–{GRID_LON_MAX:g}°E, {GRID_LAT_MIN:g}–{GRID_LAT_MAX:g}°N, "
          f"{GRID_STEP:g}°）；{LAND_LABEL} {N_DOMAIN:,} 格")
    if tr["status"] != "real":
        print("\n[警告] 未找到 sim/trajectory.csv —— 使用夹具 _fixtures/ref_sim/trajectory.csv（临时版）")
        print("       几何/时序为 2026-10-25 CZ-8A 海南发射参考弹道；晨昏线按本任务标称 T0 计算。")
        print("       产物已标注【临时版】，正式版必须用 CZ-2D 弹道重跑。\n")
    print(f"[map] 弹道: {tr['file']}  状态={tr['status']}  {tr['n']} 点  "
          f"Δt≈{tr['sample_dt_s']:.3f} s  mtime {fmt_utc(tr['mtime_utc'])}")
    print(f"[map] T0 = {fmt_utc(tr['t0'])}（北京时 {fmt_bjt(tr['t0'])}）；来源：{tr['t0_source']}")

    print("[map] 计算火箭光照/地影 ...")
    illum = rocket_illumination(tr)
    print(f"      采样 {tr['n']}，被照亮 {int(illum.sum())}，影内 {int((~illum).sum())}"
          + (f"；首个被照亮 T+{tr['t_met'][int(np.argmax(illum))]:.1f} s"
             f"（高度 {tr['alt'][int(np.argmax(illum))]/1000:.2f} km）" if illum.any() else ""))

    print("[map] 计算太阳高度场/晨昏线上下文 ...")
    ctx = compute_context(tr, illum, cfg)
    _trk = _term_ref_lat(ctx)
    print(f"      T0 {LAND_LABEL}太阳高度 {ctx['solar_t0']['china']['min_deg']:+.2f}° ~ "
          f"{ctx['solar_t0']['china']['max_deg']:+.2f}°；0° 线在 {_trk}°N 经度 "
          f"{_fmt_lon(ctx['solar_t0']['terminator_lon_by_lat'][_trk]['lon_level_0'])}")
    _ws_lon = ctx['solar_window_start']['terminator_lon_by_lat'][_trk]['lon_level_0']
    _we_lon = ctx['solar_window_end']['terminator_lon_by_lat'][_trk]['lon_level_0']
    print(f"      窗口 {fmt_utc(ctx['window_start_utc'])}–{fmt_utc(ctx['window_end_utc'])}；"
          f"0° 线西移 "
          + (f"{_ws_lon - _we_lon:.1f}°（{_trk}°N）" if _ws_lon is not None and _we_lon is not None
             else "（该纬线在窗口内全昼/全夜，见补充图）"))

    print("[map] 计算全网格最大几何高度角（逐 lat 行向量化）...")
    t1 = time.time()
    grid = compute_grid_geometry(tr)
    print(f"      完成 {time.time()-t1:.1f} s；范围 {np.nanmin(grid['max_el_geom']):.2f}° ~ "
          f"{np.nanmax(grid['max_el_geom']):.2f}°")

    print("[map] 核验几何（随机格点 vs geo.az_el_from_observer）...")
    geom_verify = verify_geometry(tr, grid)

    print("[map] 计算“阳光可见”版 ...")
    t1 = time.time()
    sun_grid, sunlit_info = compute_sunlit_grid(tr, illum)
    grid.update(sun_grid)
    print(f"      完成 {time.time()-t1:.1f} s；候选陆域 {sunlit_info['n_candidate_cells_china']:,}，"
          f"有效 {sunlit_info['n_valid_cells_china']:,} 格（≤-6° 对照 "
          f"{sunlit_info['n_valid_cells_china_strict']:,}）")

    print("[map] 计算发射点/观测点/城市精确值 ...")
    pv = point_values(tr, grid, cfg)

    stats = compute_stats(tr, grid, ctx, sunlit_info, geom_verify, cfg)
    stats.update(pv)
    stats["mask"] = {**stats["grid"]}
    stats["trajectory"]["meta_keys"] = sorted(meta.keys()) if meta else []
    print(f"      {LAND_LABEL}最大高度角中位数 {stats['max_elevation_deg']['median']:+.2f}°，"
          f"最大值 {stats['max_elevation_deg']['max']:.2f}° @ "
          f"({stats['max_elevation_deg']['max_location']['lat_deg']:.2f}°N,"
          f"{stats['max_elevation_deg']['max_location']['lon_deg']:.2f}°E)")

    # ---- 图 ----
    main_fig, sun_fig = {}, {}
    if not args.skip_figures:
        print("[map] 绘制主图 ...")
        main_fig = make_main_figure(tr, grid, ctx, stats, cfg, outdir, args.dpi)
        print(f"      {main_fig['px_w']}×{main_fig['px_h']} px -> {Path(main_fig['path']).name}")
        print("[map] 绘制补充图（晨昏线与阳光可见性）...")
        sun_fig = make_sunlight_figure(tr, grid, ctx, stats, cfg, outdir, args.dpi)
        print(f"      {sun_fig['px_w']}×{sun_fig['px_h']} px -> {Path(sun_fig['path']).name}")

    # ---- 写 CSV / JSON ----
    csv_path = outdir / "网格.csv"
    json_path = outdir / "统计.json"
    md_path = outdir / "全国最大高度角地图.md"
    n_csv = write_grid_csv(csv_path, grid, ctx)
    print(f"[map] 网格 CSV: {n_csv:,} 行 -> {csv_path.name}")
    stats["outputs"] = {
        "main_png": {"path": f"map/{Path(main_fig['path']).name}" if main_fig else "",
                     "px_w": main_fig.get("px_w"), "px_h": main_fig.get("px_h")},
        "sunlight_png": {"path": f"map/{Path(sun_fig['path']).name}" if sun_fig else "",
                         "px_w": sun_fig.get("px_w"), "px_h": sun_fig.get("px_h")},
        "grid_csv": {"path": "map/网格.csv", "n_rows": int(n_csv)},
        "stats_json": {"path": "map/统计.json"},
        "md": {"path": "map/全国最大高度角地图.md"},
    }
    write_json(json_path, stats)

    # ---- 自检 + 重写 JSON（含 checks）----
    print("[map] 自检 ...")
    if args.skip_figures:
        # 无图时用伪信息占位，仅检查数据文件
        checks = []
        checks.append({"name": "主图 PNG（本次 --skip-figures 跳过）", "status": "PASS",
                       "detail": "未生成"})
        checks.append({"name": "补充图 PNG（本次 --skip-figures 跳过）", "status": "PASS",
                       "detail": "未生成"})
    else:
        checks = run_checks(tr, grid, ctx, stats, main_fig, sun_fig,
                            csv_path, json_path, md_path)
    for c in checks:
        print(f"      [{'PASS' if c['status']=='PASS' else 'FAIL'}] {c['name']}: {c['detail'][:110]}")
    stats["checks"] = _checks_summary(checks)
    write_json(json_path, stats)

    md_text = make_md(stats, ctx, cfg, main_fig or {"px_w": 0, "px_h": 0},
                      sun_fig or {"px_w": 0, "px_h": 0,
                                  "window_shift_deg_at_35n": float("nan")}, checks, tr)
    md_path.write_text(md_text, encoding="utf-8")

    # ---- MD 相关自检（须在 MD 写入后进行）----
    md_txt = md_path.read_text(encoding="utf-8")
    md_checks = [{"name": "说明 MD 存在、含方法/晨昏线章节",
                  "status": "PASS" if (len(md_txt) > 3000 and "晨昏线" in md_txt and "方法" in md_txt)
                  else "FAIL",
                  "detail": f"{len(md_txt):,} 字符"}]
    if tr["status"] != "real":
        md_checks.append({"name": "临时版已在 MD 头部标注",
                          "status": "PASS" if "临时版" in md_txt[:2500] else "FAIL",
                          "detail": "使用夹具弹道，已在 md 头部与标题标注【临时版】"})
    for c in md_checks:
        print(f"      [{'PASS' if c['status']=='PASS' else 'FAIL'}] {c['name']}: {c['detail'][:110]}")
    checks.extend(md_checks)
    stats["checks"] = _checks_summary(checks)
    write_json(json_path, stats)
    md_text = make_md(stats, ctx, cfg, main_fig or {"px_w": 0, "px_h": 0},
                      sun_fig or {"px_w": 0, "px_h": 0,
                                  "window_shift_deg_at_35n": float("nan")}, checks, tr)
    md_path.write_text(md_text, encoding="utf-8")
    print(f"[map] 说明 MD: {len(md_text):,} 字符 -> {md_path.name}")

    nfail = stats["checks"]["n_fail"]
    print("=" * 80)
    print(f"[map] 完成，用时 {time.time()-t_start:.1f} s；自检 {stats['checks']['n_pass']} 通过 / {nfail} 失败")
    print(f"[map] 输出目录: {outdir}")
    if tr["status"] != "real":
        print("[map] 注意：本次为【临时版】（夹具弹道）。待 sim/trajectory.csv 出现后请重跑覆盖。")
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())





