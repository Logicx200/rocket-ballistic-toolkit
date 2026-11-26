# -*- coding: utf-8 -*-
"""主窗口（v4 重构）：顶部工具条 + 左侧步骤导航 + 右侧步骤页 + 底部状态栏。

与 v3 的差异
------------
* **不再用 Notebook 平铺 7 页**：改成左侧向导（见 :mod:`app.gui.nav`），每步带状态徽标，
  第一次使用的人按 1→8 顺序填即可，老手可用 Ctrl+1..8 直达；
* **常驻任务概要条**：随时看到"任务类型 / 箭型 / 工位 / T0 / 输出目录"，避免改错页；
* **步骤台账可见**：运行页直接展示每一步"完成/跳过/不适用/失败 + 原因"；
* 启动仍只导入 tkinter 与轻量模块；``config_build`` / ``runner`` / ``dem_manager``（numpy）
  延迟到首次"生成体检 / 开始计算 / 环境自检"时在后台线程导入。
"""
from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any, Dict, List, Optional, Tuple

from app.core import paths as P
from app.core.model import MissionInput

from . import nav, theme, widgets
from .actions import MainActionsMixin
from .tab_health import TabHealth
from .tab_mission import TabMission
from .tab_notam import TabNotam
from .tab_observer import TabObserver
from .tab_options import TabOptions
from .tab_run import TabRun
from .tab_station import TabStation
from .tab_vehicle import TabVehicle

TITLE = "火箭弹道工具箱 v%s · 离线版" % P.app_version()
SUBTITLE = ("常规发射 / 载人交会 / 返回舱返回 三类任务的 3-DOF 弹道、落区、窗口、"
            "天空可见性与成果报告 —— 全程离线，不联网也能跑")

#: 步骤定义（顺序即向导顺序）
STEPS: List[Dict[str, str]] = [
    {"key": "mission", "title": "任务与箭型", "subtitle": "任务类型 · 火箭 · 工位 · T0 · 载荷"},
    {"key": "vehicle", "title": "轨道与时序", "subtitle": "参考轨道 · 制导 · 分离/抛罩时序"},
    {"key": "notam", "title": "航警与落区", "subtitle": "粘贴 NOTAM；无航警可预测落区"},
    {"key": "observer", "title": "观测点", "subtitle": "天空轨迹与可见性分析的观测位置"},
    {"key": "station", "title": "空间站与返回", "subtitle": "载人交会与返回舱返回（可选）"},
    {"key": "options", "title": "输出与模块", "subtitle": "跑哪些步骤 · 反演预算 · 离线高程"},
    {"key": "health", "title": "数据体检", "subtitle": "补全结果、来源与置信度"},
    {"key": "run", "title": "运行与成果", "subtitle": "执行进度 · 步骤台账 · 成果浏览"},
]


def enable_dpi_awareness() -> None:
    """Windows 高分屏：让 Tk 使用物理像素，避免界面模糊/被系统拉伸。"""
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            try:
                ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass


class MainWindow(MainActionsMixin, tk.Tk):
    """火箭弹道工具箱主窗口。"""

    POLL_MS = 80

    def __init__(self):
        super().__init__()
        self._closing = False
        self.q: "queue.Queue[Tuple[str, Dict[str, Any]]]" = queue.Queue()
        self.running = False
        self.busy = False
        self.stop_event: Optional[threading.Event] = None
        self.last_workdir = ""
        self.last_result: Any = None
        self._last_health: Any = None
        self._pages: Dict[str, Any] = {}
        self._page_frames: Dict[str, Any] = {}
        self._current = "mission"
        self._extra = {"notes": "", "jobs": 0, "dem_radius_km": 200.0}

        self.title(TITLE)
        theme.apply_theme(self)
        self._apply_icon()
        self._build_menu()
        self._build_header()
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()
        self._bind_shortcuts()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.report_callback_exception = self._on_tk_error
        self._restore_geometry()
        self.minsize(1100, 720)
        self._poll_after: Optional[str] = None
        self._deferred_after: Optional[str] = None
        self._poll_after = self.after(self.POLL_MS, self._poll)
        self._deferred_after = self.after(40, self._deferred_init)

    # ------------------------------------------------------------------ #
    # 外观
    # ------------------------------------------------------------------ #
    def _apply_icon(self) -> None:
        icon = Path(P.ASSETS) / "app.ico"
        if icon.is_file():
            try:
                self.iconbitmap(default=str(icon))
            except tk.TclError:
                pass

    def _restore_geometry(self) -> None:
        s = P.settings() or {}
        geo = str(s.get("window_geometry") or "")
        try:
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        except tk.TclError:
            sw, sh = 1920, 1080
        if geo:
            try:
                self.geometry(geo)
                return
            except tk.TclError:
                pass
        w = min(1320, max(1100, sw - 120))
        h = min(900, max(720, sh - 140))
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 3)
        self.geometry("%dx%d+%d+%d" % (w, h, x, y))

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="新建任务", accelerator="Ctrl+N", command=self.new_mission)
        file_menu.add_command(label="载入示例（长征二号丁）", command=self.load_demo)
        file_menu.add_separator()
        file_menu.add_command(label="打开任务 JSON…", accelerator="Ctrl+O",
                              command=self.open_mission)
        file_menu.add_command(label="保存任务 JSON…", accelerator="Ctrl+S",
                              command=self.save_mission)
        file_menu.add_separator()
        file_menu.add_command(label="打开工作区", command=self.open_workspace)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self._on_close)
        menubar.add_cascade(label="文件", menu=file_menu)

        edit_menu = tk.Menu(menubar, tearoff=0)
        edit_menu.add_command(label="箭型参数编辑器…", command=lambda: self.edit_rocket())
        edit_menu.add_command(label="参考轨道编辑器…", command=lambda: self.edit_orbit())
        edit_menu.add_separator()
        edit_menu.add_command(label="我的箭型库…", command=self.manage_user_rockets)
        edit_menu.add_command(label="我的轨道库…", command=self.manage_user_orbits)
        edit_menu.add_separator()
        edit_menu.add_command(label="导出我的数据包…", command=self.export_user_bundle)
        edit_menu.add_command(label="导入我的数据包…", command=self.import_user_bundle)
        menubar.add_cascade(label="数据", menu=edit_menu)

        set_menu = tk.Menu(menubar, tearoff=0)
        set_menu.add_command(label="工作区根目录…", command=self.set_work_root)
        set_menu.add_command(label="DEM 瓦片目录…", command=self.set_dem_dir)
        set_menu.add_command(label="导入离线高程文件…", command=self.import_dem_files)
        set_menu.add_separator()
        set_menu.add_command(label="清空 DEM 缓存…", command=self.clear_dem_cache)
        set_menu.add_command(label="环境自检", command=self.check_env)
        menubar.add_cascade(label="设置", menu=set_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="帮助中心", accelerator="F1", command=self.show_help)
        help_menu.add_command(label="打开文档目录", command=self.open_docs)
        help_menu.add_separator()
        help_menu.add_command(label="关于", command=self.show_about)
        menubar.add_cascade(label="帮助", menu=help_menu)
        self.configure(menu=menubar)

    def _build_header(self) -> None:
        head = tk.Frame(self, background=theme.PANEL_BG, height=64)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(1, weight=1)
        left = tk.Frame(head, background=theme.PANEL_BG)
        left.grid(row=0, column=0, sticky="w", padx=(16, 12), pady=(10, 8))
        tk.Label(left, text="火箭弹道工具箱", background=theme.PANEL_BG,
                 foreground=theme.ACCENT,
                 font=theme.font(theme.TITLE_SIZE + 2, True)).pack(side="left")
        self.version_badge = tk.Label(left, text="v%s" % P.app_version(),
                                      background=theme.ACCENT_LIGHT,
                                      foreground=theme.ACCENT,
                                      font=theme.font(theme.SMALL_SIZE, True),
                                      padx=8, pady=1)
        self.version_badge.pack(side="left", padx=(8, 0), pady=(6, 0))
        tk.Label(left, text=SUBTITLE, background=theme.PANEL_BG,
                 foreground=theme.FG_MUTED,
                 font=theme.font(theme.SMALL_SIZE - 1)).pack(side="left", padx=(12, 0),
                                                            pady=(8, 0))
        # 任务概要条
        self.summary_frame = tk.Frame(head, background=theme.PANEL_BG)
        self.summary_frame.grid(row=0, column=2, sticky="e", padx=(8, 16), pady=(8, 6))
        self.summary_vars: Dict[str, tk.StringVar] = {}
        for key, label in (("type", "任务"), ("rocket", "箭型"),
                           ("site", "工位"), ("t0", "T0")):
            cell = tk.Frame(self.summary_frame, background=theme.PANEL_BG)
            cell.pack(side="left", padx=(0, 14))
            tk.Label(cell, text=label, background=theme.PANEL_BG,
                     foreground=theme.FG_FAINT,
                     font=theme.font(theme.SMALL_SIZE - 2)).pack(anchor="w")
            var = tk.StringVar(value="—")
            self.summary_vars[key] = var
            tk.Label(cell, textvariable=var, background=theme.PANEL_BG,
                     foreground=theme.FG,
                     font=theme.font(theme.SMALL_SIZE, True)).pack(anchor="w")
        tk.Frame(self, background=theme.BORDER, height=1).grid(row=1, column=0, sticky="ew")

    def _build_toolbar(self) -> None:
        bar = tk.Frame(self, background=theme.PANEL_BG)
        bar.grid(row=2, column=0, sticky="ew")
        bar.columnconfigure(6, weight=1)
        self.tb_new = ttk.Button(bar, text="新建", style="Tool.TButton",
                                 command=self.new_mission)
        self.tb_new.grid(row=0, column=0, padx=(16, 6), pady=8)
        self.tb_demo = ttk.Button(bar, text="载入示例", style="Tool.TButton",
                                  command=self.load_demo)
        self.tb_demo.grid(row=0, column=1, padx=(0, 6), pady=8)
        self.tb_save = ttk.Button(bar, text="保存任务", style="Tool.TButton",
                                  command=self.save_mission)
        self.tb_save.grid(row=0, column=2, padx=(0, 14), pady=8)
        self.tb_health = ttk.Button(bar, text="① 生成体检", style="Tool.TButton",
                                    command=lambda: self.run_health(switch=True))
        self.tb_health.grid(row=0, column=3, padx=(0, 6), pady=8)
        self.tb_start = ttk.Button(bar, text="② 开始计算（F5）", style="Accent.TButton",
                                   command=self.start_run)
        self.tb_start.grid(row=0, column=4, padx=(0, 6), pady=6)
        self.tb_cancel = ttk.Button(bar, text="取消", style="Tool.TButton",
                                    command=self.cancel_run, state="disabled")
        self.tb_cancel.grid(row=0, column=5, padx=(0, 14), pady=8)
        self.toolbar_hint = ttk.Label(bar, text="离线可用；不联网也能完成全流程",
                                      style="Hint.TLabel")
        self.toolbar_hint.grid(row=0, column=6, sticky="w", pady=8)
        self.tb_open = ttk.Button(bar, text="打开成果目录", style="Tool.TButton",
                                  command=self.open_workspace)
        self.tb_open.grid(row=0, column=7, padx=(6, 6), pady=8)
        self.tb_env = ttk.Button(bar, text="环境自检", style="Tool.TButton",
                                 command=self.check_env)
        self.tb_env.grid(row=0, column=8, padx=(0, 6), pady=8)
        ttk.Button(bar, text="帮助（F1）", style="Tool.TButton",
                   command=self.show_help).grid(row=0, column=9, padx=(0, 16), pady=8)
        tk.Frame(self, background=theme.BORDER, height=1).grid(row=3, column=0, sticky="ew")

    def _build_body(self) -> None:
        body = ttk.Frame(self, style="TFrame")
        body.grid(row=4, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        self.rowconfigure(4, weight=1)
        self.columnconfigure(0, weight=1)

        # ---- 左侧导航 ----
        side = ttk.Frame(body, style="Nav.TFrame", width=232)
        side.grid(row=0, column=0, sticky="ns")
        side.grid_propagate(False)
        side.rowconfigure(0, weight=1)
        side.columnconfigure(0, weight=1)
        self.nav = nav.StepNav(side, STEPS, on_select=self.select_page)
        self.nav.grid(row=0, column=0, sticky="nsew")
        tk.Frame(body, background=theme.BORDER, width=1).grid(row=0, column=1, sticky="ns")

        # ---- 右侧页面 ----
        right = ttk.Frame(body, style="TFrame")
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        self.page_host = ttk.Frame(right, style="TFrame")
        self.page_host.grid(row=0, column=0, sticky="nsew")
        self.page_host.rowconfigure(0, weight=1)
        self.page_host.columnconfigure(0, weight=1)

        classes = {"mission": TabMission, "vehicle": TabVehicle, "notam": TabNotam,
                   "observer": TabObserver, "station": TabStation,
                   "options": TabOptions, "health": TabHealth, "run": TabRun}
        for item in STEPS:
            key = item["key"]
            wrap = widgets.ScrollablePage(self.page_host)
            wrap.grid(row=0, column=0, sticky="nsew")
            page = classes[key](wrap.body, self)
            page.pack(fill="both", expand=True)
            self._pages[key] = page
            self._page_frames[key] = wrap
        # 页面底部导航（上一步/下一步）
        nav_bar = ttk.Frame(right)
        nav_bar.grid(row=1, column=0, sticky="ew", padx=12, pady=(2, 8))
        self.prev_btn = ttk.Button(nav_bar, text="← 上一步", style="Tool.TButton",
                                   command=self.prev_page)
        self.prev_btn.pack(side="left")
        self.next_btn = ttk.Button(nav_bar, text="下一步 →", style="Tool.TButton",
                                   command=self.next_page)
        self.next_btn.pack(side="left", padx=(8, 0))
        self.page_label = ttk.Label(nav_bar, text="", style="Hint.TLabel")
        self.page_label.pack(side="left", padx=(14, 0))
        self.quick_run = ttk.Button(nav_bar, text="开始计算（F5）", style="Accent.TButton",
                                    command=self.start_run)
        self.quick_run.pack(side="right")
        self.quick_health = ttk.Button(nav_bar, text="生成体检", style="Tool.TButton",
                                       command=lambda: self.run_health(switch=True))
        self.quick_health.pack(side="right", padx=(0, 8))
        self.show_page("mission", first=True)

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self, style="Statusbar.TFrame")
        bar.grid(row=5, column=0, sticky="ew")
        bar.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="就绪")
        self.status_label = ttk.Label(bar, textvariable=self.status_var,
                                      style="Status.TLabel", anchor="w")
        self.status_label.grid(row=0, column=0, sticky="ew")
        self.workroot_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.workroot_var,
                  style="StatusValue.TLabel").grid(row=0, column=1, sticky="e")
        self._refresh_workroot_label()

    def _bind_shortcuts(self) -> None:
        self.bind("<Control-n>", lambda e: self.new_mission())
        self.bind("<Control-o>", lambda e: self.open_mission())
        self.bind("<Control-s>", lambda e: self.save_mission())
        self.bind("<F5>", lambda e: self.start_run())
        self.bind("<F1>", lambda e: self.show_help())
        for i, item in enumerate(STEPS, start=1):
            self.bind("<Control-Key-%d>" % i,
                      lambda e, k=item["key"]: self.select_page(k))
        self.bind("<Alt-Left>", lambda e: self.prev_page())
        self.bind("<Alt-Right>", lambda e: self.next_page())

    # ------------------------------------------------------------------ #
    # 页面切换
    # ------------------------------------------------------------------ #
    def _deferred_init(self) -> None:
        self._deferred_after = None
        if self._closing:
            return
        try:
            self._pages["mission"].on_show()
        except Exception as exc:  # noqa: BLE001
            self._log_error("初始化箭型库", exc)
        self.refresh_summary()
        self.refresh_nav()
        self.set_status("就绪：可先「载入示例」跑一遍，或按左侧 1→8 填写后「生成体检」再「开始计算」。")

    def show_page(self, key: str, first: bool = False) -> None:
        if key not in self._page_frames:
            return
        self._current = key
        try:
            self._page_frames[key].tkraise()
        except tk.TclError:
            pass
        self.nav.set_active(key)
        idx = [s["key"] for s in STEPS].index(key)
        self.page_label.configure(text="第 %d / %d 步：%s" % (
            idx + 1, len(STEPS), STEPS[idx]["title"]))
        self.prev_btn.configure(state="normal" if idx > 0 else "disabled")
        self.next_btn.configure(state="normal" if idx < len(STEPS) - 1 else "disabled")
        try:
            self._pages[key].on_show()
        except Exception as exc:  # noqa: BLE001
            self._log_error("进入页面 %s" % key, exc)
        self.refresh_summary()
        self.refresh_nav()

    def select_page(self, key: str) -> None:
        self.show_page(key)

    def select_tab(self, tab) -> None:
        """兼容旧调用：按页面对象切页。"""
        for key, page in self._pages.items():
            if page is tab:
                self.show_page(key)
                return

    def next_page(self) -> None:
        keys = [s["key"] for s in STEPS]
        i = keys.index(self._current)
        if i < len(keys) - 1:
            self.show_page(keys[i + 1])

    def prev_page(self) -> None:
        keys = [s["key"] for s in STEPS]
        i = keys.index(self._current)
        if i > 0:
            self.show_page(keys[i - 1])

    # ------------------------------------------------------------------ #
    # 概要条 / 导航徽标
    # ------------------------------------------------------------------ #
    def refresh_summary(self) -> None:
        """更新顶部任务概要条（任何一页改动后调用）。"""
        if self._closing:
            return
        try:
            m = self._pages["mission"]
            mt = m.mission_type_cn()
            rocket = str(m.rocket_var.get() or "").strip() or "未选择"
            if getattr(m, "custom_rocket", None):
                rocket = str(m.custom_rocket.get("name") or "自定义箭型")
            site = str(m.site_var.get() or "").strip() or "未选择"
            t0 = str(m.t0_effective_text())
            self.summary_vars["type"].set(mt)
            self.summary_vars["rocket"].set(rocket[:18])
            self.summary_vars["site"].set(site[:16])
            self.summary_vars["t0"].set(t0[:22])
        except Exception:  # noqa: BLE001
            pass

    def refresh_nav(self) -> None:
        """刷新左侧每步的状态徽标。"""
        if self._closing:
            return
        setters = (
            ("mission", self._badge_mission),
            ("vehicle", self._badge_vehicle),
            ("notam", self._badge_notam),
            ("observer", self._badge_observer),
            ("station", self._badge_station),
            ("options", self._badge_options),
            ("health", self._badge_health),
            ("run", self._badge_run),
        )
        for key, fn in setters:
            try:
                text, kind = fn()
            except Exception:  # noqa: BLE001
                text, kind = ("", "muted")
            self.nav.set_badge(key, text, kind)

    def _badge_mission(self) -> Tuple[str, str]:
        m = self._pages["mission"]
        rocket = str(m.rocket_var.get() or "").strip()
        site = str(m.site_var.get() or "").strip()
        t0 = str(m.t0_effective_text())
        if not rocket and not getattr(m, "custom_rocket", None):
            return "待选火箭", "warn"
        if not site and m.type_var.get() != "return":
            return "待选工位", "warn"
        if not t0 or t0 == "（未知，待推断）":
            return "T0 待推断", "warn"
        return "已就绪", "ok"

    def _badge_vehicle(self) -> Tuple[str, str]:
        v = self._pages["vehicle"]
        mode = v.ref_mode_cn()
        if v.has_custom_sequence():
            return ("%s · 自定义时序" % mode)[:16], "accent"
        return mode[:16], "muted" if mode.startswith("自动") else "accent"

    def _badge_notam(self) -> Tuple[str, str]:
        n = self._pages["notam"]
        usable = len([x for x in n.items if x.is_usable()])
        if usable:
            return "%d 条航警" % usable, "ok"
        if n.predict_var.get():
            return "预测落区", "warn"
        return "无航警", "muted"

    def _badge_observer(self) -> Tuple[str, str]:
        o = self._pages["observer"]
        n = len(getattr(o, "items", []) or [])
        if n:
            return "%d 个观测点" % n, "ok"
        return ("默认发射场" if o.auto_observer() else "未设置"), "warn"

    def _badge_station(self) -> Tuple[str, str]:
        s = self._pages["station"]
        if s.return_only_var.get():
            return "仅返回任务", "accent"
        if s.return_enabled_var.get():
            return "含返回段", "accent"
        if s.enabled_var.get():
            return "交会启用", "accent"
        return "不适用", "muted"

    def _badge_options(self) -> Tuple[str, str]:
        o = self._pages["options"]
        on, total = o.enabled_count()
        return "%d/%d 模块" % (on, total), "ok" if on >= 8 else "muted"

    def _badge_health(self) -> Tuple[str, str]:
        br = self._last_health
        if br is None:
            return "未生成", "muted"
        gaps = len(getattr(br, "gaps", []) or [])
        if gaps:
            return "%d 项待关注" % gaps, "warn"
        return "已生成", "ok"

    def _badge_run(self) -> Tuple[str, str]:
        if self.running:
            return "运行中…", "accent"
        res = self.last_result
        if res is None:
            return "未运行", "muted"
        if getattr(res, "cancelled", False):
            return "已取消", "warn"
        if getattr(res, "ok", False):
            return "已完成", "ok"
        return "有失败步骤", "err"

    def set_status(self, text: str, kind: str = "info") -> None:
        try:
            self.status_var.set(text or "就绪")
            colors = {"info": "#2b3744", "ok": theme.OK, "warn": theme.WARN,
                      "err": theme.ERR, "accent": theme.ACCENT}
            self.status_label.configure(foreground=colors.get(kind, "#2b3744"))
        except tk.TclError:
            pass

    def notify(self, text: str, kind: str = "info") -> None:
        """状态栏提示 + 运行日志（重要事件用）。"""
        self.set_status(text, kind)
        if kind in ("warn", "err"):
            try:
                self._pages["run"].append_log(
                    "warn" if kind == "warn" else "error", text)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    # 后台线程与队列
    # ------------------------------------------------------------------ #
    def _post(self, kind: str, **payload: Any) -> None:
        try:
            self.q.put((kind, payload))
        except Exception:  # noqa: BLE001
            pass

    def _qlog(self, message: str) -> None:
        self._post("log", level="log", message=str(message))

    def _spawn(self, target, name: str = "worker") -> None:
        def runner():
            try:
                target()
            except Exception as exc:  # noqa: BLE001
                self._log_error("后台任务 %s" % name, exc)
                self._post("fail", context="后台任务异常",
                           message=widgets.explain_error(exc))

        threading.Thread(target=runner, name=name, daemon=True).start()

    def _poll(self) -> None:
        if self._closing:
            return
        try:
            while True:
                kind, payload = self.q.get_nowait()
                try:
                    self._handle(kind, payload)
                except Exception as exc:  # noqa: BLE001
                    self._log_error("处理界面事件", exc)
        except queue.Empty:
            pass
        try:
            self._poll_after = self.after(self.POLL_MS, self._poll)
        except tk.TclError:
            pass

    def _handle(self, kind: str, p: Dict[str, Any]) -> None:
        if kind == "log":
            self._pages["run"].append_log(p.get("level") or "log", p.get("message") or "")
        elif kind == "step":
            self._pages["run"].set_step(p.get("text") or "")
        elif kind == "progress":
            self._pages["run"].set_progress(p.get("frac"))
        elif kind == "health":
            self._on_health(p)
        elif kind == "written":
            self._on_written(p)
        elif kind == "done":
            self._on_done(p)
        elif kind == "fail":
            self._on_fail(p)
        elif kind == "env":
            self._on_env(p)
        elif kind == "station":
            try:
                self._pages["station"].show_station_info(p.get("info") or {})
            except Exception as exc:  # noqa: BLE001
                self._log_error("显示空间站数据", exc)
        elif kind == "dem":
            try:
                self._pages["options"].set_dem_status(p.get("text") or "")
                if p.get("release"):
                    self._set_busy(False)
                self.set_status("离线高程数据：" + (p.get("text") or "").splitlines()[0]
                                if p.get("text") else "就绪")
            except Exception as exc:  # noqa: BLE001
                self._log_error("显示高程导入状态", exc)

    def _on_health(self, p: Dict[str, Any]) -> None:
        br = p.get("br")
        plan = p.get("plan") or []
        self._last_health = br
        self._pages["health"].show_result(br, plan)
        if bool(p.get("release", p.get("switch"))):
            self._set_busy(False)
        if p.get("switch"):
            self.show_page("health")
        if br is not None:
            n_low = len(getattr(br, "gaps", []) or [])
            n_warn = len(getattr(br, "warnings", []) or [])
            self.set_status("数据体检完成：%d 项，低置信度 %d 条，提示 %d 条"
                            % (len(br.resolutions), n_low, n_warn),
                            "warn" if n_low else "ok")
        self.refresh_nav()

    def _on_written(self, p: Dict[str, Any]) -> None:
        br = p.get("br")
        workdir = p.get("workdir") or self.last_workdir
        self._last_health = br
        if br is not None:
            self._pages["health"].show_result(br, p.get("plan") or [])
        self._set_busy(False)
        self.last_workdir = workdir or self.last_workdir
        self._pages["run"].set_workdir(self.last_workdir)
        self._refresh_workroot_label()
        self.show_page("health")
        self.refresh_nav()
        widgets.show_info(
            self, "配置已生成",
            "已补全并写入配置：\n%s\n\n可点「开始计算」继续运行，或到「打开成果目录」查看。"
            % self.last_workdir)
        self.set_status("配置已生成：%s" % self.last_workdir, "ok")

    def _on_done(self, p: Dict[str, Any]) -> None:
        res = p.get("res")
        workdir = p.get("workdir") or self.last_workdir
        self.running = False
        self._set_busy(False)
        self._pages["run"].set_running(False)
        self.last_result = res
        if workdir:
            self.last_workdir = workdir
            P.save_settings({"last_workdir": workdir})
            self._refresh_workroot_label()
        self._pages["run"].show_result(res, workdir)
        self.refresh_nav()
        if res is None:
            return
        if getattr(res, "cancelled", False):
            self.set_status("计算已取消（已生成的文件保留）", "warn")
            widgets.show_info(self, "已取消",
                              "计算已取消，已生成的文件保留在工作区：\n%s" % workdir)
        elif getattr(res, "ok", False):
            self.set_status("计算完成：%s" % workdir, "ok")
            widgets.show_info(self, "计算完成",
                              "全部步骤已完成。\n\n成果目录：\n%s\n\n"
                              "建议先看 report\\任务摘要.md（一页纸）与 report\\成果清单.md。"
                              % workdir)
        else:
            failed = "、".join(s.get("name", "") for s in res.failed_steps()) or "未知步骤"
            self.set_status("计算结束但有失败步骤：%s" % failed, "err")
            widgets.show_error(
                self, "存在失败步骤",
                "以下步骤未成功：%s\n\n详情见 运行日志.txt；可修正后重试。\n工作区：%s"
                % (failed, workdir))

    def _on_fail(self, p: Dict[str, Any]) -> None:
        self.running = False
        self._set_busy(False)
        self._pages["run"].set_running(False)
        context = p.get("context") or "操作失败"
        message = p.get("message") or "未知错误"
        self._pages["run"].append_log("error", "%s：%s" % (context, message))
        self.set_status("%s：%s" % (context, message), "err")
        self.refresh_nav()
        widgets.show_error(self, context, message)

    def _on_env(self, p: Dict[str, Any]) -> None:
        self._set_busy(False)
        info = p.get("info") or {}
        content = "\n".join("%s：%s" % (k, v) for k, v in info.items())
        widgets.open_text_dialog(self, "环境自检", content or "（无信息）", 780, 620)
        self.set_status("环境自检完成。", "ok")

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self.busy = bool(busy)
        try:
            self._pages["health"].set_busy(self.busy)
            self._pages["run"].set_busy(self.busy)
            self._refresh_toolbar()
        except tk.TclError:
            pass
        if text:
            self.set_status(text)

    def _refresh_toolbar(self) -> None:
        busy = self.busy or self.running
        state = "disabled" if busy else "normal"
        try:
            for btn in (self.tb_new, self.tb_demo, self.tb_save, self.tb_env,
                        self.tb_health, self.quick_health):
                btn.configure(state=state)
            self.tb_start.configure(state="disabled" if busy else "normal")
            self.quick_run.configure(state="disabled" if busy else "normal")
            self.tb_cancel.configure(state="normal" if self.running else "disabled")
            self.toolbar_hint.configure(
                text=("正在计算，请稍候…" if self.running else
                      ("后台任务进行中…" if self.busy else
                       "离线可用；不联网也能完成全流程")))
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ #
    # 任务输入收集
    # ------------------------------------------------------------------ #
    def build_mission_input(self) -> MissionInput:
        inp = MissionInput()
        inp.notes = str(self._extra.get("notes") or "")
        inp.jobs = int(self._extra.get("jobs") or 0)
        inp.dem_radius_km = float(self._extra.get("dem_radius_km") or 200.0)
        self._pages["mission"].collect(inp)
        self._pages["vehicle"].collect(inp)
        self._pages["notam"].collect(inp)
        self._pages["observer"].collect(inp)
        self._pages["station"].collect(inp)
        self._pages["options"].collect(inp)
        return inp

    def _collect_or_error(self) -> Optional[MissionInput]:
        try:
            return self.build_mission_input()
        except ValueError as exc:
            widgets.show_error(self, "输入有误", str(exc))
            self.set_status("输入有误：%s" % exc, "err")
            self.refresh_nav()
        except Exception as exc:  # noqa: BLE001
            self._log_error("读取界面输入", exc)
            widgets.show_error(self, "读取界面输入失败", widgets.explain_error(exc))
        return None

    def set_mission_input(self, inp: MissionInput) -> None:
        self._extra["notes"] = inp.notes or ""
        self._extra["jobs"] = int(inp.jobs or 0)
        self._extra["dem_radius_km"] = float(inp.dem_radius_km or 200.0)
        self.last_result = None
        self.last_workdir = ""
        self._last_health = None
        for key in ("mission", "vehicle", "notam", "observer", "station", "options"):
            try:
                self._pages[key].load(inp)
            except Exception as exc:  # noqa: BLE001
                self._log_error("载入页面 %s" % key, exc)
        self._pages["health"].reset()
        self._pages["run"].clear_results()
        self._pages["run"].set_workdir("")
        self.show_page("mission")
        self.refresh_summary()
        self.refresh_nav()
        self.set_status("任务已载入：%s" % (inp.name or inp.rocket_label or "（未命名）"))

    # ------------------------------------------------------------------ #
    # 数据体检 / 写入配置
    # ------------------------------------------------------------------ #
    def _make_dem_resolver(self, auto_alt: bool, log=None):
        """构造 (lat, lon) -> (alt, source) 解析器；失败/关闭时返回 None。"""
        if not auto_alt:
            return None
        try:
            from app.core.dem_manager import DemManager
            dm = DemManager(str(P.dem_dir()), logger=log)
            if log:
                log("DEM 目录：%s" % P.dem_dir())
            return dm.observer_alt
        except Exception as exc:  # noqa: BLE001
            if log:
                log("DEM 模块不可用，高程改按城市库/0 m 处理：%s"
                    % widgets.explain_error(exc))
            return None

    def compute_health(self, inp: MissionInput, auto_alt: bool, log=None):
        """同步执行 build_config（供后台线程与 headless 测试共用）。"""
        from app.core import autoplan
        from app.core.config_build import build_config
        dem = self._make_dem_resolver(auto_alt, log)
        br = build_config(inp, workdir=None, logger=log, dem_resolver=dem, write=False)
        plan = autoplan.describe_plan(br.config)
        return br, plan

    def generate_health_sync(self, inp: Optional[MissionInput] = None):
        """同步生成体检并刷新界面（headless 自测与脚本使用）。"""
        if inp is None:
            inp = self.build_mission_input()
        br, plan = self.compute_health(inp, self._pages["observer"].auto_alt(), None)
        self._last_health = br
        self._pages["health"].show_result(br, plan)
        self.refresh_nav()
        return br, plan

    def run_health(self, switch: bool = True) -> None:
        if self.busy or self.running:
            return
        inp = self._collect_or_error()
        if inp is None:
            return
        auto_alt = self._pages["observer"].auto_alt()
        self._set_busy(True, "正在生成数据体检…")
        self._pages["run"].set_step("生成数据体检…")

        def worker():
            try:
                br, plan = self.compute_health(inp, auto_alt, self._qlog)
                self._post("health", br=br, plan=plan, switch=bool(switch), release=True)
            except Exception as exc:  # noqa: BLE001
                self._log_error("生成数据体检", exc)
                self._post("fail", context="生成数据体检失败",
                           message=widgets.explain_error(exc))

        self._spawn(worker, "health")

    def write_config_now(self) -> None:
        if self.busy or self.running:
            return
        inp = self._collect_or_error()
        if inp is None:
            return
        try:
            workdir = self._ensure_workdir(inp)
        except widgets.WorkdirCancelled:
            return
        except OSError as exc:
            widgets.show_error(self, "无法创建工作区", widgets.explain_error(exc))
            return
        if getattr(self, "_workdir_action_note", ""):
            self._pages["run"].append_log("info", self._workdir_action_note)
        auto_alt = self._pages["observer"].auto_alt()
        self._set_busy(True, "正在补全并写入配置…")
        self._pages["run"].set_step("补全并写入配置…")

        def worker():
            try:
                from app.core import autoplan
                from app.core.config_build import build_config
                self._qlog("正在补全配置并写入：%s" % workdir)
                dem = self._make_dem_resolver(auto_alt, self._qlog)
                br = build_config(inp, workdir=workdir, logger=self._qlog,
                                  dem_resolver=dem, write=True)
                try:
                    with open(os.path.join(workdir, "任务输入.json"), "w",
                              encoding="utf-8") as fh:
                        json.dump(inp.to_dict(), fh, ensure_ascii=False, indent=2)
                except OSError as exc:
                    self._qlog("保存任务输入.json 失败：%s" % exc)
                P.save_settings({"last_workdir": workdir})
                self._post("written", br=br, plan=autoplan.describe_plan(br.config),
                           workdir=workdir)
            except Exception as exc:  # noqa: BLE001
                self._log_error("补全并写入配置", exc)
                self._post("fail", context="补全并写入配置失败",
                           message=widgets.explain_error(exc))

        self._spawn(worker, "write-config")

    # ------------------------------------------------------------------ #
    # 开始计算 / 取消
    # ------------------------------------------------------------------ #
    def start_run(self) -> None:
        if self.running:
            return
        if self.busy:
            widgets.show_info(self, "请稍候", "后台任务尚未结束，请稍后再开始计算。")
            return
        inp = self._collect_or_error()
        if inp is None:
            return
        if not bool(inp.modules.get("export", True)):
            widgets.show_error(self, "无法开始", "「仿真与数据契约」是必选步骤，不能关闭。")
            return
        try:
            workdir = self._ensure_workdir(inp)
        except widgets.WorkdirCancelled:
            return
        except OSError as exc:
            widgets.show_error(self, "无法创建工作区", widgets.explain_error(exc))
            return
        try:
            with open(os.path.join(workdir, "任务输入.json"), "w", encoding="utf-8") as fh:
                json.dump(inp.to_dict(), fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            widgets.show_error(self, "无法保存任务输入",
                               "工作区不可写：%s\n%s" % (workdir, exc))
            return
        P.save_settings({"last_workdir": workdir})
        self.last_workdir = workdir
        self._refresh_workroot_label()

        auto_alt = self._pages["observer"].auto_alt()
        allow_net = bool(self._pages["options"].allow_network())
        self.stop_event = threading.Event()
        stop_event = self.stop_event
        self.running = True
        self._pages["run"].clear_log()
        if getattr(self, "_workdir_action_note", ""):
            self._pages["run"].append_log("info", self._workdir_action_note)
        self._pages["run"].set_workdir(workdir)
        self._pages["run"].clear_results()
        self._pages["run"].set_running(True)
        self.show_page("run")
        self._set_busy(True, "正在计算：%s" % os.path.basename(workdir))
        self.refresh_nav()
        self._qlog("工作区：%s" % workdir)
        self._qlog("任务输入已保存：%s" % os.path.join(workdir, "任务输入.json"))

        def worker():
            try:
                self._run_worker(inp, workdir, auto_alt, allow_net, stop_event)
            except Exception as exc:  # noqa: BLE001
                self._log_error("计算流水线", exc)
                self._post("fail", context="计算失败",
                           message=widgets.explain_error(exc))

        self._spawn(worker, "pipeline")

    def _run_worker(self, inp: MissionInput, workdir: str, auto_alt: bool,
                    allow_net: bool, stop_event: threading.Event) -> None:
        P.setup_env()
        P.ensure_syspath()
        from app.core import autoplan
        from app.core.config_build import build_config
        from app.core.runner import PipelineRunner, RunOptions

        self._qlog("正在补全配置（缺失字段由内置库/经验规则推断，详见数据体检）…")
        dem = self._make_dem_resolver(auto_alt, self._qlog)
        br = build_config(inp, workdir=workdir, logger=self._qlog,
                          dem_resolver=dem, write=True)
        self._post("health", br=br, plan=autoplan.describe_plan(br.config),
                   switch=False, release=False)
        modules = dict((br.config.get("_v3") or {}).get("modules") or inp.modules)
        options = RunOptions(
            modules=modules,
            mc_n=int(inp.mc_n or 0),
            jobs=int(inp.jobs or 0),
            mode=inp.mode,
            allow_network=bool(allow_net),
            quick_invert=bool(inp.inversion_maxiter == 6 and inp.inversion_popsize == 5),
            dem_skip_download=not bool(allow_net),
        )
        self._qlog("工作模式：%s；DEM 在线补全：%s；反演预算：%s" % (
            "精确" if inp.mode == "accurate" else "复现 v1（legacy）",
            "允许" if allow_net else "关闭", self._budget_text(inp)))
        runner = PipelineRunner(workdir, br.path, options,
                                on_event=self._on_run_event,
                                should_stop=stop_event.is_set)
        result = runner.run()
        self._post("done", res=result, workdir=workdir)

    @staticmethod
    def _budget_text(inp: MissionInput) -> str:
        pair = (int(inp.inversion_maxiter or 0), int(inp.inversion_popsize or 0))
        if pair == (6, 5):
            return "快速（maxiter=6, popsize=5）"
        if pair == (20, 10):
            return "精细（maxiter=20, popsize=10）"
        return "自动"

    def _on_run_event(self, ev) -> None:
        level = getattr(ev, "level", "log") or "log"
        message = getattr(ev, "message", "") or ""
        if level == "progress":
            self._post("progress", frac=getattr(ev, "progress", None))
            return
        if level == "step":
            text = message.strip().strip("=").strip()
            self._post("step", text=text)
            self._post("log", level="step", message=message)
            return
        self._post("log", level=level, message=message)

    def cancel_run(self) -> None:
        if not self.running or self.stop_event is None:
            return
        self.stop_event.set()
        self._pages["run"].append_log("warn",
                                      "已请求取消：将在当前步骤结束后停止（已生成文件保留）。")
        self._pages["run"].set_cancelling()
        self.set_status("正在取消…", "warn")
        self._refresh_toolbar()

    # ------------------------------------------------------------------ #
    # 编辑器入口（供各页面调用）
    # ------------------------------------------------------------------ #
    def edit_rocket(self, rocket: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """打开箭型参数编辑器；返回编辑后的记录（取消为 None）。"""
        try:
            from .dialog_rocket import RocketEditorDialog
        except Exception as exc:  # noqa: BLE001
            self._log_error("加载箭型编辑器", exc)
            widgets.show_error(self, "无法打开编辑器",
                               "箭型参数编辑器不可用：%s" % widgets.explain_error(exc))
            return None
        if rocket is None:
            rocket = getattr(self._pages["mission"], "current_rocket_record", lambda: None)()
        try:
            dlg = RocketEditorDialog(self, rocket)
        except Exception as exc:  # noqa: BLE001
            self._log_error("打开箭型编辑器", exc)
            widgets.show_error(self, "编辑器出错", widgets.explain_error(exc))
            return None
        return dlg.result

    def edit_orbit(self, orbit: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        try:
            from .dialog_orbit import OrbitEditorDialog
        except Exception as exc:  # noqa: BLE001
            self._log_error("加载轨道编辑器", exc)
            widgets.show_error(self, "无法打开编辑器",
                               "参考轨道编辑器不可用：%s" % widgets.explain_error(exc))
            return None
        try:
            dlg = OrbitEditorDialog(self, orbit)
        except Exception as exc:  # noqa: BLE001
            self._log_error("打开轨道编辑器", exc)
            widgets.show_error(self, "编辑器出错", widgets.explain_error(exc))
            return None
        return dlg.result

    # ------------------------------------------------------------------ #
    # 关闭
    # ------------------------------------------------------------------ #
    def destroy(self) -> None:
        """销毁窗口前取消挂起的 after 回调，避免 Tcl 报 invalid command name。"""
        self._closing = True
        try:
            P.save_settings({"window_geometry": self.geometry()})
        except Exception:  # noqa: BLE001
            pass
        for attr in ("_poll_after", "_deferred_after"):
            ident = getattr(self, attr, None)
            if ident:
                try:
                    self.after_cancel(ident)
                except tk.TclError:
                    pass
                setattr(self, attr, None)
        super().destroy()

    def _on_close(self) -> None:
        if self.running and not widgets.ask_yes_no(
                self, "正在计算",
                "计算仍在进行：退出会中断后续步骤（已生成的文件保留）。\n确定退出？"):
            return
        if self.running and self.stop_event is not None:
            self.stop_event.set()
        self._closing = True
        try:
            self.destroy()
        except tk.TclError:
            pass


# --------------------------------------------------------------------------- #
# 启动入口
# --------------------------------------------------------------------------- #
def create_window() -> MainWindow:
    """创建主窗口（不自启 mainloop；供 launch / 自测脚本使用）。"""
    enable_dpi_awareness()
    return MainWindow()


def launch() -> None:
    """入口：创建窗口并进入事件循环。"""
    app = create_window()
    try:
        app.mainloop()
    except KeyboardInterrupt:
        try:
            app.destroy()
        except tk.TclError:
            pass
