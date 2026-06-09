# 言栖 (Yán Qī) — Voice Input for AI Agents

[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-0078d4)](https://www.microsoft.com/)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776ab)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Offline](https://img.shields.io/badge/ASR-100%25%20offline-success)]()

> Hold **Right Ctrl** to dictate, release to paste at cursor. 100% local speech recognition, no cloud, no quotas. Built for AI-agent workflows on Windows.

**Author**: 孙欣阳 (Xinyang Sun) · [Project homepage](https://github.com/Xinyang-S/STT-YanQi)

*Yán Qī (言栖) — "voice resting" — a quiet place where your words land.*

Inspired by [Boson](https://github.com/dshsl000/boson) / [CapsWriter](https://github.com/HaujetZhao/CapsWriter) / [Wispr Flow](https://wisprflow.ai/).

---

## ✨ Highlights

| | |
| --- | --- |
| 🎙 **Push-to-talk** | Hold `Right Ctrl` → release → auto-paste |
| 🔒 **Recording privacy** | WASAPI exclusive stream + default-mic switch; Discord/QQ/Feishu cannot capture your voice while you hold Ctrl |
| 🧠 **Offline ASR** | [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) + [SenseVoice](https://github.com/FunAudioLLM/SenseVoice) — works without internet |
| 🌍 **Multilingual** | Auto-detects Chinese / English / Japanese / Korean / Cantonese |
| 🎨 **Modern UI** | Tokyo Night palette · 3-ring pulse · 32-bar live spectrum · Discord-style prompt tones |
| ⚙️ **Quiet by default** | Auto-start, system tray, opt-in hotkey |
| 📦 **Single .exe** | PyInstaller onefile, ~76 MB + ~120 MB model |

---

## 📸 Screenshots

> Coming soon: 3-ring pulse on record state, 32-bar spectrum, animated status dot, dark-mode result card.

---

## 🚀 Quick Start

### Option 1 — Prebuilt release (recommended)

Grab `VoiceInput-v5.0.zip` from [Releases](https://github.com/Xinyang-S/STT-YanQi/releases), unzip, double-click `VoiceInput.exe`.

### Option 2 — Run from source

#### 1. Download the model

```bash
# Windows PowerShell
mkdir models
Invoke-WebRequest -Uri "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2" -OutFile "models/sv.tar.bz2"
tar -xjf models/sv.tar.bz2 -C models
```

Layout after extraction:

```
voice-input/
  voice_input.py
  models/
    sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/
      model.int8.onnx
      tokens.txt
      ...
```

#### 2. Install dependencies

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

#### 3. Run

```bash
python voice_input.py          # normal start
python voice_input.py --test   # end-to-end test (record 3s + transcribe)
```

---

## ⌨️ Hotkeys

| Key | Action |
| --- | --- |
| **Hold `Right Ctrl`** | Record → release to recognize → auto-paste |
| `Ctrl` + `Shift` + `F9` | Toggle enable/disable |
| Click mic button | Same as holding `Right Ctrl` |

> Requires Windows 10/11. `Right Ctrl` works under CapsLock.

---

## ⚙️ Settings

| Setting | Default | Description |
| --- | --- | --- |
| **Auto-start** | ✅ on | Launch into tray at login. Registry write only in frozen mode. |
| **Exclusive device** | ✅ on | Switch default mic during recording to block other apps. Off = shared mode. |
| **Input device** | System default | List via IMMDeviceEnumerator (avoids pyaudio garbled names on Windows). |
| **Recognition language** | `auto` | `zh` / `en` / `ja` / `ko` / `yue` / `auto` |

> Config: `%USERPROFILE%\.voice_input\config.json`
> Logs: `%USERPROFILE%\.voice_input\voice_input.log`

---

## 🧠 ASR Engine

### Why [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) + [SenseVoice-Small int8](https://github.com/FunAudioLLM/SenseVoice)?

| Candidate | zh accuracy | CPU speed | Size | PyInstaller | Verdict |
| --- | --- | --- | --- | --- | --- |
| Whisper.cpp base | ⭐⭐ | ⭐⭐⭐ | 148 MB | ✅ | weak zh |
| Whisper.cpp small | ⭐⭐⭐⭐ | ⭐⭐ | 488 MB | ✅ | big |
| faster-whisper | ⭐⭐⭐ | ⭐⭐⭐⭐ | 148 MB | ⚠️ ctranslate2 80 MB+ | heavy deps |
| **sherpa-onnx SenseVoice-Small int8** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | **120 MB** | ✅ ONNX runtime bundled | **✅ chosen** |
| sherpa-onnx Zipformer zh-en | ⭐⭐⭐ | ⭐⭐⭐⭐⭐ | 150 MB | ✅ | streaming but weaker zh |
| vosk-cn small | ⭐⭐⭐ | ⭐⭐⭐⭐ | 42 MB | ✅ | single language |
| FunASR Paraformer | ⭐⭐⭐⭐ | ⭐⭐ | 200 MB+ | ❌ torch dependency | too heavy |

Reasons:
1. **One model, five languages** — auto-detect zh/en/ja/ko/yue
2. **Best Chinese accuracy** at this size class
3. **int8 = 120 MB**, real-time on CPU
4. **ONNX runtime bundled** in the wheel — no CUDA/CTranslate2 required
5. **Apache 2.0**, commercial-friendly

### Model details

- Name: `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17`
- Source: [k2-fsa/sherpa-onnx releases](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models)
- License: Apache 2.0
- Size: ~120 MB

---

## 🔒 Privacy Design

> When dictating into an AI agent (Claude / Cursor / Codeium …), you don't want Discord or Feishu recording your prompt.

1. **WASAPI exclusive stream** — try exclusive mode, fall back to shared.
2. **Switch system default mic** — apps reading the default device are disconnected for the duration.
3. **Triple-fail recovery** — normal path + `finally` + hard exit (`tray_exit`).
4. **Fully offline** — audio never leaves your machine.

> Exclusive mode requires ≥ 2 input devices. Toggle off in Settings if not.

---

## 📦 Build

```bash
# Windows
build.bat

# Output
dist/VoiceInput.exe  (~82 MB)
```

> **Model (~230 MB) is not embedded**: PyInstaller `--onefile` extracts the ONNX model
> to `_MEIPASS`, and the SenseVoice C++ decoder throws `invalid unordered_map<K, T> key`
> on `decode_stream` from that location. Loading the same model from disk works fine.
> Workaround: don't bundle the model — users run `download_model.bat` once to fetch it.
> This also keeps the EXE at 82 MB instead of 540 MB.

Release packaging:

```bash
build.bat
mkdir VoiceInput-v5.0
cp dist/VoiceInput.exe VoiceInput-v5.0/
cp download_model.bat VoiceInput-v5.0/
cp README_EN.md VoiceInput-v5.0/
# models/ stays empty — users get it via download_model.bat
```

---

## 🛠 Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| "Local engine unavailable" on launch | sherpa-onnx not installed or model missing | `pip install sherpa-onnx` + download model |
| Recognition is empty | Wrong input device / muted | Pick the right mic in Settings; raise Windows input volume |
| Exclusive mode fails | Only 1 input device | Turn off "Exclusive device" in Settings |
| No prompt sound on key press | System muted / winsound error | Unmute system; check `%USERPROFILE%\.voice_input\voice_input.log` |
| `libomp140.x64.dll` missing on first run | VC++ runtime not installed | Install [VC++ 2015-2022 Redist (x64)](https://aka.ms/vs/17/release/vc_redist.x64.exe) |
| Slow recognition | Low core count / long audio | Close CPU-heavy apps; 1-2 s first-load is normal |

---

## 🧪 Development

### End-to-end test

```bash
python test_e2e.py
```

### Layout

```
voice-input/
├── voice_input.py        # main program (~1900 lines)
├── app.ico               # app icon
├── requirements.txt      # Python deps
├── build.bat             # PyInstaller script
├── VoiceInput.spec       # PyInstaller config
├── test_e2e.py           # end-to-end self-test
└── models/               # model dir (user downloads)
```

### Stack

- **GUI**: tkinter + ttk.Notebook
- **Tray**: pystray
- **Audio**: pyaudio + WASAPI exclusive
- **Input/clipboard**: pynput / pyautogui / pyperclip
- **STT**: sherpa-onnx + SenseVoice
- **COM**: pure ctypes, `IPolicyConfig::SetDefaultEndpoint`

---

## 📝 Changelog

### v5.0 (current)

**🎉 Major refactor**
- **Single engine**: removed Baidu / iFlytek cloud APIs, SenseVoice is the only ASR
- **New setting**: "Exclusive device during recording" (default on, toggleable in Settings)
- **New ASR**: sherpa-onnx + SenseVoice multilingual (zh/en/ja/ko/yue)
- **Bug fix**: prompt tone now plays asynchronously (was silent under some PyInstaller configs)
- **Config migration**: old `baidu` / `iflytek` / `local_asr` keys are auto-stripped on load

### v4.3

- Discord-style prompt tones
- Auto-start (registry)
- Device-name garbled-character fix (IMMDeviceEnumerator)

### v4.2

- 3-ring pulse UI
- 32-bar live spectrum
- Copy-to-clipboard button

### v4.1

- MicGuard default-mic switch (core privacy feature)

---

## 🤝 Contributing

Issues and PRs welcome. Develop with `python voice_input.py` — no packaging required.

Before submitting, please make sure `python voice_input.py --test` passes.

---

## 📄 License

[MIT](LICENSE)

`sherpa-onnx` is Apache 2.0. The SenseVoice model is Apache 2.0. See upstream repos for full terms.
