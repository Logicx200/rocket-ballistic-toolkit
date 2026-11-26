# -*- coding: utf-8 -*-
"""三任务回归基准（docs-qa 独立验收，可选任务 C）。

用引擎 export 脚本重跑 3 个历史任务的 config，与历史工作区的 sim 产物对比：
  * sim/入轨根数.json：a_km / e / i_deg / perigee_km / apogee_km / period_s
  * sim/落点与射程.csv：各残骸落点经纬度（折合大圆距离 km）与落点时刻

历史参考：
  CZ-2D  F:\\DeepSeek工作区\\火箭弹道工具箱_v2.0\\config.json      → 示例运行\\sim
  CZ-2F  F:\\DeepSeek工作区\\弹道仿真工作区\\2026.11.25 CZ2-F\\config.json
  CZ-6A  F:\\DeepSeek工作区\\弹道仿真工作区\\2026.11.27 CZ6-A\\config.json

判定：轨道根数相对偏差 <0.1% 通过（0.1~1% 警告，>1% 失败）；
      落点偏差 <0.05 km 通过。失败时返回非零退出码。

用法： python tests\\qa\\regression_3missions.py
产物： tests\\qa\\runs\\reg_<任务>\\sim\\... 与 tests\\qa\\回归基准_3任务.json
"""
from __future__ import annotations

import csv
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PY = sys.executable
RUNS = HERE / "runs"
OUT_JSON = HERE / "回归基准_3任务.json"

EXT = Path(r"F:\DeepSeek工作区")
CASES = [
    {
        "name": "CZ-2D_太原",
        "config": Path(r"F:\DeepSeek工作区\火箭弹道工具箱_v2.0\config.json"),
        "reference": Path(r"F:\DeepSeek工作区\火箭弹道工具箱_v2.0\示例运行\sim"),
    },
    {
        "name": "CZ-2F_酒泉",
        "config": EXT / "弹道仿真工作区" / "2026.11.25 CZ2-F" / "config.json",
        "reference": EXT / "弹道仿真工作区" / "2026.11.25 CZ2-F" / "sim",
    },
    {
        "name": "CZ-6A_太原",
        "config": EXT / "弹道仿真工作区" / "2026.11.27 CZ6-A" / "config.json",
        "reference": EXT / "弹道仿真工作区" / "2026.11.27 CZ6-A" / "sim",
    },
]

ORBIT_KEYS = ("a_km", "e", "i_deg", "perigee_km", "apogee_km", "period_s")


def _haversine_km(lat1, lon1, lat2, lon2):
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2)
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))


def _load_orbit(sim: Path):
    p = sim / "入轨根数.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _load_impacts(sim: Path):
    p = sim / "落点与射程.csv"
    if not p.exists():
        return {}
    out = {}
    with open(p, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                out[row.get("object", "?")] = (
                    float(row["lat_deg"]), float(row["lon_deg"]),
                    float(row.get("impact_t_met_s") or "nan"))
            except (KeyError, ValueError):
                continue
    return out


def _rel_diff(new, old):
    try:
        new, old = float(new), float(old)
    except (TypeError, ValueError):
        return None
    if old == 0:
        return 0.0 if new == 0 else None
    return abs(new - old) / abs(old)


def run_case(case: dict) -> dict:
    wd = RUNS / ("reg_" + case["name"])
    if wd.exists():
        shutil.rmtree(wd, ignore_errors=True)
    wd.mkdir(parents=True, exist_ok=True)
    cfg = wd / "config.json"
    if not case["config"].exists():
        return {"name": case["name"], "ok": False, "error": "缺少历史 config: %s" % case["config"]}
    shutil.copy2(case["config"], cfg)
    env = dict(os.environ)
    env.update({
        "RK_ENGINE": str(ROOT / "engine"),
        "RK_ROOT": str(wd),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "MPLBACKEND": "Agg",
    })
    proc = subprocess.run(
        [PY, str(ROOT / "engine" / "scripts" / "export.py"), "--config", str(cfg), "--accurate"],
        cwd=str(wd), env=env, capture_output=True)
    log = (proc.stdout or b"").decode("utf-8", "replace") + \
          (proc.stderr or b"").decode("utf-8", "replace")
    (wd / "export_stdout.txt").write_text(log, encoding="utf-8")
    if proc.returncode != 0:
        return {"name": case["name"], "ok": False, "error": "export 退出码 %s" % proc.returncode,
                "log_tail": log[-800:]}

    new_orb = _load_orbit(wd / "sim")
    ref_orb = _load_orbit(case["reference"])
    new_imp = _load_impacts(wd / "sim")
    ref_imp = _load_impacts(case["reference"])

    orbit_rows = []
    worst_rel = 0.0
    for k in ORBIT_KEYS:
        rel = _rel_diff(new_orb.get(k), ref_orb.get(k))
        if rel is not None:
            worst_rel = max(worst_rel, rel)
        orbit_rows.append({"field": k, "new": new_orb.get(k), "reference": ref_orb.get(k),
                           "rel_diff": rel})

    impact_rows = []
    worst_km = 0.0
    for obj, (la, lo, t) in sorted(new_imp.items()):
        if obj not in ref_imp:
            continue
        rla, rlo, rt = ref_imp[obj]
        d = _haversine_km(la, lo, rla, rlo)
        worst_km = max(worst_km, d)
        impact_rows.append({"object": obj, "new_lat": la, "new_lon": lo,
                            "ref_lat": rla, "ref_lon": rlo,
                            "distance_km": round(d, 4),
                            "dt_s": round(t - rt, 3)})

    if worst_rel > 0.01 or worst_km > 1.0:
        level = "FAIL"
    elif worst_rel > 0.001 or worst_km > 0.05:
        level = "WARN"
    else:
        level = "PASS"
    return {
        "name": case["name"],
        "ok": level != "FAIL",
        "level": level,
        "worst_orbit_rel_diff": worst_rel,
        "worst_impact_km": round(worst_km, 4),
        "orbit": orbit_rows,
        "impacts": impact_rows,
    }


def main() -> int:
    RUNS.mkdir(parents=True, exist_ok=True)
    results = []
    for i, case in enumerate(CASES, 1):
        print("step %d/%d: %s ..." % (i, len(CASES), case["name"]), flush=True)
        r = run_case(case)
        results.append(r)
        if r.get("error"):
            print("  ERROR", r["error"], flush=True)
        else:
            print("  %s  轨道最大相对偏差 %.3g  落点最大偏差 %.4f km"
                  % (r["level"], r["worst_orbit_rel_diff"], r["worst_impact_km"]), flush=True)
    OUT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n==== 三任务回归基准（历史参考产物对比）====")
    print("%-12s %-6s %-18s %-16s" % ("任务", "判定", "轨道最大相对偏差", "落点最大偏差km"))
    for r in results:
        if r.get("error"):
            print("%-12s %-6s %s" % (r["name"], "ERROR", r["error"]))
        else:
            print("%-12s %-6s %-18.3g %-16.4f" % (
                r["name"], r["level"], r["worst_orbit_rel_diff"], r["worst_impact_km"]))
    print("明细 -> %s" % OUT_JSON)
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
