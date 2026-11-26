# -*- coding: utf-8 -*-
"""CZ-2F 专用：空间站轨道准备（拉取/导入/内置快照 → 统一 TEME 状态）。

输出
----
* ``sim/空间站轨道.json``：数据来源、历元、覆盖区间、T0/入轨时刻根数、
  星下点序列、过境（发射场/观测点）摘要；
* ``sim/空间站星下点.csv``：`t_utc, lat_deg, lon_deg, alt_km, sunlit` 序列。

用法：python scripts/station_track.py --config config.json [--hours 36]
"""
import argparse
import csv
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

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

import numpy as np
from common import geo
from common import orbit as O

try:
    from common import station as ST
except Exception:  # noqa: BLE001
    ST = None


def _write_csv(path, header, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)


def _passes(src, lat, lon, alt_m, t_start, t_end, step_s=30.0, min_el_deg=5.0):
    """地面站过境：返回 [{rise,max,set}]（简化为连续可见弧段）。"""
    n = max(2, int((t_end - t_start).total_seconds() / step_s) + 1)
    seg = []
    cur = None
    for i in range(n):
        t = t_start + timedelta(seconds=i * step_s)
        try:
            r, v = src.state(t)
            acc = O.access_from_site(t, r, lat, lon, alt_m, min_el_deg=min_el_deg)
        except Exception:  # noqa: BLE001
            continue
        if acc["visible"]:
            if cur is None:
                cur = dict(rise_utc=O.utc_text(t), max_el_deg=acc["el_deg"],
                           max_el_utc=O.utc_text(t), set_utc=None,
                           max_az_deg=acc["az_deg"])
            elif acc["el_deg"] > cur["max_el_deg"]:
                cur.update(max_el_deg=acc["el_deg"], max_el_utc=O.utc_text(t),
                           max_az_deg=acc["az_deg"])
        else:
            if cur is not None:
                cur["set_utc"] = O.utc_text(t)
                cur["duration_s"] = round((O.parse_iso(cur["set_utc"])
                                           - O.parse_iso(cur["rise_utc"])).total_seconds(), 1)
                seg.append(cur)
                cur = None
    if cur is not None:
        cur["set_utc"] = O.utc_text(t_end)
        cur["duration_s"] = round((t_end - O.parse_iso(cur["rise_utc"])).total_seconds(), 1)
        seg.append(cur)
    return seg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--hours", type=float, default=None,
                    help="星下点覆盖范围（默认 T0-6h ~ T0+36h）")
    ap.add_argument("--allow-network", action="store_true")
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))
    sim_dir = os.path.join(ROOT, "sim")
    os.makedirs(sim_dir, exist_ok=True)
    t0 = O.parse_iso(cfg["mission"]["t0_utc"])
    allow_net = bool(args.allow_network or os.environ.get("RK_ALLOW_NETWORK") == "1")

    out = dict(enabled=False, t0_utc=O.utc_text(t0), generated_utc="")
    if ST is None:
        out["error"] = "engine/common/station.py 不可用"
    else:
        try:
            src = ST.resolve(cfg, t_hint=t0, allow_network=allow_net,
                             logger=lambda s: print(s))
            out.update(enabled=True, station=src.info())
            # 星下点序列
            h0 = 6.0
            h1 = float(args.hours) if args.hours else 30.0
            t_start = t0 - timedelta(hours=h0)
            t_end = t0 + timedelta(hours=h1)
            step = 60.0
            n = int((t_end - t_start).total_seconds() / step) + 1
            rows = []
            osc = []
            for i in range(n):
                t = t_start + timedelta(seconds=i * step)
                if not src.covers(t):
                    continue
                try:
                    r, v = src.state(t)
                except Exception:  # noqa: BLE001
                    continue
                la, lo, al = O.subpoint(t, r)
                sl = O.sunlit(t, r)
                rows.append(["%s" % O.utc_text(t), "%.5f" % la, "%.5f" % lo,
                             "%.2f" % al, "1" if sl else "0"])
                if i % 10 == 0:
                    el = O.elements_from_state(r, v)
                    osc.append(dict(t_utc=O.utc_text(t), raan_deg=round(el["raan_deg"], 3)))
            _write_csv(os.path.join(sim_dir, "空间站星下点.csv"),
                       ["t_utc", "lat_deg", "lon_deg", "alt_km", "sunlit"], rows)
            out["ground_track"] = dict(start_utc=O.utc_text(t_start),
                                       end_utc=O.utc_text(t_end),
                                       step_s=step, n=len(rows),
                                       file="sim/空间站星下点.csv")
            out["raan_series"] = osc

            # T0 / 入轨时刻根数
            try:
                el0 = src.elements(t0)
                out["at_t0"] = {k: round(v0, 4) for k, v0 in el0.items()
                                if isinstance(v0, (int, float))}
                out["at_t0"]["ltan_h"] = round(O.ltan_from_raan(t0, el0["raan_deg"]), 3)
                out["at_t0"]["j2_raan_rate_deg_day"] = round(
                    O.j2_raan_rate_deg_day(el0["a_km"], el0["e"], el0["i_deg"]), 4)
            except Exception as exc:  # noqa: BLE001
                out["at_t0_error"] = str(exc)

            orb = {}
            try:
                with open(os.path.join(sim_dir, "入轨根数.json"), encoding="utf-8") as f:
                    orb = json.load(f)
            except Exception:  # noqa: BLE001
                pass
            t_ins = (orb.get("insertion") or {}).get("t_met_s")
            if t_ins is not None:
                t_ins_dt = t0 + timedelta(seconds=float(t_ins))
                try:
                    eli = src.elements(t_ins_dt)
                    out["at_insertion"] = dict(
                        t_utc=O.utc_text(t_ins_dt),
                        raan_deg=round(eli["raan_deg"], 4),
                        i_deg=round(eli["i_deg"], 4),
                        u_deg=round(eli["u_deg"], 4),
                        alt_km=round(eli["a_km"] - O.RE_EQ, 2))
                except Exception as exc:  # noqa: BLE001
                    out["at_insertion_error"] = str(exc)

            # 过境：发射场 + 观测点（T0-6h ~ T0+30h）
            pts = []
            site = (cfg.get("mission") or {}).get("launch_site") or {}
            if site:
                pts.append(("launch_site", site.get("name", "发射场"),
                            site.get("lat_deg"), site.get("lon_deg"),
                            float(site.get("alt_m_ellipsoid") or 0.0)))
            for o in cfg.get("observers") or []:
                pts.append((o.get("id", "obs"), o.get("name", "观测点"),
                            o.get("lat_deg"), o.get("lon_deg"),
                            float(o.get("alt_m") or 0.0)))
            passes = {}
            for key, name, la, lo, al in pts:
                if la is None or lo is None:
                    continue
                try:
                    passes[key] = dict(name=name, lat_deg=la, lon_deg=lo,
                                       arcs=_passes(src, float(la), float(lo), al,
                                                    t_start, t_end))
                except Exception as exc:  # noqa: BLE001
                    passes[key] = dict(name=name, error=str(exc))
            out["passes"] = passes
        except Exception as exc:  # noqa: BLE001
            out["error"] = str(exc)
            print("[station] 空间站轨道准备失败：%s" % exc)

    out["generated_utc"] = O.utc_text(datetime.now(timezone.utc))
    path = os.path.join(sim_dir, "空间站轨道.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    if out.get("enabled"):
        a = out.get("at_t0") or {}
        print("[station] 数据源：%s；T0 高度 %.1f km、倾角 %.2f°、RAAN %.2f°、LTAN %.2f h" % (
            out.get("station", {}).get("kind", "?"),
            float(a.get("a_km") or 0) - O.RE_EQ, float(a.get("i_deg") or 0),
            float(a.get("raan_deg") or 0), float(a.get("ltan_h") or 0)))
        print("[station] 星下点 %d 点 → sim/空间站星下点.csv" % out.get("ground_track", {}).get("n", 0))
    print("[station] → %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
