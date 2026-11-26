# -*- coding: utf-8 -*-
"""数据导出：把 simulate() 结果写成下游模块（天空轨迹/全国地图/KMZ/报告）共同依赖的数据契约。

输入：config.json（任务/箭型/航警/观测点）+ simulate() 结果
输出（全部在 <工作区>/sim/ 下）：
  trajectory.csv（0.5 s）、debris_trajectories.csv（1 s）、弹道事件表.csv、
  落点与射程.csv、入轨根数.json、orbit_track.csv（入轨后 1.5 圈）、
  objects.json、notam_zones.json、trajectory_meta.json

用法：
  python scripts/export.py --config config.json
"""
import sys, os, json, math, argparse, csv
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
from datetime import datetime, timedelta, timezone
import dynamics as D
from common import geo, notam as N

MU = D.MU
RE = D.RE


# ---- 命名规范化：通用模型的"级名"事件/残骸对象 → 下游脚本沿用的规范名 ----
def canonical_event(name: str, cfg: dict) -> str:
    names = [x["name"] for x in cfg["vehicle"]["stages"]]
    if name.endswith("关机/分离"):
        base = name[: -len("关机/分离")]
        if base == names[0] and len(names) == 2:
            return "一级关机/一二级分离"
    return name


def canonical_object(obj: str, cfg: dict) -> str:
    names = [x["name"] for x in cfg["vehicle"]["stages"]]
    for i, nm in enumerate(names[:-1]):
        if obj == nm:
            return "core_stage" if i == 0 else "stage_%d" % i
    return obj


def utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dt_from_met(t0, t_met):
    return t0 + timedelta(seconds=float(t_met))


def enu_from_site(lat0, lon0, alt0, lat, lon, alt):
    """目标点相对发射场 ENU（m）。"""
    r0 = D.geodetic_to_ecef(lat0, lon0, alt0)
    r1 = D.geodetic_to_ecef(lat, lon, alt)
    latr, lonr = math.radians(lat0), math.radians(lon0)
    dr = (r1[0] - r0[0], r1[1] - r0[1], r1[2] - r0[2])
    sl, cl = math.sin(latr), math.cos(latr)
    so, co = math.sin(lonr), math.cos(lonr)
    e = -so * dr[0] + co * dr[1]
    n = -sl * co * dr[0] - sl * so * dr[1] + cl * dr[2]
    u = cl * co * dr[0] + cl * so * dr[1] + sl * dr[2]
    return e, n, u


def reach(lat0, lon0, alt0, az_deg, lat, lon, alt):
    """沿发射方位角的 downrange / crossrange（m）。"""
    e, n, _ = enu_from_site(lat0, lon0, alt0, lat, lon, alt)
    a = math.radians(az_deg)
    dr = e * math.sin(a) + n * math.cos(a)
    cr = e * math.cos(a) - n * math.sin(a)
    return dr, cr


def propagate_orbit(r0, v0, t0_utc, t_seco, duration_s, dt=10.0):
    """二体开普勒轨道递推（可视化用，不含 J2；误差 <1 km/圈量级）。"""
    x, y, z = r0
    vx, vy, vz = v0
    ts, rows = [], []
    t = 0.0
    while t <= duration_s + 1e-9:
        ts.append(t_seco + t)
        rows.append((x, y, z, vx, vy, vz))
        for _ in range(int(round(dt / 0.5))):
            h = 0.5
            for _ in range(1):
                rn = math.sqrt(x * x + y * y + z * z)
                ax, ay, az_ = -MU * x / rn ** 3, -MU * y / rn ** 3, -MU * z / rn ** 3
                vx += ax * h; vy += ay * h; vz += az_ * h
                x += vx * h; y += vy * h; z += vz * h
        t += dt
    return ts, rows


def find_zone(cfg, lat, lon):
    for z in cfg.get("notams", []):
        poly = np.asarray(z["polygon_latlon"], dtype=float)
        if N.point_in_polygon(lat, lon, poly):
            return z, poly
    return None, None


def _zone_targets(cfg, z):
    """航警区对应的残骸对象前缀元组（None=不限，()=无法识别）。"""
    stages = [s.get("name", "") for s in (cfg.get("vehicle", {}).get("stages") or [])]
    oh = z.get("object_hint")
    if oh:
        return N.object_hint_targets(oh, stages)
    hint = z.get("stage_hint", "")
    if not hint:
        return None
    return N.hint_object_targets(hint, stages)


def nearest_zone(cfg, lat, lon, obj_name=None):
    """距该点**最近**且**对象类型匹配**的航警区 → ``(zone, 到边界距离 km)``。

    v3.2：增加对象类型过滤——否则会给"助推器落点"配一个毫不相干的一级/整流罩区，
    在 KML/报告里显示成误导性的"最近区"。
    """
    best, best_d = None, 1e18
    for z in cfg.get("notams", []) or []:
        poly = np.asarray(z.get("polygon_latlon") or [], dtype=float)
        if poly.ndim != 2 or poly.shape[0] < 3:
            continue
        if obj_name is not None:
            targets = _zone_targets(cfg, z)
            if targets is not None and not N.object_name_matches(obj_name, targets):
                continue
        d = float(N.distance_to_polygon_edge_km(lat, lon, poly))
        if d < best_d:
            best, best_d = z, d
    return (best, best_d) if best is not None else (None, None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--params", default=None, help="反演参数 json（默认 sim/nominal_params.json）")
    ap.add_argument("--legacy", action="store_true", help="v1.0 数值口径（事件量化到 dt 步边界）")
    ap.add_argument("--accurate", action="store_true", help="v2.0 事件对齐口径（默认，见 config.numerics）")
    ap.add_argument("--dt", type=float, default=None, help="覆盖积分步长 [s]")
    args = ap.parse_args()
    cfg = json.load(open(args.config, encoding="utf-8"))
    t0 = datetime.fromisoformat(cfg["mission"]["t0_utc"].replace("Z", "+00:00"))
    site = cfg["mission"]["launch_site"]
    sim_dir = os.path.join(ROOT, "sim")
    os.makedirs(sim_dir, exist_ok=True)

    veh = D.vehicle_from_config(cfg)
    p = D.params_from_config(cfg)
    if args.legacy:
        D.apply_mode(p, "legacy", args.dt)
    elif args.accurate:
        D.apply_mode(p, "accurate", args.dt)
    elif args.dt:
        p.dt = float(args.dt)
    if args.params:
        extra = json.load(open(args.params, encoding="utf-8"))
        for k, v in extra.items():
            if hasattr(p, k) and v is not None:
                setattr(p, k, v)
    print("[export] 仿真中：liftoff %.0f kg, %d 级%s ..." % (
        veh.liftoff_mass, len(veh.stages),
        "，%d 台助推器" % veh.boosters.count if veh.boosters else "，无助推器"))
    r = D.simulate(veh, p, collect_debris=True)
    if r["failures"]:
        print("[export] 警告：", r["failures"])

    # ---- trajectory.csv ----
    rows = []
    for i, ts in enumerate(r["t"]):
        r_ecef = D.eci_to_ecef(r["r_eci"][i], dt_from_met(t0, ts))
        v_ecef = None
        # ECEF 速度 = 惯性速度 - ω×r
        rv = D.eci_to_ecef(r["v_eci"][i], dt_from_met(t0, ts))
        th = D.gmst_rad(dt_from_met(t0, ts))
        rr = r_ecef
        om_r = np.array([-D.OMEGA * rr[1], D.OMEGA * rr[0], 0.0])
        v_ecef = rv - om_r
        rows.append(dict(t_met_s="%.3f" % ts, t_utc=utc(dt_from_met(t0, ts)),
                         lat_deg="%.7f" % r["lat"][i], lon_deg="%.7f" % r["lon"][i],
                         alt_m="%.2f" % r["alt"][i],
                         vx_ecef="%.3f" % v_ecef[0], vy_ecef="%.3f" % v_ecef[1],
                         vz_ecef="%.3f" % v_ecef[2], speed_m_s="%.3f" % r["speed"][i],
                         mass_kg="%.2f" % r["mass"][i], phase=r["phase"][i]))
    write_csv(os.path.join(sim_dir, "trajectory.csv"),
              ["t_met_s", "t_utc", "lat_deg", "lon_deg", "alt_m", "vx_ecef", "vy_ecef",
               "vz_ecef", "speed_m_s", "mass_kg", "phase"], rows)
    print("[export] trajectory.csv %d 行" % len(rows))

    # ---- 事件表 ----
    ev_rows = []
    for e in r["events"]:
        ev_rows.append(dict(event=canonical_event(e["event"], cfg), t_met_s="%.3f" % e["t_met_s"],
                            t_utc=utc(dt_from_met(t0, e["t_met_s"])),
                            lat_deg="%.6f" % e["lat"], lon_deg="%.6f" % e["lon"],
                            alt_m="%.1f" % e["alt_m"], speed_m_s="%.1f" % e["speed_m_s"],
                            mass_kg="%.1f" % e["mass_kg"], note=e["note"]))
    write_csv(os.path.join(sim_dir, "弹道事件表.csv"),
              ["event", "t_met_s", "t_utc", "lat_deg", "lon_deg", "alt_m", "speed_m_s",
               "mass_kg", "note"], ev_rows)

    # ---- 残骸轨迹 + 落点 ----
    deb_rows, imp_rows, obj_rows = [], [], []
    for d in r["debris"]:
        ts = np.asarray(d["t"])
        for k in range(0, len(ts)):
            if k % max(1, int(round(1.0 / p.debris_dt))) != 0 and k != len(ts) - 1:
                continue
            rr = D.eci_to_ecef(d["r_eci"][k], dt_from_met(t0, ts[k]))
            la, lo, al = D.ecef_to_geodetic(*rr)
            spd = float(np.linalg.norm(d["r_eci"][min(k + 1, len(ts) - 1)] - d["r_eci"][k])) / \
                max(1e-6, float(ts[min(k + 1, len(ts) - 1)] - ts[k]))
            deb_rows.append(dict(object=canonical_object(d["obj"], cfg), t_met_s="%.3f" % ts[k],
                                 t_utc=utc(dt_from_met(t0, ts[k])), lat_deg="%.6f" % la,
                                 lon_deg="%.6f" % lo, alt_m="%.1f" % al,
                                 speed_m_s="%.1f" % spd, phase="DEBRIS"))
        imp = d["impact"]
        z, poly = find_zone(cfg, imp["lat"], imp["lon"])
        dr, cr = geo.downrange_crossrange(site["lat_deg"], site["lon_deg"],
                                          p.azimuth_deg, imp["lat"], imp["lon"])
        gc = geo.great_circle_distance_km(site["lat_deg"], site["lon_deg"], imp["lat"], imp["lon"])
        db = float(N.distance_to_polygon_edge_km(imp["lat"], imp["lon"], poly)) if poly is not None else None
        # v3.2：区外落点也给出"最近的航警区"及其边界距离，便于 KML/报告显示与人工核对
        nz, nd = nearest_zone(cfg, imp["lat"], imp["lon"], canonical_object(d["obj"], cfg))
        if z is not None:
            nz, nd = z, db
        imp_rows.append(dict(object=canonical_object(d["obj"], cfg), impact_t_met_s="%.3f" % imp["t_met_s"],
                             impact_t_utc=utc(dt_from_met(t0, imp["t_met_s"])),
                             lat_deg="%.6f" % imp["lat"], lon_deg="%.6f" % imp["lon"],
                             great_circle_km="%.2f" % gc, downrange_km="%.2f" % dr,
                             crossrange_km="%.2f" % cr,
                             zone_id=z["id"] if z else "", inside_zone=("true" if z else "false"),
                             dist_to_boundary_km=("%.2f" % db if db is not None else ""),
                             nearest_zone_id=(nz["id"] if nz is not None else ""),
                             nearest_dist_km=("%.2f" % nd if nd is not None else ""),
                             note="%s 落点（3-DOF 无动力弹道 + 阻力）" % d["name_cn"]))
        obj_rows.append(dict(obj=canonical_object(d["obj"], cfg), name_cn=d["name_cn"], color=d["color"],
                             mass_kg="%.1f" % d["mass"], t_sep_s="%.3f" % d["t_sep"],
                             cda_m2="%.2f" % (d["mass"] and cfg["vehicle"]["debris"].get(
                                 {"stage_0": "stage_0"}.get(d["obj"], d["obj"]), {}).get("cda", 0) or 5.0)))
    write_csv(os.path.join(sim_dir, "debris_trajectories.csv"),
              ["object", "t_met_s", "t_utc", "lat_deg", "lon_deg", "alt_m", "speed_m_s", "phase"],
              deb_rows)
    write_csv(os.path.join(sim_dir, "落点与射程.csv"),
              ["object", "impact_t_met_s", "impact_t_utc", "lat_deg", "lon_deg", "great_circle_km",
               "downrange_km", "crossrange_km", "zone_id", "inside_zone", "dist_to_boundary_km",
               "nearest_zone_id", "nearest_dist_km", "note"],
              imp_rows)
    json.dump(obj_rows, open(os.path.join(sim_dir, "objects.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    # ---- 入轨根数 ----
    o = r["orbit_seco"]
    if o is None or "insertion_state" not in r:
        msg = ("未进入轨道（仿真中没有产生 SECO/入轨状态）。常见原因：箭型质量/推力估值偏差过大、"
               "末级推进剂不足、俯仰程序不合适。建议：换用内置库中的具体型号，或在反演里加入 "
               "t_p/pitch_end 参数。")
        print("[export] 警告：" + msg)
        if r.get("failures"):
            print("[export] 仿真提示：", r.get("failures"))
        json.dump({"error": msg, "a_km": None, "e": None, "i_deg": None,
                   "perigee_km": None, "apogee_km": None, "period_s": None,
                   "insertion": {}},
                  open(os.path.join(sim_dir, "入轨根数.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        # 其余产物（事件/残骸/轨迹）照常写出，供图件与报告使用
        o = None
    ins = r.get("insertion_state") or {}
    orb = {}
    if o is not None:
        orb = dict(a_km=o["a_km"], e=o["e"], i_deg=o["i_deg"], raan_deg=o["raan_deg"],
                   argp_deg=o["argp_deg"], nu_deg=o["nu_deg"], perigee_km=o["perigee_km"],
                   apogee_km=o["apogee_km"], period_s=o["period_s"],
                   insertion=dict(t_met_s=ins.get("t_met_s"), alt_km=ins.get("alt_km"),
                                  speed_m_s=ins.get("speed_m_s"), fpa_deg=ins.get("fpa_deg"),
                                  lat=ins.get("lat"), lon=ins.get("lon"),
                                  i_deg=ins.get("i_deg")))
        # v3：明显不是"入轨"的根数（近地点过低/负值）要显式标记，避免下游把亚轨道当正常入轨
        _peri = float(o.get("perigee_km") or 0.0)
        if _peri < 100.0:
            orb["warning"] = ("入轨根数异常：近地点 %.0f km（<100 km），实际未正常入轨。"
                              "常见原因：箭型质量/推力估值偏差、末级关机过早或制导参数未反演。" % _peri)
            print("[export] 警告：" + orb["warning"])
        json.dump(orb, open(os.path.join(sim_dir, "入轨根数.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print("[export] 入轨：h=%.1f km, v=%.1f m/s, a=%.1f km, e=%.5f, i=%.3f°" % (
            ins["alt_km"], ins["speed_m_s"], o["a_km"], o["e"], o["i_deg"]))

    # ---- 首圈轨道（1.5 圈；未入轨时跳过）----
    if o is None:
        open(os.path.join(sim_dir, "orbit_track.csv"), "w", encoding="utf-8-sig").write(
            "t_met_s,t_utc,lat_deg,lon_deg,alt_m,speed_m_s\n")
        print("[export] 未入轨：跳过首圈星下点")
    else:
      idx = int(np.argmin(np.abs(r["t"] - ins["t_met_s"])))
      ts, rows_orb = propagate_orbit(r["r_eci"][idx], r["v_eci"][idx], t0,
                                     ins["t_met_s"], 1.5 * o["period_s"])
      orows = []
      for k, ts_ in enumerate(ts):
          x, y, z, vx, vy, vz = rows_orb[k]
          la, lo, al = D.ecef_to_geodetic(*D.eci_to_ecef((x, y, z), dt_from_met(t0, ts_)))
          spd = math.sqrt(vx * vx + vy * vy + vz * vz)
          orows.append(dict(t_met_s="%.1f" % ts_, t_utc=utc(dt_from_met(t0, ts_)),
                            lat_deg="%.6f" % la, lon_deg="%.6f" % lo, alt_m="%.1f" % al,
                            speed_m_s="%.1f" % spd))
      write_csv(os.path.join(sim_dir, "orbit_track.csv"),
                ["t_met_s", "t_utc", "lat_deg", "lon_deg", "alt_m", "speed_m_s"], orows)

    # ---- 航警区 ----
    zones = []
    for z in cfg.get("notams", []):
        b = datetime.fromisoformat(z["b_utc"].replace("Z", "+00:00"))
        c = datetime.fromisoformat(z["c_utc"].replace("Z", "+00:00"))
        zones.append(dict(id=z["id"], polygon_latlon=z["polygon_latlon"],
                          b_utc=z["b_utc"], c_utc=z["c_utc"], q_line=z.get("q_line", ""),
                          fir=z.get("fir", ""), stage_hint=z.get("stage_hint", "")))
    json.dump(dict(zones=zones), open(os.path.join(sim_dir, "notam_zones.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    # ---- meta ----
    meta = dict(t0_utc=cfg["mission"]["t0_utc"],
                site=dict(lat_deg=site["lat_deg"], lon_deg=site["lon_deg"],
                          alt_m=site.get("alt_m_ellipsoid", 0.0), name=site.get("name", "")),
                rocket=cfg["mission"].get("rocket", ""), azimuth_deg=p.azimuth_deg,
                insertion_t_met_s=ins.get("t_met_s"),
                vehicle=dict(stages=len(veh.stages),
                             boosters=(veh.boosters.count if veh.boosters else 0),
                             liftoff_mass_kg=veh.liftoff_mass,
                             fairing_kg=veh.fairing_mass_kg, payload_kg=veh.payload_kg),
                guidance=dict(mode=p.guidance, t_v=p.t_v, t_p=p.t_p,
                              pitch_power=p.pitch_power,
                              pitch_ends=[s.pitch_end_deg for s in veh.stages]),
                version=cfg.get("mission", {}).get("version", "v1.0"))
    json.dump(meta, open(os.path.join(sim_dir, "trajectory_meta.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("[export] 完成 →", sim_dir)


def write_csv(path, fields, rows):
    if not fields:
        return
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})


if __name__ == "__main__":
    main()
