# -*- coding: utf-8 -*-
"""交会全流程星下点图（发射→入轨→相位→转移→对接）。

输入：``sim/trajectory.csv``（上升段）、``sim/交会轨迹.csv``（入轨→对接）、
可选 ``sim/CZ2F_任务前后30min_空间站.csv``（空间站上下文）。
输出：``viz/交会全流程星下点.png``
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("RK_ROOT") or os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# 中文字体（Windows 自带雅黑/黑体；缺失时回退 DejaVu，仅标签为方框）
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC",
                                   "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


def _read(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _segments(lons, lats):
    """按经度 ±180 跨越切分，避免地面轨迹在图上拉出横贯全图的假线。"""
    segs = []
    cur = [(lons[0], lats[0])] if lons else []
    for i in range(1, len(lons)):
        if abs(lons[i] - lons[i - 1]) > 180.0:
            segs.append(cur)
            cur = [(lons[i], lats[i])]
        else:
            cur.append((lons[i], lats[i]))
    if cur:
        segs.append(cur)
    return segs


def _plot_track(ax, lons, lats, color, lw, label, ls="-"):
    for seg in _segments(lons, lats):
        if len(seg) < 2:
            continue
        ax.plot([p[0] for p in seg], [p[1] for p in seg], color=color, lw=lw,
                ls=ls, label=label)
        label = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    args = ap.parse_args()
    cfg = json.load(open(args.config, encoding="utf-8-sig")) if os.path.exists(
        args.config) else {}
    sim = os.path.join(ROOT, "sim")
    viz = os.path.join(ROOT, "viz")
    os.makedirs(viz, exist_ok=True)

    asc = _read(os.path.join(sim, "trajectory.csv"))
    rdv = _read(os.path.join(sim, "交会轨迹.csv"))
    ctx = _read(os.path.join(sim, "CZ2F_任务前后30min_空间站.csv"))
    dock = {}
    try:
        dock = json.load(open(os.path.join(sim, "交会对接.json"),
                              encoding="utf-8-sig")).get("docking") or {}
    except Exception:  # noqa: BLE001
        dock = {}
    if not asc and not rdv:
        print("[plot_rdv] 缺少轨迹数据，跳过")
        return 1

    fig, ax = plt.subplots(figsize=(13, 7.2), dpi=130)
    if ctx:
        _plot_track(ax, [float(r["lon_deg"]) for r in ctx],
                    [float(r["lat_deg"]) for r in ctx],
                    "#2ca02c", 1.4, "空间站（T0−30min ~ 对接+30min）", ls="--")
    if asc:
        _plot_track(ax, [float(r["lon_deg"]) for r in asc],
                    [float(r["lat_deg"]) for r in asc],
                    "#1f77b4", 1.8, "上升段（发射→入轨）")
        la, lo = float(asc[0]["lon_deg"]), float(asc[0]["lat_deg"])
        ax.plot(lo, la, marker="^", ms=11, color="#2ca02c", zorder=5)
        ax.annotate("发射", (lo, la), textcoords="offset points", xytext=(6, 6),
                    fontsize=9, color="#2ca02c")
    if rdv:
        # 分阶段配色
        colors = {"相位段": "#ff7f0e", "转移段": "#d62728", "最终逼近": "#9467bd"}
        for ph, col in colors.items():
            pts = [r for r in rdv if r.get("phase") == ph]
            if len(pts) < 2:
                continue
            _plot_track(ax, [float(r["lon_deg"]) for r in pts],
                        [float(r["lat_deg"]) for r in pts], col, 1.7,
                        "%s（入轨→对接）" % ph if ph != "相位段"
                        else "相位段（绕圈）")
        if asc:
            ax.plot(float(asc[-1]["lon_deg"]), float(asc[-1]["lat_deg"]),
                    marker="o", ms=7, color="#1f77b4", zorder=5)
            ax.annotate("入轨点", (float(asc[-1]["lon_deg"]),
                                   float(asc[-1]["lat_deg"])),
                        textcoords="offset points", xytext=(6, -12),
                        fontsize=9, color="#1f77b4")
    if dock.get("station_lat_deg") is not None:
        ax.plot(float(dock["station_lon_deg"]), float(dock["station_lat_deg"]),
                marker="*", ms=16, color="#ff2d2d", zorder=6)
        ax.annotate("对接点\n%.2f°N, %.2f°E\n%.1f km\n%s UTC"
                    % (float(dock["station_lat_deg"]),
                       float(dock["station_lon_deg"]),
                       float(dock.get("alt_km") or 0.0),
                       dock.get("t_utc", "")),
                    (float(dock["station_lon_deg"]), float(dock["station_lat_deg"])),
                    textcoords="offset points", xytext=(10, 8), fontsize=8.5,
                    color="#c00000")

    ax.set_xlim(-180, 180)
    ax.set_ylim(-90, 90)
    ax.set_xticks(range(-180, 181, 30))
    ax.set_yticks(range(-90, 91, 30))
    ax.grid(color="#cccccc", lw=0.6, ls=":")
    ax.axhline(0, color="#888888", lw=0.8)
    ax.set_xlabel("经度 (°E)")
    ax.set_ylabel("纬度 (°N)")
    _t0 = ((cfg.get("mission") or {}).get("t0_utc") or "—")
    ax.set_title("交会全流程星下点：发射 %s → 对接 %s"
                 % (_t0, dock.get("t_utc") or "—"), fontsize=11)
    ax.legend(loc="lower left", fontsize=8.5, framealpha=0.9)
    fig.tight_layout()
    out = os.path.join(viz, "交会全流程星下点.png")
    fig.savefig(out)
    plt.close(fig)
    print("[plot_rdv] → %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
