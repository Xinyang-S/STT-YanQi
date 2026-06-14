# 言栖 Vernest

[English](README_EN.md)

言栖 Vernest 是一款 Windows 桌面语音输入工具。应用按住快捷键录音，松开后在本机完成识别并粘贴到当前光标位置。

当前版本：`0.8.1`  
作者：孙欣阳  
项目主页：<https://github.com/Xinyang-S/STT-YanQi/tree/main>  
版权：Copyright © 2026 孙欣阳. All rights reserved.

> 当前仍处于 Beta 阶段。仓库已经按商业化桌面应用的发布结构收束，但尚未配置 Windows 代码签名证书。

## 产品原则

- 默认使用本地识别；不会自动上传音频、识别文本或诊断数据。未来如加入云端增强能力，必须由用户显式开启。
- 默认只写本地日志，路径为 `%APPDATA%\Vernest\logs\vernest.log`。
- 用户可在设置里手动导出本地诊断文件；应用不会自动上传。
- 支持 Windows 10 / Windows 11 x64。

## 当前架构

```text
voice-input/
├── ui-tauri/                 # Tauri + React 桌面应用
│   ├── src/                  # React UI
│   └── src-tauri/            # Rust 宿主、托盘、快捷键、提示音、窗口管理
├── voice_backend.py          # Python sidecar HTTP 后端
├── voice_core/               # 录音、识别、设备、粘贴核心
├── packaging/                # 打包配置
├── scripts/                  # 构建、签名、发布脚本
├── models/                   # 本地 ASR 模型
└── legacy/                   # 旧 Tkinter/PyInstaller 桌面壳归档
```

Rust 宿主负责窗口、托盘、全局快捷键、提示音和 sidecar 生命周期。Python sidecar 负责本地录音、SenseVoice 识别、麦克风设备枚举和配置持久化。

前后端通过 `127.0.0.1:47632` 通信，并由 Rust 宿主在启动时生成一次性 `X-Vernest-Token`，防止普通本地网页直接控制录音接口。

更多开发知识库见 [docs/INDEX.md](docs/INDEX.md)。

## 数据目录

新版本统一使用：

```text
%APPDATA%\Vernest
```

主要文件：

```text
%APPDATA%\Vernest\config.json
%APPDATA%\Vernest\shortcut.json
%APPDATA%\Vernest\logs\vernest.log
%APPDATA%\Vernest\diagnostics\
```

如果检测到旧配置 `%USERPROFILE%\.voice_input\config.json`，首次启动会迁移配置；旧日志不会迁移。

## 开发

安装前端依赖：

```powershell
cd ui-tauri
npm ci
```

开发运行：

```powershell
cd ui-tauri
npm run tauri -- dev
```

构建 Python sidecar：

```powershell
.\scripts\build-backend.ps1
```

完整构建安装包和便携 zip：

```powershell
.\scripts\build-release.ps1
```

文本润色能力规划为未来的云端大模型功能。当前版本不包含本地润色模型，也不会调用云端润色 API。

## 发布产物

浅层安装包：

```text
release\Vernest_0.8.1_x64-setup.exe
```

浅层便携版主程序：

```text
release\Vernest\Vernest.exe
```

便携版 zip：

```text
release\Vernest-0.8.1-windows-x64-portable.zip
```

代码签名预留脚本：

```powershell
$env:VERNEST_SIGN_CERT="C:\path\to\certificate.pfx"
$env:VERNEST_SIGN_PASSWORD="certificate-password"
.\scripts\sign-windows.ps1 -Path ".\release\some-file.exe"
```

## 自动更新

计划使用 GitHub Releases：

```text
https://github.com/Xinyang-S/STT-YanQi/releases/latest/download/latest.json
```

Tauri 自动更新需要 Tauri updater signing key。该 key 与 Windows 代码签名证书不是同一个东西，私钥不能提交到仓库。当前仓库保留了更新配置说明，正式启用前需要生成 updater key 并接入发布流水线。

## 测试重点

商业化回归测试优先覆盖：

- 全局快捷键按住录音、松开停止
- 鼠标按住主界面和悬浮气泡录音
- 托盘显示、恢复、退出
- 关闭主窗口进入托盘或显示悬浮气泡
- 提示音串行播放
- 配置迁移和日志写入

## 支持作者

如果言栖 Vernest 对你的日常输入或 AI 工作流有帮助，可以自愿赞赏支持项目继续维护。赞赏不会解锁额外功能，也不会影响问题反馈和开源使用。

<img src="docs/assets/sponsor-alipay.jpg" alt="支付宝赞赏码" width="320">

## 许可

项目代码采用 MIT License。内置 SenseVoice / sherpa-onnx 相关模型与依赖遵循各自许可证，详见 `THIRD_PARTY_NOTICES.md` 和模型目录内的 `LICENSE`。
