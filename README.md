# 语音输入工具 STT-YanQi

[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-0078d4)](https://www.microsoft.com/)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776ab)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Offline](https://img.shields.io/badge/识别-本地离线-success)]()

> 一款为 AI 时代而生的 Windows 桌面语音输入工具。按住 `Right Ctrl` 说话，松开发送到光标位置。完全本地识别，无云端依赖。

灵感来自 [Boson](https://github.com/dshsl000/boson) / [CapsWriter](https://github.com/HaujetZhao/CapsWriter) / [Wispr Flow](https://wisprflow.ai)，针对中文用户与 AI Agent 输入场景深度优化。

---

## ✨ 核心特性

| 特性 | 说明 |
| --- | --- |
| 🎙 **按住说话** | 按住 `Right Ctrl` 录音 → 松开识别 → 自动粘贴到光标 |
| 🔒 **录音隐私** | WASAPI 独占流 + 切换系统默认麦克风，按住期间 Discord/QQ/飞书 拿不到您的声音 |
| 🧠 **本地离线识别** | [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) + [SenseVoice](https://github.com/FunAudioLLM/SenseVoice)，无网亦可用，零配额限制 |
| 🌍 **多语种自动检测** | 中文 / 英文 / 日文 / 韩文 / 粤语，按需切换 |
| 🎨 **现代 UI** | Tokyo Night 配色 + 三圈脉冲环 + 32 帧频谱条 + Discord 风格提示音 |
| ⚙️ **低打扰** | 静音启动、托盘常驻、开机自启、按需启用 |
| 📦 **单文件 EXE** | PyInstaller 打包，~76 MB + 模型 ~120 MB |

---

## 📸 界面

- 主窗口：录音按钮 / 识别结果 / 设置入口
- 托盘：状态指示 + 启用切换 + 退出
- 设置：通用（开机启动 / 独占设备）+ 音频设备

> 录音态：三圈脉冲环实时扩散 + 频谱条逐帧变化 + 状态指示呼吸

---

## 🚀 快速开始

### 1. 下载预编译版（推荐普通用户）

前往 [Releases](https://github.com/Xinyang-S/STT-YanQi/releases) 下载 `VoiceInput-v5.0.zip`，解压后双击 `VoiceInput.exe` 即可。

> 首次启动会自动检测模型；若未找到会提示下载。

### 2. 源码运行（开发者）

#### 准备模型

下载 SenseVoice 多语种 int8 模型（约 120 MB）：

```bash
# Windows PowerShell
mkdir models
Invoke-WebRequest -Uri "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2" -OutFile "models/sv.tar.bz2"
tar -xjf models/sv.tar.bz2 -C models
```

最终结构应为：

```
voice-input/
  voice_input.py
  models/
    sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/
      model.int8.onnx
      tokens.txt
      ...
```

#### 安装依赖

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

#### 启动

```bash
python voice_input.py           # 正常启动
python voice_input.py --test    # 全链路测试（录音 3 秒 + 识别）
```

---

## ⌨️ 快捷键

| 快捷键 | 功能 |
| --- | --- |
| **按住 `Right Ctrl`** | 开始录音 → 松开结束 → 自动粘贴 |
| `Ctrl` + `Shift` + `F9` | 启用 / 禁用 整体功能 |
| 录音按钮 | 鼠标点击按住同样可用 |

> 系统要求 Windows 10/11。`Right Ctrl` 在大写锁定下仍可识别。

---

## ⚙️ 设置项

| 设置 | 默认 | 说明 |
| --- | --- | --- |
| **开机自启** | ✅ 开启 | 登录后直接进托盘；注册表项仅 frozen 模式生效 |
| **录音时独占设备** | ✅ 开启 | 切走默认麦克风，阻止其他 App 旁听；关闭则共享模式 |
| **麦克风** | 系统默认 | 列表来自 IMMDeviceEnumerator（修复 pyaudio 设备名乱码） |
| **识别语言** | `auto` 自动检测 | 可选 `zh` / `en` / `ja` / `ko` / `yue` / `auto` |

> 配置文件：`%USERPROFILE%\.voice_input\config.json`
> 运行日志：`%USERPROFILE%\.voice_input\voice_input.log`

---

## 🧠 识别引擎

### 选型

| 候选 | 中文准确度 | 速度 (CPU) | 体积 | 打包 | 决定 |
| --- | --- | --- | --- | --- | --- |
| Whisper.cpp base | ⭐⭐ | ⭐⭐⭐ | 148 MB | ✅ | 中文中等 |
| Whisper.cpp small | ⭐⭐⭐⭐ | ⭐⭐ | 488 MB | ✅ | 体积大 |
| faster-whisper | ⭐⭐⭐ | ⭐⭐⭐⭐ | 148 MB | ⚠️ ctranslate2 依赖重 | CTranslate2 包 80MB+ |
| **sherpa-onnx SenseVoice-Small int8** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | **120 MB** | ✅ ONNX runtime 自带 | **✅ 选用** |
| sherpa-onnx Zipformer bilingual | ⭐⭐⭐ | ⭐⭐⭐⭐⭐ | 150 MB | ✅ | 流式，但中英准确度不如 SenseVoice |
| vosk-cn small | ⭐⭐⭐ | ⭐⭐⭐⭐ | 42 MB | ✅ | 单语种，不支持英文 |
| FunASR (Paraformer) | ⭐⭐⭐⭐ | ⭐⭐ | 200 MB+ | ❌ torch 依赖太重 | 打包失败 |

**最终选择 sherpa-onnx + SenseVoice-Small int8**，原因：
1. 单模型多语种（zh/en/ja/ko/yue 自动检测），无需切换
2. 中文准确度优于 Whisper-small 同尺寸模型
3. int8 量化后仅 120 MB，CPU 实时识别
4. ONNX Runtime 自带，无需额外 CUDA / CTranslate2 依赖
5. Apache 2.0 协议，可商用

### 模型

- 名称：`sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17`
- 来源：[k2-fsa/sherpa-onnx 模型发布页](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models)
- 协议：Apache 2.0
- 体积：~120 MB

---

## 🔒 隐私设计

> 在 AI Agent（Claude/Cursor/Codeium 等）输入时，**不希望其他 App 旁听到语音指令**。

1. **WASAPI 独占流** — 录音时尝试申请独占模式，失败回退共享
2. **切换系统默认麦克风** — 把默认设备切到回退设备，从默认设备取音频的 App 会断流
3. **三道兜底恢复** — 正常路径 + `finally` + `tray_exit` 硬退出
4. **不依赖网络** — 完全离线识别，没有任何音频数据离开您的电脑

> 至少需要 2 个录音设备才能使用独占特性。设置中可关闭。

---

## 📦 打包发布

```bash
# 一键打包 (Windows)
build.bat

# 输出
dist/VoiceInput.exe  (~76 MB)
```

> 模型文件不进 exe，发布时建议连同 `models/` 目录一起打包为 zip（[Releases](https://github.com/Xinyang-S/STT-YanQi/releases)）。

---

## 🛠 故障排查

| 症状 | 原因 | 解决 |
| --- | --- | --- |
| 启动提示 "本地引擎不可用" | sherpa-onnx 未装 / 模型未下载 | `pip install sherpa-onnx` + 下载模型 |
| 识别全是空白 | 录音设备未授权 / 静音 | 设置中选择正确的麦克风；提高 Windows 输入音量 |
| 独占设备失败 | 只有 1 个录音设备 | 在设置中关闭"录音时独占设备" |
| 按键无提示音 | 系统静音 / winsound 异常 | 取消系统静音；查看 `%USERPROFILE%\.voice_input\voice_input.log` |
| PyInstaller 打包后报 `libomp140.x64.dll` 缺失 | 系统缺少 VC++ runtime | 安装 [VC++ 2015-2022 Redist](https://aka.ms/vs/17/release/vc_redist.x64.exe) |
| 识别速度慢 | CPU 核心数低 / 录音过长 | 关闭其他占用 CPU 的程序；模型加载 1-2s 是正常的 |

---

## 🧪 开发

### 端到端测试

```bash
python test_e2e.py
```

### 项目结构

```
voice-input/
├── voice_input.py        # 主程序 (~1900 行)
├── app.ico               # 应用图标
├── requirements.txt      # Python 依赖
├── build.bat             # PyInstaller 打包脚本
├── VoiceInput.spec       # PyInstaller 配置
├── test_e2e.py           # 端到端自测
└── models/               # 模型目录 (用户下载)
```

### 技术栈

- **GUI**：tkinter + ttk.Notebook
- **系统托盘**：pystray
- **音频**：pyaudio + WASAPI 独占
- **键鼠**：pynput / pyautogui / pyperclip
- **STT**：sherpa-onnx + SenseVoice
- **COM**：纯 ctypes，IPolicyConfig::SetDefaultEndpoint

---

## 📝 更新日志

### v5.0（当前版本）

**🎉 重大重构**
- **唯一识别引擎**：移除百度 / 讯飞 API 依赖，仅保留本地 SenseVoice
- **新增设置项**：录音时独占设备（默认开启，可在设置中关闭）
- **新识别引擎**：sherpa-onnx + SenseVoice 多语种（zh/en/ja/ko/yue）
- **修复 Bug**：按键提示音改用异步播放（之前在某些 PyInstaller 配置下静音）
- **配置迁移**：自动清理旧版 `baidu` / `iflytek` / `local_asr` 字段

### v4.3

- Discord 风格提示音
- 开机自启动（注册表）
- 设备名乱码修复（IMMDeviceEnumerator）

### v4.2

- 三圈脉冲环 UI
- 32 帧频谱条
- 复制按钮

### v4.1

- MicGuard 麦克风独占（核心隐私特性）

---

## 🤝 贡献

欢迎 Issue / PR。开发模式直接 `python voice_input.py` 即可，无需打包。

提交前请确保 `python voice_input.py --test` 跑通。

---

## 📄 协议

[MIT License](LICENSE)

sherpa-onnx 遵循 Apache 2.0；SenseVoice 模型遵循 Apache 2.0。详见各项目仓库。
