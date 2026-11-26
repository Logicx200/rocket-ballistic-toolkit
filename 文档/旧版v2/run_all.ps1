# 火箭弹道预报通用工具箱 v2.0 —— 一键流水线（Windows PowerShell）
# 用法：  .\run_all.ps1                 # 全流程（DEM 已下载时）
#         .\run_all.ps1 -SkipDem       # 跳过 DEM 下载
#         .\run_all.ps1 -MC 250 -Invert -Jobs 8
#         .\run_all.ps1 -Legacy        # 用 v1.0 数值口径复现已交付结果
#         .\run_all.ps1 -Accurate      # 用 v2.0 事件对齐口径（默认，推荐）
#         .\run_all.ps1 -Invert -MC 250 -Jobs 8
#         .\run_all.ps1 -SkipAppear    # 跳过「出现方位地形判读图」（换任务时该图有定制常量，见 prompts/09）
# 说明：所有产物写入本目录（sim/ sky/ map/ viz/ uncertainty/ report/）。
# v2.0 修正：v1 的 run_all.ps1 给 plot_sky*.py / make_appearance_terrain_map.py 传了
# 它们不接受的 --config（argparse 直接报错退出），本版所有脚本统一接受 --config。
param(
    [string]$Config = "config.json",
    [string]$Obs = "",              # 观测点 id（缺省取 config.observers[0].id）
    [int]$MC = 0,                   # >0 时运行蒙特卡洛（N 次）
    [int]$Jobs = 0,                 # 并行进程数（0=自动；1=串行）
    [switch]$SkipDem,
    [switch]$Invert,
    [switch]$SkipSky,
    [switch]$SkipAppear,            # 跳过 6b（出现方位地形判读图，含本任务定制常量）
    [switch]$SkipMap,
    [switch]$SkipKml,
    [switch]$Legacy,
    [switch]$Accurate,
    [switch]$Quick                  # 快速模式：跳过全景图/太阳地影图与全国地图自检
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

# ---- 环境 ----
$py = $env:RK_PYTHON
if (-not $py) {
    $c = Get-Command python -ErrorAction SilentlyContinue
    if ($c -and $c.Source -notlike "*WindowsApps*") { $py = $c.Source }
}
if (-not $py -or $py -like "*WindowsApps*") {
    throw "未找到可用的 python（Windows 应用商店占位符不算）。请设置 `$env:RK_PYTHON=<python.exe 完整路径> 后重试，例如：`n  `$env:RK_PYTHON='C:\Python312\python.exe'"
}
$env:PYTHONIOENCODING = "utf-8"
$env:MPLBACKEND = "Agg"
$env:MPLCONFIGDIR = Join-Path $here "_tmp\mplconfig"
New-Item -ItemType Directory -Force -Path $env:MPLCONFIGDIR | Out-Null
if (Test-Path (Join-Path $here "_pylibs")) {
    $env:PYTHONPATH = (Join-Path $here "_pylibs")
}

# ---- 数值口径 ----
$modeArg = @()
if ($Legacy) { $modeArg += "--legacy" }
elseif ($Accurate) { $modeArg += "--accurate" }
$jobsArg = @()
if ($Jobs -gt 0) { $jobsArg += @("--jobs", "$Jobs") }

Write-Host "== 工具箱流水线 v2.0 ==" -ForegroundColor Cyan
Write-Host "python: $py"
Write-Host "工作区: $here"
Write-Host "配置  : $Config"
Write-Host "计时  : 分步 + 合计（v1 无此输出）"

$script:StepTimes = @()
$total = [Diagnostics.Stopwatch]::StartNew()

function Step($name, $block) {
    Write-Host "`n=== $name ===" -ForegroundColor Yellow
    $sw = [Diagnostics.Stopwatch]::StartNew()
    & $block
    if ($LASTEXITCODE -ne 0) { throw "步骤失败：$name（退出码 $LASTEXITCODE）" }
    $sw.Stop()
    $script:StepTimes += [pscustomobject]@{ Step = $name; Seconds = [math]::Round($sw.Elapsed.TotalSeconds, 2) }
    Write-Host ("    [OK] {0}  {1:N2} s" -f $name, $sw.Elapsed.TotalSeconds) -ForegroundColor DarkGray
}

if (-not $SkipDem) {
    Step "1/10 下载 SRTM DEM（按观测点/落场/航警区）" { & $py scripts\fetch_dem.py --config $Config }
} else { Write-Host "`n=== 1/10 跳过 DEM 下载 ===" -ForegroundColor DarkGray }

if ($Invert) {
    Step "2/10 参数反演（DE，并行）" { & $py scripts\invert.py --config $Config @modeArg @jobsArg }
} else { Write-Host "`n=== 2/10 跳过反演（使用 config 中的参数）===" -ForegroundColor DarkGray }

Step "3/10 仿真与数据契约导出" { & $py scripts\export.py --config $Config @modeArg }
Step "4/10 发射窗口" { & $py scripts\window.py --config $Config }

if (-not $SkipSky) {
    $obsArg = @(); if ($Obs) { $obsArg += @("--obs", $Obs) }
    Step "5/10 天空轨迹（详细版）" { & $py scripts\plot_sky.py --config $Config @obsArg }
    Step "6/10 天空轨迹主图（参考样式）" { & $py scripts\plot_sky_refstyle.py --config $Config @obsArg }
    if (-not $SkipAppear) {
        Step "6b/10 出现方位地形判读图（局部放大）" { & $py scripts\make_appearance_terrain_map.py --config $Config @obsArg }
    } else { Write-Host "`n=== 6b/10 跳过出现方位地形判读图（-SkipAppear）===" -ForegroundColor DarkGray }
} else { Write-Host "`n=== 5–6/10 跳过天空轨迹 ===" -ForegroundColor DarkGray }

if (-not $SkipMap) { Step "7/10 全国最大高度角地图（含晨昏线）" { & $py scripts\plot_map.py --config $Config } }
else { Write-Host "`n=== 7/10 跳过全国地图 ===" -ForegroundColor DarkGray }

if ($MC -gt 0) {
    Step "8/10 蒙特卡洛不确定度（N=$MC，并行）" { & $py scripts\mc.py --config $Config --n $MC @modeArg @jobsArg }
} else { Write-Host "`n=== 8/10 跳过蒙特卡洛（加 -MC 250 运行）===" -ForegroundColor DarkGray }

if (-not $SkipKml) { Step "9/10 三维 Google Earth（KMZ/KML，参考样式）" { & $py scripts\make_kml_refstyle.py --config $Config } }
else { Write-Host "`n=== 9/10 跳过 KMZ ===" -ForegroundColor DarkGray }

Write-Host "`n=== 10/10 交付自检 ===" -ForegroundColor Yellow
$sw = [Diagnostics.Stopwatch]::StartNew()
& $py scripts\qc.py --config $Config
$qcCode = $LASTEXITCODE
$sw.Stop()
$script:StepTimes += [pscustomobject]@{ Step = "10/10 交付自检"; Seconds = [math]::Round($sw.Elapsed.TotalSeconds, 2) }
if ($qcCode -ne 0) {
    Write-Host "自检存在未通过项：report/ 报告需人工撰写；其余 FAIL 请按输出修正后重跑 qc.py。" -ForegroundColor Red
} else {
    Write-Host "`n全部完成。产物：sim/ sky/ map/ viz/ uncertainty/ report/" -ForegroundColor Green
}

$total.Stop()
Write-Host "`n== 分步耗时 ==" -ForegroundColor Cyan
$script:StepTimes | Format-Table -AutoSize
Write-Host ("合计 {0:N1} s" -f $total.Elapsed.TotalSeconds) -ForegroundColor Cyan
