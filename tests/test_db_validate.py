# -*- coding: utf-8 -*-
"""火箭弹道工具箱 v3.0 内置数据库自检脚本（纯标准库，离线可跑）。

用法（项目根目录）：
    python tests\\test_db_validate.py

校验 app/data/ 下 5 个 JSON：
    rockets.json / launch_sites.json / cities.json / payloads.json / orbits.json

内容：
  1. 文件可解析、UTF-8、顶层 schema/updated/sources 齐全；
  2. 条目数下限（火箭 >= 20、工位 >= 20、城市 >= 120、载荷 >= 20、轨道 >= 10）；
  3. id / name 唯一；必填字段齐全、类型正确；
  4. 数值量级合理（起飞质量 10-900 t、比冲 150-470 s、推重比 0.8-3.0、经纬度范围等）；
  5. vehicle 块与 engine/templates/config_cz2d.json 同构（stages/boosters/fairing/debris）；
  6. 质量账目闭合（vehicle 求和 == liftoff_mass_kg，误差 <= 2%）；
  7. 交叉引用（rocket.sites -> launch_sites.id；site.vehicles -> rockets.id）；
  8. 三份历史任务锚点（CZ-2D / CZ-2F / CZ-6A）vehicle 数值逐项比对（锚点文件存在时）。

退出码 0 = 全部通过；1 = 有失败项。
"""
from __future__ import annotations

import json
import math
import os
import sys

G0 = 9.80665
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "app", "data")

FAILS = []
CHECKS = 0
WARNINGS = []


def check(cond, msg):
    """记录一项断言（返回布尔，便于组合）。"""
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILS.append(msg)
    return bool(cond)


def warn(msg):
    WARNINGS.append(msg)


def load(name):
    path = os.path.join(DATA, name)
    with open(path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:  # noqa
        FAILS.append("%s: 不是 UTF-8 编码 (%s)" % (name, e))
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:  # noqa
        FAILS.append("%s: JSON 解析失败 (%s)" % (name, e))
        return None


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def has_sources(items, label):
    ok = True
    for it in items:
        src = it.get("sources")
        if not isinstance(src, list) or not src:
            check(False, "%s: %s 缺少 sources" % (label, it.get("id", "?")))
            ok = False
            continue
        for s in src:
            if not isinstance(s, dict) or not s.get("url"):
                check(False, "%s: %s 的 sources 项缺 url" % (label, it.get("id", "?")))
                ok = False
            elif "accessed" not in s:
                check(False, "%s: %s 的 sources 缺 accessed 日期" % (label, it.get("id", "?")))
                ok = False
    return ok


def unique_ids(items, label):
    seen = {}
    for it in items:
        i = it.get("id")
        if i in seen:
            check(False, "%s: id 重复 %r" % (label, i))
        seen[i] = True
    return seen


def check_top(obj, name, entries_key, min_n):
    ok = True
    ok &= check(isinstance(obj, dict), "%s: 顶层不是对象" % name)
    if not isinstance(obj, dict):
        return False
    ok &= check(obj.get("schema") == {
        "rockets.json": "rocketkit.rockets.v1",
        "launch_sites.json": "rocketkit.sites.v1",
        "cities.json": "rocketkit.cities.v1",
        "payloads.json": "rocketkit.payloads.v1",
        "orbits.json": "rocketkit.orbits.v1",
    }[name], "%s: schema 字段应为 rocketkit.*.v1（实际 %r）" % (name, obj.get("schema")))
    ok &= check(isinstance(obj.get("updated"), str) and len(obj["updated"]) == 10,
                "%s: updated 应为 YYYY-MM-DD 字符串" % name)
    ok &= check(isinstance(obj.get("sources"), list) and len(obj["sources"]) > 0,
                "%s: 顶层 sources 不能为空" % name)
    entries = obj.get(entries_key)
    ok &= check(isinstance(entries, list), "%s: %s 应为数组" % (name, entries_key))
    if isinstance(entries, list):
        ok &= check(len(entries) >= min_n, "%s: %s 仅 %d 条（要求 >= %d）" % (name, entries_key, len(entries), min_n))
    return bool(ok)


# ------------------------------------------------------------------ vehicle
def stage_thrusters(stage):
    """展开某级第一推力段（用于起飞推力/比冲检查）。"""
    if stage.get("phases"):
        return stage["phases"][0]["thrusters"]
    return [{
        "n": stage.get("engines", 1),
        "thrust_sl_n": stage.get("thrust_sl_n"),
        "thrust_vac_n": stage.get("thrust_vac_n"),
        "isp_sl_s": stage.get("isp_sl_s"),
        "isp_vac_s": stage.get("isp_vac_s"),
    }]


def liftoff_thrust(veh):
    total = 0.0
    for t in stage_thrusters(veh["stages"][0]):
        total += t["n"] * (t.get("thrust_sl_n") or t.get("thrust_vac_n"))
    b = veh.get("boosters")
    if b:
        for t in b["thrusters"]:
            total += t["n"] * (t.get("thrust_sl_n") or t.get("thrust_vac_n"))
    return total


def vehicle_mass(veh):
    m = float(veh.get("payload_kg", 0.0)) + float(veh.get("adapter_kg", 0.0))
    m += float((veh.get("fairing") or {}).get("mass_kg", 0.0))
    m += sum(float(s["dry_kg"]) + float(s["prop_kg"]) for s in veh["stages"])
    b = veh.get("boosters")
    if b:
        m += int(b["count"]) * (float(b["dry_kg_each"]) + float(b["prop_kg_each"]))
    return m


def check_thruster(t, where, label):
    ok = True
    ok &= check(is_num(t.get("n")) and t["n"] >= 1, "%s: %s thrusters.n 必须 >=1" % (label, where))
    tv = t.get("thrust_vac_n")
    ok &= check(is_num(tv) and 1.0e3 <= tv <= 3.0e6,
                "%s: %s thrust_vac_n 应在 1e3-3e6 N（实际 %r）" % (label, where, tv))
    ts = t.get("thrust_sl_n")
    if ts is not None:
        ok &= check(is_num(ts) and 1.0e3 <= ts <= 3.0e6,
                    "%s: %s thrust_sl_n 应在 1e3-3e6 N（实际 %r）" % (label, where, ts))
        if is_num(ts) and is_num(tv):
            ok &= check(ts <= tv * 1.02, "%s: %s 海平面推力不应大于真空推力" % (label, where))
    iv = t.get("isp_vac_s")
    ok &= check(is_num(iv) and 150.0 <= iv <= 470.0,
                "%s: %s isp_vac_s 应在 150-470 s（实际 %r）" % (label, where, iv))
    isl = t.get("isp_sl_s")
    if isl is not None:
        ok &= check(is_num(isl) and 150.0 <= isl <= 470.0,
                    "%s: %s isp_sl_s 应在 150-470 s（实际 %r）" % (label, where, isl))
    return ok


def check_stage(s, idx, n_st, label):
    ok = True
    where = "stages[%d]" % idx
    ok &= check(isinstance(s, dict), "%s: %s 不是对象" % (label, where))
    if not isinstance(s, dict):
        return False
    ok &= check(isinstance(s.get("name"), str) and s["name"], "%s: %s.name 缺失" % (label, where))
    ok &= check(is_num(s.get("dry_kg")) and s["dry_kg"] > 0, "%s: %s.dry_kg 必须 >0" % (label, where))
    ok &= check(is_num(s.get("prop_kg")) and s["prop_kg"] > 0, "%s: %s.prop_kg 必须 >0" % (label, where))
    # 单工况或多工况二选一
    if s.get("phases"):
        ok &= check(isinstance(s["phases"], list) and len(s["phases"]) >= 1,
                    "%s: %s.phases 应为非空数组" % (label, where))
        for pi, ph in enumerate(s.get("phases") or []):
            ok &= check(isinstance(ph.get("thrusters"), list) and ph["thrusters"],
                        "%s: %s.phases[%d].thrusters 为空" % (label, where, pi))
            for t in ph.get("thrusters") or []:
                ok &= check_thruster(t, "%s.phases[%d]" % (where, pi), label)
    else:
        ok &= check(is_num(s.get("engines")) and s["engines"] >= 1,
                    "%s: %s.engines 必须 >=1（无 phases 时）" % (label, where))
        for k in ("thrust_vac_n", "isp_vac_s"):
            ok &= check(k in s and is_num(s[k]), "%s: %s 缺少 %s" % (label, where, k))
        ok &= check_thruster({
            "n": s.get("engines", 1), "thrust_vac_n": s.get("thrust_vac_n"),
            "isp_vac_s": s.get("isp_vac_s"), "thrust_sl_n": s.get("thrust_sl_n"),
            "isp_sl_s": s.get("isp_sl_s"),
        }, where, label)
    pe = s.get("pitch_end_deg")
    ok &= check(pe is None or (is_num(pe) and -90.0 <= pe <= 90.0),
                "%s: %s.pitch_end_deg 应在 [-90,90]（实际 %r）" % (label, where, pe))
    # 末级 seco_met 必须显式存在（None=推进剂耗尽）
    if idx == n_st - 1:
        ok &= check("seco_met" in s, "%s: 末级缺少 seco_met 键（None=推进剂耗尽）" % label)
        seco = s.get("seco_met")
        ok &= check(seco is None or (is_num(seco) and 0 < seco < 5000),
                    "%s: 末级 seco_met 应为 null 或 (0,5000) s（实际 %r）" % (label, seco))
    return ok


def check_vehicle(veh, label, liftoff_mass_kg):
    ok = True
    ok &= check(isinstance(veh, dict), "%s: vehicle 不是对象" % label)
    if not isinstance(veh, dict):
        return False
    for k in ("adapter_kg", "payload_kg"):
        ok &= check(is_num(veh.get(k)) and veh[k] >= 0, "%s: vehicle.%s 应 >=0" % (label, k))
    stages = veh.get("stages")
    ok &= check(isinstance(stages, list) and len(stages) >= 1, "%s: vehicle.stages 不能为空" % label)
    if not isinstance(stages, list) or not stages:
        return False
    for i, s in enumerate(stages):
        ok &= check_stage(s, i, len(stages), label)
    # 助推器（可选）
    b = veh.get("boosters")
    if b is not None:
        ok &= check(isinstance(b, dict) and is_num(b.get("count")) and b["count"] >= 1,
                    "%s: boosters.count 必须 >=1" % label)
        ok &= check(is_num(b.get("dry_kg_each")) and b["dry_kg_each"] > 0,
                    "%s: boosters.dry_kg_each 必须 >0" % label)
        ok &= check(is_num(b.get("prop_kg_each")) and b["prop_kg_each"] > 0,
                    "%s: boosters.prop_kg_each 必须 >0" % label)
        ok &= check(isinstance(b.get("thrusters"), list) and b["thrusters"],
                    "%s: boosters.thrusters 不能为空" % label)
        for ti, t in enumerate(b.get("thrusters") or []):
            ok &= check_thruster(t, "boosters[%d]" % ti, label)
        jm = b.get("jettison_met")
        ok &= check(jm is None or (is_num(jm) and 0 < jm < 1000),
                    "%s: boosters.jettison_met 应为 null 或 (0,1000) s" % label)
    # 整流罩（可选）
    f = veh.get("fairing")
    if f is not None:
        ok &= check(is_num(f.get("mass_kg")) and f["mass_kg"] > 0, "%s: fairing.mass_kg 必须 >0" % label)
        ok &= check(f.get("halves") in (2, 4), "%s: fairing.halves 应为 2 或 4" % label)
        ok &= check(is_num(f.get("sep_met")) and 0 < f["sep_met"] < 1500,
                    "%s: fairing.sep_met 应为 (0,1500) s" % label)
        ok &= check(is_num(f.get("cda_m2")) and 0 < f["cda_m2"] < 80, "%s: fairing.cda_m2 应在 (0,80)" % label)
    # 残骸（可选）
    d = veh.get("debris")
    if d is not None:
        ok &= check(isinstance(d, dict), "%s: debris 应为对象" % label)
        for key in ("stage_0", "fairing", "boosters"):
            if key in d:
                e = d[key]
                ok &= check(is_num(e.get("cda")) and 0 < e["cda"] < 60,
                            "%s: debris.%s.cda 应在 (0,60)" % (label, key))
                ok &= check(is_num(e.get("extra_mass_kg")),
                            "%s: debris.%s.extra_mass_kg 应为数值" % (label, key))
    ok &= check(is_num(veh.get("ascent_cda_scale")) and 0.2 <= veh["ascent_cda_scale"] <= 2.0,
                "%s: ascent_cda_scale 应在 [0.2,2.0]" % label)
    # 质量闭合 & 推重比
    mass = vehicle_mass(veh)
    if is_num(liftoff_mass_kg) and liftoff_mass_kg > 0:
        rel = abs(mass - liftoff_mass_kg) / liftoff_mass_kg
        ok &= check(rel <= 0.02,
                    "%s: vehicle 质量账目 %.0f kg 与 liftoff_mass_kg %.0f kg 差 %.1f%%（要求 <=2%%）"
                    % (label, mass, liftoff_mass_kg, rel * 100.0))
    tw = liftoff_thrust(veh) / (mass * G0)
    ok &= check(0.8 <= tw <= 3.0, "%s: 起飞推重比 %.2f 应在 0.8-3.0" % (label, tw))
    for si, s in enumerate(stages):
        # 每级推进剂不应超过总质量
        ok &= check(s["dry_kg"] + s["prop_kg"] <= mass,
                    "%s: stages[%d] 干重+推进剂超过全箭质量" % (label, si))
    return ok


# ------------------------------------------------------------------ 主流程
def validate_rockets(db, sites_by_id):
    ok = check_top(db, "rockets.json", "rockets", 20)
    rockets = db.get("rockets") if isinstance(db, dict) else None
    if not isinstance(rockets, list):
        return {}
    ids = unique_ids(rockets, "rockets")
    by_id = {}
    for r in rockets:
        label = "rockets[%s]" % r.get("id", "?")
        for k in ("id", "name", "aliases", "class", "liftoff_mass_kg", "leo_kg", "sso_kg",
                  "default_payload_kg", "typical_orbits", "sites", "guidance_defaults",
                  "vehicle", "numerics", "confidence", "note", "sources"):
            ok &= check(k in r, "%s: 缺字段 %s" % (label, k))
        if r.get("id") in by_id:
            continue
        by_id[r["id"]] = r
        ok &= check(isinstance(r.get("id"), str) and r["id"] and r["id"] == r["id"].lower().replace("-", "_"),
                    "%s: id 应为小写字母/数字/下划线" % label)
        ok &= check(isinstance(r.get("name"), str) and len(r["name"]) >= 2, "%s: name 非法" % label)
        ok &= check(isinstance(r.get("aliases"), list) and r["aliases"], "%s: aliases 不能为空" % label)
        ok &= check(isinstance(r.get("class"), str) and r["class"], "%s: class 不能为空" % label)
        lm = r.get("liftoff_mass_kg")
        ok &= check(is_num(lm) and 10_000 <= lm <= 900_000,
                    "%s: liftoff_mass_kg 应在 10-900 t（实际 %r）" % (label, lm))
        for k in ("leo_kg", "sso_kg"):
            v = r.get(k)
            ok &= check(is_num(v) and 0 < v <= 30_000, "%s: %s 应在 (0,30000] kg（实际 %r）" % (label, k, v))
        ok &= check(is_num(r.get("default_payload_kg")) and 0 < r["default_payload_kg"] <= 30_000,
                    "%s: default_payload_kg 应在 (0,30000] kg" % label)
        if is_num(r.get("leo_kg")) and is_num(r.get("default_payload_kg")):
            ok &= check(r["default_payload_kg"] <= r["leo_kg"] * 1.05,
                        "%s: 默认载荷 %.0f kg 不应超过 LEO 运力 %.0f kg"
                        % (label, r["default_payload_kg"], r["leo_kg"]))
        orbs = r.get("typical_orbits")
        ok &= check(isinstance(orbs, list) and orbs, "%s: typical_orbits 不能为空" % label)
        for o in orbs or []:
            ok &= check(isinstance(o.get("name"), str) and o["name"], "%s: typical_orbits.name 缺失" % label)
            ok &= check(is_num(o.get("alt_km")) and 100 <= o["alt_km"] <= 50_000,
                        "%s: typical_orbits.alt_km 越界（%r）" % (label, o.get("alt_km")))
            ok &= check(o.get("i_deg") is None or (is_num(o["i_deg"]) and 0 <= o["i_deg"] <= 180),
                        "%s: typical_orbits.i_deg 应在 [0,180] 或 null" % label)
        sites = r.get("sites")
        ok &= check(isinstance(sites, list) and sites, "%s: sites 不能为空" % label)
        for sid in sites or []:
            ok &= check(sid in sites_by_id, "%s: sites 引用了不存在的工位 %r" % (label, sid))
        if "launch_sites" in r:
            ok &= check(r["launch_sites"] == sites,
                        "%s: launch_sites 与 sites 应内容一致" % label)
        g = r.get("guidance_defaults")
        ok &= check(isinstance(g, dict), "%s: guidance_defaults 应为对象" % label)
        if isinstance(g, dict):
            for k in ("t_v", "t_p", "pitch_power", "t_fairing", "fairing_divert_mps",
                      "plane_keeping", "yaw_tau", "i_target_deg", "azimuth_deg"):
                ok &= check(k in g, "%s: guidance_defaults 缺 %s" % (label, k))
            ok &= check(is_num(g.get("t_v")) and 1.0 <= g["t_v"] <= 20.0,
                        "%s: t_v 应在 1-20 s（实际 %r）" % (label, g.get("t_v")))
            ok &= check(is_num(g.get("t_p")) and 30.0 <= g["t_p"] <= 320.0,
                        "%s: t_p 应在 30-320 s（实际 %r）" % (label, g.get("t_p")))
            ok &= check(is_num(g.get("pitch_power")) and 0.5 <= g["pitch_power"] <= 2.0,
                        "%s: pitch_power 应在 0.5-2.0（实际 %r）" % (label, g.get("pitch_power")))
            ok &= check(is_num(g.get("t_fairing")) and 0 < g["t_fairing"] < 1500,
                        "%s: t_fairing 应在 (0,1500) s" % label)
            ok &= check(g.get("azimuth_deg") is None or
                        (is_num(g["azimuth_deg"]) and 0 <= g["azimuth_deg"] < 360),
                        "%s: azimuth_deg 应为 null 或 [0,360)" % label)
            ok &= check(g.get("i_target_deg") is None or
                        (is_num(g["i_target_deg"]) and 0 <= g["i_target_deg"] <= 180),
                        "%s: i_target_deg 应为 null 或 [0,180]" % label)
        n = r.get("numerics")
        ok &= check(isinstance(n, dict) and n.get("mode") in ("accurate", "legacy"),
                    "%s: numerics.mode 应为 accurate/legacy" % label)
        ok &= check(r.get("confidence") in ("高", "中高", "中", "低"), "%s: confidence 取值非法" % label)
        ok &= check(isinstance(r.get("note"), str) and len(r["note"]) >= 10, "%s: note 太短" % label)
        ok &= check_vehicle(r.get("vehicle"), label, lm)
    has_sources(rockets, "rockets")
    return by_id


def validate_sites(db, rocket_ids):
    ok = check_top(db, "launch_sites.json", "sites", 20)
    sites = db.get("sites") if isinstance(db, dict) else None
    if not isinstance(sites, list):
        return {}
    ids = unique_ids(sites, "launch_sites")
    by_id = {}
    for s in sites:
        label = "sites[%s]" % s.get("id", "?")
        for k in ("id", "name", "aliases", "lat_deg", "lon_deg", "alt_m_ellipsoid",
                  "country", "region", "vehicles", "confidence", "coord_note", "sources"):
            ok &= check(k in s, "%s: 缺字段 %s" % (label, k))
        if s.get("id") in by_id:
            continue
        by_id[s["id"]] = s
        ok &= check(isinstance(s.get("id"), str) and s["id"], "%s: id 非法" % label)
        lat, lon = s.get("lat_deg"), s.get("lon_deg")
        ok &= check(is_num(lat) and -90 <= lat <= 90, "%s: lat_deg 越界（%r）" % (label, lat))
        ok &= check(is_num(lon) and -180 <= lon <= 180, "%s: lon_deg 越界（%r）" % (label, lon))
        if s.get("country") == "中国":
            ok &= check(is_num(lat) and 15 <= lat <= 55, "%s: 中国工位纬度应在 15-55°（%r）" % (label, lat))
            ok &= check(is_num(lon) and 70 <= lon <= 140, "%s: 中国工位经度应在 70-140°（%r）" % (label, lon))
        alt = s.get("alt_m_ellipsoid")
        ok &= check(is_num(alt) and -200 <= alt <= 5000,
                    "%s: alt_m_ellipsoid 应在 [-200,5000] m（%r）" % (label, alt))
        ok &= check(isinstance(s.get("vehicles"), list), "%s: vehicles 应为数组" % label)
        for vid in s.get("vehicles") or []:
            ok &= check(vid in rocket_ids, "%s: vehicles 引用了不存在的火箭 %r" % (label, vid))
        ok &= check(s.get("confidence") in ("高", "中高", "中", "低"), "%s: confidence 非法" % label)
        ok &= check(isinstance(s.get("coord_note"), str) and len(s["coord_note"]) >= 10,
                    "%s: coord_note 太短" % label)
        ok &= check(isinstance(s.get("aliases"), list), "%s: aliases 应为数组" % label)
    has_sources(sites, "launch_sites")
    return by_id


def validate_cities(db):
    ok = check_top(db, "cities.json", "cities", 120)
    cities = db.get("cities") if isinstance(db, dict) else None
    if not isinstance(cities, list):
        return
    names = set()
    for c in cities:
        label = "cities[%s]" % c.get("name", "?")
        for k in ("name", "province", "lat_deg", "lon_deg", "alt_m", "aliases"):
            ok &= check(k in c, "%s: 缺字段 %s" % (label, k))
        nm = c.get("name")
        ok &= check(isinstance(nm, str) and nm, "%s: name 非法" % label)
        if nm in names:
            check(False, "cities: 城市名重复 %r" % nm)
        names.add(nm)
        lat, lon = c.get("lat_deg"), c.get("lon_deg")
        ok &= check(is_num(lat) and 15 <= lat <= 55, "%s: 纬度应在 15-55°（中国）（%r）" % (label, lat))
        ok &= check(is_num(lon) and 70 <= lon <= 140, "%s: 经度应在 70-140°（中国）（%r）" % (label, lon))
        alt = c.get("alt_m")
        ok &= check(is_num(alt) and -200 <= alt <= 5500, "%s: 海拔应在 [-200,5500] m（%r）" % (label, alt))
        ok &= check(isinstance(c.get("aliases"), list), "%s: aliases 应为数组" % label)
        ok &= check(isinstance(c.get("province"), str) and c["province"], "%s: province 缺失" % label)


def validate_payloads(db):
    ok = check_top(db, "payloads.json", "payloads", 20)
    items = db.get("payloads") if isinstance(db, dict) else None
    if not isinstance(items, list):
        return
    unique_ids(items, "payloads")
    for p in items:
        label = "payloads[%s]" % p.get("id", "?")
        for k in ("id", "name", "aliases", "mass_kg", "min_kg", "max_kg", "confidence", "note", "sources"):
            ok &= check(k in p, "%s: 缺字段 %s" % (label, k))
        m, mn, mx = p.get("mass_kg"), p.get("min_kg"), p.get("max_kg")
        ok &= check(is_num(m) and 0.5 <= m <= 100_000, "%s: mass_kg 应在 (0,1e5]（%r）" % (label, m))
        ok &= check(is_num(mn) and is_num(mx) and mn <= mx,
                    "%s: 应满足 min_kg <= max_kg" % label)
        if is_num(m) and is_num(mn) and is_num(mx):
            ok &= check(mn <= m <= mx, "%s: mass_kg 应在 [min_kg,max_kg] 内" % label)
        ok &= check(p.get("confidence") in ("高", "中高", "中", "低"), "%s: confidence 非法" % label)
        ok &= check(isinstance(p.get("note"), str) and len(p["note"]) >= 5, "%s: note 太短" % label)
    has_sources(items, "payloads")


def validate_orbits(db):
    ok = check_top(db, "orbits.json", "orbits", 10)
    items = db.get("orbits") if isinstance(db, dict) else None
    if not isinstance(items, list):
        return
    unique_ids(items, "orbits")
    types = {"SSO", "LEO", "MEO", "GTO", "GEO", "IGSO", "polar", "HEO"}
    for o in items:
        label = "orbits[%s]" % o.get("id", "?")
        for k in ("id", "name", "alt_km", "i_deg", "type", "confidence", "note"):
            ok &= check(k in o, "%s: 缺字段 %s" % (label, k))
        alt = o.get("alt_km")
        ok &= check(is_num(alt) and 100 <= alt <= 50_000, "%s: alt_km 应在 [100,50000]（%r）" % (label, alt))
        inc = o.get("i_deg")
        ok &= check(inc is None or (is_num(inc) and 0 <= inc <= 180),
                    "%s: i_deg 应为 [0,180] 或 null（%r）" % (label, inc))
        ok &= check(o.get("type") in types, "%s: type 应为 %s" % (label, sorted(types)))
        ok &= check(o.get("confidence") in ("高", "中高", "中", "低"), "%s: confidence 非法" % label)


# ------------------------------------------------------------------ 锚点比对
ANCHORS = [
    ("cz2d", os.path.join("F:\\DeepSeek工作区", "火箭弹道工具箱_v2.0", "config.json"),
     "CZ-2D 遥106 历史任务 config"),
    ("cz2f", os.path.join("F:\\DeepSeek工作区", "弹道仿真工作区", "2026.11.25 CZ2-F", "config.json"),
     "CZ-2F/G 遥二十四 历史任务 config"),
    ("cz6a", os.path.join("F:\\DeepSeek工作区", "弹道仿真工作区", "2026.11.27 CZ6-A", "config.json"),
     "CZ-6A 遥29 历史任务 config"),
]
GUIDE_KEYS = ("t_v", "t_p", "pitch_power", "t_fairing")


def _num_equal(a, b, rel=1e-9):
    if a is None and b is None:
        return True
    if is_num(a) and is_num(b):
        return abs(float(a) - float(b)) <= rel * max(1.0, abs(float(a)))
    return a == b


def compare_anchor(rockets_by_id, rocket_id, path, desc):
    if not os.path.exists(path):
        warn("锚点比对跳过：找不到 %s（%s）" % (path, desc))
        return
    r = rockets_by_id.get(rocket_id)
    if not r:
        check(False, "锚点比对: rockets.json 缺少 %s" % rocket_id)
        return
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:  # noqa
        check(False, "锚点比对: %s 读取失败 (%s)" % (path, e))
        return
    ref = cfg.get("vehicle", {})
    got = r.get("vehicle", {})
    label = "锚点 %s" % rocket_id
    # 载荷/适配器
    for k in ("adapter_kg", "payload_kg"):
        check(_num_equal(ref.get(k), got.get(k)), "%s: %s 与历史 config 不一致" % (label, k))
    # stages
    rs, gs = ref.get("stages", []), got.get("stages", [])
    check(len(rs) == len(gs), "%s: 级数与历史 config 不一致（%d vs %d）" % (label, len(rs), len(gs)))
    for i, (a, b) in enumerate(zip(rs, gs)):
        for k in ("name", "dry_kg", "prop_kg", "engines", "thrust_sl_n", "thrust_vac_n",
                  "isp_sl_s", "isp_vac_s", "pitch_end_deg", "pitch_duration_s", "seco_met",
                  "cda_attached_m2"):
            check(_num_equal(a.get(k), b.get(k)),
                  "%s: stages[%d].%s 与历史 config 不一致（%r vs %r）"
                  % (label, i, k, a.get(k), b.get(k)))
        pa, pb = a.get("phases"), b.get("phases")
        check((pa is None) == (pb is None), "%s: stages[%d].phases 存在性与历史 config 不一致" % (label, i))
        for j, (ph_a, ph_b) in enumerate(zip(pa or [], pb or [])):
            check(ph_a.get("name") == ph_b.get("name"),
                  "%s: stages[%d].phases[%d].name 不一致" % (label, i, j))
            check(_num_equal(ph_a.get("end_met"), ph_b.get("end_met")),
                  "%s: stages[%d].phases[%d].end_met 不一致" % (label, i, j))
            for t_a, t_b in zip(ph_a.get("thrusters", []), ph_b.get("thrusters", [])):
                for k in ("n", "thrust_sl_n", "thrust_vac_n", "isp_sl_s", "isp_vac_s"):
                    check(_num_equal(t_a.get(k), t_b.get(k)),
                          "%s: stages[%d].phases[%d].thrusters.%s 不一致" % (label, i, j, k))
    # boosters
    ba, bb = ref.get("boosters"), got.get("boosters")
    check((ba is None) == (bb is None), "%s: boosters 存在性与历史 config 不一致" % label)
    if ba and bb:
        for k in ("count", "dry_kg_each", "prop_kg_each", "jettison_met"):
            check(_num_equal(ba.get(k), bb.get(k)),
                  "%s: boosters.%s 与历史 config 不一致（%r vs %r）" % (label, k, ba.get(k), bb.get(k)))
        for t_a, t_b in zip(ba.get("thrusters", []), bb.get("thrusters", [])):
            for k in ("n", "thrust_sl_n", "thrust_vac_n", "isp_sl_s", "isp_vac_s"):
                check(_num_equal(t_a.get(k), t_b.get(k)),
                      "%s: boosters.thrusters.%s 不一致" % (label, k))
    # fairing
    fa, fb = ref.get("fairing") or {}, got.get("fairing") or {}
    for k in ("mass_kg", "halves", "sep_met", "cda_m2"):
        check(_num_equal(fa.get(k), fb.get(k)),
              "%s: fairing.%s 与历史 config 不一致（%r vs %r）" % (label, k, fa.get(k), fb.get(k)))
    # debris
    da, db_ = ref.get("debris") or {}, got.get("debris") or {}
    for key in set(da) | set(db_):
        ea, eb = da.get(key) or {}, db_.get(key) or {}
        for k in ("cda", "extra_mass_kg", "divert_mps"):
            check(_num_equal(ea.get(k), eb.get(k)),
                  "%s: debris.%s.%s 与历史 config 不一致（%r vs %r）"
                  % (label, key, k, ea.get(k), eb.get(k)))
    check(_num_equal(ref.get("ascent_cda_scale", 1.0), got.get("ascent_cda_scale", 1.0)),
          "%s: ascent_cda_scale 与历史 config 不一致" % label)
    # guidance_defaults（按 DB 字段名，历史 config 里叫 guidance）
    rg, gg = cfg.get("guidance", {}), r.get("guidance_defaults", {})
    for k in GUIDE_KEYS:
        check(_num_equal(rg.get(k), gg.get(k)),
              "%s: guidance_defaults.%s 与历史 config 不一致（%r vs %r）"
              % (label, k, rg.get(k), gg.get(k)))


# ------------------------------------------------------------------ 打印
def print_summary(counts):
    print("=" * 92)
    print("火箭弹道工具箱 v3.0 内置数据库自检")
    print("=" * 92)
    print("%-18s %-12s %-12s %8s" % ("文件", "schema", "条目数", "下限"))
    for name in ("rockets.json", "launch_sites.json", "cities.json", "payloads.json", "orbits.json"):
        schema = {"rockets.json": "rocketkit.rockets.v1", "launch_sites.json": "rocketkit.sites.v1",
                  "cities.json": "rocketkit.cities.v1", "payloads.json": "rocketkit.payloads.v1",
                  "orbits.json": "rocketkit.orbits.v1"}[name]
        print("%-18s %-12s %-12s %8s" % (name, schema, counts.get(name, 0), counts.get(name + "_min", "-")))
    print("-" * 92)
    print("校验项总数: %d，失败: %d" % (CHECKS, len(FAILS)))
    if WARNINGS:
        print("提示:")
        for w in WARNINGS:
            print("  [提示] " + w)
    if FAILS:
        print("失败项:")
        for f in FAILS[:60]:
            print("  [FAIL] " + f)
        if len(FAILS) > 60:
            print("  ... 其余 %d 项省略" % (len(FAILS) - 60))
        print("结果: 不通过")
    else:
        print("结果: 全部通过")
    print("=" * 92)


def main():
    counts = {}
    rockets = load("rockets.json")
    sites = load("launch_sites.json")
    cities = load("cities.json")
    payloads = load("payloads.json")
    orbits = load("orbits.json")

    # 先建 site 索引（火箭校验需要交叉引用）
    sites_by_id = {}
    if isinstance(sites, dict) and isinstance(sites.get("sites"), list):
        sites_by_id = {s.get("id"): s for s in sites["sites"]}
    rockets_by_id = validate_rockets(rockets, sites_by_id) if rockets is not None else {}
    validate_sites(sites, set(rockets_by_id)) if sites is not None else None
    if cities is not None:
        validate_cities(cities)
    if payloads is not None:
        validate_payloads(payloads)
    if orbits is not None:
        validate_orbits(orbits)

    for rid, path, desc in ANCHORS:
        compare_anchor(rockets_by_id, rid, path, desc)

    for name, obj, key, mn in (("rockets.json", rockets, "rockets", 20),
                               ("launch_sites.json", sites, "sites", 20),
                               ("cities.json", cities, "cities", 120),
                               ("payloads.json", payloads, "payloads", 20),
                               ("orbits.json", orbits, "orbits", 10)):
        counts[name] = len(obj.get(key, [])) if isinstance(obj, dict) else 0
        counts[name + "_min"] = ">=%d" % mn
    print_summary(counts)
    # 低置信度条目汇总（lead 重点复核清单）
    low = []
    for r in (rockets or {}).get("rockets", []):
        if r.get("confidence") == "低":
            low.append("火箭 " + r.get("id", "?"))
    for s in (sites or {}).get("sites", []):
        if s.get("confidence") == "低":
            low.append("工位 " + s.get("id", "?"))
    for p in (payloads or {}).get("payloads", []):
        if p.get("confidence") == "低":
            low.append("载荷 " + p.get("id", "?"))
    if low:
        print("\n低置信度条目（%d 条，需人工复核）：" % len(low))
        print("  " + "、".join(low))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
