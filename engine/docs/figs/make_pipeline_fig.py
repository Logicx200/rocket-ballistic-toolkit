# -*- coding: utf-8 -*-
"""生成 docs/流水线耗时对比.png：v1.0 vs v2.0 各阶段耗时（数据取自 run_all 实测）。"""
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from common import plotstyle  # noqa: E402

plotstyle.setup()

stages = ["仿真+导出", "发射窗口", "天空轨迹", "参考样式图", "地形判读图",
          "全国地图", "蒙特卡洛 250", "KMZ", "QC"]
v1 = [1.84, 0.28, 4.39, 2.20, 21.79, 16.87, 187.5, 0.28, 0.27]
v2 = [0.56, 0.21, 4.32, 1.96, 4.00, 9.48, 10.78, 0.30, 0.30]

x = np.arange(len(stages))
w = 0.38
fig, ax = plt.subplots(figsize=(11.6, 5.2), dpi=160)
b1 = ax.bar(x - w / 2, v1, w, color="#c0392b", alpha=0.9, label="v1.0")
b2 = ax.bar(x + w / 2, v2, w, color="#1f6fb4", alpha=0.9, label="v2.0")
ax.set_yscale("log")
ax.set_ylabel("耗时 [s]（对数坐标）")
ax.set_xticks(x)
ax.set_xticklabels(stages, fontsize=9.5)
ax.set_title("全流水线各阶段耗时：v1.0 vs v2.0（同机、同算例、8 进程）")
ax.legend(fontsize=10)
ax.grid(True, axis="y", which="both", ls=":", lw=0.6, alpha=0.7)
for bars, vals, color in ((b1, v1, "#7b241c"), (b2, v2, "#154360")):
    for b, v in zip(bars, vals):
        lab = ("%.0f" % v) if v >= 10 else ("%.2f" % v if v >= 1 else "%.2g" % v)
        ax.annotate(lab, (b.get_x() + b.get_width() / 2, v), textcoords="offset points",
                    xytext=(0, 3), ha="center", fontsize=7.6, color=color)
ax.annotate("合计 47.9 s → 21.1 s（不含 MC）\n合计 235.4 s → 31.9 s（含 MC 250）",
            (0.015, 0.97), xycoords="axes fraction", va="top", fontsize=9.5,
            bbox=dict(boxstyle="round,pad=0.4", fc="white", ec="#888888", alpha=0.9))
ax.set_ylim(0.1, 400)
fig.tight_layout()
out = Path(__file__).resolve().parent / "流水线耗时对比.png"
fig.savefig(out, facecolor="white")
print("saved", out)
