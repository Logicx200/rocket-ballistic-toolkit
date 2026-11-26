#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""build_coarse_dem.py — 构建「内嵌全国粗 DEM」npz（离线回退地形底子）。

用途
----
v3.0 离线版在**没有任何 SRTM 1″ 瓦片**时仍要能跑完整条流水线（天空轨迹/地形遮挡/
观测点高程）。本脚本从公开地形瓦片服务下载中国范围的低分辨率高程，拼成经纬度
网格后降采样为 0.02°×0.02°（约 2 km）的 int16 网格，打包为
``app/data/china_dem_0p02.npz``（目标 ≤ 25 MB），随程序分发，运行时只读。

数据源
-----
    https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png

Mapzen / AWS Open Data「Terrain Tiles」terrarium 编码（公有领域）：
PNG 三通道编码高程 ``h = (R*256 + G + B/256) - 32768``（米）。
海洋用 ETOPO1 海底地形填充（**负值 = 水深**，与 SRTMGL1 skadi 瓦片一致），
计算遮挡角时须按海平面裁剪（见 common/dem.py 的 sea_level_clip 语义）。
z=7 时每像素约 1.2 km（赤道）/0.9 km（北纬 40°），相对 0.02°（约 2.2 km）网格
是 1.8:1 的上采样，足够；z=8 瓦片数×4、下载量约 120 MB，不必要。

瓦片组织（Web Mercator / EPSG:3857 瓦片号）
------------------------------------------
z=7 时全球 128×128 瓦片。中国范围 72–136°E / 15–55°N 对应 x=89..112、y=40..58，
共 24×19 = 456 片。全部拼成 (19*256)×(24*256) 的 int16 墨卡托高程拼图（约 60 MB
内存），再逐格点双线性采样到等经纬度网格。

断点续传
--------
每片 PNG 缓存到 ``--cache-dir``（默认 ``%TEMP%\\rocketkit_terrarium_cache``），
已存在且 PNG 头合法则跳过；重跑不会重新下载。

用法
----
    python engine\\tools\\build_coarse_dem.py --out app\\data\\china_dem_0p02.npz
    python engine\\tools\\build_coarse_dem.py --dry-run            # 只看计划
    python engine\\tools\\build_coarse_dem.py --zoom 8 --workers 12
    python engine\\tools\\build_coarse_dem.py --step-deg 0.025     # 更小体积

产物
----
npz 内容：``lat``(1D float32, 纬度格心, 升序)、``lon``(1D float32)、
``elev``(ny, nx int16, 米, 行=lat, 列=lon；负值=海底深度)、``meta``(json 字符串)。
读取端见 ``app/core/coarse_dem.py``（只依赖 numpy + 标准库，无网络）。

许可
----
Mapzen Terrain Tiles / AWS Open Data：public domain（见
https://registry.opendata.aws/terrain-tiles/）。派生网格随本程序分发无授权限制。
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
URL_TEMPLATE = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
DEFAULT_BOUNDS = dict(lat_min=15.0, lat_max=55.0, lon_min=72.0, lon_max=136.0)
DEFAULT_ZOOM = 7
DEFAULT_STEP_DEG = 0.02
DEFAULT_MAX_MB = 25.0
DEFAULT_WORKERS = 6
TILE_PX = 256
PNG_SIG = b"\x89PNG\r\n\x1a\n"
USER_AGENT = "rocketkit-v3-build-coarse-dem/1.0 (offline DEM fallback builder)"

#: 日志文件（进度同时写文件，便于后台运行时用 tail 观察）
_LOG_FH = None


def _say(msg: str) -> None:
    """print + 可选写日志文件（UTF-8，逐行 flush）。"""
    print(msg, flush=True)
    if _LOG_FH is not None:
        try:
            _LOG_FH.write(msg + "\n")
            _LOG_FH.flush()
        except Exception:                    # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# HTTP 打开器：优先**直连**（绕过系统代理），失败再退回系统默认（含代理）
# --------------------------------------------------------------------------- #
_OPENERS = None


def _openers():
    """返回 [直连 opener, 系统默认 opener]（首次调用时创建）。"""
    global _OPENERS
    if _OPENERS is None:
        _OPENERS = [urllib.request.build_opener(urllib.request.ProxyHandler({})),
                    urllib.request.build_opener()]
    return _OPENERS


def _http_get(url: str, timeout: float) -> bytes:
    """GET 一个 URL：先直连，失败再走系统代理设置。"""
    last_err = None
    for opener in _openers():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with opener.open(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:             # noqa: BLE001 - 网络错误交给下一 opener
            last_err = exc
    raise last_err if last_err is not None else IOError("无可用 HTTP 连接")


# --------------------------------------------------------------------------- #
# 瓦片编号与坐标换算（Web Mercator, EPSG:3857）
# --------------------------------------------------------------------------- #
def lon_to_gx(lon_deg, zoom: int):
    """经度 → 全局像素坐标（像素 p 覆盖 [p, p+1)，格心在 p+0.5）。

    支持标量与数组（数组输入用于整条网格重采样）。
    """
    n = float(TILE_PX << zoom)
    out = (np.asarray(lon_deg, dtype=float) + 180.0) / 360.0 * n
    return out if out.ndim else float(out)


def lat_to_gy(lat_deg, zoom: int):
    """纬度 → 全局像素坐标（Web Mercator，支持标量与数组）。"""
    n = float(TILE_PX << zoom)
    lat = np.clip(np.asarray(lat_deg, dtype=float), -85.05112878, 85.05112878)
    out = (1.0 - np.arcsinh(np.tan(np.radians(lat))) / np.pi) / 2.0 * n
    return out if out.ndim else float(out)


def tile_range(zoom: int, bounds: dict) -> tuple[int, int, int, int]:
    """覆盖经纬度范围所需的瓦片号范围 (x0, x1, y0, y1)，含端点。"""
    n = 1 << zoom
    x0 = int(math.floor(lon_to_gx(bounds["lon_min"], zoom) / TILE_PX))
    x1 = int(math.floor(lon_to_gx(bounds["lon_max"], zoom) / TILE_PX))
    y0 = int(math.floor(lat_to_gy(bounds["lat_max"], zoom) / TILE_PX))   # 北 → 小 y
    y1 = int(math.floor(lat_to_gy(bounds["lat_min"], zoom) / TILE_PX))
    x0, x1 = max(0, min(x0, n - 1)), max(0, min(x1, n - 1))
    y0, y1 = max(0, min(y0, n - 1)), max(0, min(y1, n - 1))
    return x0, x1, y0, y1


def tile_url(zoom: int, x: int, y: int) -> str:
    return URL_TEMPLATE.format(z=zoom, x=x, y=y)


def _valid_png(path: Path) -> bool:
    """缓存文件是否是完整 PNG（头合法且 > 1 kB）。截断文件返回 False。"""
    try:
        if path.stat().st_size < 1000:
            return False
        with open(path, "rb") as fh:
            return fh.read(8) == PNG_SIG
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# PNG 解码（Pillow 优先；无 Pillow 时给出明确指引，构建工具不是运行期依赖）
# --------------------------------------------------------------------------- #
def decode_terrarium_png(data: bytes) -> np.ndarray:
    """terrarium PNG 字节 → int16 高程网格 (256, 256) [m]。"""
    try:
        from PIL import Image            # 构建工具依赖；运行期读取 npz 不需要
    except Exception as exc:            # noqa: BLE001
        raise RuntimeError(
            "构建粗 DEM 需要 Pillow（pip install Pillow）；"
            "运行时读取 npz 不需要 Pillow") from exc
    with Image.open(io.BytesIO(data)) as im:
        rgb = np.asarray(im.convert("RGB"), dtype=np.int32)
    if rgb.shape != (TILE_PX, TILE_PX, 3):
        raise ValueError(f"terrarium 瓦片尺寸异常 {rgb.shape}，期望 (256,256,3)")
    h = (rgb[:, :, 0] * 256 + rgb[:, :, 1] + rgb[:, :, 2] / 256.0) - 32768.0
    return np.rint(h).astype(np.int16)


# --------------------------------------------------------------------------- #
# 下载（断点续传 + 重试）
# --------------------------------------------------------------------------- #
def download_tile(zoom: int, x: int, y: int, cache_dir: Path,
                  retries: int = 4, timeout: float = 40.0) -> tuple[Path, int, str]:
    """下载单瓦片到缓存目录（幂等）。

    Returns
    -------
    (path, bytes, status) : status ∈ {'cached', 'downloaded'}
    """
    path = cache_dir / f"z{zoom}" / f"{x}" / f"{y}.png"
    if _valid_png(path):
        return path, path.stat().st_size, "cached"
    path.parent.mkdir(parents=True, exist_ok=True)
    url = tile_url(zoom, x, y)
    part = path.with_suffix(".png.part")
    last_err = ""
    for attempt in range(retries):
        try:
            blob = _http_get(url, timeout=timeout)
            if len(blob) < 1000 or blob[:8] != PNG_SIG:
                raise IOError(f"响应不是有效 PNG（{len(blob)} B）")
            with open(part, "wb") as fh:
                fh.write(blob)
            part.replace(path)
            return path, len(blob), "downloaded"
        except Exception as exc:            # noqa: BLE001 - 网络错误统一重试
            last_err = f"{type(exc).__name__}: {exc}"
            part.unlink(missing_ok=True)
            time.sleep(min(0.8 * (attempt + 1), 5.0))
    raise IOError(f"瓦片 z{zoom}/{x}/{y} 下载失败：{last_err}")


# --------------------------------------------------------------------------- #
# 拼图 + 重采样
# --------------------------------------------------------------------------- #
def build_mosaic(zoom: int, bounds: dict, cache_dir: Path, workers: int = DEFAULT_WORKERS,
                 retries: int = 4, progress: bool = True) -> np.ndarray:
    """下载并拼成墨卡托高程拼图 (ny*256, nx*256) int16。"""
    x0, x1, y0, y1 = tile_range(zoom, bounds)
    nx, ny = x1 - x0 + 1, y1 - y0 + 1
    total = nx * ny
    mosaic = np.zeros((ny * TILE_PX, nx * TILE_PX), dtype=np.int16)
    tiles = [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
    t0 = time.time()
    done = 0
    bytes_total = 0
    n_cached = 0
    next_pct = 5

    def _one(xy):
        return download_tile(zoom, xy[0], xy[1], cache_dir, retries=retries)

    if progress:
        _say(f"[build] 瓦片计划 z={zoom}: x {x0}..{x1} ({nx}) × y {y0}..{y1} ({ny}) "
             f"= {total} 片")
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(_one, xy): xy for xy in tiles}
        for fut in as_completed(futs):
            path, nbytes, status = fut.result()
            x, y = futs[fut]
            data = path.read_bytes()
            hgt = decode_terrarium_png(data)
            iy, ix = y - y0, x - x0
            mosaic[iy * TILE_PX:(iy + 1) * TILE_PX,
                   ix * TILE_PX:(ix + 1) * TILE_PX] = hgt
            done += 1
            bytes_total += nbytes
            n_cached += 1 if status == "cached" else 0
            pct = done * 100 // total
            if progress and (pct >= next_pct or done == total):
                el = max(time.time() - t0, 1e-3)
                _say(f"[progress {done}/{total}] {pct:3d}%  "
                     f"缓存命中 {n_cached}，累计下载 {bytes_total / 1e6:5.1f} MB，"
                     f"{done / el:4.1f} 片/s，已用 {el:4.0f} s")
                while next_pct <= pct:
                    next_pct += 5
    el = time.time() - t0
    if progress:
        _say(f"[build] 拼图完成 {mosaic.shape} int16，"
             f"高程范围 {int(mosaic.min())}…{int(mosaic.max())} m，"
             f"下载 {bytes_total / 1e6:.1f} MB，耗时 {el:.0f} s")
    return mosaic


def resample_grid(mosaic: np.ndarray, zoom: int, bounds: dict,
                  step_deg: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """墨卡托拼图 → 等经纬度网格（双线性，格心在地理范围内部）。

    Returns
    -------
    (lat, lon, elev) : lat (ny,) float64 升序、lon (nx,) float64、elev (ny,nx) int16
    """
    lat_min = bounds["lat_min"]
    lat_max = bounds["lat_max"]
    lon_min = bounds["lon_min"]
    lon_max = bounds["lon_max"]
    ny = int(round((lat_max - lat_min) / step_deg))
    nx = int(round((lon_max - lon_min) / step_deg))
    lat = lat_min + (np.arange(ny) + 0.5) * step_deg
    lon = lon_min + (np.arange(nx) + 0.5) * step_deg

    # 全局像素坐标（像素 p 中心在 p+0.5）→ **局部**拼图数组下标
    x0, _x1, y0, _y1 = tile_range(zoom, bounds)
    gx = lon_to_gx(lon, zoom) - 0.5 - x0 * TILE_PX
    gy = lat_to_gy(lat, zoom) - 0.5 - y0 * TILE_PX
    gh, gw = mosaic.shape
    c0 = np.clip(np.floor(gx).astype(np.intp), 0, gw - 2)
    r0 = np.clip(np.floor(gy).astype(np.intp), 0, gh - 2)
    fc = np.clip(gx - c0, 0.0, 1.0)
    fr = np.clip(gy - r0, 0.0, 1.0)

    v = mosaic.astype(np.float32)
    top = v[r0][:, c0] * (1.0 - fc)[None, :] + v[r0][:, c0 + 1] * fc[None, :]
    bot = v[r0 + 1][:, c0] * (1.0 - fc)[None, :] + v[r0 + 1][:, c0 + 1] * fc[None, :]
    elev = top * (1.0 - fr)[:, None] + bot * fr[:, None]
    return lat, lon, np.rint(elev).astype(np.int16)


def build_npz(out_path: Path, lat: np.ndarray, lon: np.ndarray, elev: np.ndarray,
              meta: dict) -> int:
    """写压缩 npz（幂等），返回字节数。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 注意：np.savez_compressed 只在文件名不以 .npz 结尾时才追加 .npz，
    # 因此临时名也必须以 .npz 结尾（否则 replace 找不到文件）。
    tmp = out_path.with_name(out_path.name[:-len(".npz")] + ".part.npz")
    np.savez_compressed(
        tmp,
        lat=np.asarray(lat, dtype=np.float32),
        lon=np.asarray(lon, dtype=np.float32),
        elev=np.asarray(elev, dtype=np.int16),
        meta=np.str_(json.dumps(meta, ensure_ascii=False, indent=1)),
    )
    tmp.replace(out_path)
    return out_path.stat().st_size


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    app_root = Path(__file__).resolve().parents[2]      # <项目根>/engine/tools/.. → engine → 项目根
    default_out = app_root / "app" / "data" / "china_dem_0p02.npz"
    ap = argparse.ArgumentParser(
        description="构建内嵌中国范围 0.02° 粗 DEM（terrarium 地形瓦片 → npz）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--out", default=str(default_out), help="输出 npz 路径")
    ap.add_argument("--lat-min", type=float, default=DEFAULT_BOUNDS["lat_min"])
    ap.add_argument("--lat-max", type=float, default=DEFAULT_BOUNDS["lat_max"])
    ap.add_argument("--lon-min", type=float, default=DEFAULT_BOUNDS["lon_min"])
    ap.add_argument("--lon-max", type=float, default=DEFAULT_BOUNDS["lon_max"])
    ap.add_argument("--zoom", type=int, default=DEFAULT_ZOOM, help="terrarium 瓦片级数（7≈1.2 km/px）")
    ap.add_argument("--step-deg", type=float, default=DEFAULT_STEP_DEG, help="输出网格分辨率 [deg]")
    ap.add_argument("--max-mb", type=float, default=DEFAULT_MAX_MB, help="输出文件体积上限 [MB]")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="并发下载线程数")
    ap.add_argument("--retries", type=int, default=4, help="单瓦片重试次数")
    ap.add_argument("--cache-dir", default=None,
                    help="瓦片缓存目录（断点续传；默认 %%TEMP%%/rocketkit_terrarium_cache）")
    ap.add_argument("--log", default=None, help="同时把进度写入该日志文件（后台运行时看进度）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不下载")
    ap.add_argument("--quiet", action="store_true", help="少打印")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                        # noqa: BLE001
        pass

    global _LOG_FH
    if args.log:
        log_path = Path(args.log)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _LOG_FH = open(log_path, "w", encoding="utf-8")

    bounds = dict(lat_min=min(args.lat_min, args.lat_max),
                  lat_max=max(args.lat_min, args.lat_max),
                  lon_min=min(args.lon_min, args.lon_max),
                  lon_max=max(args.lon_min, args.lon_max))
    out_path = Path(args.out)
    cache_dir = Path(args.cache_dir) if args.cache_dir else (
        Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".") /
        "rocketkit_terrarium_cache")
    x0, x1, y0, y1 = tile_range(args.zoom, bounds)
    n_tiles = (x1 - x0 + 1) * (y1 - y0 + 1)
    ny = int(round((bounds["lat_max"] - bounds["lat_min"]) / args.step_deg))
    nx = int(round((bounds["lon_max"] - bounds["lon_min"]) / args.step_deg))

    _say(f"[build] 粗 DEM 构建：{bounds['lat_min']}–{bounds['lat_max']}°N, "
         f"{bounds['lon_min']}–{bounds['lon_max']}°E, z={args.zoom}, "
         f"输出 {args.step_deg}° ({nx}×{ny} 格点)")
    _say(f"[build] 瓦片 {n_tiles} 片，缓存目录 {cache_dir}")
    _say(f"[build] 输出 {out_path}（上限 {args.max_mb:.0f} MB）")
    if args.dry_run:
        _say(f"[dry-run] x {x0}..{x1}, y {y0}..{y1}，示例 URL: "
             f"{tile_url(args.zoom, x0, y0)}")
        _say(f"[dry-run] 预计下载 {n_tiles} 片 × 约 80 kB ≈ {n_tiles * 80 / 1024:.0f} MB")
        if _LOG_FH is not None:
            _LOG_FH.close()
        return 0

    t_start = time.time()
    mosaic = build_mosaic(args.zoom, bounds, cache_dir, workers=args.workers,
                          retries=args.retries, progress=not args.quiet)

    # 逐次降采样直到体积达标（正常 0.02° 只需一轮）
    step = float(args.step_deg)
    for attempt in range(6):
        lat, lon, elev = resample_grid(mosaic, args.zoom, bounds, step)
        meta = {
            "schema": "rocketkit.coarse_dem.v1",
            "product": "Mapzen/AWS Terrain Tiles (terrarium, z=%d) 拼图降采样" % args.zoom,
            "source": URL_TEMPLATE,
            "source_note": "AWS Open Data elevation-tiles-prod/terrarium，terrarium 编码 "
                           "h=(R*256+G+B/256)-32768 m；海洋为 ETOPO1 海底地形（负值=水深）",
            "license": "public domain (Mapzen Terrain Tiles / AWS Open Data; SRTM/ASTER/ETOPO1 公有领域)",
            "vertical_datum": "EGM96 正高（与 SRTMGL1 同口径；当作 WGS-84 椭球高使用，"
                              "中国区域两者相差约 -10~-40 m，对遮挡角影响近场 <0.1°）",
            "zoom": int(args.zoom),
            "resolution_deg": float(step),
            "resolution_km_ns": round(float(step) * 111.19, 3),
            "resolution_km_ew_35n": round(float(step) * 111.19 * math.cos(math.radians(35.0)), 3),
            "coverage": dict(lat_min=bounds["lat_min"], lat_max=bounds["lat_max"],
                             lon_min=bounds["lon_min"], lon_max=bounds["lon_max"]),
            "grid_shape": [int(elev.shape[0]), int(elev.shape[1])],
            "grid_note": "lat/lon 为格心坐标（半个 step 偏移），行=纬度升序，列=经度升序",
            "dtype": "int16 米（负值=海底深度，遮挡计算按海平面裁剪）",
            "created_utc": utc_now(),
            "tile_range": dict(x0=x0, x1=x1, y0=y0, y1=y1, zoom=int(args.zoom)),
            "builder": "engine/tools/build_coarse_dem.py",
            "accuracy_note": "0.02°（约 2 km）网格对 1″ SRTM 点位 RMSE 约数十~数百米，"
                             "地形遮挡角误差约 0.2–1.5°；城市高楼/近场（<1 km）遮挡不可用",
        }
        size = build_npz(out_path, lat, lon, elev, meta)
        mb = size / 1e6
        _say(f"[build] 写盘 {out_path.name}: {elev.shape[0]}×{elev.shape[1]} 格点, "
             f"{mb:.2f} MB（上限 {args.max_mb:.0f} MB, 第 {attempt + 1} 轮）")
        if mb <= args.max_mb:
            break
        step = round(step * 1.5, 6)
        _say(f"[build] 超过体积上限 → 降采样到 {step}° 重试")
    else:
        _say(f"[build] 警告：6 轮降采样后仍超过 {args.max_mb:.0f} MB（{mb:.2f} MB）")

    # 自检：重新读回，检查格点/数值（不依赖 Pillow，模拟运行期读取）
    with np.load(out_path, allow_pickle=False) as z:
        lat2, lon2, elev2 = z["lat"], z["lon"], z["elev"]
        meta2 = json.loads(str(z["meta"]))
    assert elev2.dtype == np.int16 and elev2.ndim == 2
    assert lat2.size == elev2.shape[0] and lon2.size == elev2.shape[1]
    assert np.isfinite(lat2).all() and np.isfinite(lon2).all()
    finite = float(np.mean(elev2 > -12000))
    _say(f"[check] 回读 OK: {elev2.shape}, {elev2.dtype}, "
         f"lat {lat2[0]:.3f}..{lat2[-1]:.3f}, lon {lon2[0]:.3f}..{lon2[-1]:.3f}, "
         f"elev {int(elev2.min())}..{int(elev2.max())} m, 有效格点 {finite * 100:.1f}%")
    _say(f"[check] 抽查: 北京(39.9,116.4)="
         f"{int(elev2[int((39.9 - bounds['lat_min']) / step), int((116.4 - bounds['lon_min']) / step)])} m, "
         f"太原(37.76,112.73)="
         f"{int(elev2[int((37.76 - bounds['lat_min']) / step), int((112.73 - bounds['lon_min']) / step)])} m, "
         f"珠峰(27.99,86.93)="
         f"{int(elev2[int((27.99 - bounds['lat_min']) / step), int((86.93 - bounds['lon_min']) / step)])} m")
    _say(f"[done] {out_path}  {out_path.stat().st_size / 1e6:.2f} MB  "
         f"耗时 {time.time() - t_start:.0f} s  meta.created_utc={meta2['created_utc']}")
    if _LOG_FH is not None:
        _LOG_FH.close()
        _LOG_FH = None
    return 0


if __name__ == "__main__":
    sys.exit(main())
