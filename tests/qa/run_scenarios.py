# -*- coding: utf-8 -*-
"""场景 1-7 独立验收批量脚本（docs-qa）。

在项目根目录（含 app\\ 与 engine\\）运行:
    python tests\\qa\\run_scenarios.py            # 全部场景
    python tests\\qa\\run_scenarios.py s2 s3b     # 只跑指定场景

产物: tests\\qa\\runs\\<场景>\\ 与 tests\\qa\\runs\\<场景>_stdout.txt、
      tests\\qa\\runs\\_scenario_summary.csv
"""
from __future__ import annotations

import csv
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]                      # 项目根
PY = sys.executable
RUNS = HERE / "runs"
MISSIONS = HERE / "missions"

CASES = [
    # name, mission json or None, extra argv, demo flag
    ("s1_quick", None, ["--demo", "cz2d", "--modules", "export,report"], True),
    ("s2_cz6a", "s2_cz6a_notam_no_t0.json", [], False),
    ("s3a_no_t0", "s3a_no_notam_no_t0.json", [], False),
    ("s3b_predict", "s3b_predict_zones.json", [], False),
    ("s4_city_obs", "s4_observer_city_only.json", [], False),
    ("s5_no_rocket", "s5_no_rocket.json", [], False),
    ("s6a_bad_site", "s6a_bad_site_lat.json", [], False),
    ("s6a2_bad_obs", "s6a2_bad_observer_lat.json", [], False),
    ("s6b_2vertex", "s6b_two_vertex_polygon.json", [], False),
    ("s6c_bad_t0", "s6c_bad_t0.json", [], False),
    ("s7_cli_modules", "s7_modules_cli_bug.json", ["--modules", "export"], False),
]


def run_one(name: str, mission, extra, demo: bool) -> dict:
    workdir = RUNS / name
    if workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    argv = [PY, "-m", "app.main", "--cli"]
    if demo:
        argv += extra
        argv += ["--workdir", str(workdir)]
    else:
        argv += ["--mission", str(MISSIONS / mission), "--workdir", str(workdir)] + extra
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    t0 = time.time()
    proc = subprocess.run(argv, cwd=str(ROOT), env=env, capture_output=True)
    secs = time.time() - t0
    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    (RUNS / ("%s_stdout.txt" % name)).write_text(out + "\n[STDERR]\n" + err, encoding="utf-8")
    # 提取关键结论行
    keys = []
    for line in out.splitlines():
        s = line.strip()
        if (s.startswith("[错误]") or "[失败]" in s or "[OK  ]" in s
                or s.startswith("入轨：") or s.startswith("残骸落点：") or "预测落区" in s
                or "QC " in s or "全部完成" in s):
            keys.append(s)
    traceback_leak = "Traceback (most recent call last)" in (out + err)
    return {
        "scenario": name,
        "exit": proc.returncode,
        "seconds": round(secs, 1),
        "traceback": traceback_leak,
        "workdir": str(workdir.relative_to(ROOT)),
        "key_lines": " | ".join(keys[-6:]),
    }


def main() -> int:
    want = set(sys.argv[1:])
    RUNS.mkdir(parents=True, exist_ok=True)
    todo = [c for c in CASES if not want or c[0] in want]
    rows = []
    for i, case in enumerate(todo, 1):
        name = case[0]
        print("step %d/%d: %s ..." % (i, len(todo), name), flush=True)
        row = run_one(*case)
        rows.append(row)
        print("step %d/%d done: exit=%s %.1fs traceback=%s"
              % (i, len(todo), row["exit"], row["seconds"], row["traceback"]), flush=True)
        print("    " + row["key_lines"][:600], flush=True)
    path = RUNS / "_scenario_summary.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print("\n==== summary -> %s ====" % path)
    for r in rows:
        print("%-16s exit=%-2s %6.1fs traceback=%s" % (
            r["scenario"], r["exit"], r["seconds"], r["traceback"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
