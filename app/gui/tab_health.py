# -*- coding: utf-8 -*-
"""分页 ⑤：数据体检（Resolution 明细 / 低置信度项 / 提示 / 一键写入配置）。"""
from __future__ import annotations

import tkinter as tk
from datetime import datetime, timedelta
from tkinter import ttk
from typing import List, Optional

from . import theme, widgets

COLUMNS = [
    ("field", "字段", 190, "w"),
    ("value", "取值", 230, "w"),
    ("source", "来源", 80, "center"),
    ("conf", "置信度", 66, "center"),
    ("note", "说明", 330, "w"),
]


class TabHealth(ttk.Frame):
    """⑤ 数据体检。"""

    def __init__(self, master, app):
        super().__init__(master, padding=(14, 10))
        self.app = app
        self._br = None
        self._build()
        self.reset()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=3)
        self.rowconfigure(4, weight=1)

        summary = widgets.Card(self, title="体检摘要")
        summary.grid(row=0, column=0, sticky="ew")
        body = summary.body
        body.columnconfigure(1, weight=1)
        body.columnconfigure(3, weight=1)

        self.name_var = tk.StringVar(value="（尚未生成）")
        self.t0_var = tk.StringVar(value="（尚未生成）")
        self.az_var = tk.StringVar(value="（尚未生成）")
        self.plan_var = tk.StringVar(value="（尚未生成）")
        self.stat_var = tk.StringVar(value="（尚未生成）")

        def label(r: int, c: int, text: str) -> None:
            ttk.Label(body, text=text, style="CardHint.TLabel").grid(
                row=r, column=c, sticky="ne", padx=(0, 10), pady=3)

        def value(var: tk.StringVar, r: int, c: int, span: int = 1,
                  wrap: int = 0) -> None:
            lb = ttk.Label(body, textvariable=var, style="Panel.TLabel",
                           justify="left")
            if wrap:
                lb.configure(wraplength=wrap)
            lb.grid(row=r, column=c, columnspan=span, sticky="w", pady=3)

        label(0, 0, "任务名")
        value(self.name_var, 0, 1, 3)
        label(1, 0, "T0")
        value(self.t0_var, 1, 1, 3)
        label(2, 0, "方位角")
        value(self.az_var, 2, 1)
        label(2, 2, "体检统计")
        value(self.stat_var, 2, 3, wrap=520)
        label(3, 0, "反演计划")
        value(self.plan_var, 3, 1, 3, wrap=900)

        bar = ttk.Frame(self)
        bar.grid(row=1, column=0, sticky="ew", pady=(8, 4))
        self.health_btn = ttk.Button(bar, text="生成体检",
                                     command=self.app.run_health,
                                     style="Accent.TButton")
        self.health_btn.pack(side="left")
        self.write_btn = ttk.Button(bar, text="一键补全并写入配置",
                                    command=self.app.write_config_now)
        self.write_btn.pack(side="left", padx=(8, 0))
        widgets.hint(bar, theme.SOURCE_LEGEND).pack(side="left", padx=(14, 0))

        detail = ttk.LabelFrame(self, text=" 体检明细 ")
        detail.grid(row=2, column=0, sticky="nsew", pady=(4, 4))
        detail.rowconfigure(0, weight=1)
        detail.columnconfigure(0, weight=1)
        container, self.tree = widgets.make_tree(detail, COLUMNS, height=10)
        container.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)

        bottom = ttk.Panedwindow(self, orient="horizontal")
        bottom.grid(row=4, column=0, sticky="nsew")
        gaps = ttk.LabelFrame(bottom, text=" 低置信度项（结论需注明不确定性） ")
        warns = ttk.LabelFrame(bottom, text=" 提示 ")
        for frame in (gaps, warns):
            frame.rowconfigure(0, weight=1)
            frame.columnconfigure(0, weight=1)
        self.gap_box = widgets.TextBox(gaps, height=5, wrap="word", readonly=True)
        self.gap_box.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self.warn_box = widgets.TextBox(warns, height=5, wrap="word", readonly=True)
        self.warn_box.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        bottom.add(gaps, weight=1)
        bottom.add(warns, weight=1)

    # ------------------------------------------------------------------ #
    def set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for btn in (self.health_btn, self.write_btn):
            try:
                btn.configure(state=state)
            except tk.TclError:
                pass

    def reset(self) -> None:
        self._br = None
        for var in (self.name_var, self.t0_var, self.az_var, self.plan_var, self.stat_var):
            var.set("（尚未生成）")
        widgets.clear_tree(self.tree)
        self.gap_box.set_text("")
        self.warn_box.set_text("")

    # ------------------------------------------------------------------ #
    def show_result(self, br, plan: Optional[List[str]] = None,
                    workdir: str = "") -> None:
        """展示 build_config 的结果（BuildResult）。"""
        self._br = br
        config = getattr(br, "config", None) or {}
        mission = config.get("mission") or {}
        guidance = config.get("guidance") or {}

        self.name_var.set(str(mission.get("name") or "（未命名）"))
        t0 = str(mission.get("t0_utc") or "")
        self.t0_var.set(self._fmt_t0(t0))
        az = guidance.get("azimuth_deg")
        self.az_var.set("%.2f°" % float(az) if az is not None else "（未推断）")
        self.plan_var.set("\n".join(plan) if plan else "（无自动计划）")
        res_list = list(getattr(br, "resolutions", []) or [])
        gaps = list(getattr(br, "gaps", []) or [])
        warns = list(getattr(br, "warnings", []) or [])
        self.stat_var.set("体检项 %d 条；低置信度 %d 条；提示 %d 条%s" % (
            len(res_list), len(gaps), len(warns),
            ("；工作区：%s" % workdir) if workdir else ""))

        widgets.clear_tree(self.tree)
        for r in res_list:
            self.tree.insert("", "end", values=(
                r.field, r.value, r.source_cn(), r.confidence, r.note or "",
            ), tags=(theme.source_tag(r.source, r.confidence),))

        if gaps:
            lines = ["- [%s/%s] %s = %s" % (g.source_cn(), g.confidence, g.label, g.value)
                     for g in gaps]
            lines.append("")
            lines.append("说明：低置信度项多来自「默认值兜底」或几何病态推断（例如仅凭射向"
                         "推不出倾角）；使用结论时请注明不确定性，可在对应分页补充信息后"
                         "重新生成体检。与库内典型轨道不一致但数值可信的项会出现在「提示」里，"
                         "而不是这里。")
            self.gap_box.set_text("\n".join(lines))
        else:
            self.gap_box.set_text("无低置信度项。")
        if warns:
            self.warn_box.set_text("\n".join("%d. %s" % (i + 1, w)
                                             for i, w in enumerate(warns)))
        else:
            self.warn_box.set_text("无提示。")

    @staticmethod
    def _fmt_t0(t0: str) -> str:
        if not t0:
            return "（未推断）"
        try:
            dt = datetime.strptime(t0, "%Y-%m-%dT%H:%M:%SZ")
            bj = dt + timedelta(hours=8)
            return "%s（UTC）  /  %s（北京时）" % (
                dt.strftime("%Y-%m-%d %H:%M:%S"), bj.strftime("%Y-%m-%d %H:%M:%S"))
        except ValueError:
            return t0

    # ------------------------------------------------------------------ #
    def on_show(self) -> None:
        return
