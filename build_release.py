# -*- coding: utf-8 -*-
"""build_release.py - package dist/ into a ready-to-use ZIP.

Why: the user wants "download and use immediately". The EXE does not bundle
the model (PyInstaller _MEIPASS + ONNX hits a known std::map crash), so we
ship the model alongside the EXE in a single ZIP. User extracts and double-
clicks VoiceInput.exe - no extra download step.

Result layout (after extraction):
  YanQi-v0.5.0/
    VoiceInput.exe        79 MB
    models/
      .../model.int8.onnx 230 MB
    README.md
    LICENSE
    使用说明.txt
"""
import os, sys, shutil, zipfile
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
APP_VERSION = "v0.5.0"
DIST = ROOT / "dist"
# Use ASCII release dir name to avoid Windows shell encoding issues
RELEASE_DIR = ROOT / ("YanQi-" + APP_VERSION)
RELEASE_ZIP = ROOT / ("YanQi-" + APP_VERSION + ".zip")


def main():
    if not (DIST / "VoiceInput.exe").exists():
        print("[FAIL] missing " + str(DIST) + "/VoiceInput.exe; run build.bat first")
        return 1
    if not (DIST / "models").exists() or not list((DIST / "models").iterdir()):
        print("[FAIL] missing " + str(DIST) + "/models/ or empty; run download_model.bat first")
        return 1

    # 1) clean release dir
    if RELEASE_DIR.exists():
        print("[1/4] cleaning old release dir: " + RELEASE_DIR.name)
        shutil.rmtree(RELEASE_DIR)
    RELEASE_DIR.mkdir(parents=True, exist_ok=True)

    # 2) copy EXE
    print("[2/4] copying VoiceInput.exe")
    shutil.copy2(DIST / "VoiceInput.exe", RELEASE_DIR / "VoiceInput.exe")

    # 3) copy models — 只保留 SenseVoice 实际模型文件, 不要:
    #    - test_wavs/ (940KB 测试音频, 用户用不到)
    #    - export-onnx.py (开发脚本, 用户用不到)
    #    - sherpa-onnx 之外的其他目录 (如 ggml-base.bin 旧 Whisper 模型)
    src_model_dir = DIST / "models" / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
    if not (src_model_dir / "model.int8.onnx").exists():
        print("[FAIL] " + str(src_model_dir) + " 缺少 model.int8.onnx, 请跑 download_model.bat")
        return 1
    print("[3/4] copying SenseVoice model (only the essentials)")
    dst_model_dir = RELEASE_DIR / "models" / src_model_dir.name
    dst_model_dir.mkdir(parents=True, exist_ok=True)
    for fname in ["model.int8.onnx", "tokens.txt", "README.md", "LICENSE"]:
        src = src_model_dir / fname
        if src.exists():
            shutil.copy2(src, dst_model_dir / fname)
            print("        copied " + fname)

    # 4) copy docs
    for f in ["README.md", "LICENSE"]:
        src = ROOT / f
        if src.exists():
            shutil.copy2(src, RELEASE_DIR / f)
            print("        copied " + f)

    # Chinese user guide (use Unicode escapes to avoid encoding issues)
    readme_zh = RELEASE_DIR / "使用说明.txt"
    NL = chr(10)  # newline
    EQ = "=" * 41
    guide_text = (
        "言栖 (Yán Qī) — Windows 语音输入工具" + NL
        + "版本: " + APP_VERSION + " (pre-release < 1.0.0)" + NL
        + "作者: 孙欣阳 (Xinyang Sun)" + NL
        + "项目: https://github.com/Xinyang-S/STT-YanQi" + NL
        + NL
        + EQ + NL
        + "  使用方法" + NL
        + EQ + NL
        + NL
        + "1. 双击 VoiceInput.exe 启动" + NL
        + "2. 首次启动会要求授权麦克风 (Windows 安全提示)" + NL
        + "3. 按住 Right Ctrl 说话 → 松开自动识别 + 粘贴到光标" + NL
        + "4. Ctrl+Shift+F9 切换启用/禁用" + NL
        + "5. 设置中可调整: 开机启动 / 独占设备 / 麦克风 / 悬浮气泡" + NL
        + NL
        + EQ + NL
        + "  系统要求" + NL
        + EQ + NL
        + NL
        + "- Windows 10 / 11 (x64)" + NL
        + "- 麦克风 (至少 1 个; 独占功能需要 2 个)" + NL
        + "- VC++ 2015-2022 Redist (一般系统已自带)" + NL
        + NL
        + EQ + NL
        + "  首次启动若报“缺少运行库”" + NL
        + EQ + NL
        + NL
        + "安装 VC++ 运行时:" + NL
        + "  https://aka.ms/vs/17/release/vc_redist.x64.exe" + NL
        + NL
        + EQ + NL
        + "  卸载方式" + NL
        + EQ + NL
        + NL
        + "1. 关闭主窗口 (X), 右键托盘图标 → 退出" + NL
        + "2. 删除整个解压目录" + NL
        + "3. (可选) 删除 %USERPROFILE%/.voice_input/ 配置文件" + NL
        + NL
        + EQ + NL
        + "  模型文件" + NL
        + EQ + NL
        + NL
        + "models/ 目录包含 sherpa-onnx SenseVoice 多语种 int8 模型 (~230MB)." + NL
        + "EXE 不包含模型 (PyInstaller _MEIPASS 路径下 ONNX 加载异常);" + NL
        + "ZIP 自带, 解压即可用, 不用再下载." + NL
        + NL
        + "如果想换模型位置, 启动后在设置 → 通用 → 模型目录指定." + NL
    )
    readme_zh.write_text(guide_text, encoding="utf-8")
    print("        wrote 使用说明.txt")

    # 5) ZIP
    print("[4/4] packaging ZIP: " + RELEASE_ZIP.name)
    if RELEASE_ZIP.exists():
        RELEASE_ZIP.unlink()
    with zipfile.ZipFile(RELEASE_ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in RELEASE_DIR.rglob("*"):
            if f.is_file():
                arcname = f.relative_to(ROOT)
                zf.write(f, arcname)
    zsize = RELEASE_ZIP.stat().st_size
    print("        done: " + str(RELEASE_ZIP) + " (" + str(round(zsize / 1024 / 1024, 1)) + " MB)")
    print()
    print("=" * 50)
    print("  Release: " + RELEASE_ZIP.name)
    print("  Extract and double-click VoiceInput.exe")
    print("  ZIP size: " + str(round(zsize / 1024 / 1024, 1)) + " MB")
    print("=" * 50)
    return 0


if __name__ == "__main__":
    sys.exit(main())
