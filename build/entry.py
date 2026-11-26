# -*- coding: utf-8 -*-
"""PyInstaller 入口脚本：``火箭弹道工具箱.exe`` 的启动点。

只做三件事：

1. 冻结态下把可写资源/引擎路径准备好（``app.core.paths.setup_env`` +
   ``ensure_syspath``，与源码态 ``python -m app.main`` 完全一致）；
2. 支持 PyInstaller 多进程（``multiprocessing.freeze_support``，冻结态
   ``RK_FROZEN=1`` 时 runner 会强制串行，这里只是兜底）；
3. 把 ``sys.argv`` 原样交给 :func:`app.main.main`（GUI / CLI 参数都一样）。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if not getattr(sys, "frozen", False) and ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 控制台/管道统一 UTF-8（与控制台代码页无关）；无控制台的窗口态下拿不到流也忽略
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

from app.core import paths as P  # noqa: E402


def main() -> int:
    try:
        import multiprocessing

        multiprocessing.freeze_support()
    except Exception:  # noqa: BLE001
        pass

    P.setup_env()
    P.ensure_syspath()

    from app.main import main as app_main

    return int(app_main() or 0)


if __name__ == "__main__":
    sys.exit(main())
