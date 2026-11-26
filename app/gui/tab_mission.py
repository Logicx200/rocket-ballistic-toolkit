# -*- coding: utf-8 -*-
"""步骤 ①：任务与箭型（任务类型 / 火箭 / 发射工位 / 起飞时刻 / 载荷）。

v4 的布局与 v3 的差异
--------------------
* 顶部先选**任务类型**（常规发射 / 载人交会 / 返回舱返回）——它决定后面哪些步骤有意义；
* 箭型下拉里**直接看到"我的箭型"**，右侧一个「编辑箭型参数…」按钮打开图形编辑器，
  不再需要手写 `app/data/rockets.json`；
* 箭型简介区改成"参数摘要 + 关键指标（起飞推重比/工作段/理论 Δv）"，填错能马上看出来；
* T0 输入实时解析并在下方显示 UTC / 北京时预览，解析失败给中文原因。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Dict, List, Optional, Tuple

from app.core.model import MISSION_TYPE_CN, MissionInput

from . import theme, widgets

CUSTOM_ROCKET = "自定义/未知"
CUSTOM_SITE = "自定义坐标"
TZ_CHOICES = [("自动识别", "auto"), ("UTC（世界时）", "UTC"), ("北京时间（UTC+8）", "BJT")]
CLASS_CN = {"small_solid": "小型固体", "medium_liquid": "中型液体",
            "large_liquid": "大型液体", "crewed": "载人火箭"}

MISSION_TYPE_CHOICES = [
    ("standard", "常规发射弹道", "无人任务：上升段弹道、残骸落区、发射窗口、天空可见性"),
    ("crewed", "载人发射弹道", "含空间站轨道准备与入轨+交会对接方案（需要空间站星历）"),
    ("return", "返回舱返回弹道", "只需空间站轨道 + 着陆场/返回航警，不需要发射场与上升段"),
]


def _short(text: Any, n: int = 200) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


class TabMission(ttk.Frame):
    """① 任务与箭型。"""

    def __init__(self, master, app):
        super().__init__(master, padding=(16, 12))
        self.app = app
        self._db_ready = False
        self._rocket_items: List[Dict[str, Any]] = []
        self._site_items: List[Dict[str, Any]] = []
        self._payload_items: List[Dict[str, Any]] = []
        #: 自定义箭型（编辑器返回的完整记录；None=使用内置/用户库匹配）
        self.custom_rocket: Optional[Dict[str, Any]] = None
        self._custom_rocket_mode = ""
        self._suspend_t0_parse = False
        self._build()

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        row = 0
        widgets.page_title(
            self, "任务与箭型",
            "先确定任务类型与发射条件。除任务类型外，绝大部分字段可以留空：程序会用内置库与"
            "经验规则补全，并在「数据体检」里逐项标注来源与置信度。").grid(
            row=row, column=0, sticky="ew", pady=(0, 10))
        row += 1

        # ---- 任务类型 ----
        card = widgets.Card(self, title="任务类型",
                            hint_text="任务类型决定后面哪些步骤有意义：返回任务不需要发射场/"
                                      "火箭/上升段；载人任务会增加空间站交会与返回段设置。")
        card.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        row += 1
        self.type_var = tk.StringVar(value="standard")
        self._type_buttons: Dict[str, ttk.Radiobutton] = {}
        body = ttk.Frame(card.body, style="Panel.TFrame")
        body.pack(fill="x")
        for i, (key, label, desc) in enumerate(MISSION_TYPE_CHOICES):
            cell = ttk.Frame(body, style="Panel.TFrame")
            cell.grid(row=0, column=i, sticky="nw", padx=(0, 22))
            rb = ttk.Radiobutton(cell, text=label, value=key, variable=self.type_var,
                                 style="Panel.TRadiobutton",
                                 command=self._on_type_change)
            rb.pack(anchor="w")
            widgets.hint(cell, desc, wraplength=260, style="CardHint.TLabel").pack(anchor="w")
            self._type_buttons[key] = rb
        self.type_hint = widgets.hint(card.body, "", wraplength=980,
                                      style="CardHint.TLabel")
        self.type_hint.pack(anchor="w", pady=(8, 0))

        # ---- 火箭 ----
        rocket_card = widgets.Card(self, title="火箭型号")
        rocket_card.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        row += 1
        rocket_card.add_action("编辑箭型参数…", self._open_rocket_editor)
        rocket_card.add_action("从我的箭型库…", lambda: self.app.manage_user_rockets())
        box = ttk.Frame(rocket_card.body, style="Panel.TFrame")
        box.grid(row=0, column=0, sticky="ew")
        self.rocket_var = tk.StringVar()
        self.rocket_combo = ttk.Combobox(box, textvariable=self.rocket_var, width=36)
        self.rocket_combo.pack(side="left")
        self.rocket_combo.bind("<<ComboboxSelected>>", lambda e: self._on_rocket_changed())
        self.rocket_combo.bind("<FocusOut>", lambda e: self._on_rocket_changed())
        self.rocket_combo.bind("<Return>", lambda e: self._on_rocket_changed())
        ttk.Label(box, text="遥号", style="Panel.TLabel").pack(side="left", padx=(16, 6))
        self.serial_var = tk.StringVar()
        ttk.Entry(box, textvariable=self.serial_var, width=12).pack(side="left")
        self.rocket_badge = tk.Label(box, text="", background=theme.PANEL_BG,
                                     foreground=theme.FG_MUTED,
                                     font=theme.font(theme.SMALL_SIZE, True))
        self.rocket_badge.pack(side="left", padx=(12, 0))
        widgets.hint(box, "可输入简称（如 CZ-2D）；选「自定义/未知」按通用模板计算",
                     style="CardHint.TLabel").pack(side="left", padx=(10, 0))
        self.intro_box = widgets.TextBox(rocket_card.body, height=5, wrap="word",
                                         readonly=True)
        self.intro_box.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        # ---- 发射工位 ----
        site_card = widgets.Card(self, title="发射工位")
        site_card.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        row += 1
        sbox = ttk.Frame(site_card.body, style="Panel.TFrame")
        sbox.grid(row=0, column=0, sticky="ew")
        self.site_var = tk.StringVar()
        self.site_combo = ttk.Combobox(sbox, textvariable=self.site_var, width=46)
        self.site_combo.pack(side="left")
        self.site_combo.bind("<<ComboboxSelected>>", lambda e: self._on_site())
        self.site_combo.bind("<FocusOut>", lambda e: (self._on_site(),
                                                      self.app.refresh_summary()))
        self.custom_frame = ttk.Frame(sbox, style="Panel.TFrame")
        ttk.Label(self.custom_frame, text="纬度", style="Panel.TLabel").pack(
            side="left", padx=(12, 4))
        self.site_lat_var = tk.StringVar()
        ttk.Entry(self.custom_frame, textvariable=self.site_lat_var, width=12).pack(
            side="left")
        ttk.Label(self.custom_frame, text="经度", style="Panel.TLabel").pack(
            side="left", padx=(8, 4))
        self.site_lon_var = tk.StringVar()
        ttk.Entry(self.custom_frame, textvariable=self.site_lon_var, width=12).pack(
            side="left")
        ttk.Label(self.custom_frame, text="海拔 m", style="Panel.TLabel").pack(
            side="left", padx=(8, 4))
        self.site_alt_var = tk.StringVar()
        ttk.Entry(self.custom_frame, textvariable=self.site_alt_var, width=8).pack(
            side="left")
        self.site_fmt_var = tk.StringVar(value=widgets.coord_format_label())
        ttk.Label(self.custom_frame, text="格式", style="Panel.TLabel").pack(
            side="left", padx=(8, 4))
        self.site_fmt_combo = ttk.Combobox(self.custom_frame, textvariable=self.site_fmt_var,
                                           width=9, state="readonly",
                                           values=["十进制度", "度分秒"])
        self.site_fmt_combo.pack(side="left")
        self.site_fmt_combo.bind("<<ComboboxSelected>>", lambda e: self._on_site_fmt())
        self.custom_frame.pack(side="left")
        self._site_hint = widgets.hint(site_card.body, "", wraplength=980,
                                       style="CardHint.TLabel")
        self._site_hint.grid(row=1, column=0, sticky="w", pady=(6, 0))

        # ---- 起飞时刻 ----
        t0_card = widgets.Card(self, title="起飞时刻（T0）")
        t0_card.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        row += 1
        t0box = ttk.Frame(t0_card.body, style="Panel.TFrame")
        t0box.grid(row=0, column=0, sticky="ew")
        self.t0_var = tk.StringVar()
        self.t0_entry = ttk.Entry(t0box, textvariable=self.t0_var, width=30)
        self.t0_entry.pack(side="left")
        self.t0_unknown = tk.BooleanVar(value=False)
        ttk.Checkbutton(t0box, text="未知，自动从航警推断", variable=self.t0_unknown,
                        style="Panel.TCheckbutton",
                        command=self._on_t0_unknown).pack(side="left", padx=(12, 16))
        ttk.Label(t0box, text="时区", style="Panel.TLabel").pack(side="left", padx=(0, 6))
        self.tz_var = tk.StringVar(value=TZ_CHOICES[0][0])
        self.tz_combo = ttk.Combobox(t0box, textvariable=self.tz_var, width=16,
                                     state="readonly",
                                     values=[c[0] for c in TZ_CHOICES])
        self.tz_combo.pack(side="left")
        self.tz_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_t0_preview())
        self.t0_var.trace_add("write", lambda *a: self._refresh_t0_preview())
        self.t0_backsolve_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(t0box, text="载人任务：按交会窗口反推 T0",
                        variable=self.t0_backsolve_var,
                        style="Panel.TCheckbutton",
                        command=self._on_t0_backsolve).pack(side="left",
                                                            padx=(18, 0))
        self.t0_preview = widgets.hint(t0_card.body, "", wraplength=980,
                                       style="CardHint.TLabel")
        self.t0_preview.grid(row=1, column=0, sticky="w", pady=(6, 0))
        widgets.hint(t0_card.body,
                     "支持 2026-11-19 06:50（默认按北京时间）、2026-11-19T06:50:00Z、"
                     "2026-11-19 06:50 +08:00、NOTAM 压缩时间 2611182250（UTC）。",
                     wraplength=980, style="CardHint.TLabel").grid(
            row=2, column=0, sticky="w", pady=(2, 0))

        # ---- 载荷 ----
        pl_card = widgets.Card(self, title="载荷",
                               hint_text="都可留空：载荷名按内置库补质量，质量再按箭型典型值推断。")
        pl_card.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        row += 1
        pbox = ttk.Frame(pl_card.body, style="Panel.TFrame")
        pbox.grid(row=0, column=0, sticky="ew")
        ttk.Label(pbox, text="名称", style="Panel.TLabel").pack(side="left", padx=(0, 4))
        self.payload_var = tk.StringVar()
        self.payload_combo = ttk.Combobox(pbox, textvariable=self.payload_var, width=28)
        self.payload_combo.pack(side="left")
        self.payload_combo.bind("<<ComboboxSelected>>", lambda e: self._on_payload())
        ttk.Label(pbox, text="质量", style="Panel.TLabel").pack(side="left", padx=(14, 4))
        self.payload_kg_var = tk.StringVar()
        ttk.Entry(pbox, textvariable=self.payload_kg_var, width=11).pack(side="left")
        ttk.Label(pbox, text="kg", style="Panel.TLabel").pack(side="left", padx=(4, 0))

        # ---- 任务名 ----
        name_card = widgets.Card(self, title="任务名")
        name_card.grid(row=row, column=0, sticky="ew")
        row += 1
        nbox = ttk.Frame(name_card.body, style="Panel.TFrame")
        nbox.grid(row=0, column=0, sticky="ew")
        self.name_var = tk.StringVar()
        ttk.Entry(nbox, textvariable=self.name_var, width=64).pack(side="left")
        widgets.hint(nbox, "留空则按「日期 + 箭型 + 工位」自动命名",
                     style="CardHint.TLabel").pack(side="left", padx=(10, 0))

    # ------------------------------------------------------------------ #
    # 内置库
    # ------------------------------------------------------------------ #
    def _ensure_db(self) -> None:
        if self._db_ready:
            return
        self._db_ready = True
        try:
            from app.core import infer as I
            from app.core import userdb as U
        except Exception as exc:  # noqa: BLE001
            widgets.write_error_log("加载内置库", exc)
            self.intro_box.set_text("内置库不可用：%s" % widgets.explain_error(exc))
            return
        try:
            self._rocket_items = list(I.rocket_list())
        except Exception:  # noqa: BLE001
            self._rocket_items = []
        try:
            self._site_items = list(I.site_list())
        except Exception:  # noqa: BLE001
            self._site_items = []
        try:
            self._payload_items = list(I.payload_list())
        except Exception:  # noqa: BLE001
            self._payload_items = []

        rocket_names = [str(r.get("name")) for r in self._rocket_items if r.get("name")]
        self.rocket_combo.configure(values=[CUSTOM_ROCKET] + rocket_names)
        site_names = [str(s.get("name")) for s in self._site_items if s.get("name")]
        # 场区级条目：同一场区的所有工位合成一条「XX卫星发射中心（不指定工位）」
        sites_by_center: Dict[str, List[str]] = {}
        for nm in site_names:
            parts = nm.split()
            if len(parts) <= 1:
                continue
            sites_by_center.setdefault(parts[0], []).append(nm)
        self._center_map: List[Tuple[str, str, List[str]]] = []
        for key, pads in sites_by_center.items():
            self._center_map.append(("%s（不指定工位）" % key, key, pads))
        center_names = [c[0] for c in self._center_map]
        self.site_combo.configure(values=center_names + site_names + [CUSTOM_SITE])
        payload_names = [str(p.get("name")) for p in self._payload_items if p.get("name")]
        self.payload_combo.configure(values=[""] + payload_names)
        if site_names:
            self._site_hint.configure(
                text="内置工位 %d 个；也可选「场区（不指定工位）」或用「自定义坐标」直接填经纬度。"
                     % len(site_names))
        else:
            self._site_hint.configure(
                text="未找到内置发射场库：请选「自定义坐标」并填写经纬度。")
        self._update_intro()

    def on_show(self) -> None:
        self._ensure_db()

    # ------------------------------------------------------------------ #
    # 联动
    # ------------------------------------------------------------------ #
    def _match_rocket(self, label: str) -> Tuple[Optional[Dict[str, Any]], str]:
        if not label or label == CUSTOM_ROCKET:
            return None, ""
        try:
            from app.core import infer as I
            item, why = I.match_rocket(label)
            return (item, why) if item else (None, "")
        except Exception:  # noqa: BLE001
            for it in self._rocket_items:
                if str(it.get("name")) == label:
                    return it, "按名称匹配"
        return None, ""

    def current_rocket_record(self) -> Optional[Dict[str, Any]]:
        """当前生效的箭型记录（自定义优先，其次内置/用户库匹配）。"""
        if self.custom_rocket:
            return dict(self.custom_rocket)
        label = self.rocket_var.get().strip()
        item, _why = self._match_rocket(label)
        return dict(item) if item else None

    def _open_rocket_editor(self) -> None:
        base = self.current_rocket_record()
        result = self.app.edit_rocket(base)
        if not result:
            return
        self.custom_rocket = dict(result)
        self._custom_rocket_mode = "user_db" if result.get("_user") else "session"
        self.rocket_var.set(str(result.get("name") or "自定义箭型"))
        self._update_intro()
        self.app.set_status("已应用自定义箭型：%s（%d 级）" % (
            result.get("name"), len((result.get("vehicle") or {}).get("stages") or [])),
            "ok")
        self.app.refresh_summary()
        self.app.refresh_nav()

    def _on_rocket_changed(self) -> None:
        label = self.rocket_var.get().strip()
        if self.custom_rocket and label not in (str(self.custom_rocket.get("name")), CUSTOM_ROCKET):
            # 用户改了型号文本：放弃"仅本次任务"的自定义箭型
            if self._custom_rocket_mode != "user_db":
                self.custom_rocket = None
        self._update_intro()
        self.app.refresh_summary()
        self.app.refresh_nav()

    def _update_intro(self) -> None:
        self._ensure_db()
        label = self.rocket_var.get().strip()
        item = self.current_rocket_record()
        lines: List[str] = []
        if self.custom_rocket:
            self.rocket_badge.configure(text="自定义", foreground=theme.ACCENT)
        elif item:
            self.rocket_badge.configure(text="内置库", foreground=theme.OK)
        else:
            self.rocket_badge.configure(text="未匹配", foreground=theme.WARN)
        if item:
            from app.core import userdb as U
            cls_key = U.class_key_of(item)
            lines.append("型号：%s（id：%s%s）" % (
                item.get("name"), item.get("id"),
                "，用户自定义" if item.get("_user") else ""))
            lines.append("类别：%s；库内置信度：%s" % (
                CLASS_CN.get(cls_key, str(item.get("class") or "未知")),
                item.get("confidence") or "中"))
            mass = item.get("liftoff_mass_kg")
            leo = item.get("leo_kg")
            sso = item.get("sso_kg")
            parts = []
            if mass:
                parts.append("起飞质量 %s t" % widgets.fmt_num(float(mass) / 1000.0, 1))
            if leo:
                parts.append("近地运力 %s kg" % widgets.fmt_num(leo))
            if sso:
                parts.append("太阳同步运力 %s kg" % widgets.fmt_num(sso))
            if parts:
                lines.append("；".join(parts))
            # 关键指标（起飞推重比 / 工作段 / 理论 Δv）——填错时能立刻看出来
            try:
                m = U.rocket_metrics(item)
                if m.get("liftoff_mass_kg"):
                    twr = m.get("liftoff_twr") or 0.0
                    burns = "、".join("%s %.0f s" % (b.get("name"), b.get("burn_s") or 0)
                                     for b in (m.get("burns") or []))
                    lines.append("关键指标：起飞推重比 %.2f（%s）；工作段 %s；理论 Δv≈%s m/s" % (
                        twr, "正常" if twr >= 1.15 else "偏低，可能无法起飞",
                        burns or "—", widgets.fmt_num(m.get("dv_ideal_mps"), 0)))
                    if m.get("warnings"):
                        lines.append("提示：" + "；".join(m["warnings"]))
            except Exception:  # noqa: BLE001
                pass
            tos = item.get("typical_orbits") or []
            if tos:
                lines.append("典型轨道：" + "，".join(
                    "%s（%s km / %s°）" % (t.get("name") or "轨道",
                                          widgets.fmt_num(t.get("alt_km")),
                                          widgets.fmt_num(t.get("i_deg")))
                    for t in tos[:3]))
            note = _short(item.get("note"), 200)
            if note:
                lines.append("说明：" + note)
            if item.get("_derived_from"):
                lines.append("来源：由内置箭型「%s」复制后修改" % item["_derived_from"])
        else:
            lines.append("未匹配内置箭型库：将按「通用中型液体火箭模板」计算，结果只具备量级意义。")
            lines.append("可输入内置/我的箭型名称，或点右上角「编辑箭型参数…」自己定义一型火箭"
                         "（不需要改数据库文件）。")
            lines.append("内置箭型 %d 个；发射场库 %d 个。" % (
                len(self._rocket_items), len(self._site_items)))
        self.intro_box.set_text("\n".join(lines))

    def _on_site(self) -> None:
        if self.site_var.get().strip() == CUSTOM_SITE:
            self.custom_frame.pack(side="left")
        else:
            self.custom_frame.pack_forget()
        self.app.refresh_summary()

    def _center_display(self, label: str) -> str:
        s = str(label or "").strip()
        for display, key, _pads in getattr(self, "_center_map", []):
            if s == display or s == key:
                return display
        return s

    def _site_fmt_value(self, text: str, axis: str) -> str:
        try:
            from app.core.notam_parse import parse_one_coord
            v = parse_one_coord(text, axis)
        except Exception:  # noqa: BLE001
            return str(text or "")
        return widgets.format_coord(v, axis) if v is not None else ""

    def _on_site_fmt(self) -> None:
        fmt = "dms" if self.site_fmt_var.get() == "度分秒" else "dec"
        widgets.set_coord_format(fmt)
        self.site_lat_var.set(self._site_fmt_value(self.site_lat_var.get(), "lat"))
        self.site_lon_var.set(self._site_fmt_value(self.site_lon_var.get(), "lon"))
        self.app.set_status("发射场坐标格式：%s" % widgets.coord_format_label(fmt))

    # ---- 任务类型 ----
    def mission_type_cn(self) -> str:
        key = self.type_var.get()
        for k, label, _d in MISSION_TYPE_CHOICES:
            if k == key:
                return label
        return "常规发射弹道"

    def _on_type_change(self) -> None:
        key = self.type_var.get()
        self.app._pages["station"].on_mission_type(key)
        if key == "return":
            self.type_hint.configure(
                text="返回任务：不需要发射场、火箭与上升段——本页只用于给返回标称时刻命名；"
                     "空间站星历与着陆场请在「⑤ 空间站与返回」页设置，航警页可粘贴返回航警。",
                style="Warn.TLabel")
            self._site_hint.configure(text="返回任务不使用发射场（可忽略本项）。")
        elif key == "crewed":
            self.type_hint.configure(
                text="载人任务：会增加「空间站轨道 → 交会窗口 → 入轨+对接时间线」，"
                     "并默认启用返回舱返回设置（可关闭）。请确认空间站星历覆盖任务时刻。",
                style="CardHint.TLabel")
            self._site_hint.configure(
                text="内置工位 %d 个；载人任务通常选酒泉 921 工位。" % len(self._site_items))
        else:
            self.type_hint.configure(
                text="常规发射任务：输出上升段弹道、残骸落区、发射窗口、天空可见性与全国地图。",
                style="CardHint.TLabel")
        self.app.refresh_summary()
        self.app.refresh_nav()

    # ---- T0 ----
    def _tz_value(self) -> str:
        disp = self.tz_var.get().strip()
        for label, code in TZ_CHOICES:
            if label == disp:
                return code
        return "auto"

    @staticmethod
    def _tz_display(code: str) -> str:
        for label, val in TZ_CHOICES:
            if val == (code or "auto"):
                return label
        return TZ_CHOICES[0][0]

    def _on_t0_backsolve(self) -> None:
        """「按交会窗口反推 T0」勾选状态变化。"""
        if self.t0_backsolve_var.get():
            self.app.set_status(
                "已启用：载人/交会任务在运行时会先按空间站窗口反推 T0，"
                "再用该 T0 重跑弹道与交会（报告 §4.4）。", "ok")
        else:
            self.app.set_status("已关闭 T0 反推：使用你填写的 T0（或航警推断值）。")
        self._refresh_t0_preview()
        self.app.refresh_summary()

    def _on_t0_unknown(self) -> None:
        state = "disabled" if self.t0_unknown.get() else "normal"
        self.t0_entry.configure(state=state)
        self.tz_combo.configure(state="disabled" if self.t0_unknown.get() else "readonly")
        self._refresh_t0_preview()
        self.app.refresh_summary()
        self.app.refresh_nav()

    def _refresh_t0_preview(self) -> None:
        if self._suspend_t0_parse:
            return
        if self.t0_unknown.get():
            _extra = ("；载人任务会按交会发射窗口反推 T0"
                      if self.t0_backsolve_var.get() else "")
            self.t0_preview.configure(
                text="T0 将由航警 B)/C) 与残骸到达时刻推断，首次仿真后按落区窗口精化%s。"
                     % _extra, style="Warn.TLabel")
            return
        raw = self.t0_var.get().strip()
        if not raw:
            self.t0_preview.configure(text="留空 = 未指定 T0（若有航警则自动推断）。",
                                      style="CardHint.TLabel")
            return
        try:
            from app.core.notam_parse import parse_t0_text
            dt, used, note = parse_t0_text(raw, self._tz_value(), None)
        except Exception as exc:  # noqa: BLE001
            self.t0_preview.configure(text="时间解析模块不可用：%s"
                                           % widgets.explain_error(exc),
                                      style="Err.TLabel")
            return
        if dt is None:
            self.t0_preview.configure(text="无法解析：%s" % note, style="Err.TLabel")
            return
        from datetime import timedelta
        bj = dt + timedelta(hours=8)
        _bs = ("；运行时会按交会窗口反推（报告 §4.4）"
               if self.t0_backsolve_var.get() else "")
        self.t0_preview.configure(
            text="识别为 UTC %s ／ 北京时 %s（%s%s）" % (
                dt.strftime("%Y-%m-%d %H:%M:%S"), bj.strftime("%Y-%m-%d %H:%M:%S"),
                note, _bs),
            style="Ok.TLabel")
        self.app.refresh_summary()

    def t0_effective_text(self) -> str:
        if self.t0_unknown.get() or not self.t0_var.get().strip():
            return "（未知，待推断）"
        raw = self.t0_var.get().strip()
        try:
            from app.core.notam_parse import parse_t0_text
            dt, used, _note = parse_t0_text(raw, self._tz_value(), None)
            if dt is not None:
                from datetime import timedelta
                return "%s BJT / %s UTC" % (
                    (dt + timedelta(hours=8)).strftime("%m-%d %H:%M"),
                    dt.strftime("%m-%d %H:%M"))
        except Exception:  # noqa: BLE001
            pass
        return raw[:22]

    # ---- 载荷 ----
    def _on_payload(self) -> None:
        name = self.payload_var.get().strip()
        if not name:
            return
        try:
            from app.core import infer as I
            item, _why = I.match_payload(name)
        except Exception:  # noqa: BLE001
            item = None
        if not item:
            return
        if not self.payload_kg_var.get().strip() and item.get("mass_kg"):
            self.payload_kg_var.set(widgets.fmt_num(item.get("mass_kg")))

    # ------------------------------------------------------------------ #
    # 收集 / 载入
    # ------------------------------------------------------------------ #
    def collect(self, inp: MissionInput) -> None:
        inp.name = self.name_var.get().strip()
        inp.serial = self.serial_var.get().strip()
        inp.mission_type = self.type_var.get() or "auto"

        if self.custom_rocket:
            inp.custom_rocket = dict(self.custom_rocket)
            inp.custom_rocket_mode = self._custom_rocket_mode or "session"
            inp.rocket_label = str(self.custom_rocket.get("name") or "")
            inp.rocket_id = str(self.custom_rocket.get("id") or "custom")
            inp.profile = "standard"
        else:
            rock = self.rocket_var.get().strip()
            if not rock or rock == CUSTOM_ROCKET:
                inp.rocket_id = "custom" if rock == CUSTOM_ROCKET else "auto"
                inp.rocket_label = ""
            else:
                inp.rocket_label = rock
                item, _why = self._match_rocket(rock)
                inp.rocket_id = str(item.get("id")) if item and item.get("id") else "auto"
            inp.profile = "auto"

        site = self.site_var.get().strip()
        if inp.mission_type == "return":
            inp.site_label = site if site and site != CUSTOM_SITE else ""
            inp.site_id = "auto"
            inp.site_lat = inp.site_lon = inp.site_alt = None
        elif site == CUSTOM_SITE:
            inp.site_id = "custom"
            inp.site_label = "自定义发射场"
            lat = widgets.parse_coord(self.site_lat_var.get(), "发射场纬度", "lat")
            lon = widgets.parse_coord(self.site_lon_var.get(), "发射场经度", "lon")
            if lat is None or lon is None:
                raise ValueError("已选择「自定义坐标」，请填写发射场纬度与经度"
                                 "（支持十进制度或度分秒，如 38.8587 或 38°51′31″N）。")
            inp.site_lat, inp.site_lon = lat, lon
            inp.site_alt = widgets.parse_optional_float(self.site_alt_var.get(),
                                                        "发射场海拔", -500.0, 9000.0)
        elif site:
            center_key = next((k for d, k, _p in getattr(self, "_center_map", [])
                               if d == site), "")
            if center_key:
                inp.site_id = "auto"
                inp.site_label = center_key
                inp.site_lat = inp.site_lon = inp.site_alt = None
            else:
                inp.site_label = site
                inp.site_id = "auto"
                for s in self._site_items:
                    if str(s.get("name")) == site and s.get("id"):
                        inp.site_id = str(s.get("id"))
                        break
        else:
            inp.site_label = ""
            inp.site_id = "auto"

        if self.t0_unknown.get():
            inp.t0_text = ""
        else:
            raw = self.t0_var.get().strip()
            if raw:
                try:
                    from app.core.notam_parse import parse_t0_text
                    dt, _used, note = parse_t0_text(raw, self._tz_value(), None)
                except Exception as exc:  # noqa: BLE001
                    raise ValueError("时间解析模块不可用：%s" % widgets.explain_error(exc))
                if dt is None:
                    raise ValueError("无法解析起飞时刻「%s」：%s" % (raw, note))
            inp.t0_text = raw
        inp.tz_hint = self._tz_value()
        inp.t0_mode = ("rendezvous" if self.t0_backsolve_var.get() else "auto")

        inp.payload_name = self.payload_var.get().strip()
        inp.payload_kg = widgets.parse_optional_float(self.payload_kg_var.get(),
                                                      "载荷质量", 0.1, 200000.0)

    def load(self, inp: MissionInput) -> None:
        self._ensure_db()
        mt = str(getattr(inp, "mission_type", "auto") or "auto")
        if mt not in ("standard", "crewed", "return"):
            if bool(getattr(inp, "return_only", False)) or str(inp.profile) == "return":
                mt = "return"
            elif str(inp.profile) == "cz2f":
                mt = "crewed"
            else:
                mt = "standard"
        self._suspend_t0_parse = True
        self.type_var.set(mt)
        self.name_var.set(inp.name or "")
        self.serial_var.set(inp.serial or "")
        self.payload_var.set(inp.payload_name or "")
        self.payload_kg_var.set(widgets.fmt_num(inp.payload_kg))
        cust = dict(getattr(inp, "custom_rocket", {}) or {})
        if cust:
            self.custom_rocket = cust
            self._custom_rocket_mode = str(getattr(inp, "custom_rocket_mode", "") or "session")
            self.rocket_var.set(str(cust.get("name") or ""))
        else:
            self.custom_rocket = None
            self._custom_rocket_mode = ""
            self.rocket_var.set(inp.rocket_label or "")
        custom_site = (inp.site_id == "custom" or inp.site_lat is not None
                       or inp.site_lon is not None) and inp.site_id != "auto"
        self.site_fmt_var.set(widgets.coord_format_label())
        if custom_site:
            self.site_var.set(CUSTOM_SITE)
            self.site_lat_var.set(widgets.format_coord(inp.site_lat, "lat"))
            self.site_lon_var.set(widgets.format_coord(inp.site_lon, "lon"))
            self.site_alt_var.set(widgets.fmt_num(inp.site_alt))
        else:
            self.site_var.set(self._center_display(inp.site_label or ""))
            self.site_lat_var.set(widgets.format_coord(inp.site_lat, "lat"))
            self.site_lon_var.set(widgets.format_coord(inp.site_lon, "lon"))
            self.site_alt_var.set(widgets.fmt_num(inp.site_alt))
        self.t0_unknown.set(not (inp.t0_text or "").strip())
        self.t0_backsolve_var.set(str(getattr(inp, "t0_mode", "auto")) == "rendezvous")
        self.t0_var.set(inp.t0_text or "")
        self.tz_var.set(self._tz_display(inp.tz_hint))
        self._on_site()
        self._on_t0_unknown()
        self._update_intro()
        self._suspend_t0_parse = False
        self._refresh_t0_preview()
        self._on_type_change()
