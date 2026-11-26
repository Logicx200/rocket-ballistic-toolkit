# -*- coding: utf-8 -*-
"""步骤 ⑧：运行与成果（进度 / 步骤台账 / 结果摘要 / 实时日志 / 成果文件树）。

v4 的变化
---------
* **步骤台账**（新增）：每一步显示「完成 / 跳过 / 不适用 / 未启用 / 失败 + 原因」，
  回答 v3 里最容易困惑的问题："为什么这次没出这张图？"
* **成果文件树**（新增）：直接读 ``report/成果清单.json``，按目录分组、双击打开，
  不必再去资源管理器里翻目录；
* 一键打开"任务摘要"（一页纸）与"成果清单"，这两个文件是 v4 新增的通用成果。
"""
from __future__ import annotations

import json
import os
import time
import tkinter as tk
from tkinter import filedialog, ttk
from typing import Any, Dict, List, Optional

from . import theme, widgets

STATE_TAG = {"完成": "ok", "跳过": "skip", "不适用": "na", "失败": "fail",
             "取消": "muted", "未启用（未勾选模块）": "na",
             "未勾选模块": "na"}


class TabRun(ttk.Frame):
    """⑧ 运行与成果。"""

    REPORT_CANDIDATES = ("report/任务摘要.md", "report/弹道仿真报告.docx",
                         "report/弹道仿真报告.md")
    KMZ_CANDIDATES = ("viz/弹道三维_含残骸坠落.kmz", "viz/弹道三维_含残骸坠落.kml",
                      "viz/返回舱弹道.kmz")
    INVENTORY = "report/成果清单.json"

    def __init__(self, master, app):
        super().__init__(master, padding=(16, 10))
        self.app = app
        self._running = False
        self._busy = False
        self._t_start: Optional[float] = None
        self._timer_after: Optional[str] = None
        self.last_result = None
        self._build()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        self.columnconfigure(0, weight=1)

        widgets.page_title(
            self, "运行与成果",
            "点「开始计算」后会在后台执行整条流水线；进度、日志与每一步的状态都会实时刷新。"
            "运行结束后这里会列出全部成果文件，双击即可打开。").grid(
            row=0, column=0, sticky="ew", pady=(0, 8))

        # ---- 工作区 ----
        work = widgets.Card(self, title="工作区")
        work.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        wb = ttk.Frame(work.body, style="Panel.TFrame")
        wb.grid(row=0, column=0, sticky="ew")
        wb.columnconfigure(1, weight=1)
        ttk.Label(wb, text="输出目录", style="Panel.TLabel").grid(
            row=0, column=0, sticky="e", padx=(0, 8))
        self.workdir_var = tk.StringVar()
        self.workdir_entry = ttk.Entry(wb, textvariable=self.workdir_var)
        self.workdir_entry.grid(row=0, column=1, sticky="ew")
        ttk.Button(wb, text="自动命名", style="Small.TButton",
                   command=self.app.auto_workdir).grid(row=0, column=2, padx=(6, 0))
        ttk.Button(wb, text="浏览…", style="Small.TButton",
                   command=self._browse).grid(row=0, column=3, padx=(6, 0))
        widgets.hint(work.body, "开始计算前会把界面输入保存为 工作区\\任务输入.json，便于复现；"
                                "同一目录已有成果时会询问「覆盖 / 新建副本 / 取消」。",
                     style="CardHint.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 0))

        # ---- 操作与进度 ----
        run = widgets.Card(self, title="运行")
        run.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        bar = ttk.Frame(run.body, style="Panel.TFrame")
        bar.grid(row=0, column=0, sticky="ew")
        self.start_btn = ttk.Button(bar, text="开始计算（F5）", style="Accent.TButton",
                                    command=self.app.start_run)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(bar, text="取消", style="Tool.TButton",
                                     command=self.app.cancel_run, state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="生成体检", style="Tool.TButton",
                   command=lambda: self.app.run_health(switch=True)).pack(
            side="left", padx=(8, 0))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=12)
        self.open_dir_btn = ttk.Button(bar, text="打开成果目录", style="Tool.TButton",
                                       command=self.open_workdir)
        self.open_dir_btn.pack(side="left")
        self.open_summary_btn = ttk.Button(bar, text="任务摘要", style="Tool.TButton",
                                           command=self._open_summary)
        self.open_summary_btn.pack(side="left", padx=(8, 0))
        self.open_report_btn = ttk.Button(bar, text="技术报告", style="Tool.TButton",
                                          command=self._open_report)
        self.open_report_btn.pack(side="left", padx=(8, 0))
        self.open_kmz_btn = ttk.Button(bar, text="三维成果", style="Tool.TButton",
                                       command=self._open_kmz)
        self.open_kmz_btn.pack(side="left", padx=(8, 0))

        prog = ttk.Frame(run.body, style="Panel.TFrame")
        prog.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        prog.columnconfigure(0, weight=1)
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress = ttk.Progressbar(prog, orient="horizontal", mode="determinate",
                                        maximum=100.0, variable=self.progress_var)
        self.progress.grid(row=0, column=0, sticky="ew")
        self.percent_label = ttk.Label(prog, text="0%", width=6, anchor="e",
                                       style="Panel.TLabel")
        self.percent_label.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.step_label = ttk.Label(prog, text="就绪", style="CardHint.TLabel")
        self.step_label.grid(row=0, column=2, sticky="w", padx=(12, 0))
        self.elapsed_label = ttk.Label(prog, text="", style="CardHint.TLabel")
        self.elapsed_label.grid(row=0, column=3, sticky="e", padx=(12, 0))

        # ---- 步骤台账 ----
        steps = widgets.Card(self, title="步骤台账（含跳过原因）",
                             hint_text="「跳过」= 依赖不满足（例如没有观测点、上游图没生成）；"
                                       "「不适用」= 与任务类型无关（例如返回任务不做上升段反演）；"
                                       "「未启用」= 你在「输出与模块」页没勾选。")
        steps.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        container, self.steps_tree = widgets.make_tree(
            steps.body,
            [("name", "步骤", 190, "w"), ("state", "状态", 120, "center"),
             ("sec", "耗时 s", 74, "e"), ("note", "说明 / 跳过原因", 520, "w")],
            height=6)
        container.grid(row=0, column=0, sticky="ew")

        # ---- 结果摘要 ----
        summ = widgets.Card(self, title="结果摘要")
        summ.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        container, self.summary_tree = widgets.make_tree(
            summ.body, [("item", "项目", 190, "w"), ("value", "内容", 700, "w")],
            height=7)
        container.grid(row=0, column=0, sticky="ew")

        # ---- 日志 ----
        log_card = widgets.Card(self, title="实时日志")
        log_card.grid(row=5, column=0, sticky="ew", pady=(0, 8))
        log_bar = ttk.Frame(log_card.body, style="Panel.TFrame")
        log_bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.pause_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(log_bar, text="暂停自动滚动", variable=self.pause_var,
                        style="Panel.TCheckbutton",
                        command=self._apply_pause).pack(side="right")
        ttk.Button(log_bar, text="清空日志", style="Small.TButton",
                   command=self.clear_log).pack(side="right", padx=(0, 8))
        widgets.hint(log_bar, "日志按等级着色：普通=黑，警告=橙，错误=红；"
                              "完整日志同时写入 工作区\\运行日志.txt",
                     style="CardHint.TLabel").pack(side="left")
        self.log = widgets.LogPanel(log_card.body, height=11)
        self.log.grid(row=1, column=0, sticky="ew")

        # ---- 成果文件 ----
        art = widgets.Card(self, title="成果文件（双击打开）")
        art.grid(row=6, column=0, sticky="ew")
        abar = ttk.Frame(art.body, style="Panel.TFrame")
        abar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Button(abar, text="刷新列表", style="Small.TButton",
                   command=self.refresh_artifacts).pack(side="left")
        ttk.Button(abar, text="打开选中", style="Small.TButton",
                   command=self._open_selected).pack(side="left", padx=(6, 0))
        ttk.Button(abar, text="成果清单", style="Small.TButton",
                   command=self._open_inventory).pack(side="left", padx=(6, 0))
        self.artifact_hint = widgets.hint(abar, "", style="CardHint.TLabel")
        self.artifact_hint.pack(side="left", padx=(12, 0))
        container, self.artifact_tree = widgets.make_tree(
            art.body, [("name", "文件 / 目录", 380, "w"), ("size", "大小", 90, "e"),
                       ("desc", "说明", 360, "w")],
            height=8)
        container.grid(row=1, column=0, sticky="ew")
        self.artifact_tree.bind("<Double-1>", lambda e: self._open_selected())
        self._refresh_open_buttons()

    # ------------------------------------------------------------------ #
    # 运行状态
    # ------------------------------------------------------------------ #
    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self._update_buttons()

    def set_running(self, running: bool) -> None:
        self._running = bool(running)
        if running:
            self.set_progress(0.0)
            self.set_step("准备中…")
            self._t_start = time.time()
            self._tick()
        else:
            self._stop_timer()
        self._update_buttons()

    def set_cancelling(self) -> None:
        self.cancel_btn.configure(state="disabled", text="正在取消…")

    def _update_buttons(self) -> None:
        busy = self._busy or self._running
        try:
            self.start_btn.configure(state="disabled" if busy else "normal")
            self.cancel_btn.configure(state="normal" if self._running else "disabled")
            if not self._running:
                self.cancel_btn.configure(text="取消")
        except tk.TclError:
            pass

    def _tick(self) -> None:
        self._timer_after = None
        if not self._running:
            return
        if self._t_start is None:
            self._t_start = time.time()
        sec = time.time() - self._t_start
        try:
            self.elapsed_label.configure(text="已用 %02d:%02d" % (int(sec // 60),
                                                                 int(sec % 60)))
            self._timer_after = self.after(1000, self._tick)
        except tk.TclError:
            pass

    def _stop_timer(self) -> None:
        if self._timer_after:
            try:
                self.after_cancel(self._timer_after)
            except tk.TclError:
                pass
            self._timer_after = None

    def set_progress(self, frac: Optional[float]) -> None:
        try:
            value = 100.0 * max(0.0, min(1.0, float(frac or 0.0)))
        except (TypeError, ValueError):
            value = 0.0
        self.progress_var.set(value)
        self.percent_label.configure(text="%.0f%%" % value)

    def set_step(self, text: str) -> None:
        self.step_label.configure(text=(text or "就绪")[:70])

    def append_log(self, level: str, message: str) -> None:
        self.log.append(level, message)

    def clear_log(self) -> None:
        self.log.clear()

    def _apply_pause(self) -> None:
        self.log.paused.set(bool(self.pause_var.get()))
        if not self.pause_var.get():
            self.log.scroll_to_end()

    # ------------------------------------------------------------------ #
    # 工作区
    # ------------------------------------------------------------------ #
    def get_workdir(self) -> str:
        return self.workdir_var.get().strip()

    def set_workdir(self, path: str) -> None:
        self.workdir_var.set(str(path or ""))
        self._refresh_open_buttons()
        self.refresh_artifacts()

    def _browse(self) -> None:
        initial = self.get_workdir() or str(self.app.default_work_root())
        chosen = filedialog.askdirectory(parent=self, title="选择任务工作区",
                                         initialdir=initial)
        if chosen:
            self.set_workdir(chosen)

    def _first_existing(self, workdir: str, rels) -> Optional[str]:
        if not workdir:
            return None
        for rel in rels:
            p = os.path.join(workdir, rel.replace("/", os.sep))
            if os.path.isfile(p):
                return p
        return None

    def _refresh_open_buttons(self) -> None:
        wd = self.get_workdir()
        has_dir = bool(wd) and os.path.isdir(wd)
        report = self._first_existing(wd, self.REPORT_CANDIDATES)
        kmz = self._first_existing(wd, self.KMZ_CANDIDATES)
        summary = self._first_existing(wd, ("report/任务摘要.md",))
        inv = self._first_existing(wd, (self.INVENTORY, "report/成果清单.md"))
        try:
            self.open_dir_btn.configure(state="normal" if has_dir else "disabled")
            self.open_report_btn.configure(state="normal" if report else "disabled")
            self.open_kmz_btn.configure(state="normal" if kmz else "disabled")
            self.open_summary_btn.configure(state="normal" if summary else "disabled")
            _ = inv
        except tk.TclError:
            pass

    def open_workdir(self) -> None:
        wd = self.get_workdir()
        if wd and os.path.isdir(wd):
            ok, msg = widgets.open_path_ex(wd)
            if ok:
                self.app.set_status(msg, "ok")
            else:
                widgets.show_error(self, "打开失败", msg)
            return
        wd = str(self.app.default_work_root())
        try:
            os.makedirs(wd, exist_ok=True)
        except OSError as exc:
            widgets.show_error(self, "打开失败", "无法创建工作区根目录：%s" % exc)
            return
        ok, msg = widgets.open_path_ex(wd)
        if not ok:
            widgets.show_error(self, "打开失败", msg)

    def _open_external(self, path: str, title: str, extra_tip: str = "") -> None:
        ok, msg = widgets.open_path_ex(path)
        if not ok:
            widgets.show_error(self, title, msg + extra_tip)
            return
        try:
            self.app.set_status(msg, "ok")
        except Exception:  # noqa: BLE001
            pass
        if ("定位" in msg) or ("未检测到" in msg) or ("剪贴板" in msg):
            widgets.show_info(self, title, msg + ("\n\n" + extra_tip if extra_tip else ""))

    def _open_summary(self) -> None:
        wd = self.get_workdir()
        path = self._first_existing(wd, ("report/任务摘要.md", "report/任务摘要.json"))
        if not path:
            widgets.show_info(self, "暂无摘要",
                              "任务摘要尚未生成：请先运行「通用成果汇总」步骤。")
            return
        self._open_external(path, "任务摘要")

    def _open_report(self) -> None:
        wd = self.get_workdir()
        path = self._first_existing(wd, self.REPORT_CANDIDATES[1:])
        if not path:
            widgets.show_info(self, "暂无报告", "技术报告尚未生成：请先运行「中文技术报告」步骤。")
            return
        self._open_external(path, "打开技术报告")

    def _open_kmz(self) -> None:
        wd = self.get_workdir()
        path = self._first_existing(wd, self.KMZ_CANDIDATES)
        if not path:
            widgets.show_info(self, "暂无三维成果",
                              "KMZ/KML 尚未生成：请先运行「三维 KMZ/KML」步骤。")
            return
        self._open_external(path, "打开三维成果", "可用「打开成果目录」后手动打开。")

    def _open_inventory(self) -> None:
        wd = self.get_workdir()
        path = self._first_existing(wd, ("report/成果清单.md", self.INVENTORY))
        if not path:
            widgets.show_info(self, "暂无清单", "成果清单尚未生成：请先运行「通用成果汇总」步骤。")
            return
        self._open_external(path, "成果清单")

    # ------------------------------------------------------------------ #
    # 结果展示
    # ------------------------------------------------------------------ #
    def show_result(self, result, workdir: str = "") -> None:
        self.last_result = result
        if workdir:
            self.set_workdir(workdir)
        widgets.clear_tree(self.summary_tree)
        widgets.clear_tree(self.steps_tree)
        summary = getattr(result, "summary", None) or {}
        if summary:
            for key, value in summary.items():
                self.summary_tree.insert("", "end", values=(key, str(value)))
        else:
            self.summary_tree.insert("", "end", values=("（无摘要）", ""))

        for step in getattr(result, "steps", None) or []:
            ok = bool(step.get("ok"))
            state = str(step.get("state") or ("完成" if ok else "失败"))
            note = step.get("reason") or step.get("error") or ""
            tag = STATE_TAG.get(state, "warn")
            self.steps_tree.insert("", "end", values=(
                step.get("name") or step.get("key") or "",
                state,
                "%.1f" % float(step.get("seconds") or 0.0),
                str(note)[:160],
            ), tags=(tag,))

        cancelled = bool(getattr(result, "cancelled", False))
        ok = bool(getattr(result, "ok", False))
        self.set_progress(1.0 if (ok or cancelled) else 0.0)
        if cancelled:
            self.set_step("已取消")
        elif ok:
            self.set_step("全部完成")
        else:
            self.set_step("存在失败步骤，详见运行日志.txt")
        self._refresh_open_buttons()
        self.refresh_artifacts()

    def clear_results(self) -> None:
        """清空上一次运行的结果展示（新建/载入任务时用）。"""
        self.last_result = None
        widgets.clear_tree(self.summary_tree)
        widgets.clear_tree(self.steps_tree)
        widgets.clear_tree(self.artifact_tree)
        self.set_progress(0.0)
        self.set_step("就绪")
        self.elapsed_label.configure(text="")
        self._refresh_open_buttons()

    # ------------------------------------------------------------------ #
    # 成果文件树
    # ------------------------------------------------------------------ #
    def refresh_artifacts(self) -> None:
        """读 report/成果清单.json（没有则扫描目录）填充成果文件树。"""
        widgets.clear_tree(self.artifact_tree)
        wd = self.get_workdir()
        if not wd or not os.path.isdir(wd):
            self.artifact_hint.configure(text="尚未选择工作区")
            return
        inv_path = os.path.join(wd, self.INVENTORY.replace("/", os.sep))
        files: List[Dict[str, Any]] = []
        missing: List[str] = []
        if os.path.isfile(inv_path):
            try:
                with open(inv_path, encoding="utf-8-sig") as f:
                    data = json.load(f)
                files = list(data.get("files") or [])
                missing = [m.get("path") for m in (data.get("missing") or [])]
            except Exception:  # noqa: BLE001
                files = []
        if not files:
            for root, _dirs, names in os.walk(wd):
                for nm in names:
                    p = os.path.join(root, nm)
                    rel = os.path.relpath(p, wd).replace("\\", "/")
                    try:
                        size = os.path.getsize(p)
                    except OSError:
                        continue
                    files.append({"path": rel, "size": size,
                                  "group": rel.split("/")[0] if "/" in rel else "根目录"})
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for f in files:
            g = f.get("group") or (f["path"].split("/")[0] if "/" in f["path"] else "根目录")
            groups.setdefault(g, []).append(f)
        total = sum(int(f.get("size") or 0) for f in files)
        for g in sorted(groups):
            node = self.artifact_tree.insert(
                "", "end", text=g, values=("%s/（%d 个文件）" % (g, len(groups[g])),
                                           widgets.fmt_size(sum(
                                               int(x.get("size") or 0)
                                               for x in groups[g])), ""),
                open=False, tags=("group",))
            for f in sorted(groups[g], key=lambda x: x.get("path") or ""):
                name = str(f.get("path") or "").split("/", 1)[-1]
                self.artifact_tree.insert(
                    node, "end", text=name,
                    values=(name, widgets.fmt_size(f.get("size") or 0),
                            self._describe(name)),
                    tags=("ok" if name.endswith((".md", ".json", ".csv")) else "muted",))
        hint = "共 %d 个文件 / %s" % (len(files), widgets.fmt_size(total))
        if missing:
            hint += "；应产出缺失 %d 项" % len(missing)
        self.artifact_hint.configure(text=hint)

    @staticmethod
    def _describe(name: str) -> str:
        table = {
            "config.json": "本次完整配置", "config_initial.json": "反演前初值",
            "任务输入.json": "界面/命令行输入", "运行日志.txt": "完整运行日志",
            "弹道仿真报告.md": "中文技术报告（Markdown）",
            "弹道仿真报告.docx": "中文技术报告（Word）",
            "任务摘要.md": "一页纸任务摘要", "任务摘要.json": "摘要结构化数据",
            "成果清单.md": "成果清单与步骤台账", "成果清单.json": "成果清单结构化",
            "qc_report.json": "交付自检结果", "发射窗口计算.json": "发射窗口",
            "弹道事件表.csv": "弹道事件", "落点与射程.csv": "残骸落点",
            "入轨根数.json": "入轨根数", "关键节点.csv": "统一关键节点",
            "飞行阶段.csv": "飞行阶段时间线", "飞行环境.csv": "动压/过载剖面",
            "轨迹统计.json": "轨迹统计", "飞行环境关注项.json": "过载/动压关注项",
            "返回舱返回.json": "返回舱返回结果", "返回舱_时序.csv": "返回舱时序",
            "返回舱阶段.csv": "返回舱阶段", "交会对接.json": "交会对接方案",
            "空间站轨道.json": "空间站轨道", "sky_summary.json": "天空轨迹汇总",
            "mc_stats.json": "蒙特卡洛统计",
        }
        if name in table:
            return table[name]
        ext = os.path.splitext(name)[1].lower()
        return {".png": "图片", ".kml": "KML 三维", ".kmz": "KMZ 三维",
                ".csv": "数据表", ".json": "结构化数据", ".md": "文档",
                ".docx": "Word 文档"}.get(ext, "")

    def _open_selected(self) -> None:
        sel = self.artifact_tree.selection()
        if not sel:
            widgets.show_info(self, "未选中", "请先在成果列表里选中一个文件。")
            return
        iid = sel[0]
        wd = self.get_workdir()
        parent = self.artifact_tree.parent(iid)
        if not parent:
            # 选中的是分组节点 → 打开该目录
            group = self.artifact_tree.item(iid, "text")
            path = os.path.join(wd, group)
            if os.path.isdir(path):
                ok, msg = widgets.open_path_ex(path)
                self.app.set_status(msg, "ok" if ok else "err")
            return
        group = self.artifact_tree.item(parent, "text")
        name = self.artifact_tree.item(iid, "text")
        path = os.path.join(wd, group, name)
        if not os.path.isfile(path):
            widgets.show_warning(self, "文件不存在", "该文件已被移动或删除：\n%s" % path)
            self.refresh_artifacts()
            return
        self._open_external(path, "打开成果")

    def on_show(self) -> None:
        self._refresh_open_buttons()
        self.refresh_artifacts()
