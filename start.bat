@echo off
chcp 65001 >nul
cd /d "%~dp0"
title VibeDot

rem ---------- 查找 Python ----------
where python >nul 2>nul
if %errorlevel%==0 (
  set "PY=python"
) else (
  where py >nul 2>nul
  if %errorlevel%==0 (
    set "PY=py -3"
  ) else (
    echo [错误] 未找到 Python，请先安装 Python 3.10+ 并勾选 "Add to PATH"
    pause
    exit /b 1
  )
)

rem ---------- 可选: 监控的项目路径 ----------
set "ARGS="
if not "%~1"=="" set "ARGS=--project %~1"

rem ---------- 启动服务器 (独立窗口) + 打开控制台 ----------
start "VibeDot Server" cmd /k "%PY% pc\vibedot_server.py %ARGS%"
timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:8266

echo.
echo VibeDot 已启动: http://127.0.0.1:8266  (关闭 "VibeDot Server" 窗口即停止)
timeout /t 3 /nobreak >nul
exit /b 0
