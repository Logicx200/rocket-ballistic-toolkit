# -*- coding: utf-8 -*-
"""common.notam — 航警（NOTAM）危险区几何：DMS 解析、点在多边形内、边界距离、面积。

本模块只做**几何/解析**，不含任何 I/O 副作用（除便捷函数 :func:`load_zones` 读 config）。
纯 numpy 实现。

坐标约定
--------
* 输入经纬度均为 **度**；多边形以 ``(N,2)`` 数组表示，列顺序 ``(lat, lon)``。
* 两点间距离用**球面**公式（R = 6371.0088 km），与 WGS-84 椭球差 < 0.3%
  （本任务 1000 km 量级约 1–3 km），足够用于"落点距危险区边界多少 km"的判断；
  需要 mm/km 级精确值请改用 :func:`common.geo.geodesic_inverse`。
* 面积用球面多边形公式（见 :func:`polygon_area_km2` 注释），几个度见方的区域误差 < 0.1%。

航警文本格式
------------
``131700N 1155500E`` = 13°17'00"N 115°55'00"E → (13.2833333, 115.9166667)。
``parse_dms`` 同时支持 ``1317N``（DDMM）、``131700N``（DDMMSS）、``13.2833N``（十进制度）、
``13 17 00 N`` / ``13°17'00" N`` 等写法，南/西半球用 S/W 或负号。
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .geo import EARTH_MEAN_R_KM, great_circle, wrap180, wrap360

__all__ = [
    "parse_dms", "parse_vertex", "parse_polygon", "polygon_bounds",
    "point_in_polygon", "points_in_polygon", "is_on_boundary",
    "distance_to_polygon_km", "distance_to_polygon_edge_km",
    "polygon_area_km2", "polygon_perimeter_km", "polygon_centroid",
    "load_zones", "zone_active_at",
    "hint_object_targets", "object_hint_targets", "object_name_matches",
]

# --------------------------------------------------------------------------- #
# 航警区「残骸类型」(stage_hint) → 残骸对象的统一映射（v3.1）
# --------------------------------------------------------------------------- #
# 背景：仿真实体名有两套——
#   * 原始名（dynamics.simulate 返回的 debris[].obj）：一级/二级（= stage.name）、
#     booster_1…、fairing_L/R
#   * 规范化名（export.py 写进 CSV/objects.json）：core_stage/stage_1、booster_N、fairing_L/R
# 各脚本历史实现只认其中一套，导致「一级」航警区在 invert/window/mc 里静默失效。
# 这里给出唯一映射：hint → 对象名前缀元组（同时含原始名与规范化名），由
# object_name_matches() 做"全名或前缀"匹配。
_STAGE_ORDINAL = [
    (("一子级", "芯一级", "第一级", "一级"), 0),
    (("二子级", "芯二级", "第二级", "二级"), 1),
    (("三子级", "芯三级", "第三级", "三级"), 2),
    (("四子级", "芯四级", "第四级", "四级"), 3),
]


def _stage_targets(idx: int, name: str) -> tuple:
    canonical = "core_stage" if idx == 0 else "stage_%d" % idx
    return tuple(x for x in (str(name or ""), canonical) if x)


def hint_object_targets(hint, stage_names):
    """航警 ``stage_hint`` → 残骸对象名候选前缀元组。

    Parameters
    ----------
    hint : str
        航警里的残骸类型，如 ``一级`` / ``整流罩`` / ``助推器`` / ``二级``。
    stage_names : sequence of str
        配置里的级名列表（``config.vehicle.stages[].name``）。

    Returns
    -------
    tuple or None or ()
        * ``None``：hint 为空 → 不限制对象（该区参与全部残骸判定）；
        * 非空 tuple：按"全名或前缀"匹配的对象名，如 ``("一级", "core_stage")``、
          ``("fairing",)``、``("booster", "boosters")``；
        * 空 tuple ``()``：hint 无法识别（调用方应告警，而不是静默跳过）。
    """
    h = str(hint or "").strip()
    if not h:
        return None
    stages = [str(x) for x in (stage_names or [])]
    # 1) 与级名互相包含（配置里的级名可能是"一级"/"芯一级"/"第一级"…）
    for i, nm in enumerate(stages):
        if nm and (h in nm or nm in h):
            return _stage_targets(i, nm)
    # 2) 序数词（"一级"/"一子级"/"第一级"…）
    low = h.lower()
    for keys, idx in _STAGE_ORDINAL:
        if any(k in h for k in keys):
            if idx < len(stages):
                return _stage_targets(idx, stages[idx])
            return ()
    # 3) 整流罩
    if "罩" in h or "fair" in low:
        return ("fairing",)
    # 4) 助推器
    if "助推" in h or "booster" in low:
        return ("booster", "boosters")
    return ()


def object_hint_targets(obj_name, stage_names):
    """**具体**残骸对象名（规范化名，如 ``fairing_L``/``booster_1``/``core_stage``）→ 前缀元组。

    与 :func:`hint_object_targets` 的区别：后者给的是"类型"（整流罩/助推器/一级），
    同一类型可能对应多个对象（整流罩 2 半、助推器 4 台）；本函数给的是"某一个对象"，
    用于把多条形制航警区与多个残骸对象**一对一**配对（否则两条整流罩区会同时约束两半）。

    返回 ``None`` 表示无法识别；其余情况返回的元组用 :func:`object_name_matches` 匹配。
    """
    o = str(obj_name or "").strip()
    if not o:
        return None
    stages = [str(x) for x in (stage_names or [])]
    if o in ("core_stage", "stage_0"):
        return _stage_targets(0, stages[0]) if stages else (o,)
    m = re.fullmatch(r"stage_(\d+)", o)
    if m:
        idx = int(m.group(1))
        if idx < len(stages):
            return _stage_targets(idx, stages[idx])
        return (o,)
    return (o,)


def object_name_matches(obj_name, targets) -> bool:
    """残骸对象名（原始名或规范化名）是否命中 ``hint_object_targets`` 的结果。

    ``targets is None`` → 视为不限制（返回 True）；空 tuple → 不匹配任何对象。
    """
    if targets is None:
        return True
    name = str(obj_name or "")
    if not name:
        return False
    return any(name == t or name.startswith(t) for t in (targets or ()))

# 经度方向的 1° 对应距离（赤道）[km]
_KM_PER_DEG = math.pi * EARTH_MEAN_R_KM / 180.0     # 111.1949 km/deg

_HEMI = {"N": 1.0, "S": -1.0, "E": 1.0, "W": -1.0}


# --------------------------------------------------------------------------- #
# 文本解析
# --------------------------------------------------------------------------- #
def _split_dms_digits(digits: str, deg_width: int = 2):
    """纯数字串 → [度, 分, 秒] 字符串分量（按 ``deg_width`` 位度切分）。

    ``deg_width=2``（纬度）：DD / DDMM / DDMMSS / DDMMSS.s / DDMMSS.ss；
    ``deg_width=3``（经度）：DDD / DDDMM / DDDMMSS / DDDMMSS.s。
    """
    n = len(digits)
    if deg_width == 3:
        if n <= 3:
            return [digits]
        if n <= 5:
            return [digits[:-2], digits[-2:]]
        if n <= 7:
            return [digits[:-4], digits[-4:-2], digits[-2:]]
        if n == 8:                                    # DDDMMSS.s
            return [digits[:-5], digits[-5:-3], digits[-3:-1] + "." + digits[-1]]
        raise ValueError(f"纬度/经度数字串过长: {digits!r}")
    # deg_width == 2
    if n <= 2:
        return [digits]
    if n <= 4:
        return [digits[:-2], digits[-2:]]
    if n <= 6:
        return [digits[:-4], digits[-4:-2], digits[-2:]]
    if n == 7:                                        # DDMMSS.s
        return [digits[:-5], digits[-5:-3], digits[-3:-1] + "." + digits[-1]]
    if n == 8:                                        # DDMMSS.ss
        return [digits[:-6], digits[-6:-4], digits[-4:-2] + "." + digits[-2:]]
    raise ValueError(f"经纬度数字串过长: {digits!r}")


def parse_dms(text):
    """把 NOTAM 风味的经纬度字符串解析为十进制度。

    支持格式（大小写不敏感，允许前后空格）：

    ===============  ==================  ================================
    输入              输出                 说明
    ===============  ==================  ================================
    ``131700N``      13.283333333         DDMMSS + 半球（2 位度）
    ``1155500E``     115.916666667        DDDMMSS + 半球（3 位度，经度）
    ``1317N``        13.283333333         DDMM + 半球
    ``1155E``        11.916666667         DDDMM + 半球
    ``131700.5N``    13.283472222         DDMMSS.s + 半球
    ``13.2833N``     13.2833              十进制度 + 半球
    ``13 17 00 N``   13.283333333         空格/°/′/″/冒号分隔
    ``13°17'00"N``   13.283333333         DMS 符号分隔
    ``131700S``      -13.283333333        S/W 自动取负
    ``-13.2833``     -13.2833            带符号十进制度
    ===============  ==================  ================================

    Parameters
    ----------
    text : str or float or int
        待解析字符串；若已是数值则直接返回 ``float(text)``。

    Returns
    -------
    float
        十进制度。

    Raises
    ------
    ValueError
        无法识别。

    Notes
    -----
    度宽由半球字母决定：``N/S`` → 2 位度（纬度），``E/W`` → 3 位度（经度）。
    无半球字母时按长度猜：偶数长度按 2 位度、奇数长度按 3 位度
    （例：``131700`` → 13°17'00"，``1155500`` → 115°55'00"）。
    因此**强烈建议**永远带半球字母写 DMS。

    Examples
    --------
    >>> round(parse_dms("131700N"), 6)
    13.283333
    >>> round(parse_dms("1155500E"), 6)
    115.916667
    """
    if isinstance(text, (int, float, np.floating, np.integer)):
        return float(text)
    s = str(text).strip().replace(",", ".")
    if not s:
        raise ValueError("空字符串无法解析为经纬度")

    # 1) 尾部半球字母 → 符号 + 度宽
    sign = 1.0
    deg_width = None
    m = re.search(r"([NSEWnsew])\s*$", s)
    if m:
        hemi = m.group(1).upper()
        sign = _HEMI[hemi]
        deg_width = 2 if hemi in ("N", "S") else 3
        s = s[: m.start()].strip()

    # 2) 前导正负号
    if s.startswith("-") or s.startswith("+"):
        if s[0] == "-":
            sign *= -1.0
        s = s[1:].strip()

    # 3) 含 DMS 分隔符（度分秒符号/空格/冒号）→ 直接按分量解析
    if re.search(r"[°:'″′\"\s]", s):
        parts = [p for p in re.split(r"[°:'″′\"\s]+", s) if p != ""]
        try:
            vals = [float(p) for p in parts]
        except ValueError as exc:
            raise ValueError(f"无法解析经纬度字符串: {text!r}") from exc
        if not 1 <= len(vals) <= 3:
            raise ValueError(f"经纬度分量个数异常 ({len(vals)}): {text!r}")
        deg = vals[0]
        if len(vals) >= 2:
            deg += vals[1] / 60.0
        if len(vals) >= 3:
            deg += vals[2] / 3600.0
        return sign * deg

    # 4) 纯数字（可带小数点）：按度宽切分；小数部分归到最小单位
    frac = ""
    int_part = s
    if "." in s:
        int_part, frac = s.split(".", 1)
        if not int_part:
            int_part = "0"
    if not int_part.isdigit():
        raise ValueError(f"无法解析经纬度字符串: {text!r}")
    if deg_width is None:
        # 无半球：偶数长度按 2 位度、奇数长度按 3 位度
        deg_width = 3 if len(int_part) % 2 == 1 else 2
    try:
        parts = _split_dms_digits(int_part, deg_width)
    except ValueError as exc:
        raise ValueError(f"无法解析经纬度字符串: {text!r}") from exc
    if frac:
        parts[-1] = parts[-1] + "." + frac
    vals = [float(p) for p in parts]
    deg = vals[0]
    if len(vals) >= 2:
        deg += vals[1] / 60.0
    if len(vals) >= 3:
        deg += vals[2] / 3600.0
    return sign * deg


def parse_vertex(text):
    """解析一个多边形顶点，如 ``"131700N 1155500E"`` → ``(lat, lon)``。

    Parameters
    ----------
    text : str
        含纬度、经度两段的字符串，可用空格/逗号/斜杠分隔；允许顺序颠倒
        （自动按半球字母 N/S 与 E/W 判断，无字母时按"先纬度后经度"）。

    Returns
    -------
    (lat_deg, lon_deg) : tuple of float

    Raises
    ------
    ValueError
        无法拆出两个分量。

    Examples
    --------
    >>> lat, lon = parse_vertex("131700N 1155500E")
    >>> round(lat, 4), round(lon, 4)
    (13.2833, 115.9167)
    """
    s = str(text)
    # 优先按"带半球字母"的模式抓两段
    pairs = re.findall(r"\d+(?:\.\d+)?\s*[NSEWnsew]", s)
    if len(pairs) >= 2:
        a, b = pairs[0], pairs[1]
    else:
        parts = [p for p in re.split(r"[,/;]+|\s{2,}|\s+(?=\d)", s.strip()) if p.strip()]
        if len(parts) < 2:
            parts = s.split()
        if len(parts) < 2:
            raise ValueError(f"无法从 {text!r} 解析出两个经纬度分量")
        a, b = parts[0], parts[1]

    a_hemi = (re.search(r"([NSEWnsew])\s*$", a) or [None, ""])[1].upper()
    b_hemi = (re.search(r"([NSEWnsew])\s*$", b) or [None, ""])[1].upper()
    # 若第一段是 E/W、第二段是 N/S，则交换
    if a_hemi in "EW" and b_hemi in "NS":
        a, b = b, a
    lat = parse_dms(a)
    lon = parse_dms(b)
    return lat, lon


def parse_polygon(vertices):
    """解析多边形顶点列表 → ``(N,2)`` ndarray（列顺序 lat, lon）。

    Parameters
    ----------
    vertices : sequence
        元素可以是字符串（``"131700N 1155500E"``）、``(lat, lon)`` 元组/列表，
        也可以是已经解析好的二维数组。首尾点重复与否都可以。

    Returns
    -------
    ndarray, shape (N, 2), dtype=float64
        顶点数组（**保持输入顺序**，不自动闭合；各函数内部按闭合处理）。

    Examples
    --------
    >>> poly = parse_polygon(["131700N 1155500E", "133800N 1162200E",
    ...                       "124600N 1170300E", "122600N 1163700E"])
    >>> poly.shape
    (4, 2)
    """
    if isinstance(vertices, np.ndarray) and vertices.ndim == 2 and vertices.shape[1] == 2:
        return np.asarray(vertices, dtype=float)
    out = []
    for v in vertices:
        if isinstance(v, str):
            out.append(parse_vertex(v))
        else:
            arr = np.asarray(v, dtype=float).ravel()
            if arr.size != 2:
                raise ValueError(f"顶点 {v!r} 不是 (lat, lon) 二元组")
            out.append((float(arr[0]), float(arr[1])))
    return np.asarray(out, dtype=float)


def polygon_bounds(polygon):
    """多边形经纬度包围盒 → ``(lat_min, lat_max, lon_min, lon_max)`` [deg]。"""
    poly = _as_poly(polygon)
    return (float(poly[:, 0].min()), float(poly[:, 0].max()),
            float(poly[:, 1].min()), float(poly[:, 1].max()))


def _as_poly(polygon):
    """内部：统一成闭合的 (N,2) float 数组（去掉重复末点后由算法自行首尾相连）。"""
    poly = np.asarray(polygon, dtype=float)
    if poly.ndim != 2 or poly.shape[1] != 2:
        raise ValueError(f"polygon 需为 (N,2) 数组，得到 shape={poly.shape}")
    if poly.shape[0] >= 2 and np.allclose(poly[0], poly[-1]):
        poly = poly[:-1]
    if poly.shape[0] < 3:
        raise ValueError("多边形至少需要 3 个不重合顶点")
    return poly


# --------------------------------------------------------------------------- #
# 点在多边形内（射线法 / 奇偶规则）
# --------------------------------------------------------------------------- #
def _segments(poly):
    """返回多边形的所有边 (lat1, lat2, lon1, lon2)，首尾相连。"""
    lat1, lon1 = poly[:, 0], poly[:, 1]
    lat2, lon2 = np.roll(lat1, -1), np.roll(lon1, -1)
    return lat1, lon1, lat2, lon2


def _point_on_edge(lat, lon, poly, tol_deg=1e-9):
    """判断点是否落在多边形某条边上（容差 tol_deg 度，考虑经度缩放）。"""
    lat1, lon1, lat2, lon2 = _segments(poly)
    scale = max(math.cos(math.radians(lat)), 1e-6)
    # 局部平面坐标（经度乘 cos(lat) 使 x/y 同量纲）
    px, py = lon * scale, lat
    x1, y1 = lon1 * scale, lat1
    x2, y2 = lon2 * scale, lat2
    dx, dy = x2 - x1, y2 - y1
    seg2 = dx * dx + dy * dy
    seg2 = np.where(seg2 == 0, 1e-30, seg2)
    t = np.clip(((px - x1) * dx + (py - y1) * dy) / seg2, 0.0, 1.0)
    dist2 = (px - (x1 + t * dx)) ** 2 + (py - (y1 + t * dy)) ** 2
    return bool(np.any(dist2 <= float(tol_deg) ** 2))


def point_in_polygon(lat, lon, polygon, include_boundary=True, boundary_tol_deg=1e-9):
    """判断单点是否在多边形内（射线法，奇偶规则）。

    Parameters
    ----------
    lat, lon : float
        查询点 [deg]。
    polygon : (N,2) array_like
        多边形顶点 (lat, lon) [deg]，可不闭合。
    include_boundary : bool
        点恰好落在边上时是否算"在内部"（默认 True，符合"落点在危险区内"的保守判断）。
    boundary_tol_deg : float
        边界容差 [deg]（默认 1e-9 ≈ 0.1 mm），只用于判定"恰在边上"。

    Returns
    -------
    bool

    Notes
    -----
    射线法对**非自相交**多边形可靠；对自相交/带洞多边形请自行拆分后多次调用。
    水平射线规则用半开区间 ``(lat1 > y) != (lat2 > y)``，避免顶点被重复计数。
    """
    poly = _as_poly(polygon)
    y = float(lat)
    x = float(lon)
    if include_boundary and _point_on_edge(y, x, poly, boundary_tol_deg):
        return True
    lat1, lon1, lat2, lon2 = _segments(poly)
    inside = False
    for la1, lo1, la2, lo2 in zip(lat1, lon1, lat2, lon2):
        if (la1 > y) != (la2 > y):                # 边跨越该纬度
            t = (y - la1) / (la2 - la1)
            x_cross = lo1 + t * (lo2 - lo1)
            if x < x_cross:
                inside = not inside
    return inside


def points_in_polygon(lats, lons, polygon):
    """批量判断点是否在多边形内（向量化的射线法，用于 0.2° 全国网格）。

    Parameters
    ----------
    lats, lons : array_like
        查询点纬度/经度 [deg]，形状需可广播（也接受 ``meshgrid`` 得到的 2-D 数组）。
    polygon : (N,2) array_like
        多边形顶点 (lat, lon) [deg]。

    Returns
    -------
    ndarray of bool
        与 ``np.broadcast(lats, lons)`` 同形状。
    """
    poly = _as_poly(polygon)
    y = np.asarray(lats, dtype=float)
    x = np.asarray(lons, dtype=float)
    y, x = np.broadcast_arrays(y, x)
    inside = np.zeros(y.shape, dtype=bool)
    lat1, lon1, lat2, lon2 = _segments(poly)
    for la1, lo1, la2, lo2 in zip(lat1, lon1, lat2, lon2):
        spans = (la1 > y) != (la2 > y)             # 该边跨越查询点纬度的掩膜
        with np.errstate(divide="ignore", invalid="ignore"):
            t = (y - la1) / np.where((la2 - la1) == 0.0, 1e-30, (la2 - la1))
            x_cross = lo1 + t * (lo2 - lo1)        # 边与水平射线的交点经度
        inside ^= spans & (x < x_cross)
    return inside


def is_on_boundary(lat, lon, polygon, tol_deg=1e-9):
    """点是否在多边形边界上（容差 tol_deg 度）。"""
    return _point_on_edge(float(lat), float(lon), _as_poly(polygon), tol_deg)


# --------------------------------------------------------------------------- #
# 距离（球面）
# --------------------------------------------------------------------------- #
def _lonlat_to_unit(lats, lons):
    """(lat, lon) 度 → 单位球面 3D 矢量 (N,3)。"""
    lat = np.deg2rad(np.asarray(lats, dtype=float))
    lon = np.deg2rad(np.asarray(lons, dtype=float))
    return np.stack([np.cos(lat) * np.cos(lon),
                     np.cos(lat) * np.sin(lon),
                     np.sin(lat)], axis=-1)


def distance_to_polygon_edge_km(lat, lon, polygon):
    """点到多边形**各边（线段）**的最短球面距离 [km]。

    逐边计算"点到球面大圆弧线段"的最短距离：用叉积求点到该大圆的横向距离，
    再判断投影是否落在弧段内，否则取到两端点的较小值。R = 6371.0088 km。

    Parameters
    ----------
    lat, lon : float
        查询点 [deg]。
    polygon : (N,2) array_like
        多边形顶点 (lat, lon) [deg]。

    Returns
    -------
    float
        最短距离 [km]（点在多边形内时也是正的"到边界距离"）。

    Notes
    -----
    球面近似，与 WGS-84 椭球差别 < 0.3%；本任务航警区 ~940 km 处约 1–2 km。
    """
    poly = _as_poly(polygon)
    p = _lonlat_to_unit(lat, lon)
    v1 = _lonlat_to_unit(poly[:, 0], poly[:, 1])
    v2 = _lonlat_to_unit(np.roll(poly[:, 0], -1), np.roll(poly[:, 1], -1))

    # 边所在大圆的法向
    n = np.cross(v1, v2)
    n_norm = np.linalg.norm(n, axis=1)
    # 退化边（两端点重合）→ 只算到端点距离
    degenerate = n_norm < 1e-15
    n_safe = np.where(degenerate[:, None], np.array([[0.0, 0.0, 1.0]]), n)
    n_safe = n_safe / np.linalg.norm(n_safe, axis=1, keepdims=True)

    # 横向距离（到该大圆）的正弦值
    sin_xt = np.clip(p @ n_safe.T, -1.0, 1.0)
    xt = np.arcsin(sin_xt)                                # 有符号横向角 [rad]

    # 投影点（大圆上的垂足）：p_perp = normalize(p - sin_xt * n)
    p_perp = p[None, :] - sin_xt[:, None] * n_safe
    pn = np.linalg.norm(p_perp, axis=1, keepdims=True)
    p_perp = p_perp / np.where(pn < 1e-15, 1.0, pn)

    # 垂足是否在弧段 v1→v2 内：用同一侧判定（叉积符号一致）
    def _same_side(a, b, pt):
        return (np.cross(a, pt) * np.cross(pt, b)).sum(axis=1) >= 0.0

    within = _same_side(v1, v2, p_perp)

    # 端点到查询点的中心角
    ang1 = np.arccos(np.clip(v1 @ p, -1.0, 1.0))
    ang2 = np.arccos(np.clip(v2 @ p, -1.0, 1.0))
    dist_edge = np.abs(xt)
    dist = np.where(within & ~degenerate, dist_edge, np.minimum(ang1, ang2))
    return float(np.min(dist) * EARTH_MEAN_R_KM)


def distance_to_polygon_km(lat, lon, polygon):
    """点到多边形区域的距离 [km]：在多边形**外**为到边界最短距离，在**内**为 0。

    这是"落点是否落在危险区内 / 距边界多远"的判据：

    * 返回值 0 表示点在多边形内部（或边界上）；
    * ``distance_to_polygon_edge_km`` 始终返回正的距离。

    Parameters
    ----------
    lat, lon : float
        查询点 [deg]。
    polygon : (N,2) array_like
        多边形顶点 (lat, lon) [deg]。

    Returns
    -------
    float
        距离 [km]，内部为 0。
    """
    if point_in_polygon(lat, lon, polygon):
        return 0.0
    return distance_to_polygon_edge_km(lat, lon, polygon)


# --------------------------------------------------------------------------- #
# 面积 / 周长 / 质心
# --------------------------------------------------------------------------- #
def polygon_area_km2(polygon):
    """球面多边形面积 [km²]。

    使用球面"梯形积分"公式（Green 定理在球面上的形式）：

    .. math::

        A = \\frac{R^2}{2}\\left|\\sum_i (\\lambda_{i+1}-\\lambda_i)
                          \\,(2 + \\sin\\varphi_i + \\sin\\varphi_{i+1})\\right|

    Parameters
    ----------
    polygon : (N,2) array_like
        多边形顶点 (lat, lon) [deg]，可不闭合（自动首尾相连）。

    Returns
    -------
    float
        面积 [km²]，恒为正。

    Notes
    -----
    对**不跨极、不跨越 ±180° 经线**的普通区域误差 < 0.1%（几个度见方）。
    本任务的航警区（菲律宾以西）满足该条件。
    """
    poly = _as_poly(polygon)
    lat = np.deg2rad(poly[:, 0])
    lon = np.deg2rad(poly[:, 1])
    lat_n = np.roll(lat, -1)
    dlon = wrap180(np.rad2deg(np.roll(lon, -1) - lon)) * math.pi / 180.0
    total = np.sum(dlon * (2.0 + np.sin(lat) + np.sin(lat_n)))
    return float(abs(total) * 0.5 * EARTH_MEAN_R_KM ** 2)


def polygon_perimeter_km(polygon):
    """多边形周长 [km]（球面大圆距离逐边求和）。"""
    poly = _as_poly(polygon)
    lat_n = np.roll(poly[:, 0], -1)
    lon_n = np.roll(poly[:, 1], -1)
    d = great_circle(poly[:, 0], poly[:, 1], lat_n, lon_n)[0]
    return float(np.sum(d))


def polygon_centroid(polygon):
    """多边形球面质心（顶点 3D 单位矢量平均）→ ``(lat, lon)`` [deg]。

    对凸多边形/航警四边形足够精确；对细长凹多边形可能与"视觉中心"略有偏差。

    Returns
    -------
    (lat_deg, lon_deg) : tuple of float
    """
    poly = _as_poly(polygon)
    v = _lonlat_to_unit(poly[:, 0], poly[:, 1])
    c = v.mean(axis=0)
    if np.linalg.norm(c) < 1e-12:
        return float(poly[:, 0].mean()), float(poly[:, 1].mean())
    c = c / np.linalg.norm(c)
    lat = math.degrees(math.asin(max(-1.0, min(1.0, c[2]))))
    lon = math.degrees(math.atan2(c[1], c[0]))
    return float(lat), float(wrap180(lon))


# --------------------------------------------------------------------------- #
# 便捷：从 config.json 读取航警区
# --------------------------------------------------------------------------- #
_DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.json"


def _parse_utc(s):
    """'2026-10-25T01:42:00Z' / '...+00:00' → naive UTC datetime。"""
    if s is None:
        return None
    s = str(s).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def load_zones(config_path=None):
    """读取 ``common/config.json`` 中的航警多边形 → 结构化列表。

    Parameters
    ----------
    config_path : str or Path, optional
        默认 ``common/config.json``。

    Returns
    -------
    list of dict
        每项含：``id``（航警号）、``fir``、``polygon``（(N,2) ndarray, lat/lon）、
        ``b_utc`` / ``c_utc``（naive UTC datetime）、``q_center``（(lat, lon)）、
        ``q_radius_nm``、``area_km2``、``centroid``、``source``。

    Notes
    -----
    本函数是只读便捷入口；若 Lead 已生成 ``sim/notam_zones.json``，sky/viz 模块应优先
    使用该文件（含解析后的映射判断）。
    """
    path = Path(config_path) if config_path is not None else _DEFAULT_CONFIG
    cfg = json.loads(path.read_text(encoding="utf-8"))
    zones = []
    for n in cfg.get("notams", []):
        poly = parse_polygon(n.get("polygon_latlon") or [])
        zones.append({
            "id": n.get("id"),
            "fir": n.get("fir"),
            "polygon": poly,
            "b_utc": _parse_utc(n.get("b_utc")),
            "c_utc": _parse_utc(n.get("c_utc")),
            "q_center": (n.get("q_center_lat"), n.get("q_center_lon")),
            "q_radius_nm": n.get("q_radius_nm"),
            "area_km2": polygon_area_km2(poly) if poly.shape[0] >= 3 else 0.0,
            "centroid": polygon_centroid(poly) if poly.shape[0] >= 3 else (None, None),
            "source": n.get("source"),
        })
    return zones


def zone_active_at(zone, dt_utc):
    """判断航警区在给定 UTC 时刻是否激活（``b_utc <= dt <= c_utc``）。"""
    b, c = zone.get("b_utc"), zone.get("c_utc")
    return bool(b is not None and c is not None and b <= dt_utc <= c)
