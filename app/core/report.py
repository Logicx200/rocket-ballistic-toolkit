# -*- coding: utf-8 -*-
"""自动生成中文技术报告（替代 v2 里"人工撰写报告"这一 AI 环节）。

输入：工作区里的 ``config.json`` + ``sim/*`` + ``sky/sky_summary.json`` +
``report/发射窗口计算.json`` + ``uncertainty/mc_stats.json`` + ``report/qc_report.json``
输出：``report/弹道仿真报告.md``（若装了 python-docx 另出 .docx，可选）

数据体检（每个字段的来源/置信度）来自 ``config.mission._provenance``。
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

TZ_BJ = "+08:00"


# --------------------------------------------------------------------------- #
# 读数据
# --------------------------------------------------------------------------- #
def _read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default if default is not None else {}


def _read_csv(path: str) -> List[Dict[str, str]]:
    try:
        with open(path, encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    except Exception:  # noqa: BLE001
        return []


def _fmt(v, nd: int = 2, unit: str = "") -> str:
    if v is None or v == "":
        return "—"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(x) >= 1000:
        s = "%.0f" % x
    elif abs(x) >= 10:
        s = ("%." + str(nd) + "f") % x
    else:
        s = ("%." + str(max(nd, 3)) + "f") % x
    return s + unit


def _bj(iso: str) -> str:
    """ISO UTC → 北京时显示。"""
    try:
        t = str(iso).strip()
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        from datetime import timedelta
        return (dt + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return str(iso)


def _table(headers: List[str], rows: List[List[Any]]) -> str:
    if not rows:
        return "_（无数据）_\n"
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(
            str(x).replace("|", "\\|").replace("\n", " ") for x in r) + " |")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# v4：成果速览（报告开头的「一页纸」章节，三类任务共用）
# --------------------------------------------------------------------------- #
_MT_CN = {"standard": "常规发射弹道", "crewed": "载人发射弹道（含交会）",
          "return": "返回舱返回弹道"}


def _v4_overview(workdir: str) -> List[str]:
    """读取 任务摘要.json / 关键节点.csv / 成果清单.json，生成 §0 速览。"""
    L: List[str] = []
    s = _read_json(os.path.join(workdir, "report", "任务摘要.json"), {}) or {}
    mt = (s.get("mission") or {}).get("type") or ""
    kn = s.get("key_numbers") or {}
    L.append("## 0. 成果速览")
    L.append("")
    if mt:
        L.append("**任务类型：%s**" % (_MT_CN.get(mt, mt)))
        L.append("")
    if kn:
        L.append(_table(["项目", "结果"], [[k, v] for k, v in kn.items()]))
    else:
        L.append("_（通用成果尚未生成；若本次只跑了部分模块，这是正常的）_")
        L.append("")
    nodes = _read_csv(os.path.join(workdir, "sim", "关键节点.csv"))
    if nodes:
        L.append("**关键节点（前 10 条；完整表见 `sim/关键节点.csv`）：**")
        L.append("")
        rows = []
        for r in nodes[:10]:
            rows.append([r.get("序号", ""), r.get("类别", ""), r.get("节点", ""),
                         _fmt(r.get("T+ (s)"), 1),
                         (r.get("时刻(UTC)") or "").replace("T", " "),
                         _fmt(r.get("高度(km)"), 1), _fmt(r.get("速度(m/s)"), 0)])
        L.append(_table(["#", "类别", "节点", "T+ (s)", "时刻 (UTC)", "高度 (km)",
                         "速度 (m/s)"], rows))
        L.append("")
    inv = _read_json(os.path.join(workdir, "report", "成果清单.json"), {}) or {}
    if inv:
        miss = inv.get("missing") or []
        L.append("**成果规模**：%s 个文件 / %.1f MB；%s" % (
            inv.get("total", 0), float(inv.get("bytes") or 0) / 1048576.0,
            ("应产出项缺少 %d 项（见 `report/成果清单.md`）" % len(miss)) if miss
            else "按任务类型核对：应产出项齐备"))
        L.append("")
        skips = [x for x in (inv.get("steps") or [])
                 if str(x.get("状态") or "") in ("跳过", "失败")]
        if skips:
            L.append("**本次被跳过的步骤及原因：**")
            L.append("")
            for x in skips[:8]:
                L.append("- %s（%s）：%s" % (x.get("步骤"), x.get("状态"),
                                            x.get("说明") or "—"))
            L.append("")
    L.append("> 一页纸摘要：`report/任务摘要.md`；全量文件与步骤台账：`report/成果清单.md`。")
    L.append("")
    return L



# --------------------------------------------------------------------------- #
# 主函数
# --------------------------------------------------------------------------- #
def _generate_return_report(workdir: str, cfg: Dict[str, Any],
                            ret: Dict[str, Any]) -> str:
    """仅返回任务报告：没有发射场/上升段/残骸落区。"""
    mission = cfg.get("mission") or {}
    st = _read_json(os.path.join(workdir, "sim", "空间站轨道.json"), {}) or {}
    qc = _read_json(os.path.join(workdir, "report", "qc_report.json"), {}) or {}
    L: List[str] = []
    L.append("# 返回舱返回任务技术报告")
    L.append("")
    L.append("> 任务类型：**返回舱返回**（无发射场、无上升段、无交会对接）")
    L.append("> ")
    L.append("> 数据：空间站轨道 + 返回航警/着陆场；模型：3-DOF 简化返回舱，"
             "仅供公开信息分析与教学使用。")
    L.append("")
    L.extend(_v4_overview(workdir))
    L.append("## 1. 任务概况")
    L.append("")
    site = (ret.get("landing_site") or {})
    L.append(_table(["项目", "内容"], [
        ["任务名称", mission.get("name", "—")],
        ["返回标称时刻", mission.get("t0_utc", "—")],
        ["空间站轨道", "%s（%s）" % (
            (st.get("station") or {}).get("name", "—"),
            (st.get("station") or {}).get("source") or "—")],
        ["着陆场", "%s（%s°N, %s°E，R=%s km）" % (
            site.get("name", "—"), _fmt(site.get("lat_deg"), 4),
            _fmt(site.get("lon_deg"), 4), _fmt(site.get("radius_km"), 0))],
        ["返回航警区", "、".join(ret.get("notam_zones") or []) or "未使用（固定着陆场）"],
        ["制动搜索区间", "%s ~ %s UTC" % (
            (ret.get("search") or {}).get("start_utc", "—"),
            (ret.get("search") or {}).get("end_utc", "—"))],
        ["可用返回窗口", "%d 个" % int(ret.get("n_windows") or 0)],
    ]))
    L.append("")
    L.append("## 2. 返回窗口")
    L.append("")
    ws = ret.get("windows") or []
    if ws:
        rows = []
        for w in ws:
            rows.append([w.get("start_utc", ""), w.get("end_utc", ""),
                         _fmt(w.get("duration_s"), 0),
                         w.get("best_landing_utc", ""),
                         "%s, %s" % (_fmt(w.get("landing_lat_deg"), 3),
                                     _fmt(w.get("landing_lon_deg"), 3)),
                         w.get("zone_id") or "—",
                         _fmt(w.get("dist_to_zone_km"), 1),
                         _fmt(w.get("deorbit_dv_mps"), 0),
                         _fmt(w.get("bank_deg"), 1)])
        L.append(_table(["制动窗起点 (UTC)", "制动窗终点 (UTC)", "窗宽 (s)",
                         "落地 (UTC)", "落点 (°N, °E)", "航警区",
                         "距边界 (km)", "ΔV (m/s)", "倾侧角 (°)"], rows))
    else:
        L.append("无满足条件的可行窗口；%s" % (ret.get("reason") or "见返回 JSON。"))
    L.append("")
    L.append("## 3. 选中返回弹道")
    L.append("")
    sel = ret.get("selected") or {}
    if sel:
        L.append(_table(["项目", "数值"], [
            ["制动时刻", "%s UTC（北京时 %s）" % (
                sel.get("t_deorbit_utc", "—"), sel.get("t_deorbit_bjt", "—"))],
            ["落地时刻", "%s UTC（北京时 %s）" % (
                sel.get("t_landing_utc", "—"), sel.get("t_landing_bjt", "—"))],
            ["落点", "%s°N, %s°E" % (_fmt(sel.get("lat_deg"), 4),
                                     _fmt(sel.get("lon_deg"), 4))],
            ["飞行时间", "%s s（约 %s min）" % (
                _fmt(sel.get("flight_time_s"), 0),
                _fmt(float(sel.get("flight_time_s") or 0) / 60.0, 1))],
            ["制动 ΔV / 倾侧角", "%s m/s / %s°" % (
                _fmt(sel.get("deorbit_dv_mps"), 0), _fmt(sel.get("bank_deg"), 1))],
            ["航警匹配", (("区内" if sel.get("inside_zone") else "区外")
                         + ("，时间在 B)/C) 内" if sel.get("notam_time_ok") else ""))
             if ret.get("notam_zones") else
             ("在着陆场半径内" if sel.get("inside_zone") else "在着陆场半径外（最近机会）")],
            ["距边界/中心", "%s km" % _fmt(sel.get("dist_to_zone_km"), 1)],
            ["峰值动压 / 过载 / 热流", "%s kPa / %s g / %s W·cm⁻²" % (
                _fmt((sel.get("peaks") or {}).get("q_pa", 0) / 1000.0, 1),
                _fmt((sel.get("peaks") or {}).get("g"), 1),
                _fmt((sel.get("peaks") or {}).get("heat_w_cm2"), 0))],
        ]))
        L.append("")
    ev = ret.get("timeline") or []
    if ev:
        L.append("### 3.1 返回时序")
        L.append("")
        rows = []
        for e in ev:
            rows.append([e.get("event", ""), e.get("t_utc", ""),
                         _fmt(e.get("alt_km"), 1), _fmt(e.get("speed_m_s"), 0)])
        L.append(_table(["事件", "UTC", "高度 (km)", "速度 (m/s)"], rows))
        L.append("")
    phases = ret.get("phases") or []
    if phases:
        L.append("### 3.2 返回阶段")
        L.append("")
        L.append(_table(["阶段", "开始 (UTC)", "结束 (UTC)", "时长 (s)", "说明"],
                        [[p.get("phase", ""), p.get("start_utc", ""),
                          p.get("end_utc", ""), _fmt(p.get("duration_s"), 0),
                          p.get("note", "")] for p in phases]))
        L.append("")
    seps = ret.get("separations") or []
    if seps:
        L.append("### 3.3 分离点")
        L.append("")
        L.append(_table(["分离事件", "UTC", "纬度", "经度", "高度 (km)", "速度 (m/s)"],
                        [[e.get("event", ""), e.get("t_utc", ""),
                          _fmt(e.get("lat_deg"), 3), _fmt(e.get("lon_deg"), 3),
                          _fmt(e.get("alt_km"), 1), _fmt(e.get("speed_m_s"), 0)]
                         for e in seps]))
        L.append("> 船站分离/轨道舱分离早于制动，位置按空间站轨道给出；推进舱分离在滑行段，"
                 "位置按再入时序插值；分离时刻为工程阶段标记，不是分离动力学。")
        L.append("")
    pc = ret.get("pre_context") or {}
    if pc:
        L.append("### 3.4 返回前 30 min 空间站/神舟轨道")
        L.append("")
        L.append(_table(["项目", "数值"], [
            ["区间", "%s ~ %s UTC" % (pc.get("start_utc", "—"), pc.get("end_utc", "—"))],
            ["对象", "、".join(pc.get("objects") or [])],
            ["采样点", str(pc.get("points", "—"))],
            ["文件", ret.get("pre_context_csv") or "—"],
        ]))
        L.append("")
    disp = ret.get("dispersion") or {}
    if disp:
        L.append("### 3.5 落点大致范围（1σ）")
        L.append("")
        L.append(_table(["项目", "数值"], [
            ["中心", "%s°N, %s°E" % (_fmt(disp.get("center_lat"), 3),
                                     _fmt(disp.get("center_lon"), 3))],
            ["长半轴 / 短半轴", "%s km / %s km" % (
                _fmt(disp.get("semi_major_km"), 1), _fmt(disp.get("semi_minor_km"), 1))],
            ["方向", "%s°" % _fmt(disp.get("orientation_deg"), 0)],
        ]))
        L.append("> 由 ΔV/倾侧角/质量小扰动估计，仅表示大致范围，不是官方残骸区。")
        L.append("")
    if ret.get("plot"):
        L.append("![返回舱弹道](../%s)" % ret["plot"])
        L.append("")
    if ret.get("landing_map"):
        L.append("![返回舱落区](../%s)" % ret["landing_map"])
        L.append("")
    L.append("## 4. 交付自检")
    L.append("")
    L.append(_table(["项目", "结果"], [
        ["自检", "%d/%d PASS" % (int(qc.get("passed") or 0), int(qc.get("total") or 0))],
        ["返回窗口/弹道", "已生成" if ret.get("selected") else "缺失"],
        ["时序 CSV", ret.get("timeline_csv") or "—"],
        ["弹道图", ret.get("plot") or "—"],
    ]))
    L.append("")
    L.append("## 5. 数据来源、假设与免责声明")
    L.append("")
    L.append("- 空间站轨道：%s（%s）" % (
        (st.get("station") or {}).get("name", "—"),
        (st.get("station") or {}).get("source") or "—"))
    L.append("- 返回航警：%s" % ("、".join(ret.get("notam_zones") or [])
                                  or "未使用，按固定着陆场半径判定"))
    L.append("- 再入模型：3-DOF 简化返回舱（USSA 大气、阻力、L/D + 倾侧角、"
             "减速伞/主伞、J2），非实际飞行程序；落点精度约几十 km。")
    L.append("- 本报告仅供公开信息分析与教学使用，不能作为返回/回收、航警划设或飞行安全依据。")
    L.append("")
    L.append("## 6. 附件")
    L.append("")
    for rel, desc in (("sim/返回舱返回.json", "返回窗口/落点/散布 JSON"),
                      ("sim/返回舱_时序.csv", "返回时序 CSV"),
                      ("sim/返回舱窗口.csv", "返回窗口 CSV"),
                      ("sim/返回舱事件与分离点.csv", "返回阶段/分离点 CSV"),
                      ("sim/返回舱阶段.csv", "返回阶段汇总 CSV"),
                      ("sim/返回前30分钟_空间站与神舟.csv",
                       "返回前 30 min 空间站/神舟轨道"),
                      ("sim/返回舱弹道.png", "返回弹道图"),
                      ("map/返回舱落区.png", "落区/航警区/散布图"),
                      ("viz/返回舱弹道.kml", "Google Earth KML"),
                      ("viz/返回舱弹道.kmz", "Google Earth KMZ")):
        L.append("- `%s`：%s" % (rel, desc))
    L.append("")
    text = "\n".join(L)
    out_dir = os.path.join(workdir, "report")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "弹道仿真报告.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        _write_docx(out_dir, mission, text)
    except Exception:  # noqa: BLE001
        pass
    return path


def generate_report(workdir: str, config: Optional[Dict[str, Any]] = None,
                    log_tail: str = "") -> str:
    """生成 report/弹道仿真报告.md，返回路径。"""
    workdir = os.path.abspath(workdir)
    cfg = config or _read_json(os.path.join(workdir, "config.json"), {})
    mission = cfg.get("mission") or {}
    if bool(cfg.get("return_only")) or str(mission.get("profile") or "") == "return":
        ret = _read_json(os.path.join(workdir, "sim", "返回舱返回.json"), {})
        return _generate_return_report(workdir, cfg, ret)
    site = mission.get("launch_site") or {}
    veh = cfg.get("vehicle") or {}
    gd = cfg.get("guidance") or {}
    v3 = cfg.get("_v3") or {}
    out_dir = os.path.join(workdir, "report")
    os.makedirs(out_dir, exist_ok=True)

    events = _read_csv(os.path.join(workdir, "sim", "弹道事件表.csv"))
    impacts = _read_csv(os.path.join(workdir, "sim", "落点与射程.csv"))
    orbit = _read_json(os.path.join(workdir, "sim", "入轨根数.json"), {})
    inv = _read_json(os.path.join(workdir, "sim", "反演参数.json"), {})
    window = _read_json(os.path.join(workdir, "report", "发射窗口计算.json"), {})
    sky = _read_json(os.path.join(workdir, "sky", "sky_summary.json"), {})
    mc = _read_json(os.path.join(workdir, "uncertainty", "mc_stats.json"), {})
    qc = _read_json(os.path.join(workdir, "report", "qc_report.json"), {})
    station = _read_json(os.path.join(workdir, "sim", "空间站轨道.json"), {})
    seq = cfg.get("launch_sequence") or {}
    docking = _read_json(os.path.join(workdir, "sim", "交会对接.json"), {})
    t0win = _read_json(os.path.join(workdir, "report", "交会发射窗口.json"), {})
    ret = _read_json(os.path.join(workdir, "sim", "返回舱返回.json"), {})

    L: List[str] = []
    L.append("# %s" % (mission.get("name") or "火箭上升段弹道仿真报告"))
    L.append("")
    L.append("> %s" % (mission.get("disclaimer") or "仅供公开信息分析与教学使用"))
    L.append("")
    L.append("> 本报告由「火箭弹道工具箱 v%s 离线版」自动生成（%s），未使用任何在线 AI 服务。" %
             (_app_version(), datetime.now().strftime("%Y-%m-%d %H:%M")))
    L.append("")
    L.extend(_v4_overview(workdir))

    # ---------------- 1. 任务与数据来源 ----------------
    L.append("## 1. 任务概况与数据来源")
    L.append("")
    rows = [
        ["任务名称", mission.get("name", "—")],
        ["任务类型", _MT_CN.get(str(mission.get("mission_type")
                                    or v3.get("mission_type") or ""), "—")],
        ["火箭型号", mission.get("rocket", "—")],
        ["发射工位", "%s（%.4f°N, %.4f°E, 海拔 %s m）" % (
            site.get("name", "—"), float(site.get("lat_deg") or 0),
            float(site.get("lon_deg") or 0), _fmt(site.get("alt_m_ellipsoid"), 0))],
        ["起飞时刻 T0（UTC）", mission.get("t0_utc", "—")],
        ["起飞时刻 T0（北京时）", _bj(mission.get("t0_utc", ""))],
        ["T0 依据", mission.get("t0_note", "—")],
        ["参考轨道", ("h=%s km, i=%s°（%s）" % (
            _fmt((mission.get("reference_orbit") or {}).get("alt_km"), 1),
            _fmt((mission.get("reference_orbit") or {}).get("i_deg")),
            (mission.get("reference_orbit") or {}).get("confidence", "—")))
         if mission.get("reference_orbit") else "无（未做轨道软约束）"],
        ["数值口径", "%s（dt=%s s）" % ((cfg.get("numerics") or {}).get("mode", "—"),
                                       _fmt((cfg.get("numerics") or {}).get("dt"), 2))],
    ]
    _cust = (mission.get("custom_rocket") or {}).get("record") or {}
    if _cust:
        _vch = _cust.get("vehicle") or {}
        rows.append(["自定义箭型", "%s（用户编辑器输入；%d 级%s；参数见 §12 附录与 "
                                 "config.vehicle）" % (
                                     _cust.get("name") or "—",
                                     len(_vch.get("stages") or []),
                                     "，含助推器" if _vch.get("boosters") else "")])
    _seq = cfg.get("launch_sequence") or {}
    if _seq.get("custom"):
        rows.append(["自定义时序", "是（留空项取箭型默认）：%s" % (
            "；".join("%s=%s s" % (x.get("label"), _fmt(x.get("value"), 1))
                     for x in (_seq.get("table") or [])[:6]) or "—")])
    L.append(_table(["项目", "内容"], rows))

    # 数据体检
    prov = mission.get("_provenance") or []
    if prov:
        L.append("### 1.1 数据体检（每个关键字段的来源与置信度）")
        L.append("")
        src_cn = {"user": "用户给定", "db": "内置库", "infer": "自动推断",
                  "default": "默认值", "derived": "计算得出"}
        rows = []
        for p in prov:
            rows.append([p.get("label", p.get("field", "")), p.get("value", ""),
                         src_cn.get(p.get("source", ""), p.get("source", "")),
                         p.get("confidence", ""), p.get("note", "")])
        L.append(_table(["字段", "取值", "来源", "置信度", "说明"], rows))
        low = [p for p in prov if p.get("confidence") == "低"]
        inferred = [p for p in prov if p.get("confidence") != "低"
                    and p.get("source") in ("infer", "default")]
        if low:
            L.append("**低置信度字段（%d 项，结论中必须注明不确定性）：**" % len(low))
            L.append("")
            for p in low:
                L.append("- %s = %s（%s，%s）%s" % (
                    p.get("label", ""), p.get("value", ""),
                    src_cn.get(p.get("source", ""), p.get("source", "")),
                    p.get("confidence", ""),
                    ("：" + p.get("note", "")) if p.get("note") else ""))
            L.append("")
        if inferred:
            L.append("**推断字段（%d 项，置信度中/高，可参考）：**" % len(inferred))
            L.append("")
            for p in inferred:
                L.append("- %s = %s（%s）" % (
                    p.get("label", ""), p.get("value", ""),
                    (p.get("note", "") or "")[:200]))
            L.append("")

    # 航警
    notams = cfg.get("notams") or []
    if notams:
        L.append("### 1.2 危险区（航警）")
        L.append("")
        rows = []
        for z in notams:
            rows.append([z.get("id", ""), "%s ~ %s" % (_bj(z.get("b_utc", ""))[11:16],
                                                      _bj(z.get("c_utc", ""))[11:16]),
                         z.get("stage_hint", ""), z.get("fir", ""),
                         "%d 顶点" % len(z.get("polygon_latlon") or []),
                         (z.get("q_line") or "—")])
        L.append(_table(["编号", "有效期（北京时）", "对应残骸", "FIR", "多边形", "Q 行"], rows))
    elif notams == []:
        L.append("### 1.2 危险区")
        L.append("")
        L.append("本任务**未提供官方航警**：落区由标称弹道预测生成，仅用于分析，不构成官方禁区。")
        L.append("")

    # ---------------- 2. 弹道与事件 ----------------
    L.append("## 2. 上升段弹道与关键事件")
    L.append("")
    if events:
        rows = []
        for e in events:
            rows.append([e.get("event", ""), _fmt(e.get("t_met_s"), 1),
                         _bj(e.get("t_utc", "")), "%s, %s" % (
                             _fmt(e.get("lat_deg"), 4), _fmt(e.get("lon_deg"), 4)),
                         _fmt((float(e.get("alt_m") or 0) / 1000.0), 1),
                         _fmt(e.get("speed_m_s"), 0), _fmt(e.get("mass_kg"), 0)])
        L.append(_table(["事件", "T+ (s)", "时刻（北京时）", "位置", "高度 (km)",
                         "速度 (m/s)", "质量 (kg)"], rows))
    else:
        L.append("_（未找到事件表）_")
    L.append("")
    if seq:
        _sv = seq.get("values") or {}
        _stages = (cfg.get("vehicle") or {}).get("stages") or []
        _rows = []
        if ((cfg.get("vehicle") or {}).get("boosters")):
            _rows.append(["助推器分离", _fmt(_sv.get("boosters_sep_s"), 1, " s")
                          if _sv.get("boosters_sep_s") is not None else "自动（耗尽）"])
        for _i, _s in enumerate(_stages[:-1]):
            _v = (_sv.get("stage_sep_s") or [None] * len(_stages))[_i] \
                if _i < len(_sv.get("stage_sep_s") or []) else None
            _rows.append(["%s关机/分离" % (_s.get("name") or "第%d级" % (_i + 1)),
                          _fmt(_v, 1, " s") if _v is not None else "自动（耗尽）"])
        if ((cfg.get("vehicle") or {}).get("fairing") or {}).get("mass_kg"):
            _rows.append(["整流罩分离", _fmt(_sv.get("fairing_sep_s"), 1, " s")
                          if _sv.get("fairing_sep_s") is not None else "自动"])
        if _stages:
            _rows.append(["末级关机（SECO）", _fmt(_sv.get("seco_s"), 1, " s")
                          if _sv.get("seco_s") is not None else "自动（耗尽/目标轨道）"])
        L.append("### 2.0 发射时序")
        L.append("")
        L.append(_table(["时序项", "采用值"], _rows))
        L.append("> 来源：%s。留空项采用箭型库默认；自定义分离早于自然耗尽时按程序时刻抛离，"
                 "剩余推进剂随级抛掉；晚于自然耗尽仍按自然分离。"
                 % ("用户自定义" if seq.get("custom") else "箭型库默认"))
        L.append("")

    # 入轨
    if orbit and orbit.get("error"):
        L.append("### 2.1 入轨")
        L.append("")
        L.append("**未进入轨道**：%s" % orbit.get("error"))
        L.append("")
    elif orbit:
        ins = orbit.get("insertion") or {}
        rows = [
            ["入轨时刻 T+", _fmt(ins.get("t_met_s"), 1, " s")],
            ["入轨高度", _fmt(ins.get("alt_km"), 1, " km")],
            ["入轨速度", _fmt(ins.get("speed_m_s"), 0, " m/s")],
            ["半长轴 a", _fmt(orbit.get("a_km"), 1, " km")],
            ["偏心率 e", _fmt(orbit.get("e"), 5)],
            ["倾角 i", _fmt(orbit.get("i_deg"), 2, "°")],
            ["近地点 / 远地点", "%s / %s km" % (_fmt(orbit.get("perigee_km"), 1),
                                              _fmt(orbit.get("apogee_km"), 1))],
            ["周期", _fmt(orbit.get("period_s"), 0, " s")],
            ["RAAN / 近地点幅角", "%s° / %s°" % (_fmt(orbit.get("raan_deg"), 2),
                                              _fmt(orbit.get("argp_deg"), 2))],
        ]
        L.append("### 2.1 入轨根数（SECO 时刻瞬时根数）")
        L.append("")
        if orbit.get("warning"):
            L.append("> **注意**：%s" % orbit["warning"])
            L.append("")
        L.append(_table(["参数", "数值"], rows))

    # 反演
    if inv.get("parameters"):
        L.append("### 2.2 参数反演（按落区/轨道率定）")
        L.append("")
        L.append("目标函数最优值：**%s**（%s 次仿真，%s 代）。反演参数已回填 config.json。"
                 % (_fmt(inv.get("objective"), 3), inv.get("nfev", "—"),
                    inv.get("iterations", "—")))
        L.append("")
        rows = [[k, _fmt(v, 4)] for k, v in (inv.get("parameters") or {}).items()]
        L.append(_table(["参数", "最优值"], rows))
        if (v3.get("inversion") or {}).get("reverted"):
            L.append("")
            L.append("> **注意**：本次反演未收敛（最优目标 %s），程序已**回退到标称参数**，"
                     "上表仅为反演器给出的候选值。落点可能仍在危险区外，"
                     "请核对航警残骸类型/多边形或改用与任务匹配的箭型参数。"
                     % _fmt((v3.get("inversion") or {}).get("objective"), 1))
            L.append("")

    # ---------------- 3. 落点与危险区 ----------------
    L.append("## 3. 残骸落点与危险区")
    L.append("")
    if impacts:
        rows = []
        for r in impacts:
            rows.append([r.get("object", ""), _fmt(r.get("impact_t_met_s"), 1),
                         _bj(r.get("impact_t_utc", "")),
                         "%s, %s" % (_fmt(r.get("lat_deg"), 4), _fmt(r.get("lon_deg"), 4)),
                         _fmt(r.get("great_circle_km"), 1),
                         r.get("zone_id") or "—",
                         ("是（距边界 %s km）" % r.get("dist_to_boundary_km"))
                         if r.get("inside_zone") == "true" else "否"])
        L.append(_table(["残骸", "落点 T+ (s)", "落点时刻（北京时）", "落点经纬度",
                         "大圆距离 (km)", "对应区", "区内"], rows))
        n_in = sum(1 for r in impacts if r.get("inside_zone") == "true")
        L.append("落点入区情况：**%d/%d** 个残骸落在对应危险区内。" % (n_in, len(impacts)))
        L.append("")
    else:
        L.append("_（未找到落点表）_")
        L.append("")

    # 发射窗口
    L.append("## 4. 发射窗口")
    L.append("")
    ow = (window or {}).get("orbit_window") or {}
    # ---- 4.1 轨道平面窗口 ----
    L.append("### 4.1 轨道平面窗口（已知轨道/交会目标）")
    L.append("")
    if ow.get("available"):
        tgt = ow.get("target") or {}
        kind_cn = {"station": "空间站交会", "raan": "目标 RAAN",
                   "ltan": "太阳同步 LTAN", "free": "无平面约束"}.get(
            tgt.get("kind"), str(tgt.get("kind")))
        L.append("目标类型：**%s**；平面容差 ±%s°；搜索范围 ±%s h（步长 %s s）。"
                 % (kind_cn, _fmt(ow.get("tolerance_deg"), 2),
                    _fmt(ow.get("search_hours"), 0), _fmt(ow.get("step_s"), 0)))
        L.append("")
        ins0 = ow.get("insertion") or {}
        if ins0:
            L.append("入轨点：%s（T+%s s，%s km，相对速度 %s km/s，方位角修正 %s° / 仿真 %s°）。"
                     % (ins0.get("utc", "—"), _fmt(ins0.get("t_met_s"), 1),
                        _fmt(ins0.get("alt_km"), 1), _fmt(ins0.get("v_rel_kms"), 4),
                        _fmt(ins0.get("az_deg"), 2), _fmt(ins0.get("az_sim_deg"), 2)))
            L.append("")
        rows = []
        for k, w in enumerate(ow.get("windows") or [], 1):
            rv = w.get("rendezvous") or {}
            rv_txt = "—"
            if rv:
                rv_txt = ("可行，ΔV %s m/s，%s h" % (_fmt(rv.get("dv_total_mps"), 0),
                                                   _fmt(rv.get("duration_h"), 1))
                          if rv.get("feasible") else "不可行（%s）" % (rv.get("reason") or "相位/预算"))
            rows.append([str(k), w.get("start_utc", "—")[5:19].replace("T", " "),
                         w.get("best_utc", "—"), w.get("end_utc", "—")[5:19].replace("T", " "),
                         _fmt(w.get("duration_s"), 0), _fmt(w.get("d_raan_deg"), 3),
                         _fmt(w.get("ltan_ach_h"), 2), rv_txt])
        L.append(_table(["#", "开始 (UTC)", "最佳 T0 (UTC)", "结束 (UTC)",
                         "窗宽 (s)", "ΔRAAN (°)", "LTAN (h)", "交会可行性"], rows))
        bw = ow.get("best_near_t0") or {}
        if bw:
            dt_s = (datetime.fromisoformat(bw["best_utc"].replace("Z", "+00:00"))
                    - datetime.fromisoformat(str(mission.get("t0_utc")).replace("Z", "+00:00"))
                    ).total_seconds()
            L.append("距标称 T0 最近的窗口：**%s UTC**（北京时 %s，%+.0f s，ΔRAAN %s°）。"
                     % (bw.get("best_utc", "—"), _bj(bw.get("best_utc", "")), dt_s,
                        _fmt(bw.get("d_raan_deg"), 3)))
            L.append("")
        L.append("说明：用入轨点相对速度 + 地球自转构造轨道面，按目标倾角反解入轨方位角；"
                 "窗口内 |RAAN_achieved − RAAN_target| ≤ 容差。交会可行性由相位调整 + Hohmann "
                 "+ 末段相对导航模型评估（详见第 5 章）。")
        L.append("")
    else:
        L.append("**未计算轨道平面窗口**：%s" % (ow.get("reason") or "无目标轨道/交会约束"))
        L.append("")

    # ---- 4.2 航警时窗 ----
    L.append("### 4.2 航警时窗（残骸到达时刻约束）")
    L.append("")
    wins = []
    for key in ("model_window", "practical_window"):
        w = (window or {}).get(key)
        if w:
            wins.append([key, w.get("start_utc", "—"), w.get("end_utc", "—"),
                         _fmt(w.get("duration_min"), 1),
                         w.get("start_bjt", "—"), w.get("end_bjt", "—")])
    if wins:
        rows = []
        for w in wins:
            label = "理论窗口" if w[0] == "model_window" else "可用窗口（扣准备裕度）"
            rows.append([label, w[1], w[2], w[3], "%s ~ %s" % (w[4], w[5])])
        L.append(_table(["约束", "窗口开始 (UTC)", "窗口结束 (UTC)", "时长 (min)",
                         "北京时"], rows))
        cons = (window or {}).get("constraints") or []
        if cons:
            rows = []
            for c0 in cons:
                rows.append([c0.get("object", ""), c0.get("zone_id", ""),
                             _fmt(c0.get("t_arrival_met_s"), 1),
                             _bj(c0.get("t_arrival_utc", "")),
                             c0.get("allowed_t0_from", "—"), c0.get("allowed_t0_to", "—")])
            L.append(_table(["残骸", "对应区", "到达 T+ (s)", "到达时刻（北京时）",
                             "允许 T0 起", "允许 T0 止"], rows))
        L.append("说明：窗口由「航警 B)/C) 必须覆盖残骸到达时刻」反推，已扣除准备裕度 %s s。"
                 % _fmt((cfg.get("window") or {}).get("margin_s"), 0))
        L.append("")
    else:
        reason = (window or {}).get("reason") or "未计算（无航警或步骤被跳过）"
        L.append("**航警时窗无解。** %s" % reason)
        L.append("")

    # ---- 4.3 CZ-2F 交会窗口 ----
    if ow.get("available") and (ow.get("target") or {}).get("kind") == "station":
        L.append("### 4.3 CZ-2F 交会窗口（相位可行性）")
        L.append("")
        rows = []
        for k, w in enumerate(ow.get("windows") or [], 1):
            rv = w.get("rendezvous") or {}
            if not rv:
                continue
            rows.append([str(k), w.get("best_utc", "—"),
                         _fmt(rv.get("phase_lead_deg"), 2),
                         "k=%s / a=%s km" % (_fmt(rv.get("phasing_orbits"), 0),
                                             _fmt(rv.get("phasing_a_km"), 0)),
                         _fmt(rv.get("dv_total_mps"), 0),
                         _fmt(rv.get("duration_h"), 2),
                         "是" if rv.get("feasible") else "否",
                         rv.get("dock_utc", "—")])
        if rows:
            L.append(_table(["#", "最佳 T0 (UTC)", "入轨相位差 (°)", "相位轨道 (圈/km)",
                             "总 ΔV (m/s)", "交会时长 (h)", "可行", "预计对接 (UTC)"], rows))
            L.append("说明：相位模型假设共面圆轨道；ΔV 含相位调整、Hohmann、圆化与末段修正。"
                     "预算与期望时长见 `config.station.rendezvous`。")
            L.append("")

    # ---- 4.4 交会发射窗口与 T0 反推（v4.1） ----
    if t0win.get("enabled") and t0win.get("available"):
        L.append("### 4.4 交会发射窗口与 T0 反推")
        L.append("")
        tw = (mission.get("t0_window") or {})
        _geo = (tw.get("geometric_windows") or [])
        rows = [
            ["用户原 T0", "%s UTC" % (tw.get("nominal_t0_utc") or
                                     t0win.get("t0_nominal_utc") or "—")],
            ["反推后 T0", "%s UTC（北京时 %s）" % (
                t0win.get("recommended_t0_utc") or tw.get("final_t0_utc") or "—",
                t0win.get("recommended_t0_bjt") or "—")],
            ["T0 平移（合计）", "%s s" % _fmt(
                tw.get("shift_s") if tw.get("shift_s") is not None
                else t0win.get("shift_s"), 0)],
            ["可行交会窗口", "%s 个（共 %d 个平面窗口，容差 %s°）" % (
                _fmt(t0win.get("feasible_n"), 0), len(t0win.get("windows") or []),
                _fmt(t0win.get("tolerance_deg"), 2))],
            ["入轨面残差", "%s°（实测入轨面 vs 空间站面；Δi %s°，ΔRAAN %s°）" % (
                _fmt(t0win.get("plane_residual_deg"), 3),
                _fmt(t0win.get("plane_di_deg"), 3),
                _fmt(t0win.get("plane_draan_deg"), 3))],
            ["几何窗口（发射场穿目标面）", "、".join("%s UTC" % g for g in _geo[:3])
             if _geo else "—"],
            ["是否已写回配置", "是（弹道/落区/窗口/交会均使用同一 T0）" if tw.get("applied")
             else "否（保留用户给定 T0，仅给出最近可行窗口）"],
            ["依据", tw.get("reason") or t0win.get("recommended_reason") or "—"],
        ]
        L.append(_table(["项目", "内容"], rows))
        wr = t0win.get("windows") or []
        if wr:
            rows = []
            for k, w in enumerate(wr, 1):
                rows.append([
                    str(k), w.get("start_utc", "—"), w.get("end_utc", "—"),
                    w.get("best_utc", "—"),
                    ("是" if w.get("feasible") else "否"),
                    _fmt(w.get("dv_total_mps"), 0),
                    _fmt(w.get("duration_h"), 2),
                    w.get("dock_utc") or "—",
                    ("是" if w.get("notam_ok", True) else "否"),
                ])
            L.append(_table(["#", "T0 起点 (UTC)", "T0 终点 (UTC)", "最佳 T0 (UTC)",
                             "交会可行", "总 ΔV (m/s)", "时长 (h)", "预计对接 (UTC)",
                             "航警时窗内"], rows))
        L.append("说明：窗口由空间站星历 + 发射场 + 上升段**实测**入轨点几何推出，不查询外部任务参数；"
                 "T0 反推只在「① 勾选按交会窗口反推」或 T0 本身是推断值时自动执行，"
                 "并按「重跑仿真 → 重算窗口」迭代到平面/相位自洽。"
                 "几何窗口是发射场穿越空间站轨道面的时刻（与制导无关，可作独立校核）。")
        L.append("")

    # ---------------- 5. CZ-2F 空间站交会对接 ----------------
    L.append("## 5. 空间站交会对接（CZ-2F 专用）")
    L.append("")
    if not docking.get("enabled"):
        L.append("本任务未启用空间站交会模块：%s"
                 % (docking.get("reason") or "非 CZ-2F 任务或未配置空间站轨道数据"))
        L.append("")
    else:
        st = docking.get("station") or station.get("station") or {}
        st_el = station.get("at_t0") or {}
        L.append("### 5.1 空间站轨道数据")
        L.append("")
        cov = st.get("coverage_utc") or []
        rows = [
            ["目标", st.get("name", "天宫空间站")],
            ["NORAD / 对象", "%s / %s" % (st.get("norad_id", "—"), st.get("object_id", "—"))],
            ["数据源", st.get("source", "—")],
            ["类型 / 参考系", "%s / %s" % (st.get("kind", "—"), st.get("frame", "—"))],
            ["覆盖区间", ("%s ~ %s" % (cov[0], cov[1])) if len(cov) >= 2 else "—"],
            ["轨道根数（T0）", "a=%s km, i=%s°, RAAN=%s°, u=%s°" % (
                _fmt(st_el.get("a_km"), 2), _fmt(st_el.get("i_deg"), 3),
                _fmt(st_el.get("raan_deg"), 3), _fmt(st_el.get("u_deg"), 3))],
        ]
        if st.get("warning"):
            rows.append(["注意", st.get("warning")])
        L.append(_table(["项目", "内容"], rows))
        _ctx = docking.get("task_context") or {}
        if _ctx and _ctx.get("n"):
            L.append("")
            L.append(_table(["任务上下文", "内容"], [
                ["前后各 30 min 区间", "%s ~ %s UTC" % (
                    _ctx.get("start_utc", "—"), _ctx.get("end_utc", "—"))],
                ["星下点采样", "%s 点（%s）" % (_ctx.get("n"), _ctx.get("file", ""))],
            ]))
        if st.get("sources"):
            src = (st.get("sources") or [{}])[0]
            if src.get("url"):
                L.append("数据来源：%s（%s）。" % (src.get("url"), src.get("note", "")))
                L.append("")

        ins0 = docking.get("insertion") or {}
        L.append("### 5.2 入轨段")
        L.append("")
        L.append(_table(["项目", "数值"], [
            ["入轨时刻 T+", "%s s（%s UTC）" % (_fmt(ins0.get("t_met_s"), 1),
                                              ins0.get("t_utc", "—"))],
            ["入轨高度", _fmt(ins0.get("alt_km"), 2) + " km"],
            ["入轨倾角 / RAAN", "%s° / %s°" % (_fmt(ins0.get("i_deg"), 4),
                                              _fmt(ins0.get("raan_deg"), 4))],
            ["入轨方位角", "%s°（%s）" % (_fmt(ins0.get("az_deg"), 2),
                                        ins0.get("plane_note", ""))],
        ]))
        sa = docking.get("station_at_insertion") or {}
        if sa:
            L.append("入轨时刻空间站：高度 %s km、倾角 %s°、RAAN %s°、纬度幅角 %s°。"
                     % (_fmt(sa.get("alt_km"), 1), _fmt(sa.get("i_deg"), 3),
                        _fmt(sa.get("raan_deg"), 3), _fmt(sa.get("u_deg"), 3)))
            L.append("")

        plan0 = docking.get("plan") or {}
        phase0 = docking.get("phase") or {}
        L.append("### 5.3 交会方案")
        L.append("")
        L.append("计算用 T0：**%s UTC**（%s）。入轨时目标相位差 %s°；"
                 "相位模型目标差 %s°。"
                 % (docking.get("t0_used_utc", "—"), docking.get("t0_reason", "—"),
                    _fmt(phase0.get("lead_deg"), 2),
                    _fmt(phase0.get("target_lead_deg"), 2)))
        L.append("")
        _pl0 = docking.get("plane") or {}
        if _pl0:
            L.append("本次交会按**实测入轨状态**计算：入轨面与空间站面夹角 **%s°**"
                     "（Δi %s°，ΔRAAN %s°；窗口与残差见 §4.4，原始数据见 "
                     "`sim/交会对接.json.plane`）。"
                     % (_fmt(_pl0.get("residual_deg"), 3),
                        _fmt(float(_pl0.get("achieved_i_deg") or 0.0)
                             - float(_pl0.get("target_i_deg") or 0.0), 3),
                        _fmt(float(_pl0.get("achieved_raan_deg") or 0.0)
                             - float(_pl0.get("target_raan_deg") or 0.0), 3)))
            L.append("")
        # 标称 T0 与所选 T0 的平面差提示
        try:
            bw5 = ((window or {}).get("orbit_window") or {}).get("best_near_t0") or {}
            if bw5.get("best_utc") and docking.get("t0_nominal_utc"):
                dt5 = (datetime.fromisoformat(docking["t0_nominal_utc"].replace("Z", "+00:00"))
                       - datetime.fromisoformat(bw5["best_utc"].replace("Z", "+00:00"))
                       ).total_seconds()
                if abs(dt5) > 30:
                    L.append("> 注意：标称 T0 %s 相对最近平面窗口中心 **%+.0f s**（按地球自转"
                             "15.04°/h 折算 ΔRAAN ≈ %.2f°），若严格按标称 T0 发射则轨道面不匹配；"
                             "本报告的交会时间线按窗口中心计算。"
                             % (docking.get("t0_nominal_utc"), dt5,
                                abs(dt5) * 15.041 / 3600.0))
                    L.append("")
        except Exception:  # noqa: BLE001
            pass
        L.append(_table(["项目", "数值"], [
            ["方案 / 相位圈数", "%s / k=%s" % (plan0.get("mode", "—"),
                                             _fmt(plan0.get("phasing_orbits"), 0))],
            ["相位轨道高度", "%s km（a=%s km）" % (
                _fmt((plan0.get("phasing_a_km") or 0) - 6378.137, 1),
                _fmt(plan0.get("phasing_a_km"), 1))],
            ["相位调整 / 转移 / 圆化 ΔV",
             "%s / %s / %s m/s" % (_fmt(plan0.get("dv_phase_mps"), 1),
                                   _fmt(plan0.get("dv_transfer_mps"), 1),
                                   _fmt(plan0.get("dv_circular_mps"), 1))],
            ["末段逼近 / 对接制动 ΔV",
             "%s / %s m/s" % (_fmt(plan0.get("dv_final_mps"), 2),
                              _fmt(plan0.get("dv_dock_mps"), 2))],
            ["总 ΔV / 预算", "%s / %s m/s" % (_fmt(docking.get("total_dv_mps"), 1),
                                            _fmt(docking.get("budget_mps"), 0))],
            ["总时长（入轨→对接）", _fmt(plan0.get("duration_h"), 2) + " h"],
            ["可行性", "可行" if docking.get("feasible") else "不可行"],
        ]))
        tl = docking.get("timeline") or []
        if tl:
            L.append("### 5.4 入轨→对接时间线")
            L.append("")
            rows = []
            for row in tl:
                rows.append([row.get("phase", ""), _fmt(row.get("t_met_s"), 1),
                             _bj(row.get("t_utc", "")), _fmt(row.get("duration_s"), 0),
                             _fmt(row.get("dv_mps"), 2),
                             "%s / %s" % (_fmt(row.get("lat_deg"), 3),
                                          _fmt(row.get("lon_deg"), 3)),
                             _fmt(row.get("alt_km"), 1),
                             _fmt(row.get("range_to_station_km"), 2), row.get("note", "")])
            L.append(_table(["阶段", "T+ (s)", "时刻（北京时）", "累计时长 (s)",
                             "ΔV (m/s)", "位置", "高度 (km)", "距空间站 (km)",
                             "说明"], rows))
        dk = docking.get("docking") or {}
        if dk:
            L.append("### 5.5 对接结果")
            L.append("")
            L.append(_table(["项目", "数值"], [
                ["预计对接时刻", "%s UTC（北京时 %s）" % (dk.get("t_utc", "—"),
                                                    dk.get("t_bjt", "—"))],
                ["对接点星下点", "%s°N, %s°E（高度 %s km）" % (
                    _fmt(dk.get("station_lat_deg"), 3),
                    _fmt(dk.get("station_lon_deg"), 3), _fmt(dk.get("alt_km"), 1))],
                ["位置来源", dk.get("subpoint_note")
                 or "对接瞬间空间站/飞船共同星下点"],
                ["三维标注", "全流程轨迹（发射→入轨→相位→转移→对接）+ 60 s 时间动画 + "
                             "入轨点/对接点，见 viz/弹道三维_含残骸坠落.kmz「交会对接」文件夹"],
                ["轨迹数据", "发射→入轨 sim/trajectory.csv；入轨→对接 sim/交会轨迹.csv（30 s 采样）"],
                ["末端模型残差", "%s km / %s m/s（二体 Lambert 末段，实际由相对导航修正）"
                 % (_fmt(dk.get("residual_range_km"), 3),
                    _fmt(dk.get("residual_rel_vel_mps"), 3))],
                ["接近段相对速度", _fmt(dk.get("rel_vel_before_dock_mps"), 2) + " m/s"],
            ]))
        for note in docking.get("notes") or []:
            L.append("> 注意：%s" % note)
            L.append("")
        L.append("假设与局限：")
        L.append("")
        for a in docking.get("assumptions") or []:
            L.append("- " + str(a))
        L.append("")

    # ---------------- 5.6 返回舱返回（v3.5） ----------------
    if ret.get("enabled"):
        L.append("### 5.6 返回舱返回窗口与弹道（v3.5）")
        L.append("")
        _site = ret.get("landing_site") or {}
        _zones = ret.get("notam_zones") or []
        L.append(_table(["项目", "数值"], [
            ["着陆场", "%s（%s°N, %s°E，R=%s km）" % (
                _site.get("name", "—"), _fmt(_site.get("lat_deg"), 4),
                _fmt(_site.get("lon_deg"), 4), _fmt(_site.get("radius_km"), 0))],
            ["返回航警区", "、".join(_zones) if _zones else "未使用（仅按着陆场半径）"],
            ["制动搜索区间", "%s ~ %s UTC" % ((ret.get("search") or {}).get("start_utc", "—"),
                                              (ret.get("search") or {}).get("end_utc", "—"))],
            ["可用窗口", "%d 个" % int(ret.get("n_windows") or 0)],
        ]))
        _ws = ret.get("windows") or []
        if _ws:
            rows = []
            for w in _ws:
                rows.append([w.get("start_utc", ""), w.get("end_utc", ""),
                             _fmt(w.get("duration_s"), 0),
                             w.get("best_landing_utc", ""),
                             "%s, %s" % (_fmt(w.get("landing_lat_deg"), 3),
                                         _fmt(w.get("landing_lon_deg"), 3)),
                             w.get("zone_id") or "—",
                             _fmt(w.get("dist_to_zone_km"), 1),
                             _fmt(w.get("deorbit_dv_mps"), 0),
                             _fmt(w.get("bank_deg"), 1)])
            L.append(_table(["制动窗起点 (UTC)", "制动窗终点 (UTC)", "窗宽 (s)",
                             "落地 (UTC)", "落点 (°N, °E)", "航警区",
                             "距边界 (km)", "ΔV (m/s)", "倾侧角 (°)"], rows))
            L.append("")
        _sel = ret.get("selected") or {}
        if _sel:
            L.append(_table(["选中机会", "数值"], [
                ["制动时刻", "%s UTC（北京时 %s）" % (
                    _sel.get("t_deorbit_utc", "—"), _sel.get("t_deorbit_bjt", "—"))],
                ["落地时刻", "%s UTC（北京时 %s）" % (
                    _sel.get("t_landing_utc", "—"), _sel.get("t_landing_bjt", "—"))],
                ["落点", "%s°N, %s°E" % (_fmt(_sel.get("lat_deg"), 4),
                                         _fmt(_sel.get("lon_deg"), 4))],
                ["飞行时间", "%s s（约 %s min）" % (
                    _fmt(_sel.get("flight_time_s"), 0),
                    _fmt(float(_sel.get("flight_time_s") or 0) / 60.0, 1))],
                ["制动 ΔV / 倾侧角", "%s m/s / %s°" % (
                    _fmt(_sel.get("deorbit_dv_mps"), 0), _fmt(_sel.get("bank_deg"), 1))],
                ["航警匹配", (("区内" if _sel.get("inside_zone") else "区外")
                             + ("，时间在 B)/C) 内" if _sel.get("notam_time_ok") else ""))
                 if _zones else ("在着陆场半径内" if _sel.get("inside_zone")
                                 else "在着陆场半径外（最近机会）")],
                ["距着陆区边界" if _zones else "距着陆场中心",
                 "%s km" % _fmt(_sel.get("dist_to_zone_km"), 1)],
                ["峰值动压 / 过载 / 热流", "%s kPa / %s g / %s W·cm⁻²" % (
                    _fmt((_sel.get("peaks") or {}).get("q_pa", 0) / 1000.0, 1),
                    _fmt((_sel.get("peaks") or {}).get("g"), 1),
                    _fmt((_sel.get("peaks") or {}).get("heat_w_cm2"), 0))],
            ]))
            L.append("")
        _disp = ret.get("dispersion") or {}
        if _disp:
            L.append(_table(["落点散布（1σ）", "数值"], [
                ["中心", "%s°N, %s°E" % (_fmt(_disp.get("center_lat"), 3),
                                         _fmt(_disp.get("center_lon"), 3))],
                ["长半轴 / 短半轴", "%s km / %s km" % (
                    _fmt(_disp.get("semi_major_km"), 1),
                    _fmt(_disp.get("semi_minor_km"), 1))],
                ["方向", "%s°" % _fmt(_disp.get("orientation_deg"), 0)],
                ["样本数", str(_disp.get("n", "—"))],
            ]))
            L.append("> 落点散布来自再入参数小扰动（ΔV、倾侧角、质量）的 1σ 椭圆，"
                     "用于估计\"大致范围\"，不是官方残骸区。")
            L.append("")
        _tl = ret.get("timeline") or []
        if _tl:
            rows = []
            for e in _tl:
                rows.append([e.get("event", ""), e.get("t_utc", ""),
                             _fmt(e.get("alt_km"), 1), _fmt(e.get("speed_m_s"), 0)])
            L.append(_table(["事件", "UTC", "高度 (km)", "速度 (m/s)"], rows))
            L.append("")
        _ph = ret.get("phases") or []
        if _ph:
            L.append("**返回阶段**")
            L.append("")
            L.append(_table(["阶段", "开始 (UTC)", "结束 (UTC)", "时长 (s)", "说明"],
                            [[p.get("phase", ""), p.get("start_utc", ""),
                              p.get("end_utc", ""), _fmt(p.get("duration_s"), 0),
                              p.get("note", "")] for p in _ph]))
            L.append("")
        _sp = ret.get("separations") or []
        if _sp:
            L.append("**分离点**")
            L.append("")
            L.append(_table(["分离事件", "UTC", "纬度", "经度", "高度 (km)", "速度 (m/s)"],
                            [[e.get("event", ""), e.get("t_utc", ""),
                              _fmt(e.get("lat_deg"), 3), _fmt(e.get("lon_deg"), 3),
                              _fmt(e.get("alt_km"), 1), _fmt(e.get("speed_m_s"), 0)]
                             for e in _sp]))
            L.append("> 船站/轨道舱分离早于制动，位置按空间站轨道；推进舱分离按滑行段插值；"
                     "分离时刻为工程阶段标记，不是分离动力学。")
            L.append("")
        L.append("> 模型：3-DOF 返回舱（USSA 大气、阻力、L/D=%.2f + 倾侧角、"
                 "减速伞/主伞），非实际飞行程序；落点精度约几十 km。"
                 % float((ret.get("model") or {}).get("ld_ratio")
                         or _sel.get("ld_ratio") or 0.30))
        if ret.get("kmz"):
            L.append("> Google Earth：`%s`（3D 轨迹/事件/着陆场/航警区）。"
                     % ret.get("kmz"))
        if ret.get("landing_map"):
            L.append("> 落区图：`%s`。" % ret.get("landing_map"))
        L.append("")

    # ---------------- 6. 天空可见性 ----------------
    L.append("## 6. 指定观测点的天空可见性")
    L.append("")
    obs_map = (sky or {}).get("observers") or {}
    sky_dem = (sky or {}).get("dem") or {}
    if obs_map:
        for oid, o in obs_map.items():
            name = o.get("name") or oid
            L.append("### 观测点 %s（%s）" % (oid, name))
            L.append("")
            L.append(_table(["项目", "数值"], [
                ["坐标（十进制度）", "%.5f, %.5f" % (float(o.get("lat_deg") or 0),
                                                   float(o.get("lon_deg") or 0))],
                ["坐标（度分秒）", o.get("dms") or "—"],
                ["海拔", "%s m（来源：%s；配置估计 %s m）" % (
                    _fmt(o.get("alt_m"), 1), o.get("alt_source") or "—",
                    _fmt(o.get("config_alt_m"), 1))],
                ["地形遮挡角", "%s°（近场 %s°）" % (_fmt(o.get("horizon_deg"), 2),
                                               _fmt(o.get("horizon_nearfield"), 2))],
                ["可见总时长", "%s s（无地形时 %s s）" % (
                    _fmt(o.get("visible_duration_s"), 1),
                    _fmt(o.get("visible_duration_no_terrain_s"), 1))],
                ["阳光可见时长", "%s s" % _fmt(o.get("sun_visible_duration_s"), 1)],
            ]))
            fa = o.get("first_appearance") or {}
            mx = o.get("max_elevation") or {}
            lv = o.get("last_visible") or {}

            def _row(label, d):
                return [label, _bj(d.get("t_utc", "")), _fmt(d.get("az_deg"), 1, "°"),
                        _fmt(d.get("el_deg"), 2, "°"), _fmt(d.get("slant_range_km"), 0, " km"),
                        _fmt(d.get("ground_range_km"), 0, " km")]

            L.append(_table(["可见性事件", "时刻（北京时）", "方位角", "高度角", "斜距", "地面距离"], [
                _row("首次出现", fa), _row("最大高度角", mx), _row("消失", lv)]))
            dem_mode = o.get("dem_mode") or v3.get("dem_mode") or sky_dem.get("mode") or ""
            if dem_mode and dem_mode not in ("srtm", "custom", "1"):
                L.append("> 注意：本观测点地形遮挡使用「%s」计算（非 SRTM 1″/自定义精采样），"
                         "首次出现时刻存在数十秒量级误差，不能用于正式结论。" % dem_mode)
                L.append("")
            elif dem_mode == "custom":
                L.append("> 地形遮挡使用用户导入的自定义高程文件（离线 GeoTIFF/HGT），"
                         "非 SRTM 全球瓦片；垂直基准与 SRTM 可能有数米级系统差。")
                L.append("")
            solar = o.get("solar") or {}
            if solar:
                el0 = solar.get("observer_sun_el_t0_deg")
                state = solar.get("twilight_state_t0") or ""
                if el0 is not None:
                    L.append("T0 时刻太阳高度角 %s°（%s）；火箭处于日照比例 %s。" % (
                        _fmt(el0, 2), state,
                        _fmt(solar.get("rocket_sunlit_fraction"), 2)))
                    L.append("")
            _outs = o.get("outputs") or {}
            _files = []
            for _k in ("png", "panorama_png", "sun_shadow_png"):
                _o2 = _outs.get(_k) or {}
                if isinstance(_o2, dict) and _o2.get("path"):
                    _files.append(str(_o2["path"]).replace("\\", "/").split("/")[-1])
            if _outs.get("md"):
                _files.append(str(_outs["md"]))
            if _files:
                L.append("图片/数据：`sky/` 目录下的 " + "、".join("`%s`" % f for f in _files) + "。")
            else:
                L.append("图片：`sky/` 目录下的 `天空轨迹_%s.png` 等。" % oid)
            L.append("")
    else:
        L.append("未指定观测点或未计算天空轨迹。")
        L.append("")

    # ---------------- 6. 不确定度 ----------------
    L.append("## 7. 不确定度（蒙特卡洛）")
    L.append("")
    if mc:
        L.append("样本数 N=%s，随机变量 %s 个。" % (mc.get("n", "—"),
                                                len((cfg.get("mc") or {}).get("variations") or [])))
        L.append("")
        stats = mc.get("stats") or mc.get("summary") or {}
        if isinstance(stats, dict) and stats:
            rows = [[k, json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else _fmt(v, 3)]
                    for k, v in list(stats.items())[:20]]
            L.append(_table(["统计量", "数值"], rows))
    else:
        L.append("未运行蒙特卡洛（可在「运行选项」里设置样本数后重算）。")
    L.append("")

    # ---------------- 7. 自检 ----------------
    L.append("## 8. 交付自检结论")
    L.append("")
    checks = (qc or {}).get("checks") or (qc or {}).get("results") or []
    if checks:
        fails = [c for c in checks if not c.get("ok")]
        L.append("共 %d 项检查，通过 %d 项，未通过 %d 项。" % (len(checks),
                                                      len(checks) - len(fails), len(fails)))
        L.append("")
        if fails:
            rows = [[c.get("check", ""), c.get("detail", "")] for c in fails]
            L.append(_table(["未通过项", "详情"], rows))
        else:
            L.append("全部通过。")
        L.append("")
    else:
        L.append("_（未运行交付自检）_")
        L.append("")

    # ---------------- 8. 假设与局限 ----------------
    L.append("## 9. 假设、局限与免责声明")
    L.append("")
    assumptions = []
    if (v3.get("notams_mode") == "predict"):
        assumptions.append("落区为**预测生成**，不是官方航警；不能作为飞行安全依据。")
    if not (mission.get("reference_orbit") or {}):
        assumptions.append("无参考轨道，未对入轨高度/倾角做软约束。")
    if str(mission.get("rocket_confidence", "")).startswith("低"):
        assumptions.append("箭型参数置信度低（部分为同类火箭估值），入轨结果只具备量级意义。")
    if str(mission.get("site_confidence", "")).startswith("低"):
        assumptions.append("发射场坐标/高程置信度低。")
    if v3.get("dem_mode") and v3.get("dem_mode") not in ("srtm", "custom"):
        assumptions.append("地形遮挡使用内嵌粗 DEM（约 2 km），首次出现时刻精度受限。")
    elif v3.get("dem_mode") == "custom":
        assumptions.append("地形遮挡使用用户离线导入的自定义高程文件（非 SRTM 全球瓦片），"
                           "不同高程源的垂直基准可能相差数米。")
    assumptions.append("3-DOF 模型：末级平面修正按简化几何 dogleg（目标倾角 + 偏航时间常数）"
                       "处理，**不计 Δv 与推力损失**；不含完整六自由度侧向制导、风场、真实发动机"
                       "调节与分离扰动。残骸落点按无控弹道 + 阻力估算。")
    assumptions.append("观测点坐标若来自用户输入，可能与其原意存在偏差（常见笔误）。")
    for a in assumptions:
        L.append("- " + a)
    L.append("")
    warnings = v3.get("warnings") or []
    if warnings:
        L.append("### 9.1 运行期提示")
        L.append("")
        for w in warnings:
            L.append("- " + str(w))
        L.append("")

    # ---------------- 9. 结论与建议 ----------------
    L.append("## 10. 结论与使用建议")
    L.append("")
    cons: List[str] = []
    if impacts:
        n_in = sum(1 for r in impacts if r.get("inside_zone") == "true")
        cons.append("**落区**：%d/%d 个残骸落点在对应危险区内%s。" % (
            n_in, len(impacts),
            "（含边界裕度判据）" if n_in == len(impacts) else
            "；未入区项需检查航警对应关系与反演预算"))
    if orbit:
        cons.append("**入轨**：半长轴 %s km、偏心率 %s、倾角 %s°，近地点高度 %s km。" % (
            _fmt(orbit.get("a_km"), 1), _fmt(orbit.get("e"), 4),
            _fmt(orbit.get("i_deg"), 2), _fmt(orbit.get("perigee_km"), 1)))
        ref = mission.get("reference_orbit") or {}
        if ref.get("alt_km"):
            dh = float(orbit.get("a_km") or 0) - (6378.137 + float(ref["alt_km"]))
            di = float(orbit.get("i_deg") or 0) - float(ref.get("i_deg") or 0)
            cons.append("  与参考轨道（h=%s km, i=%s°）相比：半长轴高 %s km、倾角差 %s°。"
                        % (_fmt(ref["alt_km"], 0), _fmt(ref.get("i_deg")),
                           _fmt(dh, 1), _fmt(di, 2)))
    if (window or {}).get("practical_window"):
        pw = window["practical_window"]
        cons.append("**发射窗口（航警）**：%s ~ %s UTC（%.1f min），北京时 %s ~ %s。" % (
            pw.get("start_utc"), pw.get("end_utc"), float(pw.get("duration_min") or 0),
            pw.get("start_bjt"), pw.get("end_bjt")))
    ow0 = (window or {}).get("orbit_window") or {}
    if ow0.get("available"):
        n_w = ow0.get("n_windows") or 0
        bw = ow0.get("best_near_t0") or {}
        if bw:
            cons.append("**轨道平面窗口**：搜索范围内 %d 个；距标称 T0 最近为 %s UTC"
                        "（%+.0f s，ΔRAAN %s°）。" % (
                            n_w, bw.get("best_utc", "—"),
                            (datetime.fromisoformat(bw["best_utc"].replace("Z", "+00:00"))
                             - datetime.fromisoformat(str(mission.get("t0_utc")).replace("Z", "+00:00"))
                             ).total_seconds(),
                            _fmt(bw.get("d_raan_deg"), 3)))
    if docking.get("enabled") and docking.get("docking"):
        d0 = docking.get("docking") or {}
        cons.append("**CZ-2F 交会对接**：%s；预计对接 %s UTC，总 ΔV %s m/s，"
                    "入轨 %s km。%s" % (
                        "可行" if docking.get("feasible") else "不可行",
                        d0.get("t_utc", "—"), _fmt(docking.get("total_dv_mps"), 0),
                        _fmt((docking.get("insertion") or {}).get("alt_km"), 1),
                        str(docking.get("t0_reason") or "")))
    if ret.get("enabled") and ret.get("selected"):
        rs = ret.get("selected") or {}
        cons.append("**返回舱返回**：%d 个可行窗口；选中制动 %s UTC → 落地 %s UTC"
                    "（%s°N, %s°E，%s），飞行 %s min。" % (
                        int(ret.get("n_windows") or 0), rs.get("t_deorbit_utc", "—"),
                        rs.get("t_landing_utc", "—"),
                        _fmt(rs.get("lat_deg"), 2), _fmt(rs.get("lon_deg"), 2),
                        "区内" if rs.get("inside_zone") else "区外/最近",
                        _fmt(float(rs.get("flight_time_s") or 0) / 60.0, 1)))
    if obs_map:
        for oid, o in list(obs_map.items())[:2]:
            fa = o.get("first_appearance") or {}
            mx = o.get("max_elevation") or {}
            if fa:
                cons.append("**观测点 %s**：首次出现在 %s（方位 %s°、仰角 %s°），"
                            "最大高度角 %s°。" % (
                                oid, _bj(fa.get("t_utc")), _fmt(fa.get("az_deg"), 1),
                                _fmt(fa.get("el_deg"), 2), _fmt(mx.get("el_deg"), 1)))
    if not cons:
        cons.append("本任务仅完成了部分步骤，结论请结合各章数据自行判断。")
    for c0 in cons:
        L.append("- " + c0)
    L.append("")
    L.append("**使用建议**：")
    L.append("")
    tips = []
    if impacts and any(r.get("inside_zone") != "true" for r in impacts):
        tips.append("若落点未入区：提高反演预算（「运行选项」→ 精细）、核对航警残骸类型"
                    "（一级/二级/整流罩/助推器）与多边形顺序 `[lat, lon]`。")
    if (v3.get("dem_mode") or "") not in ("srtm", "custom", "1", ""):
        tips.append("若需要正式的天空可见性结论：请补齐 SRTM 1″ 瓦片，或导入已有高精度高程文件"
                    "（「运行选项」→ 离线高程导入）后重算。")
    if (mission.get("_provenance") or []):
        n_low = len([p for p in mission["_provenance"] if p.get("confidence") == "低"])
        if n_low:
            tips.append("本任务有 %d 个低置信度字段（见 1.1 节），引用结果时请注明其不确定性。" % n_low)
    tips.append("正式使用前建议用两个以上独立来源核对 T0、发射场坐标与航警原文。")
    for t in tips:
        L.append("- " + t)
    L.append("")

    # ---------------- 10. 数据来源 ----------------
    L.append("## 11. 数据来源与参考链接")
    L.append("")
    src_rows = [
        ["地形高程 SRTM 1″（SRTMGL1）",
         "https://s3.amazonaws.com/elevation-tiles-prod/skadi/",
         "AWS Open Data（公有领域）"],
        ["内嵌粗地形（0.02°）",
         "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/",
         "Mapzen/AWS Terrain Tiles（公有领域）"],
        ["国界/省界/城市底图",
         "https://www.naturalearthdata.com/",
         "Natural Earth（公有领域，1:10m/1:50m）"],
        ["箭型/场站/载荷参数（内置库）",
         "app/data/*.json",
         "公开资料汇编（每条目含 sources 与 confidence，见 config.mission._provenance）"],
        ["航警（NOTAM）",
         "用户提供原文",
         "用户粘贴；本工具不联网检索航警"],
        ["空间站轨道（OEM 精密星历）",
         "https://www.cmse.gov.cn/gfgg/zgkjzgdcs/",
         "中国载人航天工程办公室（CMSE）公开发布；内置快照 + 可在线/手动导入"],
        ["空间站轨道（TLE 回退）",
         "https://celestrak.org/NORAD/elements/",
         "CelesTrak 公开 TLE；OEM 不可用时使用，沿迹相位误差较大"],
        ["大气模型/引力模型",
         "US Standard Atmosphere + EGM 低阶带谐项",
         "按 v2.0 引擎实现（见 engine/scripts/dynamics.py）"],
    ]
    # 自定义高程源（离线导入）
    try:
        from . import paths as _P
        from .dem_manager import DemManager as _DM
        _custom = _DM(str(_P.dem_dir())).custom_sources(valid_only=True)
        if _custom:
            src_rows.append([
                "自定义高程（离线导入）",
                "用户提供的 GeoTIFF / HGT",
                "；".join("%s（%s，分辨率 %s°）" % (
                    s.get("name") or s.get("id"), s.get("file", ""),
                    _fmt(s.get("res_deg"), 6)) for s in _custom[:3])
                + ("等 %d 个源" % len(_custom) if len(_custom) > 3 else ""),
            ])
    except Exception:  # noqa: BLE001
        pass
    L.append(_table(["数据", "来源", "说明"], src_rows))
    mission_sources = []
    for r in (mission.get("_provenance") or []):
        note = str(r.get("note") or "")
        if "http" in note:
            mission_sources.append([r.get("label", ""), note[:200]])
    if mission_sources:
        L.append("本任务字段级来源摘录：")
        L.append("")
        L.append(_table(["字段", "来源说明"], mission_sources))
    L.append("本项目全部算法与数据仅用于公开信息分析与教学，**不构成任何官方或工程依据**。")
    L.append("")

    # ---------------- 11. 误差与注意事项 ----------------
    L.append("## 12. 误差分析与使用注意")
    L.append("")
    L.append("| 环节 | 主要误差源 | 量级估计 |")
    L.append("|---|---|---|")
    L.append("| 落点 | 箭型质量/比冲估值、制导反演收敛、3-DOF 简化 | 数 km ~ 数十 km |")
    L.append("| 入轨根数 | 末级关机方式（程序关机 vs 耗尽）、阻力系数 | 半长轴数十 km 量级 |")
    L.append("| 首次出现时刻 | 地形遮挡（DEM 分辨率）、观测点坐标/高程、人眼高度假设 | SRTM 下数秒；粗 DEM 下数十秒 |")
    L.append("| 发射窗口 | 航警原文时间、准备裕度假设 | 分钟量级 |")
    L.append("| 太阳/地影 | 未计大气折射与局部遮挡 | 0.1°~1° 量级 |")
    L.append("")
    L.append("注意：")
    L.append("")
    L.append("- 本工具**不做**航警原文的联网核对，航警数据以用户粘贴内容为准；")
    L.append("- 「数据体检」里来源为 **自动推断 / 默认值** 或置信度 **低** 的字段，其结果只能作量级参考；")
    L.append("- 蒙特卡洛给出的是**参数不确定度传播**，不含模型系统性偏差。")
    L.append("")

    # ---------------- 12. 附件 ----------------
    L.append("## 13. 附件清单")
    L.append("")
    rows = []
    for rel, desc in [
        ("sim/trajectory.csv", "弹道时间序列（0.5 s 采样）"),
        ("sim/弹道事件表.csv", "关键飞行事件"),
        ("sim/落点与射程.csv", "残骸落点与危险区判定"),
        ("sim/入轨根数.json", "入轨根数"),
        ("sim/orbit_track.csv", "入轨后 1.5 圈星下点"),
        ("sim/空间站轨道.json", "空间站轨道数据与过境（CZ-2F）"),
        ("sim/空间站星下点.csv", "空间站星下点序列（CZ-2F）"),
        ("sim/交会对接.json", "交会对接方案与时间线（CZ-2F）"),
        ("sim/交会对接_时序.csv", "入轨→相位→转移→逼近→对接时序（CZ-2F）"),
        ("sim/CZ2F_任务前后30min_空间站.csv", "CZ-2F 任务前后各 30 min 空间站轨道"),
        ("sim/返回舱返回.json", "返回舱返回窗口、落点与散布（v3.5）"),
        ("sim/返回舱_时序.csv", "返回舱制动→再入→开伞→着陆时序（v3.5）"),
        ("sim/返回舱弹道.png", "返回舱星下点轨迹与高度-时间（v3.5）"),
        ("sky/", "天空轨迹图与数据"),
        ("map/全国最大高度角地图.png", "全国最大高度角地图"),
        ("viz/弹道三维_含残骸坠落.kmz", "Google Earth 三维弹道"),
        ("uncertainty/", "蒙特卡洛样本与统计"),
        ("report/qc_report.json", "交付自检明细"),
        ("运行日志.txt", "完整运行日志"),
    ]:
        p = os.path.join(workdir, rel.replace("/", os.sep))
        if os.path.exists(p):
            rows.append([rel, desc])
    L.append(_table(["文件", "说明"], rows))

    text = "\n".join(L) + "\n"
    md_path = os.path.join(out_dir, "弹道仿真报告.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(text)

    # 可选：docx（有 python-docx 才做，没装不影响）
    try:
        _write_docx(out_dir, mission, text)
    except Exception:  # noqa: BLE001
        pass
    return md_path


def _app_version() -> str:
    try:
        from . import paths as P
        return P.app_version()
    except Exception:  # noqa: BLE001
        return "3.0"


def _write_docx(out_dir: str, mission: Dict[str, Any], md_text: str) -> str:
    """把 Markdown 粗略转成 docx（标题/表格/段落），失败则抛出异常由上层忽略。"""
    from docx import Document  # type: ignore
    doc = Document()
    doc.add_heading(mission.get("name") or "弹道仿真报告", level=0)
    for line in md_text.splitlines():
        s = line.rstrip()
        if not s:
            continue
        if s.startswith("#"):
            level = min(4, len(s) - len(s.lstrip("#")))
            doc.add_heading(s.lstrip("#").strip(), level=level)
        elif s.startswith("|"):
            continue          # 表格留给 Markdown 版；docx 里省略避免错版
        elif s.startswith(">"):
            doc.add_paragraph(s.lstrip("> ").strip())
        else:
            doc.add_paragraph(s.replace("**", "").replace("`", ""))
    path = os.path.join(out_dir, "弹道仿真报告.docx")
    doc.save(path)
    return path
