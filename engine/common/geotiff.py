# -*- coding: utf-8 -*-
"""common.geotiff — 最小 GeoTIFF 读取器（离线自主导入高程文件用）。

为什么自己写
------------
工具箱的目标环境**不许装 imagecodecs**，而 ``tifffile`` 解不了 TIFF LZW；
但用户手里的 WGS84 高程 GeoTIFF（如 ``山西WGS84.tif``，1.32 GB BigTIFF、
45670×64942、int16、LZW、128×128 分块）只需要"读一个标签头 + 按块解压"这一小部分能力。
本模块只依赖 **标准库（struct/zlib）+ numpy**，实现：

* classic TIFF（magic 42，4 字节偏移）与 **BigTIFF**（magic 43，8 字节偏移）；
* 必要标签：宽高/位深/压缩/光度/波段/平面配置/预测器/分块或条带偏移与字节数、
  采样格式、大地坐标标签（33550/33922/34264）、GeoKey 目录（34735）与
  GDAL_NODATA（42113）；
* 解压：1（无）、5（TIFF LZW，early-change=1）、8/32946（zlib/deflate）、
  32773（PackBits）；预测器 1/2（水平差分），预测器 3（浮点）明确报错；
* 数据类型：8/16/32 位整型、32/64 位浮点；多波段只取**第 1 波段**；
* 地理参照优先级：ModelTransformation(34264) > ModelPixelScale(33550)+Tiepoint(33922)
  > 同名 ``.tfw`` 世界文件（A D B E C F，C/F 是左上像素**中心**，需减半像素）；
* 只接受**地理坐标（经纬度）**；投影坐标系（ModelType=1 或 ProjectedCSType 存在）
  抛 ``ValueError``，提示"请先转成 WGS84 经纬度 GeoTIFF"。

性能设计
--------
1. **绝不整幅读入**：GeoTIFF 按 128×128 块存，本模块只 seek+read 需要的块；
2. **tile LRU 缓存**：默认 64 块（64×32 KB≈2 MB），避免 1.3 GB 文件读爆内存；
3. **LZW 向量化解码**（numpy）：把码字提取、字典展开都做成批量运算，
   128×128 的 int16 块约 2–3 ms（纯 Python 逐字节解码要 15–30 ms）；
4. **按需解码**：只采样某一行时，解压到该行字节数就停，不必解整块；
5. 目录标签按需读取（TileOffsets 有 18 万个条目，inspect() 只解析头 + 标签，
   实测 < 0.1 s）。

用法
----
::

    from common.geotiff import GeoTiffSource
    g = GeoTiffSource(r"D:\\data\\山西WGS84.tif")
    g.inspect()                      # 元数据（宽高/bbox/分辨率/压缩/nodata…）
    g.sample(37.7615, 112.7252)      # 单点高程 [m]（nodata/越界 → nan）
    g.sample_many(lats, lons)        # 向量化，按块分组 + LRU 缓存

已知限制
--------
* 只读**单幅第一 IFD**（不读 overview/子 IFD）；
* 不支持 JPEG(7)/LZW 以外的压缩、不支持平铺以外的内部掩膜、不支持旋转栅格；
* 多波段只取第 1 波段（高程文件几乎都是单波段）；
* 投影坐标系不支持（需要先用 QGIS/GDAL 转 WGS84 经纬度）。
"""
from __future__ import annotations

import math
import struct
import zlib
from collections import OrderedDict
from pathlib import Path

import numpy as np

__all__ = [
    "GeoTiffSource", "TiffError", "COMPRESSION_NAMES",
    "COMPRESSION_NONE", "COMPRESSION_LZW", "COMPRESSION_DEFLATE",
    "COMPRESSION_PACKBITS",
]

# --------------------------------------------------------------------------- #
# TIFF 常量 / 标签类型
# --------------------------------------------------------------------------- #
COMPRESSION_NONE = 1
COMPRESSION_LZW = 5
COMPRESSION_DEFLATE = 8
COMPRESSION_ADOBE_DEFLATE = 32946
COMPRESSION_PACKBITS = 32773

COMPRESSION_NAMES = {
    1: "none", 2: "ccitt-rle", 3: "ccitt-g3", 4: "ccitt-g4", 5: "lzw",
    6: "jpeg-old", 7: "jpeg", 8: "deflate", 32773: "packbits",
    32946: "deflate(old)",
}

#: TIFF 标签值类型 → (numpy dtype 描述, 字节大小)
_TYPES = {
    1: ("u1", 1),      # BYTE
    2: ("u1", 1),      # ASCII
    3: ("u2", 2),      # SHORT
    4: ("u4", 4),      # LONG
    5: ("u4", 8),      # RATIONAL（分子/分母各 4 字节，按 u4 读）
    6: ("i1", 1),      # SBYTE
    7: ("u1", 1),      # UNDEFINED
    8: ("i2", 2),      # SSHORT
    9: ("i4", 4),      # SLONG
    10: ("i4", 8),     # SRATIONAL
    11: ("f4", 4),     # FLOAT
    12: ("f8", 8),     # DOUBLE
    13: ("u4", 4),     # IFD
    16: ("u8", 8),     # LONG8  (BigTIFF)
    17: ("i8", 8),     # SLONG8 (BigTIFF)
    18: ("u8", 8),     # IFD8   (BigTIFF)
}

#: 标签编号
TAG_WIDTH = 256
TAG_HEIGHT = 257
TAG_BITS = 258
TAG_COMPRESSION = 259
TAG_PHOTOMETRIC = 262
TAG_STRIP_OFFSETS = 273
TAG_SAMPLES_PER_PIXEL = 277
TAG_ROWS_PER_STRIP = 278
TAG_STRIP_BYTE_COUNTS = 279
TAG_PLANAR = 284
TAG_PREDICTOR = 317
TAG_TILE_WIDTH = 322
TAG_TILE_LENGTH = 323
TAG_TILE_OFFSETS = 324
TAG_TILE_BYTE_COUNTS = 325
TAG_SAMPLE_FORMAT = 339
TAG_MODEL_PIXEL_SCALE = 33550
TAG_MODEL_TIEPOINT = 33922
TAG_MODEL_TRANSFORMATION = 34264
TAG_GEO_KEY_DIRECTORY = 34735
TAG_GEO_DOUBLE = 34736
TAG_GEO_ASCII = 34737
TAG_GDAL_NODATA = 42113

#: LZW 码表容量（码字 12 位）
_LZW_TAB = 4096
#: 各码宽的权重（MSB-first 提取码字用）
_LZW_WEIGHTS = {w: (1 << np.arange(w - 1, -1, -1, dtype=np.int32))
                for w in (9, 10, 11, 12)}


class TiffError(ValueError):
    """TIFF 结构/压缩不支持时抛出的错误（ValueError 子类，便于调用方统一捕获）。"""


# --------------------------------------------------------------------------- #
# 解压：LZW / PackBits / zlib / 无
# --------------------------------------------------------------------------- #
def _lzw_extract(bits: np.ndarray, pos: int, width: int, n: int):
    """从位数组 ``bits`` 的 ``pos`` 处按 MSB-first 取 ``n`` 个 ``width`` 位码字。"""
    k = min(int(n), (int(bits.size) - int(pos)) // width)
    if k <= 0:
        return np.empty(0, np.int32), 0
    mat = bits[pos:pos + k * width].reshape(k, width).astype(np.int32)
    return mat @ _LZW_WEIGHTS[width], k


def _lzw_expand(c: np.ndarray, par: np.ndarray, firstb: np.ndarray,
                lastb: np.ndarray, ln: np.ndarray) -> np.ndarray:
    """把一段 LZW 码字展开成字节流（向量化：按"链深度"批量回填）。

    字典项 ``e`` 的字符串 = ``par[e]`` 的字符串 + ``lastb[e]``。逐层（从字符串末尾
    往字符串开头）把 ``lastb`` 填到输出位置上：每层是一次 numpy 花式赋值，
    层数 = 最长字符串长度（DEM 数据典型 10–60 层）。
    """
    lens = ln[c]
    starts = np.cumsum(lens, dtype=np.int64) - lens
    total = int(starts[-1] + lens[-1]) if c.size else 0
    out = np.empty(total, dtype=np.uint8)
    cur = c.astype(np.int32)
    pos = starts + lens - 1
    while cur.size:
        lit = cur < 256
        if lit.any():
            out[pos[lit]] = cur[lit]
        keep = ~lit
        cur = cur[keep]
        pos = pos[keep]
        if not cur.size:
            break
        out[pos] = lastb[cur]
        pos = pos - 1
        cur = par[cur]
    return out


def _lzw_decode(data: bytes, need_bytes: int = 0) -> bytes:
    """TIFF LZW 解压（TS 6.0 spec，early-change=1）。

    Parameters
    ----------
    data : bytes
        压缩数据（一个 tile 或一个 strip）。
    need_bytes : int
        只需要前 ``need_bytes`` 字节时提前停止（0 = 全解）。

    Returns
    -------
    bytes
        解压结果（可能短于 ``need_bytes``，表示码流结束）。
    """
    if not data:
        return b""
    raw = np.frombuffer(data, dtype=np.uint8)
    bits = np.unpackbits(raw)
    nbits = int(bits.size)
    if nbits < 9:
        return b""

    # 码表（跨 clear 段复用，避免反复分配 4096 项数组）
    par = np.full(_LZW_TAB, -1, np.int32)
    firstb = np.full(_LZW_TAB, -1, np.int32)
    lastb = np.zeros(_LZW_TAB, np.uint8)
    ln = np.ones(_LZW_TAB, np.int32)
    firstb[:256] = np.arange(256)
    lastb[:256] = np.arange(256, dtype=np.uint8)

    chunks: list[bytes] = []
    got = 0
    pos = 0
    while pos < nbits:
        codes: list[np.ndarray] = []
        k = 0
        eoi = False
        stalled = False
        # 一段连续码字（遇到 Clear/EOI 或码表满为止）
        while pos < nbits and k < _LZW_TAB:
            if k < 254:
                width, bound = 9, 254
            elif k < 766:
                width, bound = 10, 766
            elif k < 1790:
                width, bound = 11, 1790
            else:
                width, bound = 12, _LZW_TAB
            batch, n_got = _lzw_extract(bits, pos, width, bound - k)
            if n_got <= 0:
                stalled = True           # 剩余位不足一个码字（尾部填充/截断）
                break
            special = np.flatnonzero((batch == 256) | (batch == 257))
            if special.size:
                p = int(special[0])
                if p:
                    codes.append(batch[:p])
                    k += p
                pos += (p + 1) * width
                eoi = int(batch[p]) == 257
                break
            codes.append(batch)
            pos += n_got * width
            k += n_got

        if not codes:
            if eoi or stalled or pos >= nbits:
                break
            continue
        c = codes[0] if len(codes) == 1 else np.concatenate(codes)
        n = int(c.size)
        if n == 0:
            continue
        if int(c[0]) >= 258:
            raise TiffError(f"TIFF LZW 码流非法（首码 {int(c[0])}）")

        # ---- 建字典：位置 i 处新建码表项 e = 258 + (i-1) ----
        n_ent = min(n - 1, _LZW_TAB - 258)
        if n_ent > 0:
            e = np.arange(258, 258 + n_ent, dtype=np.int32)
            src = c[:n_ent]
            if (c[1:n_ent + 1] > e).any():
                raise TiffError("TIFF LZW 码流非法（引用了尚未建立的码表项）")
            par[e] = src
            tab = 258 + int(n_ent)
            cur_t = np.empty(tab, np.int32)
            cnt_t = np.empty(tab, np.int32)
            cur_t[:256] = np.arange(256)
            cnt_t[:256] = 1
            cur_t[256] = 256
            cur_t[257] = 257
            cnt_t[256] = 1
            cnt_t[257] = 1
            cur_t[258:] = src
            cnt_t[258:] = 2
            # 指针倍增：一次求每个新项的"首字节"和"字符串长度"（O(log 深度) 轮）
            for _ in range(13):
                cc = cur_t[258:]
                live = cc >= 256
                if not live.any():
                    break
                idx = np.where(live, cc, 0)
                cnt_t[258:] += np.where(live, cnt_t[idx] - 1, 0)
                cur_t[258:] = np.where(live, cur_t[idx], cc)
            firstb[e] = cur_t[258:]
            ln[e] = cnt_t[258:]
            lastb[e] = firstb[c[1:n_ent + 1]]

        out = _lzw_expand(c, par, firstb, lastb, ln)
        chunks.append(out.tobytes())
        got += out.size
        if eoi or (need_bytes and got >= need_bytes):
            break
    if not chunks:
        return b""
    return chunks[0] if len(chunks) == 1 else b"".join(chunks)


def _packbits_decode(data: bytes, need_bytes: int = 0) -> bytes:
    """TIFF PackBits（压缩码 32773）解压。"""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        h = data[i]
        i += 1
        if h < 128:                       # 0..127：原样复制 h+1 字节
            out += data[i:i + h + 1]
            i += h + 1
        elif h > 128:                     # 129..255：重复下一字节 257-h 次
            if i < n:
                out += data[i:i + 1] * (257 - h)
                i += 1
        if need_bytes and len(out) >= need_bytes:
            break
    return bytes(out)


def _decompress(compression: int, data: bytes, need_bytes: int) -> bytes:
    """按压缩码解压一个 tile/strip。不支持的压缩抛 :class:`TiffError`。"""
    if compression == COMPRESSION_NONE:
        return data if not need_bytes else data[:need_bytes]
    if compression == COMPRESSION_LZW:
        return _lzw_decode(data, need_bytes)
    if compression in (COMPRESSION_DEFLATE, COMPRESSION_ADOBE_DEFLATE):
        try:
            out = zlib.decompress(data)
        except zlib.error:
            try:                          # 少数文件是裸 deflate（无 zlib 头）
                out = zlib.decompress(data, -15)
            except zlib.error as exc:
                raise TiffError(f"deflate 解压失败：{exc}") from exc
        return out if not need_bytes else out[:need_bytes]
    if compression == COMPRESSION_PACKBITS:
        return _packbits_decode(data, need_bytes)
    name = COMPRESSION_NAMES.get(compression, "未知")
    raise TiffError(
        f"不支持的 TIFF 压缩方式 {compression}（{name}）；"
        f"本工具只支持 无压缩 / LZW / deflate(zlib) / PackBits。"
        f"请用 QGIS 或 GDAL（gdal_translate -co COMPRESS=LZW）转存后再导入。")


# --------------------------------------------------------------------------- #
# 标签读取小工具
# --------------------------------------------------------------------------- #
def _geo_value_range_ok(bbox) -> bool:
    """经纬度包围盒是否在 ±180/±90 内（未标注坐标系时用它判断是地理坐标）。"""
    lat_min, lat_max, lon_min, lon_max = bbox
    vals = np.array([lat_min, lat_max, lon_min, lon_max], dtype=float)
    return bool(np.all(np.isfinite(vals))
                and -90.0 - 1e-6 <= lat_min <= lat_max <= 90.0 + 1e-6
                and -180.0 - 1e-6 <= lon_min <= lon_max <= 180.0 + 1e-6)


def _fmt_num(v) -> float:
    """标签值 → float（数组取第一个）。"""
    a = np.asarray(v).ravel()
    return float(a[0]) if a.size else float("nan")


class GeoTiffSource:
    """最小 GeoTIFF 数据源（按块读取 + 经纬度采样）。

    Parameters
    ----------
    path : str or Path
        GeoTIFF 路径（classic TIFF 或 BigTIFF）。
    cache_limit : int
        解压后 tile 的 LRU 缓存块数（默认 64；每块 128×128×2B≈32 KB）。

    Attributes
    ----------
    path : Path
        文件路径。
    width, height : int
        像素列/行数。
    bands : int
        波段数（只用第 1 波段）。
    dtype : numpy.dtype
        样本类型。
    bbox : tuple
        ``(lat_min, lat_max, lon_min, lon_max)`` [deg]。
    res_deg : tuple
        ``(dlat, dlon)`` 像素大小 [deg]。
    crs : str
        坐标系说明（如 ``"EPSG:4326"``；未标注时为空串）。

    Raises
    ------
    TiffError
        文件不是 TIFF / 结构损坏 / 压缩或预测器不支持 / 投影坐标系。
    FileNotFoundError
        文件不存在。
    """

    def __init__(self, path, cache_limit: int = 64):
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"GeoTIFF 不存在：{self.path}")
        self._cache_limit = max(4, int(cache_limit))
        self._cache: "OrderedDict[int, tuple]" = OrderedDict()
        self._fh = None
        self._tag_cache: dict[int, np.ndarray] = {}
        self._tile_offsets = None
        self._tile_counts = None
        self._geo_source = ""
        self._parse()

    # ------------------------------------------------------------------ #
    # 基础 IO / 标签
    # ------------------------------------------------------------------ #
    @property
    def crs(self) -> str:
        """坐标系说明（如 ``"EPSG:4326"``；未标注但坐标在经纬度范围时 ``"unknown(geographic)"``）。"""
        return str(getattr(self, "_crs", ""))

    @property
    def file_size(self) -> int:
        try:
            return int(self.path.stat().st_size)
        except OSError:
            return 0

    def _file(self):
        if self._fh is None:
            self._fh = open(self.path, "rb")
        return self._fh

    def close(self) -> None:
        """关闭底层文件句柄（可重复调用）。"""
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __del__(self):
        try:
            self.close()
        except Exception:                     # noqa: BLE001 - 析构不抛
            pass

    def __repr__(self) -> str:
        try:
            return (f"GeoTiffSource({self.path.name!r}, {self.width}x{self.height}, "
                    f"{self.bands} band, {self.dtype})")
        except Exception:                     # noqa: BLE001
            return f"GeoTiffSource({self.path!r})"

    def _read_at(self, offset: int, size: int) -> bytes:
        fh = self._file()
        fh.seek(int(offset))
        data = fh.read(int(size))
        if len(data) != int(size):
            raise TiffError(f"GeoTIFF 截断：偏移 {offset} 需要 {size} 字节，只读到 {len(data)}")
        return data

    def _type_dtype(self, ttype: int):
        """标签值类型 → 数字 numpy dtype（含文件字节序）。"""
        info = _TYPES.get(int(ttype))
        if info is None:
            return None, 0
        base, size = info
        return np.dtype(self._bo + base), size

    def _tag_values(self, tag: int):
        """读取标签值 → ``(dtype, np.ndarray)``；不存在时返回 ``(None, None)``。"""
        if tag in self._tag_cache:
            v = self._tag_cache[tag]
            return (None, None) if v is None else (v.dtype, v)
        ent = self._tags.get(tag)
        if ent is None:
            self._tag_cache[tag] = None
            return None, None
        ttype, count, raw = ent
        dt, tsize = self._type_dtype(ttype)
        if dt is None:
            self._tag_cache[tag] = None
            return None, None
        nbytes = tsize * int(count)
        if nbytes <= self._value_size:
            data = raw[:nbytes]
        else:
            offset = struct.unpack(self._bo + ("Q" if self._big else "I"), raw)[0]
            data = self._read_at(offset, nbytes)
        if int(count) == 0:
            arr = np.empty(0, dtype=dt)
        else:
            arr = np.frombuffer(data, dtype=dt)
        self._tag_cache[tag] = arr
        return dt, arr

    def _tag_first(self, tag: int, default=np.nan) -> float:
        """标签首值 → float（不存在/非法返回 default）。"""
        _, arr = self._tag_values(tag)
        if arr is None or arr.size == 0:
            return float(default)
        try:
            return float(arr.ravel()[0])
        except (TypeError, ValueError):
            return float(default)

    def _tag_ascii(self, tag: int) -> str:
        """ASCII 标签（如 GDAL_NODATA / GeoAsciiParams）→ str。"""
        fh = self._file()
        ent = self._tags.get(tag)
        if ent is None:
            return ""
        ttype, count, raw = ent
        nbytes = int(count)
        if nbytes <= self._value_size:
            data = raw[:nbytes]
        else:
            offset = struct.unpack(self._bo + ("Q" if self._big else "I"), raw)[0]
            data = self._read_at(offset, nbytes)
        return data.decode("latin-1", "replace").rstrip("\x00").strip()

    # ------------------------------------------------------------------ #
    # 头部解析
    # ------------------------------------------------------------------ #
    def _parse(self) -> None:
        fh = self._file()
        fh.seek(0)
        head = fh.read(16)
        if len(head) < 8:
            raise TiffError(f"不是 TIFF 文件（太短）：{self.path}")
        if head[:2] == b"II":
            self._bo = "<"
        elif head[:2] == b"MM":
            self._bo = ">"
        else:
            raise TiffError(f"不是 TIFF 文件（字节序标记 {head[:2]!r}）：{self.path}")
        magic = struct.unpack(self._bo + "H", head[2:4])[0]
        if magic == 42:
            self._big = False
            self._value_size = 4
            first_ifd = struct.unpack(self._bo + "I", head[4:8])[0]
        elif magic == 43:
            self._big = True
            off_size = struct.unpack(self._bo + "H", head[4:6])[0]
            if off_size != 8:
                raise TiffError(f"BigTIFF 偏移宽度 {off_size} 不支持（应为 8）")
            self._value_size = 8
            if len(head) < 16:
                raise TiffError("BigTIFF 头部截断")
            first_ifd = struct.unpack(self._bo + "Q", head[8:16])[0]
        else:
            raise TiffError(f"不是 TIFF 文件（magic={magic}，应为 42 或 43）：{self.path}")

        # ---- 读第一个 IFD ----
        fh.seek(int(first_ifd))
        if self._big:
            n_ent = struct.unpack(self._bo + "Q", fh.read(8))[0]
            entry_size = 20
        else:
            n_ent = struct.unpack(self._bo + "H", fh.read(2))[0]
            entry_size = 12
        if n_ent <= 0 or n_ent > 65535:
            raise TiffError(f"IFD 条目数异常（{n_ent}）：{self.path}")
        blob = fh.read(int(n_ent) * entry_size)
        if len(blob) != int(n_ent) * entry_size:
            raise TiffError(f"IFD 截断：{self.path}")
        self._tags: dict[int, tuple] = {}
        csize = self._value_size
        for i in range(int(n_ent)):
            base = i * entry_size
            tag, ttype = struct.unpack(self._bo + "HH", blob[base:base + 4])
            if self._big:
                count = struct.unpack(self._bo + "Q", blob[base + 4:base + 12])[0]
                raw = blob[base + 12:base + 20]
            else:
                count = struct.unpack(self._bo + "I", blob[base + 4:base + 8])[0]
                raw = blob[base + 8:base + 8 + csize]
            self._tags[int(tag)] = (int(ttype), int(count), raw)

        # ---- 基本几何 ----
        self.width = int(self._tag_first(TAG_WIDTH, 0))
        self.height = int(self._tag_first(TAG_HEIGHT, 0))
        if self.width <= 0 or self.height <= 0:
            raise TiffError(f"TIFF 宽高非法（{self.width}×{self.height}）：{self.path}")
        _, bits = self._tag_values(TAG_BITS)
        self.bits = int(bits.ravel()[0]) if bits is not None and bits.size else 1
        _, fmt = self._tag_values(TAG_SAMPLE_FORMAT)
        self.sample_format = int(fmt.ravel()[0]) if fmt is not None and fmt.size else 1
        self.bands = int(self._tag_first(TAG_SAMPLES_PER_PIXEL, 1))
        self.planar = int(self._tag_first(TAG_PLANAR, 1))
        self.compression = int(self._tag_first(TAG_COMPRESSION, 1))
        self.predictor = int(self._tag_first(TAG_PREDICTOR, 1))
        self.photometric = int(self._tag_first(TAG_PHOTOMETRIC, 1))
        self._dtype = self._resolve_dtype(self.bits, self.sample_format)
        #: 文件里的样本字节序（MM 大端文件必须按大端解释）；对外 dtype 用本机字节序
        self._file_dtype = self._dtype.newbyteorder(self._bo)
        self.itemsize = int(self._dtype.itemsize)
        self.tile_width = int(self._tag_first(TAG_TILE_WIDTH, 0))
        self.tile_length = int(self._tag_first(TAG_TILE_LENGTH, 0))
        self.tiled = self.tile_width > 0 and self.tile_length > 0
        self.rows_per_strip = int(self._tag_first(TAG_ROWS_PER_STRIP, 0)) or self.height
        if not self.tiled:
            # 条带组织：RowsPerStrip 缺省 = 整幅
            self.rows_per_strip = max(1, min(self.rows_per_strip, self.height))
        self.predictor = self.predictor if self.predictor else 1
        if self.predictor not in (1, 2):
            if self.predictor == 3:
                raise TiffError(
                    "不支持 TIFF 预测器 3（浮点预测器）；请用 gdal_translate "
                    "-co PREDICTOR=1 转存后再导入。")
            raise TiffError(f"不支持的 TIFF 预测器 {self.predictor}")

        # ---- nodata ----
        self.nodata = None
        txt = self._tag_ascii(TAG_GDAL_NODATA)
        if txt:
            try:
                self.nodata = float(txt.split()[0])
            except (ValueError, IndexError):
                self.nodata = None

        # ---- 地理参照 ----
        self._parse_georef()

    @staticmethod
    def _resolve_dtype(bits: int, sample_format: int):
        if bits not in (8, 16, 32, 64):
            raise TiffError(f"不支持的位深 {bits}（只支持 8/16/32/64）")
        if sample_format in (1, 4):           # 无符号 / void
            kind = "u"
        elif sample_format == 2:              # 有符号
            kind = "i"
        elif sample_format == 3:              # 浮点
            kind = "f"
        else:
            raise TiffError(f"不支持的采样格式 {sample_format}（1/2/3）")
        if kind == "f" and bits not in (32, 64):
            raise TiffError(f"浮点样本位深 {bits} 不支持（只支持 32/64）")
        return np.dtype(f"{kind}{bits // 8}")

    # ------------------------------------------------------------------ #
    # 地理参照
    # ------------------------------------------------------------------ #
    def _parse_georef(self) -> None:
        """解析地理参照 → ``_x0/_dx/_y0/_dy``（左上角像素**边缘** + 像素大小）。"""
        self._x0 = self._y0 = float("nan")
        self._dx = self._dy = float("nan")
        self._crs = ""
        self._geo_name = ""
        self._semi_major = None
        self._inv_flattening = None

        # ---- 1) ModelTransformation（4×4，只接受无旋转） ----
        _, m = self._tag_values(TAG_MODEL_TRANSFORMATION)
        if m is not None and m.size >= 16:
            mm = np.asarray(m, dtype=float)
            if abs(mm[1]) > 1e-12 or abs(mm[4]) > 1e-12:
                raise TiffError("不支持带旋转的 GeoTIFF（ModelTransformation 含旋转项）")
            self._dx = float(mm[0])
            self._dy = float(abs(mm[5]))
            self._x0 = float(mm[3])
            self._y0 = float(mm[7])
            self._geo_source = "ModelTransformation"
            if mm[5] > 0:
                raise TiffError("不支持南向上的 GeoTIFF（ModelTransformation 的 Y 比例为正）")

        # ---- 2) ModelPixelScale + ModelTiepoint ----
        if not np.isfinite(self._x0):
            _, sc = self._tag_values(TAG_MODEL_PIXEL_SCALE)
            _, tp = self._tag_values(TAG_MODEL_TIEPOINT)
            if sc is not None and sc.size >= 2 and tp is not None and tp.size >= 6:
                sx = float(sc[0])
                sy = float(sc[1])
                i0, j0 = float(tp[0]), float(tp[1])
                x_t, y_t = float(tp[3]), float(tp[4])
                self._dx = abs(sx) if sx else float("nan")
                self._dy = abs(sy) if sy else float("nan")
                # 反算左上角（i0=0 时即左上角）
                self._x0 = x_t - i0 * float(sx)
                self._y0 = y_t + j0 * float(sy)
                self._geo_source = "ModelPixelScale+ModelTiepoint"

        # ---- 3) 同名 .tfw 世界文件 ----
        if not np.isfinite(self._x0):
            tfw = self.path.with_suffix(".tfw")
            if not tfw.is_file():
                tfw = self.path.with_suffix(".TFW")
            if tfw.is_file():
                try:
                    vals = [float(line.strip()) for line in
                            tfw.read_text(encoding="utf-8", errors="replace").splitlines()
                            if line.strip()]
                    if len(vals) >= 6:
                        a, d, b, e, c, f = vals[:6]
                        if abs(b) > 1e-12 or abs(d) > 1e-12:
                            raise TiffError("不支持带旋转的世界文件（.tfw 的 B/D 非 0）")
                        self._dx = abs(a)
                        self._dy = abs(e)
                        self._x0 = c - a / 2.0      # C/F 是左上像素中心 → 减半像素
                        self._y0 = f - e / 2.0
                        self._geo_source = "world file(.tfw)"
                except (OSError, ValueError) as exc:
                    raise TiffError(f"世界文件 {tfw.name} 解析失败：{exc}") from exc

        if not (np.isfinite(self._x0) and np.isfinite(self._y0)
                and np.isfinite(self._dx) and np.isfinite(self._dy)
                and self._dx > 0 and self._dy > 0):
            raise TiffError(
                f"GeoTIFF 缺少地理参照（无 ModelTransformation / PixelScale+Tiepoint / .tfw）："
                f"{self.path}")

        lon_min = self._x0
        lon_max = self._x0 + self.width * self._dx
        lat_min = self._y0 - self.height * self._dy
        lat_max = self._y0
        self.bbox = (lat_min, lat_max, lon_min, lon_max)
        self.res_deg = (self._dy, self._dx)

        # ---- 坐标系：只接受地理坐标（经纬度） ----
        self._crs = self._read_crs_keys()
        if self._crs is None:
            # 未标注：用坐标值判断是不是经纬度
            if not _geo_value_range_ok(self.bbox):
                raise TiffError(
                    f"GeoTIFF 未标注坐标系，且坐标范围 {tuple(round(v, 3) for v in self.bbox)} "
                    f"超出经纬度范围，疑似投影坐标（如 UTM/WebMercator）。"
                    f"请先转成 WGS84 经纬度 GeoTIFF（EPSG:4326）再导入。")
            self._crs = "unknown(geographic)"
        if not _geo_value_range_ok(self.bbox):
            raise TiffError(
                f"GeoTIFF 坐标范围 {tuple(round(v, 3) for v in self.bbox)} 不是经纬度，"
                f"疑似投影坐标系（{self._crs or '未标注'}）。"
                f"请先转成 WGS84 经纬度 GeoTIFF（EPSG:4326）再导入。")

    def _read_crs_keys(self):
        """读 GeoKeyDirectory → 坐标系字符串；投影坐标系抛 ValueError；无键返回 None。"""
        _, keys = self._tag_values(TAG_GEO_KEY_DIRECTORY)
        if keys is None or keys.size < 4:
            return None
        k = np.asarray(keys, dtype=np.int64).ravel()
        n_keys = int(k[3]) if k.size >= 4 else 0
        geo = {}
        for i in range(n_keys):
            base = 4 + i * 4
            if base + 3 >= k.size:
                break
            key_id, loc, count, value = (int(k[base]), int(k[base + 1]),
                                         int(k[base + 2]), int(k[base + 3]))
            geo[key_id] = (loc, count, value)
        model_type = geo.get(1024, (0, 0, 0))[2]
        proj_cs = geo.get(3072, (0, 0, 0))[2]
        geog_cs = geo.get(2048, (0, 0, 0))[2]
        if model_type == 1 or (proj_cs not in (0, 32767)):
            code = proj_cs if proj_cs not in (0, 32767) else "Projected"
            raise TiffError(
                f"GeoTIFF 是投影坐标系（{code}），本工具只接受 WGS84 经纬度（EPSG:4326）。"
                f"请先用 QGIS/GDAL 转成经纬度：gdalwarp -t_srs EPSG:4326 in.tif out.tif")
        if model_type == 3:
            raise TiffError("GeoTIFF 是地心坐标系，本工具只接受 WGS84 经纬度（EPSG:4326）。")
        if geog_cs and geog_cs != 32767:
            citation = self._geokey_value(geo, 2049)   # GeogCitationGeoKey（GeoAscii）
            self._geo_name = str(citation or "").split("|")[0].strip()
            # 顺带解析椭球参数（GeoDoubleParams 34736），便于 GUI 显示数据基准
            self._semi_major = self._geokey_value(geo, 2057)
            self._inv_flattening = self._geokey_value(geo, 2059)
            return f"EPSG:{geog_cs}"
        return "geographic"

    def _geokey_value(self, geo: dict, key_id: int):
        """按 GeoKey 的 location 解析取值（0=内联、34736=GeoDouble、34737=GeoAscii）。"""
        ent = geo.get(key_id)
        if not ent:
            return None
        loc, count, value = ent
        if loc == 0:
            return int(value)
        if loc == TAG_GEO_DOUBLE:
            _, arr = self._tag_values(TAG_GEO_DOUBLE)
            if arr is not None and 0 <= int(value) < arr.size:
                return float(arr[int(value)])
            return None
        if loc == TAG_GEO_ASCII:
            txt = self._tag_ascii(TAG_GEO_ASCII)
            if txt:
                return txt[int(value):int(value) + max(1, int(count))].rstrip("|")
            return None
        return int(value)

    # ------------------------------------------------------------------ #
    # 块数据
    # ------------------------------------------------------------------ #
    def _load_offsets(self):
        """按需读取 Tile/Strip 偏移与字节数数组（TileOffsets 可达 18 万项）。"""
        if self._tile_offsets is not None:
            return
        if self.tiled:
            _, offs = self._tag_values(TAG_TILE_OFFSETS)
            _, cnts = self._tag_values(TAG_TILE_BYTE_COUNTS)
        else:
            _, offs = self._tag_values(TAG_STRIP_OFFSETS)
            _, cnts = self._tag_values(TAG_STRIP_BYTE_COUNTS)
        if offs is None or cnts is None:
            raise TiffError("GeoTIFF 缺少块偏移/字节数标签（TileOffsets/StripOffsets）")
        if self.tiled:
            across = (self.width + self.tile_width - 1) // self.tile_width
            down = (self.height + self.tile_length - 1) // self.tile_length
            expect = across * down * (self.bands if self.planar == 2 else 1)
        else:
            n_strips = (self.height + self.rows_per_strip - 1) // self.rows_per_strip
            expect = n_strips * (self.bands if self.planar == 2 else 1)
        if offs.size < expect or cnts.size < expect:
            raise TiffError(
                f"GeoTIFF 块表长度不符（需要 {expect}，实际 {offs.size}/{cnts.size}）")
        self._tile_offsets = np.asarray(offs[:expect], dtype=np.int64)
        self._tile_counts = np.asarray(cnts[:expect], dtype=np.int64)
        if self.tiled:
            self._tiles_across = across
            self._tiles_down = down
            self._block_bytes = (self.tile_width * self.tile_length * self.bands
                                 * self.itemsize)
        else:
            self._n_strips = expect
            self._row_bytes = self.width * self.bands * self.itemsize

    def _block_shape(self, key: int):
        """块 key → (块的像素行数, 像素列数, 数据区内的行/列偏移)。

        平铺块的列数 = tile_width（边缘块也有填充）；条带块列数 = width。
        """
        if self.tiled:
            tr = key // self._tiles_across
            tc = key % self._tiles_across
            return (self.tile_length, self.tile_width, tr, tc)
        rows = min(self.rows_per_strip, self.height - key * self.rows_per_strip)
        return (max(rows, 0), self.width, key, 0)

    def _decode_block(self, key: int, need_rows: int = 0):
        """解压第 ``key`` 块 → (2D 数组（第 1 波段）, 有效行数)。

        Parameters
        ----------
        key : int
            块序号（平铺块按行优先；条带按条带序号）。
        need_rows : int
            只需前 N 行（0 = 全块）；用于"只采样某一行时不必解整块"。
        """
        self._load_offsets()
        rows_expect, cols, tr, tc = self._block_shape(key)
        if rows_expect <= 0:
            return np.empty((0, 0), dtype=self._dtype), 0
        want_rows = rows_expect if need_rows <= 0 else min(int(need_rows), rows_expect)
        nbytes = want_rows * cols * self.bands * self.itemsize
        offset = int(self._tile_offsets[key])
        count = int(self._tile_counts[key])
        if count <= 0 or offset <= 0:
            # 空白块（全填充/未写）：按 nodata 处理
            return np.full((rows_expect, cols), self._nodata_value(), dtype=self._dtype), rows_expect
        data = self._read_at(offset, count)
        raw = _decompress(self.compression, data, nbytes)
        n_samples = len(raw) // self.itemsize
        if n_samples <= 0:
            return np.empty((0, 0), dtype=self._dtype), 0
        n_rows = min(rows_expect, n_samples // (cols * self.bands))
        if n_rows <= 0:
            return np.empty((0, 0), dtype=self._dtype), 0
        n_use = n_rows * cols * self.bands
        arr = np.frombuffer(raw[:n_use * self.itemsize], dtype=self._file_dtype)
        if self.bands > 1:
            arr = arr.reshape(n_rows, cols, self.bands)[:, :, 0]
        else:
            arr = arr.reshape(n_rows, cols)
        # 转成本机字节序的独立数组（frombuffer 的视图只读，且 BE 文件需要换序）
        arr = np.array(arr, dtype=self._dtype, copy=True)
        if self.predictor == 2:
            if self._dtype.kind == "f":
                raise TiffError("预测器 2（水平差分）不能用于浮点样本，文件疑似损坏")
            udt = np.dtype(f"u{self.itemsize}")
            u = arr.view(udt)
            arr = np.cumsum(u, axis=1, dtype=udt).view(self._dtype)
        return arr, n_rows

    def _nodata_value(self):
        if self.nodata is None:
            return self._dtype.type(0)
        try:
            if self._dtype.kind in "iu":
                return self._dtype.type(int(round(self.nodata)))
            return self._dtype.type(self.nodata)
        except (ValueError, OverflowError):
            return self._dtype.type(0)

    def _get_block(self, key: int, need_rows: int = 0):
        """取块（带 LRU 缓存）→ (2D 数组, 有效行数)。"""
        item = self._cache.get(key)
        if item is not None:
            arr, valid = item
            if valid >= need_rows or need_rows <= 0:
                self._cache.move_to_end(key)
                return arr, valid
            # 缓存的行数不够：重解（LZW 只能从头解）
        arr, valid = self._decode_block(key, need_rows)
        self._cache[key] = (arr, valid)
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_limit:
            self._cache.popitem(last=False)
        return arr, valid

    # ------------------------------------------------------------------ #
    # 元数据
    # ------------------------------------------------------------------ #
    def inspect(self) -> dict:
        """元数据摘要（GUI 导入预览/CLI 用）。

        Returns
        -------
        dict
            ``width, height, bands, dtype, compression, tiled, bbox,
            res_deg, crs, nodata, file_size``（另有若干补充字段）。
        """
        lat_min, lat_max, lon_min, lon_max = self.bbox
        return {
            "path": str(self.path),
            "width": int(self.width),
            "height": int(self.height),
            "bands": int(self.bands),
            "dtype": self._dtype.name,
            "bits": int(self.bits),
            "sample_format": int(self.sample_format),
            "compression": int(self.compression),
            "compression_name": COMPRESSION_NAMES.get(self.compression, "?"),
            "predictor": int(self.predictor),
            "tiled": bool(self.tiled),
            "tile_size": [int(self.tile_width), int(self.tile_length)]
            if self.tiled else [int(self.width), int(self.rows_per_strip)],
            "big_tiff": bool(self._big),
            "planar": int(self.planar),
            "bbox": [round(float(v), 8) for v in (lat_min, lat_max, lon_min, lon_max)],
            "lat_min": round(float(lat_min), 8),
            "lat_max": round(float(lat_max), 8),
            "lon_min": round(float(lon_min), 8),
            "lon_max": round(float(lon_max), 8),
            "res_deg": [float(self._dy), float(self._dx)],
            "crs": str(self._crs),
            "geo_source": str(self._geo_source),
            "geo_name": str(getattr(self, "_geo_name", "")),
            "semi_major_axis": getattr(self, "_semi_major", None),
            "inv_flattening": getattr(self, "_inv_flattening", None),
            "nodata": (None if self.nodata is None else float(self.nodata)),
            "file_size": self.file_size,
        }

    # ------------------------------------------------------------------ #
    # 覆盖判断
    # ------------------------------------------------------------------ #
    def covers(self, lat, lon, margin_deg: float = 0.0) -> bool:
        """点 ``(lat, lon)`` 是否在该影像范围内（可选外扩 ``margin_deg`` 度）。"""
        try:
            la = float(lat)
            lo = float(lon)
        except (TypeError, ValueError):
            return False
        if not (math.isfinite(la) and math.isfinite(lo)):
            return False
        lat_min, lat_max, lon_min, lon_max = self.bbox
        m = float(margin_deg)
        return (lat_min - m <= la <= lat_max + m) and (lon_min - m <= lo <= lon_max + m)

    def covers_mask(self, lats, lons, margin_deg: float = 0.0) -> np.ndarray:
        """向量化的 :meth:`covers`（返回 bool 数组）。"""
        la = np.asarray(lats, dtype=float)
        lo = np.asarray(lons, dtype=float)
        la, lo = np.broadcast_arrays(la, lo)
        lat_min, lat_max, lon_min, lon_max = self.bbox
        m = float(margin_deg)
        return (np.isfinite(la) & np.isfinite(lo)
                & (la >= lat_min - m) & (la <= lat_max + m)
                & (lo >= lon_min - m) & (lo <= lon_max + m))

    def bbox_deg(self):
        """``(lat_min, lat_max, lon_min, lon_max)``（与 :attr:`bbox` 相同）。"""
        return tuple(self.bbox)

    # ------------------------------------------------------------------ #
    # 像素索引
    # ------------------------------------------------------------------ #
    def _pixel_xy(self, lat, lon):
        """经纬度 → (浮点列坐标, 浮点行坐标)，像素**中心**为整数。

        返回的坐标若在 ``[-0.5, size-0.5]`` 之外即为越界。
        """
        la = np.asarray(lat, dtype=float)
        lo = np.asarray(lon, dtype=float)
        fx = (lo - self._x0) / self._dx - 0.5
        fy = (self._y0 - la) / self._dy - 0.5
        return fx, fy

    def _gather(self, rows: np.ndarray, cols: np.ndarray,
                need_rows_first: int = 0) -> np.ndarray:
        """按 (row, col) 取值 → float 数组；nodata/不可用 → nan。

        按块分组：每块只解压一次，并只解到该组最大行号。
        """
        n = int(rows.size)
        out = np.full(n, np.nan, dtype=float)
        if n == 0:
            return out
        rows = np.asarray(rows, dtype=np.int64)
        cols = np.asarray(cols, dtype=np.int64)
        self._load_offsets()
        if self.tiled:
            tr = rows // self.tile_length
            tc = cols // self.tile_width
            keys = tr * self._tiles_across + tc
            local_r = rows - tr * self.tile_length
            local_c = cols - tc * self.tile_width
        else:
            keys = rows // self.rows_per_strip
            local_r = rows - keys * self.rows_per_strip
            local_c = cols
        nodata = self.nodata
        order = np.argsort(keys, kind="stable")
        k_sorted = keys[order]
        uniq, starts = np.unique(k_sorted, return_index=True)
        ends = np.append(starts[1:], n)
        for key, s0, s1 in zip(uniq, starts, ends):
            idx = order[s0:s1]
            lr = local_r[idx]
            lc = local_c[idx]
            need = int(lr.max()) + 1
            if need_rows_first and need < need_rows_first:
                need = need_rows_first
            arr, valid = self._get_block(int(key), need)
            if valid <= 0 or arr.size == 0:
                continue
            ok = lr < valid
            if not ok.all():
                lr = lr[ok]
                lc = lc[ok]
                idx = idx[ok]
                if idx.size == 0:
                    continue
            vals = np.asarray(arr[lr, lc])
            if nodata is not None:
                if math.isnan(nodata):
                    pass
                else:
                    try:
                        vals = np.where(vals == self._dtype.type(nodata),
                                        np.nan, vals.astype(float))
                    except (TypeError, ValueError, OverflowError):
                        vals = vals.astype(float)
            out[idx] = vals
        return out

    # ------------------------------------------------------------------ #
    # 采样
    # ------------------------------------------------------------------ #
    def height_at_xy(self, row, col) -> float:
        """整数像素 (row, col) 的高程 [m]；越界/nodata → nan。"""
        try:
            r = int(row)
            c = int(col)
        except (TypeError, ValueError):
            return float("nan")
        if not (0 <= r < self.height and 0 <= c < self.width):
            return float("nan")
        return float(self._gather(np.array([r]), np.array([c]))[0])

    def sample_many(self, lats, lons, method: str = "bilinear") -> np.ndarray:
        """向量化点采样 [m]。

        Parameters
        ----------
        lats, lons : array_like
            经纬度 [deg]（可广播）。
        method : {'bilinear', 'nearest'}
            双线性（默认）或最近邻。

        Returns
        -------
        ndarray float64
            高程 [m]；越界、nodata → ``nan``。双线性时四角含 nodata 即返回 nan。
        """
        if method not in ("bilinear", "nearest"):
            raise ValueError(f"未知 method={method!r}，可选 bilinear / nearest")
        la = np.asarray(lats, dtype=float)
        lo = np.asarray(lons, dtype=float)
        la, lo = np.broadcast_arrays(la, lo)
        out = np.full(la.shape, np.nan, dtype=float)
        if la.size == 0:
            return out
        fx, fy = self._pixel_xy(la, lo)
        eps = 1e-9
        ok = (np.isfinite(fx) & np.isfinite(fy)
              & (fx >= -0.5 - eps) & (fx <= self.width - 0.5 + eps)
              & (fy >= -0.5 - eps) & (fy <= self.height - 0.5 + eps))
        if not ok.any():
            return out
        idx = np.flatnonzero(ok.ravel())
        fxv = fx.ravel()[idx]
        fyv = fy.ravel()[idx]
        if method == "nearest":
            cc = np.clip(np.rint(fxv), 0, self.width - 1).astype(np.int64)
            rr = np.clip(np.rint(fyv), 0, self.height - 1).astype(np.int64)
            vals = self._gather(rr, cc)
        else:
            fxc = np.clip(fxv, 0.0, self.width - 1.0)
            fyc = np.clip(fyv, 0.0, self.height - 1.0)
            c0 = np.floor(fxc).astype(np.int64)
            r0 = np.floor(fyc).astype(np.int64)
            fc = fxc - c0
            fr = fyc - r0
            c1 = np.minimum(c0 + 1, self.width - 1)
            r1 = np.minimum(r0 + 1, self.height - 1)
            # 四个角点**一次**分组取值：同一块的四个角只解压一次（缓存不会被中途挤掉）
            m = idx.size
            rows4 = np.concatenate([r0, r1, r0, r1])
            cols4 = np.concatenate([c0, c0, c1, c1])
            v4 = self._gather(rows4, cols4)
            v00, v10, v01, v11 = v4[:m], v4[m:2 * m], v4[2 * m:3 * m], v4[3 * m:]
            vals = (v00 * (1.0 - fr) * (1.0 - fc) + v10 * fr * (1.0 - fc)
                    + v01 * (1.0 - fr) * fc + v11 * fr * fc)
        out.ravel()[idx] = vals
        return out

    def sample(self, lat, lon, method: str = "bilinear") -> float:
        """单点采样 [m]；越界/nodata → nan。"""
        v = self.sample_many(np.asarray(lat, dtype=float).ravel(),
                             np.asarray(lon, dtype=float).ravel(),
                             method=method)
        return float(v[0]) if v.size else float("nan")

    # ------------------------------------------------------------------ #
    # 其它
    # ------------------------------------------------------------------ #
    def parts_ascii(self) -> str:
        """GeoAsciiParams 原文（用于坐标系名称）。"""
        return self._tag_ascii(TAG_GEO_ASCII)

    def clear_cache(self) -> None:
        """清空 tile 缓存（释放内存）。"""
        self._cache.clear()
