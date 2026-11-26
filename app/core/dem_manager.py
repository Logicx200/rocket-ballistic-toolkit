# -*- coding: utf-8 -*-
"""dem_manager.py — DEM 瓦片管家：需求计划 / 历史导入 / 在线补全 / 粗网格回退 / 自定义源。

职责
----
1. :meth:`DemManager.required_tiles` —— 按任务配置算需要哪些 SRTM 1° 瓦片
   （观测点 200 km 圆 + 发射场 + 航警区多边形 + 落点），**直接复用**
   ``engine/scripts/fetch_dem.py::plan_tiles`` 的计划逻辑，不再重写一遍。
2. :meth:`DemManager.scan_and_import` —— 扫描历史工作区里已有的 ``data/dem/gz|hgt``
   瓦片，导入到本机 ``dem_dir``（同名跳过；有 hgt 优先硬链接/复制，否则复制 gz 再解压）。
3. :meth:`DemManager.download` —— AWS skadi 单瓦片下载（gz），带进度、重试、可取消；
   离线时先用 2 s 探测，快速失败返回失败列表，不卡几十秒超时。
4. :meth:`DemManager.prepare` —— 一步到位：自定义源判断 → 扫描导入 → 补缺 → 在线下载
   → 建观测点 3″ 缓存 → 返回 ``{"mode": "custom|srtm|coarse|none", ...}``。
5. :meth:`DemManager.observer_alt` / :meth:`dem_mode` / :meth:`status` —— 给 GUI/体检表
   用的高程、模式与数据源查询。
6. v3.1 自定义高程源（离线自主导入）：:meth:`custom_sources` / :meth:`custom_plan` /
   :meth:`import_dem` / :meth:`remove_custom_source` 转发给 ``app/core/dem_import.py``；
   自定义源覆盖全部关键点时 ``prepare`` 直接进入 ``custom`` 模式、跳过下载与缓存构建。

路径约定（见 app/INTERFACES.md）
------------------------------
* ``dem_dir`` 由调用方给出：部署态 ``paths.dem_dir()``，开发态 ``engine/data/dem``；
* 目录结构 ``<dem_dir>/gz/<TILE>.hgt.gz``、``<dem_dir>/hgt/<TILE>.hgt``、
  ``<dem_dir>/cache/<obs_id>.npz``、``<dem_dir>/srtm_tiles.json``（与 v2 引擎一致）、
  v3.1 新增 ``<dem_dir>/custom/index.json``（自定义 GeoTIFF 登记表）；
* 本模块不写引擎代码/配置，只写 ``dem_dir``；引擎脚本通过环境变量
  ``RK_DEM`` / ``RK_COARSE_DEM`` 复用同一份数据。

已知限制
--------
* 无瓦片时天空/遮挡自动回退内嵌 0.02° 粗 DEM（约 2 km），首次出现时刻精度约 ±数十秒；
* 城市高楼、近场（<1 km）遮挡在粗 DEM 下不可用；
* 历史扫描有**时间预算**（默认 12 s）与深度限制，不会扫全盘；
* 自定义 GeoTIFF 只接受 WGS84 经纬度、单幅第一 IFD（无 overview），
  地形遮挡要求源完整覆盖观测点 200 km 圆，否则仍回退粗 DEM（见文档 06）。
"""
from __future__ import annotations

import csv
import gzip
import importlib
import json
import math
import os
import shutil
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import numpy as np

__all__ = ["DemManager", "HGT_BYTES", "TILE_BYTES"]

# --------------------------------------------------------------------------- #
# 常量（与 engine/common/dem.py、engine/scripts/fetch_dem.py 一致）
# --------------------------------------------------------------------------- #
HGT_BYTES = 3601 * 3601 * 2                  # 25 934 402
TILE_BYTES = HGT_BYTES

#: 历史工作区（Lead 指明的已知瓦片来源；不存在会自动跳过）
KNOWN_HISTORICAL_WORKSPACES = [
    r"F:\DeepSeek工作区\弹道仿真工作区\2026.11.25 CZ2-F",
    r"F:\DeepSeek工作区\弹道仿真工作区\2026.11.27 CZ6-A",
]
#: 历史工程目录通配（限父目录一层 + 深度 2，不扫全盘）
HISTORICAL_GLOBS = [
    ("F:/DeepSeek工作区", "*_v2*"),
    ("F:/DeepSeek工作区", "*v3*"),
]

S3_BASE = "https://s3.amazonaws.com/elevation-tiles-prod/skadi"
S3_PROBE_URL = S3_BASE + "/N35/N35E110.hgt.gz"

#: 粗 DEM 文件名（与 app/core/coarse_dem.py、engine/common/dem.py 一致）
COARSE_DEM_NAME = "china_dem_0p02.npz"


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _hgt_ok(path: Path) -> bool:
    """本地 hgt 是否完整（字节数必须精确 25 934 402）。"""
    try:
        return path.is_file() and path.stat().st_size == HGT_BYTES
    except OSError:
        return False


def _gzip_hgt_size(path: Path) -> int:
    """完整解压 gzip 并返回字节数（校验完整性）；失败返回 -1。"""
    try:
        n = 0
        with gzip.open(path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 22)
                if not chunk:
                    break
                n += len(chunk)
        return n
    except Exception:                        # noqa: BLE001 - 截断/CRC 都算不完整
        return -1


def _tile_latlon_sw(name: str):
    """'N35E110' → (35, 110)。"""
    lat_i = int(name[1:3]) * (1 if name[0] == "N" else -1)
    lon_i = int(name[5:8]) * (1 if name[4] == "E" else -1)
    return lat_i, lon_i


def _tile_name(lat_i: int, lon_i: int) -> str:
    ns = "N" if lat_i >= 0 else "S"
    ew = "E" if lon_i >= 0 else "W"
    return f"{ns}{abs(lat_i):02d}{ew}{abs(lon_i):03d}"


def _coarse_dem_candidates() -> list[Path]:
    """粗 DEM 可能的位置（app/data 优先，engine/data 兼容旧布局）。"""
    here = Path(__file__).resolve()
    app_root = here.parents[1]                                # .../app
    proj_root = here.parents[2]                               # 开发态项目根
    out = [app_root / "data" / COARSE_DEM_NAME,
           proj_root / "app" / "data" / COARSE_DEM_NAME,
           proj_root / "engine" / "data" / COARSE_DEM_NAME]
    env = os.environ.get("RK_COARSE_DEM")
    if env:
        out.insert(0, Path(env))
    return out


def _import_dem_import():
    """懒加载 :mod:`app.core.dem_import`（避免与 engine 路径耦合）。"""
    try:
        from . import dem_import                                # app.core.dem_import
        return dem_import
    except Exception:                                           # noqa: BLE001 - 直接脚本运行
        here = Path(__file__).resolve()
        proj_root = here.parents[2]
        if str(proj_root) not in sys.path:
            sys.path.insert(0, str(proj_root))
        from app.core import dem_import as _di                   # type: ignore
        return _di


# --------------------------------------------------------------------------- #
# DemManager
# --------------------------------------------------------------------------- #
class DemManager:
    """SRTM 瓦片扫描/导入/下载/缓存/回退的一站式管家。

    Parameters
    ----------
    dem_dir : str or Path
        SRTM 瓦片缓存根（可写）。部署态传 ``paths.dem_dir()``；
        开发态传 ``engine/data/dem``。
    logger : callable or logging.Logger, optional
        日志回调：``logger("消息")`` 或标准 logger。缺省打印到 stdout。
    scan_roots : list[str], optional
        额外历史工作区根（每个应含 ``data/dem/gz|hgt``）。
    """

    def __init__(self, dem_dir, logger=None, scan_roots=None):
        self.dem_dir = Path(dem_dir)
        self.dir_gz = self.dem_dir / "gz"
        self.dir_hgt = self.dem_dir / "hgt"
        self.dir_cache = self.dem_dir / "cache"
        for d in (self.dem_dir, self.dir_gz, self.dir_hgt, self.dir_cache):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        self.logger = logger
        self.extra_scan_roots = [Path(p) for p in (scan_roots or [])]
        self.last_plan_notes: dict = {}
        self._last_prepare: dict = {}
        self._fetch = None                      # 懒加载的 fetch_dem 模块
        self._custom_mgr = None                 # 懒加载的 DemImportManager
        self._key_points_last: list = []        # 最近一次任务的关键点（status 算覆盖率用）

    # ------------------------------------------------------------------ #
    # 日志 / 环境
    # ------------------------------------------------------------------ #
    def _log(self, msg: str) -> None:
        line = f"[dem] {msg}"
        if self.logger is None:
            print(line, flush=True)
            return
        try:
            if callable(self.logger) and not hasattr(self.logger, "info"):
                self.logger(line)
            else:
                self.logger.info(line)         # logging.Logger
        except Exception:                       # noqa: BLE001 - 日志失败不影响主流程
            print(line, flush=True)

    def _sync_env(self) -> None:
        """让引擎侧（RK_DEM/RK_COARSE_DEM）指向同一份数据。"""
        os.environ["RK_DEM"] = str(self.dem_dir)
        app_root = Path(__file__).resolve().parents[1]
        engine = Path(os.environ.get("RK_ENGINE") or (app_root.parent / "engine"))
        if engine.is_dir():
            os.environ.setdefault("RK_ENGINE", str(engine))
        if not os.environ.get("RK_COARSE_DEM"):
            for p in _coarse_dem_candidates():
                if p.is_file():
                    os.environ["RK_COARSE_DEM"] = str(p)
                    break

    # ------------------------------------------------------------------ #
    # 引擎模块（common.dem / fetch_dem）懒加载
    # ------------------------------------------------------------------ #
    def _ensure_syspath(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        engine = Path(os.environ.get("RK_ENGINE") or (app_root.parent / "engine"))
        for p in (str(engine), str(engine / "scripts")):
            if p not in sys.path:
                sys.path.insert(0, p)

    def _fetch_dem(self):
        """懒加载 ``engine/scripts/fetch_dem.py``（复用其瓦片计划逻辑）。"""
        if self._fetch is None:
            self._ensure_syspath()
            self._fetch = importlib.import_module("fetch_dem")
        return self._fetch

    def _engine_dem(self):
        """懒加载 ``engine/common/dem.py``，并把其全局目录指到本 dem_dir。"""
        self._sync_env()
        self._ensure_syspath()
        dem = importlib.import_module("common.dem")
        if str(getattr(dem, "DIR_HGT", "")) != str(self.dir_hgt):
            dem.DATA_DEM = self.dem_dir
            dem.DIR_GZ = self.dir_gz
            dem.DIR_HGT = self.dir_hgt
            dem.DIR_CACHE = self.dir_cache
            dem.MANIFEST = self.dem_dir / "srtm_tiles.json"
            dem.OBSERVER_INFO = self.dem_dir / "observer_dem.json"
            try:
                dem.CUSTOM_INDEX = self.dem_dir / "custom" / "index.json"
            except Exception:               # noqa: BLE001
                pass
            try:
                dem._TILE_CACHE.clear()
                dem._OBS_CACHE.clear()
                dem._reset_custom_cache()
            except Exception:               # noqa: BLE001
                pass
        return dem

    # ------------------------------------------------------------------ #
    # v3.1 自定义高程源（离线自主导入的 GeoTIFF）
    # ------------------------------------------------------------------ #
    def _custom_manager(self):
        """懒加载 :class:`app.core.dem_import.DemImportManager`（同一 dem_dir）。"""
        if self._custom_mgr is None:
            mod = _import_dem_import()
            self._custom_mgr = mod.DemImportManager(self.dem_dir, logger=self.logger)
        return self._custom_mgr

    def custom_sources(self, valid_only: bool = True) -> list[dict]:
        """已登记的自定义高程源列表（GeoTIFF；打不开的默认剔除）。"""
        try:
            return self._custom_manager().list_sources(valid_only=valid_only)
        except Exception as exc:                # noqa: BLE001 - 自定义源坏了不影响主流程
            self._log(f"读取自定义高程源失败（忽略）：{exc}")
            return []

    def key_points(self, config) -> list[tuple]:
        """本任务的关键点 ``[(名称, lat, lon), ...]``：发射场、观测点、落点。"""
        pts: list[tuple] = []
        cfg = config or {}
        site = ((cfg.get("mission") or {}).get("launch_site")
                or cfg.get("launch_site") or {})
        try:
            if site.get("lat_deg") is not None and site.get("lon_deg") is not None:
                pts.append((str(site.get("name") or "发射场"),
                            float(site["lat_deg"]), float(site["lon_deg"])))
        except (TypeError, ValueError):
            pass
        for obs in (cfg.get("observers") or []):
            try:
                pts.append((str(obs.get("id") or obs.get("name") or "观测点"),
                            float(obs["lat_deg"]), float(obs["lon_deg"])))
            except (KeyError, TypeError, ValueError):
                continue
        for lat, lon, label in self._impact_points(cfg):
            try:
                pts.append((str(label or "落点"), float(lat), float(lon)))
            except (TypeError, ValueError):
                continue
        return pts

    def custom_plan(self, config) -> dict:
        """自定义高程源对本任务的覆盖情况。

        Returns
        -------
        dict
            ``{"sources": [id...], "coverage": {...}, "key_points": [...],
            "ready": bool, "radius_ready": bool, "radius_sources": [id...]}``

            * ``ready``：**所有关键点**都被自定义源覆盖（且采样到有效高程）→
              可以跳过下载与 3″ 缓存构建；
            * ``radius_ready``：观测点/发射场的 ``horizon_radius_km`` 圆包围盒被
              某个源**完整**覆盖 → 地形遮挡角也会走自定义源。
        """
        out = {"sources": [], "coverage": None, "key_points": [],
               "ready": False, "radius_ready": False, "radius_sources": []}
        sources = self.custom_sources()
        if not sources:
            return out
        out["sources"] = [str(s.get("id")) for s in sources]
        pts = self.key_points(config)
        out["key_points"] = pts
        self._key_points_last = pts
        if not pts:
            return out
        try:
            cov = self._custom_manager().coverage(pts)
        except Exception as exc:                # noqa: BLE001
            self._log(f"自定义高程源覆盖率统计失败（忽略）：{exc}")
            return out
        out["coverage"] = cov
        n_total = int(cov.get("total", {}).get("total") or 0)
        n_cov = int(cov.get("total", {}).get("covered") or 0)
        ready = bool(n_total and n_cov == n_total)
        if ready and cov.get("sampled"):
            # 关键点都真的采到高程（不是落进 nodata 区）
            ready = all(p.get("value_m") is not None for p in cov.get("points", []))
        out["ready"] = ready

        radius = float((config or {}).get("dem", {}).get("horizon_radius_km", 200.0))
        try:
            dem = self._engine_dem()
            radius_ids = []
            radius_ok = True
            for name, lat, lon in pts:
                cov = dem.custom_covers_radius(lat, lon, radius)
                if not cov["covered"]:
                    radius_ok = False
                    break
                radius_ids.append(str(cov["source"]))
            out["radius_ready"] = radius_ok
            out["radius_sources"] = sorted(set(radius_ids))
        except Exception as exc:                # noqa: BLE001
            self._log(f"自定义高程源半径覆盖判断失败（忽略）：{exc}")
        return out

    def import_dem(self, path, copy: bool = False, name: str = None,
                   progress=None) -> dict:
        """导入用户高程文件（GUI 调用入口，转发给 :class:`DemImportManager`）。

        Returns
        -------
        dict
            ``{"ok": n, "failed": [...], "sources": [...], "tiles": [...]}``
        """
        self._sync_env()
        info = self._custom_manager().import_path(path, copy=copy, name=name,
                                                  progress=progress)
        try:
            dem = self._engine_dem()
            dem._reset_custom_cache()
        except Exception:                       # noqa: BLE001
            pass
        return info

    def remove_custom_source(self, source_id: str) -> bool:
        """删除一条自定义高程源登记（转发给 :class:`DemImportManager`）。

        复制模式的源会同时删除 ``custom/data/`` 下的副本；删除前先让引擎侧释放
        文件句柄（Windows 上打开的文件不能删）。
        """
        dem = None
        try:
            dem = self._engine_dem()
            dem._reset_custom_cache()
        except Exception:                       # noqa: BLE001
            pass
        ok = self._custom_manager().remove_source(source_id)
        try:
            if dem is not None:
                dem._reset_custom_cache()
        except Exception:                       # noqa: BLE001
            pass
        return ok

    # ------------------------------------------------------------------ #
    # 瓦片计划
    # ------------------------------------------------------------------ #
    def _write_plan_config(self, config: dict) -> str:
        """把配置写成临时 config.json（fetch_dem.plan_tiles 只吃文件路径）。"""
        fd, path = tempfile.mkstemp(prefix="rocketkit_plan_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config or {}, fh, ensure_ascii=False)
        return path

    def _workdir(self, config: dict | None = None) -> Path | None:
        """当前任务工作区（找 sim/落点与射程.csv 用）。"""
        candidates = []
        if config:
            for key in ("_workdir", "workdir"):
                if config.get(key):
                    candidates.append(Path(str(config[key])))
            mission = config.get("mission") or {}
            for key in ("_workdir", "workdir"):
                if mission.get(key):
                    candidates.append(Path(str(mission[key])))
        if os.environ.get("RK_ROOT"):
            candidates.append(Path(os.environ["RK_ROOT"]))
        for p in candidates:
            try:
                if p.is_dir():
                    return p
            except OSError:
                continue
        return None

    def _impact_points(self, config: dict) -> list[tuple]:
        """读 ``<workdir>/sim/落点与射程.csv`` → [(lat, lon, label)]。"""
        wd = self._workdir(config)
        if wd is None:
            return []
        path = wd / "sim" / "落点与射程.csv"
        if not path.is_file():
            return []
        out = []
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    lat = row.get("lat_deg") or row.get("lat")
                    lon = row.get("lon_deg") or row.get("lon")
                    if lat is None or lon is None:
                        continue
                    try:
                        out.append((float(lat), float(lon),
                                    str(row.get("object") or row.get("name") or "")))
                    except (TypeError, ValueError):
                        continue
        except Exception as exc:            # noqa: BLE001 - 落点表坏了不影响计划
            self._log(f"读取落点表失败（忽略）：{exc}")
        return out

    def required_tiles(self, config) -> list[str]:
        """算本次任务需要的 1° SRTM 瓦片名（升序）。

        组成 = fetch_dem.plan_tiles 的（观测点 200 km 圆 + 发射场 + 航警区 + task_region）
        ∪ 落点 10 km 包围盒（``sim/落点与射程.csv`` 存在时）。
        """
        fetch = self._fetch_dem()
        cfg_path = None
        try:
            cfg_path = self._write_plan_config(config)
            names, notes = fetch.plan_tiles(None, config_path=cfg_path)
        except Exception as exc:            # noqa: BLE001 - 配置不完整时退回手工计划
            self._log(f"复用 fetch_dem.plan_tiles 失败（{exc}），改用手工计划")
            names, notes = self._plan_tiles_manual(config)
        finally:
            if cfg_path:
                try:
                    os.unlink(cfg_path)
                except OSError:
                    pass
        tiles = set(names or [])
        # 落点（一级/整流罩落区）局部 10 km 包围盒
        radius = float((config or {}).get("dem", {}).get("horizon_radius_km", 200.0))
        for lat, lon, label in self._impact_points(config):
            dlat = 10.0 / 111.19
            dlon = 10.0 / (111.19 * max(0.05, math.cos(math.radians(lat))))
            for i in range(math.floor(lat - dlat), math.floor(lat + dlat) + 1):
                for j in range(math.floor(lon - dlon), math.floor(lon + dlon) + 1):
                    tiles.add(_tile_name(i, j))
            notes.setdefault("impact_boxes", {})[label or f"{lat:.2f},{lon:.2f}"] = [
                round(lat - dlat, 4), round(lat + dlat, 4),
                round(lon - dlon, 4), round(lon + dlon, 4)]
        notes["coarse_dem_name"] = COARSE_DEM_NAME
        notes["radius_km"] = radius
        self.last_plan_notes = notes
        return sorted(tiles)

    def _plan_tiles_manual(self, config: dict):
        """fetch_dem 不可用时的最小计划（观测点 + 发射场，200 km 圆）。"""
        import numpy as np                     # 只用于多边形包围盒
        cfg = config or {}
        radius = float((cfg.get("dem") or {}).get("horizon_radius_km", 200.0))
        tiles: set[str] = set()
        notes: dict = {}
        pts = [(o.get("id", "obs"), o.get("lat_deg"), o.get("lon_deg"))
               for o in (cfg.get("observers") or [])]
        site = ((cfg.get("mission") or {}).get("launch_site")
                or cfg.get("launch_site") or {})
        if site.get("lat_deg") is not None:
            pts.append(("launch_site", site.get("lat_deg"), site.get("lon_deg")))
        earth_r = 6371.0088
        boxes = {}
        for pid, la, lo in pts:
            if la is None or lo is None:
                continue
            dlat = math.degrees(radius / earth_r)
            dlon = math.degrees(radius / (earth_r * max(0.02, math.cos(math.radians(la)))))
            boxes[pid] = [round(la - dlat, 4), round(la + dlat, 4),
                          round(lo - dlon, 4), round(lo + dlon, 4)]
            for i in range(math.floor(la - dlat), math.floor(la + dlat) + 1):
                for j in range(math.floor(lo - dlon), math.floor(lo + dlon) + 1):
                    tiles.add(_tile_name(i, j))
        notes["circle_boxes"] = boxes
        notes["circle_radius_km"] = radius
        for z in (cfg.get("notams") or []):
            poly = np.asarray(z.get("polygon_latlon") or [], dtype=float)
            if poly.size < 6:
                continue
            la0, lo0 = poly.min(axis=0)
            la1, lo1 = poly.max(axis=0)
            dlat = 10.0 / 111.19
            dlon = 10.0 / (111.19 * max(0.05, math.cos(math.radians((la0 + la1) / 2))))
            for i in range(math.floor(la0 - dlat), math.floor(la1 + dlat) + 1):
                for j in range(math.floor(lo0 - dlon), math.floor(lo1 + dlon) + 1):
                    tiles.add(_tile_name(i, j))
        notes["notam_boxes"] = {}
        return sorted(tiles), notes

    def present_tiles(self) -> list[str]:
        """本地可用的（已解压 hgt）瓦片名。"""
        if not self.dir_hgt.is_dir():
            return []
        return sorted(p.stem for p in self.dir_hgt.glob("*.hgt") if _hgt_ok(p))

    def missing_tiles(self, config) -> list[str]:
        """需要的瓦片里本地还没有（hgt 完整）的那些。"""
        present = set(self.present_tiles())
        return [t for t in self.required_tiles(config) if t not in present]

    def gz_tiles(self) -> list[str]:
        """本地已有压缩包（可解压）的瓦片名。"""
        if not self.dir_gz.is_dir():
            return []
        return sorted(p.name[:-len(".hgt.gz")] for p in self.dir_gz.glob("*.hgt.gz"))

    # ------------------------------------------------------------------ #
    # 历史工作区导入
    # ------------------------------------------------------------------ #
    def default_scan_roots(self) -> list[Path]:
        """默认扫描源：已知历史工作区 + 历史工程通配 + 可配置环境变量。"""
        roots: list[Path] = []
        env = os.environ.get("RK_DEM_SCAN") or ""
        for item in env.split(os.pathsep):
            if item.strip():
                roots.append(Path(item.strip()))
        roots.extend(Path(p) for p in KNOWN_HISTORICAL_WORKSPACES)
        roots.extend(self.extra_scan_roots)
        seen = {str(self.dem_dir.resolve()).lower()}
        out: list[Path] = []
        for r in roots:
            try:
                key = str(r.resolve()).lower()
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            if r.is_dir():
                out.append(r)
        # 通配历史工程（只展开一层，深度限制在 _iter_dem_dirs）
        for base, pattern in HISTORICAL_GLOBS:
            try:
                b = Path(base)
                if not b.is_dir():
                    continue
                for p in sorted(b.glob(pattern))[:40]:
                    if p.is_dir():
                        key = str(p.resolve()).lower()
                        if key not in seen:
                            seen.add(key)
                            out.append(p)
            except OSError:
                continue
        return out

    def _iter_dem_dirs(self, root: Path, deadline: float, max_depth: int = 2):
        """在 root 下找含 gz/ hgt/ 的 data/dem 目录（限深度、限时间）。"""
        skip = {"cache", "__pycache__", ".git", "node_modules", ".venv", "venv",
                "dist", "build", "_pytest_cache"}
        stack = [(root, 0)]
        while stack:
            if time.time() > deadline:
                return
            cur, depth = stack.pop()
            if (cur / "gz").is_dir() or (cur / "hgt").is_dir():
                yield cur
            if depth >= max_depth:
                continue
            try:
                entries = list(os.scandir(cur))
            except OSError:
                continue
            for ent in entries:
                try:
                    if not ent.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if ent.name.lower() in skip:
                    continue
                stack.append((Path(ent.path), depth + 1))

    def scan_and_import(self, roots=None, seconds: float = 12.0) -> int:
        """扫描历史工作区的 ``data/dem/{gz,hgt}``，导入到本 dem_dir（同名跳过）。

        Parameters
        ----------
        roots : list[str], optional
            历史工作区根列表；缺省用 :meth:`default_scan_roots`。
        seconds : float
            扫描时间预算 [s]（防止误扫全盘/网络盘长时间阻塞）。

        Returns
        -------
        int
            新导入的瓦片数（按文件名去重计）。
        """
        t0 = time.time()
        deadline = t0 + max(2.0, float(seconds))
        roots = [Path(p) for p in (roots if roots is not None
                                   else self.default_scan_roots())]
        if not roots:
            self._log("没有可扫描的历史工作区（跳过导入）")
            return 0
        self._log(f"扫描历史 DEM 瓦片：{len(roots)} 个源头，预算 {seconds:.0f} s")
        present = set(self.present_tiles())
        gz_have = set(self.gz_tiles())
        imported = 0
        n_src = 0
        for root in roots:
            if time.time() > deadline:
                self._log(f"扫描超时（>{seconds:.0f} s），已扫描 {n_src} 个 DEM 目录")
                break
            for dem_src in self._iter_dem_dirs(root, deadline):
                n_src += 1
                src_hgt = dem_src / "hgt"
                src_gz = dem_src / "gz"
                names: set[str] = set()
                if src_hgt.is_dir():
                    names.update(p.stem for p in src_hgt.glob("*.hgt"))
                if src_gz.is_dir():
                    names.update(p.name[:-len(".hgt.gz")]
                                 for p in src_gz.glob("*.hgt.gz"))
                for name in sorted(names):
                    if time.time() > deadline:
                        break
                    if name in present:
                        continue
                    src_h = src_hgt / f"{name}.hgt"
                    src_z = src_gz / f"{name}.hgt.gz"
                    if _hgt_ok(src_h):
                        if self._import_file(src_h, self.dir_hgt / src_h.name):
                            present.add(name)
                            imported += 1
                    elif src_z.is_file() and name not in gz_have:
                        # 只有压缩包：导入 gz，稍后统一解压（见 _extract_all）
                        if self._import_file(src_z, self.dir_gz / src_z.name):
                            gz_have.add(name)
                            imported += 1
                if imported and imported % 10 == 0:
                    self._log(f"  导入进度 {imported} 片（{time.time() - t0:.1f} s）")
        self._log(f"历史导入完成：新导入 {imported} 片，扫描 {n_src} 个 DEM 目录，"
                  f"耗时 {time.time() - t0:.1f} s")
        return imported

    def _import_file(self, src: Path, dst: Path) -> bool:
        """把瓦片文件放到 dst（优先硬链接，跨盘/占位文件回退复制）。"""
        if dst.exists():
            return False
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        try:
            os.link(str(src), str(dst))        # 同盘硬链接：瞬时且不占额外空间
            return True
        except (OSError, AttributeError):
            pass
        try:
            shutil.copy2(str(src), str(dst))
            return True
        except OSError as exc:
            self._log(f"  复制失败 {src.name}: {exc}")
            return False

    # ------------------------------------------------------------------ #
    # 解压
    # ------------------------------------------------------------------ #
    def _extract_one(self, name: str) -> str:
        """解压 ``gz/<TILE>.hgt.gz`` → ``hgt/<TILE>.hgt``（幂等、校验长度）。"""
        gz_path = self.dir_gz / f"{name}.hgt.gz"
        hgt_path = self.dir_hgt / f"{name}.hgt"
        if _hgt_ok(hgt_path):
            return "existing"
        if not gz_path.is_file():
            return "missing_gz"
        tmp = hgt_path.with_suffix(".hgt.part")
        n = 0
        try:
            with gzip.open(gz_path, "rb") as fin, open(tmp, "wb") as fout:
                while True:
                    chunk = fin.read(1 << 22)
                    if not chunk:
                        break
                    fout.write(chunk)
                    n += len(chunk)
        except Exception as exc:            # noqa: BLE001 - 截断/CRC
            tmp.unlink(missing_ok=True)
            return f"bad_gz({type(exc).__name__})"
        if n != HGT_BYTES:
            tmp.unlink(missing_ok=True)
            return f"bad_size({n})"
        tmp.replace(hgt_path)
        return "extracted"

    def _extract_all(self) -> int:
        """把本地所有 gz 解压成 hgt（已存在跳过）；返回新增解压数。"""
        names = [n for n in self.gz_tiles()
                 if not _hgt_ok(self.dir_hgt / f"{n}.hgt")]
        if not names:
            return 0
        self._log(f"解压本地瓦片压缩包：{len(names)} 片")
        done = 0
        for i, name in enumerate(names, 1):
            st = self._extract_one(name)
            if st == "extracted":
                done += 1
            elif st.startswith(("bad_gz", "bad_size", "missing_gz")):
                self._log(f"  解压失败 {name}: {st}")
            if i % 10 == 0 or i == len(names):
                print(f"[progress {i}/{len(names)}] 解压瓦片 {i * 100 // len(names)}%",
                      flush=True)
        self._log(f"解压完成：新增 {done} 片")
        return done

    # ------------------------------------------------------------------ #
    # 在线下载
    # ------------------------------------------------------------------ #
    @staticmethod
    def _network_ok(timeout: float = 2.0) -> bool:
        """2 s 内探测 AWS S3 是否可达（离线时快速返回 False）。"""
        t0 = time.time()
        try:
            req = urllib.request.Request(
                S3_PROBE_URL, method="HEAD",
                headers={"User-Agent": "rocketkit-v3-dem/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status in (200, 206)
        except Exception:                   # noqa: BLE001 - 任何网络错误都算离线
            return False
        finally:
            _ = time.time() - t0

    def _emit_progress(self, progress, done: int, total: int, tile: str,
                       note: str = "") -> None:
        """兼容 progress(done,total,tile) 与 progress(fraction) 两种回调。"""
        if progress is None:
            return
        try:
            progress(done, total, tile)
        except TypeError:
            try:
                progress((done / total) if total else 0.0)     # noqa: B915
            except Exception:                                   # noqa: BLE001
                pass
        except Exception:                                       # noqa: BLE001
            pass
        if note:
            print(f"[progress {done}/{total}] {note}", flush=True)

    def download(self, tiles, progress=None, should_stop=None,
                 retries: int = 3, timeout: float = 25.0) -> dict:
        """下载并解压若干 SRTMGL1 瓦片（AWS skadi，gzip）。

        Parameters
        ----------
        tiles : iterable[str]
            瓦片名列表（如 ``N37E112``）。
        progress : callable, optional
            ``progress(done, total, tile)`` 或 ``progress(fraction)``。
        should_stop : callable, optional
            返回 True 时尽快中止（已下载的保留）。
        retries : int
            单瓦片重试次数。
        timeout : float
            单次 HTTP 超时 [s]（离线探测单独用 2 s，不会卡太久）。

        Returns
        -------
        dict
            ``{"ok": n, "failed": [tile...], "bytes": n, "offline": bool,
            "cancelled": bool}``
        """
        todo = [t for t in (tiles or [])
                if not _hgt_ok(self.dir_hgt / f"{t}.hgt")]
        result = {"ok": 0, "failed": [], "bytes": 0, "offline": False,
                  "cancelled": False, "requested": len(todo)}
        if not todo:
            return result
        if not self._network_ok(timeout=2.0):
            self._log(f"网络不可用（2 s 探测 S3 失败）→ 跳过下载 {len(todo)} 片，"
                      f"将使用粗 DEM 回退")
            result["offline"] = True
            result["failed"] = list(todo)
            return result

        fetch = self._fetch_dem()
        total = len(todo)
        self._log(f"在线补全：下载 {total} 片 SRTMGL1 瓦片（并发 1，失败重试 {retries} 次）")
        for i, name in enumerate(todo, 1):
            if should_stop is not None and should_stop():
                self._log(f"下载被用户取消（已完成 {i - 1}/{total}）")
                result["cancelled"] = True
                result["failed"] = todo[i - 1:]
                return result
            url = fetch.tile_url(name)
            gz_path = self.dir_gz / f"{name}.hgt.gz"
            part = gz_path.with_suffix(".hgt.gz.part")
            last_err = ""
            ok = False
            for attempt in range(max(1, retries)):
                try:
                    req = urllib.request.Request(
                        url, headers={"User-Agent": "rocketkit-v3-dem/1.0"})
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        with open(part, "wb") as fh:
                            while True:
                                if should_stop is not None and should_stop():
                                    break
                                chunk = resp.read(1 << 20)
                                if not chunk:
                                    break
                                fh.write(chunk)
                    if should_stop is not None and should_stop():
                        part.unlink(missing_ok=True)
                        result["cancelled"] = True
                        result["failed"] = todo[i - 1:]
                        return result
                    if part.stat().st_size < 10_000:
                        raise IOError(f"文件过小 {part.stat().st_size} B")
                    if _gzip_hgt_size(part) != HGT_BYTES:
                        raise IOError("gzip 解压长度不符（截断/损坏）")
                    part.replace(gz_path)
                    st = self._extract_one(name)
                    if st not in ("extracted", "existing"):
                        raise IOError(f"解压失败 {st}")
                    ok = True
                    break
                except Exception as exc:    # noqa: BLE001 - 网络错误统一重试
                    last_err = f"{type(exc).__name__}: {exc}"
                    part.unlink(missing_ok=True)
                    time.sleep(min(1.0 * (attempt + 1), 4.0))
            done = i
            pct = done * 100 // total
            if ok:
                result["ok"] += 1
                result["bytes"] += gz_path.stat().st_size if gz_path.exists() else 0
                self._emit_progress(progress, done, total, name,
                                    f"下载瓦片 {done}/{total}（{pct}%）{name}")
            else:
                result["failed"].append(name)
                self._emit_progress(progress, done, total, name,
                                    f"下载失败 {name}（{last_err}）")
                if result["failed"] and len(result["failed"]) >= 3 and not ok \
                        and self._network_ok(timeout=2.0) is False:
                    self._log("连续失败且网络探测失败 → 中止在线补全")
                    result["offline"] = True
                    result["failed"].extend(todo[i:])
                    return result
        self._log(f"在线补全完成：成功 {result['ok']}，失败 {len(result['failed'])}"
                  if result["failed"] else f"在线补全完成：成功 {result['ok']}")
        return result

    # ------------------------------------------------------------------ #
    # 观测点缓存
    # ------------------------------------------------------------------ #
    def _tiles_for_point(self, lat: float, lon: float, radius_km: float) -> list[str]:
        """单点 radius_km 圆覆盖到的瓦片名。"""
        fetch = self._fetch_dem()
        la0, la1, lo0, lo1 = fetch.tiles_for_circle(lat, lon, radius_km)
        tiles = set()
        for i in range(math.floor(la0), math.floor(la1) + 1):
            for j in range(math.floor(lo0), math.floor(lo1) + 1):
                tiles.add(_tile_name(i, j))
        return sorted(tiles)

    def observer_ready(self, config: dict) -> dict:
        """观测点/发射场（地形遮挡、天空轨迹真正依赖的部分）的瓦片是否齐备。

        Returns
        -------
        dict
            ``{"ready": bool, "need": n, "lack": [tile...]}``。注意：
            ``prepare()["mode"]`` 是**整个任务**（含远处航警区/落区）的口径，
            比本字段严格；天空图的遮挡角只看本字段（观测点 200 km 圆）。

        v3.1：若某个自定义高程源完整覆盖观测点/发射场的 ``horizon_radius_km``
        包围盒（遮挡角确实会走自定义源），直接返回 ready=True，并多给一个
        ``"custom": [id...]`` 字段（无自定义源时返回值与旧版逐位一致）。
        """
        radius = float((config.get("dem") or {}).get("horizon_radius_km", 200.0))
        pts = [(o.get("lat_deg"), o.get("lon_deg"))
               for o in (config.get("observers") or [])]
        site = ((config.get("mission") or {}).get("launch_site")
                or config.get("launch_site") or {})
        if site.get("lat_deg") is not None:
            pts.append((site.get("lat_deg"), site.get("lon_deg")))
        pts = [(float(a), float(b)) for a, b in pts if a is not None and b is not None]
        # v3.1：自定义高程源覆盖遮挡半径 → 等价于"瓦片齐备"
        if pts:
            try:
                dem = self._engine_dem()
                ids = []
                ok = True
                for lat, lon in pts:
                    cov = dem.custom_covers_radius(lat, lon, radius)
                    if not cov["covered"]:
                        ok = False
                        break
                    ids.append(str(cov["source"]))
                if ok and ids:
                    self._log(f"观测点 {radius:.0f} km 覆盖由自定义高程源 "
                              f"{', '.join(sorted(set(ids)))} 提供")
                    return {"ready": True, "need": 0, "lack": [],
                            "custom": sorted(set(ids))}
            except Exception as exc:            # noqa: BLE001 - 回退原有判断
                self._log(f"自定义高程源半径判断失败（忽略）：{exc}")
        need: set[str] = set()
        for lat, lon in pts:
            need |= set(self._tiles_for_point(float(lat), float(lon), radius))
        present = set(self.present_tiles())
        lack = sorted(need - present)
        return {"ready": not lack, "need": len(need), "lack": lack}

    def _build_caches(self, config: dict) -> list[str]:
        """为配置里的观测点建 3″ 局部缓存（缺瓦片时跳过并记录）。"""
        dem = self._engine_dem()
        radius = float((config.get("dem") or {}).get("horizon_radius_km", 200.0))
        built: list[str] = []
        present = set(self.present_tiles())
        for obs in (config.get("observers") or []):
            oid = str(obs.get("id") or "obs")
            try:
                lat = float(obs["lat_deg"])
                lon = float(obs["lon_deg"])
            except (KeyError, TypeError, ValueError):
                self._log(f"观测点 {oid} 坐标缺失，跳过 DEM 缓存")
                continue
            need = self._tiles_for_point(lat, lon, radius)
            lack = [t for t in need if t not in present]
            if lack:
                self._log(f"观测点 {oid} 缺 {len(lack)} 片瓦片 {lack[:4]}"
                          f"{'…' if len(lack) > 4 else ''} → 不建 3″ 缓存（走粗 DEM 回退）")
                continue
            try:
                t0 = time.time()
                dem.build_observer_cache(oid, lat, lon, radius_km=radius,
                                         verbose=False)
                built.append(oid)
                self._log(f"观测点 {oid} 3″ DEM 缓存就绪"
                          f"（{time.time() - t0:.1f} s）")
            except Exception as exc:        # noqa: BLE001 - 缓存可缺，不致命
                self._log(f"观测点 {oid} 缓存构建失败（忽略）：{exc}")
        return built

    # ------------------------------------------------------------------ #
    # 粗 DEM
    # ------------------------------------------------------------------ #
    def coarse_dem_path(self) -> Path | None:
        """内嵌粗 DEM npz 路径（找不到返回 None）。"""
        for p in _coarse_dem_candidates():
            if p.is_file():
                return p
        return None

    def coarse_available(self) -> bool:
        """内嵌粗 DEM 是否可用（不抛异常）。"""
        return self.coarse_dem_path() is not None

    # ------------------------------------------------------------------ #
    # 一步到位
    # ------------------------------------------------------------------ #
    def prepare(self, config, allow_download: bool = True,
                scan: bool = True, scan_seconds: float = 12.0) -> dict:
        """扫描导入 → 解压 → 补缺（可选下载）→ 建观测点缓存 → 返回数据模式。

        Parameters
        ----------
        config : dict
            任务配置（``config.json`` 的内容）。
        allow_download : bool
            False = 只做本地（离线模式，不发起任何网络请求）。
        scan : bool
            是否扫描历史工作区导入已有瓦片。
        scan_seconds : float
            历史扫描时间预算 [s]。

        Returns
        -------
        dict
            ``{"mode": "srtm|coarse|none", "imported": n, "downloaded": n,
            "failed": [...], "tiles": n, "required": n, "observer_ready": bool,
            ...}``

        Notes
        -----
        ``mode`` 是**整个任务**（观测点 + 发射场 + 航警区 + 落点）的口径：
        远处航警区/落区缺片时 mode 可能为 ``coarse``，但仍会为观测点建 3″ 缓存，
        天空图/遮挡角照旧走 1″ SRTM（见 ``observer_ready``）。

        v3.1：先看用户离线导入的**自定义高程源**（``<dem_dir>/custom/index.json``）。
        若它覆盖本任务所有关键点 → ``mode = "custom"``，**跳过扫描/下载/缓存构建**，
        返回 ``{"mode": "custom", "tiles": 0, "custom": [...], ...}``；
        只覆盖一部分时保持原流程，并在返回值 ``custom`` 字段里标注覆盖情况。
        """
        t0 = time.time()
        self._sync_env()

        # ---- v3.1：自定义高程源覆盖全部关键点 → 直接进 custom 模式 ----
        custom = self.custom_plan(config)
        if custom["ready"]:
            self._log(f"自定义高程源覆盖本任务全部 {len(custom['key_points'])} 个关键点"
                      f"（{', '.join(custom['sources'])}）→ 跳过瓦片扫描/下载/缓存构建")
            if not custom["radius_ready"]:
                self._log("提示：观测点遮挡半径未被自定义高程源完整覆盖，"
                          "地形遮挡角会回退粗 DEM（点采样仍用自定义源）")
            coarse_path = self.coarse_dem_path()
            result = {
                "mode": "custom",
                "imported": 0,
                "downloaded": 0,
                "failed": [],
                "tiles": 0,
                "required": 0,
                "missing": [],
                "observer_ready": True,
                "observer_lack": [],
                "offline": False,
                "cache_built": [],
                "dem_dir": str(self.dem_dir),
                "coarse_dem": str(coarse_path) if coarse_path else None,
                "custom": list(custom["sources"]),
                "custom_coverage": custom.get("coverage"),
                "custom_radius_ready": bool(custom["radius_ready"]),
                "skipped": "custom_source_covers_all_key_points",
                "elapsed_s": round(time.time() - t0, 1),
            }
            self._last_prepare = result
            self._write_mode_file("custom", result)
            return result

        imported = self.scan_and_import(seconds=scan_seconds) if scan else 0
        self._extract_all()
        required = self.required_tiles(config)
        missing = [t for t in required if not _hgt_ok(self.dir_hgt / f"{t}.hgt")]
        downloaded = 0
        failed: list[str] = []
        offline = False
        if missing and allow_download:
            info = self.download(missing)
            downloaded = int(info.get("ok", 0))
            failed = list(info.get("failed") or [])
            offline = bool(info.get("offline"))
        elif missing:
            self._log(f"离线模式：缺 {len(missing)} 片瓦片，不联网下载")
            failed = list(missing)

        present = set(self.present_tiles())
        still_missing = [t for t in required if t not in present]
        # 观测点 3″ 缓存只看该点自己的 200 km 覆盖，不受远处航警/落区瓦片缺失影响
        cache_built = self._build_caches(config)
        obs_cov = self.observer_ready(config)
        if still_missing:
            self._log(f"仍缺 {len(still_missing)} 片瓦片（示例 {still_missing[:6]}）"
                      f"→ 缺片区域的地形数据回退粗 DEM")
        if obs_cov["ready"]:
            self._log("观测点/发射场 200 km 覆盖瓦片齐备（天空轨迹/遮挡角走 1″ SRTM）")

        if not required:
            mode = "none"
        elif not still_missing:
            mode = "srtm"
        elif self.coarse_available():
            mode = "coarse"
        else:
            mode = "none"
        coarse_path = self.coarse_dem_path()
        if mode == "coarse":
            self._log("DEM 模式 = coarse（内嵌 0.02° 粗 DEM 回退，约 2 km，"
                      "首次出现时刻精度约 ±数十秒）")
        elif mode == "srtm":
            self._log(f"DEM 模式 = srtm（{len(required)} 片 1″ 瓦片齐备，"
                      f"3″ 缓存 {len(cache_built)} 个）")
        else:
            self._log("DEM 模式 = none（既无瓦片也无内嵌粗 DEM）")

        result = {
            "mode": mode,
            "imported": imported,
            "downloaded": downloaded,
            "failed": failed,
            "tiles": len(required) - len(still_missing),
            "required": len(required),
            "missing": still_missing[:50],
            "observer_ready": bool(obs_cov["ready"]),
            "observer_lack": obs_cov["lack"][:20],
            "offline": offline,
            "cache_built": cache_built,
            "dem_dir": str(self.dem_dir),
            "coarse_dem": str(coarse_path) if coarse_path else None,
            # v3.1：自定义高程源的部分覆盖情况（全不覆盖时为 None）
            "custom": (list(custom["sources"]) if custom["sources"] else []),
            "custom_coverage": custom.get("coverage"),
            "custom_radius_ready": bool(custom["radius_ready"]),
            "elapsed_s": round(time.time() - t0, 1),
        }
        if custom["sources"] and not custom["ready"]:
            cov = custom.get("coverage") or {}
            tot = cov.get("total") or {}
            self._log(
                f"自定义高程源部分覆盖：{tot.get('covered', 0)}/{tot.get('total', 0)} 个"
                f"关键点（{custom['sources']}）→ 仍按 SRTM/粗 DEM 流程补全")
        self._last_prepare = result
        self._write_mode_file(mode, result)
        return result

    def _write_mode_file(self, mode: str, info: dict | None = None) -> None:
        """把模式落盘到 ``<dem_dir>/dem_mode.json``（runner/GUI 可读，engine 同路径）。"""
        try:
            p = self.dem_dir / "dem_mode.json"
            tmp = self.dem_dir / "dem_mode.json.part"
            tmp.write_text(json.dumps({
                "mode": mode,
                "prepared": True,
                "dem_dir": str(self.dem_dir),
                "coarse_dem": str(self.coarse_dem_path() or ""),
                "warning": ("未找到 SRTM 瓦片，已回退 0.02° 粗 DEM（约 2 km），"
                            "首次出现时刻精度约 ±数十秒") if mode == "coarse" else "",
                "prepare": info or {},
                "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(p)
        except Exception as exc:            # noqa: BLE001 - 模式文件只是辅助信息
            self._log(f"写 dem_mode.json 失败（忽略）：{exc}")

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def dem_mode(self) -> str:
        """当前 DEM 数据模式：``"custom" | "srtm" | "coarse" | "none"``。

        v3.1 起多一个 ``"custom"``（用户离线导入的 GeoTIFF）：只要登记了可用的
        自定义源，点采样就优先用它；``prepare()`` 跑过之后以它记录的模式为准。
        """
        if self._last_prepare.get("mode"):
            return str(self._last_prepare["mode"])
        try:
            if self.custom_sources(valid_only=True):
                return "custom"
        except Exception:                       # noqa: BLE001
            pass
        if self.present_tiles():
            return "srtm"
        if self.coarse_available():
            return "coarse"
        return "none"

    def observer_alt(self, lat, lon) -> tuple[float | None, str | None]:
        """观测点高程 [m] 与来源：``(alt, "custom"|"srtm"|"coarse"|None)``。

        顺序：自定义高程源（用户导入的 GeoTIFF）→ 1″ SRTM 瓦片 → 内嵌粗网格；
        都没有返回 ``(None, None)``。
        """
        try:
            lat = float(lat)
            lon = float(lon)
        except (TypeError, ValueError):
            return None, None
        # 0) 自定义高程源（v3.1）：只在该点真的被覆盖且取到有效高程时采用
        try:
            dem = self._engine_dem()
            for rec in dem.custom_sources():
                src = rec["_src"]
                if not src.covers(lat, lon):
                    continue
                v = float(src.sample(lat, lon, method="nearest"))
                if math.isfinite(v) and abs(v) < 12000:
                    return v, "custom"
        except Exception:                   # noqa: BLE001 - 自定义源失败就走原逻辑
            pass
        # 1) SRTM 1″ 瓦片精确采样（先确认该瓦片 hgt 完整存在，
        #    避免把 dem.sample 的粗 DEM 回退误报成 "srtm"）
        try:
            dem = self._engine_dem()
            tile = dem.tile_name(lat, lon)
            if _hgt_ok(self.dir_hgt / f"{tile}.hgt"):
                v = float(dem.sample(lat, lon))
                if math.isfinite(v) and abs(v) < 12000:
                    return v, "srtm"
        except Exception:                   # noqa: BLE001 - 瓦片路径失败就走粗网格
            pass
        # 2) 内嵌粗网格
        try:
            from .coarse_dem import get_coarse_dem      # app.core.coarse_dem
        except Exception:                               # noqa: BLE001 - 直接脚本运行时的回退
            try:
                self._ensure_syspath()
                from app.core.coarse_dem import get_coarse_dem  # type: ignore
            except Exception:                           # noqa: BLE001
                get_coarse_dem = None
        try:
            cdm = get_coarse_dem() if get_coarse_dem is not None else None
            if cdm is not None and cdm.available():
                v = float(cdm.sample(lat, lon, clip_negative=True))
                if math.isfinite(v):
                    return v, "coarse"
        except Exception:                   # noqa: BLE001
            pass
        return None, None

    def status(self) -> dict:
        """本地瓦片/缓存/粗 DEM/自定义源概览（给 GUI 体检表用）。

        v3.1 新增字段：

        * ``custom_sources``：自定义高程源列表（含 ``valid``/``error``，失效的也列出）；
        * ``custom_available``：是否有可用自定义源；
        * ``custom_coverage``：对**最近一次任务关键点**的覆盖率（没跑过 prepare 为 None）；
        * ``custom_index``：登记表路径。
        """
        present = self.present_tiles()
        caches = sorted(p.name for p in self.dir_cache.glob("*.npz")) \
            if self.dir_cache.is_dir() else []
        gz = self.gz_tiles()
        custom_sources: list[dict] = []
        custom_coverage = None
        custom_available = False
        try:
            mgr = self._custom_manager()
            custom_sources = mgr.list_sources(valid_only=False)
            custom_available = any(s.get("valid") for s in custom_sources)
            if custom_available and self._key_points_last:
                custom_coverage = mgr.coverage(self._key_points_last)
        except Exception as exc:                # noqa: BLE001 - 概览失败不影响主流程
            self._log(f"自定义高程源状态读取失败（忽略）：{exc}")
        return {
            "dem_dir": str(self.dem_dir),
            "n_hgt": len(present),
            "n_gz": len(gz),
            "tiles": present,
            "caches": caches,
            "coarse_dem": str(self.coarse_dem_path() or ""),
            "mode": self.dem_mode(),
            "custom_sources": custom_sources,
            "custom_available": bool(custom_available),
            "custom_coverage": custom_coverage,
            "custom_index": str(self.dem_dir / "custom" / "index.json"),
        }
