#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Natural Earth 底图数据下载 + 裁剪（v2.0 新增；v1 包内缺此脚本与 data/ne 数据）。

v1.0 的 ``common/boundaries.py`` 依赖 ``data/ne/*.geojson`` 才能运行，但包内既没有
这些文件，也没有生成它们的 ``_tools/fetch_ne.py``——``plot_map.py`` 直接崩在
``FileNotFoundError``。本脚本补齐这一环，流程与 ``boundaries.py`` 的约定一致：

    下载 4 个 Natural Earth shapefile 包（public domain）
      → pyshp 读取 → 按中国范围裁剪 / 精简属性
      → 写 data/ne/china_admin0.geojson   （CN/TW/HK/MO 要素）
        data/ne/china_admin1.geojson      （iso_a2 == CN 的省级要素）
        data/ne/china_cities.geojson      （人口前 120 的中国城市）
        data/ne/coastline_cn_bbox.geojson （中国及周边海岸线）

数据源：https://naciscdn.org/naturalearth/<scale>/<culture|physical>/<name>.zip
（Natural Earth 官方 CDN；public domain，无需密钥或注册）

用法
----
    python _tools/fetch_ne.py            # 下载 + 转换（幂等）
    python _tools/fetch_ne.py --list     # 只看本地状态
    python _tools/fetch_ne.py --keep-raw # 保留原始 zip

依赖：``pyshp``（pip install pyshp）；仅本脚本需要，运行主流程不需要。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(os.environ.get("RK_ROOT") or Path(__file__).resolve().parents[1])
DATA_NE = ROOT / "data" / "ne"
RAW = DATA_NE / "raw"

BASE = "https://naciscdn.org/naturalearth"
PACKS = [
    ("50m/cultural/ne_50m_admin_0_countries.zip", "ne_50m_admin_0_countries"),
    ("10m/cultural/ne_10m_admin_1_states_provinces.zip", "ne_10m_admin_1_states_provinces"),
    ("10m/cultural/ne_10m_populated_places.zip", "ne_10m_populated_places"),
    ("10m/physical/ne_10m_coastline.zip", "ne_10m_coastline"),
]
CHINA_ISO = {"CN", "TW", "HK", "MO"}
CHINA_A3 = {"CHN", "TWN", "HKG", "MAC"}
BBOX = (70.0, 10.0, 140.0, 60.0)      # 中国及周边（经度, 纬度）


# --------------------------------------------------------------------------- #
def download(url: str, dest: Path, retries: int = 4) -> Path:
    if dest.exists() and dest.stat().st_size > 100_000:
        print(f"[ne] 已存在 {dest.name}（{dest.stat().st_size/1e6:.1f} MB）")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    last = ""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "rocketkit-fetch-ne/2.0"})
            with urllib.request.urlopen(req, timeout=60) as resp, open(part, "wb") as fh:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
            if part.stat().st_size < 100_000:
                raise IOError("文件过小")
            part.replace(dest)
            print(f"[ne] 下载 {dest.name}  {dest.stat().st_size/1e6:.1f} MB")
            return dest
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
            part.unlink(missing_ok=True)
            time.sleep(1.0 + attempt)
    raise RuntimeError(f"下载失败 {url}: {last}")


def _unzip(zip_path: Path) -> Path:
    out = RAW / zip_path.stem
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(out)
    return out


def _reader(base_dir: Path, name: str):
    import shapefile  # pyshp
    shp = base_dir / f"{name}.shp"
    if not shp.exists():
        cand = list(base_dir.glob("*.shp"))
        if not cand:
            raise FileNotFoundError(f"{base_dir} 内没有 .shp")
        shp = cand[0]
    return shapefile.Reader(str(shp))


def _props(rec) -> dict:
    d = rec.as_dict()
    return {k: v for k, v in d.items()}


def _rings_from_shape(shp):
    """shapefile 多边形/折线 → 每 part 一个点列（(lon, lat) 元组列表）。"""
    pts = shp.points
    parts = list(shp.parts) + [len(pts)]
    out = []
    for i in range(len(parts) - 1):
        seg = pts[parts[i]:parts[i + 1]]
        if len(seg) >= 3:
            out.append([[float(x), float(y)] for x, y in seg])
    return out


def _bbox_hit(ring, bbox) -> bool:
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    x0, y0, x1, y1 = bbox
    return not (max(xs) < x0 or min(xs) > x1 or max(ys) < y0 or min(ys) > y1)


def build_admin0() -> dict:
    rd = _reader(RAW / "ne_50m_admin_0_countries", "ne_50m_admin_0_countries")
    feats = []
    for sr in rd.iterShapeRecords():
        pr = _props(sr.record)
        iso = str(pr.get("ISO_A2") or "").upper()
        a3 = str(pr.get("ADM0_A3") or "").upper()
        name = str(pr.get("NAME") or "")
        if iso not in CHINA_ISO and a3 not in CHINA_A3 and "china" not in name.lower():
            continue
        rings = _rings_from_shape(sr.shape)
        if not rings:
            continue
        feats.append({
            "type": "Feature",
            "properties": {"NAME": name, "NAME_ZH": pr.get("NAME_ZH") or "",
                           "ISO_A2": iso, "ADM0_A3": a3},
            "geometry": {"type": "MultiPolygon",
                         "coordinates": [[r] for r in rings]},
        })
    return {"type": "FeatureCollection",
            "source": "Natural Earth 1:50m admin_0_countries (public domain)",
            "features": feats}


def build_admin1() -> dict:
    rd = _reader(RAW / "ne_10m_admin_1_states_provinces", "ne_10m_admin_1_states_provinces")
    feats = []
    for sr in rd.iterShapeRecords():
        pr = _props(sr.record)
        a3 = str(pr.get("adm0_a3") or pr.get("ADM0_A3") or "").upper()
        iso = str(pr.get("iso_a2") or pr.get("ISO_A2") or "").upper()
        if a3 != "CHN" and iso != "CN":
            continue
        rings = _rings_from_shape(sr.shape)
        if not rings:
            continue
        feats.append({
            "type": "Feature",
            "properties": {"name": pr.get("name") or pr.get("NAME") or "",
                           "name_zh": pr.get("name_zh") or pr.get("NAME_ZH") or ""},
            "geometry": {"type": "MultiPolygon", "coordinates": [[r] for r in rings]},
        })
    return {"type": "FeatureCollection",
            "source": "Natural Earth 1:10m admin_1_states_provinces (public domain)",
            "features": feats}


def build_cities(top_n: int = 120) -> dict:
    rd = _reader(RAW / "ne_10m_populated_places", "ne_10m_populated_places")
    rows = []
    for sr in rd.iterShapeRecords():
        pr = _props(sr.record)
        a3 = str(pr.get("ADM0_A3") or pr.get("SOV_A3") or "").upper()
        if a3 not in CHINA_A3 and str(pr.get("ISO_A2") or "").upper() not in CHINA_ISO:
            continue
        lat = pr.get("LATITUDE", pr.get("LAT"))
        lon = pr.get("LONGITUDE", pr.get("LON"))
        if lat is None or lon is None:
            continue
        rows.append({"type": "Feature",
                     "properties": {"NAME": pr.get("NAME") or "",
                                    "NAME_ZH": pr.get("NAME_ZH") or "",
                                    "POP_MAX": int(pr.get("POP_MAX") or 0),
                                    "LAT": float(lat), "LON": float(lon),
                                    "ADM0_A3": a3},
                     "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]}})
    rows.sort(key=lambda f: -f["properties"]["POP_MAX"])
    rows = rows[:top_n]
    return {"type": "FeatureCollection",
            "source": "Natural Earth 1:10m populated_places (public domain)",
            "features": rows}


def build_coastline() -> dict:
    rd = _reader(RAW / "ne_10m_coastline", "ne_10m_coastline")
    feats = []
    for sr in rd.iterShapeRecords():
        rings = _rings_from_shape(sr.shape)
        rings = [r for r in rings if _bbox_hit(r, BBOX)]
        if not rings:
            continue
        feats.append({"type": "Feature", "properties": {},
                      "geometry": {"type": "MultiLineString", "coordinates": rings}})
    return {"type": "FeatureCollection",
            "source": "Natural Earth 1:10m coastline (public domain), bbox 70-140E/10-60N",
            "features": feats}


def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")
    print(f"[ne] 写出 {path.relative_to(ROOT)}  {path.stat().st_size/1e6:.2f} MB  "
          f"({len(obj['features'])} features)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="下载并裁剪 Natural Earth 底图（中国范围）")
    ap.add_argument("--list", action="store_true", help="只检查本地状态")
    ap.add_argument("--keep-raw", action="store_true", help="保留原始 shapefile 包")
    ap.add_argument("--cities", type=int, default=120, help="保留城市数（按人口）")
    args = ap.parse_args(argv)

    targets = [DATA_NE / "china_admin0.geojson", DATA_NE / "china_admin1.geojson",
               DATA_NE / "china_cities.geojson", DATA_NE / "coastline_cn_bbox.geojson"]
    if args.list:
        for p in targets:
            if p.exists():
                print(f"  OK  {p.relative_to(ROOT)}  {p.stat().st_size/1e6:.2f} MB")
            else:
                print(f"  --  {p.relative_to(ROOT)}")
        for rel, name in PACKS:
            zp = RAW / (name + ".zip")
            print(f"  zip {'OK ' if zp.exists() else '-- '} {name}")
        return 0
    if all(p.exists() for p in targets):
        print("[ne] 4 个裁剪文件已齐全，无需重建（删掉 data/ne/*.geojson 可强制重建）")
        return 0

    try:
        import shapefile  # noqa: F401
    except Exception:  # noqa: BLE001
        print("[ne] 需要 pyshp：python -m pip install pyshp")
        return 2

    for rel, name in PACKS:
        zp = download(f"{BASE}/{rel}", RAW / (name + ".zip"))
        _unzip(zp)

    write_json(DATA_NE / "china_admin0.geojson", build_admin0())
    write_json(DATA_NE / "china_admin1.geojson", build_admin1())
    write_json(DATA_NE / "china_cities.geojson", build_cities(args.cities))
    write_json(DATA_NE / "coastline_cn_bbox.geojson", build_coastline())

    if not args.keep_raw:
        for rel, name in PACKS:
            (RAW / (name + ".zip")).unlink(missing_ok=True)
        print("[ne] 已删除原始 zip（--keep-raw 可保留）")
    print("[ne] 完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
