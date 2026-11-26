# -*- coding: utf-8 -*-
"""编辑器各页截图（自动化翻页）：箭型编辑器 6 页 + 轨道编辑器。

用法：python tests/_gui_shots/capture_dialog_tabs.py
产物：tests/_gui_shots/v4/dlg_*.png
"""
from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import ttk

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.core import paths as P  # noqa: E402

P.setup_env()
P.ensure_syspath()

from app.gui import theme  # noqa: E402
from app.core import userdb as U  # noqa: E402
from app.gui.dialog_orbit import OrbitEditorDialog  # noqa: E402
from app.gui.dialog_rocket import RocketEditorDialog  # noqa: E402

OUT = os.path.join(HERE, "v4")


def find_notebooks(widget, acc):
    for ch in widget.winfo_children():
        if isinstance(ch, ttk.Notebook):
            acc.append(ch)
        find_notebooks(ch, acc)
    return acc


def grab(win, name):
    try:
        from PIL import ImageGrab
        win.update_idletasks()
        win.update()
        x, y = win.winfo_rootx(), win.winfo_rooty()
        w, h = win.winfo_width(), win.winfo_height()
        img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
        os.makedirs(OUT, exist_ok=True)
        img.save(os.path.join(OUT, name))
        print("  shot:", name, img.size)
    except Exception as exc:  # noqa: BLE001
        print("  [skip]", name, exc)


def run_rocket_tabs(root):
    rec = U.blank_rocket("截图用箭型", "medium_liquid")

    def worker():
        # 找到编辑器窗口
        win = next((w for w in root.winfo_children()
                    if isinstance(w, tk.Toplevel)), None)
        if win is None:
            print("  [warn] 未找到编辑器窗口")
            return
        nbs = find_notebooks(win, [])
        if not nbs:
            print("  [warn] 未找到 Notebook")
            return
        nb = nbs[0]
        names = ["dlg_rocket_tab1_basic.png", "dlg_rocket_tab2_stages.png",
                 "dlg_rocket_tab3_fairing.png", "dlg_rocket_tab4_guidance.png",
                 "dlg_rocket_tab5_sequence.png", "dlg_rocket_tab6_validate.png"]
        for i, nm in enumerate(names):
            try:
                if i < len(nb.tabs()):
                    nb.select(i)
                    win.update_idletasks()
                    win.update()
            except tk.TclError:
                pass
            grab(win, nm)
        win.destroy()

    root.after(900, worker)
    d = RocketEditorDialog(root, rec)
    print("rocket editor closed: result=%s saved=%s" % (bool(d.result), d.saved))


def run_orbit(root):
    rec = {"name": "截图用轨道", "alt_km": 505.0, "i_deg": 97.45, "e": 0.0,
           "ltan_h": 10.5, "type": "SSO", "confidence": "高"}

    def worker():
        win = next((w for w in root.winfo_children()
                    if isinstance(w, tk.Toplevel)), None)
        if win is None:
            return
        grab(win, "dlg_orbit_full.png")
        # 若轨道编辑器也有分页，逐页截
        nbs = find_notebooks(win, [])
        if nbs:
            nb = nbs[0]
            for i in range(len(nb.tabs())):
                try:
                    nb.select(i)
                    win.update_idletasks()
                    win.update()
                except tk.TclError:
                    pass
                grab(win, "dlg_orbit_tab%d.png" % (i + 1))
        win.destroy()

    root.after(900, worker)
    d = OrbitEditorDialog(root, rec)
    print("orbit editor closed: result=%s saved=%s" % (bool(d.result), d.saved))


def main() -> int:
    root = tk.Tk()
    theme.apply_theme(root)
    root.geometry("320x200+0+0")
    run_rocket_tabs(root)
    run_orbit(root)
    try:
        root.destroy()
    except tk.TclError:
        pass
    print("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
