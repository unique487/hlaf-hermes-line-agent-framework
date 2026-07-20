# 開機/登入時背景啟動:FastAPI 伺服器 + ngrok 通道(都隱藏視窗)
# 給 Windows工作排程器(Task Scheduler)在登入時呼叫用。

$RepoPath = "G:\我的雲端硬碟\claude\HLAF-Hermes Line Agent Framework\.claude\worktrees\hermes-agent-official-account-833679"
$NgrokExe = "C:\Users\user\AppData\Local\Microsoft\WinGet\Packages\Ngrok.Ngrok_Microsoft.Winget.Source_8wekyb3d8bbwe\ngrok.exe"
$NgrokConfig = "C:\Users\user\.ngrok-hermes.yml"
$UvExe = "C:\Users\user\AppData\Local\hermes\bin\uv.exe"
$LogDir = "$RepoPath\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

Start-Sleep -Seconds 10  # 給網路/雲端硬碟掛載一點時間再啟動

Start-Process -WindowStyle Hidden -FilePath $UvExe `
    -ArgumentList "run","uvicorn","app.main:app","--host","127.0.0.1","--port","8000" `
    -WorkingDirectory $RepoPath `
    -RedirectStandardOutput "$LogDir\uvicorn.out.log" `
    -RedirectStandardError "$LogDir\uvicorn.err.log"

Start-Sleep -Seconds 5

Start-Process -WindowStyle Hidden -FilePath $NgrokExe `
    -ArgumentList "http","8000","--config",$NgrokConfig `
    -RedirectStandardOutput "$LogDir\ngrok.out.log" `
    -RedirectStandardError "$LogDir\ngrok.err.log"
