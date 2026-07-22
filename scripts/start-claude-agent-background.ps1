# 開機/登入時背景啟動:FastAPI 伺服器 + ngrok 通道(都隱藏視窗)
# 給 Windows工作排程器(Task Scheduler)在登入時呼叫用。

$RepoPath = "G:\我的雲端硬碟\claude\HLAF-Hermes Line Agent Framework\.claude\worktrees\hermes-agent-official-account-833679"
# 2026-07-22:原本 WinGet 裝的 ngrok.exe 因為本機端點防護攔截、版本太舊等
# 一連串問題已停用,改用 Microsoft Store 版(執行別名),詳見 group-bot 那支
# start-group-bot-background.ps1 同一天的修改記錄。
$NgrokExe = "C:\Users\user\AppData\Local\Microsoft\WindowsApps\ngrok.exe"
$NgrokConfig = "C:\Users\user\.ngrok-hermes.yml"
$UvExe = "C:\Users\user\AppData\Local\hermes\bin\uv.exe"
$BootLog = "C:\Users\user\claude-agent-boot.log"

function Write-BootLog($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" | Out-File -FilePath $BootLog -Append -Encoding UTF8
}

Write-BootLog "script started"

# G: 是雲端硬碟掛載,開機/登入後不保證馬上就緒,用迴圈等它出現(最多等 3 分鐘),
# 不要用固定 Start-Sleep 賭時間(之前賭 10 秒在真的開機時不夠,腳本整個空跑失敗)。
$waited = 0
while (-not (Test-Path $RepoPath) -and $waited -lt 180) {
    Start-Sleep -Seconds 5
    $waited += 5
}

if (-not (Test-Path $RepoPath)) {
    Write-BootLog "ABORT: RepoPath still not accessible after ${waited}s wait — G: 雲端硬碟可能沒掛載成功"
    exit 1
}
Write-BootLog "RepoPath ready after ${waited}s wait"

$LogDir = "$RepoPath\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

Start-Process -WindowStyle Hidden -FilePath $UvExe `
    -ArgumentList "run","uvicorn","app.main:app","--host","127.0.0.1","--port","8000" `
    -WorkingDirectory $RepoPath `
    -RedirectStandardOutput "$LogDir\uvicorn.out.log" `
    -RedirectStandardError "$LogDir\uvicorn.err.log"
Write-BootLog "uvicorn Start-Process issued"

Start-Sleep -Seconds 5

Start-Process -WindowStyle Hidden -FilePath $NgrokExe `
    -ArgumentList "http","8000","--config",$NgrokConfig `
    -RedirectStandardOutput "$LogDir\ngrok.out.log" `
    -RedirectStandardError "$LogDir\ngrok.err.log"
Write-BootLog "ngrok Start-Process issued"
