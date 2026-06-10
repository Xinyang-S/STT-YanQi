# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ['pystray._win32', 'pynput.keyboard._win32', 'pyaudio', 'pyperclip', 'pillow', 'PIL._tkinter_finder', 'sherpa_onnx', 'soundfile']
hiddenimports += collect_submodules('pystray')
hiddenimports += collect_submodules('pynput')
hiddenimports += collect_submodules('sherpa_onnx')


a = Analysis(
    ['voice_input.py'],
    pathex=[],
    binaries=[],
    datas=[('app.ico', '.')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'sklearn', 'scipy', 'pandas', 'matplotlib', 'tensorflow', 'transformers', 'huggingface_hub', 'tensorboard', 'IPython', 'jupyter', 'pytest', 'funasr', 'modelscope'],
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
