# -*- coding: utf-8 -*-
"""编辑器对话框冒烟 + 截图：箭型参数编辑器 / 参考轨道编辑器。

用法（项目根目录）：
    python tests/_gui_shots/capture_dialogs.py            # 只冒烟
    python tests/_gui_shots/capture_dialogs.py --shots     # 截图到 tests/_gui_shots/v4/
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

from app.gui import theme  # noqa: E402
from app.gui.dialog_orbit import OrbitEditorDialog  # noqa: E402
from app.gui.dialog_rocket import RocketEditorDialog  # noqa: E402
from app.core import userdb as U  # noqa: E402

OUT = os.path.join(HERE, "v4")
SHOTS = "--shots" in sys.argv


def snap_top(root, name: str) -> None:
    if not SHOTS:
        return
    try:
        from PIL import ImageGrab
        for w in root.winfo_children():
            if isinstance(w, tk.Toplevel):
                try:
                    w.update_idletasks()
                    w.update()
                except tk.TclError:
                    continue
                x, y = w.winfo_rootx(), w.winfo_rooty()
                ww, hh = w.winfo_width(), w.winfo_height()
                if ww < 200 or hh < 150:
                    continue
                img = ImageGrab.grab(bbox=(x, y, x + ww, y + hh))
                os.makedirs(OUT, exist_ok=True)
                img.save(os.path.join(OUT, name))
                print("  shot:", os.path.join(OUT, name), img.size)
                return
    except Exception as exc:  # noqa: BLE001
        print("  [skip] 截图失败：%s" % exc)


def capture_and_close(root, name: str, delay: int = 1500) -> None:
    def _do():
        snap_top(root, name)
        for w in root.winfo_children():
            if isinstance(w, tk.Toplevel):
                try:
                    w.destroy()
                except tk.TclError:
                    pass

    root.after(delay, _do)


def main() -> int:
    root = tk.Tk()
    theme.apply_theme(root)
    # 不能 withdraw：Toplevel 是 root 的 transient 子窗口，root 隐藏时对话框不会映射，
    # 截图只能拿到 1×1。这里把 root 缩到角落即可。
    root.geometry("320x200+0+0")
    errors = []

    # ---- 箭型编辑器：空记录（新建）----
    try:
        capture_and_close(root, "dlg_rocket_new.png")
        d = RocketEditorDialog(root, None)
        print("rocket(new) ok: result=%s saved=%s mode=%s"
              % (bool(d.result), d.saved, d.mode))
    except Exception as exc:  # noqa: BLE001
        errors.append("箭型编辑器(新建)：%s" % exc)
        traceback.print_exc()

    # ---- 箭型编辑器：带记录（编辑内置克隆）----
    try:
        rec = U.blank_rocket("冒烟测试箭型", "medium_liquid")
        capture_and_close(root, "dlg_rocket_edit.png")
        d = RocketEditorDialog(root, rec)
        print("rocket(edit) ok: result=%s saved=%s mode=%s"
              % (bool(d.result), d.saved, d.mode))
    except Exception as exc:  # noqa: BLE001
        errors.append("箭型编辑器(编辑)：%s" % exc)
        traceback.print_exc()

    # ---- 轨道编辑器：新建 ----
    try:
        capture_and_close(root, "dlg_orbit_new.png")
        o = OrbitEditorDialog(root, None)
        print("orbit(new) ok: result=%s saved=%s" % (bool(o.result), o.saved))
    except Exception as exc:  # noqa: BLE001
        errors.append("轨道编辑器(新建)：%s" % exc)
        traceback.print_exc()

    # ---- 轨道编辑器：带记录 ----
    try:
        rec = {"name": "冒烟轨道", "alt_km": 500.0, "i_deg": 97.4, "e": 0.001,
               "ltan_h": 10.5, "type": "SSO", "confidence": "高"}
        capture_and_close(root, "dlg_orbit_edit.png")
        o = OrbitEditorDialog(root, rec)
        print("orbit(edit) ok: result=%s saved=%s" % (bool(o.result), o.saved))
    except Exception as exc:  # noqa: BLE001
        errors.append("轨道编辑器(编辑)：%s" % exc)
        traceback.print_exc()

    try:
        root.destroy()
    except tk.TclError:
        pass
    if errors:
        print("DIALOG SMOKE FAILED:")
        for e in errors:
            print("  -", e)
        return 1
    print("DIALOG SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
