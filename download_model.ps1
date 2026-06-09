# 下载 SenseVoice 多语种 int8 模型 (~120MB)
# 第一次运行前执行一次即可

$ErrorActionPreference = "Stop"
$modelDir = "models"
$modelName = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
$url = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/$modelName.tar.bz2"
$tar = "$modelDir\$modelName.tar.bz2"

if (-not (Test-Path $modelDir)) {
    New-Item -ItemType Directory -Path $modelDir | Out-Null
}

# 1. 优先检查镜像 (国内网络)
$mirrors = @(
    "https://hf-mirror.com/k2-fsa/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/resolve/main/$modelName.tar.bz2",
    "https://hf-mirror.com/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/resolve/main/$modelName.tar.bz2"
)

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  下载 SenseVoice 多语种模型" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

if (Test-Path "$modelDir\$modelName") {
    Write-Host "[✓] 模型已存在: $modelDir\$modelName" -ForegroundColor Green
    exit 0
}

Write-Host "正在下载 (~120MB)..." -ForegroundColor Yellow
$downloaded = $false

# 尝试镜像
foreach ($mirror in $mirrors) {
    Write-Host "  尝试镜像: $mirror"
    try {
        Invoke-WebRequest -Uri $mirror -OutFile $tar -UseBasicParsing -TimeoutSec 120
        $downloaded = $true
        Write-Host "  [✓] 镜像下载成功" -ForegroundColor Green
        break
    } catch {
        Write-Host "  [×] 镜像失败: $($_.Exception.Message)" -ForegroundColor Red
    }
}

# 镜像失败 → 走 GitHub
if (-not $downloaded) {
    Write-Host "  尝试 GitHub: $url"
    try {
        Invoke-WebRequest -Uri $url -OutFile $tar -UseBasicParsing -TimeoutSec 180
        $downloaded = $true
    } catch {
        Write-Host "  [×] GitHub 失败: $($_.Exception.Message)" -ForegroundColor Red
    }
}

if (-not $downloaded) {
    Write-Host ""
    Write-Host "[!] 自动下载失败，请手动下载：" -ForegroundColor Red
    Write-Host "    $url"
    Write-Host "    解压到 $modelDir\$modelName\"
    exit 1
}

Write-Host ""
Write-Host "正在解压..." -ForegroundColor Yellow
try {
    & tar -xjf $tar -C $modelDir
    Remove-Item $tar
    Write-Host "[✓] 完成! 模型已安装到: $modelDir\$modelName" -ForegroundColor Green
} catch {
    Write-Host "[!] 解压失败: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
