#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""v2.0 基准与精度自检（bench）：一条命令给出「快了多少、精度差多少」。

三个子命令（可组合）：

    python scripts/bench.py speed               # 数值内核耗时（legacy vs accurate，多档 dt）
    python scripts/bench.py verify --reference <v1工程>/sim
                                                # legacy 逐位复现 v1 + accurate 的 dt 收敛性
    python scripts/bench.py accuracy            # 只跑 accurate 的 dt 收敛表（无需参考工程）

输出：控制台表格 + docs/benchmark_v2.json（可复现）。

口径说明
--------
* ``legacy``  = v1.0 数值语义（事件只在步边界判定、质量在步首按步内流量扣减）；
* ``accurate``= v2.0 事件对齐 + 质量/推进剂随 RK4 同步积分；
* 两者的**物理方程完全相同**，差别只在离散化与事件时刻，详见 docs/精度验证_v2.md。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("RK_ROOT") or HERE.parent)

# --- v3 引擎/工作区解耦（自动补丁） ---
_ENG = os.environ.get("RK_ENGINE")
if _ENG:
    for _p in (_ENG, os.path.join(_ENG, "scripts")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
# --- end ---
for p in (str(ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import numpy as np  # noqa: E402
import dynamics as D  # noqa: E402

DOCS = ROOT / "docs"


def load_cfg(path) -> dict:
    return json.load(open(path, encoding="utf-8"))


def make(veh, p, mode, dt, debris_dt=None):
    p.numerics = mode
    p.dt = dt
    if mode == "accurate":
        p.debris_numerics = "accurate"
        p.debris_dt = debris_dt or p.debris_dt
    return D.simulate(veh, p, collect_debris=True)


def timed(fn, runs):
    ts = []
    for _ in range(runs):
        t0 = time.perf_counter()
        out = fn()
        ts.append(time.perf_counter() - t0)
    return statistics.median(ts), out


# --------------------------------------------------------------------------- #
def cmd_speed(cfg, runs):
    veh = D.vehicle_from_config(cfg)
    base = D.params_from_config(cfg)
    rows = []

    def run(mode, dt, ddt=None):
        p = D.params_from_config(cfg)
        p.numerics = mode
        p.dt = dt
        if mode == "accurate":
            p.debris_numerics = "accurate"
            p.debris_dt = ddt or 1.0
        return run_one(p)

    def run_one(p):
        return D.simulate(veh, p, collect_debris=True)

    for mode, dt, ddt, label in (
        ("legacy", float((cfg.get("guidance") or {}).get("dt", 0.02)), None,
         "v1 口径 dt=0.02（复现基准）"),
        ("legacy", 0.01, None, "v1 口径 dt=0.01"),
        ("accurate", 0.5, 1.0, "v2 事件对齐 dt=0.5"),
        ("accurate", 0.2, 1.0, "v2 事件对齐 dt=0.2（默认）"),
        ("accurate", 0.05, 1.0, "v2 事件对齐 dt=0.05"),
    ):
        med, r = timed(lambda: run(mode, dt, ddt), runs)
        o = r["orbit_seco"]
        rows.append(dict(mode=mode, dt=dt, debris_dt=(ddt if ddt else base.debris_dt),
                         seconds=round(med, 4), n_steps=r["stats"]["n_steps"],
                         a_km=o["a_km"], e=o["e"], i_deg=o["i_deg"],
                         v_ins=r["insertion_state"]["speed_m_s"],
                         t_seco=r["t_seco"], label=label))
    print("\n%-30s %8s %10s %8s %12s %10s" % ("口径", "耗时[s]", "步数", "a[km]", "v_ins[m/s]", "t_seco[s]"))
    print("-" * 86)
    for r in rows:
        print("%-30s %8.3f %10d %8.3f %12.4f %10.3f" % (
            r["label"], r["seconds"], r["n_steps"], r["a_km"],
            r["v_ins"], r["t_seco"]))
    base_row = rows[0]
    fast = min(rows, key=lambda r: r["seconds"])
    print("\n最快口径：%s，%.3f s（相对 v1 口径 %.3f s，加速 %.1f×）" % (
        fast["label"], fast["seconds"], base_row["seconds"],
        base_row["seconds"] / fast["seconds"]))
    return dict(rows=rows)


def cmd_accuracy(cfg, runs):
    veh = D.vehicle_from_config(cfg)
    rows = []
    ref = None
    for dt in (0.5, 0.2, 0.1, 0.05, 0.02):
        p = D.params_from_config(cfg)
        p.numerics = "accurate"
        p.dt = dt
        p.debris_numerics = "accurate"
        p.debris_dt = 1.0
        r = D.simulate(veh, p, collect_debris=True)
        o = r["orbit_seco"]
        row = dict(dt=dt, n_steps=r["stats"]["n_steps"], a_km=o["a_km"], e=o["e"],
                   i_deg=o["i_deg"], v_ins=r["insertion_state"]["speed_m_s"],
                   t_seco=r["t_seco"])
        rows.append(row)
        if ref is None:
            ref = row
    print("\n=== accurate 口径 dt 收敛性（v2 事件对齐）===")
    print("%8s %8s %14s %14s %14s" % ("dt[s]", "步数", "a[km]", "Δa vs dt=0.5[m]", "v_ins[m/s]"))
    for r in rows:
        print("%8.3f %8d %14.6f %14.4f %14.4f" % (
            r["dt"], r["n_steps"], r["a_km"], (r["a_km"] - ref["a_km"]) * 1000.0, r["v_ins"]))
    spread = (max(r["a_km"] for r in rows) - min(r["a_km"] for r in rows)) * 1000.0
    print("\ndt=0.5…0.02 的入轨半长轴极差：%.3f m（< 1 m 即认为事件对齐已收敛）" % spread)
    return dict(rows=rows, a_spread_m=spread)


def _read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def cmd_verify(cfg, reference: Path, runs):
    veh = D.vehicle_from_config(cfg)
    p = D.params_from_config(cfg)
    p.numerics = "legacy"
    p.dt = float((cfg.get("guidance") or {}).get("dt", 0.02))
    r = D.simulate(veh, p, collect_debris=True)
    traj = _read_csv(reference / "trajectory.csv")
    imp = {x["object"]: x for x in _read_csv(reference / "落点与射程.csv")}
    orb = json.load(open(reference / "入轨根数.json", encoding="utf-8"))
    ev = {e["event"]: float(e["t_met_s"]) for e in _read_csv(reference / "弹道事件表.csv")}

    t_ref = np.array([float(x["t_met_s"]) for x in traj])
    lat_ref = np.array([float(x["lat_deg"]) for x in traj])
    lon_ref = np.array([float(x["lon_deg"]) for x in traj])
    alt_ref = np.array([float(x["alt_m"]) for x in traj])
    n = min(len(r["t"]), len(t_ref))
    dh = np.hypot((r["lat"][:n] - lat_ref[:n]) * 111190.0,
                  (r["lon"][:n] - lon_ref[:n]) * 111190.0 * np.cos(np.radians(lat_ref[:n])))
    da = np.abs(r["alt"][:n] - alt_ref[:n])
    print("\n=== legacy 口径 vs v1.0 参考工程 ===")
    print("轨迹最大偏差：水平 %.3f m，高度 %.3f m（v1 回归阈值 200 / 150 m）" % (dh.max(), da.max()))
    for d in r["debris"]:
        ref = imp.get(d["obj"])
        if not ref:
            continue
        dlat = (d["impact"]["lat"] - float(ref["lat_deg"])) * 111190.0
        dlon = (d["impact"]["lon"] - float(ref["lon_deg"])) * 111190.0 * np.cos(
            np.radians(float(ref["lat_deg"])))
        print("落点 %-10s Δ=%.3f m" % (d["obj"], float(np.hypot(dlat, dlon))))
    o = r["orbit_seco"]
    print("入轨 a: %.6f vs %.6f km（Δ=%.3f m）；e: %.8f vs %.8f；i: %.6f vs %.6f" % (
        o["a_km"], orb["a_km"], (o["a_km"] - orb["a_km"]) * 1000.0,
        o["e"], orb["e"], o["i_deg"], orb["i_deg"]))
    for e in r["events"]:
        k = e["event"]
        if k in ev:
            print("事件 %-22s Δt=%+.6f s" % (k, e["t_met_s"] - ev[k]))
    return dict(traj_horiz_m=float(dh.max()), traj_alt_m=float(da.max()),
                event_dt={e["event"]: e["t_met_s"] - ev[e["event"]] for e in r["events"]
                          if e["event"] in ev},
                orbit=dict(a_km=o["a_km"], e=o["e"], i_deg=o["i_deg"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description="v2.0 性能/精度基准")
    ap.add_argument("cmd", choices=["speed", "verify", "accuracy", "all"])
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--reference", default=None, help="v1.0 参考工程 sim/ 目录（verify 用）")
    ap.add_argument("--runs", type=int, default=3, help="计时重复次数（取中位数）")
    ap.add_argument("--out", default=str(DOCS / "benchmark_v2.json"))
    args = ap.parse_args(argv)
    cfg = load_cfg(args.config)
    res = {"config": str(Path(args.config).resolve()), "python": sys.version.split()[0],
           "generated": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if args.cmd in ("speed", "all"):
        res["speed"] = cmd_speed(cfg, args.runs)
    if args.cmd in ("accuracy", "all"):
        res["accuracy"] = cmd_accuracy(cfg, args.runs)
    if args.cmd in ("verify", "all"):
        if not args.reference:
            print("[bench] verify 需要 --reference <v1工程>/sim")
        else:
            res["verify"] = cmd_verify(cfg, Path(args.reference), args.runs)
    if args.cmd == "all" and args.reference is None:
        pass
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n[bench] →", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
