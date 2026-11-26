# -*- coding: utf-8 -*-
"""生成三维 Google Earth 文件（KMZ/KML）——按用户指定参考样式重制（v3.0 参数化版）。

v3.0 参数化：本脚本不再硬编码任何算例常量，全部由 ``config.json`` + ``sim/`` 驱动
-----------------------------------------------------------------------------
* 起飞时刻 T0 ← ``config.mission.t0_utc``（缺失回退 sim/trajectory_meta.json / 首点 t_utc）
* 发射场 SITE ← ``config.mission.launch_site``（name/lon_deg/lat_deg/alt_m_ellipsoid）
* 飞行阶段 STAGE_STYLE ← ``sim/trajectory.csv`` 的 phase 列 + ``sim/弹道事件表.csv``
  事件时刻（分离/关机）+ ``sim/trajectory_meta.json``（guidance/insertion_t_met_s）；
  阶段配色沿用参考 KML 的彩虹序列；中文标题按事件时刻动态拼（如"一级（T+0 ~ 156.7 s）"）；
  无法判断阶段（无 phase 列）时退化为单段"上升段"。
* 残骸样式 DEBRIS_STYLE ← ``sim/objects.json``（obj/name_cn/color），
  兼容 core_stage/stage_N/fairing_L|R/booster_N 任意组合；没有哪类就不画哪类。
* 航警区 ZONES ← ``sim/notam_zones.json``（id/polygon_latlon/b_utc/c_utc/q_line/fir/
  stage_hint），Q 行圆（中心 + 半径 NM）从 q_line 解析（DDMMNDDDMME + 3 位半径）；
  notams 为空时仍正常输出 KML（不画危险区，仅注记"预测落区"）。

参考样式（用户 2026-11-19 提供 `弹道三维_含残骸坠落.kmz`，CZ-6A 版）：
  * Document：标题 + 长描述（火箭/发射场/起飞时刻/采样点/阶段配色/落区/落点/说明）
  * Folder「弹道（三维 + 星下点）」：发射点 + 各飞行阶段彩线（width=6，红-橙-黄-绿…）+ 星下点洋红线
  * Folder「飞行事件（N）」：事件点（icon_key / icon_event）+ T+/UTC/BJT/高度/速度/质量/备注
  * Folder「落区与残骸落点」：危险区多边形（红半透明）+ 中心标注 + Q 行圆（青）+ 落点（绿色=区内）
  * Folder「时间动画（gx:Track，N 点）」：1 s/点，可用时间滑块播放
  * Folder「预设视角与 Tour」：LookAt 预设 + gx:Tour 相机跟随
  * Folder「残骸坠落曲线」：每个残骸子文件夹（分离点 + 3D 曲线 + 地面投影 + 1 s 时间动画）
图标沿用 ``<RK_ENGINE>/viz/files/icon_*.png``（48×48；v3 引擎目录由 RK_ENGINE 解析）。

输出（文件名固定，下游 qc.py 依赖）：viz/弹道三维_含残骸坠落.kml/.kmz + viz/校验_KML.json
用法：python engine/scripts/make_kml_refstyle.py --config <RK_ROOT>/config.json [--out <dir>]
"""
import os
import sys, os, json, math, html, zipfile, re, csv
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.abspath(os.environ.get("RK_ENGINE") or os.path.dirname(HERE))
ROOT = os.path.abspath(os.environ.get("RK_ROOT") or os.path.dirname(ENGINE))
for _p in (ENGINE, HERE, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import numpy as np
from common import notam as N

VIZ = os.path.join(ROOT, "viz")
SIM = os.path.join(ROOT, "sim")

# 阶段配色：沿用参考 KML 的彩虹序列（KML 颜色为 aabbggrr），超出时按顺序追加
STAGE_COLORS = ["ff4646ff", "ff00a5ff", "ff28f0ff", "ff82ff46",
                "ffffff46", "ffff4646", "ffff46a5"]
# 运行时结构（由 build_context() 填充）
STAGE_STYLE = []      # [dict(sid,color,label,a,b)]
DEBRIS_STYLES = []    # [dict(obj,sid,col,gcol,name_cn)]
DEBRIS_BY_OBJ = {}    # obj -> style dict
ZONES = {}            # id -> dict(...)


# ------------------------------------------------------------------ helpers
def utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bj(dt):
    return (dt.astimezone(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def esc(s):
    return html.escape(str(s), quote=False)


def cdata(s):
    return "<![CDATA[" + str(s).replace("]]>", "]]]]><![CDATA[>") + "]]>"


def coords_line(lon, lat, alt, indent="  "):
    return "\n".join("%s%.6f,%.6f,%.1f" % (indent, lo, la, al) for lo, la, al in zip(lon, lat, alt))


def read_csv(path):
    import csv
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_json(path, default):
    try:
        if path and os.path.exists(path):
            return json.load(open(path, encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print("[kml] 警告: 读取 %s 失败: %s" % (path, exc))
    return default


def parse_utc_aware(s):
    """ISO 字符串 → aware UTC datetime；None/空 → None。"""
    if s is None:
        return None
    t = str(s).strip()
    if not t:
        return None
    try:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def kml_color_to_hex(col):
    """KML aabbggrr → CSS #rrggbb。"""
    c = str(col).strip().lstrip("#").lower()
    if len(c) == 8:
        return "#" + c[6:8] + c[4:6] + c[2:4]
    if len(c) == 6:
        return "#" + c
    return "#cccccc"


def hex_to_kml(hex_color, alpha="ff"):
    """CSS #rrggbb → KML aabbggrr。"""
    c = str(hex_color or "").strip().lstrip("#")
    if len(c) != 6:
        c = "cccccc"
    try:
        int(c, 16)
    except ValueError:
        c = "cccccc"
    return alpha + c[4:6] + c[2:4] + c[0:2]


def _safe_id(text):
    return "debris_" + re.sub(r"[^0-9A-Za-z]+", "_", str(text)).strip("_") or "obj"


_Q_LINE_RE = re.compile(r"(\d{2})(\d{2})([NS])(\d{3})(\d{2})([EW])(\d{2,3})\s*$")


def parse_qline_circle(q_line):
    """解析 NOTAM Q 行末尾的圆心与半径：``3322N11036E013`` → ((33.3667,110.6), 13)。

    末尾 3 位为半径 [NM]。返回 ``(center_latlon|None, radius_nm|None)``。
    """
    if not q_line:
        return None, None
    s = str(q_line).strip().replace(" ", "")
    m = _Q_LINE_RE.search(s)
    if not m:
        return None, None
    try:
        lat = int(m.group(1)) + int(m.group(2)) / 60.0
        if m.group(3).upper() == "S":
            lat = -lat
        lon = int(m.group(4)) + int(m.group(5)) / 60.0
        if m.group(6).upper() == "W":
            lon = -lon
        radius = int(m.group(7))
    except (TypeError, ValueError):
        return None, None
    return (lat, lon), (radius if radius > 0 else None)


# ------------------------------------------------------------------ KML 写出器
class KMLWriter:
    def __init__(self, title, desc):
        self.parts = []
        self.title = title
        self.desc = desc

    def begin(self, lookat):
        self.parts.append('<?xml version="1.0" encoding="UTF-8"?>\n'
                          '<kml xmlns="http://www.opengis.net/kml/2.2" '
                          'xmlns:gx="http://www.google.com/kml/ext/2.2">\n<Document>\n')
        self.parts.append("<name>%s</name>\n" % esc(self.title))
        self.parts.append("<description>%s</description>\n" % cdata(self.desc))
        self.parts.append("<open>1</open>\n")
        if lookat:
            lo, la, rng, tilt = lookat
            self.parts.append('<LookAt><longitude>%.6f</longitude><latitude>%.6f</latitude>'
                              '<altitude>0</altitude><heading>0</heading><tilt>%d</tilt>'
                              '<range>%d</range><altitudeMode>clampToGround</altitudeMode></LookAt>\n'
                              % (lo, la, tilt, rng))
        self._styles()

    def _styles(self):
        st = []
        for seg in STAGE_STYLE:
            st.append('<Style id="%s"><LineStyle><color>%s</color><width>6</width></LineStyle></Style>'
                      % (seg["sid"], seg["color"]))
        st.append('<Style id="groundTrack"><LineStyle><color>ffff28ff</color><width>3</width>'
                  '</LineStyle></Style>')
        st.append('<Style id="stationCtx"><LineStyle><color>ff00ff00</color><width>3</width>'
                  '</LineStyle></Style>')
        st.append('<Style id="rdvFlow"><LineStyle><color>ffffc800</color><width>5</width>'
                  '</LineStyle></Style>')
        st.append('<Style id="rdvFlowGround"><LineStyle><color>ffffc800</color><width>3</width>'
                  '</LineStyle></Style>')
        st.append('<Style id="rdvAnim"><LineStyle><color>ffffc800</color><width>4</width>'
                  '</LineStyle><IconStyle><scale>1.2</scale><Icon>'
                  '<href>files/icon_track.png</href></Icon></IconStyle>'
                  '<LabelStyle><scale>1.0</scale></LabelStyle></Style>')
        st.append('<Style id="trackAnim"><LineStyle><color>ffffff00</color><width>3</width>'
                  '</LineStyle><IconStyle><scale>1.1</scale><Icon><href>files/icon_track.png</href>'
                  '</Icon></IconStyle><LabelStyle><scale>0.9</scale></LabelStyle></Style>')
        st.append('<Style id="ev"><IconStyle><scale>1.05</scale><Icon><href>files/icon_event.png'
                  '</href></Icon></IconStyle><LabelStyle><color>ffffffff</color><scale>1.0</scale>'
                  '</LabelStyle></Style>')
        st.append('<Style id="evKey"><IconStyle><scale>1.25</scale><Icon><href>files/icon_key.png'
                  '</href></Icon></IconStyle><LabelStyle><color>ffffffff</color><scale>1.05</scale>'
                  '</LabelStyle></Style>')
        st.append('<Style id="launch"><IconStyle><scale>1.25</scale><Icon><href>files/icon_launch.png'
                  '</href></Icon></IconStyle><LabelStyle><color>ffffffff</color><scale>1.1</scale>'
                  '</LabelStyle></Style>')
        st.append('<Style id="impactIn"><IconStyle><scale>1.2</scale><Icon><href>files/icon_impact_in.png'
                  '</href></Icon></IconStyle><LabelStyle><color>ff90ff90</color><scale>1.05</scale>'
                  '</LabelStyle></Style>')
        st.append('<Style id="impactInSmall"><IconStyle><scale>1.0</scale><Icon>'
                  '<href>files/icon_impact_in.png</href></Icon></IconStyle>'
                  '<LabelStyle><scale>0.8</scale></LabelStyle></Style>')
        # v3.2：区外落点用独立图标/配色（此前一律画成"区内"绿色图标）
        st.append('<Style id="impactOut"><IconStyle><scale>1.2</scale><Icon>'
                  '<href>files/icon_impact_out.png</href></Icon></IconStyle>'
                  '<LabelStyle><color>ff5050ff</color><scale>1.05</scale></LabelStyle></Style>')
        st.append('<Style id="impactOther"><IconStyle><scale>1.1</scale><Icon>'
                  '<href>files/icon_impact_other.png</href></Icon></IconStyle>'
                  '<LabelStyle><color>ffffffff</color><scale>1.0</scale></LabelStyle></Style>')
        st.append('<Style id="danger"><LineStyle><color>ff1e1eff</color><width>5</width></LineStyle>'
                  '<PolyStyle><color>461e1eff</color></PolyStyle>'
                  '<LabelStyle><color>ffffffff</color><scale>1.25</scale></LabelStyle></Style>')
        st.append('<Style id="danger2"><LineStyle><color>ff00b4ff</color><width>5</width></LineStyle>'
                  '<PolyStyle><color>4600b4ff</color></PolyStyle>'
                  '<LabelStyle><color>ffffffff</color><scale>1.25</scale></LabelStyle></Style>')
        st.append('<Style id="qcircle"><LineStyle><color>e6ffeb00</color><width>2.5</width>'
                  '</LineStyle><PolyStyle><color>19ffeb00</color></PolyStyle></Style>')
        st.append('<Style id="label"><IconStyle><scale>0.1</scale><Icon><href>files/icon_lookat.png'
                  '</href></Icon></IconStyle><LabelStyle><color>ffffffff</color><scale>1.2</scale>'
                  '</LabelStyle></Style>')
        st.append('<Style id="lookat"><IconStyle><scale>1.0</scale><Icon><href>files/icon_lookat.png'
                  '</href></Icon></IconStyle></Style>')
        for ds in DEBRIS_STYLES:
            st.append('<Style id="%s"><LineStyle><color>%s</color><width>5</width></LineStyle></Style>'
                      % (ds["sid"], ds["col"]))
            st.append('<Style id="%s_ground"><LineStyle><color>%s</color><width>2</width>'
                      '</LineStyle></Style>' % (ds["sid"], ds["gcol"]))
        self.parts.append("\n".join(st) + "\n")

    def folder(self, name, desc_cdata=None, opened=True, vis=True):
        self.parts.append("<Folder><name>%s</name>%s%s%s\n"
                          % (esc(name),
                             "" if vis else "<visibility>0</visibility>",
                             "<open>1</open>" if opened else "",
                             ("<description>%s</description>" % cdata(desc_cdata)) if desc_cdata else ""))

    def endfolder(self):
        self.parts.append("</Folder>\n")

    def point(self, name, lon, lat, alt, style="ev", desc=None, clamp=False, vis=True):
        self.parts.append(
            '<Placemark><name>%s</name><styleUrl>#%s</styleUrl>%s%s<Point>'
            '<altitudeMode>%s</altitudeMode><coordinates>%.6f,%.6f,%.1f</coordinates>'
            '</Point></Placemark>\n'
            % (esc(name), style,
               "" if vis else "<visibility>0</visibility>",
               ("<description>%s</description>" % cdata(desc)) if desc else "",
               "clampToGround" if clamp else "absolute", lon, lat, alt))

    def line(self, name, lon, lat, alt, style, desc=None, mode="absolute", vis=True):
        self.parts.append(
            '<Placemark><name>%s</name><styleUrl>#%s</styleUrl>%s%s<LineString>'
            '<altitudeMode>%s</altitudeMode><tessellate>1</tessellate><coordinates>\n%s\n'
            '</coordinates></LineString></Placemark>\n'
            % (esc(name), style, "" if vis else "<visibility>0</visibility>",
               ("<description>%s</description>" % cdata(desc)) if desc else "",
               mode, coords_line(lon, lat, alt)))

    def polygon(self, name, lats, lons, style, desc=None):
        pts = list(zip(lons, lats)) + [(lons[0], lats[0])]
        self.parts.append(
            '<Placemark><name>%s</name><styleUrl>#%s</styleUrl>%s<Polygon><extrude>0</extrude>'
            '<tessellate>1</tessellate><altitudeMode>clampToGround</altitudeMode>'
            '<outerBoundaryIs><LinearRing><coordinates>\n%s\n</coordinates></LinearRing>'
            '</outerBoundaryIs></Polygon></Placemark>\n'
            % (esc(name), style, ("<description>%s</description>" % cdata(desc)) if desc else "",
               "\n".join("  %.6f,%.6f,0" % (lo, la) for lo, la in pts)))

    def circle(self, name, clat, clon, radius_nm, style, desc=None):
        r_km = radius_nm * 1.852
        dlat = r_km / 111.19
        dlon = r_km / (111.19 * math.cos(math.radians(clat)))
        lats = [clat + dlat * math.cos(math.radians(a)) for a in range(0, 361, 10)]
        lons = [clon + dlon * math.sin(math.radians(a)) for a in range(0, 361, 10)]
        self.polygon(name, lats, lons, style, desc)

    def track(self, name, times, chans, style="trackAnim", desc=None, vis=True, mode="absolute"):
        whens = "".join("<when>%s</when>" % utc(t) for t in times)
        coords = "".join("<gx:coord>%.6f %.6f %.1f</gx:coord>" % (lo, la, al)
                         for lo, la, al in chans)
        self.parts.append(
            '<Placemark><name>%s</name>%s<styleUrl>#%s</styleUrl>%s<gx:Track>'
            '<altitudeMode>%s</altitudeMode>%s%s</gx:Track></Placemark>\n'
            % (esc(name), "" if vis else "<visibility>0</visibility>", style,
               ("<description>%s</description>" % cdata(desc)) if desc else "", mode, whens, coords))

    def lookat(self, name, lon, lat, rng, tilt, desc=None):
        self.parts.append(
            '<Placemark><name>%s</name><styleUrl>#lookat</styleUrl>%s<LookAt>'
            '<longitude>%.6f</longitude><latitude>%.6f</latitude><altitude>0</altitude>'
            '<heading>0</heading><tilt>%d</tilt><range>%d</range>'
            '<altitudeMode>clampToGround</altitudeMode></LookAt></Placemark>\n'
            % (esc(name), ("<description>%s</description>" % cdata(desc)) if desc else "",
               lon, lat, tilt, rng))

    def tour(self, name, frames, desc=None):
        body = "".join(
            '<gx:FlyTo><gx:duration>2.4</gx:duration><gx:flyToMode>smooth</gx:flyToMode>'
            '<LookAt><longitude>%.6f</longitude><latitude>%.6f</latitude><altitude>0</altitude>'
            '<heading>0</heading><tilt>%d</tilt><range>%d</range>'
            '<altitudeMode>clampToGround</altitudeMode></LookAt></gx:FlyTo>'
            % (lo, la, tilt, rng) for lo, la, tilt, rng in frames)
        self.parts.append('<gx:Tour><name>%s</name>%s<gx:Playlist>%s</gx:Playlist></gx:Tour>\n'
                          % (esc(name), ("<description>%s</description>" % cdata(desc)) if desc else "",
                             body))

    def end(self):
        self.parts.append("</Document>\n</kml>\n")
        return "".join(self.parts)


# ------------------------------------------------------------------ 数据装载
def _zone_body(hint):
    h = str(hint or "").strip()
    if not h:
        return "危险区"
    if "助推" in h:
        return "助推器（无控弹道预测落区）"
    if "整流罩" in h or "fairing" in h.lower():
        return "整流罩（无控）"
    if any(k in h for k in ("一级", "二级", "三级", "末级", "子级")):
        return "%s（本模型按等效无控弹道计算落点）" % h
    return "%s（危险区）" % h


def _zone_time(s):
    dt = parse_utc_aware(s)
    return dt


def build_zones(cfg):
    """航警区：优先 sim/notam_zones.json，缺失回退 config.notams。"""
    data = load_json(os.path.join(SIM, "notam_zones.json"), None)
    raw = []
    if isinstance(data, dict):
        raw = data.get("zones") or []
    elif isinstance(data, list):
        raw = data
    src = "sim/notam_zones.json"
    if not raw:
        raw = cfg.get("notams") or []
        src = "config.json → notams"
    zones = {}
    for z in raw:
        if not isinstance(z, dict):
            continue
        zid = str(z.get("id") or "").strip()
        if not zid:
            continue
        poly = []
        for p in (z.get("polygon_latlon") or []):
            try:
                poly.append([float(p[0]), float(p[1])])
            except (TypeError, ValueError, IndexError):
                continue
        has_poly = len(poly) >= 3
        if not has_poly:
            print("[kml] 警告: 航警区 %s 多边形顶点 <3，仅画 Q 行圆/中心点" % zid)
        qc, qr = parse_qline_circle(z.get("q_line"))
        if qc is None and has_poly:
            try:
                qc = N.polygon_centroid(np.array(poly))
            except Exception:  # noqa: BLE001
                qc = None
        hint = str(z.get("stage_hint") or "")
        body = _zone_body(hint)
        zones[zid] = dict(
            id=zid, poly=poly, has_poly=has_poly,
            b=_zone_time(z.get("b_utc")), c=_zone_time(z.get("c_utc")),
            b_raw=z.get("b_utc"), c_raw=z.get("c_utc"),
            qline=z.get("q_line") or "", qc=qc, qr=qr,
            hint=hint, body=body,
            name="%s（%s）" % (zid, (hint + "落区") if hint else "危险区"),
            fix=str(z.get("fir") or ""), source=src,
        )
    return zones


_DEFAULT_OBJ_COLOR = {"fairing": "#f39c12", "booster": "#e67e22",
                      "core": "#8e44ad", "stage": "#8e44ad"}


def _default_obj_color(obj):
    o = str(obj).lower()
    for k, v in _DEFAULT_OBJ_COLOR.items():
        if k in o:
            return v
    return "#9aa0a6"


def build_debris_styles(deb_rows):
    """残骸样式：sim/objects.json 的 obj/name_cn/color；缺项按对象名兜底。"""
    objects = load_json(os.path.join(SIM, "objects.json"), []) or []
    out, by_obj = [], {}

    def add(obj, name_cn=None, color=None):
        if obj in by_obj:
            return by_obj[obj]
        name_cn = str(name_cn or obj)
        color = str(color or _default_obj_color(obj))
        kcol = hex_to_kml(color)
        gcol = "80" + kcol[2:]
        st = dict(obj=obj, sid=_safe_id(obj), col=kcol, gcol=gcol, name_cn=name_cn)
        # style id 冲突时加序号
        used = {x["sid"] for x in out}
        if st["sid"] in used:
            k = 2
            while "%s_%d" % (st["sid"], k) in used:
                k += 1
            st["sid"] = "%s_%d" % (st["sid"], k)
        by_obj[obj] = st
        out.append(st)
        return st

    for o in objects if isinstance(objects, list) else []:
        if isinstance(o, dict) and o.get("obj"):
            add(str(o["obj"]), o.get("name_cn"), o.get("color"))
    for r in deb_rows:
        obj = str(r.get("object") or "").strip()
        if obj and obj not in by_obj:
            add(obj)
    if not out:
        # 连 objects.json 都没有：按残骸轨迹里出现的对象名兜底
        for r in deb_rows:
            obj = str(r.get("object") or "").strip()
            if obj:
                add(obj)
    return out, by_obj


def _phase_runs(t, phase):
    runs = []
    n = len(phase)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and str(phase[j + 1]) == str(phase[i]):
            j += 1
        runs.append((str(phase[i]), float(t[i]), float(t[j]), i, j))
        i = j + 1
    return runs


def _phase_stage_index(ph):
    """S1/S2/S3_ALL → 阶段序号 0/1/2；入轨相位 → -2；未知 → -1。"""
    p = str(ph).strip().upper()
    m = re.match(r"S(\d+)", p)
    if m:
        return max(0, int(m.group(1)) - 1)
    if p.startswith(("ORBIT", "COAST", "INSERT", "POST")):
        return -2
    return -1


def _event_time(events, keywords, exclude=()):
    for r in events:
        name = str(r.get("event") or "")
        if any(k in name for k in keywords) and not any(e in name for e in exclude):
            try:
                return float(r["t_met_s"])
            except (KeyError, TypeError, ValueError):
                continue
    return None


def _nearest_stage_event(events, t_target, tol=6.0):
    """与相位切换采样时刻最接近的级分离/关机事件时刻（排除级内主机/游机/助推器事件）。"""
    best = None
    for r in events:
        name = str(r.get("event") or "")
        if not any(k in name for k in ("分离", "关机")):
            continue
        if any(e in name for e in ("主机", "游机", "助推器")):
            continue
        try:
            tt = float(r["t_met_s"])
        except (KeyError, TypeError, ValueError):
            continue
        if abs(tt - t_target) <= tol and (best is None or abs(tt - t_target) < abs(best - t_target)):
            best = tt
    return best


def _intra_stage_split(events, a, b):
    """级内"主机→游机"切换时刻（如"二级主机关机"），无则 None。"""
    for r in events:
        name = str(r.get("event") or "")
        if "主机" in name and "关机" in name:
            try:
                tt = float(r["t_met_s"])
            except (KeyError, TypeError, ValueError):
                continue
            if a + 1.0 < tt < b - 1.0:
                return tt
    return None


def build_stages(t, phase, events, meta, cfg):
    """按 phase 列 + 事件表生成飞行阶段（含中文标题与事件时刻边界）。"""
    stage_names = []
    for s in ((cfg.get("vehicle") or {}).get("stages") or []):
        nm = s.get("name")
        if nm:
            stage_names.append(str(nm))
    n_meta = int(((meta.get("vehicle") or {}).get("stages")) or 0)
    defaults = ["一级", "二级", "三级", "四级"]
    while len(stage_names) < n_meta:
        stage_names.append(defaults[min(len(stage_names), len(defaults) - 1)])
    t_end = float(t[-1])

    runs = []
    if phase and any(str(p).strip() for p in phase):
        runs = _phase_runs(t, phase)
    if not runs:
        print("[kml] 提示: sim/trajectory.csv 无 phase 列/无法判断阶段 → 退化为单段「上升段」")
        return [dict(sid="stage0", color=STAGE_COLORS[0], label="上升段", a=0.0, b=t_end)]

    def label_for(idx, ph):
        si = _phase_stage_index(ph)
        if si == -2:
            return "入轨后"
        if 0 <= idx < len(stage_names):
            return stage_names[idx]
        if si >= 0:
            return "第%d级" % (si + 1)
        return str(ph).strip() or "上升段"

    raw_segs = []
    for k, (ph, a, b, _i0, _i1) in enumerate(runs):
        # 边界用事件时刻细化（相位切换点的采样时间 → 关机/分离事件时刻）
        if k + 1 < len(runs):
            nb = float(runs[k + 1][1])
            et = _nearest_stage_event(events, nb)
            if et is not None:
                b = et
                runs[k + 1] = (runs[k + 1][0], et, runs[k + 1][2],
                               runs[k + 1][3], runs[k + 1][4])
        si = _phase_stage_index(ph)
        label = label_for(si if si >= 0 else k, ph)
        split = _intra_stage_split(events, a, b)
        if split is not None and si >= 0:
            raw_segs.append((a, split, label + "主机"))
            raw_segs.append((split, b, label + "游机"))
        else:
            raw_segs.append((a, b, label))

    segs = []
    for idx, (a, b, lab) in enumerate(raw_segs):
        if b <= a + 1e-9:
            continue
        segs.append(dict(sid="stage%d" % idx,
                         color=STAGE_COLORS[min(idx, len(STAGE_COLORS) - 1)],
                         label=lab, a=float(a), b=float(b)))
    if not segs:
        return [dict(sid="stage0", color=STAGE_COLORS[0], label="上升段", a=0.0, b=t_end)]
    return segs


# ------------------------------------------------------------------ main
def main():
    import argparse
    ap = argparse.ArgumentParser(description="三维 Google Earth（KMZ/KML，参考样式；config/sim 驱动）")
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"),
                    help="任务配置（默认 <RK_ROOT>/config.json）")
    ap.add_argument("--obs", default=None, help="观测点 id（兼容流水线参数，本脚本不使用）")
    ap.add_argument("--out", default=None, help="输出目录（默认 <RK_ROOT>/viz）")
    args, _unknown = ap.parse_known_args()

    cfg = load_json(args.config or os.path.join(ROOT, "config.json"), {})
    out_dir = args.out or VIZ
    if not os.path.isabs(out_dir):
        out_dir = os.path.join(ROOT, out_dir)
    os.makedirs(out_dir, exist_ok=True)

    # ---- 输入数据 ----
    need = ["trajectory.csv", "debris_trajectories.csv", "弹道事件表.csv", "落点与射程.csv"]
    missing = [f for f in need if not os.path.exists(os.path.join(SIM, f))]
    if missing:
        print("[kml] 错误: sim/ 缺少 %s；请先运行 export.py" % ", ".join(missing))
        return 2
    traj = read_csv(os.path.join(SIM, "trajectory.csv"))
    deb = read_csv(os.path.join(SIM, "debris_trajectories.csv"))
    ev = read_csv(os.path.join(SIM, "弹道事件表.csv"))
    imp = read_csv(os.path.join(SIM, "落点与射程.csv"))
    orb = load_json(os.path.join(SIM, "入轨根数.json"), {})
    meta = load_json(os.path.join(SIM, "trajectory_meta.json"), {})

    mission = cfg.get("mission") or {}
    site_raw = mission.get("launch_site") or cfg.get("launch_site") or meta.get("site") or {}
    t = np.array([float(r["t_met_s"]) for r in traj])
    lat = np.array([float(r["lat_deg"]) for r in traj])
    lon = np.array([float(r["lon_deg"]) for r in traj])
    alt = np.array([float(r["alt_m"]) for r in traj])
    _first = traj[0]
    SITE = dict(
        name=str(site_raw.get("name") or (meta.get("site") or {}).get("name") or "发射场"),
        lon=float(site_raw.get("lon_deg", site_raw.get("lon", _first["lon_deg"]))),
        lat=float(site_raw.get("lat_deg", site_raw.get("lat", _first["lat_deg"]))),
        alt=float(site_raw.get("alt_m_ellipsoid",
                               site_raw.get("alt_m", site_raw.get("alt", _first["alt_m"])))),
    )
    T0 = parse_utc_aware(
        mission.get("t0_utc") or cfg.get("t0_nominal_utc") or meta.get("t0_utc")
        or _first.get("t_utc"))
    if T0 is None:
        T0 = datetime.now(timezone.utc)
        print("[kml] 警告: config/sim 均无 T0，使用当前时刻占位")
    phase = [str(r.get("phase") or "") for r in traj]
    dt_med = float(np.median(np.diff(t))) if t.size > 1 else 0.0

    global STAGE_STYLE, DEBRIS_STYLES, DEBRIS_BY_OBJ, ZONES
    STAGE_STYLE = build_stages(t, phase, ev, meta, cfg)
    DEBRIS_STYLES, DEBRIS_BY_OBJ = build_debris_styles(deb)
    ZONES = build_zones(cfg)

    rocket = str(mission.get("rocket") or "").strip()
    mission_name = str(mission.get("name") or "").strip() or (rocket or "弹道仿真任务")
    notam_ids = "·".join(ZONES.keys())
    title = mission_name
    if notam_ids and "NOTAM" not in title:
        title = "%s / NOTAM %s 上升段 3-DOF 弹道仿真" % (
            str(mission.get("rocket") or mission_name), notam_ids)

    # 视角包围盒（站点 + 轨迹 + 航警区）
    xs = [SITE["lon"]] + list(lon) + [p[1] for z in ZONES.values() for p in z["poly"]]
    ys = [SITE["lat"]] + list(lat) + [p[0] for z in ZONES.values() for p in z["poly"]]
    lon_min, lon_max, lat_min, lat_max = min(xs), max(xs), min(ys), max(ys)

    def _diag_km(lo0, lo1, la0, la1):
        dlon = (lo1 - lo0) * 111.19 * math.cos(math.radians((la0 + la1) / 2.0))
        dlat = (la1 - la0) * 111.19
        return math.hypot(dlon, dlat)

    diag_all = _diag_km(lon_min, lon_max, lat_min, lat_max)
    ctr_lon, ctr_lat = (lon_min + lon_max) / 2.0, (lat_min + lat_max) / 2.0
    range_all = int(max(300_000.0, 0.5 * diag_all * 1000.0))
    last_lon, last_lat = float(lon[-1]), float(lat[-1])
    range_orbit = int(max(800_000.0, 0.7 * _diag_km(SITE["lon"], last_lon, SITE["lat"], last_lat) * 1000.0))

    # ---- Document 描述 ----
    d = []
    d.append("<b>%s</b>" % mission_name)
    d.append("火箭：%s；发射场：%s（%.6f°E, %.6f°N，高程 %.0f m）"
             % (rocket or "—", SITE["name"], SITE["lon"], SITE["lat"], SITE["alt"]))
    d.append("起飞时刻：%s UTC（北京时 %s）" % (utc(T0), bj(T0)))
    d.append("弹道数据：%d 个采样点（≈%.1f s），T+0.0 s ~ T+%.1f s" % (len(t), dt_med, t[-1]))
    d.append("阶段配色：" + "；".join(
        '<span style="color:%s">■</span> %s（宽线）' % (kml_color_to_hex(s["color"]), s["label"])
        for s in STAGE_STYLE))
    d.append("星下点轨迹：洋红线（clampToGround）；时间动画：gx:Track 1 s 采样，可用时间滑块播放")
    if ZONES:
        zone_parts = []
        for zid, z in ZONES.items():
            imps = [r for r in imp if str(r.get("zone_id") or "").strip() == zid]
            extra = ""
            if imps:
                try:
                    extra = "，" + "、".join("%.1f km" % float(r["great_circle_km"]) for r in imps)
                except (KeyError, TypeError, ValueError):
                    extra = ""
            zone_parts.append("<span style='color:#ff1e1e'>■</span> %s（%s%s）"
                              % (zid, (z["hint"] + "落区") if z["hint"] else "危险区", extra))
        qr_txt = ""
        radii = ["%d NM" % z["qr"] for z in ZONES.values() if z.get("qr")]
        if radii:
            qr_txt = "；青色圆 = Q 行圆（%s）" % " / ".join(radii)
        d.append("落区：" + "；".join(zone_parts) + qr_txt)
    else:
        d.append("落区：无航警区数据（config/notam_zones 均为空）——仅绘制弹道、事件与预测残骸落点。")
    d.append("落点：绿色 = 残骸落点在对应危险区内（说明中含距边界距离与 downrange/crossrange）")
    ins = (orb or {}).get("insertion") or {}
    if ins:
        d.append("入轨：T+%.1f s，高度 %.1f km，速度 %.1f m/s，半长轴 %.1f km，e=%.5f，i=%.2f°"
                 % (float(ins.get("t_met_s", 0.0)), float(ins.get("alt_km", 0.0)),
                    float(ins.get("speed_m_s", 0.0)), float((orb or {}).get("a_km", 0.0)),
                    float((orb or {}).get("e", 0.0)), float((orb or {}).get("i_deg", 0.0))))
    d.append("<b>说明：%s</b>" % (mission.get("disclaimer") or "仅供公开信息分析与教学使用；非实际发射弹道。"))
    d.append("<b>航警信息</b>")
    if ZONES:
        for zid, z in ZONES.items():
            b_txt = utc(z["b"]) if z["b"] else "—"
            c_txt = utc(z["c"]) if z["c"] else "—"
            d.append("%s：B) %s C) %s；%s；对应本体：%s"
                     % (zid, b_txt, c_txt, z["qline"] or "—", z["body"]))
    else:
        d.append("（无航警）本任务未提供 NOTAM，落区为弹道外推预测，仅作示意。")
    d.append("模型：3-DOF 质点、ECI+J2、USSA76、开环俯仰程序、方位角恒定（射面）导引；"
             "残骸落点为无动力弹道 + 阻力给出参考落点（详见技术总报告）。")
    doc = KMLWriter(title, "<br/>".join(d))
    doc.begin(lookat=(ctr_lon, ctr_lat, range_all, 30))

    # ---------------- 1. 弹道（三维 + 星下点） ----------------
    doc.folder("弹道（三维 + 星下点）", None, opened=True)
    doc.point("发射工位（起飞点）", SITE["lon"], SITE["lat"], SITE["alt"], style="launch",
              desc="<b>发射工位：%s</b><br/>坐标：%.6f°E, %.6f°N，高程 %.0f m<br/>"
                   "起飞：%s UTC（%s 北京时）"
                   % (SITE["name"], SITE["lon"], SITE["lat"], SITE["alt"], utc(T0), bj(T0)))
    for seg in STAGE_STYLE:
        a, b = seg["a"], seg["b"]
        m = (t >= a) & (t <= b)
        if m.sum() < 2:
            continue
        alt_km = (alt[m].min() / 1e3, alt[m].max() / 1e3)
        doc.line("%s（T+%.1f ~ %.1f s）" % (seg["label"], a, b),
                 lon[m], lat[m], alt[m], seg["sid"],
                 desc="飞行阶段：%s<br/>T+%.1f ~ T+%.1f s<br/>高度 %.1f ~ %.1f km"
                      % (seg["label"], a, b, alt_km[0], alt_km[1]))
    doc.line("星下点轨迹（0 – %.0f s）" % t[-1], lon, lat, np.zeros_like(alt), "groundTrack",
             desc="上升段星下点轨迹（clampToGround）", mode="clampToGround")
    doc.endfolder()

    # ---------------- 2. 飞行事件 ----------------
    doc.folder("飞行事件（%d）" % len(ev), None, opened=True)
    for r in ev:
        try:
            tt = float(r["t_met_s"])
            la, lo, al = float(r["lat_deg"]), float(r["lon_deg"]), float(r["alt_m"])
        except (KeyError, TypeError, ValueError):
            continue
        te = T0 + timedelta(seconds=tt)
        name = str(r.get("event") or "")
        key = any(k in name for k in ("起飞", "分离", "SECO", "点火"))
        style = "launch" if "起飞" in name else ("evKey" if key else "ev")
        try:
            spd = float(r["speed_m_s"]) / 1e3
            mas = float(r["mass_kg"]) / 1e3
        except (KeyError, TypeError, ValueError):
            spd = float("nan")
            mas = float("nan")
        desc = ("T+%.1f s（%s UTC / %s 北京时）<br/>高度 %.2f km<br/>速度 %.3f km/s<br/>"
                "质量 %.2f t<br/>备注：%s"
                % (tt, utc(te), bj(te), al / 1e3, spd, mas, r.get("note", "")))
        doc.point(name, lo, la, al, style=style, desc=desc)
    doc.endfolder()

    # ---------------- 3. 落区与残骸落点 ----------------
    doc.folder("落区与残骸落点（%d 处危险区）" % len(ZONES), None, opened=True)
    for i, (zid, z) in enumerate(ZONES.items()):
        if z["has_poly"]:
            lats = [p[0] for p in z["poly"]]
            lons = [p[1] for p in z["poly"]]
            try:
                area = float(N.polygon_area_km2(np.array(z["poly"])))
            except Exception:  # noqa: BLE001
                area = 0.0
            verts = "".join("P%d：%.6f°E, %.6f°N<br/>" % (k + 1, p[1], p[0])
                            for k, p in enumerate(z["poly"]))
            b_j = bj(z["b"]) if z["b"] else "—"
            c_j = bj(z["c"]) if z["c"] else "—"
            desc = ("<b>NOTAM %s（%s）</b><br/>B) %s C) %s（= %s UTC — %s UTC，%s — %s 北京时）<br/>"
                    "%s<br/>对应本体：%s<br/>%d 个顶点；面积约 %.1f km²<br/>顶点：<br/>%s"
                    % (zid, z["fix"] or "—", z["b_raw"] or "—", z["c_raw"] or "—",
                       utc(z["b"]) if z["b"] else "—", utc(z["c"]) if z["c"] else "—",
                       b_j, c_j, z["qline"] or "—", z["body"],
                       len(z["poly"]), area, verts))
            doc.polygon("NOTAM %s（%s）" % (zid, (z["hint"] + "落区") if z["hint"] else "危险区"),
                        lats, lons,
                        "danger" if i == 0 else "danger2", desc=desc)
        if z["qc"]:
            doc.point("危险区中心（标注）%s" % zid, z["qc"][1], z["qc"][0], 0, style="label",
                      clamp=True, desc="%s 中心：%.4f°E, %.4f°N"
                                       % (zid, z["qc"][1], z["qc"][0]))
        if z["qc"] and z["qr"]:
            doc.circle("Q 行 %d NM 圆形区域（%s）" % (z["qr"], zid),
                       z["qc"][0], z["qc"][1], z["qr"], "qcircle",
                       desc="Q 行圆形区域（航警原文）<br/>圆心：%.6f°E, %.6f°N<br/>"
                            "半径：%d NM = %.2f km"
                            % (z["qc"][1], z["qc"][0], z["qr"], z["qr"] * 1.852))
    if not ZONES:
        _plon, _plat = (float(imp[0]["lon_deg"]), float(imp[0]["lat_deg"])) if imp else (last_lon, last_lat)
        doc.point("预测落区（无航警注记）", _plon, _plat, 0.0, style="label", clamp=True,
                  desc="本任务未提供 NOTAM 航警区：图中仅显示弹道/事件与外推的预测残骸落点，"
                       "不绘制危险区多边形。")
    for r in imp:
        zid = str(r.get("zone_id") or "").strip()
        nzid = str(r.get("nearest_zone_id") or "").strip()
        obj = str(r.get("object") or "").strip()
        inside = str(r.get("inside_zone") or "").strip().lower() == "true"
        nm = DEBRIS_BY_OBJ[obj]["name_cn"] if obj in DEBRIS_BY_OBJ else (obj or "残骸")
        try:
            la, lo = float(r["lat_deg"]), float(r["lon_deg"])
        except (KeyError, TypeError, ValueError):
            continue
        # v3.2：区内/区外/无区分别给距离与图标
        if inside:
            try:
                dist_txt = "%.2f km（区内，距边界）" % float(r["dist_to_boundary_km"])
            except (KeyError, TypeError, ValueError):
                dist_txt = "—（区内）"
        else:
            try:
                dist_txt = "%.2f km（区外）" % float(r["nearest_dist_km"])
            except (KeyError, TypeError, ValueError):
                dist_txt = "—（无对应航警区）"
        imp_t = parse_utc_aware(r.get("impact_t_utc"))
        imp_bj = bj(imp_t) if imp_t else "—"
        vals = {}
        for k, key in (("gc", "great_circle_km"), ("dr", "downrange_km"), ("cr", "crossrange_km")):
            try:
                vals[k] = "%.1f" % float(r[key])
            except (KeyError, TypeError, ValueError):
                vals[k] = "—"
        desc = ("残骸：%s<br/>落点时刻：%s UTC（%s 北京时）<br/>落点：%.6f°E, %.6f°N<br/>"
                "大圆距离：%s km；downrange：%s km；crossrange：%s km<br/>"
                "是否在 %s 危险区内：%s<br/>距危险区多边形边界：%s<br/>备注：%s"
                % (nm, r.get("impact_t_utc") or "—", imp_bj, lo, la,
                   vals["gc"], vals["dr"], vals["cr"], zid or "（无）",
                   "是（区内）" if inside else "否（区外）", dist_txt, r.get("note", "")))
        _assoc = zid or nzid
        if inside:
            zone_txt = "（%s 区内）" % zid
            _style = "impactIn"
        elif _assoc:
            zone_txt = "（%s 区外，距边界 %s）" % (_assoc, dist_txt.split("（")[0])
            _style = "impactOut"
        else:
            zone_txt = "（无对应航警区）"
            _style = "impactOther"
        doc.point("%s%s" % (nm + "残骸落点", zone_txt),
                  lo, la, 0.0, style=_style, clamp=True, desc=desc)
    doc.endfolder()

    # ---------------- 4. 时间动画 ----------------
    tt = np.arange(0.0, t[-1] + 1e-9, 1.0)
    la1 = np.interp(tt, t, lat)
    lo1 = np.interp(tt, t, lon)
    al1 = np.interp(tt, t, alt)
    doc.folder("时间动画（gx:Track，%d 点）" % len(tt), None, opened=True)
    doc.track("弹道时间动画（可用时间滑块播放）",
              [T0 + timedelta(seconds=float(s)) for s in tt],
              list(zip(lo1, la1, al1)),
              desc="上升段 1 s/点，T+0 ~ %.0f s；拖动时间滑块或点播放键" % t[-1])
    doc.endfolder()

    # ---------------- 5. 预设视角与 Tour ----------------
    doc.folder("预设视角与 Tour", None, opened=True)
    doc.lookat("全区段视角（双击飞入）", ctr_lon, ctr_lat, range_all, 30,
               desc="双击本条目：相机覆盖发射场→上升段→落区的全区域。")
    if ZONES:
        zxs = [p[1] for z in ZONES.values() for p in z["poly"]]
        zys = [p[0] for z in ZONES.values() for p in z["poly"]]
        if zxs and zys:
            zc_lon, zc_lat = (min(zxs) + max(zxs)) / 2.0, (min(zys) + max(zys)) / 2.0
            z_range = int(max(250_000.0, _diag_km(min(zxs), max(zxs), min(zys), max(zys)) * 1000.0))
            doc.lookat("落区细节视角（双击飞入）", zc_lon, zc_lat, z_range, 35,
                       desc="双击本条目：相机飞至危险区上空（视距约 %d km）。" % (z_range // 1000))
    doc.lookat("入轨段视角（双击飞入）", last_lon, last_lat, range_orbit, 25,
               desc="双击本条目：相机覆盖上升段末段与入轨点（%.1f°N, %.1f°E）。"
                    % (last_lat, last_lon))
    frames = []
    for frac in np.linspace(0.0, 1.0, 12):
        idx = int(np.argmin(np.abs(t - frac * t[-1])))
        rng = 60_000 if frac < 0.05 else (200_000 + 1_300_000 * frac)
        frames.append((float(lon[idx]), float(lat[idx]), 50, int(rng)))
    doc.tour("弹道飞行演示（双击播放）", frames,
             desc="相机沿弹道由发射场上空逐渐拉远跟随至入轨段；双击 Folder 内条目播放。")
    doc.endfolder()

    # ---------------- 6. 残骸坠落曲线 ----------------
    objs = {}
    for r in deb:
        objs.setdefault(str(r.get("object") or ""), []).append(r)
    sep_lines = []
    for obj, rows in objs.items():
        nm = DEBRIS_BY_OBJ[obj]["name_cn"] if obj in DEBRIS_BY_OBJ else obj
        try:
            sep_lines.append("%s：T+%.1f s 分离" % (nm, float(rows[0]["t_met_s"])))
        except (KeyError, TypeError, ValueError):
            sep_lines.append("%s：分离时刻未知" % nm)
    doc.folder("残骸坠落曲线（分离后无动力弹道）",
               "<b>残骸分离后的无动力坠落曲线</b><br/>" + "；".join(sep_lines) + "<br/>"
               "曲线为 3D 绝对高度；细线为地面投影；每个子文件夹含分离点、坠落曲线与 1 s "
               "时间动画（gx:Track，可与上升段动画同时播放）。<br/>"
               "说明：仅供公开信息分析与教学使用。", opened=True)
    for obj, rows in objs.items():
        ds = DEBRIS_BY_OBJ.get(obj) or dict(sid=_safe_id(obj), col="ffffffff",
                                            gcol="80ffffff", name_cn=obj)
        ts = np.array([float(r["t_met_s"]) for r in rows])
        las = np.array([float(r["lat_deg"]) for r in rows])
        los = np.array([float(r["lon_deg"]) for r in rows])
        als = np.array([float(r["alt_m"]) for r in rows])
        t_sep, t_imp = float(ts[0]), float(ts[-1])
        try:
            spd0 = float(rows[0]["speed_m_s"]) / 1e3
        except (KeyError, TypeError, ValueError):
            spd0 = float("nan")
        doc.folder("%s：T+%.1f s 分离 → T+%.1f s 落地（坠落 %.1f s）"
                   % (ds["name_cn"], t_sep, t_imp, t_imp - t_sep), None, opened=True)
        doc.point("%s分离点（T+%.1f s，%.1f km，%.2f km/s）"
                  % (ds["name_cn"], t_sep, als[0] / 1e3, spd0),
                  los[0], las[0], als[0], style="ev")
        doc.line("%s坠落曲线（3D，%d 点）" % (ds["name_cn"], len(ts)), los, las, als, ds["sid"])
        doc.line("%s地面投影" % ds["name_cn"], los, las, np.zeros_like(als),
                 ds["sid"] + "_ground", mode="clampToGround")
        doc.track("%s坠落时间动画（1 s）" % ds["name_cn"],
                  [T0 + timedelta(seconds=float(s)) for s in np.arange(t_sep, t_imp + 1e-9, 1.0)],
                  [(float(np.interp(s, ts, los)), float(np.interp(s, ts, las)),
                    float(np.interp(s, ts, als))) for s in np.arange(t_sep, t_imp + 1e-9, 1.0)],
                  vis=False)
        doc.endfolder()
    doc.endfolder()

    # v3.5：CZ-2F 任务前后各 30 min 的空间站轨道（如已由 rendezvous 生成）
    _ctx = os.path.join(ROOT, "sim", "CZ2F_任务前后30min_空间站.csv")
    if os.path.exists(_ctx):
        try:
            _rows = list(csv.DictReader(open(_ctx, encoding="utf-8-sig")))
            if len(_rows) >= 2:
                _lo = np.array([float(r["lon_deg"]) for r in _rows])
                _la = np.array([float(r["lat_deg"]) for r in _rows])
                _al = np.array([float(r["alt_km"]) for r in _rows]) * 1000.0
                doc.folder("任务前后 30 min 上下文", None, opened=False)
                doc.line("空间站（任务前后各 30 min，%d 点）" % len(_rows),
                         _lo, _la, _al, "stationCtx",
                         desc="CZ-2F 任务 T0−30min ~ 对接+30min 的空间站轨道")
                doc.endfolder()
        except Exception as exc:  # noqa: BLE001
            print("[kml] 警告: 叠加任务前后30min 空间站轨道失败：%s" % exc)

    # v4.1：交会对接位置（入轨点 / 对接点 / 事件连线）——载人任务
    _rdv = os.path.join(ROOT, "sim", "交会对接.json")
    if os.path.exists(_rdv):
        try:
            _rd = json.load(open(_rdv, encoding="utf-8-sig"))
            _dk = _rd.get("docking") or {}
            _ins = _rd.get("insertion") or {}
            _tl = _rd.get("timeline") or []
            if _dk.get("station_lat_deg") is not None:
                doc.folder("交会对接（入轨 → 相位 → 转移 → 对接）",
                           "对接时刻 %s UTC；对接点星下点 %.3f°N / %.3f°E / %.1f km；"
                           "总 ΔV %.0f m/s。位置来自共面相位模型 + J2 递推，仅供分析。"
                           % (_dk.get("t_utc", "—"),
                              float(_dk["station_lat_deg"]),
                              float(_dk["station_lon_deg"]),
                              float(_dk.get("alt_km") or 0.0),
                              float(_rd.get("total_dv_mps") or 0.0)),
                           opened=True)
                if _ins.get("lat_deg") is not None:
                    doc.point("入轨点（T+%.0f s，%.1f km）"
                              % (float(_ins.get("t_met_s") or 0.0),
                                 float(_ins.get("alt_km") or 0.0)),
                              float(_ins["lon_deg"]), float(_ins["lat_deg"]),
                              float(_ins.get("alt_km") or 0.0) * 1000.0, style="ev",
                              desc="入轨时刻 %s UTC；%s"
                                   % (_ins.get("t_utc", "—"),
                                      _ins.get("plane_note", "")))
                doc.point("对接点（%s UTC，%.1f km）"
                          % (_dk.get("t_utc", "—"),
                             float(_dk.get("alt_km") or 0.0)),
                          float(_dk["station_lon_deg"]), float(_dk["station_lat_deg"]),
                          float(_dk.get("alt_km") or 0.0) * 1000.0, style="ev",
                          desc="<b>对接位置</b><br/>星下点 %.3f°N / %.3f°E<br/>"
                               "高度 %.1f km；制动前相对速度 %.2f m/s<br/>"
                               "总 ΔV %.0f m/s"
                               % (float(_dk["station_lat_deg"]),
                                  float(_dk["station_lon_deg"]),
                                  float(_dk.get("alt_km") or 0.0),
                                  float(_dk.get("rel_vel_before_dock_mps") or 0.0),
                                  float(_rd.get("total_dv_mps") or 0.0)))
                # ---- v4.2：发射→入轨→相位→转移→对接 全流程轨迹 ----
                _flow_t, _flow_lon, _flow_lat, _flow_alt = [], [], [], []
                _asc = os.path.join(ROOT, "sim", "trajectory.csv")
                if os.path.exists(_asc):
                    try:
                        for _r in csv.DictReader(open(_asc, encoding="utf-8-sig")):
                            try:
                                _flow_t.append(datetime.fromisoformat(
                                    str(_r["t_utc"]).replace("Z", "+00:00")))
                                _flow_lat.append(float(_r["lat_deg"]))
                                _flow_lon.append(float(_r["lon_deg"]))
                                _flow_alt.append(float(_r["alt_m"]))
                            except (KeyError, TypeError, ValueError):
                                continue
                    except Exception as _exc:  # noqa: BLE001
                        print("[kml] 警告: 读取上升段轨迹失败：%s" % _exc)
                _rdvt = os.path.join(ROOT, "sim", "交会轨迹.csv")
                if os.path.exists(_rdvt):
                    try:
                        for _r in csv.DictReader(open(_rdvt, encoding="utf-8-sig")):
                            try:
                                _t = datetime.fromisoformat(
                                    str(_r["t_utc"]).replace("Z", "+00:00"))
                                if _flow_t and _t <= _flow_t[-1]:
                                    continue
                                _flow_t.append(_t)
                                _flow_lat.append(float(_r["lat_deg"]))
                                _flow_lon.append(float(_r["lon_deg"]))
                                _flow_alt.append(float(_r["alt_km"]) * 1000.0)
                            except (KeyError, TypeError, ValueError):
                                continue
                    except Exception as _exc:  # noqa: BLE001
                        print("[kml] 警告: 读取交会轨迹失败：%s" % _exc)
                if len(_flow_t) >= 2:
                    doc.line("全流程轨迹（发射 → 入轨 → 相位 → 转移 → 对接，%d 点）"
                             % len(_flow_t),
                             _flow_lon, _flow_lat, _flow_alt, "rdvFlow",
                             desc="<b>完整任务轨迹</b><br/>发射→入轨段来自 sim/trajectory.csv，"
                                  "入轨→对接段来自 sim/交会轨迹.csv（30 s 采样）；"
                                  "时间动画为 60 s 抽样。")
                    doc.line("全流程地面投影", _flow_lon, _flow_lat,
                             [0.0] * len(_flow_lon), "rdvFlowGround",
                             mode="clampToGround",
                             desc="全流程星下点（clampToGround）")
                    # 时间动画：60 s 抽样（上升段加密到 10 s）
                    _at, _ac, _last = [], [], None
                    for _t, _lo, _la, _al in zip(_flow_t, _flow_lon, _flow_lat, _flow_alt):
                        _step = 10.0 if (_t - _flow_t[0]).total_seconds() < 700 else 60.0
                        if _last is None or (_t - _last).total_seconds() >= _step:
                            _at.append(_t)
                            _ac.append([_lo, _la, _al])
                            _last = _t
                    if len(_at) >= 2:
                        doc.track("全流程时间动画（%d 帧）" % len(_at),
                                  _at, _ac, style="rdvAnim",
                                  desc="发射 → 对接全过程（60 s 抽样，上升段 10 s）")
                # 事件点保留（细绿线，供核对）
                _lons, _lats, _alts = [], [], []
                for _r in _tl:
                    if _r.get("lat_deg") is None or _r.get("lon_deg") is None:
                        continue
                    _lons.append(float(_r["lon_deg"]))
                    _lats.append(float(_r["lat_deg"]))
                    _alts.append(float(_r.get("alt_km") or 0.0) * 1000.0)
                if len(_lons) >= 2:
                    doc.line("关键事件点（%d 个，供核对）" % len(_lons),
                             _lons, _lats, _alts, "stationCtx",
                             desc="入轨→相位→转移→对接事件点；完整轨迹见上一条")
                doc.endfolder()
        except Exception as exc:  # noqa: BLE001
            print("[kml] 警告: 叠加交会对接位置失败：%s" % exc)

    kml = doc.end()
    kml_path = os.path.join(out_dir, "弹道三维_含残骸坠落.kml")
    with open(kml_path, "w", encoding="utf-8") as f:
        f.write(kml)
    print("KML:", kml_path, len(kml.encode("utf-8")), "bytes")

    # ---- KMZ（图标取 <RK_ENGINE>/viz/files） ----
    kmz_path = os.path.join(out_dir, "弹道三维_含残骸坠落.kmz")
    icons_dir = os.path.join(ENGINE, "viz", "files")
    pngs = []
    if os.path.isdir(icons_dir):
        pngs = sorted(f for f in os.listdir(icons_dir) if f.lower().endswith(".png"))
    else:
        print("[kml] 警告: 未找到图标目录 %s（KMZ 将不含图标）" % icons_dir)
    with zipfile.ZipFile(kmz_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", kml.encode("utf-8"))
        for f in pngs:
            z.write(os.path.join(icons_dir, f), "files/" + f)
    print("KMZ:", kmz_path, os.path.getsize(kmz_path), "bytes")

    # ---- 校验 ----
    import xml.etree.ElementTree as ET
    NS = {"kml": "http://www.opengis.net/kml/2.2", "gx": "http://www.google.com/kml/ext/2.2"}
    errors, details = [], []
    try:
        tree = ET.parse(kml_path)
        root = tree.getroot()
    except Exception as exc:  # noqa: BLE001
        print("[kml] 错误: KML XML 解析失败: %s" % exc)
        return 1
    place = root.findall(".//kml:Placemark", NS)
    tracks = root.findall(".//gx:Track", NS)
    ok = True
    for trk in tracks:
        w = trk.findall("kml:when", NS)
        c = trk.findall("gx:coord", NS)
        if len(w) != len(c):
            ok = False
            details.append("Track when/coord 数量不等: %d/%d" % (len(w), len(c)))
            errors.append(details[-1])
    # 落点是否在对应危险区内（用 shapely-free 球面射线法复核；无对应区的落点跳过）
    # v3.2：对所有"能关联到航警区"的落点复核（含区外：用最近区），不再只查 zone_id 非空的
    zone_checks = []
    for r in imp:
        zid = str(r.get("zone_id") or "").strip()
        nzid = str(r.get("nearest_zone_id") or "").strip()
        z = ZONES.get(zid or nzid)
        if not z or not z["has_poly"]:
            continue
        try:
            inside = N.point_in_polygon(float(r["lat_deg"]), float(r["lon_deg"]),
                                        np.array([[p[0], p[1]] for p in z["poly"]]))
        except Exception as exc:  # noqa: BLE001
            errors.append("落点 %s 区间判定失败: %s" % (r.get("object"), exc))
            continue
        zone_checks.append(bool(inside))
    zone_ok = all(zone_checks) if zone_checks else True
    zone_warnings = []
    if zone_checks and not zone_ok:
        # v3.2：落点在区外属"数据/模型不匹配"，由 qc 与报告判定；KML 文件本身是正确产物，
        # 这里只记为告警并写入校验 JSON，不让一次数据问题把 KML 步骤也判失败。
        zone_warnings.append("存在落在危险区外的残骸落点（%d/%d 区外）"
                             % (sum(1 for x in zone_checks if not x), len(zone_checks)))
    with zipfile.ZipFile(kmz_path) as z:
        names = z.namelist()
        kmz_ok = ("doc.kml" in names and z.testzip() is None
                  and sum(1 for n in names if n.startswith("files/")) == len(pngs))
    if not kmz_ok:
        errors.append("KMZ 自包含检查失败")
    report = dict(
        generated_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        style="参考用户提供 CZ-6A 版《弹道三维_含残骸坠落.kmz》样式重制（v3 参数化）",
        mission=dict(name=mission_name, rocket=rocket, t0_utc=utc(T0),
                     launch_site=SITE, notams=notam_ids),
        source_data=dict(trajectory="sim/trajectory.csv", debris="sim/debris_trajectories.csv",
                         events="sim/弹道事件表.csv", impacts="sim/落点与射程.csv",
                         notam=("sim/notam_zones.json" if ZONES and
                                next(iter(ZONES.values()))["source"].startswith("sim/")
                                else "config.json → notams"),
                         objects="sim/objects.json", trajectory_meta="sim/trajectory_meta.json"),
        stages=[dict(sid=s["sid"], label=s["label"], color=kml_color_to_hex(s["color"]),
                     t_start_s=s["a"], t_end_s=s["b"]) for s in STAGE_STYLE],
        debris_styles=[dict(obj=d_["obj"], name_cn=d_["name_cn"], style_id=d_["sid"],
                            color=kml_color_to_hex(d_["col"])) for d_ in DEBRIS_STYLES],
        notam_zones=[dict(id=z["id"], name=z["name"], polygons=len(z["poly"]),
                          q_center=z["qc"], q_radius_nm=z["qr"],
                          b_utc=utc(z["b"]) if z["b"] else None,
                          c_utc=utc(z["c"]) if z["c"] else None)
                     for z in ZONES.values()],
        kml=dict(path="viz/弹道三维_含残骸坠落.kml", bytes=os.path.getsize(kml_path),
                 document=title,
                 folders=len(root.findall(".//kml:Folder", NS)),
                 placemarks=len(place),
                 line_strings=len(root.findall(".//kml:LineString", NS)),
                 polygons=len(root.findall(".//kml:Polygon", NS)),
                 tracks=len(tracks),
                 track_points_total=sum(len(trk.findall("kml:when", NS)) for trk in tracks),
                 tours=len(root.findall(".//gx:Tour", NS)),
                 styles=len(root.findall(".//kml:Style", NS))),
        kmz=dict(path="viz/弹道三维_含残骸坠落.kmz", bytes=os.path.getsize(kmz_path),
                 members=len(names), doc_kml=True, icons=len(pngs), crc_ok=kmz_ok),
        checks=dict(xml_parse=True, track_pairs_ok=ok, track_details=details,
                    impacts_in_zones=bool(zone_ok), kmz_selfcontained=kmz_ok,
                    n_impacts_checked=len(zone_checks),
                    n_impacts_outside=sum(1 for x in zone_checks if not x),
                    all_ok=bool(ok and zone_ok and kmz_ok and not errors)),
        errors=errors,
        warnings=(([] if pngs else ["缺少 engine/viz/files 图标目录"]) + zone_warnings),
    )
    json.dump(report, open(os.path.join(out_dir, "校验_KML.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(json.dumps(report["kml"], ensure_ascii=False, indent=1))
    print(json.dumps(report["checks"], ensure_ascii=False, indent=1))
    if errors:
        print("[kml] 校验发现问题: %s" % "；".join(errors))
        return 1
    if zone_warnings:
        print("[kml] 注意（不影响 KML 文件本身，落区问题由交付自检判定）: %s"
              % "；".join(zone_warnings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
