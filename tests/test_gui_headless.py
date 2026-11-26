# -*- coding: utf-8 -*-
"""GUI 无头自测（v4）：创建真实 Tk 窗口 → 载入内置示例 → 同步生成数据体检 → 断言。

覆盖 v4 的新结构：左侧 8 步向导（``app._pages`` / ``app.nav``）、常驻任务概要条、
步骤台账与成果文件树、帮助中心、自定义箭型/轨道编辑器的**入口可用性**。

运行方式（在项目根目录）::

    python tests\test_gui_headless.py

要求：退出码 0，且最后一行打印 OK。不点击「开始计算」，不跑长流水线。
"""
from __future__ import annotations

import os
import sys

try:  # Windows 控制台默认 GBK，统一成 UTF-8 便于打印中文
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
# 使用测试 fixtures 数据库（正式 app/data 由他人交付，可能暂无数据）
os.environ.setdefault("RK_APP_DATA", os.path.join(ROOT, "tests", "fixtures", "db"))
os.environ.setdefault("MPLBACKEND", "Agg")
os.environ.setdefault("PYTHONUTF8", "1")

from app.core import paths as P  # noqa: E402

P.setup_env()
P.ensure_syspath()

from app.core.model import MissionInput  # noqa: E402
from app.main import DEMO_CZ2D  # noqa: E402

PAGE_KEYS = ["mission", "vehicle", "notam", "observer", "station", "options",
             "health", "run"]


def main() -> int:
    # ---- 启动前：重模块不应被提前导入（启动速度约定） ------------------ #
    assert "numpy" not in sys.modules, "启动阶段不应导入 numpy"
    assert "matplotlib" not in sys.modules, "启动阶段不应导入 matplotlib"

    from app.gui.main_window import STEPS, TITLE, create_window

    app = create_window()
    app.withdraw()
    app.update_idletasks()

    assert app.title() == TITLE, "窗口标题不正确：%s" % app.title()
    assert len(STEPS) == 8, "应有 8 个步骤，实际 %d 个" % len(STEPS)
    assert list(app._pages) == PAGE_KEYS, "步骤顺序不符：%s" % list(app._pages)
    assert app.nav is not None, "左侧步骤导航未创建"
    print("窗口创建成功：%s（%d 个步骤）" % (app.title(), len(STEPS)))

    # ---- 载入示例任务 -------------------------------------------------- #
    inp = MissionInput.from_dict(dict(DEMO_CZ2D))
    app.set_mission_input(inp)
    app._pages["mission"].on_show()
    app.update_idletasks()

    cur = app.build_mission_input()
    assert cur.rocket_label == "长征二号丁", "火箭型号未载入：%r" % cur.rocket_label
    assert cur.site_label == "太原 LC-9", "发射工位未载入：%r" % cur.site_label
    assert (cur.t0_text or "").strip() == "", "示例应留空 T0（自动推断）"
    assert len(cur.observers) == 1, "示例应有 1 个观测点，实际 %d" % len(cur.observers)
    assert len(cur.notams) >= 2, "航警原文应自动解析出至少 2 条，实际 %d 条" % len(cur.notams)
    assert cur.modules.get("export") is True, "export 模块必须开启"
    assert cur.modules.get("summary") is True, "summary（通用成果）模块必须默认开启"
    print("示例载入成功：%s / %s / 航警 %d 条 / 观测点 %d 个 / 任务类型 %s" % (
        cur.rocket_label, cur.site_label, len(cur.notams), len(cur.observers),
        cur.mission_type))

    # ---- 参考轨道 / 时序的页面状态 ------------------------------------ #
    veh = app._pages["vehicle"]
    veh.on_show()
    app.update_idletasks()
    assert veh.ref_mode_var.get() in ("auto", "template", "user", "custom", "tle")
    assert veh._seq_rows, "发射时序行未按箭型生成"
    assert veh.stage_tree.winfo_exists(), "箭型参数速览表未创建"

    # ---- 同步生成数据体检（等价于点「生成体检」） ---------------------- #
    br, plan = app.generate_health_sync(cur)
    app.update_idletasks()
    assert br is not None and br.config, "build_config 未返回配置"
    assert len(br.resolutions) > 0, "体检表应有数据行"
    rows = len(app._pages["health"].tree.get_children())
    assert rows > 0, "体检表格行数为 0"
    assert rows == len(br.resolutions), "表格行数(%d)应等于体检项数(%d)" % (
        rows, len(br.resolutions))
    assert br.config["mission"]["t0_utc"], "T0 未被推断"
    assert br.config["mission"]["launch_site"]["name"], "发射场未解析"
    assert br.config.get("vehicle", {}).get("stages"), "箭型参数未组装"
    assert br.config.get("observers"), "观测点未补全"
    assert br.config["_v3"].get("mission_type") in ("standard", "crewed"), "任务类型未写入"
    print("生成体检成功：%d 行（低置信度 %d 条，提示 %d 条）" % (
        rows, len(br.gaps), len(br.warnings)))
    print("摘要：任务=%s / T0=%s" % (
        br.config["mission"].get("name"), br.config["mission"].get("t0_utc")))
    for line in (plan or [])[:3]:
        print("计划：%s" % line)

    # ---- 导航与页面切换 ------------------------------------------------ #
    for key in PAGE_KEYS:
        app.show_page(key)
        app.update()
    assert app._current == "run"
    app.show_page("mission")
    app.update()

    # ---- 运行/日志/成果 ------------------------------------------------ #
    run = app._pages["run"]
    run.set_progress(0.42)
    run.append_log("info", "自测日志：info")
    run.append_log("warn", "自测日志：warn")
    run.append_log("error", "自测日志：error")
    app.update()
    assert run.progress_var.get() == 42.0, "进度条数值未生效"

    # ---- 步骤台账渲染（构造一个假的 RunResult） ----------------------- #
    class _Res:
        ok = False
        cancelled = False
        workdir = ""
        steps = [
            {"name": "环境准备", "ok": True, "seconds": 0.1, "state": "完成"},
            {"name": "天空轨迹", "ok": True, "seconds": 0.0, "state": "跳过",
             "reason": "无观测点"},
            {"name": "返回舱返回", "ok": True, "seconds": 0.0, "state": "不适用",
             "reason": "未勾选"},
            {"name": "交付自检", "ok": False, "seconds": 0.2, "state": "失败",
             "error": "测试失败"},
        ]
        summary = {"任务类型": "常规发射弹道"}
        artifacts = []

        def failed_steps(self):
            return [s for s in self.steps if not s.get("ok")]

    run.show_result(_Res(), "")
    app.update()
    assert len(run.steps_tree.get_children()) == 4, "步骤台账行数不符"
    print("步骤台账渲染成功：%d 行" % len(run.steps_tree.get_children()))

    # ---- 编辑器入口（只验证依赖可导入，不弹窗） ----------------------- #
    import importlib
    for mod in ("app.gui.dialog_rocket", "app.gui.dialog_orbit", "app.gui.dialog_help"):
        try:
            importlib.import_module(mod)
        except Exception as exc:  # noqa: BLE001
            raise AssertionError("编辑器模块导入失败 %s：%s" % (mod, exc))
    print("编辑器模块导入成功：dialog_rocket / dialog_orbit / dialog_help")

    app.destroy()
    print("OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print("自测失败：%s" % exc)
        raise
