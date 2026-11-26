# -*- coding: utf-8 -*-
"""交会发射窗口与 T0 反推（CZ-2F / 载人任务）。

为什么单独一个脚本
------------------
``window.py`` 会算出「轨道平面窗口 + 每个窗口的交会可行性」，``rendezvous.py`` 在标称 T0
不在可行窗口时也会**临时**改用最近窗口的 T0 计算交会——但 `config.mission.t0_utc`
（也就是整条上升段弹道、报告、KML 用的 T0）并没有跟着改，于是报告里会出现
"标称 T0 相对窗口中心 +195 s、按窗口中心计算" 这种自相矛盾的说明。

本脚本把「反推 T0」这件事显式做出来：

1. 用入轨点状态 + 空间站星历算平面窗口（``window.orbit_window``），每个窗口都带交会
   可行性与预计对接时刻；
2. 在可行窗口里按「航警时窗内 > 总 ΔV+时长更小 > 离标称 T0 更近」排序，给出
   ``recommended_t0_utc`` 与偏移量；
3. 输出 ``report/交会发射窗口.json``；由 ``app/core/runner.py`` 的 ``t0win`` 步骤决定
   是否把 T0 写回 config 并重跑 export（保证弹道/落区/窗口/交会全部用同一个 T0）。

**不查询任何外部任务参数**：窗口完全由空间站星历 + 发射场 + 上升段入轨点几何推出。

用法::

    python scripts/t0_window.py --config config.json [--allow-network]
"""
import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("RK_ROOT") or os.path.dirname(HERE)

# --- v3 引擎/工作区解耦（与其它脚本一致） ---
_ENG = os.environ.get("RK_ENGINE")
if _ENG:
    for _p in (_ENG, os.path.join(_ENG, "scripts")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
# --- end ---
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import numpy as np  # noqa: E402
from common import orbit as O  # noqa: E402
from common import station as ST  # noqa: E402
import window as W  # noqa: E402


def _dt(text):
    try:
        return O.parse_iso(text) if isinstance(text, str) else text
    except Exception:  # noqa: BLE001
        return None


def _shift_s(a, b):
    """b - a 的秒数（任一为空返回 None）。"""
    if a is None or b is None:
        return None
    return (b - a).total_seconds()


def _site_plane_windows(cfg, src, t0_nom, search_h=24.0, step_s=10.0):
    """发射场穿越目标轨道面的时刻（几何窗口，不依赖上升段制导）。

    物理：升空瞬间发射场必须在目标轨道面内；10 min 上升段内目标面几乎不动，
    所以这就是"共面发射"的几何窗口。方位角取该时刻面内、朝东（顺行）的射向。
    """
    from common import geo
    m = cfg.get("mission") or {}
    site = m.get("launch_site") or {}
    if not site:
        return [], "缺少发射场"
    lat = float(site.get("lat_deg"))
    lon = float(site.get("lon_deg"))
    alt_m = float(site.get("alt_m_ellipsoid") or 1000.0)
    r_ecef = np.asarray(geo.geodetic_to_ecef(lat, lon, alt_m), dtype=float) / 1000.0

    def site_eci(dt):
        g = math.radians(O.gmst_deg(dt))
        c, s = math.cos(g), math.sin(g)
        return np.array([c * r_ecef[0] - s * r_ecef[1],
                         s * r_ecef[0] + c * r_ecef[1], r_ecef[2]])

    def d_plane(dt):
        r_st, v_st = src.state(dt)
        n = np.cross(r_st, v_st)
        n = n / np.linalg.norm(n)
        return float(site_eci(dt) @ n), n

    t = t0_nom - timedelta(hours=float(search_h))
    end = t0_nom + timedelta(hours=float(search_h))
    step = timedelta(seconds=float(step_s))
    out = []
    prev = None
    tcur = t
    while tcur <= end:
        d, _ = d_plane(tcur)
        if prev is not None and prev[1] != 0 and prev[1] * d < 0:
            frac = abs(prev[1]) / (abs(prev[1]) + abs(d) + 1e-12)
            tz = prev[0] + step * frac
            _, n = d_plane(tz)
            rs = site_eci(tz)
            u = rs / np.linalg.norm(rs)
            east = np.cross(np.array([0.0, 0.0, 1.0]), u)
            east = east / np.linalg.norm(east)
            north = np.cross(u, east)
            h = np.cross(n, u)
            if float(h @ east) < 0:
                h = -h
            az = math.degrees(math.atan2(float(h @ east), float(h @ north))) % 360.0
            el = src.elements(tz)
            out.append(dict(
                t0_utc=O.utc_text(tz), t0_bjt=O.bjt_text(tz),
                az_inertial_deg=round(az, 3), i_deg=round(float(el["i_deg"]), 4),
                raan_deg=round(float(el["raan_deg"]), 3),
                site_lat_deg=lat, site_lon_deg=lon))
        prev = (tcur, d)
        tcur = tcur + step
    return out, ""


def _rank(w, t0_nom):
    """排序键：可行优先 → 航警时窗内 → ΔV+8×时长更小 → 离标称 T0 更近。"""
    rv = w.get("rendezvous") or {}
    feasible = bool(rv.get("feasible"))
    in_notam = bool(w.get("notam_ok", True))
    dv = float(rv.get("dv_total_mps") or 1e9)
    dur = float(rv.get("duration_h") or 1e9)
    best = _dt(w.get("best_utc"))
    shift = abs(_shift_s(t0_nom, best) or 0.0)
    return (0 if feasible else 1, 0 if in_notam else 1,
            round(dv + 8.0 * dur, 1), round(shift, 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--allow-network", action="store_true")
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8-sig"))
    sim_dir = os.path.join(ROOT, "sim")
    out_dir = os.path.join(ROOT, "report")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "交会发射窗口.json")
    t0_nom = O.parse_iso(cfg["mission"]["t0_utc"])
    allow_net = bool(args.allow_network or os.environ.get("RK_ALLOW_NETWORK") == "1")

    out = dict(enabled=True, generated_utc=O.utc_text(datetime.now(timezone.utc)),
               t0_nominal_utc=O.utc_text(t0_nom), model="平面窗口 + 相位预算 + ΔV 预算",
               note=("窗口由空间站星历与上升段入轨点几何推出，不依赖任何外部任务参数；"
                     "仅为公开信息分析，不是飞行程序。"))

    st_cfg = dict(cfg.get("station") or {})
    if not st_cfg.get("enabled"):
        out.update(enabled=False, reason="未启用空间站交会（非载人任务）")
        _write(path, out)
        print("[t0win] 跳过：未启用空间站交会")
        return 0
    if ST is None:
        out.update(enabled=False, reason="空间站模块不可用")
        _write(path, out)
        return 1

    try:
        src = ST.resolve(cfg, t_hint=t0_nom, allow_network=allow_net,
                         logger=lambda s: print(s))
    except Exception as exc:  # noqa: BLE001
        out.update(enabled=False, reason="空间站轨道数据不可用：%s" % exc)
        _write(path, out)
        print("[t0win] 失败：%s" % exc)
        return 1

    ins = W.insertion_state(cfg, sim_dir, t0_nom) or W._fallback_insertion(cfg, t0_nom)
    if ins is None:
        out.update(enabled=False, reason="没有可用的入轨点状态（缺 sim/trajectory.csv）")
        _write(path, out)
        print("[t0win] 失败：缺少入轨点状态")
        return 1

    ow = W.orbit_window(cfg, ins, t0_nom, allow_network=allow_net)
    nw = None
    nw_note = ""
    try:
        nw, nw_note = W.notam_window(cfg, t0_nom, sim_dir)
    except Exception as exc:  # noqa: BLE001
        nw_note = "航警时窗计算失败：%s" % exc

    windows = [dict(w) for w in (ow.get("windows") or [])]
    # v4.3：目标平面 dogleg 允许发射时刻偏离"穿面时刻"若干分钟。
    # 半宽 = 预算角 / 地球自转角速度；默认 2.0° ≈ ±8 min。
    _lw = dict(cfg.get("launch_window") or {})
    _budget = float(_lw.get("dogleg_budget_deg") or 2.0)
    _pad_s = _budget / 15.04 * 3600.0
    for _w in windows:
        try:
            _w["dogleg_budget_deg"] = _budget
            _w["start_dogleg_utc"] = O.utc_text(
                O.parse_iso(_w["start_utc"]) - timedelta(seconds=_pad_s))
            _w["end_dogleg_utc"] = O.utc_text(
                O.parse_iso(_w["end_utc"]) + timedelta(seconds=_pad_s))
        except Exception:  # noqa: BLE001
            pass
    for w in windows:
        rv = w.get("rendezvous")
        if isinstance(rv, dict):
            # 时间字符串统一（下游报告/摘要直接读）
            w["dock_utc"] = rv.get("dock_utc")
            w["dock_bjt"] = rv.get("dock_bjt")
            w["feasible"] = bool(rv.get("feasible"))
            w["dv_total_mps"] = rv.get("dv_total_mps")
            w["duration_h"] = rv.get("duration_h")
            w["phasing_orbits"] = rv.get("phasing_orbits")
        else:
            w["feasible"] = False
    feasible = [w for w in windows if w.get("feasible")]

    out.update(station=src.info() if hasattr(src, "info") else {},
               insertion=dict(t_utc=O.utc_text(t0_nom + timedelta(seconds=float(ins["t_met_s"]))),
                              t_met_s=round(float(ins["t_met_s"]), 1),
                              lat_deg=round(float(ins["lat"]), 5),
                              lon_deg=round(float(ins["lon"]), 5),
                              alt_km=round(float(ins["alt_km"]), 2)),
               available=bool(ow.get("available")))
    if not ow.get("available"):
        out.update(windows=[], feasible_n=0, recommended_t0_utc=None,
                   reason=ow.get("reason") or "平面窗口不可用")
        _write(path, out)
        print("[t0win] 不可用：%s" % out["reason"])
        return 0

    # 几何窗口（发射场穿面）与平面残差
    try:
        _geo, _geo_note = _site_plane_windows(cfg, src, t0_nom,
                                              search_h=min(24.0, float(ow.get("search_hours") or 24.0)),
                                              step_s=10.0)
    except Exception as _exc:  # noqa: BLE001
        _geo, _geo_note = [], "几何窗口计算失败：%s" % _exc
    try:
        _el_ach = dict(ins["elements"])
        _el_st = src.elements(t0_nom + timedelta(seconds=float(ins["t_met_s"])))
        _n1 = np.array([math.sin(math.radians(_el_ach["i_deg"])) * math.sin(math.radians(_el_ach["raan_deg"])),
                        -math.sin(math.radians(_el_ach["i_deg"])) * math.cos(math.radians(_el_ach["raan_deg"])),
                        math.cos(math.radians(_el_ach["i_deg"]))])
        _n2 = np.array([math.sin(math.radians(_el_st["i_deg"])) * math.sin(math.radians(_el_st["raan_deg"])),
                        -math.sin(math.radians(_el_st["i_deg"])) * math.cos(math.radians(_el_st["raan_deg"])),
                        math.cos(math.radians(_el_st["i_deg"]))])
        _resid = math.degrees(math.acos(max(-1.0, min(1.0, float(_n1 @ _n2)))))
        _di = float(_el_ach["i_deg"]) - float(_el_st["i_deg"])
        _draan = O.wrap180(float(_el_ach["raan_deg"]) - float(_el_st["raan_deg"]))
    except Exception:  # noqa: BLE001
        _resid, _di, _draan = None, None, None
    out.update(dogleg_budget_deg=_budget,
               site_plane_windows=_geo, site_plane_note=_geo_note,
               plane_residual_deg=(round(_resid, 4) if _resid is not None else None),
               plane_di_deg=(round(_di, 4) if _di is not None else None),
               plane_draan_deg=(round(_draan, 4) if _draan is not None else None),
               windows=windows, feasible_n=len(feasible),
               tolerance_deg=ow.get("tolerance_deg"),
               search_hours=ow.get("search_hours"),
               notam_window=(nw or {}).get("practical_window"),
               notam_note=nw_note,
               best_near_t0=ow.get("best_near_t0"))

    # ---- 推荐 T0 ----
    recommended = None
    reason = ""
    inside = None
    for w in feasible:
        a = _dt(w.get("start_dogleg_utc") or w.get("start_utc"))
        b = _dt(w.get("end_dogleg_utc") or w.get("end_utc"))
        if a and b and a <= t0_nom <= b:
            inside = w
            break
    if inside is not None:
        recommended = t0_nom
        _need = None
        try:
            _best = _dt(inside.get("best_utc"))
            _need = abs((t0_nom - _best).total_seconds()) / 3600.0 * 15.04 \
                if _best else None
        except Exception:  # noqa: BLE001
            _need = None
        reason = ("标称 T0 在 dogleg 可达范围内（%s ~ %s UTC；穿越面最佳时刻 %s，"
                  "需平面修正约 %s°），保留该 T0"
                  % (inside.get("start_dogleg_utc"), inside.get("end_dogleg_utc"),
                     inside.get("best_utc"),
                     ("%.2f" % _need) if _need is not None else "—"))
    elif feasible:
        best = min(feasible, key=lambda w: _rank(w, t0_nom))
        recommended = _dt(best.get("best_utc"))
        shift = _shift_s(t0_nom, recommended) or 0.0
        reason = ("按交会窗口反推：最近可行窗口 %s（%+.0f s），窗宽 %.0f s，"
                  "预计对接 %s，总 ΔV %.0f m/s，时长 %.2f h%s"
                  % (best.get("best_utc"), shift, float(best.get("duration_s") or 0.0),
                     best.get("dock_utc") or "—",
                     float(best.get("dv_total_mps") or 0.0),
                     float(best.get("duration_h") or 0.0),
                     "" if best.get("notam_ok", True) else "（不在航警时窗内）"))
    else:
        near = min(windows, key=lambda w: _rank(w, t0_nom)) if windows else None
        if near is not None:
            recommended = _dt(near.get("best_utc"))
            reason = ("没有满足 ΔV/时长预算的可行窗口；最近平面窗口 %s 仅作参考"
                      % near.get("best_utc"))
        else:
            reason = "未找到平面窗口"

    out["recommended_t0_utc"] = O.utc_text(recommended) if recommended else None
    out["recommended_t0_bjt"] = O.bjt_text(recommended) if recommended else None
    out["shift_s"] = (round(_shift_s(t0_nom, recommended), 1)
                      if recommended else None)
    out["recommended_reason"] = reason
    if recommended is not None and inside is None and feasible:
        best = min(feasible, key=lambda w: _rank(w, t0_nom))
        out["recommended_window"] = {k: best.get(k) for k in
                                     ("start_utc", "end_utc", "best_utc", "duration_s",
                                      "dock_utc", "dv_total_mps", "duration_h",
                                      "phasing_orbits", "notam_ok", "d_raan_deg")}
    _write(path, out)

    print("[t0win] 平面窗口 %d 个，其中交会可行 %d 个（容差 %.2f°）" % (
        len(windows), len(feasible), float(ow.get("tolerance_deg") or 0.0)))
    for k, w in enumerate(windows, 1):
        print("  %d) T0 %s ~ %s，最佳 %s（%s）；%s" % (
            k, str(w.get("start_utc"))[5:19].replace("T", " "),
            str(w.get("end_utc"))[11:19], w.get("best_utc"),
            ("可行 ΔV %.0f m/s / %.2f h / 对接 %s" % (
                float(w.get("dv_total_mps") or 0.0),
                float(w.get("duration_h") or 0.0), w.get("dock_utc") or "—"))
            if w.get("feasible") else
            ("不可行（%s）" % ((w.get("rendezvous") or {}).get("reason") or "—")),
            "航警时窗内" if w.get("notam_ok", True) else "航警时窗之外"))
    if out.get("site_plane_windows"):
        print("[t0win] 几何窗口（发射场穿过目标轨道面）：")
        for _w in out["site_plane_windows"]:
            print("  - T0 %s（面内惯性射向 %.2f°）" % (_w["t0_utc"], _w["az_inertial_deg"]))
    if out.get("plane_residual_deg") is not None:
        print("[t0win] 实测入轨面残差：%.3f°（Δi %+.3f°，ΔRAAN %+.3f°）" % (
            float(out["plane_residual_deg"]), float(out.get("plane_di_deg") or 0.0),
            float(out.get("plane_draan_deg") or 0.0)))
    print("[t0win] 推荐 T0：%s（%s）" % (
        out.get("recommended_t0_utc"), out.get("recommended_reason")))
    print("[t0win] → %s" % path)
    return 0


def _write(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    sys.exit(main())
