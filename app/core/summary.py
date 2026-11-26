# -*- coding: utf-8 -*-
"""统一成果汇总：把「常规发射 / 载人 / 返回」三类任务的产物收敛成同一套通用输出。

v3 的问题
---------
每个模块各写各的文件，任务类型不同则成果差异很大（返回任务只有返回舱时序/KML，
没有事件总表、没有关键节点、没有一页纸摘要），用户很难快速回答"这次跑了什么、
哪些成果有、哪些因为缺参数没出、为什么没出"。

v4 在每个任务结束时额外产出（全部在任务工作区内）::

    sim/关键节点.csv        跨任务统一的关键事件表（起飞/分离/入轨/对接/制动/开伞/着陆）
    sim/飞行阶段.csv        阶段时间线（阶段 / 起止 / 时长 / 高度速度范围）
    sim/飞行环境.csv        动压与过载剖面（由弹道差分估算，注明方法）
    sim/轨迹统计.json       一页数字：最大高度/速度/动压/过载/射程/入轨/落点/窗口…
    report/任务摘要.md      一页纸中文摘要（任务类型无关）
    report/任务摘要.json    同上的结构化数据（供二次分析/报告引用）
    report/成果清单.md      全部成果文件清单 + 步骤台账（含跳过原因）
    report/成果清单.json    同上结构化

本模块只依赖标准库，不做任何数值积分：所有数字都来自已有成果文件（可追溯到
engine/scripts/*.py 的输出），因此不会与仿真结果产生口径冲突。
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

G0 = 9.80665
DECL_CN = "仅供公开信息分析与教学使用，非实际发射/返回弹道，非官方信息"

#: 关键节点：事件名关键词 → (类别, 统一节点名)。顺序即优先级。
LAUNCH_NODE_RULES: Tuple[Tuple[Tuple[str, ...], str, str], ...] = (
    (("起飞", "LIFTOFF"), "起飞", "起飞"),
    (("助推",), "分离", "助推器分离"),
    (("整流罩", "抛罩"), "分离", "整流罩分离"),
    (("SECO", "入轨"), "关机", "末级关机/入轨"),
    (("点火",), "点火", "发动机点火"),
    (("最大动压", "MAXQ", "MAX-Q"), "环境", "最大动压"),
    (("分离",), "分离", "级间分离"),
    (("关机",), "关机", "发动机关机"),
)

#: 返回任务：事件类别（来自 return_capsule 的 phase_timeline / timeline）
RETURN_NODE_RULES: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("分离",), "分离"),
    (("制动",), "制动"),
    (("再入",), "再入"),
    (("伞",), "伞降"),
    (("着陆", "落地"), "着陆"),
)

#: 各任务类型的"应当产出"清单（用于输出完整性核对）
EXPECTED_COMMON = [
    ("config.json", "本次完整配置"),
    ("任务输入.json", "界面/命令行原始输入"),
    ("运行日志.txt", "完整运行日志"),
    ("report/弹道仿真报告.md", "中文技术报告"),
    ("report/qc_report.json", "交付自检"),
    ("report/任务摘要.md", "一页纸任务摘要"),
    ("report/成果清单.md", "成果清单与步骤台账"),
    ("sim/关键节点.csv", "统一关键节点表"),
]
EXPECTED_LAUNCH = [
    ("sim/trajectory.csv", "弹道时间序列"),
    ("sim/弹道事件表.csv", "弹道事件表"),
    ("sim/落点与射程.csv", "残骸落点与射程"),
    ("sim/入轨根数.json", "入轨根数"),
    ("sim/飞行阶段.csv", "飞行阶段时间线"),
    ("sim/飞行环境.csv", "动压与过载剖面"),
    ("sim/轨迹统计.json", "轨迹统计"),
]
#: 只有启用 window 模块才应产出（否则会被误判为"缺失"，v4 首版就是这样）
EXPECTED_WINDOW = [("report/发射窗口计算.json", "发射窗口")]
EXPECTED_SKY = [
    ("sky/sky_summary.json", "天空轨迹汇总"),
    ("sky/天空轨迹_全景.png", "天空轨迹全景图"),
    ("map/全国最大高度角地图.png", "全国最大高度角地图"),
    ("viz/弹道三维_含残骸坠落.kmz", "三维弹道 KMZ"),
]
EXPECTED_STATION = [
    ("sim/空间站轨道.json", "空间站轨道"),
    ("sim/交会对接.json", "入轨+交会对接方案"),
    ("sim/交会对接_时序.csv", "交会对接时序"),
    ("report/交会发射窗口.json", "交会发射窗口与 T0 反推"),
]
EXPECTED_RETURN = [
    ("sim/返回舱返回.json", "返回舱返回结果"),
    ("sim/返回舱_时序.csv", "返回舱时序"),
    ("sim/返回舱事件与分离点.csv", "返回舱事件与分离点"),
    ("sim/返回舱阶段.csv", "返回舱阶段"),
    ("sim/返回舱弹道.png", "返回舱弹道图"),
    ("map/返回舱落区.png", "返回舱落区图"),
    ("viz/返回舱弹道.kmz", "返回舱三维 KMZ"),
]
EXPECTED_MC = [("uncertainty/mc_stats.json", "蒙特卡洛统计")]


# --------------------------------------------------------------------------- #
# 基础读写
# --------------------------------------------------------------------------- #
def _read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _read_csv(path: str) -> List[Dict[str, str]]:
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except Exception:  # noqa: BLE001
        return []


def _write_csv(path: str, fields: List[str], rows: Iterable[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: _cell(r.get(k)) for k in fields})


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return ""
        return "%.6g" % v
    return str(v)


def _write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _f(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if v is None or (isinstance(v, str) and not v.strip()):
            return default
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def _fmt(v: Any, nd: int = 1, unit: str = "") -> str:
    f = _f(v)
    if f is None:
        return "—"
    if abs(f) >= 1e5:
        s = "%.4g" % f
    elif abs(f - round(f)) < 1e-9 and nd == 0:
        s = "%d" % int(round(f))
    else:
        s = ("%." + str(nd) + "f") % f
    return s + (" " + unit if unit else "")


def _bj(iso: str) -> str:
    s = str(iso or "").strip()
    if not s:
        return "—"
    try:
        t = s[:-1] + "+00:00" if s.endswith("Z") else s
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt.astimezone(timezone.utc) + timedelta(hours=8)).strftime("%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return s


def _utc_short(iso: str) -> str:
    s = str(iso or "").strip()
    if not s:
        return "—"
    return s.replace("T", " ").replace("Z", "")


def _great_circle_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    dp, dl = p2 - p1, l2 - l1
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))


def mission_type(cfg: Dict[str, Any]) -> str:
    m = cfg.get("mission") or {}
    mt = str(m.get("mission_type") or (cfg.get("_v3") or {}).get("mission_type") or "")
    if mt in ("standard", "crewed", "return"):
        return mt
    if bool(cfg.get("return_only")) or str(m.get("profile") or "") == "return":
        return "return"
    if str(m.get("profile") or "") == "cz2f":
        return "crewed"
    return "standard"


MT_CN = {"standard": "常规发射弹道", "crewed": "载人发射弹道（含交会）",
         "return": "返回舱返回弹道"}


# --------------------------------------------------------------------------- #
# 1. 关键节点
# --------------------------------------------------------------------------- #
def _launch_nodes(workdir: str, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = _read_csv(os.path.join(workdir, "sim", "弹道事件表.csv"))
    out: List[Dict[str, Any]] = []
    for r in rows:
        ev = str(r.get("event") or "")
        if not ev:
            continue
        cat, label = "事件", ev
        up = ev.upper()
        for keys, c0, l0 in LAUNCH_NODE_RULES:
            if any(k.upper() in up for k in keys):
                cat, label = c0, l0
                break
        note = str(r.get("note") or "")
        if ev != label:
            note = ("%s；%s" % (ev, note)).strip("；")
        out.append({
            "序号": len(out) + 1, "类别": cat, "节点": label,
            "T+ (s)": _f(r.get("t_met_s")), "时刻(UTC)": r.get("t_utc", ""),
            "北京时": _bj(r.get("t_utc", "")),
            "纬度(°)": _f(r.get("lat_deg")), "经度(°)": _f(r.get("lon_deg")),
            "高度(km)": (round(_f(r.get("alt_m"), 0.0) / 1000.0, 2)
                         if _f(r.get("alt_m")) is not None else None),
            "速度(m/s)": _f(r.get("speed_m_s")), "质量(kg)": _f(r.get("mass_kg")),
            "说明": note,
        })
    return out


def _return_nodes(workdir: str, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    rt = _read_json(os.path.join(workdir, "sim", "返回舱返回.json"), {}) or {}
    if not rt.get("enabled"):
        return []
    out: List[Dict[str, Any]] = []
    met0 = 0.0
    sel = rt.get("selected") or {}
    deorb_iso = str(sel.get("t_deorbit_utc") or "")
    deorb = None
    try:
        deorb = datetime.fromisoformat(deorb_iso.replace("Z", "+00:00")) if deorb_iso else None
    except Exception:  # noqa: BLE001
        deorb = None

    def add(event: str, t_iso: str, lat=None, lon=None, alt_km=None, speed=None,
            note: str = "", cat: str = "") -> None:
        nonlocal met0
        if not cat:
            cat = "事件"
            for keys, c0 in RETURN_NODE_RULES:
                if any(k in event for k in keys):
                    cat = c0
                    break
        tmet = None
        if deorb is not None and t_iso:
            try:
                dt = datetime.fromisoformat(t_iso.replace("Z", "+00:00"))
                tmet = (dt - deorb).total_seconds()
            except Exception:  # noqa: BLE001
                tmet = None
        out.append({"序号": len(out) + 1, "类别": cat, "节点": event,
                    "T+ (s)": tmet, "时刻(UTC)": t_iso, "北京时": _bj(t_iso),
                    "纬度(°)": _f(lat), "经度(°)": _f(lon),
                    "高度(km)": (_f(alt_km) if alt_km is not None else None),
                    "速度(m/s)": _f(speed), "质量(kg)": None,
                    "说明": ("制动时刻为 T+0 基准；" + note) if note else
                            "制动时刻为 T+0 基准"})

    for it in (rt.get("phase_timeline") or []):
        add(str(it.get("event") or "事件"), str(it.get("t_utc") or ""),
            it.get("lat_deg"), it.get("lon_deg"), it.get("alt_km"),
            it.get("speed_m_s"), str(it.get("note") or ""))
    if not out:
        for it in (rt.get("separations") or []):
            add(str(it.get("event") or "分离"), str(it.get("t_utc") or ""),
                it.get("lat_deg"), it.get("lon_deg"), it.get("alt_km"),
                it.get("speed_m_s"), str(it.get("note") or ""), cat="分离")
    for it in (rt.get("timeline") or []):
        spd = it.get("speed_m_s")
        if spd is None and it.get("speed_km_s") is not None:
            spd = float(it["speed_km_s"]) * 1000.0
        add(str(it.get("event") or "事件"), str(it.get("t_utc") or ""),
            it.get("lat"), it.get("lon"), it.get("alt_km"), spd, "")
    landing = (sel.get("landing") or {})
    if landing.get("t_utc"):
        add("着陆", str(landing.get("t_utc")),
            landing.get("lat_deg"), landing.get("lon_deg"), 0.0,
            landing.get("speed_m_s"),
            "落点 %s, %s；飞行 %.1f min" % (
                _fmt(landing.get("lat_deg"), 3), _fmt(landing.get("lon_deg"), 3),
                (_f(landing.get("flight_time_s"), 0.0) or 0.0) / 60.0))
    return out


def _dock_nodes(workdir: str) -> List[Dict[str, Any]]:
    dk = _read_json(os.path.join(workdir, "sim", "交会对接.json"), {}) or {}
    d0 = dk.get("docking") or {}
    if not (dk.get("enabled") and d0):
        return []
    rows = []
    for it in (dk.get("timeline") or []):
        label = str(it.get("phase") or it.get("event") or "交会事件")
        note = []
        if it.get("dv_mps"):
            note.append("ΔV %s m/s%s" % (_fmt(it.get("dv_mps"), 1),
                                         ("（%s）" % it["dv_note"]) if it.get("dv_note") else ""))
        if it.get("cum_dv_mps"):
            note.append("累计 ΔV %s m/s" % _fmt(it.get("cum_dv_mps"), 1))
        if it.get("duration_s"):
            note.append("距发射 %s min" % _fmt((_f(it.get("duration_s"), 0.0) or 0.0) / 60.0, 1))
        if it.get("range_to_station_km") is not None:
            note.append("距空间站 %s km" % _fmt(it.get("range_to_station_km"), 1))
        if it.get("note"):
            note.append(str(it["note"]))
        rows.append({"序号": len(rows) + 1, "类别": "交会", "节点": label,
                     "T+ (s)": _f(it.get("t_met_s")), "时刻(UTC)": it.get("t_utc"),
                     "北京时": _bj(it.get("t_utc")),
                     "纬度(°)": _f(it.get("lat_deg")), "经度(°)": _f(it.get("lon_deg")),
                     "高度(km)": _f(it.get("alt_km")), "速度(m/s)": None,
                     "质量(kg)": None, "说明": "；".join(x for x in note if x)})
    if d0.get("t_utc"):
        rows.append({"序号": len(rows) + 1, "类别": "交会", "节点": "预计对接",
                     "T+ (s)": _f(d0.get("t_met_s")), "时刻(UTC)": d0.get("t_utc"),
                     "北京时": _bj(d0.get("t_utc")),
                     "纬度(°)": _f(d0.get("station_lat_deg")),
                     "经度(°)": _f(d0.get("station_lon_deg")),
                     "高度(km)": _f(rows[-1].get("高度(km)")) if rows else None,
                     "速度(m/s)": None, "质量(kg)": None,
                     "说明": "总 ΔV %s m/s；%s；%s" % (
                         _fmt(dk.get("total_dv_mps"), 0),
                         "方案可行" if dk.get("feasible") else "方案不可行",
                         str(d0.get("note") or ""))})
    return rows


def key_nodes(workdir: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """生成统一关键节点（返回 {"rows": [...], "by_category": {...}}）。"""
    mtype = mission_type(cfg)
    rows: List[Dict[str, Any]] = []
    if mtype == "return":
        rows.extend(_return_nodes(workdir, cfg))
    else:
        rows.extend(_launch_nodes(workdir, cfg))
        rows.extend(_dock_nodes(workdir))
        if (cfg.get("return_capsule") or {}).get("enabled"):
            # 上升 + 返回同一任务（CZ-2F 载人）：返回段节点放后半段，序号顺延
            ret = _return_nodes(workdir, cfg)
            for r in ret:
                r = dict(r)
                r["序号"] = len(rows) + 1
                r["类别"] = "返回·" + str(r.get("类别") or "事件")
                rows.append(r)
        rows.sort(key=lambda r: (r.get("T+ (s)") is None, _f(r.get("T+ (s)"), 0.0) or 0.0))
        for i, r in enumerate(rows, start=1):
            r["序号"] = i
    by_cat: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_cat.setdefault(str(r.get("类别") or "事件"), []).append(r)
    return {"rows": rows, "by_category": by_cat, "mission_type": mtype}


# --------------------------------------------------------------------------- #
# 2. 飞行阶段
# --------------------------------------------------------------------------- #
def flight_phases(workdir: str, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """阶段时间线：上升段按 trajectory.phase 分组；返回段直接取 返回舱阶段.csv。"""
    if mission_type(cfg) == "return":
        rows = _read_csv(os.path.join(workdir, "sim", "返回舱阶段.csv"))
        out = []
        for i, r in enumerate(rows, start=1):
            out.append({"序号": i, "阶段": r.get("phase") or "",
                        "开始(UTC)": r.get("start_utc") or "",
                        "结束(UTC)": r.get("end_utc") or "",
                        "时长(s)": _f(r.get("duration_s")),
                        "高度范围(km)": "", "速度范围(m/s)": "",
                        "说明": r.get("note") or ""})
        return out
    traj = _read_csv(os.path.join(workdir, "sim", "trajectory.csv"))
    out: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for r in traj:
        ph = str(r.get("phase") or "—")
        alt = _f(r.get("alt_m"), 0.0) or 0.0
        spd = _f(r.get("speed_m_s"), 0.0) or 0.0
        t = _f(r.get("t_met_s"), 0.0) or 0.0
        if cur is None or cur["phase"] != ph:
            if cur is not None:
                out.append(cur)
            cur = {"phase": ph, "t0": t, "t1": t, "utc0": r.get("t_utc", ""),
                   "utc1": r.get("t_utc", ""), "alt0": alt, "alt1": alt,
                   "alt_min": alt, "alt_max": alt, "spd0": spd, "spd1": spd,
                   "spd_min": spd, "spd_max": spd, "n": 1}
        else:
            cur["t1"] = t
            cur["utc1"] = r.get("t_utc", "")
            cur["alt1"] = alt
            cur["spd1"] = spd
            cur["alt_min"] = min(cur["alt_min"], alt)
            cur["alt_max"] = max(cur["alt_max"], alt)
            cur["spd_min"] = min(cur["spd_min"], spd)
            cur["spd_max"] = max(cur["spd_max"], spd)
            cur["n"] += 1
    if cur is not None:
        out.append(cur)
    rows = []
    for i, c in enumerate(out, start=1):
        rows.append({
            "序号": i, "阶段": _phase_cn(c["phase"]),
            "开始(UTC)": c.get("utc0", ""), "结束(UTC)": c.get("utc1", ""),
            "时长(s)": round(max(0.0, c["t1"] - c["t0"]), 2),
            "高度范围(km)": "%.1f ~ %.1f" % (c["alt_min"] / 1000.0, c["alt_max"] / 1000.0),
            "速度范围(m/s)": "%.0f ~ %.0f" % (c["spd_min"], c["spd_max"]),
            "说明": "采样 %d 点；T+%.1f → %.1f s" % (c["n"], c["t0"], c["t1"]),
        })
    return rows


# --------------------------------------------------------------------------- #
# 3. 轨迹统计（含动压/过载估算）
# --------------------------------------------------------------------------- #
def _density(alt_m: float) -> float:
    """指数大气（与引擎 3-DOF 的近似同量级；仅用于统计量，不参与积分）。"""
    h = max(0.0, float(alt_m))
    if h > 1.0e5:
        return 0.0
    return 1.225 * math.exp(-h / 8500.0)


def _air_relative_speed(row: Dict[str, str]) -> Optional[float]:
    """气动相关速度：优先用 trajectory.csv 的 ECEF 速度（地固系，已扣地球自转）。

    注意：``speed_m_s`` 列是**惯性速度**（含地球自转，38.9°N 处约 362 m/s），
    直接拿它算动压会在 t=0 得到 68 kPa 的假"最大动压"。
    """
    vx = _f(row.get("vx_ecef"))
    vy = _f(row.get("vy_ecef"))
    vz = _f(row.get("vz_ecef"))
    if vx is not None and vy is not None and vz is not None:
        return math.sqrt(vx * vx + vy * vy + vz * vz)
    return _f(row.get("speed_m_s"))


PHASE_CN = {"LAUNCH": "起飞垂直段", "S1": "一级", "S2": "二级", "S3": "三级",
            "S4": "四级", "S5": "五级", "ORBIT": "入轨段", "COAST": "滑行段",
            "PARK": "停泊段", "DEBRIS": "残骸段", "ASCENT": "上升段"}


def _phase_cn(name: str) -> str:
    s = str(name or "—").strip()
    if s in PHASE_CN:
        return PHASE_CN[s]
    if re.match(r"^S\d+$", s, re.I):
        return "第%s级" % s[1:]
    return s


def _env_profile(workdir: str) -> List[Dict[str, Any]]:
    """动压与过载剖面（由 trajectory.csv 差分估算，方法写进文件与摘要）。"""
    traj = _read_csv(os.path.join(workdir, "sim", "trajectory.csv"))
    rows: List[Dict[str, Any]] = []
    n = len(traj)
    speeds = [_air_relative_speed(r) or 0.0 for r in traj]
    for i, r in enumerate(traj):
        t = _f(r.get("t_met_s"), 0.0) or 0.0
        alt = _f(r.get("alt_m"), 0.0) or 0.0
        spd = speeds[i]
        q = 0.5 * _density(alt) * spd * spd
        acc = None
        if 0 < i < n - 1:
            t0 = _f(traj[i - 1].get("t_met_s"), t) or t
            t1 = _f(traj[i + 1].get("t_met_s"), t) or t
            v0, v1 = speeds[i - 1], speeds[i + 1]
            a0 = _f(traj[i - 1].get("alt_m"), alt) or alt
            a1 = _f(traj[i + 1].get("alt_m"), alt) or alt
            dt = max(1e-6, t1 - t0)
            dv = (v1 - v0) / dt                        # 气动相对速度变化率
            vmid = max(1.0, 0.5 * (v0 + v1))
            sin_fpa = max(-1.0, min(1.0, (a1 - a0) / dt / vmid))
            acc = dv / G0 + sin_fpa                    # 扣除重力分量后的净过载（g）
        rows.append({"t_met_s": round(t, 2), "t_utc": r.get("t_utc", ""),
                     "alt_km": round(alt / 1000.0, 3), "speed_m_s": round(spd, 2),
                     "q_pa": round(q, 1), "accel_g": (round(acc, 3) if acc is not None else None),
                     "phase": _phase_cn(r.get("phase", "")), "mass_kg": _f(r.get("mass_kg"))})
    return rows


def trajectory_stats(workdir: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    mtype = mission_type(cfg)
    stats: Dict[str, Any] = {"mission_type": mtype, "generated_utc":
                             datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "method_notes": []}
    if mtype == "return":
        rt = _read_json(os.path.join(workdir, "sim", "返回舱返回.json"), {}) or {}
        sel = rt.get("selected") or {}
        landing = sel.get("landing") or {}
        stats["return"] = {
            "enabled": bool(rt.get("enabled")), "feasible": bool(rt.get("feasible")),
            "n_windows": rt.get("n_windows"), "station": rt.get("station"),
            "landing_site": rt.get("landing_site"), "selected": {
                k: sel.get(k) for k in ("t_deorbit_utc", "t_landing_utc", "lat_deg",
                                        "lon_deg", "flight_time_s", "ground_speed_mps",
                                        "zone_id", "inside_zone", "dist_to_zone_km",
                                        "deorbit_dv_mps", "bank_deg", "target_distance_km")
            },
            "peaks": sel.get("peaks") or {}, "dispersion": rt.get("dispersion") or {},
            "dispersion_note": rt.get("dispersion_note"),
            "nominal_flight_s": rt.get("nominal_flight_s"),
        }
        stats["method_notes"].append(
            "返回段数字来自 return_capsule 模型（瞬时制动 + 升力再入 + 两级伞降）；"
            "周向散布为 ΔV/倾侧角/质量扰动样本的 1σ 椭圆，不是官方残骸区")
        if landing.get("t_utc"):
            stats["max_altitude_km"] = (sel.get("peaks") or {}).get("alt_km")
        return stats

    # ---- 上升段 ----
    traj = _read_csv(os.path.join(workdir, "sim", "trajectory.csv"))
    if traj:
        alts = [(_f(r.get("alt_m"), 0.0) or 0.0, r) for r in traj]
        spds = [(_f(r.get("speed_m_s"), 0.0) or 0.0, r) for r in traj]
        tmax = max(alts, key=lambda x: x[0])
        vmax = max(spds, key=lambda x: x[0])
        stats["trajectory"] = {
            "points": len(traj),
            "duration_s": _f(traj[-1].get("t_met_s"), 0.0),
            "max_altitude_km": round(tmax[0] / 1000.0, 2),
            "max_altitude_utc": tmax[1].get("t_utc"),
            "max_altitude_t_met_s": _f(tmax[1].get("t_met_s")),
            "max_speed_m_s": round(vmax[0], 1),
            "max_speed_utc": vmax[1].get("t_utc"),
            "max_speed_t_met_s": _f(vmax[1].get("t_met_s")),
            "liftoff_mass_kg": _f(traj[0].get("mass_kg")),
            "final_mass_kg": _f(traj[-1].get("mass_kg")),
        }
        env = _env_profile(workdir)
        if env:
            qs = [e for e in env if e.get("q_pa") is not None]
            gs = [e for e in env if e.get("accel_g") is not None]
            if qs:
                qmax = max(qs, key=lambda e: e["q_pa"])
                stats["max_q"] = {"pa": qmax["q_pa"], "t_met_s": qmax["t_met_s"],
                                  "alt_km": qmax["alt_km"], "speed_m_s": qmax["speed_m_s"]}
            if gs:
                gmax = max(gs, key=lambda e: e["accel_g"])
                stats["max_accel"] = {"g": gmax["accel_g"], "t_met_s": gmax["t_met_s"],
                                      "alt_km": gmax["alt_km"]}
            stats["env_points"] = len(env)
            stats["method_notes"].append(
                "动压 q=0.5·ρ·v²（指数大气 ρ=1.225·e^(−h/8500)）；速度取 ECEF 地固系分量"
                "（已扣地球自转），否则 38.9°N 处起飞瞬间 362 m/s 的自转速度会算出假最大动压；"
                "过载由速度差分扣除重力分量估算，含阻力影响，仅作量级参考")
            stats["method_notes"].append(
                "trajectory.max_speed_m_s 为惯性速度（入轨速度量级），与动压用的气动相对速度不同")
        # 射程（发射场→末点）
        site = ((cfg.get("mission") or {}).get("launch_site") or {})
        last = traj[-1]
        if site.get("lat_deg") is not None and last.get("lat_deg") is not None:
            stats["downrange_km"] = round(_great_circle_km(
                float(site["lat_deg"]), float(site["lon_deg"]),
                float(last["lat_deg"]), float(last["lon_deg"])), 2)
    orb = _read_json(os.path.join(workdir, "sim", "入轨根数.json"), {}) or {}
    if orb and not orb.get("error"):
        stats["orbit"] = {k: orb.get(k) for k in
                          ("a_km", "e", "i_deg", "raan_deg", "argp_deg",
                           "perigee_km", "apogee_km", "period_s", "insertion", "warning")}
    imps = _read_csv(os.path.join(workdir, "sim", "落点与射程.csv"))
    if imps:
        stats["impacts"] = [{
            "object": r.get("object"), "lat_deg": _f(r.get("lat_deg")),
            "lon_deg": _f(r.get("lon_deg")), "t_met_s": _f(r.get("impact_t_met_s")),
            "t_utc": r.get("impact_t_utc"), "inside_zone": r.get("inside_zone") == "true",
            "zone_id": r.get("zone_id") or None, "downrange_km": _f(r.get("downrange_km")),
            "dist_to_boundary_km": _f(r.get("dist_to_boundary_km")),
            "nearest_zone_id": r.get("nearest_zone_id") or None,
        } for r in imps]
    win = _read_json(os.path.join(workdir, "report", "发射窗口计算.json"), {}) or {}
    if win:
        pw = win.get("practical_window") or {}
        ow = win.get("orbit_window") or {}
        stats["windows"] = {
            "practical": {k: pw.get(k) for k in ("start_utc", "end_utc", "duration_min")}
            if pw else None,
            "orbit_available": bool(ow.get("available")),
            "orbit_n": ow.get("n_windows"),
            "orbit_best": (ow.get("best_near_t0") or {}).get("best_utc"),
            "reason": win.get("reason"),
        }
    sky = _read_json(os.path.join(workdir, "sky", "sky_summary.json"), {}) or {}
    obs = sky.get("observers") or {}
    if obs:
        stats["observers"] = {}
        for oid, o in list(obs.items())[:8]:
            fa = o.get("first_appearance") or {}
            mx = o.get("max_elevation") or {}
            last = o.get("last_appearance") or {}
            stats["observers"][oid] = {
                "first_appearance_utc": fa.get("t_utc"),
                "first_az_deg": fa.get("az_deg"), "first_el_deg": fa.get("el_deg"),
                "max_el_deg": mx.get("el_deg"), "max_el_utc": mx.get("t_utc"),
                "last_appearance_utc": last.get("t_utc"),
            }
    st = _read_json(os.path.join(workdir, "sim", "空间站轨道.json"), {}) or {}
    if st.get("enabled"):
        a0 = st.get("at_t0") or {}
        stats["station"] = {"name": (st.get("station") or {}).get("name"),
                            "source": (st.get("station") or {}).get("source"),
                            "kind": (st.get("station") or {}).get("kind"),
                            "a_km": a0.get("a_km"), "i_deg": a0.get("i_deg"),
                            "warning": (st.get("station") or {}).get("warning")}
    dk = _read_json(os.path.join(workdir, "sim", "交会对接.json"), {}) or {}
    if dk.get("enabled"):
        stats["docking"] = {"feasible": dk.get("feasible"),
                            "total_dv_mps": dk.get("total_dv_mps"),
                            "docking": dk.get("docking"), "reason": dk.get("reason"),
                            "plan_mode": (dk.get("plan") or {}).get("mode")}
    mc = _read_json(os.path.join(workdir, "uncertainty", "mc_stats.json"), {}) or {}
    if mc:
        stats["monte_carlo"] = {"n": mc.get("n"), "n_valid": mc.get("n_valid"),
                                "drms_km": (mc.get("impacts") or {}).get("drms_km")}
    rt = _read_json(os.path.join(workdir, "sim", "返回舱返回.json"), {}) or {}
    if rt.get("enabled"):
        sel = rt.get("selected") or {}
        stats["return"] = {"enabled": True, "selected": {
            k: sel.get(k) for k in ("t_deorbit_utc", "t_landing_utc", "lat_deg",
                                    "lon_deg", "flight_time_s", "inside_zone")},
            "peaks": sel.get("peaks") or {}, "n_windows": rt.get("n_windows")}
    return stats


def crew_items(stats: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """载人任务关注项（其余任务也可作为"飞行环境"参考）。"""
    mtype = mission_type(cfg)
    peaks = (stats.get("return") or {}).get("peaks") or {}
    q = (stats.get("max_q") or {}).get("pa")
    g = (stats.get("max_accel") or {}).get("g")
    items: List[Dict[str, Any]] = []
    if q is not None:
        items.append({"项目": "上升段最大动压", "数值": "%s kPa" % _fmt(q / 1000.0, 1),
                      "参考限值": "≤ 40 kPa（工程经验）",
                      "结论": "在参考范围内" if q <= 40000 else "高于参考值，需核对弹道"})
    if g is not None:
        items.append({"项目": "上升段最大净过载", "数值": "%s g" % _fmt(g, 2),
                      "参考限值": "≤ 4.0 g（载人舒适区，工程经验）",
                      "结论": "在参考范围内" if abs(g) <= 4.0
                              else "高于参考值（差分估算，建议人工核对）"})
    if peaks:
        items.append({"项目": "再入峰值动压", "数值": "%s kPa" % _fmt(_f(peaks.get("q_pa"), 0) / 1000.0, 1),
                      "参考限值": "—", "结论": "返回舱模型给出"})
        items.append({"项目": "再入峰值过载", "数值": "%s g" % _fmt(peaks.get("g"), 2),
                      "参考限值": "≤ 5.0 g（工程经验）",
                      "结论": "在参考范围内" if (_f(peaks.get("g"), 0) or 0) <= 5.0
                              else "偏大，需核对返回方案"})
        items.append({"项目": "再入峰值热流", "数值": "%s W/cm²" % _fmt(peaks.get("heat_w_cm2"), 0),
                      "参考限值": "—", "结论": "返回舱模型给出"})
    return {"mission_type": mtype, "is_crewed": mtype == "crewed",
            "items": items,
            "note": "阈值取自公开经验值，仅用于提示；不是任务判据，也不构成安全性结论。"}


# --------------------------------------------------------------------------- #
# 4. 输出完整性 / 成果清单 / 摘要
# --------------------------------------------------------------------------- #
def expected_outputs(cfg: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """→ [(相对路径, 说明, 分组)]，按任务类型与模块开关给出应产出清单。"""
    mtype = mission_type(cfg)
    mods = (cfg.get("_v3") or {}).get("modules") or {}
    out: List[Tuple[str, str, str]] = [(p, d, "通用") for p, d in EXPECTED_COMMON]
    if mtype != "return":
        out += [(p, d, "上升段") for p, d in EXPECTED_LAUNCH]
        if mods.get("window", False):
            out += [(p, d, "上升段") for p, d in EXPECTED_WINDOW]
        if mods.get("sky", True) or mods.get("appear", True):
            out += [(p, d, "天空/地图") for p, d in EXPECTED_SKY]
        if (cfg.get("station") or {}).get("enabled"):
            out += [(p, d, "空间站/交会") for p, d in EXPECTED_STATION]
        if mods.get("mc"):
            out += [(p, d, "不确定度") for p, d in EXPECTED_MC]
    if (cfg.get("return_capsule") or {}).get("enabled") or mtype == "return":
        out += [(p, d, "返回段") for p, d in EXPECTED_RETURN]
    return out


def artifact_inventory(workdir: str, cfg: Dict[str, Any],
                       steps: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """扫描工作区全部成果 + 步骤台账 + 完整性核对。"""
    files: List[Dict[str, Any]] = []
    for root, _dirs, names in os.walk(workdir):
        for nm in names:
            p = os.path.join(root, nm)
            rel = os.path.relpath(p, workdir).replace("\\", "/")
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            group = rel.split("/")[0] if "/" in rel else "根目录"
            files.append({"path": rel, "size": size, "group": group})
    files.sort(key=lambda x: (x["group"], x["path"]))
    # 完整性核对
    have = {f["path"] for f in files}
    expected = []
    for rel, desc, group in expected_outputs(cfg):
        expected.append({"path": rel, "desc": desc, "group": group,
                         "present": rel in have})
    missing = [e for e in expected if not e["present"]]
    # 步骤台账
    ledger = []
    for s in steps or []:
        ledger.append({"步骤": s.get("name") or s.get("key"),
                       "状态": s.get("state") or ("完成" if s.get("ok") else "失败"),
                       "耗时(s)": s.get("seconds"),
                       "说明": s.get("reason") or s.get("error") or ""})
    return {"files": files, "total": len(files),
            "bytes": sum(f["size"] for f in files),
            "expected": expected, "missing": missing, "steps": ledger}


def _group_files(files: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for f in files:
        out.setdefault(f["group"], []).append(f)
    return out


def _hsize(n: float) -> str:
    if n >= 1024 * 1024:
        return "%.1f MB" % (n / 1048576.0)
    if n >= 1024:
        return "%.0f KB" % (n / 1024.0)
    return "%d B" % int(n)


def build_summary(workdir: str, cfg: Dict[str, Any],
                  inv: Dict[str, Any], nodes: Dict[str, Any],
                  stats: Dict[str, Any], phases: List[Dict[str, Any]],
                  crew: Dict[str, Any]) -> Dict[str, Any]:
    m = cfg.get("mission") or {}
    mtype = mission_type(cfg)
    site = m.get("launch_site") or {}
    seq = cfg.get("launch_sequence") or {}
    ref = m.get("reference_orbit") or {}
    data: Dict[str, Any] = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "disclaimer": m.get("disclaimer") or DECL_CN,
        "mission": {
            "name": m.get("name"), "type": mtype, "type_cn": MT_CN.get(mtype, mtype),
            "rocket": m.get("rocket"), "serial": m.get("serial"),
            "site": {"name": site.get("name"), "lat_deg": site.get("lat_deg"),
                     "lon_deg": site.get("lon_deg"),
                     "alt_m": site.get("alt_m_ellipsoid")},
            "t0_utc": m.get("t0_utc"), "t0_bjt": _bj(m.get("t0_utc")),
            "t0_note": m.get("t0_note"),
            "reference_orbit": ref or None,
            "payload_note": m.get("payload_note"),
            "custom_rocket": bool(m.get("custom_rocket")),
            "auto_observer": bool(m.get("auto_observer")),
            "launch_sequence": {"custom": bool(seq.get("custom")),
                                "values": seq.get("values")},
        },
        "key_numbers": {},
        "stats": stats,
        "nodes": nodes.get("rows") or [],
        "phases": phases,
        "crew": crew,
        "warnings": (cfg.get("_v3") or {}).get("warnings") or [],
        "low_confidence": [
            {"label": p.get("label"), "value": p.get("value"),
             "source": p.get("source"), "confidence": p.get("confidence")}
            for p in (m.get("_provenance") or [])
            if p.get("confidence") == "低" or p.get("source") in ("infer", "default")],
        "artifacts": {"total": inv.get("total"), "bytes": inv.get("bytes"),
                      "missing": [e["path"] for e in inv.get("missing") or []]},
        "steps": inv.get("steps") or [],
    }
    kn = data["key_numbers"]
    t0w = m.get("t0_window") or {}
    if t0w.get("applied"):
        kn["T0 反推"] = "%s → %s（%+.0f s）" % (
            _utc_short(t0w.get("nominal_t0_utc") or ""),
            _utc_short(t0w.get("recommended_t0_utc") or ""),
            float(t0w.get("shift_s") or 0.0))
    orb = stats.get("orbit") or {}
    if orb.get("a_km"):
        kn["入轨"] = "近地点 %s km / 远地点 %s km / 倾角 %s° / e=%s" % (
            _fmt(orb.get("perigee_km"), 0), _fmt(orb.get("apogee_km"), 0),
            _fmt(orb.get("i_deg"), 2), _fmt(orb.get("e"), 4))
    tr = stats.get("trajectory") or {}
    if tr:
        kn["弹道"] = "时长 %s s；最大高度 %s km；最大速度 %s m/s" % (
            _fmt(tr.get("duration_s"), 0), _fmt(tr.get("max_altitude_km"), 1),
            _fmt(tr.get("max_speed_m_s"), 0))
    if stats.get("max_q"):
        kn["最大动压"] = "%s kPa @ T+%s s" % (
            _fmt((stats["max_q"].get("pa") or 0) / 1000.0, 1),
            _fmt(stats["max_q"].get("t_met_s"), 0))
    if stats.get("impacts"):
        n_in = sum(1 for i in stats["impacts"] if i.get("inside_zone"))
        kn["残骸落点"] = "%d/%d 在对应危险区内" % (n_in, len(stats["impacts"]))
    if stats.get("windows"):
        w = stats["windows"]
        if w.get("practical"):
            kn["发射窗口（航警）"] = "%s ~ %s UTC（%s min）" % (
                _utc_short(w["practical"].get("start_utc")),
                _utc_short(w["practical"].get("end_utc")),
                _fmt(w["practical"].get("duration_min"), 1))
        if w.get("orbit_available"):
            kn["轨道平面窗口"] = "%s 个；最近 %s" % (
                _fmt(w.get("orbit_n"), 0), _utc_short(w.get("orbit_best") or ""))
    if stats.get("observers"):
        o0 = list(stats["observers"].items())[0][1]
        if o0.get("first_appearance_utc"):
            kn["天空可见"] = "首次出现 %s（方位 %s°、仰角 %s°），最大仰角 %s°" % (
                _bj(o0["first_appearance_utc"]), _fmt(o0.get("first_az_deg"), 1),
                _fmt(o0.get("first_el_deg"), 2), _fmt(o0.get("max_el_deg"), 1))
    if stats.get("station"):
        st0 = stats["station"]
        kn["空间站"] = "%s（%s）" % (st0.get("name"), st0.get("source"))
    if stats.get("docking"):
        d0 = stats["docking"]
        dk = d0.get("docking") or {}
        kn["交会对接"] = "%s；总 ΔV %s m/s；对接 %s（星下点 %s°N, %s°E，高度 %s km）" % (
            "可行" if d0.get("feasible") else "不可行",
            _fmt(d0.get("total_dv_mps"), 0), _utc_short(dk.get("t_utc") or ""),
            _fmt(dk.get("station_lat_deg"), 2), _fmt(dk.get("station_lon_deg"), 2),
            _fmt(dk.get("alt_km"), 1))
        if dk.get("station_lat_deg") is not None:
            kn["对接点"] = "%s°N, %s°E @ %s UTC" % (
                _fmt(dk.get("station_lat_deg"), 3), _fmt(dk.get("station_lon_deg"), 3),
                _utc_short(dk.get("t_utc") or ""))
        _pl = d0.get("plane") or {}
        if _pl.get("residual_deg") is not None:
            kn["入轨面残差"] = "%s°（与空间站面夹角）" % _fmt(_pl.get("residual_deg"), 3)
    if stats.get("return"):
        r0 = stats["return"].get("selected") or {}
        if r0.get("t_landing_utc"):
            kn["返回"] = "制动 %s UTC → 落地 %s UTC（%s°N, %s°E，飞行 %s min）" % (
                _utc_short(r0.get("t_deorbit_utc")), _utc_short(r0.get("t_landing_utc")),
                _fmt(r0.get("lat_deg"), 2), _fmt(r0.get("lon_deg"), 2),
                _fmt((_f(r0.get("flight_time_s"), 0.0) or 0.0) / 60.0, 1))
    if stats.get("monte_carlo"):
        kn["蒙特卡洛"] = "N=%s" % _fmt((stats["monte_carlo"] or {}).get("n"), 0)
    return data


def render_summary_md(s: Dict[str, Any], inv: Dict[str, Any]) -> str:
    m = s.get("mission") or {}
    L: List[str] = []
    L.append("# 任务摘要 · %s" % (m.get("name") or "（未命名）"))
    L.append("")
    L.append("> %s" % (s.get("disclaimer") or DECL_CN))
    L.append(">")
    L.append("> 本摘要由「火箭弹道工具箱 v4」在运行结束时自动生成（%s）。"
             "同目录另有 `弹道仿真报告.md`（详细）、`成果清单.md`（全量文件与步骤台账）。"
             % _bj(s.get("generated_utc")))
    L.append("")
    # 1 基本信息
    L.append("## 1. 任务")
    L.append("")
    rows = [["任务类型", m.get("type_cn") or m.get("type") or "—"],
            ["火箭", m.get("rocket") or "—"],
            ["发射工位", ("%s（%s°N, %s°E, %s m）" % (
                (m.get("site") or {}).get("name") or "—",
                _fmt((m.get("site") or {}).get("lat_deg"), 4),
                _fmt((m.get("site") or {}).get("lon_deg"), 4),
                _fmt((m.get("site") or {}).get("alt_m"), 0)))
             if (m.get("site") or {}).get("name") else "不适用（返回任务）"],
            ["T0（UTC）", _utc_short(m.get("t0_utc"))],
            ["T0（北京时）", m.get("t0_bjt") or "—"]]
    ref = m.get("reference_orbit") or {}
    if ref:
        rows.append(["参考轨道", "h=%s km, i=%s°%s" % (
            _fmt(ref.get("alt_km"), 0), _fmt(ref.get("i_deg"), 2),
            ("，e=%s" % _fmt(ref.get("e"), 4)) if ref.get("e") else "")])
    if m.get("custom_rocket"):
        rows.append(["自定义箭型", "是（参数见数据体检 / config.vehicle）"])
    if m.get("auto_observer"):
        rows.append(["观测点", "未填写 → 默认使用发射场"])
    L.append(_table(["项目", "内容"], rows))
    L.append("")
    # 2 关键数字
    kn = s.get("key_numbers") or {}
    L.append("## 2. 关键数字")
    L.append("")
    if kn:
        L.append(_table(["项目", "结果"], [[k, v] for k, v in kn.items()]))
        L.append("")
    else:
        L.append("_（本次未产生可汇总的关键数字，见成果清单与运行日志）_")
        L.append("")
    # 3 关键节点
    nodes = s.get("nodes") or []
    if nodes:
        L.append("## 3. 关键节点")
        L.append("")
        rows = []
        for r in nodes[:40]:
            rows.append([str(r.get("序号")), str(r.get("类别")), str(r.get("节点")),
                         _fmt(r.get("T+ (s)"), 1), _utc_short(r.get("时刻(UTC)")),
                         ("%s, %s" % (_fmt(r.get("纬度(°)"), 3), _fmt(r.get("经度(°)"), 3))
                          if r.get("纬度(°)") is not None else "—"),
                         _fmt(r.get("高度(km)"), 1), _fmt(r.get("速度(m/s)"), 0)])
        L.append(_table(["#", "类别", "节点", "T+ (s)", "时刻(UTC)", "位置", "高度(km)",
                         "速度(m/s)"], rows))
        L.append("")
    # 4 阶段
    phases = s.get("phases") or []
    if phases:
        L.append("## 4. 飞行阶段")
        L.append("")
        L.append(_table(["#", "阶段", "开始(UTC)", "结束(UTC)", "时长(s)", "高度范围(km)"],
                        [[str(p.get("序号")), p.get("阶段"), _utc_short(p.get("开始(UTC)")),
                          _utc_short(p.get("结束(UTC)")), _fmt(p.get("时长(s)"), 1),
                          p.get("高度范围(km)")] for p in phases]))
        L.append("")
    # 5 载人关注
    crew = s.get("crew") or {}
    if crew.get("items"):
        L.append("## 5. 载人与飞行环境关注项")
        L.append("")
        L.append(_table(["项目", "数值", "参考限值", "结论"],
                        [[i.get("项目"), i.get("数值"), i.get("参考限值"), i.get("结论")]
                         for i in crew["items"]]))
        L.append("")
        L.append("> %s" % (crew.get("note") or ""))
        L.append("")
    # 6 输出完整性
    L.append("## 6. 成果与输出完整性")
    L.append("")
    L.append("本次共产出 **%s 个文件、%s**。完整清单见 `成果清单.md`。" % (
        _fmt(inv.get("total"), 0), _hsize(inv.get("bytes") or 0)))
    L.append("")
    missed = inv.get("missing") or []
    if missed:
        L.append("以下应产出项本次缺失（原因见 `成果清单.md` 的步骤台账）：")
        L.append("")
        for e in missed[:12]:
            L.append("- `%s`（%s）" % (e.get("path"), e.get("desc")))
        L.append("")
    else:
        L.append("按当前任务类型与模块开关核对，**应产出项全部齐备**。")
        L.append("")
    steps = inv.get("steps") or []
    bad = [x for x in steps if x.get("状态") not in ("完成", "不适用")]
    if bad:
        L.append("步骤台账（非完成项）：")
        L.append("")
        for x in bad:
            L.append("- %s：%s %s" % (x.get("步骤"), x.get("状态"),
                                      ("— " + str(x.get("说明"))) if x.get("说明") else ""))
        L.append("")
    # 7 提示
    warns = s.get("warnings") or []
    low = s.get("low_confidence") or []
    if warns or low:
        L.append("## 7. 提示与不确定性")
        L.append("")
        if warns:
            L.append("**运行提示（%d 条）：**" % len(warns))
            L.append("")
            for w in warns[:16]:
                L.append("- " + str(w))
            L.append("")
        low_conf = [p for p in low if str(p.get("confidence")) == "低"]
        inferred = [p for p in low if str(p.get("confidence")) != "低"]
        if low_conf:
            L.append("**低置信度字段（%d 条，引用结论时必须注明不确定性）：**"
                     % len(low_conf))
            L.append("")
            for p in low_conf[:16]:
                L.append("- %s = %s（%s / %s）" % (p.get("label"), p.get("value"),
                                                  p.get("source"), p.get("confidence")))
            L.append("")
        if inferred:
            L.append("**推断字段（%d 条，置信度中/高，可参考）：**" % len(inferred))
            L.append("")
            for p in inferred[:16]:
                L.append("- %s = %s（%s）" % (p.get("label"), p.get("value"),
                                             p.get("confidence")))
    L.append("## 8. 目录索引")
    L.append("")
    L.append("- `sim/` 弹道、事件、落点、入轨、关键节点、飞行阶段、环境剖面")
    L.append("- `sky/` 天空轨迹图与数据；`map/` 全国最大高度角地图与落区图")
    L.append("- `viz/` 三维 KML/KMZ；`report/` 报告、自检、摘要、成果清单")
    L.append("- `uncertainty/` 蒙特卡洛（若启用）")
    L.append("")
    L.append("---")
    L.append("")
    L.append("*%s*" % DECL_CN)
    L.append("")
    return "\n".join(L)


def render_inventory_md(inv: Dict[str, Any], cfg: Dict[str, Any],
                        s: Dict[str, Any]) -> str:
    L: List[str] = []
    L.append("# 成果清单与步骤台账")
    L.append("")
    L.append("> 工作区：`%s`" % ((cfg.get("_workdir") or "")))
    L.append(">")
    L.append("> 共 **%s 个文件 / %s**；由「火箭弹道工具箱 v4」自动扫描（%s）。"
             % (_fmt(inv.get("total"), 0), _hsize(inv.get("bytes") or 0),
                _bj(s.get("generated_utc"))))
    L.append("")
    L.append("## 1. 步骤台账")
    L.append("")
    steps = inv.get("steps") or []
    if steps:
        L.append(_table(["步骤", "状态", "耗时(s)", "说明"],
                        [[x.get("步骤"), x.get("状态"), _fmt(x.get("耗时(s)"), 1),
                          x.get("说明")] for x in steps]))
    else:
        L.append("_（命令行直接调用引擎脚本时无步骤台账）_")
        L.append("")
    L.append("## 2. 输出完整性核对")
    L.append("")
    exp = inv.get("expected") or []
    if exp:
        rows = []
        for e in exp:
            rows.append([e.get("group"), "`%s`" % e.get("path"), e.get("desc"),
                         "✔ 已产出" if e.get("present") else "✘ 缺失"])
        L.append(_table(["分组", "文件", "说明", "状态"], rows))
    L.append("## 3. 文件清单")
    L.append("")
    groups = _group_files(inv.get("files") or [])
    for g in sorted(groups):
        L.append("### %s/（%d 个文件）" % (g, len(groups[g])))
        L.append("")
        L.append(_table(["文件", "大小"], [["`%s`" % f["path"].split("/", 1)[-1],
                                           _hsize(f["size"])] for f in groups[g]]))
    L.append("---")
    L.append("")
    L.append("*%s*" % DECL_CN)
    L.append("")
    return "\n".join(L)


def _table(headers: List[str], rows: List[List[Any]]) -> str:
    """Markdown 表格（转义竖线，避免单元格内容破坏表格）。"""
    def cell(v: Any) -> str:
        return str("" if v is None else v).replace("|", "\\|").replace("\n", " ")
    L = ["| " + " | ".join(headers) + " |",
         "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        L.append("| " + " | ".join(cell(c) for c in r) + " |")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# 5. 入口
# --------------------------------------------------------------------------- #
def generate_all(workdir: str, cfg: Dict[str, Any],
                 steps: Optional[List[Dict[str, Any]]] = None,
                 logger=None) -> Dict[str, Any]:
    """生成全部通用成果。返回 ``{"ok":bool,"files":[...],"error":str}``。

    任何子步骤失败都不影响其它子步骤（输出最大化优先）。
    """
    def log(msg: str) -> None:
        if logger:
            try:
                logger(msg)
            except Exception:  # noqa: BLE001
                pass

    workdir = os.path.abspath(workdir)
    produced: List[str] = []
    errors: List[str] = []
    cfg = dict(cfg or {})
    cfg["_workdir"] = workdir
    sim = os.path.join(workdir, "sim")
    rep = os.path.join(workdir, "report")
    os.makedirs(sim, exist_ok=True)
    os.makedirs(rep, exist_ok=True)

    # ---- 关键节点 ----
    try:
        nodes = key_nodes(workdir, cfg)
        rows = nodes.get("rows") or []
        if rows:
            _write_csv(os.path.join(sim, "关键节点.csv"),
                       ["序号", "类别", "节点", "T+ (s)", "时刻(UTC)", "北京时",
                        "纬度(°)", "经度(°)", "高度(km)", "速度(m/s)", "质量(kg)", "说明"],
                       rows)
            produced.append("sim/关键节点.csv")
            log("关键节点：%d 条（%s）" % (len(rows), "、".join(nodes.get("by_category", {}))))
        else:
            log("关键节点：本次没有可提取的事件（事件表为空）")
    except Exception as exc:  # noqa: BLE001
        nodes = {"rows": [], "by_category": {}}
        errors.append("关键节点：%s" % exc)

    # ---- 飞行阶段 ----
    try:
        phases = flight_phases(workdir, cfg)
        if phases:
            _write_csv(os.path.join(sim, "飞行阶段.csv"),
                       ["序号", "阶段", "开始(UTC)", "结束(UTC)", "时长(s)",
                        "高度范围(km)", "速度范围(m/s)", "说明"], phases)
            produced.append("sim/飞行阶段.csv")
            log("飞行阶段：%d 段" % len(phases))
    except Exception as exc:  # noqa: BLE001
        phases = []
        errors.append("飞行阶段：%s" % exc)

    # ---- 环境剖面 + 统计 ----
    try:
        stats = trajectory_stats(workdir, cfg)
        if mission_type(cfg) != "return":
            env = _env_profile(workdir)
            if env:
                _write_csv(os.path.join(sim, "飞行环境.csv"),
                           ["t_met_s", "t_utc", "alt_km", "speed_m_s", "q_pa",
                            "accel_g", "phase", "mass_kg"],
                           [dict(e, t_utc=e.get("t_utc") or "") for e in env])
                produced.append("sim/飞行环境.csv")
                log("飞行环境：%d 点（动压/过载剖面）" % len(env))
        _write_json(os.path.join(sim, "轨迹统计.json"), stats)
        produced.append("sim/轨迹统计.json")
    except Exception as exc:  # noqa: BLE001
        stats = {}
        errors.append("轨迹统计：%s" % exc)

    # ---- 载人关注项 ----
    try:
        crew = crew_items(stats or {}, cfg)
        if crew.get("items"):
            _write_json(os.path.join(sim, "飞行环境关注项.json"), crew)
            produced.append("sim/飞行环境关注项.json")
    except Exception as exc:  # noqa: BLE001
        crew = {}
        errors.append("载人关注项：%s" % exc)

    # ---- 清单 + 摘要（摘要需要清单里的完整性信息，故清单先算） ----
    try:
        inv = artifact_inventory(workdir, cfg, steps=steps)
    except Exception as exc:  # noqa: BLE001
        inv = {"files": [], "total": 0, "bytes": 0, "expected": [], "missing": [],
               "steps": []}
        errors.append("成果清单扫描：%s" % exc)
    try:
        summary = build_summary(workdir, cfg, inv, nodes or {}, stats or {},
                                phases or [], crew or {})
        _write_json(os.path.join(rep, "任务摘要.json"), summary)
        produced.append("report/任务摘要.json")
        md = render_summary_md(summary, inv)
        with open(os.path.join(rep, "任务摘要.md"), "w", encoding="utf-8") as f:
            f.write(md)
        produced.append("report/任务摘要.md")
        log("任务摘要：%d 个字，%d 个关键节点" % (len(md), len(summary.get("nodes") or [])))
    except Exception as exc:  # noqa: BLE001
        summary = {}
        errors.append("任务摘要：%s" % exc)

    # 清单要包含刚写的摘要文件：重新扫描一次（很快）
    try:
        inv = artifact_inventory(workdir, cfg, steps=steps)
        _write_json(os.path.join(rep, "成果清单.json"),
                    {"total": inv["total"], "bytes": inv["bytes"],
                     "files": inv["files"], "expected": inv["expected"],
                     "missing": inv["missing"], "steps": inv["steps"]})
        with open(os.path.join(rep, "成果清单.md"), "w", encoding="utf-8") as f:
            f.write(render_inventory_md(inv, dict(cfg, _workdir=workdir), summary or {}))
        produced.extend(["report/成果清单.json", "report/成果清单.md"])
        log("成果清单：%d 个文件，缺失 %d 项" % (inv["total"], len(inv["missing"])))
    except Exception as exc:  # noqa: BLE001
        errors.append("成果清单：%s" % exc)

    for e in errors:
        log("[警告] 通用成果生成问题：%s" % e)
    return {"ok": not errors, "files": produced, "errors": errors,
            "nodes": len((nodes or {}).get("rows") or []),
            "phases": len(phases or []),
            "missing": [m["path"] for m in (inv.get("missing") or [])]}
