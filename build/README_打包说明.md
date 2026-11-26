# 打包说明（build\）· 火箭弹道工具箱 v4.3.0

本目录是 **PyInstaller 打包工具链**。源码工程的 README 里写的“未做应用程序打包”只描述
源码交付形态；打包产物由这里的脚本独立生成，不改动 `app/`、`engine/` 的运行逻辑。

```
build\
├─ entry.py            PyInstaller 入口（= 冻结态下的 python -m app.main）
├─ rthook_utf8.py      运行时钩子：控制台/管道统一 UTF-8
├─ version_info.txt    exe 版本资源（右键属性 → 详细信息）
├─ rocketkit.spec      打包规格（资源清单 + 隐藏依赖 + 排除项）
├─ build.ps1           一键构建：venv → 依赖 → PyInstaller → 说明文件 → 冒烟自检
├─ 使用说明.txt        随包交付给最终用户的使用说明
├─ .venv\              构建用虚拟环境（--system-site-packages，含 PyInstaller）
├─ pyi-build\          PyInstaller 中间产物
├─ dist\火箭弹道工具箱\  ← 打包产物（exe + _internal + 说明文档）
└─ .pipcache\          pip 缓存（若存在，可删）
```

## 一键重新打包

```powershell
# 在项目根目录执行；需要本机 Python 3.10+ 以及 requirements.txt 的依赖
powershell -ExecutionPolicy Bypass -File build\build.ps1

# 常用开关
powershell -ExecutionPolicy Bypass -File build\build.ps1 -NoClean      # 复用上次分析缓存（快）
powershell -ExecutionPolicy Bypass -File build\build.ps1 -QuickSmoke   # 冒烟只跑 export,report（快）
powershell -ExecutionPolicy Bypass -File build\build.ps1 -SkipSmoke    # 只打包不验证
powershell -ExecutionPolicy Bypass -File build\build.ps1 -RecreateVenv # 重建构建用 venv
```

脚本做的事：建 `build\.venv`（`--system-site-packages`，复用本机 numpy/scipy/matplotlib/
pandas/Pillow/python-docx/sgp4）→ 缺 PyInstaller 就装 → 跑 `rocketkit.spec` →
把 `README.md`（加“打包版”抬头）`/CHANGELOG.md/使用说明.txt` 放到 exe 旁 →
`--env` 自检 + 一次 CLI 全流程/快速流程。

产物：`build\dist\火箭弹道工具箱\`（约 209 MB，2175 个文件）。
把**整个文件夹**拷到任意 Windows 10/11 x64 机器即可运行，不需要装 Python。

## 规格要点（改依赖时看这里）

* **app 代码进 PYZ**；**engine 保持磁盘真实文件**（`_internal\engine\...`）：
  engine 脚本由 `runpy.run_path` 按路径执行，`common.plotstyle` / `common.notam` 等
  又按 `Path(__file__)` 定位资源，塞进 PYZ 会改变解析结果。
* **engine 脚本的第三方 import 静态分析看不到**（runpy 动态执行），所以
  `rocketkit.spec` 的 `hiddenimports` 必须显式列出：numpy、scipy.optimize/interpolate、
  matplotlib 各子模块、pandas、PIL.Image、docx、sgp4、tkinter*。新增 engine 依赖时同步补。
* **随包资源**：`app/data`（28 箭型/28 工位/343 城市/载荷/轨道/空间站 OEM/粗 DEM）、
  `app/assets`、`app/VERSION`、`engine/common`、`engine/scripts`、`engine/data/ne`、
  `engine/viz/files`、`engine/templates`、`engine/data/dem/{dem_mode.json,README.md}`、`文档\`。
* **不随包**：SRTM 瓦片缓存 `engine/data/dem/{gz,hgt,cache,custom}`（GB 级；冻结态可写
  DEM 目录固定在 `%LOCALAPPDATA%\RocketKit\dem`）、`tests/`、`作品/` 示例成果、
  `user_data/`（首次运行生成）。
* **排除项**：pandas/matplotlib 探测到的可选集成（pyarrow、tables、h5py、dask、xarray、
  numexpr、bottleneck、sklearn、zmq、Cython、win32com、cryptography 等）。
  整套依赖从 340 MB 降到约 209 MB；这些包缺失时 pandas/matplotlib 会按可选依赖降级。
* `console=True`：同一个 exe 同时支持图形界面与 `--cli / --env`，控制台窗口显示进度与日志。

## 本机打包时的验证记录（2026-11-27）

| 验证项 | 结果 |
|---|---|
| `火箭弹道工具箱.exe --env` | 通过：冻结运行=True、引擎存在、内置库 28/28/343/26/15、粗 DEM 9.2 MB |
| 常规发射全流程（含反演、天空轨迹、全国地图、KMZ、报告、QC） | 通过：QC **35/35 PASS**，退出码 0，44 个成果文件、34.8 s 左右完成（反演 45.7 s） |
| 载人交会任务（CZ-2F，含 OEM 星历/T0 反推/交会对接） | 通过：QC **37/37 PASS**，退出码 0 |
| 图形界面 | 通过：窗口正常打开、可响应，标题“火箭弹道工具箱 v4.3.0 · 离线版” |
| 源码/冻结一致性 | 同一组参数下输出一致（`--no-invert` 快速链路在源码态与冻结态同为 QC 30/31、退出码 1，行为相同） |

## 已知限制

* 冻结态多进程不可靠：`RK_FROZEN=1` 时反演/蒙特卡洛强制串行（与源码态单进程速度一致）。
* 中文字体用系统字体（Microsoft YaHei → SimHei → SimSun），不随包；缺字体时 matplotlib
  会退回 DejaVu Sans（图能出、中文会变方块）。
* 内置空间站 OEM 只覆盖 2026-11-20 ~ 2026-11-27。
* 未做代码签名，个别杀毒软件可能对 PyInstaller 产物误报，需加信任。
