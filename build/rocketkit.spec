# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包规格：火箭弹道工具箱 v4（Windows / one-folder）。

设计要点（与 ``app/core/paths.py`` 的冻结态约定一一对应）
--------------------------------------------------------
* **app 代码**进 PYZ（``_internal`` 里看不到 app/*.py，但 ``__file__`` 仍是
  ``<_MEIPASS>/app/...``，``coarse_dem`` 等按 ``__file__`` 找资源照常可用）；
* **engine 代码与数据保持磁盘真实文件**（``_internal/engine/...``）：engine 脚本由
  ``runpy.run_path`` 直接按路径执行，且 ``common.plotstyle`` / ``notam`` 等模块按
  ``Path(__file__)`` 定位资源，放进 PYZ 会改变它们的解析结果；
* **只读资源**随包：内置库 ``app/data``（含粗 DEM npz 与空间站 OEM）、图标
  ``app/assets``、Natural Earth 底图 ``engine/data/ne``、KML 图标
  ``engine/viz/files``、模板 ``engine/templates``、v4 用户文档 ``文档/``；
* **不含**：SRTM 瓦片缓存 ``engine/data/dem/{gz,hgt,cache,custom}``（GB 级、运行期
  可写、冻结态默认写到 ``%LOCALAPPDATA%\\RocketKit\\dem``）、``tests/``、``作品/``
  示例成果、``user_data/`` 用户库（首次运行自动生成）。

engine 脚本不在静态分析图里（runpy 动态执行），所以它们用到的第三方库必须在
``hiddenimports`` 里显式列出，否则会在运行时才 ``ModuleNotFoundError``。
"""
import os
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent          # build/ 的上一级 = 项目根
NAME = "火箭弹道工具箱"
VERSION = (ROOT / "app" / "VERSION").read_text(encoding="utf-8-sig").strip()

datas = []


def add_tree(rel_src, dest):
    """把目录内容递归加入 datas（跳过 __pycache__ / 字节码）。

    注意 PyInstaller 的 datas 语义：``(源, 目标)`` 里的「目标」是**目录**，文件会以
    自己的文件名放进去。所以这里逐文件加入时目标要取 ``dest/相对父目录``，否则会
    变成 ``app/data/rockets.json/rockets.json`` 这种多套一层目录的错误结构。
    """
    src = ROOT / rel_src
    if not src.is_dir():
        raise SystemExit("[spec] 缺少资源目录：%s" % src)
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for fn in sorted(filenames):
            if fn.endswith((".pyc", ".pyo")):
                continue
            p = Path(dirpath) / fn
            rel = p.relative_to(src)
            parent = str(rel.parent)
            target = dest if parent == "." else str(Path(dest) / rel.parent)
            datas.append((str(p), target))


def add_file(rel_src, dest):
    """加入单个文件；``dest`` 是目标目录（文件名保持不变）。"""
    src = ROOT / rel_src
    if not src.is_file():
        raise SystemExit("[spec] 缺少资源文件：%s" % src)
    datas.append((str(src), dest))


# --------------------------------------------------------------------------- #
# 随包资源（对应 paths.APP_ROOT / APP_DATA / ENGINE_DIR）
# --------------------------------------------------------------------------- #
# 应用层：内置数据库（28 箭型 / 28 工位 / 343 城市 / 载荷 / 轨道 / 空间站 OEM / 粗 DEM）
add_tree("app/data", "app/data")
add_tree("app/assets", "app/assets")
add_file("app/VERSION", "app")

# 引擎：脚本、公共库、Natural Earth 底图、KML 图标、模板（runpy 需要真实 .py 文件）
add_tree("engine/common", "engine/common")
add_tree("engine/scripts", "engine/scripts")
add_tree("engine/data/ne", "engine/data/ne")
add_tree("engine/viz/files", "engine/viz/files")
add_tree("engine/templates", "engine/templates")
# DEM 目录只带"说明/模式"两个小文件，瓦片缓存不进包（冻结态写到 %LOCALAPPDATA%）
add_file("engine/data/dem/dem_mode.json", "engine/data/dem")
add_file("engine/data/dem/README.md", "engine/data/dem")

# 用户文档（GUI「帮助 → 打开文档目录」指向 <APP_ROOT>/文档）
add_tree("文档", "文档")

# --------------------------------------------------------------------------- #
# 隐藏依赖：engine/scripts/*.py 由 runpy 动态执行，静态分析看不到它们的 import
# --------------------------------------------------------------------------- #
hiddenimports = [
    # 数值 / 科学计算
    "numpy",
    "scipy",
    "scipy.optimize",            # invert.py: differential_evolution / minimize
    "scipy.interpolate",         # plot_sky.py: PchipInterpolator
    "scipy.linalg",
    # 绘图（engine 出图）
    "matplotlib",
    "matplotlib.pyplot",
    "matplotlib.figure",
    "matplotlib.cm",
    "matplotlib.collections",
    "matplotlib.colors",
    "matplotlib.dates",
    "matplotlib.font_manager",
    "matplotlib.lines",
    "matplotlib.patches",
    "matplotlib.patheffects",
    "matplotlib.ticker",
    "matplotlib.backends.backend_agg",
    "matplotlib.backends.backend_tkagg",
    # 数据表 / 图片 / Word / 轨道
    "pandas",
    "PIL",
    "PIL.Image",
    "docx",
    "sgp4",
    "sgp4.api",
    # GUI
    "tkinter",
    "tkinter.ttk",
    "tkinter.filedialog",
    "tkinter.simpledialog",
    "tkinter.messagebox",
    "tkinter.font",
]

# 明显用不到的大件，避免把环境里碰巧装着的包拖进来
#（这些包只被 pandas / matplotlib 的「可选后端」探测到；程序只用 numpy/scipy/
#  matplotlib/pandas.read_csv/Pillow/python-docx/sgp4，排除后它们会按可选依赖
#  正常降级，不会报错。整套依赖从 340 MB 降到 ~230 MB。）
excludes = [
    "IPython", "ipykernel", "jupyter", "notebook", "nbconvert", "nbformat",
    "pytest", "_pytest", "sphinx", "docutils", "pygments", "traitlets",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "geopandas", "shapely", "fiona", "pyproj", "rasterio", "rtree",
    "numba", "llvmlite", "torch", "tensorflow", "cv2", "imageio",
    "SQLAlchemy", "psycopg2", "sqlmodel", "openpyxl", "xlsxwriter",
    "shapefile",
    # pandas / matplotlib 的可选集成（CSV 读取用不到）
    "dask", "distributed", "xarray", "tables", "h5py", "pyarrow",
    "numexpr", "bottleneck", "sklearn", "statsmodels",
    "zmq", "Cython",
    # pywin32 / 网络栈（程序用标准库 urllib + ctypes，不需要这些）
    "win32com", "pythoncom", "pywintypes", "Pythonwin", "win32api", "win32con",
    "cryptography", "requests", "urllib3", "certifi",
]

a = Analysis(
    [str(ROOT / "build" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "build" / "rthook_utf8.py")],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,                       # CLI（--env / --cli）与运行日志都在控制台可见
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "app" / "assets" / "app.ico"),
    version=str(ROOT / "build" / "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=NAME,
)
