<#
    build.ps1 - build the standalone Windows package (PyInstaller, one-folder).

    Usage (from anywhere):
        powershell -ExecutionPolicy Bypass -File build\build.ps1
        powershell -ExecutionPolicy Bypass -File build\build.ps1 -NoClean
        powershell -ExecutionPolicy Bypass -File build\build.ps1 -SkipSmoke

    What it does:
      1. prepares build\.venv (created with --system-site-packages so numpy/scipy/
         matplotlib/pandas/Pillow/python-docx/sgp4 come from the base interpreter);
      2. installs PyInstaller into that venv if missing;
      3. runs PyInstaller with build\rocketkit.spec  ->  build\dist\火箭弹道工具箱\;
      4. copies README / 使用说明.txt next to the exe;
      5. smoke-tests the frozen exe (--env, then a quick CLI run).

    Output: build\dist\火箭弹道工具箱\火箭弹道工具箱.exe  (+ _internal\)
    Notes : keep this file ASCII-only (Windows PowerShell 5.1 reads .ps1 as ANSI).
#>
[CmdletBinding()]
param(
    [switch]$NoClean,
    [switch]$SkipSmoke,
    [switch]$QuickSmoke,
    [switch]$RecreateVenv
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath (Split-Path -Parent $MyInvocation.MyCommand.Definition)
$here = (Get-Location).Path
$root = Split-Path -Parent $here
$venvDir = Join-Path $here ".venv"
$venvPy = Join-Path $venvDir "Scripts\python.exe"
$distDir = Join-Path $here "dist"
$workDir = Join-Path $here "pyi-build"
$exeDir = Join-Path $distDir "火箭弹道工具箱"
$exePath = Join-Path $exeDir "火箭弹道工具箱.exe"

function Write-Step([string]$msg) {
    Write-Host ("[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg) -ForegroundColor Cyan
}
function Write-Ok([string]$msg) {
    Write-Host ("    OK  " + $msg) -ForegroundColor Green
}

Write-Step "1/6  build virtual environment"
if ($RecreateVenv -and (Test-Path -LiteralPath $venvDir)) {
    Write-Host "    removing old venv ..."
    Remove-Item -LiteralPath $venvDir -Recurse -Force
}
if (-not (Test-Path -LiteralPath $venvPy)) {
    $basePy = (Get-Command python -ErrorAction SilentlyContinue)
    if (-not $basePy) { throw "Python 3.10+ not found on PATH." }
    Write-Host ("    creating {0} (--system-site-packages)" -f $venvDir)
    & $basePy.Source -m venv --system-site-packages $venvDir
    if ($LASTEXITCODE -ne 0) { throw "python -m venv failed." }
}
& $venvPy -V
Write-Ok "venv ready"

Write-Step "2/6  check runtime dependencies"
& $venvPy -c "import numpy, scipy, matplotlib, PIL, pandas, docx, sgp4; print('    numpy', numpy.__version__, '| scipy', scipy.__version__, '| matplotlib', matplotlib.__version__, '| pandas', pandas.__version__)"
if ($LASTEXITCODE -ne 0) {
    throw "Missing Python dependencies. Run: python -m pip install -r requirements.txt"
}
Write-Ok "dependencies available"

Write-Step "3/6  ensure PyInstaller in build venv"
& $venvPy -c "import PyInstaller; print('    PyInstaller', PyInstaller.__version__)" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "    installing PyInstaller (mirror configured by pip) ..."
    $env:PIP_CACHE_DIR = Join-Path $here ".pipcache"
    & $venvPy -m pip install --no-cache-dir "pyinstaller>=6.3,<7"
    if ($LASTEXITCODE -ne 0) { throw "pip install pyinstaller failed." }
    & $venvPy -c "import PyInstaller; print('    PyInstaller', PyInstaller.__version__)"
}
Write-Ok "PyInstaller ready"

Write-Step "4/6  run PyInstaller"
$pyiArgs = @("-m", "PyInstaller", "--noconfirm",
             "--distpath", $distDir,
             "--workpath", $workDir)
if (-not $NoClean) { $pyiArgs += "--clean" }
$pyiArgs += (Join-Path $here "rocketkit.spec")
Write-Host ("    " + $venvPy + " " + ($pyiArgs -join " "))
& $venvPy @pyiArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
if (-not (Test-Path -LiteralPath $exePath)) { throw "No exe produced at $exePath" }
$exeSize = [math]::Round((Get-Item -LiteralPath $exePath).Length / 1MB, 2)
Write-Ok ("exe built: {0} ({1} MB)" -f $exePath, $exeSize)

Write-Step "5/6  add readme / usage notes next to the exe"
$copyMap = @{
    (Join-Path $here "使用说明.txt")          = (Join-Path $exeDir "使用说明.txt")
    (Join-Path $root "CHANGELOG.md")         = (Join-Path $exeDir "CHANGELOG.md")
}
foreach ($k in $copyMap.Keys) {
    if (Test-Path -LiteralPath $k) {
        Copy-Item -LiteralPath $k -Destination $copyMap[$k] -Force
        Write-Host ("    + " + (Split-Path -Leaf $copyMap[$k]))
    }
}

# README 是源码工程文档（开头写着"未做应用程序打包 / 只能源码运行"），
# 直接拷进包目录会自相矛盾，所以在包内副本前面加一段说明。
$readmeSrc = Join-Path $root "README.md"
if (Test-Path -LiteralPath $readmeSrc) {
    $banner = @"
> **本目录是 v4.3.0 的打包免安装版**：双击 ``火箭弹道工具箱.exe`` 即可运行，不需要安装 Python。
> 下面这份 README 是**源码工程文档**，其中"本版未做应用程序打包 / 只能源码运行"等说明只适用于
> 源码态交付；打包版的启动方式、命令行用法与数据目录见同目录 ``使用说明.txt``。

---

"@
    $text = [IO.File]::ReadAllText($readmeSrc, [Text.Encoding]::UTF8)
    $utf8bom = New-Object System.Text.UTF8Encoding($true)
    [IO.File]::WriteAllText((Join-Path $exeDir "README.md"), ($banner + $text), $utf8bom)
    Write-Host "    + README.md (with packaged-build banner)"
}
$totalMb = [math]::Round(((Get-ChildItem -LiteralPath $exeDir -Recurse -File | Measure-Object Length -Sum).Sum) / 1MB, 1)
Write-Ok ("package size: {0} MB" -f $totalMb)

Write-Step "6/6  smoke test the frozen exe"
if ($SkipSmoke) {
    Write-Host "    skipped (-SkipSmoke)"
} else {
    Write-Host "    --env ..."
    & $exePath --env
    if ($LASTEXITCODE -ne 0) { throw "frozen exe --env failed with exit code $LASTEXITCODE" }
    Write-Ok "--env passed"

    $smokeWd = Join-Path $here ("_smoke_" + (Get-Date -Format "yyyyMMdd_HHmmss"))
    if (Test-Path -LiteralPath $smokeWd) { Remove-Item -LiteralPath $smokeWd -Recurse -Force }
    if ($QuickSmoke) {
        Write-Host ("    quick CLI run (export,report) -> {0}" -f $smokeWd)
        & $exePath --cli --demo cz2d --workdir $smokeWd --modules export,report --no-invert
    } else {
        Write-Host ("    full CLI demo (all modules, includes inversion) -> {0}" -f $smokeWd)
        & $exePath --cli --demo cz2d --workdir $smokeWd
    }
    $rc = $LASTEXITCODE
    if ($rc -ne 0) { throw "frozen exe CLI smoke failed with exit code $rc" }
    Write-Ok ("CLI smoke passed; artifacts in {0}" -f $smokeWd)
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  BUILD OK" -ForegroundColor Green
Write-Host ("  exe : {0}" -f $exePath)
Write-Host ("  dir : {0}" -f $exeDir)
Write-Host "  copy the whole folder to any Windows x64 machine and run the exe."
Write-Host "============================================================" -ForegroundColor Green
