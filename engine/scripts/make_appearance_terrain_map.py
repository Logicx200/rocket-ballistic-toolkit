# -*- coding: utf-8 -*-
"""出现方位地形判读图（局部放大）：按观测点周边地形快速判断火箭出现方位。

v3.0 参数化
-----------
* 观测点：``--obs``（缺省 = config.observers[0].id），坐标读 config.json；
* 出现方位/最大高度角/消失方位/首次出现时刻：读 ``sky/sky_summary.json`` 的
  ``observers.<obs_id>.{first_appearance,max_elevation,last_visible}``；
  缺字段时退回 ``sky/天空轨迹_<obs_id>.csv`` 现算；
* 出现方位扇区：按 ``first_appearance.az_deg`` 自动取 ±40°（越界时整体平移）；
* ``sky/sky_summary.json`` 缺失 → 明确报错并以退出码 2 结束（上游按可选步骤处理）。

用途：首次出现方位 A、仰角仅 ~1.3°（≈该方位天空线）时，说明火箭是"从山脊后
升起"。本图给出观测点 30 km 内地形晕渲 + 等高线 + 方位射线 + 各方位天空线 +
出现方位地形剖面，便于现场按地形（山脊/沟谷/建筑）快速判断火箭出现方向。

两条天空线：
  1) **稳健天空线**（主曲线）：观测点海拔 + 人眼高 1.6 m、忽略最近 200 m、地形高程
     5 点（300 m）中值去噪 —— 代表"实际地平线/山脊线"，适合现场判读；
  2) **规范判据**（细虚线，仅作对照）：sky 模块可见性计算所用口径（SRTM 1″/90 m、
     200 km、近场 1″ 精采样）。该口径会受观测点周边 ~90 m 处 SRTM 单像元噪声影响，
     出现"台阶/平台"——不是真实地形。

输出：
  sky/出现方位地形示意图_<obs_id>.png
  sky/出现方位地形_数据.json
用法：python engine/scripts/make_appearance_terrain_map.py --config config.json [--obs <id>]
"""
import os
import sys, os, json, math, csv
import numpy as np
ROOT = os.environ.get("RK_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- v3 引擎/工作区解耦（自动补丁） ---
_ENG = os.environ.get("RK_ENGINE")
if _ENG:
    for _p in (_ENG, os.path.join(_ENG, "scripts")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
# --- end ---
sys.path.insert(0, ROOT)
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LightSource
from matplotlib.collections import LineCollection
from matplotlib.patches import Circle
import matplotlib.patheffects as pe
from common import dem, plotstyle

# ---- 运行时参数（main() 里按 config/sky_summary.json 填充；此处仅占位默认值） ----
OBS_ID = None                # 观测点 id：--obs > config.observers[0].id > sky_summary 首个
CFG_PATH = os.path.join(ROOT, "config.json")
CFG_FALLBACK = os.path.join(ROOT, "common", "config.json")
SKY_DIR = os.path.join(ROOT, "sky")
SKY_SUMMARY = os.path.join(SKY_DIR, "sky_summary.json")
R_KM = 30.0
STEP_M = 60
EYE_M = 1.6                 # 人眼高
MIN_KM = 0.2                # 忽略最近 200 m（SRTM 单像元噪声）
MED_WIN = 5                 # 5 点 × 60 m = 300 m 中值去噪
AZ_FIRST = 0.0              # 首次出现方位 [deg]（sky_summary 驱动）
AZ_MAX = 0.0                # 最大高度角方位
AZ_LAST = 0.0               # 消失方位
EL_FIRST = 0.0              # 首次出现仰角
EL_MAX = 0.0                # 最大高度角
EL_LAST = 0.0               # 消失仰角
AZ_SEC_HALF = 40.0          # 出现方位扇区半宽：first_appearance ±40°
AZ_SEC_LO, AZ_SEC_HI = 320.0, 360.0
T_FIRST_S = float("nan")    # 首次出现 MET / 北京时（图注用）
T_FIRST_BJ = ""
OBS_SUMMARY = {}            # sky_summary.json 里该观测点的完整 dict
OUT_PNG = None              # 出现方位地形示意图输出路径（main 中按命名规则赋值）
OUT_JSON = None             # 出现方位地形数据 JSON 路径
RE_MEAN = 6371008.8


def load_config(cfg_path=None):
    p = cfg_path or (CFG_PATH if os.path.exists(CFG_PATH) else CFG_FALLBACK)
    return json.load(open(p, encoding="utf-8"))


_ILLEGAL_CHARS = '<>:"/\\|?*'


def _safe_label(name: str) -> str:
    s = "".join(("_" if ch in _ILLEGAL_CHARS else ch) for ch in str(name or "").strip())
    return s or "观测点"


def _obs_place_tag(obs: dict) -> str:
    lat, lon = float(obs["lat_deg"]), float(obs["lon_deg"])
    return "%s_%.2f%s_%.2f%s" % (
        _safe_label(obs.get("name") or obs.get("id")),
        abs(lat), ("N" if lat >= 0 else "S"),
        abs(lon), ("E" if lon >= 0 else "W"))


def _appear_paths(summ, obs: dict, obs_id: str):
    """出现方位地形示意图/数据文件名（多观测点带地点名+经纬度）。"""
    multi = (bool((summ.get("naming") or {}).get("multi_observer"))
             or len(summ.get("observers") or {}) > 1)
    tag = _obs_place_tag(obs) if multi else obs_id
    base = os.path.join(ROOT, "sky")
    return (os.path.join(base, "出现方位地形示意图_%s.png" % tag),
            os.path.join(base, "出现方位地形_数据.json"))


def _cfg_observers(cfg):
    obs = cfg.get("observers")
    if not obs:
        obs = (cfg.get("mission") or {}).get("observers")
    return list(obs or [])


def load_sky_summary(obs_id):
    """读 sky/sky_summary.json；缺失 → 打印明确错误并退出码 2。"""
    if not os.path.exists(SKY_SUMMARY):
        print("[appear] 错误: 缺少天空轨迹汇总 %s" % SKY_SUMMARY)
        print("         请先运行 plot_sky.py 生成 sky/sky_summary.json（本步骤可选，上游据此跳过）。")
        raise SystemExit(2)
    summ = json.load(open(SKY_SUMMARY, encoding="utf-8"))
    obs = summ.get("observers") or {}
    if not obs:
        print("[appear] 错误: %s 中没有 observers（请先运行 plot_sky.py）" % SKY_SUMMARY)
        raise SystemExit(2)
    oid = obs_id if obs_id in obs else next(iter(obs))
    if obs_id is not None and oid != obs_id:
        print("[appear] 警告: sky_summary.json 中无观测点 %r，改用 %r" % (obs_id, oid))
    return summ, obs[oid], oid


def _fallback_from_track(obs_id):
    """sky_summary 缺字段时，从天空轨迹 CSV 现算出现/最大/消失方位与仰角。

    v3.2.3：优先用 sky_summary 里记录的实际 CSV 路径（多观测点文件名含地点名+经纬度）。
    """
    path = ""
    _out = (OBS_SUMMARY.get("outputs") or {}) if isinstance(OBS_SUMMARY, dict) else {}
    _csv = _out.get("csv")
    if isinstance(_csv, dict) and _csv.get("path"):
        path = os.path.join(ROOT, str(_csv["path"]))
    if not path or not os.path.exists(path):
        path = os.path.join(SKY_DIR, "天空轨迹_%s.csv" % obs_id)
    if not os.path.exists(path):
        return {}
    try:
        import csv
        rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
        vis = [r for r in rows
               if str(r.get("geometric_visible", "1")).strip() in ("1", "True", "true")]
        if not vis:
            vis = rows
        t = np.array([float(r["t_met_s"]) for r in vis])
        az = np.array([float(r["az_deg"]) for r in vis])
        el = np.array([float(r["el_deg"]) for r in vis])
        k = int(np.argmax(el))
        t_bj = ""
        try:
            from datetime import datetime, timezone, timedelta
            dt = datetime.fromisoformat(str(vis[0].get("t_utc", "")).replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            t_bj = (dt + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:  # noqa: BLE001
            t_bj = ""
        return {"az_first": float(az[0]), "el_first": float(el[0]),
                "az_max": float(az[k]), "el_max": float(el[k]),
                "az_last": float(az[-1]), "el_last": float(el[-1]),
                "t_first": float(t[0]), "t_first_bj": t_bj}
    except Exception as exc:  # noqa: BLE001
        print("[appear] 警告: 从天空轨迹 CSV 兜底计算出现方位失败: %s" % exc)
        return {}


def load_track(obs_id):
    """读取天空轨迹 CSV（t/方位/高度/可见性），用于把弹道画到出现方位图上。

    返回 None 表示缺 CSV；调用方跳过弹道叠加（不影响其余图）。
    """
    path = ""
    _out = (OBS_SUMMARY.get("outputs") or {}) if isinstance(OBS_SUMMARY, dict) else {}
    _csv = _out.get("csv")
    if isinstance(_csv, dict) and _csv.get("path"):
        path = os.path.join(ROOT, str(_csv["path"]))
    if not path or not os.path.exists(path):
        path = os.path.join(SKY_DIR, "天空轨迹_%s.csv" % obs_id)
    if not os.path.exists(path):
        return None
    try:
        rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
        if not rows:
            return None
        t = np.array([float(r["t_met_s"]) for r in rows])
        az = np.array([float(r["az_deg"]) for r in rows])
        el = np.array([float(r["el_deg"]) for r in rows])
        gr = np.array([float(r.get("ground_range_km") or "nan") for r in rows])
        vis = np.array([str(r.get("geometric_visible", "1")).strip() in ("1", "True", "true")
                        for r in rows])
        return dict(t=t, az=az, el=el, gr=gr, vis=vis, path=path)
    except Exception as exc:  # noqa: BLE001
        print("[appear] 警告: 读取天空轨迹 CSV 失败（跳过弹道叠加）：%s" % exc)
        return None


def _draw_track_on_map(ax, track, hz_az=None, hz=None):
    """在观测点地形图上叠加弹道方位：外圈方位弧 + 内侧扫描扇（颜色=时间）。

    说明：这是"观测方向"叠加，不是弹道的真实地面投影——火箭在 160 km 以外，
    30 km 局部图放不下；真实 A–E 轨迹见右侧 (e) 小图。
    """
    if track is None or len(track["t"]) < 2:
        return
    t, az, el = track["t"], track["az"], track["el"]
    ok = np.isfinite(az) & np.isfinite(el)
    if ok.sum() < 2:
        return
    cmap = plt.get_cmap("turbo")
    norm = plt.Normalize(float(t[ok].min()), max(1.0, float(t[ok].max())))

    # ---- 内侧扫描扇：每 ~15 s 一条方位射线，颜色=时间 ----
    rr = R_KM * 0.90
    k = max(1, len(t[ok]) // 60)
    for i in range(0, len(t[ok]), k):
        aa = math.radians(float(az[ok][i]))
        ax.plot([0.0, rr * math.sin(aa)], [0.0, rr * math.cos(aa)],
                color=cmap(norm(t[ok][i])), lw=0.8, alpha=0.32, zorder=5)

    # ---- 外圈：弹道方位弧（按时间着色，更醒目）----
    rr2 = R_KM * 0.925
    a = np.radians(az[ok])
    x = rr2 * np.sin(a)
    y = rr2 * np.cos(a)
    pts = np.column_stack([x, y]).reshape(-1, 1, 2)
    segs = np.concatenate([pts[:-1], pts[1:]], axis=1)
    ax.plot(x, y, color="#222222", lw=6.4, alpha=0.55, zorder=8)
    lc = LineCollection(segs, cmap=cmap, norm=norm, linewidths=4.6, zorder=9,
                        capstyle="butt")
    lc.set_array(t[ok][:-1])
    ax.add_collection(lc)
    # 首/最大/消失三个时刻在弧上打点
    for azv, mk, col in ((AZ_FIRST, "*", "#0a8f2d"), (AZ_MAX, "o", "#d17a00"),
                         (AZ_LAST, "s", "#d6006e")):
        aa = math.radians(azv)
        ax.plot([rr2 * math.sin(aa)], [rr2 * math.cos(aa)], marker=mk, ms=11,
                mfc=col, mec="white", mew=1.0, ls="none", zorder=10)
    ax.text(0.30 * R_KM, 0.92 * R_KM,
            "弹道方位扫描 / 外圈彩弧\nT+%.0f→%.0f s" % (t[ok].min(), t[ok].max()),
            fontsize=7.2, color="#2b2b2b", ha="left", va="center", zorder=11,
            bbox=dict(boxstyle="round,pad=0.22", fc="white", ec="#777777", lw=0.6,
                      alpha=0.9))


def appearance_values(obs_sum, obs_id):
    """从 sky_summary 的 first_appearance/max_elevation/last_visible 提取角度。"""
    first = obs_sum.get("first_appearance") or {}
    maxe = obs_sum.get("max_elevation") or {}
    last = obs_sum.get("last_visible") or {}
    fb = _fallback_from_track(obs_id)

    def pick(d, key, fbkey=None):
        v = d.get(key)
        if v is None:
            v = fb.get(fbkey or key)
        return v

    vals = dict(
        az_first=pick(first, "az_deg", "az_first"),
        el_first=pick(first, "el_deg", "el_first"),
        az_max=pick(maxe, "az_deg", "az_max"),
        el_max=pick(maxe, "el_deg", "el_max"),
        az_last=pick(last, "az_deg", "az_last"),
        el_last=pick(last, "el_deg", "el_last"),
        t_first=pick(first, "t_met_s", "t_first"),
        t_first_bj=(first.get("t_bj") or fb.get("t_first_bj") or ""),
    )
    missing = [k for k, v in vals.items() if v is None]
    if missing:
        print("[appear] 警告: sky_summary 缺字段 %s，且天空轨迹 CSV 无法兜底；"
              "相关标注用 0° 占位（图仍可生成）。" % ", ".join(missing))
    for k in ("az_first", "el_first", "az_max", "el_max", "az_last", "el_last"):
        vals[k] = float(vals[k]) if vals[k] is not None else 0.0
    vals["t_first"] = float(vals["t_first"]) if vals["t_first"] is not None else float("nan")
    return vals


def sector_bounds(az_first, half=AZ_SEC_HALF):
    """首次出现方位 ±half 的扇区（宽度 2*half，越界时整体平移到 0–360）。"""
    lo, hi = az_first - half, az_first + half
    if hi > 360.0:
        hi, lo = 360.0, 360.0 - 2.0 * half
    if lo < 0.0:
        lo, hi = 0.0, 2.0 * half
    return lo, hi


def load_observer(obs_id=None, cfg_path=None):
    p = cfg_path or (CFG_PATH if os.path.exists(CFG_PATH) else CFG_FALLBACK)
    cfg = load_config(p)
    obs_list = _cfg_observers(cfg)
    if not obs_list:
        raise SystemExit("[appear] 错误: config.json 中没有 observers；无法生成出现方位地形图")
    oid = obs_id or OBS_ID or obs_list[0].get("id") or "obs1"
    o = next((x for x in obs_list if x.get("id") == oid), obs_list[0])
    try:
        info = dem.load_observer_cache(oid)
    except Exception:  # noqa: BLE001 - 无 3″ 缓存时直接用 1″ 瓦片
        info = None
    lat0, lon0 = o["lat_deg"], o["lon_deg"]
    alt_obs = float(dem.sample(lat0, lon0, method="nearest"))
    return o, info, lat0, lon0, alt_obs


def terrain_grid(lat0, lon0, r_km, n=780):
    dlat = r_km / 111.19
    dlon = r_km / (111.19 * math.cos(math.radians(lat0)))
    lats = np.linspace(lat0 + dlat, lat0 - dlat, n)
    lons = np.linspace(lon0 - dlon, lon0 + dlon, n)
    LON, LAT = np.meshgrid(lons, lats)
    try:
        z = dem.sample(LAT, LON, method="bilinear", missing="nan")
        if np.isfinite(z).mean() > 0.999:
            return lats, lons, np.asarray(z, dtype=float), "SRTM1″ 1″"
    except Exception:  # noqa: BLE001
        pass
    info = None
    try:
        info = dem.load_observer_cache(OBS_ID)
    except Exception:  # noqa: BLE001
        info = None
    if info is not None:
        z = dem.sample_from_cache(info, LAT, LON)
        return lats, lons, np.asarray(z, dtype=float), "SRTM1″ 3″缓存"
    z = dem.sample(LAT, LON, method="nearest", missing="nan")
    return lats, lons, np.asarray(z, dtype=float), "SRTM1″ 1″瓦片"


def km_grid(lat0, lon0, lats, lons):
    x = (lons - lon0) * 111.19 * math.cos(math.radians(lat0))
    y = (lats - lat0) * 111.19
    return np.meshgrid(x, y)


def robust_profile(lat0, lon0, alt_dem, az, max_km):
    """稳健剖面：人眼高 + 300 m 中值去噪；返回 (dist_km, dem_raw, dem_smooth, el_smooth)。"""
    try:
        info = dem.load_observer_cache(OBS_ID)
    except Exception:  # noqa: BLE001
        info = None
    p = dem.terrain_profile(lat0, lon0, alt_dem + EYE_M, float(az), max_range_km=max_km,
                            step_m=STEP_M, info=info,
                            near_field_km=10.0)
    d = np.asarray(p["dist_km"]); z = np.asarray(p["dem_m"])
    zz = z.copy()
    k = MED_WIN // 2
    # v2：滑动窗口中值（原逐点 np.median 循环，O(n·w) Python 开销）
    if z.size > 2 * k:
        w = np.lib.stride_tricks.sliding_window_view(np.pad(z, k, mode="edge"), 2 * k + 1)
        zz = np.median(w, axis=1)
        # 端点保持与原实现一致：窗口在端点处截断（而非边缘复制）
        for i in list(range(k)) + list(range(z.size - k, z.size)):
            zz[i] = np.median(z[max(0, i - k):min(z.size, i + k + 1)])
    else:
        for i in range(z.size):
            zz[i] = np.median(z[max(0, i - k):min(z.size, i + k + 1)])
    e = np.degrees(np.arctan2(zz - (alt_dem + EYE_M), d * 1000.0 - (d * 1000.0) ** 2 / (2 * RE_MEAN)))
    return d, z, zz, e


def robust_horizon(lat0, lon0, alt_dem, az_list, max_km):
    out = np.empty(len(az_list))
    for i, a in enumerate(az_list):
        d, z, zz, e = robust_profile(lat0, lon0, alt_dem, float(a), max_km)
        m = d >= MIN_KM
        out[i] = float(np.nanmax(e[m]))
    return out


def main():
    global OBS_ID, AZ_FIRST, AZ_MAX, AZ_LAST, EL_FIRST, EL_MAX, EL_LAST
    global AZ_SEC_LO, AZ_SEC_HI, T_FIRST_S, T_FIRST_BJ, OBS_SUMMARY
    import argparse
    ap = argparse.ArgumentParser(description="出现方位地形判读图（局部放大）")
    ap.add_argument("--obs", default=None, help="观测点 id（缺省取 config observers[0].id）")
    ap.add_argument("--config", default=None, help="任务配置（默认 <root>/config.json）")
    args = ap.parse_args()

    cfg = load_config(args.config)
    obs_list = _cfg_observers(cfg)
    default_obs = (obs_list[0].get("id") if obs_list else None) or "obs1"
    OBS_ID = args.obs or default_obs

    # sky/sky_summary.json 缺失 → 退出码 2（上游按可选步骤处理）
    _summ, obs_sum, OBS_ID = load_sky_summary(OBS_ID)
    OBS_SUMMARY = obs_sum
    global OUT_PNG, OUT_JSON
    OUT_PNG, OUT_JSON = _appear_paths(_summ, obs_sum, OBS_ID)
    vals = appearance_values(obs_sum, OBS_ID)
    AZ_FIRST = vals["az_first"]
    AZ_MAX = vals["az_max"]
    AZ_LAST = vals["az_last"]
    EL_FIRST = vals["el_first"]
    EL_MAX = vals["el_max"]
    EL_LAST = vals["el_last"]
    T_FIRST_S = vals["t_first"]
    T_FIRST_BJ = vals["t_first_bj"]
    AZ_SEC_LO, AZ_SEC_HI = sector_bounds(AZ_FIRST)
    print("[appear] 观测点 %s；出现方位（sky_summary）：首次 A=%.2f° E=%.2f°、"
          "最大高度角 A=%.2f° E=%.2f°、消失 A=%.2f° E=%.2f°；扇区 %.0f–%.0f°"
          % (OBS_ID, AZ_FIRST, EL_FIRST, AZ_MAX, EL_MAX, AZ_LAST, EL_LAST,
             AZ_SEC_LO, AZ_SEC_HI))

    plotstyle.setup()
    try:
        return _main_impl(args)
    except FileNotFoundError as exc:
        print("[appear] 警告: 缺少 SRTM DEM（%s）" % exc)
        print("         → 跳过「出现方位地形判读图」；先运行 python scripts/fetch_dem.py 再重跑。")
        return 0


def _main_impl(args):
    global OBS_ID
    o, info, lat0, lon0, alt0 = load_observer(OBS_ID, args.config)
    if not args.obs:
        OBS_ID = o.get("id", OBS_ID)
    lats, lons, Z, zsrc = terrain_grid(lat0, lon0, R_KM)
    X, Y = km_grid(lat0, lon0, lats, lons)

    ls = LightSource(azdeg=315, altdeg=42)
    dx_m = abs(float((Y[1, 0] - Y[0, 0]) * 1000.0))
    dy_m = abs(float((X[0, 1] - X[0, 0]) * 1000.0))
    hs = ls.hillshade(np.nan_to_num(Z, nan=np.nanmedian(Z)), vert_exag=1.6,
                      dx=dx_m, dy=dy_m, fraction=1.15)

    # 规范判据（sky 模块口径，仅作对照）
    az_full = np.arange(0, 360.0, 1.0)
    h_spec = np.asarray(dem.terrain_horizon_deg(lat0, lon0, None, az_list_deg=az_full,
                                                max_range_km=200.0, step_m=90,
                                                info=info), dtype=float)
    # 稳健天空线
    h_rob = robust_horizon(lat0, lon0, alt0, az_full, 200.0)

    az_sec = np.arange(AZ_SEC_LO, AZ_SEC_HI + 0.001, 0.5)
    h_rob_sec = robust_horizon(lat0, lon0, alt0, az_sec, R_KM)
    h_spec_sec = np.asarray(dem.terrain_horizon_deg(lat0, lon0, None, az_list_deg=az_sec,
                                                    max_range_km=R_KM, step_m=90, info=info),
                            dtype=float)

    # 出现方位剖面（稳健）
    d_f, z_f_raw, z_f, el_f = robust_profile(lat0, lon0, alt0, AZ_FIRST, R_KM)
    m = d_f >= MIN_KM
    i_r = int(np.nanargmax(el_f[m]))
    ridge = dict(dist_km=float(d_f[m][i_r]), dem_m=float(z_f[m][i_r]), el_deg=float(el_f[m][i_r]))
    hor_first_rob = float(np.interp(AZ_FIRST, az_full, h_rob))
    hor_first_spec = float(np.interp(AZ_FIRST, az_full, h_spec))

    fig = plt.figure(figsize=(16.2, 12.9))
    gs = fig.add_gridspec(4, 2, width_ratios=[1.30, 1.0],
                          height_ratios=[1.0, 1.0, 1.0, 0.95],
                          wspace=0.16, hspace=0.40)

    # ---------------- (a) 局部地形 ----------------
    ax = fig.add_subplot(gs[:, 0])
    ax.imshow(hs, extent=[X.min(), X.max(), Y.min(), Y.max()], origin="upper",
              cmap="gray", alpha=0.92, vmin=0, vmax=1, zorder=1)
    cs = ax.contourf(X, Y, Z, levels=14, cmap="gist_earth", alpha=0.55, zorder=2)
    cl = ax.contour(X, Y, Z, levels=12, colors="#3a3a3a", linewidths=0.45, alpha=0.75, zorder=3)
    ax.clabel(cl, inline=True, fontsize=5.8, fmt="%d", colors="#222222")
    cb = fig.colorbar(cs, ax=ax, fraction=0.035, pad=0.015)
    cb.set_label("地形高程 (m, EGM96)", fontsize=9)

    for rr in (5, 10, 20, 30):
        ax.add_patch(Circle((0, 0), rr, fill=False, ec="#1b3a63", lw=0.9, ls=(0, (5, 3)),
                            alpha=0.85, zorder=6))
        ax.text(rr * 0.7071, rr * 0.7071, "%d km" % rr, fontsize=7.6, color="#1b3a63",
                ha="left", va="bottom", zorder=8,
                path_effects=[pe.withStroke(linewidth=2, foreground="white")])

    RAYS = [(AZ_FIRST, "#0a8f2d",
             "首次出现 A=%.1f°\n仰角仅 %.2f°（从山脊后升起）" % (AZ_FIRST, EL_FIRST), 1.8),
            (AZ_MAX, "#d17a00", "最大高度角方位 A=%.1f°" % AZ_MAX, 1.3),
            (AZ_LAST, "#d6006e", "消失方位 A=%.1f°" % AZ_LAST, 1.1)]
    for az, col, lab, w in RAYS:
        a = math.radians(az)
        xe, ye = R_KM * math.sin(a), R_KM * math.cos(a)
        ax.annotate("", xy=(xe, ye), xytext=(0, 0),
                    arrowprops=dict(arrowstyle="-|>", color=col, lw=w, alpha=0.92,
                                    shrinkA=3, shrinkB=1, mutation_scale=15), zorder=7)
        xt, yt = 0.70 * xe, 0.70 * ye
        ax.text(xt, yt, lab, fontsize=8.3, color=col, fontweight="bold", zorder=9,
                ha="center", va="center",
                bbox=dict(boxstyle="round,pad=0.28", fc="white", ec=col, lw=1.0, alpha=0.92))

    a = math.radians(AZ_FIRST)
    xr, yr = ridge["dist_km"] * math.sin(a), ridge["dist_km"] * math.cos(a)
    ax.plot([xr], [yr], marker="v", ms=12, mfc="#ffd400", mec="#7a3b00", mew=1.2, zorder=10)
    # 标注文字位置随山脊方位自适应并夹在地图内，避免贴边时溢出到其它子图
    _tx = 0.58 * xr + 6.0 * math.cos(a)
    _ty = 0.58 * yr - 6.0 * math.sin(a)
    _tx = min(max(_tx, -R_KM + 16.0), R_KM - 16.0)
    _ty = min(max(_ty, -R_KM + 9.0), R_KM - 9.0)
    ax.annotate("遮挡山脊（天空线 %.2f°）\n距 %.1f km，高程 %.0f m"
                % (ridge["el_deg"], ridge["dist_km"], ridge["dem_m"]),
                (xr, yr), xytext=(_tx, _ty), textcoords="data",
                fontsize=8.2, ha="center", va="center",
                color="#4a2400", zorder=10,
                bbox=dict(boxstyle="round,pad=0.26", fc="#fff3cc", ec="#7a3b00", lw=0.9, alpha=0.96),
                arrowprops=dict(arrowstyle="-", color="#7a3b00", lw=0.8, shrinkA=0, shrinkB=4))

    ax.plot([0], [0], marker="*", ms=23, mfc="#e60000", mec="white", mew=1.1, zorder=11)
    ax.annotate("观测点\n%.6f°N, %.6f°E\n高程 %.0f m（+人眼 %.1f m）"
                % (lat0, lon0, alt0, EYE_M), (0, 0), textcoords="offset points", xytext=(16, -44),
                fontsize=8.6, fontweight="bold", color="#7a0000", zorder=11,
                bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="#7a0000", lw=0.9, alpha=0.96),
                arrowprops=dict(arrowstyle="-", color="#7a0000", lw=0.8, shrinkA=0, shrinkB=6))

    for az in range(0, 360, 45):
        aa = math.radians(az)
        x0, y0 = (R_KM - 1.3) * math.sin(aa), (R_KM - 1.3) * math.cos(aa)
        ax.text(x0, y0, {0: "北 0°", 45: "45°", 90: "东 90°", 135: "135°", 180: "南 180°",
                         225: "225°", 270: "西 270°", 315: "315°"}[az],
                fontsize=7.4, ha="center", va="center", color="#111111", zorder=9,
                bbox=dict(boxstyle="round,pad=0.18", fc="white", ec="none", alpha=0.72))

    ax.set_xlim(-R_KM, R_KM); ax.set_ylim(-R_KM, R_KM)
    ax.set_aspect("equal")
    ax.set_xlabel("东向距离 (km)", fontsize=10); ax.set_ylabel("北向距离 (km)", fontsize=10)
    ax.set_title("(a) 观测点周边 %.0f km 地形晕渲 + 等高线（%s）" % (R_KM, zsrc), fontsize=11)
    ax.grid(True, ls=":", lw=0.5, alpha=0.35)
    ax.plot([-R_KM + 2.5, -R_KM + 12.5], [-R_KM + 2.2, -R_KM + 2.2], color="black", lw=3)
    ax.plot([-R_KM + 7.5, -R_KM + 12.5], [-R_KM + 2.2, -R_KM + 2.2], color="white", lw=3)
    for xx, tt in ((-R_KM + 2.5, "0"), (-R_KM + 7.5, "5"), (-R_KM + 12.5, "10 km")):
        ax.text(xx, -R_KM + 3.5, tt, fontsize=7.2, ha="center", va="bottom")
    ax.annotate("", xy=(R_KM - 3.0, R_KM - 1.0), xytext=(R_KM - 3.0, R_KM - 7.0),
                arrowprops=dict(arrowstyle="-|>", color="black", lw=1.6), zorder=12)
    ax.text(R_KM - 3.0, R_KM - 0.4, "N", fontsize=10, fontweight="bold", ha="center", va="bottom")

    # v3.4：把弹道叠加到出现方位地形图上（外圈方位弧 + A–E 小图）
    TRACK = load_track(OBS_ID)
    _draw_track_on_map(ax, TRACK, az_full, h_spec)

    # ---------------- (b) 全周天空线 ----------------
    ax2 = fig.add_subplot(gs[0, 1])
    ax2.fill_between(az_full, -1.5, h_rob, color="#D2B48C", alpha=0.55, lw=0)
    ax2.plot(az_full, h_rob, color="#8B4513", lw=2.0, label="稳健天空线（人眼高+去噪）")
    ax2.plot(az_full, h_spec, color="#777777", lw=1.0, ls="--",
             label="规范判据（sky 模块口径，含近场噪声）")
    ax2.axhline(0, color="#999999", lw=0.8, ls=(0, (4, 3)))
    for az, col, lab in ((AZ_FIRST, "#0a8f2d", "首次出现"), (AZ_MAX, "#d17a00", "最大高度角"),
                         (AZ_LAST, "#d6006e", "消失")):
        ax2.axvline(az, color=col, lw=1.0, ls=":")
        ax2.annotate("%s\nA=%.1f°" % (lab, az), (az, 0.2), textcoords="offset points",
                     xytext=(3, 6), fontsize=7.4, color=col)
    ax2.plot([AZ_FIRST], [hor_first_rob], "*", ms=14, color="#0a8f2d", mec="#064d18", mew=0.7, zorder=6)
    ax2.set_xlim(0, 360); ax2.set_xticks(range(0, 361, 45))
    ax2.set_ylim(-1.5, max(6.0, float(np.nanmax(h_spec)) + 1.2))
    ax2.set_xlabel("方位角 (°)", fontsize=9); ax2.set_ylabel("天空线仰角 (°)", fontsize=9)
    ax2.grid(True, ls=":", lw=0.5, alpha=0.6)
    ax2.legend(fontsize=7.2, loc="upper right")
    ax2.set_title("(b) 各方位天空线：火箭仰角须高于此线才可见", fontsize=10)

    # ---------------- (c) 出现方位扇区放大 ----------------
    ax3 = fig.add_subplot(gs[1, 1])
    ax3.fill_between(az_sec, 0, h_rob_sec, color="#D2B48C", alpha=0.6, lw=0)
    ax3.plot(az_sec, h_rob_sec, color="#8B4513", lw=2.2, label="稳健天空线（30 km 内）")
    ax3.plot(az_sec, h_spec_sec, color="#777777", lw=1.0, ls="--",
             label="规范判据（含近场像素噪声）")
    ax3.axhline(EL_FIRST, color="#0a8f2d", lw=1.5, ls="--",
                label="首次出现仰角 %.2f°" % EL_FIRST)
    ax3.plot([AZ_FIRST], [EL_FIRST], marker="*", ms=17, color="#0a8f2d", mec="#064d18",
             mew=0.8, zorder=7)
    ax3.annotate("出现 A=%.1f°\nE=%.2f° vs 天空线 %.2f°\n→ 紧贴山脊露出"
                 % (AZ_FIRST, EL_FIRST, hor_first_rob), (AZ_FIRST, EL_FIRST),
                 textcoords="offset points", xytext=(-124, 30), fontsize=8.0, color="#0a5c20",
                 zorder=8,
                 bbox=dict(boxstyle="round,pad=0.28", fc="white", ec="#0a8f2d", lw=0.9, alpha=0.96),
                 arrowprops=dict(arrowstyle="-", color="#0a8f2d", lw=0.9, shrinkA=0, shrinkB=5))
    # 近场噪声说明（数值为 sky 模块口径与稳健天空线之差，非固定算例）
    ax3.annotate("规范判据在观测点周边近场受 SRTM 单像元噪声影响，出现\n"
                 "局部平台/台阶（不是真实地形）；稳健天空线该扇区 %.2f–%.2f°"
                 % (float(h_rob_sec.min()), float(h_rob_sec.max())),
                 (0.03, 0.06), xycoords="axes fraction", va="bottom", fontsize=7.0,
                 color="#5a5a5a", zorder=9,
                 bbox=dict(boxstyle="round,pad=0.25", fc="#f2f2f2", ec="#999999", lw=0.7, alpha=0.95))
    imax = int(np.nanargmax(h_rob_sec))
    ax3.plot([az_sec[imax]], [h_rob_sec[imax]], "o", ms=5, mfc="none", mec="#5a2d00", mew=1.3)
    ax3.annotate("扇区最高 %.2f°（A=%.1f°）" % (h_rob_sec[imax], az_sec[imax]),
                 (az_sec[imax], h_rob_sec[imax]), textcoords="offset points", xytext=(-10, 10),
                 fontsize=7.2, color="#5a2d00", zorder=8, ha="right")
    ax3.set_xlim(AZ_SEC_LO, AZ_SEC_HI)
    ax3.set_ylim(0, max(3.4, float(np.nanmax(h_spec_sec)) + 1.2))
    ax3.set_xlabel("方位角 (°)", fontsize=9); ax3.set_ylabel("天空线仰角 (°)", fontsize=9)
    ax3.grid(True, ls=":", lw=0.5, alpha=0.6)
    ax3.legend(fontsize=7.0, loc="upper left", bbox_to_anchor=(0.0, 0.92))
    ax3.set_title("(c) 出现方位扇区 %.0f–%.0f° 放大（首次出现 A=%.1f° ±40°）"
                  % (AZ_SEC_LO, AZ_SEC_HI, AZ_FIRST), fontsize=10)

    # ---------------- (d) 出现方位地形剖面 ----------------
    ax4 = fig.add_subplot(gs[2, 1])
    ax4.axhline(EL_FIRST, color="#0a8f2d", lw=1.4, ls="--",
                label="首次出现仰角 %.2f°" % EL_FIRST)
    ax4.plot(d_f, el_f, color="#8B4513", lw=1.9, label="地形仰角剖面（去噪，人眼高）")
    ax4.fill_between(d_f, 0, np.maximum(el_f, 0), color="#D2B48C", alpha=0.5, lw=0)
    ax4.plot([ridge["dist_km"]], [ridge["el_deg"]], marker="v", ms=11, mfc="#ffd400",
             mec="#7a3b00", mew=1.2, zorder=7)
    # 标注容易贴右边框：靠近 30 km 时改为右对齐放在图内
    if ridge["dist_km"] > 0.7 * R_KM:
        _tx4, _ha4 = R_KM - 9.0, "right"
    else:
        _tx4, _ha4 = min(ridge["dist_km"] + 2.0, R_KM - 9.0), "left"
    _ty4 = ridge["el_deg"] + 0.30
    ax4.annotate("遮挡山脊：%.1f km / %.0f m / %.2f°"
                 % (ridge["dist_km"], ridge["dem_m"], ridge["el_deg"]),
                 (ridge["dist_km"], ridge["el_deg"]), xytext=(_tx4, _ty4),
                 textcoords="data", ha=_ha4, fontsize=7.8, color="#4a2400", zorder=8,
                 bbox=dict(boxstyle="round,pad=0.24", fc="#fff3cc", ec="#7a3b00", lw=0.8, alpha=0.95),
                 arrowprops=dict(arrowstyle="-", color="#7a3b00", lw=0.8, shrinkA=0, shrinkB=4))
    ax4.set_xlim(0, R_KM)
    ax4.set_ylim(min(-1.0, float(np.nanmin(el_f))), max(2.0, float(np.nanmax(el_f)) + 0.35))
    ax4.set_xlabel("距观测点距离 (km)", fontsize=9); ax4.set_ylabel("地形仰角 (°)", fontsize=9)
    ax4.grid(True, ls=":", lw=0.5, alpha=0.6)
    ax4.legend(fontsize=7.0, loc="upper right")
    ax4.set_title("(d) 出现方位 A=%.1f° 的地形仰角剖面（%d m 步长、%d 点中值去噪）"
                  % (AZ_FIRST, STEP_M, MED_WIN), fontsize=10)

    # ---------------- (e) 弹道方位-高度角（v3.4 新增） ----------------
    ax5 = fig.add_subplot(gs[3, 1])
    if TRACK is not None and len(TRACK["t"]) > 1:
        _t, _az, _el = TRACK["t"], TRACK["az"], TRACK["el"]
        _ok = np.isfinite(_az) & np.isfinite(_el)
        if _ok.sum() > 1:
            _cmap = plt.get_cmap("turbo")
            _norm = plt.Normalize(float(_t[_ok].min()), max(1.0, float(_t[_ok].max())))
            ax5.fill_between(az_full, -0.6, h_rob, color="#D2B48C", alpha=0.6, lw=0)
            ax5.plot(az_full, h_rob, color="#8B4513", lw=1.6,
                     label="稳健天空线")
            _pts = np.column_stack([_az[_ok], _el[_ok]]).reshape(-1, 1, 2)
            _segs = np.concatenate([_pts[:-1], _pts[1:]], axis=1)
            _lc = LineCollection(_segs, cmap=_cmap, norm=_norm, linewidths=2.2, zorder=5)
            _lc.set_array(_t[_ok][:-1])
            ax5.add_collection(_lc)
            ax5.plot([AZ_FIRST], [EL_FIRST], "*", ms=13, color="#0a8f2d",
                     mec="white", mew=0.8, zorder=6, label="首次出现")
            ax5.plot([AZ_MAX], [EL_MAX], "o", ms=8, mfc="none", mec="#d17a00",
                     mew=2.0, zorder=6, label="最大高度角")
            ax5.plot([AZ_LAST], [EL_LAST], "s", ms=7, color="#d6006e",
                     mec="white", mew=0.7, zorder=6, label="消失")
            ax5.set_xlim(float(_az[_ok].min()) - 5.0, float(_az[_ok].max()) + 5.0)
            ax5.set_ylim(0, max(4.0, float(_el[_ok].max()) + 3.0))
            ax5.set_xlabel("方位角 (°)", fontsize=9)
            ax5.set_ylabel("高度角 (°)", fontsize=9)
            ax5.grid(True, ls=":", lw=0.5, alpha=0.6)
            ax5.legend(fontsize=7.0, loc="upper right")
            ax5.set_title("(e) 弹道方位–高度角（颜色=时间；火箭仰角须高于天空线才可见）",
                          fontsize=10)
        else:
            ax5.axis("off")
            ax5.text(0.5, 0.5, "天空轨迹 CSV 数据不足", ha="center", va="center",
                     transform=ax5.transAxes)
    else:
        ax5.axis("off")
        ax5.text(0.5, 0.5, "缺少天空轨迹 CSV，无法绘制弹道 A–E 小图",
                 ha="center", va="center", transform=ax5.transAxes, fontsize=8)

    _t_first_bj = T_FIRST_BJ or "—"
    _t_first_s = ("T+%.1f s" % T_FIRST_S) if math.isfinite(T_FIRST_S) else "T+— s"
    fig.suptitle("火箭出现方位地形判读图：观测点 %.4f°N, %.4f°E（高程 %.0f m + 人眼 %.1f m）｜"
                 "首次出现 A=%.1f° E=%.2f°（%s，北京时 %s）"
                 % (lat0, lon0, alt0, EYE_M, AZ_FIRST, EL_FIRST, _t_first_s, _t_first_bj),
                 fontsize=13.6, y=0.982)
    fig.text(0.5, 0.008,
             "稳健天空线 = 观测点海拔 + 人眼 %.1f m，忽略最近 %.0f m，地形高程 %d 点（%d m）中值去噪；"
             "规范判据 = sky 模块口径（SRTM1″/90 m/200 km，含近场单像元噪声）。"
             "灰色虚线的平台/台阶是近场 DEM 像元与方位采样量化，不是真实地形或弹道跳变。"
             "SRTM 不含建筑/树木，城市方向实际遮挡更高；未计大气折射。仅供公开信息分析与教学使用。"
             % (EYE_M, MIN_KM * 1000, MED_WIN, MED_WIN * STEP_M),
             ha="center", fontsize=7.1, color="#444444")
    fig.subplots_adjust(left=0.052, right=0.985, top=0.945, bottom=0.052)

    out = OUT_PNG or os.path.join(ROOT, "sky", "出现方位地形示意图_%s.png" % OBS_ID)
    fig.savefig(out, dpi=165, facecolor="white")
    plt.close(fig)
    from PIL import Image
    im = Image.open(out)
    print("saved:", out, im.size, "px")

    data = dict(
        purpose="按地形快速判断火箭出现方位（局部放大示意图）",
        method=dict(
            robust_skyline=("观测点 SRTM 海拔 + 人眼高 %.1f m；忽略最近 %.0f m；"
                            "地形高程 %d 点（%d m）中值去噪；取各方位最大仰角" % (EYE_M, MIN_KM * 1000, MED_WIN, MED_WIN * STEP_M)),
            spec_criterion="sky 模块可见性口径：SRTM 1″、90 m 步长、200 km、近场 10 km 用 1″ 精采样",
            noise_note=("规范判据在观测点周边近场受 SRTM 单像元噪声影响，出现局部台阶/平台（非真实地形）；"
                        "稳健天空线该扇区为 %.2f–%.2f°。"
                        "两者在首次出现方位分别为 %.3f° 与 %.3f°，差异对首次出现时刻影响 <0.5 s。"
                        % (float(h_rob_sec.min()), float(h_rob_sec.max()),
                           hor_first_rob, hor_first_spec)),
        ),
        observer=dict(id=OBS_ID, lat=lat0, lon=lon0, alt_m=alt0, eye_m=EYE_M, dem=zsrc,
                      name=o.get("name", ""), note=o.get("note", "")),
        map_radius_km=R_KM,
        first_appearance=dict(az_deg=AZ_FIRST, el_deg=EL_FIRST,
                              t_met_s=(float(T_FIRST_S) if math.isfinite(T_FIRST_S) else None),
                              t_bjt=T_FIRST_BJ or None),
        occluding_ridge=ridge,
        skyline=dict(
            at_first_az_robust_deg=round(hor_first_rob, 3),
            at_first_az_spec_deg=round(hor_first_spec, 3),
            sector_max_robust_deg=round(float(h_rob_sec[imax]), 3),
            sector_max_az=float(az_sec[imax]),
            sector_bounds_deg=[float(AZ_SEC_LO), float(AZ_SEC_HI)],
            max_full_robust_deg=round(float(np.nanmax(h_rob)), 3),
            max_full_az=float(az_full[int(np.nanargmax(h_rob))]),
            sector_min_robust_deg=round(float(h_rob_sec.min()), 3),
            note="天空线仰角=该方位地形/地平最高仰角（含地球曲率）；火箭仰角须大于此值才可见",
        ),
        azimuth_rays=[dict(name="首次出现", az_deg=AZ_FIRST, el_deg=EL_FIRST),
                      dict(name="最大高度角方位", az_deg=AZ_MAX, el_deg=EL_MAX),
                      dict(name="消失", az_deg=AZ_LAST, el_deg=EL_LAST)],
        trajectory=(dict(
            csv=os.path.relpath(TRACK["path"], ROOT).replace("\\", "/"),
            n=int(len(TRACK["t"])),
            t_range_s=[round(float(TRACK["t"].min()), 2), round(float(TRACK["t"].max()), 2)],
            az_range_deg=[round(float(TRACK["az"].min()), 2),
                          round(float(TRACK["az"].max()), 2)],
            note="主图外圈彩弧与右下小图按时间着色；仅表示观测方向/高度角，不表示地面距离",
        ) if TRACK else None),
        cautions=["SRTM 不含建筑/树木：城市方向实际遮挡角可能更高",
                  "近场 200 m 内不参与稳健天空线（SRTM 单像元 ±2–3 m 噪声）",
                  "灰色规范判据的台阶/平台来自近场 DEM 单像元与方位采样量化，不是真实地形，也不是弹道跳变；判读以棕色稳健天空线为准",
                  "未计大气折射（200 km 处约抬高 0.1–0.2°）",
                  "出现时刻不确定度 ±3 s（对应方位变化 <0.5°）"],
    )
    json.dump(data, open(OUT_JSON or os.path.join(ROOT, "sky", "出现方位地形_数据.json"),
                         "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(json.dumps(dict(ridge=ridge, skyline=data["skyline"]), ensure_ascii=False, indent=1))
    assert im.size[0] >= 1600


if __name__ == "__main__":
    main()
