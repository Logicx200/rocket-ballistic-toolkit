# -*- coding: utf-8 -*-
"""PyInstaller 运行时钩子：中文输出/路径统一走 UTF-8。

* 冻结后的 exe 在中文 Windows 上默认按 GBK 打开控制台/管道，日志里的中文与符号
  （≤、±、→、°）会乱码或直接抛 ``UnicodeEncodeError``；
* 这里在解释器启动最早阶段把 ``PYTHONUTF8`` / ``PYTHONIOENCODING`` 设好，并在
  有控制台流时把 stdout/stderr 重设为 UTF-8（窗口态没有流则跳过）。
"""
import os
import sys

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass
