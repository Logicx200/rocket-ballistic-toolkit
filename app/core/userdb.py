# -*- coding: utf-8 -*-
"""用户自定义库：自定义箭型 / 参考轨道 / 发射时序模板。

为什么要单独一层
----------------
v3 的自定义箭型必须手工编辑 ``app/data/rockets.json``（一个 130 KB、28 条记录的内置
数据库），字段多、格式严格，门槛极高；自定义参考轨道也只能用内置 15 条模板。v4 把
**内置库（只读）** 与 **用户库（可读写）** 分开：

* 内置库 ``app/data/*.json``：随程序升级，不被用户修改；
* 用户库 ``runtime_base()/user_data/*.json``：由 GUI 编辑器维护，可导入/导出；
* 读取时合并：用户条目优先；同 id 时用户条目覆盖内置并标注 ``_shadow_builtin``。

本模块只依赖标准库与 ``app.core.paths``，不导入 numpy / 引擎 / GUI，因此可被
``infer``、``config_build``、GUI 与测试同时使用。

数据结构（用户在界面上填写后落盘）
----------------------------------
``user_data/rockets.json``::

    {"schema": "rocketkit.user_db.v1",
     "rockets": [ { ...与内置箭型同构，另加 class_key / _user / _updated_at... } ]}

``user_data/orbits.json``::

    {"schema": "rocketkit.user_db.v1",
     "orbits": [ {"id","name","alt_km","i_deg","e","raan_deg","argp_deg",
                  "ltan_h","epoch_utc","type","confidence","note"} ]}

``user_data/sequences.json``::

    {"schema": "rocketkit.user_db.v1",
     "templates": [ {"id","name","rocket_id","values":{...}} ]}

所有写入都走 :func:`_write_json`（UTF-8 无 BOM、原子替换），读取容忍 BOM 与坏文件。
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import paths as P

G0 = 9.80665

#: 箭型类别（决定缺字段时使用的量级锚点；v3 只用 class 中文串，实际从未命中）
CLASS_KEYS = ("small_solid", "medium_liquid", "large_liquid", "crewed")
CLASS_KEY_CN = {
    "small_solid": "小型固体",
    "medium_liquid": "中型液体",
    "large_liquid": "大型液体",
    "crewed": "载人火箭",
}

#: 可编辑字段的取值域（GUI 用作 Spinbox 上下限，校验时也用它）
FIELD_LIMITS = {
    "dry_kg": (0.0, 2.0e6), "prop_kg": (0.0, 1.0e7), "thrust_sl_n": (0.0, 5.0e7),
    "thrust_vac_n": (0.0, 5.0e7), "isp_sl_s": (100.0, 500.0), "isp_vac_s": (100.0, 550.0),
    "pitch_end_deg": (-90.0, 90.0), "cda_attached_m2": (0.0, 200.0),
    "pitch_duration_s": (1.0, 3000.0), "seco_met": (1.0, 20000.0), "sep_met": (1.0, 20000.0),
    "cda_m2": (0.0, 200.0), "mass_kg": (0.0, 1.0e6), "count": (1, 8),
    "prop_kg_each": (0.0, 2.0e6), "jettison_met": (1.0, 20000.0),
    "t_v": (0.5, 60.0), "t_p": (5.0, 1200.0), "pitch_power": (0.2, 3.0),
    "t_fairing": (1.0, 20000.0), "fairing_divert_mps": (0.0, 50.0),
    "yaw_tau": (10.0, 2000.0), "yaw2_tau": (10.0, 2000.0),
    "alt_km": (80.0, 200000.0), "i_deg": (0.0, 180.0), "e": (0.0, 0.9),
    "raan_deg": (0.0, 360.0), "argp_deg": (0.0, 360.0), "ltan_h": (0.0, 24.0),
    "liftoff_mass_kg": (100.0, 1.0e7), "leo_kg": (0.0, 1.0e6),
    "sso_kg": (0.0, 1.0e6), "default_payload_kg": (0.0, 1.0e6),
    "adapter_kg": (0.0, 1.0e5), "ascent_cda_scale": (0.1, 5.0),
    "seco_band_km": (0.0, 2000.0), "seco_e_max": (0.0, 0.5),
    "end_met": (0.1, 20000.0), "n": (1, 20),
}

#: 校验严重级别
ERROR, WARN, INFO = "错误", "提示", "提示"


# --------------------------------------------------------------------------- #
# 基础 IO
# --------------------------------------------------------------------------- #
def user_dir() -> Path:
    """用户库目录（可写；首次调用自动创建）。"""
    p = P.runtime_base() / "user_data"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return p


def _db_path(name: str) -> Path:
    return user_dir() / (name if name.endswith(".json") else name + ".json")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json_tolerant(path: Path) -> Dict[str, Any]:
    """容忍 BOM / 注释尾逗号 / 坏文件的 JSON 读取（坏文件改名备份后返回空）。"""
    try:
        if not path.exists():
            return {}
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        if not text.strip():
            return {}
        return json.loads(text)
    except Exception:  # noqa: BLE001
        try:
            bad = path.with_suffix(path.suffix + ".bad-%s" % time.strftime("%Y%m%d_%H%M%S"))
            path.replace(bad)
        except Exception:  # noqa: BLE001
            pass
        return {}


def _write_json(path: Path, data: Any) -> None:
    """UTF-8 无 BOM + 原子替换写盘（避免半截文件）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# id / 名称
# --------------------------------------------------------------------------- #
def slugify(text: str, prefix: str = "user") -> str:
    """把中文名转成一个稳定的 id（保留 ASCII，其余用短哈希兜底）。"""
    s = str(text or "").strip().lower()
    ascii_part = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    h = hashlib.sha1(str(text or "").encode("utf-8")).hexdigest()[:6]
    if ascii_part:
        return "%s_%s" % (prefix, ascii_part[:32])
    return "%s_%s" % (prefix, h)


def _ensure_unique(items: List[Dict[str, Any]], rid: str,
                   aliases: Sequence[str] = ()) -> str:
    """保证 id 在列表内唯一（重复时加 _2/_3）。"""
    used = {str(it.get("id") or "") for it in items}
    used |= {str(a) for a in aliases if a}
    if rid not in used:
        return rid
    for i in range(2, 1000):
        cand = "%s_%d" % (rid, i)
        if cand not in used:
            return cand
    return "%s_%d" % (rid, int(time.time()))


# --------------------------------------------------------------------------- #
# 箭型：模板 / 归一化 / 校验 / 指标
# --------------------------------------------------------------------------- #
def class_key_of(rocket: Dict[str, Any]) -> str:
    """把 ``class`` 中文串/``class_key`` 归一成四类之一（供量级锚点使用）。"""
    if not isinstance(rocket, dict):
        return "medium_liquid"
    ck = str(rocket.get("class_key") or "").strip()
    if ck in CLASS_KEYS:
        return ck
    cls = str(rocket.get("class") or "")
    if "载人" in cls or "神舟" in cls:
        return "crewed"
    if "固体" in cls:
        return "small_solid"
    if "大型" in cls or "重型" in cls:
        return "large_liquid"
    mass = rocket.get("liftoff_mass_kg")
    try:
        m = float(mass)
        if m < 80000:
            return "small_solid"
        if m > 600000:
            return "large_liquid"
    except (TypeError, ValueError):
        pass
    return "medium_liquid"


#: 新建自定义箭型时，按类别默认克隆哪个内置箭型（数值已由历史任务率定，比手写模板可用得多）
TEMPLATE_SOURCE = {
    "small_solid": "cz11",
    "medium_liquid": "cz2d",
    "large_liquid": "cz5",
    "crewed": "cz2f",
}

_BUILTIN_CACHE: Optional[List[Dict[str, Any]]] = None


def builtin_rockets() -> List[Dict[str, Any]]:
    """只读内置箭型库（不经过 infer，避免循环导入）。"""
    global _BUILTIN_CACHE
    if _BUILTIN_CACHE is not None:
        return _BUILTIN_CACHE
    items: List[Dict[str, Any]] = []
    try:
        p = P.app_data_dir() / "rockets.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8-sig"))
            items = [x for x in (data.get("rockets") or []) if isinstance(x, dict)]
    except Exception:  # noqa: BLE001
        items = []
    _BUILTIN_CACHE = items
    return items


def get_builtin_rocket(rid: str) -> Optional[Dict[str, Any]]:
    for r in builtin_rockets():
        if str(r.get("id")) == str(rid):
            return copy.deepcopy(r)
    return None


def blank_rocket(name: str = "我的火箭", class_key: str = "medium_liquid",
                 base_id: str = "") -> Dict[str, Any]:
    """返回一个可用于「新建自定义箭型」的完整模板。

    v4 默认**克隆一个已率定的内置箭型**（按类别选 :data:`TEMPLATE_SOURCE`，
    也可用 ``base_id`` 指定），这样用户从"能跑出正常弹道"的数值出发改，
    比从手写量级模板出发可靠得多；内置库不可用时才退回内置手写模板。
    """
    class_key = class_key if class_key in CLASS_KEYS else "medium_liquid"
    base = get_builtin_rocket(base_id) if base_id else \
        get_builtin_rocket(TEMPLATE_SOURCE.get(class_key, ""))
    derived_from = ""
    if base is not None:
        derived_from = str(base.get("name") or base.get("id") or "")
        rec = base
        rec.pop("_shadow_builtin", None)
    else:
        rec = copy.deepcopy(_TEMPLATES.get(class_key, _TEMPLATES["medium_liquid"]))
    rec["name"] = name or "我的火箭"
    rec["class_key"] = class_key
    rec["class"] = "%s（用户自定义）" % CLASS_KEY_CN.get(class_key, "自定义")
    rec["id"] = slugify(rec["name"], "user")
    rec["aliases"] = [rec["name"]]
    rec["launch_sites"] = []          # 不继承原箭型的典型工位，避免误把发射场推断到别处
    rec["sites"] = []
    rec["_user"] = True
    rec["_created_at"] = _now()
    rec["_updated_at"] = rec["_created_at"]
    if derived_from:
        rec["_derived_from"] = derived_from
        rec["note"] = ("由内置箭型「%s」复制后由用户修改；原始数值来自内置库（已由历史任务率定），"
                       "修改后请核对数据体检与落区。" % derived_from)
    return rec



def normalize_rocket(rec: Dict[str, Any], *, keep_id: bool = True) -> Dict[str, Any]:
    """补默认值、去空串、统一数值类型；返回新对象（不改原对象）。"""
    r = copy.deepcopy(rec or {})
    r["name"] = str(r.get("name") or "").strip() or "未命名箭型"
    r["class_key"] = class_key_of(r)
    r["class"] = str(r.get("class") or "").strip() or \
        "%s（用户自定义）" % CLASS_KEY_CN.get(r["class_key"], "自定义")
    if not keep_id or not r.get("id"):
        r["id"] = slugify(r["name"], "user")
    r["id"] = re.sub(r"[^0-9A-Za-z_\-]+", "_", str(r["id"]))
    r.setdefault("aliases", [])
    r["aliases"] = [str(a).strip() for a in (r.get("aliases") or []) if str(a).strip()]
    r["confidence"] = str(r.get("confidence") or "中")
    r["note"] = str(r.get("note") or "").strip()
    r.setdefault("sources", [])
    r["_user"] = True
    r.setdefault("_created_at", _now())
    r["_updated_at"] = _now()

    veh = r.setdefault("vehicle", {})
    stages = veh.get("stages") or []
    if not isinstance(stages, list) or not stages:
        stages = copy.deepcopy(_TEMPLATES[r["class_key"]]["vehicle"]["stages"])
    for i, st in enumerate(stages):
        st["name"] = str(st.get("name") or ("第%d级" % (i + 1))).strip()
        for k in ("dry_kg", "prop_kg"):
            st[k] = _fnum(st.get(k), 0.0)
        st["pitch_end_deg"] = _fnum(st.get("pitch_end_deg"), 0.0)
        if st.get("cda_attached_m2") is not None:
            st["cda_attached_m2"] = _fnum(st["cda_attached_m2"], 0.0)
        for k in ("thrust_sl_n", "thrust_vac_n", "isp_sl_s", "isp_vac_s",
                  "pitch_duration_s", "seco_met", "sep_met"):
            if st.get(k) is not None:
                st[k] = _fnum(st[k], None)
        if st.get("engines") is not None:
            st["engines"] = max(1, int(_fnum(st["engines"], 1)))
        # 推力/比冲：一级常用单机字段，多级常用 phases
        ph = st.get("phases")
        if isinstance(ph, list):
            for p in ph:
                p.setdefault("name", "主机")
                for th in p.get("thrusters") or []:
                    th["n"] = max(1, int(_fnum(th.get("n"), 1)))
                    th["thrust_vac_n"] = _fnum(th.get("thrust_vac_n"), 0.0)
                    th["isp_vac_s"] = _fnum(th.get("isp_vac_s"), 0.0)
                    if th.get("thrust_sl_n") is not None:
                        th["thrust_sl_n"] = _fnum(th["thrust_sl_n"], 0.0)
                    if th.get("isp_sl_s") is not None:
                        th["isp_sl_s"] = _fnum(th["isp_sl_s"], 0.0)
                if p.get("end_met") is not None:
                    p["end_met"] = _fnum(p["end_met"], None)
            if not ph:
                st.pop("phases", None)
    veh["stages"] = stages
    veh["adapter_kg"] = _fnum(veh.get("adapter_kg"), 0.0)
    veh["ascent_cda_scale"] = _fnum(veh.get("ascent_cda_scale"), 1.0) or 1.0
    veh["payload_kg"] = _fnum(veh.get("payload_kg"), _fnum(r.get("default_payload_kg"), 1000.0))

    bo = veh.get("boosters")
    if isinstance(bo, dict) and bo:
        bo["count"] = max(1, int(_fnum(bo.get("count"), 2)))
        bo["prop_kg_each"] = _fnum(bo.get("prop_kg_each"), 0.0)
        bo["dry_kg_each"] = _fnum(bo.get("dry_kg_each"), _fnum(bo.get("dry_kg"), 0.0))
        for k in ("thrust_sl_n", "thrust_vac_n", "isp_sl_s", "isp_vac_s", "jettison_met"):
            if bo.get(k) is not None:
                bo[k] = _fnum(bo[k], None)
        ths = bo.get("thrusters")
        if isinstance(ths, list):
            for th in ths:
                th["n"] = max(1, int(_fnum(th.get("n"), 1)))
                th["thrust_vac_n"] = _fnum(th.get("thrust_vac_n"), 0.0)
                th["isp_vac_s"] = _fnum(th.get("isp_vac_s"), 0.0)
            if not ths:
                bo.pop("thrusters", None)
    elif bo is not None:
        veh["boosters"] = None

    fa = veh.get("fairing")
    if isinstance(fa, dict) and fa:
        fa["mass_kg"] = _fnum(fa.get("mass_kg"), 0.0)
        fa["halves"] = max(1, int(_fnum(fa.get("halves"), 2)))
        fa["cda_m2"] = _fnum(fa.get("cda_m2"), 10.0)
        if fa.get("sep_met") is not None:
            fa["sep_met"] = _fnum(fa["sep_met"], None)
        if fa["mass_kg"] <= 0:
            veh["fairing"] = None
    elif fa is not None:
        veh["fairing"] = None

    deb = veh.get("debris")
    if not isinstance(deb, dict):
        veh["debris"] = {}
    for key, val in list((veh.get("debris") or {}).items()):
        if not isinstance(val, dict):
            veh["debris"].pop(key)
            continue
        val["cda"] = _fnum(val.get("cda"), 7.0)
        val["extra_mass_kg"] = _fnum(val.get("extra_mass_kg"), 0.0)
        if val.get("divert_mps") is not None:
            val["divert_mps"] = _fnum(val["divert_mps"], 0.0)

    g = r.setdefault("guidance_defaults", {})
    for k in ("t_v", "t_p", "pitch_power", "t_fairing", "fairing_divert_mps",
              "yaw_tau", "yaw2_tau", "i_target_deg", "azimuth_deg",
              "seco_band_km", "seco_e_max"):
        if g.get(k) is not None:
            g[k] = _fnum(g[k], None)
    sch = g.get("pitch_schedule")
    if isinstance(sch, list):
        cleaned = []
        for pt in sch:
            try:
                t, a = float(pt[0]), float(pt[1])
            except (TypeError, ValueError, IndexError):
                continue
            if t > 0:
                cleaned.append([t, a])
        cleaned.sort(key=lambda x: x[0])
        if len(cleaned) >= 2:
            g["pitch_schedule"] = cleaned
        else:
            g.pop("pitch_schedule", None)

    # 运力/典型轨道
    for k in ("liftoff_mass_kg", "leo_kg", "sso_kg", "default_payload_kg"):
        if r.get(k) is not None:
            r[k] = _fnum(r[k], None)
    tos = r.get("typical_orbits")
    if isinstance(tos, list):
        r["typical_orbits"] = [
            {"name": str(t.get("name") or "参考轨道"),
             "alt_km": _fnum(t.get("alt_km"), None),
             "i_deg": (None if t.get("i_deg") is None else _fnum(t["i_deg"], None))}
            for t in tos if isinstance(t, dict) and t.get("alt_km") is not None]
    else:
        r["typical_orbits"] = []
    if isinstance(r.get("launch_sites"), list):
        r["launch_sites"] = [str(x) for x in r["launch_sites"] if str(x).strip()]
    else:
        r["launch_sites"] = []
    return r


def _fnum(v: Any, default: Optional[float]) -> Optional[float]:
    if v is None or (isinstance(v, str) and not v.strip()):
        return default
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def validate_rocket(rec: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """校验自定义箭型。返回 ``[(级别, 字段, 说明)]``；级别：错误/提示。

    只拦真正会让仿真跑不出来的问题（缺级、负质量、无推力…），其余给"提示"，
    让用户能带着警告跑起来并看数据体检，而不是被一堵墙拦死。
    """
    out: List[Tuple[str, str, str]] = []
    r = rec or {}
    name = str(r.get("name") or "").strip()
    if not name:
        out.append((ERROR, "name", "请填写箭型名称"))
    veh = r.get("vehicle") or {}
    stages = veh.get("stages") or []
    if not stages:
        out.append((ERROR, "vehicle.stages", "至少要有一级"))
        return out
    if len(stages) > 5:
        out.append((WARN, "vehicle.stages", "级数 %d 偏多（>5），请确认是否误填" % len(stages)))

    total_mass = 0.0
    for i, st in enumerate(stages):
        nm = str(st.get("name") or ("第%d级" % (i + 1)))
        pre = "第%d级「%s」" % (i + 1, nm)
        dry, prop = _fnum(st.get("dry_kg"), 0.0), _fnum(st.get("prop_kg"), 0.0)
        if dry is None or dry <= 0:
            out.append((ERROR, "stages.%d.dry_kg" % i, "%s 结构质量必须 > 0" % pre))
        if prop is None or prop <= 0:
            out.append((ERROR, "stages.%d.prop_kg" % i, "%s 推进剂质量必须 > 0" % pre))
        total_mass += (dry or 0.0) + (prop or 0.0)
        # 推力来源：单机字段 或 phases
        thrust_any = None
        isp_any = None
        if st.get("phases"):
            flow = 0.0
            thr = 0.0
            for p in st["phases"]:
                for th in p.get("thrusters") or []:
                    n = max(1, int(_fnum(th.get("n"), 1) or 1))
                    tv = _fnum(th.get("thrust_vac_n"), 0.0) or 0.0
                    iv = _fnum(th.get("isp_vac_s"), 0.0) or 0.0
                    thr += n * tv
                    flow += n * tv / (iv * G0) if iv > 0 else 0.0
            thrust_any, isp_any = (thr or None), (None if flow <= 0 else thr / (flow * G0))
        else:
            thr = (_fnum(st.get("thrust_sl_n"), 0.0) or 0.0) or \
                  (_fnum(st.get("thrust_vac_n"), 0.0) or 0.0)
            eng = max(1, int(_fnum(st.get("engines"), 1) or 1))
            thrust_any = (thr * eng) or None
            isp_any = _fnum(st.get("isp_vac_s"), _fnum(st.get("isp_sl_s"), None))
        if thrust_any is None:
            out.append((ERROR, "stages.%d.thrust" % i,
                        "%s 没有推力数据：请填「单机推力 + 台数」或 phases 里的 thrusters" % pre))
        if isp_any is None or isp_any <= 0:
            out.append((ERROR, "stages.%d.isp" % i, "%s 没有比冲数据（isp_vac_s）" % pre))
        elif not (150.0 <= float(isp_any) <= 520.0):
            out.append((WARN, "stages.%d.isp" % i,
                        "%s 比冲 %.0f s 超出常见范围 150~520 s，请复核" % (pre, float(isp_any))))
        if st.get("cda_attached_m2") is not None:
            cda = _fnum(st.get("cda_attached_m2"), 0.0) or 0.0
            if cda < 0 or cda > 120:
                out.append((WARN, "stages.%d.cda" % i, "%s 迎风面积 %.1f m² 不合理" % (pre, cda)))
        for k in ("sep_met", "seco_met", "pitch_duration_s"):
            v = _fnum(st.get(k), None)
            if v is not None and v <= 0:
                out.append((WARN, "stages.%d.%s" % (i, k), "%s %s 必须 > 0" % (pre, k)))

    bo = veh.get("boosters")
    if isinstance(bo, dict) and bo:
        cnt = max(1, int(_fnum(bo.get("count"), 2) or 2))
        if not (_fnum(bo.get("prop_kg_each"), 0) or 0) > 0:
            out.append((ERROR, "boosters.prop_kg_each", "助推器单枚推进剂质量必须 > 0"))
        if not (bo.get("thrusters") or (_fnum(bo.get("thrust_vac_n"), 0) or 0) > 0):
            out.append((ERROR, "boosters.thrust", "助推器没有推力数据"))
        if not 1 <= cnt <= 8:
            out.append((ERROR, "boosters.count", "助推器数量应在 1~8"))
    fa = veh.get("fairing")
    if isinstance(fa, dict) and fa:
        if (_fnum(fa.get("mass_kg"), 0) or 0) <= 0:
            out.append((WARN, "fairing.mass_kg", "整流罩质量 ≤ 0，将按无整流罩处理"))
        sep = _fnum(fa.get("sep_met"), None)
        if sep is not None and sep <= 0:
            out.append((WARN, "fairing.sep_met", "整流罩分离时刻必须 > 0（留空=自动）"))

    g = r.get("guidance_defaults") or {}
    for k, lim in (("t_v", FIELD_LIMITS["t_v"]), ("t_p", FIELD_LIMITS["t_p"]),
                   ("pitch_power", FIELD_LIMITS["pitch_power"])):
        v = _fnum(g.get(k), None)
        if v is not None and not (lim[0] <= v <= lim[1]):
            out.append((WARN, "guidance_defaults." + k,
                        "%s=%.3f 超出建议范围 %.2f~%.2f，反演可能从一个很偏的点开始" % (
                            k, v, lim[0], lim[1])))
    sch = g.get("pitch_schedule")
    if isinstance(sch, list) and sch:
        ts = []
        for pt in sch:
            try:
                ts.append(float(pt[0]))
            except (TypeError, ValueError, IndexError):
                out.append((ERROR, "guidance_defaults.pitch_schedule", "俯仰剖面节点格式应为 [时间, 角度]"))
                break
        if ts:
            if any(b <= a for a, b in zip(ts, ts[1:])):
                out.append((ERROR, "guidance_defaults.pitch_schedule",
                            "俯仰剖面的时间必须严格递增"))
            if ts[0] <= 0:
                out.append((ERROR, "guidance_defaults.pitch_schedule", "俯仰剖面第一个节点时间必须 > 0"))
            if len(ts) < 2:
                out.append((WARN, "guidance_defaults.pitch_schedule",
                            "俯仰剖面至少需要 2 个节点，当前只有 1 个（将被忽略）"))

    # 起飞质量自洽性（按分级求和 vs 用户填的起飞质量）
    lm = _fnum(r.get("liftoff_mass_kg"), None)
    payload = _fnum(veh.get("payload_kg"), 0.0) or 0.0
    adapter = _fnum(veh.get("adapter_kg"), 0.0) or 0.0
    fair = (veh.get("fairing") or {}).get("mass_kg") or 0.0
    bm = 0.0
    if isinstance(bo, dict) and bo:
        bm = (float(_fnum(bo.get("prop_kg_each"), 0) or 0)
              + float(_fnum(bo.get("dry_kg_each"), 0) or 0)) * float(
                  max(1, int(_fnum(bo.get("count"), 2) or 2)))
    calc_mass = total_mass + bm + payload + adapter + float(fair)
    if lm is not None and lm > 0 and calc_mass > 0:
        err = abs(calc_mass - lm) / lm
        if err > 0.15:
            out.append((WARN, "liftoff_mass_kg",
                        "起飞质量填 %.0f kg，但按各级+助推器+载荷+整流罩求和为 %.0f kg"
                        "（差 %.0f%%）。仿真按分级求和计算；建议把两者对齐或只为参考填运力。"
                        % (lm, calc_mass, err * 100.0)))

    # 起飞推重比：低于 1 时火箭根本飞不起来，必须提示（编辑器里也会实时显示）
    try:
        thrust0 = 0.0
        st0 = stages[0]
        if st0.get("phases"):
            for p in st0["phases"]:
                for th in p.get("thrusters") or []:
                    n = max(1, int(_fnum(th.get("n"), 1) or 1))
                    thrust0 += n * ((_fnum(th.get("thrust_sl_n"), None)
                                     or _fnum(th.get("thrust_vac_n"), 0.0) or 0.0))
        else:
            eng = max(1, int(_fnum(st0.get("engines"), 1) or 1))
            thrust0 = eng * ((_fnum(st0.get("thrust_sl_n"), None)
                              or _fnum(st0.get("thrust_vac_n"), 0.0) or 0.0))
        if isinstance(bo, dict) and bo:
            cnt = max(1, int(_fnum(bo.get("count"), 2) or 2))
            bt = 0.0
            if bo.get("thrusters"):
                for th in bo["thrusters"]:
                    n = max(1, int(_fnum(th.get("n"), 1) or 1))
                    bt += n * ((_fnum(th.get("thrust_sl_n"), None)
                                or _fnum(th.get("thrust_vac_n"), 0.0) or 0.0))
            else:
                bt = (_fnum(bo.get("thrust_sl_n"), None)
                      or _fnum(bo.get("thrust_vac_n"), 0.0) or 0.0)
            thrust0 += bt * cnt
        if thrust0 > 0 and calc_mass > 0:
            twr = thrust0 / (calc_mass * G0)
            if twr < 1.0:
                out.append((WARN, "liftoff_thrust",
                            "起飞推重比仅 %.2f（<1.0）：按此参数火箭无法离地，请核对推力/质量"
                            % twr))
            elif twr < 1.15:
                out.append((WARN, "liftoff_thrust",
                            "起飞推重比 %.2f 偏低（常见 1.15~1.45），起飞段会很长" % twr))
            elif twr > 2.2:
                out.append((WARN, "liftoff_thrust",
                            "起飞推重比 %.2f 偏高（常见 1.15~1.45），请核对是否把"
                            "台数/推力填重了" % twr))
    except Exception:  # noqa: BLE001
        pass
    return out


def rocket_metrics(rec: Dict[str, Any]) -> Dict[str, Any]:
    """估算起飞推重比、各级工作段、理论 Δv（供编辑器实时预览，不参与仿真）。"""
    r = normalize_rocket(rec)
    veh = r.get("vehicle") or {}
    stages = veh.get("stages") or []
    bo = veh.get("boosters") or {}
    out: Dict[str, Any] = {"warnings": []}

    def _stage_thrust(st: Dict[str, Any], sea: bool = False) -> Tuple[float, float]:
        """→ (推力 N, 流量 kg/s)"""
        thr = flow = 0.0
        if st.get("phases"):
            for p in st["phases"]:
                for th in p.get("thrusters") or []:
                    n = max(1, int(_fnum(th.get("n"), 1) or 1))
                    tv = _fnum(th.get("thrust_vac_n"), 0.0) or 0.0
                    iv = _fnum(th.get("isp_vac_s"), 0.0) or 0.0
                    thr += n * tv
                    flow += n * tv / (iv * G0) if iv > 0 else 0.0
        else:
            eng = max(1, int(_fnum(st.get("engines"), 1) or 1))
            tv = _fnum(st.get("thrust_vac_n"), 0.0) or 0.0
            ts = _fnum(st.get("thrust_sl_n"), 0.0) or 0.0
            iv = _fnum(st.get("isp_vac_s"), 0.0) or 0.0
            isl = _fnum(st.get("isp_sl_s"), 0.0) or 0.0
            use = (ts, isl) if (sea and ts > 0 and isl > 0) else (tv, iv)
            thr = eng * use[0]
            flow = eng * use[0] / (use[1] * G0) if use[1] > 0 else 0.0
        return thr, flow

    m = 0.0
    for st in stages:
        m += (_fnum(st.get("dry_kg"), 0.0) or 0.0) + (_fnum(st.get("prop_kg"), 0.0) or 0.0)
    m += (_fnum(veh.get("payload_kg"), 0.0) or 0.0) + (_fnum(veh.get("adapter_kg"), 0.0) or 0.0)
    if veh.get("fairing"):
        m += _fnum(veh["fairing"].get("mass_kg"), 0.0) or 0.0
    bmass = 0.0
    if bo:
        cnt = max(1, int(_fnum(bo.get("count"), 2) or 2))
        bmass = ((_fnum(bo.get("prop_kg_each"), 0.0) or 0.0)
                 + (_fnum(bo.get("dry_kg_each"), 0.0) or 0.0)) * cnt
        m += bmass
    out["liftoff_mass_kg"] = m

    # 起飞推力（一级 + 助推器，海平面优先）
    t_liftoff = 0.0
    if stages:
        t_liftoff += _stage_thrust(stages[0], sea=True)[0]
    if bo:
        cnt = max(1, int(_fnum(bo.get("count"), 2) or 2))
        if bo.get("thrusters"):
            bt = 0.0
            for th in bo["thrusters"]:
                n = max(1, int(_fnum(th.get("n"), 1) or 1))
                bt += n * (_fnum(th.get("thrust_vac_n"), 0.0) or 0.0)
            t_liftoff += bt * cnt
        else:
            ts = _fnum(bo.get("thrust_sl_n"), 0.0) or 0.0
            tv = _fnum(bo.get("thrust_vac_n"), 0.0) or 0.0
            t_liftoff += (ts or tv) * cnt
    out["liftoff_thrust_n"] = t_liftoff
    out["liftoff_twr"] = (t_liftoff / (m * G0)) if m > 0 else 0.0
    if out["liftoff_twr"] < 1.10:
        out["warnings"].append("起飞推重比 %.2f < 1.10：按此参数火箭无法起飞（请检查推力/质量）"
                               % out["liftoff_twr"])

    # 各级工作段（含助推器）
    burns: List[Dict[str, Any]] = []
    if bo:
        cnt = max(1, int(_fnum(bo.get("count"), 2) or 2))
        if bo.get("thrusters"):
            thr = sum(max(1, int(_fnum(th.get("n"), 1) or 1))
                      * (_fnum(th.get("thrust_vac_n"), 0.0) or 0.0) for th in bo["thrusters"])
            isp = _fnum((bo["thrusters"][0] or {}).get("isp_vac_s"), 0.0) or 0.0
            flow = thr / (isp * G0) if isp > 0 else 0.0
        else:
            thr = _fnum(bo.get("thrust_vac_n"), 0.0) or 0.0
            isp = _fnum(bo.get("isp_vac_s"), 0.0) or 0.0
            flow = thr / (isp * G0) if isp > 0 else 0.0
        flow *= cnt
        prop = (_fnum(bo.get("prop_kg_each"), 0.0) or 0.0) * cnt
        burns.append({"name": "助推器×%d" % cnt, "prop_kg": prop,
                      "burn_s": (prop / flow) if flow > 0 else 0.0})
    m_stage = m
    dv_total = 0.0
    for i, st in enumerate(stages):
        thr, flow = _stage_thrust(st)
        prop = _fnum(st.get("prop_kg"), 0.0) or 0.0
        dry = _fnum(st.get("dry_kg"), 0.0) or 0.0
        burn = (prop / flow) if flow > 0 else 0.0
        isp = _fnum(st.get("isp_vac_s"), None) or (
            (thr / (flow * G0)) if flow > 0 else 0.0)
        m0 = m_stage
        m1 = max(1.0, m0 - prop)
        dv = isp * G0 * math.log(m0 / m1) if (isp > 0 and m1 > 0) else 0.0
        dv_total += dv
        burns.append({"name": str(st.get("name") or ("第%d级" % (i + 1))),
                      "prop_kg": prop, "burn_s": burn, "isp_s": isp,
                      "m0_kg": m0, "m1_kg": m1, "dv_mps": dv})
        # 关机后抛掉本级（末级不抛）
        m_stage = m1 - dry if i < len(stages) - 1 else m1
    out["burns"] = burns
    out["dv_ideal_mps"] = dv_total
    out["burn_total_s"] = sum(b["burn_s"] for b in burns)
    if out["burn_total_s"] <= 0:
        out["warnings"].append("无法估算工作段时长：请检查推力与比冲")
    return out


# --------------------------------------------------------------------------- #
# 箭型 CRUD
# --------------------------------------------------------------------------- #
def load_db(name: str) -> Dict[str, Any]:
    return _read_json_tolerant(_db_path(name))


def list_rockets() -> List[Dict[str, Any]]:
    d = load_db("rockets")
    items = d.get("rockets") if isinstance(d, dict) else None
    return [r for r in (items or []) if isinstance(r, dict) and r.get("name")]


def get_rocket(rid: str) -> Optional[Dict[str, Any]]:
    for r in list_rockets():
        if str(r.get("id")) == str(rid):
            return normalize_rocket(r)
    return None


def save_rocket(rec: Dict[str, Any], *, new: bool = False) -> Dict[str, Any]:
    """保存/更新自定义箭型，返回落盘后的记录（含最终 id）。"""
    rec = normalize_rocket(rec, keep_id=not new)
    items = list_rockets()
    if new or not any(str(it.get("id")) == str(rec["id"]) for it in items):
        rec["id"] = _ensure_unique(items, str(rec["id"]))
    replace = [it for it in items if str(it.get("id")) == str(rec["id"])]
    if replace:
        idx = items.index(replace[0])
        items[idx] = rec
    else:
        items.append(rec)
    _write_json(_db_path("rockets"), {"schema": "rocketkit.user_db.v1",
                                      "updated_utc": _now(), "rockets": items})
    return rec


def delete_rocket(rid: str) -> bool:
    items = list_rockets()
    kept = [it for it in items if str(it.get("id")) != str(rid)]
    if len(kept) == len(items):
        return False
    _write_json(_db_path("rockets"), {"schema": "rocketkit.user_db.v1",
                                      "updated_utc": _now(), "rockets": kept})
    return True


def merge_rockets(builtin: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """内置 + 用户（用户优先；同 id 覆盖内置并标注 ``_shadow_builtin``）。"""
    out = list(builtin or [])
    by_id = {str(it.get("id")): i for i, it in enumerate(out)}
    for u in list_rockets():
        rid = str(u.get("id") or "")
        if not rid:
            continue
        if rid in by_id:
            shadow = dict(u)
            shadow["_shadow_builtin"] = True
            out[by_id[rid]] = shadow
        else:
            out.append(dict(u))
    return out


# --------------------------------------------------------------------------- #
# 参考轨道 CRUD
# --------------------------------------------------------------------------- #
def list_orbits() -> List[Dict[str, Any]]:
    d = load_db("orbits")
    items = d.get("orbits") if isinstance(d, dict) else None
    return [o for o in (items or []) if isinstance(o, dict) and o.get("name")]


def normalize_orbit(rec: Dict[str, Any]) -> Dict[str, Any]:
    o = copy.deepcopy(rec or {})
    o["name"] = str(o.get("name") or "").strip() or "未命名轨道"
    o["id"] = re.sub(r"[^0-9A-Za-z_\-]+", "_", str(o.get("id") or slugify(o["name"], "user_orbit")))
    o["alt_km"] = _fnum(o.get("alt_km"), None)
    o["i_deg"] = _fnum(o.get("i_deg"), None)
    o["e"] = _fnum(o.get("e"), 0.0) or 0.0
    for k in ("raan_deg", "argp_deg", "ltan_h"):
        o[k] = _fnum(o.get(k), None)
    if o.get("raan_deg") is not None:
        o["raan_deg"] = float(o["raan_deg"]) % 360.0
    o["type"] = str(o.get("type") or "LEO").upper()
    o["confidence"] = str(o.get("confidence") or "高")
    o["note"] = str(o.get("note") or "").strip()
    o["epoch_utc"] = str(o.get("epoch_utc") or "").strip()
    o["_user"] = True
    o["_updated_at"] = _now()
    return o


def validate_orbit(rec: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    out: List[Tuple[str, str, str]] = []
    o = rec or {}
    if not str(o.get("name") or "").strip():
        out.append((ERROR, "name", "请填写轨道名称"))
    alt = _fnum(o.get("alt_km"), None)
    if alt is None:
        out.append((ERROR, "alt_km", "请填写轨道高度（近圆轨道按高度；椭圆轨道填远地点高度）"))
    elif not 80 <= alt <= 200000:
        out.append((ERROR, "alt_km", "轨道高度应在 80 ~ 200000 km（当前 %.0f）" % alt))
    i = _fnum(o.get("i_deg"), None)
    if i is None:
        out.append((WARN, "i_deg", "倾角留空：将按发射场纬度/方位角推断"))
    elif not 0 <= i <= 180:
        out.append((ERROR, "i_deg", "倾角应在 0 ~ 180°（当前 %.1f）" % i))
    e = _fnum(o.get("e"), 0.0) or 0.0
    if not 0 <= e < 1:
        out.append((ERROR, "e", "偏心率应在 0 ~ 1（当前 %.3f）" % e))
    if alt is not None and alt > 300 and e > 0.05:
        out.append((WARN, "e", "偏心率 %.3f 偏高：本工具按上升段入轨点求根数，"
                                "大椭圆轨道（GTO）请把 alt_km 填远地点高度并留意报告标注" % e))
    ltan = _fnum(o.get("ltan_h"), None)
    if ltan is not None and not 0 <= ltan < 24:
        out.append((ERROR, "ltan_h", "LTAN 应在 0~24 小时（当前 %.2f）" % ltan))
    if i is not None and 95 <= i <= 105 and ltan is None:
        out.append((INFO, "", "倾角在 95~105°，属太阳同步轨道：填 LTAN 可算出轨道平面发射窗口"))
    return out


def save_orbit(rec: Dict[str, Any], *, new: bool = False) -> Dict[str, Any]:
    o = normalize_orbit(rec)
    items = list_orbits()
    if new or not any(str(it.get("id")) == str(o["id"]) for it in items):
        o["id"] = _ensure_unique(items, str(o["id"]))
    hit = [it for it in items if str(it.get("id")) == str(o["id"])]
    if hit:
        items[items.index(hit[0])] = o
    else:
        items.append(o)
    _write_json(_db_path("orbits"), {"schema": "rocketkit.user_db.v1",
                                     "updated_utc": _now(), "orbits": items})
    return o


def delete_orbit(oid: str) -> bool:
    items = list_orbits()
    kept = [it for it in items if str(it.get("id")) != str(oid)]
    if len(kept) == len(items):
        return False
    _write_json(_db_path("orbits"), {"schema": "rocketkit.user_db.v1",
                                     "updated_utc": _now(), "orbits": kept})
    return True


def merge_orbits(builtin: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = list(builtin or [])
    by_id = {str(it.get("id")): i for i, it in enumerate(out)}
    for u in list_orbits():
        rid = str(u.get("id") or "")
        if not rid:
            continue
        if rid in by_id:
            shadow = dict(u)
            shadow["_shadow_builtin"] = True
            out[by_id[rid]] = shadow
        else:
            out.append(dict(u))
    return out


# --------------------------------------------------------------------------- #
# 时序模板
# --------------------------------------------------------------------------- #
def list_sequences() -> List[Dict[str, Any]]:
    d = load_db("sequences")
    items = d.get("templates") if isinstance(d, dict) else None
    return [s for s in (items or []) if isinstance(s, dict) and s.get("name")]


def save_sequence(rec: Dict[str, Any]) -> Dict[str, Any]:
    s = copy.deepcopy(rec or {})
    s["name"] = str(s.get("name") or "").strip() or "未命名时序"
    s["id"] = re.sub(r"[^0-9A-Za-z_\-]+", "_", str(s.get("id") or slugify(s["name"], "seq")))
    s["rocket_id"] = str(s.get("rocket_id") or "")
    s["values"] = dict(s.get("values") or {})
    s["_updated_at"] = _now()
    items = list_sequences()
    hit = [it for it in items if str(it.get("id")) == str(s["id"])]
    if hit:
        items[items.index(hit[0])] = s
    else:
        s["id"] = _ensure_unique(items, str(s["id"]))
        items.append(s)
    _write_json(_db_path("sequences"), {"schema": "rocketkit.user_db.v1",
                                        "updated_utc": _now(), "templates": items})
    return s


def delete_sequence(sid: str) -> bool:
    items = list_sequences()
    kept = [it for it in items if str(it.get("id")) != str(sid)]
    if len(kept) == len(items):
        return False
    _write_json(_db_path("sequences"), {"schema": "rocketkit.user_db.v1",
                                        "updated_utc": _now(), "templates": kept})
    return True


# --------------------------------------------------------------------------- #
# 整库导入 / 导出（换机器、备份、分享）
# --------------------------------------------------------------------------- #
def export_bundle(path: str) -> int:
    """把全部用户数据写成一个 JSON 包；返回导出的条目数。

    注意：用户库文件里时序模板的键名是 ``templates``，而数据包里统一用 ``sequences``
    （v4 早期版本导出时两边键名不一致，导致导入丢时序模板——这里已修正并兼容旧包）。
    """
    bundle = {}
    n = 0
    for file_name, file_key, bundle_key in (
            ("rockets", "rockets", "rockets"),
            ("orbits", "orbits", "orbits"),
            ("sequences", "templates", "sequences")):
        d = load_db(file_name) or {}
        items = d.get(file_key) or []
        if items:
            bundle[bundle_key] = items
            n += len(items)
    _write_json(Path(path), {"schema": "rocketkit.user_bundle.v1",
                             "exported_utc": _now(), **bundle})
    return n


def import_bundle(path: str, *, replace: bool = False) -> Dict[str, int]:
    """导入用户数据包。``replace=True`` 覆盖同名条目，否则跳过冲突项。"""
    data = _read_json_tolerant(Path(path))
    if not isinstance(data, dict):
        raise ValueError("文件不是有效的用户数据包（JSON 对象）")
    stats: Dict[str, int] = {"rockets": 0, "orbits": 0, "sequences": 0, "skipped": 0}
    # 兼容 v4 早期导出过的 "templates" 键
    if "sequences" not in data and isinstance(data.get("templates"), list):
        data = dict(data, sequences=data["templates"])
    for key, saver, lister in (
            ("rockets", save_rocket, list_rockets),
            ("orbits", save_orbit, list_orbits),
            ("sequences", save_sequence, list_sequences)):
        items = data.get(key)
        if not isinstance(items, list):
            continue
        existing = lister()
        names = {str(it.get("name")) for it in existing}
        for it in items:
            if not isinstance(it, dict):
                continue
            if not replace and str(it.get("name")) in names:
                stats["skipped"] += 1
                continue
            try:
                if key == "rockets":
                    it = dict(it)
                    it.pop("_shadow_builtin", None)
                    saver(it)
                elif key == "orbits":
                    saver(it)
                else:
                    saver(it)
                stats[key] += 1
            except Exception:  # noqa: BLE001
                stats["skipped"] += 1
    return stats


# --------------------------------------------------------------------------- #
# 模板（新建自定义箭型时的起点）
# --------------------------------------------------------------------------- #
_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "medium_liquid": {
        "id": "user_medium", "name": "我的中型液体火箭", "aliases": [],
        "class": "中型液体（用户自定义）", "class_key": "medium_liquid",
        "confidence": "中", "liftoff_mass_kg": 250000.0, "leo_kg": 3000.0,
        "sso_kg": 1300.0, "default_payload_kg": 1000.0,
        "typical_orbits": [{"name": "LEO 400 km", "alt_km": 400.0, "i_deg": None}],
        "launch_sites": [], "note": "用户自定义箭型（按 250 t 级两级液体火箭量级初始化，请逐项核对）",
        "sources": [{"url": "", "note": "用户自定义参数，来源由使用者负责", "accessed": ""}],
        "guidance_defaults": {"mode": "shooting_plane", "azimuth_deg": None,
                              "t_v": 8.0, "t_p": 165.0, "pitch_power": 1.05,
                              "t_fairing": 190.0, "fairing_divert_mps": 4.0,
                              "plane_keeping": False, "yaw_tau": 150.0,
                              "i_target_deg": None},
        "vehicle": {
            "adapter_kg": 200.0, "payload_kg": 1000.0, "ascent_cda_scale": 0.8,
            "stages": [
                {"name": "一级", "dry_kg": 12000.0, "prop_kg": 190000.0, "engines": 4,
                 "thrust_sl_n": 740000.0, "thrust_vac_n": 820000.0,
                 "isp_sl_s": 260.0, "isp_vac_s": 292.0, "pitch_end_deg": 25.0,
                 "cda_attached_m2": 11.0},
                {"name": "二级", "dry_kg": 4000.0, "prop_kg": 40000.0,
                 "pitch_end_deg": -5.0, "pitch_duration_s": 400.0, "seco_met": None,
                 "cda_attached_m2": 9.0,
                 "phases": [{"name": "主机", "thrusters": [
                     {"n": 1, "thrust_vac_n": 700000.0, "isp_vac_s": 300.0}]}]},
            ],
            "boosters": None,
            "fairing": {"mass_kg": 1800.0, "halves": 2, "sep_met": 190.0, "cda_m2": 13.0},
            "debris": {"stage_0": {"cda": 7.0, "extra_mass_kg": 1200.0},
                       "stage_1": {"cda": 5.0, "extra_mass_kg": 0.0},
                       "fairing": {"cda": 7.0, "extra_mass_kg": 0.0}},
        },
    },
    "small_solid": {
        "id": "user_solid", "name": "我的小型固体火箭", "aliases": [],
        "class": "小型固体（用户自定义）", "class_key": "small_solid",
        "confidence": "中", "liftoff_mass_kg": 40000.0, "leo_kg": 500.0,
        "sso_kg": 300.0, "default_payload_kg": 200.0,
        "typical_orbits": [{"name": "SSO 500 km", "alt_km": 500.0, "i_deg": 97.4}],
        "launch_sites": [], "note": "用户自定义箭型（按 40 t 级固体小火箭量级初始化）",
        "sources": [{"url": "", "note": "用户自定义参数，来源由使用者负责", "accessed": ""}],
        "guidance_defaults": {"mode": "shooting_plane", "azimuth_deg": None,
                              "t_v": 6.0, "t_p": 50.0, "pitch_power": 1.2,
                              "t_fairing": 90.0, "fairing_divert_mps": 0.0,
                              "plane_keeping": False, "yaw_tau": 150.0,
                              "i_target_deg": None},
        "vehicle": {
            "adapter_kg": 30.0, "payload_kg": 200.0, "ascent_cda_scale": 1.0,
            "stages": [
                {"name": "一级", "dry_kg": 3500.0, "prop_kg": 35000.0, "engines": 1,
                 "thrust_sl_n": 800000.0, "thrust_vac_n": 880000.0,
                 "isp_sl_s": 240.0, "isp_vac_s": 260.0, "pitch_end_deg": 30.0,
                 "cda_attached_m2": 8.0},
                {"name": "二级", "dry_kg": 800.0, "prop_kg": 3000.0,
                 "pitch_end_deg": 0.0, "pitch_duration_s": 200.0, "seco_met": None,
                 "cda_attached_m2": 5.0,
                 "phases": [{"name": "主机", "thrusters": [
                     {"n": 1, "thrust_vac_n": 60000.0, "isp_vac_s": 290.0}]}]},
            ],
            "boosters": None,
            "fairing": None,
            "debris": {"stage_0": {"cda": 5.0, "extra_mass_kg": 0.0},
                       "stage_1": {"cda": 3.0, "extra_mass_kg": 0.0}},
        },
    },
    "large_liquid": {
        "id": "user_large", "name": "我的大型液体火箭", "aliases": [],
        "class": "大型液体（用户自定义）", "class_key": "large_liquid",
        "confidence": "中", "liftoff_mass_kg": 870000.0, "leo_kg": 25000.0,
        "sso_kg": 15000.0, "default_payload_kg": 8000.0,
        "typical_orbits": [{"name": "LEO 400 km", "alt_km": 400.0, "i_deg": None}],
        "launch_sites": [], "note": "用户自定义箭型（按 870 t 级大型三级半火箭量级初始化）",
        "sources": [{"url": "", "note": "用户自定义参数，来源由使用者负责", "accessed": ""}],
        "guidance_defaults": {"mode": "shooting_plane", "azimuth_deg": None,
                              "t_v": 9.0, "t_p": 180.0, "pitch_power": 1.1,
                              "t_fairing": 250.0, "fairing_divert_mps": 5.0,
                              "plane_keeping": False, "yaw_tau": 150.0,
                              "i_target_deg": None},
        "vehicle": {
            "adapter_kg": 500.0, "payload_kg": 8000.0, "ascent_cda_scale": 0.8,
            "stages": [
                {"name": "一级", "dry_kg": 22000.0, "prop_kg": 170000.0, "engines": 8,
                 "thrust_sl_n": 1050000.0, "thrust_vac_n": 1180000.0,
                 "isp_sl_s": 300.0, "isp_vac_s": 330.0, "pitch_end_deg": 25.0,
                 "cda_attached_m2": 18.0},
                {"name": "二级", "dry_kg": 8000.0, "prop_kg": 150000.0,
                 "pitch_end_deg": 10.0, "pitch_duration_s": 500.0,
                 "cda_attached_m2": 16.0,
                 "phases": [{"name": "主机", "thrusters": [
                     {"n": 2, "thrust_vac_n": 700000.0, "isp_vac_s": 340.0}]}]},
                {"name": "三级", "dry_kg": 3000.0, "prop_kg": 20000.0,
                 "pitch_end_deg": 0.0, "pitch_duration_s": 300.0, "seco_met": None,
                 "cda_attached_m2": 14.0,
                 "phases": [{"name": "主机", "thrusters": [
                     {"n": 2, "thrust_vac_n": 88000.0, "isp_vac_s": 440.0}]}]},
            ],
            "boosters": {"count": 4, "prop_kg_each": 80000.0, "dry_kg_each": 8000.0,
                         "jettison_met": 170.0,
                         "thrusters": [{"n": 1, "thrust_vac_n": 1200000.0, "isp_vac_s": 305.0}]},
            "fairing": {"mass_kg": 3500.0, "halves": 2, "sep_met": 250.0, "cda_m2": 20.0},
            "debris": {"stage_0": {"cda": 9.0, "extra_mass_kg": 2500.0},
                       "stage_1": {"cda": 7.0, "extra_mass_kg": 1200.0},
                       "fairing": {"cda": 8.0, "extra_mass_kg": 600.0},
                       "boosters": {"cda": 8.0, "extra_mass_kg": 1800.0, "divert_mps": 6.0}},
        },
    },
    "crewed": {
        "id": "user_crewed", "name": "我的载人火箭", "aliases": [],
        "class": "载人中型液体（用户自定义）", "class_key": "crewed",
        "confidence": "中", "liftoff_mass_kg": 480000.0, "leo_kg": 8500.0,
        "sso_kg": 4000.0, "default_payload_kg": 8000.0,
        "typical_orbits": [{"name": "LEO 200 km（初始插入）", "alt_km": 200.0, "i_deg": 41.5}],
        "launch_sites": [], "note": "用户自定义载人火箭（两级半液体 + 逃逸塔量级）",
        "sources": [{"url": "", "note": "用户自定义参数，来源由使用者负责", "accessed": ""}],
        "guidance_defaults": {"mode": "shooting_plane", "azimuth_deg": None,
                              "t_v": 8.5, "t_p": 160.0, "pitch_power": 0.97,
                              "t_fairing": None, "fairing_divert_mps": 0.0,
                              "plane_keeping": False, "yaw_tau": 150.0,
                              "i_target_deg": None},
        "vehicle": {
            "adapter_kg": 300.0, "payload_kg": 8000.0, "ascent_cda_scale": 1.0,
            "stages": [
                {"name": "一级", "dry_kg": 14000.0, "prop_kg": 300000.0, "engines": 4,
                 "thrust_sl_n": 3000000.0, "thrust_vac_n": 3300000.0,
                 "isp_sl_s": 260.0, "isp_vac_s": 293.0, "pitch_end_deg": 24.0,
                 "cda_attached_m2": 14.0},
                {"name": "二级", "dry_kg": 5200.0, "prop_kg": 90000.0,
                 "pitch_end_deg": -4.0, "pitch_duration_s": 380.0, "seco_met": None,
                 "cda_attached_m2": 11.0,
                 "phases": [{"name": "主机", "thrusters": [
                     {"n": 1, "thrust_vac_n": 740000.0, "isp_vac_s": 300.0}]}]},
            ],
            "boosters": {"count": 4, "prop_kg_each": 30000.0, "dry_kg_each": 3500.0,
                         "jettison_met": 125.0,
                         "thrusters": [{"n": 1, "thrust_vac_n": 820000.0, "isp_vac_s": 290.0}]},
            "fairing": {"mass_kg": 1000.0, "halves": 2, "sep_met": 200.0, "cda_m2": 15.0},
            "debris": {"stage_0": {"cda": 8.0, "extra_mass_kg": 1800.0},
                       "stage_1": {"cda": 6.0, "extra_mass_kg": 300.0},
                       "fairing": {"cda": 7.0, "extra_mass_kg": 200.0},
                       "boosters": {"cda": 8.0, "extra_mass_kg": 900.0, "divert_mps": 5.0}},
        },
    },
}


def templates() -> Dict[str, Dict[str, Any]]:
    """四类初始模板（深拷贝，调用方可随意改）。"""
    return copy.deepcopy(_TEMPLATES)
