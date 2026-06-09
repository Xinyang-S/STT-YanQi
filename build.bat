@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo ========================================
echo   语音输入工具 v5.0 - EXE 打包
echo ========================================
echo.

echo [1/3] 清理旧构建...
if exist "dist"  rmdir /s /q "dist"  2>nul
if exist "build" rmdir /s /q "build" 2>nul
if exist "*.spec" del /q "*.spec" 2>nul

echo [2/3] 开始打包 (需要 1-2 分钟)...
echo.

python -m PyInstaller ^
    --onefile ^
    --windowed ^
    --icon=app.ico ^
    --name=VoiceInput ^
    --add-data "app.ico;." ^
    --add-data "models;models" ^
    --hidden-import=pystray._win32 ^
    --hidden-import=pynput.keyboard._win32 ^
    --hidden-import=pyaudio ^
    --hidden-import=pyperclip ^
    --hidden-import=PIL._tkinter_finder ^
    --hidden-import=sherpa_onnx ^
    --hidden-import=soundfile ^
    --collect-submodules=pystray ^
    --collect-submodules=pynput ^
    --collect-submodules=sherpa_onnx ^
    --exclude-module=torch ^
    --exclude-module=sklearn ^
    --exclude-module=scipy ^
    --exclude-module=pandas ^
    --exclude-module=matplotlib ^
    --exclude-module=tensorflow ^
    --exclude-module=transformers ^
    --exclude-module=huggingface_hub ^
    --exclude-module=tensorboard ^
    --exclude-module=IPython ^
    --exclude-module=jupyter ^
    --exclude-module=pytest ^
    --exclude-module=funasr ^
    --exclude-module=modelscope ^
    --clean ^
    voice_input.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ========================================
    echo   打包失败，请检查错误信息
    echo ========================================
    pause
    exit /b 1
)

echo.
echo [3/3] 检查模型...
if not exist "models\sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17\model.int8.onnx" (
    echo   警告: 未检测到 SenseVoice 模型
    echo   下载: https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
    echo   解压到: models\sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17\
)

echo.
echo ========================================
echo   打包成功!
echo   输出: dist\VoiceInput.exe
echo ========================================
explorer dist
pause
