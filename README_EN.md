# Vernest

[中文](README.md)

Vernest, known as 言栖 in Chinese, is a Windows desktop voice input tool. Hold a shortcut to record, release it to transcribe locally, and paste the result into the current cursor position.

Current version: `0.8.1`  
Author: 孙欣阳  
Project homepage: <https://github.com/Xinyang-S/STT-YanQi/tree/main>  
Copyright: Copyright © 2026 孙欣阳. All rights reserved.

> Vernest is currently in beta. The repository has been reorganized around a commercial-grade desktop app structure, but Windows code signing has not been configured yet.

## Product Principles

- Local recognition by default. Vernest does not automatically upload audio, transcripts, or diagnostics; any future cloud enhancement must be explicitly enabled by the user.
- Logs are written locally to `%APPDATA%\Vernest\logs\vernest.log`.
- Diagnostics are exported only when the user explicitly clicks export.
- Supported platforms: Windows 10 / Windows 11 x64.

## Architecture

```text
voice-input/
├── ui-tauri/                 # Tauri + React desktop app
│   ├── src/                  # React UI
│   └── src-tauri/            # Rust host, tray, shortcuts, sounds, windows
├── voice_backend.py          # Python sidecar HTTP backend
├── voice_core/               # Recording, recognition, devices, paste runtime
├── packaging/                # Packaging configuration
├── scripts/                  # Build, signing, and release scripts
├── models/                   # Local ASR model files
└── legacy/                   # Archived Tkinter/PyInstaller desktop shell
```

The Rust host owns OS integration: windows, tray, global shortcuts, prompt sounds, and sidecar lifecycle. The Python sidecar owns local audio capture, SenseVoice recognition, microphone device enumeration, and voice configuration.

The app communicates with the sidecar through `127.0.0.1:47632`. Each launch generates a temporary `X-Vernest-Token`, so ordinary local webpages cannot directly control the recording API.

See [docs/INDEX.md](docs/INDEX.md) for the project knowledge base.

## Data Directory

Vernest stores new user data under:

```text
%APPDATA%\Vernest
```

Main files:

```text
%APPDATA%\Vernest\config.json
%APPDATA%\Vernest\shortcut.json
%APPDATA%\Vernest\logs\vernest.log
%APPDATA%\Vernest\diagnostics\
```

If `%USERPROFILE%\.voice_input\config.json` is found, Vernest migrates the old configuration on first launch. Old logs are not migrated.

## Development

Install frontend dependencies:

```powershell
cd ui-tauri
npm ci
```

Run in development:

```powershell
cd ui-tauri
npm run tauri -- dev
```

Build the Python sidecar:

```powershell
.\scripts\build-backend.ps1
```

Build the installer and portable zip:

```powershell
.\scripts\build-release.ps1
```

Text polishing is planned as a future cloud-model feature. The current release does not bundle a local polishing model and does not call any cloud polishing API.

## Release Artifacts

Shallow NSIS installer:

```text
release\言栖_0.8.1_x64-setup.exe
```

Shallow portable app:

```text
release\Vernest\Vernest.exe
```

Portable zip:

```text
release\Vernest-0.8.1-windows-x64-portable.zip
```

Code signing placeholder:

```powershell
$env:VERNEST_SIGN_CERT="C:\path\to\certificate.pfx"
$env:VERNEST_SIGN_PASSWORD="certificate-password"
.\scripts\sign-windows.ps1 -Path ".\release\some-file.exe"
```

## Auto Update

The planned update channel is GitHub Releases:

```text
https://github.com/Xinyang-S/STT-YanQi/releases/latest/download/latest.json
```

Tauri auto updates require a Tauri updater signing key. This is separate from a Windows code-signing certificate. Do not commit the private updater key.

## Regression Focus

Commercial-quality regression tests should prioritize:

- Hold-to-record and release-to-stop global shortcuts
- Mouse hold-to-record on the main window and floating bubble
- Tray show, restore, and quit flows
- Close-to-tray and floating bubble behavior
- Serialized prompt sound playback
- Configuration migration and local logging

## Support the Author

If Vernest helps your daily input workflow or AI work, you can voluntarily support the project. Support does not unlock extra features and does not affect issue handling or open-source use.

<img src="docs/assets/sponsor-alipay.jpg" alt="Alipay support QR code" width="320">

International users can also support the project by starring the repository, reporting issues, or sharing feedback.

## License

Project code is released under the MIT License. Bundled SenseVoice / sherpa-onnx model files and dependencies follow their own licenses. See `THIRD_PARTY_NOTICES.md` and the model directory `LICENSE` files for details.
