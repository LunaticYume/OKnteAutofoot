@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem SEC-10 方案B（2026-11-14 用户裁决）：面板以普通权限启动——HTTP/token 服务面
rem 不再随面板提权；确需管理员权限的操作（提权拉起 ok-nte 无头任务、向提权游戏
rem 窗口发送输入）由面板经一次 UAC 同意拉起的受限提权辅助进程完成。

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
