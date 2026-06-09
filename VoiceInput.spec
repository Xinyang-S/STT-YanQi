# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules
import os
import sherpa_onnx

hiddenimports = [
    'pystray._win32', 'pynput.keyboard._win32', 'pyaudio', 'pyperclip',
    'PIL._tkinter_finder', 'sherpa_onnx', 'soundfile',
]
hiddenimports += collect_submodules('pystray')
hiddenimports += collect_submodules('pynput')
hiddenimports += collect_submodules('sherpa_onnx')

# sherpa-onnx 原生 DLL 必须在二进制中显式 bundle (子目录 PyInstaller 找不到)
sherpa_lib = os.path.join(os.path.dirname(sherpa_onnx.__file__), 'lib')
extra_binaries = [
    (os.path.join(sherpa_lib, 'sherpa-onnx-c-api.dll'), 'sherpa_onnx/lib'),
    (os.path.join(sherpa_lib, 'sherpa-onnx-cxx-api.dll'), 'sherpa_onnx/lib'),
]

# 模型 (230MB) 不打包进 EXE. 用户首次运行 download_model.bat 下载.
# 原因: PyInstaller _MEIPASS 下的 ONNX 模型 decode_stream 会抛
# "invalid unordered_map<K, T> key" (C++ std::map::at 异常), 磁盘加载正常.
datas = [('app.ico', '.')]

a = Analysis(
    ['voice_input.py'],
    pathex=[],
    binaries=extra_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['hooks/hook-sherpa_onnx.py'],
    excludes=['torch', 'sklearn', 'scipy', 'pandas', 'matplotlib', 'tensorflow',
              'transformers', 'huggingface_hub', 'tensorboard', 'IPython',
              'jupyter', 'pytest', 'funasr', 'modelscope'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='VoiceInput',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['app.ico'],
)
