# -*- coding: utf-8 -*-
"""左侧步骤导航（向导式）：序号 + 标题 + 状态徽标，替代 v3 的七页 Notebook。

为什么换
--------
v3 用 ``ttk.Notebook`` 平铺 7 个分页，用户看不出"该按什么顺序填"，也看不出哪一页
已就绪、哪一页还有低置信度项。v4 改成左侧步骤列表：

* 序号 + 中文标题 + 一句副标题；
* 右侧状态徽标（已填/待填、N 条航警、N 项体检…），颜色规则与表格一致；
* 当前页高亮，键盘 Ctrl+1..8 直达；
* 底部"上一步/下一步"与页面进度，适合第一次使用的用户按向导走。

实现只用 tk 原生控件（颜色可控），不依赖 ttk 主题细节。
"""
from __future__ import annotations

import tkinter as tk
from typing import Callable, Dict, List, Optional, Tuple

from . import theme

#: 徽标颜色：kind → (背景, 前景)
BADGE_COLORS: Dict[str, Tuple[str, str]] = {
    "ok": (theme.OK_BG, theme.OK),
    "warn": (theme.WARN_BG, theme.WARN),
    "err": (theme.ERR_BG, theme.ERR),
    "muted": ("#eceff3", theme.FG_MUTED),
    "accent": (theme.ACCENT_LIGHT, theme.ACCENT),
}


class NavItem:
    """单条步骤的控件集合。"""

    def __init__(self, parent: "StepNav", index: int, key: str, title: str,
                 subtitle: str):
        self.key = key
        self.title = title
        self.frame = tk.Frame(parent, background=theme.NAV_BG, cursor="hand2",
                              highlightthickness=0)
        self.frame.pack(fill="x", padx=6, pady=1)
        self.num = tk.Label(self.frame, text=str(index), width=3,
                            background=theme.NAV_BG, foreground=theme.FG_FAINT,
                            font=theme.font(theme.BASE_SIZE, True), anchor="center")
        self.num.grid(row=0, column=0, rowspan=2, sticky="nsw", padx=(6, 4), pady=6)
        self.title_label = tk.Label(self.frame, text=title, anchor="w",
                                    background=theme.NAV_BG, foreground=theme.FG,
                                    font=theme.font(theme.BASE_SIZE, True))
        self.title_label.grid(row=0, column=1, sticky="w", pady=(6, 0))
        self.sub_label = tk.Label(self.frame, text=subtitle, anchor="w",
                                  background=theme.NAV_BG, foreground=theme.FG_MUTED,
                                  font=theme.font(theme.SMALL_SIZE - 1))
        self.sub_label.grid(row=1, column=1, sticky="w", pady=(0, 6))
        self.badge = tk.Label(self.frame, text="", anchor="e",
                              background=theme.NAV_BG, foreground=theme.FG_MUTED,
                              font=theme.font(theme.SMALL_SIZE - 1))
        self.badge.grid(row=0, column=2, rowspan=2, sticky="e", padx=(4, 8))
        self.frame.columnconfigure(1, weight=1)
        for w in (self.frame, self.num, self.title_label, self.sub_label, self.badge):
            w.bind("<Button-1>", lambda e, k=key: parent.on_select(k))
            w.bind("<Enter>", lambda e, k=key: parent.on_hover(k, True))
            w.bind("<Leave>", lambda e, k=key: parent.on_hover(k, False))
        self._badge_text = ""
        self._badge_kind = "muted"
        self._active = False

    def set_badge(self, text: str, kind: str = "muted") -> None:
        self._badge_text, self._badge_kind = text or "", kind if kind in BADGE_COLORS else "muted"
        self._paint()

    def set_active(self, active: bool) -> None:
        self._active = bool(active)
        self._paint()

    def _paint(self) -> None:
        bg = theme.NAV_ACTIVE_BG if self._active else theme.NAV_BG
        fg = theme.ACCENT if self._active else theme.FG
        for w in (self.frame, self.num, self.title_label, self.sub_label, self.badge):
            try:
                w.configure(background=bg)
            except tk.TclError:
                pass
        try:
            self.title_label.configure(foreground=fg)
            self.num.configure(foreground=theme.ACCENT if self._active
                               else theme.FG_FAINT)
            if self._badge_text:
                bbg, bfg = BADGE_COLORS[self._badge_kind]
                self.badge.configure(text=self._badge_text, background=bbg,
                                     foreground=bfg, padx=6)
            else:
                self.badge.configure(text="", background=bg, foreground=theme.FG_MUTED)
        except tk.TclError:
            pass


class StepNav(tk.Frame):
    """步骤导航栏。"""

    def __init__(self, master, steps: List[Dict[str, str]],
                 on_select: Callable[[str], None]):
        super().__init__(master, background=theme.NAV_BG, width=228)
        self.on_select = on_select
        self.items: Dict[str, NavItem] = {}
        self.order: List[str] = []
        self.active: Optional[str] = None
        head = tk.Frame(self, background=theme.NAV_BG)
        head.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(head, text="任务流程", anchor="w", background=theme.NAV_BG,
                 foreground=theme.FG_MUTED,
                 font=theme.font(theme.SMALL_SIZE, True)).pack(anchor="w")
        tk.Label(head, text="按顺序填写；灰/橙徽标表示还缺信息", anchor="w",
                 background=theme.NAV_BG, foreground=theme.FG_FAINT,
                 font=theme.font(theme.SMALL_SIZE - 1)).pack(anchor="w", pady=(1, 0))
        tk.Frame(self, background=theme.BORDER, height=1).pack(fill="x", padx=8)
        body = tk.Frame(self, background=theme.NAV_BG)
        body.pack(fill="both", expand=True, pady=(4, 6))
        for i, st in enumerate(steps, start=1):
            item = NavItem(body, i, st["key"], st["title"], st.get("subtitle", ""))
            self.items[st["key"]] = item
            self.order.append(st["key"])
        tk.Frame(self, background=theme.BORDER, height=1).pack(fill="x", padx=8)
        self.foot = tk.Label(self, text="", anchor="w", justify="left",
                             background=theme.NAV_BG, foreground=theme.FG_MUTED,
                             font=theme.font(theme.SMALL_SIZE - 1), wraplength=200)
        self.foot.pack(fill="x", padx=12, pady=(6, 10))

    # ------------------------------------------------------------------ #
    def on_hover(self, key: str, entering: bool) -> None:
        item = self.items.get(key)
        if item is None or key == self.active:
            return
        bg = theme.ACCENT_LIGHT if entering else theme.NAV_BG
        try:
            for w in (item.frame, item.num, item.title_label, item.sub_label):
                w.configure(background=bg)
            item.badge.configure(background=bg)
        except tk.TclError:
            pass

    def set_active(self, key: str) -> None:
        self.active = key
        for k, item in self.items.items():
            item.set_active(k == key)
        idx = self.order.index(key) + 1 if key in self.order else 0
        self.foot.configure(text="第 %d / %d 步" % (idx, len(self.order)) if idx else "")

    def set_badge(self, key: str, text: str, kind: str = "muted") -> None:
        item = self.items.get(key)
        if item is not None:
            item.set_badge(text, kind)

    def set_foot(self, text: str) -> None:
        self.foot.configure(text=text)
