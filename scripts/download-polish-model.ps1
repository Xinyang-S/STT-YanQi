param(
  [string]$Destination,
  [switch]$AppData
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")
$ModelName = "qwen2.5-0.5b-instruct-q4_k_m.gguf"
$ExpectedSize = 491400032
$Url = "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/$ModelName"

if ($AppData) {
  $Destination = Join-Path $env:APPDATA "Vernest\models\polish\$ModelName"
} elseif (-not $Destination) {
  $Destination = Join-Path $Root "models\polish\$ModelName"
}

$DestinationDir = Split-Path -Parent $Destination
New-Item -ItemType Directory -Force -Path $DestinationDir | Out-Null

Write-Host "Downloading Vernest polish model:"
Write-Host "  $Url"
Write-Host "  -> $Destination"
Invoke-WebRequest -Uri $Url -OutFile $Destination

$Size = (Get-Item -LiteralPath $Destination).Length
if ($Size -ne $ExpectedSize) {
  throw "Unexpected model size: $Size bytes, expected $ExpectedSize bytes"
}
Write-Host "Downloaded $ModelName ($Size bytes)"
