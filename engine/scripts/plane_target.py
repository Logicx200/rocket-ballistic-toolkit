# -*- coding: utf-8 -*-
"""按「目标 T0 + 实测入轨时刻」把空间站轨道面写进制导配置。

- ``guidance.i_target_deg`` / ``guidance.raan_target_deg``：末级 dogleg 的目标平面；
- 同时给出「起飞时发射场相对目标面的角距 / 需要的平面机动量」，
  用于判断该 T0 是否需要较大的侧向修正。

写入后由 export 执行；通常由 runner 的 t0win 步骤与 export 前调用。
"""
import argparse
import json
import math
import os
import sys
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("RK_ROOT") or os.path.dirname(HERE)
_ENG = os.environ.get("RK_ENGINE")
if _ENG:
    for _p in (_ENG, os.path.join(_ENG, "scripts")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import numpy as np  # noqa: E402
from common import orbit as O  # noqa: E402
from common import station as ST  # noqa: E402


def _site_plane_angle(cfg, t0, src, t_met):
    """起飞瞬间发射场相对「入轨时刻目标面」的角距 [deg]。"""
    site = (cfg.get("mission") or {}).get("launch_site") or {}
    lat, lon = float(site["lat_deg"]), float(site["lon_deg"])
    alt_m = float(site.get("alt_m_ellipsoid") or 1000.0)
    from common import geo
    r_ecef = np.asarray(geo.geodetic_to_ecef(lat, lon, alt_m), dtype=float) / 1000.0
    g = math.radians(O.gmst_deg(t0))
    c, s = math.cos(g), math.sin(g)
    r_eci = np.array([c * r_ecef[0] - s * r_ecef[1],
                      s * r_ecef[0] + c * r_ecef[1], r_ecef[2]])
    r_st, v_st = src.state(t0 + timedelta(seconds=t_met))
    n = np.cross(r_st, v_st)
    n = n / np.linalg.norm(n)
    return math.degrees(math.asin(max(-1.0, min(1.0,
        float(r_eci @ n) / float(np.linalg.norm(r_eci))))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--allow-network", action="store_true")
    args = ap.parse_args()
    cfg = json.load(open(args.config, encoding="utf-8-sig"))
    if not (cfg.get("station") or {}).get("enabled"):
        print("[plane_target] 未启用空间站：跳过")
        return 0
    t0 = O.parse_iso(cfg["mission"]["t0_utc"])
    t_met = 584.0
    try:
        orb = json.load(open(os.path.join(ROOT, "sim", "入轨根数.json"),
                             encoding="utf-8-sig"))
        t_met = float(orb["insertion"]["t_met_s"])
    except Exception:  # noqa: BLE001
        t_met = float((cfg.get("guidance") or {}).get("t_seco") or 584.0)
    try:
        src = ST.resolve(cfg, t_hint=t0, allow_network=bool(
            args.allow_network or os.environ.get("RK_ALLOW_NETWORK") == "1"))
    except Exception as exc:  # noqa: BLE001
        print("[plane_target] 空间站数据不可用：%s" % exc)
        return 1
    el = src.elements(t0 + timedelta(seconds=t_met))
    i_t = round(float(el["i_deg"]), 4)
    raan_t = round(float(el["raan_deg"]), 4)
    g = cfg.setdefault("guidance", {})
    g["i_target_deg"] = i_t
    g["raan_target_deg"] = raan_t
    if float(g.get("yaw2_tau") or 200.0) > 80.0:
        g["yaw2_tau"] = 60.0
    try:
        req = abs(_site_plane_angle(cfg, t0, src, t_met))
    except Exception:  # noqa: BLE001
        req = None
    cfg.setdefault("_v3", {})["plane_target"] = dict(
        t0_utc=O.utc_text(t0), i_target_deg=i_t, raan_target_deg=raan_t,
        site_plane_angle_deg=(round(req, 4) if req is not None else None),
        t_met_s=round(t_met, 1),
        note="发射场相对入轨时刻目标面的角距；dogleg 需修正该量级（含倾角差）")
    json.dump(cfg, open(args.config, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("[plane_target] T0=%s 目标面 i=%.3f° RAAN=%.3f°；发射场离面 %.3f°"
          % (O.utc_text(t0), i_t, raan_t, req if req is not None else float("nan")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
