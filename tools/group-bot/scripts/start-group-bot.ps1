# 一鍵啟動(前景,手動測試用):FastAPI 群組機器人 + ngrok 通道
# 用法: ./scripts/start-group-bot.ps1
# 開機自動啟動走 start-group-bot-background.ps1(由工作排程器 GroupBotAutoStart 呼叫),
# 這支是給你手動在終端機盯 log 用的。

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = "C:\Users\user\.venvs\group-bot\Scripts\python.exe"
$OpenCodeExe = "C:\Users\user\AppData\Roaming\npm\node_modules\opencode-ai\bin\opencode.exe"
$OpenCodeServeHost = "127.0.0.1"
$OpenCodeServePort = 4097
$NgrokConfig = "C:\Users\user\.ngrok-groupbot.yml"
$Port = 8001

if (-not (Test-Path "$RepoRoot\.env")) {
    Write-Host "❌ 找不到 .env,請先: copy .env.example .env 並填入金鑰" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $VenvPython)) {
    Write-Host "❌ 找不到 venv python ($VenvPython),請先執行:" -ForegroundColor Red
    Write-Host "   `$env:UV_PROJECT_ENVIRONMENT='C:\Users\user\.venvs\group-bot'; uv sync" -ForegroundColor Yellow
    exit 1
}
if (-not (Get-Command ngrok -ErrorAction SilentlyContinue)) {
    Write-Host "❌ 找不到 ngrok,請先安裝: winget install ngrok.ngrok" -ForegroundColor Red
    exit 1
}

Write-Host "🧠 啟動 opencode serve 常駐服務 (port $OpenCodeServePort)..." -ForegroundColor Cyan
$serveProc = Start-Process -PassThru -NoNewWindow -FilePath $OpenCodeExe `
    -ArgumentList "serve","--hostname",$OpenCodeServeHost,"--port","$OpenCodeServePort" `
    -WorkingDirectory $RepoRoot
Start-Sleep -Seconds 3

Write-Host "🚀 啟動 FastAPI (port $Port)..." -ForegroundColor Cyan
$server = Start-Process -PassThru -NoNewWindow -FilePath $VenvPython `
    -ArgumentList "-m","uvicorn","app.main:app","--host","127.0.0.1","--port","$Port" `
    -WorkingDirectory $RepoRoot

Write-Host "🌐 啟動 ngrok 通道(groupbot 帳號)..." -ForegroundColor Cyan
Write-Host "   啟動後記得看 ngrok 印出的網址,把 <網址>/line/webhook 貼回 LINE Developers Console," -ForegroundColor Yellow
Write-Host "   或直接跑 start-group-bot-background.ps1 讓它自動更新 webhook。" -ForegroundColor Yellow
try {
    ngrok http $Port --config $NgrokConfig
} finally {
    Write-Host "🛑 關閉 FastAPI + opencode serve..." -ForegroundColor Cyan
    Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    Stop-Process -Id $serveProc.Id -Force -ErrorAction SilentlyContinue
}

