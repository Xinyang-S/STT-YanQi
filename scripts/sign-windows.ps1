param(
  [Parameter(Mandatory = $true)]
  [string]$Path,
  [string]$CertificatePath = $env:VERNEST_SIGN_CERT,
  [string]$CertificatePassword = $env:VERNEST_SIGN_PASSWORD,
  [string]$TimestampServer = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"

if (-not $CertificatePath) {
  Write-Host "No signing certificate configured. Set VERNEST_SIGN_CERT to enable signing."
  exit 0
}

if (-not (Test-Path -LiteralPath $Path)) {
  throw "File to sign not found: $Path"
}

$Password = $null
if ($CertificatePassword) {
  $Password = ConvertTo-SecureString $CertificatePassword -AsPlainText -Force
}

$Params = @{
  FilePath = $Path
  CertificatePath = $CertificatePath
  TimestampServer = $TimestampServer
  HashAlgorithm = "SHA256"
}
if ($Password) {
  $Params["CertificatePassword"] = $Password
}

Set-AuthenticodeSignature @Params | Format-List
