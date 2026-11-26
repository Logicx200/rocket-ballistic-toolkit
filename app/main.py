# -*- coding: utf-8 -*-
"""火箭弹道工具箱 v4.0 离线版 —— 程序入口。

两种用法
--------
1. **图形界面（默认）**::

       python -m app.main            # 或双击 启动器.bat / 火箭弹道工具箱.exe

2. **命令行（无界面，便于批处理/自检）**::

       python -m app.main --cli --mission 任务.json --workdir "作品\\我的任务"
       python -m app.main --cli --demo cz2d          # 内置示例（验证安装）
       python -m app.main --cli --demo cz2d --overwrite  # 复用已有工作区（覆盖上次成果）
       python -m app.main --env                      # 打印环境自检

``任务.json`` 是 :class:`app.core.model.MissionInput` 的 JSON（GUI 也可导出）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from typing import Any, Dict, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 控制台/管道统一 UTF-8（Windows 默认 GBK 会因部分符号/中文崩溃或乱码）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

from app.core import paths as P  # noqa: E402


# --------------------------------------------------------------------------- #
# 演示任务（内置，能证明整条链路可用）
# --------------------------------------------------------------------------- #
DEMO_CZ2D = {
    "name": "示例：2026-11-19 长征二号丁 太原 LC-9（T0 与观测点高程由程序推断）",
    "rocket_label": "长征二号丁",
    "site_label": "太原 LC-9",
    "serial": "遥106",
    "t0_text": "",
    "notams_text": (
        "A5897/26 NOTAMN\n"
        "Q)ZXXX/QRDCA/IV/BO/W/000/999/3322N11036E013\n"
        "A)ZXXX B)2611182243 C)2611182305\n"
        "E) DANGER AREA ESTABLISHED BOUNDED BY: N333400E1103100-N333200E1104400-"
        "N331000E1104100-N331100E1102700. VERTICAL LIMITS:SFC-UNL.\n\n"
        "A5898/26 NOTAMN\n"
        "Q)ZHWH/QRDCA/IV/BO/W/000/999/3015N11004E022\n"
        "A)ZHWH B)2611182244 C)2611182310\n"
        "E) DANGER AREA ESTABLISHED BOUNDED BY: N303500E1095700-N303300E1101900-"
        "N295400E1101300-N295700E1095000. VERTICAL LIMITS:SFC-UNL.\n"
    ),
    "observers": [{"id": "obspoint", "name": "山西晋中指定观测点",
                   "lat_deg": 37.761467, "lon_deg": 112.725219, "alt_m": None}],
    "payload_kg": 1000.0,
    "orbit_alt_km": 505.0,
    "orbit_i_deg": 97.45,
    "mc_n": 0,
    "modules": {"dem": False, "invert": True, "export": True, "window": True,
                "sky": True, "appear": True, "map": True, "mc": False,
                "kml": True, "qc": True, "report": True},
}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
#: 保存真实 stdout（runner 会把 sys.stdout 重定向到日志转发器，事件输出必须走这里）
_REAL_OUT = sys.stdout


def _print_event(ev) -> None:
    out = _REAL_OUT
    if ev.level == "progress":
        if ev.progress is not None:
            out.write("\r[progress %3.0f%%]" % (ev.progress * 100.0))
            out.flush()
        return
    if ev.level == "step":
        out.write("\n")
    prefix = {"info": "", "warn": "[警告] ", "error": "[错误] ", "log": "  ", "step": ""}.get(
        ev.level, "")
    out.write("%s%s\n" % (prefix, ev.message))
    out.flush()


def _resolve_mission(args) -> Dict[str, Any]:
    if args.demo:
        key = args.demo.lower()
        if key in ("cz2d", "cz-2d", "demo"):
            return dict(DEMO_CZ2D)
        raise SystemExit("未知示例：%s（可选 cz2d）" % args.demo)
    if args.mission:
        with open(args.mission, encoding="utf-8-sig") as f:
            d = json.load(f)
        if "mission" in d and "vehicle" in d:      # 直接给的是完整 config → 反推输入
            from app.core.config_build import mission_input_from_config
            return mission_input_from_config(d).to_dict()
        return d
    raise SystemExit("需要 --mission 或 --demo")


def run_cli(args) -> int:
    from app.core.config_build import build_config
    from app.core.model import MissionInput
    from app.core.runner import PipelineRunner, RunOptions
    from app.core import infer as I

    P.setup_env()
    P.ensure_syspath()

    mission_raw = _resolve_mission(args)
    if getattr(args, "station_tle", None):
        try:
            with open(args.station_tle, "r", encoding="utf-8-sig") as fh:
                mission_raw["station_tle_text"] = fh.read()
        except OSError as exc:
            raise SystemExit("无法读取空间站 TLE 文件：%s" % exc)
    if getattr(args, "station_oem", None):
        mission_raw["station_oem_path"] = os.path.abspath(args.station_oem)
    if getattr(args, "window_raan", None) is not None:
        mission_raw["window_raan_deg"] = float(args.window_raan)
    if getattr(args, "window_ltan", None) is not None:
        mission_raw["window_ltan_h"] = float(args.window_ltan)
    if args.modules:
        from app.core.model import DEFAULT_MODULES
        keep = {m.strip() for m in args.modules.split(",") if m.strip()}
        # 以 DEFAULT_MODULES 的键全集为准（任务 JSON 没写 modules 字段也能正确裁剪）
        keys = set(DEFAULT_MODULES) | set((mission_raw.get("modules") or {}))
        mission_raw["modules"] = {k: (k in keep) for k in keys}
        mission_raw["modules"]["export"] = True
    if args.no_invert:
        mission_raw.setdefault("modules", {})["invert"] = False
    if args.mc is not None:
        mission_raw["mc_n"] = int(args.mc)
    if args.jobs is not None:
        mission_raw["jobs"] = int(args.jobs)
    inp = MissionInput.from_dict(mission_raw)

    from app.core import workspace as W
    if getattr(inp, "return_only", False):
        _asked_name = "返回任务_%s" % I._iso_to_dt(inp.t0_text or "").strftime(
            "%Y-%m-%d_%H%M") if I._iso_to_dt(inp.t0_text or "") else "返回任务"
        asked_workdir = args.workdir or os.path.join(str(P.work_root()), _asked_name)
    else:
        asked_workdir = args.workdir or os.path.join(
            str(P.work_root()), I.auto_workdir_name(inp.rocket_label, inp.site_label, None))
    workdir, wd_action = W.prepare_workdir(asked_workdir, overwrite=bool(getattr(args, "overwrite", False)))
    workdir = str(workdir)
    os.makedirs(workdir, exist_ok=True)
    if wd_action == "renamed":
        print("[提示] 工作区已有上次计算的成果：%s" % asked_workdir)
        print("[提示] 本次自动新建工作区：%s（如需覆盖旧成果请加 --overwrite）" % workdir)
    elif wd_action == "overwrite" and bool(getattr(args, "overwrite", False)):
        print("[提示] 覆盖模式：复用已有工作区 %s" % workdir)

    # DEM 解析器（可选：没有 dem_manager 时降级为 None）
    dem_resolver = None
    try:
        from app.core.dem_manager import DemManager
        dm = DemManager(str(P.dem_dir()))
        dem_resolver = dm.observer_alt
    except Exception as exc:  # noqa: BLE001
        print("[提示] DEM 模块不可用（高程将按城市库/0 m 处理）：%s" % exc)

    print("== 火箭弹道工具箱 v%s 离线版 · 命令行 ==" % P.app_version())
    print("工作区：%s" % workdir)
    print("任务  ：%s" % (inp.name or "(自动命名)"))
    from app.core.config_build import BuildResult  # noqa: F401
    try:
        br = build_config(inp, workdir, logger=lambda s: print(s), dem_resolver=dem_resolver)
    except ValueError as exc:
        print("\n[错误] %s" % exc)
        print("提示：请检查「任务信息」里的坐标/时刻，或改为「未知，自动推断」。")
        return 2
    except KeyError as exc:
        print("\n[错误] 配置构建失败（缺少字段 %s）。" % exc)
        return 2

    _print_health(br)

    # v4：命令行也把原始输入落盘，保证"任务输入.json"这一通用成果不缺
    try:
        with open(os.path.join(workdir, "任务输入.json"), "w", encoding="utf-8") as fh:
            json.dump(inp.to_dict(), fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        print("[提示] 无法写入 任务输入.json：%s" % exc)

    from app.core import autoplan
    for line in autoplan.describe_plan(br.config):
        print("  " + line)

    opts = RunOptions(modules=(br.config.get("_v3") or {}).get("modules") or inp.modules,
                      mc_n=int(inp.mc_n or 0), jobs=int(inp.jobs or 0),
                      mode=inp.mode, allow_network=bool(args.allow_network),
                      quick_invert=bool(args.quick))
    runner = PipelineRunner(workdir, br.path, opts, on_event=_print_event)
    res = runner.run()
    print("\n" + "=" * 60)
    print(res.text())
    print("=" * 60)
    if res.artifacts:
        print("产物：")
        for a in res.artifacts:
            print("  " + a)
    return 0 if res.ok else 1


def _print_health(br) -> None:
    print("\n---- 数据体检 ----")
    for r in br.resolutions:
        print("  " + r.line())
    lowish = [r for r in br.resolutions
              if r.confidence == "低" or r.source in ("infer", "default")]
    if lowish:
        print("---- 低置信度/推断项 %d 条（结论需注明不确定性）----" % len(lowish))
    if br.warnings:
        print("---- 提示 ----")
        for w in br.warnings:
            print("  ! " + w)
    print("--------------\n")


def print_env() -> int:
    from app.core.runner import check_environment
    P.setup_env()
    info = check_environment()
    print("== 环境自检 ==")
    for k, v in info.items():
        print("  %-12s %s" % (k, v))
    return 0


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
def launch_gui(args) -> int:
    P.setup_env()
    P.ensure_syspath()
    try:
        from app.gui.main_window import launch
    except Exception as exc:  # noqa: BLE001
        print("无法启动图形界面（%s）。" % exc)
        print("可用命令行模式： python -m app.main --cli --demo cz2d")
        traceback.print_exc()
        return 2
    launch()
    return 0


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="火箭弹道工具箱",
        description="离线火箭弹道工具箱 v4.0：常规发射/载人交会/返回舱返回三类任务的"
                    "3-DOF 弹道、落区、窗口、天空可见性与成果报告")
    ap.add_argument("--cli", action="store_true", help="命令行模式（无界面）")
    ap.add_argument("--mission", help="任务输入 JSON（MissionInput）或完整 config.json")
    ap.add_argument("--workdir", help="任务工作区目录（默认自动命名到 作品/ 下）")
    ap.add_argument("--demo", help="内置示例任务（cz2d）")
    ap.add_argument("--modules", help="只跑指定模块（逗号分隔，如 export,sky,kml,report）")
    ap.add_argument("--no-invert", action="store_true", help="跳过参数反演")
    ap.add_argument("--mc", type=int, default=None, help="蒙特卡洛样本数")
    ap.add_argument("--jobs", type=int, default=None, help="并行进程数（0=自动）")
    ap.add_argument("--quick", action="store_true", help="快速模式（反演小预算）")
    ap.add_argument("--allow-network", action="store_true", help="允许联网补全 DEM 瓦片/空间站轨道")
    ap.add_argument("--station-tle", help="空间站 TLE 文件（两行文本），用于 CZ-2F 交会计算")
    ap.add_argument("--station-oem", help="空间站 OEM 精密星历文件，用于 CZ-2F 交会计算")
    ap.add_argument("--window-raan", type=float, help="目标轨道 RAAN（度），用于轨道平面窗口")
    ap.add_argument("--window-ltan", type=float, help="太阳同步轨道 LTAN（小时，0-24）")
    ap.add_argument("--import-dem", help="导入离线高程文件/目录后退出（支持 GeoTIFF/HGT/ZIP）")
    ap.add_argument("--overwrite", action="store_true",
                    help="工作区已有成果时直接覆盖（默认自动新建 <目录>_2 副本，避免误删旧成果）")
    ap.add_argument("--env", action="store_true", help="打印环境自检后退出")
    ap.add_argument("--config", help="（兼容）直接用 config.json 跑流水线")
    args = ap.parse_args(argv)

    try:
        import multiprocessing
        multiprocessing.freeze_support()
    except Exception:  # noqa: BLE001
        pass

    if args.env:
        return print_env()
    if getattr(args, "import_dem", None):
        P.setup_env()
        try:
            from app.core.dem_manager import DemManager
            dm = DemManager(str(P.dem_dir()))
            info = dm.import_dem(args.import_dem)
            print("== 离线高程导入 ==")
            for s in info.get("sources") or []:
                print("  + %s（%s）" % (s.get("name") or s.get("id"), s.get("file") or ""))
            for f in info.get("failed") or []:
                print("  ! %s：%s" % (f.get("path"), f.get("error")))
            print("DEM 目录：%s；成功 %s 个文件" % (P.dem_dir(), info.get("ok", 0)))
            return 0 if int(info.get("ok") or 0) > 0 else 1
        except Exception as exc:  # noqa: BLE001
            print("导入失败：%s" % exc)
            return 2
    if args.cli or args.mission or args.demo or args.config:
        if args.config and not args.mission:
            args.mission = args.config
        return run_cli(args)
    return launch_gui(args)


if __name__ == "__main__":
    sys.exit(main())
