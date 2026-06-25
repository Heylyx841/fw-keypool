@echo off
REM ============================================================
REM fw-keypool one-click stopper (double-click to run)
REM Stops New API(3000) + sticky_proxy(3001) + registrar + Playwright
REM ============================================================

REM Switch to this bat's directory (fw-keypool project root)
cd /d "%~dp0"

REM Terminal UTF-8 (for Chinese output)
chcp 65001 >nul
setlocal enabledelayedexpansion

echo ============================================================
echo  fw-keypool one-click stopper
echo  Cwd: %cd%
echo ============================================================
echo.

REM --- 1. Kill by listening port: 3000=New API, 3001=sticky_proxy ---
call :kill_port 3000 "New API"
call :kill_port 3001 "sticky_proxy"

REM --- 2. Fallback: kill any lingering new-api.exe by image name ---
taskkill /F /IM new-api.exe >nul 2>nul
if !errorlevel!==0 (echo ✓ 已关闭 new-api.exe 残留进程) else (echo • 无 new-api.exe 残留进程)

REM --- 3. Precise match: kill fw-keypool-spawned python & browsers ---
REM    Matches processes whose command line contains this project path,
REM    so your own Chrome/Edge will NOT be touched.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$root='%~dp0'.TrimEnd('\'); $esc=[regex]::Escape($root); $k=0; Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and ($_.CommandLine -match $esc) -and ($_.Name -match 'python|py\.exe|chrome|chromium|msedge|new-api') } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; Write-Host ('✓ 关闭 ' + $_.Name + ' PID=' + $_.ProcessId); $k++ } catch {} }; if ($k -eq 0) { Write-Host '• 无 fw-keypool 相关子进程' }"

echo.
echo ============================================================
echo  关闭完成（5 秒后自动关闭窗口，或按任意键立即关闭）
echo ============================================================
timeout /t 5 >nul
endlocal
exit /b 0

:kill_port
REM args: %1=port  %2=label
set "PORT=%~1"
set "LABEL=%~2"
set "HIT=0"
for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr /C:":%PORT% " ^| findstr "LISTENING"') do (
    taskkill /F /PID %%P >nul 2>nul
    if !errorlevel!==0 (
        echo ✓ 已关闭 %LABEL% (端口 %PORT%, PID %%P)
        set "HIT=1"
    )
)
if "%HIT%"=="0" echo • %LABEL% (端口 %PORT%) 未在运行
goto :eof
