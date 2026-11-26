#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SRTMGL1 (1 arc-second) HGT 瓦片下载与解压（AWS Open Data，无需密钥）。

数据源
------
    https://s3.amazonaws.com/elevation-tiles-prod/skadi/<NAM>/<NAME>.hgt.gz
其中 NAME = N19E110（SW 角点经纬度取整，N/S + 2 位纬度, E/W + 3 位经度）。

瓦片格式
--------
1° x 1° 覆盖，3601 x 3601 个 int16 **大端** 样本，行序自北向南（第 0 行 = 瓦片北边），
采样间隔 1 arc-second（赤道上 ~30.9 m，北纬 19° 处 ~29.2 m）→ SRTMGL1 = "1 弧秒/约 30 m"。
高程单位米，为 EGM96 大地水准面正高（不是 WGS-84 椭球高，两者在本区域相差约 -20 m 量级）。
空值 void = -32768。

用法
----
    python _tools/fetch_dem.py                # 下载 + 解压任务区域全部瓦片
    python _tools/fetch_dem.py --workers 8    # 并发数
    python _tools/fetch_dem.py --dry-run      # 只打印下载计划
    python _tools/fetch_dem.py --list         # 只打印本地清单状态
    python _tools/fetch_dem.py --region 16 21 106 113

幂等
----
* `data/dem/gz/<tile>.hgt.gz` 已存在且大小 > 1 MB 时不重复下载；
* `data/dem/hgt/<tile>.hgt` 已存在且大小 == 25934402 字节时不重复解压；
* 清单 `data/dem/srtm_tiles.json` 每次运行都会按本地文件状态重写（不会丢信息）。

输出
----
    data/dem/gz/<tile>.hgt.gz      压缩包（保留，便于重跑缓存而不必重新下载）
    data/dem/hgt/<tile>.hgt        解压后的原始瓦片（内存映射读取）
    data/dem/srtm_tiles.json       瓦片清单：tile / 经纬度 / URL / 字节数 / sha256
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import gzip
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(os.environ.get("RK_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DEM = Path(os.environ.get("RK_DEM") or (ROOT / "data" / "dem"))
DIR_GZ = DATA_DEM / "gz"
DIR_HGT = DATA_DEM / "hgt"
MANIFEST = DATA_DEM / "srtm_tiles.json"
CONFIG = ROOT / "config.json"            # v2: 根目录 config.json（v1 误写为 common/config.json）
CONFIG_FALLBACK = ROOT / "common" / "config.json"

S3_BASE = "https://s3.amazonaws.com/elevation-tiles-prod/skadi"
N_SIDE = 3601                    # 3601 x 3601
HGT_BYTES = N_SIDE * N_SIDE * 2  # 25 934 402 字节
EARTH_R_KM = 6371.0088

# v2：默认不写死任务区；可用 config.dem.task_region = {lat_min,lat_max,lon_min,lon_max} 覆盖
TASK_REGION = dict(lat_min=0, lat_max=0, lon_min=0, lon_max=0)


# --------------------------------------------------------------------------- #
# 瓦片命名 / 计划
# --------------------------------------------------------------------------- #
def tile_name(lat_i: int, lon_i: int) -> str:
    """整数 SW 角点 → HGT 瓦片名，如 (19, 110) → 'N19E110'。"""
    ns = "N" if lat_i >= 0 else "S"
    ew = "E" if lon_i >= 0 else "W"
    return f"{ns}{abs(lat_i):02d}{ew}{abs(lon_i):03d}"


def tile_url(name: str) -> str:
    """瓦片名 → S3 URL（skadi 目录按纬度带分桶，如 N19/N19E110.hgt.gz）。"""
    return f"{S3_BASE}/{name[:3]}/{name}.hgt.gz"


def tiles_for_circle(lat: float, lon: float, radius_km: float):
    """圆覆盖的经纬度包围盒（球面近似），返回 (lat_min, lat_max, lon_min, lon_max)。"""
    dlat = math.degrees(radius_km / EARTH_R_KM)
    coslat = max(0.02, math.cos(math.radians(lat)))
    dlon = math.degrees(radius_km / (EARTH_R_KM * coslat))
    return lat - dlat, lat + dlat, lon - dlon, lon + dlon


def plan_tiles(radius_km: float | None = None, config_path: str | None = None):
    """计算需要下载的瓦片集合：观测点/发射场半径覆盖 + 可选任务区 + 航警区包围盒。

    v2 修正：v1 用「纬度集合 × 经度集合」的笛卡尔积，圆形覆盖会多下 2~3 倍瓦片；
    这里按每个圆形/矩形各自展开后取并集。配置路径也改为 ``<root>/config.json``（可用 --config 覆盖）。

    返回 (sorted tile names, notes dict)。
    """
    cfg_path = Path(config_path) if config_path else (CONFIG if CONFIG.exists() else CONFIG_FALLBACK)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    if radius_km is None:
        radius_km = float(cfg.get("dem", {}).get("horizon_radius_km", 200.0))

    tiles: set[str] = set()
    notes: dict = {}

    def add_box(la0, la1, lo0, lo1):
        for i in range(math.floor(la0), math.floor(la1) + 1):
            for j in range(math.floor(lo0), math.floor(lo1) + 1):
                tiles.add(tile_name(i, j))

    # (1) 可选：config.dem.task_region 指定的瓦片索引范围
    tr = (cfg.get("dem") or {}).get("task_region")
    if tr:
        for la in range(int(tr["lat_min"]), int(tr["lat_max"]) + 1):
            for lo in range(int(tr["lon_min"]), int(tr["lon_max"]) + 1):
                tiles.add(tile_name(la, lo))
        notes["task_region"] = dict(tr)

    # (2) 观测点半径圆 + 发射场
    pts = [(o["id"], o["lat_deg"], o["lon_deg"]) for o in cfg.get("observers", [])]
    site = (cfg.get("mission") or {}).get("launch_site") or cfg.get("launch_site") or {}
    if site.get("lat_deg") is not None:
        pts.append(("launch_site", site["lat_deg"], site["lon_deg"]))
    circle_boxes = {}
    for pid, la, lo in pts:
        la0, la1, lo0, lo1 = tiles_for_circle(la, lo, radius_km)
        circle_boxes[pid] = [round(la0, 4), round(la1, 4), round(lo0, 4), round(lo1, 4)]
        add_box(la0, la1, lo0, lo1)
    notes["circle_radius_km"] = radius_km
    notes["circle_boxes"] = circle_boxes

    # (3) 航警区包围盒（落区局部判读图用，外扩 10 km）
    notam_boxes = {}
    for z in cfg.get("notams", []):
        poly = np.asarray(z.get("polygon_latlon") or [], dtype=float)
        if poly.size < 6:
            continue
        la0, lo0 = poly.min(axis=0)
        la1, lo1 = poly.max(axis=0)
        dlat = 10.0 / 111.19
        dlon = 10.0 / (111.19 * max(0.05, math.cos(math.radians((la0 + la1) / 2))))
        notam_boxes[z["id"]] = [round(la0 - dlat, 4), round(la1 + dlat, 4),
                                round(lo0 - dlon, 4), round(lo1 + dlon, 4)]
        add_box(la0 - dlat, la1 + dlat, lo0 - dlon, lo1 + dlon)
    notes["notam_boxes"] = notam_boxes

    return sorted(tiles), notes


# --------------------------------------------------------------------------- #
# 下载 / 解压
# --------------------------------------------------------------------------- #
def _gzip_hgt_size(path: Path) -> int:
    """完整解压 gzip 并返回解压后的字节数（用于判断 .part/.gz 是否完整）；失败返回 -1。"""
    try:
        n = 0
        with gzip.open(path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 22)
                if not chunk:
                    break
                n += len(chunk)
        return n
    except Exception:              # noqa: BLE001 - 截断/CRC 错误都算不完整
        return -1


def _download_one(name: str, retries: int = 4, timeout: float = 45.0) -> dict:
    """下载单个瓦片压缩包到 data/dem/gz/，并校验 gzip 完整性。

    说明：SRTM 海上瓦片压缩后可能只有 ~200 kB（不是固定 7.7 MB），因此**不能**按
    固定体积判断成败；这里统一用"解压后样本字节数 == 3601×3601×2"作为完整性判据。
    上次运行遗留的 ``<tile>.hgt.gz.part`` 若已完整会直接转正（避免重下）。
    """
    url = tile_url(name)
    gz_path = DIR_GZ / f"{name}.hgt.gz"
    part = DIR_GZ / f"{name}.hgt.gz.part"
    rec = {"tile": name, "url": url, "status": "", "gz_bytes": 0, "gz_sha256": "",
           "hgt_bytes": 0}

    if gz_path.exists():
        if _gzip_hgt_size(gz_path) == HGT_BYTES:
            rec["status"] = "gz_existing"
            rec["gz_bytes"] = gz_path.stat().st_size
            return rec
        gz_path.unlink()                      # 坏包，重下

    # 上次遗留的 .part：完整则直接转正
    if part.exists():
        if _gzip_hgt_size(part) == HGT_BYTES:
            part.replace(gz_path)
            rec["status"] = "downloaded(recovered_part)"
            rec["gz_bytes"] = gz_path.stat().st_size
            return rec
        part.unlink()

    last_err = ""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "cz8a-geo-infra/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                clen = resp.headers.get("Content-Length")
                expect = int(clen) if clen else None
                with open(part, "wb") as fh:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        fh.write(chunk)
            size = part.stat().st_size
            if size < 10_000:
                raise IOError(f"文件过小 {size} B")
            if expect is not None and size != expect:
                raise IOError(f"字节数不符 got={size} expect={expect}")
            if _gzip_hgt_size(part) != HGT_BYTES:
                raise IOError("gzip 解压长度不符（截断/损坏）")
            part.replace(gz_path)
            rec["status"] = "downloaded"
            rec["gz_bytes"] = gz_path.stat().st_size
            return rec
        except Exception as exc:              # noqa: BLE001 - 网络错误统一重试
            last_err = f"{type(exc).__name__}: {exc}"
            part.unlink(missing_ok=True)      # 不保留半截，避免下次误判
            time.sleep(min(1.0 * (attempt + 1), 6.0))
    rec["status"] = "failed"
    rec["error"] = last_err
    return rec



def _sha256(path: Path, limit: int | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 22)
            if not chunk:
                break
            h.update(chunk)
            if limit is not None and fh.tell() > limit:
                break
    return h.hexdigest()


def _extract_one(name: str) -> dict:
    """解压 .hgt.gz → .hgt（幂等）。解压同时验证 gzip CRC 与样本字节数。

    解压失败（截断/损坏）时删除坏的 .gz，返回 status 便于外层重下。
    """
    gz_path = DIR_GZ / f"{name}.hgt.gz"
    hgt_path = DIR_HGT / f"{name}.hgt"
    rec = {"tile": name, "hgt_bytes": 0, "status": ""}
    if hgt_path.exists() and hgt_path.stat().st_size == HGT_BYTES:
        rec["status"] = "hgt_existing"
        rec["hgt_bytes"] = HGT_BYTES
        return rec
    if not gz_path.exists():
        rec["status"] = "missing_gz"
        return rec
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
    except Exception as exc:  # noqa: BLE001 - 截断/CRC 错误
        tmp.unlink(missing_ok=True)
        gz_path.unlink(missing_ok=True)       # 坏包删掉，下次重下
        rec["status"] = f"bad_gz({type(exc).__name__}: {exc})"
        return rec
    if n != HGT_BYTES:
        tmp.unlink(missing_ok=True)
        gz_path.unlink(missing_ok=True)
        rec["status"] = f"bad_size({n})"
        return rec
    tmp.replace(hgt_path)
    rec["status"] = "extracted"
    rec["hgt_bytes"] = n
    return rec


def tile_latlon_sw(name: str):
    """瓦片名 → SW 角点整数经纬度 (lat_i, lon_i)。"""
    lat_i = int(name[1:3]) * (1 if name[0] == "N" else -1)
    ew = name[4]
    lon_i = int(name[5:8]) * (1 if ew == "E" else -1)
    return lat_i, lon_i


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="下载并解压 SRTMGL1 (1\"≈30 m) HGT 瓦片（AWS Open Data）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--region", nargs=4, type=int, metavar=("LATMIN", "LATMAX", "LONMIN", "LONMAX"),
                    default=None, help="覆盖区瓦片索引（含端点），默认取任务给定区域")
    ap.add_argument("--radius-km", type=float, default=None,
                    help="观测点覆盖半径 km，默认读 config.demo.horizon_radius_km")
    ap.add_argument("--config", default=None, help="任务配置（默认 <root>/config.json）")
    ap.add_argument("--workers", type=int, default=8, help="并发下载线程数")
    ap.add_argument("--retries", type=int, default=4, help="单瓦片重试次数")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不下载")
    ap.add_argument("--list", action="store_true", help="只打印本地清单状态")
    ap.add_argument("--keep-gz", dest="keep_gz", action="store_true", default=True,
                    help="保留下载的 .gz（默认保留）")
    ap.add_argument("--no-keep-gz", dest="keep_gz", action="store_false",
                    help="解压后删除 .gz 以节省空间")
    args = ap.parse_args(argv)

    global TASK_REGION
    if args.region is not None:
        TASK_REGION = dict(lat_min=args.region[0], lat_max=args.region[1],
                           lon_min=args.region[2], lon_max=args.region[3])

    DIR_GZ.mkdir(parents=True, exist_ok=True)
    DIR_HGT.mkdir(parents=True, exist_ok=True)

    names, notes = plan_tiles(args.radius_km, args.config)
    print(f"[plan] 需要 {len(names)} 个 SRTMGL1 瓦片（1°/瓦片）", flush=True)
    print(f"[plan] 区域={notes.get('task_region', '（未配置，按观测点/发射场/航警区）')} "
          f"观测点圆半径={notes['circle_radius_km']:.0f} km", flush=True)
    for pid, box in notes["circle_boxes"].items():
        print(f"[plan]   {pid:12s} lat {box[0]:8.3f}..{box[1]:8.3f}  "
              f"lon {box[2]:9.3f}..{box[3]:9.3f}", flush=True)

    if args.list:
        for nm in names:
            hgt = DIR_HGT / f"{nm}.hgt"
            gz = DIR_GZ / f"{nm}.hgt.gz"
            print(f"  {nm}: hgt={'OK' if hgt.exists() else '-':2s} "
                  f"({hgt.stat().st_size if hgt.exists() else 0:>9d} B)  "
                  f"gz={'OK' if gz.exists() else '-':2s}", flush=True)
        return 0

    if args.dry_run:
        for nm in names:
            print(f"  {nm}  {tile_url(nm)}", flush=True)
        print(f"[dry-run] 预计下载 {len(names)} x ~7.7 MB ≈ {len(names) * 7.7:.0f} MB，"
              f"解压后 {len(names) * HGT_BYTES / 1e9:.2f} GB", flush=True)
        return 0

    t0 = time.time()
    results: dict[str, dict] = {}

    def _need_hgt(nm):
        hgt = DIR_HGT / f"{nm}.hgt"
        return not (hgt.exists() and hgt.stat().st_size == HGT_BYTES)

    def _download_round(todo, round_idx, workers):
        """下载一批瓦片（并发），返回 {tile: rec}。"""
        if not todo:
            return {}
        print(f"[dl] 第 {round_idx} 轮：需下载 {len(todo)} 个瓦片（并发 {workers}）", flush=True)
        out, done = {}, 0
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_download_one, nm, args.retries): nm for nm in todo}
            for fut in cf.as_completed(futs):
                rec = fut.result()
                out[rec["tile"]] = rec
                done += 1
                flag = "OK " if rec["status"] in ("downloaded", "gz_existing") else "ERR"
                print(f"[dl] {flag} {done:3d}/{len(todo)} {rec['tile']} "
                      f"{rec['gz_bytes'] / 1e6:6.2f} MB "
                      f"({rec.get('error', '')})", flush=True)
        return out

    # --- 1) 多轮：下载 → 解压校验 → 对失败的再下一轮 ---
    for rnd in range(1, 4):
        todo = [nm for nm in names if _need_hgt(nm)
                and not ((DIR_GZ / f"{nm}.hgt.gz").exists()
                         and (DIR_GZ / f"{nm}.hgt.gz").stat().st_size > 1_000_000)]
        results.update(_download_round(todo, rnd, args.workers))

        # 2) 解压 + gzip 完整性校验（失败会删除坏包）
        n_before = sum(1 for nm in names if not _need_hgt(nm))
        bad = []
        for nm in names:
            if _need_hgt(nm):
                rec = _extract_one(nm)
                if rec["status"] not in ("hgt_existing", "extracted"):
                    bad.append((nm, rec["status"]))
        n_ok = sum(1 for nm in names if not _need_hgt(nm))
        print(f"[ex] 第 {rnd} 轮解压/校验：可用 {n_ok}/{len(names)}"
              f"（本轮新增 {n_ok - n_before}）", flush=True)
        for nm, st in bad:
            print(f"[ex]   失败 {nm}: {st}", flush=True)
        if n_ok == len(names):
            break
        if not bad and not todo:
            break
        time.sleep(1.0)

    n_ok = sum(1 for nm in names if not _need_hgt(nm))
    print(f"[ex] 最终 {n_ok}/{len(names)} 瓦片可用", flush=True)

    # --- 3) 清单 ---
    tiles = []
    for nm in names:
        gz = DIR_GZ / f"{nm}.hgt.gz"
        hgt = DIR_HGT / f"{nm}.hgt"
        la_i, lo_i = tile_latlon_sw(nm)
        rec = {
            "tile": nm,
            "lat_sw": float(la_i),
            "lon_sw": float(lo_i),
            "lat_ne": float(la_i + 1),
            "lon_ne": float(lo_i + 1),
            "url": tile_url(nm),
            "gz_bytes": gz.stat().st_size if gz.exists() else 0,
            "hgt_bytes": hgt.stat().st_size if hgt.exists() else 0,
            "hgt_ok": hgt.exists() and hgt.stat().st_size == HGT_BYTES,
            "gz_sha256": "",
        }
        if gz.exists() and rec["gz_bytes"] > 1_000_000:
            rec["gz_sha256"] = (results.get(nm, {}).get("gz_sha256")
                                or _sha256(gz))
        tiles.append(rec)

    manifest = {
        "source": S3_BASE + "/<NAM>/<NAME>.hgt.gz",
        "dataset": "SRTMGL1 (NASA SRTM 1 arc-second, 地球正高 EGM96, 单位 m)",
        "license": "public domain (NASA/USGS; AWS Open Data hosting)",
        "url_template": S3_BASE + "/{latband}/{tile}.hgt.gz",
        "tile_format": {
            "size": [N_SIDE, N_SIDE],
            "dtype": "int16",
            "byteorder": "big",
            "row_order": "north_to_south",
            "bytes": HGT_BYTES,
            "sample_arcsec": 1,
            "sample_m_equator": 30.9,
            "void_value": -32768,
            "vertical_datum": "EGM96 geoid (orthometric height)",
        },
        "region": notes,
        "plan": {
            "n_tiles": len(names),
            "gz_bytes_total": sum(t["gz_bytes"] for t in tiles),
            "hgt_bytes_total": sum(t["hgt_bytes"] for t in tiles),
            "n_ok": n_ok,
        },
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_s": round(time.time() - t0, 1),
        "tiles": tiles,
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[done] {n_ok}/{len(names)} 瓦片可用, 压缩包 {manifest['plan']['gz_bytes_total'] / 1e6:.0f} MB, "
          f"解压 {manifest['plan']['hgt_bytes_total'] / 1e9:.2f} GB, 耗时 {manifest['elapsed_s']:.0f} s",
          flush=True)
    print(f"[done] 清单 → {MANIFEST}", flush=True)

    if not args.keep_gz:
        for nm in names:
            (DIR_GZ / f"{nm}.hgt.gz").unlink(missing_ok=True)
        print("[done] 已删除 .gz 压缩包", flush=True)

    return 0 if n_ok == len(names) else 1


if __name__ == "__main__":
    sys.exit(main())

