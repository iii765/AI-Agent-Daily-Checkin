@echo off
chcp 65001 >nul
title AI 客户端每日自动签到领积分 (MiniMax Code / WorkBuddy / TRAE CN)
cd /d "%~dp0"
echo ============================================================
echo   正在执行 AI 客户端每日签到 (MiniMax Code / WorkBuddy / TRAE CN)
echo ============================================================
python "%~dp0auto_checkin.py" --force
echo.
echo 执行完毕！详细历史记录已保存至 logs\checkin_history.json
pause
