# -*- coding: utf-8 -*-
"""coarse_dem.py — 内嵌 0.02° 全国粗 DEM（离线回退地形底子）读取/采样/遮挡角。

背景
----
v3.0 离线版在没有任何 SRTM 1″ 瓦片时仍要能推算天空轨迹：地形遮挡角、观测点高程
改由内嵌的 0.02°（约 2 km）中国范围粗网格提供，精度换可用性（首次出现时刻约
±数十秒）。数据由 ``engine/tools/build_coarse_dem.py`` 离线构建，产物
``app/data/china_dem_0p02.npz`` 随程序分发。

npz 格式（只依赖 numpy + 标准库，无网络、无 Pillow）
----------------------------------------------------
* ``lat``  (ny,) float32 —— 纬度格心，升序 [deg]
* ``lon``  (nx,) float32 —— 经度格心，升序 [deg]
* ``elev`` (ny, nx) int16 —— 高程 [m]，行=lat、列=lon；**负值=海底深度**
  （terrarium 用 ETOPO1 填充海洋，与 SRTMGL1 skadi 瓦片语义一致，
  遮挡角计算按海平面裁剪）
* ``meta`` json 字符串 —— 源/许可/分辨率/构建日期/覆盖范围

若 npz 不存在，:class:`CoarseDEM` 不抛异常，``available()`` 返回 ``False``。

算法口径（与 ``engine/common/dem.py`` 完全一致）
------------------------------------------------
* 沿方位角用 **WGS-84 椭球测地线正解（Vincenty 1975）** 取地形点；
* 观测点与地形点都转 ECEF，再转到观测点局部 ENU 系求仰角
  ``atan2(U, hypot(E, N))`` —— **不做** ``d²/2R`` 近似；
* 粗网格高程用**双线性插值**（``engine/common/dem.py`` 的粗 DEM 回退是同一套公式）。

已知限制
--------
* 网格分辨率约 2 km：城市高楼、河堤、近场（<1 km）遮挡完全不可用；
* 地形遮挡角通常被平滑掉 0.2–1.5°（山峰/山脊被平均）；
* 首次出现时刻精度约 ±数十秒（远场主要看几何地平，误差可接受）。
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np

__all__ = ["CoarseDEM", "resolve_npz_path", "DEFAULT_NPZ_NAME",
           "DEFAULT_RESOLUTION_DEG", "WGS84_A", "WGS84_F", "WGS84_E2"]

# --------------------------------------------------------------------------- #
# 常量（与 engine/common/geo.py 一致）
# --------------------------------------------------------------------------- #
WGS84_A = 6378137.0                        # 长半轴 [m]
WGS84_F = 1.0 / 298.257223563              # 扁率
WGS84_B = WGS84_A * (1.0 - WGS84_F)
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)
ARCSEC_PER_DEG = 3600.0

DEFAULT_NPZ_NAME = "china_dem_0p02.npz"
DEFAULT_RESOLUTION_DEG = 0.02              # 约 2.2 km（南北向）

_APP_ROOT = Path(__file__).resolve().parents[1]          # .../app
_PKG_DATA = _APP_ROOT / "data"
_PROJ_ROOT = _APP_ROOT.parent                            # 开发态项目根


# --------------------------------------------------------------------------- #
# npz 路径解析
# --------------------------------------------------------------------------- #
def resolve_npz_path(npz_path=None) -> Path:
    """确定粗 DEM npz 路径（优先显式参数 → RK_COARSE_DEM → app/data → engine/data）。

    返回的路径**不保证存在**；用 :meth:`CoarseDEM.available` 判断。
    """
    if npz_path:
        return Path(npz_path)
    env = os.environ.get("RK_COARSE_DEM")
    if env:
        return Path(env)
    candidates = [
        _PKG_DATA / DEFAULT_NPZ_NAME,                       # app/data（开发/冻结态均在此）
        _PROJ_ROOT / "app" / "data" / DEFAULT_NPZ_NAME,      # 兼容从别处安装的包
        _PROJ_ROOT / "engine" / "data" / DEFAULT_NPZ_NAME,   # 兼容只放 engine 的旧布局
        Path.cwd() / "app" / "data" / DEFAULT_NPZ_NAME,
    ]
    for p in candidates:
        if p.is_file():
            return p
    return candidates[0]


# --------------------------------------------------------------------------- #
# 粗 DEM 对象
# --------------------------------------------------------------------------- #
class CoarseDEM:
    """内嵌 0.02° 中国范围粗 DEM。

    Parameters
    ----------
    npz_path : str or Path, optional
        npz 路径；缺省按 :func:`resolve_npz_path` 顺序查找。

    Attributes
    ----------
    path : Path
        实际使用的 npz 路径。
    loaded : bool
        是否成功读入网格。
    resolution_deg : float
        网格间距 [deg]（默认 0.02，从 meta 读，缺失时按 lat 数组推算）。
    meta : dict
        npz 里的 meta（源/许可/构建日期/覆盖范围…）。
    """

    def __init__(self, npz_path=None):
        self.path = resolve_npz_path(npz_path)
        self.loaded = False
        self.load_error = ""
        self.lat = np.empty(0, dtype=np.float32)
        self.lon = np.empty(0, dtype=np.float32)
        self.elev = np.empty((0, 0), dtype=np.int16)
        self.meta: dict = {}
        self.resolution_deg = float(DEFAULT_RESOLUTION_DEG)
        self._lat0 = 0.0
        self._lon0 = 0.0
        try:
            self._load()
        except Exception as exc:                     # noqa: BLE001 - 缺文件不抛异常
            self.load_error = f"{type(exc).__name__}: {exc}"
            self.loaded = False

    # ---------------------------- 读取 ---------------------------- #
    def _load(self) -> None:
        if not self.path.is_file():
            self.load_error = f"未找到粗 DEM 文件 {self.path}"
            return
        with np.load(self.path, allow_pickle=False) as z:
            lat = np.asarray(z["lat"], dtype=np.float64).ravel()
            lon = np.asarray(z["lon"], dtype=np.float64).ravel()
            elev = np.asarray(z["elev"])
            meta_raw = z["meta"] if "meta" in z.files else None
        if elev.ndim != 2 or elev.shape != (lat.size, lon.size):
            raise ValueError(f"npz 网格形状不一致 elev={elev.shape} "
                             f"lat={lat.size} lon={lon.size}")
        if lat.size < 2 or lon.size < 2:
            raise ValueError("npz 网格过小")
        if np.any(np.diff(lat) <= 0) or np.any(np.diff(lon) <= 0):
            raise ValueError("npz 的 lat/lon 必须升序（行=纬度、列=经度）")
        self.lat = lat.astype(np.float32)
        self.lon = lon.astype(np.float32)
        self.elev = elev.astype(np.int16, copy=False)
        meta = {}
        if meta_raw is not None:
            try:
                txt = meta_raw.item() if hasattr(meta_raw, "item") else meta_raw
                meta = json.loads(str(txt))
            except Exception:                       # noqa: BLE001 - meta 坏了不影响采样
                meta = {}
        self.meta = meta if isinstance(meta, dict) else {}
        # 分辨率：round 到 1e-6°（float32 的 lat 差分有 ~5e-7 抖动，需与 engine 侧同口径）
        self.resolution_deg = round(float(np.median(np.diff(lat))), 6)
        # 覆盖范围西南角（格心 - 半个 step）
        self._lat0 = float(self.lat[0]) - 0.5 * self.resolution_deg
        self._lon0 = float(self.lon[0]) - 0.5 * self.resolution_deg
        self.loaded = True

    # ---------------------------- 查询 ---------------------------- #
    def available(self) -> bool:
        """是否成功加载粗 DEM（缺文件时 False，不抛异常）。"""
        return bool(self.loaded and self.elev.size > 0)

    @property
    def resolution_km(self) -> float:
        """网格间距的南北向物理尺度 [km]（约 2.2 km）。"""
        return self.resolution_deg * 111.19

    def coverage(self) -> dict:
        """覆盖范围 ``{lat_min, lat_max, lon_min, lon_max}``（度）；未加载返回 {}。"""
        if not self.available():
            return {}
        return {"lat_min": float(self._lat0),
                "lat_max": float(self.lat[-1]) + 0.5 * self.resolution_deg,
                "lon_min": float(self._lon0),
                "lon_max": float(self.lon[-1]) + 0.5 * self.resolution_deg}

    def describe(self) -> str:
        """一行中文说明（供 GUI/日志用）。"""
        if not self.available():
            return f"粗 DEM 不可用（{self.load_error or self.path}）"
        cv = self.coverage()
        return (f"内嵌粗 DEM 0.02°（约 {self.resolution_km:.1f} km）"
                f"[{self.elev.shape[0]}×{self.elev.shape[1]}] "
                f"覆盖 {cv['lat_min']:.0f}–{cv['lat_max']:.0f}°N / "
                f"{cv['lon_min']:.0f}–{cv['lon_max']:.0f}°E，"
                f"源 {self.meta.get('product', 'terrarium')}")

    # ---------------------------- 高程采样 ---------------------------- #
    @staticmethod
    def _bilinear(elev: np.ndarray, lat0: float, lon0: float, step: float,
                  lat, lon):
        """粗网格双线性插值（内部函数）；网格外 → nan。"""
        la = np.asarray(lat, dtype=float)
        lo = np.asarray(lon, dtype=float)
        la, lo = np.broadcast_arrays(la, lo)
        ny, nx = elev.shape
        r = (la - lat0) / step - 0.5                 # 格心索引（格心=0,1,...）
        c = (lo - lon0) / step - 0.5
        inside = (np.isfinite(la) & np.isfinite(lo)
                  & (r >= -0.5) & (r <= ny - 0.5)
                  & (c >= -0.5) & (c <= nx - 0.5))
        out = np.full(la.shape, np.nan, dtype=float)
        if not inside.any():
            return out
        rr = np.clip(r, 0.0, ny - 1.0)
        cc = np.clip(c, 0.0, nx - 1.0)
        r0 = np.floor(rr).astype(np.intp)
        c0 = np.floor(cc).astype(np.intp)
        r0 = np.minimum(r0, ny - 2)
        c0 = np.minimum(c0, nx - 2)
        fr = rr - r0
        fc = cc - c0
        v00 = elev[r0[inside], c0[inside]].astype(float)
        v10 = elev[r0[inside] + 1, c0[inside]].astype(float)
        v01 = elev[r0[inside], c0[inside] + 1].astype(float)
        v11 = elev[r0[inside] + 1, c0[inside] + 1].astype(float)
        fi, fj = fr[inside], fc[inside]
        out[inside] = (v00 * (1 - fi) * (1 - fj) + v10 * fi * (1 - fj)
                       + v01 * (1 - fi) * fj + v11 * fi * fj)
        return out

    def sample(self, lat, lon, clip_negative: bool = False):
        """双线性采样高程 [m]。

        Parameters
        ----------
        lat, lon : float or array_like
            经纬度 [deg]。
        clip_negative : bool
            True 时负高程（海底）按 0（海平面）处理，默认 False 原样返回
            （与 ``common.dem.sample`` 一致：负值 = 水深）。

        Returns
        -------
        float or ndarray
            高程 [m]；落在覆盖范围外返回 ``nan``。
        """
        if not self.available():
            return np.nan
        v = self._bilinear(self.elev, self._lat0, self._lon0,
                           self.resolution_deg, lat, lon)
        if clip_negative:
            v = np.maximum(v, 0.0)
        return float(v) if np.ndim(v) == 0 else v

    def dem_alt(self, lat, lon, clip_negative: bool = False):
        """与 :func:`sample` 同义（语义对齐 ``common.dem.dem_alt``）。"""
        return self.sample(lat, lon, clip_negative=clip_negative)

    # ---------------------------- 遮挡角 ---------------------------- #
    @staticmethod
    def _geodetic_to_ecef(lat_deg, lon_deg, h_m):
        """WGS-84 大地坐标 → ECEF（与 common/geo.py 同公式）。"""
        lat = np.deg2rad(np.asarray(lat_deg, dtype=float))
        lon = np.deg2rad(np.asarray(lon_deg, dtype=float))
        h = np.asarray(h_m, dtype=float)
        sin_lat, cos_lat = np.sin(lat), np.cos(lat)
        N = WGS84_A / np.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
        x = (N + h) * cos_lat * np.cos(lon)
        y = (N + h) * cos_lat * np.sin(lon)
        z = (N * (1.0 - WGS84_E2) + h) * sin_lat
        return np.stack(np.broadcast_arrays(x, y, z), axis=-1)

    @staticmethod
    def _elevation_angles(obs_lat, obs_lon, obs_h_m, tgt_lat, tgt_lon, tgt_h_m):
        """观测点 → 目标点的仰角 [deg]（ECEF 精确几何，含地球曲率）。"""
        obs = np.asarray(CoarseDEM._geodetic_to_ecef(obs_lat, obs_lon, obs_h_m),
                         dtype=float)
        tgt = np.asarray(CoarseDEM._geodetic_to_ecef(tgt_lat, tgt_lon, tgt_h_m),
                         dtype=float)
        lat = math.radians(float(obs_lat))
        lon = math.radians(float(obs_lon))
        sin_lat, cos_lat = math.sin(lat), math.cos(lat)
        sin_lon, cos_lon = math.sin(lon), math.cos(lon)
        # ENU 基矢量（省略与水平面无关的 U 分量也可，这里完整做）
        e_dir = np.array([-sin_lon, cos_lon, 0.0])
        n_dir = np.array([-sin_lat * cos_lon, -sin_lat * sin_lon, cos_lat])
        u_dir = np.array([cos_lat * cos_lon, cos_lat * sin_lon, sin_lat])
        d = tgt - obs
        e = d @ e_dir
        n = d @ n_dir
        u = d @ u_dir
        return np.degrees(np.arctan2(u, np.hypot(e, n)))

    @staticmethod
    def _direct_vec(lat0_deg, lon0_deg, az_deg, dist_km):
        """WGS-84 椭球测地线正解（Vincenty 1975，numpy 向量化）。

        与 ``common.dem._direct_vec`` 逐位同算法（固定 10 次迭代），用于保证粗 DEM
        遮挡角与 SRTM 遮挡角在同一几何口径下可比。
        """
        a, f, b = WGS84_A, WGS84_F, WGS84_B
        lat1 = math.radians(lat0_deg)
        az = math.radians(az_deg)
        s = np.asarray(dist_km, dtype=float) * 1000.0
        sin_a1, cos_a1 = math.sin(az), math.cos(az)
        tanU1 = (1.0 - f) * math.tan(lat1)
        cosU1 = 1.0 / math.sqrt(1.0 + tanU1 ** 2)
        sinU1 = tanU1 * cosU1
        sigma1 = math.atan2(tanU1, cos_a1)
        sin_alpha = cosU1 * sin_a1
        cos2_alpha = 1.0 - sin_alpha ** 2
        u2 = cos2_alpha * (a * a - b * b) / (b * b)
        A = 1.0 + u2 / 16384.0 * (4096.0 + u2 * (-768.0 + u2 * (320.0 - 175.0 * u2)))
        B = u2 / 1024.0 * (256.0 + u2 * (-128.0 + u2 * (74.0 - 47.0 * u2)))
        sigma = s / (b * A)
        for _ in range(10):
            cos2_sigma_m = np.cos(2.0 * sigma1 + sigma)
            sin_sigma, cos_sigma = np.sin(sigma), np.cos(sigma)
            d_sigma = B * sin_sigma * (cos2_sigma_m + B / 4.0 * (
                cos_sigma * (-1.0 + 2.0 * cos2_sigma_m ** 2)
                - B / 6.0 * cos2_sigma_m * (-3.0 + 4.0 * sin_sigma ** 2)
                * (-3.0 + 4.0 * cos2_sigma_m ** 2)))
            sigma = s / (b * A) + d_sigma
        cos2_sigma_m = np.cos(2.0 * sigma1 + sigma)
        sin_sigma, cos_sigma = np.sin(sigma), np.cos(sigma)
        tmp = sinU1 * sin_sigma - cosU1 * cos_sigma * cos_a1
        lat2 = np.arctan2(sinU1 * cos_sigma + cosU1 * sin_sigma * cos_a1,
                          (1.0 - f) * np.sqrt(sin_alpha ** 2 + tmp ** 2))
        lam = np.arctan2(sin_sigma * sin_a1,
                         cosU1 * cos_sigma - sinU1 * sin_sigma * cos_a1)
        C = f / 16.0 * cos2_alpha * (4.0 + f * (4.0 - 3.0 * cos2_alpha))
        L = lam - (1.0 - C) * f * sin_alpha * (
            sigma + C * sin_sigma * (cos2_sigma_m + C * cos_sigma *
                                     (-1.0 + 2.0 * cos2_sigma_m ** 2)))
        lon2 = lon0_deg + np.degrees(L)
        lon2 = (lon2 + 180.0) % 360.0 - 180.0
        return np.degrees(lat2), lon2

    def horizon_deg(self, lat, lon, az_deg, radius_km: float = 200.0,
                    step_km: float = 1.0, obs_alt_m: float = None,
                    sea_level_clip: bool = True):
        """粗网格地形遮挡角 [deg]（与 ``common.dem.terrain_horizon_deg`` 同口径）。

        Parameters
        ----------
        lat, lon : float
            观测点经纬度 [deg]。
        az_deg : float or array_like
            方位角 [deg]（正北 0，顺时针）。
        radius_km : float
            搜索半径 [km]，默认 200。
        step_km : float
            沿方位采样步长 [km]，默认 1.0；与 ``common.dem.terrain_horizon_deg`` 同口径，
            调用方传 0.09 即为 90 m 步长（粗网格下相邻采样点由双线性插值给出）。
        obs_alt_m : float, optional
            观测点椭球高 [m]；缺省用粗 DEM 采样值（负值按 0）。
        sea_level_clip : bool
            负高程（海底）按 0 处理，默认 True（与 SRTM 路径一致）。

        Returns
        -------
        float or ndarray
            遮挡角 [deg]；粗 DEM 不可用时返回 ``nan``。
        """
        if not self.available():
            return np.nan
        az = np.asarray(az_deg, dtype=float).ravel()
        if obs_alt_m is None:
            obs_alt = float(self.sample(lat, lon, clip_negative=True))
            if not np.isfinite(obs_alt):
                obs_alt = 0.0
        else:
            obs_alt = float(obs_alt_m)
        step_km = float(step_km) if step_km else 1.0
        dist = np.arange(step_km, float(radius_km) + step_km * 0.5, step_km)
        out = np.empty(az.size, dtype=float)
        for i, a in enumerate(az):
            tgt_lat, tgt_lon = self._direct_vec(float(lat), float(lon), float(a), dist)
            dem_m = self.sample(tgt_lat, tgt_lon)
            dem_m = np.nan_to_num(np.asarray(dem_m, dtype=float), nan=0.0)
            if sea_level_clip:
                dem_m = np.maximum(dem_m, 0.0)
            el = self._elevation_angles(lat, lon, obs_alt, tgt_lat, tgt_lon, dem_m)
            out[i] = float(np.max(el))
        return float(out[0]) if np.ndim(az_deg) == 0 else out

    def __repr__(self) -> str:                       # pragma: no cover - 调试用
        return f"CoarseDEM(path={self.path!s}, available={self.available()})"


# --------------------------------------------------------------------------- #
# 进程内单例（GUI/runner 反复查询时不重复读 13 MB npz）
# --------------------------------------------------------------------------- #
_SINGLETON = None


def get_coarse_dem(npz_path=None, force_reload: bool = False) -> CoarseDEM:
    """返回进程内共享的 :class:`CoarseDEM`（可指定路径强制重建）。"""
    global _SINGLETON
    if force_reload or _SINGLETON is None or (
            npz_path is not None and Path(npz_path) != _SINGLETON.path):
        _SINGLETON = CoarseDEM(npz_path)
    return _SINGLETON


if __name__ == "__main__":                           # pragma: no cover - 手动自检
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                # noqa: BLE001
        pass
    cdm = CoarseDEM()
    print(cdm.describe())
    if cdm.available():
        for la, lo, name in [(39.9042, 116.4074, "北京"),
                             (37.7615, 112.7252, "晋中观测点"),
                             (38.8632, 111.5896, "太原 LC-9"),
                             (18.2528, 109.5119, "三亚"),
                             (30.5728, 104.0668, "成都")]:
            print(f"  {name}: ({la}, {lo}) → {cdm.sample(la, lo):.0f} m")
        print(f"  遮挡角(晋中, az=300°) = {cdm.horizon_deg(37.7615, 112.7252, 300.0):+.3f}°")
        print(f"  遮挡角(晋中, 0..350 步长 30) = "
              f"{np.round(cdm.horizon_deg(37.7615, 112.7252, np.arange(0, 360, 30)), 2).tolist()}")
