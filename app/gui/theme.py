# -*- coding: utf-8 -*-
"""界面主题：配色、字体与 ttk 样式（v4 重构版）。

设计原则（v4）
-------------
1. **只读浅色 + 单一强调色**：降低视觉噪声，把强调色留给"主操作"和"当前步骤"；
2. **三档字号**：正文 10、辅助 9、标题 13/15——v3 的 9 号正文在 1080p 上偏小；
3. **状态色只有四种**（通过/警告/错误/中性），且同时用于表格行、徽标、日志；
4. 只依赖 tkinter：``app.core.paths`` 等重模块不在本模块导入。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# --------------------------------------------------------------------------- #
# 字体
# --------------------------------------------------------------------------- #
FONT_FAMILY = "微软雅黑"
MONO_FAMILY = "Consolas"
BASE_SIZE = 10
SMALL_SIZE = 9
TITLE_SIZE = 14
SECTION_SIZE = 11


def font(size: int = BASE_SIZE, bold: bool = False, family: str = FONT_FAMILY):
    """返回 tk 字体元组。"""
    return (family, size, "bold") if bold else (family, size)


def mono(size: int = SMALL_SIZE, bold: bool = False):
    return (MONO_FAMILY, size, "bold") if bold else (MONO_FAMILY, size)


# --------------------------------------------------------------------------- #
# 配色
# --------------------------------------------------------------------------- #
WINDOW_BG = "#f3f5f8"
PANEL_BG = "#ffffff"
PANEL_ALT = "#fafbfd"
HEAD_BG = "#e9eef5"
STATUS_BG = "#e4e9f0"
TOOLBAR_BG = "#ffffff"
NAV_BG = "#f7f9fc"
NAV_ACTIVE_BG = "#e4eef9"
BORDER = "#d3dbe5"
BORDER_STRONG = "#b9c5d4"
FG = "#1d2733"
FG_MUTED = "#65717f"
FG_FAINT = "#8b97a6"
ACCENT = "#12619f"
ACCENT_DARK = "#0d4d80"
ACCENT_LIGHT = "#e8f2fb"
OK = "#1b7f3b"
OK_BG = "#e6f4ea"
WARN = "#a96500"
WARN_BG = "#fdf3e2"
ERR = "#b3261e"
ERR_BG = "#fbeceb"
PURPLE = "#6a3d9a"
INFO_BG = "#eef4fa"
LOW_ROW_BG = "#fdeceb"
SELECT_BG = "#d6e7f7"
DISABLED_FG = "#9aa5b1"
FIELD_BG = "#ffffff"
FIELD_READONLY_BG = "#f5f7fa"

#: 数据体检「来源」中文名与配色（与 app.core.model.Resolution.SOURCE_CN 一致）
SOURCE_CN = {"user": "用户给定", "db": "内置库", "infer": "自动推断",
             "default": "默认值", "derived": "计算得出"}
SOURCE_COLORS = {"user": OK, "db": ACCENT, "infer": WARN,
                 "default": FG_MUTED, "derived": PURPLE}

#: 运行结果等级色
LEVEL_COLORS = {"info": FG, "log": "#48535f", "warn": WARN, "error": ERR,
                "step": ACCENT, "ok": OK}

#: 步骤台账状态色
STATE_COLORS = {"完成": OK, "跳过": WARN, "不适用": FG_FAINT, "失败": ERR,
                "取消": FG_MUTED, "未启用（未勾选模块）": FG_MUTED,
                "未勾选模块": FG_MUTED}

#: 通用提示（界面小字）
SOURCE_LEGEND = ("来源配色：用户给定=绿，内置库=蓝，自动推断=橙，默认值=灰，"
                 "计算得出=紫；置信度「低」整行红底。")


def source_tag(source: str, confidence: str = "") -> str:
    """数据体检表行标签名（同一标签同时含前景色与低置信度红底）。"""
    tag = "src_" + (source or "default")
    if confidence == "低":
        tag += "_low"
    return tag


# --------------------------------------------------------------------------- #
# 主题应用
# --------------------------------------------------------------------------- #
def apply_theme(root: tk.Misc) -> None:
    """应用 ttk 主题（clam 对 Treeview 行配色支持最稳定）。"""
    root.configure(background=WINDOW_BG)
    style = ttk.Style(root)
    for name in ("clam", "vista", "xpnative", "winnative", "default"):
        try:
            if name in style.theme_names():
                style.theme_use(name)
                break
        except tk.TclError:
            continue

    base = font()
    small = font(SMALL_SIZE)
    bold = font(BASE_SIZE, True)

    style.configure(".", font=base)

    # ---- 容器 ----
    style.configure("TFrame", background=WINDOW_BG)
    style.configure("Panel.TFrame", background=PANEL_BG)
    style.configure("Card.TFrame", background=PANEL_BG, relief="solid", borderwidth=1)
    style.configure("Toolbar.TFrame", background=TOOLBAR_BG)
    style.configure("Statusbar.TFrame", background=STATUS_BG)
    style.configure("Nav.TFrame", background=NAV_BG)
    style.configure("NavActive.TFrame", background=NAV_ACTIVE_BG)
    style.configure("HintBar.TFrame", background=INFO_BG)

    # ---- 文本 ----
    style.configure("TLabel", background=WINDOW_BG, foreground=FG, font=base)
    style.configure("Panel.TLabel", background=PANEL_BG, foreground=FG, font=base)
    style.configure("Hint.TLabel", background=WINDOW_BG, foreground=FG_MUTED, font=small)
    style.configure("CardHint.TLabel", background=PANEL_BG, foreground=FG_MUTED, font=small)
    style.configure("Warn.TLabel", background=WINDOW_BG, foreground=WARN, font=small)
    style.configure("Ok.TLabel", background=WINDOW_BG, foreground=OK, font=small)
    style.configure("Err.TLabel", background=WINDOW_BG, foreground=ERR, font=small)
    style.configure("Value.TLabel", background=PANEL_BG, foreground=FG, font=base)
    style.configure("Title.TLabel", background=WINDOW_BG, foreground=ACCENT,
                    font=font(TITLE_SIZE, True))
    style.configure("PageTitle.TLabel", background=WINDOW_BG, foreground=FG,
                    font=font(TITLE_SIZE, True))
    style.configure("Subtitle.TLabel", background=WINDOW_BG, foreground=FG_MUTED,
                    font=font(SMALL_SIZE))
    style.configure("Section.TLabel", background=WINDOW_BG, foreground=ACCENT,
                    font=font(SECTION_SIZE, True))
    style.configure("CardTitle.TLabel", background=PANEL_BG, foreground=FG,
                    font=font(SECTION_SIZE, True))
    style.configure("Mono.TLabel", background=PANEL_BG, foreground=FG, font=mono())
    style.configure("Status.TLabel", background=STATUS_BG, foreground="#2b3744",
                    font=base, padding=(10, 4))
    style.configure("StatusValue.TLabel", background=STATUS_BG,
                    foreground=FG_MUTED, font=small, padding=(8, 4))
    style.configure("SummaryKey.TLabel", background=PANEL_BG, foreground=FG_MUTED,
                    font=small)
    style.configure("SummaryVal.TLabel", background=PANEL_BG, foreground=FG, font=base)

    # ---- 徽标 ----
    for name, bg, fg in (("Badge", ACCENT_LIGHT, ACCENT),
                         ("BadgeOk", OK_BG, OK),
                         ("BadgeWarn", WARN_BG, WARN),
                         ("BadgeErr", ERR_BG, ERR),
                         ("BadgeMuted", "#eceff3", FG_MUTED),
                         ("BadgePurple", "#f0eaf7", PURPLE)):
        style.configure("%s.TLabel" % name, background=bg, foreground=fg,
                        font=font(SMALL_SIZE, True), padding=(7, 2))

    # ---- 容器框 ----
    style.configure("TLabelframe", background=WINDOW_BG, bordercolor=BORDER,
                    relief="solid", borderwidth=1, padding=4)
    style.configure("TLabelframe.Label", background=WINDOW_BG, foreground=FG,
                    font=font(SECTION_SIZE, True))
    style.configure("Card.TLabelframe", background=WINDOW_BG, bordercolor=BORDER,
                    relief="solid", borderwidth=1, padding=6)
    style.configure("Card.TLabelframe.Label", background=PANEL_BG, foreground=FG,
                    font=font(SECTION_SIZE, True))
    style.configure("TSeparator", background=BORDER)

    # ---- 按钮 ----
    style.configure("TButton", font=base, padding=(10, 5), background="#eef2f7",
                    foreground=FG, borderwidth=1, relief="flat")
    style.map("TButton",
              background=[("disabled", "#f0f2f5"), ("pressed", "#dbe4ef"),
                          ("active", "#e4ebf4")],
              foreground=[("disabled", DISABLED_FG)])
    style.configure("Accent.TButton", font=font(BASE_SIZE, True), padding=(16, 6),
                    borderwidth=1)
    style.map("Accent.TButton",
              background=[("disabled", "#a9bccd"), ("pressed", ACCENT_DARK),
                          ("active", ACCENT_DARK), ("!disabled", ACCENT)],
              foreground=[("disabled", "#f4f7fa"), ("!disabled", "#ffffff")])
    style.configure("Ghost.TButton", font=small, padding=(8, 3), background=WINDOW_BG,
                    foreground=ACCENT, borderwidth=0)
    style.map("Ghost.TButton", background=[("active", ACCENT_LIGHT)])
    style.configure("Tool.TButton", font=base, padding=(11, 5))
    style.configure("Small.TButton", font=small, padding=(7, 3))

    # ---- 输入 ----
    style.configure("TEntry", fieldbackground=FIELD_BG, foreground=FG,
                    insertcolor=FG, padding=4, bordercolor=BORDER_STRONG,
                    lightcolor="#ffffff", darkcolor=BORDER, relief="solid")
    style.map("TEntry", bordercolor=[("focus", ACCENT)],
              fieldbackground=[("readonly", FIELD_READONLY_BG)])
    style.configure("TCombobox", fieldbackground=FIELD_BG, foreground=FG,
                    padding=3, arrowsize=15, bordercolor=BORDER)
    style.map("TCombobox",
              fieldbackground=[("readonly", FIELD_READONLY_BG)],
              bordercolor=[("focus", ACCENT)])
    style.configure("TSpinbox", fieldbackground=FIELD_BG, foreground=FG, padding=3)
    style.configure("TCheckbutton", background=WINDOW_BG, foreground=FG, font=base,
                    padding=(0, 2))
    style.map("TCheckbutton", background=[("active", WINDOW_BG)],
              foreground=[("disabled", DISABLED_FG)])
    style.configure("Panel.TCheckbutton", background=PANEL_BG, foreground=FG, font=base)
    style.map("Panel.TCheckbutton", background=[("active", PANEL_BG)])
    style.configure("TRadiobutton", background=WINDOW_BG, foreground=FG, font=base)
    style.map("TRadiobutton", background=[("active", WINDOW_BG)])
    style.configure("Panel.TRadiobutton", background=PANEL_BG, foreground=FG, font=base)
    style.map("Panel.TRadiobutton", background=[("active", PANEL_BG)])

    # ---- 进度 ----
    style.configure("TProgressbar", troughcolor="#e6ebf1", background=ACCENT,
                    bordercolor=BORDER, lightcolor=ACCENT, darkcolor=ACCENT,
                    thickness=10)
    style.configure("Thin.Horizontal.TProgressbar", troughcolor="#e6ebf1",
                    background=ACCENT, thickness=6)

    # ---- 表格 ----
    style.configure("Treeview", font=base, rowheight=25, background=PANEL_BG,
                    fieldbackground=PANEL_BG, foreground=FG, borderwidth=0)
    style.configure("Treeview.Heading", font=bold, background=HEAD_BG,
                    foreground="#33404d", padding=(6, 4), relief="flat")
    style.map("Treeview.Heading", background=[("active", "#dfe7f0")])
    style.map("Treeview",
              background=[("selected", SELECT_BG)],
              foreground=[("selected", FG)])
    style.configure("Compact.Treeview", font=small, rowheight=22)

    # ---- 其它 ----
    style.configure("TPanedwindow", background=WINDOW_BG, sashwidth=6)
    style.configure("Sash", sashthickness=6, gripcount=0)
    style.configure("Vertical.TScrollbar", background="#cfd8e3", arrowsize=12,
                    troughcolor="#f0f3f7", bordercolor=WINDOW_BG)
    style.configure("Horizontal.TScrollbar", background="#cfd8e3", arrowsize=12,
                    troughcolor="#f0f3f7", bordercolor=WINDOW_BG)

    try:
        root.option_add("*Font", "%s %d" % (FONT_FAMILY, BASE_SIZE))
        root.option_add("*TCombobox*Listbox.font", "%s %d" % (FONT_FAMILY, BASE_SIZE))
    except tk.TclError:
        pass
