#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sky/plot_sky.py — 指定坐标天空轨迹图（方位角–高度角）生成脚本。

任务：2026-11-19 06:50 北京时（2026-11-18 22:50 UTC）长征二号丁遥106（CZ-2D）
自太原卫星发射中心九号工位（LC-9）发射。指定观测点：
37°45′41.28″N, 112°43′30.79″E（37.761467°N, 112.725219°E，山西晋中/太原南）。

主要工作
--------
1. 读取 ``sim/trajectory.csv``（0.5 s 采样）等正式仿真数据；输入目录用 ``--sim-dir``
   参数化（正式数据缺失时可临时用 ``_fixtures/ref_sim`` 的 CZ-8A 夹具跑通流程，
   此时 PNG/MD 会显著标注"临时版"）；
2. ECEF 三维几何算全弹道相对观测点的方位角/高度角/斜距/地面距离（含地球曲率与目标高度）；
3. SRTM 1″ DEM 90 m 步长算各方位 200 km 内地形遮挡角（``common.dem``，sea_level_clip），
   可见判据 = 火箭高度角 > 地形遮挡角；观测点高程取 SRTM 1″ DEM；
4. 首次出现/消失二分细化到 0.01 s、最大高度角黄金分割细化到 0.001 s；
5. 阳光可见：观测点太阳高度（``common.geo.solar_elevation_deg``）+ 圆柱/锥形地影模型，
   给出"几何可见"与"阳光可见"两套统计，并评估曙光对肉眼可见性的影响；
6. 输出 ``天空轨迹_<obs_id>.png/.csv/.md``、``天空轨迹_全景.png``、
   ``太阳地影示意_<obs_id>.png`` 与 ``sky/sky_summary.json``。

只写 ``sky/`` 目录。使用方法（PowerShell）::

    $env:PYTHONPATH = "<repo>\\_pylibs"
    python sky/plot_sky.py                                # 正式数据：<repo>/sim
    python sky/plot_sky.py --sim-dir _fixtures/ref_sim --outdir sky/_placeholder
    python sky/plot_sky.py --lat 30.76 --lon 112.73 --name 自定义点

数据来源：``sim/*``（Lead 生成的标称弹道）、``common/config.json``、
``data/dem/observer_dem.json``、SRTMGL1 DEM（AWS Open Data skadi）。
免责声明：仅供公开信息分析与教学使用，非实际发射弹道、非官方信息。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

try:  # Windows 控制台中文输出
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# v3：PROJECT_ROOT 指"任务工作区"（RK_ROOT），引擎资源目录另由 RK_ENGINE 提供
PROJECT_ROOT = Path(os.environ.get("RK_ROOT") or Path(__file__).resolve().parents[1])

# --- v3 引擎/工作区解耦（自动补丁） ---
_ENG = os.environ.get("RK_ENGINE")
if _ENG:
    for _p in (_ENG, os.path.join(_ENG, "scripts")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
# --- end ---
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common import dem, geo, plotstyle  # noqa: E402

import matplotlib  # noqa: E402

matplotlib.use(os.environ.get("MPLBACKEND", "Agg"))
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.cm import ScalarMappable  # noqa: E402
from matplotlib.collections import LineCollection  # noqa: E402
from matplotlib.colors import Normalize  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

# --------------------------------------------------------------------------- #
# 常量 / 路径
# --------------------------------------------------------------------------- #
UTC = timezone.utc
BEIJING_TZ = timezone(timedelta(hours=8))

MISSION_TITLE = "2026-11-19 长征二号丁（遥106）发射"
MISSION_T0_DEFAULT = "2026-11-18T22:50:00Z"

#: 正式输入目录（--sim-dir 可覆盖；main 中赋全局）
SIM_DIR = PROJECT_ROOT / "sim"
FIXTURE_SIM_DIR = PROJECT_ROOT / "_fixtures" / "ref_sim"
SKY_DIR = PROJECT_ROOT / "sky"
PLACEHOLDER_DIR = SKY_DIR / "_placeholder"
CONFIG_PATH = PROJECT_ROOT / "config.json"
CONFIG_FALLBACK = PROJECT_ROOT / "common" / "config.json"
OBS_DEM_PATH = Path(os.environ.get("RK_DEM") or (PROJECT_ROOT / "data" / "dem")) / "observer_dem.json"

GRID_AZ = np.arange(0.0, 360.0, 0.5)      # 720 个方位（0.5° 间隔）
HORIZON_RADIUS_KM = 200.0                 # 地形遮挡搜索半径（与 config.dem 一致）
HORIZON_STEP_M = 90.0                     # 沿方位步长（SRTM 1″ ≈ 30 m 的 3 倍）
R_EARTH_KM = 6371.0088                    # IUGG 平均半径（球面/地影模型）
R_EARTH_M = R_EARTH_KM * 1000.0
DEFAULT_T0 = MISSION_T0_DEFAULT
BISECT_TOL_S = 0.01                       # 二分细化容差（任务要求 0.01–0.1 s）
SUN_ANGULAR_RADIUS_DEG = 0.2666           # 太阳视半径（圆锥本影/半影模型）
SUN_VISIBLE_OBS_EL_DEG = -0.83            # 观测点"白昼/民用晨昏"界线（含折射 + 日面半径）
TWILIGHT_LEVELS_DEG = {                   # 晨昏分级（太阳几何高度角）
    "civil": -6.0,
    "nautical": -12.0,
    "astronomical": -18.0,
}

#: 残骸对象中文名缺省值（sim/objects.json 存在时以它为准）
OBJ_DEFAULT_NAMES = {
    "booster_L": "左助推器",
    "booster_R": "右助推器",
    "core_stage": "芯一级",
    "fairing_L": "整流罩左半",
    "fairing_R": "整流罩右半",
}
#: 本任务允许的残骸对象（CZ-2D 无助推器；夹具里若有 booster 会被跳过）
DEBRIS_ALLOWED = ("core_stage", "fairing_L", "fairing_R")
#: 主图必须标注的分离事件（本任务无助推器）
MAIN_EVENTS = ["一二级分离", "整流罩分离"]
#: 事件名模糊匹配（兼容 Lead 可能使用的英文/别名）
EVENT_ALIASES = {
    "起飞": ["起飞", "liftoff", "launch"],
    "助推器分离": ["助推器分离", "booster_sep", "助推分离"],
    "一二级分离": ["一二级分离", "stage_sep", "一二级"],
    "整流罩分离": ["整流罩分离", "fairing_sep"],
    "二级点火": ["二级点火", "S2_ignition", "二级发动机点火"],
    "二级关机": ["二级关机", "SECO", "二级发动机关机"],
    "入轨": ["入轨", "insertion", "轨道插入"],
}


# --------------------------------------------------------------------------- #
# v3.1：以下三个函数把历史版本写死的"CZ-2D 专用常量"改成按任务动态生成
# --------------------------------------------------------------------------- #
def _mission_title(cfg: dict) -> str:
    """由 ``config.mission`` 生成图题（不再写死"CZ-2D 遥106"）。"""
    m = (cfg or {}).get("mission") or {}
    rocket = str(m.get("rocket") or "").strip()
    t0 = str(m.get("t0_utc") or "")[:10]
    if rocket:
        return ("%s %s发射" % (t0, rocket)).strip()
    name = str(m.get("name") or "").strip()
    return name or MISSION_TITLE


def _debris_allowlist() -> tuple:
    """残骸对象白名单：优先 ``sim/objects.json``（可含 booster_N 等任意对象），
    缺失时回退内置表（旧行为）。"""
    data = load_json(SIM_DIR / "objects.json", default=None)
    if isinstance(data, list):
        names = tuple(str(x.get("obj")) for x in data if isinstance(x, dict) and x.get("obj"))
        if names:
            return names
    return DEBRIS_ALLOWED


def _main_events(events) -> list:
    """主图要标注的分离事件：按事件表里实际存在的种类动态选择（含助推器分离）。"""
    names = [str(e.get("event", "")) for e in (events or [])]
    prefer = ["助推器分离", "一二级分离", "级间分离", "整流罩分离"]
    out = [p for p in prefer if any(p in n for n in names)]
    return out or list(MAIN_EVENTS)


# --------------------------------------------------------------------------- #
# v3.2.3：多观测点成果文件名 = 地点名 + 经纬度（如 天空轨迹_广州_23.13N_113.26E.png）
# --------------------------------------------------------------------------- #
_ILLEGAL_CHARS = '<>:"/\\|?*'


def _safe_label(name: str) -> str:
    s = "".join(("_" if ch in _ILLEGAL_CHARS else ch) for ch in str(name or "").strip())
    return s or "观测点"


def _obs_place_tag(obs: dict) -> str:
    lat, lon = float(obs["lat_deg"]), float(obs["lon_deg"])
    return "%s_%.2f%s_%.2f%s" % (
        _safe_label(obs.get("name") or obs.get("id")),
        abs(lat), ("N" if lat >= 0 else "S"),
        abs(lon), ("E" if lon >= 0 else "W"))


def _unique_path(outdir: Path, stem: str, ext: str) -> Path:
    """同名文件自动加序号（``_2``/``_3``…），避免覆盖用户已有成果。"""
    p = outdir / (stem + ext)
    k = 2
    while p.exists():
        p = outdir / ("%s_%d%s" % (stem, k, ext))
        k += 1
    return p
ANGLE_UNC_COMPONENTS = {         # 首次出现时刻的角度不确定度预算 [deg]
    "dem_height_m": 10.0,        # SRTM 相对高程误差（1″ 量级）
    "obs_height_m": 5.0,         # 观测点高程不确定度（DEM vs 真实地面/建筑基座）
    "refraction_deg": 0.20,      # 未建模的大气折射（低仰角 0.1–0.3°）
    "building_deg": 0.30,        # 城市建筑/植被/堤岸伪影
}
_PALETTE_DEFAULT = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
                    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]


# --------------------------------------------------------------------------- #
# 1. 通用小工具
# --------------------------------------------------------------------------- #
def iso_utc(dt: datetime) -> str:
    """datetime → ISO8601 UTC 字符串（``2026-10-25T01:50:00Z``）。"""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value) -> datetime:
    """解析 ISO8601 / ``YYYY-MM-DD HH:MM:SS`` 字符串为 tz-aware UTC datetime。"""
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    s = str(value).strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.strptime(s.split(".")[0], "%Y-%m-%dT%H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def bj_str(dt: datetime) -> str:
    """UTC datetime → 北京时字符串（UTC+8，``YYYY-MM-DD HH:MM:SS``）。"""
    return dt.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def dms_str(lat_deg: float, lon_deg: float) -> str:
    """十进制度 → 度分秒字符串（如 ``30°45′42.0″N, 112°43′31.0″E``）。"""
    def _one(v: float, pos: str, neg: str) -> str:
        hemi = pos if v >= 0 else neg
        v = abs(float(v))
        d = int(v)
        m = int((v - d) * 60.0)
        s = (v - d - m / 60.0) * 3600.0
        if s >= 59.95:
            s = 0.0
            m += 1
        if m >= 60:
            m = 0
            d += 1
        return f"{d}°{m:02d}′{s:04.1f}″{hemi}"
    return f"{_one(lat_deg, 'N', 'S')}, {_one(lon_deg, 'E', 'W')}"


def now_utc() -> datetime:
    return datetime.now(UTC)


def file_meta(path: Path) -> dict:
    """文件溯源信息：相对路径、mtime(UTC)、sha256 前 16 位、字节数。"""
    p = Path(path)
    if not p.exists():
        return {"path": str(p.relative_to(PROJECT_ROOT)) if p.is_relative_to(PROJECT_ROOT) else str(p),
                "exists": False}
    try:
        rel = str(p.relative_to(PROJECT_ROOT))
    except ValueError:
        rel = str(p)
    h = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    return {
        "path": rel,
        "exists": True,
        "bytes": int(p.stat().st_size),
        "mtime_utc": iso_utc(datetime.fromtimestamp(p.stat().st_mtime, UTC)),
        "sha256_16": h,
    }


def load_json(path: Path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return default


def md_table(headers, rows) -> str:
    """Markdown 表格。"""
    lines = ["| " + " | ".join(str(h) for h in headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join("" if v is None else str(v) for v in row) + " |")
    return "\n".join(lines)


def png_size(path: Path) -> tuple[int, int]:
    """读取 PNG IHDR 得到像素宽高（不依赖 PIL）。"""
    with open(path, "rb") as f:
        head = f.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return (0, 0)
    return (int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big"))


def fmt(v, nd=2, default="-"):
    """安全格式化浮点数。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    if not np.isfinite(v):
        return default
    return f"{v:.{nd}f}"


def circular_mean_deg(az_deg) -> float:
    """方位角的圆周平均 [deg)（用于确定画图方位窗中心）。"""
    a = np.radians(np.asarray(az_deg, dtype=float))
    if a.size == 0:
        return 0.0
    return float(np.degrees(np.arctan2(np.mean(np.sin(a)), np.mean(np.cos(a)))) % 360.0)


def unwrap_around(az_deg, center_deg):
    """把方位角平移到以 center 为中心的连续区间（处理 360°→0° 回绕）。"""
    az = np.asarray(az_deg, dtype=float)
    return center_deg + ((az - center_deg + 180.0) % 360.0) - 180.0


# --------------------------------------------------------------------------- #
# 2. 配置 / 观测点 / 元数据
# --------------------------------------------------------------------------- #
def load_config(path=None) -> dict:
    p = Path(path) if path else (CONFIG_PATH if CONFIG_PATH.exists() else CONFIG_FALLBACK)
    cfg = load_json(p, default=None)
    if not cfg:
        raise FileNotFoundError(f"缺少配置文件 {p}")
    return cfg


def _site_lat_lon(site: dict) -> tuple[float, float, float]:
    """兼容多种字段命名的发射场 (lat, lon, alt_m)。"""
    lat = site.get("lat_deg", site.get("lat"))
    lon = site.get("lon_deg", site.get("lon"))
    alt = site.get("alt_m_ellipsoid", site.get("alt_m", site.get("alt", 0.0)))
    return float(lat), float(lon), float(alt)


def load_meta(cfg: dict) -> dict:
    """读取 ``sim/trajectory_meta.json``（缺失时回退 config 的 T0/发射场）。"""
    meta = load_json(SIM_DIR / "trajectory_meta.json", default={}) or {}
    t0 = parse_utc(meta.get("t0_utc") or cfg.get("t0_nominal_utc") or DEFAULT_T0)
    site = meta.get("site") or cfg.get("launch_site") or {}
    lat, lon, alt = _site_lat_lon(site)
    return {
        "t0_utc": t0,
        "site": {"lat_deg": lat, "lon_deg": lon, "alt_m": alt,
                 "name": site.get("name", "太原卫星发射中心九号工位")},
        "insertion_t_met_s": meta.get("insertion_t_met_s"),
        "version": meta.get("version"),
        "source": file_meta(SIM_DIR / "trajectory_meta.json"),
    }


def load_observers(cfg: dict) -> list[dict]:
    """读取 ``config.observers``；高程优先取 SRTM 1″ DEM。

    优先读 ``data/dem/observer_dem.json``（``python -m common.dem --build`` 的产物）；
    该文件缺失时直接采样 1″ 瓦片（``dem.dem_alt``）；仍失败则用 config 假设值。
    """
    dem_doc = load_json(OBS_DEM_PATH, default={}) or {}
    dem_by_id = {}
    for item in dem_doc.get("observers", []) or []:
        if isinstance(item, dict) and "id" in item:
            dem_by_id[item["id"]] = item
    obs_cfg = cfg.get("observers") or []
    if not obs_cfg:
        raise ValueError("common/config.json 缺少 observers")
    out = []
    for item in obs_cfg:
        oid = item["id"]
        lat = float(item["lat_deg"])
        lon = float(item["lon_deg"])
        cfg_alt = float(item.get("alt_m", 0.0))
        dinfo = dem_by_id.get(oid, {})
        cache_file = dem.cache_path(oid)
        cache_ready = cache_file.exists()
        dem_alt = dinfo.get("dem_alt_m")
        alt_source = dinfo.get("dem_alt_source")
        tiles = list(dinfo.get("tiles_used") or [])
        if dem_alt is None:
            try:
                v = dem.dem_alt(lat, lon)
                if v is not None and np.isfinite(float(v)):
                    dem_alt = float(v)
                    alt_source = "SRTMGL1 1″ 原始瓦片最近邻（EGM96 正高，当椭球高使用）"
            except Exception as exc:  # noqa: BLE001
                print(f"[sky] 警告: {oid} DEM 高程采样失败（{exc}）→ 用 config 假设值 {cfg_alt:.0f} m")
        if cache_ready and not tiles:
            try:
                info = dem.load_observer_cache(oid)
                tiles = [str(s) for s in info.get("tiles_used", [])]
            except Exception:  # noqa: BLE001
                pass
        alt = float(dem_alt) if dem_alt is not None else cfg_alt
        out.append({
            "id": oid,
            "name": item.get("name", oid),
            "lat_deg": lat, "lon_deg": lon, "alt_m": alt,
            "alt_source": alt_source or "config.json 假设值",
            "config_alt_m": cfg_alt,
            "dem_alt_m": dem_alt,
            "dem_info": dinfo,
            "tiles_used": tiles,
            "cache_file": (f"data/dem/cache/{oid}.npz" if cache_ready else None),
        })
    return out


def make_custom_observer(lat: float, lon: float, name: str, alt: float | None) -> dict:
    """任意经纬度观测点（DEM 覆盖区外自动退化为 0 m 高程 + 平地遮挡角）。"""
    oid = "custom"
    if alt is None or not np.isfinite(alt):
        try:
            a = dem.dem_alt(lat, lon)
            alt = float(a) if np.isfinite(a) else 0.0
            alt_src = "SRTM GL1 1″ 最近邻（EGM96 正高，当椭球高使用）"
        except Exception:  # noqa: BLE001
            alt = 0.0
            alt_src = "DEM 不可用，取 0 m"
    else:
        alt = float(alt)
        alt_src = "命令行 --alt 指定"
    return {
        "id": oid, "name": name or "自定义观测点",
        "lat_deg": float(lat), "lon_deg": float(lon), "alt_m": alt,
        "alt_source": alt_src, "config_alt_m": None,
        "dem_info": {}, "tiles_used": [], "cache_file": None,
    }


# --------------------------------------------------------------------------- #
# 3. 仿真数据读取（正式 sim/*；缺失时用占位数据）
# --------------------------------------------------------------------------- #
def _dt_list_from(t0: datetime, t_met) -> list[datetime]:
    return [t0 + timedelta(seconds=float(v)) for v in t_met]


def _finish_trajectory(t, lat, lon, alt, speed, t_utc, meta_info) -> dict:
    t = np.asarray(t, dtype=float)
    order = np.argsort(t, kind="stable")
    t = t[order]
    lat = np.asarray(lat, dtype=float)[order]
    lon = np.asarray(lon, dtype=float)[order]
    alt = np.asarray(alt, dtype=float)[order]
    if speed is None:
        speed = np.zeros_like(t)
    speed = np.asarray(speed, dtype=float)[order]
    # 去重（严格递增时间）
    keep = np.concatenate(([True], np.diff(t) > 1e-9))
    traj = {
        "t": t[keep], "lat": lat[keep], "lon": lon[keep], "alt": alt[keep],
        "speed": speed[keep],
        "ecef": geo.geodetic_to_ecef(lat[keep], lon[keep], alt[keep]),
        "t_utc": [t_utc[i] for i in np.where(keep)[0]] if t_utc else None,
        **meta_info,
    }
    if traj["t"].size < 10:
        raise ValueError("弹道数据点太少（<10）")
    return traj


def _rel(path) -> str:
    """路径 → 相对工程根的字符串（不在工程根内则返回绝对路径）。"""
    p = Path(path).resolve()
    try:
        return str(p.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(p)


def _read_trajectory_csv(path: Path) -> dict:
    """读取弹道 CSV → 原始数组 dict（列名兼容 BOM/首尾空白）。"""
    import pandas as pd

    df = pd.read_csv(path, encoding="utf-8-sig")
    cols = {c.strip(): c for c in df.columns}
    need = ["t_met_s", "lat_deg", "lon_deg", "alt_m"]
    for c in need:
        if c not in cols:
            raise ValueError(f"{path.name} 缺少列 {c}（实际列：{list(df.columns)}）")
    df = df.rename(columns={v: k for k, v in cols.items()})
    return {
        "t": df["t_met_s"].to_numpy(dtype=float),
        "lat": df["lat_deg"].to_numpy(dtype=float),
        "lon": df["lon_deg"].to_numpy(dtype=float),
        "alt": df["alt_m"].to_numpy(dtype=float),
        "speed": (df["speed_m_s"].to_numpy(dtype=float)
                  if "speed_m_s" in df.columns else None),
        "rows": int(len(df)),
        "phase": (df["phase"].astype(str).tolist() if "phase" in df.columns else None),
        "columns": list(df.columns),
    }


def load_trajectory(cfg: dict, t0: datetime, force_placeholder: bool = False) -> dict:
    """读取 ``<SIM_DIR>/trajectory.csv``；缺失时退回内置示意弹道（placeholder=True）。

    ``SIM_DIR`` 由命令行 ``--sim-dir`` 决定（默认 ``<repo>/sim``）。若 ``SIM_DIR`` 不是
    正式目录（例如 ``_fixtures/ref_sim``），一律标记 ``placeholder/temp=True``，
    输出会写"临时版"警告，避免与正式 CZ-2D 数据混淆。
    """
    temp = SIM_DIR.resolve() != (PROJECT_ROOT / "sim").resolve()
    path = SIM_DIR / "trajectory.csv"
    if path.exists() and not force_placeholder:
        d = _read_trajectory_csv(path)
        t_utc = _dt_list_from(t0, d["t"])
        info = {
            "placeholder": temp, "temp": temp,
            "source": _rel(path), "file": file_meta(path),
            "rows": d["rows"], "phase": d["phase"], "columns": d["columns"],
        }
        print(f"[sky] 弹道数据: {_rel(path)}  {d['rows']} 行  临时版={temp}", flush=True)
        return _finish_trajectory(d["t"], d["lat"], d["lon"], d["alt"],
                                  d["speed"], t_utc, info)
    print("[sky] 警告: 未找到 trajectory.csv → 使用内置示意弹道（仅供调通流程，非仿真结果）")
    traj = build_placeholder_trajectory(cfg, t0)
    traj["placeholder"] = True
    return traj


def build_placeholder_trajectory(cfg: dict, t0: datetime) -> dict:
    """内置示意弹道（**不是仿真结果**）：仅当 trajectory.csv 缺失时跑通流程用。

    关键点取自公开时序（一二级分离 ~T+160.5、整流罩分离 ~T+215、SECO ~T+480 s），
    高度/射程为示意曲线，不能用于任何结论性数值。
    """
    from scipy.interpolate import PchipInterpolator

    site = cfg.get("launch_site") or {}
    lat0 = float(site.get("lat_deg", 38.8632))
    lon0 = float(site.get("lon_deg", 111.5896))
    t_kp = np.array([0, 30, 60, 100, 160.5, 200, 260, 330, 400, 480, 540], dtype=float)
    alt_kp = np.array([0, 1.5, 6, 14, 50, 75, 110, 160, 215, 300, 320], dtype=float) * 1000.0
    dr_kp = np.array([0, 3, 12, 35, 95, 170, 290, 470, 700, 980, 1130], dtype=float) * 1000.0
    t = np.arange(0.0, t_kp[-1] + 1e-9, 0.5)
    alt = PchipInterpolator(t_kp, alt_kp)(t)
    dr = PchipInterpolator(t_kp, dr_kp)(t)
    heading = 186.0
    lat = np.empty_like(t)
    lon = np.empty_like(t)
    for i, d in enumerate(dr):
        lat[i], lon[i], _ = geo.geodesic_direct(lat0, lon0, heading, d / 1000.0)
    lat[0], lon[0] = lat0, lon0
    speed = np.hypot(np.gradient(dr), np.gradient(alt))
    phase = np.where(t < 160.5, "S1",
                     np.where(t < 480.0, "S2", "ORBIT"))
    return _finish_trajectory(t, lat, lon, alt, speed, _dt_list_from(t0, t), {
        "placeholder": True, "temp": True,
        "source": "内置示意弹道（非 sim 数据）",
        "file": {"path": "sky/plot_sky.py::build_placeholder_trajectory", "exists": True},
        "rows": int(t.size), "phase": phase.tolist(), "columns": [],
    })


def _interp_traj_xyz(traj: dict, tq) -> tuple[float, float, float]:
    """在弹道上线性插值经纬高（时间限定在数据范围内）。"""
    tq = float(np.clip(tq, traj["t"][0], traj["t"][-1]))
    lat = float(np.interp(tq, traj["t"], traj["lat"]))
    lon = float(np.interp(tq, traj["t"], traj["lon"]))
    alt = float(np.interp(tq, traj["t"], traj["alt"]))
    return lat, lon, alt


def load_events(traj: dict, t0: datetime) -> list[dict]:
    """读取 ``sim/弹道事件表.csv``（缺失时用占位事件）；返回事件 dict 列表。"""
    path = SIM_DIR / "弹道事件表.csv"
    events: list[dict] = []
    if path.exists():
        import pandas as pd

        df = pd.read_csv(path, encoding="utf-8-sig")
        df.columns = [c.strip() for c in df.columns]
        for _, row in df.iterrows():
            name = str(row.get("event", "")).strip()
            if "助推器" in name:
                continue  # 本任务 CZ-2D 无助推器（夹具数据中若有则跳过）
            try:
                tmet = float(row["t_met_s"])
            except (KeyError, TypeError, ValueError):
                continue
            lat = row.get("lat_deg")
            lon = row.get("lon_deg")
            alt = row.get("alt_m")
            if lat is None or (isinstance(lat, float) and not np.isfinite(lat)):
                lat, lon, alt = _interp_traj_xyz(traj, tmet)
            events.append({
                "event": name,
                "t_met_s": tmet,
                "t_utc": (parse_utc(row["t_utc"]) if "t_utc" in df.columns
                          else t0 + timedelta(seconds=tmet)),
                "lat_deg": float(lat), "lon_deg": float(lon),
                "alt_m": float(alt) if alt is not None else np.nan,
                "speed_m_s": (float(row["speed_m_s"]) if "speed_m_s" in df.columns
                              and np.isfinite(float(row.get("speed_m_s", np.nan))) else np.nan),
                "note": str(row.get("note", "")).strip() if "note" in df.columns else "",
                "source": "sim/弹道事件表.csv",
                "file": file_meta(path),
            })
        events.sort(key=lambda e: e["t_met_s"])
    if not events:
        # 占位事件：公开时序（第三方帧测，仅供流程测试）
        defaults = [("起飞", 0.0), ("一二级分离", 160.5), ("二级点火", 163.0),
                    ("整流罩分离", 215.0), ("二级关机", 480.0), ("入轨", 480.0)]
        for name, tmet in defaults:
            lat, lon, alt = _interp_traj_xyz(traj, tmet)
            events.append({"event": name, "t_met_s": tmet,
                           "t_utc": t0 + timedelta(seconds=tmet),
                           "lat_deg": lat, "lon_deg": lon, "alt_m": alt,
                           "speed_m_s": float(np.interp(tmet, traj["t"], traj["speed"])),
                           "note": "占位事件（公开时序参考）",
                           "source": "占位", "file": {"exists": False}})
    return events


def find_event(events: list[dict], canonical: str):
    """按事件名模糊匹配（支持 data_contract 中文名与英文别名）。"""
    aliases = EVENT_ALIASES.get(canonical, [canonical])
    for e in events:
        name = str(e.get("event", ""))
        low = name.lower()
        for a in aliases:
            if a in name or a.lower() in low:
                return e
    return None


def load_debris(traj: dict, events: list[dict], t0: datetime) -> list[dict]:
    """读取 ``sim/debris_trajectories.csv``（每个残骸对象一条时间序列）。"""
    path = SIM_DIR / "debris_trajectories.csv"
    objects: list[dict] = []
    allowed = _debris_allowlist()      # v3.1：按 objects.json 动态允许（含 booster_N）
    if path.exists():
        import pandas as pd

        df = pd.read_csv(path, encoding="utf-8-sig")
        df.columns = [c.strip() for c in df.columns]
        for oid, g in df.groupby("object", sort=False):
            if str(oid) not in allowed:
                print(f"[sky] 跳过不在本任务 objects.json 中的残骸对象: {oid}", flush=True)
                continue
            g = g.sort_values("t_met_s")
            t = g["t_met_s"].to_numpy(dtype=float)
            lat = g["lat_deg"].to_numpy(dtype=float)
            lon = g["lon_deg"].to_numpy(dtype=float)
            alt = g["alt_m"].to_numpy(dtype=float)
            speed = (g["speed_m_s"].to_numpy(dtype=float) if "speed_m_s" in g.columns
                     else np.zeros_like(t))
            if "t_utc" in g.columns:
                tutc = [parse_utc(v) for v in g["t_utc"].astype(str).tolist()]
            else:
                tutc = _dt_list_from(t0, t)
            objects.append({
                "object": str(oid), "t": t, "lat": lat, "lon": lon, "alt": alt,
                "speed": speed, "t_utc": tutc,
                "ecef": geo.geodetic_to_ecef(lat, lon, alt),
                "source": "sim/debris_trajectories.csv", "file": file_meta(path),
            })
    if not objects:
        objects = build_placeholder_debris(traj, events, t0)
    return objects


def build_placeholder_debris(traj: dict, events: list[dict], t0: datetime) -> list[dict]:
    """占位残骸：分离点状态 → 无阻力抛物线下落（仅用于流程测试）。"""
    specs = [("core_stage", "一二级分离"),
             ("fairing_L", "整流罩分离"), ("fairing_R", "整流罩分离")]
    out = []
    t_arr, lat_arr, lon_arr, alt_arr = traj["t"], traj["lat"], traj["lon"], traj["alt"]
    for oid, ev_name in specs:
        ev = find_event(events, ev_name) or events[min(3, len(events) - 1)]
        tsep = float(ev["t_met_s"])
        k = int(np.argmin(np.abs(t_arr - tsep)))
        k2 = min(k + 20, len(t_arr) - 1)
        if k2 == k:
            k2 = max(k - 20, 0)
        v_h = geo.great_circle_distance_km(lat_arr[k], lon_arr[k],
                                           lat_arr[k2], lon_arr[k2]) * 1000.0 / \
            max(abs(t_arr[k2] - t_arr[k]), 1e-6)
        v_up = (alt_arr[k2] - alt_arr[k]) / max(abs(t_arr[k2] - t_arr[k]), 1e-6)
        heading = geo.initial_bearing_deg(lat_arr[k], lon_arr[k],
                                          lat_arr[k2], lon_arr[k2])
        dt = np.arange(0.0, 900.0, 1.0)
        alt_t = alt_arr[k] + v_up * dt - 0.5 * 9.6 * dt ** 2
        valid = np.where(alt_t <= 0)[0]
        n_end = int(valid[0]) + 1 if valid.size else len(dt)
        dt = dt[:n_end]
        alt_t = alt_t[:n_end]
        dr = v_h * dt
        lat = np.empty_like(dt)
        lon = np.empty_like(dt)
        for i, d in enumerate(dr):
            lat[i], lon[i], _ = geo.geodesic_direct(lat_arr[k], lon_arr[k],
                                                    heading, d / 1000.0)
        if len(dt) > 1:
            alt_t[-1] = max(alt_t[-1], 0.0)
        out.append({
            "object": oid, "t": tsep + dt, "lat": lat, "lon": lon, "alt": alt_t,
            "speed": np.hypot(v_h, v_up - 9.6 * dt),
            "t_utc": _dt_list_from(t0, tsep + dt),
            "ecef": geo.geodetic_to_ecef(lat, lon, alt_t),
            "source": "占位（无阻力弹道）", "file": {"exists": False},
        })
    return out


def load_impacts(traj: dict, debris: list[dict]) -> list[dict]:
    """读取 ``sim/落点与射程.csv``（缺失时用残骸末点作为占位落点）。"""
    path = SIM_DIR / "落点与射程.csv"
    rows: list[dict] = []
    if path.exists():
        import pandas as pd

        df = pd.read_csv(path, encoding="utf-8-sig")
        df.columns = [c.strip() for c in df.columns]
        for _, r in df.iterrows():
            try:
                rows.append({
                    "object": str(r["object"]),
                    "impact_t_met_s": float(r["impact_t_met_s"]),
                    "impact_t_utc": (parse_utc(r["impact_t_utc"])
                                     if "impact_t_utc" in df.columns else None),
                    "lat_deg": float(r["lat_deg"]), "lon_deg": float(r["lon_deg"]),
                    "great_circle_km": float(r.get("great_circle_km", np.nan)),
                    "downrange_km": float(r.get("downrange_km", np.nan)),
                    "crossrange_km": float(r.get("crossrange_km", np.nan)),
                    "zone_id": str(r.get("zone_id", "")),
                    "inside_zone": (str(r.get("inside_zone", "")).strip().lower()
                                    in ("true", "1", "yes", "是")),
                    "dist_to_boundary_km": float(r.get("dist_to_boundary_km", np.nan)),
                    "note": str(r.get("note", "")),
                    "source": "sim/落点与射程.csv",
                })
            except (KeyError, TypeError, ValueError):
                continue
    if not rows:
        for obj in debris:
            rows.append({
                "object": obj["object"],
                "impact_t_met_s": float(obj["t"][-1]),
                "impact_t_utc": obj["t_utc"][-1] if obj["t_utc"] else None,
                "lat_deg": float(obj["lat"][-1]), "lon_deg": float(obj["lon"][-1]),
                "great_circle_km": np.nan, "downrange_km": np.nan,
                "crossrange_km": np.nan, "zone_id": "", "inside_zone": False,
                "dist_to_boundary_km": np.nan,
                "note": "占位落点（残骸末点）", "source": "占位",
            })
    return rows


def load_objects() -> dict:
    """读取 ``sim/objects.json`` → {object_id: {"name": 中文名, "color": ...}}。"""
    doc = load_json(SIM_DIR / "objects.json", default=None)
    out: dict[str, dict] = {}
    if isinstance(doc, list):
        items = doc
    elif isinstance(doc, dict):
        if isinstance(doc.get("objects"), list):
            items = doc["objects"]
        else:
            items = [dict(id=k, **(v if isinstance(v, dict) else {"name": v}))
                     for k, v in doc.items()]
    else:
        items = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        oid = (item.get("id") or item.get("object") or item.get("obj")
               or item.get("name_en"))
        if not oid:
            continue
        out[str(oid)] = {
            "name": str(item.get("name_zh") or item.get("name_cn")
                        or item.get("name") or item.get("name_cn")
                        or OBJ_DEFAULT_NAMES.get(str(oid), str(oid))),
            "color": item.get("color") or _PALETTE_DEFAULT[i % len(_PALETTE_DEFAULT)],
        }
    for oid, nm in OBJ_DEFAULT_NAMES.items():
        out.setdefault(oid, {"name": nm, "color": _PALETTE_DEFAULT[0]})
    return out


# --------------------------------------------------------------------------- #
# 3b. 入轨后轨道段（可选）：用 sim/orbit_track.csv 求真正的几何落下时刻
# --------------------------------------------------------------------------- #
def load_orbit_track():
    """读取 ``<SIM_DIR>/orbit_track.csv``（入轨后 1.5 圈）；不存在返回 None。

    该文件只用于**补充**"上升段数据结束之后火箭何时真正落到地平线以下"，
    不混入 0.5 s 弹道 CSV（避免两种采样口径混用）。
    """
    path = SIM_DIR / "orbit_track.csv"
    if not path.exists():
        return None
    import pandas as pd

    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
    except Exception as exc:  # noqa: BLE001
        print(f"[sky] 警告: 读取 {_rel(path)} 失败（{exc}）", flush=True)
        return None
    df.columns = [c.strip() for c in df.columns]
    need = ["t_met_s", "lat_deg", "lon_deg", "alt_m"]
    if any(c not in df.columns for c in need):
        print(f"[sky] 警告: {_rel(path)} 缺列，跳过轨道段延伸", flush=True)
        return None
    t = df["t_met_s"].to_numpy(dtype=float)
    lat = df["lat_deg"].to_numpy(dtype=float)
    lon = df["lon_deg"].to_numpy(dtype=float)
    alt = df["alt_m"].to_numpy(dtype=float)
    order = np.argsort(t, kind="stable")
    t, lat, lon, alt = t[order], lat[order], lon[order], alt[order]
    if t.size < 5:
        return None
    return {"t": t, "lat": lat, "lon": lon, "alt": alt,
            "ecef": geo.geodetic_to_ecef(lat, lon, alt),
            "file": file_meta(path), "rows": int(t.size)}


def compute_orbit_setting(obs: dict, orbit, hz: np.ndarray,
                          t_ascent_end: float, t0: datetime):
    """用 orbit_track 求上升段数据结束之后**真正的几何落下时刻**（el = 遮挡角）。

    Returns
    -------
    dict or None
        ``{"set": True, "t_met_s", "t_utc", "t_bj", "az_deg", "el_deg", ...}``；
        轨道段内未出现落下时返回 ``{"set": False, ...}``。
    """
    if not orbit or orbit["t"].size < 5:
        return None
    ecef = orbit["ecef"]
    t = orbit["t"]
    az, el = geo.az_el_from_observer(obs["lat_deg"], obs["lon_deg"], obs["alt_m"], ecef)
    az = np.asarray(az, dtype=float)
    el = np.asarray(el, dtype=float)
    block = horizon_at(hz, az)
    mask = (t > float(t_ascent_end) + 0.5) & (el <= block)
    base = {"t_span_s": [float(t[0]), float(t[-1])], "n_points": int(t.size)}
    if not np.any(mask):
        return dict(base, set=False,
                    note="轨道路段数据内火箭一直在地形遮挡线以上（未出现落下）")
    idx = int(np.where(mask)[0][0])
    if idx == 0:
        t_set = float(t[0])
        st = {"az_deg": float(az[0]), "el_deg": float(el[0]),
              "slant_range_km": float(np.linalg.norm(
                  ecef[0] - np.asarray(geo.geodetic_to_ecef(
                      obs["lat_deg"], obs["lon_deg"], obs["alt_m"]), dtype=float)) / 1000.0),
              "terrain_block_deg": float(block[0])}
    else:
        ecef_fun = _make_ecef_interp(t, ecef)

        def f_vis(tq):
            p = ecef_fun([tq])[0]
            a, e = geo.az_el_from_observer(obs["lat_deg"], obs["lon_deg"],
                                           obs["alt_m"], p)
            return float(e) - float(horizon_at(hz, float(a)))

        t_set = _refine_crossing(f_vis, float(t[idx - 1]), float(t[idx]), tol=0.05)
        st = _state_at(obs, hz, ecef_fun, t_set, t0)
    return dict(base, set=True, t_met_s=float(t_set),
                t_utc=iso_utc(t0 + timedelta(seconds=float(t_set))),
                t_bj=bj_str(t0 + timedelta(seconds=float(t_set))),
                az_deg=float(st["az_deg"]), el_deg=float(st["el_deg"]),
                slant_range_km=st.get("slant_range_km"),
                terrain_block_deg=float(st["terrain_block_deg"]))


# --------------------------------------------------------------------------- #
# 4. 地形遮挡 / 几何 / 可见性 / 阳光
# --------------------------------------------------------------------------- #
def compute_horizon(obs: dict) -> tuple[np.ndarray, dict | None, str]:
    """观测点 720 方位地形遮挡角（SRTM 1″ DEM，90 m 步长，200 km 半径）。

    Returns
    -------
    (horizon_deg, details, note)
        horizon_deg: (720,) float64 [deg]；details: dem.terrain_horizon_deg 的细节；
        note: 数据来源/退化说明字符串。
    """
    info = None
    if obs["id"] not in ("custom",) and obs.get("cache_file"):
        try:
            info = dem.load_observer_cache(obs["id"])
        except Exception:  # noqa: BLE001
            info = None
    try:
        hz, details = dem.terrain_horizon_deg(
            obs["lat_deg"], obs["lon_deg"], obs["alt_m"],
            az_list_deg=GRID_AZ, max_range_km=HORIZON_RADIUS_KM,
            step_m=HORIZON_STEP_M, observer_id=obs["id"], info=info,
            return_details=True,
        )
        hz = np.asarray(hz, dtype=float)
        finite = np.isfinite(hz)
        if finite.mean() < 0.9:
            raise ValueError("DEM 覆盖不足")
        hz = np.where(finite, hz, -0.1)
        src = f"SRTMGL1 1″ DEM（{'3″ 缓存+近场 1″ 修正' if info else '1″ 瓦片'}，90 m 步长，200 km）"
        note = f"{src}；缓存={'有' if info else '无'}；遮挡角范围 {hz.min():+.2f}°…{hz.max():+.2f}°"
        return hz, details, note
    except Exception as exc:  # noqa: BLE001
        hz = np.full(GRID_AZ.size, -0.1)
        print(f"[sky] 警告: {obs['id']} DEM 遮挡计算失败（{exc}）→ 退化为平地遮挡角 -0.1°")
        return hz, None, f"DEM 不可用（{exc}），退化为平地遮挡角 -0.1°"


def horizon_at(hz: np.ndarray, az_deg) -> np.ndarray:
    """按方位角插值地形遮挡角（0.5° 网格，处理 360° 回绕）。"""
    x = np.concatenate([GRID_AZ, [360.0]])
    y = np.concatenate([hz, [hz[0]]])
    return np.interp(np.mod(np.asarray(az_deg, dtype=float), 360.0), x, y)


def _make_ecef_interp(t: np.ndarray, ecef: np.ndarray):
    """时间 → ECEF 位置线性插值闭包（用于二分细化；曲线在 0.5 s 内足够平滑）。"""
    def f(tq) -> np.ndarray:
        tq = np.atleast_1d(np.asarray(tq, dtype=float))
        tq = np.clip(tq, t[0], t[-1])
        return np.column_stack([np.interp(tq, t, ecef[:, k]) for k in range(3)])
    return f


def _refine_crossing(fun, ta: float, tb: float, tol: float = BISECT_TOL_S) -> float:
    """二分求 fun(t)=0 的根（要求两端异号）。"""
    fa, fb = float(fun(ta)), float(fun(tb))
    if not np.isfinite(fa):
        return ta
    if not np.isfinite(fb):
        return tb
    if fa == 0.0:
        return ta
    if fb == 0.0:
        return tb
    if fa * fb > 0:  # 理论上不会出现；退回中点
        return 0.5 * (ta + tb)
    while abs(tb - ta) > tol:
        tm = 0.5 * (ta + tb)
        fm = float(fun(tm))
        if fm == 0.0:
            return tm
        if fa * fm <= 0:
            tb, fb = tm, fm
        else:
            ta, fa = tm, fm
    return 0.5 * (ta + tb)


def _refine_max(fun, t_guess: float, tmin: float, tmax: float,
                half: float = 1.5, tol: float = 1e-3) -> tuple[float, float]:
    """黄金分割求局部最大值（fun: 时间 → 高度角）。"""
    a = max(tmin, t_guess - half)
    b = min(tmax, t_guess + half)
    if b - a < 1e-6:
        return t_guess, float(fun(t_guess))
    gr = (math.sqrt(5.0) - 1.0) / 2.0
    c = b - gr * (b - a)
    d = a + gr * (b - a)
    fc, fd = float(fun(c)), float(fun(d))
    while abs(b - a) > tol:
        if fc > fd:
            b, d, fd = d, c, fc
            c = b - gr * (b - a)
            fc = float(fun(c))
        else:
            a, c, fc = c, d, fd
            d = a + gr * (b - a)
            fd = float(fun(d))
    tm = 0.5 * (a + b)
    return tm, float(fun(tm))


def _visible_intervals(t: np.ndarray, mask: np.ndarray, fun) -> list[dict]:
    """由布尔可见掩码 + 连续判据函数，给出可见区间并对边界二分细化。"""
    t = np.asarray(t, dtype=float)
    mask = np.asarray(mask, dtype=bool)
    n = t.size
    out: list[dict] = []
    i = 0
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and mask[j + 1]:
            j += 1
        if i == 0:
            t_start, refined_start = t[0], False
        else:
            t_start = _refine_crossing(fun, t[i - 1], t[i])
            refined_start = True
        if j == n - 1:
            t_end, refined_end = t[-1], False
        else:
            t_end = _refine_crossing(fun, t[j], t[j + 1])
            refined_end = True
        out.append({"t_start": float(t_start), "t_end": float(t_end),
                    "refined_start": refined_start, "refined_end": refined_end})
        i = j + 1
    return out


def _shadow_flags(ecef, sun_ecef_unit,
                  sun_radius_deg: float = SUN_ANGULAR_RADIUS_DEG) -> dict:
    """地影判定（圆柱 + 圆锥本影/半影），返回逐点布尔掩码与几何量。

    模型
    ----
    * **圆柱**：地球视为半径 R=6371.0088 km 的球、太阳光平行 → 射线被地球遮挡
      ⇔ ``proj = r·s < 0`` 且到地影轴距离 ``perp < R``。该判据与"视地平下沉"
      （高度 h 处可看见低于地平 e 的太阳 ⇔ h > R(1/cos|e|-1)）完全等价；
    * **圆锥**：考虑太阳视半径 α=0.2666°，入影深度 d=-proj 处
      本影半径 = R - d·tanα、半影半径 = R + d·tanα。LEO（d<5000 km）内
      与圆柱的差 <25 km（角度 <0.02°）；两者都输出，正文以圆柱为准并注明差异。
    """
    r = np.asarray(ecef, dtype=float)
    s = np.asarray(sun_ecef_unit, dtype=float)
    proj = np.einsum("ij,ij->i", r, s)
    perp = np.sqrt(np.maximum(np.sum(r * r, axis=1) - proj * proj, 0.0))
    behind = proj < 0.0
    depth = np.where(behind, -proj, 0.0)
    tan_a = math.tan(math.radians(sun_radius_deg))
    r_umbra = R_EARTH_M - depth * tan_a
    r_penumbra = R_EARTH_M + depth * tan_a
    in_umbra = behind & (perp < r_umbra)
    in_penumbra = behind & (perp < r_penumbra) & ~in_umbra
    in_cylinder = behind & (perp < R_EARTH_M)
    return {"proj_m": proj, "perp_m": perp, "in_umbra": in_umbra,
            "in_penumbra": in_penumbra, "in_cylinder": in_cylinder,
            "depth_m": depth, "umbra_radius_m": r_umbra}


def _sunlit_mask(ecef, sun_ecef_unit) -> np.ndarray:
    """火箭是否受阳光照射（未被本影遮挡）→ 布尔数组（True = 受照）。"""
    return ~_shadow_flags(ecef, sun_ecef_unit)["in_umbra"]


def _sunlit_altitude_km(ground_sun_el_deg) -> np.ndarray:
    """地影边界高度 [km]：地面太阳几何高度 e<0 时，竖直上方需高于 R(1/cos|e|-1) 才受照。

    这是晨昏线处"火箭何时进入阳光"的解析判据（与圆柱/圆锥本影模型的竖直剖面等价），
    用于绘制"火箭高度 vs 地影边界高度"示意。
    """
    e = np.asarray(ground_sun_el_deg, dtype=float)
    out = np.zeros_like(e)
    neg = e < 0.0
    out[neg] = R_EARTH_M * (1.0 / np.cos(np.radians(np.minimum(-e[neg], 89.0))) - 1.0)
    return out / 1000.0


def _state_at(obs: dict, hz: np.ndarray, ecef_fun, tq: float, t0: datetime) -> dict:
    """给定时刻的视位置状态（方位/高度/斜距/地面距离/遮挡角/可见性）。"""
    p = ecef_fun([tq])[0]
    az, el = geo.az_el_from_observer(obs["lat_deg"], obs["lon_deg"], obs["alt_m"], p)
    lat, lon, alt = geo.ecef_to_geodetic(p[0], p[1], p[2])
    obs_ecef = geo.geodetic_to_ecef(obs["lat_deg"], obs["lon_deg"], obs["alt_m"])
    slant = float(np.linalg.norm(p - np.asarray(obs_ecef, dtype=float)) / 1000.0)
    try:
        ground = float(geo.geodesic_distance(obs["lat_deg"], obs["lon_deg"],
                                             float(lat), float(lon)))
    except Exception:  # noqa: BLE001
        ground = float(geo.great_circle_distance_km(obs["lat_deg"], obs["lon_deg"],
                                                    float(lat), float(lon)))
    block = float(horizon_at(hz, float(az)))
    return {
        "t_met_s": float(tq),
        "t_utc": iso_utc(t0 + timedelta(seconds=float(tq))),
        "t_bj": bj_str(t0 + timedelta(seconds=float(tq))),
        "lat_deg": float(lat), "lon_deg": float(lon), "alt_m": float(alt),
        "az_deg": float(az), "el_deg": float(el),
        "slant_range_km": slant, "ground_range_km": ground,
        "terrain_block_deg": block,
        "geometrically_visible": bool(el > block),
    }


def _angular_rate(fun, tq: float, half: float = 0.25) -> float:
    """判据函数的局部变化率 [deg/s]（用于首次出现时刻不确定度）。"""
    f1 = float(fun(max(tq - half, 0.0)))
    f2 = float(fun(tq + half))
    return (f2 - f1) / (2.0 * half)


# --------------------------------------------------------------------------- #
# 5. 单观测点视图构建
# --------------------------------------------------------------------------- #
def build_view(obs: dict, traj: dict, t0: datetime, events: list[dict],
               debris: list[dict], impacts: list[dict], objects: dict,
               sun_data: dict) -> dict:
    """计算一个观测点的全部几何/可见性/事件/残骸视位置（不绘图）。"""
    t = traj["t"]
    ecef = traj["ecef"]
    hz, hz_details, hz_note = compute_horizon(obs)

    # ---- 全弹道方位/高度角（ECEF 三维几何，含地球曲率与目标高度） ----
    az, el = geo.az_el_from_observer(obs["lat_deg"], obs["lon_deg"], obs["alt_m"], ecef)
    az = np.asarray(az, dtype=float)
    el = np.asarray(el, dtype=float)
    obs_ecef = np.asarray(geo.geodetic_to_ecef(obs["lat_deg"], obs["lon_deg"],
                                               obs["alt_m"]), dtype=float)
    slant = np.linalg.norm(ecef - obs_ecef, axis=1) / 1000.0
    ground = geo.great_circle_distance_km(obs["lat_deg"], obs["lon_deg"],
                                          traj["lat"], traj["lon"])
    block = horizon_at(hz, az)

    vis = el > block
    above = el > 0.0
    sunlit = np.asarray(sun_data["sunlit"], dtype=bool)
    umbra = np.asarray(sun_data["umbra"], dtype=bool)
    ecef_fun = _make_ecef_interp(t, ecef)

    def f_vis(tq):
        p = ecef_fun([tq])[0]
        a, e = geo.az_el_from_observer(obs["lat_deg"], obs["lon_deg"], obs["alt_m"], p)
        return float(e) - float(horizon_at(hz, float(a)))

    def f_above(tq):
        p = ecef_fun([tq])[0]
        _, e = geo.az_el_from_observer(obs["lat_deg"], obs["lon_deg"], obs["alt_m"], p)
        return float(e)

    def f_el(tq):
        return f_above(tq)

    def f_sun_vis(tq):
        if not bool(_sunlit_at(sun_data, tq)):
            return -1e6
        return f_vis(tq)

    intervals = _visible_intervals(t, vis, f_vis)
    intervals_horizon = _visible_intervals(t, above, f_above)
    sun_vis = vis & sunlit
    sun_vis_intervals = _visible_intervals(t, sun_vis, f_sun_vis)

    # ---- 首次出现 / 消失 + 不确定度 ----
    first = last = None
    rate = 0.0
    unc_parts: dict = {}
    unc_reported = None
    if intervals:
        first = _state_at(obs, hz, ecef_fun, intervals[0]["t_start"], t0)
        last = _state_at(obs, hz, ecef_fun, intervals[-1]["t_end"], t0)
        first["on_data_start"] = not intervals[0]["refined_start"]
        last["on_data_end"] = not intervals[-1]["refined_end"]
        first["geometrically_visible"] = True   # 首次出现点按定义在地形遮挡线之上
        last["geometrically_visible"] = True
        rate = abs(_angular_rate(f_vis, intervals[0]["t_start"]))
        rng_ridge = 50.0
        if hz_details is not None:
            try:
                rng_ridge = float(np.interp(first["az_deg"], GRID_AZ,
                                            np.asarray(hz_details["range_km"], float)))
            except Exception:  # noqa: BLE001
                rng_ridge = 50.0
        rng_m = max(float(rng_ridge), 0.09) * 1000.0
        dem_unc = min(math.degrees(ANGLE_UNC_COMPONENTS["dem_height_m"] / rng_m), 1.0)
        obs_unc = min(math.degrees(ANGLE_UNC_COMPONENTS["obs_height_m"] / rng_m), 0.5)
        ang_unc = math.sqrt(dem_unc ** 2 + obs_unc ** 2
                            + ANGLE_UNC_COMPONENTS["refraction_deg"] ** 2
                            + ANGLE_UNC_COMPONENTS["building_deg"] ** 2)
        t_unc_calc = ang_unc / rate if rate > 1e-4 else float("inf")
        unc_parts = {
            "angle_unc_deg": ang_unc,
            "dem_angle_unc_deg": dem_unc,
            "obs_height_angle_unc_deg": obs_unc,
            "dem_at_ridge_m": ANGLE_UNC_COMPONENTS["dem_height_m"],
            "ridge_range_km": float(rng_ridge),
            "angular_rate_deg_s": float(rate),
            "t_unc_calc_s": float(t_unc_calc) if np.isfinite(t_unc_calc) else None,
        }
        unc_reported = (float(np.clip(t_unc_calc, 1.0, 3.0))
                        if np.isfinite(t_unc_calc) else 3.0)

        # ---- 敏感性：忽略近场 <0.5 km 的 DEM 单元（开阔场地假设）时的首次出现时刻 ----
        open_field = None
        try:
            cache_info = None
            if obs.get("cache_file"):
                cache_info = dem.load_observer_cache(obs["id"])
            hz_open = -9.9
            for daz in (-2.0, -1.0, 0.0, 1.0, 2.0):
                prof = dem.terrain_profile(
                    obs["lat_deg"], obs["lon_deg"], obs["alt_m"],
                    (first["az_deg"] + daz) % 360.0,
                    max_range_km=HORIZON_RADIUS_KM, step_m=HORIZON_STEP_M,
                    observer_id=obs["id"], info=cache_info, sea_level_clip=True)
                el_prof = np.asarray(prof["el_deg"], dtype=float)
                dist_prof = np.asarray(prof["dist_km"], dtype=float)
                m_far = dist_prof >= 0.5
                if m_far.any():
                    hz_open = max(hz_open, float(np.max(el_prof[m_far])))
            t_lo = max(float(t[0]), float(first["t_met_s"]) - 120.0)
            t_hi = min(float(t[-1]), float(first["t_met_s"]) + 180.0)
            g_lo = float(f_above(t_lo)) - hz_open
            g_hi = float(f_above(t_hi)) - hz_open
            if g_lo < 0.0 < g_hi:
                t_open = _refine_crossing(lambda tq: f_above(tq) - hz_open,
                                          t_lo, t_hi, tol=0.05)
                open_field = {
                    "horizon_open_deg": float(hz_open),
                    "horizon_nearfield_deg": float(first["terrain_block_deg"]),
                    "t_met_s": float(t_open),
                    "delta_s": float(first["t_met_s"] - t_open),
                }
        except Exception as exc:  # noqa: BLE001
            print(f"[sky] 警告: 近场敏感性计算失败（{exc}）", flush=True)
        unc_parts["open_field"] = open_field

    # ---- 最大高度角（v3.2.3：分"可见时段内"与"全弹道几何"两个口径） ----
    # 掠射观测点（观测点离弹道很远、低仰角斜视）会出现：几何最大高度角出现在
    # 火箭已被地形/地球曲率遮挡之后。此时几何最大值不能当"最大高度角"上报，也不能拿它
    # 做 first ≤ max ≤ last 的自检（否则会误判为错误）。
    k_geom = int(np.argmax(el))
    t_geom, el_geom = _refine_max(f_el, float(t[k_geom]), float(t[0]), float(t[-1]))
    geom_state = _state_at(obs, hz, ecef_fun, t_geom, t0)
    geom_state["geometrically_visible"] = bool(el_geom > geom_state["terrain_block_deg"])
    geom_state["basis"] = "geometric_full_trajectory"

    max_state, vis_el = None, None
    for iv in intervals:
        a, b = float(iv["t_start"]), float(iv["t_end"])
        m_in = (t >= a) & (t <= b)
        if np.any(m_in):
            idx = np.where(m_in)[0]
            t_guess = float(t[idx[int(np.argmax(el[idx]))]])
        else:
            t_guess = 0.5 * (a + b)
        tq, eq = _refine_max(f_el, t_guess, a, b)
        if vis_el is None or eq > vis_el:
            vis_el, max_state = eq, _state_at(obs, hz, ecef_fun, tq, t0)
    geom_masked = False
    if max_state is not None:
        max_state["geometrically_visible"] = True
        max_state["basis"] = "visible_interval"
        geom_masked = not any(float(iv["t_start"]) <= t_geom <= float(iv["t_end"])
                              for iv in intervals)
    else:
        # 全弹道几何不可见：保留几何最大值（图上仍标注），但明确标注来源
        max_state = geom_state
        max_state["basis"] = "geometric_only(no_visible_interval)"
    max_state["geometric_max_masked"] = bool(geom_masked)
    max_state["geom_max_el_deg"] = round(float(el_geom), 3)
    max_state["geom_max_t_met_s"] = round(float(t_geom), 3)

    visible_duration = float(sum(iv["t_end"] - iv["t_start"] for iv in intervals))
    duration_horizon = float(sum(iv["t_end"] - iv["t_start"] for iv in intervals_horizon))

    # ---- 太阳 / 地影 / 晨昏 ----
    dt_list = sun_data["t_utc"]
    obs_sun_el = np.array([float(geo.solar_elevation_deg(obs["lat_deg"], obs["lon_deg"], d))
                           for d in dt_list])
    sun_vis_duration = float(sum(iv["t_end"] - iv["t_start"] for iv in sun_vis_intervals))
    idx_sun = np.where(sunlit)[0]
    first_sunlit = None
    if idx_sun.size:
        k = int(idx_sun[0])
        first_sunlit = {
            "t_met_s": float(t[k]),
            "t_utc": iso_utc(t0 + timedelta(seconds=float(t[k]))),
            "t_bj": bj_str(t0 + timedelta(seconds=float(t[k]))),
            "alt_km": float(traj["alt"][k] / 1000.0),
            "obs_sun_el_deg": float(obs_sun_el[k]),
        }

    def _f_dark(thr):
        def f(tq):
            return float(geo.solar_elevation_deg(
                obs["lat_deg"], obs["lon_deg"],
                _SUN_CACHE["t0"] + timedelta(seconds=float(tq)))) - thr
        return f

    def _twilight_stat(thr: float) -> dict:
        m = vis & sunlit & (obs_sun_el < thr)
        ivs = _visible_intervals(t, m, _f_dark(thr))
        return {
            "threshold_deg": float(thr),
            "condition_met": bool(np.min(obs_sun_el) < thr),
            "duration_s": float(sum(iv["t_end"] - iv["t_start"] for iv in ivs)),
            "intervals": ivs,
        }

    dusk = _twilight_stat(SUN_VISIBLE_OBS_EL_DEG)
    twilight = {nm: _twilight_stat(thr) for nm, thr in TWILIGHT_LEVELS_DEG.items()}
    el_t0 = float(obs_sun_el[0])
    if el_t0 > SUN_VISIBLE_OBS_EL_DEG:
        twilight_state = "白天/日面已升出地平线"
    elif el_t0 > TWILIGHT_LEVELS_DEG["civil"]:
        twilight_state = "民用晨昏（曙光，太阳在地平线下 0°~-6°）"
    elif el_t0 > TWILIGHT_LEVELS_DEG["nautical"]:
        twilight_state = "航海晨昏（-6°~-12°）"
    else:
        twilight_state = "天文晨昏/夜（<-12°）"
    sun = {
        "observer_sun_el_t0_deg": el_t0,
        "observer_sun_az_t0_deg": float(geo.solar_azimuth_deg(obs["lat_deg"], obs["lon_deg"], t0)),
        "observer_sun_el_min_deg": float(np.min(obs_sun_el)),
        "observer_sun_el_max_deg": float(np.max(obs_sun_el)),
        "observer_sun_el_end_deg": float(obs_sun_el[-1]),
        "twilight_state_t0": twilight_state,
        "twilight_levels_deg": dict(TWILIGHT_LEVELS_DEG),
        "dusk_condition": dusk,
        "twilight_visible": twilight,
        "rocket_sunlit_t0": bool(sunlit[0]),
        "rocket_sunlit_fraction": float(np.mean(sunlit)),
        "rocket_umbra_fraction": float(np.mean(umbra)),
        "first_sunlit": first_sunlit,
        "sun_visible_duration_s": sun_vis_duration,
        "sun_visible_intervals": sun_vis_intervals,
        "shadow_models": ("圆柱本影（R=6371 km、平行光）为主判据；圆锥本影/半影"
                          "（太阳视半径 0.267°）对照，LEO 段差异 <0.02°"),
    }

    # ---- 事件视位置 ----
    ev_views = []
    for e in events:
        tq = float(np.clip(e["t_met_s"], t[0], t[-1]))
        st = _state_at(obs, hz, ecef_fun, tq, t0)
        idx = int(np.argmin(np.abs(t - tq)))
        st["event"] = e["event"]
        st["in_data_range"] = bool(t[0] - 1e-9 <= e["t_met_s"] <= t[-1] + 1e-9)
        st["sunlit"] = bool(sunlit[idx])
        st["obs_sun_el_deg"] = float(obs_sun_el[idx])
        st["note"] = e.get("note", "")
        ev_views.append(st)

    # ---- 残骸视位置 / 落点 ----
    impact_by_obj = {str(r["object"]): r for r in impacts}
    deb_views = []
    for obj in debris:
        oid = str(obj["object"])
        az_d, el_d = geo.az_el_from_observer(obs["lat_deg"], obs["lon_deg"],
                                             obs["alt_m"], obj["ecef"])
        az_d = np.asarray(az_d, dtype=float)
        el_d = np.asarray(el_d, dtype=float)
        block_d = horizon_at(hz, az_d)
        vis_d = el_d > block_d
        imp = impact_by_obj.get(oid)
        imp_state = None
        if imp is not None:
            az_i, el_i = geo.az_el_from_observer(
                obs["lat_deg"], obs["lon_deg"], obs["alt_m"],
                geo.geodetic_to_ecef(imp["lat_deg"], imp["lon_deg"], 0.0))
            blk_i = float(horizon_at(hz, float(az_i)))
            imp_state = {
                "t_met_s": float(imp["impact_t_met_s"]),
                "t_utc": (iso_utc(imp["impact_t_utc"]) if imp.get("impact_t_utc")
                          else iso_utc(t0 + timedelta(seconds=float(imp["impact_t_met_s"])))),
                "t_bj": (bj_str(imp["impact_t_utc"]) if imp.get("impact_t_utc")
                         else bj_str(t0 + timedelta(seconds=float(imp["impact_t_met_s"])))),
                "lat_deg": float(imp["lat_deg"]), "lon_deg": float(imp["lon_deg"]),
                "az_deg": float(az_i), "el_deg": float(el_i),
                "terrain_block_deg": blk_i,
                "geometrically_visible": bool(float(el_i) > blk_i),
                "great_circle_km": float(imp.get("great_circle_km", np.nan)),
                "zone_id": imp.get("zone_id", ""),
                "inside_zone": bool(imp.get("inside_zone", False)),
                "dist_to_boundary_km": float(imp.get("dist_to_boundary_km", np.nan)),
                "note": imp.get("note", ""),
                "source": imp.get("source", ""),
            }
        deb_views.append({
            "object": oid,
            "name": objects.get(oid, {}).get("name", OBJ_DEFAULT_NAMES.get(oid, oid)),
            "t": obj["t"], "az_deg": az_d, "el_deg": el_d,
            "terrain_block_deg": block_d,
            "geometrically_visible": vis_d,
            "visible_fraction": float(np.mean(vis_d)) if vis_d.size else 0.0,
            "n_points": int(obj["t"].size),
            "t_min_s": float(obj["t"][0]), "t_max_s": float(obj["t"][-1]),
            "alt_min_km": float(np.min(obj["alt"]) / 1000.0),
            "alt_max_km": float(np.max(obj["alt"]) / 1000.0),
            "impact": imp_state,
            "source": obj.get("source", ""),
        })

    horizon_nearfield = None
    if hz_details is not None:
        try:
            rr = np.asarray(hz_details["range_km"], dtype=float)
            horizon_nearfield = {
                "fraction_within_1km": float(np.mean(rr < 1.0)),
                "fraction_within_10km": float(np.mean(rr < 10.0)),
                "median_range_km": float(np.median(rr)),
            }
        except Exception:  # noqa: BLE001
            horizon_nearfield = None

    return {
        "observer": obs,
        "horizon_deg": hz,
        "horizon_details": hz_details,
        "horizon_note": hz_note,
        "horizon_stats": {"min": float(np.min(hz)), "max": float(np.max(hz)),
                          "mean": float(np.mean(hz)),
                          "max_az_deg": float(GRID_AZ[int(np.argmax(hz))]),
                          "median": float(np.median(hz))},
        "horizon_nearfield": horizon_nearfield,
        "arrays": {"t": t, "az": az, "el": el, "slant": slant, "ground": ground,
                   "block": block, "visible": vis, "above": above,
                   "obs_sun_el": obs_sun_el, "sunlit": sunlit, "umbra": umbra,
                   "sun_visible": sun_vis},
        "intervals": intervals,
        "intervals_horizon": intervals_horizon,
        "intervals_sun": sun_vis_intervals,
        "first": first,
        "last": last,
        "max": max_state,
        "max_geom": geom_state,
        "visible_duration_s": visible_duration,
        "visible_duration_no_terrain_s": duration_horizon,
        "sun_visible_duration_s": sun_vis_duration,
        "appear_uncertainty": {"reported_s": unc_reported, "parts": unc_parts},
        "sun": sun,
        "events": ev_views,
        "debris": deb_views,
    }


_SUN_CACHE: dict = {}


def _sunlit_at(sun_data: dict, tq: float) -> bool:
    """在给定时刻插值"火箭是否受照"（用于阳光可见区间二分的连续化近似）。"""
    t = sun_data["t"]
    idx = int(np.clip(np.searchsorted(t, tq) - 0, 0, len(t) - 1))
    if tq <= t[0]:
        return bool(sun_data["sunlit"][0])
    if tq >= t[-1]:
        return bool(sun_data["sunlit"][-1])
    i0 = max(idx - 1, 0)
    return bool(sun_data["sunlit"][i0])


def _obs_sun_el_at(obs: dict, tq: float) -> float:
    """给定 MET 时刻观测点太阳几何高度角 [deg]。"""
    t0 = _SUN_CACHE["t0"]
    return float(geo.solar_elevation_deg(obs["lat_deg"], obs["lon_deg"],
                                         t0 + timedelta(seconds=float(tq))))


# --------------------------------------------------------------------------- #
# 6. 绘图
# --------------------------------------------------------------------------- #
def _text_width_deg(text: str, fontsize: float, span_deg: float,
                    axes_w_in: float = 12.1) -> float:
    """估算一行文字在数据坐标宽度（度）。用于给标注选边、避免出界/重叠。"""
    lines = str(text).split("\n")
    widest = max(lines, key=len) if lines else ""
    cjk = sum(1 for ch in widest if ord(ch) > 0x2E80)
    ascii_n = len(widest) - cjk
    em = cjk + 0.55 * ascii_n
    return em * fontsize / 72.0 / axes_w_in * span_deg


def _auto_side(x: float, x_lo: float, x_hi: float, text: str, fontsize: float,
               margin_deg: float = 2.0) -> str:
    """给标注选择文本在点的右/左侧（避免出界）：返回 'right' 或 'left'。"""
    span = max(x_hi - x_lo, 1e-6)
    w = _text_width_deg(text, fontsize, span)
    if (x - x_lo) < 0.22 * span:
        return "right"
    if (x_hi - x) < w + margin_deg:
        return "left"
    return "right"


def _annotation(ax, xy, text, color, offset=(10, 8), fontsize=7.4, ha="left", va="center"):
    """带指线的文字标注。"""
    ax.annotate(text, xy=xy, xytext=offset, textcoords="offset points",
                fontsize=fontsize, color=color, ha=ha, va=va, zorder=12,
                bbox=dict(boxstyle="round,pad=0.18", fc="white", ec="none", alpha=0.82),
                arrowprops=dict(arrowstyle="-", color=color, lw=0.6,
                                shrinkA=0, shrinkB=2))


def _empty_corner(az_s, el, x_lo, x_hi, y_lo, y_hi, frac: float = 0.5) -> str:
    """按轨迹点在 4 个角的占用数，选出最空的角（'ul'/'ur'/'ll'/'lr'），给信息框定位。"""
    az_s = np.asarray(az_s, dtype=float)
    ev = np.asarray(el, dtype=float)
    if az_s.size == 0:
        return "ul"
    xn = (az_s - x_lo) / max(x_hi - x_lo, 1e-9)
    yn = (ev - y_lo) / max(y_hi - y_lo, 1e-9)
    masks = {
        "ul": (xn < frac) & (yn > 1.0 - frac),
        "ur": (xn > 1.0 - frac) & (yn > 1.0 - frac),
        "ll": (xn < frac) & (yn < frac),
        "lr": (xn > 1.0 - frac) & (yn < frac),
    }
    counts = {k: int(np.sum(v)) for k, v in masks.items()}
    return min(counts, key=counts.get)


def plot_observer(view: dict, traj: dict, sun_data: dict, out_png: Path,
                  dpi: int = 160, placeholder: bool = False) -> dict:
    """绘制指定观测点的"方位角–高度角"天空轨迹图（主图 + 可见性时间轴 + 右侧 MET 色条）。"""
    obs = view["observer"]
    arr = view["arrays"]
    sun = view["sun"]
    t = arr["t"]
    az, el, block = arr["az"], arr["el"], arr["block"]
    vis = arr["visible"]
    t_end = float(t[-1])
    temp_tag = ("【临时版：使用夹具参考弹道，仅供流程验证】"
                if placeholder else "")

    # ---- 方位画窗（按轨迹/残骸/事件的方位圆周展开，处理 360°→0° 回绕） ----
    center = circular_mean_deg(az)
    az_s = unwrap_around(az, center)
    ev_main = [e for e in (find_event(view["events"], c) for c in _main_events(view["events"]))
               if e is not None]
    qs = [az_s] + [unwrap_around(dv["az_deg"], center) for dv in view["debris"]]
    all_az = np.concatenate([q for q in qs if np.size(q)])
    q_lo, q_hi = float(np.min(all_az)), float(np.max(all_az))
    if q_hi - q_lo < 25.0:
        mid = 0.5 * (q_lo + q_hi)
        q_lo, q_hi = mid - 12.5, mid + 12.5
    margin = max(4.0, 0.08 * (q_hi - q_lo))
    x_lo = math.floor((q_lo - margin) / 5.0) * 5.0
    x_hi = math.ceil((q_hi + margin) / 5.0) * 5.0
    if x_hi - x_lo > 330.0:
        x_lo, x_hi = 0.0, 360.0
    az_plot = np.linspace(x_lo, x_hi, 900)
    hz_plot = horizon_at(view["horizon_deg"], az_plot)

    # ---- y 轴范围 ----
    y_hi = float(np.ceil((max(float(np.max(el)), float(view["max"]["el_deg"]), 10.0)
                          + 5.0) / 5.0) * 5.0)
    y_hi = min(max(y_hi, 20.0), 90.0)
    y_lo = -3.0
    for dv in view["debris"]:
        if dv["impact"] is not None:
            y_lo = min(y_lo, dv["impact"]["el_deg"] - 2.2)
    y_lo = min(y_lo, float(np.min(el)) - 0.5)
    y_lo = max(y_lo, -12.0)

    # ---- 图幅 ----
    cmap = matplotlib.colormaps["turbo"]
    t_max_all = max(t_end, 1.0)
    for dv in view["debris"]:
        if dv["t"].size:
            t_max_all = max(t_max_all, float(np.max(dv["t"])))
    for ev in view["events"]:
        t_max_all = max(t_max_all, float(ev["t_met_s"]))
    t_max_all = float(np.ceil(t_max_all / 10.0) * 10.0)
    norm = Normalize(vmin=0.0, vmax=t_max_all)
    fig = plt.figure(figsize=(13.8, 10.0))
    gs = fig.add_gridspec(2, 2, width_ratios=[1.0, 0.026],
                          height_ratios=[1.0, 0.36],
                          left=0.065, right=0.945, top=0.878, bottom=0.145,
                          hspace=0.30, wspace=0.02)
    ax = fig.add_subplot(gs[0, 0])
    cax = fig.add_subplot(gs[:, 1])
    ax2 = fig.add_subplot(gs[1, 0])

    # ---- 标题 ----
    title1 = (f"{MISSION_TITLE} · 指定观测点天空轨迹图（方位角–高度角）{temp_tag}")
    subtitle = (f"观测点：{dms_str(obs['lat_deg'], obs['lon_deg'])}（{obs['lat_deg']:.5f}°N, "
                f"{obs['lon_deg']:.5f}°E），高程 {obs['alt_m']:.1f} m"
                f"（{obs['alt_source']}）  |  "
                f"T0 = {sun_data['t0_str']}（北京时 {sun_data['t0_bj']}）  |  "
                f"数据：{traj['source']}（0.5 s，{traj['rows']} 行）")
    fig.suptitle(title1, fontsize=14.2, y=0.975)
    fig.text(0.5, 0.925, subtitle, ha="center", fontsize=8.4, color="#444444")

    # ---- 主图：遮挡区 / 遮挡线 / 地平线 ----
    ax.fill_between(az_plot, y_lo, hz_plot, color="#8B4513", alpha=0.16,
                    lw=0, zorder=1)
    ax.plot(az_plot, hz_plot, color="#8B4513", lw=1.7, zorder=3,
            label="地形遮挡线（SRTM 1″ DEM + 地球曲率）")
    ax.axhline(0.0, color="#777777", lw=0.8, ls=":", zorder=2)
    ax.text(x_lo + 0.01 * (x_hi - x_lo), 0.6, "地平线 0°", fontsize=7.2,
            color="#777777", va="bottom", zorder=6)

    # ---- 主弹道（按 MET 着色） ----
    pts = np.column_stack([az_s, el])
    segs = np.stack([pts[:-1], pts[1:]], axis=1)
    lc = LineCollection(segs, cmap=cmap, norm=norm, lw=2.8,
                        array=0.5 * (t[:-1] + t[1:]), zorder=6)
    ax.add_collection(lc)
    inv = el <= block
    if np.any(inv[1:] & inv[:-1]):
        idx = np.where(inv)[0]
        ax.plot(az_s[idx], el[idx], color="#999999", lw=1.0, ls=(0, (2, 2)),
                alpha=0.55, zorder=4, label="几何不可见段（遮挡区内）")

    # ---- 残骸轨迹（同一色条，按各自 MET 着色） + 落点 ----
    for dv in view["debris"]:
        a_s = unwrap_around(dv["az_deg"], center)
        p = np.column_stack([a_s, dv["el_deg"]])
        if p.shape[0] < 2:
            continue
        seg = np.stack([p[:-1], p[1:]], axis=1)
        tt = dv["t"]
        lc2 = LineCollection(seg, cmap=cmap, norm=norm, lw=1.35, alpha=0.92,
                             array=0.5 * (tt[:-1] + tt[1:]), zorder=5)
        ax.add_collection(lc2)
        imp = dv["impact"]
        if imp is not None:
            xi = float(unwrap_around(np.array([imp["az_deg"]]), center)[0])
            ax.scatter([xi], [imp["el_deg"]], marker="X", s=70, color="#111111",
                       edgecolors="white", linewidths=0.6, zorder=11)

    # ---- 残骸落点：分组标签（梯级排布，避免重叠） ----
    group_names = {"core": "芯一级", "fairing": "整流罩两半"}
    groups: dict[str, list] = {}
    for dv in view["debris"]:
        if dv["impact"] is None:
            continue
        key = str(dv["object"]).split("_")[0]
        groups.setdefault(key, []).append(dv)
    present = [k for k in ("core", "fairing") if groups.get(k)]
    present.sort(key=lambda k: -float(groups[k][0]["impact"]["el_deg"]))
    if present:
        pt_per_deg = 5.0 * 72.0 / max(y_hi - y_lo, 1e-6)
        step_deg = 12.5 / pt_per_deg
        top_y = max(float(groups[k][0]["impact"]["el_deg"]) for k in present) + 0.9 * step_deg
        for rank, gkey in enumerate(present):
            items = groups[gkey]
            imp0 = items[0]["impact"]
            x = float(unwrap_around(np.array([imp0["az_deg"]]), center)[0])
            y = float(imp0["el_deg"])
            label_y = top_y - step_deg * rank
            txt = (f"{group_names[gkey]}落点 T+{imp0['t_met_s']:.0f} s\n"
                   f"（{imp0['t_utc'][11:19]} UTC，地平线下不可见）")
            if (x_lo <= x <= x_hi) and (y_lo <= y <= y_hi):
                _annotation(ax, (x, y), txt, color="#111111",
                            offset=(-10, (label_y - y) * pt_per_deg),
                            fontsize=6.2, ha="right")

    # ---- 分离事件点（白圈） ----
    ev_pts = sorted(
        [(float(unwrap_around(np.array([e["az_deg"]]), center)[0]), e) for e in ev_main],
        key=lambda p: p[0])
    for x, ev in ev_pts:
        ax.scatter([x], [ev["el_deg"]], marker="o", s=42, facecolor="white",
                   edgecolor="#333333", linewidths=1.4, zorder=10)
    if ev_pts:
        span_ev_x = ev_pts[-1][0] - ev_pts[0][0]
        span_ev_el = (max(e["el_deg"] for _, e in ev_pts)
                      - min(e["el_deg"] for _, e in ev_pts))
        if span_ev_x < 6.0 and span_ev_el < 4.0:
            mid_x, mid_ev = ev_pts[len(ev_pts) // 2]
            lines = ["级间分离点（白圈）"]
            lines += [f"{e['event']} T+{e['t_met_s']:.1f}s" for _, e in ev_pts]
            ax.annotate("\n".join(lines), xy=(mid_x, mid_ev["el_deg"]),
                        xytext=(0, 54), textcoords="offset points",
                        ha="center", va="bottom", fontsize=6.8, color="#222222",
                        zorder=12,
                        bbox=dict(boxstyle="round,pad=0.25", fc="white",
                                  ec="#dddddd", alpha=0.92),
                        arrowprops=dict(arrowstyle="-", color="#333333", lw=0.6,
                                        shrinkA=0, shrinkB=2))
        else:
            last_x = None
            dy = 22.0
            for x, ev in ev_pts:
                if last_x is not None and (x - last_x) < 8.0:
                    dy += 26.0
                else:
                    dy = 22.0
                last_x = x
                txt = (f"{ev['event']} T+{ev['t_met_s']:.1f}s\n"
                       f"(az {ev['az_deg']:.1f}°, el {ev['el_deg']:.2f}°)")
                side = _auto_side(x, x_lo, x_hi, txt, 6.8)
                dx, ha = (-10, "right") if side == "left" else (10, "left")
                _annotation(ax, (x, ev["el_deg"]), txt, color="#222222",
                            offset=(dx, dy), fontsize=6.8, ha=ha)

    # ---- 首次出现（绿星）/ 最大高度角（橙圈）/ 消失（品红方块） ----
    if view["first"] is not None:
        st = view["first"]
        x = float(unwrap_around(np.array([st["az_deg"]]), center)[0])
        ax.scatter([x], [st["el_deg"]], marker="*", s=260, color="#2ca02c",
                   edgecolors="#14571b", linewidths=0.8, zorder=14)
        txt = (f"首次出现 T+{st['t_met_s']:.1f}s\n"
               f"az {st['az_deg']:.1f}°, el {st['el_deg']:.2f}°")
        _annotation(ax, (x, st["el_deg"]), txt, color="#1a6b1a",
                    offset=(-14, 26), fontsize=7.2, ha="right")
    st = view["max"]
    x = float(unwrap_around(np.array([st["az_deg"]]), center)[0])
    ax.scatter([x], [st["el_deg"]], marker="o", s=150, facecolor="none",
               edgecolor="#ff7f0e", linewidths=2.0, zorder=13)
    _annotation(ax, (x, st["el_deg"]),
                f"最大高度角 {st['el_deg']:.1f}°\nT+{st['t_met_s']:.1f}s",
                color="#b35c00", offset=(12, 18), fontsize=7.2, ha="left")
    if view["last"] is not None:
        st = view["last"]
        x = float(unwrap_around(np.array([st["az_deg"]]), center)[0])
        ax.scatter([x], [st["el_deg"]], marker="s", s=110, facecolor="#d6009c",
                   edgecolors="#7a0058", linewidths=0.8, zorder=13)
        tail = "（数据结束）" if st.get("on_data_end") else ""
        txt = (f"消失{tail} T+{st['t_met_s']:.1f}s\n"
               f"az {st['az_deg']:.1f}°, el {st['el_deg']:.2f}°")
        side = _auto_side(x, x_lo, x_hi, txt, 7.2)
        dx, ha = (-14, "right") if side == "left" else (14, "left")
        _annotation(ax, (x, st["el_deg"]), txt, color="#a00072",
                    offset=(dx, 22), fontsize=7.2, ha=ha)

    # ---- 统计信息框 ----
    if view["first"] is not None:
        last_note = "（数据结束时仍可见）" if view["last"].get("on_data_end") else ""
        stats = (f"首次出现  T+{view['first']['t_met_s']:.2f} s\n"
                 f"最大高度角（可见时段内） {view['max']['el_deg']:.2f}° "
                 f"@ T+{view['max']['t_met_s']:.2f} s\n"
                 f"消失  T+{view['last']['t_met_s']:.2f} s{last_note}\n"
                 f"几何可见时长  {view['visible_duration_s']:.1f} s")
        if view["max"].get("geometric_max_masked"):
            stats += (f"\n（几何最大 {view['max']['geom_max_el_deg']:.1f}° "
                      f"@ T+{view['max']['geom_max_t_met_s']:.0f} s 已被地形遮挡）")
    else:
        stats = "全程几何不可见（地形/地球曲率遮挡）"
    stats += (f"\n观测点太阳高度  {sun['observer_sun_el_t0_deg']:.1f}°（T0，{sun['twilight_state_t0']}）\n"
              f"火箭受照时间占比  {sun['rocket_sunlit_fraction'] * 100:.0f}%\n"
              f"阳光可见时长  {view['sun_visible_duration_s']:.1f} s\n"
              "几何可见 ≠ 肉眼可见（见说明）")
    corner = _empty_corner(az_s, el, x_lo, x_hi, y_lo, y_hi)
    tx, ty, ha_t, va_t = {
        "ul": (0.012, 0.985, "left", "top"),
        "ur": (0.988, 0.985, "right", "top"),
        "ll": (0.012, 0.02, "left", "bottom"),
        "lr": (0.988, 0.02, "right", "bottom"),
    }[corner]
    ax.text(tx, ty, stats, transform=ax.transAxes, va=va_t, ha=ha_t,
            fontsize=7.8, linespacing=1.35, zorder=15,
            bbox=dict(boxstyle="round,pad=0.32", fc="white", ec="#cccccc",
                      alpha=0.92))

    # ---- 坐标轴 ----
    ax.set_xlim(x_lo, x_hi)
    ax.set_ylim(y_lo, y_hi)
    ax.set_xlabel("方位角 (°)　正北 = 0°，顺时针", fontsize=9.5)
    ax.set_ylabel("高度角 (°)", fontsize=9.5)
    step = 10.0 if (x_hi - x_lo) <= 200 else 20.0
    xticks = np.arange(math.ceil(x_lo / step) * step, x_hi + 1e-6, step)
    ax.set_xticks(xticks)
    ax.set_xticklabels([("360" if abs(v - 360.0) < 0.5 else f"{int(round(v % 360.0))}")
                        for v in xticks])
    ax.grid(True, ls=":", lw=0.55, color="#b0b0b0", alpha=0.7)

    # ---- 右侧 MET 色条 ----
    sm = ScalarMappable(norm=norm, cmap=cmap)
    sm.set_array([])
    cb = fig.colorbar(sm, cax=cax)
    cb.set_label("MET 起飞后时间 (s)", fontsize=9)
    cb.ax.tick_params(labelsize=8)

    # ---- 下方面板：高度角/遮挡角 vs 时间 + 太阳高度 + 可见区间 ----
    for k, iv in enumerate(view["intervals"]):
        ax2.axvspan(iv["t_start"], iv["t_end"], color="#2ca02c", alpha=0.13,
                    lw=0, zorder=0,
                    label=("几何可见区间" if k == 0 else None))
    for k, iv in enumerate(view["intervals_sun"]):
        ax2.axvspan(iv["t_start"], iv["t_end"], color="#e6b800", alpha=0.10,
                    lw=0, zorder=0, label=("阳光可见区间" if k == 0 else None))
    ax2.plot(t, el, color="#1f1f1f", lw=1.1, zorder=4, label="火箭高度角")
    ax2.plot(t, block, color="#8B4513", lw=1.1, ls="--", zorder=3,
             label="地形遮挡角（沿轨迹方位）")
    if view["intervals"]:
        mvis = np.zeros_like(t, dtype=bool)
        for iv in view["intervals"]:
            mvis |= (t >= iv["t_start"]) & (t <= iv["t_end"])
        ax2.plot(t[mvis], el[mvis], color="#2ca02c", lw=2.2, zorder=5)
    ax2.plot(t, arr["obs_sun_el"], color="#e6b800", lw=1.6, zorder=4,
             label="观测点太阳高度（几何）")
    ax2.axhline(0.0, color="#888888", lw=0.7, ls=":", zorder=2)
    ax2.axhline(SUN_VISIBLE_OBS_EL_DEG, color="#997700", lw=0.8, ls="--", zorder=2)
    ax2.text(0.995, SUN_VISIBLE_OBS_EL_DEG + 0.8, "民用晨昏界线 -0.83°",
             transform=ax2.get_yaxis_transform(), fontsize=6.4, color="#7a5e00",
             ha="right", va="bottom", zorder=6)
    for ev in ev_main:
        ax2.axvline(ev["t_met_s"], color="#444444", lw=0.8, ls="--", alpha=0.6, zorder=2)
    if view["first"] is not None:
        ax2.axvline(view["first"]["t_met_s"], color="#2ca02c", lw=1.0, alpha=0.8, zorder=3)
        ax2.axvline(view["last"]["t_met_s"], color="#d6009c", lw=1.0, alpha=0.8, zorder=3)
    ax2.set_xlim(0.0, t_end)
    y_lo2 = max(-3.0, float(np.min(el)) - 1.0)
    y_hi2 = min(92.0, max(35.0, float(np.max(el)) + 8.0))
    ax2.set_ylim(y_lo2, y_hi2)
    if not np.all(arr["sunlit"]):
        n_shadow = int(np.sum(~arr["sunlit"]))
        ax2.scatter(t[~arr["sunlit"]],
                    np.full(n_shadow, y_lo2 + 0.03 * (y_hi2 - y_lo2)),
                    marker="s", s=7, color="#555555", zorder=6, label="火箭在地影中")
    ax2.set_xlabel("MET 起飞后时间 (s)", fontsize=9)
    ax2.set_ylabel("高度角 (°)", fontsize=9)
    ax2.set_title("可见性时间轴：绿 = 几何可见区间；金 = 阳光可见区间；"
                  "金色曲线 = 观测点太阳高度；竖线 = 分离事件/首次出现(绿)/消失(品红)",
                  fontsize=8.2, loc="left", pad=4)
    ax2.grid(True, ls=":", lw=0.55, color="#b0b0b0", alpha=0.7)
    ax2.legend(loc="upper left", fontsize=6.8, ncol=2, frameon=True,
               framealpha=0.85, edgecolor="#dddddd")

    # ---- 图例 ----
    handles = [
        Line2D([], [], color="#333333", lw=2.8, label="主弹道（按 MET 着色）"),
        Line2D([], [], color="#8B4513", lw=1.6, label="地形遮挡线（SRTM 1″+曲率）"),
        Patch(facecolor="#8B4513", alpha=0.16, label="遮挡区（地形+曲率）"),
        Line2D([], [], marker="*", ls="none", markerfacecolor="#2ca02c",
               markeredgecolor="#14571b", markersize=13, label="首次出现"),
        Line2D([], [], marker="o", ls="none", markerfacecolor="none",
               markeredgecolor="#ff7f0e", markeredgewidth=1.8, markersize=9,
               label="最大高度角"),
        Line2D([], [], marker="s", ls="none", markerfacecolor="#d6009c",
               markeredgecolor="#7a0058", markersize=8, label="消失"),
        Line2D([], [], marker="o", ls="none", markerfacecolor="white",
               markeredgecolor="#333333", markersize=6, label="级间分离事件"),
        Line2D([], [], color="#777777", lw=1.3, alpha=0.9,
               label="残骸轨迹（芯一级/整流罩两半，同色条）"),
        Line2D([], [], marker="X", ls="none", markerfacecolor="#111111",
               markeredgecolor="white", markersize=8, label="残骸落点（多数在地平线下）"),
    ]
    fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 0.043),
               ncol=5, fontsize=7.6, frameon=False, columnspacing=1.2,
               handletextpad=0.5)
    note = (f"观测点：{dms_str(obs['lat_deg'], obs['lon_deg'])}（高程 {obs['alt_m']:.0f} m，SRTM 1″）。"
            "几何可见 = 火箭高度角 > 地形遮挡角；几何可见 ≠ 肉眼可见："
            f"T0 时观测点太阳高度 {sun['observer_sun_el_t0_deg']:.1f}°（{sun['twilight_state_t0']}），"
            "天空背景仍较亮、斜距数百至上千 km 时箭体张角 <0.1°，肉眼/普通相机难以分辨，需双筒/长焦；"
            f"火箭受照时长 {sun['rocket_sunlit_fraction'] * (t_end - t[0]):.0f} s、阳光可见 "
            f"{view['sun_visible_duration_s']:.0f} s。地影为简化圆柱本影模型（圆锥对照差 <0.02°）。"
            "地形遮挡线为 0.5° 方位 × 90 m 距离采样；平台/台阶来自 DEM 采样量化"
            "（近场单像元或山脊切换），不是弹道跳变。"
            "弹道为 3-DOF 标称反演，非实际发射弹道。")
    fig.text(0.5, 0.006, note, ha="center", fontsize=6.6, color="#666666", wrap=True)

    out_png.parent.mkdir(parents=True, exist_ok=True)
    saved, _, _ = plotstyle.save(fig, out_png, dpi=dpi)
    plt.close(fig)
    w, h = png_size(saved)
    return {"path": _rel(saved), "width_px": w, "height_px": h}


def plot_panorama(view: dict, traj: dict, sun_data: dict, out_png: Path,
                  dpi: int = 160, placeholder: bool = False) -> dict:
    """全景版"方位角–高度角"图：**不裁剪地平线以下部分**（完整弹道 + 残骸下落）。

    主图为了看清关键细节把 y 轴下界裁到 -3°~-12°；全景图把整条弹道与残骸轨迹完整
    画到任意负高度角，并明确标注"地平线以下部分实际被地形/地球曲率遮挡"。
    """
    obs = view["observer"]
    arr = view["arrays"]
    sun = view["sun"]
    t = arr["t"]
    az, el, block = arr["az"], arr["el"], arr["block"]
    temp_tag = ("【临时版：使用夹具参考弹道，仅供流程验证】"
                if placeholder else "")
    center = circular_mean_deg(az)
    az_s = unwrap_around(az, center)
    qs = [az_s]
    els = [np.asarray(el, dtype=float)]
    for dv in view["debris"]:
        qs.append(unwrap_around(dv["az_deg"], center))
        els.append(dv["el_deg"])
    all_az = np.concatenate([q for q in qs if np.size(q)])
    all_el = np.concatenate([e for e in els if np.size(e)])
    q_lo, q_hi = float(np.min(all_az)), float(np.max(all_az))
    margin = max(3.0, 0.04 * (q_hi - q_lo))
    x_lo = math.floor((q_lo - margin) / 5.0) * 5.0
    x_hi = math.ceil((q_hi + margin) / 5.0) * 5.0
    if x_hi - x_lo > 340.0:
        x_lo, x_hi = 0.0, 360.0
    y_lo = float(np.floor((min(float(np.min(all_el)), 0.0) - 3.0) / 5.0) * 5.0)
    y_hi = float(np.ceil((max(float(np.max(all_el)), float(view["max"]["el_deg"]),
                              10.0) + 5.0) / 5.0) * 5.0)
    y_hi = min(max(y_hi, 20.0), 90.0)

    cmap = matplotlib.colormaps["turbo"]
    t_max_all = max(float(t[-1]), 1.0)
    for dv in view["debris"]:
        if dv["t"].size:
            t_max_all = max(t_max_all, float(np.max(dv["t"])))
    for ev in view["events"]:
        t_max_all = max(t_max_all, float(ev["t_met_s"]))
    t_max_all = float(np.ceil(t_max_all / 10.0) * 10.0)
    norm = Normalize(vmin=0.0, vmax=t_max_all)

    fig = plt.figure(figsize=(13.8, 9.2))
    gs = fig.add_gridspec(1, 2, width_ratios=[1.0, 0.020],
                          left=0.068, right=0.945, top=0.895, bottom=0.135,
                          wspace=0.02)
    ax = fig.add_subplot(gs[0, 0])
    cax = fig.add_subplot(gs[0, 1])

    az_plot = np.linspace(x_lo, x_hi, 900)
    hz_plot = horizon_at(view["horizon_deg"], az_plot)
    ax.axhspan(y_lo, 0.0, color="#cccccc", alpha=0.35, lw=0,
               label="地平线以下（几何视线被地球曲率/地形遮挡）")
    ax.fill_between(az_plot, y_lo, hz_plot, color="#8B4513", alpha=0.16, lw=0)
    ax.plot(az_plot, hz_plot, color="#8B4513", lw=1.7, zorder=3,
            label="地形遮挡线（SRTM 1″ DEM + 地球曲率）")
    ax.axhline(0.0, color="#777777", lw=0.9, ls=":", zorder=2)
    ax.text(x_lo + 0.01 * (x_hi - x_lo), 0.8, "地平线 0°", fontsize=7.2,
            color="#555555", va="bottom", zorder=6)

    pts = np.column_stack([az_s, el])
    segs = np.stack([pts[:-1], pts[1:]], axis=1)
    ax.add_collection(LineCollection(segs, cmap=cmap, norm=norm, lw=2.6,
                                     array=0.5 * (t[:-1] + t[1:]), zorder=6))
    for dv in view["debris"]:
        a_s = unwrap_around(dv["az_deg"], center)
        p = np.column_stack([a_s, dv["el_deg"]])
        if p.shape[0] < 2:
            continue
        seg = np.stack([p[:-1], p[1:]], axis=1)
        tt = dv["t"]
        ax.add_collection(LineCollection(seg, cmap=cmap, norm=norm, lw=1.3,
                                         alpha=0.92, array=0.5 * (tt[:-1] + tt[1:]),
                                         zorder=5))
    imp_txt = []
    for dv in view["debris"]:
        imp = dv["impact"]
        if imp is None:
            continue
        xi = float(unwrap_around(np.array([imp["az_deg"]]), center)[0])
        ax.scatter([xi], [imp["el_deg"]], marker="X", s=75, color="#111111",
                   edgecolors="white", linewidths=0.6, zorder=11)
        imp_txt.append(f"{dv['name']} T+{imp['t_met_s']:.0f} s（el {imp['el_deg']:+.1f}°）")
    ev_main = [e for e in (find_event(view["events"], c) for c in _main_events(view["events"]))
               if e is not None]
    ev_pts = sorted([(float(unwrap_around(np.array([e["az_deg"]]), center)[0]), e)
                     for e in ev_main], key=lambda p: p[0])
    for x, ev in ev_pts:
        ax.scatter([x], [ev["el_deg"]], marker="o", s=42, facecolor="white",
                   edgecolor="#333333", linewidths=1.4, zorder=10)
    if ev_pts:
        span_x = ev_pts[-1][0] - ev_pts[0][0]
        span_el = (max(e["el_deg"] for _, e in ev_pts)
                   - min(e["el_deg"] for _, e in ev_pts))
        if span_x < 8.0 and span_el < 4.0:
            mid_x = ev_pts[len(ev_pts) // 2][0]
            mid_el = ev_pts[len(ev_pts) // 2][1]["el_deg"]
            lines = ["级间分离点（白圈）"] + [f"{e['event']} T+{e['t_met_s']:.1f}s"
                                            for _, e in ev_pts]
            ax.annotate("\n".join(lines), xy=(mid_x, mid_el), xytext=(0, 50),
                        textcoords="offset points", ha="center", va="bottom",
                        fontsize=6.8, color="#222222", zorder=12,
                        bbox=dict(boxstyle="round,pad=0.25", fc="white",
                                  ec="#dddddd", alpha=0.92),
                        arrowprops=dict(arrowstyle="-", color="#333333", lw=0.6,
                                        shrinkA=0, shrinkB=2))
        else:
            dy = 22.0
            last_x = None
            for x, ev in ev_pts:
                if last_x is not None and (x - last_x) < 8.0:
                    dy += 26.0
                else:
                    dy = 22.0
                last_x = x
                txt = f"{ev['event']} T+{ev['t_met_s']:.1f}s"
                side = _auto_side(x, x_lo, x_hi, txt, 6.8)
                dx, ha = (-10, "right") if side == "left" else (10, "left")
                _annotation(ax, (x, ev["el_deg"]), txt, color="#222222",
                            offset=(dx, dy), fontsize=6.8, ha=ha)
    if view["first"] is not None:
        st = view["first"]
        x = float(unwrap_around(np.array([st["az_deg"]]), center)[0])
        ax.scatter([x], [st["el_deg"]], marker="*", s=240, color="#2ca02c",
                   edgecolors="#14571b", linewidths=0.8, zorder=14)
        st = view["max"]
        x = float(unwrap_around(np.array([st["az_deg"]]), center)[0])
        ax.scatter([x], [st["el_deg"]], marker="o", s=150, facecolor="none",
                   edgecolor="#ff7f0e", linewidths=2.0, zorder=13)
        st = view["last"]
        x = float(unwrap_around(np.array([st["az_deg"]]), center)[0])
        ax.scatter([x], [st["el_deg"]], marker="s", s=110, facecolor="#d6009c",
                   edgecolors="#7a0058", linewidths=0.8, zorder=13)

    ax.set_xlim(x_lo, x_hi)
    ax.set_ylim(y_lo, y_hi)
    ax.set_xlabel("方位角 (°)　正北 = 0°，顺时针", fontsize=9.5)
    ax.set_ylabel("高度角 (°)", fontsize=9.5)
    step = 10.0 if (x_hi - x_lo) <= 200 else 20.0
    xticks = np.arange(math.ceil(x_lo / step) * step, x_hi + 1e-6, step)
    ax.set_xticks(xticks)
    ax.set_xticklabels([("360" if abs(v - 360.0) < 0.5 else f"{int(round(v % 360.0))}")
                        for v in xticks])
    ax.grid(True, ls=":", lw=0.55, color="#b0b0b0", alpha=0.7)
    handles_pan = [
        Line2D([], [], color="#333333", lw=2.6, label="主弹道（按 MET 着色）"),
        Line2D([], [], color="#8B4513", lw=1.6, label="地形遮挡线（SRTM 1″+曲率）"),
        Patch(facecolor="#cccccc", alpha=0.35, label="地平线以下（实际被遮挡）"),
        Line2D([], [], marker="*", ls="none", markerfacecolor="#2ca02c",
               markeredgecolor="#14571b", markersize=13, label="首次出现"),
        Line2D([], [], marker="o", ls="none", markerfacecolor="none",
               markeredgecolor="#ff7f0e", markeredgewidth=1.8, markersize=9,
               label="最大高度角"),
        Line2D([], [], marker="s", ls="none", markerfacecolor="#d6009c",
               markeredgecolor="#7a0058", markersize=8, label="消失"),
        Line2D([], [], marker="X", ls="none", markerfacecolor="#111111",
               markeredgecolor="white", markersize=8, label="残骸落点"),
    ]
    ax.legend(handles=handles_pan, loc="lower left", fontsize=7.0, ncol=2,
              frameon=True, framealpha=0.88, edgecolor="#dddddd")

    sm = ScalarMappable(norm=norm, cmap=cmap)
    sm.set_array([])
    cb = fig.colorbar(sm, cax=cax)
    cb.set_label("MET 起飞后时间 (s)", fontsize=9)
    cb.ax.tick_params(labelsize=8)

    fig.suptitle(f"{MISSION_TITLE} · 指定观测点天空轨迹全景（不裁剪地平线以下部分）{temp_tag}",
                 fontsize=14.0, y=0.975)
    fig.text(0.5, 0.925,
             f"观测点：{dms_str(obs['lat_deg'], obs['lon_deg'])}，高程 {obs['alt_m']:.1f} m  |  "
             f"T0 = {sun_data['t0_str']}（北京时 {sun_data['t0_bj']}）  |  "
             f"数据：{traj['source']}",
             ha="center", fontsize=8.4, color="#444444")
    box = (f"全景：完整弹道（MET {t[0]:.0f}–{t[-1]:.0f} s）与残骸下落轨迹\n"
           f"几何可见时长 {view['visible_duration_s']:.1f} s；"
           f"最大高度角（可见时段内） {view['max']['el_deg']:.2f}°\n")
    if view["max"].get("geometric_max_masked"):
        box += (f"（几何最大 {view['max']['geom_max_el_deg']:.2f}° 在可见区间外，"
                f"该时刻已被遮挡）\n")
    box += "**地平线以下部分为几何视位置，实际被地形/地球曲率遮挡**"
    if imp_txt:
        box += "\n落点：" + "；".join(imp_txt)
    ax.text(0.012, 0.985, box, transform=ax.transAxes, va="top", ha="left",
            fontsize=7.6, linespacing=1.4, zorder=15,
            bbox=dict(boxstyle="round,pad=0.32", fc="white", ec="#cccccc", alpha=0.92))
    note = ("全景图不裁剪负高度角（主图裁到约 -3°~-12°）。"
            "地平线以下轨迹只表示几何视位置，因地球曲率与地形遮挡，实际不可见；"
            "残骸落地时刻取自 sim/落点与射程.csv。弹道为 3-DOF 标称反演，非实际发射弹道。")
    fig.text(0.5, 0.008, note, ha="center", fontsize=6.8, color="#666666", wrap=True)

    out_png.parent.mkdir(parents=True, exist_ok=True)
    saved, _, _ = plotstyle.save(fig, out_png, dpi=dpi)
    plt.close(fig)
    w, h = png_size(saved)
    return {"path": _rel(saved), "width_px": w, "height_px": h}


def plot_sun_shadow(view: dict, traj: dict, sun_data: dict, out_png: Path,
                    dpi: int = 160, placeholder: bool = False) -> dict:
    """太阳高度/地影/可见性时间轴示意（3 面板）：解释"几何可见 ≠ 肉眼可见"。"""
    obs = view["observer"]
    arr = view["arrays"]
    sun = view["sun"]
    t = arr["t"]
    obs_sun = arr["obs_sun_el"]
    alt_km = traj["alt"] / 1000.0
    h_crit = np.asarray(sun_data["sun_alt_km"], dtype=float)
    umbra = arr["umbra"]

    fig, axes = plt.subplots(3, 1, figsize=(13.8, 10.2), sharex=True,
                             gridspec_kw={"height_ratios": [1.05, 1.05, 0.72],
                                          "hspace": 0.22})
    fig.subplots_adjust(left=0.075, right=0.98, top=0.90, bottom=0.10)
    ax1, ax2, ax3 = axes

    # ---- (1) 观测点太阳高度 + 晨昏分级 ----
    ax1.axhspan(-6.0, 0.0, color="#2b2b6b", alpha=0.12, lw=0,
                label="民用晨昏（0°~-6°，天空仍较亮）")
    ax1.axhspan(-12.0, -6.0, color="#1a1a40", alpha=0.12, lw=0,
                label="航海晨昏（-6°~-12°）")
    ax1.axhline(0.0, color="#777777", lw=0.8, ls=":", zorder=2)
    ax1.axhline(SUN_VISIBLE_OBS_EL_DEG, color="#997700", lw=0.9, ls="--", zorder=2)
    ax1.plot(t, obs_sun, color="#e6b800", lw=2.2, zorder=5,
             label="观测点太阳几何高度角")
    if view["first"] is not None:
        ax1.axvline(view["first"]["t_met_s"], color="#2ca02c", lw=1.0, alpha=0.8)
        ax1.axvline(view["last"]["t_met_s"], color="#d6009c", lw=1.0, alpha=0.8)
    ax1.set_ylabel("观测点太阳\n高度角 (°)", fontsize=9)
    ax1.set_ylim(min(-7.0, float(np.min(obs_sun)) - 1.0),
                 max(3.0, float(np.max(obs_sun)) + 1.0))
    ax1.grid(True, ls=":", lw=0.55, color="#b0b0b0", alpha=0.7)
    ax1.legend(loc="lower right", fontsize=7.0, ncol=2, frameon=True,
               framealpha=0.88, edgecolor="#dddddd")
    if sun["observer_sun_el_t0_deg"] < SUN_VISIBLE_OBS_EL_DEG:
        dawn_note = ("T0 处于民用晨昏（曙光）：太阳在地平线下但天空已发亮；"
                     f"飞行时段由 {sun['observer_sun_el_min_deg']:.2f}° 升至 "
                     f"{sun['observer_sun_el_max_deg']:.2f}°")
    else:
        dawn_note = (f"T0 已为白天（太阳高度 {sun['observer_sun_el_t0_deg']:.1f}°）；"
                     f"飞行时段 {sun['observer_sun_el_min_deg']:.2f}° … "
                     f"{sun['observer_sun_el_max_deg']:.2f}°")
    ax1.set_title(f"(1) 观测点太阳高度：{dawn_note}", fontsize=9.0, loc="left", pad=4)

    # ---- (2) 火箭高度 vs 地影边界高度 ----
    ax2.plot(t, alt_km, color="#1f4e79", lw=1.9, zorder=5, label="火箭高度")
    ax2.plot(t, h_crit, color="#b35c00", lw=1.4, ls="--", zorder=4,
             label="地影边界高度 R(1/cos|e⊙|-1)（解析）")
    ax2.fill_between(t, 0.0, alt_km, where=umbra, color="#333333", alpha=0.35,
                     lw=0, zorder=3, label="火箭在地影中（未受阳光照射）")
    if sun["first_sunlit"] is not None:
        fs = sun["first_sunlit"]
        ax2.axvline(fs["t_met_s"], color="#e6b800", lw=1.2, zorder=6)
        ax2.annotate(f"火箭进入阳光\nT+{fs['t_met_s']:.1f} s（高度 {fs['alt_km']:.1f} km）",
                     xy=(fs["t_met_s"], max(fs["alt_km"], 1.0)),
                     xytext=(18, -6), textcoords="offset points",
                     fontsize=7.0, color="#8a6d00",
                     bbox=dict(boxstyle="round,pad=0.22", fc="white", ec="#dddddd", alpha=0.9),
                     arrowprops=dict(arrowstyle="-", color="#8a6d00", lw=0.6))
    ax2.set_ylabel("高度 (km)", fontsize=9)
    ax2.set_ylim(0.0, max(float(np.max(alt_km)), 10.0) * 1.06)
    ax2.grid(True, ls=":", lw=0.55, color="#b0b0b0", alpha=0.7)
    ax2.legend(loc="upper left", fontsize=7.0, ncol=2, frameon=True,
               framealpha=0.88, edgecolor="#dddddd")
    if sun["rocket_umbra_fraction"] > 0.0:
        ax2_title = ("(2) 火箭高度与地影边界：火箭爬升穿过晨昏线后进入阳光"
                     "（地影边界高度由该点星下点太阳高度解析给出）")
    else:
        ax2_title = ("(2) 火箭高度与地影边界：本任务火箭全程受阳光照射（未进入地影），"
                     "地面/低空处太阳仍在地平线下")
    ax2.set_title(ax2_title, fontsize=9.0, loc="left", pad=4)

    # ---- (3) 可见性类别时间轴 ----
    rows = [
        ("几何可见（地形判据）", view["intervals"], "#2ca02c"),
        ("阳光可见（几何 ∧ 火箭受照）", view["intervals_sun"], "#e6b800"),
        ("天空暗于 -0.83°", sun["dusk_condition"]["intervals"], "#4d4d4d"),
        ("天空暗于 -6°（航海晨昏）",
         sun["twilight_visible"]["nautical"]["intervals"], "#111111"),
    ]
    for i, (label, ivs, color) in enumerate(rows):
        if not ivs:
            ax3.text(0.5, i, "本任务不满足该条件", transform=ax3.get_yaxis_transform(),
                     va="center", ha="left", fontsize=7.0, color="#888888")
        for iv in ivs:
            ax3.barh(i, iv["t_end"] - iv["t_start"], left=iv["t_start"],
                     height=0.55, color=color, alpha=0.85, edgecolor="none")
    ax3.set_yticks(range(len(rows)))
    ax3.set_yticklabels([r[0] for r in rows], fontsize=8.2)
    ax3.set_ylim(-0.7, len(rows) - 0.3)
    ax3.set_xlim(0.0, float(t[-1]))
    ax3.set_xlabel("MET 起飞后时间 (s)", fontsize=9)
    ax3.grid(True, axis="x", ls=":", lw=0.55, color="#b0b0b0", alpha=0.7)
    ax3.set_title("(3) 可见性类别时间轴：几何可见 ≥ 阳光可见 ≥ 天空足够暗（肉眼可见更严格）",
                  fontsize=9.0, loc="left", pad=4)

    fig.suptitle(f"{MISSION_TITLE} · 太阳高度与地影示意（观测点 "
                 f"{dms_str(obs['lat_deg'], obs['lon_deg'])}）",
                 fontsize=13.6, y=0.972)
    fig.text(0.5, 0.928,
             f"T0 = {sun_data['t0_str']}（北京时 {sun_data['t0_bj']}）  |  "
             f"观测点太阳高度 T0 = {sun['observer_sun_el_t0_deg']:.2f}°（{sun['twilight_state_t0']}），"
             f"飞行末段 {sun['observer_sun_el_end_deg']:.2f}°  |  "
             f"火箭受照占比 {sun['rocket_sunlit_fraction'] * 100:.0f}%，"
             f"在地影中 {sun['rocket_umbra_fraction'] * 100:.0f}%",
             ha="center", fontsize=8.4, color="#444444")
    fig.text(0.5, 0.030,
             "说明：太阳高度为几何值（未计大气折射，地平附近约 +0.5°）；地影主判据为圆柱本影"
             "（R=6371 km、平行光），圆锥本影/半影（太阳视半径 0.267°）对照差 <0.02°。"
             "「几何可见」只表示视线未被地形/曲率遮挡；「阳光可见」要求火箭受阳光照射；"
             "肉眼能否分辨还取决于天空背景亮度（曙光）、目标张角与大气透明度。"
             "弹道为 3-DOF 标称反演，非实际发射弹道。",
             ha="center", fontsize=6.8, color="#666666", wrap=True)

    out_png.parent.mkdir(parents=True, exist_ok=True)
    saved, _, _ = plotstyle.save(fig, out_png, dpi=dpi)
    plt.close(fig)
    w, h = png_size(saved)
    return {"path": _rel(saved), "width_px": w, "height_px": h}


# --------------------------------------------------------------------------- #
# 7. 输出：CSV / MD / summary
# --------------------------------------------------------------------------- #
CSV_COLUMNS = ["t_met_s", "az_deg", "el_deg", "slant_range_km", "ground_range_km",
               "terrain_block_deg", "geometric_visible", "sun_flag",
               "t_utc", "alt_km", "lat_deg", "lon_deg",
               "observer_sun_el_deg", "sun_visible", "geometric_visible_no_terrain",
               "sky_dark_civil"]


def write_csv(view: dict, traj: dict, out_csv: Path) -> dict:
    """每观测点 0.5 s CSV：前 8 列为任务契约要求的列，其后为辅助列。

    * ``geometric_visible``：el_deg > terrain_block_deg（地形+曲率可见）
    * ``sun_flag``：火箭受阳光照射（未进入地球本影）为 1
    * ``sun_visible``：geometric_visible ∧ sun_flag
    * ``geometric_visible_no_terrain``：只用地平线判据 el_deg > 0
    * ``sky_dark_civil``：观测点太阳几何高度 < -0.83°（天空较暗/民用晨昏以下）
    """
    import pandas as pd

    arr = view["arrays"]
    n = arr["t"].size
    t_utc = traj["t_utc"] or _dt_list_from(parse_utc(DEFAULT_T0), arr["t"])
    df = pd.DataFrame({
        "t_met_s": np.round(arr["t"], 3),
        "az_deg": np.round(arr["az"], 4),
        "el_deg": np.round(arr["el"], 4),
        "slant_range_km": np.round(arr["slant"], 4),
        "ground_range_km": np.round(arr["ground"], 4),
        "terrain_block_deg": np.round(arr["block"], 4),
        "geometric_visible": arr["visible"].astype(int),
        "sun_flag": arr["sunlit"].astype(int),
        "t_utc": [iso_utc(d) for d in t_utc],
        "alt_km": np.round(traj["alt"] / 1000.0, 4),
        "lat_deg": np.round(traj["lat"], 6),
        "lon_deg": np.round(traj["lon"], 6),
        "observer_sun_el_deg": np.round(arr["obs_sun_el"], 3),
        "sun_visible": arr["sun_visible"].astype(int),
        "geometric_visible_no_terrain": arr["above"].astype(int),
        "sky_dark_civil": (arr["obs_sun_el"] < SUN_VISIBLE_OBS_EL_DEG).astype(int),
    })
    assert len(df) == n
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_csv, index=False, encoding="utf-8-sig", float_format="%.4f")
    return {"path": _rel(out_csv), "rows": int(len(df)),
            "bytes": int(out_csv.stat().st_size), "columns": list(df.columns)}


def _bj_of(t0, t_met_s: float) -> str:
    """MET → 北京时字符串。"""
    return bj_str(t0 + timedelta(seconds=float(t_met_s)))


def write_md(view: dict, traj: dict, t0: datetime, sun_data: dict,
             inputs: dict, out_png: dict, out_csv: dict,
             panorama_info: dict | None, sunpng_info: dict | None,
             out_md: Path, placeholder: bool) -> None:
    """天空轨迹说明 md：数据来源 / DEM / 方法 / 可见性统计 / 不确定度 / 局限。"""
    obs = view["observer"]
    arr = view["arrays"]
    sun = view["sun"]
    first, last, mx = view["first"], view["last"], view["max"]
    meta = sun_data["meta"]
    site = meta["site"]
    t0_bj = bj_str(t0)
    L: list[str] = []
    ap = L.append

    ap(f"# 天空轨迹图说明 — 指定观测点（`{obs['id']}`）\n")
    if placeholder:
        ap("> ⚠️ **临时版：使用夹具参考弹道（非本任务弹道）**"
           "：本文件所有数值仅用于流程验证，不能作为任务结论。\n")
    _site_name = str((site or {}).get("name") or "发射点")
    ap(f"> **任务**：{MISSION_TITLE}；T0 = {sun_data['t0_str']}（北京时 {sun_data['t0_bj']}），"
       f"发射点：{_site_name}。\n")
    ap(f"> **观测点**：{dms_str(obs['lat_deg'], obs['lon_deg'])}"
       f"（{obs['lat_deg']:.6f}°N, {obs['lon_deg']:.6f}°E）。"
       "全部成果仅供公开信息分析与教学使用，**非实际发射弹道**、非官方信息。\n")

    # ---- 0. 结论摘要 ----
    ap("## 0. 结论摘要\n")
    if first is not None:
        rows = [
            ["首次出现", f"T+{first['t_met_s']:.2f} s", first["t_utc"],
             first.get("t_bj", "-"), f"{first['az_deg']:.2f}°", f"{first['el_deg']:.2f}°"],
            ["最大高度角", f"T+{mx['t_met_s']:.2f} s", mx["t_utc"],
             mx.get("t_bj", "-"), f"{mx['az_deg']:.2f}°", f"**{mx['el_deg']:.2f}°**"],
            ["消失/可见终止", f"T+{last['t_met_s']:.2f} s", last["t_utc"],
             last.get("t_bj", "-"), f"{last['az_deg']:.2f}°", f"{last['el_deg']:.2f}°"],
        ]
        ap(md_table(["事件", "MET", "UTC", "北京时", "方位角", "高度角"], rows) + "\n")
        if mx.get("geometric_max_masked"):
            ap(f"- 注：几何最大高度角 **{mx['geom_max_el_deg']:.2f}°** 出现在 "
               f"T+{mx['geom_max_t_met_s']:.2f} s，该时刻火箭已被地形/地球曲率遮挡；"
               f"上表「最大高度角」取**可见时段内**的最大值。\n")
        end_note = ("（**数据结束、仍在地平线上方**，非真正落到地平线以下）"
                    if last.get("on_data_end") else "")
        ap(f"- 几何可见时长：**{view['visible_duration_s']:.1f} s**"
           f"（只用地平线 el>0 判据为 {view['visible_duration_no_terrain_s']:.1f} s）；"
           f"首次出现不确定度 **±{view['appear_uncertainty']['reported_s']:.1f} s**。{end_note}\n")
    else:
        ap("- 全程几何不可见（地形/地球曲率遮挡）：无首次出现/消失时刻。\n")
    ap(f"- 观测点太阳高度：T0 = **{sun['observer_sun_el_t0_deg']:.2f}°**"
       f"（{sun['twilight_state_t0']}），飞行时段 {sun['observer_sun_el_min_deg']:.2f}° … "
       f"{sun['observer_sun_el_max_deg']:.2f}°。\n")
    ap(f"- 火箭受照时间占比 {sun['rocket_sunlit_fraction'] * 100:.1f}%；"
       f"**阳光可见时长 {view['sun_visible_duration_s']:.1f} s**；"
       f"在地影中 {sun['rocket_umbra_fraction'] * 100:.1f}%。\n")
    ap(f"- 地形遮挡角（720 方位，SRTM 1″，90 m 步长，200 km）："
       f"{view['horizon_stats']['min']:+.2f}° … {view['horizon_stats']['max']:+.2f}°"
       f"（均值 {view['horizon_stats']['mean']:+.2f}°）。\n")

    # ---- 1. 产物 ----
    ap("## 1. 产物\n")
    prod = [
        [f"`{out_png['path']}`", "主图：方位角–高度角天空轨迹 + 可见性时间轴",
         f"{out_png['width_px']}×{out_png['height_px']} px"],
        [f"`{out_csv['path']}`", "0.5 s 方位/高度/斜距/地面距离/遮挡角/可见性/阳光标志",
         f"{out_csv['rows']} 行"],
        [f"`{out_md.name}`", "本说明", "-"],
    ]
    if panorama_info:
        prod.append([f"`{panorama_info['path']}`", "全景图（不裁剪地平线以下部分）",
                     f"{panorama_info['width_px']}×{panorama_info['height_px']} px"])
    if sunpng_info:
        prod.append([f"`{sunpng_info['path']}`", "太阳高度/地影示意（3 面板）",
                     f"{sunpng_info['width_px']}×{sunpng_info['height_px']} px"])
    ap(md_table(["文件", "说明", "尺寸/行数"], prod) + "\n")

    # ---- 2. 观测点与 DEM ----
    ap("## 2. 观测点与地形数据\n")
    ap(md_table(["项目", "值"], [
        ["名称", obs["name"]],
        ["经纬度（十进制 WGS-84）", f"{obs['lat_deg']:.6f}°N, {obs['lon_deg']:.6f}°E"],
        ["经纬度（度分秒）", dms_str(obs["lat_deg"], obs["lon_deg"])],
        ["观测点高程（椭球高，取 DEM）", f"{obs['alt_m']:.1f} m（{obs['alt_source']}）"],
        ["config.json 占位值",
         ("-" if obs.get("config_alt_m") is None else f"{obs['config_alt_m']} m")],
        ["DEM 产品", "SRTMGL1 v003（NASA/USGS，AWS Open Data `elevation-tiles-prod/skadi`）"],
        ["DEM 瓦片（1°×1°）", (", ".join(obs.get("tiles_used") or []) or "（未记录）")],
        ["观测点 3″ 局部缓存", obs.get("cache_file") or "-"],
        ["遮挡搜索", f"{HORIZON_STEP_M:.0f} m 步长、{HORIZON_RADIUS_KM:.0f} km 半径、"
                    f"720 方位（0.5° 间隔）、海面按 0 m 裁剪（sea_level_clip=True）"],
        ["遮挡角统计", f"{view['horizon_stats']['min']:+.2f}° … "
                    f"{view['horizon_stats']['max']:+.2f}°（均值 "
                    f"{view['horizon_stats']['mean']:+.2f}°，中位 "
                    f"{view['horizon_stats']['median']:+.2f}°），最大方位 "
                    f"{view['horizon_stats']['max_az_deg']:.1f}°"],
    ]) + "\n")
    ap(f"> DEM 数据说明：{view['horizon_note']}。\n>\n"
       "> * **高程基准**：SRTM 为 **EGM96 正高**，本任务按 WGS-84 椭球高使用；"
       "湖北地区两者差值约 -10~-30 m。地形遮挡角只依赖相对高度，远场影响可忽略；"
       "近场（<2 km）可能引入 0.1° 量级误差。\n"
       "> * **海上瓦片**是海底地形（负高程），必须 `sea_level_clip=True` 按海平面裁剪，否则"
       "会“看穿海面”产生虚假负仰角。本观测点 200 km 内全部为陆地，该处理不改变结果，"
       "但代码已强制启用。\n"
       "> * **void/城市**：SRTM 为 2000 年单次 C 波段雷达测量，void 已用 8 邻域迭代填补；"
       "城市建筑、植被、堤岸等人造物未建模，是首次出现时刻的主要系统误差之一。\n"
       "> * **遮挡线台阶**：遮挡线按 0.5° 方位 × 90 m 距离采样；观测点近场单个 DEM 像元"
       "（约 30–90 m）可占据数度方位角，平台/台阶是 DEM 采样量化与山脊切换造成的，"
       "不是弹道或数据异常；判断可见性看「火箭仰角是否高于该方位遮挡线」，不要逐格比较。\n")
    nf = view.get("horizon_nearfield")
    if nf:
        ap(f"> * **近场主导**：各方位遮挡角最大值出现在 <1 km 范围内的比例 "
           f"{nf['fraction_within_1km'] * 100:.0f}%，<10 km 比例 "
           f"{nf['fraction_within_10km'] * 100:.0f}%（距离中位数 {nf['median_range_km']:.1f} km）。"
           "近场 DEM 单元（90 m 步长）对人造物/植被不敏感，故首次出现时刻的标称 ±1–3 s "
           "应视为该量级的保守值。\n")

    # ---- 3. 数据来源 ----
    ap("## 3. 数据来源与版本（可追溯）\n")
    inp_rows = []
    for k, v in (inputs.get("files") or {}).items():
        if not isinstance(v, dict):
            inp_rows.append([k, str(v), "-", "-", ""])
            continue
        inp_rows.append([k, f"`{v.get('path', '-')}`", v.get("mtime_utc", "-"),
                         v.get("sha256_16", "-"),
                         ("" if v.get("exists", True) else "**缺失**")])
    ap(md_table(["输入", "路径", "mtime (UTC)", "sha256 前16位", "备注"], inp_rows) + "\n")
    ap(f"- 输入目录：`{inputs.get('sim_dir', '-')}`"
       + ("（**临时/夹具目录**）" if placeholder else "") + "。\n")
    ap(f"- T0（起飞时刻）：**{iso_utc(t0)}**（北京时 {t0_bj}）；"
       "标称假设值（NASA Spaceflight 2026-11-15 发射预览 + 中文维基发射列表，非官方实测）。"
       "T0 整体平移会平移全部 MET 时刻，但不改变相对几何。\n")
    ap(f"- 弹道采样：{traj['rows']} 行，t = {traj['t'][0]:.1f}–{traj['t'][-1]:.1f} s，"
       f"0.5 s 间隔；`{traj['source']}`。\n")
    ap(f"- 发射场：{site.get('name', '-')}，{site.get('lat_deg', 0):.5f}°N, "
       f"{site.get('lon_deg', 0):.5f}°E，高程 {site.get('alt_m', 0):.1f} m。\n")
    ap("- 事件表：`sim/弹道事件表.csv`；残骸轨迹：`sim/debris_trajectories.csv`（1 s）；"
       "落点：`sim/落点与射程.csv`；对象表：`sim/objects.json`。\n")
    ap("- 太阳位置：`common.geo.solar_elevation_deg` / `sun_ecef_unit`（Meeus 低精度太阳坐标，"
       "误差 ~0.01°，**几何高度角，未计大气折射**）；地影：圆柱本影 + 圆锥本影/半影对照。\n")

    # ---- 4. 方法 ----
    ap("## 4. 方法\n")
    ap("1. **视位置（含地球曲率）**：把弹道各点经纬高转 ECEF，与观测点 ECEF 相减后转到观测点"
       "局部 ENU 系：`az = atan2(E, N)`、`el = atan2(U, √(E²+N²))`"
       "（`common.geo.az_el_from_observer`）。三维严格几何，地球曲率与目标高度自动包含。\n")
    ap("2. **距离**：斜距 = |r_tgt − r_obs|（ECEF）；地面距离 = WGS-84 椭球面大圆距离"
       "（R=6371.0088 km 球面近似，与测地线差 <0.4%）。\n")
    ap(f"3. **地形遮挡**：沿 720 个方位（0.5° 间隔）在 {HORIZON_RADIUS_KM:.0f} km 内以 "
       f"{HORIZON_STEP_M:.0f} m 步长采样 SRTM 1″ DEM，每点算相对观测点的仰角（ECEF 精确，"
       "含地球曲率下沉），取最大值作为该方位**地形遮挡角**；近场 <10 km 用 1″ 原始瓦片精采样。"
       "**实际可见判据：el > terrain_block_deg**。\n")
    ap(f"4. **首次出现/消失**：在 0.5 s 采样可见掩码的边界区间内，对 f(t)=el(t)−遮挡角(az(t)) "
       f"做二分细化，容差 {BISECT_TOL_S:.2f} s；最大高度角用黄金分割在采样极值附近细化到 0.001 s。\n")
    ap("5. **太阳/地影**：火箭受照判据 = 未被地球本影遮挡："
       "`proj=r·s<0` 且到地影轴距离 `perp < R − d·tanα`（α=0.267° 太阳视半径，"
       "d 为入影深度）；同时给出圆柱本影（平行光，`perp<R`）对照。"
       "观测点太阳高度用 `geo.solar_elevation_deg`（几何值）。\n")
    ap("6. **两套可见性定义**：\n"
       "   * **几何可见** = 火箭高度角 > 地形遮挡角（只看视线是否被遮挡）；\n"
       "   * **阳光可见** = 几何可见 ∧ 火箭被阳光照射（地影外）；\n"
       "   * **肉眼可见（更严格）**还需天空背景足够暗（太阳高度低于 -0.83°/-6°）且目标张角/亮度足够。\n")

    # ---- 5. 可见性统计 ----
    ap("## 5. 可见性统计（核心结果）\n")
    if first is not None:
        unc = view["appear_uncertainty"]
        rows = [
            ["首次出现", f"T+{first['t_met_s']:.3f} s", first["t_utc"],
             first.get("t_bj", "-"), f"{first['az_deg']:.2f}°", f"{first['el_deg']:.2f}°",
             f"{first['slant_range_km']:.1f}", f"{first['ground_range_km']:.1f}",
             f"{first['terrain_block_deg']:+.2f}°"],
            ["最大高度角", f"T+{mx['t_met_s']:.3f} s", mx["t_utc"], mx.get("t_bj", "-"),
             f"{mx['az_deg']:.2f}°", f"**{mx['el_deg']:.2f}°**",
             f"{mx['slant_range_km']:.1f}", f"{mx['ground_range_km']:.1f}",
             f"{mx['terrain_block_deg']:+.2f}°"],
            ["消失/可见终止", f"T+{last['t_met_s']:.3f} s", last["t_utc"],
             last.get("t_bj", "-"), f"{last['az_deg']:.2f}°", f"{last['el_deg']:.2f}°",
             f"{last['slant_range_km']:.1f}", f"{last['ground_range_km']:.1f}",
             f"{last['terrain_block_deg']:+.2f}°"],
        ]
        ap(md_table(["事件", "MET", "UTC", "北京时", "方位角", "高度角", "斜距 (km)",
                     "地面距离 (km)", "遮挡角"], rows) + "\n")
        if mx.get("geometric_max_masked"):
            ap(f"- 注：几何最大高度角 {mx['geom_max_el_deg']:.2f}° 出现在 "
               f"T+{mx['geom_max_t_met_s']:.2f} s，该时刻已被地形遮挡；"
               f"本表「最大高度角」为**可见时段内**最大值。\n")
        ap(f"- 几何可见时长（地形判据）：**{view['visible_duration_s']:.2f} s**；"
           f"只用地平线（el>0）判据为 {view['visible_duration_no_terrain_s']:.2f} s；"
           f"阳光可见时长 **{view['sun_visible_duration_s']:.2f} s**。\n")
        if view["intervals"]:
            ap("- 几何可见区间：\n")
            for i, iv in enumerate(view["intervals"], 1):
                ap(f"  {i}. T+{iv['t_start']:.2f} – T+{iv['t_end']:.2f} s"
                   f"（{iv['t_end'] - iv['t_start']:.1f} s）\n")
        if view["intervals_sun"]:
            ap("- 阳光可见区间：\n")
            for i, iv in enumerate(view["intervals_sun"], 1):
                ap(f"  {i}. T+{iv['t_start']:.2f} – T+{iv['t_end']:.2f} s"
                   f"（{iv['t_end'] - iv['t_start']:.1f} s）\n")
        if first.get("on_data_start"):
            ap("- 注：首次出现落在数据起点（T0），说明弹道开始时火箭已在地平线以上"
               "（对地面发射不可能，通常是数据截断）。\n")
        if last.get("on_data_end"):
            ap("- 注：**「消失」时刻等于弹道数据末点（入轨 + 60 s 截断），不是真正的几何落下**；"
               "该时刻火箭高度角仍为 "
               f"{last['el_deg']:.2f}°（远高于遮挡角 {last['terrain_block_deg']:+.2f}°），"
               "入轨段仍在地平线上方。本报告另用 `sim/orbit_track.csv`（入轨后 1.5 圈）"
               "**补充计算**真正的几何落下时刻（见下条；该数据仅用于补充，不混入 0.5 s 弹道 CSV）。\n")
        oe = view.get("orbit_setting")
        if oe and oe.get("set"):
            ap(f"- **补充：入轨后轨道段（`sim/orbit_track.csv`，{oe['n_points']} 点，"
               f"t {oe['t_span_s'][0]:.0f}–{oe['t_span_s'][1]:.0f} s）**：火箭在 "
               f"**T+{oe['t_met_s']:.2f} s**（{oe['t_utc']}，北京时 {oe['t_bj']}）真正落到"
               f"地形遮挡角以下（az {oe['az_deg']:.2f}°，el {oe['el_deg']:.2f}°）；"
               f"即上升段数据结束之后仍在地平线上方约 "
               f"{oe['t_met_s'] - (last['t_met_s'] if last else 0.0):.0f} s"
               "（受目标亮度、天空亮度与观测条件限制，**肉眼实际可见时间通常更短**）。\n")
        elif oe and oe.get("set") is False:
            ap("- **补充：入轨后轨道段（`sim/orbit_track.csv`）**：覆盖时段内火箭一直在"
               "地形遮挡线以上，未出现几何落下。\n")
        elif view.get("orbit_available"):
            ap("- **补充：入轨后轨道段**：上升段数据内已发生几何落下（见上表），"
               "无需轨道段延伸（`sim/orbit_track.csv` 已读取，未使用）。\n")
        else:
            ap("- **补充：入轨后轨道段**：未找到 `sim/orbit_track.csv`（或格式不符），"
               "无法给出上升段数据之后的真正落下时刻。\n")
        ap(f"- **首次出现时刻不确定度：±{unc['reported_s']:.1f} s**（任务书口径 ±1–3 s）。"
           "误差预算：\n")
        p = unc.get("parts") or {}
        if p:
            ap(f"  * SRTM 相对高程 ±{p.get('dem_at_ridge_m', 10):.0f} m，在 "
               f"{p.get('ridge_range_km', 0):.2f} km 遮挡脊线上折算 "
               f"{p.get('dem_angle_unc_deg', 0):.2f}°（近场按 ≤1.0° 封顶）；\n"
               f"  * 观测点高程 ±5 m → {p.get('obs_height_angle_unc_deg', 0):.2f}°（≤0.5° 封顶）；\n"
               "  * 大气折射（未建模）0.20°；城市建筑/植被/堤岸 0.30°；\n"
               f"  * 取 RSS ≈ **{p.get('angle_unc_deg', 0):.2f}°**；首次出现处判据变化率仅 "
               f"{p.get('angular_rate_deg_s', 0):.3f} °/s（低仰角、角速度小），"
               f"严格传播 ≈ {fmt(p.get('t_unc_calc_s'), 1)} s，按任务口径报 ±{unc['reported_s']:.1f} s。\n")
        ap("  * **原因**：首次出现在低仰角（遮挡脊/地平线附近），角速度小，DEM 高程、植被、"
           "建筑、折射等角度误差被 1/d 放大；若遮挡来自近场（<1 km）人造物，实际不确定度更大。\n")
        of = (unc.get("parts") or {}).get("open_field")
        if of:
            dt_of = float(of["delta_s"])
            if dt_of > 0.05:
                shift = (f"首次出现提前到 T+{of['t_met_s']:.2f} s（提前 {dt_of:.1f} s）")
                tail_txt = ("真实值介于“按 DEM 近场”与“开阔场地”之间；当遮挡由 <1 km 单元决定时，"
                            "±1–3 s 应视为不确定度的**下限**。")
            elif dt_of < -0.05:
                shift = (f"首次出现反而推后到 T+{of['t_met_s']:.2f} s"
                         f"（推后 {-dt_of:.1f} s，说明首现遮挡由远场山脊主导）")
                tail_txt = "近场 DEM 单元对首现时刻的影响可忽略。"
            else:
                shift = f"首次出现基本不变（T+{of['t_met_s']:.2f} s）"
                tail_txt = "近场单元对首现时刻影响很小。"
            ap(f"  * **近场敏感性检验**：若忽略 <0.5 km 的近场 DEM 单元（开阔场地假设），"
               f"首现方位附近遮挡角由 {of['horizon_nearfield_deg']:+.2f}° 变为 "
               f"{of['horizon_open_deg']:+.2f}°，" + shift + "。" + tail_txt + "\n")
    else:
        ap("- **全程几何不可见**：地形/地球曲率遮挡，无首次出现/消失时刻。\n")

    # ---- 6. 事件视位置 ----
    ap("## 6. 关键事件视位置\n")
    ev_rows = []
    for ev in view["events"]:
        ev_rows.append([ev["event"], f"{ev['t_met_s']:.2f}", ev["t_utc"], ev.get("t_bj", "-"),
                        f"{ev['az_deg']:.2f}°", f"{ev['el_deg']:.2f}°",
                        f"{ev['slant_range_km']:.1f}", f"{ev['terrain_block_deg']:+.2f}°",
                        "是" if ev["geometrically_visible"] else "否",
                        "是" if ev.get("sunlit") else "否",
                        "是" if ev.get("in_data_range", True) else "否（超出数据范围）"])
    ap(md_table(["事件", "MET (s)", "UTC", "北京时", "视方位角", "视高度角", "斜距 (km)",
                 "遮挡角", "几何可见", "火箭受照", "在数据范围内"], ev_rows) + "\n")
    ap("> 本任务**无助推器**：图上标注的分离事件为一二级分离与整流罩分离。"
       "分离点按 `sim/弹道事件表.csv` 的视位置计算，不受弹道动力学模型影响。\n")

    # ---- 7. 残骸 ----
    ap("## 7. 残骸轨迹与落点\n")
    deb_rows = []
    for dv in view["debris"]:
        imp = dv["impact"]
        if imp is None:
            deb_rows.append([dv["name"], f"{dv['t_min_s']:.0f}–{dv['t_max_s']:.0f}",
                             dv["n_points"], "-", "-", "-", "-", "-", "-"])
            continue
        deb_rows.append([
            dv["name"], f"{dv['t_min_s']:.0f}–{dv['t_max_s']:.0f}", dv["n_points"],
            f"{imp['t_met_s']:.1f}", imp.get("t_bj", "-"),
            f"{imp['lat_deg']:.4f}/{imp['lon_deg']:.4f}",
            f"{imp['az_deg']:.2f}°", f"{imp['el_deg']:+.2f}°",
            ("可见" if imp["geometrically_visible"]
             else f"不可见（遮挡角 {imp['terrain_block_deg']:+.2f}°）")])
    ap(md_table(["对象", "存在时段 (s)", "点数", "落地 MET", "落地北京时", "落点经纬度",
                 "落点视方位", "落点视高度角", "落点地平线状态"], deb_rows) + "\n")
    ap("残骸轨迹与主弹道共用同一 MET 色条；落点通常在地平线以下（el<0），"
       "图上仍画出并标注，但**实际被地形/地球曲率遮挡、不可见**。"
       "落点经纬度与落地时刻取自 `sim/落点与射程.csv`；轨迹来自 `sim/debris_trajectories.csv`。\n")

    # ---- 8. 太阳与地影 ----
    ap("## 8. 太阳、地影与晨昏（几何可见 vs 阳光可见）\n")
    ap(md_table(["项目", "值"], [
        ["观测点太阳高度 @T0", f"{sun['observer_sun_el_t0_deg']:.2f}°（几何）"],
        ["观测点太阳方位 @T0", f"{sun['observer_sun_az_t0_deg']:.1f}°"],
        ["观测点太阳高度范围（飞行时段）",
         f"{sun['observer_sun_el_min_deg']:.2f}° … {sun['observer_sun_el_max_deg']:.2f}°"
         f"（末段 {sun['observer_sun_el_end_deg']:.2f}°）"],
        ["T0 晨昏状态", sun["twilight_state_t0"]],
        ["火箭受照时间占比", f"{sun['rocket_sunlit_fraction'] * 100:.1f}%"],
        ["火箭在地影中时间占比", f"{sun['rocket_umbra_fraction'] * 100:.1f}%"],
        ["火箭进入阳光时刻", ("T+%.2f s（高度 %.1f km）" % (
            sun["first_sunlit"]["t_met_s"], sun["first_sunlit"]["alt_km"]))
         if sun["first_sunlit"] else "全程受照或全程地影"],
        ["阳光可见时长（几何 ∧ 受照）", f"{view['sun_visible_duration_s']:.1f} s"],
        ["地影模型", sun["shadow_models"]],
    ]) + "\n")
    ap("**两套可见性结果对照**：\n")
    rows = [["几何可见（地形判据）", f"{view['visible_duration_s']:.1f} s",
             "—", "视线未被地形/曲率遮挡"],
            ["阳光可见（几何 ∧ 火箭受照）", f"{view['sun_visible_duration_s']:.1f} s",
             "—", "火箭被阳光照射"],
            ["天空暗于 -0.83°（民用晨昏以下）",
             f"{sun['dusk_condition']['duration_s']:.1f} s",
             "是" if sun["dusk_condition"]["condition_met"] else "**否**",
             "肉眼背景较暗"],
            ["天空暗于 -6°（航海晨昏以下）",
             f"{sun['twilight_visible']['nautical']['duration_s']:.1f} s",
             "是" if sun["twilight_visible"]["nautical"]["condition_met"] else "**否**",
             "肉眼接近黑暗天空"]]
    ap(md_table(["判据", "时长（本点）", "条件是否出现", "说明"], rows) + "\n")
    ap("### 曙光/背景亮度对肉眼可见性的影响（重点）\n")
    dawn = sun["observer_sun_el_t0_deg"] < SUN_VISIBLE_OBS_EL_DEG
    if dawn:
        ap(f"T0（北京时 {t0_bj}）观测点太阳**几何**高度约 "
           f"**{sun['observer_sun_el_t0_deg']:.2f}°**，属于**民用晨昏（曙光）**：太阳已在地平线下，"
           "但天空已明显发亮。由此对可见性有以下影响：\n")
    else:
        ap(f"T0（北京时 {t0_bj}）观测点太阳**几何**高度约 "
           f"**{sun['observer_sun_el_t0_deg']:.2f}°**，已在地平线以上（白天）："
           "不存在“曙光增强”效应，肉眼可见性主要受天空背景亮度与目标张角限制。\n")
    h_t0_km = (R_EARTH_M * (1.0 / math.cos(math.radians(
        max(-sun["observer_sun_el_t0_deg"], 0.0))) - 1.0) / 1000.0)
    if dawn:
        first_txt = (f"本任务火箭在 T+{sun['first_sunlit']['t_met_s']:.1f} s（高度 "
                     f"{sun['first_sunlit']['alt_km']:.1f} km）进入阳光。"
                     if sun["first_sunlit"] else "")
        ap("1. **火箭比地面更早见阳光**：晨昏线处地面还在阴影中时，火箭爬升到高度 "
           f"h > R(1/cos|e⊙|−1) ≈ {h_t0_km:.1f} km（T0/该经度处）以上即已进入阳光照射"
           "——这是「暮光现象」的成因：观测者天空尚暗、火箭已被照亮，尾焰/箭体反光对比度反而较高。"
           + first_txt)
    else:
        ap("1. **无曙光增强效应**：T0 观测点已是白天，火箭通常全程受阳光照射，"
           "与背景天空亮度差异小；可见性主要受目标张角与天空亮度限制。")
    ap(f"2. **天空亮度在快速变化**：观测点太阳高度在飞行时段由 "
       f"{sun['observer_sun_el_min_deg']:.2f}° 升到 {sun['observer_sun_el_max_deg']:.2f}°，"
       "天空背景亮度随时间快速升高；飞行后段（太阳升出地平线后）天空已转为白天，"
       "高空小目标与天空的对比度下降，肉眼/普通相机更难分辨。\n")
    sky_txt = ("但 T0 时段天空仍处于民用晨昏（不是真正的暗夜）"
               if dawn else "但 T0 与飞行前段天空为白天/明亮背景")
    ap("3. **结论**：**几何可见 ≠ 肉眼可见**。本任务中「几何可见」时段内，"
       f"火箭受阳光照射的时间约占 {sun['rocket_sunlit_fraction'] * 100:.0f}%，" + sky_txt +
       "，且斜距数百至上千公里时箭体张角远小于 0.1°——肉眼只能在极有利条件下"
       "（暗蓝天、火箭高反光/尾焰、无遮挡方位）看到亮点/亮弧，"
       "**普通手机/相机基本无法分辨，建议双筒望远镜或长焦镜头**。"
       "本报告把「阳光可见」作为**光学可见的必要条件（几何+受照）**，"
       "是否肉眼可见还取决于背景亮度、目标亮度、大气透明度与观测者经验。\n")
    met_levels = [nm for nm, d in sun["twilight_visible"].items() if d["condition_met"]]
    ap(f"4. **晨昏分级**：本任务飞行时段观测点太阳高度 "
       f"{sun['observer_sun_el_min_deg']:.2f}° … {sun['observer_sun_el_max_deg']:.2f}°；"
       + ("最低仅达民用晨昏（-6°~0°），**天空从未达到真正的黑暗**；"
          if not met_levels else
          f"达到的晨昏级别：{'、'.join(met_levels)}；")
       + f"满足 < -0.83° 的时长为 {sun['dusk_condition']['duration_s']:.0f} s，"
       f"满足 < -6° 的时长为 {sun['twilight_visible']['nautical']['duration_s']:.0f} s。\n")

    # ---- 9. 不确定度与局限 ----
    ap("## 9. 不确定度与已知限制\n")
    if placeholder:
        ap("- **T0**：临时版使用夹具参考弹道，与正式任务起飞时刻"
           "无关；正式版 T0 = 2026-11-18 22:50 UTC（假设值）。\n")
    else:
        ap("- **T0 未知**：起飞时刻为假设值（06:50 北京时/22:50 UTC，来自第三方预览与维基列表），"
           "真实 T0 偏差会整体平移全部 MET 时刻，不改变相对几何与可见时长。\n")
    ap("- **地形遮挡**：SRTM 1″（≈30 m 网格，本扫描 90 m 步长）为 2000 年 C 波段测量，"
       "相对高程误差约 ±10 m；**未计植被、城市建筑、堤岸/码头等人造物**，"
       "城市方向遮挡角可能被低估/高估 0.3°–1° 以上。\n")
    ap("- **未计大气折射**：低仰角（<5°）标准折射可抬高视高度角约 0.1°–0.5°，"
       "会使实际出现时刻略早于几何计算值；地影判定也未计大气折射与半影过渡。\n")
    ap("- **观测点高程**：使用 DEM 1″ 采样值（EGM96 正高当椭球高），"
       "真实地面/楼顶高度可能相差 ±5 m 量级；对近场遮挡影响最大。\n")
    ap("- **弹道模型**：3-DOF 标称弹道（无风、无推力偏差、无闭路制导细节），"
       "方位角/高度角对横向偏差敏感（斜距 1000 km 时 1 km 横向偏差 ≈ 0.06°）。\n")
    ap("- **地影**：主用圆柱本影（平行光、球状地球），圆锥本影/半影对照差异 <0.02°；"
       "未计半影亮度渐变与大气散射。\n")
    ap("- **数据截断**：上升段数据到入轨后 60 s 为止；若「消失」时刻 = 数据末点，"
       "则并非真正的几何落下（见 §5 注）。\n")
    ap("- 全部成果**仅供公开信息分析与教学使用，非实际发射弹道、非官方信息**。\n")

    # ---- 10. 复现 ----
    ap("## 10. 复现命令\n")
    ap("```powershell\n"
       "$env:PYTHONPATH = \"" + str(PROJECT_ROOT / "_pylibs") + "\"\n"
       "$env:PYTHONIOENCODING = \"utf-8\"\n"
       "$env:MPLBACKEND = \"Agg\"\n"
       "python sky/plot_sky.py\n"
       "python sky/plot_sky.py --sim-dir _fixtures/ref_sim --outdir sky/_placeholder\n"
       "python sky/plot_sky.py --lat 30.76 --lon 112.73 --name 自定义点\n"
       "```\n")
    ap(f"生成时间（UTC）：{iso_utc(now_utc())}（北京时 {bj_str(now_utc())}）\n")

    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(L), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 8. 主流程
# --------------------------------------------------------------------------- #
def _build_inputs_meta(traj: dict, events: list[dict], debris: list[dict],
                       impacts: list[dict], meta: dict,
                       objects_file: dict | None = None) -> dict:
    """汇总输入文件溯源信息（写入 md / summary）。"""
    return {
        "sim_dir": _rel(SIM_DIR),
        "files": {
            "trajectory": traj.get("file", {"path": _rel(SIM_DIR / "trajectory.csv"),
                                            "exists": False}),
            "debris_trajectories": file_meta(SIM_DIR / "debris_trajectories.csv"),
            "弹道事件表": file_meta(SIM_DIR / "弹道事件表.csv"),
            "落点与射程": file_meta(SIM_DIR / "落点与射程.csv"),
            "objects": objects_file or file_meta(SIM_DIR / "objects.json"),
            "orbit_track": file_meta(SIM_DIR / "orbit_track.csv"),
            "trajectory_meta": meta.get("source", {}),
            "config": file_meta(CONFIG_PATH),
            "observer_dem": file_meta(OBS_DEM_PATH),
            "dem_tiles_manifest": file_meta(PROJECT_ROOT / "data" / "dem" / "srtm_tiles.json"),
        },
    }


def process_observer(obs: dict, traj: dict, t0: datetime, events: list[dict],
                     debris: list[dict], impacts: list[dict], objects: dict,
                     sun_data: dict, orbit, outdir: Path, dpi: int, placeholder: bool,
                     extras: bool = True, place_tag: str = "") -> tuple[dict, dict]:
    """单个观测点：计算视图 → 出 PNG/CSV/MD（+首个观测点的全景/太阳地影附图）。

    ``place_tag`` 非空（多观测点）时，文件名用"地点名_纬度N_经度E"而不是 obs_id。
    """
    print(f"[sky] 计算 {obs['name']}（{obs['id']}）...", flush=True)
    view = build_view(obs, traj, t0, events, debris, impacts, objects, sun_data)
    orbit_setting = None
    need_orbit = (view["last"] is None) or bool(view["last"].get("on_data_end"))
    if orbit is not None and need_orbit:
        try:
            orbit_setting = compute_orbit_setting(
                obs, orbit, view["horizon_deg"], float(traj["t"][-1]), t0)
        except Exception as exc:  # noqa: BLE001
            print(f"[sky] 警告: 轨道段落下时刻计算失败（{exc}）", flush=True)
    view["orbit_setting"] = orbit_setting
    view["orbit_available"] = orbit is not None
    stem = f"天空轨迹_{place_tag or obs['id']}"
    out_png = _unique_path(outdir, stem, ".png")
    out_csv = _unique_path(outdir, stem, ".csv")
    out_md = _unique_path(outdir, stem, ".md")
    inputs = _build_inputs_meta(traj, events, debris, impacts, sun_data["meta"],
                                file_meta(SIM_DIR / "objects.json"))

    print("[sky] 绘图（主图）...", flush=True)
    png_info = plot_observer(view, traj, sun_data, out_png, dpi=dpi,
                             placeholder=placeholder)
    panorama_info = None
    sunpng_info = None
    if extras:
        print("[sky] 绘图（全景图）...", flush=True)
        pano_stem = (f"天空轨迹_全景_{place_tag}" if place_tag else "天空轨迹_全景")
        panorama_info = plot_panorama(
            view, traj, sun_data, _unique_path(outdir, pano_stem, ".png"),
            dpi=dpi, placeholder=placeholder)
        print("[sky] 绘图（太阳/地影示意）...", flush=True)
        sun_stem = (f"太阳地影示意_{place_tag}" if place_tag
                    else f"太阳地影示意_{obs['id']}")
        sunpng_info = plot_sun_shadow(
            view, traj, sun_data, _unique_path(outdir, sun_stem, ".png"),
            dpi=dpi, placeholder=placeholder)
    csv_info = write_csv(view, traj, out_csv)
    write_md(view, traj, t0, sun_data, inputs, png_info, csv_info,
             panorama_info, sunpng_info, out_md, placeholder)
    print(f"[sky] 完成 {obs['name']}: {png_info['path']} "
          f"({png_info['width_px']}×{png_info['height_px']} px), "
          f"{csv_info['path']}, {out_md.name}", flush=True)

    first = view["first"]
    last = view["last"]
    mx = view["max"]
    geom = view.get("max_geom")
    sun = view["sun"]
    for st in (first, last, mx):
        if st is not None and isinstance(st.get("t_utc"), datetime):
            st["t_utc"] = iso_utc(st["t_utc"])

    def _st(st):
        if st is None:
            return None
        return {
            "t_met_s": round(st["t_met_s"], 3),
            "t_utc": st.get("t_utc"), "t_bj": st.get("t_bj"),
            "az_deg": round(st["az_deg"], 3), "el_deg": round(st["el_deg"], 3),
            "slant_range_km": round(st["slant_range_km"], 2),
            "ground_range_km": round(st["ground_range_km"], 2),
            "terrain_block_deg": round(st["terrain_block_deg"], 3),
            "geometrically_visible": bool(st.get("geometrically_visible", False)),
            "on_data_start": bool(st.get("on_data_start", False)),
            "on_data_end": bool(st.get("on_data_end", False)),
        }

    twilight_simple = {
        nm: {"threshold_deg": d["threshold_deg"],
             "condition_met": d["condition_met"],
             "duration_s": round(d["duration_s"], 2)}
        for nm, d in sun["twilight_visible"].items()
    }
    summary = {
        "id": obs["id"], "name": obs["name"],
        "label": str(obs.get("name") or obs["id"]),
        "file_tag": place_tag or obs["id"],
        "lat_deg": obs["lat_deg"], "lon_deg": obs["lon_deg"],
        "dms": dms_str(obs["lat_deg"], obs["lon_deg"]),
        "alt_m": round(obs["alt_m"], 2), "alt_source": obs["alt_source"],
        "config_alt_m": obs["config_alt_m"],
        "dem_tiles": obs.get("tiles_used") or [],
        "dem_cache": obs.get("cache_file"),
        "horizon_deg": {k: round(float(v), 3) for k, v in view["horizon_stats"].items()},
        "horizon_nearfield": view.get("horizon_nearfield"),
        "first_appearance": None if first is None else dict(
            _st(first),
            uncertainty_s=view["appear_uncertainty"]["reported_s"],
            uncertainty_parts=view["appear_uncertainty"]["parts"]),
        "max_elevation": None if mx is None else dict(
            _st(mx),
            basis=mx.get("basis"),
            geometric_max_masked=bool(mx.get("geometric_max_masked", False)),
            geom_max_el_deg=mx.get("geom_max_el_deg"),
            geom_max_t_met_s=mx.get("geom_max_t_met_s")),
        "max_elevation_geometric": None if geom is None else _st(geom),
        "last_visible": _st(last),
        "visible_duration_s": round(view["visible_duration_s"], 2),
        "visible_duration_no_terrain_s": round(view["visible_duration_no_terrain_s"], 2),
        "visible_intervals": [{"t_start": round(iv["t_start"], 3),
                              "t_end": round(iv["t_end"], 3),
                              "duration_s": round(iv["t_end"] - iv["t_start"], 2)}
                              for iv in view["intervals"]],
        "sun_visible_duration_s": round(view["sun_visible_duration_s"], 2),
        "sun_visible_intervals": [{"t_start": round(iv["t_start"], 3),
                                  "t_end": round(iv["t_end"], 3),
                                  "duration_s": round(iv["t_end"] - iv["t_start"], 2)}
                                  for iv in view["intervals_sun"]],
        "appear_uncertainty_calc_s": (
            None if view["appear_uncertainty"]["parts"].get("t_unc_calc_s") is None
            else round(float(view["appear_uncertainty"]["parts"]["t_unc_calc_s"]), 2)),
        "az_window": {
            "center_deg": round(circular_mean_deg(view["arrays"]["az"]), 2),
            "span_deg": round(float(np.ptp(unwrap_around(
                view["arrays"]["az"], circular_mean_deg(view["arrays"]["az"])))), 2),
        },
        "solar": {
            "observer_sun_el_t0_deg": round(sun["observer_sun_el_t0_deg"], 2),
            "observer_sun_az_t0_deg": round(sun["observer_sun_az_t0_deg"], 2),
            "observer_sun_el_min_deg": round(sun["observer_sun_el_min_deg"], 2),
            "observer_sun_el_max_deg": round(sun["observer_sun_el_max_deg"], 2),
            "observer_sun_el_end_deg": round(sun["observer_sun_el_end_deg"], 2),
            "twilight_state_t0": sun["twilight_state_t0"],
            "rocket_sunlit_fraction": round(sun["rocket_sunlit_fraction"], 4),
            "rocket_umbra_fraction": round(sun["rocket_umbra_fraction"], 4),
            "first_sunlit": sun["first_sunlit"],
            "dusk_condition": {"threshold_deg": sun["dusk_condition"]["threshold_deg"],
                               "condition_met": sun["dusk_condition"]["condition_met"],
                               "duration_s": round(sun["dusk_condition"]["duration_s"], 2)},
            "twilight_visible": twilight_simple,
            "shadow_model": sun["shadow_models"],
        },
        "events": [
            {"event": e["event"], "t_met_s": round(e["t_met_s"], 2),
             "t_utc": e["t_utc"], "t_bj": e.get("t_bj"),
             "az_deg": round(e["az_deg"], 3), "el_deg": round(e["el_deg"], 3),
             "slant_range_km": round(e["slant_range_km"], 2),
             "terrain_block_deg": round(e["terrain_block_deg"], 3),
             "geometrically_visible": e["geometrically_visible"],
             "sunlit": e.get("sunlit")}
            for e in view["events"]],
        "debris": [
            {"object": d["object"], "name": d["name"],
             "t_range_s": [round(d["t_min_s"], 2), round(d["t_max_s"], 2)],
             "n_points": d["n_points"],
             "visible_fraction": round(d["visible_fraction"], 4),
             "impact": None if d["impact"] is None else {
                 "t_met_s": round(d["impact"]["t_met_s"], 2),
                 "t_utc": d["impact"]["t_utc"], "t_bj": d["impact"].get("t_bj"),
                 "lat_deg": d["impact"]["lat_deg"], "lon_deg": d["impact"]["lon_deg"],
                 "az_deg": round(d["impact"]["az_deg"], 3),
                 "el_deg": round(d["impact"]["el_deg"], 3),
                 "terrain_block_deg": round(d["impact"]["terrain_block_deg"], 3),
                 "geometrically_visible": d["impact"]["geometrically_visible"],
                 "zone_id": d["impact"]["zone_id"],
                 "inside_zone": d["impact"]["inside_zone"],
                 "great_circle_km": (None if not np.isfinite(d["impact"]["great_circle_km"])
                                     else round(d["impact"]["great_circle_km"], 2)),
             }}
            for d in view["debris"]],
        "outputs": {"png": png_info, "panorama_png": panorama_info,
                    "sun_shadow_png": sunpng_info, "csv": csv_info,
                    "md": str(out_md.name)},
        "orbit_setting": orbit_setting,
    }
    return view, summary


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="指定坐标天空轨迹图（任务取自 config.json；可用 --sim-dir 指定数据目录）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    p.add_argument("--sim-dir", default=None,
                   help="仿真数据目录（默认 <repo>/sim；可用 _fixtures/ref_sim 做流程验证）")
    p.add_argument("--obs", default="all", help="观测点 id（默认 all）")
    p.add_argument("--lat", type=float, default=None, help="自定义观测点纬度 [deg]")
    p.add_argument("--lon", type=float, default=None, help="自定义观测点经度 [deg]")
    p.add_argument("--name", default="自定义观测点", help="自定义观测点名称")
    p.add_argument("--alt", type=float, default=None,
                   help="自定义观测点椭球高 [m]（缺省用 SRTM DEM 采样）")
    p.add_argument("--outdir", default=None,
                   help="输出目录（默认 sky/；临时/夹具模式默认 sky/_placeholder/）")
    p.add_argument("--dpi", type=int, default=160,
                   help="PNG 分辨率（默认 160 dpi，主图约 2200×1600 px）")
    p.add_argument("--placeholder", action="store_true",
                   help="强制使用内置示意弹道（调试用）")
    p.add_argument("--no-extras", action="store_true",
                   help="不输出全景图/太阳地影示意（快速模式）")
    p.add_argument("--config", default=None,
                   help="任务配置（默认 <root>/config.json，兼容旧的 common/config.json）")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def build_sun_data(traj: dict, t0: datetime, meta: dict) -> dict:
    """计算全弹道太阳方向、地影掩码、星下点地面太阳高度与地影边界高度。"""
    dt_list = traj["t_utc"] or _dt_list_from(t0, traj["t"])
    sun_ecef = np.array([geo.sun_ecef_unit(d) for d in dt_list])
    flags = _shadow_flags(traj["ecef"], sun_ecef)
    n = int(traj["t"].size)
    ground_sun_el = np.empty(n)
    for i in range(n):
        ground_sun_el[i] = float(geo.solar_elevation_deg(
            traj["lat"][i], traj["lon"][i], dt_list[i]))
    sun_alt_km = _sunlit_altitude_km(ground_sun_el)
    sunlit = ~flags["in_umbra"]
    print(f"[sky] 火箭受照时间占比: {np.mean(sunlit) * 100:.1f}%", flush=True)
    print(f"[sky] 星下点地面太阳高度: {np.min(ground_sun_el):.2f}° … "
          f"{np.max(ground_sun_el):.2f}°", flush=True)
    return {
        "t0": t0, "t0_str": iso_utc(t0), "t0_bj": bj_str(t0),
        "t_utc": dt_list, "t": traj["t"], "meta": meta, "site": meta["site"],
        "sun_ecef": sun_ecef,
        "sunlit": sunlit, "umbra": flags["in_umbra"],
        "penumbra": flags["in_penumbra"], "in_cylinder": flags["in_cylinder"],
        "ground_sun_el": ground_sun_el, "sun_alt_km": sun_alt_km,
    }


def _clean_stale_outputs(outdir: Path) -> None:
    """清理上一次运行残留的 sky 产物。

    不清会出问题：旧图（含旧命名的 ``天空轨迹_全景.png``/``太阳地影示意_obs1.png``）
    会被本次 QC 的"天空轨迹 PNG"统计重复计入，也会让用户看到两套图。
    """
    patterns = ("天空轨迹_*.png", "天空轨迹_*.csv", "天空轨迹_*.md",
                "太阳地影示意_*.png", "出现方位地形*.png",
                "出现方位地形_数据*.json", "sky_summary.json")
    n = 0
    for pat in patterns:
        for p in outdir.glob(pat):
            try:
                p.unlink()
                n += 1
            except Exception:  # noqa: BLE001
                pass
    if n:
        print(f"[sky] 清理旧产物 {n} 个（{_rel(outdir)}）", flush=True)


def _self_check(outdir: Path, summaries: dict) -> bool:
    """交付前自检：PNG 尺寸/CSV 列与 NaN/时刻单调/遮挡角范围。"""
    try:
        import pandas as pd
    except Exception:  # noqa: BLE001 - pandas 只是可选依赖（requirements 未列）
        pd = None

    print("\n===== 自检 =====")
    ok = True
    for oid, s in summaries.items():
        for key in ("png", "panorama_png", "sun_shadow_png"):
            o = s["outputs"].get(key)
            if not o:
                continue
            w = int(o.get("width_px", 0))
            good = w >= 1600
            ok = ok and good
            print(f"  [{'OK ' if good else 'FAIL'}] PNG {o['path']}: "
                  f"{w}×{o.get('height_px', 0)} px")
        csv_rel = s["outputs"]["csv"]["path"]
        need = ["t_met_s", "az_deg", "el_deg", "slant_range_km", "ground_range_km",
                "terrain_block_deg", "geometric_visible", "sun_flag"]
        if pd is not None:
            df = pd.read_csv(PROJECT_ROOT / csv_rel, encoding="utf-8-sig")
            missing = [c for c in need if c not in df.columns]
            nan = int(df[need].isna().sum().sum()) if not missing else -1
            n_rows = len(df)
        else:
            import csv as _csv
            rows = list(_csv.DictReader(open(PROJECT_ROOT / csv_rel, encoding="utf-8-sig")))
            missing = [c for c in need if rows and c not in rows[0]]
            nan = sum(1 for r in rows for c in need if not (r.get(c) or "").strip())
            n_rows = len(rows)
        good = (not missing) and nan == 0
        ok = ok and good
        print(f"  [{'OK ' if good else 'FAIL'}] CSV {csv_rel}: {n_rows} 行, "
              f"列齐全={not missing}, NaN={nan}")
        f, m, l = s["first_appearance"], s["max_elevation"], s["last_visible"]
        if f and l:
            # v3.2.3：只要求"可见区间"内 first ≤ last；上报的最大高度角本身就是在可见区间内
            # 取的，因此不应再出现 first ≤ max ≤ last 不成立的误报。
            tol = 0.06
            max_in = (m is None) or (float(f["t_met_s"]) - tol <= float(m["t_met_s"])
                                     <= float(l["t_met_s"]) + tol)
            good = bool(float(f["t_met_s"]) <= float(l["t_met_s"]) + tol and max_in)
            ok = ok and good
            _mtxt = ("" if m is None else
                     f"；最大高度角 {m['el_deg']:.2f}° @ T+{m['t_met_s']:.2f} s（可见时段内）")
            print(f"  [{'OK ' if good else 'FAIL'}] 可见区间时刻：first {f['t_met_s']:.2f} "
                  f"≤ last {l['t_met_s']:.2f} s{_mtxt}"
                  + ("" if max_in else "（最大高度角不在可见区间内）"))
            if (m is not None) and m.get("geometric_max_masked"):
                print(f"  [INFO] 几何最大高度角 {m['geom_max_el_deg']:+.2f}° @ "
                      f"T+{m['geom_max_t_met_s']:.2f} s 落在可见区间之外"
                      f"（该时刻已被地形/地球曲率遮挡），最大高度角按可见时段统计")
        hz = s["horizon_deg"]
        plausible = (hz["max"] < 10.0) and (hz["min"] > -2.0)
        print(f"  [{'INFO' if plausible else 'WARN'}] 地形遮挡角范围 "
              f"{hz['min']:+.2f}°…{hz['max']:+.2f}°（平原应接近 -0.1°~+1°）")
    print(f"自检结果：{'全部通过' if ok else '存在失败项'}")
    return ok


def main(argv=None) -> int:
    args = parse_args(argv)
    plotstyle.setup(font_size=10, verbose=args.verbose)
    global SIM_DIR
    stock = PROJECT_ROOT / "sim"
    if args.sim_dir:
        p = Path(args.sim_dir)
        SIM_DIR = p if p.is_absolute() else (PROJECT_ROOT / p)
    elif (stock / "trajectory.csv").exists():
        SIM_DIR = stock
    elif (FIXTURE_SIM_DIR / "trajectory.csv").exists():
        SIM_DIR = FIXTURE_SIM_DIR
        print("[sky] 警告: 正式 sim/trajectory.csv 未出现 → 临时使用 _fixtures/ref_sim"
              "（CZ-8A 夹具，仅用于流程验证）", flush=True)
    else:
        SIM_DIR = stock
    temp_mode = SIM_DIR.resolve() != stock.resolve()

    cfg = load_config(args.config)
    meta = load_meta(cfg)
    t0 = meta["t0_utc"]
    _SUN_CACHE["t0"] = t0
    global MISSION_TITLE
    MISSION_TITLE = _mission_title(cfg)          # v3.1：按任务动态生成图题
    print(f"[sky] 任务: {MISSION_TITLE}", flush=True)
    print(f"[sky] 输入目录: {_rel(SIM_DIR)}  （临时版={temp_mode}）", flush=True)
    print(f"[sky] T0 = {iso_utc(t0)}（北京时 {bj_str(t0)}）", flush=True)

    # ---- 观测点 ----
    if (args.lat is not None) ^ (args.lon is not None):
        raise SystemExit("--lat 与 --lon 必须同时给出")
    if args.lat is not None:
        observers = [make_custom_observer(args.lat, args.lon, args.name, args.alt)]
    else:
        all_obs = load_observers(cfg)
        if args.obs.strip().lower() in ("", "all"):
            observers = all_obs
        else:
            wanted = [s.strip() for s in args.obs.split(",") if s.strip()]
            by_id = {o["id"]: o for o in all_obs}
            missing = [w for w in wanted if w not in by_id]
            if missing:
                raise SystemExit(f"未知观测点 id: {missing}（可用：{list(by_id)}）")
            observers = [by_id[w] for w in wanted]

    # ---- 数据 ----
    traj = load_trajectory(cfg, t0, force_placeholder=args.placeholder)
    placeholder = bool(traj.get("placeholder", False)) or temp_mode
    for fname in ("debris_trajectories.csv", "objects.json", "弹道事件表.csv", "落点与射程.csv"):
        if not (SIM_DIR / fname).exists():
            print(f"[sky] 警告: 缺少 {_rel(SIM_DIR / fname)} → 使用占位/回退数据", flush=True)

    if args.outdir:
        outdir = Path(args.outdir)
        if not outdir.is_absolute():
            outdir = PROJECT_ROOT / outdir
    else:
        outdir = PLACEHOLDER_DIR if placeholder else SKY_DIR
    outdir.mkdir(parents=True, exist_ok=True)
    _clean_stale_outputs(outdir)          # v3.2.3：先清旧产物，避免重复统计/新旧混排
    print(f"[sky] 输出目录: {_rel(outdir)}  （placeholder={placeholder}）", flush=True)

    events = load_events(traj, t0)
    debris = load_debris(traj, events, t0)
    impacts = load_impacts(traj, debris)
    objects = load_objects()
    orbit = load_orbit_track()
    if orbit is not None:
        print(f"[sky] 轨道段数据: {_rel(SIM_DIR / 'orbit_track.csv')}"
              f" {orbit['rows']} 点（t {orbit['t'][0]:.0f}–{orbit['t'][-1]:.0f} s）",
              flush=True)

    # ---- 太阳/地影（全弹道，所有观测点共用） ----
    sun_data = build_sun_data(traj, t0, meta)

    # ---- 逐观测点 ----
    multi_obs = len(observers) > 1
    summaries: dict = {}
    for k, obs in enumerate(observers):
        extras = (not args.no_extras) and (k == 0)
        place_tag = _obs_place_tag(obs) if multi_obs else ""
        _view, summary = process_observer(
            obs, traj, t0, events, debris, impacts, objects, sun_data, orbit,
            outdir, args.dpi, placeholder, extras=extras, place_tag=place_tag)
        summaries[obs["id"]] = summary

    # ---- sky_summary.json ----
    inputs = _build_inputs_meta(traj, events, debris, impacts, meta,
                                file_meta(SIM_DIR / "objects.json"))
    summary_doc = {
        "module": "sky",
        "script": "sky/plot_sky.py",
        "mission": MISSION_TITLE,
        "generated_utc": iso_utc(now_utc()),
        "generated_bj": bj_str(now_utc()),
        "coordinate_system": "WGS-84",
        "naming": {
            "multi_observer": bool(len(observers) > 1),
            "pattern": ("多观测点：天空轨迹_{地点名}_{纬度}{N|S}_{经度}{E|W}.png/csv/md，"
                        "全景/太阳地影同规则；单观测点沿用 {obs_id}" if len(observers) > 1
                        else "单观测点：天空轨迹_{obs_id}"),
        },
        "time_system": "UTC；MET = 起飞后秒数；北京时 = UTC+8",
        "data_basis": {
            "sim_dir": inputs["sim_dir"],
            "temp_or_fixture": bool(temp_mode),
            "placeholder": bool(placeholder),
            "trajectory_file": traj.get("file", {}),
            "trajectory_rows": int(traj["rows"]),
            "trajectory_t_span_s": [round(float(traj["t"][0]), 2),
                                    round(float(traj["t"][-1]), 2)],
            "inputs": inputs["files"],
        },
        "t0_utc": iso_utc(t0),
        "t0_bj": bj_str(t0),
        "t0_note": "标称假设值：NASA Spaceflight 预览（22:50 UTC）+ 中文维基发射列表"
                   "（06:50 北京时），非官方实测",
        "launch_site": meta["site"],
        "observer_note": "观测点来自 config.json -> observers[]（id/name/坐标/高程与高程来源见 observers 字段；"
                         "高程优先取 SRTM 1″ DEM，缺失时用配置假设值）",
        "visibility_criterion": "geometric_visible = el_deg > terrain_block_deg"
                                "（SRTM 1″ DEM，90 m 步长，200 km，sea_level_clip=True）",
        "sun_visibility_criterion": "sun_visible = geometric_visible ∧ 火箭受照（地影外）；"
                                    "肉眼可见还需天空足够暗（太阳高度 < -0.83°/-6°）",
        "sun_shadow_model": "圆柱本影（R=6371.0088 km、平行光）为主，"
                            "圆锥本影/半影（太阳视半径 0.2666°）对照，LEO 差异 <0.02°",
        "dem": {
            "product": "SRTMGL1 v003（NASA/USGS，AWS Open Data elevation-tiles-prod/skadi）",
            "vertical_datum": "EGM96 正高（当 WGS-84 椭球高使用，晋中盆地差约 -10~-30 m）",
            "cache": "data/dem/cache/<observer_id>.npz（有则用 3″ 缓存 + 近场 <10 km 1″ 修正；"
                     "无缓存时直接采样 1″ 瓦片）",
            "step_m": HORIZON_STEP_M,
            "radius_km": HORIZON_RADIUS_KM,
            "sea_level_clip": True,
            "observer_dem_info": file_meta(OBS_DEM_PATH),
        },
        "observers": summaries,
        "caveats": [
            "T0 为假设值（第三方预览 + 维基列表），真实 T0 未知；MET 整体平移不改变相对几何",
            "地形遮挡未计大气折射、植被、城市建筑/堤岸伪影；首次出现不确定度按 ±1–3 s 报",
            "T0 观测点处于民用晨昏（曙光，太阳高度约 -2.5°）：几何可见 ≠ 肉眼可见，"
            "需结合天空亮度、目标张角与大气透明度",
            "弹道为 3-DOF 标称反演，非实际发射弹道；仅供公开信息分析与教学使用",
        ],
        "self_check": None,
    }
    (outdir / "sky_summary.json").write_text(
        json.dumps(summary_doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[sky] 汇总: {_rel(outdir / 'sky_summary.json')}", flush=True)

    # ---- 自检 + 控制台摘要 ----
    ok = _self_check(outdir, summaries)
    summary_doc["self_check"] = {
        "ok": bool(ok),
        "checked": ["PNG 宽度≥1600 px", "CSV 列齐全且无 NaN", "首次/最大/消失时刻单调",
                    "地形遮挡角范围合理"],
        "outdir": _rel(outdir),
    }
    (outdir / "sky_summary.json").write_text(
        json.dumps(summary_doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n===== 天空轨迹结果摘要 =====")
    print(f"T0 = {summary_doc['t0_utc']}（北京时 {summary_doc['t0_bj']}）")
    for oid, s in summaries.items():
        f, m, l = s["first_appearance"], s["max_elevation"], s["last_visible"]
        if f is None:
            print(f"{s['name']}({oid}): 全程几何不可见")
            continue
        print(f"{s['name']}({oid}):")
        print(f"  首次出现 T+{f['t_met_s']:.2f}s = {f['t_utc']}"
              f"（北京时 {f['t_bj']}）, az {f['az_deg']:.2f}°, el {f['el_deg']:.2f}°,"
              f" ±{f['uncertainty_s']:.1f}s")
        print(f"  最大高度角（可见时段内） {m['el_deg']:.2f}° @ T+{m['t_met_s']:.2f}s"
              f" = {m['t_utc']}（北京时 {m['t_bj']}）, az {m['az_deg']:.2f}°")
        if m.get("geometric_max_masked"):
            print(f"  （几何最大 {m['geom_max_el_deg']:+.2f}° @ T+{m['geom_max_t_met_s']:.1f}s "
                  f"落在可见区间外，已被地形遮挡，仅供参考）")
        print(f"  消失/可见终止 T+{l['t_met_s']:.2f}s = {l['t_utc']}"
              f"（北京时 {l['t_bj']}）, el {l['el_deg']:.2f}°"
              f"{'（数据结束）' if l.get('on_data_end') else ''}")
        print(f"  几何可见 {s['visible_duration_s']:.1f}s | 阳光可见 "
              f"{s['sun_visible_duration_s']:.1f}s | 观测点太阳高度 T0 "
              f"{s['solar']['observer_sun_el_t0_deg']:.2f}° ({s['solar']['twilight_state_t0']})")
        print(f"  遮挡角 {s['horizon_deg']['min']:+.2f}°…{s['horizon_deg']['max']:+.2f}°"
              f" | PNG {s['outputs']['png']['width_px']}×"
              f"{s['outputs']['png']['height_px']} px")
    print("==============================")
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
