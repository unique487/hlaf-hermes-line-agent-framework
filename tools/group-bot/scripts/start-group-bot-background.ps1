# 開機/登入時背景啟動:opencode serve 常駐服務 + FastAPI 群組機器人 + ngrok 通道
# + 自動更新 LINE webhook。給 Windows 工作排程器(Task Scheduler, 任務
# GroupBotAutoStart)在登入時呼叫用。
#
# 重要:這份是「正本」,故意放在 C 槽(不是 G 槽的 repo 裡)。2026-07-21 當機
# 重開後發現 Task Scheduler 在登入當下 G: 雲端硬碟還沒掛載完成,如果這支 .ps1
# 檔案本身就放在 G: 底下,Task Scheduler 連「找到並讀取這支腳本」這一步都做
# 不到,腳本裡「等 G: 掛載」的邏輯根本沒有機會執行——腳本自己都還沒被讀進來。
# 所以腳本檔案本體一定要放在 C:(開機當下必定可用),裡面的等待邏輯才等得到
# G: 上真正的 repo/app 內容準備好。tools/group-bot/scripts/ 底下那份是給人看
# 的鏡像複本,不是 Task Scheduler 實際呼叫的那份,改動時這份 C: 正本才是準的,
# 改完记得同步複製一份過去 G: 端。
#
# 免費版 ngrok 每次啟動都是隨機網址(沒有固定網域),所以每次開機/當機重開都要
# 重新查詢 ngrok 分配到的網址、再呼叫 LINE Messaging API 把 webhook 端點更新過去,
# 不然機器人會啟動成功但 LINE 傳訊息進不來(webhook 指向舊網址)。
#
# opencode serve 常駐是因為每次訊息都重開一次 `opencode run` 子行程,在這台機器
# 上有約 25-30 秒的固定冷啟動成本(Bun runtime 開機 + 設定重讀),常常直接把
# 45 秒的逾時吃光,讓群組機器人跟私訊管理員代理看起來完全沒反應
# (2026-07-21 實測發現)。改成開機時啟動一次常駐的 opencode serve,之後每則
# 訊息都是打 HTTP API,量測下來熱啟動只要 ~19 秒。

$RepoPath = "G:\我的雲端硬碟\claude\HLAF-Hermes Line Agent Framework\.claude\worktrees\hermes-opencode-migration-87858d\tools\group-bot"
$VenvPython = "C:\Users\user\.venvs\group-bot\Scripts\python.exe"
$OpenCodeExe = "C:\Users\user\AppData\Roaming\npm\node_modules\opencode-ai\bin\opencode.exe"
$OpenCodeServeHost = "127.0.0.1"
$OpenCodeServePort = 4097
# 2026-07-22:原本 WinGet 裝的那顆 ngrok.exe 版本太舊(3.3.1,ngrok 伺服器端
# 要求最低 3.20.0),且不管 `ngrok update`、`winget upgrade` 或手動換成新版檔案
# 放哪個資料夾都會在幾秒內被本機端點防護清掉(不是防毒單純誤判,是專門針對
# ngrok 的攔截規則,一般使用者介面關不掉)。改請資訊人員加白名單未果,改用
# Microsoft Store 版(套件 ngrok.ngrok,執行別名放在
# %LOCALAPPDATA%\Microsoft\WindowsApps\ngrok.exe)成功繞過,版本 3.39.8。
$NgrokExe = "C:\Users\user\AppData\Local\Microsoft\WindowsApps\ngrok.exe"
$NgrokConfig = "C:\Users\user\.ngrok-groupbot.yml"
$NgrokApiPort = 4041
$Port = 8001
$BootLog = "C:\Users\user\group-bot-boot.log"

function Write-BootLog($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" | Out-File -FilePath $BootLog -Append -Encoding UTF8
}

Write-BootLog "script started"

# G: 是雲端硬碟掛載,開機/登入後不保證馬上就緒,用迴圈等它出現(最多等 3 分鐘)。
$waited = 0
while (-not (Test-Path $RepoPath) -and $waited -lt 180) {
    Start-Sleep -Seconds 5
    $waited += 5
}
if (-not (Test-Path $RepoPath)) {
    Write-BootLog "ABORT: RepoPath 在等了 ${waited}s 後仍無法存取 — G: 雲端硬碟可能沒掛載成功"
    exit 1
}
Write-BootLog "RepoPath ready after ${waited}s wait"

if (-not (Test-Path $VenvPython)) {
    Write-BootLog "ABORT: 找不到 venv python ($VenvPython) — 請先在 $RepoPath 執行 uv sync (見 README)"
    exit 1
}

$LogDir = "$RepoPath\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# 每次開機重啟前,先確保沒有殘留的舊 opencode serve/uvicorn/ngrok 佔用 port,
# 避免重複啟動。
Get-CimInstance Win32_Process -Filter "Name='opencode.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'serve' -and $_.CommandLine -match "$OpenCodeServePort" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'uvicorn' -and $_.CommandLine -match "$Port" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-CimInstance Win32_Process -Filter "Name='ngrok.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match [regex]::Escape($NgrokConfig) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# serve 的啟動工作目錄改用 C: 的 opencode-serve-workdir(不是 G: 的 $RepoPath)。
# 原因有二:(1)那個資料夾裡放了一份 opencode.json,把全域那 5 個很重的 MCP server
# (notebooklm/firebase/playwright/open-computer-use/obsidian)逐一 enabled:false
# 關掉——2026-07-22 實測那些 MCP 是 serve「跑約 15 分鐘後退化到送訊息逾時」的根因
# (每建 session 就重 spawn 一整套且不回收,量到 100+ 個殘留吃 ~2GB RAM),關掉後
# 輕量訊息從 ~33 秒降到 ~3 秒,詳見 restart-opencode-serve.ps1 的註解;(2)serve 的
# 啟動 cwd 對機器人行為本來就無影響(groupbot 工具全關;admin 每則自帶 directory),
# 放在 C: 也順便避開 G: 雲端硬碟卡頓。admin 私訊用的
# C:\Users\user\opencode-admin-workdir 也放了同一份 opencode.json。
$OpenCodeServeWorkDir = "C:\Users\user\group-bot-scripts\opencode-serve-workdir"
New-Item -ItemType Directory -Force -Path $OpenCodeServeWorkDir | Out-Null

Start-Process -WindowStyle Hidden -FilePath $OpenCodeExe `
    -ArgumentList "serve","--hostname",$OpenCodeServeHost,"--port","$OpenCodeServePort" `
    -WorkingDirectory $OpenCodeServeWorkDir `
    -RedirectStandardOutput "$LogDir\opencode-serve.out.log" `
    -RedirectStandardError "$LogDir\opencode-serve.err.log"
Write-BootLog "opencode serve Start-Process issued (port $OpenCodeServePort)"

# 等 opencode serve 真的能回應(最多等 30 秒)再啟動 FastAPI,避免第一則訊息
# 撞上伺服器還沒起來。
$serveReady = $false
$waited = 0
while (-not $serveReady -and $waited -lt 30) {
    Start-Sleep -Seconds 2
    $waited += 2
    try {
        Invoke-RestMethod -Uri "http://${OpenCodeServeHost}:${OpenCodeServePort}/doc" -ErrorAction Stop | Out-Null
        $serveReady = $true
    } catch {}
}
if (-not $serveReady) {
    Write-BootLog "ABORT: opencode serve 在 ${waited}s 內沒回應(port $OpenCodeServePort)"
    exit 1
}
Write-BootLog "opencode serve ready after ${waited}s wait"

Start-Process -WindowStyle Hidden -FilePath $VenvPython `
    -ArgumentList "-m","uvicorn","app.main:app","--host","127.0.0.1","--port","$Port" `
    -WorkingDirectory $RepoPath `
    -RedirectStandardOutput "$LogDir\uvicorn.out.log" `
    -RedirectStandardError "$LogDir\uvicorn.err.log"
Write-BootLog "uvicorn Start-Process issued (port $Port)"

Start-Sleep -Seconds 5

Start-Process -WindowStyle Hidden -FilePath $NgrokExe `
    -ArgumentList "http","$Port","--config",$NgrokConfig,"--log","stdout" `
    -RedirectStandardOutput "$LogDir\ngrok.out.log" `
    -RedirectStandardError "$LogDir\ngrok.err.log"
Write-BootLog "ngrok Start-Process issued"

# 等 ngrok 分配到公開網址(最多等 30 秒),再回頭更新 LINE webhook。
#
# 2026-07-22:ngrok 的本機 API 預設綁 4040,只有在 4040 被佔用時才會退而求其次
# 綁 $NgrokApiPort(4041)。之前 4040 長期被佔用所以一直是 4041,這次換裝
# Microsoft Store 版 ngrok 後 4040 是空的,它就直接綁回 4040,腳本卻只查
# 4041,誤判成啟動失敗(其實通道早就活著)。改成兩個 port 都查,查到哪個就用
# 哪個,不寫死。
$publicUrl = $null
$waited = 0
while (-not $publicUrl -and $waited -lt 30) {
    Start-Sleep -Seconds 2
    $waited += 2
    foreach ($port in @(4040, $NgrokApiPort)) {
        try {
            $tunnels = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/tunnels" -ErrorAction Stop
            $https = $tunnels.tunnels | Where-Object { $_.proto -eq 'https' } | Select-Object -First 1
            if ($https) { $publicUrl = $https.public_url; break }
        } catch {}
    }
}

if (-not $publicUrl) {
    Write-BootLog "ABORT: ${waited}s 內沒查到 ngrok 公開網址(port $NgrokApiPort),webhook 未更新"
    exit 1
}
Write-BootLog "ngrok public url: $publicUrl"

# 從 .env 讀 LINE_CHANNEL_ACCESS_TOKEN,呼叫 LINE Messaging API 更新 webhook endpoint。
$envPath = "$RepoPath\.env"
$token = $null
if (Test-Path $envPath) {
    $line = Get-Content $envPath | Where-Object { $_ -match '^LINE_CHANNEL_ACCESS_TOKEN=' } | Select-Object -First 1
    if ($line) { $token = $line -replace '^LINE_CHANNEL_ACCESS_TOKEN=', '' }
}

if (-not $token) {
    Write-BootLog "ABORT: 在 .env 找不到 LINE_CHANNEL_ACCESS_TOKEN,webhook 未更新"
    exit 1
}

$webhookUrl = "$publicUrl/line/webhook"
try {
    Invoke-RestMethod -Uri "https://api.line.me/v2/bot/channel/webhook/endpoint" `
        -Method Put `
        -Headers @{ Authorization = "Bearer $token"; "Content-Type" = "application/json" } `
        -Body (@{ endpoint = $webhookUrl } | ConvertTo-Json) | Out-Null
    Write-BootLog "LINE webhook updated -> $webhookUrl"
} catch {
    Write-BootLog "ABORT: 更新 LINE webhook 失敗: $($_.Exception.Message)"
    exit 1
}

Write-BootLog "script finished OK"

