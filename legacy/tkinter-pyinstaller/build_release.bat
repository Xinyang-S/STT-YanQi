@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo ========================================
echo   言栖 v0.5.0 - 一键发布 (EXE + 模型 + ZIP)
echo ========================================
echo.

echo [1/3] 编译 EXE (需要 1-2 分钟)...
echo.
call build.bat
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [FAIL] EXE 打包失败
    pause
    exit /b 1
)

echo.
echo [2/3] 检查模型...
if not exist "models\sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17\model.int8.onnx" (
    echo   [WARN] 未检测到 SenseVoice 模型, ZIP 里也不会有
    echo   下载: https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
    echo   解压到: models\sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17\
    echo.
    echo   继续打包 (不含模型, 用户需要手动下载)?
    choice /c YN /n /m "Continue? "
    if errorlevel 2 exit /b 1
)

echo.
echo [3/3] 打包 ZIP (解压即用)...
echo.
python build_release.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [FAIL] ZIP 打包失败
    pause
    exit /b 1
)

echo.
echo ========================================
echo   发布完成!
echo   产物: YanQi-v0.5.0.zip
echo   下一步: 上传到 GitHub Releases
echo ========================================
explorer .
pause
