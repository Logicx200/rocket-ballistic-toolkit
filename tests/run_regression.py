# -*- coding: utf-8 -*-
"""回归测试：确保 v3.0（引擎解耦 + 参数化改脚本 + 自动补全）没有改变数值结果。

两部分
------
1. **引擎回归**（严格）：把 3 个历史任务的 config.json 原样放进工作区，只跑
   ``engine/scripts/export.py``，与历史 sim 产物逐项比较（轨迹/事件/落点/入轨根数）。
   这一步验证 RK_ENGINE/RK_ROOT 分离、路径补丁、qc 补丁没有改变物理数值。
2. **应用回归**（宽松）：用 ``python -m app.main --cli --mission <历史 config>`` 走
   v3 的"配置反推 + 自动补全"路径，比较关键量（T0、落点、入轨）差异是否在预期内。

用法::

    python tests\run_regression.py            # 全部
    python tests\run_regression.py --strict    # 有偏差即非 0 退出
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
BASE = os.path.join(HERE, "_regression")

#: 3 个历史任务（config 路径 + 历史 sim 参考目录）
MISSIONS = [
    dict(key="cz2d", name="CZ-2D 太原 LC-9",
         config=r"F:\DeepSeek工作区\火箭弹道工具箱_v2.0\config.json",
         ref=r"F:\DeepSeek工作区\火箭弹道工具箱_v2.0\示例运行\sim"),
    dict(key="cz2f", name="CZ-2F/G 酒泉 921",
         config=r"F:\DeepSeek工作区\弹道仿真工作区\2026.11.25 CZ2-F\config.json",
         ref=r"F:\DeepSeek工作区\弹道仿真工作区\2026.11.25 CZ2-F\sim"),
    dict(key="cz6a", name="CZ-6A 太原 LC-9A",
         config=r"F:\DeepSeek工作区\弹道仿真工作区\2026.11.27 CZ6-A\config.json",
         ref=r"F:\DeepSeek工作区\弹道仿真工作区\2026.11.27 CZ6-A\sim"),
]

#: 引擎回归允许的最大偏差（相对/绝对）
TOL = dict(orbit_a_km_rel=1e-9, orbit_i_deg_abs=1e-9, impact_km_rel=1e-6,
           event_t_s_abs=1e-6, impact_t_s_abs=1e-6)


def _run(cmd, cwd, env_extra=None, timeout=600):
    env = dict(os.environ)
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "MPLBACKEND": "Agg",
                "RK_ENGINE": os.path.join(ROOT, "engine")})
    if env_extra:
        env.update(env_extra)
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return p


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _read_csv(path):
    import csv
    try:
        with open(path, encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    except Exception:  # noqa: BLE001
        return []


def _rel(a, b):
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return None
    d = abs(a - b)
    if max(abs(a), abs(b)) < 1e-12:
        return 0.0
    return d / max(abs(a), abs(b))


def engine_regression(m) -> dict:
    """原样 config 跑 export，与历史产物对比。"""
    out = dict(case=m["key"], mode="engine", ok=True, checks=[], error="")
    wd = os.path.join(BASE, m["key"] + "_engine")
    if os.path.isdir(wd):
        shutil.rmtree(wd)
    os.makedirs(wd, exist_ok=True)
    cfg = _read_json(m["config"], {})
    if not cfg:
        out.update(ok=False, error="历史 config 不存在：%s" % m["config"])
        return out
    with open(os.path.join(wd, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    p = _run([PY, os.path.join(ROOT, "engine", "scripts", "export.py"),
              "--config", os.path.join(wd, "config.json")], cwd=wd,
             env_extra={"RK_ROOT": wd})
    if p.returncode != 0:
        out.update(ok=False, error="export 失败：%s" % (p.stderr or p.stdout)[-800:])
        return out
    ref = m["ref"]
    # 1) 入轨根数
    a_new = _read_json(os.path.join(wd, "sim", "入轨根数.json"), {})
    a_ref = _read_json(os.path.join(ref, "入轨根数.json"), {})
    for key, kind in (("a_km", "rel"), ("e", "rel"), ("i_deg", "abs"), ("period_s", "rel")):
        got, want = a_new.get(key), a_ref.get(key)
        d = _rel(got, want) if kind == "rel" else (abs(float(got) - float(want))
                                                  if got is not None and want is not None else None)
        ok = d is not None and d <= (TOL["orbit_a_km_rel"] if kind == "rel"
                                     else TOL["orbit_i_deg_abs"])
        out["checks"].append(dict(item="入轨 %s" % key, got=got, want=want, diff=d, ok=ok))
    # 2) 事件表
    ev_new = {r["event"]: r for r in _read_csv(os.path.join(wd, "sim", "弹道事件表.csv"))}
    ev_ref = {r["event"]: r for r in _read_csv(os.path.join(ref, "弹道事件表.csv"))}
    for name, r0 in ev_ref.items():
        r1 = ev_new.get(name)
        d = _rel(r1.get("t_met_s") if r1 else None, r0.get("t_met_s"))
        out["checks"].append(dict(item="事件 %s t_met" % name,
                                  got=(r1 or {}).get("t_met_s"), want=r0.get("t_met_s"),
                                  diff=d, ok=(d is not None and d <= TOL["event_t_s_abs"])))
    # 3) 落点
    imp_new = {r["object"]: r for r in _read_csv(os.path.join(wd, "sim", "落点与射程.csv"))}
    imp_ref = {r["object"]: r for r in _read_csv(os.path.join(ref, "落点与射程.csv"))}
    for obj, r0 in imp_ref.items():
        r1 = imp_new.get(obj)
        if not r1:
            out["checks"].append(dict(item="落点 %s" % obj, got="缺失", want="存在", diff=None,
                                      ok=False))
            continue
        for fld in ("lat_deg", "lon_deg", "impact_t_met_s"):
            d = _rel(r1.get(fld), r0.get(fld))
            ok = d is not None and d <= (TOL["impact_t_s_abs"] if "t_" in fld
                                         else TOL["impact_km_rel"])
            out["checks"].append(dict(item="落点 %s %s" % (obj, fld), got=r1.get(fld),
                                      want=r0.get(fld), diff=d, ok=ok))
    out["ok"] = all(c["ok"] for c in out["checks"]) and bool(out["checks"])
    return out


def app_regression(m) -> dict:
    """v3 应用路径（config → MissionInput → 自动补全 → export），宽松比较。"""
    out = dict(case=m["key"], mode="app", ok=True, checks=[], error="")
    wd = os.path.join(BASE, m["key"] + "_app")
    if os.path.isdir(wd):
        shutil.rmtree(wd)
    os.makedirs(wd, exist_ok=True)
    env_extra = {"RK_APP_DATA": os.path.join(HERE, "fixtures", "db")}
    p = _run([PY, "-m", "app.main", "--cli", "--mission", m["config"], "--workdir", wd,
              "--modules", "export", "--no-invert"], cwd=ROOT, env_extra=env_extra,
             timeout=300)
    if p.returncode != 0:
        out.update(ok=False, error="app 路径失败：%s" % (p.stderr or p.stdout)[-1200:])
        return out
    ref = m["ref"]
    a_new = _read_json(os.path.join(wd, "sim", "入轨根数.json"), {})
    a_ref = _read_json(os.path.join(ref, "入轨根数.json"), {})
    for key in ("a_km", "i_deg"):
        got, want = a_new.get(key), a_ref.get(key)
        d = _rel(got, want)
        # 无反演路径（自动补全初值直接仿真）只做量级校验：轨道半长轴允许 5% 偏差，
        # 真正的精度由"自动反演"保证（引擎回归部分是逐位严格比较）。
        out["checks"].append(dict(item="入轨 %s" % key, got=got, want=want, diff=d,
                                  ok=(d is not None and d <= 5e-2)))
    imp_new = {r["object"]: r for r in _read_csv(os.path.join(wd, "sim", "落点与射程.csv"))}
    imp_ref = {r["object"]: r for r in _read_csv(os.path.join(ref, "落点与射程.csv"))}
    for obj, r0 in imp_ref.items():
        r1 = imp_new.get(obj)
        if not r1:
            out["checks"].append(dict(item="落点 %s 缺失" % obj, got="缺失", want="存在",
                                      diff=None, ok=False))
            continue
        import math
        dlat = float(r1["lat_deg"]) - float(r0["lat_deg"])
        dlon = float(r1["lon_deg"]) - float(r0["lon_deg"])
        km = math.hypot(dlat * 111.195, dlon * 111.195 * math.cos(math.radians(
            float(r0["lat_deg"]))))
        out["checks"].append(dict(item="落点 %s 偏差(km)" % obj, got=round(km, 2), want="≤100",
                                  diff=km, ok=km <= 100.0))
    # T0 是否被自动推断出来
    cfg_new = _read_json(os.path.join(wd, "config.json"), {})
    t0_new = (cfg_new.get("mission") or {}).get("t0_utc")
    t0_ref = (_read_json(m["config"], {}).get("mission") or {}).get("t0_utc")
    out["checks"].append(dict(item="T0", got=t0_new, want=t0_ref,
                              diff=_rel(t0_new, t0_ref), ok=bool(t0_new)))
    out["ok"] = all(c["ok"] for c in out["checks"]) and bool(out["checks"])
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--only", help="只跑某个 case key")
    args = ap.parse_args(argv)
    os.makedirs(BASE, exist_ok=True)
    results = []
    for m in MISSIONS:
        if args.only and m["key"] != args.only:
            continue
        print("== %s（%s）引擎回归 ==" % (m["key"], m["name"]))
        r1 = engine_regression(m)
        _print(r1)
        print("== %s（%s）应用回归 ==" % (m["key"], m["name"]))
        r2 = app_regression(m)
        _print(r2)
        results.extend([r1, r2])
    path = os.path.join(BASE, "regression_summary.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    n_ok = sum(1 for r in results if r["ok"])
    print("\n==== 回归 %d/%d 通过 → %s ====" % (n_ok, len(results), path))
    bad = [r for r in results if not r["ok"]]
    if bad and args.strict:
        return 1
    return 0


def _print(r):
    if r.get("error"):
        print("  [失败] %s" % r["error"].strip().splitlines()[-1][:200])
    for c in r["checks"]:
        mark = "OK  " if c["ok"] else "FAIL"
        print("  [%s] %-34s got=%s want=%s diff=%s" % (
            mark, c["item"], c.get("got"), c.get("want"),
            ("%.2e" % c["diff"]) if isinstance(c["diff"], float) else c["diff"]))
    print("  => %s" % ("通过" if r["ok"] else "未通过"))


if __name__ == "__main__":
    sys.exit(main())
