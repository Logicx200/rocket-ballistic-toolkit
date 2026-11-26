# -*- coding: utf-8 -*-
"""生成 docs/收敛对比.png：v1 legacy 与 v2 accurate 的 dt 收敛性对比（数据取自 bench.py）。"""
import os
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import rcParams

ROOT = Path(__file__).resolve().parents[2]
sys_path = str(ROOT)
import sys
sys.path.insert(0, sys_path)
from common import plotstyle  # noqa: E402

plotstyle.setup()

legacy_dt = [0.2, 0.1, 0.05, 0.02, 0.01, 0.005, 0.0025, 0.001]
legacy_a = [6948.154, 6916.013, 6874.987, 6868.962, 6858.664, 6853.541, 6852.736, 6850.866]
acc_dt = [0.5, 0.2, 0.1, 0.05, 0.02]
acc_a = [6850.411429, 6850.412058, 6850.412002, 6850.412001, 6850.412013]
converged = 6850.411

fig, ax = plt.subplots(figsize=(9.2, 5.4), dpi=160)
ax.plot(legacy_dt, legacy_a, "o-", color="#c0392b", lw=2.2, ms=6,
        label="v1.0 legacy（事件量化到 dt）")
ax.plot(acc_dt, acc_a, "s-", color="#1f6fb4", lw=2.2, ms=6,
        label="v2.0 accurate（事件对齐 + 状态耦合 RK4）")
ax.axhline(converged, color="#555555", ls="--", lw=1.2)
ax.annotate("收敛解 a = 6850.411 km", (0.6, converged + 3), color="#555555", fontsize=10)
for dt, a in zip(legacy_dt, legacy_a):
    if dt in (0.02, 0.001):
        ax.annotate("dt=%.3f\n%.2f km" % (dt, a), (dt, a), textcoords="offset points",
                    xytext=(8, 10), fontsize=8.6, color="#c0392b")
ax.annotate("dt=0.5…0.02 极差 0.63 m", (0.28, 6846.5), fontsize=9.5, color="#1f6fb4")
ax.set_xscale("log")
ax.set_xlabel("积分步长 dt [s]（对数坐标）")
ax.set_ylabel("入轨半长轴 a [km]")
ax.set_title("入轨半长轴对积分步长的敏感性：v1.0 一阶偏差 vs v2.0 事件对齐收敛")
ax.grid(True, which="both", ls=":", lw=0.6, alpha=0.7)
ax.legend(loc="upper right", fontsize=9.5)
ax.set_ylim(6840, 6960)
fig.tight_layout()
out = Path(__file__).resolve().parent / "收敛对比.png"
fig.savefig(out, facecolor="white")
print("saved", out)
