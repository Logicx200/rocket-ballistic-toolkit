# -*- coding: utf-8 -*-
"""交付自检（通用版）：文件齐全、CSV 列/行/无 NaN、PNG 尺寸、事件/落区一致性、
入轨根数合理性、天空轨迹汇总、KML 结构、报告要素。

用法：python scripts/qc.py --config config.json
输出：report/qc_report.json（含逐项 PASS/FAIL），退出码 0=全通过。
"""
import sys, os, json, math, argparse, csv, zipfile
import xml.etree.ElementTree as ET
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
from PIL import Image
from common import notam as N

def _is_return_task(cfg):
    m = cfg.get("mission") or {}
    return bool(cfg.get("return_only")) or str(m.get("profile") or "") == "return" \
        or str(m.get("mission_type") or "") == "return"


RES = []


def chk(name, ok, detail=""):
    RES.append(dict(check=name, ok=bool(ok), detail=str(detail)))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  | " + str(detail)) if detail else ""))


def rd_csv(path):
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    args = ap.parse_args()
    cfg = json.load(open(args.config, encoding="utf-8-sig"))
    if bool(cfg.get("return_only")) or str((cfg.get("mission") or {}).get("profile")
                                           or "") == "return":
        RES.clear()
        st = json.load(open(os.path.join(ROOT, "sim", "空间站轨道.json"),
                            encoding="utf-8-sig")) if os.path.exists(
            os.path.join(ROOT, "sim", "空间站轨道.json")) else {}
        rt = json.load(open(os.path.join(ROOT, "sim", "返回舱返回.json"),
                            encoding="utf-8-sig")) if os.path.exists(
            os.path.join(ROOT, "sim", "返回舱返回.json")) else {}
        sel = rt.get("selected") or {}
        chk("空间站轨道数据可用", bool(st.get("enabled")),
            (st.get("station") or {}).get("source") or st.get("error") or "—")
        chk("返回舱返回已启用且有选中机会", bool(rt.get("enabled")) and bool(sel),
            "窗口=%s 落点=(%s,%s)" % (rt.get("n_windows"), sel.get("lat_deg"),
                                      sel.get("lon_deg")))
        wins = rt.get("windows") or []
        chk("返回窗口时长 > 0", all(float(w.get("duration_s") or 0) > 0 for w in wins)
            if wins else bool(sel), "n=%d" % len(wins))
        csvp = os.path.join(ROOT, "sim", "返回舱_时序.csv")
        rows = rd_csv(csvp) if os.path.exists(csvp) else []
        chk("返回时序 CSV 存在且非空", len(rows) >= 50, "rows=%d" % len(rows))
        pngp = os.path.join(ROOT, "sim", "返回舱弹道.png")
        png_ok = False
        try:
            if os.path.exists(pngp):
                png_ok = Image.open(pngp).size[0] >= 1600
        except Exception:  # noqa: BLE001
            png_ok = os.path.exists(pngp) and os.path.getsize(pngp) > 10000
        chk("返回弹道图存在且宽度 ≥1600 px", png_ok,
            str(Image.open(pngp).size) if os.path.exists(pngp) else "缺")
        kmlp = os.path.join(ROOT, "viz", "返回舱弹道.kml")
        kmzp = os.path.join(ROOT, "viz", "返回舱弹道.kmz")
        kml_ok = False
        try:
            if os.path.exists(kmlp):
                kml_ok = ET.parse(kmlp).getroot().tag.endswith("kml")
        except Exception:  # noqa: BLE001
            kml_ok = False
        chk("返回舱 KML 可解析", kml_ok, "kml=%s" % os.path.exists(kmlp))
        kmz_ok = False
        try:
            if os.path.exists(kmzp):
                with zipfile.ZipFile(kmzp) as z:
                    kmz_ok = "doc.kml" in z.namelist()
        except Exception:  # noqa: BLE001
            kmz_ok = False
        chk("返回舱 KMZ 含 doc.kml", kmz_ok, "kmz=%s" % os.path.exists(kmzp))
        wcsv = os.path.join(ROOT, "sim", "返回舱窗口.csv")
        chk("返回窗口 CSV 存在", os.path.exists(wcsv), wcsv)
        mp = os.path.join(ROOT, "map", "返回舱落区.png")
        map_ok = False
        try:
            if os.path.exists(mp):
                map_ok = Image.open(mp).size[0] >= 1600
        except Exception:  # noqa: BLE001
            map_ok = os.path.exists(mp) and os.path.getsize(mp) > 10000
        chk("返回落区图存在", map_ok, "map/返回舱落区.png")
        phase_csv = os.path.join(ROOT, "sim", "返回舱事件与分离点.csv")
        sep = rt.get("separations") or []
        chk("返回阶段/分离点 CSV 存在", os.path.exists(phase_csv),
            "separations=%d" % len(sep))
        chk("返回阶段/分离点非空", bool(rt.get("phases")) and len(sep) >= 2,
            "phases=%d separations=%d" % (len(rt.get("phases") or []), len(sep)))
        ctxp = os.path.join(ROOT, "sim", "返回前30分钟_空间站与神舟.csv")
        ctx_rows = rd_csv(ctxp) if os.path.exists(ctxp) else []
        ctx_objs = {r.get("object") for r in ctx_rows}
        chk("返回前30min 空间站/神舟轨道", len(ctx_rows) >= 10
            and len(ctx_objs) >= 2, "rows=%d objects=%s" % (len(ctx_rows), ctx_objs))
        rp = os.path.join(ROOT, "report", "弹道仿真报告.md")
        txt = open(rp, encoding="utf-8").read() if os.path.exists(rp) else ""
        chk("返回任务报告存在且含免责声明", bool(txt) and "返回" in txt and "免责" in txt,
            "%.1f KB" % (len(txt.encode("utf-8")) / 1024) if txt else "缺")
        _sum_ok = all(os.path.exists(os.path.join(ROOT, p)) for p in (
            "report/任务摘要.md", "report/成果清单.md", "sim/关键节点.csv"))
        chk("返回任务通用成果（摘要/清单/关键节点）", _sum_ok,
            "report/任务摘要.md / report/成果清单.md / sim/关键节点.csv")
        npass = sum(1 for r in RES if r["ok"])
        print("\n==== QC %d/%d PASS ====" % (npass, len(RES)))
        out = os.path.join(ROOT, "report")
        os.makedirs(out, exist_ok=True)
        json.dump(dict(generated=__import__("datetime").datetime.now().isoformat(
            timespec="seconds"), config=os.path.abspath(args.config),
            total=len(RES), passed=npass, results=RES),
            open(os.path.join(out, "qc_report.json"), "w", encoding="utf-8"),
            ensure_ascii=False, indent=2)
        return 0 if npass == len(RES) else 1
    sim = os.path.join(ROOT, "sim")
    # v3：按实际启用的模块做自检（只跑 export 的任务不该被"缺天空图"扣分）
    MODS = (cfg.get("_v3") or {}).get("modules") or {}
    MISSING = object()

    def mod_on(key):
        return bool(MODS.get(key, True))

    def skip(name, note):
        chk(name + "（未启用，跳过）", True, note)

    # ---- 1. 文件齐全 ----
    need = {
        "sim/trajectory.csv": "弹道时间序列", "sim/debris_trajectories.csv": "残骸轨迹",
        "sim/弹道事件表.csv": "事件表", "sim/落点与射程.csv": "落点与射程",
        "sim/入轨根数.json": "入轨根数", "sim/orbit_track.csv": "首圈星下点",
        "sim/objects.json": "残骸对象", "sim/notam_zones.json": "航警区",
        "sim/trajectory_meta.json": "元数据",
    }
    for rel, desc in need.items():
        chk("文件存在：%s（%s）" % (desc, rel), os.path.exists(os.path.join(ROOT, rel)))

    # ---- 2. CSV 结构与 NaN ----
    spec = {
        "sim/trajectory.csv": (["t_met_s", "lat_deg", "lon_deg", "alt_m", "speed_m_s", "mass_kg", "phase"], 100),
        "sim/debris_trajectories.csv": (["object", "t_met_s", "lat_deg", "lon_deg", "alt_m"], 100),
        "sim/弹道事件表.csv": (["event", "t_met_s", "lat_deg", "lon_deg", "alt_m"], 4),
        "sim/落点与射程.csv": (["object", "impact_t_met_s", "lat_deg", "lon_deg", "zone_id", "inside_zone"], 1),
    }
    for rel, (cols, minrows) in spec.items():
        p = os.path.join(ROOT, rel)
        if not os.path.exists(p):
            continue
        rows = rd_csv(p)
        ok = len(rows) >= minrows and all(c in rows[0] for c in cols)
        # v3：落点未入区时 zone_id / dist_to_boundary_km 允许为空（不是数据缺陷）
        # v3.2：落点不在任何航警区时，这几列允许为空（不是数据缺陷）：
        #   zone_id / dist_to_boundary_km  —— 区外落点
        #   nearest_zone_id / nearest_dist_km —— 该残骸类型没有对应航警区时
        allow_empty = ("zone_id", "dist_to_boundary_km",
                       "nearest_zone_id", "nearest_dist_km")
        nan = any((v or "").strip().lower() in ("nan", "") for r in rows
                  for k, v in r.items() if k not in allow_empty) if rows else True
        chk("CSV 结构：%s" % rel, ok and not nan, "rows=%d cols=%d nan/empty=%s" % (
            len(rows), len(rows[0]) if rows else 0, nan))

    # ---- 3. 事件与落区 ----
    ev_path = os.path.join(sim, "弹道事件表.csv")
    if os.path.exists(ev_path):
        ev = {r["event"]: r for r in rd_csv(ev_path)}
        need_ev = ["起飞", "整流罩分离"]
        chk("事件表含起飞/抛罩/关机类事件",
            all(any(k in e for e in ev) for k in need_ev) and any("SECO" in e for e in ev),
            list(ev.keys()))
    imp_path = os.path.join(sim, "落点与射程.csv")
    if os.path.exists(imp_path):
        imps = rd_csv(imp_path)
        # 只有"有对应航警区"的残骸才参与落区判定（末级/无区对象不参与）
        stages = [s.get("name", "") for s in (cfg.get("vehicle", {}).get("stages") or [])]
        allowed = set()

        def _allow(hint):
            if not hint:
                return
            for i, nm in enumerate(stages):
                if hint in nm or nm in hint:
                    allowed.add("core_stage" if i == 0 else "stage_%d" % i)
            h = hint.lower()
            if "罩" in hint or "fair" in h:
                allowed.update(("fairing_L", "fairing_R", "fairing"))
            if "助推" in hint or "booster" in h:
                allowed.update(("boosters", "booster"))

        for z in (cfg.get("notams") or []):
            if z.get("object_hint"):
                allowed.add(str(z["object_hint"]))      # 具体对象（一对一约束）
            else:
                _allow(z.get("stage_hint", ""))

        def _mapped(obj):
            if not allowed:
                return True
            for a in allowed:
                if obj == a or obj.startswith(a):
                    return True
            return False

        judged = [r for r in imps if _mapped(r.get("object", ""))]
        if not (cfg.get("notams") or []):
            skip("所有落点在对应航警区内", "本任务无航警（预测落区/关闭），不做落区判定")
        elif not judged:
            skip("所有落点在对应航警区内", "没有与航警区对应的残骸对象（检查 stage_hint）")
        else:
            all_in = all(r["inside_zone"].strip().lower() == "true" for r in judged)
            chk("对应残骸落点在航警区内", all_in,
                [(r["object"], r["zone_id"], r["inside_zone"]) for r in judged])
            try:
                margins = [float(r["dist_to_boundary_km"]) for r in judged
                           if r.get("dist_to_boundary_km")]
                if margins:
                    chk("落点距边界余量 > 0 km", all(m > 0 for m in margins),
                        dict(zip([r["object"] for r in judged], [round(m, 2) for m in margins])))
            except Exception:  # noqa: BLE001
                pass
        # 时刻与区有效期一致性
        t0 = cfg["mission"]["t0_utc"]
        from datetime import datetime, timezone, timedelta
        t0dt = datetime.fromisoformat(t0.replace("Z", "+00:00"))
        zmap = {z["id"]: z for z in cfg.get("notams", [])}
        ok_valid = True
        for r in imps:
            z = zmap.get(r["zone_id"])
            if not z:
                # 未入任何区：该残骸本身就判不过"落点在区内"，这里不重复扣分
                continue
            t_imp = t0dt + timedelta(seconds=float(r["impact_t_met_s"]))
            b = datetime.fromisoformat(z["b_utc"].replace("Z", "+00:00"))
            c = datetime.fromisoformat(z["c_utc"].replace("Z", "+00:00"))
            if not (b <= t_imp <= c):
                ok_valid = False
        chk("落点时刻在对应航警区有效期内", ok_valid)

    # ---- 4. 入轨根数 ----
    orb_path = os.path.join(sim, "入轨根数.json")
    if os.path.exists(orb_path):
        o = json.load(open(orb_path, encoding="utf-8"))
        h = o["insertion"]["alt_km"]
        chk("入轨根数合理（e<0.05, 150<h<2000 km, P>4000 s）",
            o["e"] < 0.05 and 150 < h < 2000 and o["period_s"] > 4000,
            "e=%.5f h=%.1f km P=%.1f s i=%.2f°" % (o["e"], h, o["period_s"], o["i_deg"]))

    # ---- 5. 天空轨迹 / 全国地图 ----
    sky_dir = os.path.join(ROOT, "sky")
    if not mod_on("sky"):
        skip("天空轨迹 PNG 存在且宽度 ≥1600 px", "天空轨迹模块未启用")
    elif os.path.isdir(sky_dir):
        pngs = [f for f in glob_list(sky_dir) if f.lower().endswith(".png")]
        chk("天空轨迹 PNG 存在且宽度 ≥1600 px", len(pngs) >= 1 and all(
            Image.open(os.path.join(sky_dir, f)).size[0] >= 1600 for f in pngs),
            {f: Image.open(os.path.join(sky_dir, f)).size for f in pngs})
        sp = os.path.join(sky_dir, "sky_summary.json")
        if os.path.exists(sp):
            s = json.load(open(sp, encoding="utf-8"))
            chk("天空轨迹汇总含首次出现/最大高度角/消失",
                all(k in json.dumps(s, ensure_ascii=False) for k in ("first", "max", "last")))
    else:
        chk("天空轨迹 PNG 存在且宽度 ≥1600 px", False, "sky/ 目录不存在")
    map_dir = os.path.join(ROOT, "map")
    if not mod_on("map"):
        skip("全国地图 PNG 存在且宽度 ≥1600 px", "全国地图模块未启用")
    elif os.path.isdir(map_dir):
        pngs = [f for f in glob_list(map_dir) if f.lower().endswith(".png")]
        chk("全国地图 PNG 存在且宽度 ≥1600 px", len(pngs) >= 1 and all(
            Image.open(os.path.join(map_dir, f)).size[0] >= 1600 for f in pngs),
            {f: Image.open(os.path.join(map_dir, f)).size for f in pngs})
    else:
        chk("全国地图 PNG 存在且宽度 ≥1600 px", False, "map/ 目录不存在")

    # ---- 6. KML/KMZ ----
    kml = os.path.join(ROOT, "viz", "弹道三维_含残骸坠落.kml")
    kmz = os.path.join(ROOT, "viz", "弹道三维_含残骸坠落.kmz")
    if not mod_on("kml"):
        skip("KML XML 可解析 + Placemark + gx:Track 配对", "三维 KML 模块未启用")
        skip("KMZ 自包含（doc.kml + 图标 + CRC）", "三维 KML 模块未启用")
        kml = kmz = ""
    if os.path.exists(kml):
        NS = {"kml": "http://www.opengis.net/kml/2.2", "gx": "http://www.google.com/kml/ext/2.2"}
        try:
            root = ET.parse(kml).getroot()
            pm = root.findall(".//kml:Placemark", NS)
            tracks = root.findall(".//gx:Track", NS)
            pair_ok = all(len(t.findall("kml:when", NS)) == len(t.findall("gx:coord", NS))
                          for t in tracks)
            chk("KML XML 可解析 + Placemark + gx:Track 配对",
                len(pm) >= 20 and len(tracks) >= 1 and pair_ok,
                "placemarks=%d tracks=%d" % (len(pm), len(tracks)))
        except Exception as exc:  # noqa: BLE001
            chk("KML XML 可解析", False, exc)
    if os.path.exists(kmz):
        with zipfile.ZipFile(kmz) as z:
            names = z.namelist()
            chk("KMZ 自包含（doc.kml + 图标 + CRC）",
                "doc.kml" in names and z.testzip() is None and any(n.startswith("files/") for n in names),
                "members=%d" % len(names))

    # ---- 7. 不确定度/窗口/报告 ----
    mc = os.path.join(ROOT, "uncertainty", "mc_stats.json")
    if not mod_on("mc") and not os.path.exists(mc):
        skip("MC 未运行（可选）", "蒙特卡洛模块未启用")
    elif os.path.exists(mc):
        m = json.load(open(mc, encoding="utf-8"))
        chk("MC 统计有效（N≥50 且有 DRMS）", m.get("n_valid", 0) >= 50 and "impacts" in m,
            "n=%s" % m.get("n_valid"))
    else:
        chk("MC 未运行（可选）", True, "uncertainty/mc_stats.json 不存在，跳过")
    win = os.path.join(ROOT, "report", "发射窗口计算.json")
    if not (cfg.get("notams") or []):
        skip("发射窗口含 UTC+北京时区间", "无航警，未计算航警时窗")
    elif os.path.exists(win):
        w = json.load(open(win, encoding="utf-8"))
        mw = w.get("model_window") or {}
        pw = w.get("practical_window") or {}
        chk("发射窗口含 UTC+北京时区间", bool(mw) and bool(pw),
            ("%s → %s" % (mw.get("start_utc"), mw.get("end_utc"))) if mw
            else (w.get("reason") or "无可行航警时窗"))
    # ---- 7b. 轨道平面窗口 / CZ-2F 交会 ----
    if os.path.exists(win):
        w = json.load(open(win, encoding="utf-8"))
        ow = w.get("orbit_window") or {}
        if ow.get("available"):
            wins = ow.get("windows") or []
            ok = bool(wins) and all(float(x.get("duration_s") or 0) > 0 for x in wins)
            tgt = (ow.get("target") or {}).get("kind", "?")
            chk("轨道平面窗口有效（时长>0）", ok,
                "target=%s n=%d tol=%s°" % (tgt, len(wins), ow.get("tolerance_deg")))
            if tgt == "station":
                rv = [x for x in wins if (x.get("rendezvous") or {}).get("feasible")]
                chk("CZ-2F 交会窗口存在可行解", bool(rv),
                    "可行 %d/%d；最近 T0 %s" % (
                        len(rv), len(wins),
                        ((ow.get("best_near_t0") or {}).get("best_utc") or "—")))
        else:
            skip("轨道平面窗口有效（时长>0）", "无平面约束：%s" % (ow.get("reason") or "未计算"))
    # ---- 7c. 空间站 / 交会对接（CZ-2F）----
    st_path = os.path.join(ROOT, "sim", "空间站轨道.json")
    dk_path = os.path.join(ROOT, "sim", "交会对接.json")
    cz2f = str((cfg.get("mission") or {}).get("profile") or "") == "cz2f"
    if not cz2f:
        skip("空间站轨道数据可用（CZ-2F）", "非 CZ-2F 任务")
        skip("交会对接方案自洽（CZ-2F）", "非 CZ-2F 任务")
    else:
        st = {}
        if os.path.exists(st_path):
            st = json.load(open(st_path, encoding="utf-8"))
        chk("空间站轨道数据可用（CZ-2F）", bool(st.get("enabled")),
            (st.get("station") or {}).get("source") or st.get("error") or "—")
        dk = {}
        if os.path.exists(dk_path):
            dk = json.load(open(dk_path, encoding="utf-8"))
        if not dk.get("enabled"):
            chk("交会对接方案自洽（CZ-2F）", False, dk.get("reason") or "未计算")
        else:
            tl = dk.get("timeline") or []
            times = []
            for r0 in tl:
                try:
                    from datetime import datetime as _dt
                    times.append(_dt.fromisoformat(str(r0.get("t_utc")).replace("Z", "+00:00")))
                except Exception:  # noqa: BLE001
                    times.append(None)
            mono = all(times[i] is not None and times[i + 1] is not None
                       and times[i + 1] >= times[i] for i in range(len(times) - 1))
            dock = dk.get("docking") or {}
            budget = float(dk.get("budget_mps") or 300.0)
            total = float(dk.get("total_dv_mps") or 0)
            resid = float(dock.get("residual_range_km") or 0)
            chk("交会对接方案自洽（CZ-2F）",
                bool(tl) and mono and total <= budget and resid <= 1.0,
                "阶段=%d 单调=%s ΔV=%.0f/%.0f m/s 末端残余=%.2f km 对接=%s" % (
                    len(tl), mono, total, budget, resid, dock.get("t_utc", "—")))
            ctxp = os.path.join(ROOT, "sim", "CZ2F_任务前后30min_空间站.csv")
            if os.path.exists(ctxp):
                crows = rd_csv(ctxp)
                _t0wp = os.path.join(ROOT, "report", "交会发射窗口.json")
            _t0w = {}
            try:
                if os.path.exists(_t0wp):
                    _t0w = json.load(open(_t0wp, encoding="utf-8-sig"))
            except Exception:  # noqa: BLE001
                _t0w = {}
            chk("交会发射窗口（T0 反推）存在且可用",
                bool(_t0w.get("enabled")) and bool(_t0w.get("available")),
                "推荐 T0=%s，可行窗口 %s 个，T0 偏移 %s s" % (
                    _t0w.get("recommended_t0_utc") or "—",
                    _t0w.get("feasible_n"), _t0w.get("shift_s")))
            chk("CZ2F 任务前后30min 空间站轨道", len(crows) >= 10,
                    "rows=%d" % len(crows))
        # v3.5 返回舱
        rt_path = os.path.join(ROOT, "sim", "返回舱返回.json")
        if mod_on("return") or os.path.exists(rt_path):
            rt = json.load(open(rt_path, encoding="utf-8")) if os.path.exists(rt_path) else {}
            sel = rt.get("selected") or {}
            csvp = os.path.join(ROOT, "sim", "返回舱_时序.csv")
            csv_ok = os.path.exists(csvp) and os.path.getsize(csvp) > 100
            pngp = os.path.join(ROOT, "sim", "返回舱弹道.png")
            png_ok = False
            try:
                if os.path.exists(pngp):
                    png_ok = Image.open(pngp).size[0] >= 1600
            except Exception:  # noqa: BLE001
                png_ok = os.path.exists(pngp) and os.path.getsize(pngp) > 10000
            chk("返回舱返回窗口/弹道自洽",
                bool(rt.get("enabled")) and bool(sel) and csv_ok and png_ok
                and abs(float(sel.get("lat_deg") or 0)) <= 90.0
                and abs(float(sel.get("lon_deg") or 0)) <= 180.0
                and (not rt.get("windows") or all(
                    float(w.get("duration_s") or 0) > 0 for w in rt.get("windows") or [])),
                "窗口=%d 落点=(%.2f,%.2f) 时序=%s 图=%s" % (
                    int(rt.get("n_windows") or 0),
                    float(sel.get("lat_deg") or 0), float(sel.get("lon_deg") or 0),
                    "有" if csv_ok else "缺", "有" if png_ok else "缺"))
    # ---- v4：通用成果（任务摘要 / 关键节点 / 飞行阶段 / 成果清单） ----
    if "summary" in MODS and not MODS.get("summary"):
        skip("通用成果（任务摘要/关键节点/成果清单）", "summary 模块未启用")
    else:
        _v4_items = [("report/任务摘要.md", "任务摘要"),
                     ("report/成果清单.md", "成果清单"),
                     ("sim/关键节点.csv", "关键节点表"),
                     ("sim/轨迹统计.json", "轨迹统计"),
                     ("sim/飞行阶段.csv", "飞行阶段表")]
        for rel, name in _v4_items:
            p = os.path.join(ROOT, rel)
            if os.path.exists(p):
                sz = os.path.getsize(p)
                chk("通用成果：%s 存在且非空" % name, sz > 60,
                    "%s（%.1f KB）" % (rel, sz / 1024.0))
            elif "summary" in MODS:
                chk("通用成果：%s 存在" % name, False, rel + " 缺失")
            else:
                skip("通用成果：%s" % name, "旧版工程（无 summary 步骤）")
        kn = os.path.join(ROOT, "sim", "关键节点.csv")
        if os.path.exists(kn):
            krows = rd_csv(kn)
            want = ("着陆",) if _is_return_task(cfg) else ("起飞",)
            chk("关键节点表覆盖起飞/着陆等关键事件",
                len(krows) >= 2 and any(any(w in (r.get("节点") or "") for w in want)
                                        for r in krows),
                "rows=%d" % len(krows))

    rp = os.path.join(ROOT, "report", "弹道仿真报告.md")
    if not mod_on("report") and not os.path.exists(rp):
        skip("报告存在（report/弹道仿真报告.md）", "报告模块未启用")
    elif os.path.exists(rp):
        txt = open(rp, encoding="utf-8").read()
        nch = len([l for l in txt.splitlines() if l.startswith("## ")])
        chk("报告 ≥12 章、含免责声明与来源", nch >= 12 and "免责" in txt and "http" in txt,
            "%.1f KB, %d 章" % (len(txt.encode("utf-8")) / 1024, nch))
    else:
        chk("报告存在（report/弹道仿真报告.md）", False)

    npass = sum(1 for r in RES if r["ok"])
    print("\n==== QC %d/%d PASS ====" % (npass, len(RES)))
    out = os.path.join(ROOT, "report")
    os.makedirs(out, exist_ok=True)
    json.dump(dict(generated=__import__("datetime").datetime.now().isoformat(timespec="seconds"),
                   config=os.path.abspath(args.config), total=len(RES), passed=npass,
                   results=RES), open(os.path.join(out, "qc_report.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("→ report/qc_report.json")
    return 0 if npass == len(RES) else 1


def glob_list(d):
    return [f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f))]


if __name__ == "__main__":
    sys.exit(main())
