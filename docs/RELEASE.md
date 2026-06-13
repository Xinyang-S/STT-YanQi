# Vernest Release Process

## Local Release

```powershell
.\scripts\build-release.ps1
```

Outputs:

- NSIS installer under `ui-tauri/src-tauri/target/release/bundle/nsis`
- Portable zip under `release/`

## Code Signing

Windows code signing requires a certificate. The repository only includes a
placeholder script:

```powershell
.\scripts\sign-windows.ps1 -Path "path\to\artifact.exe"
```

Set `VERNEST_SIGN_CERT` and `VERNEST_SIGN_PASSWORD` locally or in CI.

## Auto Update

Use GitHub Releases and publish Tauri updater metadata as `latest.json`.
Do not commit updater private keys.
