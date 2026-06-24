@echo off
REM ============================================================
REM fw-keypool one-click launcher (double-click to run)
REM Runs start.py: New API(3000) + sticky_proxy(3001) + register + sync
REM ============================================================

REM Switch to this bat's directory (fw-keypool project root)
cd /d "%~dp0"

REM Terminal UTF-8 (for Chinese/emoji output from python)
chcp 65001 >nul

REM Find python: prefer 'python', fallback 'py'
where python >nul 2>nul
if %errorlevel%==0 (
    set "PY=python"
) else (
    where py >nul 2>nul
    if %errorlevel%==0 (
        set "PY=py"
    ) else (
        echo [ERROR] Python not found. Please install Python 3.11+ and add to PATH.
        pause
        exit /b 1
    )
)

echo ============================================================
echo  fw-keypool one-click launcher
echo  Python: %PY%
echo  Cwd:    %cd%
echo ============================================================
echo.

REM Run start.py (-X utf8 ensures UTF-8 output)
"%PY%" -X utf8 start.py %*

echo.
echo ============================================================
echo  start.py finished (press any key to close)
echo ============================================================
pause >nul
