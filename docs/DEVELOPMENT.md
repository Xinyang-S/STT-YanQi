# Vernest Development Notes

## Repository Shape

```text
voice-input/
├── ui-tauri/                 # Tauri + React desktop app
│   ├── src/                  # React UI
│   └── src-tauri/            # Rust host: windows, tray, shortcuts, sounds
├── voice_backend.py          # Local HTTP sidecar
├── voice_core/               # Recording, ASR, paste runtime
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

There is no bundled local LLM polishing model. Future text polishing will be a
cloud-provider feature and is tracked in `docs/CLOUD_POLISH_PLAN.md`.

## Runtime Boundaries

- Rust host: window lifecycle, tray, close-to-tray, global shortcut hooks, prompt sounds, and sidecar lifecycle.
- Python sidecar: local HTTP API, audio capture, device enumeration, ASR, clipboard paste, and config persistence.
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
- Future cloud text-polishing settings remain disabled until the provider layer is implemented
