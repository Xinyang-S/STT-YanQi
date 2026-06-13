param(
  [switch]$SkipBackend,
  [switch]$SkipPortable
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")
$Version = "0.6.7"
$Ui = Join-Path $Root "ui-tauri"
$ReleaseRoot = Join-Path $Root "release"
$TauriRelease = Join-Path $Ui "src-tauri\target\release"
$PortableName = "Vernest-$Version-windows-x64-portable"
$PortableDir = Join-Path $ReleaseRoot $PortableName
$PortableZip = Join-Path $ReleaseRoot "$PortableName.zip"

if (-not $SkipBackend) {
  & (Join-Path $PSScriptRoot "build-backend.ps1")
}

Push-Location $Ui
try {
  if (-not (Test-Path -LiteralPath "node_modules")) {
    npm ci
  }
  npm run build
  npm run tauri -- build
} finally {
  Pop-Location
}

if ($SkipPortable) {
  exit 0
}

New-Item -ItemType Directory -Force -Path $ReleaseRoot | Out-Null
if (Test-Path -LiteralPath $PortableDir) {
  Remove-Item -LiteralPath $PortableDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $PortableDir | Out-Null

$AppExe = @(
  Join-Path $TauriRelease "vernest-desktop.exe"
  Join-Path $TauriRelease "ui-tauri.exe"
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if (-not $AppExe) {
  throw "Tauri release exe was not found in $TauriRelease"
}

Copy-Item -LiteralPath $AppExe -Destination (Join-Path $PortableDir "Vernest.exe") -Force

$BackendExe = @(
  Join-Path $TauriRelease "vernest-backend.exe"
  Join-Path $TauriRelease "vernest-backend-x86_64-pc-windows-msvc.exe"
  Join-Path $TauriRelease "yanqi-backend.exe"
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if ($BackendExe) {
  Copy-Item -LiteralPath $BackendExe -Destination (Join-Path $PortableDir "vernest-backend.exe") -Force
}

foreach ($Name in @("models", "resources")) {
  $Source = Join-Path $TauriRelease $Name
  if (Test-Path -LiteralPath $Source) {
    Copy-Item -LiteralPath $Source -Destination (Join-Path $PortableDir $Name) -Recurse -Force
  }
}

foreach ($Name in @("README.md", "LICENSE", "THIRD_PARTY_NOTICES.md")) {
  $Source = Join-Path $Root $Name
  if (Test-Path -LiteralPath $Source) {
    Copy-Item -LiteralPath $Source -Destination (Join-Path $PortableDir $Name) -Force
  }
}

if (Test-Path -LiteralPath $PortableZip) {
  Remove-Item -LiteralPath $PortableZip -Force
}
Compress-Archive -Path (Join-Path $PortableDir "*") -DestinationPath $PortableZip -Force
Write-Host "Portable package:" $PortableZip
