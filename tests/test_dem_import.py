# -*- coding: utf-8 -*-
"""tests/test_dem_import.py — 离线自主导入高程文件（GeoTIFF / HGT / HGT.GZ / ZIP）测试。

覆盖内容
--------
1. **合成 HGT 瓦片**：在临时目录生成 3601×3601 大端 int16 瓦片 → 用
   :class:`app.core.dem_import.DemImportManager` 导入临时 DEM 根 →
   验证 ``engine/common/dem.py`` 的 ``sample``（最近邻/双线性）取到正确高程。
2. **真实山西 BigTIFF**（存在时）：``inspect`` 宽高/bbox/分辨率/坐标系、已知点采样、
   随机点采样（值域与耗时）、引用式导入 + ``list_sources``/``coverage``、
   以及 ``dem.sample`` 在自定义源下走 ``"custom"`` 而不是粗 DEM。
3. **合成 tiled LZW TIFF**：自写 LZW 编码器（与 libtiff 的 nodata 瓦片逐字节一致）
   生成小 TIFF，测试 LZW/deflate/无压缩、预测器 2、BigTIFF、大小端、tfw 地理参照、
   nodata→nan，以及各种错误路径（投影坐标系 / 不支持的压缩 / 预测器 3 / 头损坏）。
4. 所有临时文件都放在 ``tempfile`` 目录并在 ``finally`` 清理，不写 D:\\ 与工程目录。

运行::

    python tests\\test_dem_import.py
    python tests\\test_dem_import.py -v
"""
from __future__ import annotations

import base64
import gzip
import json
import math
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest
import zipfile
import zlib
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ENGINE = ROOT / "engine"
for _p in (str(ROOT), str(ENGINE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: 参考数据（不存在则相关测试 skip）
SHANXI_TIF = Path(r"D:\BaiduNetdiskDownload\山西\山西WGS84.tif")

HGT_N = 3601
HGT_BYTES = HGT_N * HGT_N * 2
HGT_1201_BYTES = 1201 * 1201 * 2
VOID = -32768

#: 山西文件第 0 个 tile 的原始 LZW 字节流（424 B，由 GDAL/libtiff 生成）。
#: 解压后恰好 32768 字节，全部为 int16 -32767（nodata）。用于在**没有真实文件**时
#: 也能验证 LZW 解码器与 libtiff 的 bit 级兼容性。
_LZW_TILE_B64 = (
    "gABQECgkDg0FhEHhUJhkLh0NiEPiURikTi0VjEXjUZjkbj0dkEfkUhkkjk0llEnlUplkrl0tmEvmUxmkzm01nE3nU5nk7n09oE/o"
    "VBolDo1FpFHpVJplLp1NqFPqVRqlTq1VrFXrVZrlbr1dsFfsVhsljs1ltFntVptlrt1tuFvuVxulzu11vF3vV5vl7v19wF/wWBwm"
    "Dw2FxGHxWJxmLx2NyGPyWRymTy2VzGXzWZzmbz2d0Gf0Wh0mj02l1Gn1Wp1mr12t2Gv2Wx2mz2213G33W53m73294G/4XB4nD43F"
    "5HH5XJ5nL53N6HP6XR6nT63V7HX7XZ7nb73d8Hf8Xh8nj83l9Hn9Xp9nr93t+Hv+Xx+nz+31/H3/X5/n7/z+wA/8BQDAkBwNAsEQ"
    "PBUEwZBcHQbCEHwlCMKQnC0KwxC8NQzDkNw9DsQQ/EUQxJEcTRLFETxVFMWRXF0WxhF8ZRjGkZxtGscRvHUcx5HcfR7IEfyFIMiS"
    "HI0iyRI8lSTJklydJsoSfKUoypKcrSrLEry1LMuS3L0uzBL8xTDMkxzNMs0TOhyAgA=="
)
_LZW_TILE_BYTES = base64.b64decode(_LZW_TILE_B64)


# --------------------------------------------------------------------------- #
# 临时目录 / dem 模块隔离
# --------------------------------------------------------------------------- #
class TempDirMixin(unittest.TestCase):
    """每个测试一个临时目录，结束（含异常）时清理。"""

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="rocketkit_dem_test_"))
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        # 先关掉引擎侧可能持有的 GeoTIFF 文件句柄，Windows 才能删掉临时目录
        try:
            import common.dem as dem
            dem._reset_custom_cache()
        except Exception:                       # noqa: BLE001
            pass
        for i in range(3):
            try:
                shutil.rmtree(self.tmp)
                return
            except OSError:
                time.sleep(0.05 * (i + 1))
        shutil.rmtree(self.tmp, ignore_errors=True)

    def bind_dem(self, dem_dir: Path):
        """把 ``common.dem`` 的全局目录指向临时 DEM 根（等价 DemManager._engine_dem）。"""
        import common.dem as dem
        dem_dir = Path(dem_dir)
        os.environ["RK_DEM"] = str(dem_dir)
        dem.DATA_DEM = dem_dir
        dem.DIR_GZ = dem_dir / "gz"
        dem.DIR_HGT = dem_dir / "hgt"
        dem.DIR_CACHE = dem_dir / "cache"
        dem.MANIFEST = dem_dir / "srtm_tiles.json"
        dem.OBSERVER_INFO = dem_dir / "observer_dem.json"
        dem.CUSTOM_INDEX = dem_dir / "custom" / "index.json"
        dem._TILE_CACHE.clear()
        dem._OBS_CACHE.clear()
        dem._reset_custom_cache()
        dem._LAST_WRITTEN_MODE = None
        dem.LAST_MODE = "none"
        dem.LAST_MODE_NOTE = ""
        return dem


# --------------------------------------------------------------------------- #
# TIFF LZW 编码器（测试用；与 libtiff 的 nodata 瓦片逐字节一致）
# --------------------------------------------------------------------------- #
class _BitWriter:
    __slots__ = ("buf", "acc", "nbits")

    def __init__(self):
        self.buf = bytearray()
        self.acc = 0
        self.nbits = 0

    def write(self, code: int, n: int) -> None:
        self.acc = (self.acc << n) | (code & ((1 << n) - 1))
        self.nbits += n
        while self.nbits >= 8:
            self.nbits -= 8
            self.buf.append((self.acc >> self.nbits) & 0xFF)

    def finish(self) -> bytes:
        if self.nbits:
            self.buf.append((self.acc << (8 - self.nbits)) & 0xFF)
        return bytes(self.buf)


def _lzw_width(k: int) -> int:
    """第 k 个码字（clear 之后从 0 计数）的位宽（early-change=1）。"""
    if k < 254:
        return 9
    if k < 766:
        return 10
    if k < 1790:
        return 11
    return 12


def lzw_encode(data: bytes) -> bytes:
    """最小 TIFF-LZW 编码器（仅测试用）。"""
    bw = _BitWriter()
    table = {bytes([i]): i for i in range(256)}
    next_code, k = 258, 0
    bw.write(256, _lzw_width(k))
    w = b""
    for byte in data:
        wc = w + bytes([byte])
        if wc in table:
            w = wc
            continue
        bw.write(table[w], _lzw_width(k))
        k += 1
        table[wc] = next_code
        next_code += 1
        if next_code >= 4096:                   # 码表满 → Clear
            bw.write(256, _lzw_width(k))
            k = 0
            table = {bytes([i]): i for i in range(256)}
            next_code = 258
        w = bytes([byte])
    if w:
        bw.write(table[w], _lzw_width(k))
        k += 1
    bw.write(257, _lzw_width(k))
    return bw.finish()


# --------------------------------------------------------------------------- #
# 最小 GeoTIFF 写出器（测试用）
# --------------------------------------------------------------------------- #
_TIFF_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 11: 4, 12: 8, 16: 8}
_TIFF_TYPE_FMT = {1: "B", 2: "B", 3: "H", 4: "I", 11: "f", 12: "d", 16: "Q"}


def _geo_key_directory(bo: str, projected_epsg=None, geographic_epsg=4326) -> bytes:
    """GeoKeyDirectory：地理坐标（EPSG:4326）或投影坐标（用于错误路径测试）。"""
    if projected_epsg:
        keys = [(1024, 0, 1, 1), (1025, 0, 1, 1),
                (3072, 0, 1, int(projected_epsg)), (3076, 0, 1, 9001)]
    else:
        keys = [(1024, 0, 1, 2), (1025, 0, 1, 1),
                (2048, 0, 1, int(geographic_epsg)), (2054, 0, 1, 9102)]
    values = [1, 1, 0, len(keys)] + [int(v) for k in keys for v in k]
    return struct.pack(bo + "%dH" % len(values), *values)


def _predictor_encode(arr: np.ndarray) -> np.ndarray:
    """水平差分（预测器 2）：按行做 uint 环绕差分。"""
    udt = np.dtype(f"u{arr.dtype.itemsize}")
    u = np.ascontiguousarray(arr).view(udt)
    out = np.empty_like(u)
    out[:, 0] = u[:, 0]
    out[:, 1:] = u[:, 1:] - u[:, :-1]
    return out.view(arr.dtype)


def write_tiff(path, arr, *, tile=(64, 64), compression=5, predictor=1,
               nodata=None, big=False, byte_order="<", geo="geokeys",
               tfw=False, projected_epsg=None, pad_value=0,
               block_data_override=None, geo_box=(110.0, 40.0, 0.001, 0.001)):
    """写一个测试用 GeoTIFF（单波段、平铺或条带）。

    Parameters
    ----------
    arr : ndarray (h, w)
        int16 / float32 等。
    tile : tuple or None
        (tile_width, tile_length)；None = 条带（RowsPerStrip=16）。
    compression : int
        1（无）、5（LZW）、8（deflate）、7（伪 JPEG，用于错误路径）。
    predictor : int
        1 或 2。
    geo : {'geokeys','transform','tfw','none','projected'}
        地理参照方式。
    block_data_override : list[bytes], optional
        直接给出**压缩后**的块数据（用于注入 libtiff 产生的真实 LZW 流）。
    """
    arr = np.ascontiguousarray(arr)
    h, w = arr.shape
    bo = byte_order
    x0, y0, dx, dy = geo_box
    fill = pad_value
    if nodata is not None:
        fill = nodata

    # ---- 1) 组织块（平铺块按 tile 全尺寸填充；条带按实际行数）----
    if tile:
        tw, th = int(tile[0]), int(tile[1])
        across = (w + tw - 1) // tw
        down = (h + th - 1) // th
        if block_data_override is not None:
            if len(block_data_override) != across * down:
                raise ValueError("block_data_override 数量与块数不符")
        raw_blocks, shape = [], []
        for tr in range(down):
            for tc in range(across):
                sub = np.full((th, tw), fill, dtype=arr.dtype)
                nr = min(th, h - tr * th)
                nc = min(tw, w - tc * tw)
                sub[:nr, :nc] = arr[tr * th:tr * th + nr, tc * tw:tc * tw + nc]
                raw_blocks.append(_predictor_encode(sub) if predictor == 2 else sub)
                shape.append((th, tw))
        tags_extra = [(322, 3, 1, struct.pack(bo + "H", tw)),
                      (323, 3, 1, struct.pack(bo + "H", th))]
        counts_tag, offsets_tag = 325, 324
    else:
        rps = 16
        n_strips = (h + rps - 1) // rps
        raw_blocks = []
        for i in range(n_strips):
            sub = arr[i * rps:min((i + 1) * rps, h)]
            raw_blocks.append(_predictor_encode(sub) if predictor == 2 else sub)
        tags_extra = [(278, 4, 1, struct.pack(bo + "I", rps))]
        counts_tag, offsets_tag = 279, 273

    # ---- 2) 压缩 ----
    if block_data_override is not None:
        blocks = list(block_data_override)
    elif compression == 5:
        blocks = [lzw_encode(np.ascontiguousarray(b).tobytes()) for b in raw_blocks]
    elif compression == 8:
        blocks = [zlib.compress(np.ascontiguousarray(b).tobytes()) for b in raw_blocks]
    elif compression == 1:
        blocks = [np.ascontiguousarray(b).tobytes() for b in raw_blocks]
    elif compression == 7:
        blocks = [np.ascontiguousarray(b).tobytes() for b in raw_blocks]
    else:
        raise ValueError("测试写出器不支持的压缩码")

    # ---- 3) 标签 ----
    sample_format = {("i", 2): 2, ("u", 2): 1, ("i", 4): 2,
                     ("u", 4): 1, ("f", 4): 3, ("f", 8): 3}[
        (arr.dtype.kind, arr.dtype.itemsize)]
    bits = arr.dtype.itemsize * 8
    tags = [
        (256, 3, 1, struct.pack(bo + "H", w)),
        (257, 3, 1, struct.pack(bo + "H", h)),
        (258, 3, 1, struct.pack(bo + "H", bits)),
        (259, 3, 1, struct.pack(bo + "H", int(compression))),
        (262, 3, 1, struct.pack(bo + "H", 1)),
        (277, 3, 1, struct.pack(bo + "H", 1)),
        (284, 3, 1, struct.pack(bo + "H", 1)),
        (339, 3, 1, struct.pack(bo + "H", sample_format)),
    ]
    if predictor != 1:
        tags.append((317, 3, 1, struct.pack(bo + "H", int(predictor))))
    tags.extend(tags_extra)
    n_blocks = len(blocks)
    tags.append((offsets_tag, 16 if big else 4, n_blocks,
                 struct.pack(bo + "%d%s" % (n_blocks, "Q" if big else "I"),
                             *([0] * n_blocks))))
    tags.append((counts_tag, 16 if big else 4, n_blocks,
                 struct.pack(bo + "%d%s" % (n_blocks, "Q" if big else "I"),
                             *[len(b) for b in blocks])))
    if geo == "geokeys":
        tags.append((33550, 12, 3, struct.pack(bo + "3d", dx, dy, 0.0)))
        tags.append((33922, 12, 6, struct.pack(bo + "6d", 0.0, 0.0, 0.0, x0, y0, 0.0)))
        tags.append((34735, 3, 0, _geo_key_directory(bo)))
        tags.append((34737, 2, 0, b"WGS 84|\x00"))
    elif geo == "transform":
        m = struct.pack(bo + "16d",
                        dx, 0.0, 0.0, x0,
                        0.0, -dy, 0.0, y0,
                        0.0, 0.0, 0.0, 0.0,
                        0.0, 0.0, 0.0, 1.0)
        tags.append((34264, 12, 16, m))
        tags.append((34735, 3, 0, _geo_key_directory(bo)))
    elif geo == "projected":
        tags.append((33550, 12, 3, struct.pack(bo + "3d", dx, dy, 0.0)))
        tags.append((33922, 12, 6, struct.pack(bo + "6d", 0.0, 0.0, 0.0, x0, y0, 0.0)))
        tags.append((34735, 3, 0, _geo_key_directory(bo, projected_epsg=projected_epsg or 32650)))
    # geo == 'twf' / 'none'：不写地理标签
    if nodata is not None:
        txt = (str(nodata)).encode("ascii") + b"\x00"
        tags.append((42113, 2, len(txt), txt))

    # GeoKeyDirectory / GeoAscii 的 count 按实际长度修正
    tags = [(t, ty, (len(v) // _TIFF_TYPE_SIZE[ty]) if ty in (2, 3, 12) and c == 0
             else c, v) for (t, ty, c, v) in tags]
    tags.sort(key=lambda x: x[0])

    # ---- 4) 布局：header → IFD → 外置值 → 块数据 ----
    value_size = 8 if big else 4
    ifd_size = (8 + len(tags) * 20 + 8) if big else (2 + len(tags) * 12 + 4)
    header_size = 16 if big else 8
    inline_limit = value_size
    ext = bytearray()
    placements = []                       # (tag, type, count, payload|offset_pos)
    for tag, ttype, count, payload in tags:
        if len(payload) <= inline_limit:
            placements.append((tag, ttype, count, payload, None))
        else:
            pos = len(ext)
            ext += payload
            if len(ext) % 2:
                ext += b"\x00"
            placements.append((tag, ttype, count, None, pos))
    block_base = header_size + ifd_size + len(ext)

    # 计算块偏移并回填 offsets 标签
    block_offsets, cur = [], block_base
    for b in blocks:
        block_offsets.append(cur)
        cur += len(b)
        if cur % 2:
            cur += 1

    def _offsets_payload():
        fmt = bo + "%d%s" % (n_blocks, "Q" if big else "I")
        return struct.pack(fmt, *block_offsets)

    new_placements = []
    for tag, ttype, count, payload, extpos in placements:
        if tag == offsets_tag:
            payload = _offsets_payload()
            if len(payload) <= inline_limit:
                extpos = None
            else:
                # 覆盖到 ext 里同长度位置
                start = extpos
                ext[start:start + len(payload)] = payload
                payload = None
        new_placements.append((tag, ttype, count, payload, extpos))
    placements = new_placements

    # ---- 5) 组装 ----
    out = bytearray()
    if big:
        out += (b"II" if bo == "<" else b"MM") + struct.pack(bo + "H", 43)
        out += struct.pack(bo + "HH", 8, 0)
        out += struct.pack(bo + "Q", header_size)
    else:
        out += (b"II" if bo == "<" else b"MM") + struct.pack(bo + "H", 42)
        out += struct.pack(bo + "I", header_size)
    if big:
        out += struct.pack(bo + "Q", len(placements))
        for tag, ttype, count, payload, extpos in placements:
            out += struct.pack(bo + "HHQ", tag, ttype, count)
            if extpos is None:
                out += payload + b"\x00" * (value_size - len(payload))
            else:
                out += struct.pack(bo + "Q", header_size + ifd_size + extpos)
        out += struct.pack(bo + "Q", 0)
    else:
        out += struct.pack(bo + "H", len(placements))
        for tag, ttype, count, payload, extpos in placements:
            out += struct.pack(bo + "HHI", tag, ttype, count)
            if extpos is None:
                out += payload + b"\x00" * (value_size - len(payload))
            else:
                out += struct.pack(bo + "I", header_size + ifd_size + extpos)
        out += struct.pack(bo + "I", 0)
    assert len(out) == header_size + ifd_size
    out += ext
    for pos, b in zip(block_offsets, blocks):
        if len(out) < pos:
            out += b"\x00" * (pos - len(out))
        out += b
        if len(out) % 2:
            out += b"\x00"
    path = Path(path)
    path.write_bytes(bytes(out))
    if tfw:
        tw = path.with_suffix(".tfw")
        tw.write_text(
            f"{dx:.10f}\n0.0000000000\n0.0000000000\n{-dy:.10f}\n"
            f"{x0 + dx / 2.0:.10f}\n{y0 - dy / 2.0:.10f}\n", encoding="utf-8")
    return path


def elev_field(rows, cols):
    """测试用连续高程场 [m]：线性，便于解析核对双线性插值。"""
    return 500.0 + 2.0 * np.asarray(rows, float) + 3.0 * np.asarray(cols, float)


# --------------------------------------------------------------------------- #
# 1) 合成 HGT 导入 + dem.sample
# --------------------------------------------------------------------------- #
class TestHgtImport(TempDirMixin):
    """3601×3601 1″ HGT 的导入与采样。"""

    def _make_hgt(self, name: str, seed: int = 0) -> Path:
        alt = np.full((HGT_N, HGT_N), 100, dtype=">i2")
        alt[1800, 1800] = 1234
        alt[1801, 1800] = 1300
        alt[1800, 1801] = 1100
        alt[1799, 1799] = 900
        path = self.tmp / f"{name}.hgt"
        alt.tofile(path)
        return path

    def test_import_hgt_and_sample(self):
        dem_dir = self.tmp / "dem"
        src = self._make_hgt("N37E112")
        self.assertEqual(src.stat().st_size, HGT_BYTES)

        from app.core.dem_import import DemImportManager
        mgr = DemImportManager(dem_dir, logger=lambda m: None)
        res = mgr.import_path(src)
        self.assertEqual(res["failed"], [])
        self.assertEqual(res["ok"], 1)
        self.assertEqual(res["tiles"], ["N37E112"])
        self.assertTrue((dem_dir / "hgt" / "N37E112.hgt").is_file())
        self.assertEqual((dem_dir / "hgt" / "N37E112.hgt").stat().st_size, HGT_BYTES)

        dem = self.bind_dem(dem_dir)
        # 最近邻：37.5N/112.5E 正好是 (row=1800, col=1800)
        self.assertAlmostEqual(float(dem.sample(37.5, 112.5)), 1234.0, places=6)
        # 双线性：像素中心 → 同样取该像素
        self.assertAlmostEqual(float(dem.sample(37.5, 112.5, method="bilinear")),
                               1234.0, places=6)
        # 略往北一点 → row 1800 与 1799 之间
        v = float(dem.sample(37.5 + 0.5 / 3600.0, 112.5))
        self.assertIn(round(v), (1234, 900, 100))
        # 瓦片可用性
        self.assertTrue(dem.tiles_available_for(37.5, 112.5, 10.0))
        self.assertIn("N37E112", dem.available_tiles())
        # 命中的瓦片 → 模式为 srtm（不触发粗 DEM）
        self.assertEqual(dem.last_mode()["mode"], "none")   # nearest 不写模式
        self.assertTrue(np.isfinite(float(dem.sample(37.9, 112.9))))

    def test_reject_3arcsec_hgt(self):
        from app.core.dem_import import DemImportManager
        src = self.tmp / "N36E112.hgt"
        np.zeros(1201 * 1201, dtype=">i2").tofile(src)
        self.assertEqual(src.stat().st_size, HGT_1201_BYTES)
        mgr = DemImportManager(self.tmp / "dem", logger=lambda m: None)
        res = mgr.import_path(src)
        self.assertEqual(res["ok"], 0)
        self.assertEqual(len(res["failed"]), 1)
        self.assertIn("3601", res["failed"][0]["error"])
        self.assertFalse((self.tmp / "dem" / "hgt" / "N36E112.hgt").exists())

    def test_reject_bad_size_and_name(self):
        from app.core.dem_import import DemImportManager
        mgr = DemImportManager(self.tmp / "dem", logger=lambda m: None)
        bad = self.tmp / "N35E112.hgt"
        bad.write_bytes(b"\x00" * 1000)
        res = mgr.import_path(bad)
        self.assertEqual(res["ok"], 0)
        self.assertIn("字节", res["failed"][0]["error"])
        # 大小对但名字不是瓦片名
        good_size_bad_name = self.tmp / "foo.hgt"
        np.zeros(HGT_N * HGT_N, dtype=">i2").tofile(good_size_bad_name)
        res2 = mgr.import_path(good_size_bad_name)
        self.assertEqual(res2["ok"], 0)
        self.assertIn("瓦片名", res2["failed"][0]["error"])

    def test_import_hgt_gz(self):
        from app.core.dem_import import DemImportManager
        alt = np.full((HGT_N, HGT_N), 200, dtype=">i2")
        alt[100, 100] = 777
        gz = self.tmp / "N38E112.hgt.gz"
        with gzip.open(gz, "wb") as fh:
            fh.write(alt.tobytes())
        mgr = DemImportManager(self.tmp / "dem", logger=lambda m: None)
        res = mgr.import_path(gz)
        self.assertEqual(res["ok"], 1, res)
        self.assertEqual(res["tiles"], ["N38E112"])
        self.assertTrue((self.tmp / "dem" / "hgt" / "N38E112.hgt").is_file())
        self.assertTrue((self.tmp / "dem" / "gz" / "N38E112.hgt.gz").is_file())
        dem = self.bind_dem(self.tmp / "dem")
        # row = (39 - 38.97222…)*3600 = 100
        lat = 39.0 - 100.0 / 3600.0
        lon = 112.0 + 100.0 / 3600.0
        self.assertAlmostEqual(float(dem.sample(lat, lon)), 777.0, places=6)

    def test_zip_import_and_path_traversal(self):
        from app.core.dem_import import DemImportManager
        alt = np.full((HGT_N, HGT_N), 300, dtype=">i2")
        alt[50, 60] = 888
        hgt_path = self.tmp / "N39E113.hgt"
        alt.tofile(hgt_path)
        zpath = self.tmp / "tiles.zip"
        evil = self.tmp.parent / "rocketkit_evil.txt"
        try:
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.write(hgt_path, arcname="N39E113.hgt")
                zf.writestr("../rocketkit_evil.txt", "bad")
                zf.writestr("sub/readme.txt", "ignored")
            mgr = DemImportManager(self.tmp / "dem", logger=lambda m: None)
            res = mgr.import_path(zpath)
            self.assertEqual(res["ok"], 1, res)
            self.assertEqual(res["tiles"], ["N39E113"])
            self.assertFalse(evil.exists(), "zip 路径穿越没有得到防护")
        finally:
            evil.unlink(missing_ok=True)
            hgt_path.unlink(missing_ok=True)

    def test_import_directory(self):
        from app.core.dem_import import DemImportManager
        src_dir = self.tmp / "inbox"
        src_dir.mkdir()
        sub = src_dir / "a" / "b"
        sub.mkdir(parents=True)
        np.zeros(HGT_N * HGT_N, dtype=">i2").tofile(src_dir / "N40E113.hgt")
        # 一个小的合成 GeoTIFF 也一起导入
        arr = np.full((40, 50), 660, dtype=np.int16)
        write_tiff(sub / "small.tif", arr, tile=(32, 32), compression=1,
                   geo="geokeys")
        mgr = DemImportManager(self.tmp / "dem", logger=lambda m: None)
        res = mgr.import_path(src_dir)
        self.assertEqual(res["ok"], 2, res)
        self.assertEqual(res["tiles"], ["N40E113"])
        self.assertEqual(len(res["sources"]), 1)
        self.assertEqual(len(mgr.list_sources()), 1)


# --------------------------------------------------------------------------- #
# 2) GeoTIFF 读取器（合成小文件）
# --------------------------------------------------------------------------- #
class TestGeoTiffReader(TempDirMixin):
    """最小读取器：LZW/deflate/BigTIFF/大小端/预测器/地理参照/错误路径。"""

    def setUp(self):
        super().setUp()
        self.dem_dir = self.tmp / "dem"
        self.dem_dir.mkdir(parents=True, exist_ok=True)

    def _source(self, path):
        from common.geotiff import GeoTiffSource
        g = GeoTiffSource(path)
        self.addCleanup(g.close)
        return g

    def test_real_libtiff_lzw_tile(self):
        """用山西文件里真实的 424 B libtiff LZW 流做 bit 级兼容性验证。"""
        arr = np.full((128, 128), -32767, dtype=np.int16)
        path = write_tiff(self.tmp / "lzw_real.tif", arr, tile=(128, 128),
                          compression=5, predictor=1, nodata=-32767,
                          block_data_override=[_LZW_TILE_BYTES])
        g = self._source(path)
        info = g.inspect()
        self.assertEqual(info["compression"], 5)
        self.assertTrue(info["tiled"])
        self.assertEqual(info["tile_size"], [128, 128])
        block, rows = g._decode_block(0, 0)
        self.assertEqual(rows, 128)
        self.assertEqual(block.shape, (128, 128))
        self.assertTrue(np.all(block == -32767))
        # 公开接口：nodata → nan
        self.assertTrue(math.isnan(g.sample(39.99, 110.01)))

    def test_tiled_lzw_predictor_roundtrip(self):
        h, w = 200, 300
        rr, cc = np.mgrid[0:h, 0:w]
        arr = elev_field(rr, cc).astype(np.int16)
        for predictor in (1, 2):
            with self.subTest(predictor=predictor):
                path = write_tiff(self.tmp / f"p{predictor}.tif", arr,
                                  tile=(64, 64), compression=5,
                                  predictor=predictor, geo="geokeys")
                g = self._source(path)
                info = g.inspect()
                np.testing.assert_allclose(info["bbox"], [39.8, 40.0, 110.0, 110.3], atol=1e-9)
                self.assertAlmostEqual(info["res_deg"][0], 0.001, places=12)
                # 整数像素最近邻
                for r, c in [(0, 0), (1, 2), (50, 70), (199, 299), (64, 64)]:
                    lat = 40.0 - (r + 0.5) * 0.001
                    lon = 110.0 + (c + 0.5) * 0.001
                    self.assertAlmostEqual(g.sample(lat, lon, method="nearest"),
                                           float(arr[r, c]), delta=0.5)
                # 双线性（线性场 → 解析值）
                rng = np.random.default_rng(7)
                lats = 40.0 - rng.uniform(0.5, 199.5, 40) * 0.001
                lons = 110.0 + rng.uniform(0.5, 299.5, 40) * 0.001
                got = g.sample_many(lats, lons, method="bilinear")
                fy = (40.0 - lats) / 0.001 - 0.5
                fx = (lons - 110.0) / 0.001 - 0.5
                want = elev_field(fy, fx)
                np.testing.assert_allclose(got, want, atol=1e-6)

    def test_truncated_lzw_stream_no_hang(self):
        """截断的 LZW 码流不能让解码器死循环/抛未捕获异常。"""
        arr = np.full((128, 128), -32767, dtype=np.int16)
        path = write_tiff(self.tmp / "trunc_lzw.tif", arr, tile=(128, 128),
                          compression=5, nodata=-32767,
                          block_data_override=[_LZW_TILE_BYTES[:200]])
        g = self._source(path)
        block, rows = g._decode_block(0, 0)
        self.assertGreaterEqual(rows, 0)
        self.assertLessEqual(rows, 128)
        v = g.sample(40.0 - 100.5 * 0.001, 110.0 + 100.5 * 0.001)
        self.assertTrue(isinstance(v, float))

    def test_nodata_bilinear_is_nan(self):
        h, w = 20, 20
        arr = np.full((h, w), 1000, dtype=np.int16)
        arr[5:8, 5:8] = -32767
        path = write_tiff(self.tmp / "nodata.tif", arr, tile=(16, 16),
                          compression=1, nodata=-32767, geo="geokeys")
        g = self._source(path)
        lat = 40.0 - (6 + 0.5) * 0.001
        lon = 110.0 + (6 + 0.5) * 0.001
        self.assertTrue(math.isnan(g.sample(lat, lon)))
        self.assertTrue(math.isnan(g.sample(lat, lon, method="nearest")))
        lat2 = 40.0 - (0 + 0.5) * 0.001
        lon2 = 110.0 + (0 + 0.5) * 0.001
        self.assertAlmostEqual(g.sample(lat2, lon2), 1000.0, delta=1e-6)
        # 越界 → nan
        self.assertTrue(math.isnan(g.sample(50.0, 110.0)))
        self.assertTrue(math.isnan(g.sample(39.0, 110.0)))

    def test_deflate_and_strip(self):
        h, w = 100, 80
        rr, cc = np.mgrid[0:h, 0:w]
        arr = elev_field(rr, cc).astype(np.int16)
        path = write_tiff(self.tmp / "deflate.tif", arr, tile=None,
                          compression=8, geo="geokeys")
        g = self._source(path)
        self.assertFalse(g.inspect()["tiled"])
        self.assertAlmostEqual(g.sample(40.0 - 10.5 * 0.001, 110.0 + 20.5 * 0.001,
                                        method="nearest"),
                               float(arr[10, 20]), delta=0.5)

    def test_bigtiff(self):
        h, w = 60, 70
        rr, cc = np.mgrid[0:h, 0:w]
        arr = elev_field(rr, cc).astype(np.int16)
        path = write_tiff(self.tmp / "big.tif", arr, tile=(32, 32),
                          compression=5, big=True, geo="geokeys")
        g = self._source(path)
        info = g.inspect()
        self.assertTrue(info["big_tiff"])
        self.assertEqual((info["width"], info["height"]), (w, h))
        self.assertAlmostEqual(g.sample(40.0 - 3.5 * 0.001, 110.0 + 4.5 * 0.001,
                                        method="nearest"),
                               float(arr[3, 4]), delta=0.5)

    def test_big_endian(self):
        h, w = 40, 40
        rr, cc = np.mgrid[0:h, 0:w]
        arr = elev_field(rr, cc).astype(">i2")
        path = write_tiff(self.tmp / "mm.tif", arr, tile=(16, 16),
                          compression=5, byte_order=">", geo="geokeys")
        g = self._source(path)
        info = g.inspect()
        self.assertEqual(info["dtype"], "int16")
        self.assertAlmostEqual(g.sample(40.0 - 2.5 * 0.001, 110.0 + 3.5 * 0.001,
                                        method="nearest"),
                               float(arr[2, 3]), delta=0.5)

    def test_tfw_world_file(self):
        """无地理标签 + 同名 .tfw（C/F 是左上像素中心 → 需减半像素）。"""
        h, w = 30, 40
        arr = np.zeros((h, w), dtype=np.int16)
        path = write_tiff(self.tmp / "plain.tif", arr, tile=(16, 16),
                          compression=1, geo="none", tfw=True,
                          geo_box=(110.0, 40.0, 0.001, 0.001))
        g = self._source(path)
        info = g.inspect()
        np.testing.assert_allclose(info["bbox"], [40.0 - h * 0.001, 40.0, 110.0, 110.0 + w * 0.001], atol=1e-9)
        self.assertEqual(info["geo_source"], "world file(.tfw)")
        # 第一像素中心
        self.assertAlmostEqual(g.sample(40.0 - 0.0005, 110.0 + 0.0005), 0.0)

    def test_transform_tag(self):
        h, w = 30, 40
        rr, cc = np.mgrid[0:h, 0:w]
        arr = elev_field(rr, cc).astype(np.int16)
        path = write_tiff(self.tmp / "tf.tif", arr, tile=(16, 16),
                          compression=1, geo="transform")
        g = self._source(path)
        info = g.inspect()
        self.assertEqual(info["geo_source"], "ModelTransformation")
        np.testing.assert_allclose(info["bbox"], [39.97, 40.0, 110.0, 110.04], atol=1e-9)
        self.assertAlmostEqual(g.sample(40.0 - 4.5 * 0.001, 110.0 + 5.5 * 0.001,
                                        method="nearest"),
                               float(arr[4, 5]), delta=0.5)

    def test_crs_is_epsg4326(self):
        arr = np.zeros((10, 10), dtype=np.int16)
        path = write_tiff(self.tmp / "crs.tif", arr, tile=(8, 8), compression=1,
                          geo="geokeys")
        self.assertEqual(self._source(path).inspect()["crs"], "EPSG:4326")

    def test_projected_crs_rejected(self):
        from common.geotiff import TiffError
        arr = np.zeros((10, 10), dtype=np.int16)
        path = write_tiff(self.tmp / "utm.tif", arr, tile=(8, 8), compression=1,
                          geo="projected", projected_epsg=32650)
        with self.assertRaises(TiffError) as ctx:
            self._source(path)
        self.assertIn("WGS84", str(ctx.exception))
        self.assertIn("投影", str(ctx.exception))

    def test_unknown_crs_out_of_range_rejected(self):
        from common.geotiff import TiffError
        arr = np.zeros((10, 10), dtype=np.int16)
        # 不写 GeoKey、坐标是 UTM 量级 → 拒收
        path = write_tiff(self.tmp / "utm2.tif", arr, tile=(8, 8), compression=1,
                          geo="geokeys", geo_box=(500000.0, 4000000.0, 30.0, 30.0))
        with self.assertRaises(TiffError) as ctx:
            self._source(path)
        self.assertIn("经纬度", str(ctx.exception))

    def test_predictor3_rejected(self):
        from common.geotiff import TiffError
        arr = np.zeros((10, 10), dtype=np.int16)
        path = write_tiff(self.tmp / "pred3.tif", arr, tile=(8, 8), compression=1,
                          predictor=3, geo="geokeys")
        with self.assertRaises(TiffError) as ctx:
            self._source(path)
        self.assertIn("预测器 3", str(ctx.exception))

    def test_unsupported_compression(self):
        from common.geotiff import TiffError
        arr = np.zeros((20, 20), dtype=np.int16)
        path = write_tiff(self.tmp / "jpg.tif", arr, tile=(16, 16),
                          compression=7, geo="geokeys")
        g = self._source(path)                  # 头能解析
        self.assertEqual(g.inspect()["compression"], 7)
        with self.assertRaises(TiffError) as ctx:   # 解压时才报错
            g.sample(39.99, 110.01)
        self.assertIn("不支持的 TIFF 压缩", str(ctx.exception))

    def test_bad_headers(self):
        from common.geotiff import TiffError
        # 非 TIFF
        p1 = self.tmp / "not_a_tiff.tif"
        p1.write_bytes(b"hello world, definitely not a tiff")
        with self.assertRaises(TiffError):
            self._source(p1)
        # BigTIFF 偏移宽度不是 8
        p2 = self.tmp / "bad_big.tif"
        p2.write_bytes(b"II" + struct.pack("<H", 43) + struct.pack("<H", 4)
                       + b"\x00\x00" + struct.pack("<Q", 16))
        with self.assertRaises(TiffError) as ctx:
            self._source(p2)
        self.assertIn("BigTIFF", str(ctx.exception))
        # classic TIFF 的 IFD 截断
        p3 = self.tmp / "trunc.tif"
        p3.write_bytes(b"II" + struct.pack("<H", 42) + struct.pack("<I", 8)
                       + struct.pack("<H", 5) + b"\x00" * 6)
        with self.assertRaises(TiffError):
            self._source(p3)
        # 未知 magic
        p4 = self.tmp / "magic.tif"
        p4.write_bytes(b"II" + struct.pack("<H", 99) + b"\x00" * 8)
        with self.assertRaises(TiffError):
            self._source(p4)

    def test_missing_georef_rejected(self):
        from common.geotiff import TiffError
        arr = np.zeros((10, 10), dtype=np.int16)
        path = write_tiff(self.tmp / "nogeo.tif", arr, tile=(8, 8), compression=1,
                          geo="none")
        with self.assertRaises(TiffError) as ctx:
            self._source(path)
        self.assertIn("地理参照", str(ctx.exception))


# --------------------------------------------------------------------------- #
# 3) 导入管理器：索引容错 / 复制 / 删除
# --------------------------------------------------------------------------- #
class TestImportRegistry(TempDirMixin):
    """index.json 容错、copy 模式、remove_source。"""

    def test_corrupt_index_recovers(self):
        from app.core.dem_import import DemImportManager
        dem_dir = self.tmp / "dem"
        mgr = DemImportManager(dem_dir, logger=lambda m: None)
        mgr.registry_path().parent.mkdir(parents=True, exist_ok=True)
        mgr.registry_path().write_text("{ this is not json", encoding="utf-8")
        # 不崩、返回空表，并留下 .bad-* 备份
        self.assertEqual(mgr.list_sources(), [])
        bads = list(mgr.registry_path().parent.glob("index.json.bad-*"))
        self.assertEqual(len(bads), 1, bads)
        # 之后还能正常导入
        arr = np.full((20, 20), 500, dtype=np.int16)
        path = write_tiff(self.tmp / "ok.tif", arr, tile=(16, 16), compression=1,
                          geo="geokeys")
        res = mgr.import_path(path)
        self.assertEqual(res["ok"], 1)
        self.assertEqual(len(mgr.list_sources()), 1)

    def test_copy_mode_and_remove(self):
        from app.core.dem_import import DemImportManager
        dem_dir = self.tmp / "dem"
        mgr = DemImportManager(dem_dir, logger=lambda m: None)
        arr = np.full((30, 40), 700, dtype=np.int16)
        src = write_tiff(self.tmp / "copy_src.tif", arr, tile=(16, 16),
                         compression=1, geo="tfw", tfw=True)
        prog = []
        res = mgr.import_path(src, copy=True, name="副本测试",
                              progress=lambda d, t, n: prog.append((d, t)))
        self.assertEqual(res["ok"], 1, res)
        rec = res["sources"][0]
        self.assertTrue(rec["copied"])
        copied = Path(rec["file"])
        self.assertTrue(copied.is_file())
        self.assertEqual(copied.parent.name, "data")
        self.assertTrue(copied.name.startswith("副本测试"))
        # sidecar（.tfw）一起复制
        self.assertTrue(any(Path(s).suffix.lower() == ".tfw" for s in rec["sidecars"]))
        for s in rec["sidecars"]:
            self.assertTrue(Path(s).is_file(), s)
        self.assertTrue(prog, "复制时应回调进度")
        # 删除 → 副本一并删除，索引清空，原文件保留
        self.assertTrue(mgr.remove_source(rec["id"]))
        self.assertEqual(mgr.list_sources(), [])
        self.assertFalse(copied.exists())
        self.assertTrue(src.is_file(), "绝不能删除用户原文件")

    def test_import_missing_path(self):
        from app.core.dem_import import DemImportManager
        mgr = DemImportManager(self.tmp / "dem", logger=lambda m: None)
        res = mgr.import_path(self.tmp / "nope.tif")
        self.assertEqual(res["ok"], 0)
        self.assertEqual(len(res["failed"]), 1)
        self.assertIn("不存在", res["failed"][0]["error"])

    def test_unsupported_extension(self):
        from app.core.dem_import import DemImportManager
        mgr = DemImportManager(self.tmp / "dem", logger=lambda m: None)
        p = self.tmp / "a.txt"
        p.write_text("x", encoding="utf-8")
        res = mgr.import_path(p)
        self.assertEqual(res["ok"], 0)
        self.assertIn("不支持", res["failed"][0]["error"])


# --------------------------------------------------------------------------- #
# 4) 真实山西 BigTIFF（存在时）
# --------------------------------------------------------------------------- #
@unittest.skipUnless(SHANXI_TIF.is_file(), f"缺少参考数据 {SHANXI_TIF}")
class TestShanxiGeoTiff(TempDirMixin):
    """真实 1.32 GB BigTIFF：元数据、采样、导入、dem 接入。"""

    @classmethod
    def setUpClass(cls):
        from common.geotiff import GeoTiffSource
        cls.src = GeoTiffSource(SHANXI_TIF)

    @classmethod
    def tearDownClass(cls):
        cls.src.close()

    def test_inspect(self):
        t0 = time.time()
        info = self.src.inspect()
        dt = time.time() - t0
        print(f"\n[山西] inspect 耗时 {dt * 1e3:.1f} ms")
        self.assertLess(dt, 1.0, "inspect 应 < 1 s")
        self.assertEqual(info["width"], 45670)
        self.assertEqual(info["height"], 64942)
        self.assertEqual(info["bands"], 1)
        self.assertEqual(info["dtype"], "int16")
        self.assertEqual(info["compression"], 5)
        self.assertTrue(info["tiled"])
        self.assertTrue(info["big_tiff"])
        self.assertEqual(info["crs"], "EPSG:4326")
        self.assertEqual(info["nodata"], -32767.0)
        lat_min, lat_max, lon_min, lon_max = info["bbox"]
        self.assertLess(lat_min, 37.7615)
        self.assertGreater(lat_max, 37.7615)
        self.assertLess(lon_min, 112.7252)
        self.assertGreater(lon_max, 112.7252)
        self.assertAlmostEqual(info["res_deg"][0] * 3600, 0.34154, places=4)
        # covers
        self.assertTrue(self.src.covers(37.7615, 112.7252))
        self.assertFalse(self.src.covers(19.6, 110.9))

    def test_sample_known_points(self):
        cases = [(37.7615, 112.7252), (40.0, 113.0)]
        for lat, lon in cases:
            t0 = time.time()
            v = self.src.sample(lat, lon)
            dt = time.time() - t0
            print(f"[山西] sample({lat}, {lon}) = {v:.2f} m  ({dt * 1e3:.1f} ms)")
            self.assertTrue(math.isfinite(v), (lat, lon, v))
            self.assertGreater(v, -500.0)
            self.assertLess(v, 3500.0)
            # 最近邻与双线性应接近（同一地点）
            vn = self.src.sample(lat, lon, method="nearest")
            self.assertLess(abs(v - vn), 60.0)
        # 已知地点合理性（太原盆地 ~800 m、五台山 > 2000 m）
        self.assertGreater(self.src.sample(39.0803, 113.5794), 2000.0)
        self.assertLess(self.src.sample(37.8706, 112.5489), 1500.0)

    def test_sample_random_2000(self):
        rng = np.random.default_rng(2026)
        lat_min, lat_max, lon_min, lon_max = self.src.bbox
        n = 2000
        lats = rng.uniform(lat_min + 0.01, lat_max - 0.01, n)
        lons = rng.uniform(lon_min + 0.01, lon_max - 0.01, n)
        t0 = time.time()
        vals = self.src.sample_many(lats, lons, method="bilinear")
        dt = time.time() - t0
        print(f"[山西] 随机 {n} 点双线性采样耗时 {dt:.2f} s"
              f"（{dt / n * 1000:.2f} ms/点，含 LZW 解码）")
        self.assertEqual(vals.shape, (n,))
        fin = vals[np.isfinite(vals)]
        # 山西省域外的 nodata 会被剔除，至少有 1/3 的点有效
        self.assertGreater(fin.size, n // 3)
        self.assertGreater(fin.min(), -500.0)
        self.assertLess(fin.max(), 3500.0)
        self.assertTrue(np.all(np.abs(fin) < 4000.0))
        self.assertLess(dt, 120.0)              # 宽松上限（一般 CPU 数秒）

    def test_import_reference_and_coverage(self):
        from app.core.dem_import import DemImportManager
        dem_dir = self.tmp / "dem"
        mgr = DemImportManager(dem_dir, logger=lambda m: None)
        t0 = time.time()
        res = mgr.import_path(SHANXI_TIF)
        print(f"[山西] 引用式导入耗时 {time.time() - t0:.2f} s")
        self.assertEqual(res["failed"], [])
        self.assertEqual(res["ok"], 1)
        self.assertEqual(len(res["sources"]), 1)
        rec = res["sources"][0]
        self.assertFalse(rec["copied"], "默认应为引用模式（不复制 1.3 GB）")
        self.assertEqual(Path(rec["file"]), SHANXI_TIF.resolve())
        self.assertGreater(rec["size"], 1_000_000_000)
        self.assertIn("tfw", " ".join(rec["sidecars"]).lower())
        sources = mgr.list_sources()
        self.assertEqual(len(sources), 1)
        self.assertTrue(sources[0]["valid"])
        # index.json 结构
        idx = json.loads(mgr.registry_path().read_text(encoding="utf-8"))
        self.assertEqual(idx["schema"], "rocketkit.dem_custom.v1")
        self.assertEqual(len(idx["sources"]), 1)
        # coverage：3 个山西境内点 100%
        pts = [("太原", 37.8706, 112.5489), ("大同", 40.0768, 113.3001),
               ("运城", 35.0263, 111.0039)]
        cov = mgr.coverage(pts)
        self.assertEqual(cov["total"], {"covered": 3, "total": 3, "ratio": 1.0})
        for p in cov["points"]:
            self.assertTrue(p["covered"])
            self.assertIsNotNone(p["value_m"])
        # 再导入一次是幂等的（同 id，不重复登记）
        res2 = mgr.import_path(SHANXI_TIF)
        self.assertEqual(len(mgr.list_sources()), 1)
        self.assertEqual(res2["sources"][0]["id"], rec["id"])

    def test_dem_sample_uses_custom(self):
        from app.core.dem_import import DemImportManager
        dem_dir = self.tmp / "dem"
        mgr = DemImportManager(dem_dir, logger=lambda m: None)
        mgr.import_path(SHANXI_TIF)
        dem = self.bind_dem(dem_dir)
        # 该临时 DEM 根没有任何 SRTM 瓦片；没有自定义源时必然走粗 DEM
        self.assertFalse(dem.available_tiles())
        expected = self.src.sample(37.7615, 112.7252, method="nearest")
        t0 = time.time()
        v = float(dem.sample(37.7615, 112.7252))
        dt = time.time() - t0
        print(f"[山西] dem.sample 走自定义源 = {v:.1f} m（{dt * 1e3:.1f} ms）")
        self.assertAlmostEqual(v, expected, delta=1e-6)
        self.assertEqual(dem.last_mode()["mode"], "custom")
        self.assertTrue(dem.last_mode()["note"].startswith("point:"))
        self.assertEqual(dem.last_mode()["custom_sources"], [mgr.list_sources()[0]["id"]])
        # 数组形状/dem_alt 语义
        lat = np.array([[37.8706, 40.0768], [35.0263, 19.6008]])
        lon = np.array([[112.5489, 113.3001], [111.0039, 110.9358]])
        vals = dem.sample(lat, lon)
        self.assertEqual(vals.shape, (2, 2))
        self.assertAlmostEqual(vals[0, 0], self.src.sample(37.8706, 112.5489,
                                                           method="nearest"), delta=1e-6)
        self.assertTrue(np.isfinite(vals[1, 0]))
        # 自定义源覆盖不到的点仍走原逻辑（这里退粗 DEM，不至于崩）
        self.assertTrue(math.isfinite(float(dem.dem_alt(19.6, 110.9))))
        # 未覆盖且 missing='raise' → 仍抛 FileNotFoundError
        with self.assertRaises(FileNotFoundError):
            dem.sample(19.6, 110.9, missing="raise")

    def test_dem_manager_prepare_custom(self):
        from app.core.dem_import import DemImportManager
        from app.core.dem_manager import DemManager
        dem_dir = self.tmp / "dem"
        mgr = DemImportManager(dem_dir, logger=lambda m: None)
        mgr.import_path(SHANXI_TIF)
        dm = DemManager(dem_dir, logger=lambda m: None)
        config = {
            "dem": {"horizon_radius_km": 50},
            "mission": {"launch_site": {"name": "太原", "lat_deg": 37.8706,
                                        "lon_deg": 112.5489, "alt_m_ellipsoid": 10}},
            "observers": [
                {"id": "taiyuan", "lat_deg": 37.8706, "lon_deg": 112.5489},
                {"id": "datong", "lat_deg": 40.0768, "lon_deg": 113.3001},
            ],
        }
        t0 = time.time()
        res = dm.prepare(config, allow_download=False, scan=False, scan_seconds=1)
        print(f"[山西] DemManager.prepare(custom) 耗时 {time.time() - t0:.2f} s")
        self.assertEqual(res["mode"], "custom")
        self.assertEqual(res["tiles"], 0)
        self.assertEqual(res["required"], 0)
        self.assertTrue(res["observer_ready"])
        self.assertTrue(res["custom_radius_ready"])
        self.assertEqual(len(res["custom"]), 1)
        self.assertEqual(res["downloaded"], 0)
        self.assertEqual(dm.dem_mode(), "custom")
        st = dm.status()
        self.assertTrue(st["custom_available"])
        self.assertEqual(len(st["custom_sources"]), 1)
        self.assertEqual((st["custom_coverage"] or {}).get("total", {}).get("ratio"), 1.0)
        alt, source = dm.observer_alt(37.8706, 112.5489)
        self.assertEqual(source, "custom")
        self.assertAlmostEqual(alt, self.src.sample(37.8706, 112.5489,
                                                    method="nearest"), delta=1e-6)
        # 模式文件
        mode_file = json.loads((dem_dir / "dem_mode.json").read_text(encoding="utf-8"))
        self.assertEqual(mode_file["mode"], "custom")


# --------------------------------------------------------------------------- #
# 5) 无真实文件时也跑的 dem 兼容性测试（确保没有 custom 源时行为不变）
# --------------------------------------------------------------------------- #
class TestNoCustomRegression(TempDirMixin):
    """没有自定义源时，原有 DEM 行为/模式字符串保持不变。"""

    def test_no_custom_behaviour(self):
        dem_dir = self.tmp / "dem"
        dem_dir.mkdir(parents=True, exist_ok=True)
        dem = self.bind_dem(dem_dir)
        self.assertFalse(dem.custom_available())
        # 无瓦片、有粗 DEM → coarse；模式字符串仍是 'coarse'
        v = float(dem.sample(37.5, 112.5))
        self.assertTrue(math.isfinite(v))
        self.assertEqual(dem.last_mode()["mode"], "coarse")
        self.assertFalse(dem.tiles_available_for(37.5, 112.5, 100.0))
        # 稀疏数组形状
        out = dem.sample(np.zeros((2, 3)), np.zeros((2, 3)))
        self.assertEqual(out.shape, (2, 3))
        # 缺瓦片 + raise
        with self.assertRaises(FileNotFoundError):
            dem.sample(37.5, 112.5, missing="raise")
        # 无自定义源时 _make_samplers 仍返回 coarse（粗 DEM 回退）
        smp, fine, source = dem._make_samplers(37.5, 112.5, 100.0)
        self.assertEqual(source, "coarse")
        self.assertIsNone(fine)


def main():
    unittest.main(verbosity=2)


if __name__ == "__main__":
    main()
