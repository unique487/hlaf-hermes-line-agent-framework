# 一鍵啟動 Hermes:FastAPI 伺服器 + ngrok 通道
# 用法: ./scripts/start-hermes.ps1 [-Domain your-static-domain.ngrok-free.app]
param(
    [string]$Domain = $env:NGROK_DOMAIN,
    [int]$Port = 8000
)

if (-not (Test-Path ".env")) {
    Write-Host "❌ 找不到 .env,請先: copy .env.example .env 並填入金鑰" -ForegroundColor Red
    exit 1
}
if (-not (Get-Command ngrok -ErrorAction SilentlyContinue)) {
    Write-Host "❌ 找不到 ngrok,請先安裝: winget install ngrok.ngrok" -ForegroundColor Red
    exit 1
}

Write-Host "🚀 啟動 FastAPI (port $Port)..." -ForegroundColor Cyan
$server = Start-Process -PassThru -NoNewWindow uv -ArgumentList "run","uvicorn","app.main:app","--host","127.0.0.1","--port","$Port"

Write-Host "🌐 啟動 ngrok 通道..." -ForegroundColor Cyan
try {
    if ($Domain) {
        Write-Host "   Webhook 網址: https://$Domain/line/webhook" -ForegroundColor Green
        ngrok http $Port --domain $Domain
    } else {
        Write-Host "   (未指定固定網域,ngrok 會給隨機網址,記得去 LINE 後台更新 webhook)" -ForegroundColor Yellow
        ngrok http $Port
    }
} finally {
    Write-Host "🛑 關閉 FastAPI..." -ForegroundColor Cyan
    Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
}
