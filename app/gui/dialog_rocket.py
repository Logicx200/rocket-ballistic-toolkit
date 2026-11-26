# -*- coding: utf-8 -*-
"""自定义箭型参数编辑器（v4）。

背景
----
v3 的自定义箭型必须手工编辑 ``app/data/rockets.json``（130 KB、28 条记录），
字段多、格式严格，门槛极高。v4 把内置库与用户库分开（:mod:`app.core.userdb`），
本模块提供图形化的箭型编辑器：

* 六个分页：基本信息 / 分级与助推器 / 整流罩与残骸 / 制导与俯仰剖面 /
  发射时序默认值 / 校验与预览；
* 顶部工具条支持「四类模板新建 / 从内置箭型载入 / 从我的箭型库载入 /
  导入 JSON / 导出 JSON」；
* 底部提供「仅用于本次任务 / 保存到我的箭型库 / 取消」三条出口。

退出时：

* ``self.result``：完整箭型记录（已 ``normalize_rocket``）；取消为 ``None``；
* ``self.saved``：是否已写入用户库；
* ``self.mode``：``"session"``（仅本次任务）或 ``"user_db"``（已保存）。

设计约束
--------
1. 只依赖标准库与项目内 ``app.core.userdb``、``app.gui.theme/widgets``；
2. **不改动任何既有文件**；缺少的控件在本文件里就地实现；
3. 记录一律按 ``.get()`` 容错读取，保存前再交给 ``userdb`` 归一化；
4. ``validate_rocket`` / ``rocket_metrics`` 只在初始化与手动刷新时调用。
"""
from __future__ import annotations

import contextlib
import copy
import json
import re
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk
from typing import (Any, Callable, Dict, Iterator, List, Optional, Sequence,
                    Tuple)

from app.core import userdb

from . import theme, widgets

#: 置信度可选值（与内置库习惯一致）
CONFIDENCE_LEVELS: Tuple[str, ...] = ("高", "中高", "中", "低")

#: 四类箭型的下拉显示顺序
CLASS_CHOICES: Tuple[Tuple[str, str], ...] = tuple(
    (key, userdb.CLASS_KEY_CN.get(key, key)) for key in userdb.CLASS_KEYS)

#: 新建级别时的初始占位值（用户随后会逐项修改）
_NEW_STAGE_TEMPLATE: Dict[str, Any] = {
    "name": "新一级",
    "dry_kg": 1000.0,
    "prop_kg": 10000.0,
    "pitch_end_deg": 0.0,
    "pitch_duration_s": 300.0,
    "cda_attached_m2": 5.0,
    "phases": [{"name": "主机",
                "thrusters": [{"n": 1, "thrust_vac_n": 50000.0,
                               "isp_vac_s": 300.0}]}],
}

#: 启用助推器时的默认参数（可继续编辑）
_DEFAULT_BOOSTER: Dict[str, Any] = {
    "count": 2,
    "dry_kg_each": 2000.0,
    "prop_kg_each": 20000.0,
    "jettison_met": None,
    "thrusters": [{"n": 1, "thrust_vac_n": 500000.0, "isp_vac_s": 280.0}],
}

#: 启用整流罩时的默认参数
_DEFAULT_FAIRING: Dict[str, Any] = {
    "mass_kg": 500.0,
    "halves": 2,
    "sep_met": None,
    "cda_m2": 10.0,
}

#: 折线俯仰剖面的默认三点（仅在勾选且表为空时使用）
_DEFAULT_PITCH_SCHEDULE: List[List[float]] = [[10.0, 89.0], [60.0, 60.0], [300.0, 5.0]]


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _safe_filename(name: str, fallback: str = "rocket") -> str:
    """把箭型名收拾成可用作文件名的字符串。"""
    text = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(name or "")).strip(" ._")
    return text[:60] or fallback


def _as_float(value: Any) -> Optional[float]:
    """容错转 float（失败返回 None）。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _as_int(value: Any, default: int = 1) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _num(value: Any, nd: int = 3) -> str:
    return widgets.fmt_num(value, nd)


def _put_optional(target: Dict[str, Any], key: str, value: Optional[float]) -> None:
    """把可空数值写回字典：空值删除键（表示交给自动）。"""
    if value is None:
        target.pop(key, None)
    else:
        target[key] = value


# --------------------------------------------------------------------------- #
# 通用选择框
# --------------------------------------------------------------------------- #
class _PickerDialog(tk.Toplevel):
    """带搜索的单项选择框（内置箭型 / 我的箭型库 / 导入包内的多条记录）。"""

    def __init__(self, parent: tk.Misc, title: str, items: Sequence[Dict[str, Any]],
                 label_of: Callable[[Dict[str, Any]], str],
                 subtitle: str = "") -> None:
        super().__init__(parent)
        self.result: Optional[Dict[str, Any]] = None
        self._items: List[Dict[str, Any]] = list(items)
        self._label_of = label_of
        self._shown: List[Dict[str, Any]] = []
        self._closing = False

        self.title(title)
        self.configure(background=theme.WINDOW_BG)
        self.transient(parent)
        self.resizable(True, True)

        body = ttk.Frame(self, padding=(14, 12))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)

        ttk.Label(body, text=title, style="PageTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w")
        if subtitle:
            ttk.Label(body, text=subtitle, style="Subtitle.TLabel",
                      wraplength=420, justify="left").grid(
                row=1, column=0, columnspan=2, sticky="w", pady=(2, 8))
        search_box = ttk.Frame(body)
        search_box.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        search_box.columnconfigure(1, weight=1)
        ttk.Label(search_box, text="搜索", style="Hint.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 6))
        self.search_var = tk.StringVar()
        ttk.Entry(search_box, textvariable=self.search_var).grid(
            row=0, column=1, sticky="ew")
        self.search_var.trace_add("write", lambda *a: self._refresh())

        list_box = ttk.Frame(body)
        list_box.grid(row=3, column=0, columnspan=2, sticky="nsew")
        list_box.columnconfigure(0, weight=1)
        list_box.rowconfigure(0, weight=1)
        self.listbox = tk.Listbox(list_box, height=14, activestyle="none",
                                  relief="solid", borderwidth=1,
                                  background=theme.FIELD_BG,
                                  foreground=theme.FG,
                                  selectbackground=theme.SELECT_BG,
                                  selectforeground=theme.FG,
                                  highlightthickness=0,
                                  font=theme.font())
        vsb = ttk.Scrollbar(list_box, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=vsb.set)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self.listbox.bind("<Double-Button-1>", lambda e: self._ok())
        self.listbox.bind("<Return>", lambda e: self._ok())

        bar = ttk.Frame(body)
        bar.grid(row=4, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(bar, text="取消", command=self._cancel).pack(side="right")
        ttk.Button(bar, text="选择", style="Accent.TButton",
                   command=self._ok).pack(side="right", padx=(0, 8))

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda e: self._cancel())
        widgets.center_window(self, parent, 480, 520)
        self._refresh()
        self.after(60, self._grab)
        try:
            self.wait_window(self)
        except tk.TclError:
            pass
        try:
            parent.grab_set()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    def _grab(self) -> None:
        try:
            self.grab_set()
            self.search_var.set("")
            self.listbox.focus_set()
        except Exception:  # noqa: BLE001
            pass

    def _refresh(self) -> None:
        query = self.search_var.get().strip().lower()
        self.listbox.delete(0, "end")
        self._shown = []
        for item in self._items:
            label = self._label_of(item)
            if query and query not in label.lower():
                continue
            self._shown.append(item)
            self.listbox.insert("end", label)
        if self._shown:
            self.listbox.selection_set(0)
            self.listbox.activate(0)
        else:
            self.listbox.insert("end", "（没有匹配项）")

    def _ok(self) -> None:
        sel = self.listbox.curselection()
        if not sel or not self._shown:
            widgets.show_info(self.master, "未选择", "请先在列表中选择一项。")
            return
        idx = int(sel[0])
        if 0 <= idx < len(self._shown):
            self.result = self._shown[idx]
        self._closing = True
        self.destroy()

    def _cancel(self) -> None:
        self._closing = True
        self.destroy()


# --------------------------------------------------------------------------- #
# 推进剂组编辑器（多台发动机 / 多次点火共用）
# --------------------------------------------------------------------------- #
class _ThrusterTable(ttk.Frame):
    """推进剂组（thrusters）表格 + 编辑行。

    直接操作调用方传入的 ``list[dict]``（按引用），因此父级字典里的
    ``phases[...]["thrusters"]`` 或 ``boosters["thrusters"]`` 会同步变化。
    """

    def __init__(self, master: tk.Misc, *,
                 on_change: Optional[Callable[[], None]] = None,
                 height: int = 4) -> None:
        super().__init__(master, style="Panel.TFrame")
        self.on_change = on_change
        self.items: List[Dict[str, Any]] = []
        self._idx: Optional[int] = None
        self._enabled = True
        self._selecting = False
        self._loading = False
        self._entries: List[ttk.Entry] = []
        self._buttons: List[ttk.Button] = []

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        columns = [
            ("n", "台数", 55, "center"),
            ("tv", "真空推力 N", 110, "e"),
            ("iv", "真空比冲 s", 100, "e"),
            ("ts", "海平面推力 N", 110, "e"),
            ("is", "海平面比冲 s", 100, "e"),
        ]
        container, self.tree = widgets.make_tree(self, columns, height=height)
        container.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        form = ttk.Frame(self, style="Panel.TFrame")
        form.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.n_var = tk.StringVar()
        self.tv_var = tk.StringVar()
        self.iv_var = tk.StringVar()
        self.ts_var = tk.StringVar()
        self.is_var = tk.StringVar()
        specs: Sequence[Tuple[str, tk.StringVar, int]] = (
            ("台数", self.n_var, 6),
            ("真空推力 N", self.tv_var, 11),
            ("真空比冲 s", self.iv_var, 10),
            ("海平面推力 N", self.ts_var, 11),
            ("海平面比冲 s", self.is_var, 10),
        )
        for col, (label, var, width) in enumerate(specs):
            ttk.Label(form, text=label, style="CardHint.TLabel").grid(
                row=0, column=col, sticky="w", padx=(0, 8))
            entry = ttk.Entry(form, textvariable=var, width=width)
            entry.grid(row=1, column=col, sticky="w", padx=(0, 8), pady=(1, 0))
            self._entries.append(entry)
            var.trace_add("write", lambda *a: self._notify())

        bar = ttk.Frame(self, style="Panel.TFrame")
        bar.grid(row=2, column=0, sticky="w", pady=(6, 0))
        self._add_btn = ttk.Button(bar, text="添加推进剂组", style="Small.TButton",
                                   command=self._add)
        self._add_btn.pack(side="left")
        self._upd_btn = ttk.Button(bar, text="更新选中", style="Small.TButton",
                                   command=self._update)
        self._upd_btn.pack(side="left", padx=(6, 0))
        self._del_btn = ttk.Button(bar, text="删除选中", style="Small.TButton",
                                   command=self._delete)
        self._del_btn.pack(side="left", padx=(6, 0))
        self._buttons = [self._add_btn, self._upd_btn, self._del_btn]

        self.empty_label = ttk.Label(
            self, text="（没有推进剂组：该级/助推器将按上面的单机字段计算）",
            style="CardHint.TLabel")
        self.empty_label.grid(row=3, column=0, sticky="w", pady=(4, 0))
        self._refresh()

    # ------------------------------------------------------------------ #
    def _notify(self) -> None:
        if self._loading:
            return
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001 - 回调失败不影响编辑
                pass

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        state = "normal" if enabled else "disabled"
        for widget in list(self._entries) + list(self._buttons):
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass
        try:
            self.empty_label.configure(
                text=("（没有推进剂组：该级/助推器将按上面的单机字段计算）"
                      if enabled else "（未启用：此表不参与计算）"))
        except tk.TclError:
            pass

    def load(self, items: Optional[List[Dict[str, Any]]]) -> None:
        """载入列表（按引用保存，之后 add/delete 会同步回父级）。"""
        self.items = items if isinstance(items, list) else []
        self._idx = 0 if self.items else None
        self._refresh()
        self._load_form()

    def flush_selected(self) -> None:
        """把当前表单写回选中行（供切换页/保存前调用）；非法输入抛 ValueError。"""
        if not self._enabled:
            return
        idx = self._idx
        if idx is None or not (0 <= idx < len(self.items)):
            return
        self._apply(idx, notify=False)

    # ------------------------------------------------------------------ #
    def _apply(self, idx: int, *, notify: bool) -> None:
        item = self.items[idx]
        n = widgets.parse_int(self.n_var.get(), "推进剂组 台数", default=1,
                              minimum=1, maximum=20)
        tv = widgets.parse_optional_float(self.tv_var.get(), "推进剂组 真空推力",
                                          *userdb.FIELD_LIMITS["thrust_vac_n"])
        iv = widgets.parse_optional_float(self.iv_var.get(), "推进剂组 真空比冲",
                                          *userdb.FIELD_LIMITS["isp_vac_s"])
        ts = widgets.parse_optional_float(self.ts_var.get(), "推进剂组 海平面推力",
                                          *userdb.FIELD_LIMITS["thrust_sl_n"])
        isl = widgets.parse_optional_float(self.is_var.get(), "推进剂组 海平面比冲",
                                           *userdb.FIELD_LIMITS["isp_sl_s"])
        item["n"] = n
        item["thrust_vac_n"] = 0.0 if tv is None else tv
        item["isp_vac_s"] = 0.0 if iv is None else iv
        _put_optional(item, "thrust_sl_n", ts)
        _put_optional(item, "isp_sl_s", isl)
        if notify:
            self._notify()

    def _add(self) -> None:
        if not self._enabled:
            return
        try:
            n = widgets.parse_int(self.n_var.get(), "推进剂组 台数", default=1,
                                  minimum=1, maximum=20)
            tv = widgets.parse_optional_float(
                self.tv_var.get(), "推进剂组 真空推力",
                *userdb.FIELD_LIMITS["thrust_vac_n"])
            iv = widgets.parse_optional_float(
                self.iv_var.get(), "推进剂组 真空比冲",
                *userdb.FIELD_LIMITS["isp_vac_s"])
            ts = widgets.parse_optional_float(
                self.ts_var.get(), "推进剂组 海平面推力",
                *userdb.FIELD_LIMITS["thrust_sl_n"])
            isl = widgets.parse_optional_float(
                self.is_var.get(), "推进剂组 海平面比冲",
                *userdb.FIELD_LIMITS["isp_sl_s"])
        except ValueError as exc:
            widgets.show_error(self, "推进剂组参数有误", str(exc))
            return
        item: Dict[str, Any] = {"n": n,
                                "thrust_vac_n": 0.0 if tv is None else tv,
                                "isp_vac_s": 0.0 if iv is None else iv}
        _put_optional(item, "thrust_sl_n", ts)
        _put_optional(item, "isp_sl_s", isl)
        self.items.append(item)
        self._idx = len(self.items) - 1
        self._refresh()
        self._notify()

    def _update(self) -> None:
        idx = self._idx
        if idx is None or not (0 <= idx < len(self.items)):
            widgets.show_info(self, "未选中", "请先在推进剂组表格选择一行。")
            return
        try:
            self._apply(idx, notify=True)
        except ValueError as exc:
            widgets.show_error(self, "推进剂组参数有误", str(exc))
            return
        self._refresh()

    def _delete(self) -> None:
        idx = self._idx
        if idx is None or not (0 <= idx < len(self.items)):
            widgets.show_info(self, "未选中", "请先在推进剂组表格选择一行。")
            return
        self.items.pop(idx)
        self._idx = min(idx, len(self.items) - 1) if self.items else None
        self._refresh()
        self._notify()

    def _on_select(self, _event: Any = None) -> None:
        if self._selecting or not self._enabled:
            return
        try:
            idx = widgets.selected_index(self.tree)
        except tk.TclError:
            return
        if idx is None:
            return
        self._idx = idx
        self._load_form()

    def _refresh(self) -> None:
        widgets.clear_tree(self.tree)
        for i, item in enumerate(self.items):
            values = (
                _num(item.get("n"), 0),
                _num(item.get("thrust_vac_n"), 0),
                _num(item.get("isp_vac_s"), 1),
                _num(item.get("thrust_sl_n"), 0),
                _num(item.get("isp_sl_s"), 1),
            )
            self.tree.insert("", "end", iid=str(i), values=values,
                             tags=(("alt",) if i % 2 else ()))
        if self.items:
            self.empty_label.grid_remove()
        else:
            self.empty_label.grid()
        if self._idx is not None and 0 <= self._idx < len(self.items):
            self._select(self._idx)

    def _select(self, idx: int) -> None:
        self._selecting = True
        try:
            self.tree.selection_set(str(idx))
            self.tree.see(str(idx))
        except tk.TclError:
            pass
        finally:
            self._selecting = False
        self._idx = idx
        self._load_form()

    def _load_form(self) -> None:
        idx = self._idx
        if idx is None or not (0 <= idx < len(self.items)):
            return
        item = self.items[idx]
        self._loading = True
        try:
            self.n_var.set("%d" % _as_int(item.get("n"), 1))
            for var, key, nd in ((self.tv_var, "thrust_vac_n", 3),
                                 (self.iv_var, "isp_vac_s", 2),
                                 (self.ts_var, "thrust_sl_n", 3),
                                 (self.is_var, "isp_sl_s", 2)):
                var.set(_num(item.get(key), nd))
        finally:
            self._loading = False


# --------------------------------------------------------------------------- #
# 箭型编辑器
# --------------------------------------------------------------------------- #
class RocketEditorDialog(widgets.Dialog):
    """自定义箭型参数编辑器（阻塞式）。

    Parameters
    ----------
    parent : tk.Misc
        父窗口。
    rocket : dict | None
        要编辑的箭型；``None`` 时新建一个中型液体模板。
    title / allow_session / allow_save
        标题与底部按钮开关。
    """

    def __init__(self, parent: tk.Misc, rocket: Optional[Dict[str, Any]] = None, *,
                 title: str = "箭型参数编辑器",
                 allow_session: bool = True, allow_save: bool = True) -> None:
        # 这些属性必须在 Dialog.__init__（内部 wait_window）之前就位
        self.result: Optional[Dict[str, Any]] = None
        self.saved: bool = False
        self.mode: str = "session"
        self.allow_session = bool(allow_session)
        self.allow_save = bool(allow_save)

        self._loading = 0
        self._dirty = False
        self._built = False
        self._closing = False
        self._selecting = False
        self._active_page = 0

        self._stage_idx: Optional[int] = None
        self._phase_idx: Optional[int] = None
        self._orb_idx: Optional[int] = None
        self._debris_idx: Optional[str] = None
        self._sched_idx: Optional[int] = None

        self._from_library = False
        self._class_at_load = ""
        self._shadow_builtin_id = ""
        self._shadow_builtin_name = ""
        self._fairing_backup: Optional[Dict[str, Any]] = None
        self._booster_backup: Optional[Dict[str, Any]] = None
        self._seq_rows: List[Dict[str, Any]] = []
        self._schedule_list: List[List[float]] = []
        self._preview_ready = False
        self._preview_stale = False
        self._preview: Optional[Tuple[Dict[str, Any], List[Tuple[str, str, str]],
                                      Dict[str, Any]]] = None

        source = copy.deepcopy(rocket) if isinstance(rocket, dict) else None
        source_is_user = bool(source and source.get("_user") is True)
        if source is None:
            source = userdb.blank_rocket()
        self.rec = userdb.normalize_rocket(source)
        self.rec.pop("_shadow_builtin", None)
        lib_ids = {str(it.get("id")) for it in userdb.list_rockets()}
        self._from_library = source_is_user and str(self.rec.get("id") or "") in lib_ids
        self._detect_builtin_shadow()
        self._class_at_load = str(self.rec.get("class_key") or "")
        veh = self.rec.get("vehicle") or {}
        if isinstance(veh.get("fairing"), dict) and veh["fairing"]:
            self._fairing_backup = copy.deepcopy(veh["fairing"])
        if isinstance(veh.get("boosters"), dict) and veh["boosters"]:
            self._booster_backup = copy.deepcopy(veh["boosters"])

        super().__init__(
            parent, title=title,
            subtitle="在用户库里编辑自定义箭型；标「留空 = 自动」的字段可以不管。"
                     "「仅用于本次任务」不会写入用户库。从内置箭型载入后若名称不改，"
                     "保存会用同 id 覆盖内置（只写用户库，不改内置文件）。",
            width=1080, height=760, resizable=True)

    # ------------------------------------------------------------------ #
    # 运行期小工具
    # ------------------------------------------------------------------ #
    def _detect_builtin_shadow(self) -> None:
        """识别「当前记录就是内置箭型」：保存时同 id 写入用户库即可覆盖内置。

        这样从内置箭型载入、改完参数但**不改名**时，用户库里不会出现一条同名不同
        id 的记录（主窗口按名称匹配时会被内置记录抢先匹配到）；一旦改名，则按新
        箭型保存为新记录。
        """
        self._shadow_builtin_id = ""
        self._shadow_builtin_name = ""
        if self._from_library:
            return
        rid = str(self.rec.get("id") or "")
        if not rid:
            return
        base = userdb.get_builtin_rocket(rid)
        if base is not None:
            self._shadow_builtin_id = rid
            self._shadow_builtin_name = str(base.get("name") or "")

    @contextlib.contextmanager
    def _quiet(self) -> Iterator[None]:
        """载入数据时屏蔽“已修改”标记。"""
        self._loading += 1
        try:
            yield
        finally:
            self._loading = max(0, self._loading - 1)

    def _var(self, value: Any = "") -> tk.StringVar:
        var = tk.StringVar(value="" if value is None else str(value))
        var.trace_add("write", self._on_var_edited)
        return var

    def _bool_var(self, value: bool = False) -> tk.BooleanVar:
        var = tk.BooleanVar(value=bool(value))
        var.trace_add("write", self._on_var_edited)
        return var

    def _on_var_edited(self, *_args: Any) -> None:
        self._mark_dirty()

    def _mark_dirty(self) -> None:
        if self._loading or not self._built:
            return
        self._dirty = True
        self._update_badges()
        if self._preview_ready:
            self._set_preview_stale(True)

    def _watch_text(self, box: widgets.TextBox) -> None:
        """让多行文本框的修改也进入“未保存”判定。"""
        def _on_modified(_event: Any = None) -> None:
            try:
                if not box.text.edit_modified():
                    return
                box.text.edit_modified(False)
            except tk.TclError:
                return
            self._mark_dirty()
        try:
            box.text.bind("<<Modified>>", _on_modified, add="+")
            box.text.edit_modified(False)
        except tk.TclError:
            pass

    def _update_badges(self) -> None:
        if not hasattr(self, "dirty_badge"):
            return
        try:
            if self._dirty:
                self.dirty_badge.configure(text="● 有未保存修改", style="BadgeWarn.TLabel")
            else:
                self.dirty_badge.configure(text="未修改", style="BadgeMuted.TLabel")
            if hasattr(self, "source_badge"):
                if self._from_library:
                    self.source_badge.configure(text="来自我的箭型库", style="Badge.TLabel")
                elif self._shadow_builtin_id:
                    self.source_badge.configure(text="内置箭型副本（未保存）",
                                                style="BadgeMuted.TLabel")
                else:
                    self.source_badge.configure(text="新箭型（未保存）",
                                                style="BadgeMuted.TLabel")
        except tk.TclError:
            pass

    def _opt(self, var: tk.StringVar, label: str,
             limits: Optional[Tuple[float, float]] = None) -> Optional[float]:
        low, high = limits if limits else (None, None)
        return widgets.parse_optional_float(var.get(), label, low, high)

    def _req(self, var: tk.StringVar, label: str,
             limits: Optional[Tuple[float, float]] = None,
             *, positive: bool = False) -> float:
        value = self._opt(var, label, limits)
        if value is None:
            raise ValueError("「%s」不能为空" % label)
        if positive and value <= 0:
            raise ValueError("「%s」必须大于 0" % label)
        return value

    def _form_field(self, master: tk.Misc, row: int, label: str,
                    var: tk.StringVar, *, width: int = 14, hint: str = "",
                    label_width: int = 12, column: int = 0, kind: str = "entry",
                    values: Optional[Sequence[str]] = None) -> ttk.Widget:
        """在网格里放一行「标签 + 控件 + 提示」。

        ``widgets.field`` 要求控件先以容器为父再 pack 到子框里，在已经有
        grid 子控件的卡片里会触发 Tk 的「pack inside ... already has slaves
        managed by grid」错误；这里直接把控件建在子框里，避开这个问题。
        """
        ttk.Label(master, text=label, style="Panel.TLabel", width=label_width,
                  anchor="e").grid(row=row, column=column, sticky="e",
                                   padx=(0, 10), pady=5)
        box = ttk.Frame(master, style="Panel.TFrame")
        box.grid(row=row, column=column + 1, sticky="ew", pady=5)
        if kind == "combo":
            widget = ttk.Combobox(box, textvariable=var, width=width,
                                  state="readonly", values=list(values or []))
        elif kind == "frame":
            widget = ttk.Frame(box, style="Panel.TFrame")
        else:
            widget = ttk.Entry(box, textvariable=var, width=width)
        widget.pack(side="left")
        if hint:
            ttk.Label(box, text=hint, style="CardHint.TLabel").pack(
                side="left", padx=(10, 0))
        return widget

    # ------------------------------------------------------------------ #
    # 构建：工具条 + 分页
    # ------------------------------------------------------------------ #
    def build_body(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=0)
        parent.rowconfigure(1, weight=1)
        self._build_toolbar(parent)

        self.nb = ttk.Notebook(parent)
        self.nb.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        builders: Sequence[Tuple[str, Callable[[widgets.ScrollablePage], None]]] = (
            ("① 基本信息", self._build_page_basic),
            ("② 分级与助推器", self._build_page_stages),
            ("③ 整流罩与残骸", self._build_page_aero),
            ("④ 制导与俯仰剖面", self._build_page_guidance),
            ("⑤ 发射时序默认值", self._build_page_sequence),
            ("⑥ 校验与预览", self._build_page_preview),
        )
        self.pages: List[widgets.ScrollablePage] = []
        for label, builder in builders:
            page = widgets.ScrollablePage(self.nb)
            self.nb.add(page, text=label)
            builder(page)
            self.pages.append(page)

        self._load_all()
        self._built = True
        try:
            self.nb.select(0)
        except tk.TclError:
            pass
        self._active_page = 0
        self._update_badges()
        self._set_preview_stale(False)
        self._refresh_validation(silent=False)

    def _build_toolbar(self, parent: ttk.Frame) -> None:
        bar = ttk.Frame(parent, style="Panel.TFrame", padding=(10, 8))
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(9, weight=1)

        ttk.Label(bar, text="新建模板", style="Panel.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 6))
        self.new_class_var = tk.StringVar(value=userdb.CLASS_KEY_CN["medium_liquid"])
        self.new_class_combo = ttk.Combobox(
            bar, textvariable=self.new_class_var, state="readonly", width=11,
            values=[label for _key, label in CLASS_CHOICES])
        self.new_class_combo.grid(row=0, column=1, sticky="w", padx=(0, 6))
        ttk.Button(bar, text="新建", style="Small.TButton",
                   command=self._on_new_template).grid(row=0, column=2,
                                                       sticky="w", padx=(0, 8))

        ttk.Separator(bar, orient="vertical").grid(row=0, column=3, sticky="ns",
                                                    padx=(0, 8), pady=2)
        ttk.Button(bar, text="从内置箭型载入…", style="Small.TButton",
                   command=self._on_load_builtin).grid(row=0, column=4,
                                                       sticky="w", padx=(0, 6))
        ttk.Button(bar, text="从我的箭型库载入…", style="Small.TButton",
                   command=self._on_load_user).grid(row=0, column=5,
                                                    sticky="w", padx=(0, 6))
        ttk.Button(bar, text="导入 JSON…", style="Small.TButton",
                   command=self._on_import_json).grid(row=0, column=6,
                                                      sticky="w", padx=(0, 6))
        ttk.Button(bar, text="导出 JSON…", style="Small.TButton",
                   command=self._on_export_json).grid(row=0, column=7,
                                                      sticky="w", padx=(0, 8))

        self.source_badge = widgets.badge(bar, "新箭型（未保存）", "BadgeMuted")
        self.source_badge.grid(row=0, column=8, sticky="w", padx=(0, 6))
        self.dirty_badge = widgets.badge(bar, "未修改", "BadgeMuted")
        self.dirty_badge.grid(row=0, column=10, sticky="e")

    # ------------------------------------------------------------------ #
    # 页1：基本信息
    # ------------------------------------------------------------------ #
    def _build_page_basic(self, page: widgets.ScrollablePage) -> None:
        body = page.body
        body.columnconfigure(0, weight=1)

        card = widgets.Card(body, title="基本信息",
                            hint_text="名称用于在「任务与箭型」「轨道与时序」里识别；"
                                      "类别决定缺省量级锚点，切换类别不会重排已有数据。")
        card.pack(fill="x", padx=10, pady=(0, 8))
        card.body.columnconfigure(1, weight=1)

        self.name_var = self._var()
        self._form_field(card.body, 0, "名称", self.name_var, width=44,
                         hint="例：我的两级液体火箭")
        self.aliases_var = self._var()
        self._form_field(card.body, 1, "别名", self.aliases_var, width=64,
                         hint="逗号分隔，供任务文本自动匹配")

        self.class_var = self._var()
        class_box = self._form_field(card.body, 2, "类别", self.class_var,
                                     kind="frame", label_width=12)
        for key, label in CLASS_CHOICES:
            ttk.Radiobutton(class_box, text=label, value=key,
                            variable=self.class_var, style="Panel.TRadiobutton").pack(
                side="left", padx=(0, 14))

        self.confidence_var = self._var()
        self.confidence_combo = self._form_field(
            card.body, 3, "置信度", self.confidence_var, width=8, kind="combo",
            values=CONFIDENCE_LEVELS, hint="高 / 中高 / 中 / 低")

        self.default_payload_var = self._var()
        self._form_field(card.body, 4, "默认载荷 kg", self.default_payload_var,
                         width=16, hint="任务页首次打开时使用")
        self.leo_var = self._var()
        self._form_field(card.body, 5, "LEO 运力 kg", self.leo_var, width=16,
                         hint="只作参考，不直接参与仿真")
        self.sso_var = self._var()
        self._form_field(card.body, 6, "SSO 运力 kg", self.sso_var, width=16,
                         hint="只作参考")
        self.liftoff_var = self._var()
        self._form_field(card.body, 7, "起飞质量 kg", self.liftoff_var, width=16,
                         hint="与分级求和差异 >15% 时校验会给提示")

        ttk.Label(card.body, text="说明", style="Panel.TLabel", width=12,
                  anchor="e").grid(row=8, column=0, sticky="ne", padx=(0, 10), pady=5)
        self._note_wrap = ttk.Frame(card.body, style="Panel.TFrame")
        self._note_wrap.grid(row=8, column=1, sticky="ew", pady=5)
        self._note_wrap.columnconfigure(0, weight=1)
        self.note_box = widgets.TextBox(self._note_wrap, height=4, width=72,
                                        wrap="word")
        self.note_box.grid(row=0, column=0, sticky="ew")
        self._watch_text(self.note_box)

        common = widgets.Card(
            body, title="箭体公共参数",
            hint_text="适配器与本次载荷计入起飞质量；上升段 cda 系数用于气动阻力缩放。")
        common.pack(fill="x", padx=10, pady=(0, 8))
        common.body.columnconfigure(1, weight=1)
        common.body.columnconfigure(3, weight=1)
        self.adapter_var = self._var()
        self._form_field(common.body, 0, "适配器 kg", self.adapter_var, width=14,
                         label_width=10, column=0)
        self.payload_var = self._var()
        self._form_field(common.body, 0, "本次载荷 kg", self.payload_var, width=14,
                         label_width=10, column=2)
        self.cda_scale_var = self._var()
        self._form_field(common.body, 1, "cda 系数", self.cda_scale_var, width=14,
                         hint="0.1~5.0", label_width=10, column=0)

        orbits = widgets.Card(body, title="典型轨道",
                              hint_text="箭型常用的入轨目标（名称/高度/倾角）。"
                                        "任务未给轨道时，推断会优先参考这里。")
        orbits.pack(fill="x", padx=10, pady=(0, 10))
        orbits.body.columnconfigure(0, weight=1)
        container, self.typical_tree = widgets.make_tree(
            orbits.body,
            [("name", "名称", 260, "w"), ("alt", "高度 km", 100, "e"),
             ("inc", "倾角 °", 100, "e")],
            height=4)
        container.grid(row=0, column=0, sticky="nsew")
        self.typical_tree.bind("<<TreeviewSelect>>", self._on_typical_select)

        orbit_form = ttk.Frame(orbits.body, style="Panel.TFrame")
        orbit_form.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.orb_name_var = self._var()
        self.orb_alt_var = self._var()
        self.orb_inc_var = self._var()
        ttk.Label(orbit_form, text="名称", style="CardHint.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Entry(orbit_form, textvariable=self.orb_name_var, width=24).grid(
            row=1, column=0, sticky="w", padx=(0, 10))
        ttk.Label(orbit_form, text="高度 km", style="CardHint.TLabel").grid(
            row=0, column=1, sticky="w")
        ttk.Entry(orbit_form, textvariable=self.orb_alt_var, width=10).grid(
            row=1, column=1, sticky="w", padx=(0, 10))
        ttk.Label(orbit_form, text="倾角 °", style="CardHint.TLabel").grid(
            row=0, column=2, sticky="w")
        ttk.Entry(orbit_form, textvariable=self.orb_inc_var, width=10).grid(
            row=1, column=2, sticky="w", padx=(0, 12))
        ttk.Button(orbit_form, text="添加", style="Small.TButton",
                   command=self._on_typical_add).grid(row=1, column=3, padx=(0, 4))
        ttk.Button(orbit_form, text="更新选中", style="Small.TButton",
                   command=self._on_typical_update).grid(row=1, column=4, padx=(0, 4))
        ttk.Button(orbit_form, text="删除选中", style="Small.TButton",
                   command=self._on_typical_delete).grid(row=1, column=5)

    # ------------------------------------------------------------------ #
    # 页2：分级与助推器
    # ------------------------------------------------------------------ #
    def _build_page_stages(self, page: widgets.ScrollablePage) -> None:
        body = page.body
        body.columnconfigure(0, weight=1)

        card = widgets.Card(body, title="分级列表",
                            hint_text="级按飞行顺序排列；删除只能从末级开始。"
                                      "每级可填「单机推力 + 台数」，也可用下面的 phases。")
        card.pack(fill="x", padx=10, pady=(0, 8))
        card.body.columnconfigure(0, weight=1)
        container, self.stage_tree = widgets.make_tree(
            card.body,
            [("name", "级名", 90, "w"), ("dry", "结构 kg", 90, "e"),
             ("prop", "推进剂 kg", 100, "e"), ("eng", "台数", 55, "center"),
             ("tsl", "单机推力(海平面) N", 140, "e"),
             ("tvac", "单机推力(真空) N", 140, "e"),
             ("isp", "真空比冲 s", 95, "e"),
             ("pitch", "俯仰终角 °", 90, "e")],
            height=5)
        container.grid(row=0, column=0, sticky="nsew")
        self.stage_tree.bind("<<TreeviewSelect>>", self._on_stage_select)

        bar = ttk.Frame(card.body, style="Panel.TFrame")
        bar.grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Button(bar, text="新增级", style="Small.TButton",
                   command=self._on_add_stage).pack(side="left")
        ttk.Button(bar, text="删除末级", style="Small.TButton",
                   command=self._on_delete_stage).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="上移", style="Small.TButton",
                   command=lambda: self._move_stage(-1)).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="下移", style="Small.TButton",
                   command=lambda: self._move_stage(1)).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="应用本级修改（刷新列表）", style="Small.TButton",
                   command=self._apply_stage_form).pack(side="left", padx=(16, 0))

        edit = widgets.Card(
            body, title="选中级参数",
            hint_text="推力/比冲都是「单机」值；台数在仿真里相乘。"
                      "如果级里填了 phases，则本卡的推力/比冲不参与计算。")
        edit.pack(fill="x", padx=10, pady=(0, 8))
        edit.body.columnconfigure(1, weight=1)
        edit.body.columnconfigure(3, weight=1)
        self.stage_name_var = self._var()
        self.stage_dry_var = self._var()
        self.stage_prop_var = self._var()
        self.stage_engines_var = self._var()
        self.stage_tsl_var = self._var()
        self.stage_tvac_var = self._var()
        self.stage_isl_var = self._var()
        self.stage_ivac_var = self._var()
        self.stage_pitch_var = self._var()
        self.stage_cda_var = self._var()
        self.stage_pdur_var = self._var()
        rows: Sequence[Tuple[int, int, str, tk.StringVar, str, str]] = (
            (0, 0, "级名称", self.stage_name_var, "16", ""),
            (0, 2, "结构质量 kg", self.stage_dry_var, "14", ""),
            (1, 0, "推进剂 kg", self.stage_prop_var, "14", ""),
            (1, 2, "发动机台数", self.stage_engines_var, "10", "1~20"),
            (2, 0, "单机推力(海平面) N", self.stage_tsl_var, "14", ""),
            (2, 2, "单机推力(真空) N", self.stage_tvac_var, "14", ""),
            (3, 0, "海平面比冲 s", self.stage_isl_var, "14", ""),
            (3, 2, "真空比冲 s", self.stage_ivac_var, "14", ""),
            (4, 0, "俯仰终角 °", self.stage_pitch_var, "12", "-90~90"),
            (4, 2, "迎风面积 cda m²", self.stage_cda_var, "12", ""),
            (5, 0, "俯仰持续时间 s", self.stage_pdur_var, "12", "留空=自动"),
        )
        for row, col, label, var, width, hint_text in rows:
            self._form_field(edit.body, row, label, var, width=int(width),
                             hint=hint_text, label_width=15, column=col)

        phases = widgets.Card(
            body, title="多阶段 / 多次点火（phases）",
            hint_text="一台发动机多次点火、或一级内多组发动机各有关机时刻时用 phases。"
                      "它有数据时优先于上面的单机推力/比冲；首次添加会用单机字段生成一组。")
        phases.pack(fill="x", padx=10, pady=(0, 8))
        phases.body.columnconfigure(0, weight=1)
        head = ttk.Frame(phases.body, style="Panel.TFrame")
        head.grid(row=0, column=0, sticky="ew")
        self.phase_stage_label = ttk.Label(head, text="当前级：—",
                                           style="CardTitle.TLabel")
        self.phase_stage_label.pack(side="left")
        self.phase_warn = ttk.Label(head, text="", style="CardHint.TLabel",
                                    foreground=theme.WARN)
        self.phase_warn.pack(side="left", padx=(12, 0))

        container, self.phase_tree = widgets.make_tree(
            phases.body,
            [("name", "阶段名称", 110, "w"), ("end", "结束 MET s", 95, "e"),
             ("emit", "发事件", 65, "center"),
             ("thrust", "真空推力合计 N", 135, "e"),
             ("isp", "混合比冲 s", 105, "e")],
            height=4)
        container.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        self.phase_tree.bind("<<TreeviewSelect>>", self._on_phase_select)

        phase_bar = ttk.Frame(phases.body, style="Panel.TFrame")
        phase_bar.grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Button(phase_bar, text="添加阶段", style="Small.TButton",
                   command=self._on_add_phase).pack(side="left")
        ttk.Button(phase_bar, text="删除选中阶段", style="Small.TButton",
                   command=self._on_delete_phase).pack(side="left", padx=(6, 0))
        ttk.Button(phase_bar, text="应用阶段修改", style="Small.TButton",
                   command=self._apply_phase_form).pack(side="left", padx=(6, 0))

        phase_form = ttk.Frame(phases.body, style="Panel.TFrame")
        phase_form.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self.phase_name_var = self._var()
        self.phase_end_var = self._var()
        self.phase_emit_var = self._bool_var()
        ttk.Label(phase_form, text="阶段名称", style="CardHint.TLabel").grid(
            row=0, column=0, sticky="w")
        self.phase_name_entry = ttk.Entry(phase_form,
                                          textvariable=self.phase_name_var, width=16)
        self.phase_name_entry.grid(row=1, column=0, sticky="w", padx=(0, 10))
        ttk.Label(phase_form, text="结束 MET s", style="CardHint.TLabel").grid(
            row=0, column=1, sticky="w")
        self.phase_end_entry = ttk.Entry(phase_form,
                                         textvariable=self.phase_end_var, width=12)
        self.phase_end_entry.grid(row=1, column=1, sticky="w", padx=(0, 10))
        self.phase_emit_check = ttk.Checkbutton(
            phase_form, text="发事件（emit_event）", variable=self.phase_emit_var,
            style="Panel.TCheckbutton")
        self.phase_emit_check.grid(row=1, column=2, sticky="w", padx=(0, 12))
        self.phase_form_entries = [self.phase_name_entry, self.phase_end_entry]
        ttk.Label(phase_form, text="留空 = 该阶段按推进剂耗尽结束；结束 MET 用于多次点火。",
                  style="CardHint.TLabel").grid(row=1, column=3, sticky="w")

        thr_head = ttk.Label(phases.body, text="该阶段的推进剂组（thrusters）",
                             style="CardHint.TLabel")
        thr_head.grid(row=4, column=0, sticky="w", pady=(10, 2))
        self.phase_thrusters = _ThrusterTable(phases.body, height=4,
                                              on_change=self._mark_dirty)
        self.phase_thrusters.grid(row=5, column=0, sticky="ew")

        boost = widgets.Card(
            body, title="助推器（可选）",
            hint_text="助推器按「枚数 × 每枚一组推进剂」计算；thrusters 里的 n 是"
                      "每枚助推器的发动机台数（内置库里也有合计写法，按数据原有口径填）。")
        boost.pack(fill="x", padx=10, pady=(0, 10))
        boost.body.columnconfigure(1, weight=1)
        boost.body.columnconfigure(3, weight=1)
        self.boosters_var = self._bool_var()
        self.boosters_check = ttk.Checkbutton(
            boost.body, text="启用助推器", variable=self.boosters_var,
            style="Panel.TCheckbutton", command=self._on_boosters_toggle)
        self.boosters_check.grid(row=0, column=0, columnspan=4, sticky="w",
                                 pady=(0, 4))
        self.booster_count_var = self._var()
        self.booster_dry_var = self._var()
        self.booster_prop_var = self._var()
        self.booster_jett_var = self._var()
        self.booster_tvac_var = self._var()
        self.booster_ivac_var = self._var()
        booster_fields: Sequence[Tuple[int, int, str, tk.StringVar, str, str]] = (
            (1, 0, "数量", self.booster_count_var, "10", "1~8"),
            (1, 2, "单枚结构质量 kg", self.booster_dry_var, "14", ""),
            (2, 0, "单枚推进剂 kg", self.booster_prop_var, "14", ""),
            (2, 2, "分离时刻 s", self.booster_jett_var, "12", "留空=自动"),
            (3, 0, "备用单枚推力(真空) N", self.booster_tvac_var, "14",
             "仅无 thrusters 时使用"),
            (3, 2, "备用比冲 s", self.booster_ivac_var, "12", ""),
        )
        self._booster_entries: List[ttk.Entry] = []
        for row, col, label, var, width, hint_text in booster_fields:
            widget = self._form_field(boost.body, row, label, var,
                                      width=int(width), hint=hint_text,
                                      label_width=17, column=col)
            if isinstance(widget, ttk.Entry):
                self._booster_entries.append(widget)
        self.booster_thrusters = _ThrusterTable(boost.body, height=4,
                                                on_change=self._mark_dirty)
        self.booster_thrusters.grid(row=4, column=0, columnspan=4, sticky="ew",
                                    pady=(8, 0))

    # ------------------------------------------------------------------ #
    # 页3：整流罩与残骸
    # ------------------------------------------------------------------ #
    def _build_page_aero(self, page: widgets.ScrollablePage) -> None:
        body = page.body
        body.columnconfigure(0, weight=1)

        fair = widgets.Card(
            body, title="整流罩（可选）",
            hint_text="质量 ≤ 0 会被归一化当成「无整流罩」；分离时刻留空 = 按程序/自动。")
        fair.pack(fill="x", padx=10, pady=(0, 8))
        fair.body.columnconfigure(1, weight=1)
        fair.body.columnconfigure(3, weight=1)
        self.fairing_var = self._bool_var()
        self.fairing_check = ttk.Checkbutton(
            fair.body, text="启用整流罩", variable=self.fairing_var,
            style="Panel.TCheckbutton", command=self._on_fairing_toggle)
        self.fairing_check.grid(row=0, column=0, columnspan=4, sticky="w",
                                pady=(0, 4))
        self.fairing_mass_var = self._var()
        self.fairing_halves_var = self._var()
        self.fairing_sep_var = self._var()
        self.fairing_cda_var = self._var()
        fairing_fields: Sequence[Tuple[int, int, str, tk.StringVar, str, str]] = (
            (1, 0, "质量 kg", self.fairing_mass_var, "14", ""),
            (1, 2, "半罩数", self.fairing_halves_var, "10", "通常 2"),
            (2, 0, "分离时刻 s", self.fairing_sep_var, "12", "留空=自动"),
            (2, 2, "迎风面积 cda m²", self.fairing_cda_var, "12", ""),
        )
        self._fairing_entries: List[ttk.Entry] = []
        for row, col, label, var, width, hint_text in fairing_fields:
            widget = self._form_field(fair.body, row, label, var,
                                      width=int(width), hint=hint_text,
                                      label_width=13, column=col)
            if isinstance(widget, ttk.Entry):
                self._fairing_entries.append(widget)

        deb = widgets.Card(
            body, title="残骸气动参数",
            hint_text="对象行按当前级数/整流罩/助推器自动生成：stage_0..n-2 是"
                      "已抛掉的分级，末级与卫星不在此表。")
        deb.pack(fill="x", padx=10, pady=(0, 10))
        deb.body.columnconfigure(0, weight=1)
        container, self.debris_tree = widgets.make_tree(
            deb.body,
            [("obj", "对象", 210, "w"), ("cda", "迎风面积 cda m²", 130, "e"),
             ("extra", "附带质量 kg", 130, "e"),
             ("divert", "横向分离 divert m/s", 150, "e")],
            height=5)
        container.grid(row=0, column=0, sticky="nsew")
        self.debris_tree.bind("<<TreeviewSelect>>", self._on_debris_select)

        deb_form = ttk.Frame(deb.body, style="Panel.TFrame")
        deb_form.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.debris_cda_var = self._var()
        self.debris_extra_var = self._var()
        self.debris_divert_var = self._var()
        for col, (label, var, width) in enumerate((
                ("cda m²", self.debris_cda_var, 10),
                ("附带质量 kg", self.debris_extra_var, 12),
                ("divert m/s", self.debris_divert_var, 10))):
            ttk.Label(deb_form, text=label, style="CardHint.TLabel").grid(
                row=0, column=col, sticky="w")
            ttk.Entry(deb_form, textvariable=var, width=width).grid(
                row=1, column=col, sticky="w", padx=(0, 12))
        ttk.Button(deb_form, text="更新选中", style="Small.TButton",
                   command=self._on_debris_update).grid(row=1, column=3,
                                                        padx=(4, 4))
        ttk.Button(deb_form, text="恢复默认", style="Small.TButton",
                   command=self._on_debris_reset).grid(row=1, column=4)

    # ------------------------------------------------------------------ #
    # 页4：制导与俯仰剖面
    # ------------------------------------------------------------------ #
    def _build_page_guidance(self, page: widgets.ScrollablePage) -> None:
        body = page.body
        body.columnconfigure(0, weight=1)

        card = widgets.Card(
            body, title="制导默认值",
            hint_text="这里的值作为该箭型的默认制导参数，任务页可以覆盖。"
                      "留空 = 使用引擎/推断的默认值。")
        card.pack(fill="x", padx=10, pady=(0, 8))
        card.body.columnconfigure(1, weight=1)
        card.body.columnconfigure(3, weight=1)
        self.g_vars: Dict[str, tk.StringVar] = {}
        self.g_entries: Dict[str, ttk.Entry] = {}
        g_fields: Sequence[Tuple[str, str, str, str]] = (
            ("t_v", "t_v s", "12", "推力转向时刻"),
            ("t_p", "t_p s", "12", "俯仰程序时长"),
            ("pitch_power", "pitch_power", "12", "俯仰幂次 0.2~3"),
            ("t_fairing", "t_fairing s", "12", "抛罩时刻（制导用）"),
            ("fairing_divert_mps", "抛罩横向 m/s", "12", "0~50"),
            ("yaw_tau", "yaw_tau s", "12", "偏航时间常数"),
            ("yaw2_tau", "yaw2_tau s", "12", "二级偏航时间常数"),
            ("i_target_deg", "i_target °", "12", "末级目标倾角"),
            ("seco_band_km", "seco_band km", "12", "末级关机高度带"),
            ("seco_e_max", "seco_e_max", "12", "末级偏心率上限"),
        )
        for idx, (key, label, width, hint_text) in enumerate(g_fields):
            var = self._var()
            self.g_vars[key] = var
            row, col = divmod(idx, 2)
            entry = self._form_field(card.body, row, label, var,
                                     width=int(width), hint=hint_text,
                                     label_width=15, column=col * 2)
            self.g_entries[key] = entry

        sched = widgets.Card(
            body, title="折线俯仰剖面",
            hint_text="按「时间 s / 角度 °」给若干折点，线性插值。"
                      "启用后 t_v / t_p / pitch_power 对该箭型不起作用。")
        sched.pack(fill="x", padx=10, pady=(0, 10))
        sched.body.columnconfigure(0, weight=1)
        self.schedule_var = self._bool_var()
        self.schedule_check = ttk.Checkbutton(
            sched.body, text="使用折线剖面（替代 t_v / t_p / pitch_power）",
            variable=self.schedule_var, style="Panel.TCheckbutton",
            command=self._on_schedule_toggle)
        self.schedule_check.grid(row=0, column=0, sticky="w")
        self.schedule_warn = ttk.Label(sched.body, text="", style="CardHint.TLabel",
                                       justify="left", wraplength=980)
        self.schedule_warn.grid(row=1, column=0, sticky="w", pady=(2, 4))
        container, self.sched_tree = widgets.make_tree(
            sched.body,
            [("t", "时间 s", 110, "e"), ("ang", "角度 °", 110, "e")],
            height=5)
        container.grid(row=2, column=0, sticky="nsew")
        self.sched_tree.bind("<<TreeviewSelect>>", self._on_schedule_select)

        sched_form = ttk.Frame(sched.body, style="Panel.TFrame")
        sched_form.grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.sched_t_var = self._var()
        self.sched_a_var = self._var()
        ttk.Label(sched_form, text="时间 s", style="CardHint.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Entry(sched_form, textvariable=self.sched_t_var, width=10).grid(
            row=1, column=0, sticky="w", padx=(0, 8))
        ttk.Label(sched_form, text="角度 °", style="CardHint.TLabel").grid(
            row=0, column=1, sticky="w")
        ttk.Entry(sched_form, textvariable=self.sched_a_var, width=10).grid(
            row=1, column=1, sticky="w", padx=(0, 12))
        ttk.Button(sched_form, text="添加节点", style="Small.TButton",
                   command=self._on_schedule_add).grid(row=1, column=2, padx=(0, 4))
        ttk.Button(sched_form, text="更新选中", style="Small.TButton",
                   command=self._on_schedule_update).grid(row=1, column=3, padx=(0, 4))
        ttk.Button(sched_form, text="删除选中", style="Small.TButton",
                   command=self._on_schedule_delete).grid(row=1, column=4, padx=(0, 4))
        ttk.Button(sched_form, text="按时间排序", style="Small.TButton",
                   command=self._on_schedule_sort).grid(row=1, column=5)

    # ------------------------------------------------------------------ #
    # 页5：发射时序默认值
    # ------------------------------------------------------------------ #
    def _build_page_sequence(self, page: widgets.ScrollablePage) -> None:
        body = page.body
        body.columnconfigure(0, weight=1)
        card = widgets.Card(
            body, title="发射时序默认值",
            hint_text="单位秒（MET）。留空 = 按推进剂耗尽 / 程序自动。"
                      "这里填的是箭型默认值，任务页仍可以逐项覆盖。")
        card.pack(fill="x", padx=10, pady=(0, 8))
        card.body.columnconfigure(1, weight=1)
        self.seq_host = ttk.Frame(card.body, style="Panel.TFrame")
        self.seq_host.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.seq_host.columnconfigure(1, weight=1)
        self.seq_hint = ttk.Label(card.body, text="", style="CardHint.TLabel",
                                  justify="left", wraplength=980)
        self.seq_hint.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

    # ------------------------------------------------------------------ #
    # 页6：校验与预览
    # ------------------------------------------------------------------ #
    def _build_page_preview(self, page: widgets.ScrollablePage) -> None:
        body = page.body
        body.columnconfigure(0, weight=1)

        val = widgets.Card(body, title="校验结果",
                           hint_text="错误会阻止保存；提示只提醒复核，可以继续保存。")
        val.pack(fill="x", padx=10, pady=(0, 8))
        val.body.columnconfigure(0, weight=1)
        head = ttk.Frame(val.body, style="Panel.TFrame")
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(1, weight=1)
        self.val_badge = widgets.badge(head, "尚未校验", "BadgeMuted")
        self.val_badge.grid(row=0, column=0, sticky="w")
        self.stale_label = ttk.Label(head, text="", style="CardHint.TLabel",
                                     foreground=theme.WARN)
        self.stale_label.grid(row=0, column=1, sticky="w", padx=(12, 0))
        ttk.Button(head, text="重新校验", style="Small.TButton",
                   command=self._on_refresh_preview).grid(row=0, column=2,
                                                         sticky="e")
        self.val_box = widgets.TextBox(val.body, height=9, wrap="word",
                                       readonly=True)
        self.val_box.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        text = self.val_box.text
        text.tag_configure("err", foreground=theme.ERR)
        text.tag_configure("warn", foreground=theme.WARN)
        text.tag_configure("ok", foreground=theme.OK)
        text.tag_configure("muted", foreground=theme.FG_MUTED)
        text.tag_configure("title", foreground=theme.FG,
                           font=theme.font(theme.BASE_SIZE, True))

        prev = widgets.Card(body, title="性能预览",
                            hint_text="按分级质量/推力/比冲粗算（不含重力损失与气动损失），"
                                      "只用于发现量级问题。")
        prev.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        prev.body.columnconfigure(0, weight=1)
        prev.body.rowconfigure(0, weight=1)
        self.preview_box = widgets.TextBox(prev.body, height=14, wrap="word",
                                           readonly=True)
        self.preview_box.grid(row=0, column=0, sticky="nsew")
        text = self.preview_box.text
        text.tag_configure("title", foreground=theme.ACCENT,
                           font=theme.font(theme.BASE_SIZE, True))
        text.tag_configure("key", foreground=theme.FG_MUTED)
        text.tag_configure("warn", foreground=theme.WARN)
        text.tag_configure("ok", foreground=theme.OK)
        text.tag_configure("muted", foreground=theme.FG_MUTED)

    # ------------------------------------------------------------------ #
    # 载入 / 刷新
    # ------------------------------------------------------------------ #
    def _load_all(self) -> None:
        with self._quiet():
            self._load_basic()
            self._load_typical()
            self._stage_idx = 0 if self._stages() else None
            self._refresh_stage_tree()
            if self._stage_idx is not None:
                self._load_stage_form(self._stage_idx)
            self._load_booster()
            self._load_fairing()
            self._sync_debris_rows()
            self._refresh_debris_tree()
            self._load_guidance()
            self._load_schedule()
            self._sync_sequence_rows()
        self._update_badges()

    def _stages(self) -> List[Dict[str, Any]]:
        veh = self.rec.setdefault("vehicle", {})
        stages = veh.get("stages")
        if not isinstance(stages, list) or not stages:
            stages = [copy.deepcopy(_NEW_STAGE_TEMPLATE)]
            stages[0]["name"] = "一级"
            veh["stages"] = stages
        return stages

    def _current_stage(self) -> Optional[Dict[str, Any]]:
        stages = self._stages()
        idx = self._stage_idx
        if idx is None or not (0 <= idx < len(stages)):
            return None
        return stages[idx]

    def _current_phase(self) -> Optional[Dict[str, Any]]:
        stage = self._current_stage()
        if stage is None:
            return None
        phases = stage.get("phases")
        idx = self._phase_idx
        if not isinstance(phases, list) or idx is None or not (0 <= idx < len(phases)):
            return None
        return phases[idx]

    def _load_basic(self) -> None:
        self.name_var.set(str(self.rec.get("name") or ""))
        self.aliases_var.set("，".join(str(a) for a in (self.rec.get("aliases") or [])))
        key = userdb.class_key_of(self.rec)
        self._class_at_load = key
        self.class_var.set(key)
        confidence = str(self.rec.get("confidence") or "中")
        values = list(CONFIDENCE_LEVELS)
        if confidence not in values:
            values.append(confidence)
        self.confidence_combo.configure(values=values)
        self.confidence_var.set(confidence)
        for var, key_name in (
                (self.default_payload_var, "default_payload_kg"),
                (self.leo_var, "leo_kg"),
                (self.sso_var, "sso_kg"),
                (self.liftoff_var, "liftoff_mass_kg")):
            var.set(_num(self.rec.get(key_name), 3))
        self.note_box.set_text(str(self.rec.get("note") or ""))
        try:
            self.note_box.text.edit_modified(False)
        except tk.TclError:
            pass

        veh = self.rec.get("vehicle") or {}
        self.adapter_var.set(_num(veh.get("adapter_kg"), 3))
        self.payload_var.set(_num(veh.get("payload_kg"), 3))
        self.cda_scale_var.set(_num(veh.get("ascent_cda_scale"), 3))

    def _flush_basic(self) -> Dict[str, Any]:
        name = self.name_var.get().strip()
        if not name:
            raise ValueError("请填写箭型名称")
        aliases = [part.strip() for part in re.split(r"[,，、;；]", self.aliases_var.get())
                   if part.strip()]
        class_key = self.class_var.get().strip()
        if class_key not in userdb.CLASS_KEYS:
            class_key = userdb.class_key_of(self.rec)
        confidence = self.confidence_var.get().strip() or "中"

        default_payload = self._opt(self.default_payload_var, "默认载荷 kg",
                                    userdb.FIELD_LIMITS["default_payload_kg"])
        leo = self._opt(self.leo_var, "LEO 运力 kg", userdb.FIELD_LIMITS["leo_kg"])
        sso = self._opt(self.sso_var, "SSO 运力 kg", userdb.FIELD_LIMITS["sso_kg"])
        liftoff = self._opt(self.liftoff_var, "起飞质量 kg",
                            userdb.FIELD_LIMITS["liftoff_mass_kg"])

        veh = self.rec.setdefault("vehicle", {})
        adapter = self._opt(self.adapter_var, "适配器质量 kg",
                            userdb.FIELD_LIMITS["adapter_kg"])
        payload = self._opt(self.payload_var, "本次载荷 kg",
                            userdb.FIELD_LIMITS["default_payload_kg"])
        cda_scale = self._opt(self.cda_scale_var, "cda 系数",
                              userdb.FIELD_LIMITS["ascent_cda_scale"])

        self.rec["name"] = name
        self.rec["aliases"] = aliases
        self.rec["class_key"] = class_key
        if class_key != self._class_at_load:
            self.rec["class"] = "%s（用户自定义）" % userdb.CLASS_KEY_CN.get(
                class_key, "自定义")
            self._class_at_load = class_key
        else:
            self.rec["class"] = str(self.rec.get("class") or "").strip() or \
                "%s（用户自定义）" % userdb.CLASS_KEY_CN.get(class_key, "自定义")
        self.rec["confidence"] = confidence
        self.rec["default_payload_kg"] = default_payload
        self.rec["leo_kg"] = leo
        self.rec["sso_kg"] = sso
        self.rec["liftoff_mass_kg"] = liftoff
        self.rec["note"] = self.note_box.get_text().strip()
        veh["adapter_kg"] = 0.0 if adapter is None else adapter
        veh["payload_kg"] = 0.0 if payload is None else payload
        veh["ascent_cda_scale"] = 1.0 if cda_scale is None else cda_scale
        return self.rec

    # ---- 典型轨道 ----------------------------------------------------- #
    def _typical_rows(self) -> List[Dict[str, Any]]:
        rows = self.rec.get("typical_orbits")
        if not isinstance(rows, list):
            rows = []
            self.rec["typical_orbits"] = rows
        return rows

    def _load_typical(self) -> None:
        rows = self._typical_rows()
        self._refresh_typical_tree(0 if rows else None)
        self.orb_name_var.set("")
        self.orb_alt_var.set("")
        self.orb_inc_var.set("")
        if rows:
            self._load_typical_form(0)

    def _refresh_typical_tree(self, select: Optional[int] = None) -> None:
        widgets.clear_tree(self.typical_tree)
        for i, item in enumerate(self._typical_rows()):
            self.typical_tree.insert("", "end", iid=str(i), values=(
                str(item.get("name") or "参考轨道"),
                _num(item.get("alt_km"), 1),
                _num(item.get("i_deg"), 2),
            ), tags=(("alt",) if i % 2 else ()))
        self._selecting = True
        try:
            if select is not None and 0 <= select < len(self._typical_rows()):
                self.typical_tree.selection_set(str(select))
                self._orb_idx = select
            else:
                self._orb_idx = None
        except tk.TclError:
            self._orb_idx = None
        finally:
            self._selecting = False

    def _load_typical_form(self, idx: int) -> None:
        rows = self._typical_rows()
        if not (0 <= idx < len(rows)):
            return
        item = rows[idx]
        self._orb_idx = idx
        with self._quiet():
            self.orb_name_var.set(str(item.get("name") or ""))
            self.orb_alt_var.set(_num(item.get("alt_km"), 1))
            self.orb_inc_var.set(_num(item.get("i_deg"), 2))

    def _typical_form_values(self) -> Tuple[str, Optional[float], Optional[float]]:
        name = self.orb_name_var.get().strip() or "参考轨道"
        alt = self._opt(self.orb_alt_var, "典型轨道高度 km",
                        userdb.FIELD_LIMITS["alt_km"])
        inc = self._opt(self.orb_inc_var, "典型轨道倾角 °",
                        userdb.FIELD_LIMITS["i_deg"])
        return name, alt, inc

    def _flush_typical(self) -> None:
        idx = self._orb_idx
        rows = self._typical_rows()
        if idx is None or not (0 <= idx < len(rows)):
            return
        name, alt, inc = self._typical_form_values()
        if alt is None:
            raise ValueError("「典型轨道高度 km」不能为空")
        item = rows[idx]
        item["name"] = name
        item["alt_km"] = alt
        item["i_deg"] = inc

    def _on_typical_select(self, _event: Any = None) -> None:
        if self._selecting:
            return
        idx = widgets.selected_index(self.typical_tree)
        if idx is None:
            return
        if idx == self._orb_idx:
            return
        self._orb_idx = idx
        self._load_typical_form(idx)

    def _on_typical_add(self) -> None:
        try:
            name, alt, inc = self._typical_form_values()
            if alt is None:
                raise ValueError("「典型轨道高度 km」不能为空")
        except ValueError as exc:
            widgets.show_error(self, "典型轨道参数有误", str(exc))
            return
        rows = self._typical_rows()
        rows.append({"name": name, "alt_km": alt, "i_deg": inc})
        self._refresh_typical_tree(len(rows) - 1)
        self._load_typical_form(len(rows) - 1)
        self._mark_dirty()

    def _on_typical_update(self) -> None:
        if self._orb_idx is None:
            widgets.show_info(self, "未选中", "请先在典型轨道表格选择一行。")
            return
        try:
            self._flush_typical()
        except ValueError as exc:
            widgets.show_error(self, "典型轨道参数有误", str(exc))
            return
        self._refresh_typical_tree(self._orb_idx)
        self._load_typical_form(self._orb_idx)
        self._mark_dirty()

    def _on_typical_delete(self) -> None:
        idx = self._orb_idx
        rows = self._typical_rows()
        if idx is None or not (0 <= idx < len(rows)):
            widgets.show_info(self, "未选中", "请先在典型轨道表格选择一行。")
            return
        rows.pop(idx)
        new_idx = min(idx, len(rows) - 1) if rows else None
        self._refresh_typical_tree(new_idx)
        if new_idx is not None:
            self._load_typical_form(new_idx)
        else:
            with self._quiet():
                self.orb_name_var.set("")
                self.orb_alt_var.set("")
                self.orb_inc_var.set("")
        self._mark_dirty()

    # ---- 分级 --------------------------------------------------------- #
    def _refresh_stage_tree(self) -> None:
        stages = self._stages()
        widgets.clear_tree(self.stage_tree)
        for i, st in enumerate(stages):
            self.stage_tree.insert("", "end", iid=str(i), values=(
                str(st.get("name") or ("第%d级" % (i + 1))),
                _num(st.get("dry_kg"), 0),
                _num(st.get("prop_kg"), 0),
                _num(st.get("engines"), 0),
                _num(st.get("thrust_sl_n"), 0),
                _num(st.get("thrust_vac_n"), 0),
                _num(st.get("isp_vac_s"), 1),
                _num(st.get("pitch_end_deg"), 2),
            ), tags=(("alt",) if i % 2 else ()))
        idx = self._stage_idx
        self._selecting = True
        try:
            if idx is not None and 0 <= idx < len(stages):
                self.stage_tree.selection_set(str(idx))
                self.stage_tree.see(str(idx))
            else:
                self._stage_idx = None
        except tk.TclError:
            self._stage_idx = None
        finally:
            self._selecting = False

    def _load_stage_form(self, idx: int) -> None:
        stages = self._stages()
        if not (0 <= idx < len(stages)):
            return
        st = stages[idx]
        self._stage_idx = idx
        with self._quiet():
            self.stage_name_var.set(str(st.get("name") or ""))
            self.stage_dry_var.set(_num(st.get("dry_kg"), 3))
            self.stage_prop_var.set(_num(st.get("prop_kg"), 3))
            self.stage_engines_var.set(_num(st.get("engines"), 0))
            self.stage_tsl_var.set(_num(st.get("thrust_sl_n"), 3))
            self.stage_tvac_var.set(_num(st.get("thrust_vac_n"), 3))
            self.stage_isl_var.set(_num(st.get("isp_sl_s"), 2))
            self.stage_ivac_var.set(_num(st.get("isp_vac_s"), 2))
            self.stage_pitch_var.set(_num(st.get("pitch_end_deg"), 2))
            self.stage_cda_var.set(_num(st.get("cda_attached_m2"), 2))
            self.stage_pdur_var.set(_num(st.get("pitch_duration_s"), 2))
        self._load_phase_page()

    def _flush_stage_form(self) -> None:
        st = self._current_stage()
        if st is None:
            return
        idx = self._stage_idx or 0
        name = self.stage_name_var.get().strip() or ("第%d级" % (idx + 1))
        dry = self._opt(self.stage_dry_var, "结构质量 kg",
                        userdb.FIELD_LIMITS["dry_kg"])
        prop = self._opt(self.stage_prop_var, "推进剂质量 kg",
                         userdb.FIELD_LIMITS["prop_kg"])
        engines = widgets.parse_int(self.stage_engines_var.get(), "发动机台数",
                                    default=1, minimum=1, maximum=20)
        t_sl = self._opt(self.stage_tsl_var, "单机推力（海平面）N",
                         userdb.FIELD_LIMITS["thrust_sl_n"])
        t_vac = self._opt(self.stage_tvac_var, "单机推力（真空）N",
                          userdb.FIELD_LIMITS["thrust_vac_n"])
        isp_sl = self._opt(self.stage_isl_var, "海平面比冲 s",
                           userdb.FIELD_LIMITS["isp_sl_s"])
        isp_vac = self._opt(self.stage_ivac_var, "真空比冲 s",
                            userdb.FIELD_LIMITS["isp_vac_s"])
        pitch = self._opt(self.stage_pitch_var, "俯仰终角 °",
                          userdb.FIELD_LIMITS["pitch_end_deg"])
        cda = self._opt(self.stage_cda_var, "迎风面积 cda m²",
                        userdb.FIELD_LIMITS["cda_attached_m2"])
        pdur = self._opt(self.stage_pdur_var, "俯仰持续时间 s",
                         userdb.FIELD_LIMITS["pitch_duration_s"])
        st["name"] = name
        st["dry_kg"] = 0.0 if dry is None else dry
        st["prop_kg"] = 0.0 if prop is None else prop
        st["engines"] = engines
        _put_optional(st, "thrust_sl_n", t_sl)
        _put_optional(st, "thrust_vac_n", t_vac)
        _put_optional(st, "isp_sl_s", isp_sl)
        _put_optional(st, "isp_vac_s", isp_vac)
        st["pitch_end_deg"] = 0.0 if pitch is None else pitch
        _put_optional(st, "cda_attached_m2", cda)
        _put_optional(st, "pitch_duration_s", pdur)

    def _apply_stage_form(self) -> None:
        try:
            self._flush_stage_form()
            self._flush_phase_form()
            self._flush_phase_thrusters()
        except ValueError as exc:
            widgets.show_error(self, "参数有误", str(exc))
            return
        self._refresh_stage_tree()
        if self._stage_idx is not None:
            self._load_stage_form(self._stage_idx)
        self._mark_dirty()
        self._set_preview_stale(True)

    def _on_stage_select(self, _event: Any = None) -> None:
        if self._selecting:
            return
        idx = widgets.selected_index(self.stage_tree)
        if idx is None or idx == self._stage_idx:
            return
        if self._stage_idx is not None:
            try:
                self._flush_stage_form()
                self._flush_phase_form()
                self._flush_phase_thrusters()
            except ValueError as exc:
                widgets.show_warning(self, "当前级参数有误",
                                     "%s\n请先修正后再切换级。" % exc)
                self._reselect_stage(self._stage_idx)
                return
        self._stage_idx = idx
        self._phase_idx = None
        self._load_stage_form(idx)

    def _reselect_stage(self, idx: Optional[int]) -> None:
        self._selecting = True
        try:
            self.stage_tree.selection_remove(
                *[str(i) for i in range(len(self._stages()))])
            if idx is not None:
                self.stage_tree.selection_set(str(idx))
        except tk.TclError:
            pass
        finally:
            self._selecting = False

    def _on_add_stage(self) -> None:
        try:
            self._flush_stage_form()
            self._flush_phase_form()
            self._flush_phase_thrusters()
        except ValueError as exc:
            widgets.show_error(self, "参数有误", str(exc))
            return
        stages = self._stages()
        new_stage = copy.deepcopy(_NEW_STAGE_TEMPLATE)
        new_stage["name"] = "第%d级" % (len(stages) + 1)
        stages.append(new_stage)
        self._stage_idx = len(stages) - 1
        self._phase_idx = 0
        self._refresh_stage_tree()
        self._load_stage_form(self._stage_idx)
        self._mark_dirty()
        self._set_preview_stale(True)

    def _on_delete_stage(self) -> None:
        stages = self._stages()
        if len(stages) <= 1:
            widgets.show_warning(self, "不能删除", "至少需要保留一级。")
            return
        last = stages[-1]
        if not widgets.ask_yes_no(
                self, "删除末级",
                "确定删除第 %d 级「%s」？该级的推力、phases 与残骸设置都会一起移除。"
                % (len(stages), last.get("name") or "末级")):
            return
        try:
            self._flush_stage_form()
            self._flush_phase_form()
            self._flush_phase_thrusters()
        except ValueError as exc:
            widgets.show_error(self, "参数有误", str(exc))
            return
        stages.pop()
        if self._stage_idx is not None:
            self._stage_idx = min(self._stage_idx, len(stages) - 1)
        self._phase_idx = None
        self._refresh_stage_tree()
        self._load_stage_form(self._stage_idx or 0)
        self._mark_dirty()
        self._set_preview_stale(True)

    def _move_stage(self, delta: int) -> None:
        stages = self._stages()
        idx = self._stage_idx
        if idx is None:
            return
        target = idx + int(delta)
        if not (0 <= target < len(stages)):
            return
        try:
            self._flush_stage_form()
            self._flush_phase_form()
            self._flush_phase_thrusters()
        except ValueError as exc:
            widgets.show_error(self, "参数有误", str(exc))
            return
        stages[idx], stages[target] = stages[target], stages[idx]
        self._stage_idx = target
        self._refresh_stage_tree()
        self._load_stage_form(target)
        self._mark_dirty()
        self._set_preview_stale(True)

    # ---- phases ------------------------------------------------------- #
    def _phase_metrics(self, phase: Dict[str, Any]) -> Tuple[float, float]:
        thrust = 0.0
        flow = 0.0
        for th in phase.get("thrusters") or []:
            n = max(1, _as_int(th.get("n"), 1))
            tv = _as_float(th.get("thrust_vac_n")) or 0.0
            iv = _as_float(th.get("isp_vac_s")) or 0.0
            thrust += n * tv
            if iv > 0:
                flow += n * tv / (iv * userdb.G0)
        isp = (thrust / (flow * userdb.G0)) if flow > 0 else 0.0
        return thrust, isp

    def _load_phase_page(self) -> None:
        st = self._current_stage()
        phases = (st or {}).get("phases")
        if not isinstance(phases, list):
            # 旧记录/手改数据里 phases 可能是 dict 或字符串：丢掉，避免子表迭代报错
            if st is not None and "phases" in st:
                st.pop("phases", None)
            phases = []
        if phases:
            self._phase_idx = 0 if self._phase_idx is None else \
                min(max(self._phase_idx, 0), len(phases) - 1)
        else:
            self._phase_idx = None
        self.phase_stage_label.configure(
            text="当前级：%s" % ((st or {}).get("name") or "—"))
        self.phase_warn.configure(
            text=("该级已启用 phases：上面的单机推力/比冲不参与计算。"
                  if phases else "该级暂无 phases：使用上面的单机推力/比冲。"))
        self._refresh_phase_tree(select=self._phase_idx)
        self._load_phase_form()

    def _refresh_phase_tree(self, select: Optional[int] = None) -> None:
        st = self._current_stage()
        phases = (st or {}).get("phases")
        if not isinstance(phases, list):
            phases = []
        self.phase_warn.configure(
            text=("该级已启用 phases：上面的单机推力/比冲不参与计算。"
                  if phases else "该级暂无 phases：使用上面的单机推力/比冲。"))
        widgets.clear_tree(self.phase_tree)
        for i, phase in enumerate(phases):
            thrust, isp = self._phase_metrics(phase)
            self.phase_tree.insert("", "end", iid=str(i), values=(
                str(phase.get("name") or "阶段%d" % (i + 1)),
                _num(phase.get("end_met"), 1),
                "是" if phase.get("emit_event") else "",
                _num(thrust, 0),
                _num(isp, 1),
            ), tags=(("alt",) if i % 2 else ()))
        self._selecting = True
        try:
            if select is not None and 0 <= select < len(phases):
                self.phase_tree.selection_set(str(select))
                self.phase_tree.see(str(select))
                self._phase_idx = select
            else:
                self._phase_idx = None
        except tk.TclError:
            self._phase_idx = None
        finally:
            self._selecting = False

    def _set_phase_editing(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for entry in getattr(self, "phase_form_entries", []):
            try:
                entry.configure(state=state)
            except tk.TclError:
                pass
        try:
            self.phase_emit_check.configure(state=state)
        except (AttributeError, tk.TclError):
            pass
        try:
            self.phase_thrusters.set_enabled(enabled)
        except (AttributeError, tk.TclError):
            pass

    def _load_phase_form(self) -> None:
        phase = self._current_phase()
        if phase is None:
            with self._quiet():
                self.phase_name_var.set("")
                self.phase_end_var.set("")
                self.phase_emit_var.set(False)
            self._set_phase_editing(False)
            self.phase_thrusters.load([])
            return
        with self._quiet():
            self.phase_name_var.set(str(phase.get("name") or ""))
            self.phase_end_var.set(_num(phase.get("end_met"), 2))
            self.phase_emit_var.set(bool(phase.get("emit_event")))
        self._set_phase_editing(True)
        thrusters = phase.get("thrusters")
        if not isinstance(thrusters, list):
            thrusters = []
            phase["thrusters"] = thrusters
        self.phase_thrusters.load(thrusters)

    def _flush_phase_form(self) -> None:
        phase = self._current_phase()
        if phase is None:
            return
        name = self.phase_name_var.get().strip() or "阶段"
        end_met = self._opt(self.phase_end_var, "阶段结束 MET s",
                            userdb.FIELD_LIMITS["end_met"])
        phase["name"] = name
        _put_optional(phase, "end_met", end_met)
        if self.phase_emit_var.get():
            phase["emit_event"] = True
        else:
            phase.pop("emit_event", None)

    def _flush_phase_thrusters(self) -> None:
        phase = self._current_phase()
        if phase is None:
            return
        self.phase_thrusters.flush_selected()

    def _apply_phase_form(self) -> None:
        try:
            self._flush_phase_form()
            self._flush_phase_thrusters()
        except ValueError as exc:
            widgets.show_error(self, "阶段参数有误", str(exc))
            return
        self._refresh_phase_tree(self._phase_idx)
        self._load_phase_form()
        self._mark_dirty()
        self._set_preview_stale(True)

    def _on_phase_select(self, _event: Any = None) -> None:
        if self._selecting:
            return
        idx = widgets.selected_index(self.phase_tree)
        if idx is None or idx == self._phase_idx:
            return
        if self._phase_idx is not None:
            try:
                self._flush_phase_form()
                self._flush_phase_thrusters()
            except ValueError as exc:
                widgets.show_warning(self, "当前阶段参数有误",
                                     "%s\n请先修正后再切换阶段。" % exc)
                self._selecting = True
                try:
                    self.phase_tree.selection_set(str(self._phase_idx))
                finally:
                    self._selecting = False
                return
        self._phase_idx = idx
        self._load_phase_form()

    def _on_add_phase(self) -> None:
        st = self._current_stage()
        if st is None:
            return
        try:
            self._flush_stage_form()
            self._flush_phase_form()
            self._flush_phase_thrusters()
        except ValueError as exc:
            widgets.show_error(self, "参数有误", str(exc))
            return
        phases = st.get("phases")
        if not isinstance(phases, list):
            phases = []
            st["phases"] = phases
        if phases:
            # 已有 phases 时，以最后一组推进剂为模板，避免生成 0 推力阶段
            thrusters = copy.deepcopy(phases[-1].get("thrusters") or [])
        else:
            thrust = _as_float(st.get("thrust_vac_n"))
            if thrust is None:
                thrust = _as_float(st.get("thrust_sl_n")) or 0.0
            isp = _as_float(st.get("isp_vac_s"))
            if isp is None:
                isp = _as_float(st.get("isp_sl_s")) or 0.0
            n = max(1, _as_int(st.get("engines"), 1))
            thrusters = [{"n": n, "thrust_vac_n": thrust, "isp_vac_s": isp}]
        phases.append({
            "name": "阶段%d" % (len(phases) + 1),
            "thrusters": thrusters,
        })
        self._phase_idx = len(phases) - 1
        self._refresh_phase_tree(self._phase_idx)
        self._load_phase_form()
        self._mark_dirty()
        self._set_preview_stale(True)

    def _on_delete_phase(self) -> None:
        st = self._current_stage()
        if st is None:
            return
        phases = st.get("phases")
        idx = self._phase_idx
        if not isinstance(phases, list) or idx is None or not (0 <= idx < len(phases)):
            widgets.show_info(self, "未选中", "请先在阶段表格选择一行。")
            return
        phases.pop(idx)
        if not phases:
            st.pop("phases", None)
            self._phase_idx = None
        else:
            self._phase_idx = min(idx, len(phases) - 1)
        self._refresh_phase_tree(self._phase_idx)
        self._load_phase_form()
        self._mark_dirty()
        self._set_preview_stale(True)

    # ---- 助推器 ------------------------------------------------------- #
    def _load_booster(self) -> None:
        veh = self.rec.setdefault("vehicle", {})
        bo = veh.get("boosters")
        enabled = isinstance(bo, dict) and bool(bo)
        source: Dict[str, Any]
        if enabled:
            source = bo
        elif isinstance(self._booster_backup, dict) and self._booster_backup:
            source = self._booster_backup
        else:
            source = _DEFAULT_BOOSTER
        with self._quiet():
            self.boosters_var.set(enabled)
            self.booster_count_var.set(_num(source.get("count"), 0))
            self.booster_dry_var.set(_num(source.get("dry_kg_each"), 3))
            self.booster_prop_var.set(_num(source.get("prop_kg_each"), 3))
            self.booster_jett_var.set(_num(source.get("jettison_met"), 2))
            self.booster_tvac_var.set(_num(source.get("thrust_vac_n"), 3))
            self.booster_ivac_var.set(_num(source.get("isp_vac_s"), 2))
        if enabled:
            thrusters = bo.get("thrusters")
            if not isinstance(thrusters, list):
                thrusters = []
                bo["thrusters"] = thrusters
            self.booster_thrusters.load(thrusters)
        else:
            self.booster_thrusters.load([])
        self._set_booster_editing(enabled)

    def _set_booster_editing(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for entry in getattr(self, "_booster_entries", []):
            try:
                entry.configure(state=state)
            except tk.TclError:
                pass
        try:
            self.booster_thrusters.set_enabled(enabled)
        except (AttributeError, tk.TclError):
            pass

    def _flush_booster_form(self) -> None:
        if not self.boosters_var.get():
            return
        veh = self.rec.setdefault("vehicle", {})
        bo = veh.get("boosters")
        if not isinstance(bo, dict):
            bo = copy.deepcopy(self._booster_backup or _DEFAULT_BOOSTER)
            veh["boosters"] = bo
        bo.update(self._read_booster_values())

    def _read_booster_values(self) -> Dict[str, Any]:
        count = widgets.parse_int(self.booster_count_var.get(), "助推器数量",
                                  default=2, minimum=1, maximum=8)
        dry = self._opt(self.booster_dry_var, "助推器单枚结构质量 kg",
                        userdb.FIELD_LIMITS["dry_kg"])
        prop = self._opt(self.booster_prop_var, "助推器单枚推进剂 kg",
                         userdb.FIELD_LIMITS["prop_kg_each"])
        jett = self._opt(self.booster_jett_var, "助推器分离时刻 s",
                         userdb.FIELD_LIMITS["jettison_met"])
        tvac = self._opt(self.booster_tvac_var, "备用单枚推力（真空）N",
                         userdb.FIELD_LIMITS["thrust_vac_n"])
        ivac = self._opt(self.booster_ivac_var, "备用比冲 s",
                         userdb.FIELD_LIMITS["isp_vac_s"])
        values: Dict[str, Any] = {
            "count": count,
            "dry_kg_each": 0.0 if dry is None else dry,
            "prop_kg_each": 0.0 if prop is None else prop,
        }
        _put_optional(values, "jettison_met", jett)
        _put_optional(values, "thrust_vac_n", tvac)
        _put_optional(values, "isp_vac_s", ivac)
        return values

    def _on_boosters_toggle(self) -> None:
        if self._loading:
            return
        veh = self.rec.setdefault("vehicle", {})
        if self.boosters_var.get():
            bo = veh.get("boosters")
            if not isinstance(bo, dict):
                bo = copy.deepcopy(self._booster_backup or _DEFAULT_BOOSTER)
                bo.setdefault("thrusters", [])
                veh["boosters"] = bo
            with self._quiet():
                self._load_booster()
        else:
            bo = veh.get("boosters")
            if not isinstance(bo, dict):
                bo = copy.deepcopy(self._booster_backup or _DEFAULT_BOOSTER)
                bo.setdefault("thrusters", [])
                veh["boosters"] = bo
            try:
                bo.update(self._read_booster_values())
                self.booster_thrusters.flush_selected()
            except ValueError as exc:
                with self._quiet():
                    self.boosters_var.set(True)
                widgets.show_warning(self, "助推器参数有误", str(exc))
                return
            self._booster_backup = copy.deepcopy(bo)
            veh["boosters"] = None
            self._set_booster_editing(False)
        self._mark_dirty()
        self._set_preview_stale(True)

    # ---- 整流罩 ------------------------------------------------------- #
    def _load_fairing(self) -> None:
        veh = self.rec.setdefault("vehicle", {})
        fa = veh.get("fairing")
        enabled = isinstance(fa, dict) and bool(fa)
        source: Dict[str, Any]
        if enabled:
            source = fa
        elif isinstance(self._fairing_backup, dict) and self._fairing_backup:
            source = self._fairing_backup
        else:
            source = _DEFAULT_FAIRING
        with self._quiet():
            self.fairing_var.set(enabled)
            self.fairing_mass_var.set(_num(source.get("mass_kg"), 3))
            self.fairing_halves_var.set(_num(source.get("halves"), 0))
            self.fairing_sep_var.set(_num(source.get("sep_met"), 2))
            self.fairing_cda_var.set(_num(source.get("cda_m2"), 2))
        self._set_fairing_editing(enabled)

    def _set_fairing_editing(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for entry in getattr(self, "_fairing_entries", []):
            try:
                entry.configure(state=state)
            except tk.TclError:
                pass

    def _flush_fairing_form(self) -> None:
        veh = self.rec.setdefault("vehicle", {})
        if not self.fairing_var.get():
            veh["fairing"] = None
            return
        fa = veh.get("fairing")
        if not isinstance(fa, dict):
            fa = {}
        fa.update(self._read_fairing_values())
        veh["fairing"] = fa

    def _read_fairing_values(self) -> Dict[str, Any]:
        mass = self._req(self.fairing_mass_var, "整流罩质量 kg",
                         userdb.FIELD_LIMITS["mass_kg"], positive=True)
        halves = widgets.parse_int(self.fairing_halves_var.get(), "整流罩半罩数",
                                   default=2, minimum=1, maximum=10)
        sep = self._opt(self.fairing_sep_var, "整流罩分离时刻 s",
                        userdb.FIELD_LIMITS["sep_met"])
        cda = self._opt(self.fairing_cda_var, "整流罩迎风面积 cda m²",
                        userdb.FIELD_LIMITS["cda_m2"])
        values: Dict[str, Any] = {"mass_kg": mass, "halves": halves,
                                  "cda_m2": 10.0 if cda is None else cda}
        _put_optional(values, "sep_met", sep)
        return values

    def _on_fairing_toggle(self) -> None:
        if self._loading:
            return
        veh = self.rec.setdefault("vehicle", {})
        if self.fairing_var.get():
            fa = veh.get("fairing")
            if not isinstance(fa, dict):
                fa = copy.deepcopy(self._fairing_backup or _DEFAULT_FAIRING)
                veh["fairing"] = fa
            with self._quiet():
                self._load_fairing()
        else:
            fa = veh.get("fairing")
            if not isinstance(fa, dict):
                fa = copy.deepcopy(self._fairing_backup or _DEFAULT_FAIRING)
            try:
                fa.update(self._read_fairing_values())
            except ValueError as exc:
                with self._quiet():
                    self.fairing_var.set(True)
                widgets.show_warning(self, "整流罩参数有误", str(exc))
                return
            self._fairing_backup = copy.deepcopy(fa)
            veh["fairing"] = None
            self._set_fairing_editing(False)
        self._sync_debris_rows()
        self._refresh_debris_tree()
        self._mark_dirty()
        self._set_preview_stale(True)

    # ---- 残骸 --------------------------------------------------------- #
    def _default_debris(self, key: str) -> Dict[str, Any]:
        veh = self.rec.setdefault("vehicle", {})
        stages = self._stages()
        if key.startswith("stage_"):
            try:
                idx = int(key.split("_")[1])
            except (IndexError, ValueError):
                idx = 0
            cda = None
            if 0 <= idx < len(stages):
                cda = _as_float(stages[idx].get("cda_attached_m2"))
            return {"cda": 7.0 if cda is None else max(0.0, cda),
                    "extra_mass_kg": 0.0}
        if key == "fairing":
            cda = _as_float((veh.get("fairing") or {}).get("cda_m2"))
            return {"cda": 7.0 if cda is None else max(0.0, cda),
                    "extra_mass_kg": 0.0}
        if key == "boosters":
            return {"cda": 8.0, "extra_mass_kg": 0.0, "divert_mps": 0.0}
        return {"cda": 7.0, "extra_mass_kg": 0.0}

    def _debris_keys(self) -> List[str]:
        veh = self.rec.get("vehicle") or {}
        stages = self._stages()
        keys = ["stage_%d" % i for i in range(max(0, len(stages) - 1))]
        if isinstance(veh.get("fairing"), dict) and veh["fairing"]:
            keys.append("fairing")
        if isinstance(veh.get("boosters"), dict) and veh["boosters"]:
            keys.append("boosters")
        return keys

    def _sync_debris_rows(self) -> None:
        veh = self.rec.setdefault("vehicle", {})
        deb = veh.get("debris")
        if not isinstance(deb, dict):
            deb = {}
            veh["debris"] = deb
        stages = self._stages()
        for key in list(deb.keys()):
            if not isinstance(deb.get(key), dict):
                deb.pop(key, None)
                continue
            match = re.fullmatch(r"stage_(\d+)", str(key))
            if match and int(match.group(1)) > len(stages) - 2:
                deb.pop(key, None)
                continue
            if key == "fairing" and not veh.get("fairing"):
                deb.pop(key, None)
            if key == "boosters" and not veh.get("boosters"):
                deb.pop(key, None)
        for key in self._debris_keys():
            deb.setdefault(key, self._default_debris(key))

    def _debris_label(self, key: str) -> str:
        stages = self._stages()
        match = re.fullmatch(r"stage_(\d+)", str(key))
        if match:
            idx = int(match.group(1))
            name = "—"
            if 0 <= idx < len(stages):
                name = str(stages[idx].get("name") or ("第%d级" % (idx + 1)))
            return "%s 残骸（stage_%d）" % (name, idx)
        if key == "fairing":
            return "整流罩残骸"
        if key == "boosters":
            return "助推器残骸"
        return str(key)

    def _refresh_debris_tree(self, select: Optional[str] = None) -> None:
        self._sync_debris_rows()
        deb = (self.rec.get("vehicle") or {}).get("debris") or {}
        keys = self._debris_keys()
        if select is None:
            select = self._debris_idx
        widgets.clear_tree(self.debris_tree)
        for i, key in enumerate(keys):
            item = deb.get(key) or {}
            self.debris_tree.insert("", "end", iid=key, values=(
                self._debris_label(key),
                _num(item.get("cda"), 2),
                _num(item.get("extra_mass_kg"), 1),
                _num(item.get("divert_mps"), 2),
            ), tags=(("alt",) if i % 2 else ()))
        self._selecting = True
        try:
            if select in keys:
                self.debris_tree.selection_set(select)
                self._debris_idx = select
            elif keys:
                self.debris_tree.selection_set(keys[0])
                self._debris_idx = keys[0]
            else:
                self._debris_idx = None
        except tk.TclError:
            self._debris_idx = None
        finally:
            self._selecting = False
        if self._debris_idx is not None:
            self._load_debris_form(self._debris_idx)
        else:
            with self._quiet():
                self.debris_cda_var.set("")
                self.debris_extra_var.set("")
                self.debris_divert_var.set("")

    def _load_debris_form(self, key: str) -> None:
        deb = (self.rec.get("vehicle") or {}).get("debris") or {}
        item = deb.get(key)
        if not isinstance(item, dict):
            return
        with self._quiet():
            self.debris_cda_var.set(_num(item.get("cda"), 2))
            self.debris_extra_var.set(_num(item.get("extra_mass_kg"), 1))
            self.debris_divert_var.set(_num(item.get("divert_mps"), 2))

    def _flush_debris(self) -> None:
        key = self._debris_idx
        deb = (self.rec.get("vehicle") or {}).get("debris") or {}
        item = deb.get(key) if key is not None else None
        if not isinstance(item, dict):
            return
        cda = self._opt(self.debris_cda_var, "残骸迎风面积 cda m²",
                        userdb.FIELD_LIMITS["cda_m2"])
        extra = self._opt(self.debris_extra_var, "残骸附带质量 kg",
                          userdb.FIELD_LIMITS["mass_kg"])
        divert = self._opt(self.debris_divert_var, "残骸横向分离速度 m/s",
                           (0.0, 100.0))
        item["cda"] = 0.0 if cda is None else cda
        item["extra_mass_kg"] = 0.0 if extra is None else extra
        _put_optional(item, "divert_mps", divert)

    def _on_debris_select(self, _event: Any = None) -> None:
        if self._selecting:
            return
        sel = self.debris_tree.selection()
        if not sel:
            return
        key = str(sel[0])
        if key == self._debris_idx:
            return
        try:
            self._flush_debris()
        except ValueError as exc:
            widgets.show_warning(self, "残骸参数有误", str(exc))
            self._selecting = True
            try:
                if self._debris_idx:
                    self.debris_tree.selection_set(self._debris_idx)
            finally:
                self._selecting = False
            return
        self._debris_idx = key
        self._load_debris_form(key)

    def _on_debris_update(self) -> None:
        if self._debris_idx is None:
            widgets.show_info(self, "未选中", "请先在残骸表格选择一行。")
            return
        try:
            self._flush_debris()
        except ValueError as exc:
            widgets.show_error(self, "残骸参数有误", str(exc))
            return
        key = self._debris_idx
        self._refresh_debris_tree(key)
        self._mark_dirty()

    def _on_debris_reset(self) -> None:
        key = self._debris_idx
        if key is None:
            return
        deb = (self.rec.get("vehicle") or {}).get("debris") or {}
        deb[key] = self._default_debris(key)
        self._refresh_debris_tree(key)
        self._mark_dirty()

    # ---- 制导默认值 --------------------------------------------------- #
    def _load_guidance(self) -> None:
        g = self.rec.get("guidance_defaults")
        if not isinstance(g, dict):
            g = {}
            self.rec["guidance_defaults"] = g
        with self._quiet():
            for key, var in self.g_vars.items():
                var.set(_num(g.get(key), 3))

    def _flush_guidance_form(self) -> None:
        g = self.rec.get("guidance_defaults")
        if not isinstance(g, dict):
            g = {}
            self.rec["guidance_defaults"] = g
        limits = {
            "t_v": userdb.FIELD_LIMITS["t_v"],
            "t_p": userdb.FIELD_LIMITS["t_p"],
            "pitch_power": userdb.FIELD_LIMITS["pitch_power"],
            "t_fairing": userdb.FIELD_LIMITS["t_fairing"],
            "fairing_divert_mps": userdb.FIELD_LIMITS["fairing_divert_mps"],
            "yaw_tau": userdb.FIELD_LIMITS["yaw_tau"],
            "yaw2_tau": userdb.FIELD_LIMITS["yaw2_tau"],
            "i_target_deg": (0.0, 180.0),
            "seco_band_km": userdb.FIELD_LIMITS["seco_band_km"],
            "seco_e_max": userdb.FIELD_LIMITS["seco_e_max"],
        }
        labels = {
            "t_v": "t_v s", "t_p": "t_p s", "pitch_power": "pitch_power",
            "t_fairing": "t_fairing s",
            "fairing_divert_mps": "抛罩横向速度 m/s",
            "yaw_tau": "yaw_tau s", "yaw2_tau": "yaw2_tau s",
            "i_target_deg": "i_target °",
            "seco_band_km": "seco_band km", "seco_e_max": "seco_e_max",
        }
        for key, var in self.g_vars.items():
            value = self._opt(var, labels.get(key, key), limits.get(key))
            _put_optional(g, key, value)

    # ---- 折线俯仰剖面 ------------------------------------------------- #
    def _schedule_rows(self) -> List[List[float]]:
        return self._schedule_list

    def _load_schedule(self) -> None:
        g = self.rec.get("guidance_defaults") or {}
        raw = g.get("pitch_schedule")
        rows: List[List[float]] = []
        if isinstance(raw, list):
            for point in raw:
                try:
                    t, ang = float(point[0]), float(point[1])
                except (TypeError, ValueError, IndexError):
                    continue
                # 与 userdb.normalize_rocket 同口径：t<=0 的节点会被归一化丢弃
                if t <= 0:
                    continue
                rows.append([t, ang])
            rows.sort(key=lambda p: p[0])
        self._schedule_list = rows
        with self._quiet():
            # 少于 2 个节点等于没有剖面（normalize 也会删除），保留节点供用户补足
            self.schedule_var.set(len(rows) >= 2)
        self._refresh_schedule_tree(0 if rows else None)
        self._update_schedule_state()

    def _refresh_schedule_tree(self, select: Optional[int] = None) -> None:
        rows = self._schedule_rows()
        widgets.clear_tree(self.sched_tree)
        for i, (t, ang) in enumerate(rows):
            self.sched_tree.insert("", "end", iid=str(i), values=(
                _num(t, 2), _num(ang, 2)),
                tags=(("alt",) if i % 2 else ()))
        self._selecting = True
        try:
            if select is not None and 0 <= select < len(rows):
                self.sched_tree.selection_set(str(select))
                self._sched_idx = select
            else:
                self._sched_idx = None
        except tk.TclError:
            self._sched_idx = None
        finally:
            self._selecting = False
        self._load_schedule_form()

    def _load_schedule_form(self) -> None:
        rows = self._schedule_rows()
        idx = self._sched_idx
        if idx is None or not (0 <= idx < len(rows)):
            return
        with self._quiet():
            self.sched_t_var.set(_num(rows[idx][0], 2))
            self.sched_a_var.set(_num(rows[idx][1], 2))

    def _schedule_form_values(self) -> List[float]:
        # 折线剖面的时间可以到数百秒（示例 300 s），不能用 t_v 的 0.5~60 限制
        t = self._opt(self.sched_t_var, "俯仰剖面时间 s", (0.1, 20000.0))
        if t is None or t <= 0:
            raise ValueError("「俯仰剖面时间 s」必须填写正数")
        ang = self._opt(self.sched_a_var, "俯仰剖面角度 °",
                        userdb.FIELD_LIMITS["pitch_end_deg"])
        if ang is None:
            raise ValueError("「俯仰剖面角度 °」不能为空")
        return [float(t), float(ang)]

    def _flush_schedule(self) -> None:
        rows = self._schedule_rows()
        idx = self._sched_idx
        if idx is None or not (0 <= idx < len(rows)):
            return
        rows[idx] = self._schedule_form_values()

    def _update_schedule_state(self) -> None:
        checked = bool(self.schedule_var.get())
        state = "disabled" if checked else "normal"
        for key in ("t_v", "t_p", "pitch_power"):
            entry = self.g_entries.get(key)
            if entry is not None:
                try:
                    entry.configure(state=state)
                except tk.TclError:
                    pass
        try:
            if checked:
                self.schedule_warn.configure(
                    text="⚠ 已启用折线俯仰剖面：t_v / t_p / pitch_power 对该箭型不起作用。",
                    foreground=theme.WARN)
            else:
                self.schedule_warn.configure(
                    text="未启用：按 t_v / t_p / pitch_power 的幂次俯仰程序飞行。",
                    foreground=theme.FG_MUTED)
        except tk.TclError:
            pass

    def _on_schedule_toggle(self) -> None:
        if self._loading:
            return
        if self.schedule_var.get() and not self._schedule_rows():
            self._schedule_list = [list(point) for point in _DEFAULT_PITCH_SCHEDULE]
            self._refresh_schedule_tree(0)
        self._update_schedule_state()
        self._mark_dirty()
        self._set_preview_stale(True)

    def _on_schedule_select(self, _event: Any = None) -> None:
        if self._selecting:
            return
        idx = widgets.selected_index(self.sched_tree)
        if idx is None or idx == self._sched_idx:
            return
        try:
            self._flush_schedule()
        except ValueError as exc:
            widgets.show_warning(self, "俯仰剖面参数有误", str(exc))
            self._selecting = True
            try:
                if self._sched_idx is not None:
                    self.sched_tree.selection_set(str(self._sched_idx))
            finally:
                self._selecting = False
            return
        self._sched_idx = idx
        self._load_schedule_form()

    def _on_schedule_add(self) -> None:
        try:
            point = self._schedule_form_values()
        except ValueError as exc:
            widgets.show_error(self, "俯仰剖面参数有误", str(exc))
            return
        self._schedule_list.append(point)
        self._refresh_schedule_tree(len(self._schedule_list) - 1)
        self._mark_dirty()
        self._set_preview_stale(True)

    def _on_schedule_update(self) -> None:
        if self._sched_idx is None:
            widgets.show_info(self, "未选中", "请先在俯仰剖面表格选择一行。")
            return
        try:
            self._flush_schedule()
        except ValueError as exc:
            widgets.show_error(self, "俯仰剖面参数有误", str(exc))
            return
        self._refresh_schedule_tree(self._sched_idx)
        self._mark_dirty()
        self._set_preview_stale(True)

    def _on_schedule_delete(self) -> None:
        idx = self._sched_idx
        if idx is None or not (0 <= idx < len(self._schedule_list)):
            widgets.show_info(self, "未选中", "请先在俯仰剖面表格选择一行。")
            return
        self._schedule_list.pop(idx)
        new_idx = min(idx, len(self._schedule_list) - 1) if self._schedule_list else None
        self._refresh_schedule_tree(new_idx)
        self._mark_dirty()
        self._set_preview_stale(True)

    def _on_schedule_sort(self) -> None:
        try:
            self._flush_schedule()
        except ValueError as exc:
            widgets.show_error(self, "俯仰剖面参数有误", str(exc))
            return
        self._schedule_list.sort(key=lambda p: p[0])
        self._refresh_schedule_tree(0 if self._schedule_list else None)
        self._mark_dirty()
        self._set_preview_stale(True)

    def _flush_schedule_state(self) -> None:
        g = self.rec.get("guidance_defaults")
        if not isinstance(g, dict):
            g = {}
            self.rec["guidance_defaults"] = g
        if not self.schedule_var.get():
            g.pop("pitch_schedule", None)
            return
        rows = self._schedule_rows()
        if len(rows) < 2:
            raise ValueError("已勾选「使用折线剖面」，至少需要 2 个节点")
        cleaned: List[List[float]] = []
        last_t = None
        for point in rows:
            t, ang = float(point[0]), float(point[1])
            if t <= 0:
                raise ValueError("俯仰剖面节点时间必须大于 0（当前 %s）" % t)
            if last_t is not None and t <= last_t:
                raise ValueError("俯仰剖面节点时间必须严格递增（%s 之后又出现 %s）"
                                 % (last_t, t))
            last_t = t
            cleaned.append([t, ang])
        g["pitch_schedule"] = cleaned

    # ------------------------------------------------------------------ #
    # 页5：时序
    # ------------------------------------------------------------------ #
    def _sync_sequence_rows(self) -> None:
        host = self.seq_host
        for child in host.winfo_children():
            child.destroy()
        self._seq_rows = []
        veh = self.rec.setdefault("vehicle", {})
        stages = self._stages()
        boost = veh.get("boosters") if isinstance(veh.get("boosters"), dict) else None
        fair = veh.get("fairing") if isinstance(veh.get("fairing"), dict) else None

        for col, (text, width) in enumerate((("项目", 22), ("值（秒）", 14),
                                             ("说明", 60))):
            ttk.Label(host, text=text, style="CardHint.TLabel").grid(
                row=0, column=col, sticky="w", padx=(0, 10))

        def add_row(kind: str, idx: Optional[int], label: str,
                    value: Any, enabled: bool, note: str) -> None:
            row = len(self._seq_rows) + 1
            ttk.Label(host, text=label, style="Panel.TLabel").grid(
                row=row, column=0, sticky="e", padx=(0, 10), pady=3)
            var = self._var()
            with self._quiet():
                var.set(_num(value, 2))
            entry = ttk.Entry(host, textvariable=var, width=14)
            entry.grid(row=row, column=1, sticky="w", pady=3)
            if not enabled:
                entry.configure(state="disabled")
            ttk.Label(host, text=note, style="CardHint.TLabel").grid(
                row=row, column=2, sticky="w", padx=(0, 8), pady=3)
            self._seq_rows.append({"kind": kind, "idx": idx, "var": var,
                                   "entry": entry, "enabled": enabled,
                                   "label": label})

        if boost:
            add_row("boosters", None,
                    "助推器分离（×%s）" % _num(boost.get("count"), 0),
                    boost.get("jettison_met"), True,
                    "留空 = 推进剂耗尽时自动分离")
        else:
            add_row("boosters", None, "助推器分离", None, False,
                    "未启用助推器")
        for i, stage in enumerate(stages[:-1]):
            add_row("stage", i,
                    "%s 关机 / 分离" % (stage.get("name") or ("第%d级" % (i + 1))),
                    stage.get("sep_met"), True,
                    "第 %d 级分离时刻" % (i + 1))
        if fair:
            add_row("fairing", None, "整流罩分离", fair.get("sep_met"), True,
                    "留空 = 按制导/程序自动")
        else:
            add_row("fairing", None, "整流罩分离", None, False, "未启用整流罩")
        if stages:
            last = stages[-1]
            add_row("seco", None,
                    "%s 关机（SECO）" % (last.get("name") or "末级"),
                    last.get("seco_met"), True,
                    "末级关机时刻直接决定入轨速度")
        notes: List[str] = ["留空 = 按推进剂耗尽 / 程序自动。"]
        if len(stages) <= 1:
            notes.append("当前只有一级，没有级间分离。")
        self.seq_hint.configure(text="；".join(notes))

    def _flush_sequence(self) -> None:
        veh = self.rec.setdefault("vehicle", {})
        stages = self._stages()
        boost = veh.get("boosters") if isinstance(veh.get("boosters"), dict) else None
        fair = veh.get("fairing") if isinstance(veh.get("fairing"), dict) else None
        for row in self._seq_rows:
            if not row.get("enabled"):
                continue
            kind = row["kind"]
            value = self._opt(row["var"], row["label"],
                              userdb.FIELD_LIMITS["sep_met"])
            if kind == "boosters":
                if boost is not None:
                    _put_optional(boost, "jettison_met", value)
            elif kind == "stage":
                idx = row.get("idx")
                if isinstance(idx, int) and 0 <= idx < len(stages):
                    _put_optional(stages[idx], "sep_met", value)
            elif kind == "fairing":
                if fair is not None:
                    _put_optional(fair, "sep_met", value)
            elif kind == "seco":
                if stages:
                    _put_optional(stages[-1], "seco_met", value)

    # ------------------------------------------------------------------ #
    # 页6：校验与预览
    # ------------------------------------------------------------------ #
    def _set_preview_stale(self, flag: bool) -> None:
        self._preview_stale = bool(flag)
        if not hasattr(self, "stale_label"):
            return
        try:
            self.stale_label.configure(
                text=("参数已修改：下方仍是上次校验结果，点「重新校验」刷新。"
                      if flag else ""))
        except tk.TclError:
            pass

    def _on_refresh_preview(self) -> None:
        self._refresh_validation(silent=False)

    def _refresh_validation(self, *, silent: bool = False) -> bool:
        try:
            self._flush_all()
        except ValueError as exc:
            if not silent:
                widgets.show_error(self, "参数有误",
                                   "%s\n\n请修正后再校验 / 保存。" % exc)
            return False
        try:
            probe = userdb.normalize_rocket(self.rec)
            problems = userdb.validate_rocket(probe)
            metrics = userdb.rocket_metrics(probe)
        except Exception as exc:  # noqa: BLE001 - 预览失败不能把对话框打挂
            if not silent:
                widgets.show_error(self, "预览失败", widgets.explain_error(exc))
            return False
        self._preview = (probe, problems, metrics)
        self._preview_ready = True
        self._render_validation(probe, problems, metrics)
        self._set_preview_stale(False)
        return True

    def _write_segments(self, box: widgets.TextBox,
                        segments: Sequence[Tuple[str, Sequence[str]]]) -> None:
        text = box.text
        try:
            was_disabled = str(text.cget("state")) == "disabled"
            text.configure(state="normal")
            text.delete("1.0", "end")
            for content, tags in segments:
                text.insert("end", content, tuple(tags))
            if was_disabled:
                text.configure(state="disabled")
        except tk.TclError:
            pass

    def _render_validation(self, probe: Dict[str, Any],
                           problems: Sequence[Tuple[str, str, str]],
                           metrics: Dict[str, Any]) -> None:
        errors = [p for p in problems if p[0] == userdb.ERROR]
        warns = [p for p in problems if p[0] != userdb.ERROR]
        try:
            if errors:
                self.val_badge.configure(text="%d 个错误 / %d 条提示"
                                         % (len(errors), len(warns)),
                                         style="BadgeErr.TLabel")
            elif warns:
                self.val_badge.configure(text="0 个错误 / %d 条提示" % len(warns),
                                         style="BadgeWarn.TLabel")
            else:
                self.val_badge.configure(text="校验通过", style="BadgeOk.TLabel")
        except tk.TclError:
            pass

        segments: List[Tuple[str, Sequence[str]]] = []
        if not problems:
            segments.append(("✓ 没有发现会阻止保存的问题。\n\n", ("ok",)))
            segments.append(("保存前仍建议看一眼下面的量级预览与数据体检来源。\n",
                             ("muted",)))
        else:
            segments.append(("共 %d 个错误、%d 条提示。\n"
                             % (len(errors), len(warns)), ("title",)))
            if errors:
                segments.append(("错误会阻止保存；请逐条修正。\n\n", ("err",)))
            elif warns:
                segments.append(("提示不阻止保存，但建议复核。\n\n", ("warn",)))
            for level, field, message in problems:
                if level == userdb.ERROR:
                    segments.append(("【错误】", ("err",)))
                else:
                    segments.append(("【提示】", ("warn",)))
                if field:
                    segments.append((" %s：" % field, ("muted",)))
                segments.append(("%s\n" % message, ("err",) if level == userdb.ERROR
                                 else ("warn",)))
        self._write_segments(self.val_box, segments)

        prev_segments: List[Tuple[str, Sequence[str]]] = []
        title = "性能预览（二体粗算，不含重力/气动损失）\n"
        prev_segments.append((title, ("title",)))
        mass = _as_float(metrics.get("liftoff_mass_kg")) or 0.0
        thrust = _as_float(metrics.get("liftoff_thrust_n")) or 0.0
        twr = _as_float(metrics.get("liftoff_twr")) or 0.0
        dv = _as_float(metrics.get("dv_ideal_mps")) or 0.0
        burn_total = _as_float(metrics.get("burn_total_s")) or 0.0
        prev_segments.append(("起飞质量：", ("key",)))
        prev_segments.append(("%.1f t（%.0f kg，按各级+助推器+载荷+整流罩求和）\n"
                              % (mass / 1000.0, mass), ()))
        prev_segments.append(("起飞推力：", ("key",)))
        prev_segments.append(("%.1f kN\n" % (thrust / 1000.0), ()))
        prev_segments.append(("起飞推重比：", ("key",)))
        prev_segments.append(("%.2f\n" % twr, ("ok",) if twr >= 1.15 else ("warn",)))
        prev_segments.append(("理论 Δv：", ("key",)))
        prev_segments.append(("%.2f km/s    总工作段：%.1f s\n\n"
                              % (dv / 1000.0, burn_total), ()))
        burns = metrics.get("burns") or []
        if burns:
            prev_segments.append(("各级工作段：\n", ("title",)))
            for burn in burns:
                name = str(burn.get("name") or "—")
                prop = _as_float(burn.get("prop_kg")) or 0.0
                sec = _as_float(burn.get("burn_s")) or 0.0
                isp = _as_float(burn.get("isp_s"))
                dv_b = _as_float(burn.get("dv_mps"))
                line = "  %-12s 推进剂 %7.1f t  工作 %7.1f s" % (
                    name, prop / 1000.0, sec)
                if isp:
                    line += "  比冲 %.0f s" % isp
                if dv_b:
                    line += "  Δv %.2f km/s" % (dv_b / 1000.0)
                prev_segments.append((line + "\n", ()))
            prev_segments.append(("\n", ()))
        conclusions = self._metric_conclusions(probe, metrics)
        prev_segments.append(("结论：\n", ("title",)))
        for line in conclusions:
            tag = ("warn",) if line.startswith("⚠") or "偏" in line else ("ok",)
            prev_segments.append(("  · %s\n" % line, tag))
        self._write_segments(self.preview_box, prev_segments)

    def _metric_conclusions(self, probe: Dict[str, Any],
                            metrics: Dict[str, Any]) -> List[str]:
        out: List[str] = []
        twr = _as_float(metrics.get("liftoff_twr")) or 0.0
        if twr <= 0:
            out.append("无法估算起飞推重比：请检查一级推力与总质量。")
        elif twr < 1.05:
            out.append("起飞推重比 %.2f 过低，火箭无法离地；请增大推力或减小质量。" % twr)
        elif twr < 1.15:
            out.append("起飞推重比 %.2f 偏低（常见 1.15~1.7），重力损失会偏大。" % twr)
        elif twr <= 1.7:
            out.append("起飞推重比 %.2f，属于正常范围。" % twr)
        else:
            out.append("起飞推重比 %.2f 偏高（常见 1.15~1.7），气动载荷与过载可能偏大。" % twr)

        veh = probe.get("vehicle") or {}
        stages = veh.get("stages") or []
        for i, stage in enumerate(stages):
            dry = _as_float(stage.get("dry_kg")) or 0.0
            prop = _as_float(stage.get("prop_kg")) or 0.0
            if dry + prop > 0 and dry / (dry + prop) > 0.50:
                out.append("第 %d 级结构质量占本级总质量 %.0f%%，明显偏高，建议复核干重。"
                           % (i + 1, dry / (dry + prop) * 100.0))
        for burn in metrics.get("burns") or []:
            name = str(burn.get("name") or "某级")
            if name.startswith("助推器"):
                continue
            sec = _as_float(burn.get("burn_s")) or 0.0
            if sec > 420:
                out.append("%s工作时间 %.0f s 偏长（常见 100~400 s），建议复核推进剂与流量。"
                           % (name, sec))
            elif 0 < sec < 20:
                out.append("%s工作时间仅 %.1f s，可能关机过早。" % (name, sec))
            isp = _as_float(burn.get("isp_s"))
            if isp is not None and 0 < isp < 230:
                out.append("%s比冲 %.0f s 偏低，请确认推进剂与发动机型号。" % (name, isp))
        dv = _as_float(metrics.get("dv_ideal_mps")) or 0.0
        if 0 < dv < 7800:
            out.append("理论 Δv %.2f km/s 偏低（LEO 通常需 9 km/s 量级，含损失）。"
                       % (dv / 1000.0))
        elif dv > 12000:
            out.append("理论 Δv %.2f km/s 偏高，请确认是否有级未抛掉或推进剂填多了。"
                       % (dv / 1000.0))
        total = _as_float(metrics.get("burn_total_s")) or 0.0
        if total > 1200:
            out.append("总工作段 %.0f s 偏长，请确认串联级数与推进剂。 " % total)
        for warning in metrics.get("warnings") or []:
            out.append("⚠ %s" % warning)
        seen = set()
        unique: List[str] = []
        for line in out:
            if line in seen:
                continue
            seen.add(line)
            unique.append(line)
        if not unique:
            unique.append("未发现明显的量级问题；仍建议用数据体检核对来源。")
        return unique

    # ------------------------------------------------------------------ #
    # 分页切换 / 全量提交
    # ------------------------------------------------------------------ #
    def _flush_all(self) -> None:
        self._flush_basic()
        self._flush_typical()
        self._flush_stage_form()
        self._flush_phase_form()
        self._flush_phase_thrusters()
        self.booster_thrusters.flush_selected()
        self._flush_booster_form()
        self._flush_fairing_form()
        self._flush_debris()
        self._sync_debris_rows()
        self._flush_guidance_form()
        self._flush_schedule()
        self._flush_schedule_state()
        self._flush_sequence()

    def _flush_page(self, index: int) -> None:
        if index == 0:
            self._flush_basic()
            self._flush_typical()
        elif index == 1:
            self._flush_stage_form()
            self._flush_phase_form()
            self._flush_phase_thrusters()
            self.booster_thrusters.flush_selected()
            self._flush_booster_form()
        elif index == 2:
            self._flush_fairing_form()
            self._flush_debris()
        elif index == 3:
            self._flush_guidance_form()
            self._flush_schedule()
            self._flush_schedule_state()
        elif index == 4:
            self._flush_sequence()

    def _on_tab_changed(self, _event: Any = None) -> None:
        if not self._built or self._selecting:
            return
        try:
            new_index = int(self.nb.index(self.nb.select()))
        except tk.TclError:
            return
        if new_index == self._active_page:
            return
        old_index = self._active_page
        try:
            self._flush_page(old_index)
        except ValueError as exc:
            widgets.show_warning(self, "当前页参数有误",
                                 "%s\n\n请先修正后再切换到其它分页。" % exc)
            self._selecting = True
            try:
                self.nb.select(self.pages[old_index])
            except (tk.TclError, IndexError):
                pass
            finally:
                self._selecting = False
            return
        self._active_page = new_index
        self._on_page_entered(new_index)

    def _on_page_entered(self, index: int) -> None:
        if index == 0:
            self._refresh_typical_tree(self._orb_idx)
        elif index == 2:
            self._sync_debris_rows()
            self._refresh_debris_tree()
        elif index == 3:
            self._update_schedule_state()
        elif index == 4:
            with self._quiet():
                self._sync_sequence_rows()
        elif index == 5:
            self._set_preview_stale(self._preview_stale)

    def _goto_page(self, index: int) -> None:
        if not self._built:
            return
        self._active_page = index
        try:
            self.nb.select(self.pages[index])
        except (tk.TclError, IndexError):
            return
        self._on_page_entered(index)

    def _confirm_discard(self) -> bool:
        if not self._dirty:
            return True
        return widgets.ask_yes_no(
            self, "未保存的修改",
            "当前箭型还有未保存的修改，继续会丢弃这些修改。\n确定继续？")

    # ------------------------------------------------------------------ #
    # 工具条动作
    # ------------------------------------------------------------------ #
    def _replace_record(self, rec: Dict[str, Any], *, from_library: bool,
                        allow_shadow: bool = True) -> None:
        if not self._confirm_discard():
            return
        with self._quiet():
            self.rec = userdb.normalize_rocket(copy.deepcopy(rec))
            self.rec.pop("_shadow_builtin", None)
            self._from_library = bool(from_library)
            self._shadow_builtin_id = ""
            self._shadow_builtin_name = ""
            if allow_shadow and not from_library:
                self._detect_builtin_shadow()
            self._class_at_load = str(self.rec.get("class_key") or "")
            veh = self.rec.get("vehicle") or {}
            self._fairing_backup = (copy.deepcopy(veh["fairing"])
                                    if isinstance(veh.get("fairing"), dict)
                                    and veh["fairing"] else None)
            self._booster_backup = (copy.deepcopy(veh["boosters"])
                                    if isinstance(veh.get("boosters"), dict)
                                    and veh["boosters"] else None)
            self._stage_idx = None
            self._phase_idx = None
            self._orb_idx = None
            self._debris_idx = None
            self._sched_idx = None
            self._schedule_list = []
            self._load_all()
        self._dirty = False
        self._preview_ready = False
        self._update_badges()
        for page in self.pages:
            try:
                page.scroll_to_top()
            except tk.TclError:
                pass
        self._refresh_validation(silent=True)
        self._goto_page(0)

    def _on_new_template(self) -> None:
        label = self.new_class_var.get().strip()
        key = next((k for k, text in CLASS_CHOICES if text == label),
                   "medium_liquid")
        name = "我的%s火箭" % userdb.CLASS_KEY_CN.get(key, "自定义")
        try:
            rec = userdb.blank_rocket(name, key)
        except Exception as exc:  # noqa: BLE001
            widgets.show_error(self, "新建失败", widgets.explain_error(exc))
            return
        self._replace_record(rec, from_library=False)

    def _rocket_label(self, rec: Dict[str, Any]) -> str:
        name = str(rec.get("name") or rec.get("id") or "未命名箭型")
        stages = len((rec.get("vehicle") or {}).get("stages") or [])
        return "%s%s" % (name, ("    · %d 级" % stages) if stages else "")

    def _on_load_builtin(self) -> None:
        items = list(userdb.builtin_rockets())
        if not items:
            widgets.show_info(self, "内置库为空", "没有读到内置箭型库。")
            return
        picked = _PickerDialog(
            self, "从内置箭型载入", items, self._rocket_label,
            "选择一型作为起点；载入后会变成一个「新箭型」，保存时写入你的用户库，"
            "不会改动内置库。").result
        if not picked:
            return
        rec = copy.deepcopy(picked)
        rec.pop("_shadow_builtin", None)
        rec["_derived_from"] = str(picked.get("name") or "内置箭型")
        # 保留内置 id：若名称不改，保存为同 id 的用户记录（覆盖内置）；改名则另存新箭型。
        rec["launch_sites"] = []
        rec["sites"] = []
        self._replace_record(rec, from_library=False)

    def _on_load_user(self) -> None:
        items = list(userdb.list_rockets())
        if not items:
            widgets.show_info(self, "我的箭型库为空",
                              "还没有自定义箭型。可以先用模板新建，再保存到我的箭型库。")
            return
        picked = _PickerDialog(
            self, "从我的箭型库载入", items, self._rocket_label,
            "选择一条已保存的自定义箭型继续编辑；保存时会更新同一条记录。").result
        if not picked:
            return
        self._replace_record(picked, from_library=True)

    def _import_candidates(self, data: Any) -> List[Dict[str, Any]]:
        if isinstance(data, dict):
            if isinstance(data.get("rockets"), list):
                return [it for it in data["rockets"] if isinstance(it, dict)]
            if isinstance(data.get("rocket"), dict):
                return [data["rocket"]]
            if isinstance(data.get("vehicle"), dict) or data.get("stages") is not None:
                return [data]
            return []
        if isinstance(data, list):
            return [it for it in data if isinstance(it, dict)]
        return []

    def _on_import_json(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="导入箭型 JSON",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            widgets.show_error(self, "导入失败", "读取 JSON 失败：%s"
                               % widgets.explain_error(exc))
            return
        candidates = self._import_candidates(data)
        if not candidates:
            widgets.show_error(
                self, "导入失败",
                "文件里没有可识别的箭型记录（需要含 vehicle.stages 的 JSON 对象，"
                "或含 rockets 列表的用户数据包）。")
            return
        picked = candidates[0] if len(candidates) == 1 else _PickerDialog(
            self, "选择要导入的箭型", candidates, self._rocket_label).result
        if not picked:
            return
        rec = copy.deepcopy(picked)
        rec.pop("_shadow_builtin", None)
        self._replace_record(rec, from_library=False, allow_shadow=False)

    def _on_export_json(self) -> None:
        try:
            self._flush_all()
            probe = userdb.normalize_rocket(self.rec)
        except ValueError as exc:
            widgets.show_error(self, "导出失败", str(exc))
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="导出箭型 JSON",
            defaultextension=".json",
            initialfile="%s.json" % _safe_filename(probe.get("name") or "rocket"),
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(probe, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            widgets.show_error(self, "导出失败", widgets.explain_error(exc))
            return
        widgets.show_info(self, "已导出", "箭型已导出到：\n%s" % path)

    # ------------------------------------------------------------------ #
    # 底部按钮
    # ------------------------------------------------------------------ #
    def _build_buttons(self) -> None:
        ttk.Label(self.button_bar,
                  text="保存会写入 user_data/rockets.json；内置库不会被修改。",
                  style="Hint.TLabel").pack(side="left")
        ttk.Button(self.button_bar, text="取消", command=self._on_cancel).pack(
            side="right")
        if self.allow_save:
            ttk.Button(self.button_bar, text="保存到我的箭型库",
                       style="Accent.TButton", command=self._on_save).pack(
                side="right", padx=(0, 8))
        if self.allow_session:
            ttk.Button(self.button_bar, text="仅用于本次任务",
                       command=self._on_session).pack(side="right", padx=(0, 8))

    def _collect_probe(self) -> Dict[str, Any]:
        self._flush_all()
        return userdb.normalize_rocket(self.rec)

    def _on_session(self) -> None:
        try:
            probe = self._collect_probe()
        except ValueError as exc:
            widgets.show_error(self, "参数有误", "%s\n\n请修正后再使用。" % exc)
            return
        except Exception as exc:  # noqa: BLE001
            widgets.show_error(self, "无法整理箭型记录", widgets.explain_error(exc))
            return
        result = copy.deepcopy(probe)
        # 会话记录不宣称自己是用户库记录：主窗口用 _user 区分「已保存/仅本次」
        result.pop("_user", None)
        self.result = result
        self.mode = "session"
        self.saved = False
        self._dirty = False
        self._update_badges()
        self.close()

    def _on_save(self) -> None:
        try:
            probe = self._collect_probe()
        except ValueError as exc:
            widgets.show_error(self, "参数有误", "%s\n\n请修正后再保存。" % exc)
            return
        except Exception as exc:  # noqa: BLE001
            widgets.show_error(self, "无法整理箭型记录", widgets.explain_error(exc))
            return
        try:
            problems = userdb.validate_rocket(probe)
            metrics = userdb.rocket_metrics(probe)
        except Exception as exc:  # noqa: BLE001
            widgets.show_error(self, "校验失败", widgets.explain_error(exc))
            return
        errors = [item for item in problems if item[0] == userdb.ERROR]
        if errors:
            self._preview = (probe, problems, metrics)
            self._render_validation(probe, problems, metrics)
            self._preview_ready = True
            self._set_preview_stale(False)
            self._goto_page(5)
            widgets.show_error(
                self, "存在错误，不能保存",
                "发现 %d 个错误，请到「⑥ 校验与预览」页逐条修正。\n\n%s"
                % (len(errors), errors[0][2]))
            return
        warns = [item for item in problems if item[0] != userdb.ERROR]
        if warns:
            if not widgets.ask_yes_no(
                    self, "有 %d 条提示" % len(warns),
                    "校验发现 %d 条提示（不是错误）。仍然保存吗？\n\n%s"
                    % (len(warns), warns[0][2])):
                return
        name = str(probe.get("name") or "").strip()
        use_shadow = bool(self._shadow_builtin_id
                          and name == self._shadow_builtin_name)
        try:
            if use_shadow:
                # 名称没改：同 id 写入用户库，按 userdb 的合并规则覆盖内置记录
                probe["id"] = self._shadow_builtin_id
                saved = userdb.save_rocket(probe, new=False)
            else:
                saved = userdb.save_rocket(probe, new=not self._from_library)
        except Exception as exc:  # noqa: BLE001
            widgets.show_error(self, "保存失败", widgets.explain_error(exc))
            return
        self.rec = saved
        self.result = saved
        self.saved = True
        self.mode = "user_db"
        self._from_library = True
        self._dirty = False
        self._update_badges()
        self.close()

    def _on_cancel(self) -> None:
        self.result = None
        self.close()

    # ------------------------------------------------------------------ #
    # 关闭
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        if getattr(self, "_closing", False):
            return
        if getattr(self, "_dirty", False):
            if not widgets.ask_yes_no(
                    self, "未保存的修改",
                    "当前箭型还有未保存的修改，关闭会丢弃它们。\n确定关闭？"):
                return
        self._closing = True
        super().close()

