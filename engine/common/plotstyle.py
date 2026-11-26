# -*- coding: utf-8 -*-
"""common.plotstyle — 统一绘图风格：中文字体、负号、字号、配色、保存。

用法（所有画图模块的第一步）::

    from common import plotstyle
    plotstyle.setup()                 # 设置 rcParams（中文字体 + 统一风格）
    fig, ax = plt.subplots(...)
    ...
    plotstyle.save(fig, "sky/天空轨迹_sanya.png", dpi=160)   # 白底 + 紧凑布局

字体策略
--------
1. 优先 Microsoft YaHei（``C:/Windows/Fonts/msyh.ttc``）；
2. 回退 SimHei（``simhei.ttf``）；
3. 都没有则用 matplotlib 内置 DejaVu Sans（中文会显示为方块，但不会崩）。

``save`` 默认 dpi=160、白底、``bbox_inches="tight"``；同时把 dpi 写进 PNG 元信息，
便于报告核对分辨率要求（sky 图要求 ≥1600 px 宽，用 ``figsize`` 保证）。
"""
from __future__ import annotations

import os
from pathlib import Path

import matplotlib

__all__ = ["setup", "save", "COLORS", "PALETTE", "new_figure", "available_cjk_font",
           "FONT_CANDIDATES", "register_cjk_font"]

FONT_CANDIDATES = [
    r"C:/Windows/Fonts/msyh.ttc",      # Microsoft YaHei（中易雅黑）
    r"C:/Windows/Fonts/msyhbd.ttc",
    r"C:/Windows/Fonts/simhei.ttf",    # 黑体
    r"C:/Windows/Fonts/simsun.ttc",    # 宋体
]

#: 统一配色（供 sim/sky/map/viz/report 共用，色盲友好、打印可分辨）
COLORS = {
    "trajectory": "#1f77b4",       # 主弹道
    "booster": "#d62728",          # 助推器残骸
    "core_stage": "#ff7f0e",       # 芯一级
    "fairing": "#2ca02c",          # 整流罩
    "stage2": "#9467bd",           # 二级
    "payload": "#8c564b",
    "impact": "#111111",           # 落点
    "notam": "#e377c2",            # 危险区
    "observer": "#17becf",         # 观测点
    "sun": "#ffd92f",
    "terminator": "#4d4d4d",       # 晨昏线
    "border": "#5a5a5a",           # 国界
    "province": "#a0a0a0",         # 省界
    "coast": "#2f6f9f",            # 海岸线
    "city": "#333333",
    "land": "#f2efe9",
    "sea": "#dbe9f4",
    "highlight": "#ff3b30",
    "grid": "#b0b0b0",
}

#: 观测点/残骸对象编号色序列（可循环取用）
PALETTE = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
           "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]


def available_cjk_font():
    """检查系统可用中文字体，返回 (font_path or None, family_name or None)。

    只做**检查**不改 rcParams；返回的 family_name 已由 matplotlib 识别。
    """
    from matplotlib import font_manager

    for path in FONT_CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            if path.lower().endswith((".ttc", ".ttf", ".otf")):
                font_manager.fontManager.addfont(path)   # 幂等，重复调用无副作用
            name = font_manager.FontProperties(fname=path).get_name()
            if name:
                return path, name
        except Exception:  # noqa: BLE001 - 某一字体不可用就跳过
            continue
    # 退路：问 matplotlib 是否已认识常见中文字体
    for fam in ("Microsoft YaHei", "SimHei", "SimSun", "Noto Sans CJK SC"):
        try:
            p = font_manager.findfont(font_manager.FontProperties(family=fam),
                                      fallback_to_default=False)
            return p, fam
        except Exception:  # noqa: BLE001
            continue
    return None, None


def register_cjk_font(font_path=None):
    """把中文字体注册进 matplotlib 并返回字体族名（失败返回 None）。"""
    from matplotlib import font_manager
    if font_path is None:
        font_path, fam = available_cjk_font()
        return fam
    try:
        font_manager.fontManager.addfont(font_path)
        return font_manager.FontProperties(fname=font_path).get_name()
    except Exception:  # noqa: BLE001
        return None


def setup(font_size: float = 10.0, font_path=None, grid: bool = True,
          verbose: bool = False):
    """设置全局 matplotlib rcParams（中文字体 / 负号 / 字号 / 网格 / 配色）。

    Parameters
    ----------
    font_size : float
        基础字号（其余字号按比例派生：标题 1.3x、标签 1.1x、刻度 0.9x、图例 0.9x）。
    font_path : str, optional
        指定中文字体文件；默认自动挑选（雅黑 → 黑体 → 宋体）。
    grid : bool
        是否默认开启网格（浅灰点线）。
    verbose : bool
        是否打印选中的字体，便于排查"中文变方块"。

    Returns
    -------
    dict
        实际生效的关键 rcParams 字典副本（可继续用 ``plt.rcParams.update(...)`` 覆盖）。

    Notes
    -----
    * ``axes.unicode_minus = False`` 解决中文负号显示为方框的经典问题；
    * 同时设置 ``font.family = 'sans-serif'`` 与 ``font.sans-serif`` 列表；
    * 对 Agg 后端无副作用，可在无显示器环境使用。
    """
    path, family = (font_path, register_cjk_font(font_path)) if font_path else available_cjk_font()
    sans = []
    if family:
        sans.append(family)
    for fam in ("Microsoft YaHei", "SimHei", "SimSun", "Noto Sans CJK SC",
                "WenQuanYi Zen Hei", "DejaVu Sans"):
        if fam not in sans:
            sans.append(fam)

    rc = {
        # ---- 字体 ----
        "font.family": "sans-serif",
        "font.sans-serif": sans,
        "axes.unicode_minus": False,          # 负号用 ASCII '-'
        "font.size": font_size,
        "mathtext.fontset": "dejavusans",
        # ---- 字号 ----
        "axes.titlesize": font_size * 1.3,
        "axes.labelsize": font_size * 1.1,
        "xtick.labelsize": font_size * 0.9,
        "ytick.labelsize": font_size * 0.9,
        "legend.fontsize": font_size * 0.9,
        "figure.titlesize": font_size * 1.4,
        # ---- 图形/白底 ----
        "figure.dpi": 110,
        "figure.facecolor": "white",
        "savefig.facecolor": "white",
        "savefig.edgecolor": "white",
        "savefig.dpi": 160,
        "savefig.bbox": "tight",
        "savefig.pad_inches": 0.12,
        # ---- 坐标轴 ----
        "axes.facecolor": "white",
        "axes.edgecolor": "#333333",
        "axes.linewidth": 0.8,
        "axes.grid": grid,
        "axes.axisbelow": True,
        "axes.labelcolor": "#111111",
        "axes.titlecolor": "#111111",
        "axes.prop_cycle": matplotlib.cycler(color=PALETTE),
        "axes.formatter.use_mathtext": False,
        # ---- 网格 ----
        "grid.color": COLORS["grid"],
        "grid.linestyle": ":",
        "grid.linewidth": 0.6,
        "grid.alpha": 0.7,
        # ---- 线/点 ----
        "lines.linewidth": 1.6,
        "lines.markersize": 4.0,
        "lines.solid_capstyle": "round",
        "patch.linewidth": 0.6,
        # ---- 图例 ----
        "legend.frameon": False,
        "legend.borderaxespad": 0.4,
        "legend.handlelength": 1.8,
        # ---- 其它 ----
        "image.interpolation": "nearest",
        "figure.autolayout": False,
        "errorbar.capsize": 2.0,
        "hist.bins": 40,
    }
    matplotlib.rcParams.update(rc)
    if verbose:
        print(f"[plotstyle] 中文字体: {family}  ({path})")
    return dict(rc)


def new_figure(width_in: float = 10.0, height_in: float = 6.0, dpi: int = 110, **kwargs):
    """按英寸尺寸新建 figure/ax（保证导出像素宽 = width_in × dpi）。

    Parameters
    ----------
    width_in, height_in : float
        图幅尺寸 [英寸]。
    dpi : int
        屏幕 dpi（保存时由 :func:`save` 的 dpi 决定最终像素）。

    Returns
    -------
    (fig, ax) : matplotlib Figure, Axes
    """
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(width_in, height_in), dpi=dpi, **kwargs)
    return fig, ax


def save(fig, path, dpi: int = 160, tight: bool = True, verbose: bool = True):
    """保存图形：白底、紧凑布局、自动建目录；返回 (绝对路径, 像素宽, 像素高)。

    Parameters
    ----------
    fig : matplotlib.figure.Figure
        要保存的图。
    path : str or Path
        输出路径（目录不存在会自动创建）。
    dpi : int
        输出分辨率，默认 160（sky 模块要求 ≥1600 px 宽时可提高 figsize 或 dpi）。
    tight : bool
        是否用 ``bbox_inches="tight"`` 裁掉白边。
    verbose : bool
        是否打印保存信息（含像素尺寸）。

    Returns
    -------
    (Path, int, int)
        绝对路径、像素宽、像素高。
    """
    import matplotlib.pyplot as plt

    p = Path(path)
    if not p.is_absolute():
        # 相对路径按工程根目录解析：<repo>/common/plotstyle.py → parents[1]
        p = Path(__file__).resolve().parents[1] / p
    p.parent.mkdir(parents=True, exist_ok=True)

    kwargs = dict(dpi=dpi, facecolor="white", edgecolor="white",
                  metadata={"Software": "common.plotstyle.save"})
    if tight:
        kwargs.update(bbox_inches="tight", pad_inches=0.12)
    fig.savefig(p, **kwargs)
    px_w, px_h = (int(round(v * dpi)) for v in fig.get_size_inches())
    if tight:
        # tight 裁剪后实际尺寸会变；用渲染后的像素尺寸更准
        px_w, px_h = fig.canvas.get_width_height()
        px_w, px_h = int(px_w * dpi / fig.dpi), int(px_h * dpi / fig.dpi)
    if verbose:
        print(f"[plotstyle] 图已保存 {p}  ({px_w}x{px_h} px @ {dpi} dpi)", flush=True)
    return p, px_w, px_h
