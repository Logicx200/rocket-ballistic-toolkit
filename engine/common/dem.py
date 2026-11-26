# -*- coding: utf-8 -*-
"""common.dem — SRTM 1″ (SRTMGL1) 地形读取、采样与**地形遮挡角**计算。

数据格式
--------
SRTMGL1 HGT 瓦片：1°×1° 覆盖，3601×3601 个 **int16 大端** 样本，
行序**自北向南**（第 0 行 = 瓦片北边 1′ 边界），采样间隔 1 arc-second。

* 1″ ≈ 30.9 m（赤道）/ 29.3 m（北纬 19°）——**这是"1 弧秒 DEM"的物理采样间隔**；
* 任务里说的 "90 m 采样" 指 :func:`terrain_horizon_deg` 的**沿方位步长**取 90 m
  （≈3″），以及观测点局部缓存用 3″ 网格（≈90 m）。两者都不改变原始瓦片分辨率的说明。
* 高程为 **EGM96 大地水准面正高** [m]，与 WGS-84 椭球高相差约 -10 ~ -30 m；
  本模块把 DEM 高程**当作椭球高使用**（见下方"高程基准"）。
* **海面是负高程（水深）而不是 0 或 void** —— 实测 AWS skadi 的 SRTMGL1 瓦片在海上
  用**海底地形**填充（如 N16E113 全瓦片 -3285…-377 m，N17E109 中位数 -100 m，
  与南海实际水深一致），void(-32768) 在已下载 63 个瓦片中比例为 0。
  因此：等值面/遮挡计算必须用 ``sea_level_clip=True``（负高程→0，视为海面）；
  查询"海拔"时负值表示水深（:func:`dem_alt` 默认原样返回，可 ``clip_negative=True``）。
* 空值 void = **-32768**（SRTM 无回波区）。本模块策略：
  - 点采样（:func:`sample`）→ 返回 ``np.nan``；
  - :func:`dem_alt` → 返回 0.0（海上/无数据按海平面处理）；
  - 局部缓存网格 → 用**邻域迭代填补**（8 邻域，最多 8 轮），并记录 ``void_ratio``；
  - 地形遮挡计算 → void 视为 0（不会凭空造出遮挡）。

高程基准
--------
观测点与地形点的高程都用 DEM 正高（EGM96），地形遮挡角只用**两者之差**，
大地水准面起伏在 200 km 内变化约几米，对遮挡角影响 < 0.01°（远场）；
近场（<2 km）可能到 0.1° 量级，报告需注明。整个任务的高程系统见
``common/data_contract.md``：轨迹用椭球高、DEM 用正高。

地球曲率
--------
遮挡角计算**不做** d²/2R 近似，而是把观测点与地形点都换成 ECEF，再转到观测点
局部 ENU 系求仰角。等价于球面近似下的

.. math:: h = \\arctan\\frac{\\Delta H - d^2/(2R)}{d}

地平下沉量在 d=100 km 时 ≈ 785 m（0.45°）、d=200 km 时 ≈ 3.14 km（0.90°）。
**不计大气折射**（标准折射在 200 km 处约抬高 0.1°–0.2°，小于地形遮挡本身的不确定度）。

局部缓存
--------
``data/dem/cache/<obs_id>.npz``：以观测点为中心、覆盖 ``radius_km`` 的 3″（≈90 m）网格
（float32 存经纬度范围 + int16 高程），供 :func:`terrain_horizon_deg` 快速采样。
``data/dem/observer_dem.json`` 记录每个观测点的 DEM 高程、用到的瓦片、void 比例。

v3.0 粗 DEM 回退（离线版关键）
------------------------------
离线版在**没有任何 SRTM 瓦片**时也要能跑完整条流水线（天空轨迹/遮挡角/观测点高程）。
因此本模块内置一层"粗 DEM 回退"：

* 粗 DEM = ``app/data/china_dem_0p02.npz``（内嵌 0.02°≈2 km 中国范围网格，
  由 ``engine/tools/build_coarse_dem.py`` 从公开 terrarium 地形瓦片构建）；
* 路径解析：环境变量 ``RK_COARSE_DEM`` → ``<引擎>/data/china_dem_0p02.npz`` →
  ``<项目根>/app/data/china_dem_0p02.npz``；
* 触发条件：点采样差瓦片（:func:`sample` / :func:`dem_alt`）、观测点 200 km 内
  1″ 瓦片不齐（:func:`terrain_horizon_deg` / :func:`terrain_profile`）；
* 算法口径不变：粗网格**双线性插值**取高程，仍走 WGS-84 测地线正解 + ECEF/ENU
  精确仰角（不做 d²/2R 近似）；粗网格下遮挡角通常被平滑掉 0.2–1.5°；
* 模式记录在模块级变量 :data:`LAST_MODE` / :data:`LAST_MODE_NOTE`（供上层 runner
  写入 meta）；同时打一条中文警告："未找到 SRTM 瓦片，已回退 0.02° 粗 DEM（约 2 km），
  首次出现时刻精度约 ±数十秒"；
* **有瓦片时原有 SRTM 路径逐位不变**（回归红线）；engine 不 import app，
  粗网格读取只依赖 numpy（npz 格式见 ``app/core/coarse_dem.py``）。

v3.1 离线自主导入的高程源（自定义源优先）
------------------------------------------
用户在无网络时可以通过 ``app/core/dem_import.py`` 导入自己的 GeoTIFF（WGS84 经纬度）。
登记结果在 ``<RK_DEM>/custom/index.json``，本模块**懒加载**：

* :func:`custom_available` / :func:`custom_covering` / :func:`custom_sampler_for_bbox`
  读取登记表并用 :class:`common.geotiff.GeoTiffSource` 打开引用文件（只在第一次
  采样时打开，模块 import 时绝不做 1.3 GB 文件 IO）；
* :func:`sample` 先按"自定义源覆盖掩码"取数，**未覆盖的点继续走原来的 SRTM/粗 DEM
  逻辑**，输出形状/``method``/``missing``/``void_value`` 语义完全不变；
* :func:`tiles_available_for` 认为"自定义源覆盖"等价于"瓦片齐备"；
* :func:`_make_samplers` 在自定义源覆盖观测点半径时返回
  ``(custom_sampler, None, "custom")``，模式记为 ``"custom"``；自定义源优先于粗 DEM；
* **没有 custom/index.json 时所有原有函数行为、返回值、模式字符串逐位不变**；
* 自定义源 nodata 视为缺测（nan），上层按原规则 nan→0 处理。

命令行
------
    python -m common.dem --build                  # 构建 3 个观测点 + 发射场缓存
    python -m common.dem --build --radius-km 200  # 指定半径
    python -m common.dem --query 19.60081 110.93575   # 查询单点高程
    python -m common.dem --horizon sanya          # 打印观测点遮挡角摘要
    python -m common.dem --list                   # 本地瓦片状态
    python -m common.dem --custom                 # 自定义高程源状态
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .geo import (EARTH_MEAN_R_KM, WGS84_A, WGS84_B, WGS84_E2, WGS84_F,
                  ecef_to_enu, geodetic_to_ecef)

__all__ = [
    "VOID_VALUE", "HGT_N", "HGT_BYTES", "DIR_GZ", "DIR_HGT", "DIR_CACHE",
    "tile_name", "tile_path", "tile_bounds", "available_tiles", "tile_manifest",
    "read_tile", "sample", "sample_points", "dem_alt",
    "build_observer_cache", "load_observer_cache", "cache_path",
    "observer_dem_info", "fill_voids_in_grid",
    "terrain_profile", "terrain_horizon_deg", "horizon_table",
    "COARSE_DEM_NAME", "COARSE_WARNING", "LAST_MODE", "LAST_MODE_NOTE",
    "coarse_dem_path", "coarse_dem_available", "last_mode", "tiles_available_for",
    "CUSTOM_INDEX", "custom_index_path", "custom_available", "custom_sources",
    "custom_covering", "custom_sampler_for_bbox", "custom_covers_radius",
    "_reset_custom_cache",
]

# --------------------------------------------------------------------------- #
# 常量 / 路径
# --------------------------------------------------------------------------- #
HGT_N = 3601                              # 每边样本数
HGT_BYTES = HGT_N * HGT_N * 2             # 25 934 402
VOID_VALUE = -32768                       # SRTM 空值
ARCSEC_PER_DEG = 3600.0
CACHE_STEP_ARCSEC = 3.0                   # 观测点缓存网格间距（3″ ≈ 90 m）
CACHE_DLAT = CACHE_STEP_ARCSEC / ARCSEC_PER_DEG    # 1/1200 度

# v3.0：引擎（只读代码/资源）与数据缓存目录解耦，便于冻结打包后把 SRTM 瓦片缓存
# 写到可写目录（%LOCALAPPDATA%）或用户指定目录，而不改只读的引擎目录。
#   RK_ENGINE : 引擎根目录（含 common/ scripts/ data/ne/ viz/files/），默认本文件上两级
#   RK_DEM    : SRTM 瓦片缓存根目录（可写），默认 <引擎>/data/dem
#   RK_ROOT   : 工作区目录（产物 sim/ sky/ map/ ... 与 config.json），由各脚本自行解析
ROOT = Path(os.environ.get("RK_ENGINE") or Path(__file__).resolve().parents[1])
CONFIG_PATH = Path(os.environ.get("RK_ROOT") or ROOT) / "config.json"
DATA_DEM = Path(os.environ.get("RK_DEM") or (ROOT / "data" / "dem"))
DIR_GZ = DATA_DEM / "gz"
DIR_HGT = DATA_DEM / "hgt"
DIR_CACHE = DATA_DEM / "cache"
MANIFEST = DATA_DEM / "srtm_tiles.json"
OBSERVER_INFO = DATA_DEM / "observer_dem.json"
#: 自定义高程源（用户导入的 GeoTIFF）登记表；由 app/core/dem_import.py 维护
CUSTOM_INDEX = DATA_DEM / "custom" / "index.json"
CUSTOM_SCHEMA = "rocketkit.dem_custom.v1"

# 内存中的瓦片缓存（memmap，避免重复打开）：{tile_name: np.memmap}
_TILE_CACHE: dict[str, np.ndarray] = {}
# 观测点缓存（npz 解压后的 dict）
_OBS_CACHE: dict[str, dict] = {}


# --------------------------------------------------------------------------- #
# v3.0 内嵌粗 DEM（0.02°）回退
# --------------------------------------------------------------------------- #
# 说明：离线版没有 SRTM 瓦片时，点采样/地形遮挡自动回退到内嵌粗网格，
#       保证 plot_sky / appear 等整条流水线不崩。粗网格 npz 由
#       engine/tools/build_coarse_dem.py 生成（见 app/core/coarse_dem.py 的格式说明），
#       **只依赖 numpy**，engine 不需要 import app。
COARSE_DEM_NAME = "china_dem_0p02.npz"
COARSE_WARNING = ("未找到 SRTM 瓦片，已回退 0.02° 粗 DEM（约 2 km），"
                  "首次出现时刻精度约 ±数十秒")
#: 最近一次地形/高程查询的数据模式：'srtm' | 'coarse' | 'none'
LAST_MODE = "none"
#: 模式附带说明（'tiles' / 'cache' / 'cache+1' / 'coarse' / 'sample'）
LAST_MODE_NOTE = ""
_LAST_COARSE_PATH = ""
_COARSE = None                 # None=未加载; False=不可用; dict=已加载粗网格
_COARSE_WARNED = False
_LAST_WRITTEN_MODE = None      # 上次写入 <RK_DEM>/dem_mode.json 的模式（去重）


def coarse_dem_path() -> Path:
    """内嵌粗 DEM npz 路径（不保证存在）。

    解析顺序：环境变量 ``RK_COARSE_DEM`` → ``<引擎根>/data/china_dem_0p02.npz`` →
    ``<项目根>/app/data/china_dem_0p02.npz``（开发态 engine 的上一级）→ 当前工作目录。
    """
    env = os.environ.get("RK_COARSE_DEM")
    if env:
        return Path(env)
    candidates = [
        ROOT / "data" / COARSE_DEM_NAME,
        ROOT.parent / "app" / "data" / COARSE_DEM_NAME,
        Path.cwd() / "app" / "data" / COARSE_DEM_NAME,
    ]
    for p in candidates:
        if p.is_file():
            return p
    return candidates[0]


def _load_coarse():
    """懒加载粗网格 → dict 或 None（失败不抛异常）。"""
    global _COARSE, _LAST_COARSE_PATH
    if _COARSE is not None:
        return _COARSE or None
    try:
        path = coarse_dem_path()
        if not path.is_file():
            _COARSE = False
            return None
        with np.load(path, allow_pickle=False) as z:
            lat = np.asarray(z["lat"], dtype=float).ravel()
            lon = np.asarray(z["lon"], dtype=float).ravel()
            elev = np.asarray(z["elev"], dtype=np.int16)
        if elev.ndim != 2 or elev.shape != (lat.size, lon.size) \
                or lat.size < 2 or lon.size < 2:
            _COARSE = False
            return None
        # 分辨率 round 到 1e-6°：npz 里 lat 是 float32，差分有 ~5e-7 抖动；
        # 与 app/core/coarse_dem.py 用同一口径，保证两边双线性插值逐点一致。
        step = round(float(np.median(np.diff(lat))), 6)
        if not np.isfinite(step) or step <= 0:
            _COARSE = False
            return None
        _COARSE = {
            "lat": lat, "lon": lon, "elev": elev, "step": step,
            "lat0": float(lat[0]) - 0.5 * step,
            "lon0": float(lon[0]) - 0.5 * step,
            "path": path,
        }
        _LAST_COARSE_PATH = str(path)
        return _COARSE
    except Exception:                    # noqa: BLE001 - 粗 DEM 坏了也不能让流水线崩
        _COARSE = False
        return None


def coarse_dem_available() -> bool:
    """内嵌粗 DEM 是否可用（缺文件/坏文件返回 False，不抛异常）。"""
    return _load_coarse() is not None


def _coarse_bilinear(lat, lon):
    """粗网格双线性插值 [m]；粗 DEM 不可用或网格外 → nan。

    公式与 ``app/core/coarse_dem.py::CoarseDEM.sample`` 完全一致
    （格心定义：``lat0 = lat[0] - step/2``，索引 ``(lat-lat0)/step - 0.5``）。
    """
    g = _load_coarse()
    if not g:
        return None
    la = np.asarray(lat, dtype=float)
    lo = np.asarray(lon, dtype=float)
    la, lo = np.broadcast_arrays(la, lo)
    elev = g["elev"]
    ny, nx = elev.shape
    step, lat0, lon0 = g["step"], g["lat0"], g["lon0"]
    r = (la - lat0) / step - 0.5
    c = (lo - lon0) / step - 0.5
    inside = (np.isfinite(la) & np.isfinite(lo)
              & (r >= -0.5) & (r <= ny - 0.5)
              & (c >= -0.5) & (c <= nx - 0.5))
    out = np.full(la.shape, np.nan, dtype=float)
    if not inside.any():
        return out
    rr = np.clip(r, 0.0, ny - 1.0)
    cc = np.clip(c, 0.0, nx - 1.0)
    r0 = np.minimum(np.floor(rr).astype(np.intp), ny - 2)
    c0 = np.minimum(np.floor(cc).astype(np.intp), nx - 2)
    fr = rr - r0
    fc = cc - c0
    v00 = elev[r0[inside], c0[inside]].astype(float)
    v10 = elev[r0[inside] + 1, c0[inside]].astype(float)
    v01 = elev[r0[inside], c0[inside] + 1].astype(float)
    v11 = elev[r0[inside] + 1, c0[inside] + 1].astype(float)
    fi, fj = fr[inside], fc[inside]
    out[inside] = (v00 * (1 - fi) * (1 - fj) + v10 * fi * (1 - fj)
                   + v01 * (1 - fi) * fj + v11 * fi * fj)
    return out


def _coarse_warn_once() -> None:
    """粗 DEM 回退警告只打一次（同一进程内）。"""
    global _COARSE_WARNED
    if not _COARSE_WARNED:
        _COARSE_WARNED = True
        print(f"[dem] 警告: {COARSE_WARNING}（{_LAST_COARSE_PATH}）", flush=True)


# --------------------------------------------------------------------------- #
# v3.1 自定义高程源（离线自主导入的 GeoTIFF，自定义源优先）
# --------------------------------------------------------------------------- #
# 说明：用户在无网络时用 app/core/dem_import.py 导入自己的 WGS84 经纬度 GeoTIFF，
# 登记在 <RK_DEM>/custom/index.json。本模块**懒加载**这些源：第一次采样时才打开
# 文件（只读 TIFF 头，不读像素），并缓存 GeoTiffSource 实例。
# 没有 index.json 时下面的函数全部快速返回空，原有行为逐位不变。
_CUSTOM_SOURCES = None         # None=未加载; list[dict]（含 "_src"）
_CUSTOM_INDEX_KEY = None       # (path, mtime_ns, size)：索引变化时自动重载
_CUSTOM_WARNED: set[str] = set()
_GEOTIFF_CLASS = None


def _geotiff_source_class():
    """懒加载 :class:`common.geotiff.GeoTiffSource`（同包相对导入，失败再绝对导入）。"""
    global _GEOTIFF_CLASS
    if _GEOTIFF_CLASS is not None:
        return _GEOTIFF_CLASS
    try:
        from .geotiff import GeoTiffSource as cls
    except ImportError:                          # 被当作顶层模块 import 时
        from common.geotiff import GeoTiffSource as cls   # type: ignore
    _GEOTIFF_CLASS = cls
    return cls


def custom_index_path() -> Path:
    """自定义高程源登记表路径（不保证存在）。"""
    return Path(CUSTOM_INDEX)


def _custom_warn_once(msg: str) -> None:
    """自定义源相关的警告同一进程内只打一次（按消息去重）。"""
    if msg in _CUSTOM_WARNED:
        return
    _CUSTOM_WARNED.add(msg)
    print(f"[dem] 警告: {msg}", flush=True)


def _close_custom(sources=None) -> None:
    for rec in (sources if sources is not None else (_CUSTOM_SOURCES or [])):
        try:
            rec["_src"].close()
        except Exception:                        # noqa: BLE001 - 关闭失败忽略
            pass


def _reset_custom_cache() -> None:
    """清空自定义源缓存（dem_dir 变化 / 测试隔离 / 重新导入后调用）。"""
    global _CUSTOM_SOURCES, _CUSTOM_INDEX_KEY
    _close_custom()
    _CUSTOM_SOURCES = None
    _CUSTOM_INDEX_KEY = None


def custom_sources() -> list:
    """已加载的自定义高程源记录列表（首次调用时才读索引 + 打开文件）。

    Returns
    -------
    list of dict
        每条含 ``id``/``name``/``file``/``bbox``/``res_deg``/``crs``/``_src``
        （``_src`` 指向 :class:`GeoTiffSource` 实例）。打不开的源会跳过并
        warning 一次，不影响其它源。
    """
    global _CUSTOM_SOURCES, _CUSTOM_INDEX_KEY
    path = custom_index_path()
    try:
        st = path.stat()
        key = (str(path), int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        key = (str(path), 0, 0)
    if _CUSTOM_SOURCES is not None and _CUSTOM_INDEX_KEY == key:
        return _CUSTOM_SOURCES

    _close_custom(_CUSTOM_SOURCES)
    out: list[dict] = []
    if key[1]:
        try:
            # v4：容忍 BOM —— 用户用记事本/Windows PowerShell 编辑过索引时会带 UTF-8 BOM，
            # v3 用 encoding="utf-8" 读取会直接判为"索引损坏"并丢掉全部自定义高程源。
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            records = data.get("sources") or [] if isinstance(data, dict) else []
        except Exception as exc:                 # noqa: BLE001 - 索引坏了不能拖垮流水线
            _custom_warn_once(f"自定义高程索引损坏（{exc}），已忽略：{path}")
            records = []
        cls = None
        for rec in records:
            if not isinstance(rec, dict) or not rec.get("file"):
                continue
            rid = str(rec.get("id") or Path(str(rec["file"])).stem)
            try:
                if cls is None:
                    cls = _geotiff_source_class()
                src = cls(rec["file"])           # 只读 TIFF 头（约 1 ms）
            except Exception as exc:             # noqa: BLE001 - 单个源坏了跳过
                _custom_warn_once(f"自定义高程源 {rid} 打不开（{exc}），已跳过")
                continue
            bbox = rec.get("bbox") or []
            try:
                if len(bbox) >= 4:
                    bbox_t = tuple(float(v) for v in bbox[:4])
                else:
                    bbox_t = tuple(float(v) for v in src.bbox)
            except (TypeError, ValueError):
                bbox_t = tuple(float(v) for v in src.bbox)
            out.append({
                "id": rid,
                "name": str(rec.get("name") or rid),
                "file": str(rec["file"]),
                "bbox": bbox_t,
                "res_deg": tuple(float(v) for v in (rec.get("res_deg") or src.res_deg)),
                "crs": str(rec.get("crs") or getattr(src, "crs", "")),
                "_src": src,
            })
        if out:
            print(f"[dem] 自定义高程源 {len(out)} 个：" +
                  "、".join(f"{r['id']}({r['file']})" for r in out), flush=True)
    _CUSTOM_SOURCES = out
    _CUSTOM_INDEX_KEY = key
    return out


def custom_available() -> bool:
    """是否有可用的自定义高程源（不抛异常）。"""
    try:
        return bool(custom_sources())
    except Exception:                            # noqa: BLE001
        return False


def _custom_registry() -> list:
    """（内部）已加载的自定义高程源登记列表，等价于 :func:`custom_sources`。

    设计文档里叫 ``_custom_registry()``：懒加载 ``<RK_DEM>/custom/index.json``，
    用 :class:`common.geotiff.GeoTiffSource` 打开引用文件（失败跳过并 warning 一次），
    打开后的实例缓存在模块级变量里。
    """
    return custom_sources()


def custom_covering(lat, lon, margin_deg: float = 0.0):
    """哪些自定义源覆盖给定点。

    Parameters
    ----------
    lat, lon : float or array_like
        经纬度 [deg]。标量 → 返回覆盖该点的源记录列表；
        数组 → 返回 bool 数组（任一所源覆盖即为 True）。
    margin_deg : float
        覆盖判断的外扩量 [deg]（默认 0）。

    Returns
    -------
    list or ndarray
    """
    srcs = custom_sources()
    if np.isscalar(lat) and np.isscalar(lon):
        la, lo = float(lat), float(lon)
        return [r for r in srcs if r["_src"].covers(la, lo, margin_deg)]
    mask = np.zeros(np.broadcast(np.asarray(lat, float), np.asarray(lon, float)).shape,
                    dtype=bool)
    for r in srcs:
        mask |= r["_src"].covers_mask(lat, lon, margin_deg)
    return mask


def _custom_covers_bbox(lat_min: float, lat_max: float,
                        lon_min: float, lon_max: float):
    """有某个自定义源完整覆盖 bbox → 返回该源记录，否则 None。"""
    for r in custom_sources():
        src = r["_src"]
        la0, la1, lo0, lo1 = src.bbox
        if (la0 <= lat_min + 1e-9 and la1 >= lat_max - 1e-9
                and lo0 <= lon_min + 1e-9 and lo1 >= lon_max - 1e-9):
            return r
    return None


def _custom_sampler(src, method: str = "nearest"):
    """把 GeoTiffSource 包装成"接受数组、返回 float 数组"的采样器。

    缺测/nodata → ``nan``（与 :func:`_tile_sampler` 的 void 语义由上层统一处理：
    ``_profile_heights`` / ``_terrain_horizon_batch`` 会把 nan 当作 0）。
    """
    def _sampler(la, lo, _src=src, _m=method):
        return _src.sample_many(la, lo, method=_m)
    return _sampler


def custom_sampler_for_bbox(lat_min, lat_max, lon_min, lon_max, method: str = "nearest"):
    """返回能覆盖整个 bbox 的自定义源采样器 ``callable(la, lo) -> ndarray``。

    没有源完整覆盖该 bbox 时返回 ``None``（调用方回退原 SRTM/粗 DEM 逻辑）。
    多个源**联合**覆盖不算覆盖（地形遮挡必须用同一份数据，避免拼缝）。
    """
    try:
        rec = _custom_covers_bbox(float(lat_min), float(lat_max),
                                  float(lon_min), float(lon_max))
    except (TypeError, ValueError):
        return None
    if rec is None:
        return None
    return _custom_sampler(rec["_src"], method)


def _custom_sampler_and_record(lat_min, lat_max, lon_min, lon_max,
                               method: str = "nearest"):
    """:func:`custom_sampler_for_bbox` 的内部版：同时返回源记录（用于模式说明）。"""
    rec = _custom_covers_bbox(float(lat_min), float(lat_max),
                              float(lon_min), float(lon_max))
    if rec is None:
        return None, None
    return _custom_sampler(rec["_src"], method), rec


def _custom_fill_points(out: np.ndarray, lats: np.ndarray, lons: np.ndarray,
                        method: str, void_value: float):
    """用自定义源填充点采样结果（就地）。

    Returns
    -------
    filled : ndarray bool
        已经由自定义源确定（出值或按 void 处理）的点掩码；未填充的点继续走原逻辑。
    """
    filled = np.zeros(lats.shape, dtype=bool)
    if lats.size == 0:
        return filled
    for rec in custom_sources():
        src = rec["_src"]
        mask = src.covers_mask(lats, lons) & ~filled
        if not mask.any():
            continue
        try:
            vals = np.asarray(src.sample_many(lats[mask], lons[mask], method=method),
                              dtype=float)
        except Exception as exc:                 # noqa: BLE001 - 单源失败继续下一个
            _custom_warn_once(f"自定义高程源 {rec['id']} 采样失败（{exc}），已跳过")
            continue
        hit = np.isfinite(vals)
        sub = out[mask]
        sub[hit] = vals[hit]
        out[mask] = sub
        filled[mask] = hit
        if not np.isnan(void_value):
            # 覆盖但 nodata：与 SRTM void 语义一致 → 返回 void_value
            miss = ~hit
            if miss.any():
                sub = out[mask]
                sub[miss] = float(void_value)
                out[mask] = sub
                filled[mask] = True
        if hit.any():
            _set_mode("custom", f"point:{rec['id']}")
    return filled


def _custom_horizon_eligible(obs_lat: float, obs_lon: float, radius_km: float):
    """观测点半径圆 bbox 是否被某个自定义源完整覆盖 → (sampler, record) 或 (None, None)。"""
    try:
        dlat, dlon = _bbox_halfsize_deg(float(obs_lat), float(radius_km))
    except Exception:                            # noqa: BLE001
        return None, None
    return _custom_sampler_and_record(float(obs_lat) - dlat, float(obs_lat) + dlat,
                                      float(obs_lon) - dlon, float(obs_lon) + dlon)


def custom_covers_radius(obs_lat, obs_lon, radius_km) -> dict:
    """观测点 ``radius_km`` 圆的外接矩形是否被某个自定义源**完整**覆盖。

    供 ``app/core/dem_manager.py`` 判断"地形遮挡角会不会走自定义源"。

    Returns
    -------
    dict
        ``{"covered": bool, "source": 源 id（未覆盖为空串）}``。
    """
    try:
        smp, rec = _custom_horizon_eligible(float(obs_lat), float(obs_lon),
                                            float(radius_km))
    except (TypeError, ValueError):
        return {"covered": False, "source": ""}
    return {"covered": smp is not None,
            "source": str(rec["id"]) if rec is not None else ""}


def _set_mode(mode: str, note: str = "") -> None:
    """记录最近一次地形查询的数据模式（供上层 runner 写入 meta）。

    同时把模式落盘到 ``<RK_DEM>/dem_mode.json``：engine 脚本（plot_sky 等）通常由
    runner 以子进程/runpy 方式调用，父进程读这个文件即可知道**本次运行实际用的**
    数据模式，而无需改 plot_sky 的 sky_summary 结构。
    """
    global LAST_MODE, LAST_MODE_NOTE, _LAST_WRITTEN_MODE
    LAST_MODE = str(mode)
    LAST_MODE_NOTE = str(note)
    if LAST_MODE == _LAST_WRITTEN_MODE:
        return
    _LAST_WRITTEN_MODE = LAST_MODE
    try:
        p = DATA_DEM / "dem_mode.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.part")
        payload = {
            "mode": LAST_MODE,
            "note": LAST_MODE_NOTE,
            "coarse_dem": _LAST_COARSE_PATH if _load_coarse() else "",
            "coarse_available": bool(_load_coarse()),
            "warning": COARSE_WARNING if LAST_MODE == "coarse" else "",
            "dem_dir": str(DATA_DEM),
            "updated_utc": _utc_now(),
        }
        if LAST_MODE == "custom":
            # 只有已经在用自定义源时才附带清单（避免额外文件 IO）
            payload["custom_sources"] = [r["id"] for r in (_CUSTOM_SOURCES or [])]
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
    except Exception:                    # noqa: BLE001 - 写不了模式文件不影响计算
        pass


def last_mode() -> dict:
    """当前 DEM 数据模式摘要（runner / GUI 可读）。

    Returns
    -------
    dict
        ``{"mode": "srtm|coarse|none|custom", "note": ..., "coarse_dem": 路径,
        "coarse_available": bool, "warning": 中文警告(仅粗模式),
        "custom_available": bool}``；mode 为 ``"custom"`` 时另有
        ``"custom_sources": [id, ...]``。
    """
    g = _load_coarse()
    info = {
        "mode": LAST_MODE,
        "note": LAST_MODE_NOTE,
        "coarse_dem": _LAST_COARSE_PATH if g else "",
        "coarse_available": bool(g),
        "warning": COARSE_WARNING if LAST_MODE == "coarse" else "",
        "custom_available": bool(_CUSTOM_SOURCES),
    }
    if LAST_MODE == "custom":
        info["custom_sources"] = [r["id"] for r in (_CUSTOM_SOURCES or [])]
    return info


def tiles_available_for(obs_lat: float, obs_lon: float, radius_km: float) -> bool:
    """观测点 ``radius_km`` 覆盖包围盒内的 1″ 瓦片是否**全部**就绪（hgt 完整）。

    粗 DEM 回退的触发判据：不齐 → 整段遮挡角改用粗网格（避免"一半瓦片一半 void"
    得到自相矛盾的结果）。

    v3.1：若某个**自定义高程源**完整覆盖该包围盒，直接返回 True
    （等价于"瓦片齐备"，让 SRTM/粗 DEM 回退逻辑让位给自定义源）。
    """
    try:
        dlat, dlon = _bbox_halfsize_deg(float(obs_lat), float(radius_km))
    except Exception:                    # noqa: BLE001
        return False
    la0, la1 = float(obs_lat) - dlat, float(obs_lat) + dlat
    lo0, lo1 = float(obs_lon) - dlon, float(obs_lon) + dlon
    try:
        if _custom_covers_bbox(la0, la1, lo0, lo1) is not None:
            return True
    except Exception:                    # noqa: BLE001 - 自定义源异常不影响原逻辑
        pass
    for i in range(int(math.floor(la0)), int(math.floor(la1)) + 1):
        for j in range(int(math.floor(lo0)), int(math.floor(lo1)) + 1):
            name = tile_name(float(i) + 0.5, float(j) + 0.5)
            if name in _TILE_CACHE:
                continue
            path = DIR_HGT / f"{name}.hgt"
            try:
                if not path.exists() or path.stat().st_size != HGT_BYTES:
                    return False
            except OSError:
                return False
    return True


# --------------------------------------------------------------------------- #
# 瓦片定位
# --------------------------------------------------------------------------- #
def tile_name(lat: float, lon: float) -> str:
    """经纬度所在 HGT 瓦片名（SW 角点取整）：(19.6008, 110.9358) → 'N19E110'。"""
    ilat = int(math.floor(float(lat)))
    ilon = int(math.floor(float(lon)))
    ns = "N" if ilat >= 0 else "S"
    ew = "E" if ilon >= 0 else "W"
    return f"{ns}{abs(ilat):02d}{ew}{abs(ilon):03d}"


def tile_bounds(lat: float, lon: float):
    """经纬度所在瓦片的 SW 角点 → ``(lat_sw, lon_sw)``（整数度）。"""
    return int(math.floor(float(lat))), int(math.floor(float(lon)))


def tile_path(lat: float, lon: float, compressed: bool = False) -> Path:
    """经纬度所在瓦片的本地路径。

    Parameters
    ----------
    lat, lon : float
        任意落在该瓦片内的经纬度 [deg]。
    compressed : bool
        True 时返回 ``data/dem/gz/<TILE>.hgt.gz``，否则 ``data/dem/hgt/<TILE>.hgt``。

    Returns
    -------
    pathlib.Path
        文件路径（不保证存在，用 :func:`available_tiles` 检查）。
    """
    name = tile_name(lat, lon)
    return (DIR_GZ / f"{name}.hgt.gz") if compressed else (DIR_HGT / f"{name}.hgt")


def available_tiles():
    """本地已解压的瓦片名列表（升序）。"""
    if not DIR_HGT.exists():
        return []
    return sorted(p.stem for p in DIR_HGT.glob("*.hgt") if p.stat().st_size == HGT_BYTES)


def tile_manifest():
    """读取 ``data/dem/srtm_tiles.json``（下载清单）；不存在返回 {}。"""
    if not MANIFEST.exists():
        return {}
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 原始瓦片读取
# --------------------------------------------------------------------------- #
def read_tile(lat: float, lon: float):
    """读取（内存映射）经纬度所在 SRTM 瓦片 → shape (3601, 3601) 的 int16 数组。

    Parameters
    ----------
    lat, lon : float
        瓦片内任意点的经纬度 [deg]。

    Returns
    -------
    numpy.ndarray
        ``dtype=int16``（大端在读取时已按 ``>i2`` 映射，数值即 int16），
        行序**自北向南**：``tile[0, 0]`` 是瓦片西北角 (lat_sw+1, lon_sw)，
        ``tile[3600, 3600]`` 是东南角 (lat_sw, lon_sw+1)。
        空值为 -32768。

    Raises
    ------
    FileNotFoundError
        本地缺瓦片（先跑 ``_tools/fetch_dem.py``）。

    Notes
    -----
    用 ``np.memmap`` 只映射不读入内存，重复调用有缓存。
    """
    name = tile_name(lat, lon)
    if name in _TILE_CACHE:
        return _TILE_CACHE[name]
    path = DIR_HGT / f"{name}.hgt"
    if not path.exists() or path.stat().st_size != HGT_BYTES:
        raise FileNotFoundError(
            f"缺 SRTM 瓦片 {name} ({path})；请先运行: python _tools/fetch_dem.py")
    arr = np.memmap(path, dtype=">i2", mode="r", shape=(HGT_N, HGT_N))
    _TILE_CACHE[name] = arr
    return arr


def _tile_nearest(tile, lat: float, lon: float) -> np.ndarray:
    """在单个瓦片内按最近邻取样本（内部函数，向量化）。

    Parameters
    ----------
    tile : ndarray (3601,3601)
    lat, lon : float or ndarray
        经纬度 [deg]，需保证落在该瓦片内（[lat_sw, lat_sw+1] × [lon_sw, lon_sw+1]）。

    Returns
    -------
    ndarray
        样本高程 [m]（可能为 -32768）。
    """
    lat_sw = math.floor(float(lat) if np.isscalar(lat) else float(np.min(lat)))
    lon_sw = math.floor(float(lon) if np.isscalar(lon) else float(np.min(lon)))
    # 行：北边界对应 row 0，故 row = (lat_sw+1 - lat) * 3600
    row = np.rint((lat_sw + 1.0 - np.asarray(lat, dtype=float)) * ARCSEC_PER_DEG)
    col = np.rint((np.asarray(lon, dtype=float) - lon_sw) * ARCSEC_PER_DEG)
    row = np.clip(row, 0, HGT_N - 1).astype(np.intp)
    col = np.clip(col, 0, HGT_N - 1).astype(np.intp)
    return np.asarray(tile[row, col])


def sample(lat, lon, method: str = "nearest", missing: str = "nan",
           void_value: float = np.nan):
    """点采样 DEM 高程 [m]（自定义源优先，其次 SRTMGL1 1″，最后粗 DEM）。

    Parameters
    ----------
    lat, lon : float or array_like
        经纬度 [deg]（同一形状或可广播）。
    method : {'nearest', 'bilinear'}
        最近邻（默认，单点查询用）或双线性插值（连续地形用；SRTM 仅对单个瓦片内部
        有效，跨瓦片边界时按最近邻处理；自定义 GeoTIFF 内部全程双线性）。
    missing : {'nan', 'raise'}
        本地缺该瓦片时的行为：``'nan'``（默认）返回 nan，``'raise'`` 抛 FileNotFoundError。
        需要"静默忽略"的地方用 nan，需要**保证结果可信**的地方（如遮挡计算）用 raise。
    void_value : float
        SRTM void(-32768) 的返回值，默认 nan；:func:`dem_alt` 传 0.0。
        自定义源 nodata 采用同一语义：``void_value`` 为 nan 时视为缺测继续回退，
        否则返回 ``void_value``。

    Returns
    -------
    ndarray or float
        高程 [m]；void → ``void_value``；缺瓦片 → nan。
        **负值 = 海上水深**（skadi 瓦片用海底地形填充海面）。

    Notes
    -----
    v3.1：先按 ``<RK_DEM>/custom/index.json`` 里的自定义高程源（用户导入的 WGS84
    GeoTIFF）采样；已覆盖的点直接出值，未覆盖的点继续走原有 SRTM/粗 DEM 逻辑。
    **没有自定义源时行为与 v3.0 完全一致。**

    Examples
    --------
    >>> round(float(sample(19.60081, 110.93575)), 1)
    11.0
    """
    lats = np.asarray(lat, dtype=float)
    lons = np.asarray(lon, dtype=float)
    lats, lons = np.broadcast_arrays(lats, lons)
    out = np.full(lats.shape, np.nan, dtype=float)
    if lats.size == 0:
        return out

    # v3.1：自定义高程源优先（没有源时 filled=None，后面逐位走原逻辑）
    filled = None
    try:
        if custom_available():
            filled = _custom_fill_points(out, lats, lons, method, void_value)
            if filled.all():
                return out
    except Exception as exc:                 # noqa: BLE001 - 自定义源异常不能拖垮采样
        _custom_warn_once(f"自定义高程源采样异常（{exc}），已忽略并回退 SRTM")
        filled = None

    # 按瓦片分组（通常 1~4 个）
    tlat = np.floor(lats).astype(np.int64)
    tlon = np.floor(lons).astype(np.int64)
    for la_i, lo_i in sorted(set(zip(tlat.ravel(), tlon.ravel()))):
        mask = (tlat == la_i) & (tlon == lo_i)
        if filled is not None:
            mask = mask & ~filled
            if not mask.any():
                continue
        try:
            tile = read_tile(float(la_i) + 0.5, float(lo_i) + 0.5)
        except FileNotFoundError:
            if missing == "raise":
                raise
            # v3.0：缺瓦片 → 粗 DEM 双线性回退（点采样也要能出数，避免上层崩）
            fb = _coarse_bilinear(lats[mask], lons[mask])
            if fb is not None:
                out[mask] = np.asarray(fb, dtype=float)
                _coarse_warn_once()
                _set_mode("coarse", "sample")
            continue                     # 缺瓦片且无粗 DEM → 该部分保持 nan
        if method == "nearest":
            vals = _tile_nearest(tile, lats[mask], lons[mask])
        elif method == "bilinear":
            # 双线性：先算浮点行列
            row = (la_i + 1.0 - lats[mask]) * ARCSEC_PER_DEG
            col = (lons[mask] - lo_i) * ARCSEC_PER_DEG
            r0 = np.clip(np.floor(row).astype(np.intp), 0, HGT_N - 2)
            c0 = np.clip(np.floor(col).astype(np.intp), 0, HGT_N - 2)
            fr = np.clip(row - r0, 0.0, 1.0)
            fc = np.clip(col - c0, 0.0, 1.0)
            v = np.asarray(tile[r0, c0], dtype=float)
            v10 = np.asarray(tile[np.minimum(r0 + 1, HGT_N - 1), c0], dtype=float)
            v01 = np.asarray(tile[r0, np.minimum(c0 + 1, HGT_N - 1)], dtype=float)
            v11 = np.asarray(tile[np.minimum(r0 + 1, HGT_N - 1),
                                  np.minimum(c0 + 1, HGT_N - 1)], dtype=float)
            vals = (v * (1 - fr) * (1 - fc) + v10 * fr * (1 - fc)
                    + v01 * (1 - fr) * fc + v11 * fr * fc)
        else:
            raise ValueError(f"未知 method={method!r}，可选 nearest / bilinear")
        vals = np.asarray(vals, dtype=float)
        vals[vals == VOID_VALUE] = void_value
        out[mask] = vals
    return out


def dem_alt(lat, lon, clip_negative: bool = False, missing: str = "nan"):
    """DEM 高程 [m]（void 按 0 处理；缺瓦片按 ``missing`` 处理）。

    与 :func:`sample` 的区别：``sample`` 对 void 返回 nan，本函数返回 0.0
    （无回波区按海平面处理，适合陆地掩膜、海平面附近观测点）。
    **注意**：缺瓦片（本地没下载）与 void 不同 —— 缺瓦片返回 nan（或抛异常），
    不会把"没有数据"误当成海平面。

    Parameters
    ----------
    lat, lon : float or array_like
        经纬度 [deg]。
    clip_negative : bool
        默认 False：海上点返回**负值（水深，来自 skadi 的海底地形填充）**；
        True：负值按 0（海平面）处理。求"地形海拔/遮挡角"时用 True。
    missing : {'nan', 'raise'}
        本地缺该瓦片时的行为：'nan' 返回 nan（默认），'raise' 抛 FileNotFoundError。

    Returns
    -------
    ndarray or float
        高程 [m]；void → 0.0；负值 = 水深（除非 clip_negative）；缺瓦片 → nan。
    """
    v = np.asarray(sample(lat, lon, missing=missing, void_value=0.0), dtype=float)
    if clip_negative:
        v = np.maximum(v, 0.0)
    return v


def sample_points(lats, lons) -> np.ndarray:
    """批量点采样（等价于 :func:`sample` 的数组版，保留 void=nan）。"""
    return np.asarray(sample(lats, lons), dtype=float)


# --------------------------------------------------------------------------- #
# void 填补
# --------------------------------------------------------------------------- #
def fill_voids_in_grid(alt_int16: np.ndarray, max_iter: int = 8):
    """用 8 邻域迭代填补 int16 网格里的 SRTM void（-32768）。

    Parameters
    ----------
    alt_int16 : ndarray (ny, nx) int16
        含 void 的高程网格（通常是观测点局部缓存）。
    max_iter : int
        最多迭代轮数（每轮把 void 用一圈已知邻居的中位数/均值填一层）。

    Returns
    -------
    (filled, void_ratio) : (ndarray int16, float)
        ``filled``：void 已填补的网格（仍为 int16；无法填补的残余 void 置 0）；
        ``void_ratio``：原始 void 像素占比。

    Notes
    -----
    只用 numpy：每轮把网格向 8 个方向平移，取 4 邻域+对角已知值的平均。
    典型 SRTMGL1 瓦片 void < 0.1%，8 轮足够填满海岸/河谷的细缝。
    """
    alt = np.asarray(alt_int16)
    void = alt == VOID_VALUE
    n_void = int(void.sum())
    if n_void == 0:
        return alt.astype(np.int16), 0.0
    void_ratio = n_void / float(void.size)

    work = alt.astype(np.int32)
    work[void] = 0
    known = ~void
    for _ in range(max_iter):
        if not void.any():
            break
        acc = np.zeros(work.shape, dtype=np.float64)
        cnt = np.zeros(work.shape, dtype=np.int16)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy == 0 and dx == 0:
                    continue
                # work[dy:, dx:] 与 acc[:ny-|dy|, :nx-|dx|] 对齐
                ys_dst = slice(max(dy, 0), work.shape[0] + min(dy, 0))
                xs_dst = slice(max(dx, 0), work.shape[1] + min(dx, 0))
                ys_src = slice(max(-dy, 0), work.shape[0] + min(-dy, 0))
                xs_src = slice(max(-dx, 0), work.shape[1] + min(-dx, 0))
                acc[ys_dst, xs_dst] += work[ys_src, xs_src] * known[ys_src, xs_src]
                cnt[ys_dst, xs_dst] += known[ys_src, xs_src].astype(np.int16)
        fill = void & (cnt > 0)
        if not fill.any():
            break
        work[fill] = np.rint(acc[fill] / cnt[fill]).astype(np.int32)
        known = known | fill
        void = void & ~fill

    if void.any():
        work[void] = 0                       # 残余 void 按 0（海平面）处理
    return np.clip(work, -32767, 32767).astype(np.int16), float(void_ratio)


# --------------------------------------------------------------------------- #
# 观测点局部缓存（3″ ≈ 90 m 网格）
# --------------------------------------------------------------------------- #
def cache_path(observer_id: str) -> Path:
    """观测点缓存的 npz 路径：``data/dem/cache/<id>.npz``。"""
    return DIR_CACHE / f"{observer_id}.npz"


def _align_down(x: float, step: float) -> float:
    """向下对齐到 step 的整数倍（用于让缓存网格与 1″ 瓦片精确对齐）。"""
    return math.floor(x / step + 1e-9) * step


def _bbox_halfsize_deg(lat_deg: float, radius_km: float):
    """半径圆覆盖所需的最小纬度/经度半宽 [deg]（保守估计，保证椭球距离不越界）。

    不能用"平均半径 6371 km"折角：北纬 19° 处子午圈曲率半径 M≈6336 km，
    200 km 的南北向大地线对应 1.809° 纬度，而 200/(6371·π/180)=1.799° 会**越界**
    （实测越界 ~900 m）。这里用 WGS-84 曲率半径：
    纬度方向用子午圈半径 M（南北向变化最大），经度方向用卯酉圈半径 N·cosφ（东西向），
    再乘 1.005 安全系数。
    """
    phi = math.radians(max(min(lat_deg, 89.5), -89.5))
    w = math.sqrt(1.0 - WGS84_E2 * math.sin(phi) ** 2)
    M = WGS84_A * (1.0 - WGS84_E2) / w ** 3          # 子午圈曲率半径
    N = WGS84_A / w                                  # 卯酉圈曲率半径
    deg_per_rad = 180.0 / math.pi
    dlat = radius_km * 1000.0 / M * deg_per_rad
    dlon = radius_km * 1000.0 / (N * math.cos(phi)) * deg_per_rad
    return dlat * 1.005, dlon * 1.005


def _grid_sample_axis_aligned(lats: np.ndarray, lons: np.ndarray) -> tuple[np.ndarray, list]:
    """把轴对齐 3″ 规则网格从瓦片采样为 int16（含 void=-32768）。

    网格步长必须是 1/1200 度（3″）的整数倍，且网格点对齐到整数弧秒，
    这样每个点都能用整数索引直接取瓦片样本（无插值、无误差）。

    Returns
    -------
    (alt, tiles_used) : (ndarray int16 shape (ny,nx), list[str])
    """
    ny, nx = len(lats), len(lons)
    alt = np.full((ny, nx), VOID_VALUE, dtype=np.int16)
    tiles_used: list[str] = []

    # 经度方向按瓦片分块（每行同纬度 → 瓦片行固定）
    lon_tiles = np.floor(lons).astype(np.int64)
    bounds = []
    j = 0
    while j < nx:
        t = lon_tiles[j]
        j2 = j
        while j2 + 1 < nx and lon_tiles[j2 + 1] == t:
            j2 += 1
        bounds.append((j, j2 + 1, int(t)))
        j = j2 + 1

    for i, lat in enumerate(lats):
        la_tile = int(math.floor(lat))
        lat_frac = lat - la_tile
        row = int(round((1.0 - lat_frac) * ARCSEC_PER_DEG))
        row = min(max(row, 0), HGT_N - 1)
        for j0, j1, lo_tile in bounds:
            name = tile_name(lat, float(lo_tile) + 0.5)
            try:
                tile = read_tile(lat, float(lo_tile) + 0.5)
            except FileNotFoundError:
                continue
            if name not in tiles_used:
                tiles_used.append(name)
            cols = np.rint((lons[j0:j1] - lo_tile) * ARCSEC_PER_DEG).astype(np.intp)
            cols = np.clip(cols, 0, HGT_N - 1)
            alt[i, j0:j1] = tile[row, cols]
    return alt, tiles_used


def build_observer_cache(observer_id: str, lat: float, lon: float,
                         radius_km: float = 200.0, refresh: bool = False,
                         verbose: bool = True) -> dict:
    """为单个观测点构建 ``radius_km`` 半径内 3″（≈90 m）DEM 局部缓存。

    缓存覆盖观测点周围正方形（经纬度包围盒，含半径圆），网格 3″ 对齐整数弧秒，
    从本地 SRTMGL1 瓦片直接整数索引采样（无插值），再用 8 邻域填补 void。

    Parameters
    ----------
    observer_id : str
        观测点 id（缓存文件名）。
    lat, lon : float
        观测点经纬度 [deg]。
    radius_km : float
        覆盖半径 [km]（默认 200）。
    refresh : bool
        是否强制重建（默认已存在则跳过）。
    verbose : bool
        是否打印进度。

    Returns
    -------
    dict
        观测点信息（:func:`observer_dem_info` 的单条记录）。

    Notes
    -----
    缓存 npz 内容：``alt``(int16, ny×nx, 行号向北递增)、``lat0``/``lon0``(SW 角点)、
    ``dlat``/``dlon``(度)、``void_ratio``、``radius_km``、``obs_lat``/``obs_lon``、
    ``tiles_used``。体积约 30–45 MB/点（int16 + zlib），可重复运行（幂等）。
    """
    p = cache_path(observer_id)
    if p.exists() and not refresh:
        info = load_observer_cache(observer_id)
        return _cache_to_info(observer_id, info, lat, lon, radius_km)

    DIR_CACHE.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    dlat, dlon = _bbox_halfsize_deg(lat, radius_km)                   # 保守半宽 [deg]

    lat0 = _align_down(lat - dlat, CACHE_DLAT)
    lon0 = _align_down(lon - dlon, CACHE_DLAT)
    nrows = int(math.ceil((lat + dlat - lat0) / CACHE_DLAT)) + 1
    ncols = int(math.ceil((lon + dlon - lon0) / CACHE_DLAT)) + 1
    lats = lat0 + np.arange(nrows) * CACHE_DLAT
    lons = lon0 + np.arange(ncols) * CACHE_DLAT

    if verbose:
        print(f"[dem] 构建 {observer_id} 缓存: {nrows}x{ncols} 点 (3″≈90 m), "
              f"lat {lats[0]:.4f}..{lats[-1]:.4f}, lon {lons[0]:.4f}..{lons[-1]:.4f}",
              flush=True)

    raw, tiles_used = _grid_sample_axis_aligned(lats, lons)
    alt, void_ratio = fill_voids_in_grid(raw)
    del raw

    np.savez_compressed(
        p,
        alt=alt,
        lat0=np.float64(lat0), lon0=np.float64(lon0),
        dlat=np.float64(CACHE_DLAT), dlon=np.float64(CACHE_DLAT),
        void_ratio=np.float64(void_ratio),
        radius_km=np.float64(radius_km),
        obs_lat=np.float64(lat), obs_lon=np.float64(lon),
        tiles_used=np.array(tiles_used),
        created_utc=np.str_(_utc_now()),
    )
    if verbose:
        mb = p.stat().st_size / 1e6
        print(f"[dem]   → {p.name} {mb:.1f} MB, void_ratio={void_ratio:.4%}, "
              f"瓦片 {len(tiles_used)} 个, 耗时 {time.time() - t0:.1f} s", flush=True)
    _OBS_CACHE.pop(observer_id, None)
    return _cache_to_info(observer_id, load_observer_cache(observer_id), lat, lon, radius_km)


def load_observer_cache(observer_id: str) -> dict:
    """读取观测点缓存 npz → dict（内存缓存，返回的 ``alt`` 是 int16 数组）。

    Returns
    -------
    dict
        keys: ``alt``, ``lat0``, ``lon0``, ``dlat``, ``dlon``, ``void_ratio``,
        ``radius_km``, ``obs_lat``, ``obs_lon``, ``tiles_used``, ``created_utc``。
    """
    if observer_id in _OBS_CACHE:
        return _OBS_CACHE[observer_id]
    p = cache_path(observer_id)
    if not p.exists():
        raise FileNotFoundError(f"缺观测点 DEM 缓存 {p}；先运行 python -m common.dem --build")
    with np.load(p, allow_pickle=False) as z:
        info = {k: z[k] for k in z.files}
    _OBS_CACHE[observer_id] = info
    return info


def _cache_to_info(observer_id: str, info: dict, lat: float, lon: float,
                   radius_km: float) -> dict:
    """把缓存 dict 汇总成 observer_dem.json 里的记录。

    注意 ``dem_alt_m`` 用**原始 1″ 瓦片精确点采样**（不受 3″ 缓存取整影响）；
    ``cache_cell_dem_m`` 是 3″ 网格该格点的值，两者可能差几米。
    """
    alt = info["alt"]
    lat0 = float(info["lat0"])
    lon0 = float(info["lon0"])
    dlat = float(info["dlat"])
    dlon = float(info["dlon"])
    row = int(round((lat - lat0) / dlat))
    col = int(round((lon - lon0) / dlon))
    row = min(max(row, 0), alt.shape[0] - 1)
    col = min(max(col, 0), alt.shape[1] - 1)
    exact = float(np.nan_to_num(np.asarray(sample(lat, lon), dtype=float), nan=0.0))
    # v3.1：观测点若被自定义高程源覆盖，高程来源要如实标注（否则报告会误写成 SRTM）
    source_note = "SRTMGL1 1″ 原始瓦片最近邻（EGM96 正高，当作椭球高使用）"
    try:
        if custom_covering(lat, lon):
            source_note = "自定义高程源（用户导入的 WGS84 GeoTIFF）最近邻"
    except Exception:                        # noqa: BLE001 - 标注失败不影响数值
        pass
    return {
        "id": observer_id,
        "lat": float(lat),
        "lon": float(lon),
        "radius_km": float(radius_km),
        "dem_alt_m": round(exact, 1),
        "dem_alt_source": source_note,
        "cache_cell_dem_m": int(alt[row, col]),
        "cache_min_m": int(alt.min()),
        "cache_max_m": int(alt.max()),
        "void_ratio": float(info["void_ratio"]),
        "tiles_used": [str(s) for s in info["tiles_used"]],
        "cache_file": f"data/dem/cache/{observer_id}.npz",
        "cache_grid": [int(alt.shape[0]), int(alt.shape[1])],
        "cache_step_arcsec": CACHE_STEP_ARCSEC,
        "cache_bbox": [round(lat0, 6), round(lon0, 6),
                       round(lat0 + dlat * (alt.shape[0] - 1), 6),
                       round(lon0 + dlon * (alt.shape[1] - 1), 6)],
        "bathymetry_note": "缓存网格中负值为海底水深（skadi 海上填充），遮挡计算按海平面裁剪",
    }


def sample_from_cache(info: dict, lats, lons, outside_value=np.nan) -> np.ndarray:
    """在观测点缓存网格上最近邻采样（向量化）。

    Parameters
    ----------
    info : dict
        :func:`load_observer_cache` 的返回值。
    lats, lons : array_like
        查询经纬度 [deg]。
    outside_value : float
        落在缓存覆盖范围之外的点返回的值，默认 ``np.nan``（提醒调用方回退到瓦片）；
        设 0.0 则按海平面处理。

    Returns
    -------
    ndarray float64
        高程 [m]（网格已填 void）。
    """
    alt = info["alt"]
    lat0 = float(info["lat0"])
    lon0 = float(info["lon0"])
    dlat = float(info["dlat"])
    dlon = float(info["dlon"])
    ny, nx = alt.shape
    la = np.asarray(lats, dtype=float)
    lo = np.asarray(lons, dtype=float)
    la, lo = np.broadcast_arrays(la, lo)
    r = np.rint((la - lat0) / dlat).astype(np.intp)
    c = np.rint((lo - lon0) / dlon).astype(np.intp)
    inside = (r >= 0) & (r < ny) & (c >= 0) & (c < nx)
    out = np.full(la.shape, float(outside_value), dtype=float)
    out[inside] = alt[r[inside], c[inside]].astype(float)
    return out


# --------------------------------------------------------------------------- #
# 地形剖面 / 遮挡角
# --------------------------------------------------------------------------- #
def _direct_vec(lat0_deg, lon0_deg, az_deg, dist_km):
    """WGS-84 椭球测地线正解（Vincenty 1975，numpy 向量化）。

    与 :func:`common.geo.geodesic_direct` 用同一算法，但把"距离"作为数组一次求出
    全部点（720 方位 × 2223 点 ≈ 160 万点，向量化后总耗时 ~1 s）。实测与标量
    Vincenty 正解位置差 < 1e-5 m。

    Parameters
    ----------
    lat0_deg, lon0_deg : float
        起点 [deg]。
    az_deg : float
        初始方位角 [deg]（正北 0 顺时针）。
    dist_km : array_like
        距离数组 [km]。

    Returns
    -------
    (lat2_deg, lon2_deg) : tuple of ndarray
        终点经纬度 [deg]。

    Notes
    -----
    **不要**用等距球面正解代替：在北纬 19°、200 km 处，球面正解的位置误差约 0.9 km
    （经线方向距离标度差 0.5%），且方位角 90°/270° 时用地平椭球近似会漏掉大地线
    向赤道弯曲的约 1.1 km 侧偏。Vincenty 正解没有这些近似误差。
    """
    a, f, b = WGS84_A, WGS84_F, WGS84_B
    lat1 = math.radians(lat0_deg)
    az = math.radians(az_deg)
    s = np.asarray(dist_km, dtype=float) * 1000.0
    sin_a1, cos_a1 = math.sin(az), math.cos(az)
    tanU1 = (1.0 - f) * math.tan(lat1)
    cosU1 = 1.0 / math.sqrt(1.0 + tanU1 ** 2)
    sinU1 = tanU1 * cosU1
    sigma1 = math.atan2(tanU1, cos_a1)
    sin_alpha = cosU1 * sin_a1
    cos2_alpha = 1.0 - sin_alpha ** 2
    u2 = cos2_alpha * (a * a - b * b) / (b * b)
    A = 1.0 + u2 / 16384.0 * (4096.0 + u2 * (-768.0 + u2 * (320.0 - 175.0 * u2)))
    B = u2 / 1024.0 * (256.0 + u2 * (-128.0 + u2 * (74.0 - 47.0 * u2)))

    sigma = s / (b * A)
    for _ in range(10):                     # 200 km 内 4~5 次即收敛，固定 10 次足够
        cos2_sigma_m = np.cos(2.0 * sigma1 + sigma)
        sin_sigma, cos_sigma = np.sin(sigma), np.cos(sigma)
        d_sigma = B * sin_sigma * (cos2_sigma_m + B / 4.0 * (
            cos_sigma * (-1.0 + 2.0 * cos2_sigma_m ** 2)
            - B / 6.0 * cos2_sigma_m * (-3.0 + 4.0 * sin_sigma ** 2)
            * (-3.0 + 4.0 * cos2_sigma_m ** 2)))
        sigma = s / (b * A) + d_sigma

    cos2_sigma_m = np.cos(2.0 * sigma1 + sigma)
    sin_sigma, cos_sigma = np.sin(sigma), np.cos(sigma)
    tmp = sinU1 * sin_sigma - cosU1 * cos_sigma * cos_a1
    lat2 = np.arctan2(sinU1 * cos_sigma + cosU1 * sin_sigma * cos_a1,
                      (1.0 - f) * np.sqrt(sin_alpha ** 2 + tmp ** 2))
    lam = np.arctan2(sin_sigma * sin_a1,
                     cosU1 * cos_sigma - sinU1 * sin_sigma * cos_a1)
    C = f / 16.0 * cos2_alpha * (4.0 + f * (4.0 - 3.0 * cos2_alpha))
    L = lam - (1.0 - C) * f * sin_alpha * (
        sigma + C * sin_sigma * (cos2_sigma_m + C * cos_sigma *
                                 (-1.0 + 2.0 * cos2_sigma_m ** 2)))
    lon2 = lon0_deg + np.degrees(L)
    lon2 = (lon2 + 180.0) % 360.0 - 180.0
    return np.degrees(lat2), lon2


def _direct_vec_many(lat0_deg, lon0_deg, az_deg, dist_km, chunk=120):
    """WGS-84 测地线正解（**多方位角批量**，v2 新增）。

    与 :func:`_direct_vec` 逐点同算法（Vincenty 1975、固定 10 次迭代），只是把
    方位角放到行方向一次性广播，避免 720 次 Python 级调用与 720 次小数组运算。

    Parameters
    ----------
    lat0_deg, lon0_deg : float
        起点 [deg]。
    az_deg : array_like (A,)
        方位角 [deg]。
    dist_km : array_like (M,)
        距离 [km]。
    chunk : int
        分块行数（控制峰值内存；16 行 × 2223 点 ≈ 285 kB/数组，实测最快）。

    Returns
    -------
    (lat2_deg, lon2_deg) : tuple of ndarray (A, M)
    """
    a, f, b = WGS84_A, WGS84_F, WGS84_B
    az = np.asarray(az_deg, dtype=float).ravel()
    s = np.asarray(dist_km, dtype=float).ravel() * 1000.0
    lat1 = math.radians(lat0_deg)
    tanU1 = (1.0 - f) * math.tan(lat1)
    cosU1 = 1.0 / math.sqrt(1.0 + tanU1 ** 2)
    sinU1 = tanU1 * cosU1
    out_lat = np.empty((az.size, s.size), dtype=float)
    out_lon = np.empty((az.size, s.size), dtype=float)
    for k0 in range(0, az.size, chunk):
        k1 = min(k0 + chunk, az.size)
        azr = np.radians(az[k0:k1])[:, None]
        sin_a1, cos_a1 = np.sin(azr), np.cos(azr)
        sigma1 = np.arctan2(tanU1, cos_a1)
        sin_alpha = cosU1 * sin_a1
        cos2_alpha = 1.0 - sin_alpha ** 2
        u2 = cos2_alpha * (a * a - b * b) / (b * b)
        A = 1.0 + u2 / 16384.0 * (4096.0 + u2 * (-768.0 + u2 * (320.0 - 175.0 * u2)))
        B = u2 / 1024.0 * (256.0 + u2 * (-128.0 + u2 * (74.0 - 47.0 * u2)))
        sigma = s[None, :] / (b * A)
        for _ in range(10):
            cos2_sigma_m = np.cos(2.0 * sigma1 + sigma)
            sin_sigma, cos_sigma = np.sin(sigma), np.cos(sigma)
            d_sigma = B * sin_sigma * (cos2_sigma_m + B / 4.0 * (
                cos_sigma * (-1.0 + 2.0 * cos2_sigma_m ** 2)
                - B / 6.0 * cos2_sigma_m * (-3.0 + 4.0 * sin_sigma ** 2)
                * (-3.0 + 4.0 * cos2_sigma_m ** 2)))
            sigma = s[None, :] / (b * A) + d_sigma
        cos2_sigma_m = np.cos(2.0 * sigma1 + sigma)
        sin_sigma, cos_sigma = np.sin(sigma), np.cos(sigma)
        tmp = sinU1 * sin_sigma - cosU1 * cos_sigma * cos_a1
        lat2 = np.arctan2(sinU1 * cos_sigma + cosU1 * sin_sigma * cos_a1,
                          (1.0 - f) * np.sqrt(sin_alpha ** 2 + tmp ** 2))
        lam = np.arctan2(sin_sigma * sin_a1,
                         cosU1 * cos_sigma - sinU1 * sin_sigma * cos_a1)
        C = f / 16.0 * cos2_alpha * (4.0 + f * (4.0 - 3.0 * cos2_alpha))
        L = lam - (1.0 - C) * f * sin_alpha * (
            sigma + C * sin_sigma * (cos2_sigma_m + C * cos_sigma *
                                     (-1.0 + 2.0 * cos2_sigma_m ** 2)))
        lon2 = lon0_deg + np.degrees(L)
        lon2 = (lon2 + 180.0) % 360.0 - 180.0
        out_lat[k0:k1] = np.degrees(lat2)
        out_lon[k0:k1] = lon2
    return out_lat, out_lon


def _elevation_angles(obs_lat, obs_lon, obs_h_m, tgt_lat, tgt_lon, tgt_h_m):
    """观测点 → 目标点的仰角 [deg]（ECEF 精确几何，含地球曲率与目标高度）。

    Parameters
    ----------
    obs_lat, obs_lon, obs_h_m : float
        观测点经纬度 [deg] / 椭球高 [m]。
    tgt_lat, tgt_lon, tgt_h_m : array_like
        目标点经纬度 [deg] / 高程 [m]。

    Returns
    -------
    ndarray
        仰角 [deg]（地平线以上为正）。
    """
    obs = np.asarray(geodetic_to_ecef(obs_lat, obs_lon, obs_h_m), dtype=float)
    tgt = np.asarray(geodetic_to_ecef(tgt_lat, tgt_lon, tgt_h_m), dtype=float)
    enu = ecef_to_enu(tgt - obs, obs_lat, obs_lon)
    e, n, u = enu[..., 0], enu[..., 1], enu[..., 2]
    return np.degrees(np.arctan2(u, np.hypot(e, n)))


def _tile_sampler(la, lo):
    """1″ 原始瓦片采样（缺瓦片直接抛错，避免静默出错）。"""
    return sample(la, lo, missing="raise", void_value=0.0)


def _make_samplers(obs_lat, obs_lon, radius_km, observer_id=None, info=None):
    """构造采样器：返回 (coarse_sampler, fine_sampler_or_None, source)。

    * **v3.1 自定义高程源优先**：某个自定义源完整覆盖观测点 ``radius_km`` 包围盒时
      → ``coarse`` = 该 GeoTIFF 采样器，``fine`` = None，``source`` = ``'custom'``，
      模式记为 ``"custom"``（优先于下面的缓存/瓦片/粗 DEM）；
    * 有覆盖足够的观测点缓存 → ``coarse`` = 3″ 缓存采样，``fine`` = 1″ 瓦片采样
      （调用方对**近场**用 fine 修正，见 ``near_field_km``）；
    * 有缓存但 1″ 瓦片不齐 → 只用 3″ 缓存（``fine`` = None，不做近场修正）；
    * 无缓存、1″ 瓦片齐备 → ``coarse`` = 1″ 瓦片采样，``fine`` = None；
    * 无缓存、1″ 瓦片不齐（v3.0 离线回退）→ ``coarse`` = **0.02° 粗网格双线性采样**，
      ``fine`` = None，``source`` = ``'coarse'``；粗 DEM 也没有时保持旧行为
      （返回 1″ 瓦片采样器，调用方采样时会抛 FileNotFoundError）。
    """
    # v3.1：自定义高程源（用户离线导入的 GeoTIFF）优先于缓存/瓦片/粗 DEM
    try:
        if custom_available():
            smp, rec = _custom_horizon_eligible(obs_lat, obs_lon, radius_km)
            if smp is not None:
                _set_mode("custom", f"geotiff:{rec['id']}")
                return smp, None, "custom"
    except Exception:                    # noqa: BLE001 - 自定义源异常回退原逻辑
        pass

    if info is None and observer_id is not None:
        try:
            info = load_observer_cache(observer_id)
        except FileNotFoundError:
            info = None
    if info is not None:
        lat_lo = float(info["lat0"])
        lon_lo = float(info["lon0"])
        dlat = float(info["dlat"])
        dlon = float(info["dlon"])
        lat_hi = lat_lo + dlat * (info["alt"].shape[0] - 1)
        lon_hi = lon_lo + dlon * (info["alt"].shape[1] - 1)
        need_lat, need_lon = _bbox_halfsize_deg(obs_lat, radius_km)
        covered = (lat_lo <= obs_lat - need_lat + 1e-9
                   and lat_hi >= obs_lat + need_lat - 1e-9
                   and lon_lo <= obs_lon - need_lon + 1e-9
                   and lon_hi >= obs_lon + need_lon - 1e-9)
        if covered:
            if tiles_available_for(obs_lat, obs_lon, radius_km):
                _set_mode("srtm", "cache+1")
                return (lambda la, lo: sample_from_cache(info, la, lo),
                        _tile_sampler, "cache")
            # 3″ 缓存存在但没有 1″ 瓦片：远场用缓存，近场不再做 1″ 修正
            _set_mode("srtm", "cache")
            return (lambda la, lo: sample_from_cache(info, la, lo), None, "cache")

    # v3.0：无缓存覆盖时先检查 1″ 瓦片覆盖；不齐则回退 0.02° 粗 DEM
    if not tiles_available_for(obs_lat, obs_lon, radius_km) \
            and coarse_dem_available():
        _coarse_warn_once()
        _set_mode("coarse", "horizon")
        return (lambda la, lo: _coarse_bilinear(la, lo), None, "coarse")

    if tiles_available_for(obs_lat, obs_lon, radius_km):
        _set_mode("srtm", "tiles")
    else:
        _set_mode("none", "missing_tiles")
    return _tile_sampler, None, "tiles"


def _profile_heights(coarse, fine, dist_km, tgt_lat, tgt_lon, near_field_km):
    """剖面高程：近场（< near_field_km）用 1″ 瓦片精采样，远场用 coarse（缓存）。

    缓存覆盖不到的点（nan）自动回退 1″ 瓦片；最后把仍无数据的点按海平面 0 处理。
    """
    dem_m = np.asarray(coarse(tgt_lat, tgt_lon), dtype=float)
    if fine is not None and near_field_km and near_field_km > 0:
        k = int(np.searchsorted(dist_km, near_field_km, side="right"))
        if k > 0:
            dem_m[:k] = fine(tgt_lat[:k], tgt_lon[:k])
    missing = np.isnan(dem_m)
    if missing.any() and fine is not None:
        dem_m[missing] = fine(tgt_lat[missing], tgt_lon[missing])
    return np.nan_to_num(dem_m, nan=0.0)


def terrain_profile(obs_lat, obs_lon, obs_alt_m, az_deg, max_range_km=200.0,
                    step_m=90.0, observer_id=None, info=None,
                    sea_level_clip: bool = True, near_field_km: float = 10.0):
    """单方位角地形剖面：沿方位角按步长取 DEM 高度并算相对观测点的仰角。

    Parameters
    ----------
    obs_lat, obs_lon : float
        观测点经纬度 [deg]。
    obs_alt_m : float
        观测点**椭球高** [m]（通常取 DEM 海拔；地形遮挡只看相对高度）。
    az_deg : float
        方位角 [deg]（正北 0，顺时针）。
    max_range_km : float
        最远距离 [km]（默认 200，对应 config.dem.horizon_radius_km）。
    step_m : float
        沿方位采样步长 [m]（默认 90 m ≈ 3″）。
    observer_id : str, optional
        若给出且存在 ``data/dem/cache/<id>.npz``，用缓存加速。
    info : dict, optional
        直接传入缓存 dict（优先于 observer_id）。
    sea_level_clip : bool
        把负高程（海上水深）按 0（海面）处理，默认 True。若为 False，海上剖面会显示
        海底负高程，导致"看穿海面"的负仰角，仅用于调试/展示水深。
    near_field_km : float
        使用观测点 3″ 缓存时，``dist < near_field_km`` 的**近场**点改用 1″ 原始瓦片
        精采样（近场 1 m 高度差即造成 >0.5° 仰角误差，必须用 1″ 数据），默认 10 km（覆盖主要的近场遮挡来源）；
        0 表示不修正（纯缓存）。

    Returns
    -------
    dict
        ``dist_km`` (M,), ``dem_m`` (M,), ``el_deg`` (M,), ``az_deg``,
        ``source`` ∈ {'cache','tiles'}。

    Notes
    -----
    仰角由 ECEF 精确几何给出；等价于 ``atan((h-d²/2R)/d)``，不做小角近似。
    未计大气折射。平地海面的最大仰角（视线最接近水平处）≈ -0.1°（11 m 观测高度，
    d≈sqrt(2Rh)=11.8 km），即"地平线下沉"。
    """
    coarse, fine, source = _make_samplers(obs_lat, obs_lon, max_range_km,
                                          observer_id=observer_id, info=info)
    step_km = step_m / 1000.0
    dist_km = np.arange(step_km, max_range_km + step_km * 0.5, step_km)
    tgt_lat, tgt_lon = _direct_vec(obs_lat, obs_lon, az_deg, dist_km)
    dem_m = _profile_heights(coarse, fine, dist_km, tgt_lat, tgt_lon, near_field_km)
    if sea_level_clip:
        dem_m = np.maximum(dem_m, 0.0)
    el = _elevation_angles(obs_lat, obs_lon, obs_alt_m, tgt_lat, tgt_lon, dem_m)
    return {"dist_km": dist_km, "dem_m": dem_m, "el_deg": el,
            "az_deg": float(az_deg), "source": source}


def _terrain_horizon_batch(obs_lat, obs_lon, obs_alt_m, az, max_range_km, step_m,
                           coarse, fine, near_field_km, sea_level_clip,
                           chunk=16, want_details=True):
    """地形遮挡角（**批量向量化**，v2 新增）：一次算多方位。

    与逐方位循环的结果逐位一致（Vincenty 固定 10 次迭代、最近邻采样、同样的
    「近场 1″ 修正 + 缺测回退」规则），但把 720 次 Python 循环 + 720 次小数组
    运算合并为「分块 × (方位, 距离)」二维运算，实测快 4–8×。
    """
    step_km = step_m / 1000.0
    dist_km = np.arange(step_km, max_range_km + step_km * 0.5, step_km)
    A = az.size
    horizon = np.empty(A, dtype=np.float64)
    rng_max = np.empty(A, dtype=np.float64) if want_details else None
    dem_max = np.empty(A, dtype=np.float64) if want_details else None
    kcut = int(np.searchsorted(dist_km, near_field_km, side="right")) if (
        fine is not None and near_field_km and near_field_km > 0) else 0
    for k0 in range(0, A, chunk):
        k1 = min(k0 + chunk, A)
        lat2, lon2 = _direct_vec_many(obs_lat, obs_lon, az[k0:k1], dist_km)
        dem = np.asarray(coarse(lat2, lon2), dtype=float)
        if kcut > 0:
            dem[:, :kcut] = fine(lat2[:, :kcut], lon2[:, :kcut])
        miss = np.isnan(dem)
        if miss.any() and fine is not None:
            dem[miss] = fine(lat2[miss], lon2[miss])
        dem = np.nan_to_num(dem, nan=0.0)
        if sea_level_clip:
            dem = np.maximum(dem, 0.0)
        el = _elevation_angles(obs_lat, obs_lon, obs_alt_m, lat2, lon2, dem)
        k = np.argmax(el, axis=1)
        rows = np.arange(k1 - k0)
        horizon[k0:k1] = el[rows, k]
        if want_details:
            rng_max[k0:k1] = dist_km[k]
            dem_max[k0:k1] = dem[rows, k]
    return horizon, rng_max, dem_max


def terrain_horizon_deg(obs_lat, obs_lon, obs_alt_m=None, az_list_deg=None,
                        max_range_km: float = 200.0, step_m: float = 90.0,
                        observer_id: str = None, info: dict = None,
                        sea_level_clip: bool = True, near_field_km: float = 10.0,
                        return_details: bool = False,
                        out_dtype=np.float32, batch: bool = True):
    """**地形遮挡角**：每个方位角上地形/地平的最高仰角 [deg]。

    对每个方位角，沿方位角以 ``step_m`` 步长取 DEM 高程直到 ``max_range_km``，
    计算每个地形点相对观测点的仰角（ECEF 精确几何，自动含地球曲率下沉与目标高度），
    取最大值作为该方位的**遮挡角**：

    * 遮挡角 > 0：近处有山/高地挡住视线，火箭需爬升到该仰角以上才可见；
    * 遮挡角 ≈ 0 或略负：海平面/平原，只有几何地平线（d=200 km 处约 -0.90°；
      最近点 90 m 处约 -0.00004°，故平地返回值 ≈ 0）。

    Parameters
    ----------
    obs_lat, obs_lon : float
        观测点经纬度 [deg]。
    obs_alt_m : float, optional
        观测点椭球高 [m]；``None`` 时自动用 ``dem_alt(obs_lat, obs_lon)``。
    az_list_deg : array_like, optional
        方位角列表 [deg]（正北 0 顺时针）；默认 ``np.arange(0, 360, 0.5)``（720 个）。
    max_range_km : float
        最远距离 [km]，默认 200。
    step_m : float
        沿方位步长 [m]，默认 90（≈3″，与 SRTM 1″ 的 3 倍对齐）。
    observer_id : str, optional
        观测点 id；存在 ``data/dem/cache/<id>.npz`` 时用缓存加速（快 ~20×）。
    info : dict, optional
        直接传缓存 dict。
    sea_level_clip : bool
        负高程（AWS skadi 瓦片在海上填的是**水深**）按 0（海面）处理，默认 True。
        必须保持 True 才能得到正确的"地形遮挡角"；False 仅用于查看水深对仰角的影响。
    near_field_km : float
        用观测点 3″ 缓存时，``dist < near_field_km`` 的**近场**点改用 1″ 瓦片精采样
        （近场对高度误差极敏感：90 m 处 1 m 高度差 = 0.64° 仰角差），默认 10 km。
    return_details : bool
        为 True 时额外返回每个方位最大仰角出现的位置（距离/地形高度）。
    out_dtype : numpy dtype
        输出 dtype（默认 float32，省内存）。

    Returns
    -------
    ndarray or (ndarray, dict)
        遮挡角 [deg]，形状 ``(len(az_list_deg),)``。
        ``return_details=True`` 时返回 ``(horizon_deg, details)``，
        details 含 ``az_deg``, ``range_km``（最大仰角处的距离）, ``dem_m``,
        ``el_deg``（=horizon）, ``source``。

    Notes
    -----
    * 地形遮挡对 DEM 垂直基准不敏感（只用相对高度），但**未计大气折射**；
    * void 像素按海平面 0 m 处理，不会造成虚假遮挡；海上负高程按 0（海面）处理
      （``sea_level_clip=True``），否则会"看穿海面"；
    * 判据建议：火箭高度角 > 遮挡角即地形可见（sky 模块的 ``terrain_block_deg``）；
    * 计算量：720 方位 × 2223 点 ≈ 160 万次采样，用缓存时约 1–3 s/观测点。

    Examples
    --------
    >>> hz = terrain_horizon_deg(18.2528, 109.5119, observer_id="sanya")
    >>> hz.shape
    (720,)
    """
    if az_list_deg is None:
        az_list_deg = np.arange(0.0, 360.0, 0.5)
    az = np.asarray(az_list_deg, dtype=float).ravel()
    if obs_alt_m is None:
        obs_alt_m = float(dem_alt(obs_lat, obs_lon))

    coarse, fine, source = _make_samplers(obs_lat, obs_lon, max_range_km,
                                          observer_id=observer_id, info=info)
    step_km = step_m / 1000.0
    dist_km = np.arange(step_km, max_range_km + step_km * 0.5, step_km)

    horizon = np.empty(az.size, dtype=np.float64)
    rng_at_max = np.empty(az.size, dtype=np.float64)
    dem_at_max = np.empty(az.size, dtype=np.float64)

    if batch and az.size > 4:
        horizon, rng_at_max, dem_at_max = _terrain_horizon_batch(
            obs_lat, obs_lon, obs_alt_m, az, max_range_km, step_m,
            coarse, fine, near_field_km, sea_level_clip, want_details=True)
    else:
        for i, a in enumerate(az):
            tgt_lat, tgt_lon = _direct_vec(obs_lat, obs_lon, a, dist_km)
            dem_m = _profile_heights(coarse, fine, dist_km, tgt_lat, tgt_lon, near_field_km)
            if sea_level_clip:
                dem_m = np.maximum(dem_m, 0.0)
            el = _elevation_angles(obs_lat, obs_lon, obs_alt_m, tgt_lat, tgt_lon, dem_m)
            k = int(np.argmax(el))
            horizon[i] = el[k]
            rng_at_max[i] = dist_km[k]
            dem_at_max[i] = dem_m[k]

    horizon = horizon.astype(out_dtype)
    if not return_details:
        return horizon
    details = {
        "az_deg": az.astype(out_dtype),
        "range_km": rng_at_max.astype(out_dtype),
        "dem_m": dem_at_max.astype(out_dtype),
        "el_deg": horizon,
        "source": source,
        "obs_alt_m": float(obs_alt_m),
        "step_m": float(step_m),
        "max_range_km": float(max_range_km),
    }
    return horizon, details


def horizon_table(observer_id: str, info: dict = None, azimuths=None, **kwargs):
    """生成便于写文件/报告的遮挡角表：[(az_deg, horizon_deg, range_km, dem_m), ...]。

    Parameters
    ----------
    observer_id : str
        观测点 id（需已建缓存）。
    info : dict, optional
        缓存 dict；默认自动读取。
    azimuths : array_like, optional
        方位角 [deg]，默认 0..355 步长 1°。
    **kwargs
        透传给 :func:`terrain_horizon_deg`（如 ``max_range_km``）。

    Returns
    -------
    list of tuple
    """
    if info is None:
        info = load_observer_cache(observer_id)
    obs_lat = float(info["obs_lat"])
    obs_lon = float(info["obs_lon"])
    if azimuths is None:
        azimuths = np.arange(0.0, 360.0, 1.0)
    obs_alt = float(dem_alt(obs_lat, obs_lon))
    hz, det = terrain_horizon_deg(obs_lat, obs_lon, obs_alt, azimuths,
                                  info=info, return_details=True, **kwargs)
    return [(float(a), float(h), float(r), float(d)) for a, h, r, d in
            zip(det["az_deg"], hz, det["range_km"], det["dem_m"])]


# --------------------------------------------------------------------------- #
# 观测点信息汇总
# --------------------------------------------------------------------------- #
def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def observer_dem_info(refresh: bool = False, radius_km: float = None) -> dict:
    """构建/刷新 ``data/dem/observer_dem.json``（3 个观测点 + 发射场）。

    Parameters
    ----------
    refresh : bool
        是否强制重建缓存（否则已存在则复用）。
    radius_km : float, optional
        覆盖半径 [km]；默认取 ``config.dem.horizon_radius_km``（200）。

    Returns
    -------
    dict
        ``{"generated_utc":..., "source":..., "vertical_datum":...,
        "observers":[...], "launch_site":{...}}``
    """
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if radius_km is None:
        radius_km = float(cfg.get("dem", {}).get("horizon_radius_km", 200.0))

    records = []
    for obs in cfg.get("observers", []):
        rec = build_observer_cache(obs["id"], float(obs["lat_deg"]), float(obs["lon_deg"]),
                                   radius_km=radius_km, refresh=refresh)
        rec["name"] = obs.get("name")
        rec["config_alt_m"] = float(obs.get("alt_m", 0.0))
        rec["dem_minus_config_alt_m"] = round(float(rec["dem_alt_m"]) - rec["config_alt_m"], 2)
        records.append(rec)

    site = cfg.get("launch_site", {})
    site_rec = {}
    if site:
        site_rec = build_observer_cache("launch_site", float(site["lat_deg"]),
                                        float(site["lon_deg"]), radius_km=radius_km,
                                        refresh=refresh)
        site_rec["name"] = site.get("name")
        site_rec["config_alt_m"] = float(site.get("alt_m_ellipsoid", 0.0))
        site_rec["dem_minus_config_alt_m"] = round(
            float(site_rec["dem_alt_m"]) - site_rec["config_alt_m"], 2)
        site_rec["note"] = ("SRTM 高程为 EGM96 正高；config 的 alt_m_ellipsoid=10 m 是占位假设，"
                            "建议 Lead 用本值（+ 工位台面高度）修正发射场海拔")

    info = {
        "generated_utc": _utc_now(),
        "source": "SRTMGL1 (NASA SRTM 1 arc-second, AWS elevation-tiles-prod/skadi)",
        "tile_manifest": "data/dem/srtm_tiles.json",
        "vertical_datum": "EGM96 geoid (orthometric). 本任务把 DEM 高程当作 WGS-84 椭球高使用；"
                          "该地区两者相差约 -10~-30 m，对遮挡角影响近场 <0.1°、远场可忽略",
        "cache_grid": f"{CACHE_STEP_ARCSEC} arcsec (~90 m) 对齐整数弧秒，最近邻取自 1″ 瓦片",
        "void_policy": "8 邻域迭代填补，记录 void_ratio；残余 void → 0 m（海平面）",
        "radius_km": radius_km,
        "observers": records,
        "launch_site": site_rec,
    }
    OBSERVER_INFO.parent.mkdir(parents=True, exist_ok=True)
    OBSERVER_INFO.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    return info


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def _cmd_list():
    man = tile_manifest()
    tiles = available_tiles()
    print(f"本地瓦片 {len(tiles)} 个（{DIR_HGT}）")
    if man.get("plan"):
        p = man["plan"]
        print(f"清单: 计划 {p.get('n_tiles')}，压缩包 {p.get('gz_bytes_total', 0) / 1e6:.0f} MB，"
              f"解压 {p.get('hgt_bytes_total', 0) / 1e9:.2f} GB")
    print(" ".join(tiles))
    for c in sorted(DIR_CACHE.glob("*.npz")) if DIR_CACHE.exists() else []:
        print(f"  缓存 {c.name}: {c.stat().st_size / 1e6:.1f} MB")


def _cmd_query(lat, lon):
    print(f"({lat}, {lon}) → 瓦片 {tile_name(lat, lon)}")
    v = sample(lat, lon)
    print(f"  nearest  : {float(v):.1f} m" + ("  (void!)" if np.isnan(float(v)) else ""))
    print(f"  dem_alt  : {float(dem_alt(lat, lon)):.1f} m")
    try:
        print(f"  bilinear : {float(sample(lat, lon, method='bilinear')):.1f} m")
    except Exception as exc:  # noqa: BLE001
        print(f"  bilinear : 失败 {exc}")


def _cmd_horizon(obs_id):
    if obs_id in ("", "all"):
        info = json.loads(OBSERVER_INFO.read_text(encoding="utf-8")) \
            if OBSERVER_INFO.exists() else {}
        ids = [o["id"] for o in info.get("observers", [])] or ["sanya", "haikou", "yongxing"]
    else:
        ids = [obs_id]
    for i in ids:
        c = load_observer_cache(i)
        lat, lon = float(c["obs_lat"]), float(c["obs_lon"])
        alt = float(dem_alt(lat, lon))
        hz, det = terrain_horizon_deg(lat, lon, alt, info=c, return_details=True)
        n = hz.size
        print(f"\n[{i}] ({lat:.4f}, {lon:.4f}) alt={alt:.1f} m  "
              f"方位 {n} 个, 采样源={det['source']}")
        print(f"  遮挡角: min={float(hz.min()):.3f}° max={float(hz.max()):.3f}° "
              f"mean={float(hz.mean()):.3f}°")
        top = np.argsort(hz)[-5:][::-1]
        print("  最高的 5 个方位: " + ", ".join(
            f"az={float(det['az_deg'][k]):.1f}° h={float(hz[k]):.2f}° "
            f"@{float(det['range_km'][k]):.1f}km" for k in top))


def _cmd_custom():
    """打印自定义高程源（离线导入的 GeoTIFF）状态。"""
    path = custom_index_path()
    print(f"自定义高程源登记表：{path}"
          + ("" if path.is_file() else "（不存在，引擎使用 SRTM 瓦片/粗 DEM）"))
    srcs = custom_sources()
    if not srcs:
        print("  没有可用的自定义高程源")
        return
    for r in srcs:
        bb = r["bbox"]
        try:
            info = r["_src"].inspect()
        except Exception as exc:            # noqa: BLE001 - 只影响显示
            info = {"width": "?", "height": "?", "dtype": "?",
                    "compression_name": f"读取失败 {exc}"}
        print(f"  id={r['id']}  名称={r['name']}")
        print(f"    文件 {r['file']}")
        print(f"    {info.get('width')}×{info.get('height')} {info.get('dtype')} "
              f"{info.get('compression_name')}  分辨率 "
              f"{r['res_deg'][0] * 3600:.3f}″×{r['res_deg'][1] * 3600:.3f}″")
        print(f"    范围 lat {bb[0]:.4f}..{bb[1]:.4f}, lon {bb[2]:.4f}..{bb[3]:.4f}"
              f"  {r['crs']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="SRTM DEM 读取/采样/地形遮挡（SRTMGL1, 1″≈30 m）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--build", action="store_true", help="构建观测点 3″ 局部缓存 + observer_dem.json")
    g.add_argument("--query", nargs=2, type=float, metavar=("LAT", "LON"), help="查询单点高程")
    g.add_argument("--horizon", nargs="?", const="all", default=None, metavar="OBS_ID",
                   help="打印观测点地形遮挡角（默认全部）")
    g.add_argument("--list", action="store_true", help="本地瓦片/缓存状态")
    g.add_argument("--custom", action="store_true", help="自定义高程源（离线导入）状态")
    ap.add_argument("--radius-km", type=float, default=200.0, help="缓存覆盖半径 km")
    ap.add_argument("--refresh", action="store_true", help="强制重建缓存")
    args = ap.parse_args(argv)

    if args.query:
        _cmd_query(*args.query)
        return 0
    if args.horizon is not None:
        _cmd_horizon(args.horizon)
        return 0
    if args.list:
        _cmd_list()
        return 0
    if args.custom:
        _cmd_custom()
        return 0
    if args.build:
        info = observer_dem_info(refresh=args.refresh, radius_km=args.radius_km)
        print(json.dumps({k: v for k, v in info.items() if k != "observers"},
                         ensure_ascii=False, indent=1))
        for o in info["observers"] + ([info["launch_site"]] if info["launch_site"] else []):
            print(f"  {o['id']:12s} lat={o['lat']:.4f} lon={o['lon']:.4f} "
                  f"dem_alt={o['dem_alt_m']:.1f} m  void={o['void_ratio']:.4%}  "
                  f"tiles={len(o['tiles_used'])}  网格={o['cache_grid']}")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
