# -*- coding: utf-8 -*-
"""任务工作区（成果目录）辅助：重复成果检测、新建序号目录。

GUI 与 CLI 共用，避免两边对“覆盖 / 新建”的判定不一致。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

# 只要命中任意一个，就认为该目录里有“上次计算的成果”
RESULT_MARKERS = (
    "config.json",
    "运行日志.txt",
    "sim/trajectory.csv",
    "sim/落点与射程.csv",
    "report/qc_report.json",
    "report/弹道仿真报告.md",
)


def has_results(workdir) -> bool:
    """目录里是否已有上一次计算的成果（命中任一 RESULT_MARKERS）。"""
    try:
        p = Path(workdir)
    except (TypeError, ValueError):
        return False
    if not p.is_dir():
        return False
    for rel in RESULT_MARKERS:
        try:
            if (p / rel).exists():
                return True
        except OSError:
            continue
    return False


def unique_dir(base) -> Path:
    """已存在则依次尝试 ``<name>_2``、``<name>_3`` …"""
    p = Path(base)
    if not p.exists():
        return p
    parent = p.parent
    stem = p.name
    for i in range(2, 1000):
        cand = parent / ("%s_%d" % (stem, i))
        if not cand.exists():
            return cand
    return parent / ("%s_%d" % (stem, int(time.time())))


def prepare_workdir(workdir, overwrite: bool = False) -> "tuple[Path, str]":
    """决定本次使用的成果目录。

    返回 ``(最终目录, 动作)``，动作为：

    * ``"new"``           目录不存在（全新）
    * ``"empty"``         目录已存在但里面没有成果（可安全复用）
    * ``"overwrite"``     目录已有成果且调用方选择覆盖（复用）
    * ``"renamed"``       目录已有成果、未选择覆盖 → 自动新建 ``_2`` 副本

    仅做判定，不创建目录（由调用方 ``mkdir``）。
    """
    p = Path(workdir)
    if not p.exists():
        return p, "new"
    if not has_results(p):
        return p, "empty"
    if overwrite:
        return p, "overwrite"
    return unique_dir(p), "renamed"


def overwrite_warning(workdir) -> str:
    """覆盖模式给用户看的提示文案（GUI/CLI 共用）。"""
    return ("工作区已有上次成果：%s\n"
            "覆盖模式将复用该目录：本次生成的同类文件会覆盖/清理旧文件；"
            "请确认不再需要旧结果。" % workdir)
