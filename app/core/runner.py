# -*- coding: utf-8 -*-
"""流水线执行器：把 engine/scripts/*.py 串起来，全程可观测、可取消、可中断恢复。

关键设计
--------
* 脚本以 ``runpy.run_path`` 在**同进程**执行（冻结打包后没有独立 python 可调），
  每步前设 ``RK_ROOT=工作区``，脚本自己解析路径；stdout/stderr 实时转发给 GUI。
* 步骤键与 ``MissionInput.modules`` 一致：dem / invert / export / window / sky /
  appear / map / mc / kml / qc / report，另外内部有 t0refine / predict 两个自动步骤。
* 反演完成后把 ``sim/反演参数.json`` 的最优点**回填到 config.json**（v2 里这一步靠人做），
  保证后续 export/天空/KMZ 用的是同一组参数。
* 无航警时（预测落区模式）：先跑一次 export，用预测落点合成提示性落区，再继续。
* 冻结态（PyInstaller）多进程不可靠 → ``RK_FROZEN=1`` 时强制串行。

用法::

    from app.core.runner import PipelineRunner, RunOptions
    r = PipelineRunner(workdir, config_path, RunOptions(...), on_event=print)
    res = r.run()
"""
from __future__ import annotations

import io
import json
import os
import runpy
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from . import paths as P


# --------------------------------------------------------------------------- #
# 事件 / 结果
# --------------------------------------------------------------------------- #
@dataclass
class RunEvent:
    level: str = "info"          # info / warn / error / log / step / progress
    step: str = ""
    message: str = ""
    progress: Optional[float] = None    # 0..1
    elapsed: Optional[float] = None


@dataclass
class RunOptions:
    modules: Dict[str, bool] = field(default_factory=dict)
    mc_n: int = 0
    jobs: int = 0
    mode: str = "accurate"
    allow_network: bool = False
    quick_invert: bool = False
    dem_skip_download: bool = True

    def enabled(self, key: str) -> bool:
        return bool(self.modules.get(key, False))


@dataclass
class RunResult:
    ok: bool = False
    cancelled: bool = False
    steps: List[Dict[str, Any]] = field(default_factory=list)
    artifacts: List[str] = field(default_factory=list)
    log_path: str = ""
    workdir: str = ""
    error: str = ""
    summary: Dict[str, Any] = field(default_factory=dict)

    def failed_steps(self) -> List[Dict[str, Any]]:
        return [s for s in self.steps if not s.get("ok")]

    def text(self) -> str:
        lines = []
        for s in self.steps:
            state = str(s.get("state") or "")
            if s.get("ok"):
                mark = {"跳过": "跳过", "不适用": "N/A ",
                        "未启用（未勾选模块）": "未启"}.get(state, "OK  ")
            else:
                mark = "取消" if s.get("cancelled") else "失败"
            note = s.get("error") or s.get("reason") or ""
            lines.append("[%s] %-22s %6.1f s %s" % (mark, s.get("name", ""),
                                                   s.get("seconds", 0.0), note))
        if self.summary:
            lines.append("")
            for k, v in self.summary.items():
                lines.append("%s：%s" % (k, v))
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# stdout 重定向（转发给 GUI + 写日志）
# --------------------------------------------------------------------------- #
class _Tee(io.TextIOBase):
    """把脚本的 print 转发到 GUI/日志；带重入保护（sink 里若再次写 stdout 不会递归）。"""

    def __init__(self, sink: Callable[[str, str], None], level: str = "log"):
        self.sink = sink
        self.level = level
        self.buf = ""
        self._busy = False

    def write(self, s: str) -> int:  # noqa: D401
        if not isinstance(s, str):
            s = str(s)
        if self._busy:
            return len(s)
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            line = line.rstrip("\r")
            if line.strip():
                self._busy = True
                try:
                    self.sink(self.level, line)
                finally:
                    self._busy = False
        return len(s)

    def flush(self) -> None:
        if self.buf.strip():
            self.sink(self.level, self.buf.strip())
            self.buf = ""

    def isatty(self) -> bool:
        return False


# --------------------------------------------------------------------------- #
# 执行器
# --------------------------------------------------------------------------- #
class PipelineRunner:
    """按模块开关执行流水线。线程安全：只在后台线程里用（GUI 约定）。"""

    #: 步骤显示名
    STEP_NAMES = {
        "env": "环境准备",
        "dem": "DEM 地形准备",
        "t0refine": "T0 精化（按落区窗口）",
        "t0win": "T0 反推（交会发射窗口）",
        "predict": "预测落区生成",
        "stagehint": "残骸类型校正（按预测落点）",
        "invert": "制导/箭型参数反演",
        "export": "仿真与数据契约",
        "station": "空间站轨道准备",
        "window": "发射窗口（航警+轨道面/交会）",
        "docking": "入轨+交会对接方案",
        "return": "返回舱返回窗口与弹道",
        "sky": "天空轨迹（详细+主图）",
        "appear": "出现方位地形判读图",
        "map": "全国最大高度角地图",
        "mc": "蒙特卡洛不确定度",
        "kml": "三维 KMZ/KML",
        "summary": "通用成果汇总（摘要/关键节点/清单）",
        "report": "中文技术报告",
        "qc": "交付自检",
    }

    #: 步骤状态中文
    ST_OK, ST_SKIP, ST_NA, ST_FAIL, ST_CANCEL, ST_OFF = (
        "完成", "跳过", "不适用", "失败", "取消", "未启用（未勾选模块）")

    def __init__(self, workdir: str, config_path: str, options: RunOptions,
                 on_event: Optional[Callable[[RunEvent], None]] = None,
                 should_stop: Optional[Callable[[], bool]] = None):
        self.workdir = os.path.abspath(workdir)
        self.config_path = os.path.abspath(config_path)
        self.opt = options
        self._on_event = on_event
        self._should_stop = should_stop
        self.log_path = os.path.join(self.workdir, "运行日志.txt")
        self._log_fh = None
        self._t0 = time.time()
        self._step = ""
        self._progress = 0.0
        self._weights: List[Any] = []
        self._done_w = 0.0
        #: 当前步骤"跳过原因"（步骤函数里调用 :meth:`_skip` 设置，写入步骤台账）
        self._note = ""
        self.result = RunResult(workdir=self.workdir, log_path=self.log_path)

    # ------------------------------------------------------------------ #
    # 基础设施
    # ------------------------------------------------------------------ #
    def _emit(self, level: str, message: str, progress: Optional[float] = None) -> None:
        ev = RunEvent(level=level, step=self._step, message=message,
                      progress=self._progress if progress is None else progress,
                      elapsed=time.time() - self._t0)
        if self._on_event:
            try:
                self._on_event(ev)
            except Exception:  # noqa: BLE001
                pass

    def _sink(self, level: str, line: str) -> None:
        self._write_log(line)
        self._emit(level, line)

    def _write_log(self, line: str) -> None:
        if self._log_fh:
            try:
                self._log_fh.write("%s %s\n" % (datetime.now().strftime("%H:%M:%S"), line))
                self._log_fh.flush()
            except Exception:  # noqa: BLE001
                pass

    def _check_cancel(self) -> bool:
        try:
            return bool(self._should_stop and self._should_stop())
        except Exception:  # noqa: BLE001
            return False

    def _set_step(self, key: str) -> None:
        self._step = key
        self._emit("step", "=== %s ===" % self.STEP_NAMES.get(key, key), self._progress)

    def _advance(self, key: str, frac: float = 1.0) -> None:
        try:
            i = [w[0] for w in self._weights].index(key)
        except ValueError:
            return
        self._done_w = sum(w[1] for w in self._weights[:i]) + self._weights[i][1] * max(0.0, min(1.0, frac))
        total = sum(w[1] for w in self._weights) or 1.0
        self._progress = min(0.999, self._done_w / total)
        self._emit("progress", "", self._progress)

    def _env(self) -> Dict[str, str]:
        env = dict(os.environ)
        P.setup_env()
        env["RK_ROOT"] = self.workdir
        env["RK_ENGINE"] = str(P.ENGINE_DIR)
        env["RK_DEM"] = str(P.dem_dir())
        env["MPLBACKEND"] = "Agg"
        env["MPLCONFIGDIR"] = str(P.mpl_config_dir())
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        if P.FROZEN:
            env["RK_FROZEN"] = "1"
        return env

    def _apply_env(self) -> None:
        for k, v in self._env().items():
            os.environ[k] = v
        P.ensure_syspath()

    def _run_script(self, script: str, argv: Optional[List[str]] = None) -> int:
        """在同进程以 __main__ 方式运行 engine/scripts/<script>.py，返回退出码。"""
        path = P.engine_script(script)
        if not path.exists():
            self._sink("error", "找不到脚本：%s" % path)
            return 127
        old_argv = sys.argv[:]
        sys.argv = [str(path)] + list(argv or [])
        old_cwd = os.getcwd()
        try:
            os.chdir(self.workdir)
        except Exception:  # noqa: BLE001
            pass
        code = 0
        try:
            runpy.run_path(str(path), run_name="__main__")
        except SystemExit as exc:  # 脚本正常用 sys.exit(code) 结束
            try:
                code = int(exc.code or 0)
            except (TypeError, ValueError):
                code = 1
        except Exception:  # noqa: BLE001
            code = 1
            for ln in traceback.format_exc().splitlines():
                self._sink("error", "  " + ln)
        finally:
            sys.argv = old_argv
            try:
                os.chdir(old_cwd)
            except Exception:  # noqa: BLE001
                pass
        return code

    def _run_step(self, key: str, func: Callable[[], bool], essential: bool = False) -> bool:
        """执行一步：耗时、异常、取消都记录。返回是否成功（取消算 False）。

        v4：步骤函数可用 :meth:`_skip` 声明"本步按条件跳过"，台账里记为「跳过 + 原因」，
        而不是含糊的「完成」。
        """
        if self._check_cancel():
            self.result.cancelled = True
            return False
        self._set_step(key)
        self._note = ""
        t0 = time.time()
        ok, err, cancelled = False, "", False
        try:
            ok = bool(func())
        except Exception as exc:  # noqa: BLE001
            err = "%s: %s" % (type(exc).__name__, exc)
            for ln in traceback.format_exc().splitlines():
                self._sink("error", "  " + ln)
        secs = time.time() - t0
        cancelled = self._check_cancel() and not ok
        state = self.ST_OK
        if cancelled:
            state = self.ST_CANCEL
        elif not ok:
            state = self.ST_FAIL
        elif self._note:
            state = self.ST_SKIP
        self.result.steps.append({"key": key, "name": self.STEP_NAMES.get(key, key),
                                  "ok": ok, "seconds": round(secs, 2), "error": err,
                                  "cancelled": cancelled, "essential": essential,
                                  "state": state, "reason": self._note})
        if ok and self._note:
            self._sink("info", "[跳过] %s：%s" % (self.STEP_NAMES.get(key, key), self._note))
            self._advance(key, 1.0)
        elif ok:
            self._advance(key, 1.0)
            self._sink("info", "[完成] %s  %.1f s" % (self.STEP_NAMES.get(key, key), secs))
        else:
            self._sink("warn" if not essential else "error",
                       "[%s] %s  %.1f s %s" % ("取消" if cancelled else "失败",
                                              self.STEP_NAMES.get(key, key), secs, err))
        return ok

    def _skip(self, reason: str) -> None:
        """在步骤函数里声明"本步按条件跳过"，原因会写进步骤台账与成果清单。"""
        self._note = str(reason or "按条件跳过")

    def _record_state(self, key: str, state: str, reason: str) -> None:
        """记录一个没有真正执行的步骤（不适用 / 模块未勾选）。"""
        self.result.steps.append({"key": key, "name": self.STEP_NAMES.get(key, key),
                                  "ok": True, "seconds": 0.0, "error": "",
                                  "cancelled": False, "essential": False,
                                  "state": state, "reason": reason})
        self._advance(key, 1.0)
        self._sink("log", "[%s] %s：%s" % (state, self.STEP_NAMES.get(key, key), reason))

    # ------------------------------------------------------------------ #
    # 各步骤实现
    # ------------------------------------------------------------------ #
    def _load_config(self) -> Dict[str, Any]:
        with open(self.config_path, encoding="utf-8-sig") as f:
            return json.load(f)

    def _save_config(self, cfg: Dict[str, Any], name: str = "config.json") -> str:
        path = os.path.join(self.workdir, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        if name == "config.json":
            self.config_path = path
        return path

    # ---- env / dem ---------------------------------------------------- #
    def _step_env(self) -> bool:
        os.makedirs(self.workdir, exist_ok=True)
        self._apply_env()
        for sub in ("sim", "sky", "map", "viz", "report", "uncertainty", "数据体检"):
            os.makedirs(os.path.join(self.workdir, sub), exist_ok=True)
        self._sink("info", "工作区：%s" % self.workdir)
        self._sink("info", "引擎  ：%s" % P.ENGINE_DIR)
        self._sink("info", "瓦片  ：%s" % P.dem_dir())
        return True

    def _step_dem(self) -> bool:
        from .dem_manager import DemManager
        cfg = self._load_config()
        dm = DemManager(str(P.dem_dir()), logger=lambda s: self._sink("log", s))
        allow_dl = bool(self.opt.allow_network and not self.opt.dem_skip_download)
        info = dm.prepare(cfg, allow_download=allow_dl)
        self._sink("info", "DEM 模式：%s（观测点可用：%s）；本地瓦片 %s 片；导入 %s；下载 %s；缺片 %s" % (
            info.get("mode"), info.get("observer_ready", "未知"), info.get("tiles", 0),
            info.get("imported", 0), info.get("downloaded", 0),
            len(info.get("failed") or [])))
        _custom = info.get("custom") or {}
        if isinstance(_custom, dict) and _custom.get("sources"):
            _cov = (_custom.get("coverage") or {}).get("total") or {}
            self._sink("info", "自定义高程源：%s（关键点覆盖 %s/%s）" % (
                "、".join(str(s.get("name") or s.get("id") or "?")
                         for s in (_custom.get("sources") or [])[:3]),
                _cov.get("covered", "?"), _cov.get("total", "?")))
        _missing = len(info.get("missing") or []) or len(info.get("failed") or [])
        if info.get("observer_ready") and _missing:
            self._sink("info", "观测点/发射场周边 1″ SRTM 齐备；另有 %d 片瓦片不在本地，"
                               "缺失区域回退粗 DEM（不影响观测点遮挡计算）" % _missing)
        elif info.get("mode") not in ("srtm", "custom", "1"):
            self._sink("warn", "未取得 SRTM/自定义精细高程：天空轨迹将使用内嵌粗 DEM"
                               "（约 2 km 分辨率），首次出现时刻误差可达数十秒，"
                               "不能用于正式结论")
        cfg.setdefault("_v3", {})["dem_task_mode"] = info.get("mode")
        # 观测点/发射场的精细地形是否就绪，决定天空轨迹的 dem_mode（报告用）
        cfg["_v3"]["dem_mode"] = ("custom" if info.get("mode") == "custom" else
                                  ("srtm" if info.get("observer_ready")
                                   else info.get("mode")))
        cfg["_v3"]["dem_observer_ready"] = info.get("observer_ready")
        self._save_config(cfg)
        return True

    # ---- T0 精化 ------------------------------------------------------ #
    def _t0_feasible(self, cfg):
        """按"航警区 ↔ 残骸对象"的 hint 配对，算可行 T0 区间。

        返回 ``(lo, hi, detail)`` 或 ``None``。修正点（v3.1）：v2 里 T0 校核只盯一级落点，
        遇到"只有助推器落区"的任务（如 CZ-6A）会把一级到达时刻硬套到助推区窗口上，
        导致 T0 被平移 3–4 min、后续反演全部跑偏。这里改为**只配对 hint 一致的区与对象**。
        """
        from . import infer as I
        zones = cfg.get("notams") or []
        if not zones:
            return None
        impacts = self._read_csv("sim/落点与射程.csv")
        if not impacts:
            return None
        t0 = I._iso_to_dt((cfg.get("mission") or {}).get("t0_utc", ""))
        if t0 is None:
            return None
        lo = hi = None
        detail = []
        try:
            from common import notam as _N
        except Exception:  # noqa: BLE001
            _N = None
        for z in zones:
            hint = z.get("stage_hint") or ""
            obj_hint = z.get("object_hint") or ""
            if not hint and not obj_hint:
                continue
            rows = []
            if _N is not None:
                stages = [s.get("name", "") for s in
                          (cfg.get("vehicle", {}).get("stages") or [])]
                targets = (_N.object_hint_targets(obj_hint, stages) if obj_hint
                           else _N.hint_object_targets(hint, stages))
                if targets == ():
                    detail.append("%s: 残骸类型「%s」无法识别" % (z.get("id", "?"), hint))
                    continue
                rows = [r for r in impacts
                        if _N.object_name_matches(r.get("object", ""), targets)]
            if not rows:
                detail.append("%s: 找不到匹配的残骸对象（hint=%s, object=%s）"
                              % (z.get("id", "?"), hint or "—", obj_hint or "—"))
                continue
            arr = max(float(x.get("impact_t_met_s") or 0) for x in rows)
            b = I._iso_to_dt(z.get("b_utc", ""))
            c = I._iso_to_dt(z.get("c_utc", ""))
            if b is None or c is None:
                continue
            lo_z = b - timedelta(seconds=arr)
            hi_z = c - timedelta(seconds=arr)
            lo = lo_z if lo is None else max(lo, lo_z)
            hi = hi_z if hi is None else min(hi, hi_z)
            detail.append("%s ↔ %s：到达 T+%.0f s，允许 T0 ∈ [%s, %s]" % (
                z.get("id", "?"), rows[0].get("object", "?"), arr,
                lo_z.strftime("%H:%M:%S"), hi_z.strftime("%H:%M:%S")))
        if lo is None or hi is None or lo >= hi:
            return None
        return lo, hi, detail, t0

    def _step_t0refine(self) -> bool:
        """先用当前参数跑一次仿真，按一级/助推落区窗口中心反推 T0，再重建 config。"""
        from . import infer as I
        from . import notam_parse as NP
        cfg = self._load_config()
        if not (cfg.get("_v3") or {}).get("needs_refine_t0"):
            self._skip("T0 为用户给定或航警时间不可靠，无需精化")
            return True
        self._sink("info", "首次试算：用当前制导参数跑一遍，按落区窗口反推 T0…")
        code = self._run_script("export", ["--config", self.config_path])
        if code != 0:
            self._sink("warn", "试算失败（退出码 %s），跳过 T0 精化" % code)
            return False
        imp = self._read_csv("sim/落点与射程.csv")
        zones = cfg.get("notams") or []
        if not imp or not zones:
            return True
        # 挑 stage_0 或与落区匹配的对象
        target = None
        for row in imp:
            obj = row.get("object", "")
            if obj in ("core_stage", "stage_0") or "一" in obj:
                target = row
                break
        target = target or imp[0]
        # 找该对象对应的航警区（按 stage_hint 匹配）
        hint_map = {"core_stage": "一级", "stage_0": "一级", "fairing_L": "整流罩",
                    "fairing_R": "整流罩", "boosters": "助推器"}
        hint = hint_map.get(target.get("object", ""), "")
        zone = None
        for z in zones:
            if hint and (hint in (z.get("stage_hint") or "") or
                         (z.get("stage_hint") or "") in hint):
                zone = z
                break
        zone = zone or zones[0]
        # v3.1：只配对 hint 一致的区与对象（避免"一级到达时刻套助推区窗口"）
        feas = self._t0_feasible(cfg)
        if feas is None:
            self._sink("info", "T0 精化：没有可配对的航警区/残骸对象，跳过")
            cfg["_v3"]["needs_refine_t0"] = False
            self._save_config(cfg)
            return True
        lo, hi, detail, t0 = feas
        for d in detail:
            self._sink("log", "  " + d)
        if lo <= t0 <= hi:
            self._sink("info", "T0 精化：初值 %s 已在可行区间 [%s, %s] 内，保持不变" % (
                t0.strftime("%H:%M:%SZ"), lo.strftime("%H:%M:%S"), hi.strftime("%H:%M:%S")))
            cfg["_v3"]["needs_refine_t0"] = False
            self._save_config(cfg)
            return True
        margin = timedelta(seconds=60)
        target_t0 = lo + margin if t0 < lo else hi - margin
        if target_t0 < lo or target_t0 > hi:
            target_t0 = lo + (hi - lo) / 2
        cfg["mission"]["t0_utc"] = target_t0.strftime("%Y-%m-%dT%H:%M:%SZ")
        cfg["mission"]["t0_note"] = (cfg["mission"].get("t0_note", "") +
                                     "；精化：按航警可行区间 %s ~ %s 平移 %+.1f min" % (
                                         lo.strftime("%H:%M:%S"), hi.strftime("%H:%M:%S"),
                                         (target_t0 - t0).total_seconds() / 60.0))
        cfg["_v3"]["needs_refine_t0"] = False
        self._save_config(cfg)
        self._sink("info", "T0 精化：%s → %s（%+.1f min）" % (
            t0.strftime("%H:%M:%SZ"), target_t0.strftime("%H:%M:%SZ"),
            (target_t0 - t0).total_seconds() / 60.0))
        return True

    # ---- 预测落区 ------------------------------------------------------ #
    def _step_predict(self) -> bool:
        cfg = self._load_config()
        if (cfg.get("_v3") or {}).get("notams_mode") != "predict":
            self._skip("未开启「预测落区」模式（用户提供了航警或关闭了预测）")
            return True
        if cfg.get("notams"):
            self._skip("已有可用航警区，无需预测落区")
            return True
        imp = self._read_csv("sim/落点与射程.csv")
        if not imp:
            # 全新工作区：先试算一次，拿到预测落点再合成落区
            self._sink("info", "预测落区：先按标称参数试算一次…")
            code = self._run_script("export", ["--config", self.config_path])
            if code != 0:
                self._sink("warn", "预测落区：试算失败（退出码 %s）" % code)
                return False
            imp = self._read_csv("sim/落点与射程.csv")
        if not imp:
            self._sink("warn", "预测落区：仍没有 sim/落点与射程.csv，跳过")
            return False
        hint_map = {"core_stage": "一级", "stage_0": "一级", "stage_1": "二级",
                    "fairing_L": "整流罩", "fairing_R": "整流罩", "boosters": "助推器"}
        zones = []
        for row in imp:
            try:
                lat = float(row["lat_deg"])
                lon = float(row["lon_deg"])
                t_met = float(row["impact_t_met_s"])
            except (KeyError, ValueError):
                continue
            if abs(lat) < 1e-6 and abs(lon) < 1e-6:
                continue
            half = 0.35   # 约 ±39 km 的提示性范围（不是官方禁区！）
            poly = [[round(lat - half, 4), round(lon - half, 4)],
                    [round(lat - half, 4), round(lon + half, 4)],
                    [round(lat + half, 4), round(lon + half, 4)],
                    [round(lat + half, 4), round(lon - half, 4)]]
            obj = row.get("object", "?")
            hint = hint_map.get(obj, "一级")
            t0 = __import__("datetime").datetime.strptime(cfg["mission"]["t0_utc"],
                                                         "%Y-%m-%dT%H:%M:%SZ")
            b = t0 + timedelta(seconds=t_met) - timedelta(minutes=20)
            c = t0 + timedelta(seconds=t_met) + timedelta(minutes=20)
            zones.append({"id": "预测-%s" % obj, "b_utc": b.strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "c_utc": c.strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "q_line": "", "fir": "", "polygon_latlon": poly,
                          "stage_hint": hint,
                          "verified": "由标称弹道预测生成（无官方航警依据，仅供分析）"})
        cfg["notams"] = zones
        cfg["_v3"]["notams_mode"] = "predicted"
        self._save_config(cfg)
        self._sink("info", "已按预测落点生成 %d 个提示性落区（非官方）" % len(zones))
        # 重新导出一次，让 sim/notam_zones.json 与落点判定包含预测区
        code = self._run_script("export", ["--config", self.config_path])
        return code == 0

    # ---- 残骸类型两遍法校正 -------------------------------------------- #
    def _step_stagehint(self) -> bool:
        """两遍法：先按标称参数试算，得到各残骸预测落点，再按「落点↔航警区」最近匹配修正类型。

        为什么需要：航警原文通常不写"哪一级的落区"，只按"距发射场远近排序"猜类型并不可靠
        （实测某 CZ-6A 任务两条区距离只差 74 km，被猜成"一级/整流罩"，而预测落点显示它们
        更可能都是整流罩区）。用模型预测落点反查航警类型，比距离排序可靠得多。
        """
        cfg = self._load_config()
        ids = (cfg.get("_v3") or {}).get("stage_hints_inferred") or []
        if not ids:
            self._skip("残骸类型为人工指定，无需按预测落点校正")
            return True
        impacts = self._read_csv("sim/落点与射程.csv")
        if not impacts:
            self._sink("info", "先按标称参数试算一次，得到各残骸预测落点…")
            if self._run_script("export", ["--config", self.config_path]) != 0:
                self._sink("warn", "试算失败，跳过残骸类型校正")
                return False
            impacts = self._read_csv("sim/落点与射程.csv")
        if not impacts:
            return False
        from . import infer as I
        zones = [z for z in (cfg.get("notams") or []) if z.get("id") in ids]
        pts = []
        for r in impacts:
            try:
                pts.append((r.get("object", ""), float(r["lat_deg"]), float(r["lon_deg"])))
            except (KeyError, TypeError, ValueError):
                continue
        if not zones or not pts:
            return True
        used = set()
        changed = []
        for z in zones:
            cen = I.zone_centroid(z)
            if not cen:
                continue
            cand = []
            for obj, la, lo in pts:
                if obj in used:
                    continue
                cand.append((I.great_circle_km(cen[0], cen[1], la, lo), obj))
            if not cand:
                continue
            cand.sort()
            dist, obj = cand[0]
            hint = self._obj_hint(obj)
            if not hint:
                continue
            used.add(obj)
            old = z.get("stage_hint") or "未标"
            z["stage_hint"] = hint
            z["object_hint"] = obj          # 一对一绑定到具体残骸对象（如 fairing_L）
            z["verified"] = (z.get("verified") or "") + \
                "；按预测落点最近匹配修正为「%s / %s」（距区中心 %.0f km）" % (hint, obj, dist)
            changed.append("%s：%s → %s（%s，%.0f km）" % (
                z.get("id", "?"), old, hint, obj, dist))
            if old not in ("", "未标", None) and str(hint) not in str(old):
                _msg = ("航警 %s 原文残骸类型为「%s」，程序按预测落点改判为「%s / %s」"
                        "（距区中心 %.0f km）；如与航警原文不符，请在「航警」页手动指定类型，"
                        "或核对该区是否属于本任务" % (z.get("id", "?"), old, hint, obj, dist))
                self._sink("warn", _msg)
                cfg.setdefault("_v3", {}).setdefault("warnings", []).append(_msg)
            if dist > 500.0:
                _msg = ("航警 %s 与最近预测落点 %s 相距 %.0f km（>500 km）：该区可能不属于本任务"
                        "（另一发任务/另一类残骸），建议核对航警原文或删除该区"
                        % (z.get("id", "?"), obj, dist))
                self._sink("warn", _msg)
                cfg.setdefault("_v3", {}).setdefault("warnings", []).append(_msg)
        cfg["_v3"]["stage_hints_inferred"] = []
        cfg["_v3"]["stage_hints_revised"] = True
        if changed:
            cfg.setdefault("_v3", {}).setdefault("warnings", []).append(
                "残骸类型已按预测落点校正：" + "；".join(changed))
            for c0 in changed:
                self._sink("info", "残骸类型校正（两遍法）：" + c0)
        else:
            self._sink("info", "残骸类型按预测落点校核：与初值一致")
        self._save_config(cfg)
        return True

    # ---- 反演 --------------------------------------------------------- #
    def _step_invert(self) -> bool:
        cfg = self._load_config()
        inv = cfg.get("inversion") or {}
        if not (inv.get("parameters") or []):
            self._skip("本任务没有需要反演的参数（配置未生成 inversion.parameters）")
            return True
        argv = ["--config", self.config_path]
        if self.opt.quick_invert:
            argv.append("--quick")
        jobs = self._jobs()
        # 始终显式传 --jobs：invert.py 的缺省是"自动取 CPU 核数"，实测多进程反而更慢
        argv += ["--jobs", str(jobs)]
        if self.opt.mode == "legacy":
            argv.append("--legacy")
        else:
            argv.append("--accurate")
        de = inv.get("de") or {}
        if de.get("maxiter"):
            argv += ["--maxiter", str(de["maxiter"])]
        if de.get("popsize"):
            argv += ["--popsize", str(de["popsize"])]
        est = ((inv.get("_plan") or {}).get("est_simulations")
               or int(de.get("maxiter", 0)) * int(de.get("popsize", 0)) * len(inv["parameters"]))
        self._sink("info", "反演参数：%s（约 %s 次仿真，本机约 %.0f s，进程数 %s）" % (
            "、".join(p["name"] for p in inv["parameters"]), est, 0.3 * float(est), jobs))
        code = self._run_script("invert", argv)
        if code not in (0, 1):
            return False
        # 回填最优点到 config.json
        try:
            best = json.load(open(os.path.join(self.workdir, "sim", "反演参数.json"),
                                  encoding="utf-8-sig"))
            params = best.get("parameters") or {}
            changed = []
            for spec in inv.get("parameters") or []:
                name = spec.get("name")
                if name in params:
                    self._set_path(cfg, spec["path"], params[name])
                    changed.append("%s=%.4f" % (name, params[name]))
            conv = bool(best.get("converged", True))
            obj_val = float(best.get("objective") or 0)
            nom_val = best.get("nominal_objective")
            cfg["_v3"]["inversion"] = {"objective": obj_val,
                                       "nominal_objective": nom_val,
                                       "converged": conv,
                                       "nfev": best.get("nfev"),
                                       "applied": params}
            if not conv and os.path.exists(os.path.join(self.workdir, "config_initial.json")):
                # v3.2 收敛守卫：反演没能显著改善就**回退标称参数**，避免交付"越反越坏"的结果
                try:
                    init_cfg = json.load(open(os.path.join(self.workdir, "config_initial.json"),
                                              encoding="utf-8-sig"))
                    for spec in inv.get("parameters") or []:
                        try:
                            self._set_path(cfg, spec["path"],
                                           self._get_path(init_cfg, spec["path"]))
                        except Exception:  # noqa: BLE001
                            pass
                except Exception as exc:  # noqa: BLE001
                    self._sink("warn", "回退标称参数失败：%s" % exc)
                cfg["_v3"]["inversion"]["reverted"] = True
                cfg.setdefault("_v3", {}).setdefault("warnings", []).append(
                    "参数反演未收敛（最优目标 %.1f，标称 %s），已回退到标称参数；"
                    "请核对航警残骸类型/多边形，或改用与本次任务匹配的箭型参数"
                    % (obj_val, ("%.1f" % float(nom_val)) if nom_val is not None else "—"))
                self._save_config(cfg)
                self._sink("warn", "反演未显著改善（最优 %.1f vs 标称 %s）：已**回退标称参数**，"
                                   "落点仍可能不在区内，请核对航警/箭型参数" % (
                                       obj_val, ("%.1f" % float(nom_val))
                                       if nom_val is not None else "—"))
                return True
            cfg.setdefault("mission", {})["t0_note"] = cfg["mission"].get("t0_note", "")
            self._save_config(cfg)
            self._sink("info", "反演目标 %.3f；已回填 config：%s" % (obj_val, "，".join(changed)))
            if obj_val > 50:
                self._sink("warn", "反演目标值偏大（>50）：落点可能仍未入区，请检查航警 stage_hint、"
                                   "多边形方向、参考轨道是否与箭型匹配")
        except Exception as exc:  # noqa: BLE001
            self._sink("warn", "读取反演结果失败（不影响后续）：%s" % exc)
        return True

    @staticmethod
    def _get_path(d: Dict[str, Any], path: str) -> Any:
        cur: Any = d
        for k in path.split("."):
            cur = cur[int(k)] if isinstance(cur, list) else cur[k]
        return cur

    @staticmethod
    def _set_path(d: Dict[str, Any], path: str, value: Any) -> None:
        keys = path.split(".")
        cur: Any = d
        for k in keys[:-1]:
            cur = cur[int(k)] if isinstance(cur, list) else cur[k]
        last = keys[-1]
        if isinstance(cur, list):
            cur[int(last)] = value
        else:
            cur[last] = value

    # ---- 单脚本步骤 ---------------------------------------------------- #
    def _step_t0win(self) -> bool:
        """载人任务：按交会发射窗口反推 T0（迭代到平面/T0 自洽）。

        v4.2 起：
        * 窗口用**实测入轨平面**（不再用理想平面替代），并额外报告几何窗口
          （发射场穿越目标轨道面）；
        * T0 采用后迭代重跑 export → 重算窗口，直到推荐偏移 ≤ 5 s（最多 3 轮），
          因为 T0 平移会轻微改变入轨面/相位；
        * 记录入轨面残差（与空间站面的夹角），>0.15° 时给出警告。
        """
        cfg = self._load_config()
        if not (cfg.get("station") or {}).get("enabled"):
            self._skip("未启用空间站交会（非载人任务）")
            return True
        if not os.path.exists(os.path.join(self.workdir, "sim", "trajectory.csv")):
            self._sink("info", "先跑一次仿真，得到入轨点状态（交会窗口反推需要它）…")
            if self._run_script("export", ["--config", self.config_path]) != 0:
                self._skip("入轨点试算失败，无法反推交会窗口")
                return True
        argv = ["--config", self.config_path]
        if self.opt.allow_network:
            argv.append("--allow-network")
        # v4.3：先按目标 T0 把空间站平面写入制导（i_target/raan_target），
        # 再算窗口；这样"给定 T0 略微偏离穿面时刻"也能用末级 dogleg 修正。
        self._run_script("plane_target", argv)
        if self._run_script("t0_window", argv) != 0:
            self._skip("交会窗口计算失败（详见上方日志）")
            return True
        data = self._read_json("report/交会发射窗口.json") or {}
        if not data.get("enabled"):
            self._skip("交会窗口不可用：%s" % (data.get("reason") or "未知原因"))
            return True
        v3 = cfg.get("_v3") or {}
        mode = str(v3.get("t0_mode") or "auto")
        user_given = bool(v3.get("t0_user_given"))
        adopt = (mode == "rendezvous") or (mode == "auto" and not user_given)
        mission = cfg.setdefault("mission", {})
        applied = 0
        for _it in range(3):
            rec = data.get("recommended_t0_utc")
            if not rec:
                self._sink("warn", "没有可行的交会窗口：%s"
                           % (data.get("recommended_reason") or "—"))
                return True
            shift = float(data.get("shift_s") or 0.0)
            resid = data.get("plane_residual_deg")
            self._sink("info", "交会窗口（第 %d 轮）：可行 %s 个；推荐 T0 %s（%+.0f s）；"
                       "实测入轨面残差 %s°" % (
                           _it + 1, data.get("feasible_n"), rec, shift,
                           ("%.3f" % float(resid)) if resid is not None else "—"))
            if resid is not None and float(resid) > 0.15:
                _dw = data.get("site_plane_windows") or []
                _hint = ("（几何窗口：%s）" % "、".join(
                    "%s" % w.get("t0_utc") for w in _dw[:2])) if _dw else ""
                self._sink("warn", "入轨面与空间站面仍差 %.3f°%s；如需更精确，"
                                   "可在步骤②指定发射方位角或目标轨道后用反演重算。"
                           % (float(resid), _hint))
            if _it == 0:
                window_info = {
                    "nominal_t0_utc": str(mission.get("t0_utc") or ""),
                    "recommended_t0_utc": rec,
                    "recommended_t0_bjt": data.get("recommended_t0_bjt"),
                    "shift_s": data.get("shift_s"),
                    "feasible_n": data.get("feasible_n"),
                    "reason": data.get("recommended_reason"),
                    "plane_residual_deg": data.get("plane_residual_deg"),
                    "geometric_windows": [w.get("t0_utc") for w in
                                          (data.get("site_plane_windows") or [])],
                    "applied": False,
                }
                if data.get("recommended_window"):
                    window_info["window"] = data["recommended_window"]
            if not adopt:
                mission["t0_window"] = window_info
                self._save_config(cfg)
                # 保留用户 T0 时也要按目标平面飞一遍，保证残差可核算
                self._run_script("plane_target", argv)
                self._run_script("export", ["--config", self.config_path])
                self._run_script("t0_window", argv)
                if abs(shift) > 60:
                    self._sink("warn", "标称 T0 不在交会可行窗口内（最近窗口 %+.0f s）。"
                                       "本次保留你给定的 T0；若希望按窗口反推，请在"
                                       "「① 任务与箭型」勾选「按交会窗口反推 T0」后重跑。"
                               % shift)
                self._skip("保留用户给定的 T0（未勾选按交会窗口反推）")
                return True
            if abs(shift) <= 5.0:
                window_info["applied"] = bool(applied)
                mission["t0_window"] = window_info
                v3["t0_from_window"] = bool(applied)
                cfg["_v3"] = v3
                self._save_config(cfg)
                # v4.3：T0 不变时也要按目标平面飞一遍（含倾角/RAAN 的 dogleg），
                # 否则"保留用户 T0"只是文字保留，弹道平面仍按旧参数飞。
                self._run_script("plane_target", argv)
                self._run_script("export", ["--config", self.config_path])
                # 刷新窗口 JSON，让报告 §4.4 的残差/最佳 T0 与最终弹道一致
                self._run_script("t0_window", argv)
                if applied:
                    self._sink("info", "T0 反推完成（共平移 %d 次），入轨面残差 %s°"
                               % (applied, ("%.3f" % float(resid))
                                  if resid is not None else "—"))
                    return True
                self._skip("标称 T0 已在交会可行窗口内（%+.0f s），无需平移" % shift)
                return True
            # 平移 T0 → 重跑 export → 重算窗口
            old = str(mission.get("t0_utc") or "")
            mission["t0_utc"] = rec
            mission["t0_note"] = (
                "%s；按交会发射窗口反推（原 T0 %s，偏移 %+.0f s，预计对接 %s）"
                % (mission.get("t0_note") or "", old, shift,
                   (data.get("recommended_window") or {}).get("dock_utc") or "—"))
            window_info["applied"] = True
            window_info["final_t0_utc"] = rec
            mission["t0_window"] = window_info
            v3["t0_from_window"] = True
            v3["needs_refine_t0"] = False
            cfg["_v3"] = v3
            self._save_config(cfg)
            self.result.summary["T0 反推"] = "%s → %s（%+.0f s）" % (old, rec, shift)
            self._sink("info", "T0 按交会窗口反推：%s → %s（%+.0f s），重跑仿真…"
                       % (old, rec, shift))
            self._run_script("plane_target", argv)
            if self._run_script("export", ["--config", self.config_path]) != 0:
                self._sink("warn", "T0 平移后重跑仿真失败；后续步骤仍按新 T0 的配置执行")
                return False
            if self._run_script("t0_window", argv) != 0:
                return True
            data = self._read_json("report/交会发射窗口.json") or {}
            applied += 1
        return True

    def _step_export(self) -> bool:
        argv = ["--config", self.config_path]
        argv.append("--legacy" if self.opt.mode == "legacy" else "--accurate")
        code = self._run_script("export", argv)
        if code != 0:
            return False
        if self._fix_t0_if_needed():
            # T0 平移后需重算一次，保证产物与最终 config 一致
            code = self._run_script("export", argv)
            if code != 0:
                return False
        return self._check_export_sane()

    #: 残骸对象名 → 航警区 stage_hint 关键词（前缀匹配由 _obj_hint 处理）
    OBJ_HINT = {"core_stage": "一级", "stage_0": "一级", "stage_1": "二级",
                "fairing_L": "整流罩", "fairing_R": "整流罩", "boosters": "助推器"}

    @classmethod
    def _obj_hint(cls, obj: str) -> str:
        """残骸对象名 → 残骸类型。export 里助推器会叫 ``booster_1``…，这里用前缀兜住。"""
        o = str(obj or "")
        if o in cls.OBJ_HINT:
            return cls.OBJ_HINT[o]
        if o.startswith("booster"):
            return "助推器"
        if o.startswith("fairing"):
            return "整流罩"
        if o.startswith("stage_"):
            return "一级" if o == "stage_0" else "二级"
        return ""

    def _fix_t0_if_needed(self) -> bool:
        """T0 自洽性校正：航警 B)/C) 必须覆盖各残骸到达时刻。

        v2 靠人工调 T0；v3 在仿真出到达时刻后反推"可行 T0 区间"并做**最小平移**。
        只在 T0 是推断值时自动平移（用户明确给定的 T0 不擅自改，只提示）。
        """
        cfg = self._load_config()
        mission = cfg.get("mission") or {}
        feas = self._t0_feasible(cfg)
        if feas is None:
            return False
        lo, hi, detail, t0 = feas
        if lo <= t0 <= hi:
            self._sink("info", "T0 与航警窗口自洽（可行区间 %s ~ %s）" % (
                lo.strftime("%H:%M:%S"), hi.strftime("%H:%M:%S")))
            return False
        inferred = "推断" in (mission.get("t0_note") or "")
        if not inferred:
            self._sink("warn", "用户给定的 T0 落在航警可行区间 %s ~ %s 之外：可能航警与 T0 不匹配，"
                               "请核对（程序不擅自修改用户给定 T0）" % (
                                   lo.strftime("%H:%M:%S"), hi.strftime("%H:%M:%S")))
            return False
        margin = timedelta(seconds=60)
        target = lo + margin if t0 < lo else hi - margin
        if target < lo or target > hi:
            target = (lo + (hi - lo) / 2)
        shift = (target - t0).total_seconds()
        cfg["mission"]["t0_utc"] = target.strftime("%Y-%m-%dT%H:%M:%SZ")
        cfg["mission"]["t0_note"] = "%s；按航警可行区间平移 %+.1f min（区间 %s ~ %s）" % (
            mission.get("t0_note", ""), shift / 60.0, lo.strftime("%H:%M:%S"),
            hi.strftime("%H:%M:%S"))
        self._save_config(cfg)
        self._sink("info", "T0 校正：%s → %s（%+.1f min）" % (
            t0.strftime("%H:%M:%S"), target.strftime("%H:%M:%SZ"[:8]), shift / 60.0))
        for d in detail:
            self._sink("log", "  " + d)
        return True

    def _check_export_sane(self) -> bool:
        """仿真基本可用性检查（跑飞/落点异常直接报错，避免后面白跑十几分钟）。"""
        orb = self._read_json("sim/入轨根数.json")
        imp = self._read_csv("sim/落点与射程.csv")
        ev = self._read_csv("sim/弹道事件表.csv")
        if not ev:
            self._sink("error", "弹道事件表为空：仿真未产生事件，参数可能严重不合理")
            return False
        if orb:
            a, e = orb.get("a_km"), orb.get("e")
            self._sink("info", "入轨：a=%.1f km, e=%.4f, i=%.3f°，近地点 %.0f km" % (
                float(a or 0), float(e or 0), float(orb.get("i_deg") or 0),
                float(orb.get("perigee_km") or 0)))
            if e is not None and float(e) > 0.2 and float(orb.get("perigee_km") or 0) < 100:
                self._sink("warn", "轨道偏心率偏大/近地点过低：可能未正常入轨，请检查箭型参数与制导")
        for row in imp or []:
            if row.get("inside_zone") == "true":
                self._sink("info", "落点 %s 在 %s 区内（距边界 %s km）" % (
                    row.get("object"), row.get("zone_id"), row.get("dist_to_boundary_km")))
        return True

    def _step_station(self) -> bool:
        cfg = self._load_config()
        if not (cfg.get("station") or {}).get("enabled"):
            self._skip("未启用空间站交会（非载人任务，或用户在「空间站与返回」页关闭）")
            return True
        argv = ["--config", self.config_path]
        if self.opt.allow_network:
            argv.append("--allow-network")
        return self._run_script("station_track", argv) == 0

    def _step_docking(self) -> bool:
        cfg = self._load_config()
        if not (cfg.get("station") or {}).get("enabled"):
            self._skip("未启用空间站交会（非载人任务，或用户关闭）")
            return True
        argv = ["--config", self.config_path]
        if self.opt.allow_network:
            argv.append("--allow-network")
        if self._run_script("rendezvous", argv) != 0:
            return False
        # v4.2：全流程星下点图（发射→入轨→相位→转移→对接）
        rc = self._run_script("plot_rendezvous", ["--config", self.config_path])
        if rc != 0:
            self._sink("warn", "交会全流程星下点图生成失败（不影响其余成果）")
        return True

    def _step_return(self) -> bool:
        cfg = self._load_config()
        rc = cfg.get("return_capsule") or {}
        if not rc.get("enabled"):
            self._skip("未勾选「返回舱返回」（在「空间站与返回」页启用）")
            return True
        argv = ["--config", self.config_path]
        if self.opt.allow_network:
            argv.append("--allow-network")
        return self._run_script("return_capsule", argv) == 0

    def _step_window(self) -> bool:
        # v3.4：发射窗口既包含航警时窗，也包含轨道平面/交会窗口；
        # 即使没有航警也值得计算（由窗口脚本自行判断是否有平面约束）。
        return self._run_script("window", ["--config", self.config_path]) == 0

    def _step_sky(self) -> bool:
        cfg = self._load_config()
        if not cfg.get("observers"):
            self._skip("无观测点且关闭了「自动用发射场作观测点」")
            return True
        obs_id = cfg["observers"][0].get("id", "")
        code = self._run_script("plot_sky", ["--config", self.config_path])
        if code != 0:
            return False
        self._advance("sky", 0.6)
        if not self._check_cancel():
            rc = self._run_script("plot_sky_refstyle", ["--config", self.config_path,
                                                        "--obs", obs_id])
            if rc != 0:
                self._sink("warn", "参考样式主图生成失败（不影响其余 sky 成果，详见上方日志）")
        return True

    def _step_appear(self) -> bool:
        cfg = self._load_config()
        if not cfg.get("observers"):
            self._skip("无观测点（观测点为空）")
            return True
        if not os.path.exists(os.path.join(self.workdir, "sky", "sky_summary.json")):
            self._skip("上游天空轨迹未生成 sky_summary.json（本步骤依赖它）")
            return True
        return self._run_script("make_appearance_terrain_map",
                                ["--config", self.config_path]) == 0

    def _step_map(self) -> bool:
        # v3.2.2：全国最大高度角地图不依赖用户指定观测点（plot_map 内部对空观测点有兜底），
        # 因此不再因缺观测点跳过。
        cfg = self._load_config()
        if not cfg.get("observers"):
            self._sink("info", "未指定观测点：全国地图照常生成（图上不叠加观测点标注）")
        return self._run_script("plot_map", ["--config", self.config_path]) == 0

    def _step_mc(self) -> bool:
        n = int(self.opt.mc_n or 0)
        if n <= 0:
            self._skip("蒙特卡洛样本数为 0（在「输出选项」页填写 100~500 可启用）")
            return True
        argv = ["--config", self.config_path, "--n", str(n)]
        jobs = self._jobs()
        argv += ["--jobs", str(jobs)]
        argv.append("--legacy" if self.opt.mode == "legacy" else "--accurate")
        t0 = time.time()
        code = self._run_script("mc", argv)
        self._sink("info", "蒙特卡洛完成 %.1f s" % (time.time() - t0))
        return code in (0, 1)

    def _step_kml(self) -> bool:
        cfg = self._load_config()
        if bool(cfg.get("return_only")) or str(
                (cfg.get("mission") or {}).get("profile") or "") == "return":
            self._skip("返回任务的 KML/KMZ 已由「返回舱返回」步骤生成："
                       "viz/返回舱弹道.kml / .kmz")
            return True
        return self._run_script("make_kml_refstyle", ["--config", self.config_path]) == 0

    def _step_summary(self) -> bool:
        """通用成果：关键节点 / 飞行阶段 / 环境剖面 / 轨迹统计 / 摘要 / 成果清单。"""
        from . import summary as S
        cfg = self._load_config()
        res = S.generate_all(self.workdir, cfg, steps=self.result.steps,
                             logger=lambda s: self._sink("log", s))
        self.result.summary["通用成果"] = "%d 个文件；关键节点 %d 条" % (
            len(res.get("files") or []), res.get("nodes") or 0)
        # 本步之后的 report/qc/成果清单 还没生成，此时不报"缺失"，留到 _finish 统一核对
        _pending = {"report/弹道仿真报告.md", "report/qc_report.json",
                    "report/成果清单.md", "report/成果清单.json"}
        _real = [x for x in (res.get("missing") or []) if x not in _pending]
        if _real:
            self._sink("warn", "输出完整性：缺少 %d 项（%s）；原因见 report/成果清单.md" % (
                len(_real), "、".join(_real[:6])))
        return True

    def _refresh_common(self, quiet: bool = False) -> None:
        """重算一次通用成果（用最终步骤台账刷新成果清单/摘要）。"""
        try:
            from . import summary as S
            cfg = self._load_config()
            steps = [dict(s) for s in self.result.steps]
            S.generate_all(self.workdir, cfg, steps=steps,
                           logger=(None if quiet else (lambda s: self._sink("log", s))))
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                self._sink("warn", "刷新通用成果失败（不影响其余成果）：%s" % exc)

    def _step_report(self) -> bool:
        from . import report as R
        cfg = self._load_config()
        path = R.generate_report(self.workdir, cfg, log_tail=self._tail_log(80))
        self._sink("info", "报告：%s" % path)
        return bool(path)

    def _step_qc(self) -> bool:
        code = self._run_script("qc", ["--config", self.config_path])
        try:
            from . import qc_summary as Q
            s = Q.summarize_qc(self.workdir)
            self.result.summary["交付自检"] = "%d 项通过 / %d 项未通过" % (s["n_pass"], s["n_fail"])
            for adv in s.get("advice") or []:
                self._sink("warn", adv)
        except Exception:  # noqa: BLE001
            pass
        # 报告在自检前生成（qc 需要它存在），这里用自检结果再刷新一次，让 §7 有内容
        try:
            from . import report as _R
            _R.generate_report(self.workdir, self._load_config(), log_tail=self._tail_log(60))
            self._sink("info", "已用交付自检结果刷新技术报告")
        except Exception:  # noqa: BLE001
            pass
        return code == 0

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    def _jobs(self) -> int:
        """并行进程数。

        v4 修正：v3 这里写了一串互相抵消的分支，实际永远返回 1（注释说"默认串行"，
        但用户显式设 jobs>1 时也被吞掉）。现在按"用户显式值 > 环境变量 > 串行"生效：
        * 冻结（PyInstaller）态多进程不可靠 → 强制 1；
        * ``RPK_FORCE_PARALLEL=1``（或旧名 RK_FORCE_PARALLEL）→ 取 CPU 核数（上限 8）；
        * 用户显式 jobs>1 → 用它；其余情况串行（本机实测 DE 串行更快）。
        """
        if P.FROZEN:
            return 1
        j = int(self.opt.jobs or 0)
        if j > 1:
            return min(j, 32)
        if str(os.environ.get("RK_FORCE_PARALLEL", "")).lower() in ("1", "true"):
            return max(1, min(8, os.cpu_count() or 1))
        return 1

    def _read_json(self, rel: str) -> Optional[Dict[str, Any]]:
        try:
            with open(os.path.join(self.workdir, rel), encoding="utf-8-sig") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return None

    def _read_csv(self, rel: str) -> List[Dict[str, str]]:
        import csv
        try:
            with open(os.path.join(self.workdir, rel), encoding="utf-8-sig") as f:
                return list(csv.DictReader(f))
        except Exception:  # noqa: BLE001
            return []

    def _tail_log(self, n: int = 80) -> str:
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as f:
                return "".join(f.readlines()[-n:])
        except Exception:  # noqa: BLE001
            return ""

    def _collect_artifacts(self) -> None:
        arts: List[str] = []
        for rel in ("config.json", "config_initial.json", "任务输入.json",
                    "sim/落点与射程.csv", "sim/弹道事件表.csv", "sim/入轨根数.json",
                    "sim/反演参数.json",
                    "sim/关键节点.csv", "sim/飞行阶段.csv", "sim/飞行环境.csv",
                    "sim/轨迹统计.json", "sim/飞行环境关注项.json",
                    "sim/空间站轨道.json", "sim/空间站星下点.csv",
                    "sim/交会对接.json", "sim/交会对接_时序.csv",
                    "sim/CZ2F_任务前后30min_空间站.csv",
                    "sim/返回舱返回.json", "sim/返回舱_时序.csv",
                    "sim/返回舱弹道.png", "sim/返回舱窗口.csv",
                    "sim/返回舱事件与分离点.csv", "sim/返回舱阶段.csv",
                    "sim/返回前30分钟_空间站与神舟.csv",
                    "map/返回舱落区.png",
                    "viz/返回舱弹道.kml", "viz/返回舱弹道.kmz",
                    "report/发射窗口计算.json", "report/任务摘要.md",
                    "report/任务摘要.json", "report/成果清单.md", "report/成果清单.json",
                    "sky/sky_summary.json", "map/全国最大高度角地图.png",
                    "viz/弹道三维_含残骸坠落.kmz", "viz/弹道三维_含残骸坠落.kml",
                    "uncertainty/mc_stats.json", "report/弹道仿真报告.md",
                    "report/qc_report.json", "运行日志.txt"):
            p = os.path.join(self.workdir, rel.replace("/", os.sep))
            if os.path.exists(p):
                arts.append(p)
        self.result.artifacts = arts

    def _build_summary(self) -> None:
        s: Dict[str, Any] = {}
        orb = self._read_json("sim/入轨根数.json") or {}
        if orb:
            s["入轨"] = "h_peri=%.0f km, h_apo=%.0f km, i=%.2f°, e=%.4f" % (
                float(orb.get("perigee_km") or 0), float(orb.get("apogee_km") or 0),
                float(orb.get("i_deg") or 0), float(orb.get("e") or 0))
        imp = self._read_csv("sim/落点与射程.csv")
        if imp:
            parts = []
            for r in imp:
                if r.get("inside_zone") == "true":
                    state = "区内 %s（距边界 %s km）" % (r.get("zone_id"),
                                                    r.get("dist_to_boundary_km"))
                elif r.get("zone_id"):
                    state = "%s 区外" % r.get("zone_id")
                else:
                    state = "不在任何区内"
                parts.append("%s %.3f,%.3f（%s）" % (r.get("object"),
                                                    float(r.get("lat_deg") or 0),
                                                    float(r.get("lon_deg") or 0), state))
            s["残骸落点"] = "；".join(parts)
        win = self._read_json("report/发射窗口计算.json") or {}
        w0 = win.get("practical_window") or win.get("model_window") or None
        if w0 and float(w0.get("duration_min") or 0) > 0:
            s["发射窗口"] = "%s ~ %s UTC（%.1f min）" % (
                w0.get("start_utc", "?"), w0.get("end_utc", "?"),
                float(w0.get("duration_min") or 0))
        elif win:
            s["发射窗口"] = "无可行窗口%s" % (("（" + str(win.get("reason")) + "）")
                                       if win.get("reason") else "")
        ow = win.get("orbit_window") or {}
        if ow.get("available") and ow.get("best_near_t0"):
            bw = ow["best_near_t0"]
            rv = bw.get("rendezvous") or {}
            s["轨道/交会窗口"] = "%s UTC（ΔRAAN %.3f°%s）" % (
                bw.get("best_utc", "?"), float(bw.get("d_raan_deg") or 0),
                ("；交会可行，ΔV %.0f m/s，对接 %s" % (
                    float(rv.get("dv_total_mps") or 0), rv.get("dock_utc", "")))
                if rv.get("feasible") else "")
        st = self._read_json("sim/空间站轨道.json") or {}
        if st.get("enabled"):
            a = st.get("at_t0") or {}
            s["空间站"] = "%s %.1f km / %.2f°（%s）" % (
                (st.get("station") or {}).get("name", "空间站"),
                float(a.get("a_km") or 0) - 6378.137, float(a.get("i_deg") or 0),
                (st.get("station") or {}).get("kind", "?"))
        dk = self._read_json("sim/交会对接.json") or {}
        if dk.get("enabled") and dk.get("docking"):
            d0 = dk["docking"]
            s["交会对接"] = "%s（%s UTC，总 ΔV %.0f m/s，%s）" % (
                "可行" if dk.get("feasible") else "不可行",
                d0.get("t_utc", "?"), float(dk.get("total_dv_mps") or 0),
                (dk.get("plan") or {}).get("mode", ""))
        rt = self._read_json("sim/返回舱返回.json") or {}
        if rt.get("enabled"):
            sel = rt.get("selected") or {}
            if sel:
                s["返回舱"] = "%s → 落地 %s（%.2fN, %.2fE；%s）" % (
                    sel.get("t_deorbit_utc", "?"), sel.get("t_landing_utc", "?"),
                    float(sel.get("lat_deg") or 0), float(sel.get("lon_deg") or 0),
                    ("区内" if sel.get("inside_zone") else
                     ("最近 %.0f km" % float(sel.get("target_distance_km") or 0))))
                if rt.get("windows"):
                    s["返回窗口"] = "%d 个（%s）" % (
                        len(rt["windows"]), rt["windows"][0].get("start_utc", "?"))
        sky = self._read_json("sky/sky_summary.json") or {}
        obs = (sky.get("observers") or {})
        for oid, o in list(obs.items())[:2]:
            fa = o.get("first_appearance") or {}
            mx = o.get("max_elevation") or {}
            if fa:
                s["天空轨迹(%s)" % oid] = "首次出现 %s 方位 %.1f° 仰角 %.2f°；最大仰角 %.1f°" % (
                    fa.get("t_utc", "?"), float(fa.get("az_deg") or 0), float(fa.get("el_deg") or 0),
                    float(mx.get("el_deg") or 0))
        mc = self._read_json("uncertainty/mc_stats.json") or {}
        if mc:
            s["蒙特卡洛"] = "N=%s" % (mc.get("n") or "?")
        # v4：通用成果 + 输出完整性 + 任务类型
        kn = self._read_csv("sim/关键节点.csv")
        if kn:
            s["关键节点"] = "%d 条（见 sim/关键节点.csv）" % len(kn)
        inv = self._read_json("report/成果清单.json") or {}
        if inv:
            s["成果文件"] = "%s 个" % (inv.get("total") or 0)
            miss = inv.get("missing") or []
            if miss:
                s["输出完整性"] = "缺少 %d 项（见 report/成果清单.md）" % len(miss)
            elif inv.get("expected"):
                s["输出完整性"] = "应产出项全部齐备（%d 项）" % len(inv.get("expected") or [])
        mt = self._read_json("sim/轨迹统计.json") or {}
        if mt.get("mission_type"):
            s["任务类型"] = {"standard": "常规发射弹道", "crewed": "载人发射弹道（含交会）",
                            "return": "返回舱返回弹道"}.get(mt["mission_type"],
                                                            mt["mission_type"])
        if self._skipped_steps():
            s["跳过步骤"] = "、".join("%s（%s）" % (x.get("name"), x.get("reason") or "")
                                     for x in self._skipped_steps()[:5])
        self.result.summary = s

    def _skipped_steps(self) -> List[Dict[str, Any]]:
        """被条件跳过的步骤（不含"未勾选/不适用"，那些是用户选择或任务类型决定）。"""
        return [x for x in self.result.steps
                if str(x.get("state") or "") == self.ST_SKIP]

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def run(self) -> RunResult:
        os.makedirs(self.workdir, exist_ok=True)
        try:
            self._log_fh = open(self.log_path, "w", encoding="utf-8")
        except Exception:  # noqa: BLE001
            self._log_fh = None
        self._write_log("=== 火箭弹道工具箱 v%s 运行开始 %s ===" % (
            P.app_version(), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

        cfg = {}
        try:
            cfg = self._load_config()
        except Exception as exc:  # noqa: BLE001
            self.result.error = "读取配置失败：%s" % exc
            self._sink("error", self.result.error)

        mods = dict((cfg.get("_v3") or {}).get("modules") or {})
        mods.update(self.opt.modules or {})
        _return_only = bool(cfg.get("return_only")) or str(
            (cfg.get("mission") or {}).get("profile") or "") == "return"

        # 步骤计划：常规/载人 = 上升段全流程；返回 = 空间站+返回段（其余记为「不适用」，
        # 而不是静默消失——用户能从步骤台账看到"为什么没有这一项"）。
        if _return_only:
            order = ["station", "return", "kml", "summary", "report", "qc"]
            not_applicable = {
                "dem": "返回任务无上升段，不需要地形瓦片",
                "invert": "返回任务无可反演参数（无落区/上升段）",
                "export": "返回任务不计算上升段弹道",
                "window": "返回任务无发射窗口；返回窗口见 sim/返回舱窗口.csv",
                "docking": "返回任务不做入轨—交会对接",
                "sky": "天空轨迹依赖上升段弹道（观测点天空轨迹）",
                "appear": "出现方位图依赖上升段弹道与天空汇总",
                "map": "全国最大高度角地图依赖上升段弹道",
                "mc": "蒙特卡洛依赖上升段弹道",
            }
            essential = set()
        else:
            order = ["dem", "invert", "export", "station", "window", "docking",
                     "return", "sky", "appear", "map", "mc", "kml", "summary",
                     "report", "qc"]
            not_applicable = {}
            if not (cfg.get("station") or {}).get("enabled"):
                not_applicable["station"] = "非载人/交会任务（可在「空间站与返回」页启用）"
                not_applicable["docking"] = "非载人/交会任务（可在「空间站与返回」页启用）"
            if not (cfg.get("return_capsule") or {}).get("enabled"):
                not_applicable["return"] = "未勾选「返回舱返回」（在「空间站与返回」页启用）"
            essential = {"export"}

        # 预估权重（进度条）
        w = [("env", 0.3)]
        for k, wt in (("dem", 2.0), ("invert", 30.0), ("export", 3.0),
                      ("station", 1.0), ("window", 0.5), ("docking", 1.5),
                      ("return", 3.0),
                      ("sky", 8.0), ("appear", 2.0), ("map", 5.0), ("mc", 8.0),
                      ("kml", 1.5), ("summary", 0.4), ("report", 0.5), ("qc", 0.5)):
            if k in order and mods.get(k):
                w.append((k, wt))
        if mods.get("invert") and not ((cfg.get("inversion") or {}).get("parameters")):
            w = [x for x in w if x[0] != "invert"]
        if (cfg.get("_v3") or {}).get("stage_hints_inferred"):
            w.insert(1, ("stagehint", 2.0))
        _crewed0 = bool((cfg.get("station") or {}).get("enabled")) and str(
            (cfg.get("mission") or {}).get("profile") or "") == "cz2f"
        if _crewed0:
            w.insert(2, ("t0win", 3.5))
        elif (cfg.get("_v3") or {}).get("needs_refine_t0"):
            w.insert(2, ("t0refine", 3.0))
        if (cfg.get("_v3") or {}).get("notams_mode") == "predict" and not cfg.get("notams"):
            w.insert(1, ("predict", 3.0))
        self._weights = w
        self._done_w = 0.0
        self.result.summary = {"任务类型": (
            "返回舱返回" if _return_only else "上升段弹道")}

        # ---------------- 执行 ----------------
        old_stdout, old_stderr = sys.stdout, sys.stderr
        if not self._run_step("env", self._step_env, essential=True):
            self._finish(old_stdout, old_stderr)
            return self.result
        try:
            sys.stdout = _Tee(self._sink, "log")  # type: ignore[assignment]
            sys.stderr = _Tee(self._sink, "error")  # type: ignore[assignment]

            if not _return_only:
                # 预测落区 / 残骸类型 / T0 精化 需要先有一次 export
                if (cfg.get("_v3") or {}).get("notams_mode") == "predict" and not cfg.get("notams"):
                    self._run_step("predict", self._step_predict, essential=False)
                    cfg = self._load_config()
                # 残骸类型校正要在 T0 精化之前：T0 精化也依赖"航警区↔残骸对象"的正确配对
                if (cfg.get("_v3") or {}).get("stage_hints_inferred"):
                    self._run_step("stagehint", self._step_stagehint, essential=False)
                    cfg = self._load_config()
                _crewed = bool((cfg.get("station") or {}).get("enabled")) and str(
                    (cfg.get("mission") or {}).get("profile") or "") == "cz2f"
                if _crewed:
                    # 载人任务：T0 由交会发射窗口决定（落区窗口宽、且不应与交会窗口打架）
                    if (cfg.get("_v3") or {}).get("needs_refine_t0"):
                        self._record_state(
                            "t0refine", self.ST_SKIP,
                            "载人任务 T0 由交会发射窗口决定，不做落区窗口精化")
                    ok = self._run_step("t0win", self._step_t0win, essential=False)
                    if ok:
                        cfg = self._load_config()
                elif (cfg.get("_v3") or {}).get("needs_refine_t0"):
                    ok = self._run_step("t0refine", self._step_t0refine, essential=False)
                    if ok:
                        cfg = self._load_config()

            handlers = {"dem": self._step_dem, "invert": self._step_invert,
                        "export": self._step_export, "station": self._step_station,
                        "window": self._step_window, "docking": self._step_docking,
                        "return": self._step_return,
                        "sky": self._step_sky, "appear": self._step_appear,
                        "map": self._step_map, "mc": self._step_mc,
                        "kml": self._step_kml, "summary": self._step_summary,
                        "report": self._step_report, "qc": self._step_qc}
            for key in order:
                if self._check_cancel():
                    self.result.cancelled = True
                    self._sink("warn", "用户取消，停止后续步骤")
                    self._record_state(key, self.ST_CANCEL, "用户取消")
                    break
                if key in not_applicable:
                    self._record_state(key, self.ST_NA, not_applicable[key])
                    continue
                if not mods.get(key, False) and key not in essential:
                    self._record_state(key, self.ST_OFF, "用户未勾选该模块（输出会相应减少）")
                    continue
                ok = self._run_step(key, handlers[key], essential=key in essential)
                if key in essential and not ok:
                    self.result.error = "关键步骤失败：%s" % self.STEP_NAMES.get(key, key)
                    break
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
        self._finish(old_stdout, old_stderr)
        return self.result

    def _finish(self, old_stdout, old_stderr) -> None:
        self._progress = 1.0
        # 用最终步骤台账刷新一遍通用成果（成果清单要包含 summary/report/qc 本身）
        self._refresh_common(quiet=True)
        self._collect_artifacts()
        self._build_summary()
        self.result.ok = bool(not self.result.failed_steps() and not self.result.cancelled
                              and not self.result.error)
        self._emit("progress", "", 1.0)
        if self.result.cancelled:
            self._sink("warn", "已取消。")
        elif self.result.ok:
            self._sink("info", "全部完成。成果目录：%s" % self.workdir)
        else:
            bad = "、".join(s["name"] for s in self.result.failed_steps())
            self._sink("warn", "存在失败步骤：%s（详情见 运行日志.txt）" % bad)
        self._write_log("=== 结束：ok=%s cancelled=%s ===" % (self.result.ok,
                                                            self.result.cancelled))
        if self._log_fh:
            try:
                self._log_fh.close()
            except Exception:  # noqa: BLE001
                pass
            self._log_fh = None


# --------------------------------------------------------------------------- #
# 环境体检（GUI "环境自检" 按钮 / CLI 使用）
# --------------------------------------------------------------------------- #
def check_environment() -> Dict[str, Any]:
    info: Dict[str, Any] = {}
    info["冻结运行"] = P.FROZEN
    info["应用版本"] = P.app_version()
    info["应用目录"] = str(P.APP_ROOT)
    info["引擎目录"] = str(P.ENGINE_DIR)
    info["引擎存在"] = P.ENGINE_DIR.exists()
    info["可写根"] = str(P.runtime_base())
    info["DEM 目录"] = str(P.dem_dir())
    try:
        from common.station import data_dir as _st_dir
        info["空间站数据"] = str(_st_dir())
    except Exception:  # noqa: BLE001
        info["空间站数据"] = str(P.APP_DATA / "station")
    try:
        from .dem_manager import DemManager as _DM
        info["自定义高程源"] = "%d 个" % len(_DM(str(P.dem_dir())).custom_sources())
    except Exception:  # noqa: BLE001
        info["自定义高程源"] = "不可用"
    info["Python"] = sys.version.split()[0]
    for name in ("numpy", "scipy", "matplotlib", "PIL", "sgp4"):
        try:
            m = __import__(name)
            info[name] = getattr(m, "__version__", "ok")
        except Exception as exc:  # noqa: BLE001
            info[name] = "缺失(%s)" % exc
    from . import infer as I
    db = {}
    for name in ("rockets", "launch_sites", "cities", "payloads", "orbits"):
        d = I.load_db(name)
        key = {"rockets": "rockets", "launch_sites": "sites", "cities": "cities",
               "payloads": "payloads", "orbits": "orbits"}[name]
        db[name] = len(d.get(key) or [])
    info["内置库"] = db
    npz = P.APP_DATA / "china_dem_0p02.npz"
    info["粗 DEM"] = ("%s (%.1f MB)" % (npz.name, npz.stat().st_size / 1048576.0)
                     if npz.exists() else "缺失")
    return info
