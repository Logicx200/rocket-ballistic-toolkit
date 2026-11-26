# -*- coding: utf-8 -*-
"""步骤 ⑥：输出与模块（模块分组 / 反演预算 / 蒙特卡洛 / 离网高程 / 预计成果）。

v3 的 14 个复选框平铺成两列，用户看不出"哪些是必需的、关掉会少什么"。v4 改为：

* **按用途分组**（核心流程 / 轨道与窗口 / 观测与图件 / 三维与报告 / 不确定度），
  每组带一句"关掉会少什么"；
* **四种预设**：快速演示、标准（推荐）、全量、只要仿真；
* **实时"预计产出"**：按当前勾选与任务类型列出会生成的成果，以及"缺了哪一类"。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Dict, List, Tuple

from app.core.model import DEFAULT_MODULES, MissionInput

from . import theme, widgets

#: 模块分组：key → (分组名, 说明)
MODULE_GROUPS: List[Tuple[str, List[Tuple[str, str, str]]]] = [
    ("核心流程", [
        ("export", "仿真与数据契约", "必选：产出 sim/ 下全部弹道数据"),
        ("invert", "参数反演", "按落区/参考轨道自动求解制导参数（约 30~60 s）"),
        ("dem", "DEM 地形准备", "天空轨迹的地形遮挡；离线时用本地瓦片/粗 DEM"),
    ]),
    ("轨道与窗口", [
        ("window", "发射窗口", "航警时窗 + 轨道平面/交会窗口"),
        ("station", "空间站轨道准备", "载人/交会任务用；其他任务自动跳过"),
        ("docking", "入轨+交会对接方案", "载人/交会任务用；其他任务自动跳过"),
        ("return", "返回舱返回", "需要空间站轨道 + 着陆场/返回航警"),
    ]),
    ("观测与图件", [
        ("sky", "天空轨迹", "观测点看到的轨迹、方位、地形遮挡"),
        ("appear", "出现方位地形图", "从观测点看火箭出现方向的实景判读"),
        ("map", "全国最大高度角地图", "全国范围可见性（不依赖观测点）"),
    ]),
    ("三维与报告", [
        ("kml", "三维 KMZ/KML", "Google Earth 三维弹道与残骸坠落"),
        ("summary", "通用成果汇总", "任务摘要/关键节点/飞行阶段/成果清单"),
        ("report", "中文技术报告", "自动生成 .md + .docx"),
        ("qc", "交付自检", "逐项核对成果完整性并输出 qc_report.json"),
    ]),
    ("不确定度", [
        ("mc", "蒙特卡洛不确定度", "抽样扰动后的落点散布（样本数在下方设置）"),
    ]),
]

BUDGETS = [
    ("auto", "自动（按箭型置信度与参数个数选预算，推荐）"),
    ("fast", "快速（maxiter=6 / popsize=5，约 30 次仿真，先验证流程）"),
    ("fine", "精细（maxiter=20 / popsize=10，约 200 次仿真，耗时更长）"),
]

PRESETS = [
    ("quick", "快速演示", "只跑 仿真 + 通用成果 + 报告（约 10~20 s，适合先验证环境）"),
    ("standard", "标准（推荐）", "全部常规步骤，反演预算自动（首次跑一个任务约 1~3 min）"),
    ("full", "全量", "含天空轨迹/全国地图/三维/蒙特卡洛，耗时最长"),
    ("sim_only", "只要仿真", "只产出 sim/ 数据与任务摘要（最快，用于批量试算）"),
]


class TabOptions(ttk.Frame):
    """⑥ 输出与模块。"""

    def __init__(self, master, app):
        super().__init__(master, padding=(16, 12))
        self.app = app
        self.mod_vars: Dict[str, tk.BooleanVar] = {}
        self._build()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        widgets.page_title(
            self, "输出与模块",
            "勾选要执行的步骤。程序的设计原则是「能算的都算」：只在依赖确实不满足时才会跳过，"
            "并在运行结束的步骤台账里写明原因（不会静默少跑）。").grid(
            row=0, column=0, sticky="ew", pady=(0, 10))

        # ---- 模块分组 ----
        mod_card = widgets.Card(self, title="计算模块")
        mod_card.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        grid = ttk.Frame(mod_card.body, style="Panel.TFrame")
        grid.grid(row=0, column=0, sticky="ew")
        for i in range(2):
            grid.columnconfigure(i, weight=1, uniform="mod")
        r = 0
        self._group_children: List[int] = []
        for gname, items in MODULE_GROUPS:
            ttk.Label(grid, text=gname, style="Section.TLabel").grid(
                row=r, column=0, columnspan=2, sticky="w", pady=(8, 2))
            r += 1
            for j, (key, label, desc) in enumerate(items):
                col = j % 2
                var = tk.BooleanVar(value=bool(DEFAULT_MODULES.get(key, False)))
                self.mod_vars[key] = var
                cell = ttk.Frame(grid, style="Panel.TFrame")
                cell.grid(row=r + j // 2, column=col, sticky="w", padx=(4, 10), pady=1)
                cb = ttk.Checkbutton(cell, text=label, variable=var,
                                     style="Panel.TCheckbutton",
                                     command=self._on_module_change)
                cb.pack(side="left")
                widgets.hint(cell, desc, style="CardHint.TLabel").pack(
                    side="left", padx=(10, 0))
                if key == "export":
                    cb.configure(state="disabled")
                    var.set(True)
            r += (len(items) + 1) // 2
        # 预设
        preset_box = ttk.Frame(mod_card.body, style="Panel.TFrame")
        preset_box.grid(row=1, column=0, sticky="w", pady=(10, 0))
        for key, label, desc in PRESETS:
            ttk.Button(preset_box, text=label, style="Small.TButton",
                       command=lambda k=key: self._apply_preset(k)).pack(side="left",
                                                                        padx=(0, 6))
            _ = desc
        self.mod_count_label = ttk.Label(preset_box, text="", style="CardHint.TLabel")
        self.mod_count_label.pack(side="left", padx=(12, 0))
        self.extra_hint = widgets.hint(mod_card.body, "", wraplength=980,
                                       style="CardHint.TLabel")
        self.extra_hint.grid(row=2, column=0, sticky="w", pady=(8, 0))

        # ---- 数值口径 ----
        num = widgets.Card(self, title="数值口径与预算")
        num.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        nb = ttk.Frame(num.body, style="Panel.TFrame")
        nb.grid(row=0, column=0, sticky="ew")
        # 竖排：这些说明文字较长，横排会把页面撑出横向滚动条
        self.mode_var = tk.StringVar(value="accurate")
        ttk.Label(nb, text="数值口径", style="Panel.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 2))
        mode_box = ttk.Frame(nb, style="Panel.TFrame")
        mode_box.grid(row=1, column=0, sticky="w", padx=(16, 0))
        ttk.Radiobutton(mode_box,
                        text="accurate（v4 默认：事件对齐 + 质量随 RK4 积分，推荐）",
                        value="accurate", variable=self.mode_var,
                        style="Panel.TRadiobutton").pack(anchor="w")
        ttk.Radiobutton(mode_box, text="legacy（复现 v1 结果，仅用于对比）",
                        value="legacy", variable=self.mode_var,
                        style="Panel.TRadiobutton").pack(anchor="w")

        ttk.Label(nb, text="反演预算", style="Panel.TLabel").grid(
            row=2, column=0, sticky="w", pady=(10, 2))
        budget_box = ttk.Frame(nb, style="Panel.TFrame")
        budget_box.grid(row=3, column=0, sticky="w", padx=(16, 0))
        self.budget_var = tk.StringVar(value="auto")
        for key, label in BUDGETS:
            ttk.Radiobutton(budget_box, text=label, value=key,
                            variable=self.budget_var,
                            style="Panel.TRadiobutton").pack(anchor="w")

        row2 = ttk.Frame(nb, style="Panel.TFrame")
        row2.grid(row=4, column=0, sticky="w", pady=(10, 0))
        ttk.Label(row2, text="蒙特卡洛样本", style="Panel.TLabel",
                  width=14, anchor="e").pack(side="left", padx=(0, 10))
        self.mc_var = tk.StringVar(value="0")
        self._mc_spin = ttk.Spinbox(row2, from_=0, to=100000, increment=50, width=9,
                                    textvariable=self.mc_var)
        self._mc_spin.pack(side="left")
        widgets.hint(row2, "0 = 不跑；勾选「蒙特卡洛」时建议 100~500",
                     style="CardHint.TLabel").pack(side="left", padx=(10, 0))
        ttk.Label(row2, text="并行进程", style="Panel.TLabel").pack(side="left",
                                                               padx=(24, 8))
        self.jobs_var = tk.StringVar(value="0")
        ttk.Spinbox(row2, from_=0, to=32, increment=1, width=6,
                    textvariable=self.jobs_var).pack(side="left")
        widgets.hint(row2, "0/1 = 串行（实测更快）", style="CardHint.TLabel").pack(
            side="left", padx=(10, 0))

        self.net_var = tk.BooleanVar(value=False)
        net = ttk.Frame(nb, style="Panel.TFrame")
        net.grid(row=5, column=0, sticky="w", pady=(8, 0))
        ttk.Checkbutton(net, text="允许联网补全（DEM 瓦片 / 空间站轨道）",
                        variable=self.net_var, style="Panel.TCheckbutton").pack(
            side="left", padx=(14, 0))
        widgets.hint(net, "默认关闭：离线时不会等待网络超时，自动使用本地瓦片或内嵌粗 DEM",
                     style="CardHint.TLabel").pack(side="left", padx=(10, 0))

        # ---- 离线高程 ----
        dem = widgets.Card(self, title="离线高程数据（弱网/内网环境）",
                           hint_text="可导入自带的 GeoTIFF（WGS84，带 .tfw 或内嵌地理参照）"
                                     "或 SRTM HGT/HGT.GZ/ZIP；导入后计算时优先使用它。")
        dem.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        ttk.Button(dem.body, text="导入高程文件（GeoTIFF / HGT）…",
                   command=self.app.import_dem_files).grid(row=0, column=0, sticky="w")
        self.dem_status_var = tk.StringVar(
            value="未导入自定义高程；可导入已有 GeoTIFF/HGT，计算时优先于在线 SRTM。")
        tk.Label(dem.body, textvariable=self.dem_status_var, justify="left", anchor="w",
                 wraplength=960, fg="#1f4e79", background=theme.PANEL_BG).grid(
            row=1, column=0, sticky="ew", pady=(6, 0))

        # ---- 预计产出 ----
        expect = widgets.Card(self, title="预计产出（按当前勾选与任务类型）")
        expect.grid(row=4, column=0, sticky="ew")
        self.expect_box = widgets.TextBox(expect.body, height=8, wrap="word",
                                          readonly=True)
        self.expect_box.grid(row=0, column=0, sticky="ew")
        self._refresh_states()

    # ------------------------------------------------------------------ #
    def set_dem_status(self, text: str) -> None:
        try:
            self.dem_status_var.set(str(text or ""))
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ #
    def _on_module_change(self) -> None:
        self._refresh_states()

    def _refresh_states(self) -> None:
        enabled = bool(self.mod_vars.get("mc") and self.mod_vars["mc"].get())
        try:
            self._mc_spin.configure(state="normal" if enabled else "disabled")
        except tk.TclError:
            pass
        on = [k for k, v in self.mod_vars.items() if v.get()]
        self.mod_count_label.configure(text="已选 %d / %d 个模块" % (len(on),
                                                                    len(self.mod_vars)))
        self.extra_hint.configure(text=self._expect_hint())
        self.expect_box.set_text(self._expect_text())
        if hasattr(self.app, "refresh_nav"):
            self.app.refresh_nav()

    def _mission_type(self) -> str:
        try:
            return self.app._pages["mission"].type_var.get() or "standard"
        except Exception:  # noqa: BLE001
            return "standard"

    def _expect_hint(self) -> str:
        notes = []
        if not self.mod_vars.get("invert", tk.BooleanVar()).get():
            notes.append("关闭「参数反演」时，落点/入轨可能偏离航警与参考轨道（结果只按箭型默认值算）")
        if not (self.mod_vars.get("sky") and self.mod_vars["sky"].get()):
            notes.append("关闭「天空轨迹」将没有观测点视角的轨迹图与首次出现时刻")
        if not (self.mod_vars.get("map") and self.mod_vars["map"].get()):
            notes.append("关闭「全国地图」将没有全国最大高度角与晨昏线图")
        if self._mission_type() == "return":
            notes = ["返回任务只需「空间站轨道准备 + 返回舱返回 + 通用成果 + 报告 + 自检」；"
                     "上升段相关模块会自动记为「不适用」"]
        if not notes:
            notes.append("当前勾选覆盖了全部常规成果；预计完成时间 1~3 分钟（视反演预算）")
        return "；".join(notes)

    def _expect_text(self) -> str:
        mtype = self._mission_type()
        lines = []
        groups: Dict[str, List[str]] = {}
        order = {"核心流程": ["sim 弹道/事件/落点/入轨"],
                 "轨道与窗口": [], "观测与图件": [], "三维与报告": [], "不确定度": []}
        for gname, items in MODULE_GROUPS:
            for key, label, _d in items:
                if not self.mod_vars.get(key) or not self.mod_vars[key].get():
                    continue
                mapping = {
                    "export": ["sim/ 弹道时间序列、事件表、落点与射程、入轨根数、首圈星下点"],
                    "invert": ["反演参数.json（并回填 config）"],
                    "dem": ["DEM 瓦片/自定义高程准备（决定天空轨迹精度）"],
                    "window": ["report/发射窗口计算.json（航警时窗 + 轨道平面/交会窗口）"],
                    "station": ["sim/空间站轨道.json、空间站星下点.csv"],
                    "docking": ["sim/交会对接.json、交会对接_时序.csv"],
                    "return": ["sim/返回舱返回.json、返回舱_时序.csv、落区图、返回舱 KML"],
                    "sky": ["sky/ 天空轨迹图（全景/主图/太阳地影/地形示意）+ sky_summary.json"],
                    "appear": ["sky/出现方位地形示意图"],
                    "map": ["map/ 全国最大高度角地图、晨昏线与阳光可见性"],
                    "kml": ["viz/ 三维弹道 KMZ/KML"],
                    "summary": ["report/任务摘要.md、成果清单.md、sim/关键节点.csv 等"],
                    "report": ["report/弹道仿真报告.md/.docx"],
                    "qc": ["report/qc_report.json（逐项自检）"],
                    "mc": ["uncertainty/ 蒙特卡洛样本与统计"],
                }.get(key, [label])
                groups.setdefault(gname, []).extend(mapping)
        for gname in order:
            if groups.get(gname):
                lines.append("【%s】" % gname)
                for item in groups[gname]:
                    lines.append("  · " + item)
        lines.append("")
        if mtype == "return":
            lines.append("任务类型=返回舱返回：不产出上升段相关成果（步骤台账会标「不适用」）。")
        else:
            lines.append("通用成果（关键节点/飞行阶段/环境剖面/轨迹统计/摘要/清单）"
                         "在所有任务类型下都会尝试生成。")
        return "\n".join(lines)

    def _apply_preset(self, key: str) -> None:
        core = {"export": True, "summary": True, "report": True, "qc": True}
        off = {"dem": False, "invert": False, "window": False, "station": False,
               "docking": False, "return": False, "sky": False, "appear": False,
               "map": False, "kml": False, "mc": False}
        if key == "quick":
            base = dict(core, **off)
            self.budget_var.set("fast")
        elif key == "sim_only":
            base = dict(core, **off)
            base["invert"] = True
            self.budget_var.set("fast")
        elif key == "full":
            base = {k: True for k in self.mod_vars}
            base["mc"] = True
            self.budget_var.set("fine")
            if not self.mc_var.get().strip() or self.mc_var.get().strip() == "0":
                self.mc_var.set("300")
        else:  # standard
            base = dict(DEFAULT_MODULES)
            base["mc"] = False
            self.budget_var.set("auto")
        for k, var in self.mod_vars.items():
            var.set(bool(base.get(k, DEFAULT_MODULES.get(k, False))))
        self.mod_vars["export"].set(True)
        self._refresh_states()
        label = {p[0]: p[1] for p in PRESETS}.get(key, key)
        self.app.set_status("已应用预设：%s" % label, "ok")

    # ------------------------------------------------------------------ #
    def allow_network(self) -> bool:
        return bool(self.net_var.get())

    def enabled_count(self) -> Tuple[int, int]:
        return (len([k for k, v in self.mod_vars.items() if v.get()]), len(self.mod_vars))

    def on_show(self) -> None:
        self._refresh_states()

    def collect(self, inp: MissionInput) -> None:
        modules = {key: bool(var.get()) for key, var in self.mod_vars.items()}
        modules["export"] = True
        inp.modules = modules
        inp.mc_n = widgets.parse_int(self.mc_var.get(), "蒙特卡洛样本数", 0, 0, 1000000)
        inp.jobs = widgets.parse_int(self.jobs_var.get(), "并行进程数", 0, 0, 32)
        if modules.get("mc") and inp.mc_n <= 0:
            raise ValueError("已勾选「蒙特卡洛」模块，请填写样本数（建议 100 ~ 500；"
                             "填 0 表示不跑，可取消勾选）。")
        inp.mode = "legacy" if self.mode_var.get() == "legacy" else "accurate"
        budget = self.budget_var.get()
        if budget == "fast":
            inp.inversion_maxiter, inp.inversion_popsize = 6, 5
        elif budget == "fine":
            inp.inversion_maxiter, inp.inversion_popsize = 20, 10
        else:
            inp.inversion_maxiter, inp.inversion_popsize = 0, 0

    def load(self, inp: MissionInput) -> None:
        for key, var in self.mod_vars.items():
            var.set(bool((inp.modules or {}).get(key, DEFAULT_MODULES.get(key, False))))
        self.mod_vars["export"].set(True)
        self.mc_var.set(str(int(inp.mc_n or 0)))
        self.jobs_var.set(str(int(getattr(inp, "jobs", 0) or 0)))
        self.mode_var.set("legacy" if inp.mode == "legacy" else "accurate")
        if (inp.inversion_maxiter, inp.inversion_popsize) == (6, 5):
            self.budget_var.set("fast")
        elif (inp.inversion_maxiter, inp.inversion_popsize) == (20, 10):
            self.budget_var.set("fine")
        else:
            self.budget_var.set("auto")
        self._refresh_states()
