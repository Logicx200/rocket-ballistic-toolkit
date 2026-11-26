# -*- coding: utf-8 -*-
"""蒙特卡洛（MC）不确定度（v2.0：并行 + 事件对齐内核）。

按配置对关键参数抽样，统计
  * 事件时刻 1σ、入轨根数 1σ、各残骸落点 DRMS/95%/入区率；
  * 指定时刻轨迹散布（位置 1σ 表）。

v2 改动
-------
1. **并行**：``ProcessPoolExecutor`` 按 ``--jobs`` 分发（默认 CPU 核数，最多 8）。
   受限环境（命名管道被禁）自动退化为串行，不影响结果。随机数在**主进程**一次
   生成后分发，因此 N=250 的结果与串行逐位一致、与并行度无关。
2. **内核**：默认沿用 ``config.numerics.mode``（推荐 accurate，dt=0.2，单次 ~0.18 s
   而 legacy dt=0.05 约 0.6 s）。``--legacy`` 可回到 v1 口径复现旧结果。
3. 进度输出带耗时/ETA（每 10%）。

配置（可选）：
  "mc": {"n": 250, "dt": 0.2, "seed": 7,
         "variations": [{"name":"azimuth","path":"guidance.azimuth_deg","sigma":0.15,"dist":"normal"}, ...]}
用法：python scripts/mc.py --config config.json [--n 250] [--jobs 8] [--legacy]
输出：uncertainty/mc_samples.csv、uncertainty/mc_stats.json
"""
import sys, os, json, math, argparse, copy, csv, time
import concurrent.futures as cf
from itertools import repeat

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("RK_ROOT") or os.path.dirname(HERE)

# --- v3 引擎/工作区解耦（自动补丁） ---
_ENG = os.environ.get("RK_ENGINE")
if _ENG:
    for _p in (_ENG, os.path.join(_ENG, "scripts")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
# --- end ---
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)
import numpy as np
import dynamics as D
from common import notam as N

DEFAULT_VARIATIONS = [
    {"name": "flow_scale", "path": "guidance.flow_scale", "sigma": 0.010, "dist": "normal"},
    {"name": "azimuth_deg", "path": "guidance.azimuth_deg", "sigma": 0.150, "dist": "normal"},
    {"name": "t_fairing", "path": "guidance.t_fairing", "sigma": 2.000, "dist": "normal"},
    {"name": "pitch_power", "path": "guidance.pitch_power", "sigma": 0.020, "dist": "normal"},
    {"name": "seco_met", "path": "vehicle.stages.-1.seco_met", "sigma": 2.000, "dist": "normal"},
]
EVENT_KEYS = ("起飞", "整流罩分离")
_WORKER = {}


def get_path(cfg, path):
    cur = cfg
    for key in path.split("."):
        cur = cur[int(key)] if isinstance(cur, list) else cur[key]
    return cur


def set_path(cfg, path, value):
    keys = path.split(".")
    cur = cfg
    for key in keys[:-1]:
        cur = cur[int(key)] if isinstance(cur, list) else cur[key]
    cur[int(keys[-1]) if isinstance(cur, list) else keys[-1]] = value


def _worker_init(cfg_dict, mode, dt, debris_dt):
    _WORKER["cfg"] = cfg_dict
    _WORKER["mode"] = mode
    _WORKER["dt"] = dt
    _WORKER["debris_dt"] = debris_dt


def _run_one(job):
    """单个样本：job = (run_id, {name: value})。返回记录 dict（失败返回 {'_fail': True}）。"""
    idx, vals = job
    cfg = copy.deepcopy(_WORKER["cfg"])
    rec = {"run_id": idx}
    for name, val in vals.items():
        rec[name] = val
        for v in cfg.get("mc", {}).get("variations", []):
            if v["name"] == name:
                set_path(cfg, v["path"], val)
                break
    veh = D.vehicle_from_config(cfg)
    p = D.params_from_config(cfg)
    D.apply_mode(p, _WORKER.get("mode") or p.numerics, _WORKER.get("dt"))
    if _WORKER.get("debris_dt"):
        p.debris_dt = _WORKER["debris_dt"]
    try:
        r = D.simulate(veh, p, collect_debris=True)
    except Exception:  # noqa: BLE001
        return {"run_id": idx, "_fail": True}
    if r["failures"] or not r.get("orbit_seco"):
        return {"run_id": idx, "_fail": True}
    for e in r["events"]:
        if e["event"] in EVENT_KEYS or "SECO" in e["event"] or "分离" in e["event"]:
            key = e["event"].replace("/", "_").replace("(", "").replace(")", "")
            rec["t_" + key] = e["t_met_s"]
    o = r["orbit_seco"]
    rec.update(a_km=o["a_km"], e=o["e"], i_deg=o["i_deg"])
    for d in r["debris"]:
        rec["lat_" + d["obj"]] = d["impact"]["lat"]
        rec["lon_" + d["obj"]] = d["impact"]["lon"]
        rec["timp_" + d["obj"]] = d["impact"]["t_met_s"]
    return rec


def _jobs_from_sample(sample):
    """把一次抽样的 {name: value} 转成 (run_id, values) 任务。"""
    return sample


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--n", type=int, default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--dt", type=float, default=None)
    ap.add_argument("--debris-dt", type=float, default=None)
    ap.add_argument("--jobs", type=int, default=0, help="并行进程数（0=自动，1=串行）")
    ap.add_argument("--legacy", action="store_true", help="用 v1 口径（legacy 内核）复现旧结果")
    ap.add_argument("--accurate", action="store_true", help="用 v2 事件对齐口径（默认，见 config.numerics）")
    args = ap.parse_args()
    cfg = json.load(open(args.config, encoding="utf-8"))
    mc = cfg.setdefault("mc", {})
    mc.setdefault("variations", copy.deepcopy(DEFAULT_VARIATIONS))
    n = args.n or int(mc.get("n", 250))
    seed = args.seed if args.seed is not None else int(mc.get("seed", 7))
    mode = "legacy" if args.legacy else str(cfg.get("numerics", {}).get("mode", "accurate"))
    dt = args.dt if args.dt is not None else float(
        mc.get("dt") if mc.get("dt") is not None else cfg.get("numerics", {}).get("dt", 0.2))
    debris_dt = args.debris_dt if args.debris_dt is not None else float(
        cfg.get("numerics", {}).get("debris_dt", 1.0))
    rng = np.random.default_rng(seed)
    out_dir = os.path.join(ROOT, "uncertainty")
    os.makedirs(out_dir, exist_ok=True)
    print("[mc] N=%d seed=%d mode=%s dt=%.3f debris_dt=%.2f variations=%s" % (
        n, seed, mode, dt, debris_dt, [v["name"] for v in mc["variations"]]))

    # ---- 抽样在主进程一次生成（保障并行/串行结果一致）----
    jobs = []
    for k in range(n):
        vals = {}
        for v in mc["variations"]:
            sigma = float(v["sigma"])
            base = float(get_path(cfg, v["path"]))
            if v.get("dist", "normal") == "normal":
                val = base + rng.normal(0.0, sigma)
            else:
                val = base + rng.uniform(-sigma, sigma)
            if v["name"] == "flow_scale":
                val = max(0.95, min(1.05, val))
            vals[v["name"]] = val
        jobs.append((k, vals))

    jobs_n = args.jobs if args.jobs > 0 else min(8, os.cpu_count() or 1)
    t0 = time.time()
    rows, fails = [], 0

    def _collect(rec, done):
        nonlocal fails
        if rec.get("_fail"):
            fails += 1
        else:
            rows.append(rec)
        if done % max(1, n // 10) == 0 or done == n:
            el = time.time() - t0
            eta = el / max(done, 1) * (n - done)
            print("[mc] %d/%d  已用 %.1fs  预计剩余 %.0fs" % (done, n, el, eta), flush=True)

    if jobs_n > 1 and n > 1:
        try:
            with cf.ProcessPoolExecutor(max_workers=jobs_n, initializer=_worker_init,
                                        initargs=(cfg, mode, dt, debris_dt)) as pool:
                futs = {pool.submit(_run_one, j): j[0] for j in jobs}
                done = 0
                for fut in cf.as_completed(futs):
                    done += 1
                    _collect(fut.result(), done)
        except Exception as exc:  # noqa: BLE001 - 受限沙箱：退化串行
            print("[mc] 并行不可用（%s）→ 串行重跑" % exc)
            rows, fails = [], 0
            _worker_init(cfg, mode, dt, debris_dt)
            for i, j in enumerate(jobs, 1):
                _collect(_run_one(j), i)
    else:
        _worker_init(cfg, mode, dt, debris_dt)
        for i, j in enumerate(jobs, 1):
            _collect(_run_one(j), i)
    rows.sort(key=lambda r: r["run_id"])

    if not rows:
        print("[mc] 全部失败")
        return 1
    keys = list(rows[0].keys())
    with open(os.path.join(out_dir, "mc_samples.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in keys})

    stats = dict(n=n, n_valid=len(rows), n_failed=fails, mode=mode, dt=dt, seed=seed,
                 jobs=jobs_n, elapsed_s=round(time.time() - t0, 1),
                 variations=mc["variations"])
    ev_keys = [k for k in keys if k.startswith("t_")]
    stats["events"] = {k: dict(mean=float(np.mean([r[k] for r in rows if k in r])),
                               std=float(np.std([r[k] for r in rows if k in r], ddof=1)))
                       for k in ev_keys if sum(1 for r in rows if k in r) > 2}
    for k in ("a_km", "e", "i_deg"):
        vals = [r[k] for r in rows if k in r]
        stats[k] = dict(mean=float(np.mean(vals)), std=float(np.std(vals, ddof=1)))
    # 落点统计（DRMS / 95% / 入区率）
    stats["impacts"] = {}
    zones = {z["id"]: np.asarray(z["polygon_latlon"], dtype=float) for z in cfg.get("notams", [])}
    # v3.1：统一映射（MC 样本里的对象名是**原始名**：一级/booster_1/fairing_L）
    _stage_names = [s.get("name", "") for s in cfg["vehicle"]["stages"]]
    obj_zone = []          # [(targets, zone_id)]
    for z in cfg.get("notams", []):
        hint = z.get("stage_hint", "")
        obj = z.get("object_hint", "")
        if not hint and not obj:
            continue
        targets = (N.object_hint_targets(obj, _stage_names) if obj
                   else N.hint_object_targets(hint, _stage_names))
        if targets:
            obj_zone.append((targets, z["id"]))

    def _zone_of(obj_name):
        for targets, zid in obj_zone:
            if N.object_name_matches(obj_name, targets):
                return zid
        return None
    for obj in sorted({k[4:] for k in keys if k.startswith("lat_")}):
        lats = np.array([r["lat_" + obj] for r in rows if "lat_" + obj in r])
        lons = np.array([r["lon_" + obj] for r in rows if "lon_" + obj in r])
        if len(lats) < 3:
            continue
        mlat, mlon = float(lats.mean()), float(lons.mean())
        dy = (lats - mlat) * 111.19
        dx = (lons - mlon) * 111.19 * math.cos(math.radians(mlat))
        d = np.hypot(dx, dy)
        zid = _zone_of(obj)
        inzone = None
        if zid and zid in zones:
            inzone = float(np.mean([N.point_in_polygon(la, lo, zones[zid]) for la, lo in zip(lats, lons)]))
        stats["impacts"][obj] = dict(mean_lat=mlat, mean_lon=mlon,
                                     sigma_n_km=float(dy.std(ddof=1)), sigma_e_km=float(dx.std(ddof=1)),
                                     drms_km=float(np.sqrt(np.mean(dx ** 2 + dy ** 2))),
                                     p95_km=float(np.percentile(d, 95)), in_zone_fraction=inzone,
                                     zone_id=zid)
    json.dump(stats, open(os.path.join(out_dir, "mc_stats.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("[mc] 有效 %d / 失败 %d（%.1fs，jobs=%d）" % (
        len(rows), fails, time.time() - t0, jobs_n))
    for obj, s in stats["impacts"].items():
        print("   %-10s DRMS=%.2f km  95%%=%.2f km  入区率=%s" % (
            obj, s["drms_km"], s["p95_km"],
            "%.0f%%" % (100 * s["in_zone_fraction"]) if s["in_zone_fraction"] is not None else "-"))
    print("[mc] → uncertainty/mc_samples.csv, uncertainty/mc_stats.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
