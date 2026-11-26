# -*- coding: utf-8 -*-
"""自定义参考轨道编辑器（v4）。

v4 支持把常用入轨目标保存到「我的轨道库」（``user_data/orbits.json``），
任务页（「轨道与时序」）可以直接选用。本模块提供图形化编辑器：

* 顶部：轨道模板下拉（内置库 + 我的轨道库全部名称）、从 TLE 导入、
  从我的轨道库载入、保存到我的轨道库、删除；
* 表单：名称、类型、高度（椭圆填远地点）、倾角、偏心率、RAAN、
  近地点幅角、LTAN、历元 UTC、置信度、说明；
* 右侧：实时校验结果与派生量（周期、半长轴、近/远地点、太阳同步提示、
  发射方位角粗估）。

退出时：

* ``self.result``：轨道记录或 ``None``；
* ``self.saved``：是否已写入用户库。

「使用此轨道」返回的记录**不带** ``_user`` 标记，主窗口据此区分
「已保存到用户库」与「仅用于本次任务」；保存成功的记录带 ``_user=True``。
"""
from __future__ import annotations

import contextlib
import copy
import math
import tkinter as tk
from tkinter import ttk
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from app.core import userdb

from . import theme, widgets

#: 轨道类型下拉（可动态补充库里的非标准值）
TYPE_CHOICES: Tuple[str, ...] = ("SSO", "LEO", "MEO", "GTO", "GEO", "IGSO",
                                 "极轨", "自定义")
CONFIDENCE_LEVELS: Tuple[str, ...] = ("高", "中高", "中", "低")
KIND_CN = {"SSO": "太阳同步", "LEO": "近地", "MEO": "中轨", "GTO": "地球同步转移",
           "GEO": "地球静止", "IGSO": "倾斜地球同步", "极轨": "极轨",
           "自定义": "自定义", "CUSTOM": "自定义", "TLE": "TLE 反算"}

MU = 398600.4418              # km^3 / s^2
RE_KM = 6378.137              # km
QIANJIU_LAT = 38.86           # 酒泉纬度（只用于方位角粗估）


def _num(value: Any, nd: int = 3) -> str:
    return widgets.fmt_num(value, nd)


def _as_float(value: Any) -> Optional[float]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# TLE 导入小窗口
# --------------------------------------------------------------------------- #
class _TleDialog(tk.Toplevel):
    """粘贴两行 TLE → 解析 → 返回 ``(orbit_dict, note)``。"""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.result: Optional[Tuple[Dict[str, Any], str]] = None
        self._parsed: Optional[Dict[str, Any]] = None
        self._note = ""
        self.title("从 TLE 导入参考轨道")
        self.transient(parent)
        self.configure(background=theme.WINDOW_BG)
        self.resizable(True, True)

        body = ttk.Frame(self, padding=(14, 12))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        ttk.Label(body, text="粘贴两行 TLE（含名称行也可以）",
                  style="PageTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(body,
                  text="只做平均根数反算（不跑 SGP4）：高度由平均运动反算，"
                       "再取倾角/偏心率/RAAN/近地点幅角。",
                  style="Subtitle.TLabel", wraplength=520, justify="left").grid(
            row=1, column=0, sticky="w", pady=(2, 8))
        self.text_box = widgets.TextBox(body, height=7, width=64, wrap="none",
                                        font=theme.mono())
        self.text_box.grid(row=2, column=0, sticky="nsew")
        self.msg = ttk.Label(body, text="尚未解析。", style="Hint.TLabel",
                             wraplength=520, justify="left")
        self.msg.grid(row=3, column=0, sticky="w", pady=(8, 0))

        bar = ttk.Frame(body)
        bar.grid(row=4, column=0, sticky="e", pady=(10, 0))
        ttk.Button(bar, text="取消", command=self._cancel).pack(side="right")
        self.fill_btn = ttk.Button(bar, text="填入表单", style="Accent.TButton",
                                   command=self._fill, state="disabled")
        self.fill_btn.pack(side="right", padx=(0, 8))
        ttk.Button(bar, text="解析 TLE", command=self._parse).pack(
            side="right", padx=(0, 8))

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda e: self._cancel())
        widgets.center_window(self, parent, 600, 430)
        self.after(60, self._grab)
        try:
            self.wait_window(self)
        except tk.TclError:
            pass
        try:
            parent.grab_set()
        except Exception:  # noqa: BLE001
            pass

    def _grab(self) -> None:
        try:
            self.grab_set()
            self.text_box.text.focus_set()
        except Exception:  # noqa: BLE001
            pass

    def _parse(self) -> None:
        text = self.text_box.get_text().strip()
        if not text:
            self._set_msg("请先粘贴两行 TLE。", theme.WARN)
            return
        try:
            from app.core.infer import parse_tle_elements
            orbit, note = parse_tle_elements(text)
        except Exception as exc:  # noqa: BLE001
            self._set_msg("解析失败：%s" % widgets.explain_error(exc), theme.ERR)
            return
        if not orbit:
            self._set_msg("解析失败：%s" % note, theme.ERR)
            self._parsed = None
            self.fill_btn.configure(state="disabled")
            return
        self._parsed = orbit
        self._note = note
        self._set_msg("解析成功：%s" % note, theme.OK)
        self.fill_btn.configure(state="normal")

    def _set_msg(self, text: str, color: str) -> None:
        try:
            self.msg.configure(text=text, foreground=color)
        except tk.TclError:
            pass

    def _fill(self) -> None:
        if self._parsed is None:
            return
        self.result = (copy.deepcopy(self._parsed), self._note)
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


# --------------------------------------------------------------------------- #
# 参考轨道编辑器
# --------------------------------------------------------------------------- #
class OrbitEditorDialog(widgets.Dialog):
    """自定义参考轨道编辑器（阻塞式）。"""

    def __init__(self, parent: tk.Misc,
                 orbit: Optional[Dict[str, Any]] = None, *,
                 title: str = "参考轨道编辑器", allow_save: bool = True) -> None:
        self.result: Optional[Dict[str, Any]] = None
        self.saved: bool = False
        self.mode: str = "session"
        self.allow_save = bool(allow_save)

        self._loading = 0
        self._dirty = False
        self._built = False
        self._closing = False
        self._current_id = ""
        self._from_library = False
        self._template_items: List[Dict[str, Any]] = []
        self._builtin_orbits: Dict[str, str] = {}
        self._shadow_builtin_id = ""
        self._shadow_builtin_name = ""

        source = copy.deepcopy(orbit) if isinstance(orbit, dict) else None
        source_is_user = bool(source and source.get("_user") is True)
        if source is None:
            source = {"name": "我的参考轨道", "type": "SSO", "alt_km": 500.0,
                      "i_deg": 97.4, "e": 0.0, "confidence": "中", "note": ""}
        self.rec = userdb.normalize_orbit(source)
        self.rec.pop("_shadow_builtin", None)
        lib_ids = {str(it.get("id")) for it in userdb.list_orbits()}
        rid = str(self.rec.get("id") or "")
        self._from_library = source_is_user and rid in lib_ids
        self._current_id = rid if self._from_library else ""

        super().__init__(
            parent, title=title,
            subtitle="参考轨道决定入轨目标与轨道平面窗口；保存后可在「轨道与时序」页"
                     "直接选用。高度按近圆轨道理解，椭圆轨道请填远地点高度。",
            width=760, height=560, resizable=True)

    # ------------------------------------------------------------------ #
    # 基础小工具
    # ------------------------------------------------------------------ #
    @contextlib.contextmanager
    def _quiet(self) -> Iterator[None]:
        self._loading += 1
        try:
            yield
        finally:
            self._loading = max(0, self._loading - 1)

    def _var(self, value: Any = "") -> tk.StringVar:
        var = tk.StringVar(value="" if value is None else str(value))
        var.trace_add("write", self._on_var_edited)
        return var

    def _on_var_edited(self, *_args: Any) -> None:
        if self._loading or not self._built:
            return
        self._dirty = True
        self._update_preview()

    def _watch_text(self, box: widgets.TextBox) -> None:
        def _on_modified(_event: Any = None) -> None:
            try:
                if not box.text.edit_modified():
                    return
                box.text.edit_modified(False)
            except tk.TclError:
                return
            if self._loading or not self._built:
                return
            self._dirty = True
            self._update_preview()
        try:
            box.text.bind("<<Modified>>", _on_modified, add="+")
            box.text.edit_modified(False)
        except tk.TclError:
            pass

    def _form_field(self, master: tk.Misc, row: int, label: str,
                    var: tk.StringVar, *, width: int = 12, hint: str = "",
                    label_width: int = 9, column: int = 0, kind: str = "entry",
                    values: Optional[Sequence[str]] = None) -> ttk.Widget:
        """在网格里放一行「标签 + 输入框」，控件直接建在子框里。"""
        ttk.Label(master, text=label, style="Panel.TLabel", width=label_width,
                  anchor="e").grid(row=row, column=column, sticky="e",
                                   padx=(0, 8), pady=4)
        box = ttk.Frame(master, style="Panel.TFrame")
        box.grid(row=row, column=column + 1, sticky="ew", pady=4)
        if kind == "combo":
            widget = ttk.Combobox(box, textvariable=var, width=width,
                                  state="readonly", values=list(values or []))
            widget.pack(side="left")
        else:
            widget = ttk.Entry(box, textvariable=var, width=width)
            widget.pack(side="left")
        if hint:
            ttk.Label(box, text=hint, style="CardHint.TLabel").pack(
                side="left", padx=(8, 0))
        return widget

    def _confirm_discard(self) -> bool:
        if not self._dirty:
            return True
        return widgets.ask_yes_no(
            self, "未保存的修改",
            "当前轨道还有未保存的修改，继续会丢弃这些修改。\n确定继续？")

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def build_body(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=0)
        parent.rowconfigure(2, weight=1)
        self._build_toolbar(parent)

        self.status_label = ttk.Label(
            parent, text="提示：高度按近圆轨道理解；椭圆轨道填远地点高度。"
                         "RAAN 与 LTAN 二选一即可。",
            style="Hint.TLabel", justify="left", wraplength=720)
        self.status_label.grid(row=1, column=0, sticky="ew", pady=(6, 4))

        main = ttk.Frame(parent)
        main.grid(row=2, column=0, sticky="nsew")
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        form_card = widgets.Card(
            main, title="参考轨道参数",
            hint_text="类型只用于展示与筛选；仿真实际使用高度、倾角、偏心率与平面参数。")
        form_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        form_card.body.columnconfigure(1, weight=1)
        form_card.body.columnconfigure(3, weight=1)
        try:
            form_card.hint.configure(wraplength=360)
        except (AttributeError, tk.TclError):
            pass
        self._build_form(form_card)

        prev_card = widgets.Card(
            main, title="校验与派生量",
            hint_text="派生量按二体近似计算；发射方位角按酒泉纬度粗估，仅供参照。")
        prev_card.grid(row=0, column=1, sticky="nsew")
        prev_card.body.columnconfigure(0, weight=1)
        prev_card.body.rowconfigure(0, weight=1)
        try:
            prev_card.hint.configure(wraplength=270)
        except (AttributeError, tk.TclError):
            pass
        self.preview_box = widgets.TextBox(prev_card.body, height=14, width=30,
                                           wrap="word", readonly=True)
        self.preview_box.grid(row=0, column=0, sticky="nsew")
        text = self.preview_box.text
        text.tag_configure("title", foreground=theme.ACCENT,
                           font=theme.font(theme.BASE_SIZE, True))
        text.tag_configure("key", foreground=theme.FG_MUTED)
        text.tag_configure("err", foreground=theme.ERR)
        text.tag_configure("warn", foreground=theme.WARN)
        text.tag_configure("ok", foreground=theme.OK)
        text.tag_configure("muted", foreground=theme.FG_MUTED)

        self._load_templates()
        self._fill_form(self.rec)
        self._built = True
        self._dirty = False
        self._detect_builtin_shadow()
        if self._shadow_builtin_id:
            self.status_label.configure(
                text="当前是内置轨道副本：名称不改时保存会用同 id 覆盖内置（仅写用户库，"
                     "不改内置文件）；改名则另存为新轨道。",
                foreground=theme.FG_MUTED)
        self._update_preview()
        self._update_delete_button()

    def _build_toolbar(self, parent: ttk.Frame) -> None:
        bar = ttk.Frame(parent, style="Panel.TFrame", padding=(8, 6))
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(bar, text="轨道模板", style="Panel.TLabel").pack(
            side="left", padx=(0, 6))
        self.tpl_var = tk.StringVar()
        self.tpl_combo = ttk.Combobox(bar, textvariable=self.tpl_var,
                                      state="readonly", width=24)
        self.tpl_combo.pack(side="left", padx=(0, 10))
        self.tpl_combo.bind("<<ComboboxSelected>>",
                            lambda e: self._on_template_selected())
        ttk.Button(bar, text="从 TLE 导入…", style="Small.TButton",
                   command=self._on_tle_import).pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="从我的轨道库载入…", style="Small.TButton",
                   command=self._on_load_library).pack(side="left", padx=(0, 6))
        if self.allow_save:
            ttk.Button(bar, text="保存到我的轨道库", style="Small.TButton",
                       command=self._on_save).pack(side="left", padx=(0, 6))
            self.delete_btn = ttk.Button(bar, text="删除", style="Small.TButton",
                                         command=self._on_delete, state="disabled")
            self.delete_btn.pack(side="left")

    def _build_form(self, card: widgets.Card) -> None:
        body = card.body
        self.name_var = self._var()
        self.type_var = self._var()
        self.alt_var = self._var()
        self.inc_var = self._var()
        self.ecc_var = self._var()
        self.raan_var = self._var()
        self.argp_var = self._var()
        self.ltan_var = self._var()
        self.epoch_var = self._var()
        self.confidence_var = self._var()

        self._form_field(body, 0, "名称", self.name_var, width=17)
        self.type_combo = self._form_field(
            body, 0, "类型", self.type_var, width=8, kind="combo",
            values=TYPE_CHOICES, column=2)
        self._form_field(body, 1, "高度 km", self.alt_var, width=9)
        self._form_field(body, 1, "倾角 °", self.inc_var, width=8, column=2)
        self._form_field(body, 2, "偏心率", self.ecc_var, width=9)
        self._form_field(body, 2, "RAAN °", self.raan_var, width=8, column=2)
        self._form_field(body, 3, "近地点幅角 °", self.argp_var, width=9,
                         label_width=10)
        self._form_field(body, 3, "LTAN h", self.ltan_var, width=8, column=2)
        self._form_field(body, 4, "历元 UTC", self.epoch_var, width=17)
        self.confidence_combo = self._form_field(
            body, 4, "置信度", self.confidence_var, width=8, kind="combo",
            values=CONFIDENCE_LEVELS, column=2)

        ttk.Label(body, text="说明", style="Panel.TLabel", width=9,
                  anchor="e").grid(row=5, column=0, sticky="ne", padx=(0, 8),
                                   pady=4)
        note_wrap = ttk.Frame(body, style="Panel.TFrame")
        note_wrap.grid(row=5, column=1, columnspan=3, sticky="ew", pady=4)
        note_wrap.columnconfigure(0, weight=1)
        self.note_box = widgets.TextBox(note_wrap, height=3, width=32, wrap="word")
        self.note_box.grid(row=0, column=0, sticky="ew")
        self._watch_text(self.note_box)
        ttk.Label(body,
                  text="高度：近圆填轨道高度，椭圆填远地点；RAAN 与 LTAN 二选一。",
                  style="CardHint.TLabel", justify="left",
                  wraplength=360).grid(row=6, column=0, columnspan=4,
                                       sticky="w", pady=(4, 0))

    def _load_templates(self) -> None:
        try:
            from app.core import infer as infer
            items = [o for o in infer.orbit_list()
                     if isinstance(o, dict) and o.get("name")]
            self._template_items = items
            self._builtin_orbits = {
                str(o.get("id")): str(o.get("name"))
                for o in items
                if not o.get("_user") and o.get("id") and o.get("name")
            }
        except Exception as exc:  # noqa: BLE001
            self._template_items = []
            self._builtin_orbits = {}
            try:
                self.status_label.configure(
                    text="轨道库加载失败：%s" % widgets.explain_error(exc),
                    foreground=theme.ERR)
            except (AttributeError, tk.TclError):
                pass
        try:
            self.tpl_combo.configure(
                values=[str(o.get("name")) for o in self._template_items])
        except tk.TclError:
            pass

    def _detect_builtin_shadow(self) -> None:
        """识别「当前就是内置轨道」：名称不改时保存为同 id 的用户记录。

        userdb 的合并规则是「用户条目覆盖同 id 内置条目」。若不这样做，从内置
        模板保存的同名副本会与内置记录在名称匹配时冲突（内置在前，副本选不中）。
        """
        self._shadow_builtin_id = ""
        self._shadow_builtin_name = ""
        if self._from_library:
            return
        rid = str(self.rec.get("id") or "")
        if rid and rid in self._builtin_orbits:
            self._shadow_builtin_id = rid
            self._shadow_builtin_name = self._builtin_orbits[rid]

    # ------------------------------------------------------------------ #
    # 数据 ←→ 表单
    # ------------------------------------------------------------------ #
    def _ensure_combo_values(self, rec: Dict[str, Any]) -> None:
        type_value = str(rec.get("type") or "").strip().upper()
        values = list(TYPE_CHOICES)
        if type_value and type_value not in values:
            values.insert(0, type_value)
        try:
            self.type_combo.configure(values=values)
        except tk.TclError:
            pass
        confidence = str(rec.get("confidence") or "中")
        cvalues = list(CONFIDENCE_LEVELS)
        if confidence and confidence not in cvalues:
            cvalues.append(confidence)
        try:
            self.confidence_combo.configure(values=cvalues)
        except tk.TclError:
            pass

    def _fill_form(self, rec: Dict[str, Any]) -> None:
        self._ensure_combo_values(rec)
        with self._quiet():
            self.name_var.set(str(rec.get("name") or ""))
            self.type_var.set(str(rec.get("type") or "LEO").upper())
            self.alt_var.set(_num(rec.get("alt_km"), 2))
            self.inc_var.set(_num(rec.get("i_deg"), 3))
            self.ecc_var.set(_num(rec.get("e"), 5))
            self.raan_var.set(_num(rec.get("raan_deg"), 2))
            self.argp_var.set(_num(rec.get("argp_deg"), 2))
            self.ltan_var.set(_num(rec.get("ltan_h"), 2))
            self.epoch_var.set(str(rec.get("epoch_utc") or ""))
            self.confidence_var.set(str(rec.get("confidence") or "中"))
            self.note_box.set_text(str(rec.get("note") or ""))
        try:
            self.note_box.text.edit_modified(False)
        except tk.TclError:
            pass

    def _form_probe(self, *, collect_errors: bool = False
                    ) -> Tuple[Dict[str, Any], List[str]]:
        errors: List[str] = []

        def read(var: tk.StringVar, label: str,
                 limits: Optional[Tuple[float, float]] = None) -> Optional[float]:
            try:
                low, high = limits if limits else (None, None)
                return widgets.parse_optional_float(var.get(), label, low, high)
            except ValueError as exc:
                errors.append(str(exc))
                return None

        alt = read(self.alt_var, "轨道高度 km", userdb.FIELD_LIMITS["alt_km"])
        inc = read(self.inc_var, "倾角 °", userdb.FIELD_LIMITS["i_deg"])
        ecc = read(self.ecc_var, "偏心率", userdb.FIELD_LIMITS["e"])
        raan = read(self.raan_var, "RAAN °", userdb.FIELD_LIMITS["raan_deg"])
        argp = read(self.argp_var, "近地点幅角 °", userdb.FIELD_LIMITS["argp_deg"])
        ltan = read(self.ltan_var, "LTAN h", userdb.FIELD_LIMITS["ltan_h"])
        probe = copy.deepcopy(self.rec)
        probe.update({
            "id": self._current_id if self._from_library else "",
            "name": self.name_var.get().strip(),
            "type": self.type_var.get().strip().upper() or "LEO",
            "alt_km": alt,
            "i_deg": inc,
            "e": 0.0 if ecc is None else ecc,
            "raan_deg": raan,
            "argp_deg": argp,
            "ltan_h": ltan,
            "epoch_utc": self.epoch_var.get().strip(),
            "confidence": self.confidence_var.get().strip() or "中",
            "note": self.note_box.get_text().strip(),
        })
        if not self._from_library:
            probe["id"] = ""
        return probe, errors

    def _collect(self) -> Tuple[Dict[str, Any], List[Tuple[str, str, str]]]:
        probe, parse_errors = self._form_probe(collect_errors=True)
        if parse_errors:
            raise ValueError("\n".join(parse_errors))
        problems = userdb.validate_orbit(probe)
        return probe, problems

    def _apply_record(self, rec: Dict[str, Any], *, from_library: bool) -> bool:
        if not self._confirm_discard():
            return False
        try:
            normalized = userdb.normalize_orbit(copy.deepcopy(rec))
        except Exception as exc:  # noqa: BLE001
            widgets.show_error(self, "载入失败", widgets.explain_error(exc))
            return False
        normalized.pop("_shadow_builtin", None)
        self.rec = normalized
        self._from_library = bool(from_library)
        self._current_id = str(normalized.get("id") or "") if from_library else ""
        self._fill_form(self.rec)
        self._detect_builtin_shadow()
        self._dirty = False
        self._update_preview()
        self._update_delete_button()
        return True

    # ------------------------------------------------------------------ #
    # 预览
    # ------------------------------------------------------------------ #
    def _update_preview(self) -> None:
        if not hasattr(self, "preview_box"):
            return
        probe, errors = self._form_probe(collect_errors=True)
        problems = userdb.validate_orbit(probe)
        segments: List[Tuple[str, Sequence[str]]] = []
        name = str(probe.get("name") or "（未命名）")
        kind = str(probe.get("type") or "LEO")
        segments.append(("%s\n" % name, ("title",)))
        segments.append(("类型：%s%s\n\n" % (kind, "（%s）" % KIND_CN[kind]
                                            if kind in KIND_CN else ""),
                         ("muted",)))
        if errors:
            segments.append(("输入格式：\n", ("title",)))
            for message in errors:
                segments.append(("  ✗ %s\n" % message, ("err",)))
            segments.append(("\n", ()))
        segments.append(("校验：\n", ("title",)))
        if not problems:
            segments.append(("  ✓ 未发现明显问题，可用于参考轨道。\n", ("ok",)))
        else:
            for level, field, message in problems:
                tag = "err" if level == userdb.ERROR else "warn"
                mark = "✗" if level == userdb.ERROR else "!"
                prefix = "[%s] " % field if field else ""
                segments.append(("  %s %s%s\n" % (mark, prefix, message), (tag,)))
        segments.append(("\n派生量（二体近似）：\n", ("title",)))
        alt = _as_float(probe.get("alt_km"))
        inc = _as_float(probe.get("i_deg"))
        ecc = _as_float(probe.get("e")) or 0.0
        if alt is not None and alt > 0 and 0.0 <= ecc < 1.0:
            r_a = RE_KM + alt
            a = r_a / (1.0 + ecc)
            r_p = a * (1.0 - ecc)
            period_min = 2.0 * math.pi * math.sqrt(a ** 3 / MU) / 60.0
            segments.append(("  半长轴       : %.1f km\n" % a, ()))
            segments.append(("  近地点 / 远地点: %.1f / %.1f km\n"
                             % (r_p - RE_KM, r_a - RE_KM), ()))
            segments.append(("  周期         : %.2f min（约 %.2f h）\n"
                             % (period_min, period_min / 60.0), ()))
            if ecc > 0.001:
                segments.append(("  说明：按「高度 = 远地点」处理，a = r_a/(1+e)。\n",
                                 ("muted",)))
            if inc is None:
                segments.append(("  倾角留空：太阳同步/发射方位角暂不判断。\n",
                                 ("muted",)))
            elif not (0.0 <= inc <= 180.0):
                segments.append(("  倾角超出 0~180°，无法判断太阳同步/方位角。\n",
                                 ("err",)))
            else:
                sso = 95.0 <= inc <= 105.0 and 150.0 <= alt <= 2000.0
                if sso:
                    segments.append(("  太阳同步     : 是（倾角 %.2f° 在 95~105° 带）\n"
                                     % inc, ("ok",)))
                else:
                    segments.append(("  太阳同步     : 否（SSO 通常需 95~105° 且"
                                     "高度 150~2000 km）\n", ("muted",)))
                try:
                    from app.core.infer import azimuth_from_orbit
                    azimuth = azimuth_from_orbit(inc, QIANJIU_LAT) % 360.0
                    segments.append(("  发射方位角粗估: %.1f°（按酒泉 %.2f°N，"
                                     "按无平面机动的理想射向）\n"
                                     % (azimuth, QIANJIU_LAT), ("muted",)))
                except Exception:  # noqa: BLE001
                    pass
        else:
            segments.append(("  填写「高度 km」与「偏心率」后显示周期/近远地点。\n",
                             ("muted",)))
        ltan = _as_float(probe.get("ltan_h"))
        raan = _as_float(probe.get("raan_deg"))
        if ltan is not None:
            segments.append(("  LTAN        : %.2f h\n" % ltan, ()))
        if raan is not None:
            segments.append(("  RAAN        : %.2f°\n" % raan, ()))
        if ltan is None and raan is None:
            segments.append(("  轨道平面    : 未给 RAAN/LTAN，暂不计算平面发射窗口。\n",
                             ("muted",)))
        self._write_segments(segments)

    def _write_segments(self, segments: Sequence[Tuple[str, Sequence[str]]]) -> None:
        text = self.preview_box.text
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

    # ------------------------------------------------------------------ #
    # 工具条动作
    # ------------------------------------------------------------------ #
    def _on_template_selected(self) -> None:
        name = self.tpl_var.get().strip()
        item = next((o for o in self._template_items
                     if str(o.get("name")) == name), None)
        if item is None:
            return
        previous = getattr(self, "_tpl_applied", "")
        if not self._apply_record(item, from_library=False):
            self.tpl_var.set(previous)
            return
        self.tpl_var.set(name)
        self._tpl_applied = name
        self.status_label.configure(
            text="已载入模板：%s（h=%s km，i=%s°）。名称不改时保存会覆盖内置同 id 记录，"
                 "改名则另存为新轨道。"
                 % (name, _num(item.get("alt_km"), 1), _num(item.get("i_deg"), 2)),
            foreground=theme.FG_MUTED)

    def _on_load_library(self) -> None:
        items = [o for o in userdb.list_orbits() if str(o.get("name") or "").strip()]
        if not items:
            widgets.show_info(self, "我的轨道库为空",
                              "还没有保存过参考轨道。可以先填好参数，再点"
                              "「保存到我的轨道库」。")
            return
        picked = _OrbitPicker(self, items).result
        if not picked:
            return
        if not self._apply_record(picked, from_library=True):
            return
        self.tpl_var.set(str(picked.get("name") or ""))
        self._tpl_applied = str(picked.get("name") or "")
        self.status_label.configure(
            text="已从我的轨道库载入：%s。保存会更新同一条记录。"
                 % (picked.get("name") or "—"),
            foreground=theme.FG_MUTED)

    def _on_tle_import(self) -> None:
        parsed = _TleDialog(self).result
        if not parsed:
            return
        orbit, note = parsed
        rec = copy.deepcopy(self.rec)
        rec.update(orbit)
        rec["type"] = "TLE"
        rec["confidence"] = rec.get("confidence") or "中"
        if not str(rec.get("name") or "").strip():
            rec["name"] = "TLE 参考轨道"
        if self._apply_record(rec, from_library=False):
            self.tpl_var.set("")
            self._tpl_applied = ""
            self.status_label.configure(text="TLE 解析：%s" % note,
                                        foreground=theme.OK)

    def _update_delete_button(self) -> None:
        if not hasattr(self, "delete_btn"):
            return
        exists = False
        if self._current_id:
            exists = any(str(o.get("id")) == self._current_id
                         for o in userdb.list_orbits())
        try:
            self.delete_btn.configure(state="normal" if exists else "disabled")
        except tk.TclError:
            pass

    def _on_delete(self) -> None:
        if not self.allow_save:
            return
        if not self._current_id:
            widgets.show_info(self, "无法删除", "当前轨道不在我的轨道库中。")
            return
        if not any(str(o.get("id")) == self._current_id
                   for o in userdb.list_orbits()):
            widgets.show_info(self, "无法删除", "我的轨道库里已经没有这条记录。")
            self._update_delete_button()
            return
        if not widgets.ask_yes_no(
                self, "删除轨道",
                "确定从我的轨道库删除「%s」？\n删除后可以通过「保存到我的轨道库」"
                "重新创建。" % (self.name_var.get().strip() or self._current_id)):
            return
        try:
            ok = userdb.delete_orbit(self._current_id)
        except Exception as exc:  # noqa: BLE001
            widgets.show_error(self, "删除失败", widgets.explain_error(exc))
            return
        if not ok:
            widgets.show_warning(self, "删除失败", "没有找到这条记录。")
            return
        self._current_id = ""
        self._from_library = False
        self.rec["id"] = ""
        self._update_delete_button()
        self.status_label.configure(
            text="已从我的轨道库删除。当前表单仍可继续编辑，"
                 "需要时可再保存为新记录。", foreground=theme.WARN)
        widgets.show_info(self, "已删除", "已从我的轨道库删除该轨道。")

    # ------------------------------------------------------------------ #
    # 保存 / 使用 / 取消
    # ------------------------------------------------------------------ #
    def _on_use(self) -> None:
        try:
            probe, problems = self._collect()
        except ValueError as exc:
            widgets.show_error(self, "参数有误", str(exc))
            return
        errors = [p for p in problems if p[0] == userdb.ERROR]
        if errors:
            self._update_preview()
            widgets.show_error(self, "存在错误，不能使用",
                               "请先修正 %d 个错误：\n%s"
                               % (len(errors), errors[0][2]))
            return
        try:
            result = userdb.normalize_orbit(probe)
        except Exception as exc:  # noqa: BLE001
            widgets.show_error(self, "轨道记录有误", widgets.explain_error(exc))
            return
        result.pop("_shadow_builtin", None)
        # 仅本次任务：不宣称是用户库记录（主窗口用 _user 区分）
        result.pop("_user", None)
        self.result = result
        self.mode = "session"
        self.saved = False
        self._dirty = False
        self.close()

    def _on_save(self) -> None:
        if not self.allow_save:
            return
        try:
            probe, problems = self._collect()
        except ValueError as exc:
            widgets.show_error(self, "参数有误", str(exc))
            return
        errors = [p for p in problems if p[0] == userdb.ERROR]
        if errors:
            self._update_preview()
            widgets.show_error(
                self, "存在错误，不能保存",
                "请先修正 %d 个错误：\n%s\n\n提示：错误会阻止保存，"
                "右侧预览里能看到逐条原因。" % (len(errors), errors[0][2]))
            return
        warns = [p for p in problems if p[0] != userdb.ERROR]
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
                # 名称没改：同 id 写入用户库，覆盖内置模板（userdb 合并规则）
                probe["id"] = self._shadow_builtin_id
                saved = userdb.save_orbit(probe, new=False)
            else:
                saved = userdb.save_orbit(probe, new=not self._from_library)
        except Exception as exc:  # noqa: BLE001
            widgets.show_error(self, "保存失败", widgets.explain_error(exc))
            return
        self.rec = saved
        self.rec.pop("_shadow_builtin", None)
        self.result = self.rec
        self.saved = True
        self.mode = "user_db"
        self._from_library = True
        self._current_id = str(self.rec.get("id") or "")
        self._dirty = False
        self._update_delete_button()
        self.close()

    def _on_cancel(self) -> None:
        self.result = None
        self.close()

    def _build_buttons(self) -> None:
        ttk.Label(self.button_bar,
                  text="「使用此轨道」只返回给当前任务，不写入用户库。",
                  style="Hint.TLabel").pack(side="left")
        ttk.Button(self.button_bar, text="取消", command=self._on_cancel).pack(
            side="right")
        use_style = "Accent.TButton" if not self.allow_save else "TButton"
        ttk.Button(self.button_bar, text="使用此轨道", style=use_style,
                   command=self._on_use).pack(side="right", padx=(0, 8))
        if self.allow_save:
            ttk.Button(self.button_bar, text="保存到我的轨道库",
                       style="Accent.TButton", command=self._on_save).pack(
                side="right", padx=(0, 8))

    # ------------------------------------------------------------------ #
    # 关闭
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        if getattr(self, "_closing", False):
            return
        if getattr(self, "_dirty", False):
            if not widgets.ask_yes_no(
                    self, "未保存的修改",
                    "当前轨道还有未保存的修改，关闭会丢弃它们。\n确定关闭？"):
                return
        self._closing = True
        super().close()


# --------------------------------------------------------------------------- #
# 我的轨道库选择框
# --------------------------------------------------------------------------- #
class _OrbitPicker(tk.Toplevel):
    """选择「我的轨道库」里的一条轨道（带搜索）。"""

    def __init__(self, parent: tk.Misc, items: Sequence[Dict[str, Any]]) -> None:
        super().__init__(parent)
        self.result: Optional[Dict[str, Any]] = None
        self._items = [it for it in items if isinstance(it, dict)]
        self._shown: List[Dict[str, Any]] = []

        self.title("从我的轨道库载入")
        self.transient(parent)
        self.configure(background=theme.WINDOW_BG)
        self.resizable(True, True)
        body = ttk.Frame(self, padding=(14, 12))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        ttk.Label(body, text="从我的轨道库载入", style="PageTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(body, text="选择一条已保存的轨道；载入后可继续编辑并更新同一条记录。",
                  style="Subtitle.TLabel", wraplength=420, justify="left").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(2, 8))
        search_box = ttk.Frame(body)
        search_box.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        search_box.columnconfigure(1, weight=1)
        ttk.Label(search_box, text="搜索", style="Hint.TLabel").grid(
            row=0, column=0, padx=(0, 6))
        self.search_var = tk.StringVar()
        ttk.Entry(search_box, textvariable=self.search_var).grid(
            row=0, column=1, sticky="ew")
        self.search_var.trace_add("write", lambda *a: self._refresh())

        list_box = ttk.Frame(body)
        list_box.grid(row=3, column=0, columnspan=2, sticky="nsew")
        list_box.columnconfigure(0, weight=1)
        list_box.rowconfigure(0, weight=1)
        self.listbox = tk.Listbox(list_box, height=12, activestyle="none",
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
        widgets.center_window(self, parent, 520, 480)
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

    def _grab(self) -> None:
        try:
            self.grab_set()
            self.listbox.focus_set()
        except Exception:  # noqa: BLE001
            pass

    def _label(self, item: Dict[str, Any]) -> str:
        name = str(item.get("name") or "未命名轨道")
        alt = _num(item.get("alt_km"), 1)
        inc = _num(item.get("i_deg"), 2)
        return "%s    · h=%s km / i=%s° / %s" % (
            name, alt or "—", inc or "—", item.get("type") or "—")

    def _refresh(self) -> None:
        query = self.search_var.get().strip().lower()
        self.listbox.delete(0, "end")
        self._shown = []
        for item in self._items:
            label = self._label(item)
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
            widgets.show_info(self.master, "未选择", "请先在列表中选择一条轨道。")
            return
        idx = int(sel[0])
        if 0 <= idx < len(self._shown):
            self.result = self._shown[idx]
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()
