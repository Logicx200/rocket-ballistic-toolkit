# -*- coding: utf-8 -*-
"""MissionInput → 完整 config.json + 数据体检单（provenance / gaps）。

这一步是"离线独立运行"的核心：用户只填最少信息，缺失字段由
:mod:`app.core.infer` 从内置库/经验规则/航警几何补出来，并逐条记录来源与置信度。
"""
from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import autoplan
from . import infer as I
from . import notam_parse as NP
from . import paths as P
from .model import (MISSION_TYPE_CN, BuildResult, MissionInput, NotamInput,
                    Resolution, fmt_num)

DISCLAIMER = "仅供公开信息分析与教学使用，非实际发射弹道、非官方信息"

#: 航警 stage_hint 与残骸对象的匹配（与 engine/scripts/invert.py 的规则保持一致）
FAIRING_KEYS = ("罩", "fair")


def _res(field: str, label: str, value: Any, source: str, conf: str,
         note: str = "") -> Resolution:
    return Resolution(field=field, label=label, value=fmt_num(value), source=source,
                      confidence=conf, note=note)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ref_year_from_notams(notams: List[Dict[str, Any]]) -> Optional[int]:
    for z in notams:
        for k in ("b_utc", "c_utc"):
            dt = I._iso_to_dt(z.get(k, ""))
            if dt:
                return dt.year
    return None


def _validate_hints(notams: List[Dict[str, Any]], vehicle: Dict[str, Any]) -> List[str]:
    """检查 stage_hint 与残骸对象名的对应关系（不匹配会导致反演目标函数形同虚设）。"""
    warnings: List[str] = []
    stages = [s.get("name", "") for s in (vehicle.get("stages") or [])]
    targets = set()
    for i, nm in enumerate(stages[:-1]):
        targets.add("stage_%d" % i)
    if vehicle.get("fairing"):
        targets.add("fairing")
    if vehicle.get("boosters"):
        targets.add("boosters")
    for z in notams:
        hint = z.get("stage_hint", "")
        if not hint:
            continue
        matched = None
        for i, nm in enumerate(stages):
            if hint in nm or nm in hint:
                matched = "stage_%d" % i
                break
        if matched is None and any(k in hint.lower() or k in hint for k in FAIRING_KEYS):
            matched = "fairing"
        if matched is not None and matched not in targets:
            warnings.append("航警 %s 的类型「%s」对应 %s，但该对象不会产生残骸落点（如末级入轨），"
                            "该区不参与落点判定" % (z.get("id", "?"), hint, matched))
        elif matched is None:
            warnings.append("航警 %s 的类型「%s」匹配不到任何残骸对象（级名：%s），"
                            "该区不参与反演/窗口计算" % (
                                z.get("id", "?"), hint, "、".join(stages)))
    return warnings


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def _return_only_station_cfg(inp: MissionInput) -> Dict[str, Any]:
    tle = [ln.strip() for ln in str(inp.station_tle_text or "").splitlines() if ln.strip()]
    if len(tle) >= 3 and not tle[0].startswith("1 "):
        tle = tle[1:]
    cfg = {
        "enabled": True,
        "name": inp.station_name or "天宫空间站",
        "norad_id": int(inp.station_norad or 48274),
        "source": str(inp.station_source or "auto"),
        "oem_path": inp.station_oem_path or "",
        "rendezvous": {
            "target_time_h": float(inp.rendezvous_h or 6.5),
            "max_phasing_h": max(6.0, 2.0 * float(inp.rendezvous_h or 6.5)),
            "dv_budget_mps": float(inp.dv_budget_mps or 300.0),
            "final_approach_dv_mps": 10.0,
            "final_approach_s": 1800.0,
            "max_phasing_orbits": 10,
            "min_phasing_orbits": 1,
        },
    }
    if len(tle) >= 2:
        cfg["tle"] = tle[:2]
    return cfg


def _return_only_return_cfg(inp: MissionInput) -> Dict[str, Any]:
    return {
        "enabled": True,
        "use_current_notams": bool(inp.return_use_notams),
        "landing_site": {
            "name": inp.return_landing_name or "四子王旗着陆场",
            "lat_deg": float(inp.return_landing_lat if inp.return_landing_lat is not None
                             else 42.27),
            "lon_deg": float(inp.return_landing_lon if inp.return_landing_lon is not None
                             else 111.70),
            "radius_km": float(inp.return_landing_radius_km or 50.0),
        },
        "search_start_utc": str(inp.return_search_start or "").strip() or None,
        "search_end_utc": str(inp.return_search_end or "").strip() or None,
        "deorbit_dv_mps": float(inp.return_dv_mps or 120.0),
        "ld_ratio": float(inp.return_ld_ratio or 0.30),
        "bank_deg": (None if inp.return_bank_deg is None else float(inp.return_bank_deg)),
        "auto_dv": bool(inp.return_auto_dv),
        "auto_bank": bool(inp.return_auto_bank),
        "mass_kg": 3000.0,
        "cd_area_m2": 5.0,
        "drogue_cd_area_m2": 20.0,
        "chute_cd_area_m2": 1200.0,
        "drogue_deploy_km": 10.0,
        "main_deploy_km": 6.0, "undock_lead_s": 3600.0, "orbital_sep_lead_s": 600.0, "burn_duration_s": 150.0, "prop_sep_delay_s": 300.0,
        "coarse_step_s": float(inp.return_coarse_step_s or 300.0),
        "scan_step_s": float(inp.return_scan_step_s or 10.0),
    }


def _build_return_only_config(inp: MissionInput, workdir: Optional[str], logger,
                              write: bool) -> BuildResult:
    """仅返回任务：不需要发射场/火箭/上升段，只保留空间站 + 返回航警 + 返回舱。"""
    def log(msg: str) -> None:
        if logger:
            try:
                logger(msg)
            except Exception:  # noqa: BLE001
                pass

    resolutions: List[Resolution] = []
    warnings: List[str] = []
    raw_notams: List[Dict[str, Any]] = []
    if inp.notams:
        for z in inp.notams:
            d = z.as_config() if isinstance(z, NotamInput) else dict(z)
            if d.get("b_utc") and d.get("c_utc") and len(d.get("polygon_latlon") or []) >= 3:
                raw_notams.append(d)
            else:
                warnings.append("返回航警 %s 字段不完整，已忽略" % (d.get("id") or "?"))
    elif inp.notams_text.strip():
        parsed, w = NP.parse_notams(inp.notams_text, datetime.now(timezone.utc).year)
        warnings.extend(w)
        raw_notams.extend([z for z in parsed
                           if z.get("b_utc") and z.get("c_utc")
                           and len(z.get("polygon_latlon") or []) >= 3])
    log("[return-only] 返回任务：不需要发射场/火箭/上升段；解析航警 %d 条" % len(raw_notams))

    t0 = None
    t0_note = ""
    if inp.t0_text.strip():
        t0, _tz, note = NP.parse_t0_text(inp.t0_text, inp.tz_hint or "auto",
                                         datetime.now(timezone.utc).year)
        t0_note = "用户给定返回标称时刻：" + note
    if t0 is None and raw_notams:
        bs = [I._iso_to_dt(z.get("b_utc", "")) for z in raw_notams]
        cs = [I._iso_to_dt(z.get("c_utc", "")) for z in raw_notams]
        bs = [x for x in bs if x is not None]
        cs = [x for x in cs if x is not None]
        if bs:
            b0 = min(bs)
            c0 = max(cs) if cs else b0 + timedelta(hours=1)
            t0 = b0 + (c0 - b0) / 2.0
            t0_note = "由返回航警 B)/C) 中点推断（仅作搜索中心，不参与判据）"
    if t0 is None:
        t0 = datetime.now(timezone.utc).replace(microsecond=0)
        t0_note = "未给返回时刻/航警，取当前 UTC 作为搜索中心"
    t0_iso = t0.strftime("%Y-%m-%dT%H:%M:%SZ")
    resolutions.append(_res("mission.t0_utc", "返回标称时刻", t0_iso, "user" if inp.t0_text else
                            "infer", "高" if inp.t0_text else "中", t0_note))
    resolutions.append(_res("mission.profile", "任务类型", "返回舱返回（无发射场/上升段）",
                            "user", "高",
                            "return_only / mission_type=return：不计算发射弹道、残骸落区与"
                            "交会对接；返回段独立产出时序、事件、落点、KML、报告与通用摘要"))
    for z in raw_notams:
        resolutions.append(_res("notams.%s" % z.get("id", "?"), "返回航警区",
                                "%s ~ %s，%d 顶点" % (z.get("b_utc", ""), z.get("c_utc", ""),
                                                     len(z.get("polygon_latlon") or [])),
                                "user", "高", "仅用于返回段落点/落地时间判定"))

    station_cfg = _return_only_station_cfg(inp)
    return_cfg = _return_only_return_cfg(inp)
    # v4：返回任务同样尊重用户的模块开关（默认把能算的都算上）。
    # 与上升段无关的模块（dem/invert/export/window/sky/appear/mc）不适用，显式标记为
    # 「不适用」由 runner 记入步骤台账，而不是静默消失。
    from .model import DEFAULT_MODULES
    modules = dict(DEFAULT_MODULES)
    modules.update(inp.modules or {})
    for k in ("dem", "invert", "export", "window", "sky", "appear", "mc", "docking"):
        modules[k] = False
    modules["station"] = True
    modules["return"] = True
    modules["kml"] = bool(modules.get("kml", True))
    modules["report"] = bool(modules.get("report", True))
    modules["qc"] = bool(modules.get("qc", True))
    # 返回段无上升弹道：勾选了相关模块时明确提示，避免用户以为程序漏跑了
    if any(bool((inp.modules or {}).get(k)) for k in ("sky", "appear", "map", "mc")):
        warnings.append("返回任务的天空轨迹/出现方位/全国地图/蒙特卡洛依赖上升段弹道，"
                        "本任务不计算；返回段已有：返回舱弹道图、落区图、时序与事件表")
    name = inp.name.strip() or "返回舱返回任务 %s" % t0.strftime("%Y-%m-%d %H:%M")
    config: Dict[str, Any] = {
        "mission": {
            "name": name,
            "profile": "return",
            "mission_type": "return",
            "t0_utc": t0_iso,
            "t0_note": t0_note,
            "t0_timezone_used": "UTC",
            "disclaimer": "仅供公开信息分析与教学使用，非实际返回/回收程序、非官方信息",
            "version": "v4.0",
        },
        "return_only": True,
        "notams": raw_notams,
        "observers": [],
        "station": station_cfg,
        "return_capsule": return_cfg,
        "_v3": {
            "profile": "return",
            "return_only": True,
            "mission_type": "return",
            "modules": modules,
            "notams_mode": "provided" if raw_notams else "off",
            "dem_mode": "none",
            "needs_refine_t0": False,
            "station_enabled": True,
            "auto_observer": bool(getattr(inp, "auto_observer", True)),
            "auto_observer_used": False,
            "custom_rocket": False,
            "warnings": warnings,
        },
    }
    if inp.notes:
        config["mission"]["user_notes"] = inp.notes
    path = ""
    if write and workdir:
        os.makedirs(workdir, exist_ok=True)
        path = os.path.join(workdir, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    summary = "\n".join([
        "返回舱返回任务（无发射场/上升段）",
        "标称时刻：%s" % t0_iso,
        "着陆场：%s (%.4f, %.4f, R=%.0f km)" % (
            return_cfg["landing_site"]["name"], return_cfg["landing_site"]["lat_deg"],
            return_cfg["landing_site"]["lon_deg"], return_cfg["landing_site"]["radius_km"]),
        "返回航警：%d 条" % len(raw_notams),
        "空间站：%s（%s）" % (station_cfg["name"], station_cfg["source"]),
    ])
    return BuildResult(config=config, path=path, resolutions=resolutions,
                       gaps=[], warnings=warnings, needs_refine_t0=False,
                       summary=summary)


def _launch_sequence_defaults(vehicle: Dict[str, Any], guidance: Dict[str, Any]) -> Dict[str, Any]:
    stages = list(vehicle.get("stages") or [])
    boost = vehicle.get("boosters") or {}
    fair = vehicle.get("fairing") or {}
    return {
        "boosters_sep_s": boost.get("jettison_met"),
        "stage_sep_s": [s.get("sep_met") for s in stages],
        "fairing_sep_s": (fair.get("sep_met") if fair.get("sep_met") is not None
                          else guidance.get("t_fairing")),
        "seco_s": (stages[-1].get("seco_met") if stages else None),
    }


#: 时序字段 → 中文名（GUI/报告/日志共用一套叫法）
SEQ_LABELS = {
    "boosters_sep_s": "助推器分离",
    "fairing_sep_s": "整流罩分离",
    "seco_s": "末级关机（SECO）",
}


def parse_seq_time(value: Any, t0_iso: str = "") -> Optional[float]:
    """把时序输入解析成「相对起飞秒」。

    支持：``206`` / ``"206.5"`` / ``"T+206"`` / ``"206s"`` / ``"206 s"``（相对秒），
    ``"2026-11-19T07:10:00Z"``（绝对 UTC 时刻，需给 t0）。空/无法解析返回 None。
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    s = s.replace("＋", "+").replace("，", ",").replace("秒", "s").replace("Ｓ", "S")
    up = s.upper()
    if up.startswith("T+"):
        s = s[2:].strip()
        up = s.upper()
    if up.endswith(("S", "秒")):
        s = s[:-1].strip()
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        pass
    # 绝对时刻：换算成相对 t0 的秒
    dt = I._iso_to_dt(s)
    if dt is None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    t0 = I._iso_to_dt(t0_iso) if t0_iso else None
    if dt is not None and t0 is not None:
        sec = (dt - t0).total_seconds()
        return sec if sec > 0 else None
    return None


def _apply_launch_sequence(inp: MissionInput, vehicle: Dict[str, Any],
                           guidance: Dict[str, Any], t0_iso: str = ""
                           ) -> Tuple[Dict[str, Any], List[Any]]:
    """把用户自定义发射时序写入 vehicle/guidance；未填的项保留箭型库默认。

    v4 修正/增强：
    * 兼容两种输入结构：扁平 ``{custom, boosters_sep_s, ...}`` 与嵌套
      ``{custom, values:{...}}``（v3 从历史 config 载入时是后者，会把自定义值丢掉）；
    * 支持 ``T+206`` / ``206s`` / 绝对 UTC 时刻三种写法（``parse_seq_time``）；
    * 给出「时序表」结构化结果（含每级名称、默认值、实际值、来源），供 GUI 与报告复用。
    """
    defaults = _launch_sequence_defaults(vehicle, guidance)
    seq = dict(inp.launch_sequence or {})
    custom = bool(seq.get("custom"))
    nested = seq.get("values") if isinstance(seq.get("values"), dict) else None
    raw = dict(nested or seq)
    warnings: List[Any] = []
    vals = dict(defaults)
    stages = list(vehicle.get("stages") or [])
    n_sep = max(0, len(stages) - 1)

    if custom:
        for key in ("boosters_sep_s", "fairing_sep_s", "seco_s"):
            v = raw.get(key)
            if v is None or str(v).strip() == "":
                continue
            sec = parse_seq_time(v, t0_iso)
            if sec is None:
                warnings.append("发射时序「%s」的输入「%s」无法识别（可用 206 / T+206 / "
                                "206s / 2026-11-19T07:10:00Z），已用默认值"
                                % (SEQ_LABELS.get(key, key), v))
            else:
                vals[key] = sec
        raw_stage = raw.get("stage_sep_s") or []
        st = list(vals.get("stage_sep_s") or [None] * len(stages))
        if not isinstance(raw_stage, (list, tuple)):
            raw_stage = [raw_stage]
        for i, v in enumerate(list(raw_stage)[:n_sep]):
            if v is None or str(v).strip() == "":
                continue
            sec = parse_seq_time(v, t0_iso)
            if sec is None:
                warnings.append("第 %d 级分离时刻「%s」无法识别，已用默认值" % (i + 1, v))
            else:
                st[i] = sec
        vals["stage_sep_s"] = st
        # 写回配置
        if vehicle.get("boosters") is not None:
            vehicle["boosters"]["jettison_met"] = vals.get("boosters_sep_s")
        for i, s in enumerate(stages):
            if i < n_sep:
                s["sep_met"] = (st[i] if i < len(st) else None)
            else:
                s["sep_met"] = None
        if stages:
            stages[-1]["seco_met"] = vals.get("seco_s")
        if vehicle.get("fairing") is not None:
            vehicle["fairing"]["sep_met"] = vals.get("fairing_sep_s")
        guidance["t_fairing"] = vals.get("fairing_sep_s")
        # 排序检查（只警告不拦截：型号可能有特殊时序）
        bs = vals.get("boosters_sep_s")
        ss = [x for x in (vals.get("stage_sep_s") or []) if x is not None]
        fa = vals.get("fairing_sep_s")
        se = vals.get("seco_s")
        for name, tv in (("助推器分离", bs), ("整流罩分离", fa), ("SECO", se)):
            if tv is not None and float(tv) <= 0:
                warnings.append("发射时序：%s 时刻应 > 0 s（当前 %s）" % (name, tv))
        if ss and se is not None and max(ss) >= float(se):
            warnings.append("发射时序：末级 SECO（%.1f s）不晚于第 %d 级分离（%.1f s），请检查"
                            % (float(se), len(ss), max(ss)))
        if fa is not None and se is not None and float(fa) >= float(se):
            warnings.append("发射时序：整流罩分离（%.1f s）晚于 SECO（%.1f s），请检查"
                            % (float(fa), float(se)))
        if bs is not None and fa is not None and float(bs) > float(fa) + 120:
            warnings.append("发射时序：助推器分离（%.0f s）明显晚于抛罩（%.0f s），请核对"
                            % (float(bs), float(fa)))
        # 与箭型默认差得太多时提醒：末级关机时刻直接决定入轨速度，偏几十秒就会不入轨
        _dfl = defaults
        for _k, _lbl, _tol in (("seco_s", "末级关机（SECO）", 45.0),
                               ("fairing_sep_s", "整流罩分离", 90.0),
                               ("boosters_sep_s", "助推器分离", 60.0)):
            _dv0 = parse_seq_time(raw.get(_k), t0_iso)
            _d0 = _dfl.get(_k)
            if _dv0 is not None and _d0 is not None and abs(float(_dv0) - float(_d0)) > _tol:
                warnings.append(
                    "发射时序：%s 自定义 %.1f s 与箭型默认 %.1f s 相差 %+.1f s（>%.0f s）。"
                    "末级关机时刻偏差过大会导致入轨高度/偏心率异常，请确认是有意为之"
                    % (_lbl, float(_dv0), float(_d0), float(_dv0) - float(_d0), _tol))

    # 结构化时序表（GUI/报告共用）：每行 = 项目 / 默认 / 实际 / 来源
    table: List[Dict[str, Any]] = []
    if vehicle.get("boosters") is not None:
        table.append({"key": "boosters_sep_s", "label": "助推器分离",
                      "default": defaults.get("boosters_sep_s"),
                      "value": vals.get("boosters_sep_s")})
    for i, s in enumerate(stages[:-1]):
        table.append({"key": "stage_%d_sep_s" % i,
                      "label": "%s关机/分离" % (s.get("name") or ("第%d级" % (i + 1))),
                      "default": (defaults.get("stage_sep_s") or [None] * len(stages))[i]
                      if i < len(defaults.get("stage_sep_s") or []) else None,
                      "value": (vals.get("stage_sep_s") or [None] * len(stages))[i]
                      if i < len(vals.get("stage_sep_s") or []) else None})
    if vehicle.get("fairing") is not None:
        table.append({"key": "fairing_sep_s", "label": "整流罩分离",
                      "default": defaults.get("fairing_sep_s"),
                      "value": vals.get("fairing_sep_s")})
    if stages:
        table.append({"key": "seco_s", "label": "末级关机（SECO）",
                      "default": defaults.get("seco_s"), "value": vals.get("seco_s")})
    for row in table:
        row["source"] = ("user" if (custom and row.get("value") is not None
                                    and row.get("value") != row.get("default")) else "default")
    return dict(custom=custom, source=("user" if custom else "default"),
                defaults=defaults, values=vals, table=table,
                warnings=[str(x) for x in warnings]), warnings


def build_config(inp: MissionInput, workdir: Optional[str] = None,
                 logger: Optional[Callable[[str], None]] = None,
                 dem_resolver: Optional[Callable[[float, float], Tuple[Optional[float], str]]] = None,
                 write: bool = True) -> BuildResult:
    """构建完整配置。

    Parameters
    ----------
    inp : MissionInput
        用户输入。
    workdir : str, optional
        任务工作区；给了就会把 ``config.json`` 写进去。
    logger : callable, optional
        进度日志回调（接收一行中文）。
    dem_resolver : callable, optional
        ``(lat, lon) -> (alt_m|None, "srtm"|"coarse"|None)``，由 ``DemManager.observer_alt`` 提供。
    write : bool
        是否写盘（GUI 预览体检表时可 False）。
    """
    def log(msg: str) -> None:
        if logger:
            try:
                logger(msg)
            except Exception:  # noqa: BLE001
                pass

    resolutions: List[Resolution] = []
    warnings: List[str] = []
    gaps: List[Resolution] = []

    # ---------------- 0. 任务类型（v4） ----------------
    # 三支：常规发射 / 载人（含空间站交会）/ 返回舱返回。
    # 返回任务不需要发射场与上升段，走独立构建分支（但仍产出通用成果，见 summary.py）。
    mt_req = str(getattr(inp, "mission_type", "auto") or "auto").lower()
    want_return = bool(getattr(inp, "return_only", False)) or \
        str(inp.profile or "").lower() == "return" or mt_req == "return"
    if want_return:
        return _build_return_only_config(inp, workdir, logger, bool(write))

    # ---------------- 1. 箭型 ----------------
    log("[1/8] 解析箭型…")
    rocket, rocket_why = (None, "")
    custom_rec = dict(getattr(inp, "custom_rocket", {}) or {})
    if custom_rec:
        from . import userdb as _U
        try:
            rocket = _U.normalize_rocket(custom_rec)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("自定义箭型数据无法解析：%s" % exc) from None
        rocket_why = ("用户自定义箭型（%s）" % (
            "已保存到用户库" if str(getattr(inp, "custom_rocket_mode", "")) == "user_db"
            else "仅本次任务"))
        _issues = _U.validate_rocket(rocket)
        _errs = [m for lv, _f, m in _issues if lv == "错误"]
        if _errs:
            raise ValueError("自定义箭型参数不完整，无法仿真：\n- " + "\n- ".join(_errs))
        warnings.extend(m for lv, _f, m in _issues if lv != "错误")
        resolutions.append(Resolution(
            "mission.custom_rocket", "自定义箭型参数", rocket.get("name") or "自定义箭型",
            "user", "中" if str(rocket.get("confidence")) != "高" else "高",
            "由「箭型参数编辑器」输入；%d 级%s；%s" % (
                len((rocket.get("vehicle") or {}).get("stages") or []),
                "，含助推器" if (rocket.get("vehicle") or {}).get("boosters") else "",
                ("；%d 条参数提醒（见数据体检）" % len(_issues)) if _issues else "")))
    if rocket is None and inp.rocket_id not in ("", "auto"):
        for r in I.rocket_list():
            if r.get("id") == inp.rocket_id:
                rocket, rocket_why = r, "按 id「%s」" % inp.rocket_id
                break
    if rocket is None and inp.rocket_label:
        rocket, rocket_why = I.match_rocket(inp.rocket_label)
    if rocket is None and inp.vehicle_overrides:
        rocket = copy.deepcopy(I._FALLBACK_ROCKETS["generic_medium_liquid"])
        rocket_why = "用户自定义参数（无匹配箭型库）"
        warnings.append("未匹配到箭型库条目，按通用中型液体模板 + 用户覆盖参数计算（量级参考）")
    if rocket is None:
        rocket = copy.deepcopy(I._FALLBACK_ROCKETS["generic_medium_liquid"])
        rocket_why = "未指定箭型"
        warnings.append("未指定箭型且无法匹配内置库：按「通用中型液体火箭模板」计算，"
                        "结果只具备量级意义（请补充箭型或参数后重算）")
    rocket_name = rocket.get("name") or inp.rocket_label or "未知箭型"
    # ---- v3.4/v4：任务类型（CZ-2F 载人交会任务单独成一支）----
    _rid = str(rocket.get("id") or "")
    _rname = str(rocket_name).upper().replace(" ", "")
    _is_cz2f = ("CZ2F" in _rname or "CZ-2F" in _rname
                or "长征二号F" in rocket_name or "長征二號F" in rocket_name
                or _rid == "cz2f")
    _profile_req = str(inp.profile or "auto").lower()
    if _profile_req == "cz2f":
        _is_cz2f = True
    # 任务类型：auto → 按箭型/开关识别；显式指定则尊重用户
    if mt_req not in ("standard", "crewed", "return"):
        mt = "crewed" if _is_cz2f else "standard"
        mt_src, mt_note = "infer", ("按箭型「%s」自动识别为载人交会任务" % rocket_name
                                    if _is_cz2f else "非载人箭型，按常规发射任务计算")
    else:
        mt = mt_req
        mt_src, mt_note = "user", "用户指定任务类型：「%s」" % mt_req
    profile = "cz2f" if mt == "crewed" else "standard"
    station_enabled = bool(inp.station_enabled) and (mt == "crewed" or _is_cz2f)
    resolutions.append(_res(
        "mission.mission_type", "任务类型",
        MISSION_TYPE_CN.get(mt, mt), mt_src, "中" if mt == "crewed" else "高", mt_note))
    resolutions.append(_res(
        "mission.profile", "计算分支",
        "载人交会（含入轨+对接）" if profile == "cz2f" else "通用上升段任务",
        "infer" if mt_src == "infer" else "user",
        "中" if profile == "cz2f" else "高",
        "空间站交会模块：%s" % ("启用" if station_enabled else "不启用")))
    resolutions.append(_res("mission.rocket", "火箭型号", rocket_name,
                            "user" if custom_rec else (
                                "db" if rocket.get("id") not in ("generic_medium_liquid",
                                                                 "generic_small_solid")
                                else "default"),
                            rocket.get("confidence", "低"),
                            "%s；%s" % (rocket_why, str(rocket.get("note") or "")[:160])))
    if str(rocket.get("confidence", "中")).startswith("低"):
        gaps.append(resolutions[-1])


    # ---------------- 2. 发射场 ----------------
    log("[2/8] 解析发射场…")
    site, site_src, site_conf, site_note, w = I.resolve_site(
        inp.site_label, inp.site_id, inp.site_lat, inp.site_lon, inp.site_alt,
        rocket, dem_resolver)
    warnings.extend(w)
    resolutions.append(_res("mission.launch_site", "发射工位/坐标",
                            "%s (%.4f, %.4f, %s m)" % (site["name"], site["lat_deg"],
                                                       site["lon_deg"],
                                                       fmt_num(site["alt_m_ellipsoid"])),
                            site_src, site_conf, site_note))
    if site_conf == "低":
        gaps.append(resolutions[-1])

    # ---- 输入合法性（常见笔误：经纬写反、超范围）----
    _lat = float(site["lat_deg"])
    _lon = float(site["lon_deg"])
    if abs(_lat) > 90.0:
        hint = "（疑似经纬度写反）" if abs(_lon) <= 90.0 else ""
        raise ValueError("发射场纬度 %.4f 超出 ±90°%s，请检查坐标顺序应为「纬度, 经度」" % (_lat, hint))
    if abs(_lon) > 180.0:
        raise ValueError("发射场经度 %.4f 超出 ±180°" % _lon)
    if abs(_lat) < 1e-6 and abs(_lon) < 1e-6:
        raise ValueError("发射场坐标是 (0, 0)：请填写真实经纬度")
    for _i, _o in enumerate(inp.observers or []):
        if _o.lat_deg is not None and abs(float(_o.lat_deg)) > 90.0:
            hint = "（疑似经纬度写反）" if (_o.lon_deg is not None and abs(float(_o.lon_deg)) <= 90.0) else ""
            raise ValueError("观测点「%s」纬度 %.4f 超出 ±90°%s" % (
                _o.name or ("第%d个" % (_i + 1)), float(_o.lat_deg), hint))
        if _o.lon_deg is not None and abs(float(_o.lon_deg)) > 180.0:
            raise ValueError("观测点「%s」经度 %.4f 超出 ±180°" % (
                _o.name or ("第%d个" % (_i + 1)), float(_o.lon_deg)))

    # ---------------- 3. 航警 ----------------
    log("[3/8] 解析航警（NOTAM）…")
    raw_notams: List[Dict[str, Any]] = []
    if inp.notams:
        for z in inp.notams:
            d = z.as_config() if isinstance(z, NotamInput) else dict(z)
            d.setdefault("raw_text", "")
            if d.get("b_utc") and d.get("c_utc") and len(d.get("polygon_latlon") or []) >= 3:
                raw_notams.append(d)
            else:
                warnings.append("航警 %s 字段不完整，已忽略" % (d.get("id") or "?"))
    elif inp.notams_text.strip():
        parsed, w = NP.parse_notams(inp.notams_text, _ref_year_from_notams([]) or
                                    datetime.now(timezone.utc).year)
        warnings.extend(w)
        raw_notams.extend(parsed)
    notams_mode = "provided" if raw_notams else ("predict" if inp.notams_mode != "off" else "off")
    if not raw_notams and notams_mode == "predict":
        warnings.append("未提供航警：进入「预测落区」模式——先按标称程序仿真，再用预测落点"
                        "生成提示性落区（无官方依据，不能用于正式结论）")
    elif not raw_notams and notams_mode == "off":
        warnings.append("用户关闭了航警输入：本任务不做落区判定与发射窗口计算")
    notams, notam_res, w = I.resolve_notams(raw_notams, site, rocket)
    warnings.extend(w)
    resolutions.extend(notam_res)

    # ---------------- 4. 起飞时刻 T0 ----------------
    log("[4/8] 解析/推断起飞时刻 T0…")
    t0, t0_src, t0_conf, t0_note = None, "", "高", ""
    ref_year = _ref_year_from_notams(notams) or datetime.now(timezone.utc).year
    if inp.t0_text.strip():
        t0, used_tz, note = NP.parse_t0_text(inp.t0_text, inp.tz_hint or "auto", ref_year)
        if t0 is not None:
            t0_src, t0_conf, t0_note = "user", "高", "用户给定：" + note
        else:
            warnings.append("无法解析用户给定的起飞时刻「%s」：%s" % (inp.t0_text, note))
    if t0 is None and notams:
        t0, t0_src, t0_conf, t0_note = I.infer_t0_from_notams(notams)
    if t0 is None and notams:
        # 退一步：用航警窗口中点
        b = I._iso_to_dt(notams[0].get("b_utc", ""))
        if b is not None:
            t0 = b + (I._iso_to_dt(notams[0].get("c_utc", "")) or b) - b
            t0_src, t0_conf, t0_note = "infer", "低", "由航警窗口推算（可能偏早/偏晚数分钟）"
    if t0 is None and str(getattr(inp, "t0_mode", "auto")) == "rendezvous" \
            and (station_enabled or mt == "crewed"):
        # 载人任务选了「按交会窗口反推」：可以用空间站窗口搜索，不需要先知道 T0。
        # 以当前 UTC（取整到分钟）作为搜索中心；运行时由 t0win 步骤在窗口内定 T0。
        t0 = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        t0_src, t0_conf = "infer", "低"
        t0_note = ("未给定 T0（按交会窗口反推模式）：以当前 UTC %s 作为窗口搜索中心，"
                   "运行时按交会发射窗口确定 T0" % t0.strftime("%Y-%m-%d %H:%M"))
        warnings.append("未给定 T0：已用当前 UTC 作为交会窗口搜索中心；"
                        "如需指定日期范围，请粘贴对应时段的空间站/航警数据或在①填写 T0。")
    if t0 is None:
        raise ValueError("无法确定起飞时刻 T0：请填写 T0，或提供带 B)/C) 的航警以便自动推断；"
                         "载人任务也可在①勾选「按交会窗口反推 T0」后不填 T0")
    t0_iso = t0.strftime("%Y-%m-%dT%H:%M:%SZ")
    resolutions.append(_res("mission.t0_utc", "起飞时刻 T0", t0_iso + "（北京时 " +
                            (t0 + __import__("datetime").timedelta(hours=8)).strftime("%m-%d %H:%M") + "）",
                            t0_src, t0_conf, t0_note))
    if t0_conf == "低":
        gaps.append(resolutions[-1])
    needs_refine_t0 = bool(t0_src == "infer" and notams)

    # ---------------- 5. 参考轨道 / 方位角 ----------------
    log("[5/8] 推断方位角与参考轨道…")
    ref_orbit_prov, ref_src, ref_conf, ref_note = None, "", "高", ""
    user_alt = inp.orbit_alt_km
    user_i = inp.orbit_i_deg
    # 载荷名 → 载荷质量/轨道提示
    payload_kg = inp.payload_kg
    payload_note = ""
    if payload_kg is None and inp.payload_name:
        pl, why = I.match_payload(inp.payload_name)
        if pl:
            payload_kg = pl.get("mass_kg")
            payload_note = "载荷「%s」按内置库 %s kg（%s）" % (inp.payload_name, fmt_num(payload_kg), why)
            if pl.get("alt_km") and user_alt is None:
                user_alt = pl.get("alt_km")
            if pl.get("i_deg") and user_i is None:
                user_i = pl.get("i_deg")
    # 两遍推断（顺序很重要）：
    #   ① 先只用箭型典型轨道拿到"备用参考轨道"，用于无落区时反算方位角；
    #   ② 算完方位角后，再以"方位角反算倾角"优先（任务特定）重新推参考轨道，
    #      否则会出现"参考轨道取箭型典型 SSO、方位角来自落区"的自相矛盾（曾导致 dogleg 误触发）。
    # v4：用户明确指定参考轨道（模板 / 我的轨道库 / 手填 / TLE）时，优先采用用户值。
    _uorb, _usrc, _uconf, _unote = I.resolve_user_reference_orbit(
        getattr(inp, "ref_orbit_mode", "auto"),
        getattr(inp, "ref_orbit_name", ""),
        getattr(inp, "ref_orbit", {}) or {},
        getattr(inp, "ref_orbit_tle", "") or "")
    if _uorb:
        ref_orbit_prov = dict(_uorb)
        ref_orbit_prov["confidence"] = _uconf
        ref_src, ref_conf, ref_note = _usrc, _uconf, _unote
        if _uorb.get("alt_km") is not None:
            user_alt = float(_uorb["alt_km"])
        if _uorb.get("i_deg") is not None:
            user_i = float(_uorb["i_deg"])
    else:
        if str(getattr(inp, "ref_orbit_mode", "auto")) not in ("", "auto"):
            warnings.append("参考轨道：%s" % (_unote or "用户指定的参考轨道无法使用，已回退自动推断"))
        ref_orbit_prov, ref_src, ref_conf, ref_note = I.infer_reference_orbit(
            None, site["lat_deg"], rocket, user_alt, user_i)
    az, az_src, az_conf, az_note = I.guess_azimuth(site, notams, rocket, ref_orbit_prov)
    # 射向自身的不确定度：落区质心反算 ≈ ±2°；由目标轨道反算 ≈ ±1.5°；用户给定 ≈ ±0.5°
    if az_src == "user":
        _az_sigma = 0.5
    elif I.stage_like_zones(notams):
        _az_sigma = 2.0
    else:
        _az_sigma = 1.5
    if az is not None and (user_alt is None or user_i is None) and not _uorb:
        ref2, src2, conf2, note2 = I.infer_reference_orbit(
            az, site["lat_deg"], rocket, user_alt, user_i, az_sigma_deg=_az_sigma)
        if ref2:
            # 方位角来自落区时，倾角应优先用"方位角反算"；若仍与箭型典型一致则不覆盖
            if src2 == "infer" or note2 != ref_note:
                ref_orbit_prov, ref_src, ref_conf, ref_note = ref2, src2, conf2, note2
    if az is None:
        warnings.append("无法推断发射方位角：将使用 180° 初值，反演大概率不收敛")
        az = 180.0
    resolutions.append(_res("guidance.azimuth_deg", "发射方位角", "%.2f°" % az, az_src, az_conf, az_note))
    if ref_orbit_prov:
        _mm = ref_orbit_prov.get("mismatch_deg")
        _near = ref_orbit_prov.get("nearest_template") or {}
        if _mm is not None and float(_mm) > 5.0:
            warnings.append(
                "参考轨道倾角按射向反算为 %.2f°±%.1f°，与库内最近典型轨道「%s」（%.2f°）"
                "相差 %.1f°：可能是特殊射向/新任务，也可能是该典型轨道不属于本次任务"
                "（例如航警来自别的场址）。如需锁定轨道，请在「② 轨道与时序」里显式选择"
                "目标轨道（内置模板 / 我的轨道库 / 手填 / TLE），数据体检会随之升为高置信度。"
                % (float(ref_orbit_prov.get("i_est_deg") or 0.0),
                   float(ref_orbit_prov.get("i_sigma_deg") or 0.0),
                   _near.get("name") or "DB", float(_near.get("i_deg") or 0.0), float(_mm)))
        _extra = ""
        if ref_orbit_prov.get("e"):
            _extra += "，e=%s" % fmt_num(ref_orbit_prov.get("e"), 5)
        if ref_orbit_prov.get("raan_deg") is not None:
            _extra += "，RAAN=%s°" % fmt_num(ref_orbit_prov.get("raan_deg"), 2)
        if ref_orbit_prov.get("ltan_h") is not None:
            _extra += "，LTAN=%s h" % fmt_num(ref_orbit_prov.get("ltan_h"), 2)
        resolutions.append(_res("mission.reference_orbit", "参考轨道",
                                "h=%s km, i=%s°%s" % (
                                    fmt_num(ref_orbit_prov.get("alt_km")),
                                    fmt_num(ref_orbit_prov.get("i_deg")), _extra),
                                ref_src, ref_conf, ref_note))
        if ref_conf == "低":
            gaps.append(resolutions[-1])
    # CZ-2F：交会任务需要 200 km / 41.47° 量级的初始插入轨道
    if _is_cz2f:
        if not ref_orbit_prov:
            ref_orbit_prov = {"alt_km": 200.0, "i_deg": 41.47,
                              "confidence": "中",
                              "source": "CZ-2F 典型初始插入轨道（200 km / 41.47°，与空间站倾角一致）"}
            resolutions.append(_res("mission.reference_orbit", "参考轨道",
                                    "h=200 km, i=41.47°", "db", "中",
                                    ref_orbit_prov["source"]))
        else:
            if ref_orbit_prov.get("alt_km") is None:
                ref_orbit_prov["alt_km"] = 200.0
            if ref_orbit_prov.get("i_deg") is None:
                ref_orbit_prov["i_deg"] = 41.47


    # ---------------- 6. 箭型参数 / 制导 ----------------
    log("[6/8] 组装箭型参数与制导初值…")
    vehicle, vres, w = I.build_vehicle(rocket, payload_kg, inp.vehicle_overrides)
    resolutions.extend(vres)
    warnings.extend(w)
    if payload_note:
        resolutions.append(_res("vehicle.payload", "载荷", vehicle.get("payload_kg"),
                                "db", "中", payload_note))
    guidance, gres = I.build_guidance(rocket, vehicle, az, inp.mode,
                                      ref_orbit=ref_orbit_prov,
                                      site_lat=site["lat_deg"])
    resolutions.extend(gres)
    # v4.2：载人/交会任务收紧末级平面修正时间常数。默认 200 s 时，末级工作段
    # 不足以把入轨面拉到目标面（实测残差 0.2~0.4°）；收紧到 60 s 后残差可到 0.01°。
    if station_enabled and guidance.get("i_target_deg") is not None:
        try:
            _tau = float(guidance.get("yaw2_tau") or 200.0)
        except (TypeError, ValueError):
            _tau = 200.0
        if _tau > 80.0:
            guidance["yaw2_tau"] = 60.0
            resolutions.append(_res(
                "guidance.yaw2_tau", "平面修正时间常数", 60.0, "infer", "中",
                "载人交会任务：由 %.0f s 收紧到 60 s（使末级平面修正在 SECO 前收敛；"
                "窗宽 T0 精度随之提高）" % _tau))
    # v4：用户显式指定的制导参数（留空=不覆盖，交给箭型默认+反演）
    _gu = {k: v for k, v in (getattr(inp, "guidance_overrides", {}) or {}).items()
           if v is not None and str(v).strip() != ""}
    _locked_guidance: List[str] = []
    if _gu:
        _gl = {"t_v": "推力转向时刻 t_v", "t_p": "俯仰程序时长 t_p",
               "pitch_power": "俯仰程序幂次", "azimuth_deg": "发射方位角",
               "i_target_deg": "末级平面修正目标倾角", "yaw_tau": "偏航时间常数",
               "fairing_divert_mps": "整流罩分离横向速度",
               "seco_band_km": "SECO 高度带", "seco_e_max": "SECO 偏心率上限"}
        for _k, _v in _gu.items():
            try:
                if _k in ("i_target_deg",):
                    guidance[_k] = float(_v)
                elif isinstance(_v, (int, float)) or str(_v).replace(".", "", 1).lstrip("-").isdigit():
                    guidance[_k] = float(_v)
                else:
                    raise ValueError("不是数字")
            except (TypeError, ValueError):
                warnings.append("制导参数覆盖 %s=%r 无法解析为数字，已忽略" % (_k, _v))
                continue
            resolutions.append(_res("guidance." + _k, _gl.get(_k, _k),
                                    fmt_num(guidance[_k]), "user", "高",
                                    "用户在「箭型与制导」页手工指定（不再由反演决定）"))
        if "azimuth_deg" in guidance:
            az = float(guidance["azimuth_deg"])
            az_src, az_conf = "user", "高"
        # 记录下来交给 autoplan：这些参数用户已固定，不应再进反演
        _locked_guidance = sorted(_gu.keys())
    # v3.6/v4：自定义发射时序（未自定义/留空 = 箭型库默认）
    launch_seq_cfg, _seq_w = _apply_launch_sequence(inp, vehicle, guidance, t0_iso)
    warnings.extend(_seq_w)
    if launch_seq_cfg.get("custom"):
        _st = launch_seq_cfg.get("values") or {}
        _stg = _st.get("stage_sep_s") or []
        _parts = []
        if vehicle.get("boosters"):
            _parts.append("助推器 %s s" % fmt_num(_st.get("boosters_sep_s")))
        if _stg:
            _parts.append("级分离 %s s" % ("、".join(fmt_num(x) for x in _stg) or "—"))
        if vehicle.get("fairing"):
            _parts.append("抛罩 %s s" % fmt_num(_st.get("fairing_sep_s")))
        _parts.append("SECO %s s" % fmt_num(_st.get("seco_s")))
        resolutions.append(_res(
            "launch_sequence", "发射时序", "自定义（留空项用默认）", "user", "高",
            "；".join(_parts)))
    else:
        resolutions.append(_res(
            "launch_sequence", "发射时序", "箭型库默认", "db", "高",
            "未勾选自定义；使用各箭型内置分离/抛罩/SECO 时刻"))
    # 方位角来源标记（给 autoplan 决定反演范围）
    if az_src == "infer" and I.stage_like_zones(notams):
        az_from = "zone"
    elif az_src == "infer":
        az_from = "orbit"
    else:
        az_from = az_src

    # ---------------- 7. 观测点 ----------------
    log("[7/8] 补全观测点…")
    observers, ores, w = I.resolve_observers(inp.observers, dem_resolver, site)
    resolutions.extend(ores)
    warnings.extend(w)
    # v4：没有观测点不等于"不能出天空类成果"——默认把发射场当观测点，
    # 让天空轨迹 / 出现方位图 / 全国地图照常产出（数据体检里标明来源=自动推断）。
    auto_obs_used = False
    if not observers and bool(getattr(inp, "auto_observer", True)):
        alt = float(site.get("alt_m_ellipsoid") or 0.0)
        observers = [{"id": "obs1", "name": "%s（发射场观测点）" % (site.get("name") or "发射场"),
                      "lat_deg": round(float(site["lat_deg"]), 6),
                      "lon_deg": round(float(site["lon_deg"]), 6),
                      "alt_m": round(alt, 1),
                      "note": "未指定观测点：程序默认用发射场作为观测点（可在「观测点」页修改）"}]
        auto_obs_used = True
        resolutions.append(Resolution(
            "observers.obs1", "观测点", "%s, %s, %s m（发射场）" % (
                fmt_num(site["lat_deg"], 5), fmt_num(site["lon_deg"], 5), fmt_num(alt, 0)),
            "infer", "中",
            "用户未填写观测点：默认取发射场作为观测点，保证天空轨迹/出现方位/全国地图等成果不丢失"))
        warnings.append("未提供观测点，已默认用发射场作为观测点（观测点页可增删改）")
    elif not observers:
        warnings.append("未提供观测点且关闭了「自动使用发射场」：天空轨迹/出现方位图将跳过，"
                        "全国最大高度角地图仍会生成")

    # ---------------- 8. 网格 / DEM / 数值 ----------------
    log("[8/8] 组装网格、DEM、数值口径与自动计划…")
    grid, grid_note = I.infer_grid(site)
    from .model import DEFAULT_MODULES
    modules = dict(DEFAULT_MODULES)
    modules.update(inp.modules or {})
    _ref_i = 0.0
    try:
        _ref_i = float((ref_orbit_prov or {}).get("i_deg") or 0.0)
    except (TypeError, ValueError):
        _ref_i = 0.0
    # 轨道平面窗口：有航警时窗 或 有平面约束（RAAN/LTAN/空间站/太阳同步倾角）才有意义
    _plane_constraint = bool(station_enabled or inp.window_raan_deg is not None
                             or inp.window_ltan_h is not None
                             or (ref_orbit_prov or {}).get("raan_deg") is not None
                             or (ref_orbit_prov or {}).get("ltan_h") is not None
                             or 95.0 <= _ref_i <= 105.0)
    if not notams and not _plane_constraint:
        modules["window"] = False
    _return_enabled = bool(inp.return_enabled)
    if not station_enabled:
        # 载人/交会类模块在非载人任务上自动跳过（但保留"返回舱返回"独立可用）
        modules["station"] = False
        modules["docking"] = False
    else:
        modules["station"] = True
        modules["docking"] = True
    modules["return"] = bool(_return_enabled and modules.get("return", True))
    if _return_enabled and not station_enabled:
        warnings.append("已勾选返回舱返回，但当前任务未启用空间站轨道：返回段需要空间站星历，"
                        "请到「空间站与返回」页启用，或直接把任务类型改为「载人」/「返回」")
    if inp.mc_n and int(inp.mc_n) > 0:
        modules["mc"] = True
    if not modules.get("export", True):
        raise ValueError("export（仿真与数据契约）是必选步骤，不能关闭")

    mission = {
        "name": inp.name.strip() or I.auto_mission_name(rocket_name, site["name"], t0),
        "rocket": "%s%s" % (rocket_name, (" " + inp.serial) if inp.serial else ""),
        "serial": inp.serial,
        "profile": profile,
        "mission_type": mt,
        "t0_utc": t0_iso,
        "t0_note": "%s；来源：%s（%s）" % (t0_note, t0_src, t0_conf),
        "t0_timezone_used": "UTC",
        "site_confidence": site_conf,
        "rocket_confidence": rocket.get("confidence", "中"),
        "_azimuth_from": az_from,
        "launch_site": site,
        "payload_note": payload_note or "载荷质量与来源见 vehicle.payload_kg / provenance",
        "disclaimer": DISCLAIMER,
        "version": "v4.0",
    }
    if custom_rec:
        mission["custom_rocket"] = {
            "id": rocket.get("id"), "name": rocket.get("name"),
            "mode": str(getattr(inp, "custom_rocket_mode", "") or "session"),
            "record": rocket}
    if getattr(inp, "ref_orbit_mode", "auto") not in ("", "auto"):
        mission["reference_orbit_mode"] = {
            "mode": str(inp.ref_orbit_mode), "name": str(inp.ref_orbit_name or ""),
            "source": ref_src}
    if ref_orbit_prov:
        mission["reference_orbit"] = ref_orbit_prov
    if inp.notes:
        mission["user_notes"] = inp.notes
    if auto_obs_used:
        mission["auto_observer_used"] = True

    numerics = I.default_numerics(inp.mode)
    dem_cfg = {"type": "SRTMGL1", "source": "AWS elevation-tiles-prod/skadi",
               "sampling_m": 90, "horizon_radius_km": float(inp.dem_radius_km or 200.0)}

    # ---- v3.4：空间站交会（CZ-2F）与轨道平面窗口 ----
    _tle_lines = [ln.strip() for ln in str(inp.station_tle_text or "").splitlines()
                  if ln.strip()]
    if len(_tle_lines) >= 3 and not _tle_lines[0].startswith("1 "):
        _tle_lines = _tle_lines[1:]
    station_cfg: Dict[str, Any] = {
        "enabled": bool(station_enabled),
        "name": inp.station_name or "天宫空间站",
        "norad_id": int(inp.station_norad or 48274),
        "source": str(inp.station_source or "auto"),
        "oem_path": inp.station_oem_path or "",
        "rendezvous": {
            "target_time_h": float(inp.rendezvous_h or 6.5),
            "max_phasing_h": max(6.0, 2.0 * float(inp.rendezvous_h or 6.5)),
            "dv_budget_mps": float(inp.dv_budget_mps or 300.0),
            "final_approach_dv_mps": 10.0,
            "final_approach_s": 1800.0,
            "max_phasing_orbits": 10,
            "min_phasing_orbits": 1,
        },
    }
    if len(_tle_lines) >= 2:
        station_cfg["tle"] = _tle_lines[:2]
    # 轨道平面约束：用户显式填的 RAAN/LTAN 优先；否则用参考轨道里带的 RAAN/LTAN
    _eff_raan = inp.window_raan_deg
    _eff_ltan = inp.window_ltan_h
    _raan_from_orbit = False
    if _eff_raan is None and (ref_orbit_prov or {}).get("raan_deg") is not None:
        _eff_raan = float(ref_orbit_prov["raan_deg"])
        _raan_from_orbit = True
    if _eff_ltan is None and (ref_orbit_prov or {}).get("ltan_h") is not None:
        _eff_ltan = float(ref_orbit_prov["ltan_h"])
    lw_cfg: Dict[str, Any] = {
        "mode": "auto",
        "tolerance_deg": float(inp.window_tolerance_deg or 0.2),
        "search_hours": float(inp.window_search_hours or 48.0),
        "step_s": 20.0,
        "ltan_h": _eff_ltan,
        "raan_deg": _eff_raan,
        "raan_epoch_utc": (t0_iso if _eff_raan is not None else None),
    }
    return_cfg: Dict[str, Any] = {
        "enabled": bool(_return_enabled and station_enabled),
        "use_current_notams": bool(inp.return_use_notams),
        "landing_site": {
            "name": inp.return_landing_name or "四子王旗着陆场",
            "lat_deg": float(inp.return_landing_lat if inp.return_landing_lat is not None
                             else 42.27),
            "lon_deg": float(inp.return_landing_lon if inp.return_landing_lon is not None
                             else 111.70),
            "radius_km": float(inp.return_landing_radius_km or 50.0),
        },
        "search_start_utc": str(inp.return_search_start or "").strip() or None,
        "search_end_utc": str(inp.return_search_end or "").strip() or None,
        "deorbit_dv_mps": float(inp.return_dv_mps or 120.0),
        "ld_ratio": float(inp.return_ld_ratio or 0.30),
        "bank_deg": (None if inp.return_bank_deg is None
                     else float(inp.return_bank_deg)),
        "auto_dv": bool(inp.return_auto_dv),
        "auto_bank": bool(inp.return_auto_bank),
        "mass_kg": 3000.0,
        "cd_area_m2": 5.0,
        "drogue_cd_area_m2": 20.0,
        "chute_cd_area_m2": 1200.0,
        "drogue_deploy_km": 10.0,
        "main_deploy_km": 6.0, "undock_lead_s": 3600.0, "orbital_sep_lead_s": 600.0, "burn_duration_s": 150.0, "prop_sep_delay_s": 300.0,
        "coarse_step_s": float(inp.return_coarse_step_s or 300.0),
        "scan_step_s": float(inp.return_scan_step_s or 10.0),
    }
    if _return_enabled and station_enabled:
        resolutions.append(_res(
            "return_capsule.landing_site", "返回舱着陆场",
            "%s (%.4f, %.4f, R=%.0f km)" % (
                return_cfg["landing_site"]["name"],
                return_cfg["landing_site"]["lat_deg"],
                return_cfg["landing_site"]["lon_deg"],
                return_cfg["landing_site"]["radius_km"]),
            "user", "中",
            ("用当前航警区作为着陆区" if return_cfg["use_current_notams"]
             else "固定着陆场；如返回航警区带返回/回收/着陆 hint 会自动优先使用")))
    if station_enabled:
        resolutions.append(_res(
            "station.source", "空间站轨道数据",
            str(inp.station_source or "auto"),
            "user" if (inp.station_oem_path or _tle_lines) else "db",
            "高" if inp.station_oem_path else "中",
            ("用户导入 OEM：%s" % inp.station_oem_path) if inp.station_oem_path else
            ("用户 TLE（%d 行）" % len(_tle_lines) if _tle_lines else
             "优先内置官方 CMSE OEM 快照；不覆盖任务时刻时回退 TLE/在线")))
        resolutions.append(_res(
            "launch_window.orbit", "轨道平面窗口约束",
            "空间站交会（%s）" % (inp.station_name or "天宫空间站"),
            "db", "中",
            "容差 %.2f°，搜索 ±%.0f h；交会目标 ΔV 预算 %.0f m/s、期望时长 %.1f h"
            % (lw_cfg["tolerance_deg"], lw_cfg["search_hours"],
               station_cfg["rendezvous"]["dv_budget_mps"],
               station_cfg["rendezvous"]["target_time_h"])))
    elif _eff_raan is not None:
        resolutions.append(_res("launch_window.orbit", "轨道平面窗口约束",
                                "目标 RAAN %.3f°" % float(_eff_raan),
                                "user" if not _raan_from_orbit else "db",
                                "中" if not _raan_from_orbit else "低",
                                ("用户给定" if not _raan_from_orbit
                                 else "取自任务参考轨道的 RAAN（历元按 T0 解释，仅作平面窗口搜索用）")
                                + "；窗口容差 %.2f°" % lw_cfg["tolerance_deg"]))
    elif _eff_ltan is not None:
        resolutions.append(_res("launch_window.orbit", "轨道平面窗口约束",
                                "太阳同步 LTAN %.2f h" % float(_eff_ltan),
                                "user", "高", "用户给定；窗口容差 %.2f°" % lw_cfg["tolerance_deg"]))

    config: Dict[str, Any] = {
        "_schema": "rocketkit.config.v3",
        "_created_by": "火箭弹道工具箱 v%s 离线版" % P.app_version(),
        "_created_at": _now_iso(),
        "mission": mission,
        "vehicle": vehicle,
        "guidance": {k: v for k, v in guidance.items()},
        "numerics": numerics,
        "notams": [{"id": z["id"], "b_utc": z["b_utc"], "c_utc": z["c_utc"],
                    "q_line": z.get("q_line", ""), "fir": z.get("fir", ""),
                    "polygon_latlon": z["polygon_latlon"],
                    "stage_hint": z.get("stage_hint", ""),
                    "object_hint": z.get("object_hint", ""),
                    "verified": z.get("verified", ""),
                    "raw_text": z.get("raw_text", "")} for z in notams],
        "observers": observers,
        "grid": grid,
        "dem": dem_cfg,
        "window": {"margin_s": 60.0},
        "launch_window": lw_cfg,
        "station": station_cfg,
        "return_capsule": return_cfg,
        "launch_sequence": launch_seq_cfg,
        "inversion": {},
        "mc": {},
    }
    if inp.vehicle_overrides:
        config["vehicle"]["_user_overrides"] = inp.vehicle_overrides

    # ---- 自动计划 ----
    # 先把"用户固定的制导参数"交给 autoplan（最终的 _v3 在下面才生成，这里先放一份）
    config.setdefault("_v3", {})["guidance_locked"] = list(_locked_guidance)
    if modules.get("invert", True):
        if autoplan.has_anything_to_fit(config):
            inv, notes = autoplan.plan_inversion(config,
                                                quick=(inp.inversion_maxiter == 0 and
                                                       inp.inversion_popsize == 0 and False),
                                                maxiter=inp.inversion_maxiter,
                                                popsize=inp.inversion_popsize)
            config["inversion"] = inv
            for n in notes:
                log("    自动计划：" + n)
            if not (inv.get("parameters") or []):
                modules["invert"] = False
                config.pop("inversion", None)
                warnings.append("可反演参数都被你手工固定了（%s）：本次不做参数反演，"
                                "直接按你的值仿真" % "、".join(_locked_guidance))
        else:
            modules["invert"] = False
            warnings.append("既无可用落区也无参考轨道：跳过参数反演，使用箭型默认制导参数")
    if not modules.get("invert", True):
        config.pop("inversion", None)
    if modules.get("mc"):
        mc = autoplan.plan_mc(config, n=int(inp.mc_n or 0))
        if mc:
            config["mc"] = mc
        else:
            modules["mc"] = False
            warnings.append("蒙特卡洛没有可抽样变量，已跳过")
    if not modules.get("mc") and "mc" in config and not config["mc"]:
        config.pop("mc", None)

    # ---- 元数据 / 体检 ----
    inferred_hint_ids = [z.get("id") for z in notams if z.get("_hint_inferred")]
    if inferred_hint_ids:
        warnings.append("残骸类型由程序推断：%s（进行时会按预测落点自动校正，建议人工核对）"
                        % "、".join(str(x) for x in inferred_hint_ids))
    config["_v3"] = {
        "app_version": P.app_version(),
        "built_at": _now_iso(),
        "notams_mode": notams_mode,
        "needs_refine_t0": needs_refine_t0,
        "t0_mode": str(getattr(inp, "t0_mode", "auto") or "auto"),
        "t0_user_given": bool(t0_src == "user"),
        "modules": modules,
        "dem_resolver": "srtm+coarse" if dem_resolver else "none",
        "stage_hints_inferred": inferred_hint_ids,
        "profile": profile,
        "mission_type": mt,
        "station_enabled": bool(station_enabled),
        # auto_observer = 用户设置（无观测点时是否用发射场）；auto_observer_used = 本次是否真的用了
        "auto_observer": bool(getattr(inp, "auto_observer", True)),
        "auto_observer_used": auto_obs_used,
        "custom_rocket": bool(custom_rec),
        "ref_orbit_mode": str(getattr(inp, "ref_orbit_mode", "auto") or "auto"),
        # 用户固定（不进反演）的制导参数键名，见 autoplan.plan_inversion
        "guidance_locked": list(_locked_guidance),
        "warnings": warnings,
    }
    if payload_note:
        config["_v3"]["payload_note"] = payload_note

    # 体检表去重（同一字段可能被"推断 + 制导组装"各记一次）：
    # 保留第一次出现（顺序即优先级：用户 > 内置库 > 推断 > 默认），把重复项说明合并到 note
    _seen = {}
    _dedup = []
    for r in resolutions:
        key = r.field
        if key in _seen:
            prev = _seen[key]
            # v4：用户手工指定的值优先于内置库/推断值——否则体检里会显示"内置库"
            # （v3 的"保留第一次出现"就是这样把用户覆盖标错的）。
            if r.source == "user" and prev.source != "user":
                if prev.note and prev.note not in (r.note or ""):
                    r.note = (r.note + "；" if r.note else "") + prev.note
                idx = _dedup.index(prev)
                _dedup[idx] = r
                _seen[key] = r
                continue
            if r.note and r.note not in (prev.note or ""):
                prev.note = (prev.note + "；" if prev.note else "") + r.note
            continue
        _seen[key] = r
        _dedup.append(r)
    resolutions = _dedup

    # 体检表落盘到 mission._provenance
    config["mission"]["_provenance"] = [r.to_dict() for r in resolutions]

    result = BuildResult(config=config, resolutions=resolutions, gaps=gaps,
                         warnings=warnings, needs_refine_t0=needs_refine_t0)
    result.summary = _summary(config, resolutions, gaps, warnings)
    if write and workdir:
        os.makedirs(workdir, exist_ok=True)
        path = os.path.join(workdir, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        # 保留"初值版本"，便于事后核对反演改了哪些参数
        with open(os.path.join(workdir, "config_initial.json"), "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        result.path = path
        log("配置已写入：%s" % path)
    return result


def _summary(config: Dict[str, Any], resolutions: List[Resolution],
             gaps: List[Resolution], warnings: List[str]) -> str:
    m = config.get("mission") or {}
    lines = [
        "任务：%s" % m.get("name", ""),
        "T0：%s" % m.get("t0_utc", ""),
        "发射场：%s" % (m.get("launch_site") or {}).get("name", ""),
        "航警：%d 个（%s）" % (len(config.get("notams") or []),
                            ("%d 个字段由推断得到" % len(gaps)) if gaps else "全部字段来自用户/内置库"),
    ]
    if warnings:
        lines.append("注意：%d 条提示（见数据体检）" % len(warnings))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 便捷：从历史 config.json 反推 MissionInput（"导入上次任务"功能）
# --------------------------------------------------------------------------- #
def mission_input_from_config(cfg: Dict[str, Any]) -> MissionInput:
    m = cfg.get("mission") or {}
    site = m.get("launch_site") or {}
    ref = m.get("reference_orbit") or {}
    notams = []
    for z in cfg.get("notams") or []:
        notams.append(NotamInput(
            id=z.get("id", ""), b_utc=z.get("b_utc", ""), c_utc=z.get("c_utc", ""),
            q_line=z.get("q_line", ""), fir=z.get("fir", ""),
            polygon_latlon=z.get("polygon_latlon") or [],
            stage_hint=z.get("stage_hint", ""), raw_text=z.get("raw_text", ""),
            verified=z.get("verified", "")))
    from .model import ObserverInput
    obs = []
    for o in cfg.get("observers") or []:
        obs.append(ObserverInput(id=o.get("id", "obs1"), name=o.get("name", ""),
                                 lat_deg=o.get("lat_deg"), lon_deg=o.get("lon_deg"),
                                 alt_m=o.get("alt_m"), note=o.get("note", "")))
    st = cfg.get("station") or {}
    tle = st.get("tle") or []
    lw = cfg.get("launch_window") or {}
    rd = st.get("rendezvous") or {}
    rc = cfg.get("return_capsule") or {}
    ls = rc.get("landing_site") or {}
    v3 = cfg.get("_v3") or {}
    # v4：自定义箭型 / 参考轨道来源回填（保证「打开上次任务」后可继续编辑）
    custom = m.get("custom_rocket") or {}
    custom_rec = dict(custom.get("record") or {}) if isinstance(custom, dict) else {}
    ref_mode = str(v3.get("ref_orbit_mode") or
                   (m.get("reference_orbit_mode") or {}).get("mode") or "auto")
    ref_manual: Dict[str, Any] = {}
    if ref_mode in ("custom", "tle"):
        ref_manual = {
            "name": ref.get("name") or "",
            "alt_km": ref.get("alt_km"), "i_deg": ref.get("i_deg"),
            "e": ref.get("e"), "raan_deg": ref.get("raan_deg"),
            "argp_deg": ref.get("argp_deg"), "ltan_h": ref.get("ltan_h"),
            "epoch_utc": ref.get("epoch_utc") or "",
            "type": ref.get("type") or "CUSTOM",
        }
    ref_name = ""
    if ref_mode in ("template", "user"):
        ref_name = str((m.get("reference_orbit_mode") or {}).get("name") or "")
    # v4：用户手工固定的制导参数（provenance 里 source=user 的 guidance.* 字段）要回填，
    # 否则「打开上次任务」后这些值会变成由反演/箭型默认决定，等于丢了用户输入。
    _go: Dict[str, Any] = {}
    _guid_keys = ("t_v", "t_p", "pitch_power", "azimuth_deg", "i_target_deg",
                  "yaw_tau", "fairing_divert_mps", "seco_band_km", "seco_e_max")
    for _p in (m.get("_provenance") or []):
        _f = str(_p.get("field") or "")
        if not _f.startswith("guidance.") or _p.get("source") != "user":
            continue
        _k = _f.split(".", 1)[1]
        if _k in _guid_keys:
            _v = (cfg.get("guidance") or {}).get(_k)
            if _v is not None:
                _go[_k] = _v
    return MissionInput(
        name=m.get("name", ""), rocket_label=m.get("rocket", ""), serial=m.get("serial", ""),
        site_label=site.get("name", ""), site_lat=site.get("lat_deg"),
        site_lon=site.get("lon_deg"), site_alt=site.get("alt_m_ellipsoid"),
        t0_text=m.get("t0_utc", ""), notams=notams,
        observers=obs, orbit_alt_km=ref.get("alt_km"), orbit_i_deg=ref.get("i_deg"),
        mode=(cfg.get("numerics") or {}).get("mode", "accurate"),
        profile=str(m.get("profile") or "auto"),
        mission_type=str(m.get("mission_type") or v3.get("mission_type") or "auto"),
        custom_rocket=custom_rec,
        custom_rocket_mode=str(custom.get("mode") or "") if custom_rec else "",
        ref_orbit_mode=ref_mode,
        ref_orbit_name=ref_name,
        ref_orbit=ref_manual,
        auto_observer=bool(v3.get("auto_observer", True)),
        t0_mode=str(v3.get("t0_mode") or "auto"),
        return_only=bool(cfg.get("return_only") or m.get("profile") == "return"),
        station_enabled=bool(st.get("enabled", True)),
        station_source=str(st.get("source") or "auto"),
        station_name=str(st.get("name") or "天宫空间站"),
        station_norad=int(st.get("norad_id") or 48274),
        station_tle_text="\n".join(str(x) for x in tle),
        station_oem_path=str(st.get("oem_path") or ""),
        rendezvous_h=float(rd.get("target_time_h") or 6.5),
        dv_budget_mps=float(rd.get("dv_budget_mps") or 300.0),
        window_tolerance_deg=float(lw.get("tolerance_deg") or 0.2),
        window_search_hours=float(lw.get("search_hours") or 48.0),
        window_ltan_h=lw.get("ltan_h"),
        window_raan_deg=lw.get("raan_deg"),
        guidance_overrides=_go,
        launch_sequence=dict(cfg.get("launch_sequence") or {}),
        return_enabled=bool(rc.get("enabled")),
        return_use_notams=bool(rc.get("use_current_notams")),
        return_landing_name=str(ls.get("name") or "四子王旗着陆场"),
        return_landing_lat=ls.get("lat_deg"),
        return_landing_lon=ls.get("lon_deg"),
        return_landing_radius_km=float(ls.get("radius_km") or 50.0),
        return_search_start=str(rc.get("search_start_utc") or ""),
        return_search_end=str(rc.get("search_end_utc") or ""),
        return_dv_mps=float(rc.get("deorbit_dv_mps") or 120.0),
        return_ld_ratio=float(rc.get("ld_ratio") or 0.30),
        return_bank_deg=rc.get("bank_deg"),
        return_auto_dv=bool(rc.get("auto_dv", True)),
        return_auto_bank=bool(rc.get("auto_bank", True)),
        return_coarse_step_s=float(rc.get("coarse_step_s") or 300.0),
        return_scan_step_s=float(rc.get("scan_step_s") or 10.0),
    )
