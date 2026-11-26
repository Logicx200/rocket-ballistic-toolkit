# -*- coding: utf-8 -*-
"""步骤 ②：轨道与时序（参考轨道自定义 / 制导参数 / 分离抛罩时序 / 箭型参数速览）。

这是 v4 新增的核心页面，把 v3 里散落在「① 任务信息」和「④ 运行选项」的
"参考轨道、箭型参数、发射时序"集中到一处，并补上 v4 的三项能力：

1. **参考轨道自定义**：支持「自动推断 / 内置模板 / 我的轨道库 / 手填六要素 / TLE 导入」，
   编辑器里能保存到「我的轨道库」，也支持 RAAN / LTAN —— 直接决定轨道平面发射窗口；
2. **制导参数覆盖**：t_v / t_p / pitch_power / 方位角 / 目标倾角等，留空即交给箭型默认与
   自动反演，填了就固定（不会被反演改）；
3. **发射时序编辑体验**：左侧显示箭型默认值、右侧填自定义值（支持 206 / T+206 / 206s /
   绝对 UTC 时刻四种写法），带时间轴预览与"与默认相差过大"的即时提醒。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import simpledialog, ttk
from typing import Any, Dict, List, Optional, Tuple

from app.core.model import REF_ORBIT_MODE_CN, MissionInput

from . import theme, widgets

MODE_ORDER = ["auto", "template", "user", "custom", "tle"]
MODE_LABELS = {
    "auto": "自动推断",
    "template": "内置轨道模板",
    "user": "我的轨道库",
    "custom": "手填参数",
    "tle": "TLE 导入",
}

#: 制导参数覆盖字段：key → (标签, 提示)
GUIDANCE_FIELDS: List[Tuple[str, str, str]] = [
    ("t_v", "推力转向 t_v (s)", "起飞后开始程序转弯的时刻；留空=箭型默认"),
    ("t_p", "俯仰程序时长 t_p (s)", "俯仰程序结束时刻；留空=箭型默认"),
    ("pitch_power", "俯仰幂次", "0.8~1.2 常见；留空=箭型默认"),
    ("azimuth_deg", "发射方位角 (°)", "留空=由落区/轨道推断，反演会精化"),
    ("i_target_deg", "末级平面修正倾角 (°)", "需要 dogleg 平面机动时填目标倾角"),
    ("yaw_tau", "偏航时间常数 (s)", "平面保持/转向的平滑时间常数"),
    ("fairing_divert_mps", "抛罩横向速度 (m/s)", "整流罩/头锥分离时的横向分离速度"),
]


class TabVehicle(ttk.Frame):
    """② 轨道与时序。"""

    def __init__(self, master, app):
        super().__init__(master, padding=(16, 12))
        self.app = app
        self._orbit_items: List[Dict[str, Any]] = []
        self._user_orbits: List[Dict[str, Any]] = []
        self._seq_rows: List[Dict[str, Any]] = []
        self._seq_rocket_key = ""
        self._tle_preview: Dict[str, Any] = {}
        self._build()

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        widgets.page_title(
            self, "轨道与时序",
            "参考轨道决定入轨目标与轨道平面发射窗口；时序决定各级分离/抛罩/末级关机时刻。"
            "两者都可以留空——留空即由箭型库默认值与自动反演负责。").grid(
            row=0, column=0, sticky="ew", pady=(0, 10))

        # ---- 参考轨道 ----
        orbit = widgets.Card(self, title="参考轨道")
        orbit.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        orbit.add_action("参考轨道编辑器…", self._open_orbit_editor)
        orbit.add_action("保存为我的轨道", self._save_orbit_to_userdb)

        mode_box = ttk.Frame(orbit.body, style="Panel.TFrame")
        mode_box.grid(row=0, column=0, sticky="ew")
        self.ref_mode_var = tk.StringVar(value="auto")
        for i, mode in enumerate(MODE_ORDER):
            ttk.Radiobutton(mode_box, text=MODE_LABELS[mode], value=mode,
                            variable=self.ref_mode_var, style="Panel.TRadiobutton",
                            command=self._on_mode_change).grid(
                row=0, column=i, sticky="w", padx=(0, 16))

        # 模板 / 我的轨道
        pick = ttk.Frame(orbit.body, style="Panel.TFrame")
        pick.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self._tpl_label = ttk.Label(pick, text="内置模板", style="Panel.TLabel")
        self._tpl_label.grid(row=0, column=0, sticky="e", padx=(0, 8))
        self.orbit_tpl_var = tk.StringVar()
        self.orbit_tpl_combo = ttk.Combobox(pick, textvariable=self.orbit_tpl_var,
                                            width=34, state="readonly")
        self.orbit_tpl_combo.grid(row=0, column=1, sticky="w")
        self.orbit_tpl_combo.bind("<<ComboboxSelected>>",
                                  lambda e: self._on_template_change())
        self._user_label = ttk.Label(pick, text="我的轨道", style="Panel.TLabel")
        self._user_label.grid(row=0, column=2, sticky="e", padx=(16, 8))
        self.orbit_user_var = tk.StringVar()
        self.orbit_user_combo = ttk.Combobox(pick, textvariable=self.orbit_user_var,
                                             width=30, state="readonly")
        self.orbit_user_combo.grid(row=0, column=3, sticky="w")
        self.orbit_user_combo.bind("<<ComboboxSelected>>",
                                   lambda e: self._on_user_orbit_change())

        # 手填参数
        self.custom_box = ttk.Frame(orbit.body, style="Panel.TFrame")
        self.custom_box.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.orbit_name_var = tk.StringVar()
        self.orbit_alt_var = tk.StringVar()
        self.orbit_i_var = tk.StringVar()
        self.orbit_e_var = tk.StringVar()
        self.orbit_raan_var = tk.StringVar()
        self.orbit_ltan_var = tk.StringVar()
        widgets.field(self.custom_box, 0, "名称",
                      lambda b: ttk.Entry(b, textvariable=self.orbit_name_var, width=30),
                      "自定义轨道名称（便于识别）")
        widgets.field(self.custom_box, 1, "高度 km",
                      lambda b: ttk.Entry(b, textvariable=self.orbit_alt_var, width=12),
                      "近圆轨道填轨道高度；椭圆（GTO）填远地点")
        widgets.field(self.custom_box, 2, "倾角 °",
                      lambda b: ttk.Entry(b, textvariable=self.orbit_i_var, width=12),
                      "留空=由发射方位角反算")
        widgets.field(self.custom_box, 3, "偏心率",
                      lambda b: ttk.Entry(b, textvariable=self.orbit_e_var, width=12),
                      "0 = 圆轨道；大椭圆填 0~0.9")
        widgets.field(self.custom_box, 4, "RAAN °",
                      lambda b: ttk.Entry(b, textvariable=self.orbit_raan_var, width=12),
                      "升交点赤经：填了就能算轨道平面发射窗口（0~360）")
        widgets.field(self.custom_box, 5, "LTAN h",
                      lambda b: ttk.Entry(b, textvariable=self.orbit_ltan_var, width=12),
                      "太阳同步轨道的地方时升交点（0~24）；与 RAAN 二选一")

        # TLE
        self.tle_box = ttk.Frame(orbit.body, style="Panel.TFrame")
        self.tle_box.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.tle_text = tk.Text(self.tle_box, height=3, width=90, wrap="none",
                                font=theme.mono(), relief="solid", borderwidth=1,
                                background=theme.FIELD_BG, padx=6, pady=4)
        self.tle_text.grid(row=0, column=0, sticky="ew")
        self.tle_box.columnconfigure(0, weight=1)
        bar = ttk.Frame(self.tle_box, style="Panel.TFrame")
        bar.grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Button(bar, text="解析 TLE 并填入", style="Small.TButton",
                   command=self._parse_tle).pack(side="left")
        self.tle_hint = widgets.hint(bar, "粘贴两行 TLE（含名称行也可以）",
                                     style="CardHint.TLabel")
        self.tle_hint.pack(side="left", padx=(10, 0))

        self.orbit_preview = widgets.hint(orbit.body, "", wraplength=900,
                                          style="CardHint.TLabel")
        self.orbit_preview.grid(row=4, column=0, sticky="w", pady=(8, 0))

        # ---- 制导参数 ----
        gd = widgets.Card(self, title="制导参数（高级；留空 = 箭型默认 + 自动反演）",
                          hint_text="这里填了就以你的值为准（反演不再改它）；留空则用箭型库默认值，"
                                    "并在「参数反演」步骤里自动优化。一般只在明确知道实测值时填写。")
        gd.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self.g_entries: Dict[str, tk.StringVar] = {}
        grid = ttk.Frame(gd.body, style="Panel.TFrame")
        grid.grid(row=0, column=0, sticky="ew")
        # 单列布局：两列时"标签+输入框+提示"很容易撑宽页面，右侧提示被裁掉
        for idx, (key, label, tip) in enumerate(GUIDANCE_FIELDS):
            cell = ttk.Frame(grid, style="Panel.TFrame")
            cell.grid(row=idx, column=0, sticky="w", pady=3)
            ttk.Label(cell, text=label, style="Panel.TLabel", width=20,
                      anchor="e").pack(side="left", padx=(0, 10))
            var = tk.StringVar()
            self.g_entries[key] = var
            ent = ttk.Entry(cell, textvariable=var, width=14)
            ent.pack(side="left")
            widgets.hint(cell, tip, wraplength=520,
                         style="CardHint.TLabel").pack(side="left", padx=(10, 0))
        ttk.Button(gd.body, text="全部清空（恢复自动）", style="Small.TButton",
                   command=self._clear_guidance).grid(row=1, column=0, sticky="w",
                                                      pady=(8, 0))

        # ---- 发射时序 ----
        seq = widgets.Card(self, title="发射时序（各级分离 / 抛罩 / 末级关机）",
                           hint_text="留空 = 该箭型默认（按推进剂耗尽或箭型库程序时刻）。"
                                     "支持 206 / T+206 / 206s / 2026-11-19T07:10:00Z 四种写法；"
                                     "末级关机时刻直接决定入轨速度，偏几十秒就可能不入轨。")
        seq.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        seq.add_action("填入箭型默认", self._fill_sequence_defaults)
        seq.add_action("全部清空", self._clear_sequence)
        top = ttk.Frame(seq.body, style="Panel.TFrame")
        top.grid(row=0, column=0, sticky="ew")
        self.seq_custom_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="自定义时序（不勾选 = 完全使用箭型库默认）",
                        variable=self.seq_custom_var, style="Panel.TCheckbutton",
                        command=self._on_seq_toggle).pack(side="left")
        ttk.Button(top, text="应用模板…", style="Small.TButton",
                   command=self._apply_seq_template).pack(side="right", padx=(8, 0))
        ttk.Button(top, text="保存为模板…", style="Small.TButton",
                   command=self._save_seq_template).pack(side="right")

        self.seq_host = ttk.Frame(seq.body, style="Panel.TFrame")
        self.seq_host.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.seq_host.columnconfigure(2, weight=1)
        self.seq_preview = widgets.hint(seq.body, "", wraplength=980,
                                        style="CardHint.TLabel")
        self.seq_preview.grid(row=2, column=0, sticky="w", pady=(8, 0))

        # ---- 当前箭型参数速览 ----
        summ = widgets.Card(self, title="当前箭型参数速览",
                            hint_text="完整参数（分级质量/推力/比冲/整流罩/残骸气动/典型轨道）"
                                      "可在「箭型参数编辑器」里改，无需手写数据库文件。")
        summ.grid(row=4, column=0, sticky="ew")
        summ.add_action("编辑箭型参数…", self._open_rocket_editor)
        container, self.stage_tree = widgets.make_tree(
            summ.body,
            [("name", "级/部件", 120, "w"), ("dry", "结构 kg", 80, "e"),
             ("prop", "推进剂 kg", 90, "e"), ("eng", "台数", 60, "center"),
             ("thrust", "真空推力 kN", 100, "e"), ("isp", "真空比冲 s", 100, "e"),
             ("pitch", "俯仰终角 °", 90, "e"), ("sep", "分离/关机 s", 110, "e")],
            height=6)
        container.grid(row=0, column=0, sticky="nsew")
        self.stage_note = widgets.hint(summ.body, "", wraplength=980,
                                       style="CardHint.TLabel")
        self.stage_note.grid(row=1, column=0, sticky="w", pady=(6, 0))

        self._on_mode_change()

    # ------------------------------------------------------------------ #
    # 内置库
    # ------------------------------------------------------------------ #
    def _ensure_db(self) -> None:
        if self._orbit_items:
            return
        try:
            from app.core import infer as I
            from app.core import userdb as U
            self._orbit_items = list(I.orbit_list())
            self._user_orbits = list(U.list_orbits())
        except Exception as exc:  # noqa: BLE001
            widgets.write_error_log("加载轨道库", exc)
            self._orbit_items, self._user_orbits = [], []
        tpl_names = [str(o.get("name")) for o in self._orbit_items if o.get("name")]
        self.orbit_tpl_combo.configure(values=tpl_names)
        user_names = [str(o.get("name")) for o in self._user_orbits if o.get("name")]
        self.orbit_user_combo.configure(values=user_names or ["（我的轨道库为空）"])

    # ------------------------------------------------------------------ #
    # 参考轨道
    # ------------------------------------------------------------------ #
    def ref_mode_cn(self) -> str:
        mode = self.ref_mode_var.get() or "auto"
        if mode == "custom":
            alt = self.orbit_alt_var.get().strip()
            inc = self.orbit_i_var.get().strip()
            extra = []
            if alt:
                extra.append("h=%s" % alt)
            if inc:
                extra.append("i=%s°" % inc)
            return "手填" + ((" " + " ".join(extra)) if extra else "")
        if mode == "template":
            return "模板：%s" % (self.orbit_tpl_var.get() or "未选")
        if mode == "user":
            return "我的轨道：%s" % (self.orbit_user_var.get() or "未选")
        if mode == "tle":
            return "TLE 导入"
        return "自动推断"

    def _on_mode_change(self) -> None:
        mode = self.ref_mode_var.get() or "auto"
        self._ensure_db()
        show_tpl = mode == "template"
        show_user = mode == "user"
        for w in (self._tpl_label, self.orbit_tpl_combo):
            w.grid() if show_tpl else w.grid_remove()
        for w in (self._user_label, self.orbit_user_combo):
            w.grid() if show_user else w.grid_remove()
        if mode == "custom":
            self.custom_box.grid()
        else:
            self.custom_box.grid_remove()
        if mode == "tle":
            self.tle_box.grid()
        else:
            self.tle_box.grid_remove()
        self._update_orbit_preview()
        self.app.refresh_nav()

    def _on_template_change(self) -> None:
        name = self.orbit_tpl_var.get().strip()
        for o in self._orbit_items:
            if str(o.get("name")) == name:
                self.orbit_alt_var.set(widgets.fmt_num(o.get("alt_km")))
                self.orbit_i_var.set(widgets.fmt_num(o.get("i_deg")))
                self.orbit_e_var.set("0" if not o.get("e") else widgets.fmt_num(o.get("e")))
                self.orbit_raan_var.set(widgets.fmt_num(o.get("raan_deg"))
                                        if o.get("raan_deg") is not None else "")
                self.orbit_ltan_var.set(widgets.fmt_num(o.get("ltan_h"))
                                        if o.get("ltan_h") is not None else "")
                break
        self._update_orbit_preview()

    def _on_user_orbit_change(self) -> None:
        name = self.orbit_user_var.get().strip()
        for o in self._user_orbits:
            if str(o.get("name")) == name:
                self.orbit_name_var.set(name)
                self.orbit_alt_var.set(widgets.fmt_num(o.get("alt_km")))
                self.orbit_i_var.set(widgets.fmt_num(o.get("i_deg")))
                self.orbit_e_var.set(widgets.fmt_num(o.get("e")))
                self.orbit_raan_var.set(widgets.fmt_num(o.get("raan_deg"))
                                        if o.get("raan_deg") is not None else "")
                self.orbit_ltan_var.set(widgets.fmt_num(o.get("ltan_h"))
                                        if o.get("ltan_h") is not None else "")
                break
        self._update_orbit_preview()

    def _update_orbit_preview(self) -> None:
        mode = self.ref_mode_var.get() or "auto"
        lines: List[str] = []
        if mode == "auto":
            lines.append("自动推断：先按箭型典型轨道取初值，再用发射方位角反算倾角校正；"
                         "反演时作为软约束。若填了 RAAN/LTAN 才会算轨道平面窗口。")
        elif mode in ("template", "user"):
            name = (self.orbit_tpl_var.get() if mode == "template"
                    else self.orbit_user_var.get())
            item = next((o for o in (self._orbit_items if mode == "template"
                                     else self._user_orbits)
                         if str(o.get("name")) == str(name)), None)
            if item:
                lines.append("轨道：%s（h=%s km，i=%s°，e=%s）" % (
                    item.get("name"), widgets.fmt_num(item.get("alt_km")),
                    widgets.fmt_num(item.get("i_deg")),
                    widgets.fmt_num(item.get("e") or 0, 4)))
                if item.get("ltan_h") is not None:
                    lines.append("LTAN 约束：%s h → 会计算太阳同步轨道平面窗口" %
                                 widgets.fmt_num(item.get("ltan_h")))
            else:
                lines.append("尚未选择轨道模板。")
        elif mode == "custom":
            bits = []
            for label, var, unit in (("高度", self.orbit_alt_var, "km"),
                                     ("倾角", self.orbit_i_var, "°"),
                                     ("偏心率", self.orbit_e_var, ""),
                                     ("RAAN", self.orbit_raan_var, "°"),
                                     ("LTAN", self.orbit_ltan_var, "h")):
                v = var.get().strip()
                if v:
                    bits.append("%s %s%s" % (label, v, unit))
            lines.append("手填参数：%s" % ("；".join(bits) if bits else "（尚未填写）"))
            if self.orbit_raan_var.get().strip() or self.orbit_ltan_var.get().strip():
                lines.append("已给平面约束：会额外计算「轨道平面发射窗口」。")
        elif mode == "tle":
            if self._tle_preview:
                lines.append("TLE 解析：h=%s km，i=%s°，e=%s（%s）" % (
                    widgets.fmt_num(self._tle_preview.get("alt_km")),
                    widgets.fmt_num(self._tle_preview.get("i_deg")),
                    widgets.fmt_num(self._tle_preview.get("e"), 5),
                    self._tle_preview.get("name") or "—"))
            else:
                lines.append("粘贴 TLE 后点「解析 TLE 并填入」。")
        self.orbit_preview.configure(text="\n".join(lines))
        self.app.refresh_summary()

    def _parse_tle(self) -> None:
        text = self.tle_text.get("1.0", "end").strip()
        if not text:
            widgets.show_info(self, "没有内容", "请先粘贴两行 TLE。")
            return
        try:
            from app.core.infer import parse_tle_elements
            orb, note = parse_tle_elements(text)
        except Exception as exc:  # noqa: BLE001
            widgets.show_error(self, "TLE 解析失败", widgets.explain_error(exc))
            return
        if not orb:
            widgets.show_error(self, "TLE 解析失败", note)
            return
        self._tle_preview = orb
        self.orbit_name_var.set(orb.get("name") or "TLE 参考轨道")
        self.orbit_alt_var.set(widgets.fmt_num(orb.get("alt_km")))
        self.orbit_i_var.set(widgets.fmt_num(orb.get("i_deg"), 4))
        self.orbit_e_var.set(widgets.fmt_num(orb.get("e"), 6))
        if orb.get("raan_deg") is not None:
            self.orbit_raan_var.set(widgets.fmt_num(orb.get("raan_deg"), 3))
        self.tle_hint.configure(text=note, style="Ok.TLabel")
        self._update_orbit_preview()
        self.app.set_status("TLE 已解析：%s" % note, "ok")

    def _orbit_values(self) -> Dict[str, Any]:
        """当前页面上的手填轨道（用于保存到用户库 / 编辑器回填）。"""
        def _f(var: tk.StringVar) -> Optional[float]:
            return widgets.parse_optional_float(var.get(), "参考轨道参数")

        return {
            "name": self.orbit_name_var.get().strip() or "我的参考轨道",
            "alt_km": _f(self.orbit_alt_var),
            "i_deg": _f(self.orbit_i_var),
            "e": _f(self.orbit_e_var) or 0.0,
            "raan_deg": _f(self.orbit_raan_var),
            "ltan_h": _f(self.orbit_ltan_var),
            "type": "CUSTOM",
        }

    def _open_orbit_editor(self) -> None:
        current = self._orbit_values()
        if self.ref_mode_var.get() in ("template", "user"):
            item = next((o for o in self._orbit_items
                         if str(o.get("name")) == self.orbit_tpl_var.get()), None)
            if item:
                current = dict(item)
        result = self.app.edit_orbit(current)
        if not result:
            return
        self._ensure_db()
        self.orbit_name_var.set(str(result.get("name") or ""))
        self.orbit_alt_var.set(widgets.fmt_num(result.get("alt_km")))
        self.orbit_i_var.set(widgets.fmt_num(result.get("i_deg")))
        self.orbit_e_var.set(widgets.fmt_num(result.get("e") or 0, 5))
        self.orbit_raan_var.set(widgets.fmt_num(result.get("raan_deg"))
                                if result.get("raan_deg") is not None else "")
        self.orbit_ltan_var.set(widgets.fmt_num(result.get("ltan_h"))
                                if result.get("ltan_h") is not None else "")
        if result.get("_user"):
            self._user_orbits = list(self._user_orbits)
            try:
                from app.core import userdb as U
                self._user_orbits = list(U.list_orbits())
            except Exception:  # noqa: BLE001
                pass
            self.orbit_user_combo.configure(
                values=[str(o.get("name")) for o in self._user_orbits] or
                ["（我的轨道库为空）"])
            self.orbit_user_var.set(str(result.get("name") or ""))
            self.ref_mode_var.set("user")
        else:
            self.ref_mode_var.set("custom")
        self._on_mode_change()
        self.app.set_status("参考轨道已更新：%s" % (result.get("name") or "—"), "ok")

    def _save_orbit_to_userdb(self) -> None:
        vals = self._orbit_values()
        if vals.get("alt_km") is None:
            widgets.show_warning(self, "无法保存", "请先填写轨道高度（km）。")
            return
        name = simpledialog.askstring("保存到我的轨道库", "轨道名称：",
                                      initialvalue=vals.get("name") or "我的参考轨道",
                                      parent=self)
        if not name:
            return
        vals["name"] = name.strip()
        try:
            from app.core import userdb as U
            saved = U.save_orbit(vals)
            self._user_orbits = list(U.list_orbits())
            self.orbit_user_combo.configure(
                values=[str(o.get("name")) for o in self._user_orbits])
            self.orbit_user_var.set(str(saved.get("name") or name))
            self.ref_mode_var.set("user")
            self._on_mode_change()
            self.app.set_status("已保存到我的轨道库：%s" % name, "ok")
        except Exception as exc:  # noqa: BLE001
            self.app._log_error("保存轨道", exc)
            widgets.show_error(self, "保存失败", widgets.explain_error(exc))

    # ------------------------------------------------------------------ #
    # 制导参数
    # ------------------------------------------------------------------ #
    def _clear_guidance(self) -> None:
        for var in self.g_entries.values():
            var.set("")
        self.app.set_status("制导参数已全部清空：将使用箭型默认值并参与自动反演。", "ok")

    # ------------------------------------------------------------------ #
    # 发射时序
    # ------------------------------------------------------------------ #
    def _rocket_record(self) -> Optional[Dict[str, Any]]:
        try:
            return self.app._pages["mission"].current_rocket_record()
        except Exception:  # noqa: BLE001
            return None

    def _seq_defaults(self, rocket: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """→ [{key, label, default}]（与 config_build._launch_sequence_defaults 同口径）"""
        rows: List[Dict[str, Any]] = []
        if not rocket:
            return rows
        veh = rocket.get("vehicle") or {}
        g0 = rocket.get("guidance_defaults") or {}
        stages = list(veh.get("stages") or [])
        boost = veh.get("boosters") or {}
        fair = veh.get("fairing") or {}
        if boost:
            rows.append({"key": "boosters_sep_s",
                         "label": "助推器分离（×%s）" % (boost.get("count") or 2),
                         "default": boost.get("jettison_met")})
        for i, st in enumerate(stages[:-1]):
            rows.append({"key": "stage_%d_sep_s" % i,
                         "label": "%s 关机/分离" % (st.get("name") or ("第%d级" % (i + 1))),
                         "default": st.get("sep_met")})
        if fair:
            rows.append({"key": "fairing_sep_s", "label": "整流罩分离",
                         "default": (fair.get("sep_met") if fair.get("sep_met") is not None
                                     else g0.get("t_fairing"))})
        if stages:
            rows.append({"key": "seco_s",
                         "label": "%s 关机（SECO）" % (stages[-1].get("name") or "末级"),
                         "default": stages[-1].get("seco_met")})
        return rows

    def refresh_from_rocket(self, force: bool = False) -> None:
        """按当前箭型重建时序行与参数速览（箭型变了就强制刷新）。"""
        rocket = self._rocket_record()
        key = str((rocket or {}).get("id") or (rocket or {}).get("name") or "")
        if not force and key == self._seq_rocket_key and self._seq_rows:
            self._refresh_stage_table(rocket)
            return
        self._seq_rocket_key = key
        # 保留已填的自定义值（按 key 匹配）
        old = {r["key"]: r["var"].get() for r in self._seq_rows}
        for w in self.seq_host.winfo_children():
            w.destroy()
        self._seq_rows = []
        defaults = self._seq_defaults(rocket)
        if not defaults:
            widgets.hint(self.seq_host, "先在「① 任务与箭型」选择火箭型号，或点上方按钮编辑一型火箭。",
                         style="CardHint.TLabel").grid(row=0, column=0, sticky="w")
            self._seq_preview_update()
            self._refresh_stage_table(rocket)
            return
        head = [("项目", 0), ("箭型默认", 1), ("自定义值（留空=默认）", 2), ("说明", 3)]
        for i, (text, col) in enumerate(head):
            ttk.Label(self.seq_host, text=text, style="CardHint.TLabel").grid(
                row=0, column=col, sticky="w", padx=(0, 12), pady=(0, 2))
        for r, row in enumerate(defaults, start=1):
            ttk.Label(self.seq_host, text=row["label"], style="Panel.TLabel").grid(
                row=r, column=0, sticky="e", padx=(0, 10), pady=2)
            dflt = row.get("default")
            ttk.Label(self.seq_host,
                      text=("%g s" % float(dflt)) if dflt is not None else "自动（耗尽）",
                      style="CardHint.TLabel").grid(row=r, column=1, sticky="w",
                                                    padx=(0, 16), pady=2)
            var = tk.StringVar(value=old.get(row["key"], ""))
            ent = ttk.Entry(self.seq_host, textvariable=var, width=14)
            ent.grid(row=r, column=2, sticky="w", pady=2)
            var.trace_add("write", lambda *a: self._seq_preview_update())
            ttk.Label(self.seq_host,
                      text=("默认 T+%g s" % float(dflt)) if dflt is not None
                      else "留空 = 推进剂耗尽时自动分离",
                      style="CardHint.TLabel").grid(row=r, column=3, sticky="w",
                                                    padx=(8, 0), pady=2)
            self._seq_rows.append(dict(row, var=var, entry=ent))
        self._on_seq_toggle()
        self._refresh_stage_table(rocket)

    def _refresh_stage_table(self, rocket: Optional[Dict[str, Any]]) -> None:
        widgets.clear_tree(self.stage_tree)
        if not rocket:
            self.stage_note.configure(text="未选择箭型。")
            return
        veh = rocket.get("vehicle") or {}
        g0 = rocket.get("guidance_defaults") or {}
        for i, st in enumerate(veh.get("stages") or []):
            thrust = None
            if st.get("phases"):
                thrust = sum((t.get("thrust_vac_n") or 0) * (t.get("n") or 1)
                             for p in st["phases"] for t in (p.get("thrusters") or []))
                isp = None
            else:
                thrust = (st.get("thrust_vac_n") or 0) * max(1, int(st.get("engines") or 1))
                isp = st.get("isp_vac_s")
            self.stage_tree.insert("", "end", values=(
                st.get("name") or ("第%d级" % (i + 1)),
                widgets.fmt_num(st.get("dry_kg"), 0),
                widgets.fmt_num(st.get("prop_kg"), 0),
                st.get("engines") or ("多机" if st.get("phases") else ""),
                widgets.fmt_num((thrust or 0) / 1000.0, 0),
                widgets.fmt_num(isp, 1) if isp else "多相",
                widgets.fmt_num(st.get("pitch_end_deg"), 1),
                widgets.fmt_num(st.get("sep_met") or st.get("seco_met")),
            ))
        boost = veh.get("boosters") or {}
        if boost:
            thr = sum((t.get("thrust_vac_n") or 0) * (t.get("n") or 1)
                      for t in (boost.get("thrusters") or []))
            self.stage_tree.insert("", "end", values=(
                "助推器×%s" % (boost.get("count") or 2),
                widgets.fmt_num(boost.get("dry_kg_each"), 0),
                widgets.fmt_num(boost.get("prop_kg_each"), 0),
                boost.get("count") or "",
                widgets.fmt_num((thr or 0) / 1000.0, 0), "—", "—",
                widgets.fmt_num(boost.get("jettison_met")),
            ))
        fair = veh.get("fairing") or {}
        if fair:
            self.stage_tree.insert("", "end", values=(
                "整流罩×%s" % (fair.get("halves") or 2),
                widgets.fmt_num(fair.get("mass_kg"), 0), "—", "—", "—", "—", "—",
                widgets.fmt_num(fair.get("sep_met") if fair.get("sep_met") is not None
                                else g0.get("t_fairing")),
            ))
        note = "箭型：%s" % (rocket.get("name") or "—")
        try:
            from app.core import userdb as U
            m = U.rocket_metrics(rocket)
            note += "；起飞质量 %s t；起飞推重比 %.2f；理论 Δv≈%s m/s" % (
                widgets.fmt_num((m.get("liftoff_mass_kg") or 0) / 1000.0, 1),
                m.get("liftoff_twr") or 0.0, widgets.fmt_num(m.get("dv_ideal_mps"), 0))
            if m.get("warnings"):
                note += "（%s）" % "；".join(m["warnings"])
        except Exception:  # noqa: BLE001
            pass
        if g0.get("pitch_schedule"):
            note += "；使用折线俯仰剖面（%d 节点，t_v/t_p 不生效）" % len(g0["pitch_schedule"])
        self.stage_note.configure(text=note)

    def has_custom_sequence(self) -> bool:
        return bool(self.seq_custom_var.get())

    def _on_seq_toggle(self) -> None:
        state = "normal" if self.seq_custom_var.get() else "disabled"
        for row in self._seq_rows:
            try:
                row["entry"].configure(state=state)
            except tk.TclError:
                pass
        self._seq_preview_update()
        self.app.refresh_nav()

    def _seq_preview_text(self) -> str:
        if not self.seq_custom_var.get():
            return "未启用自定义：完全使用箭型库默认时序。"
        if not self._seq_rows:
            return "未选择箭型：无法自定义时序。"
        try:
            from app.core.config_build import parse_seq_time
        except Exception:  # noqa: BLE001
            parse_seq_time = None
        items: List[Tuple[float, str]] = []
        problems: List[str] = []
        for row in self._seq_rows:
            txt = row["var"].get().strip()
            if not txt:
                continue
            sec = parse_seq_time(txt) if parse_seq_time else None
            if sec is None:
                problems.append("「%s」的输入「%s」无法识别" % (row["label"], txt))
                continue
            items.append((sec, row["label"]))
            dflt = row.get("default")
            if dflt is not None and abs(sec - float(dflt)) > 45.0:
                problems.append("「%s」比箭型默认 %+.0f s" % (row["label"], sec - float(dflt)))
        items.sort()
        lines = []
        if items:
            lines.append("时间轴：" + "  →  ".join("T+%.0f %s" % (t, lbl) for t, lbl in items))
            gaps = [items[i + 1][0] - items[i][0] for i in range(len(items) - 1)]
            if gaps:
                lines.append("相邻间隔：" + "，".join("%.0f s" % g for g in gaps))
        else:
            lines.append("尚未填写任何自定义时刻（全部使用默认）。")
        if problems:
            lines.append("⚠ " + "；".join(problems[:4]) +
                         ("；…" if len(problems) > 4 else ""))
        return "\n".join(lines)

    def _seq_preview_update(self) -> None:
        text = self._seq_preview_text()
        style = "Warn.TLabel" if ("⚠" in text) else "CardHint.TLabel"
        self.seq_preview.configure(text=text, style=style)

    def _fill_sequence_defaults(self) -> None:
        for row in self._seq_rows:
            d = row.get("default")
            row["var"].set("" if d is None else "%g" % float(d))
        self.seq_custom_var.set(True)
        self._on_seq_toggle()
        self.app.set_status("已把箭型默认时刻填入自定义列（可继续微调）。", "ok")

    def _clear_sequence(self) -> None:
        for row in self._seq_rows:
            row["var"].set("")
        self._on_seq_toggle()

    def _apply_seq_template(self) -> None:
        try:
            from app.core import userdb as U
            items = list(U.list_sequences())
        except Exception as exc:  # noqa: BLE001
            widgets.show_error(self, "读取模板失败", widgets.explain_error(exc))
            return
        if not items:
            widgets.show_info(self, "没有模板", "还没有保存过时序模板：先填好自定义时刻，"
                                                 "再点「保存为模板…」。")
            return
        names = [str(t.get("name")) for t in items]
        name = simpledialog.askstring("应用时序模板", "模板名称（可用：%s）" % "、".join(names[:6]),
                                      initialvalue=names[0], parent=self)
        if not name:
            return
        tpl = next((t for t in items if str(t.get("name")) == str(name).strip()), None)
        if not tpl:
            widgets.show_warning(self, "未找到模板", "没有名为「%s」的模板。" % name)
            return
        vals = dict(tpl.get("values") or {})
        for row in self._seq_rows:
            key = row["key"]
            if key.startswith("stage_"):
                idx = int(key.split("_")[1])
                seq = vals.get("stage_sep_s") or []
                v = seq[idx] if idx < len(seq) else None
            else:
                v = vals.get(key)
            row["var"].set("" if v is None else "%g" % float(v))
        self.seq_custom_var.set(True)
        self._on_seq_toggle()
        self.app.set_status("已应用时序模板：%s" % tpl.get("name"), "ok")

    def _save_seq_template(self) -> None:
        values: Dict[str, Any] = {}
        stages: List[Optional[float]] = []
        for row in self._seq_rows:
            try:
                from app.core.config_build import parse_seq_time
                sec = parse_seq_time(row["var"].get()) if row["var"].get().strip() else None
            except Exception:  # noqa: BLE001
                sec = None
            if row["key"].startswith("stage_"):
                idx = int(row["key"].split("_")[1])
                while len(stages) <= idx:
                    stages.append(None)
                stages[idx] = sec
            else:
                values[row["key"]] = sec
        values["stage_sep_s"] = stages
        rocket = self._rocket_record() or {}
        name = simpledialog.askstring("保存时序模板", "模板名称：",
                                      initialvalue="%s 时序" % (rocket.get("name") or "自定义"),
                                      parent=self)
        if not name:
            return
        try:
            from app.core import userdb as U
            U.save_sequence({"name": name.strip(),
                             "rocket_id": str(rocket.get("id") or ""),
                             "values": values})
            self.app.set_status("时序模板已保存：%s" % name, "ok")
        except Exception as exc:  # noqa: BLE001
            self.app._log_error("保存时序模板", exc)
            widgets.show_error(self, "保存失败", widgets.explain_error(exc))

    def _open_rocket_editor(self) -> None:
        self.app._pages["mission"]._open_rocket_editor()
        self.refresh_from_rocket(force=True)

    # ------------------------------------------------------------------ #
    # 页面生命周期
    # ------------------------------------------------------------------ #
    def on_show(self) -> None:
        self._ensure_db()
        self.refresh_from_rocket()

    def collect(self, inp: MissionInput) -> None:
        mode = self.ref_mode_var.get() or "auto"
        inp.ref_orbit_mode = mode
        inp.ref_orbit_name = ""
        inp.ref_orbit = {}
        inp.ref_orbit_tle = ""
        inp.orbit_alt_km = None
        inp.orbit_i_deg = None
        if mode == "template":
            inp.ref_orbit_name = self.orbit_tpl_var.get().strip()
            if not inp.ref_orbit_name:
                raise ValueError("已选择「内置轨道模板」，请先在下拉框里选一个模板。")
        elif mode == "user":
            inp.ref_orbit_name = self.orbit_user_var.get().strip()
            if not inp.ref_orbit_name or inp.ref_orbit_name.startswith("（"):
                raise ValueError("已选择「我的轨道库」，但我的轨道库还是空的："
                                 "请到「参考轨道编辑器」里新建并保存一条。")
        elif mode == "custom":
            vals = self._orbit_values()
            if vals.get("alt_km") is None and vals.get("i_deg") is None:
                raise ValueError("已选择「手填参数」，请至少填写轨道高度或倾角。")
            inp.ref_orbit = {k: v for k, v in vals.items() if v is not None}
            inp.orbit_alt_km = vals.get("alt_km")
            inp.orbit_i_deg = vals.get("i_deg")
        elif mode == "tle":
            txt = self.tle_text.get("1.0", "end").strip()
            if not txt:
                raise ValueError("已选择「TLE 导入」，请粘贴两行 TLE 或改用「自动推断」。")
            inp.ref_orbit_tle = txt

        # 制导覆盖
        out: Dict[str, Any] = {}
        for key, var in self.g_entries.items():
            txt = var.get().strip()
            if not txt:
                continue
            v = widgets.parse_optional_float(txt, "制导参数 %s" % key)
            if v is not None:
                out[key] = v
        inp.guidance_overrides = out

        # 发射时序
        seq: Dict[str, Any] = {"custom": bool(self.seq_custom_var.get())}
        stage_vals: List[Optional[str]] = []
        for row in self._seq_rows:
            txt = row["var"].get().strip()
            if row["key"].startswith("stage_"):
                idx = int(row["key"].split("_")[1])
                while len(stage_vals) <= idx:
                    stage_vals.append(None)
                stage_vals[idx] = txt or None
            else:
                seq[row["key"]] = txt or None
        if stage_vals:
            seq["stage_sep_s"] = stage_vals
        inp.launch_sequence = seq

    def load(self, inp: MissionInput) -> None:
        self._ensure_db()
        mode = str(getattr(inp, "ref_orbit_mode", "auto") or "auto")
        if mode == "auto" and (inp.orbit_alt_km is not None or inp.orbit_i_deg is not None):
            mode = "custom"
        self.ref_mode_var.set(mode if mode in MODE_ORDER else "auto")
        self.orbit_name_var.set("")
        self.orbit_alt_var.set(widgets.fmt_num(inp.orbit_alt_km))
        self.orbit_i_var.set(widgets.fmt_num(inp.orbit_i_deg))
        self.orbit_e_var.set("")
        self.orbit_raan_var.set("")
        self.orbit_ltan_var.set("")
        ref = dict(getattr(inp, "ref_orbit", {}) or {})
        if ref:
            self.orbit_name_var.set(str(ref.get("name") or ""))
            self.orbit_alt_var.set(widgets.fmt_num(ref.get("alt_km")))
            self.orbit_i_var.set(widgets.fmt_num(ref.get("i_deg")))
            self.orbit_e_var.set(widgets.fmt_num(ref.get("e") or 0, 5))
            self.orbit_raan_var.set(widgets.fmt_num(ref.get("raan_deg"))
                                    if ref.get("raan_deg") is not None else "")
            self.orbit_ltan_var.set(widgets.fmt_num(ref.get("ltan_h"))
                                    if ref.get("ltan_h") is not None else "")
        name = str(getattr(inp, "ref_orbit_name", "") or "")
        if mode == "template":
            self.orbit_tpl_var.set(name)
        elif mode == "user":
            self.orbit_user_var.set(name)
        self.tle_text.delete("1.0", "end")
        self.tle_text.insert("1.0", str(getattr(inp, "ref_orbit_tle", "") or ""))
        if str(getattr(inp, "ref_orbit_tle", "") or "").strip():
            self._parse_tle()
        self._on_mode_change()

        for key, var in self.g_entries.items():
            v = (getattr(inp, "guidance_overrides", {}) or {}).get(key)
            var.set("" if v is None else "%g" % float(v))

        self.refresh_from_rocket(force=True)
        seq = dict(getattr(inp, "launch_sequence", {}) or {})
        vals = seq.get("values") if isinstance(seq.get("values"), dict) else seq
        self.seq_custom_var.set(bool(seq.get("custom")))
        stage_vals = (vals or {}).get("stage_sep_s") or []
        for row in self._seq_rows:
            key = row["key"]
            if key.startswith("stage_"):
                idx = int(key.split("_")[1])
                v = stage_vals[idx] if idx < len(stage_vals) else None
            else:
                v = (vals or {}).get(key)
            row["var"].set("" if v is None else "%g" % float(v))
        self._on_seq_toggle()
