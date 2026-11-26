# -*- coding: utf-8 -*-
"""README / 主页展示图：用「北京示例」任务截取 v4 GUI 的 8 个步骤页。

与 ``capture_v4.py`` 的区别：不载入内置示例（太原），而是读
``examples/北京示例_任务输入.json``，让界面里的地点全部是北京：
任务名带“北京”、发射场为北京北部坐标、观测点为北京天安门。

用法（在项目根目录）::

    python tests/_gui_shots/capture_beijing.py            # 只冒烟，不截图
    python tests/_gui_shots/capture_beijing.py --shots    # 截图到 docs/showcase/
"""
from __future__ import annotations

import json
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

from app.core.model import MissionInput  # noqa: E402
from app.gui.main_window import STEPS, MainWindow, enable_dpi_awareness  # noqa: E402

OUT = os.path.join(ROOT, "docs", "showcase")
MISSION = os.path.join(ROOT, "examples", "北京示例_任务输入.json")
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
        app.attributes("-topmost", True)
        pump(app, 12)
        x, y = app.winfo_rootx(), app.winfo_rooty()
        w, h = app.winfo_width(), app.winfo_height()
        img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
        os.makedirs(OUT, exist_ok=True)
        path = os.path.join(OUT, name)
        img.save(path)
        app.attributes("-topmost", False)
        print("  shot:", path, img.size)
        return True
    except Exception as exc:  # noqa: BLE001
        print("  [skip] 截图失败：%s" % exc)
        return False


def main() -> int:
    shots = "--shots" in sys.argv
    if not os.path.isfile(MISSION):
        print("缺少任务文件：%s（先运行 python tests/tools/make_beijing_demo.py）" % MISSION)
        return 2
    with open(MISSION, encoding="utf-8-sig") as fh:
        inp = MissionInput.from_dict(json.load(fh))

    enable_dpi_awareness()
    errors = []
    app = MainWindow()
    app.geometry("1320x900+40+30")
    pump(app, 40)
    try:
        app.set_mission_input(inp)
        app.auto_workdir()
        pump(app, 20)
        for key in PAGES:
            app.show_page(key)
            pump(app, 12)
            title = next((s["title"] for s in STEPS if s["key"] == key), key)
            print("page ok:", key, title)
            if shots:
                capture(app, "%02d_%s.png" % (PAGES.index(key) + 1, key))
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
