# -*- coding: utf-8 -*-
"""帮助中心窗口：左侧主题列表 + 右侧正文 + 搜索。

比 v3 的"一个大文本框"更进一步：
* 按"我卡在哪一步"分主题（快速开始 / 六个步骤 / 自定义箭型 / FAQ / 术语 / 限制）；
* 顶部搜索框实时过滤主题标题与正文；
* 键盘 ↑/↓ 切换主题，Esc 关闭；
* 右上角「打开完整文档」直接跳到 ``文档/`` 目录（详细规范在那里）。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import List, Optional

from . import help_text, theme, widgets


class HelpDialog(tk.Toplevel):
    """帮助中心。"""

    def __init__(self, parent, topic: str = "", topic_key: str = ""):
        super().__init__(parent)
        self.title("帮助中心 · 火箭弹道工具箱 v4")
        self.configure(background=theme.WINDOW_BG)
        self.transient(parent)
        self.resizable(True, True)
        self._topics = list(help_text.HELP_TOPICS)
        self._filtered = list(self._topics)
        self._build(topic, topic_key)
        widgets.center_window(self, parent, 1020, 720)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda e: self.close())
        try:
            self.after(60, widgets.safe_grab, self)
        except tk.TclError:
            pass
        try:
            self.wait_window(self)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ #
    def _build(self, topic: str, topic_key: str) -> None:
        outer = ttk.Frame(self, padding=(14, 12))
        outer.pack(fill="both", expand=True)
        outer.rowconfigure(2, weight=1)
        outer.columnconfigure(0, weight=1)

        head = ttk.Frame(outer)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(1, weight=1)
        ttk.Label(head, text="帮助中心", style="PageTitle.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Label(head, text="按「我正卡在哪一步」找；详细规范在 文档/ 目录",
                  style="Subtitle.TLabel").grid(row=1, column=0, sticky="w",
                                                pady=(2, 0))
        btns = ttk.Frame(head)
        btns.grid(row=0, column=2, rowspan=2, sticky="e")
        ttk.Button(btns, text="打开完整文档", style="Small.TButton",
                   command=self._open_docs).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="关闭（Esc）", style="Small.TButton",
                   command=self.close).pack(side="left")

        search = ttk.Frame(outer)
        search.grid(row=1, column=0, sticky="ew", pady=(10, 8))
        ttk.Label(search, text="搜索", style="TLabel").pack(side="left")
        self.search_var = tk.StringVar()
        ttk.Entry(search, textvariable=self.search_var, width=40).pack(
            side="left", padx=(6, 10))
        self.search_var.trace_add("write", lambda *a: self._refresh_list())
        ttk.Label(search, text="（输入关键字过滤主题；↑/↓ 切换）",
                  style="Hint.TLabel").pack(side="left")

        body = ttk.Frame(outer)
        body.grid(row=2, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)

        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="ns")
        left.rowconfigure(0, weight=1)
        self.listbox = tk.Listbox(left, width=26, font=theme.font(theme.BASE_SIZE),
                                  activestyle="none", exportselection=False,
                                  background=theme.PANEL_BG, foreground=theme.FG,
                                  selectbackground=theme.SELECT_BG,
                                  selectforeground=theme.FG, borderwidth=1,
                                  relief="solid", highlightthickness=0)
        self.listbox.grid(row=0, column=0, sticky="ns")
        sb = ttk.Scrollbar(left, orient="vertical", command=self.listbox.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.bind("<<ListboxSelect>>", lambda e: self._on_select())
        self.listbox.bind("<Down>", lambda e: self._move(1))
        self.listbox.bind("<Up>", lambda e: self._move(-1))

        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        self.text_box = widgets.TextBox(right, height=20, wrap="word", readonly=True,
                                        font=theme.font(theme.BASE_SIZE))
        self.text_box.grid(row=0, column=0, sticky="nsew")

        self._refresh_list()
        self._select_default(topic, topic_key)

    # ------------------------------------------------------------------ #
    def _refresh_list(self) -> None:
        q = self.search_var.get().strip().lower()
        self._filtered = [t for t in self._topics
                          if not q or q in (t["title"] + t["body"]).lower()]
        self.listbox.delete(0, "end")
        for t in self._filtered:
            self.listbox.insert("end", t["title"])
        if self._filtered:
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(0)
            self._show(self._filtered[0])
        else:
            self.text_box.set_text("没有匹配的主题。换个关键字试试，"
                                   "或点右上角「打开完整文档」。")

    def _select_default(self, topic: str, topic_key: str) -> None:
        key = topic_key or help_text.PAGE_TOPIC.get(topic, "")
        target = help_text.TOPIC_BY_KEY.get(key)
        names = [t["title"] for t in self._filtered]
        if target is not None and target["title"] in names:
            idx = names.index(target["title"])
        elif topic and topic in names:
            idx = names.index(topic)
        else:
            idx = 0
        if self._filtered:
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(idx)
            self.listbox.see(idx)
            self._show(self._filtered[idx])

    def _on_select(self) -> None:
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self._filtered):
            self._show(self._filtered[idx])

    def _move(self, delta: int) -> None:
        sel = self.listbox.curselection()
        idx = int(sel[0]) if sel else 0
        idx = max(0, min(len(self._filtered) - 1, idx + delta))
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(idx)
        self.listbox.see(idx)
        self._on_select()
        return "break"

    def _show(self, topic: dict) -> None:
        body = topic.get("body", "")
        header = topic.get("title", "")
        self.text_box.set_text("%s\n%s\n\n%s" % (header, "=" * min(60, len(header) * 2),
                                                 body))
        self.text_box.text.yview_moveto(0.0)

    def _open_docs(self) -> None:
        try:
            from app.core import paths as P
            root = P.APP_ROOT / "文档"
            if not root.is_dir():
                root = P.APP_ROOT
            ok, msg = widgets.open_path_ex(root)
            if not ok:
                widgets.show_error(self, "无法打开", msg)
        except Exception as exc:  # noqa: BLE001
            widgets.show_error(self, "无法打开文档目录", widgets.explain_error(exc))

    def close(self) -> None:
        try:
            self.grab_release()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()


def show_help(parent, topic: str = "", topic_key: str = "") -> Optional[HelpDialog]:
    """打开帮助中心（供 MainActionsMixin 调用）。"""
    try:
        return HelpDialog(parent, topic=topic, topic_key=topic_key)
    except Exception as exc:  # noqa: BLE001
        widgets.write_error_log("打开帮助中心", exc)
        widgets.show_error(parent, "帮助中心打开失败", widgets.explain_error(exc))
        return None
