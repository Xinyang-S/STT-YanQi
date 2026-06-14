from pathlib import Path
from PyInstaller.utils.hooks import collect_dynamic_libs

ROOT = Path(SPECPATH).resolve().parents[1]

hiddenimports = [
    "pyaudio",
    "pyperclip",
    "comtypes",
    "sherpa_onnx",
    "soundfile",
    "llama_cpp",
]

llama_binaries = collect_dynamic_libs("llama_cpp", destdir="llama_cpp/lib")


a = Analysis(
    [str(ROOT / "voice_backend.py")],
    pathex=[str(ROOT)],
    binaries=llama_binaries,
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "torch",
        "sklearn",
        "scipy",
        "pandas",
        "matplotlib",
        "tensorflow",
        "transformers",
        "huggingface_hub",
        "tensorboard",
        "IPython",
        "jupyter",
        "pytest",
        "funasr",
        "modelscope",
    ],
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
    name="vernest-backend",
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
    icon=[str(ROOT / "app.ico")],
)
