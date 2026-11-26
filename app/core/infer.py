# -*- coding: utf-8 -*-
"""推断引擎：把「用户只给了一部分信息」补全成完整配置。

设计原则
--------
1. **每条推断都要留痕**：返回值统一带 ``source``（user/db/infer/default）、
   ``confidence``（高/中/低）与 ``note``，最终写进 ``config.mission.provenance`` 与数据体检表。
2. **不硬编具体任务**：所有型号/场站/城市数据都来自 ``app/data/*.json``；缺失时退化为
   通用模板并明确报警。
3. **尽量用可解释的物理/经验规则**，不"猜一个好看的数字"：
   * T0 未知 → 用航警 B)/C) 与残骸到达时刻的经验关系（一级落区 B) ≈ T0−7…10 min，三个
     历史算例实测）；并在首次仿真后按"落区窗口中心 − 到达时刻"再精化。
   * 方位角未知 → 发射场→一级落区质心的大圆初始方位；无航警时由目标倾角反算。
   * 目标倾角未知 → 由方位角反算（i = arccos(cosφ·sin A)，φ=发射场纬度）。
   * 制导参数未知 → 由箭型类别的锚点 + 一级工作段时长给初值，交给自动反演求解。
   * 观测点高程未知 → 城市库 → DEM（SRTM/粗网格）→ 0 m 兜底。

对外接口见 ``app/INTERFACES.md``。
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .model import ObserverInput, Resolution, fmt_num

try:  # 引擎在 sys.path 上时可用（提供大圆/方位角/质心）
    from common import geo as _geo
    from common import notam as _notam
except Exception:  # noqa: BLE001 - 兜底：不依赖引擎
    _geo = None
    _notam = None

G0 = 9.80665
MU = 3.986004418e14
RE_KM = 6378.137

#: 航警 B) 相对 T0 的经验偏移（秒）。来源：三个历史算例实测
#: CZ-2D 太原 一级落区 B)=T0−7 min；CZ-2F 酒泉 一级区 B)=T0−10 min；CZ-6A 太原 助推区 B)=T0−8 min
T0_OFFSET_BY_HINT = {
    "一级": 7 * 60, "一子级": 7 * 60, "芯一级": 7 * 60,
    "助推器": 8 * 60, "boosters": 8 * 60,
    "整流罩": 8.5 * 60, "二级": 6 * 60, "三级": 6 * 60,
}
T0_OFFSET_DEFAULT = 8 * 60

#: 箭型类别默认值（DB 缺字段时的兜底）
CLASS_DEFAULTS = {
    "small_solid": dict(t_v=6.0, t_p=45.0, pitch_power=1.20, alt_km=500.0, payload_kg=300.0),
    "medium_liquid": dict(t_v=8.0, t_p=165.0, pitch_power=1.05, alt_km=500.0, payload_kg=1200.0),
    "large_liquid": dict(t_v=9.0, t_p=180.0, pitch_power=1.10, alt_km=200.0, payload_kg=6000.0),
    "crewed": dict(t_v=8.5, t_p=160.0, pitch_power=0.97, alt_km=200.0, payload_kg=8000.0),
}

#: 通用模板（DB 完全缺失时兜底，保证程序仍能出量级结果）
_FALLBACK_ROCKETS = {
    "generic_medium_liquid": {
        "id": "generic_medium_liquid", "name": "通用中型液体火箭（模板）",
        "aliases": ["通用", "模板", "generic"], "class": "medium_liquid",
        "liftoff_mass_kg": 250000.0, "leo_kg": 3000.0, "sso_kg": 1300.0,
        "default_payload_kg": 1000.0,
        "typical_orbits": [{"name": "SSO 500", "alt_km": 500.0, "i_deg": 97.4}],
        "launch_sites": [], "confidence": "低",
        "note": "内置通用模板：按 250 t 级两级液体火箭量级构造，仅用于在缺少箭型数据时给出量级结果。",
        "sources": [],
        "guidance_defaults": {"t_v": 8.0, "t_p": 165.0, "pitch_power": 1.05, "t_fairing": 190.0,
                              "fairing_divert_mps": 4.0, "plane_keeping": False, "yaw_tau": 150.0,
                              "i_target_deg": None, "azimuth_deg": None},
        "vehicle": {
            "adapter_kg": 200.0, "payload_kg": 1000.0, "ascent_cda_scale": 0.8,
            "stages": [
                {"name": "一级", "dry_kg": 12000.0, "prop_kg": 190000.0, "engines": 4,
                 "thrust_sl_n": 740000.0, "thrust_vac_n": 820000.0,
                 "isp_sl_s": 260.0, "isp_vac_s": 292.0, "pitch_end_deg": 25.0,
                 "cda_attached_m2": 11.0},
                {"name": "二级", "dry_kg": 4000.0, "prop_kg": 40000.0,
                 "pitch_end_deg": -5.0, "pitch_duration_s": 400.0, "seco_met": None,
                 "cda_attached_m2": 9.0,
                 "phases": [{"name": "主机", "thrusters": [
                     {"n": 1, "thrust_vac_n": 700000.0, "isp_vac_s": 300.0}]}]},
            ],
            "boosters": None,
            "fairing": {"mass_kg": 1800.0, "halves": 2, "sep_met": 190.0, "cda_m2": 13.0},
            "debris": {"stage_0": {"cda": 7.0, "extra_mass_kg": 1200.0},
                       "stage_1": {"cda": 5.0, "extra_mass_kg": 0.0},
                       "fairing": {"cda": 7.0, "extra_mass_kg": 0.0}},
        },
    },
    "generic_small_solid": {
        "id": "generic_small_solid", "name": "通用小型固体火箭（模板）",
        "aliases": ["固体", "小火箭"], "class": "small_solid",
        "liftoff_mass_kg": 40000.0, "leo_kg": 500.0, "sso_kg": 300.0,
        "default_payload_kg": 200.0,
        "typical_orbits": [{"name": "SSO 500", "alt_km": 500.0, "i_deg": 97.4}],
        "launch_sites": [], "confidence": "低",
        "note": "内置通用模板：40 t 级固体小火箭量级，仅用于缺数据时给出量级结果。",
        "sources": [],
        "guidance_defaults": {"t_v": 6.0, "t_p": 50.0, "pitch_power": 1.2, "t_fairing": 90.0,
                              "fairing_divert_mps": 0.0, "plane_keeping": False, "yaw_tau": 150.0,
                              "i_target_deg": None, "azimuth_deg": None},
        "vehicle": {
            "adapter_kg": 30.0, "payload_kg": 200.0, "ascent_cda_scale": 1.0,
            "stages": [
                {"name": "一级", "dry_kg": 3500.0, "prop_kg": 35000.0, "engines": 1,
                 "thrust_sl_n": 800000.0, "thrust_vac_n": 880000.0,
                 "isp_sl_s": 240.0, "isp_vac_s": 260.0, "pitch_end_deg": 30.0,
                 "cda_attached_m2": 8.0},
                {"name": "二级", "dry_kg": 800.0, "prop_kg": 3000.0,
                 "pitch_end_deg": 0.0, "pitch_duration_s": 200.0, "seco_met": None,
                 "cda_attached_m2": 5.0,
                 "phases": [{"name": "主机", "thrusters": [
                     {"n": 1, "thrust_vac_n": 60000.0, "isp_vac_s": 290.0}]}]},
            ],
            "boosters": None,
            "fairing": None,
            "debris": {"stage_0": {"cda": 5.0, "extra_mass_kg": 0.0},
                       "stage_1": {"cda": 3.0, "extra_mass_kg": 0.0}},
        },
    },
}


# --------------------------------------------------------------------------- #
# 数据库
# --------------------------------------------------------------------------- #
_DB_CACHE: Dict[str, Dict[str, Any]] = {}
_APP_DATA = None


def _app_data_dir():
    global _APP_DATA
    if _APP_DATA is None:
        try:
            from . import paths as _P_mod
            _APP_DATA = Path(os.environ.get("RK_APP_DATA") or _P_mod.APP_DATA)
        except Exception:  # noqa: BLE001
            _APP_DATA = Path(__file__).resolve().parents[1] / "data"
    return _APP_DATA


def load_db(name: str) -> Dict[str, Any]:
    """读取 ``app/data/<name>.json``（带缓存）；不存在返回空骨架。"""
    if name in _DB_CACHE:
        return _DB_CACHE[name]
    p = _app_data_dir() / (name if name.endswith(".json") else name + ".json")
    data: Dict[str, Any] = {}
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        data = {}
    _DB_CACHE[name] = data
    return data


def rocket_list() -> List[Dict[str, Any]]:
    """内置箭型库 + 用户自定义箭型库（v4：用户条目优先，同 id 覆盖内置）。"""
    rs = load_db("rockets").get("rockets") or []
    if not rs:
        rs = list(_FALLBACK_ROCKETS.values())
    try:
        from . import userdb as _U
        rs = _U.merge_rockets(list(rs))
    except Exception:  # noqa: BLE001 - 用户库坏了不能影响内置库可用
        pass
    return rs


def site_list() -> List[Dict[str, Any]]:
    return load_db("launch_sites").get("sites") or []


def city_list() -> List[Dict[str, Any]]:
    return load_db("cities").get("cities") or []


def payload_list() -> List[Dict[str, Any]]:
    return load_db("payloads").get("payloads") or []


def orbit_list() -> List[Dict[str, Any]]:
    """内置参考轨道库 + 用户自定义轨道（v4）。"""
    rs = load_db("orbits").get("orbits") or []
    try:
        from . import userdb as _U
        rs = _U.merge_orbits(list(rs))
    except Exception:  # noqa: BLE001
        pass
    return rs


def class_key_of(rocket: Optional[Dict[str, Any]]) -> str:
    """箭型类别键（small_solid / medium_liquid / large_liquid / crewed）。

    v3 的 ``CLASS_DEFAULTS`` 用英文键，但库里的 ``class`` 全是中文长句，
    因此 ``CLASS_DEFAULTS.get(cls)`` 从来只命中兜底项——量级锚点等于没用上。
    v4 统一走这里：优先 ``class_key``，否则按中文关键词/起飞质量归类。
    """
    try:
        from . import userdb as _U
        return _U.class_key_of(rocket or {})
    except Exception:  # noqa: BLE001
        cls = str((rocket or {}).get("class") or "")
        if "载人" in cls:
            return "crewed"
        if "固体" in cls:
            return "small_solid"
        if "大型" in cls:
            return "large_liquid"
        return "medium_liquid"



# --------------------------------------------------------------------------- #
# 名称归一化与匹配
# --------------------------------------------------------------------------- #
def norm_name(s: Any) -> str:
    """型号/地名归一化：小写、去空白与常见分隔符、全角转半角。"""
    if s is None:
        return ""
    t = str(s).strip().lower()
    trans = str.maketrans({"（": "(", "）": ")", "　": " ", "—": "-", "－": "-",
                           "／": "/", "、": " ", "，": " ", "。": " "})
    t = t.translate(trans)
    t = re.sub(r"[\s\-_·•/\\()\[\]（）:：,，.。;；]+", "", t)
    return t


def _iter_aliases(item: Dict[str, Any], keys=("id", "name")):
    out = []
    for k in keys:
        if item.get(k):
            out.append(item[k])
    out.extend(item.get("aliases") or [])
    return out


def _match_item(items: List[Dict[str, Any]], label: str,
                extra_keys=("id", "name")) -> Tuple[Optional[Dict[str, Any]], str]:
    """在列表中按 id/name/aliases 精确 → 包含 → 分词全含 匹配，返回 ``(item, 说明)``。"""
    if not label:
        return None, ""
    q = norm_name(label)
    if not q:
        return None, ""
    for it in items:
        for a in _iter_aliases(it, extra_keys):
            if norm_name(a) == q:
                return it, "精确匹配「%s」" % a
    # 包含匹配（取最长匹配，避免 "长征二号" 命中 "长征二号丁" 与 "长征二号F"）
    best, best_len, best_alias = None, 0, ""
    for it in items:
        for a in _iter_aliases(it, extra_keys):
            na = norm_name(a)
            if not na:
                continue
            if na in q or q in na:
                if len(na) > best_len:
                    best, best_len, best_alias = it, len(na), a
    if best is not None:
        return best, "模糊匹配「%s」" % best_alias
    # 分词全含匹配（"太原 LC-9" → "太原" + "lc9" 都在别名里）
    toks = [norm_name(t) for t in re.split(r"[\s,，、/()（）\[\]\-_]+", str(label))
            if t.strip()]
    toks = [t for t in toks if len(t) >= 2]
    if toks:
        scored: List[Tuple[int, Dict[str, Any], str]] = []
        for it in items:
            for a in _iter_aliases(it, extra_keys):
                na = norm_name(a)
                if not na:
                    continue
                if all(t in na for t in toks):
                    scored.append((sum(len(t) for t in toks), it, a))
        if scored:
            scored.sort(key=lambda x: -x[0])
            if len(scored) > 1 and scored[0][0] == scored[1][0] and scored[0][1] is not scored[1][1]:
                return scored[0][1], "多种匹配可能（另含「%s」），已取「%s」" % (
                    scored[1][2], scored[0][2])
            return scored[0][1], "分词匹配「%s」" % scored[0][2]
    return None, ""


def match_rocket(label: str) -> Tuple[Optional[Dict[str, Any]], str]:
    return _match_item(rocket_list(), label)


#: 中国发射场区关键词（用于"只给场区名、不给工位号"时取中心代表点）
SITE_GROUP_KEYS = ("太原", "酒泉", "西昌", "文昌", "海阳", "东方航天港",
                   "海南商业", "宁波", "日照", "南海", "黄海")


def _site_group(name: str) -> str:
    nn = norm_name(name)
    for key in SITE_GROUP_KEYS:
        if norm_name(key) in nn:
            return key
    return ""


def match_site(label: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """发射场匹配：精确/别名 → 模糊 → **场区中心代表点回退**。

    中心代表点：只写"太原"/"酒泉"这类场区名时，取该场区全部内置工位经纬度/海拔的平均值，
    confidence=中，note 明确提示"不代表具体工位，建议写全工位号"。
    """
    q = norm_name(label)
    if not q:
        return None, ""
    # 0) 只写了场区名（与场区关键词完全一致）→ 直接用场区中心代表点。
    #    必须先于"模糊包含匹配"，否则"太原"会被最长别名吸到某个具体工位（LC-9A）上。
    _group_names = {norm_name(k): k for k in SITE_GROUP_KEYS}
    if q in _group_names:
        center = _site_center(_group_names[q])
        if center is not None:
            return center, "场区中心回退（%d 个工位平均）" % center.pop("_n_sites", 0)
    it, why = _match_item(site_list(), label)
    if it is not None:
        return it, why
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for s in site_list():
        key = _site_group(str(s.get("name", "")))
        if key:
            groups.setdefault(key, []).append(s)
    for key, items in groups.items():
        if norm_name(key) in q:
            center = _site_center(key)
            if center is not None:
                return center, "场区中心回退（%d 个工位平均）" % center.pop("_n_sites", 0)
    return None, ""


def _site_center(key: str) -> Optional[Dict[str, Any]]:
    """场区中心代表点：该场区全部内置工位的经纬度/海拔平均（不代表具体工位）。"""
    items = [s for s in site_list() if _site_group(str(s.get("name", ""))) == key]
    lats = [float(s["lat_deg"]) for s in items if s.get("lat_deg") is not None]
    lons = [float(s["lon_deg"]) for s in items if s.get("lon_deg") is not None]
    if not lats or not lons:
        return None
    alts = [float(s.get("alt_m_ellipsoid") or 0.0) for s in items]
    return dict(
        id="center_%s" % key,
        name="%s（场区中心代表点）" % key,
        aliases=[], lat_deg=round(sum(lats) / len(lats), 6),
        lon_deg=round(sum(lons) / len(lons), 6),
        alt_m_ellipsoid=round(sum(alts) / len(alts), 1),
        country="CN", region="", vehicles=[], confidence="中",
        coord_note="按该场区 %d 个内置工位坐标平均得到的代表点，**不代表具体工位**；"
                   "如需精确落点请写全工位号（如「%s LC-9」）" % (len(lats), key),
        _n_sites=len(lats),
    )


def match_city(name: str) -> Tuple[Optional[Dict[str, Any]], str]:
    return _match_item(city_list(), name)


def match_payload(name: str) -> Tuple[Optional[Dict[str, Any]], str]:
    return _match_item(payload_list(), name)


def match_orbit(name: str) -> Tuple[Optional[Dict[str, Any]], str]:
    return _match_item(orbit_list(), name)


def nearest_site(lat: float, lon: float, max_km: float = 30.0) -> Optional[Dict[str, Any]]:
    best, best_d = None, 1e9
    for s in site_list():
        if s.get("lat_deg") is None or s.get("lon_deg") is None:
            continue
        d = great_circle_km(lat, lon, s["lat_deg"], s["lon_deg"])
        if d < best_d:
            best, best_d = s, d
    if best is not None and best_d <= max_km:
        return best
    return None


# --------------------------------------------------------------------------- #
# 几何小工具（不依赖引擎也能算）
# --------------------------------------------------------------------------- #
def great_circle_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    if _geo is not None:
        try:
            return float(_geo.great_circle_distance_km(lat1, lon1, lat2, lon2))
        except Exception:  # noqa: BLE001
            pass
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    dp, dl = p2 - p1, l2 - l1
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))


def initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """起点→终点的初始方位角（自北顺时针，0–360）。"""
    if _geo is not None:
        try:
            return float(_geo.initial_bearing_deg(lat1, lon1, lat2, lon2)) % 360.0
        except Exception:  # noqa: BLE001
            pass
    p1, p2, dl = map(math.radians, (lat1, lat2, lon2 - lon1))
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def polygon_centroid(poly: List[List[float]]) -> Tuple[float, float]:
    if _notam is not None:
        try:
            import numpy as np
            return _notam.polygon_centroid(np.asarray(poly, dtype=float))
        except Exception:  # noqa: BLE001
            pass
    lat = sum(p[0] for p in poly) / len(poly)
    lon = sum(p[1] for p in poly) / len(poly)
    return lat, lon


def zone_centroid(z: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    poly = z.get("polygon_latlon") or []
    if len(poly) >= 3:
        try:
            return polygon_centroid(poly)
        except Exception:  # noqa: BLE001
            return None
    return None


def orbit_from_azimuth(az_deg: float, site_lat_deg: float) -> float:
    """由发射方位角与发射场纬度估算入轨倾角：cos i = cosφ · sin A（水平起飞近似）。"""
    v = math.cos(math.radians(site_lat_deg)) * math.sin(math.radians(az_deg))
    return math.degrees(math.acos(max(-1.0, min(1.0, v))))


def azimuth_from_orbit(i_deg: float, site_lat_deg: float) -> float:
    """由入轨倾角与发射场纬度反算发射方位角（南向/北向取与倾角一致的一支）。"""
    cosphi = max(0.05, math.cos(math.radians(site_lat_deg)))
    s = max(-1.0, min(1.0, math.cos(math.radians(i_deg)) / cosphi))
    a1 = math.degrees(math.asin(s))          # 0..90（东北）
    a2 = 180.0 - a1                          # 90..180（东南）
    # i > 纬度 → 东北向；i < 纬度 → 东南向；i ≳ 90（极轨/逆行）→ 南向（>180）
    if i_deg > 90.0:
        s2 = max(-1.0, min(1.0, math.cos(math.radians(i_deg)) / cosphi))
        return (180.0 + math.degrees(math.asin(-s2))) % 360.0
    return a1 if i_deg >= site_lat_deg else a2


# --------------------------------------------------------------------------- #
# T0 推断
# --------------------------------------------------------------------------- #
def _hint_offset_s(hint: str) -> Tuple[int, bool]:
    h = norm_name(hint)
    for k, v in T0_OFFSET_BY_HINT.items():
        if norm_name(k) and norm_name(k) in h:
            return v, True
    return T0_OFFSET_DEFAULT, False


def _iso_to_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        t = str(s).strip()
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:  # noqa: BLE001
        return None


def infer_t0_from_notams(notams: List[Dict[str, Any]]) -> Tuple[Optional[datetime], str, str, str]:
    """由航警 B)/C) 推断 T0。

    返回 ``(t0, source, confidence, note)``。规则：取"最像一级/助推器落区"的那条
    （hint 命中优先，否则 B) 最早的一条），T0 = B) + 经验偏移。
    """
    cands = []
    for z in notams:
        b = _iso_to_dt(z.get("b_utc", ""))
        c = _iso_to_dt(z.get("c_utc", ""))
        if b is None:
            continue
        off, hit = _hint_offset_s(z.get("stage_hint", ""))
        cands.append((hit, b, c, off, z))
    if not cands:
        return None, "", "低", "航警中没有可用的 B) 生效时刻，无法推断 T0"
    hit_cands = [c for c in cands if c[0]]
    pool = hit_cands or cands
    # 一级/助推器落区的 B) 最早（整流罩/二级区通常略晚）
    pool.sort(key=lambda c: (c[1], -c[3]))
    hit, b, c, off, z = pool[0]
    t0 = b + timedelta(seconds=off)
    conf = "中" if hit else "低"
    note = "由航警 %s 的 B)=%s 推 %s 后 T0（经验偏移 %d min：一级落区 B)≈T0−7~10 min，" \
           "来自 CZ-2D/CZ-2F/CZ-6A 三个历史算例）；首次仿真后会按落区窗口中心再精化" % (
               z.get("id", "?"), b.strftime("%Y-%m-%d %H:%M UTC"), "%d min" % (off // 60),
               int(off // 60))
    return t0, "infer", conf, note


def refine_t0_from_window(t0_prov: datetime, arrival_met_s: float,
                          zone: Dict[str, Any]) -> Tuple[datetime, str]:
    """用落区窗口自洽性校核/微调 T0。

    规则（比"直接取窗口中心"稳健）：
    * 若 ``T0_prov + 到达时刻`` 已落在 [B+margin, C−margin] 内 → **保持** T0
      （说明 B)+经验偏移的初值自洽，历史算例就是这种情况）；
    * 若落在窗口外 → 只做"最小平移"，把到达时刻推到最近边界内 margin 秒处。
    """
    b = _iso_to_dt(zone.get("b_utc", ""))
    c = _iso_to_dt(zone.get("c_utc", ""))
    if b is None or c is None:
        return t0_prov, "航警时间不完整，未精化 T0"
    if c <= b:
        return t0_prov, "航警 C) 不晚于 B)，未精化 T0"
    margin = min(60.0, max(10.0, (c - b).total_seconds() * 0.1))
    arrival_abs = t0_prov + timedelta(seconds=float(arrival_met_s))
    lo = b + timedelta(seconds=margin)
    hi = c - timedelta(seconds=margin)
    if lo <= arrival_abs <= hi:
        return t0_prov, ("初次仿真到达时刻 %s 已落在 %s 窗口 [%s, %s] 内（距开始 %+.1f min、"
                         "距结束 %+.1f min），保持 T0 不变" % (
                             arrival_abs.strftime("%m-%d %H:%M:%S UTC"), zone.get("id", "?"),
                             b.strftime("%H:%M:%S"), c.strftime("%H:%M:%S"),
                             (arrival_abs - b).total_seconds() / 60.0,
                             (arrival_abs - c).total_seconds() / 60.0))
    target = lo if arrival_abs < lo else hi
    t0_new = t0_prov + (target - arrival_abs)
    return t0_new, ("初次仿真到达时刻 %s 在 %s 窗口外 → T0 平移 %+.1f min 使到达时刻 %s 落入窗口" % (
        arrival_abs.strftime("%m-%d %H:%M:%S UTC"), zone.get("id", "?"),
        (t0_new - t0_prov).total_seconds() / 60.0, target.strftime("%m-%d %H:%M:%S UTC")))


def infer_t0_from_window_text(text: str, ref_year: Optional[int] = None):
    """从"发射窗口 12:53–13:15"这类文本推断 T0（取窗口中心）。返回 (dt|None, note)。"""
    from . import notam_parse as NP
    s = str(text or "")
    m = re.search(r"(\d{1,2}:\d{2})\s*[-–~至到]\s*(\d{1,2}:\d{2})", s)
    if not m:
        return None, ""
    t1, t2 = m.group(1), m.group(2)
    d = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    base = "%s-%s-%s " % (d.group(1), d.group(2), d.group(3)) if d else ""
    a, _, _ = NP.parse_t0_text(base + t1, "auto", ref_year)
    b, _, _ = NP.parse_t0_text(base + t2, "auto", ref_year)
    if a is None or b is None:
        return None, ""
    if b < a:
        b += timedelta(days=1)
    t0 = a + (b - a) / 2
    return t0, "由发射窗口 %s–%s 取中心" % (t1, t2)


# --------------------------------------------------------------------------- #
# 方位角 / 参考轨道
# --------------------------------------------------------------------------- #
def stage_like_zones(notams: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """挑出"一级/助推器"类落区（方位角基准）。"""
    out = []
    for z in notams:
        h = norm_name(z.get("stage_hint", ""))
        if any(norm_name(k) in h for k in ("一级", "一子级", "芯一级", "助推器")):
            out.append(z)
    return out


def guess_azimuth(site: Dict[str, Any], notams: List[Dict[str, Any]],
                  rocket: Optional[Dict[str, Any]] = None,
                  ref_orbit: Optional[Dict[str, Any]] = None
                  ) -> Tuple[Optional[float], str, str, str]:
    """初值方位角：优先发射场→一级落区质心；否则由目标倾角反算。"""
    if site.get("lat_deg") is None:
        return None, "", "低", "缺发射场坐标"
    pool = stage_like_zones(notams) or notams
    best = None
    for z in pool:
        cen = zone_centroid(z)
        if not cen:
            continue
        d = great_circle_km(site["lat_deg"], site["lon_deg"], cen[0], cen[1])
        if best is None or d > best[0]:      # 取最远的落区（一级/助推区通常最远）
            best = (d, cen, z)
    if best is not None:
        az = initial_bearing_deg(site["lat_deg"], site["lon_deg"], best[1][0], best[1][1]) % 360.0
        note = ("发射场→%s 质心(%.3f, %.3f) 大圆初始方位，距离 %.0f km" %
                (best[2].get("id", "落区"), best[1][0], best[1][1], best[0]))
        return round(az, 3), "infer", "中", note
    i_t = None
    if ref_orbit and ref_orbit.get("i_deg"):
        i_t = float(ref_orbit["i_deg"])
    elif rocket:
        to = (rocket.get("typical_orbits") or [None])[0]
        if to and to.get("i_deg"):
            i_t = float(to["i_deg"])
    if i_t:
        az = azimuth_from_orbit(i_t, site["lat_deg"])
        return round(az, 3), "infer", "低", \
            "无可用落区，由目标倾角 %.2f° 与发射场纬度 %.2f° 反算方位（误差可达数度，建议反演）" % (
                i_t, site["lat_deg"])
    return None, "", "低", "无法推断方位角（既无落区也无目标倾角）"


def inclination_sigma_deg(az_deg: float, site_lat_deg: float,
                          i_deg: float) -> float:
    """「由射向反算倾角」对 1° 方位角误差的敏感度（度/度）。

    i = arccos(cosφ·sinA) ⇒ |di/dA| = cosφ·|cosA| / sin i。

    * 中纬度、通常射向：约 0.5–1.0 °/°；
    * 近赤道面任务（i→0）或近 180° 逆行（sin i→0）：敏感度发散，
      说明"仅凭射向推不出倾角"，这时才应该给低置信度。
    """
    try:
        ph = math.radians(float(site_lat_deg))
        a = math.radians(float(az_deg))
        i = math.radians(max(0.5, min(179.5, float(i_deg))))
        den = math.sin(i)
        if den < 1e-6:
            return 99.0
        return abs(math.cos(ph) * math.cos(a)) / den
    except (TypeError, ValueError):
        return 99.0


def infer_reference_orbit(az: Optional[float], site_lat: Optional[float],
                          rocket: Optional[Dict[str, Any]],
                          user_alt: Optional[float], user_i: Optional[float],
                          az_sigma_deg: float = 2.0
                          ) -> Tuple[Optional[Dict[str, Any]], str, str, str]:
    """参考轨道（高度/倾角）。优先级：用户给定 > 方位角反算&与典型轨道一致 > 箭型典型 > 方位角反算。

    v4.1：置信度不再按"与典型轨道差多少度"一刀切，而是**按射向敏感度定量**：

    * 由 ``i = arccos(cosφ·sinA)`` 与方位角不确定度 ``az_sigma_deg`` 得
      ``σ_i = |di/dA|·σ_A``；
    * 与箭型典型轨道的倾角差 ≤ max(5°, 2σ_i) → 采用典型轨道（中）；
    * 否则采用反算倾角：σ_i ≤ 2.5° → 中（数值可信，另发"与典型轨道不一致"的提示）；
      σ_i > 2.5°（近赤道/近逆行等几何病态）→ 低；
    * 说明文本给出 ``i=…±…``，不再出现"未计侧向机动"（那是模型局限，写在报告 §9）。
    """
    if user_alt is not None and user_i is not None:
        return ({"alt_km": float(user_alt), "i_deg": float(user_i), "confidence": "高"},
                "user", "高", "用户给定高度与倾角")
    tos = [t for t in ((rocket or {}).get("typical_orbits") or [])
           if isinstance(t, dict) and t.get("i_deg") is not None]
    to0 = tos[0] if tos else None
    alt = user_alt if user_alt is not None else (to0.get("alt_km") if to0 else None)
    i_deg = user_i if user_i is not None else (to0.get("i_deg") if to0 else None)
    src, conf, notes = [], "中", []
    if user_alt is not None:
        src.append("user_alt")
    if user_i is not None:
        src.append("user_i")

    i_est: Optional[float] = None
    i_sigma: Optional[float] = None
    mismatch: Optional[float] = None
    near: Optional[Dict[str, Any]] = None
    if user_i is None and az is not None and site_lat is not None:
        i_est = round(orbit_from_azimuth(az, site_lat), 2)
        src.append("az")
        i_sigma = round(inclination_sigma_deg(az, site_lat, i_est) *
                        max(0.0, float(az_sigma_deg)), 2)
        # 与箭型典型轨道比对，取最接近的一条
        near, near_d = None, 1e9
        for t in tos:
            d = abs(float(t["i_deg"]) - i_est)
            if d < near_d:
                near, near_d = t, d
        mismatch = round(near_d, 2) if near is not None else None
        # 采纳典型轨道的门限：5° 或 2σ_i（取小者不超过 8°，避免过宽）
        tol = 5.0
        if i_sigma:
            tol = max(5.0, min(8.0, 2.0 * i_sigma))
        if near is not None and near_d <= tol:
            # 反算值与典型轨道一致 → 采用典型轨道（高度/倾角成套）
            i_deg = float(near["i_deg"])
            if user_alt is None and near.get("alt_km") is not None:
                alt = float(near["alt_km"])
            conf = "中"
            notes.append(
                "方位角 %.1f° 反算倾角 %.2f°（±%.1f°），与箭型典型轨道「%s」的 %.2f° 相差 %.1f°"
                "（≤%.0f°），采用典型轨道值%s" % (
                    az, i_est, i_sigma or 0.0, near.get("name") or "DB",
                    float(near["i_deg"]), near_d, tol,
                    ("（高度 %.0f km）" % float(near["alt_km"]))
                    if (user_alt is None and near.get("alt_km") is not None) else ""))
        else:
            i_deg = i_est
            # v4.1：置信度由"射向敏感度"决定，而不是由"是否像库内典型轨道"决定
            conf = "低" if (i_sigma is None or i_sigma > 2.5) else "中"
            notes.append(
                "倾角由发射方位角 %.1f° 与场址纬度 %.1f° 反算：i=%.2f°±%.1f°"
                "（i=arccos(cosφ·sinA)，按射向不确定度 %.1f° 传播）"
                % (az, site_lat, i_est, i_sigma or 0.0, float(az_sigma_deg)))
            if near is not None:
                notes.append(
                    "与库内最近典型轨道「%s」（%.2f°）相差 %.1f°：可能是特殊射向/新任务，"
                    "或该典型轨道不属于本次任务；如需锁定请显式选择目标轨道"
                    % (near.get("name") or "DB", float(near["i_deg"]), near_d))
    if alt is None:
        cls = class_key_of(rocket)
        alt = CLASS_DEFAULTS.get(cls, CLASS_DEFAULTS["medium_liquid"])["alt_km"]
        notes.append("高度按箭型量级取 %.0f km" % alt)
        conf = "低"
    if alt is None or i_deg is None:
        return None, "", "低", "参考轨道信息不足，已忽略轨道软约束"
    if not notes:
        notes.append("箭型典型轨道（%s）" % (to0.get("name") if to0 else "DB"))
    out = {"alt_km": float(alt), "i_deg": float(i_deg), "confidence": conf}
    # 供上层（config_build / 体检 / 报告）使用的推断细节，不影响引擎
    if i_est is not None:
        out["i_est_deg"] = float(i_est)
        out["i_sigma_deg"] = float(i_sigma or 0.0)
    if mismatch is not None:
        out["mismatch_deg"] = float(mismatch)
        if near is not None:
            out["nearest_template"] = {"name": near.get("name"),
                                       "i_deg": float(near["i_deg"])}
    return (out, "infer" if ("az" in src or not src) else "db", conf, "；".join(notes))


# --------------------------------------------------------------------------- #
# v4：用户自定义参考轨道（模板 / 用户库 / 手填 / TLE）
# --------------------------------------------------------------------------- #
def parse_tle_elements(text: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """从两行 TLE（平均根数）里取近圆轨道的参考要素。

    不做 SGP4 传播——这里只需要"参考轨道"：高度（由平均运动反算半长轴）、倾角、
    偏心率、RAAN、近地点幅角。返回 ``(orbit_dict|None, 说明)``。
    """
    lines = [ln.rstrip() for ln in str(text or "").splitlines() if ln.strip()]
    l2 = next((ln for ln in lines if ln.startswith("2 ")), "")
    if not l2:
        return None, "没有找到以「2 」开头的 TLE 第二行"
    try:
        inc = float(l2[8:16])
        raan = float(l2[17:25])
        ecc = float("0." + l2[26:33].strip())
        argp = float(l2[34:42])
        mm = float(l2[52:63])           # rev/day
    except (ValueError, IndexError) as exc:
        return None, "TLE 第二行格式无法解析：%s" % exc
    if mm <= 0:
        return None, "TLE 平均运动 ≤ 0，无法反算半长轴"
    n = mm * 2.0 * math.pi / 86400.0    # rad/s
    a = (MU / (n * n)) ** (1.0 / 3.0) / 1000.0   # km
    alt = a - RE_KM
    name = ""
    if lines and not lines[0].startswith(("1 ", "2 ")):
        name = lines[0].strip()
    return ({"alt_km": round(float(alt), 1), "i_deg": round(inc, 4),
             "e": round(ecc, 6), "raan_deg": round(raan, 4),
             "argp_deg": round(argp, 4), "type": "TLE",
             "name": name or "来自 TLE 的参考轨道",
             "note": "由 TLE 平均根数反算（a=%.1f km）；TLE 历元数天后沿迹误差可达数度"
                     % a},
            "TLE：h=%.1f km, i=%.3f°, e=%.5f" % (alt, inc, ecc))


def resolve_user_reference_orbit(mode: str, name: str,
                                 custom: Optional[Dict[str, Any]],
                                 tle_text: str = "",
                                 user_orbits: Optional[List[Dict[str, Any]]] = None
                                 ) -> Tuple[Optional[Dict[str, Any]], str, str, str]:
    """把 v4 的「参考轨道来源」解析成统一的参考轨道字典。

    返回 ``(orbit, source, confidence, note)``；``orbit`` 为空表示交给自动推断。
    统一字段：``alt_km / i_deg / e / raan_deg / argp_deg / ltan_h / name / type``。
    """
    mode = str(mode or "auto").lower()
    if mode in ("", "auto"):
        return None, "", "", ""
    if mode == "tle":
        orb, note = parse_tle_elements(tle_text)
        if not orb:
            return None, "infer", "低", "TLE 解析失败（%s），已回退自动推断" % note
        return orb, "user", "中", note
    if mode == "custom":
        c = dict(custom or {})
        try:
            alt = float(c["alt_km"]) if c.get("alt_km") is not None else None
        except (TypeError, ValueError):
            alt = None
        try:
            inc = float(c["i_deg"]) if c.get("i_deg") is not None else None
        except (TypeError, ValueError):
            inc = None
        if alt is None and inc is None:
            return None, "infer", "低", "手填参考轨道既无高度也无倾角，已回退自动推断"
        orb = {"alt_km": alt, "i_deg": inc,
               "e": (float(c["e"]) if c.get("e") not in (None, "") else 0.0),
               "raan_deg": (float(c["raan_deg"]) if c.get("raan_deg") not in (None, "")
                            else None),
               "argp_deg": (float(c["argp_deg"]) if c.get("argp_deg") not in (None, "")
                            else None),
               "ltan_h": (float(c["ltan_h"]) if c.get("ltan_h") not in (None, "")
                          else None),
               "epoch_utc": str(c.get("epoch_utc") or ""),
               "name": str(c.get("name") or "手填参考轨道"),
               "type": str(c.get("type") or "CUSTOM")}
        note = "用户手填参考轨道" + ("（h=%s km, i=%s°）" % (
            ("%.0f" % alt) if alt is not None else "—",
            ("%.2f" % inc) if inc is not None else "—"))
        return orb, "user", "高", note
    if mode in ("template", "user"):
        pool = list(user_orbits) if (mode == "user" and user_orbits is not None) \
            else orbit_list()
        label = str(name or "").strip()
        item = None
        if label:
            item, why = _match_item(pool, label)
        if item is None and pool:
            return None, "infer", "低", "参考轨道名称「%s」在库中未找到，已回退自动推断" % label
        if item is None:
            return None, "infer", "低", "参考轨道库为空，已回退自动推断"
        orb = {"alt_km": (float(item["alt_km"]) if item.get("alt_km") is not None else None),
               "i_deg": (float(item["i_deg"]) if item.get("i_deg") is not None else None),
               "e": float(item.get("e") or 0.0),
               "raan_deg": (float(item["raan_deg"]) if item.get("raan_deg") is not None
                            else None),
               "argp_deg": (float(item["argp_deg"]) if item.get("argp_deg") is not None
                            else None),
               "ltan_h": (float(item["ltan_h"]) if item.get("ltan_h") is not None else None),
               "epoch_utc": str(item.get("epoch_utc") or ""),
               "name": str(item.get("name") or label),
               "type": str(item.get("type") or "LEO")}
        conf = str(item.get("confidence") or ("高" if mode == "user" else "中"))
        src = "user" if (mode == "user" or item.get("_user")) else "db"
        note = "%s「%s」：h=%s km, i=%s°%s" % (
            "用户轨道库" if src == "user" else "内置轨道模板", orb["name"],
            ("%.0f" % orb["alt_km"]) if orb["alt_km"] is not None else "—",
            ("%.2f" % orb["i_deg"]) if orb["i_deg"] is not None else "—",
            ("，e=%.4f" % orb["e"]) if orb.get("e") else "")
        return orb, src, conf, note
    return None, "", "", ""


# --------------------------------------------------------------------------- #
# 箭型 / 制导
# --------------------------------------------------------------------------- #
def estimate_burn_s(veh: Dict[str, Any]) -> Dict[str, float]:
    """估算各级/助推器工作段时长 [s]（用于自动反演边界）。"""
    out: Dict[str, float] = {}
    for i, st in enumerate(veh.get("stages") or []):
        flow = 0.0
        if st.get("phases"):
            for ph in st["phases"]:
                for t in ph.get("thrusters") or []:
                    if t.get("thrust_vac_n") and t.get("isp_vac_s"):
                        flow += float(t.get("n") or 1) * float(t["thrust_vac_n"]) / (
                            float(t["isp_vac_s"]) * G0)
        else:
            if st.get("thrust_vac_n") and st.get("isp_vac_s"):
                flow = float(st.get("engines") or 1) * float(st["thrust_vac_n"]) / (
                    float(st["isp_vac_s"]) * G0)
        prop = float(st.get("prop_kg") or 0.0)
        out["stage_%d" % i] = prop / flow if flow > 0 else 0.0
    bo = veh.get("boosters")
    if bo:
        flow = 0.0
        for t in bo.get("thrusters") or []:
            thr = float(t.get("thrust_vac_n") or t.get("thrust_sl_n") or 0.0)
            isp = float(t.get("isp_vac_s") or t.get("isp_sl_s") or 0.0)
            if thr and isp:
                flow += float(t.get("n") or 1) * thr / (isp * G0)
        flow *= float(bo.get("count") or 1)
        prop = float(bo.get("prop_kg_each") or 0.0) * float(bo.get("count") or 1)
        out["boosters"] = prop / flow if flow > 0 else 0.0
    return out


def build_vehicle(rocket: Dict[str, Any], payload_kg: Optional[float],
                  overrides: Optional[Dict[str, Any]] = None
                  ) -> Tuple[Dict[str, Any], List[Resolution], List[str]]:
    """由箭型预设 + 载荷 + 用户覆盖生成 config.vehicle。"""
    res: List[Resolution] = []
    warns: List[str] = []
    veh = copy.deepcopy(rocket.get("vehicle") or {})
    if not veh.get("stages"):
        raise ValueError("箭型数据缺少 stages，无法仿真")
    # 载荷质量
    if payload_kg is None:
        payload_kg = rocket.get("default_payload_kg")
        if payload_kg is None:
            cls = class_key_of(rocket)
            payload_kg = CLASS_DEFAULTS.get(cls, CLASS_DEFAULTS["medium_liquid"])["payload_kg"]
            res.append(Resolution("vehicle.payload_kg", "载荷质量", "%s kg" % fmt_num(payload_kg),
                                  "infer", "低", "未给载荷质量，按箭型量级估 %s kg" % fmt_num(payload_kg)))
        else:
            res.append(Resolution("vehicle.payload_kg", "载荷质量", "%s kg" % fmt_num(payload_kg),
                                  "db", "中", "取内置库典型载荷质量"))
    else:
        res.append(Resolution("vehicle.payload_kg", "载荷质量", "%s kg" % fmt_num(payload_kg),
                              "user", "高", "用户给定"))
    veh["payload_kg"] = float(payload_kg)
    veh.setdefault("adapter_kg", 0.0)
    veh.setdefault("ascent_cda_scale", 1.0)

    # 必备字段补齐 + 校验
    for i, st in enumerate(veh["stages"]):
        name = st.get("name") or ("第%d级" % (i + 1))
        st["name"] = name
        for k in ("dry_kg", "prop_kg"):
            if st.get(k) is None:
                raise ValueError("第%s 级缺少 %s，箭型数据不完整" % (name, k))
        st.setdefault("pitch_end_deg", 0.0)
        # 注意：不要给 cda_attached_m2 补默认值——引擎缺省按 0.0 处理，
        # 擅自补 10 m² 会显著改变气动阻力与入轨半长轴（实测可达上百 km）。
    if veh.get("fairing") is None and class_key_of(rocket) in ("medium_liquid", "large_liquid"):
        warns.append("箭型无整流罩参数：按无整流罩处理（近地轨道载人/货船常见）")
    if veh.get("debris") is None:
        veh["debris"] = {}
    for key in ["stage_%d" % i for i in range(max(1, len(veh["stages"]) - 1))]:
        veh["debris"].setdefault(key, {"cda": 7.0, "extra_mass_kg": 0.0})
    if veh.get("fairing"):
        veh["debris"].setdefault("fairing", {"cda": 7.0, "extra_mass_kg": 0.0})
    if veh.get("boosters"):
        veh["debris"].setdefault("boosters", {"cda": 8.0, "extra_mass_kg": 0.0,
                                              "divert_mps": 4.0})
        if veh["boosters"].get("jettison_met") is None:
            warns.append("助推器分离时刻未知（按推进剂耗尽/事件自动判断）")

    # 用户覆盖（点号路径）
    for path, val in (overrides or {}).items():
        if val is None:
            continue
        try:
            _set_path(veh, path, val)
            res.append(Resolution("vehicle." + path, "用户覆盖", fmt_num(val), "user", "高",
                                  "用户手工指定"))
        except Exception as exc:  # noqa: BLE001
            warns.append("覆盖 %s 失败：%s" % (path, exc))
    return veh, res, warns


def _set_path(d: Dict[str, Any], path: str, value: Any) -> None:
    keys = [k for k in str(path).split(".") if k != ""]
    cur: Any = d
    for k in keys[:-1]:
        if isinstance(cur, list):
            cur = cur[int(k)]
        else:
            if k not in cur:
                cur[k] = {}
            cur = cur[k]
    last = keys[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value


def get_path(d: Any, path: str) -> Any:
    cur = d
    for k in str(path).split("."):
        if isinstance(cur, list):
            cur = cur[int(k)]
        else:
            cur = cur[k]
    return cur


def build_guidance(rocket: Dict[str, Any], vehicle: Dict[str, Any],
                   az: Optional[float], numerics_mode: str = "accurate",
                   ref_orbit: Optional[Dict[str, Any]] = None,
                   site_lat: Optional[float] = None
                   ) -> Tuple[Dict[str, Any], List[Resolution]]:
    """生成 config.guidance（制导/开环俯仰程序初值）。

    v3 新增：若给了参考轨道倾角、且它与"由发射方位角估算的倾角"相差 >=2 度，
    说明该任务需要末级平面机动（dogleg，例如太原发 CZ-6A 的逆行轨道），
    自动设置 ``i_target_deg``，否则入轨倾角会明显偏。
    """
    cls = class_key_of(rocket)
    cd = CLASS_DEFAULTS.get(cls, CLASS_DEFAULTS["medium_liquid"])
    g0 = dict(rocket.get("guidance_defaults") or {})
    res: List[Resolution] = []

    def _pick(key: str, fallback: Any, label: str, conf_default="中") -> Any:
        if g0.get(key) is not None:
            res.append(Resolution("guidance." + key, label, fmt_num(g0[key]), "db",
                                  rocket.get("confidence", conf_default),
                                  "内置箭型默认值（反演会重算）"))
            return g0[key]
        res.append(Resolution("guidance." + key, label, fmt_num(fallback), "infer", "低",
                              "缺箭型制导数据，按 %s 类别锚点给初值，反演会重算" % cls))
        return fallback

    # 抛罩时刻：箭型 guidance_defaults 优先，其次 vehicle.fairing.sep_met。
    # v3 这里有一个逻辑错误：只要 fairing.sep_met 为空就把 t_fairing 置空，
    # 连箭型库里已有的 t_fairing 一起丢掉（引擎虽会回退 fairing.sep_met，但报告/时序表读到 None）。
    t_fair = g0.get("t_fairing")
    fair_met = (vehicle.get("fairing") or {}).get("sep_met")
    if t_fair is None and fair_met is not None:
        t_fair = fair_met
    if not vehicle.get("fairing"):
        t_fair = None

    guidance = {
        "mode": "shooting_plane",
        "azimuth_deg": float(az) if az is not None else None,
        "t_v": float(_pick("t_v", cd["t_v"], "推力转向时刻 t_v")),
        "t_p": float(_pick("t_p", cd["t_p"], "俯仰程序时长 t_p")),
        "pitch_power": float(_pick("pitch_power", cd["pitch_power"], "俯仰程序幂次")),
        "t_fairing": (float(t_fair) if t_fair is not None else None),
        "fairing_divert_mps": float(g0.get("fairing_divert_mps") or 0.0),
        "plane_keeping": bool(g0.get("plane_keeping", False)),
        "yaw_tau": float(g0.get("yaw_tau") or 150.0),
        "i_target_deg": g0.get("i_target_deg"),
        "dt": 0.02,
        "debris_dt": 0.25,
        "post_seco_s": 60.0,
        "flow_scale": 1.0,
    }
    if g0.get("yaw2_tau") is not None:
        guidance["yaw2_tau"] = float(g0["yaw2_tau"])
    # ---- v3.3：整条折线俯仰剖面（低推重比末级的箭型需要；缺省仍用 t_v/t_p/各级 pitch_end）----
    _sch = g0.get("pitch_schedule")
    if isinstance(_sch, list) and len(_sch) >= 2:
        try:
            sch = [[float(a), float(b)] for a, b in _sch]
            guidance["pitch_schedule"] = sch
            res.append(Resolution(
                "guidance.pitch_schedule", "俯仰程序（折线剖面）",
                "%d 节点 / %.0f s" % (len(sch), sch[-1][0]), "db",
                rocket.get("confidence", "中"),
                "内置箭型标称俯仰剖面（按公开运力与落区自洽拟合；该箭型二级推重比低，"
                "t_v/t_p/pitch_end 线性口径描述不了入轨剖面）；反演会微调关键节点"))
        except (TypeError, ValueError):
            pass
    for _k in ("seco_band_km", "seco_e_max"):
        if g0.get(_k) is not None:
            guidance[_k] = float(g0[_k])
    if az is not None:
        res.append(Resolution("guidance.azimuth_deg", "发射方位角", "%.3f°" % az, "infer", "中",
                              "由发射场与落区/目标轨道推断，反演会精化"))
    # ---- 末级平面机动（dogleg）自动判定 ----
    ref_i = None
    if ref_orbit and ref_orbit.get("i_deg") is not None:
        ref_i = float(ref_orbit["i_deg"])
    #: dogleg 可实现的平面差上限（超过它说明"参考轨道与发射方位角自相矛盾"，强行做会飞不出去）
    DOGLEG_MAX_DEG = 12.0
    if ref_i is not None and az is not None and site_lat is not None:
        i_est = orbit_from_azimuth(az, site_lat)
        diff = abs(ref_i - i_est)
        if 2.0 <= diff <= DOGLEG_MAX_DEG:
            guidance["i_target_deg"] = ref_i
            if guidance.get("yaw2_tau") is None:
                guidance["yaw2_tau"] = float(g0.get("yaw2_tau") or 200.0)
            # 量级代价：把高度处的圆轨道速度按平面差旋转 → 2v·sin(Δi/2)
            _v = 7600.0
            try:
                _alt = float((ref_orbit or {}).get("alt_km") or 0.0)
                if _alt > 0:
                    _v = math.sqrt(MU / ((RE_KM + _alt) * 1000.0))
            except (TypeError, ValueError):
                pass
            _dv = 2.0 * _v * math.sin(math.radians(diff) / 2.0)
            res.append(Resolution(
                "guidance.i_target_deg", "末级平面机动目标倾角", "%.2f°" % ref_i,
                "infer", "中",
                "参考轨道倾角 %.2f° 与由方位角 %.1f° 估算的 %.2f° 相差 %.1f°（2°~%.0f°），"
                "判定需要末级平面修正（dogleg），已设 i_target_deg；"
                "量级代价 ≈2v·sin(Δi/2)≈%.0f m/s（引擎按几何 dogleg 处理，不计 Δv 与推力损失）"
                % (ref_i, az, i_est, diff, DOGLEG_MAX_DEG, _dv)))
        elif diff > DOGLEG_MAX_DEG:
            guidance["i_target_deg"] = None
            res.append(Resolution(
                "guidance.i_target_deg", "末级平面机动目标倾角", "不启用（矛盾，已护栏）",
                "infer", "低",
                "参考轨道倾角 %.2f° 与由方位角 %.1f° 估算的 %.2f° 相差 %.1f°（>%.0f°）："
                "两者自相矛盾，强行做平面修正会飞不出去，已**关闭 dogleg**。"
                "请显式填写目标轨道倾角/高度后重算" % (ref_i, az, i_est, diff, DOGLEG_MAX_DEG)))
        else:
            res.append(Resolution(
                "guidance.i_target_deg", "末级平面机动目标倾角", "不启用",
                "infer", "中",
                "参考轨道倾角 %.2f° 与方位角估算 %.2f° 相差 %.1f°（<2°），按无 dogleg 处理"
                % (ref_i, i_est, diff)))
    return guidance, res


# --------------------------------------------------------------------------- #
# 场站 / 观测点 / 航警
# --------------------------------------------------------------------------- #
def resolve_site(site_label: str, site_id: str,
                 lat: Optional[float], lon: Optional[float], alt: Optional[float],
                 rocket: Optional[Dict[str, Any]],
                 dem_resolver: Optional[Callable[[float, float], Tuple[Optional[float], str]]] = None
                 ) -> Tuple[Dict[str, Any], str, str, str, List[str]]:
    """解析发射场 → (launch_site dict, source, confidence, note, warnings)。"""
    warns: List[str] = []
    # 1) 用户直接给坐标
    if lat is not None and lon is not None:
        near = nearest_site(lat, lon, max_km=25.0)
        if near:
            alt_m = alt if alt is not None else near.get("alt_m_ellipsoid")
            note = "用户坐标与内置工位「%s」相距 %.1f km，按已知工位参数" % (
                near.get("name"), great_circle_km(lat, lon, near["lat_deg"], near["lon_deg"]))
            return (dict(name=near.get("name"), lat_deg=float(lat), lon_deg=float(lon),
                         alt_m_ellipsoid=float(alt_m or 0.0), coord_note=note),
                    "db", "高", note, warns)
        alt_m, alt_src = (alt, "user") if alt is not None else (None, "")
        if alt_m is None and dem_resolver:
            alt_m, alt_src = dem_resolver(lat, lon)
        if alt_m is None:
            warns.append("自定义发射场高程未知，按 0 m 处理（会影响入轨高度约数十米）")
            alt_m, alt_src = 0.0, "default"
        note = "用户给定坐标（高程来源：%s）" % (alt_src or "未知")
        return (dict(name="自定义发射场", lat_deg=float(lat), lon_deg=float(lon),
                     alt_m_ellipsoid=float(alt_m), coord_note=note), "user", "高", note, warns)

    # 2) 内置库
    label = site_label or site_id
    it, why = match_site(label if label not in ("", "auto", "custom") else "")
    if it is None and rocket:
        ids = rocket.get("launch_sites") or []
        for sid in ids:
            for s in site_list():
                if s.get("id") == sid:
                    it, why = s, "按箭型典型工位「%s」" % s.get("name")
                    warns.append("未指定发射场，取该箭型典型工位「%s」（请核对）" % s.get("name"))
                    break
            if it:
                break
    if it is None:
        raise ValueError("无法确定发射场：请选择内置工位或直接给出经纬度")
    alt_m = it.get("alt_m_ellipsoid")
    alt_src = "db"
    if alt_m is None and dem_resolver:
        alt_m, alt_src = dem_resolver(it["lat_deg"], it["lon_deg"])
    conf = it.get("confidence", "中")
    note = it.get("coord_note") or it.get("note") or ""
    return (dict(name=it.get("name"), lat_deg=float(it["lat_deg"]), lon_deg=float(it["lon_deg"]),
                 alt_m_ellipsoid=float(alt_m or 0.0), coord_note=note),
            "db", conf, "%s %s" % (why, note), warns)


def resolve_observers(observers: List[ObserverInput],
                      dem_resolver: Optional[Callable[[float, float], Tuple[Optional[float], str]]],
                      site: Optional[Dict[str, Any]] = None
                      ) -> Tuple[List[Dict[str, Any]], List[Resolution], List[str]]:
    """补全观测点（名称→城市库、高程→城市/DEM）。"""
    res: List[Resolution] = []
    warns: List[str] = []
    out: List[Dict[str, Any]] = []
    for i, o in enumerate(observers or []):
        lat, lon = o.lat_deg, o.lon_deg
        src, conf, note = "user", "高", o.note or ""
        label = o.name or ("观测点%d" % (i + 1))
        city = None
        if lat is None or lon is None:
            city, why = match_city(o.name)
            if city is None:
                warns.append("观测点「%s」缺经纬度且城市库未匹配，已忽略" % label)
                continue
            lat, lon = city["lat_deg"], city["lon_deg"]
            src, conf = "db", "中"
            note = "按城市库「%s」（%s）" % (city.get("name"), why)
            label = label or city.get("name")
        alt = o.alt_m
        if alt is None:
            if city is None:
                city, _ = match_city(o.name)
            if city and city.get("alt_m") is not None:
                alt = float(city["alt_m"])
                src2, conf2 = "db", "中"
                note = (note + "；" if note else "") + "高程取城市库 %s m" % fmt_num(alt)
            elif dem_resolver:
                alt, ds = dem_resolver(float(lat), float(lon))
                src2, conf2 = ("db", "中") if ds == "srtm" else ("infer", "低")
                note = (note + "；" if note else "") + "高程由 %s 采样" % (
                    "SRTM 1″" if ds == "srtm" else "内嵌 0.02° 粗 DEM（约 2 km，低置信）")
            else:
                alt, src2, conf2 = 0.0, "default", "低"
                note = (note + "；" if note else "") + "高程缺失，按 0 m 处理"
        else:
            src2, conf2 = "user", "高"
        oid = o.id or ("obs%d" % (i + 1))
        out.append({"id": oid, "name": label, "lat_deg": round(float(lat), 6),
                    "lon_deg": round(float(lon), 6), "alt_m": round(float(alt), 1), "note": note})
        res.append(Resolution("observers.%s" % oid, "观测点 %s" % label,
                              "%.5f, %.5f, %s m" % (lat, lon, fmt_num(alt)), src2, conf2, note))
    if observers and not out:
        warns.append("所有观测点都无法解析，天空轨迹相关模块将被跳过")
    return out, res, warns


def resolve_notams(parsed: List[Dict[str, Any]], site: Optional[Dict[str, Any]],
                   rocket: Optional[Dict[str, Any]]
                   ) -> Tuple[List[Dict[str, Any]], List[Resolution], List[str]]:
    """补全航警的 stage_hint（按距离/窗口顺序推断），并做基本校验。"""
    res: List[Resolution] = []
    warns: List[str] = []
    out: List[Dict[str, Any]] = []
    has_boosters = bool((rocket or {}).get("vehicle", {}).get("boosters"))
    zones = []
    for z in parsed:
        zz = dict(z)
        zones.append(zz)
    # 按距发射场距离排序
    def _dist(z):
        cen = zone_centroid(z)
        if site is None or cen is None:
            return 1e9
        return great_circle_km(site["lat_deg"], site["lon_deg"], cen[0], cen[1])

    ordered = sorted(zones, key=_dist)
    guessed = []
    for idx, z in enumerate(ordered):
        if z.get("stage_hint"):
            continue
        d = _dist(z)
        hint = ""
        if idx == 0:
            if has_boosters and 150.0 <= d <= 600.0:
                hint = "助推器"
            else:
                hint = "一级"
        elif idx == 1:
            hint = "整流罩"
        else:
            hint = "二级"
        z["stage_hint"] = hint
        z["_hint_inferred"] = True          # v3.1：标记为自动推断，运行时会按预测落点两遍法校正
        z["verified"] = (z.get("verified") or "") + "；残骸类型按距离顺序推断（%s，%.0f km）" % (hint, d)
        guessed.append(z.get("id") or "?")
    if guessed:
        warns.append("以下航警未标残骸类型，已按距离/顺序推断：%s；运行时会先用标称参数试算、"
                     "再按「预测落点↔航警区」最近匹配自动校正（建议仍人工核对）"
                     % "、".join(guessed))
    for z in zones:
        keys = [k for k in ("id", "b_utc", "c_utc") if not z.get(k)]
        poly_ok = len(z.get("polygon_latlon") or []) >= 3
        if keys or not poly_ok:
            warns.append("航警 %s 字段不完整（缺 %s%s），该区不参与落点判定" % (
                z.get("id") or "?", "、".join(keys),
                "，多边形顶点不足" if not poly_ok else ""))
            continue
        out.append(z)
        cen = zone_centroid(z)
        res.append(Resolution("notams.%s" % z["id"], "航警区 %s" % z["id"],
                              "%s ~ %s，%d 顶点" % (z["b_utc"][11:16], z["c_utc"][11:16],
                                                    len(z["polygon_latlon"])),
                              "user", "高" if z.get("stage_hint") else "中",
                              "残骸类型：%s" % (z.get("stage_hint") or "未标")))
    return out, res, warns


# --------------------------------------------------------------------------- #
# 网格 / 数值口径
# --------------------------------------------------------------------------- #
def infer_grid(site: Optional[Dict[str, Any]]) -> Tuple[Dict[str, float], str]:
    """全国地图网格：默认中国范围；发射场/落区在中国外时自动外扩。"""
    g = {"lon_min": 72.0, "lon_max": 136.0, "lat_min": 15.0, "lat_max": 55.0, "step_deg": 0.2}
    if site and (site.get("lon_deg") is not None):
        lon, lat = float(site["lon_deg"]), float(site["lat_deg"])
        changed = False
        if lon < g["lon_min"] + 2 or lon > g["lon_max"] - 2:
            g["lon_min"] = max(-180.0, min(g["lon_min"], lon - 30.0))
            g["lon_max"] = max(g["lon_max"], min(180.0, lon + 30.0))
            changed = True
        if lat < g["lat_min"] + 2 or lat > g["lat_max"] - 2:
            g["lat_min"] = max(-80.0, min(g["lat_min"], lat - 20.0))
            g["lat_max"] = max(g["lat_max"], min(84.0, lat + 20.0))
            changed = True
        if changed:
            return g, "发射场在中国网格边缘之外，已自动外扩网格（国界/省界底图仍为中国范围）"
    return g, "中国全国网格（0.2°）"


def default_numerics(mode: str = "accurate") -> Dict[str, Any]:
    return {
        "mode": mode if mode in ("accurate", "legacy") else "accurate",
        "dt": 0.2, "dt_legacy": 0.02, "sample_dt": 0.5,
        "debris_dt": 1.0, "debris_mode": "accurate",
        "_note": "v3 默认 accurate（事件对齐 + 质量随 RK4 积分）；legacy 仅用于复现 v1 结果。",
    }


# --------------------------------------------------------------------------- #
# 任务名
# --------------------------------------------------------------------------- #
def auto_mission_name(rocket_name: str, site_name: str, t0: Optional[datetime]) -> str:
    parts = []
    if t0 is not None:
        parts.append(t0.strftime("%Y-%m-%d"))
    if rocket_name:
        parts.append(rocket_name)
    if site_name:
        parts.append(site_name.split("(")[0].strip())
    parts.append("上升段 3-DOF 弹道仿真")
    return " ".join(parts)


def auto_workdir_name(rocket_name: str, site_name: str, t0: Optional[datetime]) -> str:
    stamp = t0.strftime("%Y-%m-%d") if t0 is not None else datetime.now().strftime("%Y-%m-%d")
    rk = re.sub(r"[\\/:*?\"<>|\s]+", "", rocket_name or "任务")[:20]
    st = re.sub(r"[\\/:*?\"<>|\s]+", "", (site_name or "").split("(")[0])[:12]
    return "%s_%s%s" % (stamp, rk, ("_" + st) if st else "")
