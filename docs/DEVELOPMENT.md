# Vernest Development Notes

## Repository Shape

```text
voice-input/
├── ui-tauri/                 # Tauri + React desktop app
│   ├── src/                  # React UI
│   └── src-tauri/            # Rust host: windows, tray, shortcuts, sounds
├── voice_backend.py          # Local HTTP sidecar
├── voice_core/               # Recording, ASR, text polishing, paste runtime
├── packaging/pyinstaller/    # Python sidecar packaging
├── scripts/                  # Build, release, model helper scripts
├── models/                   # Local model files, not committed when large
├── docs/                     # Project knowledge base
└── legacy/                   # Archived Tkinter/PyInstaller shell
```

## Local Development

Install Python dependencies:

```powershell
pip install -r requirements.txt
```

`requirements.txt` includes the official CPU wheel index for
`llama-cpp-python`. This avoids Windows source builds and long-path extraction
failures when installing the local text-polishing runtime.

Install frontend dependencies:

```powershell
cd ui-tauri
npm ci
```

Run the desktop app in development:

```powershell
cd ui-tauri
npm run tauri -- dev
```

Build the Python sidecar:

```powershell
.\scripts\build-backend.ps1
```

Build release artifacts:

```powershell
.\scripts\build-release.ps1
```

## User Data

New user data is under:

```text
%APPDATA%\Vernest
```

Important files:

```text
%APPDATA%\Vernest\config.json
%APPDATA%\Vernest\shortcut.json
%APPDATA%\Vernest\logs\vernest.log
%APPDATA%\Vernest\diagnostics\
```

The old `%USERPROFILE%\.voice_input\config.json` is migrated on first launch.
Old logs are intentionally not migrated.

## Local Models

ASR model:

```text
models\sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17\
```

LLM polishing model:

```text
models\polish\qwen2.5-0.5b-instruct-q4_k_m.gguf
```

Download the polishing model:

```powershell
.\scripts\download-polish-model.ps1
```

Install the polishing model into the user app data directory instead:

```powershell
.\scripts\download-polish-model.ps1 -AppData
```

The default development and release path is `models\polish\`, alongside the STT
model directory. AppData is only a compatibility fallback for user-installed
models. Large model files are ignored by git. The installer and portable package
include `models\polish\qwen2.5-0.5b-instruct-q4_k_m.gguf` when that file exists
in the repository working tree during `build-release.ps1`.

## Runtime Boundaries

- Rust host: window lifecycle, tray, close-to-tray, global shortcut hooks, prompt sounds, and sidecar lifecycle.
- Python sidecar: local HTTP API, audio capture, device enumeration, ASR, text polishing, clipboard paste, and config persistence.
- React UI: liquid-glass shell, settings, appearance controls, device selection, diagnostics export, and shortcut capture UI.

## Regression Focus

Commercial-quality regression should prioritize:

- Global shortcut hold-to-record and release-to-stop
- Mouse hold-to-record on the main orb and floating bubble
- Close-to-tray and floating bubble behavior
- Tray show, restore, and quit
- Prompt sounds for press, release, enable, and pause
- Shortcut customization with keyboard, mouse, and mixed combinations
- Config migration and local logging
- STT text polishing enabled, disabled, missing-model fallback, and successful local polish
