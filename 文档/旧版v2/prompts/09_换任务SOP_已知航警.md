> 仅供公开信息分析与教学使用，非实际发射弹道。

# 换一个「已知航警」的发射任务：操作手册（v2）

> 适用：手上有某次发射的 **航警原文（多边形的 B)/C) 时间、Q 行、多边形）** + 大致起飞时刻 +
> 发射场 + 箭型，想用本工具箱出一套弹道/落区/天空轨迹/地图/KMZ/不确定度。
>
> 本手册以「不要覆盖本算例」为前提：先复制一份工作目录，再改配置。

---

## 0. 前置检查（2 分钟）

```powershell
cd <新工作目录>
python -c "import numpy, scipy, matplotlib; print('ok')"      # 依赖
python scripts\qc.py --help                                   # 脚本可运行（会提示缺产物，正常）
```

- Python ≥3.10；`requirements.txt` 里的四个包即可（`pyshp` 只在重建 `data/ne/` 时需要）。
- 中文字体：`C:\Windows\Fonts\msyh.ttc`（缺失时改 `common/plotstyle.py` 顶部常量）。

## 1. 建工作目录

```powershell
Copy-Item -Recurse "F:\...\火箭弹道工具箱_v2.0" "F:\...\<任务名>_v2工作区"
cd "F:\...\<任务名>_v2工作区"
Remove-Item -Recurse -Force 示例运行, sim, sky, map, uncertainty, report -ErrorAction SilentlyContinue
```

**不要**在原始交付目录里改，否则本算例的示例与对照就没了。

## 2. 资料清单（先收集齐，再动手）

| 资料 | 用途 | 来源要求 |
|---|---|---|
| 航警原文（编号、B)/C)、Q 行、多边形顶点） | 落区约束、发射窗口 | 航警网站/官方 NOTAM，记录 URL + 访问日期 |
| 起飞时刻 T0（UTC） | 全流程时间基准 | 官方预告/发射场公告；不确定就写"标称假设"并在 `t0_note` 写明 |
| 发射场经纬度与椭球高 | 初值、ENU/射程 | 多源交叉（GE、维基、AeroPattern） |
| 箭型分级/推力/比冲/推进剂/干重 | 仿真 | 公开资料；未知的用同类箭型估值并标注置信度 |
| 参考轨道（高度/倾角）| 反演软约束、核对 | 公开轨道数据；没有就删掉 `mission.reference_orbit` |
| 观测点（可选） | 天空轨迹 | 用户指定坐标；**必须核对**（常见笔误） |

## 3. 改 `config.json`（核心，逐段）

以 `templates/config_cz2d.json` 为模板整体复制后修改。下面只列「换任务必须动」的字段。

### 3.1 `mission`

```jsonc
"mission": {
  "name": "任务名（用于标题）",
  "rocket": "火箭型号/批次", "serial": "遥号",
  "t0_utc": "2026-11-18T22:50:00Z",          // 必须带 Z，UTC
  "t0_note": "标称来源与置信度",
  "launch_site": {"name": "...", "lat_deg": 38.8632, "lon_deg": 111.5896,
                  "alt_m_ellipsoid": 1459.0, "coord_note": "多源核对记录"},
  "reference_orbit": {"alt_km": 505.0, "i_deg": 97.45, "confidence": "中"}  // 没有就整个删掉
}
```

### 3.2 `notams`（**最关键**）

```jsonc
"notams": [{
  "id": "A1234/26",
  "b_utc": "2026-11-18T22:43:00Z", "c_utc": "2026-11-18T23:05:00Z",
  "q_line": "Q)ZXXX/QRDCA/IV/BO/W/000/999/3322N11036E013",
  "fir": "ZLHW ZHWH",
  "polygon_latlon": [[33.566667, 110.516667], [33.533333, 110.733333],
                     [33.166667, 110.683333], [33.183333, 110.450000]],
  "stage_hint": "一级",                        // ← 见下方匹配规则
  "verified": "来源 URL + 访问日期；与原文逐字核对"
}]
```

- **多边形顺序是 `[lat, lon]`**（不是 GeoJSON 的 lon/lat），这也是最常见的错误。
- `stage_hint` 用于把航警区映射到残骸对象，匹配规则（`window.py` / `invert.py`）：
  - `hint in 级名` 或 `级名 in hint` → 对应 `stage_0`、`stage_1`…
  - 含「罩」或 `fair`（不区分大小写）→ 整流罩（`fairing_L/R`）
  - 例：级名为「一级」时写 `"一级"`；整流罩写 `"整流罩"`；助推器写 `"助推器"`。
- 多个航警区 = 多个数组项；`B)/C)` 必须覆盖对应残骸的**到达时刻**，否则 `window.py` 会报"无可行窗口"。

### 3.3 `observers` / `grid` / `dem`

```jsonc
"observers": [{"id": "obs1", "name": "...", "lat_deg": 37.761467,
               "lon_deg": 112.725219, "alt_m": 840.0, "note": "来源与核对"}],
"grid":      {"lon_min": 72, "lon_max": 136, "lat_min": 15, "lat_max": 55, "step_deg": 0.2},
"dem":       {"type": "SRTMGL1", "source": "AWS skadi", "sampling_m": 90, "horizon_radius_km": 200}
```

- `observers` 可多个；第一个会额外出全景图/太阳地影图。
- `grid` 目前只作记录：全国地图脚本的网格是**写死的中国范围**（见 §7）。
- `dem.horizon_radius_km` 决定地形遮挡搜索半径（默认 200 km）。

### 3.4 `vehicle`（未知参数的填法）

- 每级：`name / dry_kg / prop_kg` + 推力比冲（单台值 × `engines` 或 `phases[].thrusters[].n`）。
- **单工况**用 `engines` + `thrust_*`/`isp_*`；**多工况**（如二级主机+游机）用 `phases`：
  `end_met` 是绝对 MET，`emit_event: true` 会生成"XX关机"事件。
- 末级关机：已知程序关机时刻写 `seco_met`；否则留 `null`（按推进剂耗尽）。
- 抛罩：`guidance.t_fairing` 优先；只写 `vehicle.fairing.sep_met` 也能生效（v2 起有回退）。
- 残骸：`vehicle.debris` 里给每类 `cda`（m²）与 `extra_mass_kg`（栅格舵/回收装置等）；
  不知道就先用同类量级，反演只看落点、不看 cda 的细节。
- 并联助推器：`boosters` 块（`count / dry_kg_each / prop_kg_each / thrusters / jettison_met`）。

### 3.5 `guidance` 与 `numerics`

```jsonc
"guidance": {"mode": "shooting_plane", "azimuth_deg": 188.0,
             "t_v": 7.76, "t_p": 169.08, "pitch_power": 1.056,
             "t_fairing": 206.0, "fairing_divert_mps": 4.0,
             "plane_keeping": false, "i_target_deg": null},
"numerics": {"mode": "accurate", "dt": 0.2, "dt_legacy": 0.02,
             "sample_dt": 0.5, "debris_dt": 1.0, "debris_mode": "accurate"}
```

- `azimuth_deg` 初值：用发射场 → 一级航警区质心的方位角（可用 `common.geo.initial_bearing_deg` 算），
  通常在正负几度内即可，反演会收敛。
- `t_v/t_p/pitch_power/pitch_end_deg/pitch_duration_s` 是**开环俯仰程序**，一般都要反演。
- **口径一致性**：反演与最终导出必须用同一口径（`--legacy` 或 `--accurate`），
  否则参数会"补偿"离散化误差，换口径后落点会跑偏。

### 3.6 可选：`inversion` / `mc` / `window`

```jsonc
"inversion": {
  "parameters": [
    {"name": "azimuth_deg", "path": "guidance.azimuth_deg", "bounds": [180, 196]},
    {"name": "t_v", "path": "guidance.t_v", "bounds": [4, 14]},
    {"name": "t_p", "path": "guidance.t_p", "bounds": [130, 210]},
    {"name": "pitch_power", "path": "guidance.pitch_power", "bounds": [0.8, 1.6]},
    {"name": "t_fairing", "path": "guidance.t_fairing", "bounds": [180, 240]}   // 可选
  ],
  "weights": {"zone_km": 10.0, "orbit_alt_km": 1.0, "orbit_i_deg": 20.0},
  "de": {"maxiter": 20, "popsize": 10, "seed": 42}
}
```

- **边界必须按本任务改**（默认值是 CZ-2D 的）。边界给宽了收敛慢，给窄了会卡在边界上。
- 路径语法：点号分隔，数组用下标（`vehicle.stages.0.prop_kg`、`vehicle.stages.-1.seco_met`）。
- 未知量优先级：方位角 → 俯仰程序 → 抛罩时刻 → 末级关机时刻 → 推进剂/推力（最后才动，容易与别的量耦合）。
- `mc.variations` 的 `path` 必须存在于配置中，否则 `get_path` 抛 KeyError。
- `window.margin_s` 默认 60 s。

## 4. 先"粗跑"一遍，判断参数是否离谱（1 分钟）

```powershell
python scripts\export.py --config config.json
# 看输出与 sim\落点与射程.csv、sim\弹道事件表.csv：
#   inside_zone / dist_to_boundary_km 是否入区、余量多少
#   SECO 高度/速度是否接近参考轨道
```

落点差 10–100 km 很正常（参数未反演）；**差 >500 km 或飞不出去**，先查：
方位角方向反了、推力/推进剂少一个数量级、`t_p/pitch_end` 符号错、级数/分离时刻错。

## 5. 反演率定（2–10 分钟）

```powershell
python scripts\invert.py --config config.json --jobs 8 --maxiter 20 --popsize 10
# 想快：--quick（8 代）；要复现旧口径：--legacy
```

- 目标函数 = 各航警区落点罚项（区外按距离×10，未入区额外 +50）+ 参考轨道罚项（高度×1、倾角×20）。
- 完成后写 `sim\反演参数.json` / `sim\nominal_params.json`；**把最优点回填到 `config.json`**，
  这样后续换口径/重跑可复现（反演结果是可选的 `--params` 输入）。
- 收敛判据：`obj < ~1` 说明落点基本入区；若卡在几十，检查 `stage_hint`、多边形、或把 `t_fairing` 也加入反演。

## 6. DEM（天空轨迹需要）

```powershell
python scripts\fetch_dem.py --config config.json      # 观测点 200 km + 发射场 + 航警区
```

- 从 AWS skadi 下载（1″，约 25 MB/瓦片）；**国内网络可能很慢**（实测 ~11 KB/s），
  可换网络/代理，或在别的机器下完把 `data\dem\hgt\` 整目录拷来（瓦片级幂等）。
- 没有 DEM 也能跑：天空轨迹会打印警告并退化为平地遮挡角 −0.1°，**不能用于正式结论**。

## 7. 全流程 + 验收

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\run_all.ps1 -SkipDem -MC 250 -Jobs 8
# 或分步：export → window → plot_sky → plot_sky_refstyle → plot_map → mc → make_kml → qc
```

| 验收项 | 命令/文件 | 通过判据 |
|---|---|---|
| 文件齐全/CSV/PNG/KML | `python scripts\qc.py --config config.json` | 除"报告未撰写"外全 PASS |
| 落点入区 + 余量 | `sim\落点与射程.csv` | `inside_zone=true`，`dist_to_boundary_km` > 1–2 km |
| 落点时刻在航警有效期内 | `qc.py` 第 7 项 | PASS |
| 发射窗口非空 | `report\发射窗口计算.json` | duration > 0 |
| 入轨合理 | `sim\入轨根数.json` | e<0.05、150<h<2000 km、P>4000 s |
| 天空轨迹 | `sky\sky_summary.json` | 首次出现/最大/消失单调，遮挡角 −1…+10° |

最后按 `prompts/06_自检与验收清单.md` 写 `report\弹道仿真报告.md`（人工环节，qc 会一直提示）。

## 8. 三个"带本任务定制常量"的脚本（换任务必看）

| 脚本 | 写死了什么 | 换任务怎么办 |
|---|---|---|
| `scripts\make_kml_refstyle.py` | `T0`（26 行）、`SITE`（27 行）、`STAGE_STYLE` 阶段标题（29–34）、`DEBRIS_STYLE` 名称（35–39）、`ZONES` 两个航警区多边形/时间/名称（40–54） | 改这几处常量，或 `-SkipKml` 跳过；需要我参数化（从 `sim/notam_zones.json` 读）可以说 |
| `scripts\make_appearance_terrain_map.py` | 首次/最大/消失方位与仰角 `AZ_FIRST/AZ_MAX/AZ_LAST/EL_FIRST/EL_MAX`（42–46）、出现扇区 `280–360°`（169 行） | 按 `sky\sky_summary.json` 的 `first_appearance/max_elevation/last_visible` 改 6 个常量；或 `-SkipAppear` 跳过 |
| `scripts\plot_map.py` | 中国网格 `72–136°E / 15–55°N`、国界/省界/城市掩膜 | 中国任务直接可用；非中国任务改顶部 4 个网格常量并替换 `common\boundaries` 的掩膜，或 `-SkipMap` |

其余脚本（export/window/mc/invert/plot_sky/plot_sky_refstyle/qc/regression_check/bench）**完全配置驱动**，
换任务不需要改代码。

## 9. 常见坑（按出现频率排序）

1. **多边形写反**：`[lat, lon]` 写成 `[lon, lat]` → 落区跑到南极/判定必失败。
2. **T0 忘记 Z、或用了北京时**：所有时刻、太阳高度、窗口全错。
3. **`stage_hint` 不匹配级名**：落区不参与反演，`window` 也算不出窗口。
4. **`seco_met` 与 `prop_kg` 矛盾**：先耗尽就分离、到点关机就截断，二者只能一个决定末端。
5. **反演与导出用了不同口径**：参数"补偿"了离散化误差，换口径落点跑偏（v1↔v2 相差可达 ~1 km）。
6. **观测点坐标笔误**：天空轨迹的方位/仰角全错；先用 GE/多源核对。
7. **航警 B)/C) 没覆盖残骸到达时刻**：`window.py` 输出"无可行窗口"。
8. **DEM 缺失/瓦片不全**：天空线退化为 −0.1°，出现时刻偏早（本例差 ~80 s）。
9. **非中国任务直接跑全国地图**：网格/国界是中国范围，图会空白或错位。
10. **KMZ 直接打开看到旧航警区**：因为 §8 的 `ZONES` 写死，先改常量或跳过。

## 10. 时间预算（熟手）

| 环节 | 耗时 |
|---|---|
| 资料检索 + 配置 | 30–60 min（检索占大头） |
| 粗跑 + 调参 | 5–15 min |
| 反演（20×10，8 进程） | 2–10 min |
| 全流程（含 MC 250、地图、KML） | 1–2 min |
| 报告撰写 | 1–2 h |

```powershell
# 一条命令验证"快了多少、精度差多少"
python scripts\bench.py all --config config.json --reference <旧工程>\sim
```
