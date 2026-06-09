"""PyInstaller runtime hook for sherpa_onnx.

sherpa_onnx 1.13.x 的原生 DLL 在 sherpa_onnx/lib/ 子目录.
PyInstaller --onefile 在 _MEIPASS 解压时, Windows 不会自动搜索子目录加载 DLL.
直接把 DLL 复制到 _MEIPASS 根目录, 让 Windows 找到.
"""
import os
import shutil
import sys

def _patch_sherpa_dll_path():
    if not getattr(sys, "frozen", False):
        return
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return
    candidates = [
        os.path.join(meipass, "sherpa_onnx", "lib"),
        os.path.join(meipass, "_internal", "sherpa_onnx", "lib"),
    ]
    for lib_dir in candidates:
        if not os.path.isdir(lib_dir):
            continue
        # 1. 复制所有 DLL 到 _MEIPASS 根
        for f in os.listdir(lib_dir):
            if f.endswith(".dll"):
                src = os.path.join(lib_dir, f)
                dst = os.path.join(meipass, f)
                if not os.path.exists(dst):
                    try:
                        shutil.copy(src, dst)
                    except OSError:
                        pass
        # 2. 同时加到 PATH
        os.environ["PATH"] = meipass + os.pathsep + os.environ.get("PATH", "")
        try:
            os.add_dll_directory(meipass)
        except (AttributeError, OSError):
            pass
        break

_patch_sherpa_dll_path()
