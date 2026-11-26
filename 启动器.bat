@echo off
rem ===========================================================================
rem  Rocket Ballistic Toolkit v4.0 - source launcher (needs Python 3.10+)
rem
rem  Double click = start GUI.  Also usable as CLI:
rem      launcher.bat --env
rem      launcher.bat --cli --demo cz2d --workdir "D:\tasks\demo"
rem
rem  NOTE: v4.0 is shipped as source only (no packaged exe yet).
rem        Requirements: Python 3.10+ with numpy/scipy/matplotlib/pillow
rem        (see requirements.txt:  python -m pip install -r requirements.txt)
rem  NOTE: keep this file ASCII-only (cmd.exe reads .bat in OEM code page).
rem ===========================================================================
setlocal
cd /d "%~dp0"

set "PYEXE="
set "PYARGS="

if defined RK_PYTHON if exist "%RK_PYTHON%" set "PYEXE=%RK_PYTHON%"

if not defined PYEXE (
    where py >nul 2>nul
    if not errorlevel 1 (
        set "PYEXE=py"
        set "PYARGS=-3"
    )
)

if not defined PYEXE (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        echo %%P | findstr /i "WindowsApps" >nul || (
            if not defined PYEXE set "PYEXE=%%P"
        )
    )
)

if not defined PYEXE (
    for %%D in (
        "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
        "C:\Python313\python.exe"
        "C:\Python312\python.exe"
        "C:\Python311\python.exe"
        "C:\Python310\python.exe"
    ) do if exist "%%~D" if not defined PYEXE set "PYEXE=%%~D"
)

if not defined PYEXE (
    echo.
    echo [ERROR] Python 3.10+ not found.
    echo         Install Python 3.10+ and dependencies:
    echo             python -m pip install -r requirements.txt
    echo         You can also set:  set RK_PYTHON=C:\Python311\python.exe
    echo.
    pause
    exit /b 1
)

"%PYEXE%" %PYARGS% -m app.main %*
set "RC=%ERRORLEVEL%"

if "%~1"=="" (
    if not "%RC%"=="0" (
        echo.
        echo [Program exited with code %RC%] Press any key to close...
        pause >nul
    )
)
exit /b %RC%
