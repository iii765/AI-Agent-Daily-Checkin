@echo off
chcp 65001 >nul
title 配置每日开机自动签到任务
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_schedule.ps1"
echo.
pause
