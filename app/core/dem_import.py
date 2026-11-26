# -*- coding: utf-8 -*-
"""dem_import.py — 离线自主导入高程文件（GeoTIFF / HGT / HGT.GZ / ZIP）。

用途
----
用户在没有网络、下载不到 SRTM 瓦片时，可以把自己的高程数据拖进工具箱：

* **GeoTIFF**（.tif/.tiff，classic 或 BigTIFF，LZW/deflate/无压缩）→ 登记到
  ``<dem_dir>/custom/index.json``，运行时由 ``engine/common/dem.py`` 优先采样；
  默认**引用原文件**（不复制 1.3 GB），可选 ``copy=True`` 复制到本机缓存目录。
* **SRTM HGT**（.hgt / .hgt.gz / 含 .hgt 的 .zip）→ 解压/复制到
  ``<dem_dir>/hgt/<TILE>.hgt``，走引擎原有 1″ 瓦片路径（``dem.sample`` 直接可用）。
* **目录** → 递归扫描上述扩展名逐个导入。

设计要点
--------
1. ``index.json`` 结构 ``{"schema": "rocketkit.dem_custom.v1", "updated_utc": ..., "sources": [...]}``；
   读写容错：JSON 损坏时备份为 ``index.json.bad-<时间戳>`` 并重建，不崩溃；
   写入用 ``.part`` + 原子替换。
2. GeoTIFF 导入前先用 :class:`common.geotiff.GeoTiffSource` **验证**（能解析头、
   地理坐标、压缩方式支持），不合格的文件直接报错，不会把坏数据写进索引。
3. .hgt 只接受 1″（3601×3601×2 字节）；3″（1201）明确报错，避免"登记了却不参与计算"。
4. .zip 解压前做**路径穿越**防护（拒绝绝对路径、盘符、``..``、符号链接）。
5. 中文日志，UTF-8。

接口
----
::

    from app.core.dem_import import DemImportManager
    mgr = DemImportManager(dem_dir, logger=print)
    info = mgr.import_path(r"D:\\data\\山西WGS84.tif")       # 引用模式
    mgr.import_path(r"D:\\data\\N37E112.hgt")                # 1″ 瓦片
    mgr.list_sources()                                       # 已登记的 GeoTIFF
    mgr.coverage([("发射场", 38.5, 112.0), ...])             # 覆盖率
    mgr.remove_source(id)
"""
from __future__ import annotations

import gzip
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path


__all__ = ["DemImportManager", "SCHEMA", "HGT_BYTES", "HGT_1201_BYTES",
           "GEO_EXTENSIONS", "HGT_EXTENSIONS"]

#: index.json 结构版本
SCHEMA = "rocketkit.dem_custom.v1"
#: 1″ SRTM 瓦片字节数（3601×3601×int16）
HGT_BYTES = 3601 * 3601 * 2
#: 3″ SRTM 瓦片字节数（1201×1201×int16，本工具箱暂不支持）
HGT_1201_BYTES = 1201 * 1201 * 2

GEO_EXTENSIONS = (".tif", ".tiff")
HGT_EXTENSIONS = (".hgt", ".hgt.gz")
ZIP_EXTENSIONS = (".zip",)

#: HGT 瓦片名（N37E112 / S01W070）
_TILE_RE = re.compile(r"^([NS])(\d{2})([EW])(\d{3})$")

#: zip 里允许解压出来的扩展名（tif 的 sidecar 一起解，保证地理参照可用）
_ZIP_ALLOWED = (".hgt", ".hgt.gz", ".tif", ".tiff", ".tfw", ".ovr")

#: 复制缓冲（4 MiB）
_COPY_CHUNK = 1 << 22

#: 单源覆盖采样的最大点数（超过则只按 bbox 统计，避免读大文件太久）
_COVERAGE_SAMPLE_LIMIT = 200


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _human_bytes(n: int) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024.0 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} GB"


def _slug(text: str, limit: int = 60) -> str:
    """文件名/名称 → 安全的 slug（保留中文、字母数字、下划线、短横线）。"""
    s = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", str(text or ""), flags=re.UNICODE)
    s = re.sub(r"_+", "_", s).strip("_-")
    return (s or "dem")[:limit]


def _tile_name_of(stem: str) -> str:
    """文件名（不含扩展名）→ 规范瓦片名；非法返回 ''。"""
    m = _TILE_RE.match(str(stem or "").strip().upper())
    if not m:
        return ""
    return m.group(1) + m.group(2) + m.group(3) + m.group(4)


def _zip_member_safe(name: str) -> bool:
    """zip 成员名是否安全（防路径穿越 / 绝对路径 / 盘符）。"""
    raw = str(name or "").replace("\\", "/")
    if not raw or raw.startswith("/") or "\x00" in raw:
        return False
    if re.match(r"^[A-Za-z]:", raw):
        return False
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts:
        return False
    return all(p != ".." for p in parts)


def _zip_member_is_link(info: zipfile.ZipInfo) -> bool:
    """zip 里的符号链接（unix mode 的 S_IFLNK 位）。"""
    try:
        return (info.external_attr >> 16) & 0o170000 == 0o120000
    except (AttributeError, TypeError):
        return False


class DemImportManager:
    """高程文件导入 / 索引 / 覆盖率管理。

    Parameters
    ----------
    dem_dir : str or Path
        DEM 可写根目录（部署态 ``paths.dem_dir()``，开发态 ``engine/data/dem``）。
        目录结构：``hgt/``、``gz/``、``cache/``、``custom/index.json``、
        ``custom/data/``（copy=True 时的副本）。
    logger : callable or logging.Logger, optional
        日志回调 ``logger("消息")`` 或标准 logger；缺省打印到 stdout。
    """

    def __init__(self, dem_dir, logger=None):
        self.dem_dir = Path(dem_dir)
        self.dir_hgt = self.dem_dir / "hgt"
        self.dir_gz = self.dem_dir / "gz"
        self.dir_custom = self.dem_dir / "custom"
        self.dir_data = self.dir_custom / "data"
        self.index_path = self.dir_custom / "index.json"
        self.logger = logger
        self._log_lines: list[str] = []
        self._valid_cache: dict[tuple, tuple] = {}
        for d in (self.dem_dir, self.dir_hgt, self.dir_gz, self.dir_custom):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    # 日志 / 进度
    # ------------------------------------------------------------------ #
    def _log(self, msg: str) -> None:
        line = f"[dem-import] {msg}"
        self._log_lines.append(line)
        if len(self._log_lines) > 500:
            del self._log_lines[:100]
        if self.logger is None:
            print(line, flush=True)
            return
        try:
            if callable(self.logger) and not hasattr(self.logger, "info"):
                self.logger(line)
            else:
                self.logger.info(line)
        except Exception:                       # noqa: BLE001 - 日志失败不影响主流程
            print(line, flush=True)

    @staticmethod
    def _emit(progress, done: int, total: int, note: str = "") -> None:
        """进度回调：兼容 ``(done,total,note)`` / ``(fraction)`` / ``(note)`` 三种签名。"""
        if progress is None:
            return
        total = max(int(total), 1)
        try:
            progress(done, total, note)
            return
        except TypeError:
            pass
        except Exception:                       # noqa: BLE001 - 回调自身异常忽略
            return
        try:
            progress(done / total)              # noqa: B915
            return
        except Exception:                       # noqa: BLE001
            pass
        if note:
            try:
                progress(note)
            except Exception:                   # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    # index.json 读写
    # ------------------------------------------------------------------ #
    def _empty_index(self) -> dict:
        return {"schema": SCHEMA, "updated_utc": _utc_now(), "sources": []}

    def load_index(self) -> dict:
        """读取 ``custom/index.json``（容错：损坏时备份为 ``.bad-<时间戳>`` 并重建）。"""
        if not self.index_path.is_file():
            return self._empty_index()
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
                raise ValueError("结构不符（缺 sources 列表）")
        except Exception as exc:                # noqa: BLE001 - 损坏就备份重建
            stamp = time.strftime("%Y%m%d_%H%M%S")
            bad = self.index_path.with_name(f"index.json.bad-{stamp}")
            try:
                shutil.copy2(self.index_path, bad)
            except OSError:
                bad = None
            self._log(f"索引损坏（{exc}），已备份为 {bad.name if bad else '（备份失败）'}"
                      f"，将重建空索引")
            return self._empty_index()
        data.setdefault("schema", SCHEMA)
        data.setdefault("updated_utc", _utc_now())
        keep = []
        for item in data["sources"]:
            if isinstance(item, dict) and item.get("id") and item.get("file"):
                keep.append(item)
        data["sources"] = keep
        return data

    def save_index(self, data: dict) -> None:
        """原子写入索引（先写 .part 再替换，UTF-8、不转义中文）。"""
        data = dict(data or {})
        data["schema"] = SCHEMA
        data["updated_utc"] = _utc_now()
        try:
            self.dir_custom.mkdir(parents=True, exist_ok=True)
            tmp = self.index_path.with_name("index.json.part")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(self.index_path)
        except OSError as exc:
            self._log(f"写索引失败：{exc}")
            raise

    # ------------------------------------------------------------------ #
    # 单个文件导入（自动识别类型）
    # ------------------------------------------------------------------ #
    def import_path(self, path, copy: bool = False, name: str = None,
                    progress=None) -> dict:
        """导入一个文件/目录（自动识别 .tif/.tiff/.hgt/.hgt.gz/.zip/目录）。

        Parameters
        ----------
        path : str or Path
            文件或目录路径（只读原文件；copy=True 时才复制）。
        copy : bool
            GeoTIFF 是否复制到 ``<dem_dir>/custom/data/``（默认 False = 引用原文件，
            适合 1.3 GB 的大文件；hgt/gz 总是复制到 dem_dir）。
        name : str, optional
            导入后显示的名称（默认用文件名）。
        progress : callable, optional
            进度回调：``progress(done, total, note)`` 或 ``progress(fraction)``。

        Returns
        -------
        dict
            ``{"ok": n, "failed": [{"path","error"}], "sources": [...], "tiles": [...]}``
        """
        result = {"ok": 0, "failed": [], "sources": [], "tiles": []}
        try:
            p = Path(str(path)).expanduser()
        except (TypeError, ValueError) as exc:
            result["failed"].append({"path": str(path), "error": f"路径非法：{exc}"})
            return result
        if p.is_dir():
            self._import_dir(p, copy, name, progress, result)
            return result
        if not p.is_file():
            result["failed"].append({"path": str(p), "error": "文件不存在"})
            return result
        try:
            self._import_one(p, copy, name, progress, result)
        except Exception as exc:                # noqa: BLE001 - 单个失败不影响其余
            result["failed"].append({"path": str(p), "error": str(exc)})
            self._log(f"导入失败 {p.name}：{exc}")
        self._log(f"导入完成：成功 {result['ok']}，失败 {len(result['failed'])}"
                  f"（{p.name if p.is_file() else p}）")
        return result

    def _import_one(self, p: Path, copy: bool, name, progress, result: dict) -> None:
        low = p.name.lower()
        if low.endswith(ZIP_EXTENSIONS):
            self._import_zip(p, copy, name, progress, result)
        elif low.endswith(".hgt.gz"):
            tile = self._import_hgt_gz(p, progress)
            result["tiles"].append(tile)
            result["ok"] += 1
        elif low.endswith(".hgt"):
            tile = self._import_hgt(p, progress)
            result["tiles"].append(tile)
            result["ok"] += 1
        elif low.endswith(GEO_EXTENSIONS):
            rec = self._import_geotiff(p, copy, name, progress)
            result["sources"].append(rec)
            result["ok"] += 1
        else:
            raise ValueError(f"不支持的文件类型：{p.suffix or p.name}"
                             f"（支持 .tif/.tiff/.hgt/.hgt.gz/.zip 或目录）")

    def _import_dir(self, root: Path, copy: bool, name, progress, result: dict) -> None:
        """递归扫描目录（跳过隐藏/缓存目录），逐个导入。"""
        exts = GEO_EXTENSIONS + HGT_EXTENSIONS + ZIP_EXTENSIONS
        files: list[Path] = []
        skip = {"__pycache__", ".git", ".svn", "node_modules", ".venv", "venv"}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d.lower() not in skip]
            for fn in sorted(filenames):
                if fn.startswith("."):
                    continue
                if fn.lower().endswith(exts):
                    files.append(Path(dirpath) / fn)
        self._log(f"扫描目录 {root}：找到 {len(files)} 个候选高程文件")
        total = len(files)
        for i, f in enumerate(files, 1):
            self._emit(progress, i - 1, total, f"导入 {f.name}（{i}/{total}）")
            try:
                self._import_one(f, copy, name if total == 1 else None, progress, result)
            except Exception as exc:            # noqa: BLE001
                result["failed"].append({"path": str(f), "error": str(exc)})
                self._log(f"  跳过 {f.name}：{exc}")
        self._emit(progress, total, total, "目录导入完成")
        self._log(f"目录导入完成：成功 {result['ok']}，失败 {len(result['failed'])}")

    # ------------------------------------------------------------------ #
    # GeoTIFF
    # ------------------------------------------------------------------ #
    @staticmethod
    def _geotiff_class():
        """懒加载 ``engine/common/geotiff.py``（只依赖 numpy + 标准库）。"""
        try:
            from common.geotiff import GeoTiffSource       # 已把 engine 加入 sys.path
            return GeoTiffSource
        except Exception:                                  # noqa: BLE001 - 直接脚本运行
            here = Path(__file__).resolve()
            engine = Path(os.environ.get("RK_ENGINE")
                          or (here.parents[2] / "engine"))
            if str(engine) not in sys.path:
                sys.path.insert(0, str(engine))
            from common.geotiff import GeoTiffSource       # type: ignore
            return GeoTiffSource

    def _validate_geotiff(self, p: Path) -> tuple:
        """打开 GeoTIFF 校验并返回 (GeoTiffSource 实例, inspect 字典)。"""
        cls = self._geotiff_class()
        src = cls(p)
        try:
            info = src.inspect()
        finally:
            src.close()
        return src, info

    def _import_geotiff(self, p: Path, copy: bool, name, progress) -> dict:
        p = p.resolve()
        _, info = self._validate_geotiff(p)
        bbox = info["bbox"]
        lat_span = bbox[1] - bbox[0]
        lon_span = bbox[3] - bbox[2]
        # 只读取头，不读像素；这里再确认文件大小 > 0
        size = int(info.get("file_size") or 0)
        if size <= 0:
            raise ValueError("文件为空")
        self._log(
            f"GeoTIFF 校验通过：{p.name} {info['width']}×{info['height']} "
            f"{info['dtype']} {info['compression_name']}"
            f"{' 分块' if info['tiled'] else ' 条带'}，"
            f"范围 lat {bbox[0]:.4f}..{bbox[1]:.4f}, lon {bbox[2]:.4f}..{bbox[3]:.4f}"
            f"（{lat_span:.2f}°×{lon_span:.2f}°，{_human_bytes(size)}），"
            f"坐标系 {info['crs']}，nodata={info['nodata']}")

        sidecars_src = self._find_sidecars(p)
        dest_file = None
        sidecars = []
        if copy:
            self.dir_data.mkdir(parents=True, exist_ok=True)
            slug = _slug(name if name else p.stem)
            ext = p.suffix.lower()
            dest = self.dir_data / f"{slug}{ext}"
            dest = self._unique_path(dest)
            self._log(f"复制 GeoTIFF → {dest}（{_human_bytes(size)}，引用模式可不复制）")
            self._copy_file(p, dest, size, progress)
            dest_file = str(dest)
            for sc in sidecars_src:
                sc_dst = dest.with_suffix(sc.suffix)
                try:
                    shutil.copy2(sc, sc_dst)
                    sidecars.append(str(sc_dst))
                except OSError as exc:          # noqa: BLE001 - sidecar 可选
                    self._log(f"  sidecar 复制失败（忽略）{sc.name}: {exc}")
            register_file = dest_file
            note = f"已复制到 custom/data（原文件 {p}）"
        else:
            register_file = str(p)
            sidecars = [str(s) for s in sidecars_src]
            note = "引用原文件（未复制；移动/删除原文件后需重新导入）"

        rec = {
            "id": "",
            "kind": "geotiff",
            "name": str(name or p.stem),
            "file": register_file,
            "original": str(p),
            "copied": bool(copy),
            "bbox": [round(float(v), 8) for v in bbox],
            "lat_min": round(float(bbox[0]), 8),
            "lat_max": round(float(bbox[1]), 8),
            "lon_min": round(float(bbox[2]), 8),
            "lon_max": round(float(bbox[3]), 8),
            "res_deg": [float(info["res_deg"][0]), float(info["res_deg"][1])],
            "width": int(info["width"]),
            "height": int(info["height"]),
            "bands": int(info["bands"]),
            "dtype": str(info["dtype"]),
            "compression": int(info["compression"]),
            "compression_name": str(info["compression_name"]),
            "tiled": bool(info["tiled"]),
            "big_tiff": bool(info["big_tiff"]),
            "nodata": info["nodata"],
            "crs": str(info["crs"]),
            "size": int(size),
            "sidecars": sidecars,
            "added_utc": _utc_now(),
            "note": note,
        }
        idx = self.load_index()
        existing = None
        for item in idx["sources"]:
            try:
                if str(Path(item["file"]).resolve()).lower() == \
                        str(Path(register_file).resolve()).lower():
                    existing = item
                    break
            except OSError:
                continue
        if existing is not None:
            rec["id"] = existing["id"]
            rec["added_utc"] = existing.get("added_utc") or rec["added_utc"]
            idx["sources"] = [rec if it is existing else it for it in idx["sources"]]
            self._log(f"该文件已在索引中，更新记录 id={rec['id']}")
        else:
            rec["id"] = self._new_id(idx, name or p.stem)
            idx["sources"].append(rec)
        self.save_index(idx)
        self._log(f"已登记自定义高程源 id={rec['id']}，bbox="
                  f"lat {bbox[0]:.4f}..{bbox[1]:.4f}, lon {bbox[2]:.4f}..{bbox[3]:.4f}")
        return rec

    @staticmethod
    def _find_sidecars(p: Path) -> list[Path]:
        """.tfw / .tif.ovr（GDAL 外置金字塔）等旁挂文件。"""
        out: list[Path] = []
        for cand in (p.with_suffix(".tfw"), p.with_suffix(".TFW"),
                     p.with_suffix(p.suffix + ".ovr")):
            try:
                if cand.is_file() and cand != p:
                    out.append(cand)
            except OSError:
                continue
        return out

    @staticmethod
    def _unique_path(path: Path) -> Path:
        """目标已存在时加 -2/-3 后缀，不覆盖已有副本。"""
        if not path.exists():
            return path
        stem, suf = path.stem, path.suffix
        for i in range(2, 1000):
            cand = path.with_name(f"{stem}-{i}{suf}")
            if not cand.exists():
                return cand
        return path.with_name(f"{stem}-{int(time.time())}{suf}")

    def _new_id(self, idx: dict, display_name: str) -> str:
        base = _slug(display_name, 40) or "dem"
        used = {str(s.get("id")) for s in idx.get("sources", [])}
        if base not in used:
            return base
        for i in range(2, 1000):
            cand = f"{base}-{i}"
            if cand not in used:
                return cand
        return f"{base}-{int(time.time())}"

    def _copy_file(self, src: Path, dst: Path, total: int = 0, progress=None) -> None:
        """流式复制（分块 + 进度回调 + 先写 .part 再原子替换）。"""
        total = int(total or src.stat().st_size)
        tmp = dst.with_name(dst.name + ".part")
        done = 0
        t0 = time.time()
        try:
            with open(src, "rb") as fin, open(tmp, "wb") as fout:
                while True:
                    chunk = fin.read(_COPY_CHUNK)
                    if not chunk:
                        break
                    fout.write(chunk)
                    done += len(chunk)
                    self._emit(progress, done, total,
                               f"复制 {src.name} {done * 100 // max(total, 1)}%")
                    if time.time() - t0 > 1.0 and progress is None:
                        print(f"[progress {done}/{total}] 复制 {src.name} "
                              f"{done * 100 // max(total, 1)}%", flush=True)
                        t0 = time.time()
            tmp.replace(dst)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        self._log(f"  复制完成：{dst.name}（{_human_bytes(done)}）")

    # ------------------------------------------------------------------ #
    # HGT / HGT.GZ
    # ------------------------------------------------------------------ #
    @staticmethod
    def _check_hgt_size(n_bytes: int, what: str) -> None:
        if n_bytes == HGT_BYTES:
            return
        if n_bytes == HGT_1201_BYTES:
            raise ValueError(
                f"{what} 是 3″ 瓦片（1201×1201）；本工具箱只使用 1″ 瓦片"
                f"（3601×3601）。请改用 1″ 数据（如 SRTMGL1/N37E112.hgt）。")
        raise ValueError(
            f"{what} 大小 {n_bytes} 字节不是 1″ SRTM 瓦片"
            f"（应为 {HGT_BYTES} = 3601×3601×2，或 3″ 的 {HGT_1201_BYTES}）：文件可能损坏")

    def _import_hgt(self, p: Path, progress=None) -> str:
        self._check_hgt_size(p.stat().st_size, p.name)
        tile = _tile_name_of(p.stem)
        if not tile:
            raise ValueError(f"文件名不是合法瓦片名（应为 N37E112 形式）：{p.name}")
        self.dir_hgt.mkdir(parents=True, exist_ok=True)
        dst = self.dir_hgt / f"{tile}.hgt"
        if dst.is_file() and dst.stat().st_size == HGT_BYTES:
            self._log(f"瓦片 {tile} 已存在，跳过复制")
            return tile
        self._log(f"导入 1″ 瓦片 {tile}：{p.name} → {dst}")
        self._copy_file(p, dst, p.stat().st_size, progress)
        # 若有同名 .hgt.gz 也顺带归档（可选，不影响使用）
        self._log(f"瓦片 {tile} 导入完成（{_human_bytes(dst.stat().st_size)}）")
        return tile

    def _import_hgt_gz(self, p: Path, progress=None) -> str:
        base = p.name[:-len(".hgt.gz")]
        tile = _tile_name_of(base)
        if not tile:
            raise ValueError(f"文件名不是合法瓦片名（应为 N37E112.hgt.gz）：{p.name}")
        self.dir_hgt.mkdir(parents=True, exist_ok=True)
        dst = self.dir_hgt / f"{tile}.hgt"
        # 1) 先归档原始 .gz（引擎的 gz/ 目录约定）
        self.dir_gz.mkdir(parents=True, exist_ok=True)
        gz_dst = self.dir_gz / f"{tile}.hgt.gz"
        if not gz_dst.is_file():
            try:
                shutil.copy2(p, gz_dst)
            except OSError as exc:              # noqa: BLE001 - 归档失败不致命
                self._log(f"  归档 {gz_dst.name} 失败（忽略）：{exc}")
        if dst.is_file() and dst.stat().st_size == HGT_BYTES:
            self._log(f"瓦片 {tile} 已解压，跳过")
            return tile
        # 2) 流式解压到 .part，校验字节数后原子替换
        tmp = dst.with_name(dst.name + ".part")
        n = 0
        t0 = time.time()
        self._log(f"解压 {p.name} → {dst.name}")
        try:
            with gzip.open(p, "rb") as fin, open(tmp, "wb") as fout:
                while True:
                    chunk = fin.read(_COPY_CHUNK)
                    if not chunk:
                        break
                    fout.write(chunk)
                    n += len(chunk)
                    if n > HGT_BYTES:           # 太大就不必再读了
                        break
                    self._emit(progress, n, HGT_BYTES, f"解压 {tile} …")
                    if time.time() - t0 > 1.0 and progress is None:
                        print(f"[progress {min(n, HGT_BYTES)}/{HGT_BYTES}] "
                              f"解压 {tile} {min(n, HGT_BYTES) * 100 // HGT_BYTES}%",
                              flush=True)
                        t0 = time.time()
        except (OSError, EOFError) as exc:
            tmp.unlink(missing_ok=True)
            raise ValueError(f"gzip 解压失败（文件损坏？）：{exc}") from exc
        if n != HGT_BYTES:
            tmp.unlink(missing_ok=True)
            self._check_hgt_size(n, p.name)
            raise ValueError(f"解压后大小 {n} 字节不符（应为 {HGT_BYTES}）")
        tmp.replace(dst)
        self._emit(progress, HGT_BYTES, HGT_BYTES, f"{tile} 完成")
        self._log(f"瓦片 {tile} 解压完成（{_human_bytes(n)}）")
        return tile

    # ------------------------------------------------------------------ #
    # ZIP
    # ------------------------------------------------------------------ #
    def _import_zip(self, p: Path, copy: bool, name, progress, result: dict) -> None:
        """安全解压 zip 里的 .hgt/.hgt.gz/.tif（防路径穿越），逐个导入。"""
        try:
            zf = zipfile.ZipFile(p)
        except (zipfile.BadZipFile, OSError) as exc:
            raise ValueError(f"不是有效的 zip：{exc}") from exc
        tmp_root = Path(tempfile.mkdtemp(prefix="rocketkit_zip_", dir=str(self.dir_custom)))
        try:
            with zf:
                members = []
                skipped = []
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    if not _zip_member_safe(info.filename):
                        skipped.append(info.filename)
                        continue
                    if _zip_member_is_link(info):
                        skipped.append(info.filename)
                        continue
                    low = info.filename.lower()
                    if not low.endswith(_ZIP_ALLOWED):
                        continue
                    members.append(info)
                if skipped:
                    self._log(f"zip 中跳过 {len(skipped)} 个不安全成员"
                              f"（如 {skipped[:3]}）")
                if not members:
                    raise ValueError("zip 里没有可导入的 .hgt/.hgt.gz/.tif")
                self._log(f"zip {p.name}：{len(members)} 个高程文件待导入")
                total = len(members)
                extracted: list[Path] = []
                for i, info in enumerate(members, 1):
                    self._emit(progress, i - 1, total,
                               f"解压 {Path(info.filename).name}（{i}/{total}）")
                    target = tmp_root / Path(info.filename.replace("\\", "/")).name
                    with zf.open(info) as fin, open(target, "wb") as fout:
                        shutil.copyfileobj(fin, fout, _COPY_CHUNK)
                    extracted.append(target)
                for i, f in enumerate(extracted, 1):
                    self._emit(progress, i - 1, total, f"导入 {f.name}（{i}/{total}）")
                    try:
                        # zip 里的 GeoTIFF 必须复制出来（临时解压目录随后会删除），
                        # hgt/hgt.gz 本来就会复制进 dem_dir，不受影响
                        force_copy = f.name.lower().endswith(GEO_EXTENSIONS)
                        self._import_one(f, bool(copy or force_copy), None, None, result)
                    except Exception as exc:    # noqa: BLE001 - 单个成员失败继续
                        result["failed"].append({"path": f"{p}!{f.name}",
                                                 "error": str(exc)})
                        self._log(f"  成员导入失败 {f.name}：{exc}")
                self._emit(progress, total, total, "zip 导入完成")
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)

    # ------------------------------------------------------------------ #
    # 查询 / 校验
    # ------------------------------------------------------------------ #
    def _validate_record(self, rec: dict) -> tuple:
        """记录的文件是否仍可用 → (valid, error, inspect_info 或 None)。"""
        f = Path(str(rec.get("file") or ""))
        try:
            st = f.stat()
        except OSError:
            return False, f"文件不存在：{f}", None
        if st.st_size <= 0:
            return False, "文件为空", None
        key = (str(f).lower(), int(st.st_size), int(st.st_mtime))
        cached = self._valid_cache.get(str(rec.get("id")))
        if cached is not None and cached[0] == key:
            return cached[1], cached[2], None
        try:
            _, info = self._validate_geotiff(f)
            self._valid_cache[str(rec.get("id"))] = (key, True, "")
            return True, "", info
        except Exception as exc:                # noqa: BLE001 - 坏文件不阻塞其它源
            err = f"{type(exc).__name__}: {exc}"
            self._valid_cache[str(rec.get("id"))] = (key, False, err)
            return False, err, None

    def list_sources(self, valid_only: bool = True) -> list[dict]:
        """已登记的 GeoTIFF 自定义源列表。

        Parameters
        ----------
        valid_only : bool
            只返回文件仍存在且能解析的记录（默认 True）；False 时附带
            ``valid``/``error`` 字段便于 GUI 显示"失效"。

        Returns
        -------
        list of dict
            每条记录在 index 内容基础上补 ``valid``（bool）与 ``error``（str）。
        """
        idx = self.load_index()
        out = []
        for rec in idx.get("sources", []):
            ok, err, _ = self._validate_record(rec)
            item = dict(rec)
            item["valid"] = bool(ok)
            item["error"] = err
            if ok or not valid_only:
                out.append(item)
            if not ok and valid_only:
                self._log(f"自定义源 {rec.get('id')} 不可用（{err}），已跳过")
        return out

    def remove_source(self, source_id: str) -> bool:
        """删除一条自定义源登记；若为复制模式则同时删除副本（不删除用户原文件）。"""
        sid = str(source_id)
        idx = self.load_index()
        keep, removed = [], []
        for rec in idx.get("sources", []):
            if str(rec.get("id")) == sid:
                removed.append(rec)
            else:
                keep.append(rec)
        if not removed:
            self._log(f"未找到自定义源 id={sid}")
            return False
        idx["sources"] = keep
        self.save_index(idx)
        self._valid_cache.pop(sid, None)
        for rec in removed:
            if not rec.get("copied"):
                continue
            f = Path(str(rec.get("file") or ""))
            try:
                f.relative_to(self.dir_data)
            except (ValueError, OSError):
                continue                        # 不在本 dem_dir 下，绝不删
            # 没有别的记录引用同一文件才删
            if any(str(it.get("file")) == str(rec.get("file")) for it in keep):
                continue
            for path in [f] + [Path(s) for s in (rec.get("sidecars") or [])]:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError as exc:          # noqa: BLE001
                    self._log(f"  删除副本失败（忽略）{path}：{exc}")
        self._log(f"已删除自定义源 id={sid}")
        return True

    def coverage(self, points) -> dict:
        """计算自定义源对关键点的覆盖率。

        Parameters
        ----------
        points : sequence
            ``[(name, lat, lon), ...]``（也接受``(lat, lon)`` 二元组）。

        Returns
        -------
        dict
            ``{"points": [...], "sources": [...], "total": {"covered","total","ratio"},
            "n_points": N}``。``covered`` 按各源 bbox 统计；点数不多时额外给出
            ``has_data``（真正采样到有效高程的点数，能识别 bbox 内 nodata 区域）。
        """
        pts = []
        for item in (points or []):
            try:
                if len(item) >= 3:
                    nm, la, lo = item[0], item[1], item[2]
                elif len(item) == 2:
                    nm, (la, lo) = "", item[0], item[1]
                else:
                    continue
                la = float(la)
                lo = float(lo)
            except (TypeError, ValueError, IndexError):
                continue
            if math.isfinite(la) and math.isfinite(lo):
                pts.append({"name": str(nm or f"{la:.4f},{lo:.4f}"),
                            "lat": la, "lon": lo})
        if not pts:
            return {"points": [], "sources": [],
                    "total": {"covered": 0, "total": 0, "ratio": 0.0}, "n_points": 0}

        sources = self.list_sources(valid_only=True)
        src_stats = []
        point_cover = [set() for _ in pts]
        point_val = [None] * len(pts)
        sample_ok = (len(pts) * max(len(sources), 1)) <= _COVERAGE_SAMPLE_LIMIT
        opened = []
        for rec in sources:
            try:
                opened.append((rec, self._geotiff_class()(rec["file"])))
            except Exception as exc:            # noqa: BLE001
                self._log(f"自定义源 {rec.get('id')} 打不开（{exc}），跳过覆盖统计")
        try:
            for rec, gate in opened:
                covered = 0
                n_data = 0
                vals = []
                for i, pt in enumerate(pts):
                    if not gate.covers(pt["lat"], pt["lon"]):
                        continue
                    covered += 1
                    point_cover[i].add(str(rec.get("id")))
                    if sample_ok:
                        try:
                            v = gate.sample(pt["lat"], pt["lon"], method="nearest")
                        except Exception:       # noqa: BLE001
                            v = float("nan")
                        if math.isfinite(v):
                            n_data += 1
                            vals.append(v)
                            if point_val[i] is None:
                                point_val[i] = float(v)
                src_stats.append({
                    "id": str(rec.get("id")),
                    "name": str(rec.get("name") or ""),
                    "file": str(rec.get("file") or ""),
                    "bbox": [float(v) for v in (rec.get("bbox") or [0, 0, 0, 0])],
                    "covered": int(covered),
                    "has_data": int(n_data) if sample_ok else None,
                    "total": len(pts),
                    "ratio": round(covered / len(pts), 4),
                    "value_range": ([round(min(vals), 1), round(max(vals), 1)]
                                    if vals else None),
                })
        finally:
            for _, g in opened:
                try:
                    g.close()
                except Exception:               # noqa: BLE001
                    pass
        n_cov = sum(1 for s in point_cover if s)
        return {
            "points": [{"name": pt["name"], "lat": pt["lat"], "lon": pt["lon"],
                        "covered": bool(point_cover[i]),
                        "sources": sorted(point_cover[i]),
                        "value_m": (None if point_val[i] is None
                                    else round(point_val[i], 1))}
                       for i, pt in enumerate(pts)],
            "sources": src_stats,
            "total": {"covered": int(n_cov), "total": len(pts),
                      "ratio": round(n_cov / len(pts), 4)},
            "n_points": len(pts),
            "sampled": bool(sample_ok),
        }

    # ------------------------------------------------------------------ #
    # 其它
    # ------------------------------------------------------------------ #
    def registry_path(self) -> Path:
        """``custom/index.json`` 路径（给 GUI 显示用）。"""
        return self.index_path

    def tiles(self) -> list[str]:
        """本地 1″ 瓦片名（含导入的 .hgt）。"""
        if not self.dir_hgt.is_dir():
            return []
        out = []
        for p in sorted(self.dir_hgt.glob("*.hgt")):
            try:
                if p.stat().st_size == HGT_BYTES:
                    out.append(p.stem)
            except OSError:
                continue
        return out

    def log_lines(self) -> list[str]:
        """本次会话的中文日志行（GUI 日志面板可直接显示）。"""
        return list(self._log_lines)
