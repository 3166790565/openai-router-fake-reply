# 智能部署（Windows PowerShell 版）：检测源码更新才重建，否则只 ensure 运行
# 用法：.\deploy.ps1   （在服务器项目根目录执行）
# 依赖：git, docker compose
param(
    [string]$Branch = "main",
    [string]$Remote = "origin"
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "==> 拉取远端信息..." -ForegroundColor Cyan
git fetch $Remote $Branch --quiet

$Local  = git rev-parse HEAD
$RemoteHead = git rev-parse "$Remote/$Branch"

Write-Host "    本地:  $($Local.Substring(0,8))"
Write-Host "    远端:  $($RemoteHead.Substring(0,8))"

if ($Local -eq $RemoteHead) {
    Write-Host "==> 源码无更新，仅确保容器运行（不重建）" -ForegroundColor Yellow
    docker compose up -d --no-build
    Write-Host "==> 完成：无变更" -ForegroundColor Green
    exit 0
}

Write-Host "==> 检测到源码更新，开始拉取并重建..." -ForegroundColor Yellow
git pull $Remote $Branch --ff-only

Write-Host "==> 重建镜像并启动（注入 git 版本号）..." -ForegroundColor Cyan
$env:GIT_COMMIT = git rev-parse HEAD
$env:GIT_BRANCH = git rev-parse --abbrev-ref HEAD
docker compose up -d --build

Write-Host "==> 清理悬空旧镜像..." -ForegroundColor DarkGray
docker image prune -f 2>$null | Out-Null

Write-Host "==> 完成：已更新到 $($RemoteHead.Substring(0,8))" -ForegroundColor Green
docker compose ps
