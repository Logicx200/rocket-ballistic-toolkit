# -*- coding: utf-8 -*-
"""分页 ②：航警（NOTAM）—— 粘贴原文解析 / 手工增删改 / 预测落区。"""
from __future__ import annotations

import re
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk
from typing import List, Optional

from app.core.model import MissionInput, NotamInput

from . import theme, widgets

STAGES = ["一级", "二级", "整流罩", "助推器", "未知"]
COLUMNS = [
    ("id", "编号", 120, "w"),
    ("b", "开始（UTC）", 165, "w"),
    ("c", "结束（UTC）", 165, "w"),
    ("stage", "残骸类型", 90, "center"),
    ("fir", "FIR", 80, "center"),
    ("n", "顶点数", 70, "center"),
]


def _display_time(iso: str) -> str:
    return (iso or "").replace("T", " ").replace("Z", "").strip()


def _norm_time(text: str, label: str) -> str:
    """把 ``2611182243`` / ISO 时间统一成 ``YYYY-MM-DDTHH:MM:SSZ``。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    from app.core.notam_parse import parse_notam_dt, iso_utc, to_iso_z
    if raw.isdigit() and len(raw) in (10, 12):
        iso = parse_notam_dt(raw)
        if iso:
            return iso
    dt = iso_utc(raw)
    if dt is None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
                    "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        raise ValueError("%s 格式无法识别：「%s」\n可用 2611182243（NOTAM 压缩时间，UTC）"
                         "或 2026-11-18 22:43:00。" % (label, raw))
    return to_iso_z(dt)


def _as_notam(d: dict) -> NotamInput:
    return NotamInput(
        id=str(d.get("id") or ""), b_utc=str(d.get("b_utc") or ""),
        c_utc=str(d.get("c_utc") or ""), q_line=str(d.get("q_line") or ""),
        fir=str(d.get("fir") or ""),
        polygon_latlon=[[float(a), float(b)] for a, b in (d.get("polygon_latlon") or [])],
        stage_hint=str(d.get("stage_hint") or ""),
        raw_text=str(d.get("raw_text") or ""), verified=str(d.get("verified") or ""))


class TabNotam(ttk.Frame):
    """② 航警（NOTAM）。"""

    def __init__(self, master, app):
        super().__init__(master, padding=(14, 10))
        self.app = app
        self.items: List[NotamInput] = []
        self._build()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.rowconfigure(4, weight=2)

        widgets.hint(
            self,
            "把航警原文整段粘贴到下面，点「解析原文」自动识别编号、B)/C) 时刻、Q 行、多边形"
            "与残骸类型；也可手工添加/修改。上升段任务用「残骸落区」，返回任务用「返回/回收"
            "着陆区」，程序会按关键字（返回/回收/着陆）自动区分。",
            wraplength=1000).grid(row=0, column=0, sticky="w", pady=(0, 6))

        raw_frame = ttk.LabelFrame(self, text=" 航警原文 ")
        raw_frame.grid(row=1, column=0, sticky="nsew")
        raw_frame.rowconfigure(0, weight=1)
        raw_frame.columnconfigure(0, weight=1)
        self.raw_box = widgets.TextBox(raw_frame, height=8, wrap="word")
        self.raw_box.grid(row=0, column=0, sticky="nsew", padx=6, pady=(6, 2))

        bar = ttk.Frame(raw_frame)
        bar.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 6))
        self.parse_btn = ttk.Button(bar, text="解析原文", command=self.parse_now,
                                    style="Accent.TButton")
        self.parse_btn.pack(side="left")
        ttk.Button(bar, text="清空原文", command=self._clear_raw).pack(side="left", padx=(8, 0))
        self.count_label = ttk.Label(bar, text="已加入 0 条航警", style="Hint.TLabel")
        self.count_label.pack(side="left", padx=(14, 0))

        opt = ttk.Frame(self)
        opt.grid(row=2, column=0, sticky="ew", pady=(6, 4))
        self.predict_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="无可用航警时，按标称弹道预测落区（提示性，无官方依据）",
                        variable=self.predict_var).pack(side="left")
        widgets.hint(opt, "勾选后若航警列表为空，程序先仿真再按预测落点生成提示性落区；"
                          "注意这不是官方禁区。").pack(side="left", padx=(12, 0))

        parsed = ttk.LabelFrame(self, text=" 已解析航警（双击可编辑） ")
        parsed.grid(row=4, column=0, sticky="nsew", pady=(4, 4))
        parsed.rowconfigure(1, weight=1)
        parsed.columnconfigure(0, weight=1)

        bar2 = ttk.Frame(parsed)
        bar2.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 2))
        ttk.Button(bar2, text="添加", command=self._add).pack(side="left")
        ttk.Button(bar2, text="编辑选中", command=self._edit).pack(side="left", padx=(8, 0))
        ttk.Button(bar2, text="删除选中", command=self._delete).pack(side="left", padx=(8, 0))
        ttk.Button(bar2, text="清空列表", command=self._clear_items).pack(side="left", padx=(8, 0))
        widgets.hint(bar2, "残骸类型错会导致反演不收敛；字段不全的航警会提示但不参与落点判定。").pack(
            side="left", padx=(12, 0))

        container, self.tree = widgets.make_tree(parsed, COLUMNS, height=8)
        container.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self.tree.bind("<Double-1>", lambda e: self._edit())

        warn_frame = ttk.LabelFrame(self, text=" 解析提示 ")
        warn_frame.grid(row=5, column=0, sticky="nsew")
        warn_frame.rowconfigure(0, weight=1)
        warn_frame.columnconfigure(0, weight=1)
        self.warn_box = widgets.TextBox(warn_frame, height=4, wrap="word", readonly=True)
        self.warn_box.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self.warn_box.set_text("（尚未解析）")

    # ------------------------------------------------------------------ #
    # 解析 / 列表
    # ------------------------------------------------------------------ #
    def parse_now(self, silent: bool = False) -> int:
        """解析原文并加入列表（按编号去重），返回新增条数。"""
        text = self.raw_box.get_text().strip()
        if not text:
            if not silent:
                widgets.show_info(self, "没有原文", "请先粘贴航警原文，再点「解析原文」。")
            return 0
        try:
            from app.core.notam_parse import parse_notams
            parsed, warns = parse_notams(text)
        except Exception as exc:  # noqa: BLE001
            widgets.write_error_log("解析航警", exc)
            if not silent:
                widgets.show_error(self, "解析失败", widgets.explain_error(exc))
            return 0
        existing = {it.id for it in self.items if it.id}
        added = skipped = bad = 0
        for d in parsed:
            nid = str(d.get("id") or "")
            if nid and nid in existing:
                skipped += 1
                continue
            item = _as_notam(d)
            self.items.append(item)
            if nid:
                existing.add(nid)
            added += 1
            if not item.is_usable():
                bad += 1
        self._refresh_tree()
        lines = list(warns or [])
        if not parsed:
            lines.insert(0, "未能从原文解析出航警，请检查格式（编号 / B) / C) / 多边形是否齐全）。")
        elif added == 0 and skipped:
            lines.insert(0, "解析出 %d 条，但都已在列表中（按编号去重），未重复添加。" % skipped)
        else:
            lines.insert(0, "解析出 %d 条，新增 %d 条，重复跳过 %d 条%s。" % (
                len(parsed), added, skipped,
                ("，其中 %d 条字段不全（不会参与落点判定）" % bad) if bad else ""))
        self.warn_box.set_text("\n".join("- " + s for s in lines if s))
        self.app.set_status("航警解析：新增 %d 条，重复 %d 条" % (added, skipped))
        if not silent:
            if added == 0 and bad == 0 and skipped == 0:
                widgets.show_info(self, "解析结果", "\n".join(lines[:8]))
        return added

    def _refresh_tree(self) -> None:
        widgets.clear_tree(self.tree)
        for i, it in enumerate(self.items):
            tags = () if it.is_usable() else ("warn",)
            self.tree.insert("", "end", iid=str(i), values=(
                it.id or "（未识别）",
                _display_time(it.b_utc) or "（缺）",
                _display_time(it.c_utc) or "（缺）",
                it.stage_hint or "未标",
                it.fir or "-",
                len(it.polygon_latlon),
            ), tags=tags)
        self.count_label.configure(text="已加入 %d 条航警（可用 %d 条）" % (
            len(self.items), len([x for x in self.items if x.is_usable()])))

    def _clear_raw(self) -> None:
        self.raw_box.clear()

    def _add(self) -> None:
        dialog = NotamDialog(self, None)
        if dialog.result is not None:
            self.items.append(dialog.result)
            self._refresh_tree()

    def _edit(self) -> None:
        idx = widgets.selected_index(self.tree)
        if idx is None:
            widgets.show_info(self, "未选中", "请先在表格中选中要编辑的航警。")
            return
        if idx >= len(self.items):
            return
        dialog = NotamDialog(self, self.items[idx])
        if dialog.result is not None:
            self.items[idx] = dialog.result
            self._refresh_tree()

    def _delete(self) -> None:
        idx = widgets.selected_index(self.tree)
        if idx is None:
            widgets.show_info(self, "未选中", "请先在表格中选中要删除的航警。")
            return
        if 0 <= idx < len(self.items):
            self.items.pop(idx)
            self._refresh_tree()

    def _clear_items(self) -> None:
        if not self.items:
            return
        if widgets.ask_yes_no(self, "清空列表", "确定清空已加入的 %d 条航警？原文不会删除。"
                              % len(self.items)):
            self.items = []
            self._refresh_tree()

    # ------------------------------------------------------------------ #
    def on_show(self) -> None:
        return

    def collect(self, inp: MissionInput) -> None:
        inp.notams = [_as_notam(it.__dict__) for it in self.items]
        inp.notams_text = self.raw_box.get_text().strip()
        inp.notams_mode = "predict" if self.predict_var.get() else "auto"

    def load(self, inp: MissionInput) -> None:
        self.raw_box.set_text(inp.notams_text or "")
        self.predict_var.set(inp.notams_mode == "predict")
        self.items = [_as_notam(it.__dict__) for it in (inp.notams or [])]
        self._refresh_tree()
        self.warn_box.set_text("（尚未解析）")
        if not self.items and (inp.notams_text or "").strip():
            self.parse_now(silent=True)


# --------------------------------------------------------------------------- #
# 航警编辑对话框
# --------------------------------------------------------------------------- #
class NotamDialog(tk.Toplevel):
    """添加 / 编辑单条航警。"""

    def __init__(self, parent, item: Optional[NotamInput] = None):
        super().__init__(parent)
        self.result: Optional[NotamInput] = None
        self.title("编辑航警" if item is not None else "添加航警")
        self.configure(background=theme.WINDOW_BG)
        self.transient(parent)
        self.resizable(True, True)

        body = ttk.Frame(self, padding=(16, 12))
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(8, weight=1)

        self.id_var = tk.StringVar(value=(item.id if item else ""))
        self.b_var = tk.StringVar(value=_display_time(item.b_utc) if item else "")
        self.c_var = tk.StringVar(value=_display_time(item.c_utc) if item else "")
        self.q_var = tk.StringVar(value=(item.q_line if item else ""))
        self.fir_var = tk.StringVar(value=(item.fir if item else ""))
        stage = (item.stage_hint if item else "") or "未知"
        self.stage_var = tk.StringVar(value=stage)
        stages = list(STAGES)
        if stage not in stages:
            stages.append(stage)

        def row(r: int, label: str):
            ttk.Label(body, text=label).grid(row=r, column=0, sticky="e",
                                             padx=(0, 8), pady=4)
            frame = ttk.Frame(body)
            frame.grid(row=r, column=1, sticky="ew", pady=4)
            return frame

        f = row(0, "编号")
        ttk.Entry(f, textvariable=self.id_var, width=18).pack(side="left")
        widgets.hint(f, "例：A5897/26").pack(side="left", padx=(8, 0))

        f = row(1, "开始 B)")
        ttk.Entry(f, textvariable=self.b_var, width=24).pack(side="left")
        widgets.hint(f, "2611182243（NOTAM 时间，UTC）或 2026-11-18 22:43:00").pack(
            side="left", padx=(8, 0))

        f = row(2, "结束 C)")
        ttk.Entry(f, textvariable=self.c_var, width=24).pack(side="left")
        widgets.hint(f, "同上").pack(side="left", padx=(8, 0))

        f = row(3, "Q 行")
        ttk.Entry(f, textvariable=self.q_var, width=70).pack(side="left", fill="x",
                                                             expand=True)
        f = row(4, "FIR")
        ttk.Entry(f, textvariable=self.fir_var, width=10).pack(side="left")
        widgets.hint(f, "留空时自动取 Q 行首段").pack(side="left", padx=(8, 0))

        f = row(5, "残骸类型")
        ttk.Combobox(f, textvariable=self.stage_var, width=12, state="readonly",
                     values=stages).pack(side="left")
        widgets.hint(f, "与箭型级名/整流罩/助推器对应；不确定选「未知」").pack(
            side="left", padx=(8, 0))

        ttk.Label(body, text="多边形（每行一个顶点：纬度,经度）").grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(8, 2))
        self.poly_box = widgets.TextBox(body, height=8, wrap="word")
        self.poly_box.grid(row=7, column=1, sticky="nsew")
        body.rowconfigure(7, weight=1)
        widgets.hint(body, "支持十进制度（33.5667,110.5167）或度分秒符号写法；"
                          "NOTAM 原文里的 N333400E1103100 也可直接粘贴。",
                     wraplength=560).grid(row=9, column=1, sticky="w", pady=(4, 0))
        if item is not None and item.polygon_latlon:
            self.poly_box.set_text("\n".join("%.6f,%.6f" % (float(a), float(b))
                                             for a, b in item.polygon_latlon))

        bar = ttk.Frame(body)
        bar.grid(row=10, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="保存", command=self._save,
                   style="Accent.TButton").pack(side="right", padx=(0, 8))

        widgets.center_window(self, parent, 700, 590)
        self.after(60, widgets.safe_grab, self)
        self.wait_window(self)

    # ------------------------------------------------------------------ #
    def _parse_polygon(self):
        text = self.poly_box.get_text().strip()
        if not text:
            return [], []
        from app.core.notam_parse import parse_polygon
        return parse_polygon(text)

    def _save(self) -> None:
        try:
            nid = self.id_var.get().strip().upper()
            if not nid:
                raise ValueError("请填写航警编号（例：A5897/26）；自定义任务可写任意唯一编号。")
            if not re.match(r"^[A-Z0-9][A-Z0-9\-/_.]{1,24}$", nid):
                raise ValueError("航警编号应只含字母/数字/「-/_.」（例：A5897/26、"
                                 "RETURN-01）。")
            b = _norm_time(self.b_var.get(), "开始时刻 B)")
            c = _norm_time(self.c_var.get(), "结束时刻 C)")
            if b and c and b > c:
                raise ValueError("开始时刻 B) 不能晚于结束时刻 C)。")
            poly, _warns = self._parse_polygon()
            if len(poly) < 3:
                raise ValueError("多边形至少需要 3 个顶点（当前解析到 %d 个）。"
                                 "请在每行填写「纬度,经度」。" % len(poly))
            stage = self.stage_var.get().strip()
            if stage == "未知":
                stage = ""
            q_line = self.q_var.get().strip()
            fir = self.fir_var.get().strip().upper()
            if not fir and q_line:
                try:
                    from app.core.notam_parse import extract_qline
                    fir = extract_qline(q_line).get("fir") or ""
                except Exception:  # noqa: BLE001
                    fir = ""
            self.result = NotamInput(id=nid, b_utc=b, c_utc=c, q_line=q_line,
                                     fir=fir, polygon_latlon=poly,
                                     stage_hint=stage, raw_text="",
                                     verified="用户手工编辑")
            self.destroy()
        except ValueError as exc:
            messagebox.showerror("航警字段有误", str(exc), parent=self)
        except Exception as exc:  # noqa: BLE001
            widgets.write_error_log("保存航警", exc)
            messagebox.showerror("保存失败", widgets.explain_error(exc), parent=self)

    def destroy(self) -> None:
        try:
            self.grab_release()
        except Exception:  # noqa: BLE001
            pass
        super().destroy()
