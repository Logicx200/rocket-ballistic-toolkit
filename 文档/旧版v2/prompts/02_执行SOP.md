> 仅供公开信息分析与教学使用，非实际发射弹道。

# 执行 SOP（10 步）

> **v2.0 提示**：所有脚本都接受统一的 `--config config.json`；`export.py / invert.py / mc.py`
> 另接受 `--legacy / --accurate / --dt`（数值口径见 `README.md` §数值口径）。
> 也可以直接 `run_all.ps1 -SkipDem -MC 250 -Jobs 8` 一把跑完（每步有计时）。
> 新增 `python scripts/bench.py all --reference <旧工程>\sim` 可一条命令复核“快了多少、精度差多少”。

> 每步都写：命令 / 输入 / 产物 / 验收点 / 常见失败。全部在工具包根目录执行。

## 0. 准备
- 命令：`Copy-Item templates\config_cz2d.json config.json`
- 环境变量（每个新终端都要设）：
  `$env:PYTHONIOENCODING="utf-8"`、`$env:MPLBACKEND="Agg"`、
  `$env:MPLCONFIGDIR="<工作区>\_tmp\mplconfig"`；若依赖装在 `_pylibs/`：`$env:PYTHONPATH="<工作区>\_pylibs"`
- 验收：`python -c "import numpy, scipy, matplotlib; print('ok')"`
- 常见失败：`ModuleNotFoundError` → 装 `requirements.txt` 或复用 `_pylibs`；中文乱码 → 未设 `PYTHONIOENCODING`。

## 1. 信息检索（公开来源）
- 必查：航警原文（编号/B) C) 时间/Q 行/多边形）、发射时刻、发射场、箭型参数、参考轨道、载荷。
- 产物：`sources/来源清单.md`（每条 URL + 访问日期 + 关键原文摘录）、`sources/raw/`（网页/PDF 存档）。
- 验收：每个关键数值都能指到来源；冲突项写明“采用哪个、为什么”。
- 常见失败：只有二手转述 → 找 NOTAM 原文站点/官方发布；时间基准混淆（UTC vs 北京时）。

## 2. 配置
- 按 `prompts/01` 与 `prompts/07` 填 `config.json`：`mission / vehicle / guidance / notams / observers / grid`。
- 验收：`python -c "import json;json.load(open('config.json',encoding='utf-8'))"`；坐标/时间格式正确。
- 常见失败：多边形顶点顺序（lat,lon 顺序搞反）；`t0_utc` 忘加 Z；两级参数填成总推力。

## 3. DEM
- 命令：`python scripts/fetch_dem.py --config config.json`
- 产物：`data/dem/*.hgt.gz`（按观测点 200 km + 落区 + 走廊所需瓦片）
- 验收：脚本打印瓦片数/覆盖范围；`data/dem/cache/<obs>.npz` 生成。
- 常见失败：无网络；瓦片缺 1°×1° 边缘 → 观测点靠近整度线时要多取一圈。

## 4. 反演（可选但推荐）
- 命令：`python scripts/invert.py --config config.json`（急用 `--quick`）
- 输入：`config.inversion.parameters` 的边界；目标=落区硬约束+参考轨道软约束。
- 产物：`sim/反演参数.json`、`sim/nominal_params.json`
- 验收：目标函数值 < ~5 且落点全部落在对应区内；多解性说明写进 `反演参数.json`。
- 常见失败：DE 预算太小不收敛（加 `--maxiter/--popsize`）；参数边界把真值排除在外。

## 5. 仿真与数据契约
- 命令：`python scripts/export.py --config config.json`
- 验收：`sim/trajectory.csv` 行数 ≈ 采样时长/0.5；事件表含起飞/分离/抛罩/SECO；落点全部给区内判定。
- 常见失败：模型数值发散（改 dt/参数）→ `failures` 非空要停下检查。

## 6. 发射窗口
- 命令：`python scripts/window.py --config config.json`
- 验收：`report/发射窗口计算.json` 含 model/practical 两个区间（UTC+北京时）与逐区约束。
- 常见失败：窗口为空 → 检查航警时间与落点到达时刻是否矛盾。

## 7. 天空轨迹
- 命令：`python scripts/plot_sky.py --config config.json --obs <id>`（详细版）
  `python scripts/plot_sky_refstyle.py --config config.json --obs <id>`（参考样式主图）
  `python scripts/make_appearance_terrain_map.py --config config.json --obs <id>`
- 产物：`sky/天空轨迹_<obs>.png/.csv/.md`、`sky_summary.json`、`出现方位地形示意图_<obs>.png`。
- 验收：首次出现/最大高度角/消失三要素齐全；PNG 宽度 ≥1600；遮挡线平滑（无方波）。
- 常见失败：观测点坐标未核对；DEM 未下载；用规范口径画遮挡角出现平台（用稳健天空线）。

## 8. 全国最大高度角地图
- 命令：`python scripts/plot_map.py --config config.json`
- 产物：`map/全国最大高度角地图.png`、`晨昏线与阳光可见性.png`、`网格.csv`、`统计.json`。
- 验收：网格数=行×列；观测点数值与 `sky_summary.json` 一致；晨昏线/T0 太阳高度标注正确。
- 常见失败：网格太密导致超时（0.2° 已足够）；DEM 覆盖不足（海域/境外按 0 处理）。

## 9. 三维 Google Earth
- 命令：`python scripts/make_kml_refstyle.py --config config.json`
- 产物：`viz/弹道三维_含残骸坠落.kml/.kmz` + `viz/校验_KML.json`；GE 实测截图 2 张 + `viz/GE实测验证.md`。
- 验收：XML 可解析、gx:Track when/coord 等长、KMZ 自包含（doc.kml + files/*.png）、
  阶段彩线/事件/危险区/落点/时间动画/Tour 齐全；GE 打开后时间滑块覆盖 T0→落地。
- 常见失败：在 GE 里“另存到我的地点”导致线条变白（改用 NetworkLink）；图标路径写成绝对路径。

## 10. 不确定度 → 报告 → 自检
- 命令：`python scripts/mc.py --config config.json --n 250`（串行，约 3–6 min）
- 报告：`report/弹道仿真报告.md`（≥12 章，内嵌图件，含假设/精度/局限/来源/免责声明）。
- 自检：`python scripts/qc.py --config config.json` → 全 PASS 后交付。
- 常见失败：CSV 有 NaN/列缺失；PNG 宽度不足；报告缺来源或免责声明。
