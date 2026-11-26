# -*- coding: utf-8 -*-
"""分页 ③：观测点（多行坐标粘贴 / 城市库选择 / 高程自动补全）。"""
from __future__ import annotations

import re
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Dict, List, Optional, Tuple

from app.core.model import MissionInput, ObserverInput

from . import theme, widgets

COLUMNS = [
    ("id", "编号", 70, "center"),
    ("name", "名称", 200, "w"),
    ("lat", "纬度（°）", 120, "e"),
    ("lon", "经度（°）", 120, "e"),
    ("alt", "高程（m）", 100, "e"),
    ("note", "备注", 260, "w"),
]


def _parse_observer_lines(text: str):
    """把多行文本解析成观测点；坐标行与城市名行分别处理。"""
    from app.core.notam_parse import parse_observers_text

    lines = [ln.strip() for ln in re.split(r"[\r\n]+", text or "") if ln.strip()]
    coord_lines: List[str] = []
    city_lines: List[str] = []
    for ln in lines:
        if len(re.findall(r"\d", ln)) >= 3:
            coord_lines.append(ln)
        else:
            city_lines.append(ln)
    out: List[ObserverInput] = []
    warns: List[str] = []
    if coord_lines:
        try:
            parsed, ws = parse_observers_text("\n".join(coord_lines))
            warns.extend(ws or [])
        except Exception as exc:  # noqa: BLE001
            parsed = []
            warns.append("坐标解析异常：%s" % exc)
        for p in parsed:
            out.append(ObserverInput(
                id="", name=str(p.get("name") or "").strip(),
                lat_deg=float(p.get("lat")), lon_deg=float(p.get("lon")),
                alt_m=p.get("alt_m"), note="粘贴解析"))
    if city_lines:
        try:
            from app.core import infer as I
        except Exception:  # noqa: BLE001
            I = None  # type: ignore
        for ln in city_lines:
            item, why = (None, "")
            if I is not None:
                try:
                    item, why = I.match_city(ln)
                except Exception:  # noqa: BLE001
                    item, why = None, ""
            if item:
                out.append(ObserverInput(
                    id="", name=str(item.get("name") or ln),
                    lat_deg=item.get("lat_deg"), lon_deg=item.get("lon_deg"),
                    alt_m=item.get("alt_m"),
                    note="城市库：%s" % (why or ln)))
            else:
                warns.append("无法解析该行（既不是坐标也不是城市库名称）：%s" % ln)
    return out, warns


class TabObserver(ttk.Frame):
    """③ 观测点。"""

    def __init__(self, master, app):
        super().__init__(master, padding=(14, 10))
        self.app = app
        self.items: List[ObserverInput] = []
        self._build()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        widgets.hint(
            self,
            "观测点用于天空轨迹、出现方位与全国可见性地图。可直接粘贴任何常见坐标格式"
            "（每行一个点），也可只写城市名；高程留空时按「城市库 → DEM → 0 m」自动补全。"
            "不填也不会丢成果：勾选下方「无观测点时用发射场」后，程序默认把发射场作为观测点。",
            wraplength=1000).grid(row=0, column=0, sticky="w", pady=(0, 6))

        bar = ttk.Frame(self)
        bar.grid(row=1, column=0, sticky="ew", pady=(2, 6))
        ttk.Button(bar, text="添加", command=self._add,
                   style="Accent.TButton").pack(side="left")
        ttk.Button(bar, text="编辑选中", command=self._edit).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="删除选中", command=self._delete).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="粘贴多行坐标解析", command=self._paste).pack(side="left",
                                                                       padx=(16, 0))
        ttk.Button(bar, text="从城市库选", command=self._pick_city).pack(side="left",
                                                                     padx=(8, 0))
        ttk.Button(bar, text="清空", command=self._clear).pack(side="left", padx=(8, 0))
        self.fmt_var = tk.StringVar(value=widgets.coord_format_label())
        self.fmt_combo = ttk.Combobox(bar, textvariable=self.fmt_var, width=9,
                                      state="readonly", values=["十进制度", "度分秒"])
        self.fmt_combo.pack(side="right", padx=(6, 10))
        ttk.Label(bar, text="坐标格式").pack(side="right")
        self.fmt_combo.bind("<<ComboboxSelected>>", lambda e: self._on_fmt_change())
        self.auto_alt_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="高程留空时由 DEM / 城市库自动补全",
                        variable=self.auto_alt_var).pack(side="right")
        self.auto_obs_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="无观测点时用发射场",
                        variable=self.auto_obs_var,
                        command=lambda: self.app.refresh_nav()).pack(side="right",
                                                                     padx=(0, 14))

        parsed = ttk.LabelFrame(self, text=" 观测点列表（双击可编辑） ")
        parsed.grid(row=3, column=0, sticky="nsew")
        parsed.rowconfigure(0, weight=1)
        parsed.columnconfigure(0, weight=1)
        container, self.tree = widgets.make_tree(parsed, COLUMNS, height=12)
        container.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self.tree.bind("<Double-1>", lambda e: self._edit())

        self.count_label = ttk.Label(self, text="已加入 0 个观测点", style="Hint.TLabel")
        self.count_label.grid(row=4, column=0, sticky="w", pady=(4, 0))
        widgets.hint(
            self,
            "坐标示例：39°54′15.1″N 116°24′26.6″E 44m；39.9042,116.4074；"
            "N39.9042 E116.4074；也可直接写「北京」「上海」等城市库名称。",
            wraplength=1000).grid(row=5, column=0, sticky="w")

    # ------------------------------------------------------------------ #
    def _on_fmt_change(self) -> None:
        fmt = "dms" if self.fmt_var.get() == "度分秒" else "dec"
        widgets.set_coord_format(fmt)
        self._refresh_tree()
        try:
            self.app.set_status("坐标格式已切换为「%s」：影响观测点添加/编辑与列表显示"
                                % widgets.coord_format_label(fmt))
        except Exception:  # noqa: BLE001
            pass

    def _refresh_tree(self) -> None:
        widgets.clear_tree(self.tree)
        for i, ob in enumerate(self.items):
            self.tree.insert("", "end", iid=str(i), values=(
                ob.id or ("obs%d" % (i + 1)),
                ob.name or "",
                widgets.format_coord(ob.lat_deg, "lat"),
                widgets.format_coord(ob.lon_deg, "lon"),
                widgets.fmt_num(ob.alt_m, 1),
                ob.note or "",
            ))
        self.count_label.configure(text="已加入 %d 个观测点" % len(self.items))

    def _add(self) -> None:
        dialog = ObserverDialog(self, None)
        if dialog.result is not None:
            self.items.append(dialog.result)
            self._refresh_tree()

    def _edit(self) -> None:
        idx = widgets.selected_index(self.tree)
        if idx is None:
            widgets.show_info(self, "未选中", "请先在表格中选中要编辑的观测点。")
            return
        if idx >= len(self.items):
            return
        dialog = ObserverDialog(self, self.items[idx])
        if dialog.result is not None:
            self.items[idx] = dialog.result
            self._refresh_tree()

    def _delete(self) -> None:
        idx = widgets.selected_index(self.tree)
        if idx is None:
            widgets.show_info(self, "未选中", "请先在表格中选中要删除的观测点。")
            return
        if 0 <= idx < len(self.items):
            self.items.pop(idx)
            self._refresh_tree()

    def _clear(self) -> None:
        if self.items and widgets.ask_yes_no(self, "清空观测点", "确定清空全部观测点？"):
            self.items = []
            self._refresh_tree()

    def _paste(self) -> None:
        dialog = PasteObserverDialog(self)
        if dialog.result:
            self.items.extend(dialog.result)
            self._refresh_tree()
            message = "已加入 %d 个观测点。" % len(dialog.result)
            if dialog.warns:
                message += "\n\n提示：\n" + "\n".join(dialog.warns[:8])
            widgets.show_info(self, "粘贴解析结果", message)

    def _pick_city(self) -> None:
        dialog = CityPickerDialog(self)
        if dialog.result:
            for c in dialog.result:
                self.items.append(ObserverInput(
                    id="", name=str(c.get("name") or ""),
                    lat_deg=c.get("lat_deg"), lon_deg=c.get("lon_deg"),
                    alt_m=c.get("alt_m"), note="城市库"))
            self._refresh_tree()

    # ------------------------------------------------------------------ #
    def auto_alt(self) -> bool:
        return bool(self.auto_alt_var.get())

    def auto_observer(self) -> bool:
        """无观测点时是否自动用发射场作为观测点（v4）。"""
        try:
            return bool(self.auto_obs_var.get())
        except Exception:  # noqa: BLE001
            return True

    def on_show(self) -> None:
        return

    def collect(self, inp: MissionInput) -> None:
        inp.auto_observer = self.auto_observer()
        inp.observers = []
        for i, ob in enumerate(self.items):
            ob.id = "obs%d" % (i + 1)
            inp.observers.append(ObserverInput(
                id=ob.id, name=ob.name, lat_deg=ob.lat_deg, lon_deg=ob.lon_deg,
                alt_m=ob.alt_m, note=ob.note or ""))
        self._refresh_tree()

    def load(self, inp: MissionInput) -> None:
        try:
            self.auto_obs_var.set(bool(getattr(inp, "auto_observer", True)))
        except Exception:  # noqa: BLE001
            pass
        self.items = [ObserverInput(id=o.id, name=o.name, lat_deg=o.lat_deg,
                                    lon_deg=o.lon_deg, alt_m=o.alt_m, note=o.note)
                      for o in (inp.observers or [])]
        self._refresh_tree()


# --------------------------------------------------------------------------- #
# 观测点编辑对话框
# --------------------------------------------------------------------------- #
class ObserverDialog(tk.Toplevel):
    """添加 / 编辑单个观测点。"""

    def __init__(self, parent, item: Optional[ObserverInput] = None):
        super().__init__(parent)
        self.result: Optional[ObserverInput] = None
        self.title("编辑观测点" if item is not None else "添加观测点")
        self.configure(background=theme.WINDOW_BG)
        self.transient(parent)
        self.resizable(False, False)

        body = ttk.Frame(self, padding=(16, 12))
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        self.name_var = tk.StringVar(value=(item.name if item else ""))
        self.fmt_var = tk.StringVar(value=widgets.coord_format_label())
        dms = self.fmt_var.get() == "度分秒"
        self.lat_var = tk.StringVar(
            value=widgets.format_coord(item.lat_deg, "lat") if item else "")
        self.lon_var = tk.StringVar(
            value=widgets.format_coord(item.lon_deg, "lon") if item else "")
        self.alt_var = tk.StringVar(value=widgets.fmt_num(item.alt_m, 1) if item else "")
        self.note_var = tk.StringVar(value=(item.note if item else ""))

        def row(r: int, label: str):
            ttk.Label(body, text=label).grid(row=r, column=0, sticky="e",
                                             padx=(0, 8), pady=5)
            frame = ttk.Frame(body)
            frame.grid(row=r, column=1, sticky="ew", pady=5)
            return frame

        f = row(0, "名称")
        ttk.Entry(f, textvariable=self.name_var, width=30).pack(side="left")
        f = row(1, "坐标格式")
        self.fmt_combo = ttk.Combobox(f, textvariable=self.fmt_var, width=10,
                                      state="readonly", values=["十进制度", "度分秒"])
        self.fmt_combo.pack(side="left")
        self.fmt_combo.bind("<<ComboboxSelected>>", lambda e: self._on_fmt_change())
        widgets.hint(f, "切换后已填经纬度会按新格式换算显示；两种格式都可直接输入").pack(
            side="left", padx=(8, 0))
        f = row(2, "纬度")
        ttk.Entry(f, textvariable=self.lat_var, width=26).pack(side="left")
        self._lat_hint = widgets.hint(f, "")
        self._lat_hint.pack(side="left", padx=(8, 0))
        f = row(3, "经度")
        ttk.Entry(f, textvariable=self.lon_var, width=26).pack(side="left")
        self._lon_hint = widgets.hint(f, "")
        self._lon_hint.pack(side="left", padx=(8, 0))
        f = row(4, "高程")
        ttk.Entry(f, textvariable=self.alt_var, width=14).pack(side="left")
        widgets.hint(f, "单位 m；留空=自动由城市库 / DEM 补全").pack(side="left", padx=(8, 0))
        f = row(5, "备注")
        ttk.Entry(f, textvariable=self.note_var, width=46).pack(side="left", fill="x",
                                                              expand=True)
        widgets.hint(body, "若经纬度留空，则按名称在城市库中查找（适用于「北京」这类名称）。"
                           "坐标系：WGS-84。",
                     wraplength=520).grid(row=6, column=1, sticky="w", pady=(2, 0))
        self._update_hints()

        bar = ttk.Frame(body)
        bar.grid(row=7, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="保存", command=self._save,
                   style="Accent.TButton").pack(side="right", padx=(0, 8))

        widgets.center_window(self, parent, 660, 350)
        self.after(60, widgets.safe_grab, self)
        self.wait_window(self)

    def _fmt_value(self, text, axis: str) -> str:
        """把当前输入按当前格式重写（解析失败则原样保留）。"""
        try:
            from app.core.notam_parse import parse_one_coord
            v = parse_one_coord(text, axis)
        except Exception:  # noqa: BLE001
            return str(text or "")
        return widgets.format_coord(v, axis) if v is not None else ""

    def _on_fmt_change(self) -> None:
        fmt = "dms" if self.fmt_var.get() == "度分秒" else "dec"
        widgets.set_coord_format(fmt)
        self.lat_var.set(self._fmt_value(self.lat_var.get(), "lat"))
        self.lon_var.set(self._fmt_value(self.lon_var.get(), "lon"))
        self._update_hints()

    def _update_hints(self) -> None:
        if self.fmt_var.get() == "度分秒":
            self._lat_hint.configure(text="北纬 N / 南纬 S，如 37°45′41.28″N")
            self._lon_hint.configure(text="东经 E / 西经 W，如 112°43′30.79″E")
        else:
            self._lat_hint.configure(text="十进制度，北纬为正（-90 ~ 90）")
            self._lon_hint.configure(text="十进制度，东经为正（-180 ~ 180）")

    def _save(self) -> None:
        try:
            name = self.name_var.get().strip()
            lat = widgets.parse_coord(self.lat_var.get(), "纬度", "lat")
            lon = widgets.parse_coord(self.lon_var.get(), "经度", "lon")
            alt = widgets.parse_optional_float(self.alt_var.get(), "高程", -500.0, 9000.0)
            if lat is None or lon is None:
                city = None
                if name:
                    try:
                        from app.core import infer as I
                        city, _why = I.match_city(name)
                    except Exception:  # noqa: BLE001
                        city = None
                if not city:
                    raise ValueError("请填写经纬度；或填写城市库中的城市名（如「北京」）"
                                     "后留空经纬度。")
                lat = lat if lat is not None else city.get("lat_deg")
                lon = lon if lon is not None else city.get("lon_deg")
                if alt is None and city.get("alt_m") is not None:
                    alt = float(city.get("alt_m"))
                name = name or str(city.get("name") or "")
            self.result = ObserverInput(id="", name=name, lat_deg=lat, lon_deg=lon,
                                        alt_m=alt, note=self.note_var.get().strip())
            self.destroy()
        except ValueError as exc:
            messagebox.showerror("观测点字段有误", str(exc), parent=self)
        except Exception as exc:  # noqa: BLE001
            widgets.write_error_log("保存观测点", exc)
            messagebox.showerror("保存失败", widgets.explain_error(exc), parent=self)

    def destroy(self) -> None:
        try:
            self.grab_release()
        except Exception:  # noqa: BLE001
            pass
        super().destroy()


# --------------------------------------------------------------------------- #
# 粘贴解析对话框
# --------------------------------------------------------------------------- #
class PasteObserverDialog(tk.Toplevel):
    """粘贴多行坐标（每行一个观测点）。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.result: List[ObserverInput] = []
        self.warns: List[str] = []
        self.title("粘贴多行坐标解析")
        self.configure(background=theme.WINDOW_BG)
        self.transient(parent)
        self.resizable(True, True)

        body = ttk.Frame(self, padding=(16, 12))
        body.pack(fill="both", expand=True)
        widgets.hint(
            body,
            "每行一个观测点，支持：39°54′15.1″N 116°24′26.6″E 44m、"
            "39.9042,116.4074、N39.9042 E116.4074、"
            "也可某行只写城市名（如「北京」）。",
            wraplength=620).pack(anchor="w", pady=(0, 6))
        self.box = widgets.TextBox(body, height=12, wrap="word")
        self.box.pack(fill="both", expand=True)
        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(10, 0))
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="解析并加入", command=self._ok,
                   style="Accent.TButton").pack(side="right", padx=(0, 8))
        widgets.center_window(self, parent, 720, 480)
        self.after(60, widgets.safe_grab, self)
        self.wait_window(self)

    def _ok(self) -> None:
        text = self.box.get_text().strip()
        if not text:
            messagebox.showinfo("没有内容", "请先粘贴观测点文本。", parent=self)
            return
        try:
            items, warns = _parse_observer_lines(text)
        except Exception as exc:  # noqa: BLE001
            widgets.write_error_log("解析观测点", exc)
            messagebox.showerror("解析失败", widgets.explain_error(exc), parent=self)
            return
        if not items:
            messagebox.showwarning("未解析出观测点",
                                   "\n".join(warns[:8]) or "请检查坐标格式。", parent=self)
            return
        self.result, self.warns = items, warns
        self.destroy()

    def destroy(self) -> None:
        try:
            self.grab_release()
        except Exception:  # noqa: BLE001
            pass
        super().destroy()


# --------------------------------------------------------------------------- #
# 城市库选择对话框
# --------------------------------------------------------------------------- #
class CityPickerDialog(tk.Toplevel):
    """从内置城市库选择观测点（可多选）。"""

    COLUMNS = [("name", "城市", 150, "w"), ("province", "省份", 130, "w"),
               ("lat", "纬度", 100, "e"), ("lon", "经度", 100, "e"),
               ("alt", "海拔 m", 80, "e")]

    def __init__(self, parent):
        super().__init__(parent)
        self.result: List[Dict[str, Any]] = []
        self._cities: List[Dict[str, Any]] = []
        self.title("从城市库选择观测点")
        self.configure(background=theme.WINDOW_BG)
        self.transient(parent)
        self.resizable(True, True)

        body = ttk.Frame(self, padding=(14, 10))
        body.pack(fill="both", expand=True)
        body.rowconfigure(2, weight=1)
        body.columnconfigure(0, weight=1)

        top = ttk.Frame(body)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(top, text="搜索").pack(side="left")
        self.search_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.search_var, width=24).pack(side="left",
                                                                  padx=(6, 0))
        self.search_var.trace_add("write", lambda *a: self._refresh())
        self.count_label = ttk.Label(top, text="", style="Hint.TLabel")
        self.count_label.pack(side="left", padx=(12, 0))
        widgets.hint(top, "可按住 Ctrl / Shift 多选；双击直接添加。").pack(side="right")

        container, self.tree = widgets.make_tree(body, self.COLUMNS, height=14,
                                                 selectmode="extended")
        container.grid(row=2, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", lambda e: self._ok())

        bar = ttk.Frame(body)
        bar.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(bar, text="关闭", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="添加选中", command=self._ok,
                   style="Accent.TButton").pack(side="right", padx=(0, 8))

        self._load()
        widgets.center_window(self, parent, 720, 560)
        self.after(60, widgets.safe_grab, self)
        self.wait_window(self)

    def _load(self) -> None:
        try:
            from app.core import infer as I
            self._cities = list(I.city_list())
        except Exception as exc:  # noqa: BLE001
            widgets.write_error_log("读取城市库", exc)
            self._cities = []
        if not self._cities:
            widgets.show_info(self, "城市库为空",
                              "未读取到城市库（app/data/cities.json）。\n"
                              "可直接在观测点列表里粘贴经纬度，或到「设置」检查数据目录。")
        self._refresh()

    def _refresh(self) -> None:
        q = self.search_var.get().strip().lower()
        widgets.clear_tree(self.tree)
        shown = 0
        for c in self._cities:
            text = " ".join(str(c.get(k) or "") for k in ("name", "province", "aliases"))
            if q and q not in text.lower():
                continue
            self.tree.insert("", "end", iid=str(id(c)), values=(
                c.get("name") or "", c.get("province") or "",
                widgets.fmt_num(c.get("lat_deg"), 5),
                widgets.fmt_num(c.get("lon_deg"), 5),
                widgets.fmt_num(c.get("alt_m"), 0)))
            shown += 1
            if shown >= 400:
                break
        self.count_label.configure(text="共 %d 个城市，显示 %d 个" % (
            len(self._cities), shown))

    def _ok(self) -> None:
        sel = self.tree.selection()
        if not sel:
            widgets.show_info(self, "未选中", "请先选中城市（可多选）。")
            return
        by_iid = {str(id(c)): c for c in self._cities}
        chosen = [by_iid[i] for i in sel if i in by_iid]
        if chosen:
            self.result = chosen
            self.destroy()

    def destroy(self) -> None:
        try:
            self.grab_release()
        except Exception:  # noqa: BLE001
            pass
        super().destroy()
