# 模块接口契约（团队共享）· v4.0

> 本文档的 v3 部分仍然有效（路径约定、环境变量、数据类、内置库、引擎脚本约定）；
> **v4 的增量接口见文末「v4 增量接口」**。更完整的架构说明见 文档\07_架构与维护.md。

> 目标：用户双击 exe → 填最少信息 → 自动补全缺失数据 → 一键出全部成果。全程不需要 AI。
> 本文件是分工与接口的唯一权威，改动接口需先通知 Lead（lead）。

## 目录结构（固定）

```
F:\DeepSeek工作区\火箭弹道工具箱_v3.0\
├─ app\                      ← 应用层（Python 包，PyInstaller 打包对象）
│  ├─ main.py                ← 入口（freeze_support → paths.setup → GUI）
│  ├─ INTERFACES.md          ← 本文件
│  ├─ core\                  ← 无 GUI 依赖的核心逻辑（可 headless 测试）
│  │  ├─ paths.py            ← 资源/工作区路径解析（开发态 & 冻结态）
│  │  ├─ model.py            ← MissionInput / Resolution / BuildResult 数据类
│  │  ├─ notam_parse.py      ← 航警原文 / 经纬度 / 多边形解析
│  │  ├─ infer.py            ← 缺失数据推断引擎（T0/方位角/轨道/箭型/高程…）
│  │  ├─ config_build.py     ← MissionInput → config.json + 数据体检报告
│  │  ├─ autoplan.py         ← 自动生成 inversion / mc / window 计划
│  │  ├─ dem_manager.py      ← DEM 瓦片扫描/导入/下载/粗网格回退（owner: dem-data）
│  │  ├─ coarse_dem.py       ← 内嵌 0.02° 全国高程网格读取（owner: dem-data）
│  │  ├─ runner.py           ← 流水线执行器（进度/日志/取消）+ runpy 调用 engine 脚本
│  │  ├─ report.py           ← 自动中文技术报告生成（owner: report）
│  │  └─ qc_summary.py       ← qc_report.json → 人话结论
│  ├─ gui\                   ← Tkinter 界面（owner: gui）
│  ├─ data\                  ← 内置数据库 JSON + 粗网格 npz（owner: db-research / dem-data）
│  ├─ assets\                ← 图标等
├─ engine\                   ← v2.0 引擎（common/ scripts/ data/ne viz/files templates）
│  ├─ common\  scripts\  data\  viz\  templates\
│  └─ tools\                 ← v3 新增构建工具（如 build_coarse_dem.py）
├─ build\                    ← PyInstaller spec / 构建脚本（owner: lead）
├─ tests\                    ← 回归与自检脚本
├─ 作品\                     ← 默认工作区根（每个任务一个子目录）
└─ 文档\                     ← 用户手册
```

## 路径与环境变量（核心约定，所有脚本都依赖）

| 变量 | 含义 | 由谁设置 |
|---|---|---|
| `RK_ENGINE` | 引擎根目录（含 `common/` `scripts/` `data/ne/` `viz/files/`） | `paths.setup_env()` |
| `RK_ROOT` | **当前任务工作区**：`config.json` 与 `sim/ sky/ map/ viz/ report/ uncertainty/` 都在这 | `runner` 每步设置 |
| `RK_DEM` | SRTM 瓦片缓存根（可写，含 `gz/ hgt/ cache/ srtm_tiles.json`） | `paths.setup_env()` |
| `MPLBACKEND` | `Agg`（无窗口绘图） | `paths.setup_env()` |
| `MPLCONFIGDIR` | 可写字体缓存目录 | `paths.setup_env()` |
| `PYTHONIOENCODING` | `utf-8` | `paths.setup_env()` |

`app/core/paths.py` 提供（**唯一路径来源，任何模块不要自己拼路径**）：

```python
import app.core.paths as P
P.APP_ROOT      # 开发态=项目根目录；冻结态=sys._MEIPASS
P.ENGINE_DIR    # APP_ROOT/engine
P.APP_DATA      # APP_ROOT/app/data
P.ASSETS        # APP_ROOT/app/assets
P.runtime_base()      # 可写根：开发态=项目根；冻结态=exe 所在目录（不可写时回落 %LOCALAPPDATA%\RocketKit）
P.work_root()         # 默认作品根：runtime_base()/作品（可由 settings.json 覆盖）
P.dem_dir()           # SRTM 瓦片根：开发态=engine/data/dem；冻结态=%LOCALAPPDATA%\RocketKit\dem
P.settings()          # dict：{"work_root":..., "dem_dir":..., "jobs":..., "mc_n":..., "last_workdir":...}
P.save_settings(d)
P.setup_env()         # 设置上表环境变量 + 建目录 + matplotlib Agg；返回 None
P.ensure_syspath()    # sys.path 插入 ENGINE_DIR 与 ENGINE_DIR/scripts（幂等）
P.open_in_explorer(path)
```

## 数据类（`app/core/model.py`）

```python
@dataclass
class NotamInput:
    id: str = ""
    b_utc: str = ""            # ISO "...Z"
    c_utc: str = ""
    q_line: str = ""
    fir: str = ""
    polygon_latlon: list = field(default_factory=list)   # [[lat, lon], ...]
    stage_hint: str = ""       # 一级 / 二级 / 助推器 / 整流罩
    raw_text: str = ""

@dataclass
class ObserverInput:
    id: str = "obs1"
    name: str = ""
    lat_deg: float | None = None
    lon_deg: float | None = None
    alt_m: float | None = None
    note: str = ""

@dataclass
class MissionInput:
    name: str = ""
    rocket_id: str = "auto"        # rockets.json 的 id；"auto"=按型号名模糊匹配；"custom"=用 vehicle_overrides
    rocket_label: str = ""         # 用户显示名/型号（如 "长征二号丁"）
    site_id: str = "auto"          # launch_sites.json 的 id；"auto"=按名称/坐标匹配
    site_label: str = ""
    site_lat: float | None = None  # 自定义发射场（site_id=="custom"）
    site_lon: float | None = None
    site_alt: float | None = None
    t0_text: str = ""              # 任意格式；空 = 自动推断
    tz_hint: str = "auto"          # auto/UTC/BJT
    serial: str = ""
    payload_name: str = ""
    payload_kg: float | None = None
    orbit_alt_km: float | None = None
    orbit_i_deg: float | None = None
    notams_text: str = ""                     # 用户粘贴的航警原文
    notams: list[NotamInput] = field(default_factory=list)   # 已解析/手工编辑
    notams_mode: str = "auto"      # auto / provided / predict（无航警→按标称弹道预测落区）
    observers: list[ObserverInput] = field(default_factory=list)
    mode: str = "accurate"         # accurate / legacy
    modules: dict = field(default_factory=dict)   # 见下
    mc_n: int = 0
    jobs: int = 0
    dem_radius_km: float = 200.0
    vehicle_overrides: dict = field(default_factory=dict)  # 点号路径 → 值
    inversion_maxiter: int = 0     # 0=按 autoplan
    inversion_popsize: int = 0
    notes: str = ""

DEFAULT_MODULES = {"dem": True, "invert": True, "export": True, "window": True,
                   "sky": True, "appear": True, "map": True, "mc": False,
                   "kml": True, "qc": True, "report": True}

@dataclass
class Resolution:       # 数据体检单行
    field: str          # "mission.t0_utc"
    label: str          # "起飞时刻 T0"
    value: str          # "2026-11-18T22:50:00Z"
    source: str         # user / db / infer / default / derived
    confidence: str     # 高 / 中 / 低
    note: str = ""

@dataclass
class BuildResult:
    config: dict                 # 可直接写 config.json 的完整配置
    path: str                    # config.json 落盘路径
    resolutions: list[Resolution]
    gaps: list[Resolution]       # source in (infer/default) 或 confidence=="低" 的项
    warnings: list[str]
    needs_refine_t0: bool        # True → runner 先 export 再按窗口反推 T0 重建配置
```

## 内置数据库（`app/data/*.json`，owner: db-research）

统一带 `schema` / `updated` / `sources`。字段：

- `rockets.json`：`{"schema":"rocketkit.rockets.v1","rockets":[{id,name,aliases[],class,liftoff_mass_kg,leo_kg,sso_kg,default_payload_kg,typical_orbits[{name,alt_km,i_deg}],sites[],guidance_defaults{...},vehicle{...与 config.vehicle 同构},numerics{mode},confidence,note,sources[]}]}`
  - `vehicle` 必须与 `templates/config_cz2d.json` 的 `vehicle` 同构（stages[].dry_kg/prop_kg/thrust/isp/pitch_end_deg/cda_attached_m2/phases…、fairing、debris、ascent_cda_scale）。
  - `guidance_defaults`：`t_v/t_p/pitch_power/t_fairing/fairing_divert_mps/plane_keeping/yaw_tau/i_target_deg`；`azimuth_deg` 允许为 `null`（由发射场→一级落区方位自动算）。
- `launch_sites.json`：`{"schema":"rocketkit.sites.v1","sites":[{id,name,aliases[],lat_deg,lon_deg,alt_m_ellipsoid,country,region,vehicles[],confidence,coord_note,sources[]}]}`
- `cities.json`：`{"schema":"rocketkit.cities.v1","cities":[{name,province,lat_deg,lon_deg,alt_m,aliases[]}]}`（≥120 个地级市，供观测点预置与名称查找）
- `payloads.json`：`{"schema":"rocketkit.payloads.v1","payloads":[{id,name,aliases[],mass_kg,min_kg,max_kg,confidence,note,sources[]}]}`（神舟/天舟/遥感/高分/北斗/通信/试验/微小卫星…，mass_kg 为单星或组合总质量）
- `orbits.json`：`{"schema":"rocketkit.orbits.v1","orbits":[{id,name,alt_km,i_deg,type,confidence,note}]}`（SSO500/SSO700/LEO400/LEO500/MEO2000/GTO/IGSO…）

## 核心 API 速查

```python
# notam_parse.py
parse_notams(text) -> (list[dict], list[str])      # dict 同 NotamInput；warnings
parse_latlon(text) -> dict                         # {"lat":..,"lon":..,"alt_m":..or None,"format":"dms|dec"}
parse_polygon(text) -> (list[[lat,lon]], list[str])# 支持 "N380800E1075600-N375700E1080000-..." 与 "lat,lon;" 混排
extract_qline_circle(q_line) -> (lat, lon, radius_nm) | None

# infer.py（全部纯函数，输入 config/DB，输出 (值, source, confidence, note)）
parse_t0(text, tz_hint) -> datetime | None
infer_t0(notams, tz_hint) -> (datetime, source, confidence, note)
match_rocket(db, label) -> dict | None
match_site(db, label_or_latlon) -> dict | None
match_city(db, name) -> dict | None
build_vehicle(rocket_preset, payload_kg, overrides) -> (vehicle_dict, list[Resolution])
infer_reference_orbit(azimuth_deg, site_lat_deg, rocket_preset, user_alt, user_i) -> dict
guess_azimuth(site, zone_centroid) -> float
typical_guidance(rocket_preset) -> dict
refine_t0_from_window(t0_prov, arrival_t_met_s, notam_zone) -> (datetime, note)

# autoplan.py
plan_inversion(config, resolutions) -> dict        # 直接写入 config["inversion"]
plan_mc(config) -> dict | None                     # 直接写入 config["mc"]
describe_plan(config) -> list[str]                 # 给 GUI/日志看的中文计划摘要

# config_build.py
build_config(inp: MissionInput, workdir: str, logger=None) -> BuildResult
# 约定：config["mission"]["provenance"] = [Resolution as dict...]，config["mission"]["_v3"] = {...}

# dem_manager.py（owner: dem-data）
class DemManager:
    def __init__(self, dem_dir: str, logger=None)
    def required_tiles(self, config) -> list[str]
    def missing_tiles(self, config) -> list[str]
    def scan_and_import(self, roots: list[str]) -> int          # 从其它工作区/旧工程导入瓦片，返回导入数
    def download(self, tiles, progress=None, should_stop=None) -> dict  # {"ok":n,"failed":[..]}
    def prepare(self, config, allow_download=True) -> dict      # 一步到位：扫描→下载→建缓存→返回 {"mode":"srtm|coarse","tiles":n}
    def observer_alt(self, lat, lon) -> tuple[float|None, str]  # (高程, "srtm"|"coarse"|None)
    def dem_mode(self) -> str                                   # "srtm" | "coarse" | "none"

# coarse_dem.py（owner: dem-data）
class CoarseDEM:
    def __init__(self, npz_path=None)
    def sample(self, lat, lon) -> float
    def horizon_deg(self, lat, lon, az_deg, radius_km=200.0, step_km=1.0) -> float
    def available(self) -> bool

# runner.py
class RunOptions:      # 与 MissionInput.modules 对应
    modules: dict; mc_n: int; jobs: int; mode: str; allow_network: bool = False
class RunEvent:  level, step, message, progress(float|None), elapsed(float|None)
class PipelineRunner:
    def __init__(self, workdir, config_path, options, on_event=None, should_stop=None)
    def run(self) -> "RunResult"     # {"ok":bool,"steps":[{name,ok,seconds}],"artifacts":[...],"log":path}
#  步骤键：dem / invert / export / window / sky / appear / map / mc / kml / qc / report
#  必须：每步前 os.environ["RK_ROOT"]=workdir；输出 stdout 实时回调 on_event(level="log")
#  必须：支持 should_stop()→True 时终止（抛出内部异常→RunResult.ok=False, "cancelled"）
#  冻结态 multiprocessing 风险：先尝试 jobs；os.environ 里 RK_FROZEN=1 时 jobs 强制 1（见 spec）

# report.py（owner: report）
generate_report(workdir, config, build_result, log_tail="") -> str   # report/弹道仿真报告.md（+ .docx 可选）
generate_html_report(...) -> str | None

# qc_summary.py
summarize_qc(workdir) -> dict   # {"ok":bool,"n_pass":n,"n_fail":m,"items":[...],"advice":[str]}
```

## GUI 契约（owner: gui，界面只调用 core，不直接 import engine）

- 入口 `app/main.py`：`multiprocessing.freeze_support()` → `P.setup_env()` → `P.ensure_syspath()` → `from app.gui.main_window import launch; launch()`。
- 界面语言：**全中文**。窗口标题「火箭弹道工具箱 v3.0 · 离线版」。
- 主流程：① 任务信息 ② 航警（可粘贴原文/一键解析/或选"预测落区"）③ 观测点（可粘贴任何格式坐标/选城市/留空=跳过天空图）④ 运行选项 ⑤ 数据体检 → ⑥ 开始计算（进度 + 实时日志 + 可取消）→ ⑦ 结果页（要点摘要 + 打开成果目录 + 打开报告 + 打开 KMZ）。
- 数据体检表格列：字段 / 取值 / 来源 / 置信度 / 说明；来源色：user=绿、db=蓝、infer=橙、default=灰、低置信度=红底。
- 计算在后台线程执行，主线程只更新 UI（用 `queue.Queue` + `root.after(100,...)`）。
- 退出/取消必须安全：不阻塞、不留僵尸进程。
- 「在线补全」按钮（可选联网）：DEM 瓦片下载、航警在线检索（可后置，先留接口）。
- 兼容无网络：所有联网操作都必须有离线回退并明确提示。

## 分工与写作用域（避免冲突）

| 成员 | 文件/目录（**仅这些**） |
|---|---|
| lead | `app/core/*.py`（paths/model/notam_parse/infer/config_build/autoplan/runner/qc_summary）、`app/main.py`、`build/*`、`tests/*`、根目录文档 |
| engine-param | `engine/scripts/make_kml_refstyle.py`、`engine/scripts/make_appearance_terrain_map.py`、`engine/scripts/plot_map.py` |
| dem-data | `app/core/dem_manager.py`、`app/core/coarse_dem.py`、`engine/tools/build_coarse_dem.py`、`engine/common/dem.py`、`engine/scripts/fetch_dem.py`、`app/data/china_dem_*.npz` |
| db-research | `app/data/rockets.json`、`launch_sites.json`、`cities.json`、`payloads.json`、`orbits.json`、`tests/test_db_validate.py` |
| gui | `app/gui/**`、`app/assets/**` |
| report | `app/core/report.py`、`文档/**` |

**禁止**改别人作用域内的文件；需要改 → 发消息给 lead。

## 质量红线

1. 不得编造数据：DB 每条参数带 `sources`（URL + 访问日期）或 `note` 说明估值方法；标 `confidence`。
2. 冻结打包后**不得**依赖 cwd、不得依赖系统 Python、不得写引擎目录（只写工作区/`%LOCALAPPDATA%`）。
3. 所有新增代码 `python -m py_compile` 通过；核心模块要能 headless 导入（不 import tkinter）。
4. 中文输出一律 UTF-8（CSV 用 `utf-8-sig`）；禁止在代码里出现 emoji/特殊排版字符。
5. 回归：任一次改动后，用 `tests/run_regression.py` 验证 3 个历史任务（CZ-2D/CZ-2F/CZ-6A）结果不漂移。

## v3.4 增量接口（2026-11-26）

### 新增配置块

```json
"mission": {"profile": "standard|cz2f"},
"launch_window": {"mode": "auto|raan|ltan|free", "tolerance_deg": 0.2,
                  "search_hours": 48, "step_s": 20, "raan_deg": null,
                  "raan_epoch_utc": null, "ltan_h": null},
"station": {"enabled": true, "name": "天宫空间站", "norad_id": 48274,
            "source": "auto|bundled|online|tle|oem", "oem_path": "",
            "tle": ["...", "..."],
            "rendezvous": {"target_time_h": 6.5, "max_phasing_h": 13,
                           "dv_budget_mps": 300, "final_approach_s": 1800,
                           "final_approach_dv_mps": 10,
                           "max_phasing_orbits": 10, "min_phasing_orbits": 1}}
```

### 新增模块与步骤

- `DEFAULT_MODULES` 增加 `station`、`docking`（非 CZ-2F 自动跳过）。
- runner 步骤键：`station`（`engine/scripts/station_track.py`）、`docking`（`engine/scripts/rendezvous.py`）；
  顺序：`dem→invert→export→station→window→docking→sky→...`。
- `window.py` 输出新增 `orbit_window`（保留 v3.0 `model_window`/`practical_window`）。

### 新增内核 API

```python
# engine/common/orbit.py
parse_tle(text); TleSatellite(l1,l2).state(dt)
OemEphemeris(path).state(dt, frame="teme"|"eme2000")
gcrs_to_teme_matrix(dt); teme_to_gcrs_matrix(dt)
elements_from_state(r,v); state_from_elements(...)
j2_raan_rate_deg_day(a,e,i); raan_for_ltan(dt,h); ltan_from_raan(dt,raan)
azimuth_for_inclination(lat,alt,speed,i,dt,...); plane_from_launch(...)
lambert(r1,r2,tof); hohmann_dv(...); phasing_plan(...); cw_rendezvous(...)
propagate_rk4(r,v,dt); subpoint(dt,r); sunlit(dt,r); access_from_site(...)

# engine/common/station.py
resolve(cfg, t_hint, allow_network=False, logger=None) -> StationSource
StationSource.state(dt)/elements(dt)/info()/coverage()

# engine/common/geotiff.py
GeoTiffSource(path).inspect()/covers()/sample()/sample_many()

# app/core/dem_manager.py
import_dem(path, copy=False); custom_sources(); custom_plan(config);
remove_custom_source(id); status()["custom_sources"]
```

### GUI 契约增量

- 分页变为 7 个：①任务 ②航警 ③观测 ④运行选项 ⑤空间站（CZ-2F）⑥数据体检 ⑦运行与结果。
- `MainWindow._handle` 新增事件：`station`（显示空间站数据校验结果）、`dem`（显示高程导入结果）。
- `TabOptions.set_dem_status(text)`；`MainActionsMixin.import_dem_files()`。
- `TabStation.collect(inp)/load(inp)/on_show()/show_station_info(info)`。

---

## v4 增量接口（2026-11-26）

### 新增模块

| 模块 | 职责 |
|---|---|
| pp/core/userdb.py | 用户自定义库：箭型 / 参考轨道 / 时序模板。user_dir()=可写根下 user_data\；提供 lank_rocket / normalize_rocket / validate_rocket / rocket_metrics / list_rockets / save_rocket / delete_rocket / merge_rockets、轨道与模板的同类函数、export_bundle / import_bundle。**不导入 infer/GUI/numpy**。 |
| pp/core/summary.py | 通用成果汇总：key_nodes / flight_phases / trajectory_stats / crew_items / artifact_inventory / build_summary / generate_all，产出 sim\关键节点.csv、sim\飞行阶段.csv、sim\飞行环境.csv、sim\轨迹统计.json、sim\飞行环境关注项.json、
eport\任务摘要.md/.json、
eport\成果清单.md/.json。 |
| pp/gui/nav.py | 左侧八步向导（序号+标题+状态徽标）。StepNav(steps, on_select)，set_active(key) / set_badge(key,text,kind)。 |
| pp/gui/tab_vehicle.py | 步骤②：参考轨道来源选择、制导覆盖、发射时序（时间轴预览/模板）、箭型参数速览。 |
| pp/gui/dialog_rocket.py | 箭型参数编辑器（六页，可仅本次任务或保存到用户库）。RocketEditorDialog(parent, rocket=None) → 
esult / saved / mode。 |
| pp/gui/dialog_orbit.py | 参考轨道编辑器（模板/TLE/用户库）。OrbitEditorDialog(parent, orbit=None) → 
esult / saved。 |
| pp/gui/dialog_help.py | 帮助中心（主题列表+搜索）。show_help(parent, topic, topic_key)。 |

### MissionInput 新增字段（都有默认值，旧 JSON 可直接读）

`python
mission_type: str = "auto"        # auto / standard / crewed / return
custom_rocket: dict = {}          # 完整自定义箭型记录（空=按库匹配）
custom_rocket_mode: str = ""      # "" / "user_db" / "session"
ref_orbit_mode: str = "auto"      # auto / template / user / custom / tle
ref_orbit_name: str = ""
ref_orbit: dict = {}              # {alt_km,i_deg,e,raan_deg,argp_deg,ltan_h,epoch_utc,type,name}
ref_orbit_tle: str = ""
guidance_overrides: dict = {}     # 点号键：t_v/t_p/pitch_power/azimuth_deg/i_target_deg/...
auto_observer: bool = True        # 无观测点时用发射场
`

### config.json / _v3 新增键

- mission.mission_type、mission.custom_rocket、mission.reference_orbit_mode、
  mission.auto_observer；
- _v3.mission_type、_v3.auto_observer、_v3.custom_rocket、_v3.ref_orbit_mode；
- _v3.modules 新增 summary；
- launch_sequence 增加 	able（结构化时序：key/label/default/value/source）；
- mission.reference_orbit 可带 e / raan_deg / argp_deg / ltan_h（用户指定时）。

### 
unner.PipelineRunner 契约变化

- 步骤状态写入 
esult.steps[*]["state"]：完成 / 跳过 / 不适用 / 未启用（未勾选模块）/ 失败 / 取消，
  跳过原因在 ["reason"]；新增步骤键 summary。
- 步骤函数可调用 self._skip(reason) 声明按条件跳过，_run_step 会记为「跳过」而不是「完成」。
- 返回任务（
eturn_only / mission_type=return）的步骤计划为
  station, return, kml, summary, report, qc，其余上升段步骤以「不适用」入账。
- _finish() 会用最终步骤台账刷新一次通用成果。

### 引擎脚本增量

- engine/scripts/qc.py：新增「通用成果」检查（任务摘要/成果清单/关键节点/轨迹统计/飞行阶段）
  与「关键节点覆盖起飞/着陆」检查；返回任务分支也检查通用成果。
- engine/common/dem.py：自定义高程索引改按 utf-8-sig 读取（容忍 BOM）。

### GUI 契约变化（页面 → 主窗口）

- 主窗口暴露：self._pages[key]（mission/vehicle/notam/observer/station/options/health/run）、
  show_page(key)、
efresh_nav()、
efresh_summary()、set_status(text, kind)、
  
otify(text, kind)、edit_rocket(rec=None)、edit_orbit(rec=None)、manage_user_rockets()、
  manage_user_orbits()、export_user_bundle()、import_user_bundle()、show_help()。
- 页面契约：collect(inp) / load(inp) / on_show()；	ab_vehicle 额外有
  
efresh_from_rocket(force=False)、
ef_mode_cn()、has_custom_sequence()；
  	ab_mission 额外有 mission_type_cn()、	0_effective_text()、current_rocket_record()、
  custom_rocket 属性。
- 不再使用 self.tab_xxx 属性（v3 旧代码/脚本请改用 _pages["xxx"]）。
