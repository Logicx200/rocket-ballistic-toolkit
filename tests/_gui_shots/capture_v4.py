# -*- coding: utf-8 -*-
"""GUI 冒烟 + 截图脚本（v4）。

用法（在项目根目录）：
    python tests/_gui_shots/capture_v4.py            # 只做冒烟（不截图）
    python tests/_gui_shots/capture_v4.py --shots     # 同时把 8 个步骤页 + 帮助中心截成 PNG

说明：Tk 需要真实桌面；截图用 PIL.ImageGrab 抓取窗口区域。若当前会话没有桌面
（远程/服务会话），脚本会跳过截图但仍会完成冒烟检查。
"""
from __future__ import annotations

import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.core import paths as P  # noqa: E402

P.setup_env()
P.ensure_syspath()

import tkinter as tk  # noqa: E402

from app.gui.main_window import STEPS, MainWindow, enable_dpi_awareness  # noqa: E402

OUT = os.path.join(HERE, "v4")
PAGES = [s["key"] for s in STEPS]


def pump(app, n: int = 30) -> None:
    for _ in range(n):
        try:
            app.update_idletasks()
            app.update()
        except tk.TclError:
            return


def capture(app, name: str) -> bool:
    try:
        from PIL import ImageGrab
        app.update_idletasks()
        app.lift()
        pump(app, 12)
        x, y = app.winfo_rootx(), app.winfo_rooty()
        w, h = app.winfo_width(), app.winfo_height()
        img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
        os.makedirs(OUT, exist_ok=True)
        path = os.path.join(OUT, name)
        img.save(path)
        print("  shot:", path, img.size)
        return True
    except Exception as exc:  # noqa: BLE001
        print("  [skip] 截图失败：%s" % exc)
        return False


def main() -> int:
    shots = "--shots" in sys.argv
    enable_dpi_awareness()
    errors = []
    app = MainWindow()
    app.geometry("1320x900+40+30")
    pump(app, 40)
    try:
        app.load_demo()
        pump(app, 20)
        for key in PAGES:
            app.show_page(key)
            pump(app, 12)
            title = next((s["title"] for s in STEPS if s["key"] == key), key)
            print("page ok:", key, title)
            if shots:
                capture(app, "%s_%s.png" % (PAGES.index(key) + 1, key))
        # 体检（同步版本，不启动线程）
        try:
            br, plan = app.generate_health_sync()
            print("health ok: %d 项 / 低置信 %d" % (len(br.resolutions), len(br.gaps)))
            app.show_page("health")
            pump(app, 12)
            if shots:
                capture(app, "07_health_result.png")
        except Exception as exc:  # noqa: BLE001
            errors.append("数据体检：%s" % exc)
            traceback.print_exc()
        # 帮助中心
        try:
            app.show_page("mission")
            pump(app, 6)
            if shots:
                import threading

                def _snap():
                    capture(app, "help_center.png")
                    for w in app.winfo_children():
                        if isinstance(w, tk.Toplevel):
                            try:
                                w.destroy()
                            except tk.TclError:
                                pass

                app.after(600, _snap)
            app.show_help()
            pump(app, 20)
            print("help ok")
        except Exception as exc:  # noqa: BLE001
            errors.append("帮助中心：%s" % exc)
            traceback.print_exc()
    finally:
        try:
            app.destroy()
        except tk.TclError:
            pass
    if errors:
        print("SMOKE FAILED:")
        for e in errors:
            print("  -", e)
        return 1
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
