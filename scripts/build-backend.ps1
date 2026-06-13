param(
  [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")
$Spec = Join-Path $Root "packaging\pyinstaller\VoiceBackend.spec"
$Dist = Join-Path $Root "build\pyinstaller-dist"
$Work = Join-Path $Root "build\pyinstaller-work"
$Bin = Join-Path $Root "ui-tauri\src-tauri\bin"
$Target = Join-Path $Bin "vernest-backend-x86_64-pc-windows-msvc.exe"

New-Item -ItemType Directory -Force -Path $Dist, $Work, $Bin | Out-Null

& $Python -m PyInstaller --noconfirm --clean --distpath $Dist --workpath $Work $Spec
if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$Built = Join-Path $Dist "vernest-backend.exe"
if (-not (Test-Path -LiteralPath $Built)) {
  throw "Backend build output not found: $Built"
}

Get-Process | Where-Object {
  $_.Path -eq $Target -or $_.Path -like (Join-Path $Bin "*backend*.exe")
} | Stop-Process -Force
Start-Sleep -Milliseconds 300

Copy-Item -LiteralPath $Built -Destination $Target -Force
Write-Host "Backend sidecar:" $Target
