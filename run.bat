@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

if exist "dist\VoiceInput.exe" (
    start "" "dist\VoiceInput.exe"
    echo 语音输入工具已启动，请查看系统托盘
) else (
    echo 未找到 dist\VoiceInput.exe，请先运行 build.bat 打包
)

REM 按任意键关闭此窗口
pause >nul
