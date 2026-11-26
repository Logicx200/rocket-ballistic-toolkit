# -*- coding: utf-8 -*-
"""分页 ⑤：空间站任务（CZ-2F 专用）。

职责
----
* 启用/关闭 CZ-2F 空间站交会模块；
* 选择空间站轨道数据来源：自动（内置 OEM→TLE→在线）/ 内置快照 / 手动 TLE / OEM 文件；
* 粘贴 TLE、导入 OEM/TLE 文件、校验数据、在线拉取；
* 交会参数：期望交会时长、ΔV 预算；
* 显示当前解析到的数据源/历元/覆盖区间（“校验/识别”按钮）。

数据解析统一走 ``engine/common/station.resolve()``（与流水线完全同源）。
"""
from __future__ import annotations

import os
import sys
import threading
import tkinter as tk
from datetime import datetime, timezone
from tkinter import filedialog, ttk
from typing import Any, Dict, Optional

from app.core import paths as P
from app.core.model import MissionInput

from . import theme, widgets

SOURCE_CHOICES = [
    ("自动（内置 OEM → TLE → 在线）", "auto"),
    ("仅用内置快照", "bundled"),
    ("仅用我粘贴/导入的 TLE", "tle"),
    ("仅用我导入的 OEM 文件", "oem"),
]


class TabStation(ttk.Frame):
    """⑤ 空间站任务（CZ-2F）。"""

    def __init__(self, master, app):
        super().__init__(master, padding=(14, 10))
        self.app = app
        self._user_touched = False
        self._last_info: Dict[str, Any] = {}
        self._build()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        self.columnconfigure(0, weight=1)

        box = ttk.LabelFrame(self, text=" 空间站交会对接（载人任务：单独计算，含入轨+对接） ")
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(1, weight=1)

        self.enabled_var = tk.BooleanVar(value=True)
        cb = ttk.Checkbutton(
            box, text="启用空间站交会模块（长征二号F / 神舟任务自动识别；其他箭型自动跳过）",
            variable=self.enabled_var, command=self._on_toggle)
        cb.grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(8, 4))

        ttk.Label(box, text="数据来源").grid(row=1, column=0, sticky="e", padx=(10, 8), pady=5)
        self.source_var = tk.StringVar(value=SOURCE_CHOICES[0][0])
        self.source_combo = ttk.Combobox(box, textvariable=self.source_var, width=34,
                                         state="readonly",
                                         values=[c[0] for c in SOURCE_CHOICES])
        self.source_combo.grid(row=1, column=1, sticky="w")
        self.source_combo.bind("<<ComboboxSelected>>", lambda e: self._on_source())

        ttk.Label(box, text="目标").grid(row=2, column=0, sticky="e", padx=(10, 8), pady=5)
        row = ttk.Frame(box)
        row.grid(row=2, column=1, columnspan=2, sticky="w")
        self.name_var = tk.StringVar(value="天宫空间站")
        ttk.Entry(row, textvariable=self.name_var, width=22).pack(side="left")
        ttk.Label(row, text="NORAD").pack(side="left", padx=(10, 4))
        self.norad_var = tk.StringVar(value="48274")
        ttk.Entry(row, textvariable=self.norad_var, width=10).pack(side="left")

        ttk.Label(box, text="TLE 两行").grid(row=3, column=0, sticky="ne", padx=(10, 8), pady=5)
        self.tle_text = tk.Text(box, height=4, width=96, wrap="none",
                                font=("Consolas", 9))
        self.tle_text.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(0, 10), pady=4)
        widgets.hint(box, "可粘贴 CelesTrak/空间站公告中的 TLE（含名称行也可）；"
                          "置空时按「数据来源」自动选择。",
                     wraplength=980).grid(row=4, column=1, columnspan=2, sticky="w",
                                          padx=(0, 10))

        ttk.Label(box, text="OEM 文件").grid(row=5, column=0, sticky="e", padx=(10, 8), pady=5)
        orow = ttk.Frame(box)
        orow.grid(row=5, column=1, columnspan=2, sticky="w")
        self.oem_var = tk.StringVar(value="")
        ttk.Entry(orow, textvariable=self.oem_var, width=70).pack(side="left")
        ttk.Button(orow, text="浏览…", command=self._pick_oem).pack(side="left", padx=(6, 0))

        btns = ttk.Frame(box)
        btns.grid(row=6, column=0, columnspan=3, sticky="w", padx=10, pady=(6, 2))
        ttk.Button(btns, text="校验/识别数据", command=lambda: self._probe(False)).pack(side="left")
        ttk.Button(btns, text="在线拉取", command=lambda: self._probe(True)).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="导入 TLE/OEM 文件…", command=self._import_file).pack(
            side="left", padx=(8, 0))

        self.status_var = tk.StringVar(value="尚未校验。点「校验/识别数据」可确认当前来源是否覆盖任务时刻。")
        self.status_label = tk.Label(box, textvariable=self.status_var, justify="left",
                                     anchor="w", wraplength=1000, fg="#1f4e79")
        self.status_label.grid(row=7, column=0, columnspan=3, sticky="ew", padx=12, pady=(4, 8))

        # ---- 交会参数 ----
        rd = ttk.LabelFrame(self, text=" 交会参数（工程估算，不是飞行程序） ")
        rd.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(rd, text="期望交会时长 (h)").grid(row=0, column=0, sticky="e",
                                                 padx=(10, 8), pady=6)
        self.rendezvous_var = tk.StringVar(value="6.5")
        ttk.Spinbox(rd, from_=1.0, to=48.0, increment=0.5, width=8,
                    textvariable=self.rendezvous_var).grid(row=0, column=1, sticky="w")
        ttk.Label(rd, text="ΔV 预算 (m/s)").grid(row=0, column=2, sticky="e",
                                              padx=(24, 8), pady=6)
        self.dv_var = tk.StringVar(value="300")
        ttk.Spinbox(rd, from_=50, to=2000, increment=10, width=8,
                    textvariable=self.dv_var).grid(row=0, column=3, sticky="w")
        ttk.Label(rd, text="平面容差 (°)").grid(row=0, column=4, sticky="e",
                                            padx=(24, 8), pady=6)
        self.tol_var = tk.StringVar(value="0.2")
        ttk.Spinbox(rd, from_=0.02, to=2.0, increment=0.05, width=8,
                    textvariable=self.tol_var).grid(row=0, column=5, sticky="w")
        ttk.Label(rd, text="搜索半宽 (h)").grid(row=0, column=6, sticky="e",
                                            padx=(24, 8), pady=6)
        self.search_var = tk.StringVar(value="48")
        ttk.Spinbox(rd, from_=6, to=240, increment=6, width=8,
                    textvariable=self.search_var).grid(row=0, column=7, sticky="w")
        widgets.hint(rd, "交会方案按「相位调整 + Hohmann + 末段相对导航」估算；"
                         "期望时长为软目标，若相位条件更好会给出更短的可行方案。",
                     wraplength=1000).grid(row=1, column=0, columnspan=8, sticky="w",
                                           padx=10, pady=(0, 8))

        widgets.hint(self, "空间站数据优先级：用户 OEM > 用户 TLE > 内置官方 CMSE OEM 快照 "
                           "> 内置 TLE > 在线（仅勾选「允许在线」时）。"
                           "内置 OEM 仅覆盖 2026-11-20 ~ 2026-11-27；其它日期请导入新 OEM/TLE。",
                     wraplength=1000, style="Warn.TLabel").grid(row=3, column=0, sticky="w",
                                                                pady=(8, 0))
        # ---- 返回舱返回（v3.5） ----
        rf = ttk.LabelFrame(self, text=" 返回舱返回（空间站轨道 + 返回航警，可选） ")
        rf.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        rf.columnconfigure(1, weight=1)
        self.return_enabled_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            rf, text="计算返回舱返回窗口/弹道/落地时间（需空间站轨道；可结合返回航警区）",
            variable=self.return_enabled_var,
            command=self._on_return_toggle).grid(row=0, column=0, columnspan=6,
                                                 sticky="w", padx=10, pady=(8, 4))
        self.return_only_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            rf, text="仅返回任务：不需要发射场/火箭/上升段（任务页的发射场可留空）",
            variable=self.return_only_var).grid(row=1, column=0, columnspan=6,
                                                 sticky="w", padx=10, pady=(0, 4))
        ttk.Label(rf, text="着陆场").grid(row=2, column=0, sticky="e", padx=(10, 8), pady=5)
        lrow = ttk.Frame(rf)
        lrow.grid(row=2, column=1, columnspan=5, sticky="w")
        self.return_name_var = tk.StringVar(value="四子王旗着陆场")
        ttk.Entry(lrow, textvariable=self.return_name_var, width=18).pack(side="left")
        ttk.Label(lrow, text="纬度").pack(side="left", padx=(10, 4))
        self.return_lat_var = tk.StringVar(value="42.27")
        ttk.Entry(lrow, textvariable=self.return_lat_var, width=9).pack(side="left")
        ttk.Label(lrow, text="经度").pack(side="left", padx=(8, 4))
        self.return_lon_var = tk.StringVar(value="111.70")
        ttk.Entry(lrow, textvariable=self.return_lon_var, width=9).pack(side="left")
        ttk.Label(lrow, text="半径 (km)").pack(side="left", padx=(8, 4))
        self.return_radius_var = tk.StringVar(value="50")
        ttk.Entry(lrow, textvariable=self.return_radius_var, width=6).pack(side="left")
        self.return_notams_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(rf, text="强制把当前全部航警区作为着陆区（不勾选时，带返回/回收/着陆 hint 的区仍会自动识别）",
                        variable=self.return_notams_var).grid(
            row=3, column=0, columnspan=6, sticky="w", padx=10, pady=3)
        ttk.Label(rf, text="搜索时段").grid(row=4, column=0, sticky="e", padx=(10, 8), pady=5)
        srow = ttk.Frame(rf)
        srow.grid(row=4, column=1, columnspan=5, sticky="w")
        self.return_start_var = tk.StringVar(value="")
        self.return_end_var = tk.StringVar(value="")
        ttk.Entry(srow, textvariable=self.return_start_var, width=20).pack(side="left")
        ttk.Label(srow, text="~").pack(side="left", padx=4)
        ttk.Entry(srow, textvariable=self.return_end_var, width=20).pack(side="left")
        ttk.Label(srow, text="（留空 = 返回航警 B)/C) 或 T0±6h）",
                  foreground="#666666").pack(side="left", padx=(8, 0))
        ttk.Label(rf, text="再入参数").grid(row=5, column=0, sticky="e", padx=(10, 8), pady=5)
        prow = ttk.Frame(rf)
        prow.grid(row=5, column=1, columnspan=5, sticky="w")
        ttk.Label(prow, text="ΔV (m/s)").pack(side="left")
        self.return_dv_var = tk.StringVar(value="120")
        ttk.Entry(prow, textvariable=self.return_dv_var, width=6).pack(side="left", padx=(4, 10))
        ttk.Label(prow, text="L/D").pack(side="left")
        self.return_ld_var = tk.StringVar(value="0.30")
        ttk.Entry(prow, textvariable=self.return_ld_var, width=6).pack(side="left", padx=(4, 10))
        ttk.Label(prow, text="倾侧角 (°，空=自动)").pack(side="left")
        self.return_bank_var = tk.StringVar(value="")
        ttk.Entry(prow, textvariable=self.return_bank_var, width=6).pack(side="left", padx=(4, 10))
        ttk.Label(prow, text="粗扫/细扫 (s)").pack(side="left")
        self.return_coarse_var = tk.StringVar(value="300")
        ttk.Entry(prow, textvariable=self.return_coarse_var, width=5).pack(side="left", padx=(4, 2))
        self.return_scan_var = tk.StringVar(value="10")
        ttk.Entry(prow, textvariable=self.return_scan_var, width=5).pack(side="left")
        self.return_hint_var = tk.StringVar(
            value="默认只对着陆场半径判定；若提供返回/回收/着陆航警区，则按多边形+ B)/C) 判定。"
                  " ΔV/倾侧角可自动扫描，粗扫 300 s、细扫 10 s 适合 1–2 h 航警窗口。")
        tk.Label(rf, textvariable=self.return_hint_var, justify="left", anchor="w",
                 wraplength=1000, fg="#555555").grid(row=6, column=0, columnspan=6,
                                                     sticky="ew", padx=12, pady=(2, 8))
        self._on_return_toggle()
        self._on_toggle()

    # ------------------------------------------------------------------ #
    def _on_source(self) -> None:
        self._user_touched = True

    def _on_toggle(self) -> None:
        state = "normal" if self.enabled_var.get() else "disabled"
        for w in (self.source_combo, self.tle_text):
            try:
                w.configure(state="readonly" if (w is self.source_combo and state == "normal")
                            else state)
            except tk.TclError:
                pass
        self._user_touched = True

    def _on_return_toggle(self) -> None:
        if not hasattr(self, "return_enabled_var"):
            return
        # 只做提示：具体字段留空时有默认值；不强制禁用（用户可能先填再勾选）
        if self.return_only_var.get():
            self.return_enabled_var.set(True)
        if self.return_enabled_var.get():
            self._user_touched = True

    def _pick_oem(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="选择空间站 OEM/星历文件",
            filetypes=[("OEM/星历", "*.oem *.dat *.txt"), ("所有文件", "*.*")])
        if path:
            self.oem_var.set(path)
            self.source_var.set(SOURCE_CHOICES[3][0])
            self._user_touched = True

    def _import_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="导入 TLE 或 OEM 文件",
            filetypes=[("TLE/OEM/文本", "*.tle *.txt *.oem *.dat"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            widgets.show_error(self, "读取失败", "%s\n%s" % (path, exc))
            return
        low = str(path).lower()
        if low.endswith((".oem", ".dat")) or "CCSDS_OEM" in text[:400]:
            self.oem_var.set(path)
            self.source_var.set(SOURCE_CHOICES[3][0])
            self.status_var.set("已导入 OEM 文件：%s" % path)
        else:
            self.tle_text.delete("1.0", "end")
            self.tle_text.insert("1.0", text.strip())
            self.source_var.set(SOURCE_CHOICES[2][0])
            self.status_var.set("已导入 TLE 文本：%s" % path)
        self._user_touched = True

    # ------------------------------------------------------------------ #
    def _probe(self, allow_network: bool) -> None:
        if not self.enabled_var.get():
            self.status_var.set("未启用空间站交会模块。")
            return
        cfg = self._station_cfg()
        t0 = self._mission_t0()
        self.status_var.set("正在解析空间站轨道数据…%s"
                            % ("（含在线拉取，可能等待数秒）" if allow_network else ""))
        app = self.app

        def worker():
            try:
                P.setup_env()
                P.ensure_syspath()
                from common import station as ST
                info = ST.resolve({"mission": {"t0_utc": t0}, "station": cfg},
                                  t_hint=_parse_iso(t0), allow_network=allow_network,
                                  logger=None, force=True).info()
                app._post("station", info=info)
            except Exception as exc:  # noqa: BLE001
                app._post("station", info={"error": str(exc)})

        threading.Thread(target=worker, name="station-probe", daemon=True).start()

    def show_station_info(self, info: Dict[str, Any]) -> None:
        self._last_info = dict(info or {})
        if info.get("error"):
            self.status_var.set("解析失败：%s" % info.get("error"))
            return
        cov = info.get("coverage_utc") or []
        self.status_var.set(
            "数据可用：%s；类型 %s；覆盖 %s；TLE 历元 %s。%s"
            % (info.get("source") or info.get("label") or "—",
               info.get("kind") or "—",
               ("%s ~ %s" % (cov[0], cov[1])) if len(cov) >= 2 else "（无区间限制）",
               info.get("epoch_utc") or "—",
               ("警告：" + info["warning"]) if info.get("warning") else ""))

    # ------------------------------------------------------------------ #
    def _mission_t0(self) -> str:
        try:
            text = self.app._pages["mission"].t0_var.get().strip()
            tz = "auto"
            if hasattr(self.app._pages["mission"], "tz_var"):
                label = self.app._pages["mission"].tz_var.get()
                tz = {"UTC（世界时）": "UTC", "北京时间（UTC+8）": "BJT"}.get(label, "auto")
            if text:
                from app.core.notam_parse import parse_t0_text
                dt, _tz, _note = parse_t0_text(text, tz, None)
                if dt is not None:
                    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:  # noqa: BLE001
            pass
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _station_cfg(self) -> Dict[str, Any]:
        source = dict(SOURCE_CHOICES).get(self.source_var.get(), "auto")
        tle = [ln.strip() for ln in self.tle_text.get("1.0", "end").splitlines() if ln.strip()]
        if len(tle) >= 3 and not tle[0].startswith("1 "):
            tle = tle[1:]
        return {
            "enabled": bool(self.enabled_var.get()),
            "name": self.name_var.get().strip() or "天宫空间站",
            "norad_id": int(widgets.parse_int(self.norad_var.get(), "NORAD", 48274, 1,
                                              999999)),
            "source": source,
            "oem_path": self.oem_var.get().strip(),
            "tle": tle[:2] if len(tle) >= 2 else [],
        }

    # ------------------------------------------------------------------ #
    def on_mission_type(self, key: str) -> None:
        """任务类型变化时的联动（由「任务与箭型」页调用，v4）。"""
        try:
            if key == "return":
                self.return_only_var.set(True)
                self.return_enabled_var.set(True)
                self.enabled_var.set(True)
                self.status_var.set("任务类型=返回舱返回：不需要发射场/火箭/上升段，"
                                    "本页设置空间站星历与着陆场即可。")
            elif key == "crewed":
                self.return_only_var.set(False)
                self.enabled_var.set(True)
                self.status_var.set("任务类型=载人发射：会计算空间站轨道 → 交会窗口 → "
                                    "入轨+对接时间线；需要空间站星历覆盖任务时刻。")
            else:
                self.return_only_var.set(False)
                if self.enabled_var.get() and not self._user_touched:
                    self.enabled_var.set(False)
                self.status_var.set("任务类型=常规发射：交会/返回设置会自动跳过"
                                    "（需要时可在本页手动启用）。")
        except Exception:  # noqa: BLE001
            pass

    def on_show(self) -> None:
        """进入本页时按任务箭型自动启用/禁用。"""
        try:
            rocket = str(self.app._pages["mission"].rocket_var.get() or "")
        except Exception:  # noqa: BLE001
            rocket = ""
        is_cz2f = ("2F" in rocket.upper() or "二号F" in rocket or "二号f" in rocket)
        if is_cz2f and not self._user_touched:
            self.enabled_var.set(True)
        if not is_cz2f and not self._user_touched:
            if self.return_enabled_var.get():
                self.status_var.set("当前任务不是 CZ-2F：交会对接会被跳过；返回舱返回仍可用"
                                    "（只要空间站轨道数据覆盖任务时段）。")
            else:
                self.status_var.set("当前任务不是 CZ-2F：交会/返回设置会被自动跳过"
                                    "（不影响通用上升段计算）。")
        self._on_toggle()

    def collect(self, inp: MissionInput) -> None:
        inp.station_enabled = bool(self.enabled_var.get())
        cfg = self._station_cfg()
        inp.station_source = cfg["source"]
        inp.station_name = cfg["name"]
        inp.station_norad = cfg["norad_id"]
        inp.station_oem_path = cfg["oem_path"]
        tle = cfg.get("tle") or []
        inp.station_tle_text = "\n".join(tle)
        inp.rendezvous_h = float(widgets.parse_optional_float(
            self.rendezvous_var.get(), "期望交会时长", 0.5, 72.0) or 6.5)
        inp.dv_budget_mps = float(widgets.parse_optional_float(
            self.dv_var.get(), "ΔV 预算", 10.0, 5000.0) or 300.0)
        inp.window_tolerance_deg = float(widgets.parse_optional_float(
            self.tol_var.get(), "平面容差", 0.01, 5.0) or 0.2)
        inp.window_search_hours = float(widgets.parse_optional_float(
            self.search_var.get(), "搜索半宽", 1.0, 240.0) or 48.0)
        # v3.5 返回舱
        inp.return_only = bool(self.return_only_var.get())
        inp.return_enabled = bool(self.return_enabled_var.get())
        inp.return_use_notams = bool(self.return_notams_var.get())
        inp.return_landing_name = self.return_name_var.get().strip() or "四子王旗着陆场"
        inp.return_landing_lat = widgets.parse_optional_float(
            self.return_lat_var.get(), "着陆场纬度", -90.0, 90.0)
        inp.return_landing_lon = widgets.parse_optional_float(
            self.return_lon_var.get(), "着陆场经度", -180.0, 180.0)
        inp.return_landing_radius_km = float(widgets.parse_optional_float(
            self.return_radius_var.get(), "着陆场半径", 1.0, 500.0) or 50.0)
        inp.return_search_start = self.return_start_var.get().strip()
        inp.return_search_end = self.return_end_var.get().strip()
        inp.return_dv_mps = float(widgets.parse_optional_float(
            self.return_dv_var.get(), "制动 ΔV", 10.0, 500.0) or 120.0)
        inp.return_ld_ratio = float(widgets.parse_optional_float(
            self.return_ld_var.get(), "升阻比", 0.0, 1.5) or 0.30)
        _bank = self.return_bank_var.get().strip()
        inp.return_bank_deg = (float(widgets.parse_optional_float(
            _bank, "倾侧角", -120.0, 120.0)) if _bank else None)
        inp.return_auto_dv = True
        inp.return_auto_bank = inp.return_bank_deg is None
        inp.return_coarse_step_s = float(widgets.parse_optional_float(
            self.return_coarse_var.get(), "粗扫步长", 30.0, 1800.0) or 300.0)
        inp.return_scan_step_s = float(widgets.parse_optional_float(
            self.return_scan_var.get(), "细扫步长", 1.0, 120.0) or 10.0)

    def load(self, inp: MissionInput) -> None:
        self.enabled_var.set(bool(getattr(inp, "station_enabled", True)))
        src = str(getattr(inp, "station_source", "auto") or "auto")
        for label, key in SOURCE_CHOICES:
            if key == src:
                self.source_var.set(label)
                break
        self.name_var.set(str(getattr(inp, "station_name", "") or "天宫空间站"))
        self.norad_var.set(str(getattr(inp, "station_norad", 48274) or 48274))
        self.oem_var.set(str(getattr(inp, "station_oem_path", "") or ""))
        self.tle_text.delete("1.0", "end")
        self.tle_text.insert("1.0", str(getattr(inp, "station_tle_text", "") or ""))
        # v3.5 返回舱
        self.return_only_var.set(bool(getattr(inp, "return_only", False)))
        self.return_enabled_var.set(bool(getattr(inp, "return_enabled", False)))
        self.return_notams_var.set(bool(getattr(inp, "return_use_notams", False)))
        self.return_name_var.set(str(getattr(inp, "return_landing_name", "") or
                                    "四子王旗着陆场"))
        self.return_lat_var.set("" if getattr(inp, "return_landing_lat", None) is None
                                else "%g" % float(inp.return_landing_lat))
        self.return_lon_var.set("" if getattr(inp, "return_landing_lon", None) is None
                                else "%g" % float(inp.return_landing_lon))
        self.return_radius_var.set("%g" % float(getattr(inp, "return_landing_radius_km",
                                                        50.0) or 50.0))
        self.return_start_var.set(str(getattr(inp, "return_search_start", "") or ""))
        self.return_end_var.set(str(getattr(inp, "return_search_end", "") or ""))
        self.return_dv_var.set("%g" % float(getattr(inp, "return_dv_mps", 120.0) or 120.0))
        self.return_ld_var.set("%g" % float(getattr(inp, "return_ld_ratio", 0.30) or 0.30))
        _bk = getattr(inp, "return_bank_deg", None)
        self.return_bank_var.set("" if _bk is None else "%g" % float(_bk))
        self.return_coarse_var.set("%g" % float(getattr(inp, "return_coarse_step_s",
                                                       600.0) or 300.0))
        self.return_scan_var.set("%g" % float(getattr(inp, "return_scan_step_s",
                                                     10.0) or 10.0))
        self.rendezvous_var.set(str(getattr(inp, "rendezvous_h", 6.5) or 6.5))
        self.dv_var.set(str(getattr(inp, "dv_budget_mps", 300.0) or 300.0))
        self.tol_var.set(str(getattr(inp, "window_tolerance_deg", 0.2) or 0.2))
        self.search_var.set(str(getattr(inp, "window_search_hours", 48.0) or 48.0))
        self._user_touched = True
        self._on_toggle()


def _parse_iso(text: str) -> Optional[datetime]:
    try:
        t = str(text).strip()
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        dt = datetime.fromisoformat(t)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None
