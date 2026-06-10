@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo ========================================
echo   言栖 v0.5.0 - EXE 打包
echo ========================================
echo.

echo [1/3] 清理旧构建...
if exist "dist"  rmdir /s /q "dist"  2>nul
if exist "build" rmdir /s /q "build" 2>nul
if exist "*.spec" del /q "*.spec" 2>nul

echo [2/3] 开始打包 (需要 1-2 分钟)...
echo.

REM 找 sherpa-onnx 的原生 DLL 路径
for /f "delims=" %%i in ('python -c "import sherpa_onnx, os; print(os.path.join(os.path.dirname(sherpa_onnx.__file__), 'lib'))"') do set SHERPA_LIB=%%i
echo   sherpa-onnx lib: %SHERPA_LIB%

REM 注意: 模型不打包进 EXE. 原因:
REM   1) PyInstaller --onefile 解压 _MEIPASS 的 ONNX 模型, decode_stream 偶尔抛
REM      "invalid unordered_map<K, T> key" (C++ std::map::at 异常), 同代码 + 同模型
REM      从磁盘加载就 OK. 怀疑是 _MEIPASS 路径下文件 IO 行为差异.
REM   2) 模型 230MB 进 EXE 不合理 — 用户首次跑 download_model.bat 即可.
python -m PyInstaller ^
    --onefile ^
    --windowed ^
    --icon=app.ico ^
    --name=VoiceInput ^
    --add-data "app.ico;." ^
    --add-data "assets/app_icon.png;assets" ^
    --add-binary "%SHERPA_LIB%\sherpa-onnx-c-api.dll;sherpa_onnx/lib" ^
    --add-binary "%SHERPA_LIB%\sherpa-onnx-cxx-api.dll;sherpa_onnx/lib" ^
    --runtime-hook=hooks/hook-sherpa_onnx.py ^
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
