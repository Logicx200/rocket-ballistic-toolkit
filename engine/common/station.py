# -*- coding: utf-8 -*-
"""空间站轨道数据解析：用户导入 / 内置快照 / 在线拉取（带离线回退）。

数据源优先级
------------
1. 用户在 ``config.station`` 里显式给的 OEM 文本/文件（最高优先）；
2. 用户显式给的 TLE 两行；
3. 内置官方 CMSE/BACC OEM 精密星历快照（``app/data/station/*.dat``），
   仅当所需时刻落在其覆盖区间内时使用；
4. 内置 CelesTrak TLE 快照（历元 ±45 天内可用，超出会警告）；
5. 在线拉取（仅在 ``allow_network=True`` 时尝试）：
   CMSE 公告页 ZIP → 解压 OEM；CelesTrak TLE；
6. 全部失败 → 抛 :class:`StationDataError`，由上层给出中文提示。

坐标系
------
* OEM 为 EME2000，读取时按需转 TEME（与工具箱上升段 ECI 同口径）；
* TLE 由 SGP4 直接给出 TEME；
* 所有对外状态均为 km / km/s。

仅供公开信息分析与教学使用，非实际飞行程序。
"""
from __future__ import annotations

import io
import json
import os
import re
import ssl
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import orbit as O


class StationDataError(RuntimeError):
    """空间站轨道数据不可用（缺文件/超时/格式错误）。"""


# --------------------------------------------------------------------------- #
# 数据目录与内置库
# --------------------------------------------------------------------------- #
def data_dir() -> Path:
    """内置空间站数据目录：``RK_APP_DATA/station`` 或引擎内回退。"""
    env = os.environ.get("RK_STATION_DIR")
    if env:
        return Path(env)
    app_data = os.environ.get("RK_APP_DATA")
    if app_data:
        p = Path(app_data) / "station"
        if p.is_dir():
            return p
    here = Path(__file__).resolve().parents[2]      # 项目根（engine/common/station.py 的上两级）
    for cand in (here / "data" / "station",
                 here / "app" / "data" / "station",
                 here.parent / "app" / "data" / "station"):
        if cand.is_dir():
            return cand
    return here / "data" / "station"


def cache_dir() -> Path:
    env = os.environ.get("RK_STATION_CACHE")
    p = Path(env) if env else Path(os.environ.get("RK_DEM") or tempfile.gettempdir()) / "station"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        p = Path(tempfile.gettempdir()) / "rocketkit_station"
        p.mkdir(parents=True, exist_ok=True)
    return p


def load_db() -> Dict[str, Any]:
    p = data_dir() / "station.json"
    try:
        with open(p, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {"schema": "rocketkit.stations.v1", "stations": []}


def list_stations() -> List[Dict[str, Any]]:
    return list(load_db().get("stations") or [])


def default_station() -> Dict[str, Any]:
    st = list_stations()
    for s in st:
        if s.get("default"):
            return s
    return st[0] if st else {}


def match_station(label: str = "", norad_id: Optional[int] = None) -> Dict[str, Any]:
    st = list_stations()
    if norad_id is not None:
        for s in st:
            if int(s.get("norad_id") or 0) == int(norad_id):
                return s
    text = str(label or "").strip().lower()
    if text:
        for s in st:
            names = [s.get("name", ""), s.get("id", "")] + list(s.get("aliases") or [])
            if any(text == str(n).lower() for n in names):
                return s
        for s in st:
            names = [s.get("name", ""), s.get("id", "")] + list(s.get("aliases") or [])
            if any(text and (text in str(n).lower() or str(n).lower() in text) for n in names):
                return s
    return default_station()


# --------------------------------------------------------------------------- #
# 在线工具
# --------------------------------------------------------------------------- #
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RocketKit/3.4"


def _http_get(url: str, timeout: float = 8.0, binary: bool = False):
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Accept": "*/*"})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        data = resp.read(8 * 1024 * 1024 + 1)
    return data if binary else data.decode("utf-8", errors="replace")


def _fetch_cmse_oem() -> Optional[Tuple[str, bytes, str]]:
    """尝试从 CMSE 公开公告页抓取最新 OEM ZIP → (name, oem_text, url)。"""
    pages = [
        "https://www.cmse.gov.cn/gfgg/zgkjzgdcs/",
        "https://www.cmse.gov.cn/gfgg/",
    ]
    errors = []
    for page in pages:
        try:
            html = _http_get(page, timeout=6.0)
        except Exception as exc:  # noqa: BLE001
            errors.append("%s: %s" % (page, exc))
            continue
        urls = re.findall(r'href="([^"]+\.zip)"', html, flags=re.I)
        urls = [u if u.startswith("http") else
                urllib.parse.urljoin(page, u) for u in urls]
        for url in list(dict.fromkeys(urls))[:6]:
            try:
                blob = _http_get(url, timeout=20.0, binary=True)
                with zipfile.ZipFile(io.BytesIO(blob)) as z:
                    names = [n for n in z.namelist()
                             if n.lower().endswith((".dat", ".oem"))]
                    if not names:
                        continue
                    raw = z.read(names[0])
                text = raw.decode("utf-8", errors="replace")
                if "CCSDS_OEM" not in text[:400].upper() and "META_START" not in text:
                    continue
                return (Path(names[0]).name, text, url)
            except Exception as exc:  # noqa: BLE001
                errors.append("%s: %s" % (url, exc))
    if errors:
        raise StationDataError("CMSE 在线拉取失败：%s" % errors[-1])
    raise StationDataError("CMSE 公告页未找到可用的 OEM ZIP")


def _fetch_celestrak_tle(norad_id: int) -> Tuple[str, str, str]:
    urls = [
        "https://celestrak.org/NORAD/elements/gp.php?CATNR=%d&FORMAT=TLE" % norad_id,
        "https://celestrak.com/NORAD/elements/gp.php?CATNR=%d&FORMAT=TLE" % norad_id,
        "https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=tle",
    ]
    last = ""
    for url in urls:
        try:
            text = _http_get(url, timeout=7.0)
        except Exception as exc:  # noqa: BLE001
            last = "%s: %s" % (url, exc)
            continue
        recs = []
        try:
            recs = O.parse_tle(text)
        except Exception:  # noqa: BLE001
            continue
        for rec in recs:
            if int(rec["norad_id"]) == int(norad_id):
                return rec["line1"], rec["line2"], url
    raise StationDataError("CelesTrak TLE 在线拉取失败：%s" % (last or "无匹配"))


# --------------------------------------------------------------------------- #
# 空间站数据源
# --------------------------------------------------------------------------- #
class StationSource:
    """统一的空间站轨道数据源（OEM 或 TLE）。"""

    def __init__(self, kind: str, name: str, norad_id: Optional[int],
                 source: str, detail: Dict[str, Any],
                 oem: Optional[O.OemEphemeris] = None,
                 tle: Optional[O.TleSatellite] = None,
                 warning: str = ""):
        self.kind = str(kind)
        self.name = name
        self.norad_id = norad_id
        self.source = source
        self.detail = dict(detail or {})
        self.oem = oem
        self.tle = tle
        self.warning = warning

    # -- 基本属性 ---------------------------------------------------------- #
    @property
    def is_high_precision(self) -> bool:
        return self.kind == "oem"

    def coverage(self) -> Optional[Tuple[datetime, datetime]]:
        if self.oem is not None:
            return self.oem.coverage
        return None

    def covers(self, dt: datetime) -> bool:
        if self.oem is not None:
            return self.oem.covers(dt)
        return True

    def state(self, dt: datetime, frame: str = "teme") -> Tuple[Any, Any]:
        """UTC → (r, v) km / km/s；OEM 自动转帧，TLE 直接给 TEME。"""
        dt = O.ensure_utc(dt)
        if self.oem is not None:
            return self.oem.state(dt, frame=frame)
        if self.tle is None:
            raise StationDataError("空间站数据源为空")
        return self.tle.state(dt)

    def elements(self, dt: datetime) -> Dict[str, float]:
        r, v = self.state(dt)
        return O.elements_from_state(r, v)

    def info(self) -> Dict[str, Any]:
        d = dict(kind=self.kind, name=self.name, norad_id=self.norad_id,
                 source=self.source, warning=self.warning,
                 label=self.detail.get("label") or self.source)
        d.update(self.detail)
        if self.oem is not None:
            oem_info = self.oem.info()
            d["oem_file"] = oem_info.pop("source", "")
            d.update(oem_info)
            d["coverage_utc"] = [O.utc_text(self.oem.t0), O.utc_text(self.oem.t1)]
        if self.tle is not None:
            d["tle"] = [self.tle.line1, self.tle.line2]
            d["epoch_utc"] = O.utc_text(self.tle.epoch)
        # 统一用人类可读的来源描述（OEM 内部 info() 的 source 是文件路径）
        d["source"] = self.detail.get("label") or self.source
        return d


def _source_from_oem_text(text: str, name: str, norad_id: Optional[int],
                          source: str, detail: Dict[str, Any],
                          warning: str = "") -> StationSource:
    oem = O.OemEphemeris(text=text, source=detail.get("file") or source)
    return StationSource("oem", name, norad_id, source, detail, oem=oem,
                         warning=warning)


def _source_from_tle(lines: Sequence[str], name: str, norad_id: Optional[int],
                     source: str, detail: Dict[str, Any],
                     warning: str = "") -> StationSource:
    recs = O.parse_tle("\n".join(lines))
    rec = recs[0]
    tle = O.TleSatellite(rec["line1"], rec["line2"], name=name)
    return StationSource("tle", name, norad_id or rec["norad_id"], source, detail,
                         tle=tle, warning=warning)


def resolve(cfg: Optional[Dict[str, Any]] = None,
            t_hint: Optional[datetime] = None,
            allow_network: bool = False,
            logger: Optional[Any] = None,
            force: bool = False) -> StationSource:
    """按优先级解析空间站轨道数据。

    Parameters
    ----------
    cfg : dict, optional
        完整 config（读取 ``config["station"]``；缺省用内置默认）。
    t_hint : datetime, optional
        需要覆盖的时刻（通常是 T0）；用于选择内置 OEM/TLE。
    allow_network : bool
        是否允许在线拉取。
    logger : callable, optional
        中文日志回调。
    force : bool
        忽略 ``station.enabled=False``（供 GUI「校验轨道」使用）。
    """
    def log(msg: str) -> None:
        if logger:
            try:
                logger(msg)
            except Exception:  # noqa: BLE001
                pass

    cfg = cfg or {}
    st_cfg = dict(cfg.get("station") or {})
    if st_cfg.get("enabled") is False and not force:
        raise StationDataError("空间站交会模块未启用（station.enabled=False）")

    db = load_db()
    meta = dict(st_cfg.get("_db") or {})
    if not meta:
        meta = match_station(st_cfg.get("name") or st_cfg.get("id") or "",
                             st_cfg.get("norad_id"))
    name = str(st_cfg.get("name") or meta.get("name") or "天宫空间站")
    norad = st_cfg.get("norad_id") or meta.get("norad_id")
    norad = int(norad) if norad else None
    if t_hint is None:
        t0 = ((cfg.get("mission") or {}).get("t0_utc")) if cfg else None
        if t0:
            try:
                t_hint = O.parse_iso(t0)
            except Exception:  # noqa: BLE001
                t_hint = None
    t_hint = O.ensure_utc(t_hint) if t_hint else datetime.now(timezone.utc)
    source_pref = str(st_cfg.get("source") or "auto").lower()

    # 1) 用户 OEM
    oem_text = st_cfg.get("oem_text") or ""
    oem_path = st_cfg.get("oem_path") or ""
    if source_pref in ("auto", "oem", "file") and (oem_text or oem_path):
        try:
            if oem_text:
                text = str(oem_text)
                label = "用户粘贴的 OEM 文本"
            else:
                with open(oem_path, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                label = "用户导入的 OEM 文件：%s" % oem_path
            src = _source_from_oem_text(text, name, norad, label,
                                        dict(file=os.path.basename(str(oem_path or "")),
                                             user=True))
            log("[空间站] 使用%s（%s ~ %s）" % (label, src.detail.get("start_utc", "?"),
                                             src.detail.get("stop_utc", "?")))
            return src
        except Exception as exc:  # noqa: BLE001
            log("[空间站] 用户 OEM 不可用：%s" % exc)
            if source_pref == "oem":
                raise StationDataError("用户 OEM 文件不可用：%s" % exc)

    # 2) 用户 TLE
    tle_cfg = st_cfg.get("tle")
    if source_pref in ("auto", "tle", "manual") and tle_cfg:
        try:
            lines = tle_cfg if isinstance(tle_cfg, (list, tuple)) else str(tle_cfg).splitlines()
            src = _source_from_tle(lines, name, norad, "用户粘贴的 TLE",
                                   dict(user=True))
            log("[空间站] 使用用户 TLE（历元 %s）" % O.utc_text(src.tle.epoch))
            return src
        except Exception as exc:  # noqa: BLE001
            log("[空间站] 用户 TLE 不可用：%s" % exc)
            if source_pref == "tle":
                raise StationDataError("用户 TLE 无法解析：%s" % exc)

    # 3) 内置 OEM（优先于内置 TLE）
    oem_meta = dict(meta.get("oem") or {})
    if source_pref in ("auto", "oem", "bundled") and oem_meta.get("file"):
        p = data_dir() / oem_meta["file"]
        if p.is_file():
            try:
                src = _source_from_oem_text(p.read_text(encoding="utf-8", errors="replace"),
                                            name, norad,
                                            "内置官方 CMSE 精密星历快照",
                                            dict(file=str(p),
                                                 sha256=oem_meta.get("sha256", ""),
                                                 start_utc=oem_meta.get("start_utc", ""),
                                                 stop_utc=oem_meta.get("stop_utc", ""),
                                                 sources=oem_meta.get("sources") or []))
                if src.covers(t_hint):
                    log("[空间站] 使用内置 OEM（覆盖到 %s）" % O.utc_text(src.oem.t1))
                    return src
                log("[空间站] 内置 OEM 不覆盖 %s（%s ~ %s），尝试其他来源"
                    % (O.utc_text(t_hint), O.utc_text(src.oem.t0), O.utc_text(src.oem.t1)))
                if source_pref == "oem":
                    raise StationDataError(
                        "内置 OEM 覆盖 %s ~ %s，不含任务时刻 %s；请导入新的 OEM/TLE"
                        % (O.utc_text(src.oem.t0), O.utc_text(src.oem.t1), O.utc_text(t_hint)))
            except StationDataError:
                raise
            except Exception as exc:  # noqa: BLE001
                log("[空间站] 内置 OEM 读取失败：%s" % exc)

    # 4) 内置 TLE
    tle_meta = dict(meta.get("tle") or {})
    if source_pref in ("auto", "tle", "bundled") and tle_meta.get("lines"):
        try:
            src = _source_from_tle(tle_meta["lines"], name, norad,
                                   "内置 CelesTrak TLE 快照",
                                   dict(sources=tle_meta.get("sources") or []))
            age_days = abs((t_hint - src.tle.epoch).total_seconds()) / 86400.0
            warn = ""
            if age_days > 20.0:
                warn = ("TLE 历元距任务时刻 %.1f 天，沿迹相位误差可达数度，"
                        "交会窗口/对接时刻仅供量级参考" % age_days)
            src.warning = warn
            if warn:
                log("[空间站] 警告：" + warn)
            if age_days <= 45.0:
                log("[空间站] 使用内置 TLE（历元 %s）" % O.utc_text(src.tle.epoch))
                return src
            log("[空间站] 内置 TLE 过旧（%.0f 天），继续尝试在线或要求用户导入" % age_days)
        except Exception as exc:  # noqa: BLE001
            log("[空间站] 内置 TLE 不可用：%s" % exc)

    # 5) 在线
    if allow_network:
        log("[空间站] 尝试在线拉取空间站轨道数据（超时 8 s）…")
        # 5a) CMSE OEM
        if source_pref in ("auto", "oem", "online"):
            try:
                fname, text, url = _fetch_cmse_oem()
                src = _source_from_oem_text(text, name, norad, "在线拉取 CMSE OEM",
                                            dict(file=fname, url=url))
                stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                try:
                    (cache_dir() / ("station_oem_%s.dat" % stamp)).write_text(
                        text, encoding="utf-8")
                except Exception:  # noqa: BLE001
                    pass
                log("[空间站] 在线 OEM 拉取成功（%s）" % url)
                return src
            except Exception as exc:  # noqa: BLE001
                log("[空间站] 在线 OEM 失败：%s" % exc)
        # 5b) CelesTrak TLE
        if norad:
            try:
                l1, l2, url = _fetch_celestrak_tle(norad)
                src = _source_from_tle([l1, l2], name, norad, "在线拉取 CelesTrak TLE",
                                       dict(url=url))
                try:
                    (cache_dir() / ("station_tle_%d.txt" % norad)).write_text(
                        l1 + "\n" + l2 + "\n", encoding="utf-8")
                except Exception:  # noqa: BLE001
                    pass
                log("[空间站] 在线 TLE 拉取成功（%s）" % url)
                return src
            except Exception as exc:  # noqa: BLE001
                log("[空间站] 在线 TLE 失败：%s" % exc)

    raise StationDataError(
        "无法获得空间站轨道数据：内置 OEM 不覆盖任务时刻，且没有可用的用户 TLE/OEM。"
        "请到「空间站任务（CZ-2F）」页粘贴 TLE 或导入 OEM 文件；"
        "联网环境可勾选「允许在线拉取」。")


def rendezvous_defaults(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """CZ-2F 交会对接默认参数（config.station.rendezvous 优先，其次内置库）。"""
    db = load_db()
    d = dict((db.get("cz2f_defaults") or {}).get("rendezvous") or {})
    d.update(((cfg or {}).get("station") or {}).get("rendezvous") or {})
    return d


__all__ = ["StationDataError", "StationSource", "resolve", "list_stations",
           "match_station", "default_station", "load_db", "data_dir",
           "cache_dir", "rendezvous_defaults"]
