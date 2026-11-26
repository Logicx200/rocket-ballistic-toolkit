# -*- coding: utf-8 -*-
"""航警（NOTAM）原文解析 / 经纬度 & 多边形解析。

支持的实际输入形态（用户从航警网站、聊天记录、任务卡里直接粘贴）：

1. ICAO NOTAM 原文（英文）::

    A6107/26 NOTAMN
    Q)ZLHW/QRDCA/IV/BO/W/000/999/3800N10745E012
    A)ZLHW B)2611271253 C)2611271315
    E) A TEMPORARY DANGER AREA ESTABLISHED BOUNDED BY:
    N380800E1075600-N375700E1080000-N375100E1073300-N380200E1072900,
    BACK TO START. VERTICAL LIMITS:SFC-UNL.

2. 中文/任务卡式（字段名 + 值，每行一条）::

    航警号：A5897/26
    生效：B) 2611182243  失效：C) 2611182305
    Q)ZXXX/QRDCA/IV/BO/W/000/999/3322N11036E013
    多边形：33.566667,110.516667; 33.533333,110.733333; ...
    残骸类型：一级

3. 只有 Q 行圆 + 起止时间（无多边形）→ 用 Q 行圆心 + 半径合成八边形（标注为"由 Q 行合成"）。

4. 多段航警合并粘贴（按 ``A1234/26`` 样式自动切分）。

坐标解析复用引擎 ``common.notam`` 的 ``parse_dms / parse_vertex``（支持 DDMMSS、
DMS 符号、十进制度、度分等写法），本模块只做"高层切分与字段抽取"。

对外接口
--------
parse_notams(text) -> (list[dict], list[str])
parse_one_notam(text) -> (dict, list[str])
parse_latlon(text) -> dict
parse_polygon(text) -> (list[[lat, lon]], list[str])
extract_qline(q) -> dict
infer_stage_hint(text) -> str
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# 依赖引擎的几何/坐标解析（懒加载，缺引擎时降级）
# --------------------------------------------------------------------------- #
_CN = None


def _cn():
    global _CN
    if _CN is None:
        from common import notam as _n  # noqa: WPS433  (engine 在 sys.path 上)
        _CN = _n
    return _CN


_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}

_RE_NOTAM_ID = re.compile(r"\b([A-Z])\s?(\d{4})\s?/\s?(\d{2})\b")
_RE_Q = re.compile(r"Q\)\s*([^\r\n]+)")
_RE_A = re.compile(r"(?<![A-Z])A\)\s*([A-Z]{4}(?:\s+[A-Z]{4})*)")
_RE_B = re.compile(r"(?<![A-Z])B\)\s*([0-9]{10}|[0-9]{8})")
_RE_C = re.compile(r"(?<![A-Z])C\)\s*([0-9]{10}|[0-9]{8}|PERM)")
_RE_E = re.compile(r"(?<![A-Z])E\)\s*([\s\S]+?)(?=(?:\n\s*[FGP]\))|$)")
_RE_BOUNDED = re.compile(r"BOUNDED BY\s*:?\s*([\s\S]+?)(?:,\s*BACK TO START|\.\s*(?:VERTICAL|SFC|F\))|$)",
                         re.IGNORECASE)
_RE_COORD_PAIR = re.compile(
    r"(\d{1,8}(?:\.\d+)?\s*[NS])\s*[,，/]?\s*(\d{1,9}(?:\.\d+)?\s*[EW])", re.IGNORECASE)
_RE_COORD_PAIR_PREFIX = re.compile(
    r"([NS])\s*(\d{1,8}(?:\.\d+)?)\s*[,，/]?\s*([EW])\s*(\d{1,9}(?:\.\d+)?)", re.IGNORECASE)
_RE_DMS_SYMBOL = re.compile(
    r"(\d{1,3}(?:\.\d+)?)\s*°\s*(?:(\d{1,2}(?:\.\d+)?)\s*[′'])?\s*"
    r"(?:(\d{1,2}(?:\.\d+)?)\s*(?:″|\"|''))?\s*([NSns])", re.IGNORECASE)
_RE_DMS_SYMBOL_E = re.compile(
    r"(\d{1,3}(?:\.\d+)?)\s*°\s*(?:(\d{1,2}(?:\.\d+)?)\s*[′'])?\s*"
    r"(?:(\d{1,2}(?:\.\d+)?)\s*(?:″|\"|''))?\s*([EWew])", re.IGNORECASE)
_RE_DEC_PAIR = re.compile(
    r"(-?\d{1,3}(?:\.\d+)+)\s*[,，/ ]\s*(-?\d{1,3}(?:\.\d+)+)")

#: E) 文本 → 残骸对象提示
_STAGE_KEYWORDS = [
    (("一级", "一子级", "第一级", "芯一级", "stage 1", "first stage", "core stage"), "一级"),
    (("二级", "二子级", "第二级", "芯二级", "stage 2", "second stage"), "二级"),
    (("整流罩", "罩", "fairing", "nose cone"), "整流罩"),
    (("助推", " booster"), "助推器"),
    (("三级", "三子级", "third stage"), "三级"),
    (("上面级", "upper stage"), "上面级"),
]

_TZ_CN = {"utc": "UTC", "z": "UTC", "格林威治": "UTC",
          "北京": "BJT", "bjt": "BJT", "cst": "BJT", "本地": "BJT"}


# --------------------------------------------------------------------------- #
# 时间解析
# --------------------------------------------------------------------------- #
def parse_notam_dt(s: str, ref_year: Optional[int] = None) -> Optional[str]:
    """NOTAM 时间 ``YYMMDDHHMM``（或 ``YYMMDDHHMMSS``）→ ISO UTC 字符串 ``YYYY-MM-DDTHH:MM:SSZ``。"""
    s = (s or "").strip().upper()
    if not s:
        return None
    if s == "PERM":
        return None
    if not s.isdigit() or len(s) < 8:
        return None
    yy, mm, dd, hh = int(s[0:2]), int(s[2:4]), int(s[4:6]), int(s[6:8])
    mi = int(s[8:10]) if len(s) >= 10 else 0
    ss = int(s[10:12]) if len(s) >= 12 else 0
    year = 2000 + yy if yy < 70 else 1900 + yy
    if ref_year and abs(year - ref_year) > 2:      # 无年份信息时以任务年为准
        year = ref_year
    try:
        dt = datetime(year, mm, dd, hh, mi, ss, tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_t0_text(text: str, tz_hint: str = "auto", ref_year: Optional[int] = None):
    """解析用户输入的起飞时刻，返回 ``(datetime_utc_naive, used_tz, note)`` 或 ``(None, "", reason)``。

    支持的写法：
    * ``2026-11-18T22:50:00Z`` / ``2026-11-18 22:50Z`` / ``2026/11/18 22:50 UTC``
    * ``2026-11-19 06:50``（tz_hint=auto → 按北京时间；可用 tz_hint="UTC" 覆盖）
    * ``2026-11-19 06:50 +08:00`` / ``2026-11-19T06:50:00+08:00``
    * ``19日 06:50`` / ``11-19 06:50``（年取 ref_year 或当前年）
    * ``2611182250``（NOTAM 压缩格式 → 视为 UTC）
    """
    if text is None:
        return None, "", "空"
    raw = str(text).strip()
    if not raw:
        return None, "", "空"

    tz = (tz_hint or "auto").strip()
    low = raw.lower()
    explicit_tz = ""
    for k, v in _TZ_CN.items():
        if k in low:
            explicit_tz = v
            break
    if re.search(r"(?<=\d)\s*(z|utc)\s*$", low):
        explicit_tz = "UTC"

    # 纯 NOTAM 压缩时间
    m = re.fullmatch(r"\s*(\d{10}|\d{12})\s*", raw)
    if m:
        iso = parse_notam_dt(m.group(1), ref_year)
        if iso:
            dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
            return dt, "UTC", "按 NOTAM 压缩时间（UTC）解释"

    # ISO / 常见格式
    s = raw.replace("年", "-").replace("月", "-").replace("日", " ").replace("/", "-")
    s = re.sub(r"[Tt]\s*", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?", s)
    offset_min = 0
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4) or 0)
        mi = int(m.group(5) or 0)
        ss = int(m.group(6) or 0)
    else:
        # 无年份：MM-DD HH:MM 或 DD HH:MM
        m2 = re.search(r"(?:(\d{4})-)?(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
        if not m2:
            m3 = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
            if not m3:
                return None, "", "无法识别的时间格式"
            y = ref_year or datetime.now(timezone.utc).year
            mo = d = None
            hh, mi, ss = int(m3.group(1)), int(m3.group(2)), int(m3.group(3) or 0)
        else:
            y = int(m2.group(1)) if m2.group(1) else (ref_year or datetime.now(timezone.utc).year)
            mo, d = int(m2.group(2)), int(m2.group(3))
            hh, mi, ss = int(m2.group(4)), int(m2.group(5)), int(m2.group(6) or 0)

    # 显式时区偏移 +08:00（必须带冒号，避免把日期里的 "-" 误当偏移）
    off = re.search(r"([+-])(\d{1,2}):(\d{2})\s*$", raw)
    if off:
        sign = 1 if off.group(1) == "+" else -1
        offset_min = sign * (int(off.group(2)) * 60 + int(off.group(3)))

    used = explicit_tz or ("UTC" if offset_min else "")
    if used == "UTC" and offset_min == 0:
        offset_min = 0
    elif used == "BJT":
        offset_min = 8 * 60
    elif offset_min:
        used = "UTC%+d" % (offset_min // 60)
    elif tz.upper() in ("UTC", "Z", "GMT"):
        used, offset_min = "UTC", 0
    elif tz.upper() in ("BJT", "CST", "CN", "北京"):
        used, offset_min = "BJT", 8 * 60
    else:
        used, offset_min = "BJT", 8 * 60       # auto 默认按北京时间（中文用户习惯）
        if hh >= 16:                            # 深夜时刻更像 UTC 的"当日傍晚"？仍按 BJT，仅提示
            pass

    year_now = datetime.now(timezone.utc).year
    if mo is None or d is None:
        return None, "", "缺月日"
    if y < 1990:
        y = year_now
    try:
        dt = datetime(y, mo, d, hh, mi, ss, tzinfo=timezone.utc) - timedelta(minutes=offset_min)
    except ValueError:
        return None, "", "日期越界"
    note = "按 %s 解释" % ("北京时间（UTC+8）" if used == "BJT" else used)
    return dt.replace(tzinfo=None), used, note


def to_iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_utc(s: str) -> Optional[datetime]:
    """ISO 字符串 → naive UTC datetime。"""
    if not s:
        return None
    s = str(s).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# --------------------------------------------------------------------------- #
# 坐标 / 多边形
# --------------------------------------------------------------------------- #
def _norm_hemi_prefix(s: str) -> str:
    """半球字母前缀写法 → 后缀写法（``N380800E1075600`` → ``380800N 1075600E``）。

    ICAO NOTAM 的 BOUNDED BY 段常用前缀式，而引擎 :func:`common.notam.parse_dms`
    需要后缀式；这里统一归一化后再解析。
    """
    out = _RE_COORD_PAIR_PREFIX.sub(lambda m: "%s%s %s%s" % (m.group(2), m.group(1),
                                                             m.group(4), m.group(3)), s)
    # 前缀式单点（如 "N37.761467"）
    out = re.sub(r"(?<![A-Za-z])([NS])\s*(\d{1,3}(?:\.\d+)?)(?![0-9.])", r"\2\1", out,
                 flags=re.IGNORECASE)
    out = re.sub(r"(?<![A-Za-z])([EW])\s*(\d{1,3}(?:\.\d+)?)(?![0-9.])", r"\2\1", out,
                 flags=re.IGNORECASE)
    return out


def _dms_symbol_pair(text: str) -> Optional[Tuple[float, float]]:
    """解析 ``37°45′41.28″N 112°43′30.79″E`` 这类带度分秒符号的一对坐标。"""

    def _to_deg(m):
        d = float(m.group(1))
        mm = float(m.group(2)) if m.group(2) else 0.0
        ss = float(m.group(3)) if m.group(3) else 0.0
        v = d + mm / 60.0 + ss / 3600.0
        if m.group(4).upper() in ("S", "W"):
            v = -v
        return v

    lat_m = list(_RE_DMS_SYMBOL.finditer(text))
    lon_m = list(_RE_DMS_SYMBOL_E.finditer(text))
    if lat_m and lon_m:
        return _to_deg(lat_m[0]), _to_deg(lon_m[0])
    if len(lat_m) >= 2:      # 两个都是纬度？不做处理
        return None
    return None


def parse_latlon(text: str) -> Dict[str, Any]:
    """从任意文本中解析出单个坐标（及可选高程）。

    支持：``37°45′41.28″N 112°43′30.79″E``、``37.761467N 112.725219E``、
    ``37.761467, 112.725219``、``N37.761467 E112.725219``、``37.761467 112.725219 840``
    （末位认作高程 m）。返回 ``{"lat","lon","alt_m","format"}``，失败抛 ValueError。
    """
    s = str(text or "").strip()
    if not s:
        raise ValueError("空坐标")
    alt = None
    m_alt = re.search(r"(?:高程|海拔|alt|高度)\s*[:=]?\s*(-?\d+(?:\.\d+)?)\s*(?:m|米)?", s,
                      re.IGNORECASE)
    if m_alt:
        alt = float(m_alt.group(1))
        s = s.replace(m_alt.group(0), " ")
    if alt is None:
        m_alt2 = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:m|米)\s*$", s, re.IGNORECASE)
        if m_alt2:
            hv = float(m_alt2.group(1))
            if -400.0 <= hv <= 9000.0:
                alt = hv
                s = s[:m_alt2.start()]

    # 0) 带 °′″ 符号（最常见的 GE 截图贴法）
    dms = _dms_symbol_pair(s)
    if dms:
        return {"lat": float(dms[0]), "lon": float(dms[1]), "alt_m": alt, "format": "dms-symbol"}

    s_norm = _norm_hemi_prefix(s)
    pairs = _RE_COORD_PAIR.findall(s_norm)
    if len(pairs) >= 2:
        a, b = pairs[0], pairs[1]
        lat, lon = _cn().parse_vertex("%s %s" % (a, b))
        fmt = "dms" if re.search(r"\d{5,}(?:\.\d+)?\s*[NS]$", a, re.IGNORECASE) else "deg-hemi"
        return {"lat": float(lat), "lon": float(lon), "alt_m": alt, "format": fmt}

    m = re.search(r"N\s*(\d+(?:\.\d+)?)\s*[, ]\s*E\s*(\d+(?:\.\d+)?)", s, re.IGNORECASE)
    if m:
        return {"lat": float(m.group(1)), "lon": float(m.group(2)), "alt_m": alt,
                "format": "deg-hemi-prefix"}
    dec = _RE_DEC_PAIR.findall(s)
    if len(dec) >= 2:
        lat, lon = float(dec[0][0] if isinstance(dec[0], tuple) else dec[0]), \
            float(dec[0][1] if isinstance(dec[0], tuple) else dec[1])
        return {"lat": lat, "lon": lon, "alt_m": alt, "format": "decimal"}
    # 最后尝试：空格分隔的数（超限则交换；第三个数当高程）
    nums = re.findall(r"-?\d+(?:\.\d+)?", s)
    if len(nums) >= 2:
        a, b = float(nums[0]), float(nums[1])
        if abs(a) > 90 and abs(b) <= 90:
            a, b = b, a
        if abs(a) <= 90 and abs(b) <= 180:
            if alt is None and len(nums) == 3:
                try:
                    hv = float(nums[2])
                    if -400.0 <= hv <= 9000.0:
                        alt = hv
                except ValueError:
                    pass
            return {"lat": a, "lon": b, "alt_m": alt, "format": "decimal-plain"}
    raise ValueError("无法解析坐标：%s" % text)


# --------------------------------------------------------------------------- #
# 单坐标（纬度/经度分开输入）解析与格式化：十进制度 ⇄ 度分秒
# --------------------------------------------------------------------------- #
_AXIS_CN = {"lat": "纬度", "lon": "经度"}

#: ``37°45′41.28″N`` / ``N37°45′41.28″`` / ``37 45 41.28 N`` 等单个坐标
_RE_ONE_DMS = re.compile(
    r"^\s*([NSEWnsew])?\s*(\d{1,3}(?:\.\d+)?)\s*[°度]\s*"
    r"(?:(\d{1,2}(?:\.\d+)?)\s*[′'分]\s*)?"
    r"(?:(\d{1,2}(?:\.\d+)?)\s*[″\"秒]?\s*)?([NSEWnsew])?\s*$")
#: ``37 45 41.28 N`` / ``N37 45 41.28``（空格分隔的度分秒）
_RE_ONE_DMS_SPACE = re.compile(
    r"^\s*([NSEWnsew])?\s*(\d{1,3})\s+(\d{1,2}(?:\.\d+)?)\s+"
    r"(?:(\d{1,2}(?:\.\d+)?)\s*)?([NSEWnsew])?\s*$")
#: ``N37.761467`` / ``37.761467N`` / ``E112.725219``（带半球字母的十进制度）
_RE_ONE_DEC_HEMI = re.compile(
    r"^\s*([NSEWnsew])?\s*(-?\d{1,3}(?:\.\d+)?)\s*([NSEWnsew])?\s*$")


def _axis_key(axis: Any) -> str:
    a = str(axis or "lat").strip().lower()
    return "lon" if a in ("lon", "lng", "long", "经度", "e", "w") else "lat"


def _hemi_sign(pre: Optional[str], post: Optional[str]) -> float:
    h = (post or pre or "").upper()
    return -1.0 if h in ("S", "W") else 1.0


def _dms_one_to_deg(text: str) -> Optional[float]:
    """把单个「度分秒」文本转成十进制度；失败返回 None。"""
    s = str(text or "").strip()
    for pat in (_RE_ONE_DMS, _RE_ONE_DMS_SPACE):
        m = pat.match(s)
        if not m:
            continue
        pre, d, mm, ss, post = m.groups()
        v = float(d) + (float(mm) / 60.0 if mm else 0.0) + (float(ss) / 3600.0 if ss else 0.0)
        return _hemi_sign(pre, post) * v
    return None


def _dec_hemi_to_deg(text: str) -> Optional[float]:
    """``N37.76`` / ``37.76N`` 这类带半球字母的十进制度；无半球字母返回 None。"""
    m = _RE_ONE_DEC_HEMI.match(str(text or "").strip())
    if not m:
        return None
    pre, num, post = m.groups()
    if not (pre or post):
        return None
    return _hemi_sign(pre, post) * abs(float(num))


def parse_one_coord(text: str, axis: str = "lat") -> Optional[float]:
    """解析**单个**纬度或经度输入（十进制 或 度分秒），失败抛 ``ValueError``。

    支持写法示例（纬度）::

        37.761467        N37.761467        37.761467N
        37°45′41.28″N    37 45 41.28 N     N37 45 41.28
        37°45.688′N      37d45m41.28sN      -37.761467

    经度同理（E/W）。空字符串返回 ``None``。
    """
    axis = _axis_key(axis)
    label = _AXIS_CN[axis]
    s = str(text or "").strip()
    if not s:
        return None
    norm = (s.replace("，", ",").replace("。", ".").replace("　", " ")
             .replace("º", "°").replace("’", "′").replace("”", "″"))
    # 1) 纯十进制
    v: Optional[float] = None
    try:
        v = float(norm)
    except ValueError:
        v = None
    # 2) 带半球字母的十进制度（N37.76 / 37.76N）
    if v is None:
        v = _dec_hemi_to_deg(norm)
    # 3) 度分秒（先试本模块宽松正则，再退回引擎 parse_dms）
    if v is None:
        v = _dms_one_to_deg(norm)
    if v is None:
        try:
            v = float(_cn().parse_dms(norm))
        except Exception:  # noqa: BLE001
            v = None
    if v is None:
        raise ValueError("「%s」无法解析：请输入十进制度（如 37.761467）"
                         "或度分秒（如 37°45′41.28″N）" % s)
    lim = 90.0 if axis == "lat" else 180.0
    if abs(v) > lim + 1e-9:
        raise ValueError("%s「%s」超出范围（绝对值应 ≤ %g）：解析值 %.6f"
                         % (label, s, lim, v))
    return float(v)


def format_dms(value: float, axis: str = "lat") -> str:
    """十进制度 → 度分秒文本，如 ``37°45′41.28″N``。"""
    axis = _axis_key(axis)
    v = float(value)
    hemi = ("N" if v >= 0 else "S") if axis == "lat" else ("E" if v >= 0 else "W")
    av = abs(v)
    d = int(av)
    m = int((av - d) * 60.0)
    sec = ((av - d) * 60.0 - m) * 60.0
    if sec >= 59.995:
        sec = 0.0
        m += 1
    if m >= 60:
        m = 0
        d += 1
    return "%d°%02d′%05.2f″%s" % (d, m, sec, hemi)


def format_one_coord(value: Any, axis: str = "lat", fmt: str = "dec") -> str:
    """十进制度 → 显示文本（``fmt``：``dec`` 十进制 / ``dms`` 度分秒）。"""
    if value is None or value == "":
        return ""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    f = str(fmt or "dec").strip().lower()
    if f in ("dms", "度分秒", "dm", "deg-min-sec"):
        return format_dms(v, axis)
    if abs(v - round(v)) < 1e-9:
        return "%d" % int(round(v))
    return "%.6f" % v


def parse_polygon(text: str) -> Tuple[List[List[float]], List[str]]:
    """解析多边形（顶点序列）→ ``([[lat, lon], ...], warnings)``。

    支持：
    * NOTAM 原生串：``N380800E1075600-N375700E1080000-N375100E1073300-N380200E1072900``
    * 十进制对：``33.566667,110.516667; 33.533333,110.733333; ...``
    * 每行一个顶点；行内 ``lat lon`` 空格分隔
    * DMS 对：``33°34′00″N 110°31′00″E``
    """
    warns: List[str] = []
    s = str(text or "").strip()
    if not s:
        return [], ["多边形为空"]

    verts: List[List[float]] = []

    # 0) 带 °′″ 符号的 DMS 顶点（GE 截图/中文资料常见）
    verts = _polygon_from_dms_symbols(s)
    if len(verts) >= 3:
        return _dedupe(verts), warns

    # 1) 归一化半球前缀后按 "lat hemi + lon hemi" 抓取（ICAO NOTAM 原生写法）
    s_norm = _norm_hemi_prefix(s)
    pairs = _RE_COORD_PAIR.findall(s_norm)
    if len(pairs) >= 3:
        for a, b in pairs:
            try:
                lat, lon = _cn().parse_vertex("%s %s" % (a, b))
                verts.append([float(lat), float(lon)])
            except Exception:  # noqa: BLE001
                continue
        if len(verts) >= 3:
            return _dedupe(verts), warns

    # 2) 十进制对（", " 或 ";" 或换行分隔）
    verts = []
    for m in re.finditer(
            r"(-?\d{1,3}(?:\.\d+))\s*[,，\s]\s*(-?\d{1,3}(?:\.\d+))", s):
        a, b = float(m.group(1)), float(m.group(2))
        if abs(a) > 90 and abs(b) <= 90:
            a, b = b, a
        if abs(a) <= 90 and abs(b) <= 180:
            verts.append([a, b])
    verts = _dedupe(verts)
    if len(verts) >= 3:
        return verts, warns

    warns.append("多边形顶点不足 3 个（解析到 %d 个），请检查格式" % len(verts))
    return verts, warns


def _polygon_from_dms_symbols(s: str) -> List[List[float]]:
    """从文本中抽取所有 ``25°12′34.5″N`` 形式坐标并按"纬度-经度"配对成顶点。"""
    lat_pat = re.compile(
        r"(\d{1,3}(?:\.\d+)?)\s*°\s*(?:(\d{1,2}(?:\.\d+)?)\s*[′'])?\s*"
        r"(?:(\d{1,2}(?:\.\d+)?)\s*(?:″|\"|''))?\s*([NSns])", re.IGNORECASE)
    lon_pat = re.compile(
        r"(\d{1,3}(?:\.\d+)?)\s*°\s*(?:(\d{1,2}(?:\.\d+)?)\s*[′'])?\s*"
        r"(?:(\d{1,2}(?:\.\d+)?)\s*(?:″|\"|''))?\s*([EWew])", re.IGNORECASE)

    def _val(m):
        v = float(m.group(1))
        if m.group(2):
            v += float(m.group(2)) / 60.0
        if m.group(3):
            v += float(m.group(3)) / 3600.0
        if m.group(4).upper() in ("S", "W"):
            v = -v
        return v

    lat_toks = [_val(m) for m in lat_pat.finditer(s)]
    lon_toks = [_val(m) for m in lon_pat.finditer(s)]
    verts: List[List[float]] = []
    for la, lo in zip(lat_toks, lon_toks):
        verts.append([round(la, 8), round(lo, 8)])
    return verts


def _dedupe(verts: List[List[float]]) -> List[List[float]]:
    out: List[List[float]] = []
    for v in verts:
        if not out or abs(out[-1][0] - v[0]) > 1e-9 or abs(out[-1][1] - v[1]) > 1e-9:
            out.append([round(v[0], 8), round(v[1], 8)])
    if len(out) >= 2 and abs(out[0][0] - out[-1][0]) < 1e-9 and abs(out[0][1] - out[-1][1]) < 1e-9:
        out = out[:-1]
    return out


def extract_qline(q: str) -> Dict[str, Any]:
    """解析 Q 行：``Q)ZLHW/QRDCA/IV/BO/W/000/999/3800N10745E012``。

    返回 ``{"fir","subject","condition","polygon":[lat,lon],"radius_nm","raw"}``。
    末段 5 位 = DDMM + DDDMM + 半径 nm 的紧凑写法（例 ``3800N10745E012``）。
    """
    out: Dict[str, Any] = {"raw": (q or "").strip(), "fir": "", "qcode": "",
                           "center": None, "radius_nm": None}
    s = (q or "").strip()
    if not s:
        return out
    s2 = re.sub(r"^Q\)\s*", "", s, flags=re.IGNORECASE)
    parts = [p for p in s2.split("/") if p.strip()]
    if parts:
        out["fir"] = parts[0].strip().upper()
    # 末段：DDMMN DDDMME RRR
    m = re.search(r"(\d{2})(\d{2})([NS])(\d{3})(\d{2})([EW])(\d{3})", s2.replace(" ", ""))
    if m:
        lat = int(m.group(1)) + int(m.group(2)) / 60.0
        lon = int(m.group(4)) + int(m.group(5)) / 60.0
        if m.group(3).upper() == "S":
            lat = -lat
        if m.group(6).upper() == "W":
            lon = -lon
        out["center"] = [round(lat, 5), round(lon, 5)]
        out["radius_nm"] = int(m.group(7))
        if len(parts) >= 3:
            out["qcode"] = "/".join(parts[1:3])
    return out


def polygon_from_circle(center: List[float], radius_nm: float, n: int = 8) -> List[List[float]]:
    """圆心 + 半径（nm）→ 内接 n 边形（球面近似，1 nm = 1.852 km）。"""

    import math
    lat0, lon0 = float(center[0]), float(center[1])
    r_km = float(radius_nm) * 1.852
    dlat = r_km / 111.195
    dlon = r_km / (111.195 * max(0.15, math.cos(math.radians(lat0))))
    pts = []
    for i in range(n):
        a = 2.0 * math.pi * i / n
        pts.append([round(lat0 + dlat * math.cos(a), 6), round(lon0 + dlon * math.sin(a), 6)])
    return pts


# --------------------------------------------------------------------------- #
# 残骸类型推断
# --------------------------------------------------------------------------- #
def infer_stage_hint(text: str) -> Tuple[str, str]:
    """从 E) 文本/备注里推断残骸类型，返回 ``(hint, 依据)``；无法判断返回 ``("", "")``。"""
    low = (text or "").lower()
    for keys, hint in _STAGE_KEYWORDS:
        for k in keys:
            if k in low:
                return hint, "关键词「%s」" % k
    return "", ""


# --------------------------------------------------------------------------- #
# 单条 / 多条航警解析
# --------------------------------------------------------------------------- #
def _split_notams(text: str) -> List[str]:
    """把粘贴文本按航警编号切成若干段。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    idx = [m.start() for m in _RE_NOTAM_ID.finditer(text)]
    if not idx:
        return [text]
    idx = sorted(set(idx))
    blocks = []
    for i, st in enumerate(idx):
        en = idx[i + 1] if i + 1 < len(idx) else len(text)
        blocks.append(text[st:en])
    head = text[:idx[0]].strip()
    if head and ("Q)" in head.upper() or "B)" in head.upper()):
        blocks.insert(0, head)
    return [b for b in blocks if b.strip()]


def parse_one_notam(text: str, ref_year: Optional[int] = None) -> Tuple[Dict[str, Any], List[str]]:
    """解析一条航警 → 配置字典（``id/b_utc/c_utc/q_line/fir/polygon_latlon/stage_hint/verified``）。"""
    warns: List[str] = []
    raw = (text or "").strip()
    z: Dict[str, Any] = {
        "id": "", "b_utc": "", "c_utc": "", "q_line": "", "fir": "",
        "polygon_latlon": [], "stage_hint": "", "verified": "",
        "raw_text": raw,
    }
    m = _RE_NOTAM_ID.search(raw)
    if m:
        z["id"] = "%s%s/%s" % (m.group(1), m.group(2), m.group(3))
    else:
        # 中文"航警号/编号：..."
        m2 = re.search(r"(?:航警号|编号|NOTAM\s*(?:号)?)\s*[:：]?\s*([A-Z]\d{4}/\d{2})", raw,
                       re.IGNORECASE)
        if m2:
            z["id"] = m2.group(1).upper()
        else:
            warns.append("未识别航警编号")

    mq = _RE_Q.search(raw)
    if mq:
        z["q_line"] = "Q)" + mq.group(1).strip()
        q = extract_qline(z["q_line"])
        if q.get("fir"):
            z["fir"] = q["fir"]
    else:
        warns.append("未识别 Q 行")

    ma = _RE_A.search(raw)
    if ma and not z["fir"]:
        z["fir"] = ma.group(1).strip()

    mb = _RE_B.search(raw)
    if mb:
        z["b_utc"] = parse_notam_dt(mb.group(1), ref_year) or ""
    if not z["b_utc"]:
        m2 = re.search(r"(?:B\)|生效|开始时间|起始)\s*[:：]?\s*(\d{10}|[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9:]{5,8}Z?)",
                       raw, re.IGNORECASE)
        if m2:
            iso = parse_notam_dt(m2.group(1), ref_year)
            if iso:
                z["b_utc"] = iso
    mc = _RE_C.search(raw)
    if mc and mc.group(1) != "PERM":
        z["c_utc"] = parse_notam_dt(mc.group(1), ref_year) or ""
    if not z["c_utc"]:
        m3 = re.search(r"(?:C\)|失效|结束时间|终止)\s*[:：]?\s*(\d{10}|[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9:]{5,8}Z?)",
                       raw, re.IGNORECASE)
        if m3:
            iso = parse_notam_dt(m3.group(1), ref_year)
            if iso:
                z["c_utc"] = iso

    # 多边形：先找 BOUNDED BY 段，再找"多边形/顶点/坐标"标注，再全局扫
    poly_text = ""
    mbd = _RE_BOUNDED.search(raw)
    if mbd:
        poly_text = mbd.group(1)
    if not poly_text:
        m4 = re.search(r"(?:多边形|顶点|边界|BOUNDED BY|POLYGON|VERTICES)\s*[:：]?\s*([\s\S]+)", raw,
                       re.IGNORECASE)
        if m4:
            poly_text = m4.group(1)
    if poly_text:
        verts, w = parse_polygon(poly_text)
        if len(verts) >= 3:
            z["polygon_latlon"] = verts
        warns.extend(w)
    if not z["polygon_latlon"]:
        # 全文本扫描（排除 Q 行里的紧凑串，避免误判）
        body = raw
        if z["q_line"]:
            body = body.replace(z["q_line"], " ")
        verts, w = parse_polygon(body)
        if len(verts) >= 3:
            z["polygon_latlon"] = verts
            warns.append("多边形由全文本扫描得到，请核对顺序/顶点")
    if not z["polygon_latlon"] and z["q_line"]:
        q = extract_qline(z["q_line"])
        if q.get("center") and q.get("radius_nm"):
            z["polygon_latlon"] = polygon_from_circle(q["center"], q["radius_nm"])
            warns.append("无多边形顶点，已按 Q 行圆心+%s nm 合成八边形（近似）" % q["radius_nm"])

    # 残骸类型
    mh = re.search(r"(?:残骸|stage_hint|类型|归属)\s*[:：]?\s*([^\r\n，,;；]+)", raw)
    if mh:
        hint, why = infer_stage_hint(mh.group(1))
        if hint:
            z["stage_hint"] = hint
            z["verified"] = "用户标注（%s）" % why
    if not z["stage_hint"]:
        me = _RE_E.search(raw)
        etext = me.group(1) if me else raw
        hint, why = infer_stage_hint(etext)
        if hint:
            z["stage_hint"] = hint
            z["verified"] = "由 E) 正文推断（%s）" % why

    if not z["b_utc"]:
        warns.append("未识别生效时刻 B)")
    if not z["c_utc"]:
        warns.append("未识别失效时刻 C)")
    if not z["polygon_latlon"]:
        warns.append("未识别多边形顶点")
    z["verified"] = z["verified"] or "用户粘贴原文（未逐字核对）"
    return z, warns


def parse_notams(text: str, ref_year: Optional[int] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    """解析多段航警粘贴文本 → (list[dict], warnings)。"""
    text = (text or "").strip()
    if not text:
        return [], []
    blocks = _split_notams(text)
    out, warns = [], []
    for i, b in enumerate(blocks):
        try:
            z, w = parse_one_notam(b, ref_year)
        except Exception as exc:  # noqa: BLE001
            warns.append("第 %d 段解析异常：%s" % (i + 1, exc))
            continue
        if not any([z["id"], z["b_utc"], z["polygon_latlon"]]):
            warns.append("第 %d 段未解析出有效字段，已跳过" % (i + 1))
            continue
        out.append(z)
        warns.extend(["%s: %s" % (z["id"] or ("第%d段" % (i + 1)), w) for w in w])
    return out, warns


def parse_observers_text(text: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """解析"观测点"粘贴文本（每行一个点，支持 ``名称 37°45′41″N 112°43′30″E 840m``）。"""
    out, warns = [], []
    for ln in re.split(r"[\r\n;；]+", text or ""):
        ln = ln.strip()
        if not ln or len(re.findall(r"\d", ln)) < 3:
            continue
        try:
            p = parse_latlon(ln)
        except Exception:  # noqa: BLE001
            warns.append("无法解析观测点行：%s" % ln)
            continue
        name = re.sub(r"[-\d\.°′″'\"NSEWnsew,，/;；:：]+", " ", ln)
        name = re.sub(r"(?:^|\s)(?:m|米|高程|海拔)(?=\s|$)", " ", name, flags=re.IGNORECASE)
        name = re.sub(r"\s+", " ", name).strip(" -_)(")
        p["name"] = name[:40]
        p["raw"] = ln
        out.append(p)
    return out, warns
