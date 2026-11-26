# -*- coding: utf-8 -*-
"""指定坐标天空轨迹图 —— 按用户指定参考样式重制（参考 `天空轨迹_运城市.png`，CZ-6A 版）。

参考样式要点：
  * 单幅图：x=方位角（°）、y=高度角（°），细网格、方框、无多余子图
  * 轨迹按时间着色（viridis：黄=早，深紫=晚），右侧竖色条「时间 t (s)」
  * 棕色地形遮挡线 + 米色「地形+地球曲率遮挡区」填充（填充到坐标区底部）
  * 事件点：黑色小圆点 + 就地文字标注（一二级分离 / 整流罩分离 / SECO）
  * 绿色星 = 首次出现；橙色圆 = 最大高度角；品红方块 = 消失
  * 右上图例；底部居中方框给出首次出现/最大高度角数值
  * 中文两行标题（观测点坐标/高程 + 颜色与遮挡说明）

输出：覆盖 sky_summary.json 中该观测点的主图 PNG（参考样式；多观测点文件名含地点名+经纬度）
用法：python sky/plot_sky_refstyle.py [--obs shanxi_obspoint] [--out ...]
"""
import os
import sys, os, json, csv, argparse
from datetime import datetime, timezone
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
from matplotlib.collections import LineCollection
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
import matplotlib.dates as mdates
from common import plotstyle, geo, dem

OBS_ID = None                # v2：由 sky_summary.json / --obs 决定（v1 硬编码 shanxi_obspoint）
COL_TERRAIN = "#8B4513"
COL_BAND = "#D2B48C"
COL_EVENT = "#222222"
COL_FIRST = "#2ca02c"
COL_MAX = "#ff7f0e"
COL_LAST = "#e6007e"


def _main_png_path(obs: dict, obs_id: str) -> str:
    """参考样式主图输出路径 = sky_summary 中记录的主图（多观测点含地点名+经纬度）。"""
    p = (((obs.get("outputs") or {}).get("png")) or {}).get("path")
    if p:
        return os.path.join(ROOT, str(p))
    return os.path.join(ROOT, "sky", "天空轨迹_%s.png" % obs_id)


def load(obs_id=None):
    sky = os.path.join(ROOT, "sky")
    summ = json.load(open(os.path.join(sky, "sky_summary.json"), encoding="utf-8"))
    if obs_id is None or obs_id not in summ.get("observers", {}):
        # v2：默认取 sky_summary.json 里实际存在的观测点（v1 硬编码 shanxi_obspoint）
        ids = list(summ.get("observers", {}).keys())
        if not ids:
            raise SystemExit("sky/sky_summary.json 没有 observers，请先运行 plot_sky.py")
        obs_id = ids[0]
    obs = summ["observers"][obs_id]
    # v3.2.3：多观测点 CSV 文件名含地点名+经纬度，优先用 sky_summary 记录的真实路径
    csv_rel = (((obs.get("outputs") or {}).get("csv")) or {}).get("path")
    csv_path = (os.path.join(ROOT, str(csv_rel)) if csv_rel
                else os.path.join(sky, "天空轨迹_%s.csv" % obs_id))
    if not os.path.exists(csv_path):
        csv_path = os.path.join(sky, "天空轨迹_%s.csv" % obs_id)
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8-sig")))
    return rows, obs, obs_id


def arr(rows, key):
    return np.array([float(r[key]) for r in rows])


def terrain_dense(lat, lon, az_grid, obs_id=None):
    try:
        if obs_id:
            try:
                info = dem.load_observer_cache(obs_id)
            except Exception:  # noqa: BLE001 - 无 3″ 缓存时直接用 1″ 瓦片
                info = None
        else:
            info = None
        h = dem.terrain_horizon_deg(lat, lon, None, az_list_deg=az_grid, info=info)
        return np.asarray(h, dtype=float)
    except Exception as exc:  # noqa: BLE001
        print("[warn] terrain horizon failed:", exc)
        return np.full_like(np.asarray(az_grid, dtype=float), np.nan)


def colored_path(ax, x, y, c, cmap, norm, lw=2.6, zorder=5):
    pts = np.array([x, y]).T.reshape(-1, 1, 2)
    segs = np.concatenate([pts[:-1], pts[1:]], axis=1)
    lc = LineCollection(segs, cmap=cmap, norm=norm, linewidths=lw, zorder=zorder)
    lc.set_array(c[:-1])
    ax.add_collection(lc)
    return lc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--obs", default=None, help="观测点 id（缺省取 sky_summary 中第一个）")
    ap.add_argument("--config", default=None, help="任务配置（本脚本不直接使用，兼容流水线参数）")
    ap.add_argument("--out", default=None)
    ap.add_argument("--dpi", type=int, default=150)
    args = ap.parse_args()

    plotstyle.setup()
    rows, o, obs_id = load(args.obs)
    t = arr(rows, "t_met_s")
    az = arr(rows, "az_deg")
    el = arr(rows, "el_deg")
    slab = arr(rows, "slant_range_km")
    vis = arr(rows, "geometric_visible").astype(int)
    sunv = arr(rows, "sun_visible").astype(int)

    first = o["first_appearance"]; mx = o["max_elevation"]; last = o["last_visible"]
    lat_o, lon_o = o["lat_deg"], o["lon_deg"]
    alt_o = o["alt_m"]

    # ---- 方位角范围（必要时 0/360 环绕）----
    lo = min(np.percentile(az, 0.2), first["az_deg"], mx["az_deg"], last["az_deg"], 360.0 if az.max() > 350 else az.min())
    hi = max(np.percentile(az, 99.8), first["az_deg"], mx["az_deg"], last["az_deg"], az.max())
    pad = 6.0
    x0, x1 = lo - pad, hi + pad
    if x1 - x0 > 200:            # 不跨 0° 就不加 360
        x0, x1 = 185.0, 335.0
    az_plot = az.copy()
    # 统一到 [x0, x1] 连续区间
    for i in range(len(az_plot)):
        while az_plot[i] < x0 and az_plot[i] + 360 <= x1:
            az_plot[i] += 360
        while az_plot[i] > x1 and az_plot[i] - 360 >= x0:
            az_plot[i] -= 360

    # ---- 地形遮挡（密采样）----
    az_grid = np.arange(np.floor(x0), np.ceil(x1) + 0.001, 0.25)
    hor = terrain_dense(lat_o, lon_o, az_grid, obs_id)

    y_candidates = [el.min(), hor.min() if np.isfinite(hor).any() else 0.0,
                    o["debris"][0]["impact"]["el_deg"], o["debris"][1]["impact"]["el_deg"],
                    o["events"][-1]["el_deg"]]
    y0 = min(-1.6, min(y_candidates) - 0.8)
    y1 = max(el.max() + 8.0, 52.0)

    fig, ax = plt.subplots(figsize=(12.3, 8.3))
    # 遮挡区填充（到坐标区底部）
    if np.isfinite(hor).any():
        ax.fill_between(az_grid, y0, hor, color=COL_BAND, alpha=0.55, lw=0,
                        label="地形叠加地球曲率遮挡区", zorder=1)
        ax.plot(az_grid, hor, color=COL_TERRAIN, lw=2.6, label="地形遮挡线（各方位角地面最高仰角）",
                zorder=3)
    ax.axhline(0.0, color="#999999", lw=0.9, ls=(0, (4, 3)), zorder=2)

    # ---- 轨迹（按时间着色）----
    cmap = plt.get_cmap("viridis")
    c = t
    lc = colored_path(ax, az_plot, el, c, cmap, plt.Normalize(vmin=0, vmax=max(800.0, t.max())),
                      lw=2.8, zorder=5)
    cb = fig.colorbar(lc, ax=ax, pad=0.012, fraction=0.035)
    cb.set_label("时间 t (s)", fontsize=11)
    cb.ax.tick_params(labelsize=9)

    # ---- 事件点（黑色小圆点）----
    EV_LABEL = {"一级关机/一二级分离": "一二级分离"}
    for e in o["events"]:
        lab = EV_LABEL.get(e["event"])
        x, y = e["az_deg"], e["el_deg"]
        while x < x0:
            x += 360
        while x > x1:
            x -= 360
        ax.plot([x], [y], "o", ms=4.8, color=COL_EVENT, zorder=7)
        if lab:
            ax.annotate("%s\nT+%.0f s" % (lab, e["t_met_s"]), (x, y), textcoords="offset points",
                        xytext=(10, 12), fontsize=8.6, color="#111111", zorder=8,
                        bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="none", alpha=0.75))

    # ---- 首次出现 / 最大高度角 / 消失 ----
    xf = first["az_deg"]
    ax.plot([xf], [first["el_deg"]], marker="*", ms=17, color=COL_FIRST, mec="#0b5d0b", mew=0.8,
            zorder=9, label="首次出现 t=%.1f s" % first["t_met_s"])
    ax.annotate("首次出现 T+%.1f s\nA=%.2f° E=%.2f°" % (first["t_met_s"], first["az_deg"], first["el_deg"]),
                (xf, first["el_deg"]), textcoords="offset points", xytext=(-10, 16), fontsize=8.4,
                color="#0b5d0b", ha="right", zorder=9,
                arrowprops=dict(arrowstyle="-", color="#0b5d0b", lw=0.7, shrinkA=0, shrinkB=6))
    xm = mx["az_deg"]
    ax.plot([xm], [mx["el_deg"]], "o", ms=13, mfc="none", mec=COL_MAX, mew=2.2, zorder=9,
            label="最大高度角 %.2f° @ t=%.1f s" % (mx["el_deg"], mx["t_met_s"]))
    _fair_t = [e["t_met_s"] for e in o["events"] if e["event"] == "整流罩分离"][0]
    ax.annotate("最大高度角 %.2f°\nT+%.1f s（%s 北京时）\n整流罩分离 T+%.0f s（同点附近）"
                % (mx["el_deg"], mx["t_met_s"], o["max_elevation"]["t_bj"][11:], _fair_t),
                (xm, mx["el_deg"]), textcoords="offset points", xytext=(-34, 26), fontsize=8.4,
                color="#a05500", ha="right", zorder=9,
                arrowprops=dict(arrowstyle="-", color=COL_MAX, lw=0.7, shrinkA=0, shrinkB=6))
    xl = last["az_deg"]
    ax.plot([xl], [last["el_deg"]], marker="s", ms=8, color=COL_LAST, mec="white", mew=0.8, zorder=9,
            label="消失 T+%.1f s" % last["t_met_s"])
    ax.annotate("消失 T+%.1f s\nA=%.2f° E=%.2f°" % (last["t_met_s"], last["az_deg"], last["el_deg"]),
                (xl, last["el_deg"]), textcoords="offset points", xytext=(16, 16), fontsize=8.6,
                color=COL_LAST, ha="left", zorder=9,
                arrowprops=dict(arrowstyle="-", color=COL_LAST, lw=0.7, shrinkA=0, shrinkB=5))

    # ---- 残骸落点方向（地平线下，仅画 ×；文字说明放入底部信息框）----
    for d in o["debris"]:
        ax.plot([d["impact"]["az_deg"]], [d["impact"]["el_deg"]], marker="x", ms=7.5,
                color="#555555", mew=1.3, ls="none", zorder=7)

    # ---- 坐标轴/标题 ----
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_xlabel("方位角 (°；正北 = 0°，顺时针)", fontsize=11)
    ax.set_ylabel("高度角 (°)", fontsize=11)
    ax.grid(True, ls=":", lw=0.6, color="#bbbbbb", alpha=0.8)
    for sp in ax.spines.values():
        sp.set_linewidth(0.9)
        sp.set_color("#443333")

    sun0 = o["solar"]["observer_sun_el_t0_deg"]
    # v3.4：图题/T0 改为从 sky_summary.json 读取，修掉历史版本写死 CZ-2D 遥106 的 bug
    try:
        _summ = json.load(open(os.path.join(ROOT, "sky", "sky_summary.json"),
                               encoding="utf-8"))
    except Exception:  # noqa: BLE001
        _summ = {}
    mission_title = str(_summ.get("mission") or "本任务").replace("发射", "").strip()
    t0_utc = str(_summ.get("t0_utc") or "—")
    t0_bj = str(_summ.get("t0_bj") or "—")
    obs_label = o["name"]
    ax.set_title(
        "天空轨迹（方位角-高度角）：%s（%.4f°N, %.4f°E，高程≈%.0f m） 看 %s 上升段\n"
        "颜色=时间（黄→深紫为早→晚）；棕线以下=地形+地球曲率遮挡区\n"
        "（棕线平台/台阶=DEM 方位/距离采样量化，不是弹道跳变）T0=%s UTC（北京时 %s），"
        "观测点太阳高度 %.2f°"
        % (obs_label, lat_o, lon_o, alt_o, mission_title, t0_utc, t0_bj, sun0),
        fontsize=13.5, pad=12)

    # ---- 图例 ----
    handles = [
        Line2D([], [], color=COL_TERRAIN, lw=2.6, label="地形遮挡线（各方位角地面最高仰角）"),
        Patch(facecolor=COL_BAND, alpha=0.55, label="地形+地球曲率遮挡区"),
        Line2D([], [], marker="*", ms=13, color=COL_FIRST, mec="#0b5d0b", ls="none",
               label="首次出现 t=%.1f s" % first["t_met_s"]),
        Line2D([], [], marker="o", ms=10, mfc="none", mec=COL_MAX, mew=2.0, ls="none",
               label="最大高度角 %.2f° @t=%.1f s" % (mx["el_deg"], mx["t_met_s"])),
        Line2D([], [], marker="s", ms=8, color=COL_LAST, mec="white", ls="none",
               label="消失（落到地形遮挡线下）t=%.1f s" % last["t_met_s"]),
        Line2D([], [], marker="o", ms=4.6, color=COL_EVENT, ls="none", label="关键事件（T+ 标注）"),
        Line2D([], [], marker="x", ms=6, color="#555555", ls="none", label="残骸落点方向（地平线下）"),
    ]
    ax.legend(handles=handles, loc="upper left", fontsize=8.4, framealpha=0.92, borderpad=0.6)

    # ---- 底部信息框 ----
    _core = next((d for d in o["debris"] if d["object"] == "core_stage"), None)
    _fair = next((d for d in o["debris"] if d["object"] == "fairing_L"), None)
    _unc = float(first.get("uncertainty_s") or 3.0)
    txt = ("可见：几何 %.1f s / 阳光 %.1f s；火箭受照 %.0f%%；出现不确定度 ±%.1f s；"
           "最大高度角 %.2f°（T+%.1f s，方位 %.1f°，斜距 %.0f km）"
           % (o["visible_duration_s"], o["sun_visible_duration_s"],
              100 * o["solar"]["rocket_sunlit_fraction"], _unc,
              mx["el_deg"], mx["t_met_s"], mx["az_deg"], mx["slant_range_km"]))
    if _core is not None and _fair is not None:
        txt += ("\n× 残骸落点方向（均在地平线下）：一子级 T+%.0f s A=%.1f° E=%.1f°；"
                "整流罩 T+%.0f s A=%.1f° E=%.1f°"
                % (_core["impact"]["t_met_s"], _core["impact"]["az_deg"],
                   _core["impact"]["el_deg"],
                   _fair["impact"]["t_met_s"], _fair["impact"]["az_deg"],
                   _fair["impact"]["el_deg"]))
    ax.text(0.5, 0.035, txt, transform=ax.transAxes, ha="center", va="bottom", fontsize=8.2,
            bbox=dict(boxstyle="round,pad=0.45", fc="white", ec="#555555", alpha=0.95), zorder=10)

    fig.tight_layout()
    out = args.out or _main_png_path(o, obs_id)
    fig.savefig(out, dpi=args.dpi, facecolor="white", bbox_inches="tight")
    plt.close(fig)
    im = __import__("PIL.Image", fromlist=["Image"]).open(out)
    print("saved:", out, im.size, "px")
    assert im.size[0] >= 1600, "PNG 宽度不足 1600 px"
    print("OK")


if __name__ == "__main__":
    main()
