# -*- coding: utf-8 -*-
"""参数反演：差分进化（DE）搜索制导/箭型参数，使
  (1) 各残骸落点落入 config 中对应航警区（stage_hint 匹配，硬约束优先）；
  (2) 入轨高度/倾角接近 config.mission.reference_orbit（软约束）；
  (3) 其余（如一级关机时刻、抛罩时刻）在合理区间内。

配置（可选，缺省用内置默认）：
  "inversion": {
    "parameters": [{"name":"azimuth_deg","path":"guidance.azimuth_deg","bounds":[185,191]}, ...],
    "weights": {"zone_km": 10.0, "orbit_alt_km": 1.0, "orbit_i_deg": 20.0},
    "de": {"maxiter": 20, "popsize": 10, "seed": 42, "dt": 0.05}
  }
path 支持点号路径（如 vehicle.stages.0.pitch_end_deg）。

用法：
  python scripts/invert.py --config config.json            # 完整反演
  python scripts/invert.py --config config.json --quick    # 快速（小预算、粗步长）
输出：sim/反演参数.json、sim/nominal_params.json
"""
import sys, os, json, math, argparse, copy
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

#: 区内落点希望留出的边界裕度 [km]（低于它按 0.1×zone_km 计罚，鼓励"往区中间落"）
MARGIN_KM = 10.0
#: v3.3：**区外**罚项斜率。原来区外斜率取 1.0（罚 MARGIN + d），与"区内裕度奖励"同量级，
#: 于是出现了"一区落到很深处 + 另一区刚出区 3 km"比"两区都进区但裕度只有 3 km"得分更低
#: 的病态最优（实测 CZ-6C 一级/整流罩两区交叉约束时复现）。把区外斜率提到 3.0，
#: 在 d=0 处仍与区内分支连续（都等于 w·MARGIN），但"出区"会明显更差。
OUT_SLOPE = 3.0

DEFAULT_PARAMS = [
    {"name": "azimuth_deg", "path": "guidance.azimuth_deg", "bounds": [180.0, 196.0]},
    {"name": "t_v", "path": "guidance.t_v", "bounds": [4.0, 14.0]},
    {"name": "t_p", "path": "guidance.t_p", "bounds": [130.0, 210.0]},
    {"name": "pitch_power", "path": "guidance.pitch_power", "bounds": [0.8, 1.6]},
]


def get_path(cfg, path):
    cur = cfg
    for key in path.split("."):
        if isinstance(cur, list):
            cur = cur[int(key)]
        else:
            cur = cur[key]
    return cur


def set_path(cfg, path, value):
    keys = path.split(".")
    cur = cfg
    for key in keys[:-1]:
        cur = cur[int(key)] if isinstance(cur, list) else cur[key]
    last = keys[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value


def zone_objects(cfg):
    """把航警区与残骸对象配对 → ``[(zone, targets)]``。

    targets 来自 :func:`common.notam.hint_object_targets`（同时含"原始名"与"规范化名"，
    如 ``("一级", "core_stage")``）；``None`` 表示该区不管 hint、参与全部残骸判定。
    """
    stages = [s["name"] for s in cfg["vehicle"]["stages"]]
    pairs = []
    for z in cfg.get("notams", []):
        hint = z.get("stage_hint", "")
        obj = z.get("object_hint", "")
        if obj:
            # 具体对象优先（一对一；两条整流罩区各自约束自己那一半）
            pairs.append((z, N.object_hint_targets(obj, stages)))
        elif hint:
            pairs.append((z, N.hint_object_targets(hint, stages)))
    return pairs


def objective(cfg, params_vec, pnames, weights, ref_orbit, de_dt, mode="legacy"):
    cfg2 = copy.deepcopy(cfg)
    for name, val in zip(pnames, params_vec):
        for spec in cfg2["inversion"]["parameters"]:
            if spec["name"] == name:
                set_path(cfg2, spec["path"], float(val))
    veh = D.vehicle_from_config(cfg2)
    p = D.params_from_config(cfg2)
    D.apply_mode(p, mode, de_dt)
    if mode == "accurate":
        p.debris_dt = max(float(cfg.get("numerics", {}).get("debris_dt", 1.0)), 0.5)
    try:
        r = D.simulate(veh, p, collect_debris=True)
    except Exception:  # noqa: BLE001
        return 1e9
    if r["failures"] or not r["debris"]:
        return 1e9
    # 落区罚项
    penalty = 0.0
    for z, target in zone_objects(cfg2):
        poly = np.asarray(z["polygon_latlon"], dtype=float)
        cands = [d for d in r["debris"] if N.object_name_matches(d["obj"], target)]
        if not cands:
            # v3.1：匹配不到对象不是"无事发生"，而是该区**完全没被约束**——罚一个固定大值，
            # 让反演不会掉进"忽略该区"的局部最优；具体原因在主流程里打印 warning。
            penalty += 3000.0
            continue
        worst_out = None     # 区外候选中的最小距离（有任一条在区外就算"没落进区"）
        worst_in = None      # 区内候选中的最小裕度
        for d in cands:
            la, lo = d["impact"]["lat"], d["impact"]["lon"]
            if N.point_in_polygon(la, lo, poly):
                _in = float(N.distance_to_polygon_edge_km(la, lo, poly))
                worst_in = _in if worst_in is None else min(worst_in, _in)
            else:
                _out = float(N.distance_to_polygon_km(la, lo, poly))
                worst_out = _out if worst_out is None else min(worst_out, _out)
        # v3：等价于"有符号距离"的连续罚项——
        #   区外：w * (MARGIN + OUT_SLOPE * 到边界距离)（越远越差，鼓励进入）
        #   区内：w * max(0, MARGIN - 到边界距离)（离边界越远越好，>=MARGIN 为 0）
        # 两者在边界处连续（d=0 时都等于 w*MARGIN）。
        # v3.3：一个航警区可能匹配多个残骸对象（两半整流罩/多个助推器）——原来取"最好的那个"
        # 作裕度，于是"一半进区、另一半在区外"也能拿满分，但交付自检是逐对象判定的（会失败）。
        # 现在改为按**最差**的那一个计罚，与自检口径一致。
        if worst_out is not None:
            penalty += weights["zone_km"] * (MARGIN_KM + OUT_SLOPE * worst_out)
        elif worst_in is not None:
            penalty += weights["zone_km"] * max(0.0, MARGIN_KM - worst_in)
    # 轨道罚项
    o = r.get("orbit_seco")
    if o and ref_orbit:
        if ref_orbit.get("alt_km"):
            penalty += weights["orbit_alt_km"] * abs(o["a_km"] - (6378.137 + ref_orbit["alt_km"]))
        if ref_orbit.get("i_deg"):
            penalty += weights["orbit_i_deg"] * abs(o["i_deg"] - ref_orbit["i_deg"])
        # 圆度正则（v3.1）：近地/太阳同步任务的入轨应接近圆（e<0.02）。
        # 缺少这条时，若任务只有落区约束（参考轨道又是低置信推断值），反演会解出
        # e≈0.09 的椭圆"入轨"，交付自检的"入轨根数合理"会不通过。
        try:
            _alt = float(ref_orbit.get("alt_km") or 0.0)
            if 0.0 < _alt < 1500.0:
                penalty += float(weights.get("orbit_e", 100.0)) * max(
                    0.0, float(o.get("e") or 0.0) - 0.02)
        except (TypeError, ValueError):
            _alt = 0.0
        # v4：有效入轨护栏。
        # 落区罚项（20/km）远大于轨道罚项（1/km、倾角 20/°、圆度 100×Δe），
        # 当地面落区需要大机动才能满足时，DE 会收敛到"落区进区但近地点为负"的解
        # （实测自定义箭型任务出现 e=0.09、近地点 -135 km）。这里加一条与权重重无关的
        # 硬罚：近地点 <150 km 或 e>0.15 的解在物理上不算入轨，必须先被淘汰。
        try:
            _peri = o.get("perigee_km")
            if _peri is not None and float(_peri) < 150.0:
                penalty += 5000.0 + 20.0 * (150.0 - max(-2000.0, float(_peri)))
            if 0.0 < _alt < 1500.0 and float(o.get("e") or 0.0) > 0.15:
                penalty += 3000.0
        except (TypeError, ValueError):
            pass
    return float(penalty)


def _objective_ext(cfg, pnames, weights, ref_orbit, de_dt, mode, vec):
    """并行 DE 的目标函数入口（模块级，可 pickle）。"""
    return objective(cfg, vec, pnames, weights, ref_orbit, de_dt, mode)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--maxiter", type=int, default=None)
    ap.add_argument("--popsize", type=int, default=None)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--jobs", type=int, default=0,
                    help="并行进程数（0=自动取 CPU 核数，1=串行）；受限沙箱自动退化串行")
    ap.add_argument("--legacy", action="store_true", help="用 v1 口径（legacy 内核）")
    ap.add_argument("--accurate", action="store_true", help="用 v2 事件对齐口径（默认，见 config.numerics）")
    args = ap.parse_args()
    cfg = json.load(open(args.config, encoding="utf-8"))
    inv = cfg.setdefault("inversion", {})
    inv.setdefault("parameters", copy.deepcopy(DEFAULT_PARAMS))
    inv.setdefault("weights", {"zone_km": 10.0, "orbit_alt_km": 1.0, "orbit_i_deg": 20.0})
    inv.setdefault("de", {})
    ref_orbit = cfg.get("mission", {}).get("reference_orbit") or {}
    de = inv["de"]
    maxiter = args.maxiter or (8 if args.quick else int(de.get("maxiter", 20)))
    popsize = args.popsize or (6 if args.quick else int(de.get("popsize", 10)))
    de_dt = float(de.get("dt") or (0.2 if not args.legacy else 0.05))
    mode = "legacy" if args.legacy else str(cfg.get("numerics", {}).get("mode", "accurate"))
    jobs_n = args.jobs if args.jobs > 0 else min(8, os.cpu_count() or 1)
    specs = inv["parameters"]
    names = [s["name"] for s in specs]
    bounds = [tuple(s["bounds"]) for s in specs]
    print("[invert] 参数 %d 个：%s" % (len(names), names))
    # 预检"航警区 ↔ 残骸对象"配对（v3.1：不再静默丢弃任何有 hint 的区）
    _stage_names = [s.get("name", "") for s in cfg.get("vehicle", {}).get("stages", [])]
    for _z, _tg in zone_objects(cfg):
        if _tg == ():
            print("[invert] 警告：航警 %s 的残骸类型「%s」无法映射到任何级/整流罩/助推器，"
                  "该区不参与反演（请核对 stage_hint）" % (_z.get("id", "?"),
                                                          _z.get("stage_hint", "")))
        else:
            print("[invert] 航警 %s（%s）→ 对象前缀 %s" % (
                _z.get("id", "?"), _z.get("stage_hint", ""), list(_tg or ("<全部>",))))
    print("[invert] DE：maxiter=%d popsize=%d mode=%s dt=%.3f jobs=%d（每代 %d 次仿真）"
          % (maxiter, popsize, mode, de_dt, jobs_n, popsize))
    try:
        from scipy.optimize import differential_evolution
    except Exception as exc:  # noqa: BLE001
        print("[invert] 需要 scipy：", exc)
        return 2
    trace = []

    def fun(vec):
        val = objective(cfg, vec, names, inv["weights"], ref_orbit, de_dt, mode)
        trace.append(val)
        return val

    # ---- v3 第 0 阶段：方位角一维粗扫（落区拟合对航向最敏感，先把它定准）----
    seed_map = dict((inv.get("_plan") or {}).get("seed") or {})
    az_scan_note = ""
    if "azimuth_deg" in names and (inv.get("_plan") or {}).get("azimuth_scan", True) \
            and len(cfg.get("notams") or []) >= 1:
        try:
            import numpy as np
            i_az = names.index("azimuth_deg")
            lo_az, hi_az = bounds[i_az]
            # 扫描目标：只算落区罚项（临时把轨道权重清零），否则会被轨道软约束拉偏
            w_zone = {"zone_km": inv["weights"]["zone_km"], "orbit_alt_km": 0.0,
                      "orbit_i_deg": 0.0}
            cache = {}

            def f_az(a):
                key = round(float(a), 5)
                if key not in cache:
                    vec = [float(seed_map.get(nm, 0.5 * (b[0] + b[1])))
                           for nm, b in zip(names, bounds)]
                    vec = [min(max(seed_map.get(nm, 0.5 * (b[0] + b[1])), b[0]), b[1])
                           for nm, b in zip(names, bounds)]
                    vec[i_az] = float(a)
                    cache[key] = objective(cfg, vec, names, w_zone, None, de_dt, mode)
                return cache[key]

            g = (5.0 ** 0.5 - 1.0) / 2.0
            a, b = float(lo_az), float(hi_az)
            c = b - g * (b - a)
            d = a + g * (b - a)
            fc, fd = f_az(c), f_az(d)
            for _ in range(9):
                if fc < fd:
                    b, d, fd = d, c, fc
                    c = b - g * (b - a)
                    fc = f_az(c)
                else:
                    a, c, fc = c, d, fd
                    d = a + g * (b - a)
                    fd = f_az(d)
            az_best = 0.5 * (a + b)
            az_val = f_az(az_best)
            az0 = float(seed_map.get("azimuth_deg", 0.5 * (lo_az + hi_az)))
            if az_val < f_az(az0):
                seed_map["azimuth_deg"] = az_best
                # 收窄方位角边界到 ±2°，把 DE 的搜索自由度让给其它参数
                bounds[i_az] = (max(lo_az, az_best - 2.0), min(hi_az, az_best + 2.0))
                az_scan_note = ("方位角粗扫：%.2f° → %.3f°（落区罚项 %.2f→%.2f，%d 次仿真）" %
                                (az0, az_best, f_az(az0), az_val, len(cache)))
            else:
                az_scan_note = ("方位角粗扫：初值 %.2f° 已最优（罚项 %.2f，%d 次仿真）" %
                                (az0, f_az(az0), len(cache)))
            print("[invert] " + az_scan_note)
        except Exception as exc:  # noqa: BLE001
            print("[invert] 方位角粗扫不可用（%s），继续 DE" % exc)

    # ---- v3：用当前 config 的参数值作为 DE 初始种群中心（自动反演的关键提速）----
    init_arr = None
    seed_map = dict((inv.get("_plan") or {}).get("seed") or {})
    if seed_map:
        try:
            import numpy as np
            rng = np.random.default_rng(int(args.seed))
            lo = np.array([b[0] for b in bounds], dtype=float)
            hi = np.array([b[1] for b in bounds], dtype=float)
            n_par = len(names)
            total = int(popsize) * n_par
            center = np.array([float(seed_map.get(nm, 0.5 * (l + h)))
                               for nm, l, h in zip(names, lo, hi)], dtype=float)
            center = np.clip(center, lo, hi)
            init_arr = np.empty((total, n_par), dtype=float)
            init_arr[0] = center
            span = hi - lo
            n_local = max(1, int(total * 0.65))
            init_arr[1:n_local] = np.clip(
                center[None, :] + rng.normal(0.0, 0.06, size=(n_local - 1, n_par)) * span[None, :],
                lo[None, :], hi[None, :])
            init_arr[n_local:] = rng.uniform(lo[None, :], hi[None, :],
                                             size=(total - n_local, n_par))
            print("[invert] 用当前参数作为初始种群中心（seed）：%s" % json.dumps(
                seed_map, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            print("[invert] 初始种群构造失败，退回随机初始化：%s" % exc)
            init_arr = None

    # 注意：不要开 polish（L-BFGS-B 对"入区/出区"这种分片目标会做上千次数值梯度评估，
    # 实测一次反演因此从 ~60 s 涨到 ~320 s）；局部精修交给后面的 Powell（有 maxfev 上限）。
    de_kw = dict(seed=args.seed, tol=1e-3, disp=True, polish=False)
    if init_arr is not None:
        de_kw["init"] = init_arr

    # 并行：scipy 需要可 pickle 的目标函数 → 模块级函数 + partial
    res = None
    if jobs_n > 1:
        try:
            import functools
            f_par = functools.partial(_objective_ext, cfg, names, inv["weights"],
                                      ref_orbit, de_dt, mode)
            res = differential_evolution(f_par, bounds, maxiter=maxiter, popsize=popsize,
                                         workers=jobs_n, updating="deferred", **de_kw)
        except Exception as exc:  # noqa: BLE001 - 受限沙箱：退化串行
            print("[invert] 并行不可用（%s）→ 串行重跑" % exc)
            res = None
    if res is None:
        res = differential_evolution(fun, bounds, maxiter=maxiter, popsize=popsize, **de_kw)
    best_vec = [float(v) for v in res.x]
    best_val = float(res.fun)
    print("[invert] 差分进化最优目标 %.4f；参数：%s" % (
        best_val, json.dumps(dict(zip(names, best_vec)), ensure_ascii=False)))

    # v3：再跑一次带边界的局部精修（Powell 对"入区/出区"这类分片目标更稳）
    if best_val > 1e-9 and str(inv.get("de", {}).get("local_refine", True)).lower() not in ("0", "false"):
        try:
            from scipy.optimize import minimize
            r2 = minimize(fun, best_vec, method="Powell", bounds=bounds,
                          options={"maxfev": 300, "xtol": 1e-3, "ftol": 1e-3})
            if float(r2.fun) <= best_val:
                print("[invert] 局部精修：目标 %.4f → %.4f" % (best_val, float(r2.fun)))
                best_vec, best_val = [float(v) for v in r2.x], float(r2.fun)
        except Exception as exc:  # noqa: BLE001
            print("[invert] 局部精修不可用（%s）" % exc)
    # ---- v3 第 3 阶段：方位角"可行性修复"（只调方位角，目标只算落区罚项）----
    if "azimuth_deg" in names and len(cfg.get("notams") or []) >= 1 and best_val > 1e-9:
        try:
            import numpy as np
            i_az = names.index("azimuth_deg")
            lo_az, hi_az = bounds[i_az]
            w_zone = {"zone_km": inv["weights"]["zone_km"], "orbit_alt_km": 0.0,
                      "orbit_i_deg": 0.0}

            def f_az2(a):
                vec = list(best_vec)
                vec[i_az] = float(a)
                return objective(cfg, vec, names, w_zone, None, de_dt, mode)

            # 在当前最优点 ±1.2° 内做黄金分割（落区罚项随方位角近似单峰）
            g = (5.0 ** 0.5 - 1.0) / 2.0
            a = max(lo_az, best_vec[i_az] - 1.2)
            b = min(hi_az, best_vec[i_az] + 1.2)
            c = b - g * (b - a)
            d = a + g * (b - a)
            fc, fd = f_az2(c), f_az2(d)
            for _ in range(8):
                if fc < fd:
                    b, d, fd = d, c, fc
                    c = b - g * (b - a)
                    fc = f_az2(c)
                else:
                    a, c, fc = c, d, fd
                    d = a + g * (b - a)
                    fd = f_az2(d)
            az_fix = 0.5 * (a + b)
            vec2 = list(best_vec)
            vec2[i_az] = az_fix
            val2 = objective(cfg, vec2, names, inv["weights"], ref_orbit, de_dt, mode)
            if val2 < best_val:
                print("[invert] 方位角修复：%.3f° → %.3f°（总目标 %.4f→%.4f）" % (
                    best_vec[i_az], az_fix, best_val, val2))
                best_vec, best_val = vec2, float(val2)
        except Exception as exc:  # noqa: BLE001
            print("[invert] 方位角修复不可用（%s）" % exc)

    # ---- v3 第 4 阶段：坐标网格精修（解决"贴着危险区边界 0.1 km 停住"的问题）----
    if (inv.get("_plan") or {}).get("grid_polish", True) and best_val > 1e-9:
        try:
            def _try(i_par, offsets):
                nonlocal best_vec, best_val
                for off in offsets:
                    vec = list(best_vec)
                    b0 = bounds[i_par]
                    vec[i_par] = min(max(vec[i_par] + off, b0[0]), b0[1])
                    if vec[i_par] == best_vec[i_par]:
                        continue
                    val = objective(cfg, vec, names, inv["weights"], ref_orbit, de_dt, mode)
                    if val < best_val - 1e-9:
                        best_vec, best_val = vec, float(val)
                        return True
                return False

            # 方位角：±0.4° 内 0.05° 步长（落区横向最敏感）
            if "azimuth_deg" in names:
                i_par = names.index("azimuth_deg")
                offs = [d * 0.05 for d in (1, -1, 2, -2, 3, -3, 4, -4, 5, -5, 6, -6, 7, -7, 8, -8)]
                for _ in range(3):
                    if not _try(i_par, offs):
                        break
            # 俯仰幂次：±0.04 内 0.01 步长（纵向射程调节）
            if "pitch_power" in names:
                i_par = names.index("pitch_power")
                offs = [d * 0.01 for d in (1, -1, 2, -2, 3, -3, 4, -4)]
                for _ in range(2):
                    if not _try(i_par, offs):
                        break
            if "t_v" in names:
                i_par = names.index("t_v")
                offs = [d * 0.1 for d in (1, -1, 2, -2, 3, -3)]
                for _ in range(2):
                    if not _try(i_par, offs):
                        break
            print("[invert] 网格精修后目标 %.4f" % best_val)
        except Exception as exc:  # noqa: BLE001
            print("[invert] 网格精修不可用（%s）" % exc)

    best = dict(zip(names, best_vec))
    print("[invert] 最终目标 %.4f；参数：%s" % (best_val, json.dumps(best, ensure_ascii=False)))

    # 用最优参数以正式步长复算，写结果
    cfg2 = copy.deepcopy(cfg)
    for spec in specs:
        set_path(cfg2, spec["path"], best[spec["name"]])
    veh = D.vehicle_from_config(cfg2)
    p = D.params_from_config(cfg2)
    D.apply_mode(p, mode)
    r = D.simulate(veh, p, collect_debris=True)
    sim_dir = os.path.join(ROOT, "sim")
    os.makedirs(sim_dir, exist_ok=True)
    o = r.get("orbit_seco") or {}
    # v3.2 收敛守卫：与"标称参数（config 原值）"的目标值比较，判断反演是否真的改善了
    nominal_val = None
    try:
        nominal_val = float(objective(cfg, [float(get_path(cfg, s["path"])) for s in specs],
                                      names, inv["weights"], ref_orbit, de_dt, mode))
    except Exception:  # noqa: BLE001
        nominal_val = None
    _ref = ref_orbit or {}
    _ref_low = str(_ref.get("confidence", "")).startswith("低")
    converged = bool(best_val <= 200.0 or (nominal_val is not None
                                          and best_val <= 0.6 * nominal_val))
    print("[invert] 标称目标 %.3f → 最优目标 %.3f；收敛判定=%s%s" % (
        nominal_val if nominal_val is not None else float("nan"), best_val,
        "是" if converged else "否（落区约束可能与本任务不匹配）",
        "；参考轨道为低置信推断值" if _ref_low else ""))
    out = dict(parameters=best, objective=float(best_val),
               nominal_objective=(float(nominal_val) if nominal_val is not None else None),
               converged=converged,
               bounds={s["name"]: s["bounds"] for s in specs},
               nfev=int(getattr(res, "nfev", len(trace))), iterations=int(res.nit),
               de=dict(maxiter=maxiter, popsize=popsize, seed=args.seed, dt=de_dt),
               config_mtime=os.path.getmtime(args.config))
    json.dump(out, open(os.path.join(sim_dir, "反演参数.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    flat = dict(azimuth_deg=p.azimuth_deg, t_v=p.t_v, t_p=p.t_p, pitch_power=p.pitch_power,
                t_fairing=p.t_fairing, dt=p.dt,
                orbit=dict(a_km=o.get("a_km"), e=o.get("e"), i_deg=o.get("i_deg")))
    json.dump(flat, open(os.path.join(sim_dir, "nominal_params.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("[invert] 写入 sim/反演参数.json / sim/nominal_params.json")
    for e in r["events"]:
        print("   %-24s T+%8.2f s  alt=%8.1f km  v=%7.1f m/s" % (
            e["event"], e["t_met_s"], e["alt_m"] / 1000.0, e["speed_m_s"]))
    for d in r["debris"]:
        imp = d["impact"]
        print("   落点 %-10s (%.5f, %.5f)  T+%.1f s" % (d["obj"], imp["lat"], imp["lon"], imp["t_met_s"]))
    return 0 if best_val < 1e6 else 1


if __name__ == "__main__":
    sys.exit(main())
