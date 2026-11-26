# -*- coding: utf-8 -*-
"""qc_report.json → 人话结论 + 可执行建议。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

#: 常见 FAIL 项的修复建议（不区分大小写匹配）
ADVICE = [
    ("报告未撰写", "已由 v3 自动生成 report/弹道仿真报告.md；若仍报此项，请检查 report 步骤是否启用"),
    ("PNG", "图片缺失或宽度不足：请确认天空/地图/KML 步骤是否跑完（日志里看该步骤是否失败）"),
    ("KML", "KMZ/KML 结构异常：检查 viz/ 目录与 engine/viz/files 图标是否缺失"),
    ("落区", "落点未入区：提高反演预算（maxiter/popsize）、核对航警 stage_hint 与多边形顺序 [lat,lon]"),
    ("窗口", "发射窗口为空：航警 B)/C) 未覆盖残骸到达时刻，或 T0 偏差过大"),
    ("入轨", "入轨根数异常：检查箭型推进剂/推力/末级关机设置，或参考轨道是否与箭型匹配"),
    ("天空", "天空轨迹缺失：确认观测点存在且 DEM 可用（无瓦片时用粗 DEM 回退）"),
    ("DEM", "DEM 缺失：可在「环境」页在线补全 SRTM 瓦片（需联网），或用现有瓦片目录导入"),
    ("NaN", "存在 NaN：多为仿真发散，检查 dt 与箭型参数"),
]


def summarize_qc(workdir: str) -> Dict[str, Any]:
    path = os.path.join(workdir, "report", "qc_report.json")
    out: Dict[str, Any] = {"ok": False, "n_pass": 0, "n_fail": 0, "items": [], "advice": []}
    if not os.path.exists(path):
        out["advice"].append("未找到 report/qc_report.json：交付自检未运行")
        return out
    try:
        data = json.load(open(path, encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        out["advice"].append("读取 qc_report.json 失败：%s" % exc)
        return out
    items = data.get("checks") or data.get("results") or data.get("items") or []
    fails = [it for it in items if not it.get("ok")]
    out.update(ok=len(fails) == 0, n_pass=len(items) - len(fails), n_fail=len(fails),
               items=items)
    for it in fails:
        name = str(it.get("check") or "")
        advice = None
        for k, adv in ADVICE:
            if k.lower() in name.lower():
                advice = adv
                break
        out["advice"].append("%s%s" % (name, ("  → " + advice) if advice else ""))
    return out
