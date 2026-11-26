# -*- coding: utf-8 -*-
"""路径与环境：开发态 / PyInstaller 冻结态统一解析。

设计原则
--------
* **只读资源**（引擎代码、`data/ne`、`viz/files`、内置 DB）→ `APP_ROOT` / `ENGINE_DIR` / `APP_DATA`
* **可写数据**（任务工作区、SRTM 瓦片缓存、matplotlib 字体缓存、设置）→ `runtime_base()` / `dem_dir()`
* 冻结后 exe 可能在 `C:\\Program Files\\...`（不可写），因此可写根不可写时自动回落到
  `%LOCALAPPDATA%\\RocketKit`。

环境变量（由 :func:`setup_env` 设置，供 engine 脚本使用）
    RK_ENGINE, RK_ROOT（每个任务单独）, RK_DEM, MPLBACKEND, MPLCONFIGDIR, PYTHONIOENCODING
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# --------------------------------------------------------------------------- #
# 基础目录
# --------------------------------------------------------------------------- #
FROZEN = bool(getattr(sys, "frozen", False))
#: 冻结态 = PyInstaller 解包目录（_MEIPASS）；开发态 = 项目根（含 app/ 与 engine/）
APP_ROOT = Path(getattr(sys, "_MEIPASS", "")) if FROZEN else Path(__file__).resolve().parents[2]
ENGINE_DIR = APP_ROOT / "engine"
APP_DATA = APP_ROOT / "app" / "data"
ASSETS = APP_ROOT / "app" / "assets"


def exe_dir() -> Path:
    """exe 所在目录（冻结态）；开发态返回项目根。"""
    if FROZEN:
        return Path(sys.executable).resolve().parent
    return APP_ROOT


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".wtest_", dir=str(path))
        os.close(fd)
        os.unlink(tmp)
        return True
    except Exception:  # noqa: BLE001
        return False


_LOCAL_APPDATA = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


def runtime_base() -> Path:
    """可写根目录：开发态=项目根；冻结态=exe 目录（不可写则 %LOCALAPPDATA%\\RocketKit）。"""
    if not FROZEN:
        base = APP_ROOT
    else:
        base = exe_dir()
        if not _writable(base):
            base = _LOCAL_APPDATA / "RocketKit"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        base = _LOCAL_APPDATA / "RocketKit"
        base.mkdir(parents=True, exist_ok=True)
    return base


_SETTINGS_PATH = None


def settings_path() -> Path:
    global _SETTINGS_PATH
    if _SETTINGS_PATH is None:
        _SETTINGS_PATH = runtime_base() / "settings.json"
    return _SETTINGS_PATH


DEFAULT_SETTINGS = {
    "work_root": "",          # 空 = runtime_base()/作品
    "dem_dir": "",            # 空 = 默认（见 dem_dir()）
    "jobs": 0,                # 0 = 自动
    "mc_n": 0,
    "last_workdir": "",
    "allow_network": True,    # 允许在"在线补全"时联网
    "keep_window_open": True,
    "coord_format": "dec",    # 经纬度输入/显示格式：dec=十进制度，dms=度分秒
}


def settings() -> dict:
    d = dict(DEFAULT_SETTINGS)
    try:
        p = settings_path()
        if p.exists():
            d.update(json.loads(p.read_text(encoding="utf-8-sig")))
    except Exception:  # noqa: BLE001
        pass
    return d


def save_settings(d: dict) -> None:
    try:
        merged = settings()
        merged.update(d)
        settings_path().write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def work_root() -> Path:
    s = settings()
    p = Path(s["work_root"]) if s.get("work_root") else runtime_base() / "作品"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        p = _LOCAL_APPDATA / "RocketKit" / "作品"
        p.mkdir(parents=True, exist_ok=True)
    return p


def dem_dir() -> Path:
    """SRTM 瓦片缓存根（含 gz/ hgt/ cache/ srtm_tiles.json）。"""
    s = settings()
    if s.get("dem_dir"):
        p = Path(s["dem_dir"])
    elif FROZEN:
        p = _LOCAL_APPDATA / "RocketKit" / "dem"
    else:
        p = ENGINE_DIR / "data" / "dem"
    for sub in ("", "gz", "hgt", "cache"):
        try:
            (p / sub).mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            break
    return p


def mpl_config_dir() -> Path:
    p = Path(tempfile.gettempdir()) / "rocketkit_mplconfig"
    p.mkdir(parents=True, exist_ok=True)
    return p


# --------------------------------------------------------------------------- #
# 环境与 sys.path
# --------------------------------------------------------------------------- #
_env_ready = False


def setup_env() -> None:
    """设置 engine 脚本所需环境变量。幂等，可重复调用。"""
    global _env_ready
    os.environ["RK_ENGINE"] = str(ENGINE_DIR)
    os.environ["RK_DEM"] = str(dem_dir())
    os.environ["RK_APP_DATA"] = str(APP_DATA)
    os.environ["RK_STATION_CACHE"] = str(runtime_base() / "cache" / "station")
    try:
        Path(os.environ["RK_STATION_CACHE"]).mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    os.environ.setdefault("MPLBACKEND", "Agg")
    os.environ["MPLCONFIGDIR"] = str(mpl_config_dir())
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ.setdefault("PYTHONUTF8", "1")
    if FROZEN:
        os.environ["RK_FROZEN"] = "1"
    _env_ready = True


def ensure_syspath() -> None:
    """把 engine 及其 scripts 目录加入 sys.path（幂等）。"""
    for p in (str(ENGINE_DIR), str(ENGINE_DIR / "scripts")):
        if p not in sys.path:
            sys.path.insert(0, p)


def engine_script(name: str) -> Path:
    return ENGINE_DIR / "scripts" / (name if name.endswith(".py") else name + ".py")


# --------------------------------------------------------------------------- #
# 杂项
# --------------------------------------------------------------------------- #
def coord_format() -> str:
    """经纬度输入/显示格式：``dec``（十进制度）或 ``dms``（度分秒）。"""
    v = str(settings().get("coord_format") or "dec").lower()
    return "dms" if v in ("dms", "度分秒", "dm", "deg-min-sec") else "dec"


def save_coord_format(fmt: str) -> None:
    save_settings({"coord_format": "dms" if str(fmt).lower() in (
        "dms", "度分秒", "dm", "deg-min-sec") else "dec"})


def find_google_earth() -> str:
    """找本机 Google Earth（Pro / 普通版）可执行文件；找不到返回空串。"""
    cands = [
        r"C:\Program Files\Google\Google Earth Pro\client\googleearth.exe",
        r"C:\Program Files (x86)\Google\Google Earth Pro\client\googleearth.exe",
        r"C:\Program Files\Google\Google Earth\client\googleearth.exe",
        r"C:\Program Files (x86)\Google\Google Earth\client\googleearth.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Google Earth Pro\client\googleearth.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Google Earth\client\googleearth.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Google Earth Pro\client\googleearth.exe"),
    ]
    for c in cands:
        try:
            if c and os.path.isfile(c):
                return c
        except OSError:
            continue
    return ""


_ERR_DLL_INIT_FAILED = (0xC0000142, -1073741502, 3221225794)


def _user_home() -> str:
    d = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    try:
        if d and os.path.isdir(d):
            return d
    except OSError:
        pass
    return os.environ.get("SystemRoot", r"C:\Windows")


def _system_exe(*names) -> str:
    """返回 %SystemRoot% 下的系统程序绝对路径（优先 System32）。"""
    root = os.environ.get("SystemRoot", r"C:\Windows")
    for name in names:
        for cand in (os.path.join(root, "System32", name), os.path.join(root, name)):
            try:
                if os.path.isfile(cand):
                    return cand
            except OSError:
                continue
    return ""


def _clean_env() -> dict:
    """给子进程的干净环境：去掉 PyInstaller / Python 相关变量与 _internal 的 PATH 项。

    冻结态下若把 ``_MEIPASS``（``_internal``）留在 PATH 里，被启动的系统程序（explorer 等）
    可能加载到包内自带的 DLL（如 ucrtbase）而初始化失败（0xC0000142）。
    """
    env = dict(os.environ)
    meipass = str(getattr(sys, "_MEIPASS", "") or "")
    for k in list(env):
        ku = k.upper()
        if ku.startswith("_PYI") or ku in ("_MEIPASS", "_MEIPASS2", "PYTHONHOME",
                                           "PYTHONPATH", "PYTHONSTARTUP", "__COMPAT_LAYER"):
            env.pop(k, None)
    if meipass:
        try:
            norm = os.path.normcase(os.path.abspath(meipass))
            kept = []
            for item in str(env.get("PATH", "")).split(os.pathsep):
                if not item:
                    continue
                try:
                    pn = os.path.normcase(os.path.abspath(item))
                except Exception:  # noqa: BLE001
                    pn = ""
                if pn and (pn == norm or pn.startswith(norm + os.sep)):
                    continue
                kept.append(item)
            env["PATH"] = os.pathsep.join(kept)
        except Exception:  # noqa: BLE001
            pass
    return env


def _shell_execute(path) -> int:
    """ShellExecuteW 打开文件/目录；返回 >32 表示成功（0/5/2 等为失败码）。"""
    if os.name != "nt":
        return 0
    try:
        import ctypes
        return int(ctypes.windll.shell32.ShellExecuteW(
            None, "open", str(path), None, _user_home(), 1))
    except Exception:  # noqa: BLE001
        return 0


def _spawn_checked(exe: str, args, wait: float = 0.9) -> bool:
    """显式启动外部程序；用干净环境 + 用户目录 + 尝试脱离沙箱 Job。

    * 子进程仍在运行（GUI 程序正常启动）→ 成功；
    * 很快退出且退出码为 0xC0000142（DLL 初始化失败）→ 该方式失败；
    * explorer 常以非 0 且非 0xC0000142 的码退出（把请求转交给已有 shell 实例），视为成功。
    """
    if not exe:
        return False
    env = _clean_env()
    cwd = _user_home()
    for flags in (0x01000000, 0):        # 先试 CREATE_BREAKAWAY_FROM_JOB，再普通启动
        try:
            proc = subprocess.Popen([str(exe)] + [str(a) for a in args],
                                    close_fds=False, cwd=cwd, env=env,
                                    creationflags=flags)
        except Exception:  # noqa: BLE001
            continue
        try:
            rc = proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            return True
        if rc < 0 or rc in _ERR_DLL_INIT_FAILED:
            continue
        return True
    return False


def _copy_clipboard(text: str) -> bool:
    """把文本复制到 Windows 剪贴板（不依赖 tkinter）。"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        u32 = ctypes.windll.user32
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k32.GlobalAlloc.restype = ctypes.c_void_p
        k32.GlobalLock.argtypes = [ctypes.c_void_p]
        k32.GlobalLock.restype = ctypes.c_void_p
        k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        u32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
        data = (str(text) + "\0").encode("utf-16-le")
        h = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False
        ptr = k32.GlobalLock(h)
        if not ptr:
            k32.GlobalFree(h)
            return False
        ctypes.memmove(ptr, data, len(data))
        k32.GlobalUnlock(h)
        if not u32.OpenClipboard(None):
            k32.GlobalFree(h)
            return False
        try:
            u32.EmptyClipboard()
            return bool(u32.SetClipboardData(CF_UNICODETEXT, h))
        finally:
            u32.CloseClipboard()
    except Exception:  # noqa: BLE001
        return False


def _fail_message(what: str, p: Path) -> str:
    copied = _copy_clipboard(str(p))
    tail = ("\n\n当前处于受限运行环境（沙箱/无桌面权限），系统不允许本程序启动资源管理器。\n"
            "路径已复制到剪贴板，可直接粘贴到「文件资源管理器」地址栏打开。"
            if copied else
            "\n\n当前处于受限运行环境（沙箱/无桌面权限），系统不允许本程序启动资源管理器。\n"
            "请手动复制下面的路径，在「文件资源管理器」地址栏打开。")
    return "无法打开%s：%s%s" % (what, p, tail)


def open_path(path) -> "tuple[bool, str]":
    """用系统程序打开文件/目录。返回 ``(是否成功, 给用户看的说明)``。

    策略（优先“真实桌面可用”的标准方式，再逐级降级）::

        1. ShellExecuteW（走系统 shell，等同资源管理器里双击；正常用户机首选）
        2. 显式启动系统程序（绝对路径 + 干净环境 + 用户目录 + 尝试脱离沙箱 Job）
        3. 失败 → 路径自动复制到剪贴板，并说明当前环境的限制
    """
    p = Path(path)
    try:
        p = p.expanduser().resolve()
    except Exception:  # noqa: BLE001
        pass
    if not p.exists():
        return False, "路径不存在：%s" % p
    explorer = _system_exe("explorer.exe")
    notepad = _system_exe("notepad.exe")

    ext = p.suffix.lower()
    # ShellExecute 返回 5 = SE_ERR_ACCESSDENIED：当前是受限运行环境（沙箱/无桌面）。
    # 这种情况下再显式 spawn 系统程序只会得到 0xC0000142（STATUS_DLL_INIT_FAILED），
    # 所以直接走“复制路径 + 说明”的降级，避免弹出系统错误框。
    _rc = _shell_execute(p)

    # ---- 目录：资源管理器 ----
    if p.is_dir():
        if _rc > 32:
            return True, "已在资源管理器中打开：%s" % p
        if _rc == 5:
            return False, _fail_message("目录", p)
        if _spawn_checked(explorer, [str(p)]):
            return True, "已在资源管理器中打开：%s" % p
        return False, _fail_message("目录", p)

    # ---- 三维成果：Google Earth 优先 ----
    if ext in (".kmz", ".kml"):
        ge = find_google_earth()
        if ge and _spawn_checked(ge, [str(p)]):
            return True, "已用 Google Earth 打开：%s" % p
        if _rc > 32:
            return True, "已用系统默认程序打开：%s" % p
        if _rc != 5 and _spawn_checked(explorer, ["/select,", str(p)]):
            return True, ("未检测到 Google Earth，已在资源管理器中定位该文件"
                          "（安装 Google Earth 后双击即可三维查看）：%s" % p)
        return False, _fail_message("三维成果", p)

    # ---- 文本类：记事本最稳（技术报告 .md / 日志 / CSV 预览）----
    if ext in (".md", ".txt", ".log", ".csv", ".json", ".xml", ".py", ".bat", ".ps1"):
        if _spawn_checked(notepad, [str(p)]):
            return True, "已用记事本打开：%s" % p
        if _rc > 32:
            return True, "已用系统默认程序打开：%s" % p
        if _rc != 5 and _spawn_checked(explorer, ["/select,", str(p)]):
            return True, "已在资源管理器中定位该文件：%s" % p
        return False, _fail_message("文件", p)

    # ---- 其它文件：系统关联（Word / 看图 / …）----
    if _rc > 32:
        return True, "已用系统默认程序打开：%s" % p
    if _rc != 5 and _spawn_checked(explorer, ["/select,", str(p)]):
        tip = "已在资源管理器中定位该文件"
        if ext == ".docx":
            tip += "（未检测到可打开 Word 文档的程序）"
        return True, tip + "：%s" % p
    return False, _fail_message("文件", p)


def open_in_explorer(path) -> None:
    """用系统文件管理器打开目录或文件（Windows）；失败时静默（兼容旧调用）。"""
    open_path(path)


def app_version() -> str:
    try:
        return (APP_ROOT / "app" / "VERSION").read_text(encoding="utf-8-sig").strip()
    except Exception:  # noqa: BLE001
        return "3.0.0"


def data_json(name: str) -> Path:
    return app_data_dir() / (name if name.endswith(".json") else name + ".json")


def app_data_dir() -> Path:
    """内置数据库目录；可用环境变量 RK_APP_DATA 覆盖（测试/便携部署用）。"""
    p = os.environ.get("RK_APP_DATA")
    return Path(p) if p else APP_DATA
