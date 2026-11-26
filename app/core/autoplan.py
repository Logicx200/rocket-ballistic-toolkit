# -*- coding: utf-8 -*-
"""自动规划：把 v2.0 里"AI 手工给的"反演边界、蒙特卡洛变量、发射窗口裕度自动算出来。

v2.0 的 ``inversion.parameters`` 每条边界都靠人（或 AI）按任务填，边界给宽了收敛慢、
给窄了卡边界。这里按**物理量级 + 箭型数据 + 落区几何**推导边界：

* 方位角：由发射场→一级落区质心反算值 ±8°（无落区时由目标倾角反算值 ±20°）
* t_v（推力转向时刻）：箭型值 ±3 s
* t_p（俯仰程序时长）：箭型值 ×0.75…×1.30，并限制在一级工作段 0.55…1.55 倍内
* pitch_power：箭型值 ±0.30
* 各级 pitch_end_deg：箭型值 ±6°（内部数据置信度低时 ±15°）
* t_fairing：仅在未知时才进反演（已知抛罩时刻就固定）
* 末级 seco_met：已知就固定（unknown=按推进剂耗尽，不反演）

预算：DE 的 maxiter/popsize 按参数个数与可选"快速模式"自动取。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from . import infer as I

MAX_PARAMS = 6
#: 落区罚项权重要足够大，否则"轨道倾角软约束"会把解拉到危险区边界上
WEIGHTS = {"zone_km": 20.0, "orbit_alt_km": 1.0, "orbit_i_deg": 20.0, "orbit_e": 100.0}


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _plan_schedule_params(config: Dict[str, Any], sch: List[Any], burn0: float,
                          trusted: bool, notes: List[str]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """折线俯仰剖面的反演参数：方位角 + 关键节点角度（±4°，低置信时 ±6°）。

    节点挑选：一级关机时刻前后各 1 个（控制 MECO 状态/落区），以及时间最靠后的 3 个
    （控制末级入轨剖面）。上限 MAX_PARAMS，避免 DE 维度失控。
    """
    g = config.get("guidance") or {}
    mission = config.get("mission") or {}
    pts = [[float(a), float(b)] for a, b in sch]
    half_end = 4.0 if trusted else 6.0
    params: List[Dict[str, Any]] = []
    seed: Dict[str, Any] = {}
    az0 = _f(g.get("azimuth_deg"))
    if az0 is not None and str((config.get("mission") or {}).get("_azimuth_from")
                               or "") != "user":
        half = 5.0 if mission.get("_azimuth_from") == "zone" else 8.0
        params.append({"name": "azimuth_deg", "path": "guidance.azimuth_deg",
                       "bounds": [round(az0 - half, 3), round(az0 + half, 3)]})
        seed["azimuth_deg"] = az0
        notes.append("折线剖面：方位角初值 %.2f°，反演范围 ±%.0f°" % (az0, half))
    # 一级关机时刻前后的节点
    idx_burn = min(range(len(pts)), key=lambda i: abs(pts[i][0] - burn0))
    # 最靠后的 3 个节点
    idx_tail = list(range(max(len(pts) - 3, 0), len(pts)))
    pick = []
    for i in ([idx_burn - 1] if idx_burn >= 1 else []) + [idx_burn] + idx_tail:
        if i not in pick and 0 <= i < len(pts):
            pick.append(i)
    for i in pick:
        if len(params) >= MAX_PARAMS:
            break
        ang = pts[i][1]
        params.append({"name": "pitch_t%d" % int(pts[i][0]),
                       "path": "guidance.pitch_schedule.%d.1" % i,
                       "bounds": [round(ang - half_end, 3), round(ang + half_end, 3)]})
        seed["pitch_t%d" % int(pts[i][0])] = ang
    notes.append("折线剖面节点反演：%s（±%.0f°）" % (
        "、".join("t=%.0fs" % pts[i][0] for i in pick), half_end))
    for spec in params:
        _sd = seed.get(spec["name"])
        if _sd is None:
            continue
        b = spec["bounds"]
        b[0] = min(b[0], _sd)
        b[1] = max(b[1], _sd)
    return params, seed


def has_anything_to_fit(config: Dict[str, Any]) -> bool:
    """有落区约束或参考轨道，才值得反演。"""
    zones = [z for z in (config.get("notams") or []) if z.get("polygon_latlon")]
    ref = (config.get("mission") or {}).get("reference_orbit") or {}
    return bool(zones) or bool(ref.get("alt_km") or ref.get("i_deg"))


def plan_inversion(config: Dict[str, Any], quick: bool = False,
                   maxiter: int = 0, popsize: int = 0
                   ) -> Tuple[Dict[str, Any], List[str]]:
    """生成 ``config["inversion"]``。返回 (inversion_dict, notes)。"""
    notes: List[str] = []
    mission = config.get("mission") or {}
    veh = config.get("vehicle") or {}
    g = config.get("guidance") or {}
    rocket_conf = mission.get("rocket_confidence", "中")
    stages = veh.get("stages") or []
    burns = I.estimate_burn_s(veh)
    # v4：用户手工固定的制导参数不再进反演（界面/帮助里承诺"填了=固定"）
    _locked = {str(k) for k in ((config.get("_v3") or {}).get("guidance_locked") or [])}
    _path_locked = {"guidance." + k for k in _locked}
    # 芯一级工作段（有并联助推器时**不能**取助推器工作段：助推器只有 ~29–115 s，
    # 会把 t_p 的搜索区间整体锁到 100~120 s，而 CZ-6A 的标称 t_p≈133.7 s 直接被排除，
    # 反演因此不可能收敛到标称轨迹附近。t_p/pitch_end 描述的是芯级俯仰程序。）
    burn0 = max(10.0, burns.get("stage_0") or burns.get("boosters") or 120.0)
    az0 = _f(g.get("azimuth_deg"))
    params: List[Dict[str, Any]] = []
    seed: Dict[str, Any] = {}
    # DB 里该箭型的制导参数是否为"已率定的可信值"（高置信）→ 用较紧的锚定边界；
    # 但若参考轨道本身是低置信推断值（由方位角反算），说明本任务的弹道剖面可能与库内
    # 锚点差别较大，需要放宽搜索范围，否则落区约束根本够不到。
    trusted = str(rocket_conf).startswith("高") or str(rocket_conf).startswith("中高")
    _ref0 = (config.get("mission") or {}).get("reference_orbit") or {}
    if str(_ref0.get("confidence", "")).startswith("低"):
        trusted = False
        notes.append("参考轨道为低置信推断值 → 放宽反演边界（弹道剖面可能偏离库内锚点）")

    # 0) v3.3：若本箭型用"折线俯仰剖面"（guidance.pitch_schedule），
    #    t_v/t_p/pitch_power/各级 pitch_end 对轨迹完全不起作用，反演必须改为
    #    **方位角 + 关键节点角度**，否则 DE 会在"无效参数"上空转（实测 CZ-6C 就是这种情形）。
    _sch = g.get("pitch_schedule")
    if isinstance(_sch, list) and len(_sch) >= 3:
        params, seed = _plan_schedule_params(config, _sch, burn0, trusted, notes)
        if _locked:
            params = [p for p in params if p.get("path") not in _path_locked]
            notes.append("折线剖面：用户固定的参数不进反演（%s）" % "、".join(sorted(_locked)))
        n_par = len(params)
        mi_s = 6 if quick else 10
        ps_s = 4 if quick else 6
        if maxiter > 0:
            mi_s = int(maxiter)
        if popsize > 0:
            ps_s = int(popsize)
        inv = {
            "parameters": params,
            "weights": dict(WEIGHTS),
            "de": {"maxiter": mi_s, "popsize": ps_s, "seed": 42, "dt": 0.5},
            "_plan": {"n_params": n_par, "est_simulations": mi_s * ps_s,
                      "seed": seed, "trusted_anchor": trusted, "profile": "pitch_schedule",
                      "note": "由 app/core/autoplan.py 按折线俯仰剖面自动生成（节点角度微调）"},
        }
        _ref0 = (config.get("mission") or {}).get("reference_orbit") or {}
        if str(_ref0.get("confidence", "")).startswith("低") and \
                len([z for z in (config.get("notams") or []) if z.get("polygon_latlon")]) > 0:
            inv["weights"]["orbit_alt_km"] = 0.3
            inv["weights"]["orbit_i_deg"] = 2.0
            notes.append("参考轨道置信度为「低」：轨道罚项减权（高度×0.3、倾角×2），启用圆度正则")
        notes.append("差分进化预算：maxiter=%d × popsize=%d（折线剖面口径）"
                     % (inv["de"]["maxiter"], inv["de"]["popsize"]))
        return inv, notes

    # 1) 方位角（用户显式固定时不再进反演）
    _az_locked = str(mission.get("_azimuth_from") or "") == "user"
    if az0 is not None and not _az_locked:
        from_zone = mission.get("_azimuth_from") == "zone"
        if from_zone:
            half = 5.0 if trusted else 8.0
        else:
            half = 10.0 if trusted else 20.0
        params.append({"name": "azimuth_deg", "path": "guidance.azimuth_deg",
                       "bounds": [round(az0 - half, 3), round(az0 + half, 3)]})
        seed["azimuth_deg"] = az0
        notes.append("方位角初值 %.2f°，反演范围 ±%.0f°" % (az0, half))

    # 2) t_v
    tv0 = _f(g.get("t_v"), 8.0)
    half_tv = 2.0 if trusted else 3.0
    if "guidance.t_v" not in _path_locked:
        params.append({"name": "t_v", "path": "guidance.t_v",
                       "bounds": [round(max(2.0, tv0 - half_tv), 2),
                                  round(tv0 + half_tv, 2)]})
        seed["t_v"] = tv0
    else:
        notes.append("t_v 由用户固定（%.2f s），不进反演" % tv0)

    # 3) t_p
    tp0 = _f(g.get("t_p"), 160.0)
    fr_lo, fr_hi = (0.88, 1.12) if trusted else (0.75, 1.30)
    lo = max(0.55 * burn0, tp0 * fr_lo)
    hi = min(1.75 * burn0, tp0 * fr_hi)
    if hi <= lo:
        hi = lo + 20.0
    if "guidance.t_p" not in _path_locked:
        params.append({"name": "t_p", "path": "guidance.t_p",
                       "bounds": [round(lo, 1), round(hi, 1)]})
        seed["t_p"] = tp0
        notes.append("俯仰时长 t_p %.1f s（一级工作段约 %.0f s）→ 范围 %.0f…%.0f s" %
                     (tp0, burn0, lo, hi))
    else:
        notes.append("t_p 由用户固定（%.1f s），不进反演" % tp0)

    # 4) pitch_power
    pp0 = _f(g.get("pitch_power"), 1.05)
    half_pp = 0.15 if trusted else 0.30
    if "guidance.pitch_power" not in _path_locked:
        params.append({"name": "pitch_power", "path": "guidance.pitch_power",
                       "bounds": [round(max(0.40, pp0 - half_pp), 3),
                                  round(pp0 + half_pp, 3)]})
        seed["pitch_power"] = pp0
    else:
        notes.append("pitch_power 由用户固定（%.3f），不进反演" % pp0)

    # 5) 各级 pitch_end_deg（一二级最关键）
    half_end = 4.0 if trusted else 15.0
    for si, st in enumerate(stages[:2]):
        pe = _f(st.get("pitch_end_deg"))
        if pe is None:
            continue
        params.append({"name": "pitch_end_%d" % si,
                       "path": "vehicle.stages.%d.pitch_end_deg" % si,
                       "bounds": [round(pe - half_end, 3), round(pe + half_end, 3)]})
        seed["pitch_end_%d" % si] = pe
        notes.append("%s pitch_end=%.2f° → ±%.0f°" % (st.get("name", "级%d" % si), pe, half_end))

    # 6) 抛罩时刻（仅未知时）
    fair = veh.get("fairing") or {}
    if fair and _f(g.get("t_fairing")) is None and _f(fair.get("sep_met")) is None:
        params.append({"name": "t_fairing", "path": "guidance.t_fairing",
                       "bounds": [round(0.45 * burn0, 1), round(1.25 * burn0, 1)]})
        notes.append("抛罩时刻未知 → 进反演（%.0f…%.0f s）" % (0.45 * burn0, 1.25 * burn0))
    elif fair:
        notes.append("抛罩时刻已知（T+%.1f s），固定不反演" % _f(fair.get("sep_met"), -1))

    if len(params) > MAX_PARAMS:
        notes.append("参数 %d 个超过上限 %d，按优先级截断（方位角/t_v/t_p/pitch_power 优先）"
                     % (len(params), MAX_PARAMS))
        params = params[:MAX_PARAMS]

    # v3.2.1：保证每个参数的搜索区间都包含标称值（do-no-harm：反演至少能"停在标称解"，
    # 不会因为边界写窄了而被迫走到物理上更差的解）
    for spec in params:
        _seed = seed.get(spec["name"])
        if _seed is None:
            continue
        b = spec["bounds"]
        b[0] = min(b[0], _seed)
        b[1] = max(b[1], _seed)

    n_par = len(params)
    if quick:
        mi, ps = 6, 5
    elif trusted:
        # 初值已是率定过的可信值：DE 只需在附近微调（总评估 ≈ ps*n_par*mi）
        mi, ps = (4, 4) if n_par <= 4 else (5, 5)
    else:
        mi, ps = (5, 4) if n_par <= 3 else (7, 5)
        if n_par >= 6:
            mi, ps = 8, 5
    if maxiter > 0:
        mi = int(maxiter)
    if popsize > 0:
        ps = int(popsize)
    # 参考轨道是"由方位角反算/箭型典型值"得来的低置信结果时，**不拿它当反演目标**：
    # 否则会与落区硬约束互相拉扯（实测 CZ-6A 只剩助推落区时落点被拉出区 3 km）。
    weights = dict(WEIGHTS)
    ref = (config.get("mission") or {}).get("reference_orbit") or {}
    zones_n = len([z for z in (config.get("notams") or []) if z.get("polygon_latlon")])
    if str(ref.get("confidence", "")).startswith("低") and zones_n > 0:
        # 低置信参考轨道不主导反演（避免与落区互相拉扯），但仍保留弱约束 + 圆度正则，
        # 否则"只有落区约束"时反演可能解出大偏心率的假入轨。
        weights["orbit_alt_km"] = 0.3
        weights["orbit_i_deg"] = 2.0
        notes.append("参考轨道置信度为「低」（推断值）：轨道罚项减权（高度×0.3、倾角×2），"
                     "并启用入轨圆度正则（e<0.02）")
    inv = {
        "parameters": params,
        "weights": weights,
        # de.dt=0.5：accurate 口径允许 dt 0.2–0.5（半长轴差 <1 m），反演快一倍
        "de": {"maxiter": mi, "popsize": ps, "seed": 42,
               "dt": 0.05 if config.get("numerics", {}).get("mode") == "legacy" else 0.5},
        "_plan": {"n_params": n_par, "est_simulations": mi * ps * n_par, "seed": seed,
                  "trusted_anchor": trusted,
                  "note": "由 app/core/autoplan.py 按箭型/落区几何自动生成；seed=当前配置参数值（DE 初始种群以它为中心）"}
    }
    notes.append("差分进化预算：maxiter=%d × popsize=%d ≈ %d 次仿真（约 %.0f s/进程数）"
                 % (mi, ps, mi * ps, mi * ps * 0.25 / 1.0))
    return inv, notes


def plan_mc(config: Dict[str, Any], n: int = 0) -> Optional[Dict[str, Any]]:
    """生成 ``config["mc"]``（抽样的路径必须真实存在，否则 mc.py 会 KeyError）。"""
    g = config.get("guidance") or {}
    veh = config.get("vehicle") or {}
    stages = veh.get("stages") or []
    vars_: List[Dict[str, Any]] = []

    def add(name, path, sigma, dist="normal"):
        try:
            I.get_path(config, path)
        except Exception:  # noqa: BLE001
            return
        vars_.append({"name": name, "path": path, "sigma": sigma, "dist": dist})

    add("flow_scale", "guidance.flow_scale", 0.010)
    add("azimuth_deg", "guidance.azimuth_deg", 0.150)
    if g.get("t_fairing") is not None:
        add("t_fairing", "guidance.t_fairing", 2.0)
    add("pitch_power", "guidance.pitch_power", 0.020)
    if stages and stages[-1].get("seco_met") is not None:
        add("seco_met", "vehicle.stages.%d.seco_met" % (len(stages) - 1), 2.0)
    if veh.get("boosters"):
        add("booster_prop", "vehicle.boosters.prop_kg_each", 0.005, "normal")
    if not vars_:
        return None
    return {"n": int(n or 250), "seed": 7, "variations": vars_}


def describe_plan(config: Dict[str, Any]) -> List[str]:
    """把自动计划翻译成给 GUI/日志看的中文摘要。"""
    out: List[str] = []
    inv = config.get("inversion") or {}
    params = inv.get("parameters") or []
    if params:
        out.append("参数反演：%d 个参数（%s）" % (
            len(params), "、".join(p["name"] for p in params)))
        de = inv.get("de") or {}
        n_par = len(params)
        out.append("  差分进化：maxiter=%s popsize=%s（约 %s 次仿真，本机约 %.0f s）"
                   % (de.get("maxiter"), de.get("popsize"),
                      (inv.get("_plan") or {}).get("est_simulations",
                                                   int(de.get("maxiter", 0)) * int(de.get("popsize", 0)) * n_par),
                      0.3 * (inv.get("_plan") or {}).get("est_simulations", 0)))
    else:
        out.append("参数反演：未启用（用户关闭，或该任务既无落区约束也无参考轨道）")
    mc = config.get("mc") or {}
    if mc.get("variations"):
        out.append("蒙特卡洛：N=%s，%d 个随机变量" % (mc.get("n"), len(mc["variations"])))
    else:
        out.append("蒙特卡洛：未配置")
    return out
