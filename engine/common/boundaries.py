# -*- coding: utf-8 -*-
"""common.boundaries — Natural Earth 国界/省界/海岸线/城市 + 中国陆地掩膜。

数据来源（public domain）
-------------------------
``data/ne/`` 下由 ``_tools/fetch_ne.py`` 下载并裁剪：

* ``china_admin0.geojson``   Natural Earth 50m admin_0，仅 CN / TW / HK / MO 四个要素
* ``china_admin1.geojson``   Natural Earth 10m admin_1，仅 iso_a2 == "CN"（省级）
* ``china_cities.geojson``   Natural Earth 10m populated_places，中国人口前 120
* ``coastline_cn_bbox.geojson`` Natural Earth 10m coastline，裁剪到中国及周边
* ``raw/`` 原始下载（73 MB），``ne_manifest.json`` 清单

**坐标顺序约定（本模块）**：所有几何数组都是 ``(N, 2)``，列顺序 ``(lon, lat)``
（GeoJSON 原生顺序，方便直接 ``ax.plot(seg[:, 0], seg[:, 1])``）。
与 :mod:`common.notam`（``(lat, lon)``）不同，注意区分。
:func:`major_cities` 返回 ``(name, lat, lon, population)``（按任务书约定）。

用途
----
* ``map`` 模块画国界/省界/城市/陆地填色；
* ``china_land_mask`` 为 0.2° 全国网格（321×201）生成陆地 bool 掩膜（含台湾、海南、
  港澳），用于只在大陆/岛屿上做统计与填色。

声明
----
Natural Earth 是 **public domain** 的示意性边界数据（1:50m / 1:10m 比例尺），
**不是任何官方界线**；本模块及其输出仅用于本任务的科学可视化与教学分析。
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np

__all__ = [
    "DATA_NE", "CHINA_ISO", "china_extent", "load_china_country", "load_coastline",
    "country_rings", "iter_borders", "iter_provinces", "province_names",
    "major_cities", "city_names", "china_land_mask", "land_mask_stats",
    "plot_china", "data_available",
]

ROOT = Path(os.environ.get("RK_ENGINE") or Path(__file__).resolve().parents[1])
DATA_NE = ROOT / "data" / "ne"
F_ADMIN0 = DATA_NE / "china_admin0.geojson"
F_ADMIN1 = DATA_NE / "china_admin1.geojson"
F_CITIES = DATA_NE / "china_cities.geojson"
F_COAST = DATA_NE / "coastline_cn_bbox.geojson"

#: 视为"中国"的 ISO alpha-2 代码（含台港澳）
CHINA_ISO = ("CN", "TW", "HK", "MO")

_JSON_CACHE: dict[str, dict] = {}


# --------------------------------------------------------------------------- #
# 读文件 / 几何提取
# --------------------------------------------------------------------------- #
def data_available() -> bool:
    """Natural Earth 裁剪文件是否齐全。"""
    return all(p.exists() for p in (F_ADMIN0, F_ADMIN1, F_CITIES, F_COAST))


def _load(path: Path) -> dict:
    """读 GeoJSON（带缓存）。"""
    key = str(path)
    if key not in _JSON_CACHE:
        if not path.exists():
            raise FileNotFoundError(f"缺 Natural Earth 文件 {path}；先运行 python _tools/fetch_ne.py")
        _JSON_CACHE[key] = json.loads(path.read_text(encoding="utf-8"))
    return _JSON_CACHE[key]


def _geom_rings(geom: dict):
    """GeoJSON Polygon/MultiPolygon → 依次产出每个环的 (N,2) float 数组 (lon, lat)。

    外环与内环（洞）都会产出；顺序为 GeoJSON 原始顺序。
    """
    if not geom:
        return
    t = geom.get("type")
    if t == "Polygon":
        polys = [geom.get("coordinates") or []]
    elif t == "MultiPolygon":
        polys = geom.get("coordinates") or []
    elif t == "LineString":
        rings = [geom.get("coordinates") or []]
        for r in rings:
            arr = np.asarray(r, dtype=float)
            if arr.ndim == 2 and arr.shape[0] >= 2:
                yield arr[:, :2]
        return
    elif t == "MultiLineString":
        for r in geom.get("coordinates") or []:
            arr = np.asarray(r, dtype=float)
            if arr.ndim == 2 and arr.shape[0] >= 2:
                yield arr[:, :2]
        return
    else:
        return
    for poly in polys:
        for ring in poly:
            arr = np.asarray(ring, dtype=float)
            if arr.ndim == 2 and arr.shape[0] >= 3:
                yield arr[:, :2]


def _feature_kind(props: dict) -> str:
    """判断 admin_0 要素属于哪一类中国领土：'CN' / 'TW' / 'HK' / 'MO' / ''。

    Natural Earth 50m 的台湾要素 ``ISO_A2 == "CN-TW"``（不是 "TW"），因此不能
    只按 ISO_A2 匹配；这里同时看 ISO_A2 / ADM0_A3 / NAME。
    """
    iso = (props.get("ISO_A2") or "").upper()
    a3 = (props.get("ADM0_A3") or "").upper()
    name = (props.get("NAME") or "").lower()
    if iso in ("HK", "MO"):
        return iso
    if a3 == "HKG":
        return "HK"
    if a3 == "MAC":
        return "MO"
    if iso in ("TW", "CN-TW") or a3 == "TWN" or "taiwan" in name:
        return "TW"
    if iso == "CN" or a3 == "CHN" or "china" in name:
        return "CN"
    return ""


def load_china_country():
    """中国（含台湾/香港/澳门）国界要素列表。

    Returns
    -------
    list of dict
        每项 ``{"name", "name_zh", "iso_a2", "kind", "rings": [ (N,2) lon/lat 数组, ... ]}``；
        ``kind`` ∈ {'CN','TW','HK','MO'}；``rings`` 含外环与洞。

    Examples
    --------
    >>> feats = load_china_country()
    >>> sorted(f["kind"] for f in feats)
    ['CN', 'HK', 'MO', 'TW']
    """
    gj = _load(F_ADMIN0)
    out = []
    for f in gj.get("features", []):
        pr = f.get("properties") or {}
        rings = list(_geom_rings(f.get("geometry") or {}))
        out.append({
            "name": pr.get("NAME") or pr.get("NAME_EN") or "",
            "name_zh": pr.get("NAME_ZH") or "",
            "iso_a2": (pr.get("ISO_A2") or "").upper(),
            "kind": _feature_kind(pr),
            "rings": rings,
        })
    return out


def load_coastline():
    """中国及周边海岸线 → list of (N,2) 数组 (lon, lat)（可含多段）。"""
    gj = _load(F_COAST)
    segs = []
    for f in gj.get("features", []):
        for r in _geom_rings(f.get("geometry") or {}):
            segs.append(r)
    return segs


def country_rings(include_taiwan: bool = True, include_hk_mo: bool = True,
                  bbox=None):
    """中国国界所有环（lon, lat），可按要素/包围盒过滤。

    Parameters
    ----------
    include_taiwan : bool
        是否包含台湾（ISO_A2 == 'TW'）。
    include_hk_mo : bool
        是否包含香港/澳门。
    bbox : (lon_min, lat_min, lon_max, lat_max), optional
        只保留与 bbox 相交的环。

    Returns
    -------
    list of ndarray (N,2)  (lon, lat)
    """
    keep_iso = {"CN"}
    if include_taiwan:
        keep_iso.add("TW")
    if include_hk_mo:
        keep_iso |= {"HK", "MO"}
    out = []
    for f in load_china_country():
        if (f.get("kind") or f["iso_a2"]) not in keep_iso:
            continue
        for r in f["rings"]:
            if bbox is not None and not _ring_in_bbox(r, bbox):
                continue
            out.append(r)
    return out


def _ring_in_bbox(ring: np.ndarray, bbox) -> bool:
    """环的包围盒是否与给定 bbox 相交（粗略过滤，不做几何裁剪）。"""
    lon0, lat0, lon1, lat1 = bbox
    return not (ring[:, 0].max() < lon0 or ring[:, 0].min() > lon1
                or ring[:, 1].max() < lat0 or ring[:, 1].min() > lat1)


def iter_borders(bbox=None, include_taiwan: bool = True,
                 include_hk_mo: bool = True):
    """国界线（可绘制线段）→ list of (N,2) 数组，列顺序 **(lon, lat)**。

    Parameters
    ----------
    bbox : (lon_min, lat_min, lon_max, lat_max), optional
        例如 ``(72, 15, 136, 55)``（config.grid 范围）；只保留相交的环。
    include_taiwan, include_hk_mo : bool
        是否包含台湾/港澳。

    Returns
    -------
    list of ndarray
        直接可用于 ``LineCollection(segs)`` 或 ``ax.plot(s[:,0], s[:,1])``。

    Examples
    --------
    >>> segs = iter_borders(bbox=(72, 15, 136, 55))
    >>> sum(len(s) for s in segs) > 10000
    True
    """
    return country_rings(include_taiwan, include_hk_mo, bbox=bbox)


def iter_provinces(bbox=None, with_names: bool = False):
    """中国省级行政界线（可绘制线段）。

    Parameters
    ----------
    bbox : tuple, optional
        同上。
    with_names : bool
        True 时返回 ``[(province_name, (N,2) 数组), ...]``；False 只返回数组列表。

    Returns
    -------
    list
        线段数组列表，或 (name, 数组) 列表。列顺序 (lon, lat)。
    """
    gj = _load(F_ADMIN1)
    segs, named = [], []
    for f in gj.get("features", []):
        pr = f.get("properties") or {}
        name = pr.get("name_zh") or pr.get("name") or ""
        for r in _geom_rings(f.get("geometry") or {}):
            if bbox is not None and not _ring_in_bbox(r, bbox):
                continue
            segs.append(r)
            if with_names:
                named.append((name, r))
    return named if with_names else segs


def province_names():
    """中国省级行政区名称列表（Natural Earth 中文名优先，缺失用英文名）。"""
    gj = _load(F_ADMIN1)
    names = []
    for f in gj.get("features", []):
        pr = f.get("properties") or {}
        names.append(pr.get("name_zh") or pr.get("name") or "")
    return names


def china_extent():
    """中国（含台湾/港澳）几何包围盒 → ``(lon_min, lat_min, lon_max, lat_max)``。"""
    rings = country_rings()
    lons = np.concatenate([r[:, 0] for r in rings])
    lats = np.concatenate([r[:, 1] for r in rings])
    return (float(lons.min()), float(lats.min()), float(lons.max()), float(lats.max()))


# --------------------------------------------------------------------------- #
# 城市
# --------------------------------------------------------------------------- #
def major_cities(top_n: int = 40, min_pop: int = 0):
    """中国主要城市列表 → ``[(name, lat, lon, population), ...]``（按人口降序）。

    Parameters
    ----------
    top_n : int
        最多返回多少个（默认 40，数据文件本身已按人口前 120 裁剪）。
    min_pop : int
        额外的人口下限过滤（默认 0 不过滤）。

    Returns
    -------
    list of tuple
        ``(中文名或英文名, lat, lon, POP_MAX)``，经纬度 [deg]。

    Examples
    --------
    >>> cities = major_cities(5)
    >>> cities[0][0]   # 人口最多的中国城市（重庆/上海一带）
    """
    gj = _load(F_CITIES)
    out = []
    for f in gj.get("features", []):
        pr = f.get("properties") or {}
        pop = int(pr.get("POP_MAX") or 0)
        if pop < min_pop:
            continue
        lat = float(pr.get("LAT") if pr.get("LAT") is not None
                    else (f.get("geometry") or {}).get("coordinates", [0, 0])[1])
        lon = float(pr.get("LON") if pr.get("LON") is not None
                    else (f.get("geometry") or {}).get("coordinates", [0, 0])[0])
        name = pr.get("NAME_ZH") or pr.get("NAME") or ""
        out.append((name, lat, lon, pop))
    out.sort(key=lambda t: -t[3])
    return out[:top_n] if top_n else out


def city_names(top_n: int = 40):
    """城市名列表（按人口降序）。"""
    return [c[0] for c in major_cities(top_n)]


# --------------------------------------------------------------------------- #
# 陆地掩膜（多边形栅格化）
# --------------------------------------------------------------------------- #
def _rasterize_rings(rings, lons: np.ndarray, lats: np.ndarray) -> np.ndarray:
    """奇偶规则扫描线栅格化：多个环（外环+洞）用 XOR 合成，自然处理嵌套洞。

    Parameters
    ----------
    rings : list of (N,2) arrays
        多边形环（lon, lat）；外环与内环（洞）混排，靠奇偶规则区分。
    lons, lats : 1-D 数组
        目标网格的经度/纬度坐标 [deg]（升序或降序均可）。

    Returns
    -------
    ndarray bool, shape (len(lats), len(lons))
    """
    lons = np.asarray(lons, dtype=float)
    lats = np.asarray(lats, dtype=float)
    mask = np.zeros((lats.size, lons.size), dtype=bool)

    for ring in rings:
        x1 = np.asarray(ring[:, 0], dtype=float)
        y1 = np.asarray(ring[:, 1], dtype=float)
        if x1.size < 3:
            continue
        x2 = np.roll(x1, -1)
        y2 = np.roll(y1, -1)
        rows = np.nonzero((lats >= y1.min()) & (lats <= y1.max()))[0]
        for j in rows:
            y = lats[j]
            spans = (y1 > y) != (y2 > y)          # 标准半开穿越规则
            if not spans.any():
                continue
            t = (y - y1[spans]) / (y2[spans] - y1[spans])
            xs = np.sort(x1[spans] + t * (x2[spans] - x1[spans]))
            # 落在第奇数个交点右侧 → 在环内
            idx = np.searchsorted(xs, lons, side="right")
            mask[j] ^= (idx % 2 == 1)
    return mask


def china_land_mask(lons, lats, include_taiwan: bool = True,
                    include_hk_mo: bool = True) -> np.ndarray:
    """中国陆地掩膜（含台湾/海南/港澳），在给定经纬度网格上栅格化国界多边形。

    Parameters
    ----------
    lons : array_like
        经度一维网格 [deg]，如 ``np.arange(72, 136.0001, 0.2)``（321 点）。
    lats : array_like
        纬度一维网格 [deg]，如 ``np.arange(15, 55.0001, 0.2)``（201 点）。
    include_taiwan, include_hk_mo : bool
        是否包含台湾/港澳（默认都包含）。

    Returns
    -------
    ndarray bool, shape ``(len(lats), len(lons))``
        True = 中国陆地（含岛屿）。第 ``[j, i]`` 点对应 ``(lats[j], lons[i])``。

    Notes
    -----
    * 用**奇偶规则**（even-odd）扫描线栅格化：外环与内环（洞）异或合成，
      可正确处理青海湖等内部洞、以及多段岛屿；
    * 网格点恰好落在边界上时结果不做特殊处理（0.2° 网格下影响 ≤1 个格点）；
    * Natural Earth 50m 国界为**示意边界**，非官方界线；海岸线精度约 1 km，
      0.2° 网格（≈22 km）下足够；
    * 全国网格 321×201 计算耗时约 0.3–1 s。

    Examples
    --------
    >>> lons = np.arange(72, 136.0001, 0.2)
    >>> lats = np.arange(15, 55.0001, 0.2)
    >>> m = china_land_mask(lons, lats)
    >>> m.shape
    (201, 321)
    >>> bool(m[np.argmin(abs(lats-39.9)), np.argmin(abs(lons-116.4))])   # 北京
    True
    """
    rings = country_rings(include_taiwan=include_taiwan, include_hk_mo=include_hk_mo)
    return _rasterize_rings(rings, lons, lats)


def land_mask_stats(mask: np.ndarray, lons, lats, step_deg: float = None):
    """陆地掩膜统计（格点数、等面积近似面积）。

    Parameters
    ----------
    mask : ndarray bool (ny, nx)
        :func:`china_land_mask` 的结果。
    lons, lats : array_like
        对应的一维网格。
    step_deg : float, optional
        网格步长 [deg]；默认由 lons 差分推断。

    Returns
    -------
    dict
        ``{"n_land": .., "n_total": .., "land_fraction": .., "area_km2": ..}``
        （面积用球面梯形公式近似，仅示意）。
    """
    lons = np.asarray(lons, dtype=float)
    lats = np.asarray(lats, dtype=float)
    if step_deg is None:
        step_deg = float(np.mean(np.diff(lons))) if lons.size > 1 else 0.2
    R = 6371.0088
    dlam = math.radians(step_deg)
    # 每行格点面积 ≈ R² Δλ (sinφ2 - sinφ1)
    half = math.radians(step_deg) / 2.0
    row_area = R ** 2 * dlam * (np.sin(np.radians(lats) + half)
                                - np.sin(np.radians(lats) - half))
    n_land = int(mask.sum())
    area = float(np.sum(row_area[:, None] * mask))
    return {
        "n_land": n_land,
        "n_total": int(mask.size),
        "land_fraction": n_land / float(mask.size),
        "area_km2": area,
    }


# --------------------------------------------------------------------------- #
# 绘图辅助
# --------------------------------------------------------------------------- #
def plot_china(ax, bbox=None, borders=True, provinces=True, coastline=False,
               cities=False, city_min_pop=0, n_cities=12, colors=None,
               lw_border=0.9, lw_province=0.35, lw_coast=0.6,
               label_cities=False, font_size=None, zorder=5):
    """在 matplotlib Axes 上叠加中国国界/省界/海岸线/城市点（map 模块可直接用）。

    Parameters
    ----------
    ax : matplotlib.axes.Axes
        目标坐标轴（应已设置经纬度范围，``aspect`` 建议按纬度缩放）。
    bbox : tuple, optional
        ``(lon_min, lat_min, lon_max, lat_max)``，用于过滤不可见线段。
    borders, provinces, coastline, cities : bool
        各图层开关。
    city_min_pop : int
        城市点的人口过滤下限。
    n_cities : int
        最多画多少个城市点（按人口降序）。
    colors : dict, optional
        覆盖默认颜色（键：``border``, ``province``, ``coast``, ``city``）。
    lw_border, lw_province, lw_coast : float
        线宽。
    label_cities : bool
        是否标注城市中文名。
    font_size : float, optional
        城市标注字号。
    zorder : float
        基础 zorder。

    Returns
    -------
    dict
        实际绘制所用参数与元素数，如 ``{"n_border_rings":.., "n_province_rings":..}``。

    Notes
    -----
    * 坐标顺序 (lon, lat)，与 :func:`iter_borders` 一致；
    * 不负责填色（陆地填色用 :func:`china_land_mask` + ``pcolormesh``/``imshow``）；
    * 调用前请先 ``common.plotstyle.setup()`` 设置中文字体。
    """
    from matplotlib.collections import LineCollection

    default_colors = {"border": "#333333", "province": "#9a9a9a",
                      "coast": "#2f6f9f", "city": "#111111"}
    if colors:
        default_colors.update(colors)
    info = {"n_border_rings": 0, "n_province_rings": 0, "n_coast_lines": 0, "n_cities": 0}

    if borders:
        segs = iter_borders(bbox=bbox)
        if segs:
            ax.add_collection(LineCollection(segs, colors=default_colors["border"],
                                             linewidths=lw_border, zorder=zorder + 1))
            info["n_border_rings"] = len(segs)
    if provinces:
        segs = iter_provinces(bbox=bbox)
        if segs:
            ax.add_collection(LineCollection(segs, colors=default_colors["province"],
                                             linewidths=lw_province, alpha=0.9,
                                             linestyles=(0, (4, 2)),
                                             zorder=zorder))
            info["n_province_rings"] = len(segs)
    if coastline:
        segs = load_coastline()
        if bbox is not None:
            segs = [s for s in segs if _ring_in_bbox(s, bbox)]
        if segs:
            ax.add_collection(LineCollection(segs, colors=default_colors["coast"],
                                             linewidths=lw_coast, zorder=zorder))
            info["n_coast_lines"] = len(segs)
    if cities:
        cs = [c for c in major_cities(top_n=n_cities, min_pop=city_min_pop)]
        if cs:
            xs = [c[2] for c in cs]
            ys = [c[1] for c in cs]
            ax.scatter(xs, ys, s=9, c=default_colors["city"], marker="o",
                       linewidths=0, zorder=zorder + 3)
            if label_cities:
                for name, la, lo, _pop in cs:
                    ax.annotate(name, (lo, la), xytext=(3, 2),
                                textcoords="offset points",
                                fontsize=(font_size or 8), zorder=zorder + 4,
                                color="#111111")
            info["n_cities"] = len(cs)
    return info
