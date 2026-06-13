@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

REM 控制台启动（用于调试，查看日志输出）
python voice_input.py %*
pause
