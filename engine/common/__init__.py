# -*- coding: utf-8 -*-
"""common — CZ-8A 上升段任务的公共基础库（地理/天文/绘图/数据访问）。

导入方式
--------
.. code-block:: python

    import sys
    sys.path.insert(0, r"D:\\Users\\rocket_launching\\20261025CZ8A")
    from common import geo, notam, plotstyle          # 明确导入
    import common.geo as geo                          # 等价
    common.geo.geodetic_to_ecef(19.6, 110.9, 10.0)    # 惰性属性访问亦可

子模块
------
* :mod:`common.geo`         WGS-84 大地↔ECEF、ECI/ECEF(GMST)、太阳位置、方位/高度角、
                            大圆与 Vincenty 测地线、ENU、downrange/crossrange
* :mod:`common.notam`       航警多边形解析、点在多边形内、到边界距离、面积/质心
* :mod:`common.plotstyle`   统一绘图风格（中文字体 + 配色 + 保存）
* :mod:`common.dem`         SRTM 1″ HGT 读取、DEM 采样、地形遮挡角（阶段 2）
* :mod:`common.boundaries`  Natural Earth 国界/省界/城市 + 中国陆地掩膜（阶段 2）

设计约束
--------
* `geo` / `notam` 只依赖 numpy；`plotstyle` 依赖 matplotlib；`dem`/`boundaries` 依赖 numpy(+Pillow)。
* 本包为**只读共享库**，任何模块都可以 import；修改需先联系 Lead（见 data_contract.md）。
"""
from __future__ import annotations

import json
import os
from importlib import import_module
from pathlib import Path

__version__ = "1.0.0"
__all__ = ["geo", "notam", "plotstyle", "dem", "boundaries", "load_config", "ROOT", "__version__"]

#: 工程根目录（common/ 的上一级；v3.0 起允许 RK_ENGINE 覆盖，供冻结打包定位 data/ne）
ROOT = Path(os.environ.get("RK_ENGINE") or Path(__file__).resolve().parents[1])
#: 公共配置路径（优先工作区 config.json，其次引擎根 config.json，最后 common/config.json）
CONFIG_PATH = Path(os.environ.get("RK_ROOT") or ROOT) / "config.json"

_SUBMODULES = {"geo", "notam", "plotstyle", "dem", "boundaries"}


def __getattr__(name):
    """惰性导入子模块：``common.geo`` / ``common.plotstyle`` 等按需加载。"""
    if name in _SUBMODULES:
        mod = import_module(f".{name}", __name__)
        globals()[name] = mod
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(list(globals().keys()) + list(_SUBMODULES))


def load_config(path=None):
    """读取公共配置（默认 ``common/config.json``），返回 dict。

    Parameters
    ----------
    path : str or Path, optional
        自定义配置路径。

    Returns
    -------
    dict
        配置内容（只读使用，**不要**在代码中修改后写回）。
    """
    p = Path(path) if path is not None else CONFIG_PATH
    return json.loads(p.read_text(encoding="utf-8"))


def observers(path=None):
    """读取配置中的观测点列表（``[{id, name, lat_deg, lon_deg, alt_m}, ...]``）。"""
    return load_config(path).get("observers", [])
