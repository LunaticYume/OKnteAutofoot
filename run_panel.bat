@echo off
chcp 65001 >nul
cd /d "%~dp0"

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo 正在请求管理员权限（ok-nte 以管理员运行，规划器需要同级权限才能驱动它）...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

set "EMBEDDED=D:\GamingAssistant\ok-nte\data\apps\ok-nte\python\python.exe"
if exist "%EMBEDDED%" (
    set "PY=%EMBEDDED%"
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo 未找到可用的 Python，请编辑本文件把 PY 指向你的 Python。
        pause
        exit /b 1
    )
    set "PY=python"
)

echo 正在启动 异环养成规划器 面板...
"%PY%" -m app.main
pause
