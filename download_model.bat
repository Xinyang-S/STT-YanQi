@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File download_model.ps1
pause
