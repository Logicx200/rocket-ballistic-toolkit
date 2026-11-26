# -*- coding: utf-8 -*-
"""主窗口动作：任务文件、工作区、我的库管理、环境自检、帮助。

以 Mixin 形式提供给 :class:`app.gui.main_window.MainWindow`，避免主窗口文件过长。
页面访问统一走 ``self._pages[key]``（v3 是 ``self.tab_xxx``，v4 已统一）。
"""
from __future__ import annotations

import json
import os
import shutil
import time
import traceback
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, simpledialog, ttk
from typing import Any, Dict, List, Optional

from app.core import paths as P
from app.core import workspace as W
from app.core.model import MissionInput

from . import help_text, widgets


class MainActionsMixin:
    """依赖 MainWindow 上的 ``q / busy / running / _pages / _post / _spawn`` 等成员。"""

    # ------------------------------------------------------------------ #
    # 基础信息
    # ------------------------------------------------------------------ #
    def default_work_root(self) -> Path:
        """默认工作区根（不创建目录，供界面展示/选择初始目录）。"""
        root = (P.settings() or {}).get("work_root") or ""
        return Path(root) if root else Path(P.runtime_base()) / "作品"

    def _refresh_workroot_label(self) -> None:
        try:
            path = str(self.default_work_root())
            if len(path) > 76:
                path = path[:36] + "…" + path[-36:]
            self.workroot_var.set("工作区根目录：%s" % path)
        except Exception:  # noqa: BLE001
            self.workroot_var.set("")

    def _log_error(self, context: str, exc: BaseException) -> None:
        path = widgets.write_error_log(context, exc)
        msg = widgets.explain_error(exc)
        try:
            self._pages["run"].append_log("error", "%s：%s" % (context, msg))
        except Exception:  # noqa: BLE001
            pass
        note = ("（详细堆栈见 %s）" % path) if path else ""
        self.set_status("发生错误：%s%s" % (msg, note), "err")

    def _on_tk_error(self, exc, val, tb) -> None:
        """Tk 回调里未捕获的异常：写日志，避免进程崩溃。"""
        try:
            with open(P.runtime_base() / "gui_error.log", "a", encoding="utf-8") as fh:
                fh.write("\n[%s] Tk 回调异常\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
                fh.write("".join(traceback.format_exception(exc, val, tb)))
        except Exception:  # noqa: BLE001
            pass
        self.set_status("界面回调异常：%s" % val, "err")
        try:
            self._pages["run"].append_log("error", "界面回调异常：%s" % val)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    # 工作区
    # ------------------------------------------------------------------ #
    def planned_workdir(self, inp: MissionInput) -> Path:
        """按 工作区根 + 自动命名 生成（已存在则加 _2/_3 后缀）。"""
        from app.core import infer as I
        t0 = None
        if (inp.t0_text or "").strip():
            try:
                from app.core.notam_parse import parse_t0_text
                t0, _tz, _note = parse_t0_text(inp.t0_text, inp.tz_hint or "auto", None)
            except Exception:  # noqa: BLE001
                t0 = None
        if t0 is None and inp.notams:
            try:
                t0, _s, _c, _n = I.infer_t0_from_notams(
                    [z.as_config() for z in inp.notams])
            except Exception:  # noqa: BLE001
                t0 = None
        if getattr(inp, "return_only", False) or str(inp.mission_type) == "return":
            name = ("返回任务_%s" % t0.strftime("%Y-%m-%d_%H%M")) if t0 else "返回任务"
        else:
            name = I.auto_workdir_name(inp.rocket_label or "火箭任务",
                                       inp.site_label or "", t0)
        return self._unique_dir(Path(P.work_root()) / name)

    @staticmethod
    def _unique_dir(base: Path) -> Path:
        if not base.exists():
            return base
        for i in range(2, 1000):
            cand = base.with_name("%s_%d" % (base.name, i))
            if not cand.exists():
                return cand
        return base.with_name("%s_%d" % (base.name, int(time.time())))

    def _ensure_workdir(self, inp: MissionInput) -> str:
        raw = self._pages["run"].get_workdir().strip()
        path = Path(raw) if raw else self.planned_workdir(inp)
        self._workdir_action_note = ""
        if path.exists() and W.has_results(path):
            choice = widgets.ask_workdir_conflict(self, str(path))
            if choice == "cancel":
                raise widgets.WorkdirCancelled()
            if choice == "new":
                newp = self._unique_dir(path)
                self._workdir_action_note = (
                    "工作区已有成果：%s；按选择新建副本：%s" % (path, newp))
                path = newp
            else:
                self._workdir_action_note = (
                    "工作区已有成果：%s；已选择覆盖（复用该目录）" % path)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OSError("无法创建工作区「%s」：%s" % (path, exc)) from exc
        if not os.access(str(path), os.W_OK):
            raise OSError("工作区不可写：%s" % path)
        self._pages["run"].set_workdir(str(path))
        if self._workdir_action_note:
            self.set_status(self._workdir_action_note, "warn")
        return str(path)

    def auto_workdir(self) -> None:
        if getattr(self, "busy", False) or getattr(self, "running", False):
            return
        inp = self._collect_or_error()
        if inp is None:
            return
        try:
            path = self.planned_workdir(inp)
        except Exception as exc:  # noqa: BLE001
            self._log_error("生成工作区名", exc)
            return
        self._pages["run"].set_workdir(str(path))
        self.set_status("工作区将在开始时创建：%s" % path)

    def open_workspace(self) -> None:
        cand = self._pages["run"].get_workdir().strip() or self.last_workdir
        result = getattr(self, "last_result", None)
        res_wd = getattr(result, "workdir", "") if result is not None else ""
        for target in (cand, res_wd):
            if target and os.path.isdir(str(target)):
                ok, msg = widgets.open_path_ex(target)
                if ok:
                    self.set_status(msg, "ok")
                else:
                    widgets.show_error(self, "打开失败", msg)
                return
        self._pages["run"].open_workdir()

    # ------------------------------------------------------------------ #
    # 任务文件
    # ------------------------------------------------------------------ #
    def new_mission(self) -> None:
        if getattr(self, "running", False):
            return
        self.set_mission_input(MissionInput())
        self.set_status("已新建空白任务：按左侧 1→8 填写，或先「载入示例」。")

    def load_demo(self) -> None:
        if getattr(self, "busy", False) or getattr(self, "running", False):
            return
        try:
            from app.main import DEMO_CZ2D
            inp = MissionInput.from_dict(dict(DEMO_CZ2D))
        except Exception as exc:  # noqa: BLE001
            self._log_error("载入示例", exc)
            widgets.show_error(self, "载入示例失败", widgets.explain_error(exc))
            return
        self.set_mission_input(inp)
        self.auto_workdir()
        self.set_status("已载入内置示例（长征二号丁 / 太原 LC-9）。直接点「开始计算」即可。",
                        "ok")

    def open_mission(self) -> None:
        if getattr(self, "busy", False) or getattr(self, "running", False):
            return
        path = filedialog.askopenfilename(
            parent=self, title="打开任务 JSON",
            initialdir=str(self.default_work_root()),
            filetypes=[("任务输入 / 配置 JSON", "*.json"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and "mission" in data and "vehicle" in data:
                from app.core.config_build import mission_input_from_config
                inp = mission_input_from_config(data)
            else:
                inp = MissionInput.from_dict(data)
        except Exception as exc:  # noqa: BLE001
            self._log_error("打开任务 JSON", exc)
            widgets.show_error(self, "打开任务失败",
                               "%s\n%s" % (path, widgets.explain_error(exc)))
            return
        self.set_mission_input(inp)
        self.set_status("已打开任务：%s" % path, "ok")

    def save_mission(self) -> None:
        inp = self._collect_or_error()
        if inp is None:
            return
        default_name = (os.path.basename(
            self._pages["run"].get_workdir().rstrip("\\/")) or "任务输入") + ".json"
        path = filedialog.asksaveasfilename(
            parent=self, title="保存任务 JSON",
            initialdir=str(self.default_work_root()), initialfile=default_name,
            defaultextension=".json",
            filetypes=[("任务输入 JSON", "*.json"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(inp.to_dict(), fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            widgets.show_error(self, "保存失败", "无法写入 %s：%s" % (path, exc))
            return
        self.set_status("任务已保存：%s" % path, "ok")

    # ------------------------------------------------------------------ #
    # 设置
    # ------------------------------------------------------------------ #
    def set_work_root(self) -> None:
        path = filedialog.askdirectory(parent=self, title="选择工作区根目录",
                                       initialdir=str(self.default_work_root()))
        if not path:
            return
        P.save_settings({"work_root": path})
        self._refresh_workroot_label()
        self.set_status("工作区根目录已设为：%s" % path, "ok")

    def set_dem_dir(self) -> None:
        path = filedialog.askdirectory(parent=self, title="选择 DEM 瓦片目录",
                                       initialdir=str(P.dem_dir()))
        if not path:
            return
        P.save_settings({"dem_dir": path})
        try:
            P.setup_env()
        except Exception as exc:  # noqa: BLE001
            self._log_error("更新 DEM 目录", exc)
        self.set_status("DEM 瓦片目录已设为：%s（下次运行生效）" % path, "ok")

    # ------------------------------------------------------------------ #
    # 离线高程导入
    # ------------------------------------------------------------------ #
    def import_dem_files(self) -> None:
        """选择本地 GeoTIFF / HGT / ZIP 高程文件并导入为「自定义高程源」。"""
        if getattr(self, "busy", False) or getattr(self, "running", False):
            widgets.show_info(self, "请稍候", "后台任务/计算尚未结束。")
            return
        paths = filedialog.askopenfilenames(
            parent=self, title="导入离线高程文件（GeoTIFF / HGT / HGT.GZ / ZIP）",
            filetypes=[("高程文件", "*.tif *.tiff *.hgt *.hgt.gz *.gz *.zip"),
                       ("GeoTIFF", "*.tif *.tiff"), ("SRTM HGT", "*.hgt *.hgt.gz"),
                       ("所有文件", "*.*")])
        if not paths:
            return
        self._set_busy(True, "正在导入离线高程文件…")
        self._pages["run"].set_step("导入离线高程文件…")
        app = self

        def worker():
            try:
                from app.core.dem_manager import DemManager
                dm = DemManager(str(P.dem_dir()), logger=app._qlog)
                app._qlog("DEM 目录：%s" % P.dem_dir())
                n_ok, failed = 0, []
                for p in paths:
                    app._qlog("导入：%s" % p)
                    try:
                        info = dm.import_dem(p, copy=False)
                        n_ok += int(info.get("ok") or 0)
                        for item in info.get("failed") or []:
                            failed.append("%s：%s" % (item.get("path"), item.get("error")))
                        for s in info.get("sources") or []:
                            app._qlog("  + %s（%s）" % (s.get("name") or s.get("id"),
                                                       s.get("bbox_text") or "已登记"))
                    except Exception as exc:  # noqa: BLE001
                        failed.append("%s：%s" % (p, exc))
                st = dm.status() or {}
                srcs = st.get("custom_sources") or []
                tiles = st.get("tiles") or []
                lines = ["已导入 %d 个文件；自定义高程源 %d 个；1″ HGT 瓦片 %d 片。"
                         % (n_ok, len(srcs), len(tiles))]
                for s in srcs[:6]:
                    lines.append("- %s：%s（%s ~ %s）" % (
                        s.get("name") or s.get("id"),
                        s.get("file") or "",
                        _bbox_text(s.get("bbox")),
                        s.get("res_text") or ""))
                if tiles:
                    lines.append("- HGT 瓦片：%s" % "、".join(str(t) for t in tiles[:8]))
                if failed:
                    lines.append("失败 %d 项：" % len(failed))
                    lines.extend("  ! " + x for x in failed[:8])
                lines.append("提示：导入后「准备 DEM 地形」将优先使用自定义源；"
                             "引用模式请勿移动原文件。")
                text = "\n".join(lines)
                app._post("dem", text=text, release=True)
                app._post("log", level=("warn" if failed else "info"), message=text)
            except Exception as exc:  # noqa: BLE001
                app._log_error("导入离线高程", exc)
                app._post("dem", text="导入失败：%s" % widgets.explain_error(exc),
                          release=True)
                app._post("fail", context="导入离线高程",
                          message=widgets.explain_error(exc))

        self._spawn(worker, "dem-import")

    def clear_dem_cache(self) -> None:
        dem = Path(P.dem_dir())
        if not widgets.ask_yes_no(
                self, "清空 DEM 缓存",
                "将删除 %s 下的瓦片与缓存文件（不影响内置粗 DEM）。\n继续？" % dem):
            return
        removed = 0
        errors = []
        for sub in ("gz", "hgt", "cache"):
            folder = dem / sub
            if not folder.is_dir():
                continue
            for entry in folder.iterdir():
                try:
                    if entry.is_dir():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
                    removed += 1
                except OSError as exc:
                    errors.append("%s：%s" % (entry.name, exc))
        for name in ("srtm_tiles.json", "dem_mode.json", "observer_dem.json"):
            try:
                (dem / name).unlink()
                removed += 1
            except OSError:
                pass
        for sub in ("gz", "hgt", "cache"):
            try:
                (dem / sub).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        text = "已删除 %d 个缓存项。" % removed
        if errors:
            text += "\n\n以下文件删除失败：\n" + "\n".join(errors[:8])
        widgets.show_info(self, "清空 DEM 缓存", text)
        self.set_status("DEM 缓存已清理：%s" % dem, "ok")

    # ------------------------------------------------------------------ #
    # 我的库（用户自定义箭型 / 轨道 / 时序模板）
    # ------------------------------------------------------------------ #
    def manage_user_rockets(self) -> None:
        """打开「我的箭型库」管理对话框。"""
        if getattr(self, "busy", False) or getattr(self, "running", False):
            widgets.show_info(self, "请稍候", "后台任务/计算尚未结束。")
            return

        def on_edit(rec: Dict[str, Any]) -> None:
            result = self.edit_rocket(dict(rec))
            if result:
                dlg.refresh()

        def on_new() -> None:
            from app.core import userdb as U
            rec = U.blank_rocket("我的新火箭", "medium_liquid")
            result = self.edit_rocket(rec)
            if result:
                dlg.refresh()

        def on_delete(rec: Dict[str, Any]) -> None:
            if not widgets.ask_yes_no(self, "删除箭型",
                                      "确定从「我的箭型库」删除：%s ？" % rec.get("name")):
                return
            try:
                from app.core import userdb as U
                U.delete_rocket(str(rec.get("id") or ""))
                dlg.refresh()
                self._pages["mission"]._ensure_db()
                self._pages["mission"]._update_intro()
                self.set_status("已删除自定义箭型：%s" % rec.get("name"), "ok")
            except Exception as exc:  # noqa: BLE001
                self._log_error("删除箭型", exc)

        dlg = _UserLibDialog(self, kind="rocket", on_edit=on_edit, on_new=on_new,
                             on_delete=on_delete)

    def manage_user_orbits(self) -> None:
        if getattr(self, "busy", False) or getattr(self, "running", False):
            widgets.show_info(self, "请稍候", "后台任务/计算尚未结束。")
            return

        def on_edit(rec: Dict[str, Any]) -> None:
            result = self.edit_orbit(dict(rec))
            if result:
                dlg.refresh()

        def on_new() -> None:
            rec = {"name": "我的新轨道", "alt_km": 500.0, "i_deg": 97.4, "e": 0.0,
                   "type": "SSO", "confidence": "高"}
            result = self.edit_orbit(rec)
            if result:
                dlg.refresh()

        def on_delete(rec: Dict[str, Any]) -> None:
            if not widgets.ask_yes_no(self, "删除轨道",
                                      "确定从「我的轨道库」删除：%s ？" % rec.get("name")):
                return
            try:
                from app.core import userdb as U
                U.delete_orbit(str(rec.get("id") or ""))
                dlg.refresh()
                self.set_status("已删除自定义轨道：%s" % rec.get("name"), "ok")
            except Exception as exc:  # noqa: BLE001
                self._log_error("删除轨道", exc)

        dlg = _UserLibDialog(self, kind="orbit", on_edit=on_edit, on_new=on_new,
                             on_delete=on_delete)

    def export_user_bundle(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self, title="导出我的数据包（箭型 + 轨道 + 时序模板）",
            initialdir=str(P.runtime_base()), initialfile="rocketkit_user_data.json",
            defaultextension=".json", filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            from app.core import userdb as U
            n = U.export_bundle(path)
            widgets.show_info(self, "导出完成", "已导出 %d 条自定义数据：\n%s" % (n, path))
            self.set_status("已导出用户数据包：%s（%d 条）" % (path, n), "ok")
        except Exception as exc:  # noqa: BLE001
            self._log_error("导出用户数据包", exc)
            widgets.show_error(self, "导出失败", widgets.explain_error(exc))

    def import_user_bundle(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="导入我的数据包",
            filetypes=[("JSON", "*.json"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            from app.core import userdb as U
            stats = U.import_bundle(path)
            msg = ("导入完成：箭型 %d 条、轨道 %d 条、时序模板 %d 条；跳过重复 %d 条。"
                   % (stats.get("rockets", 0), stats.get("orbits", 0),
                      stats.get("sequences", 0), stats.get("skipped", 0)))
            widgets.show_info(self, "导入完成", msg)
            self.set_status(msg, "ok")
            try:
                self._pages["mission"]._ensure_db()
                self._pages["mission"]._update_intro()
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            self._log_error("导入用户数据包", exc)
            widgets.show_error(self, "导入失败", widgets.explain_error(exc))

    # ------------------------------------------------------------------ #
    # 环境自检 / 帮助
    # ------------------------------------------------------------------ #
    def check_env(self) -> None:
        if getattr(self, "busy", False) or getattr(self, "running", False):
            return
        self._set_busy(True, "正在检查运行环境…")
        self._pages["run"].set_step("环境自检…")

        def worker():
            try:
                from app.core.runner import check_environment
                info = check_environment()
                self._post("env", info=info)
            except Exception as exc:  # noqa: BLE001
                self._log_error("环境自检", exc)
                self._post("fail", context="环境自检失败",
                           message=widgets.explain_error(exc))

        self._spawn(worker, "env-check")

    def show_help(self, topic: str = "", topic_key: str = "") -> None:
        """打开帮助中心（带当前页面的推荐主题）。"""
        try:
            from .dialog_help import show_help as _show
        except Exception as exc:  # noqa: BLE001
            self._log_error("加载帮助中心", exc)
            widgets.open_text_dialog(self, "使用说明", help_text.HELP_TEXT, 900, 660)
            return
        page = getattr(self, "_current", "mission")
        _show(self, topic=page, topic_key=topic_key or help_text.PAGE_TOPIC.get(page, ""))

    def open_docs(self) -> None:
        try:
            root = P.APP_ROOT / "文档"
            target = root if root.is_dir() else P.APP_ROOT
            ok, msg = widgets.open_path_ex(target)
            if not ok:
                widgets.show_error(self, "打开文档目录失败", msg)
            else:
                self.set_status(msg, "ok")
        except Exception as exc:  # noqa: BLE001
            widgets.show_error(self, "打开文档目录失败", widgets.explain_error(exc))

    def show_about(self) -> None:
        content = help_text.about_text(P.app_version(), frozen=bool(P.FROZEN))
        widgets.open_text_dialog(self, "关于", content, 700, 560)


# --------------------------------------------------------------------------- #
# 「我的库」管理对话框
# --------------------------------------------------------------------------- #
class _UserLibDialog(widgets.Dialog):
    """列出用户库条目（箭型/轨道），支持编辑、删除、导入导出。"""

    COLUMNS_ROCKET = [("name", "名称", 260, "w"), ("cls", "类别", 110, "w"),
                      ("mass", "起飞质量 t", 100, "e"), ("stages", "级数", 60, "center"),
                      ("conf", "置信度", 70, "center"), ("note", "说明", 360, "w")]
    COLUMNS_ORBIT = [("name", "名称", 240, "w"), ("alt", "高度 km", 90, "e"),
                     ("inc", "倾角 °", 80, "e"), ("e", "偏心率", 80, "e"),
                     ("ltan", "LTAN h", 80, "e"), ("note", "说明", 300, "w")]

    def __init__(self, parent, kind: str = "rocket", on_edit=None, on_new=None,
                 on_delete=None):
        self.kind = kind
        self._on_edit = on_edit
        self._on_new = on_new
        self._on_delete = on_delete
        self._items: List[Dict[str, Any]] = []
        super().__init__(parent,
                         title="我的箭型库" if kind == "rocket" else "我的轨道库",
                         subtitle=("这里是你自己定义的箭型（保存在 user_data\\rockets.json，"
                                   "由程序管理）。双击可编辑；同 id 会覆盖内置库条目。"
                                   if kind == "rocket" else
                                   "这里是你自己保存的参考轨道（user_data\\orbits.json）。"),
                         width=1000, height=560)

    # ---- 内容 ----
    def build_body(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)
        bar = ttk.Frame(parent)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(bar, text="新建…", style="Accent.TButton",
                   command=self._new).pack(side="left")
        ttk.Button(bar, text="编辑选中…", command=self._edit).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="删除选中", command=self._delete).pack(side="left",
                                                                  padx=(8, 0))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=12)
        ttk.Button(bar, text="导出数据包…", command=self._export).pack(side="left")
        ttk.Button(bar, text="导入数据包…", command=self._import).pack(side="left",
                                                                   padx=(8, 0))
        self.count_label = ttk.Label(bar, text="", style="Hint.TLabel")
        self.count_label.pack(side="left", padx=(16, 0))

        cols = (self.COLUMNS_ROCKET if self.kind == "rocket" else self.COLUMNS_ORBIT)
        container, self.tree = widgets.make_tree(parent, cols, height=13)
        container.grid(row=1, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", lambda e: self._edit())
        self.refresh()

    def _build_buttons(self) -> None:
        ttk.Button(self.button_bar, text="关闭", command=self.close).pack(side="right")

    # ---- 数据 ----
    def refresh(self) -> None:
        widgets.clear_tree(self.tree)
        try:
            from app.core import userdb as U
        except Exception as exc:  # noqa: BLE001
            widgets.write_error_log("读取用户库", exc)
            self._items = []
            self.count_label.configure(text="用户库不可用：%s" % widgets.explain_error(exc))
            return
        if self.kind == "rocket":
            self._items = list(U.list_rockets())
            for rec in self._items:
                veh = rec.get("vehicle") or {}
                self.tree.insert("", "end", iid=str(rec.get("id")), values=(
                    rec.get("name") or "—",
                    U.CLASS_KEY_CN.get(U.class_key_of(rec), "—"),
                    widgets.fmt_num((rec.get("liftoff_mass_kg") or 0) / 1000.0, 1),
                    len(veh.get("stages") or []),
                    rec.get("confidence") or "中",
                    str(rec.get("note") or "")[:60],
                ))
        else:
            self._items = list(U.list_orbits())
            for i, rec in enumerate(self._items, start=1):
                self.tree.insert("", "end", iid=str(i), values=(
                    rec.get("name") or "—",
                    widgets.fmt_num(rec.get("alt_km")),
                    widgets.fmt_num(rec.get("i_deg")),
                    widgets.fmt_num(rec.get("e"), 4),
                    widgets.fmt_num(rec.get("ltan_h")),
                    str(rec.get("note") or "")[:60],
                ))
        self.count_label.configure(text="共 %d 条" % len(self._items))

    def _selected(self) -> Optional[Dict[str, Any]]:
        sel = self.tree.selection()
        if not sel:
            widgets.show_info(self, "未选中", "请先选中一条记录。")
            return None
        if self.kind == "rocket":
            rid = sel[0]
            return next((r for r in self._items if str(r.get("id")) == rid), None)
        try:
            idx = int(sel[0]) - 1
        except ValueError:
            return None
        return self._items[idx] if 0 <= idx < len(self._items) else None

    def _new(self) -> None:
        if callable(self._on_new):
            self._on_new()

    def _edit(self) -> None:
        rec = self._selected()
        if rec and callable(self._on_edit):
            self._on_edit(rec)

    def _delete(self) -> None:
        rec = self._selected()
        if rec and callable(self._on_delete):
            self._on_delete(rec)

    def _export(self) -> None:
        try:
            from app.core import userdb as U
            path = filedialog.asksaveasfilename(
                parent=self, title="导出我的数据包",
                initialdir=str(U.user_dir()), initialfile="rocketkit_user_data.json",
                defaultextension=".json", filetypes=[("JSON", "*.json")])
            if not path:
                return
            n = U.export_bundle(path)
            widgets.show_info(self, "导出完成", "已导出 %d 条数据：\n%s" % (n, path))
        except Exception as exc:  # noqa: BLE001
            widgets.show_error(self, "导出失败", widgets.explain_error(exc))

    def _import(self) -> None:
        try:
            from app.core import userdb as U
            path = filedialog.askopenfilename(parent=self, title="导入我的数据包",
                                              filetypes=[("JSON", "*.json")])
            if not path:
                return
            stats = U.import_bundle(path)
            widgets.show_info(self, "导入完成",
                              "箭型 %d、轨道 %d、时序模板 %d；跳过 %d。"
                              % (stats.get("rockets", 0), stats.get("orbits", 0),
                                 stats.get("sequences", 0), stats.get("skipped", 0)))
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            widgets.show_error(self, "导入失败", widgets.explain_error(exc))


def _bbox_text(bbox) -> str:
    try:
        lat0, lat1, lon0, lon1 = [float(x) for x in bbox]
        return "%.2f~%.2f°N, %.2f~%.2f°E" % (lat0, lat1, lon0, lon1)
    except Exception:  # noqa: BLE001
        return ""
