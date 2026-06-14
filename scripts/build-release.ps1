param(
  [switch]$SkipBackend,
  [switch]$SkipPortable
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")
$Version = "0.8.1"
$Ui = Join-Path $Root "ui-tauri"
$ReleaseRoot = Join-Path $Root "release"
$TauriRelease = Join-Path $Ui "src-tauri\target\release"
$PortableName = "Vernest"
$PortableDir = Join-Path $ReleaseRoot $PortableName
$PortableZip = Join-Path $ReleaseRoot "Vernest-$Version-windows-x64-portable.zip"
$LegacyPortableDir = Join-Path $ReleaseRoot "Vernest-$Version-windows-x64-portable"
$PolishModel = Join-Path $Root "models\polish\qwen2.5-0.5b-instruct-q4_k_m.gguf"

function Invoke-Native {
  param(
    [Parameter(Mandatory=$true)][string]$FilePath,
    [string[]]$Arguments = @()
  )
  & $FilePath @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "$FilePath $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
  }
}

function Stop-VernestBuildProcesses {
  $Prefixes = @(
    (Join-Path $Ui "src-tauri\bin"),
    $TauriRelease,
    $ReleaseRoot
  )

  Get-Process | Where-Object {
    if (-not $_.Path) { return $false }
    foreach ($Prefix in $Prefixes) {
      if ($_.Path.StartsWith($Prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
      }
    }
    return $false
  } | Stop-Process -Force
  Start-Sleep -Milliseconds 300
}

if (-not $SkipBackend) {
  & (Join-Path $PSScriptRoot "build-backend.ps1")
}

if (-not (Test-Path -LiteralPath $PolishModel)) {
  throw "Polish model not found: $PolishModel. Run .\scripts\download-polish-model.ps1 first."
}

Stop-VernestBuildProcesses

Push-Location $Ui
try {
  if (-not (Test-Path -LiteralPath "node_modules")) {
    Invoke-Native "npm" @("ci")
  }
  Invoke-Native "npm" @("run", "build")
  Invoke-Native "npm" @("run", "tauri", "--", "build")
} finally {
  Pop-Location
}

if ($SkipPortable) {
  exit 0
}

New-Item -ItemType Directory -Force -Path $ReleaseRoot | Out-Null
Get-ChildItem -LiteralPath $ReleaseRoot -File -Filter "言栖_*_x64-setup.exe" -ErrorAction SilentlyContinue |
  Remove-Item -Force
Get-ChildItem -LiteralPath $ReleaseRoot -File -Filter "Vernest-*-windows-x64-portable.zip" -ErrorAction SilentlyContinue |
  Remove-Item -Force
if (Test-Path -LiteralPath $PortableDir) {
  Remove-Item -LiteralPath $PortableDir -Recurse -Force
}
if (Test-Path -LiteralPath $LegacyPortableDir) {
  Remove-Item -LiteralPath $LegacyPortableDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $PortableDir | Out-Null

$InstallerSource = Get-ChildItem -LiteralPath (Join-Path $TauriRelease "bundle\nsis") -Filter "*.exe" -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1
if ($InstallerSource) {
  $InstallerTarget = Join-Path $ReleaseRoot $InstallerSource.Name
  Copy-Item -LiteralPath $InstallerSource.FullName -Destination $InstallerTarget -Force
  Write-Host "Installer:" $InstallerTarget
}

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

if (Test-Path -LiteralPath $PolishModel) {
  $PolishTarget = Join-Path $PortableDir "models\polish"
  New-Item -ItemType Directory -Force -Path $PolishTarget | Out-Null
  Copy-Item -LiteralPath $PolishModel -Destination (Join-Path $PolishTarget (Split-Path -Leaf $PolishModel)) -Force
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
Write-Host "Portable app:" (Join-Path $PortableDir "Vernest.exe")
Write-Host "Portable package:" $PortableZip
