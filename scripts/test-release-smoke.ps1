param(
  [string]$ReleaseDir = "$PSScriptRoot\..\ui-tauri\src-tauri\target\release"
)

$ErrorActionPreference = "Stop"
$ReleaseDir = (Resolve-Path -LiteralPath $ReleaseDir).Path

$AppExe = @(
  Join-Path $ReleaseDir "vernest-desktop.exe"
  Join-Path $ReleaseDir "ui-tauri.exe"
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if (-not $AppExe) {
  throw "App exe not found in $ReleaseDir"
}

$BackendExe = @(
  Join-Path $ReleaseDir "vernest-backend.exe"
  Join-Path $ReleaseDir "vernest-backend-x86_64-pc-windows-msvc.exe"
  Join-Path $ReleaseDir "yanqi-backend.exe"
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if (-not $BackendExe) {
  throw "Backend sidecar not found in $ReleaseDir"
}

$Model = Join-Path $ReleaseDir "models\sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17\model.int8.onnx"
if (-not (Test-Path -LiteralPath $Model)) {
  throw "Bundled ASR model not found: $Model"
}

$Installer = Get-ChildItem -LiteralPath (Join-Path $ReleaseDir "bundle\nsis") -Filter "言栖_0.6.7_x64-setup.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $Installer) {
  throw "NSIS installer not found"
}

[PSCustomObject]@{
  AppExe = $AppExe
  BackendExe = $BackendExe
  Model = $Model
  Installer = $Installer.FullName
}
