# CHANGELOG — v1.0 → v2.0

> 原则：**物理方程、数据契约（CSV/JSON 列名与格式）、产物目录一律不变**；
> 只改数值实现、事件处理、并行方式与打包/联调缺陷。

## 一、数值内核 `scripts/dynamics.py`（重写）

| 变更 | 说明 | 实测影响 |
|---|---|---|
| 新增 `legacy` / `accurate` 双口径 | `AscentParams.numerics`；`simulate()` 分发 | legacy 与 v1 逐位一致；accurate 为收敛解 |
| 事件对齐步进 | 抛罩/推力段切换/级分离/SECO/助推器分离/采样时刻/推进剂耗尽都作为步长断点 | 事件时刻与 dt 无关；dt 可放大 25× |
| 质量与推进剂纳入 RK4 状态 | `dm/dt=-Σṁ(p_amb)`，四阶段分别求推力/流量 | 一阶 → 四阶；消除 +18.55 km 半长轴偏差 |
| 采样时刻对齐 | 0.5 s 采样点作为断点 | `trajectory.csv` 时间戳严格 0.5 s 整数倍 |
| 大气模型 | 导入期预计算分层常数 + `bisect`（公式逐位不变） | 更快 |
| 大地坐标 | Bowring 闭式 + 1 次 Newton（与 v1 迭代解差 < 5e-5 m） | 更快 |
| 右端闭包 | 每次仿真建一次，RK4 四阶段复用 | 更快 |
| 残骸 GMST | 每次传播只算一次（v1 每步算一次） | 更快 |
| 残骸落点 | accurate：高度过零二分（legacy 保留原步末判定） | 落点量化 40–75 m → <1 m |
| 兼容 | `simulate/vehicle_from_config/params_from_config/elements/...` 签名与键不变 | 下游零改动 |

## 二、脚本

| 文件 | 变更 |
|---|---|
| `scripts/export.py` | 新增 `--legacy/--accurate/--dt`；仿真加速（1.84→0.59 s）；CSV 格式不变 |
| `scripts/mc.py` | 重写：`ProcessPoolExecutor` 并行（`--jobs`，失败自动串行）、随机数主进程生成、进度带 ETA、`--legacy/--accurate`；N=250 187.5→11.3 s |
| `scripts/invert.py` | DE 并行（`--jobs`，模块级目标函数 + partial）、默认 accurate/dt=0.2、`--legacy` |
| `scripts/plot_map.py` | **修**：`import os`、`ROOT` 转 `Path`、配置路径、`--config`；**优化**：全网格 GEMM 化 + 阳光可见版预计算（16.9→9.5 s，CSV 逐位不变） |
| `scripts/plot_sky.py` | **修**：配置路径、`--config`、pandas 可选回退；DEM 遮挡走多方位批量实现 |
| `scripts/plot_sky_refstyle.py` | **修**：`--config`、观测点 id 从 `sky_summary.json` 推断、无 3″ 缓存时直接用 1″ 瓦片 |
| `scripts/make_appearance_terrain_map.py` | **修**：配置路径、`--obs/--config`、缓存缺失回退；**优化**：滑动窗口中值（21.8→4.6 s） |
| `scripts/make_kml_refstyle.py` | 接受并忽略流水线统一参数（`--config/--obs`） |
| `scripts/fetch_dem.py` | **修**：配置路径、`mission.launch_site`、去硬编码任务区、按圆/矩形并集规划瓦片（不再用笛卡尔积）、新增航警区包围盒 |
| `scripts/regression_check.py` | 默认 legacy 口径（与 v1 参考同口径），新增 `--accurate` |
| `scripts/bench.py` | **新增**：`speed / accuracy / verify` 三条基准，输出 `docs/benchmark_v2.json` |
| `common/dem.py` | **新增** 多方位批量遮挡角 `_direct_vec_many` + `_terrain_horizon_batch`（与逐方位版逐位相同） |
| `_tools/fetch_ne.py` | **新增**：Natural Earth 下载 + 中国范围裁剪（v1 缺此脚本导致 `plot_map` 无法运行） |

## 三、打包与流水线

| 文件 | 变更 |
|---|---|
| `run_all.ps1` | 重写：参数不再错传（原版给 `plot_sky*` 传它们不接受的 `--config`）、UTF-8 **带 BOM**（PowerShell 5.1 兼容）、新增 `-Jobs/-Legacy/-Accurate`、每步计时 + 合计 |
| `config.json` / `templates/config_cz2d.json` | 新增 `numerics` 段（mode/dt/dt_legacy/sample_dt/debris_dt/debris_mode） |
| `requirements.txt` | 补 `pyshp`（可选，仅 `fetch_ne.py` 用） |
| `data/ne/*.geojson` | **随包提供** Natural Earth 裁剪底图（public domain，约 4 MB） |
| `README.md` | 重写：v2 速览、口径说明、目录、依赖、验收 |
| `docs/优化分析报告_v2.md` | **新增**：问题清单、逐项优化、实测数据、风险与建议 |
| `docs/精度与性能验证_v2.md` | **新增**：逐位回归、dt 收敛、全流水线计时、复现命令 |
| `docs/benchmark_v2.json` | **新增**：基准原始结果 |

## 四、明确**未**改动

- 物理模型：USSA76、WGS-84、J2、推力随环境压力线性修正、阻力（地球相对速度）、
  平面导引/狗腿、残骸常值 CdA 弹道 —— 公式与参数来源不变；
- 数据契约：`sim/` 下 9 个文件的列名、含义、单位、`%.6f/%.3f` 格式；
- 产物与验收：`qc.py` 的检查项（25 项）与 DoD 清单；
- 绘图风格与图件版式。

## 五、升级注意事项

1. 旧工程若要用 v2 复现：`--legacy`（或 `numerics.mode="legacy"`）；
2. 若采用 accurate 作为正式口径：重新反演制导参数（`invert.py --jobs 8`）、
   用真实 SRTM 重跑天空轨迹、重跑 `mc.py` 更新 DRMS；
3. `plot_map.py` 需要 `data/ne/*.geojson`（已随包）；如删除，运行 `_tools/fetch_ne.py` 重建；
4. 首次跑 `run_all.ps1` 前确认执行策略：`powershell -NoProfile -ExecutionPolicy Bypass -File .\run_all.ps1`。
