# Watchdog for the persistent `opencode serve` process (port 4097) used by
# the group-bot LINE integration. Each run does a two-stage health check:
# (1) ping /doc for HTTP liveness, then (2) actually send a tiny generation
# request and confirm the message pipeline answers within
# $GenProbeTimeoutSeconds. Stage 2 is the important one - /doc keeps
# returning 200 even when the serve has degraded to the point where sending
# a message hangs (the exact failure mode seen 2026-07-21/22), so a
# liveness-only probe never catches it. If either stage fails
# $MaxConsecutiveFailures times in a row (default 3, run every 2 minutes by
# the "GroupBotWatchdog" scheduled task -> ~6 minutes of real downtime),
# kills and restarts just the opencode serve process. Does NOT touch
# uvicorn/ngrok/the LINE webhook - those aren't the ones that get stuck.
#
# Background (2026-07-21): admin DM sometimes returned "all models failed"
# even though it wasn't a real multi-provider outage - logs showed every
# model rung timing out at its own full timeout ceiling, consistent with a
# single stuck tool call (the admin agent has full bash/read/write access)
# wedging the whole opencode serve process rather than the models actually
# being down. app/services/opencode_agent.py deliberately does not call the
# session abort endpoint on a per-rung timeout (that caused a worse cascade
# in testing), so a genuinely stuck request can only be cleared by
# restarting opencode serve itself - which is what this script automates.
#
# Canonical copy lives on C: (same reasoning as start-group-bot-background.ps1:
# G: is a cloud-drive mount not guaranteed to be ready at boot). The copy
# under tools/group-bot/scripts/ is a mirror for visibility - keep both in
# sync when editing.

$OpenCodeServeHost = "127.0.0.1"
$OpenCodeServePort = 4097
$OpenCodeExe = "C:\Users\user\AppData\Roaming\npm\node_modules\opencode-ai\bin\opencode.exe"
$RepoPath = "G:\我的雲端硬碟\claude\HLAF-Hermes Line Agent Framework\.claude\worktrees\hermes-opencode-migration-87858d\tools\group-bot"
$WatchdogLog = "C:\Users\user\group-bot-watchdog.log"
$FailCountFile = "C:\Users\user\group-bot-watchdog-failcount.txt"
$MaxConsecutiveFailures = 3
$ProbeTimeoutSeconds = 8
# 除了 ping /doc(只證明 HTTP 伺服器活著),還會實際送一則極輕量的生成請求,量「訊息
# 處理管線」有沒有真的在合理時間內回應。2026-07-22 之前的看門狗只 ping /doc,而
# /doc 在 serve 退化(worker 卡滿/送訊息逾時)時仍回 200,所以永遠抓不到「活著但
# 處理不動」這個最常見的故障。健康的 serve 這則探針約 13-20 秒(見下方探針模型
# 選擇的說明);超過下面的秒數就判定退化。靠「連續 N 次才重啟」的門檻加上下面的
# 最短存活時間護欄,雙重吸收偶發的單次逾時,不會亂重啟。
$GenProbeTimeoutSeconds = 45
# 2026-07-22:探針模型從 opencode/deepseek-v4-flash-free(opencode-zen 免費
# 池)換成直連 NVIDIA 的付費模型。實測發現免費池被限流到繁忙時,不是快速
# 回錯誤,而是完全 hang 到逾時連一點回應都沒有(curl 直測 26 秒 HTTP code
# 000)——用這種模型當探針,不管怎麼調整下面的錯誤分類邏輯都沒用,因為
# 「serve 健康但這顆免費模型忙到不回應」跟「serve 真的卡死」在 HTTP 層
# 呈現的症狀是一樣的(都是沒有任何回應)。改用直連 nvidia/deepseek-ai/
# deepseek-v4-flash,不受免費池限流影響,才能真正只測「serve 這個 process
# 有沒有卡死」。
# 逾時秒數:多次實測這顆模型走 groupbot agent(內含相關性判斷推理步驟)
# 的真實反應時間落在 13-22 秒,一開始估 25 秒、後來拉到 30 秒都還是曾經
# 被單次探針卡到逾時(22.5 秒那次已經很貼近 30 秒門檻)——不是模型壞掉,
# 是這個 agent 的推理開銷本來就佔掉大半反應時間,安全邊際要留夠,索性直接
# 對齊群組機器人自己 app/config.py 的 groupbot_model_timeout_seconds(45
# 秒),不再自己另外估一個更緊的數字。
$GenProbeModelProvider = "nvidia"
$GenProbeModelId = "deepseek-ai/deepseek-v4-flash"

# 2026-07-22:發現這支腳本本身就是造成「回覆異常慢」的元兇——探針模型
# opencode/deepseek-v4-flash-free 常被上游 opencode-zen 免費池限流,限流
# 時 serve 其實仍活著、很快就回應(HTTP 錯誤狀態或內嵌 error 文字),但舊版
# 邏輯把任何 Invoke-RestMethod 例外(不管是真的逾時連不上,還是伺服器很快
# 回了一個錯誤狀態碼)都當成「serve 不健康」。結果變成每 2 分鐘跑一次、連
# 3 次判定不健康就重啟——而免費模型被限流常常一限就是連續好幾輪,於是
# 每 6 分鐘左右就把一台其實健康的 serve 砍掉重啟一次,正在處理中的真實
# LINE 請求也跟著被腰斬。查 opencode.log 證實:同一時段 173 次
# 「Worker local total request limit reached」+ 621 次「Rate limit
# exceeded」都是上游限流,不是 serve 本身卡死;serve 本身直接測(換一個
# 沒被限流的 model)0.1~13 秒內都能正常回應。
#
# 修法:區分「serve 有回應(就算是錯誤狀態碼)」跟「serve 完全沒回應」。
# 前者代表 serve 這個 process 活著、HTTP 層在動,只是上游模型繁忙——這不
# 該被當成「serve 退化」處理,重啟對上游限流毫無幫助,只會腰斬其他請求。
# 只有真正連不上(連線被拒)或整個掛住到連 HTTP 回應都生不出來,才算不
# 健康。另外加一個「最短存活時間」護欄:serve 剛重啟不久就不能再被這支
# 腳本二度重啟,避免萬一分類邏輯還有漏洞,也不會連環砍。
$MinServeUptimeSecondsBeforeRestart = 600
$LastRestartFile = "C:\Users\user\group-bot-watchdog-last-restart.txt"

function Write-Log($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" | Out-File -FilePath $WatchdogLog -Append -Encoding UTF8
}

# Keep the log from growing forever - trim to the last 500 lines.
if (Test-Path $WatchdogLog) {
    $existingLines = Get-Content $WatchdogLog -ErrorAction SilentlyContinue
    if ($existingLines -and $existingLines.Count -gt 500) {
        $trimmed = $existingLines[-500..-1]
        $trimmed | Set-Content -Path $WatchdogLog -Encoding UTF8
    }
}

$failCount = 0
if (Test-Path $FailCountFile) {
    $raw = Get-Content $FailCountFile -Raw -ErrorAction SilentlyContinue
    if ($raw) {
        $parsed = 0
        if ([int]::TryParse($raw.Trim(), [ref]$parsed)) { $failCount = $parsed }
    }
}

$base = "http://${OpenCodeServeHost}:${OpenCodeServePort}"

# 第一關:/doc liveness(HTTP 伺服器有沒有活著)。
$docOk = $false
try {
    $resp = Invoke-WebRequest -Uri "$base/doc" -TimeoutSec $ProbeTimeoutSeconds -UseBasicParsing -ErrorAction Stop
    if ($resp.StatusCode -eq 200) { $docOk = $true }
} catch {
    $docOk = $false
}

# 第二關(只有 /doc 通過才做):實際送一則輕量生成,量處理管線有沒有真的在動。
# 這才是能抓到「/doc 200 但送訊息卡死」退化的關鍵。整段包在 try 裡,任何例外
# (逾時、連線失敗、回傳格式不對)都當成不健康。
$healthy = $false
if ($docOk) {
    try {
        $sid = (Invoke-RestMethod -Uri "$base/session" -Method Post -Body '{}' -ContentType 'application/json' -TimeoutSec 10 -ErrorAction Stop).id
        try {
            $body = @{
                parts = @(@{ type = 'text'; text = 'ping' })
                model = @{ providerID = $GenProbeModelProvider; modelID = $GenProbeModelId }
                agent = 'groupbot'
            } | ConvertTo-Json -Depth 6
            try {
                $msg = Invoke-RestMethod -Uri "$base/session/$sid/message" -Method Post -Body $body -ContentType 'application/json' -TimeoutSec $GenProbeTimeoutSeconds -ErrorAction Stop
                # 有拿到 assistant 訊息物件(不論內容是不是 (silent))就算處理管線活著。
                if ($msg) { $healthy = $true }
            } catch {
                # 2026-07-22:關鍵區分。$_.Exception.Response 不是 $null,代表
                # opencode serve 確實在合理時間內回了一個 HTTP 回應(即使是
                # 4xx/5xx 錯誤狀態,例如上游模型被限流時 serve 會很快回傳
                # 錯誤,而不是掛著不回應)——這代表 serve process 本身活著、
                # HTTP 層正常運作,只是這次探針剛好打到一個暫時繁忙/被限流
                # 的上游模型,不代表 serve 退化。只有完全沒有 Response(真的
                # 連不上,或整個逾時到連錯誤回應都生不出來)才算不健康。
                if ($_.Exception.Response) {
                    $healthy = $true
                    Write-Log "generation probe got HTTP error response ($($_.Exception.Response.StatusCode)) - treating as serve alive, likely upstream model busy/rate-limited"
                } else {
                    $healthy = $false
                    Write-Log "generation probe got no response at all: $($_.Exception.Message)"
                }
            }
        } finally {
            try { Invoke-RestMethod -Uri "$base/session/$sid" -Method Delete -TimeoutSec 5 -ErrorAction SilentlyContinue | Out-Null } catch {}
        }
    } catch {
        $healthy = $false
        Write-Log "generation probe failed (couldn't even create a session): $($_.Exception.Message)"
    }
} else {
    Write-Log "/doc liveness probe failed"
}

if ($healthy) {
    if ($failCount -gt 0) { Write-Log "recovered on its own (was $failCount consecutive failed probes)" }
    "0" | Set-Content -Path $FailCountFile -Encoding UTF8
    exit 0
}

$failCount = $failCount + 1
"$failCount" | Set-Content -Path $FailCountFile -Encoding UTF8
Write-Log "probe failed (consecutive=$failCount/$MaxConsecutiveFailures)"

if ($failCount -lt $MaxConsecutiveFailures) {
    exit 0
}

Write-Log "threshold reached"

# 2026-07-22:最短存活時間護欄。就算上面的分類邏輯還有沒堵到的漏洞,也不
# 讓這支腳本無限連環重啟——剛重啟過的 serve 在保護窗口內即使又被判定
# 「不健康」,也只記錄、不動手重啟,把 failCount 歸零重新累計,避免重演
# 這次「每 6 分鐘砍一次」的狀況。
$lastRestartEpoch = 0
if (Test-Path $LastRestartFile) {
    $raw = Get-Content $LastRestartFile -Raw -ErrorAction SilentlyContinue
    if ($raw) {
        $parsed = 0.0
        if ([double]::TryParse($raw.Trim(), [ref]$parsed)) { $lastRestartEpoch = $parsed }
    }
}
$nowEpoch = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$secondsSinceLastRestart = $nowEpoch - $lastRestartEpoch
if ($lastRestartEpoch -gt 0 -and $secondsSinceLastRestart -lt $MinServeUptimeSecondsBeforeRestart) {
    Write-Log "threshold reached but only ${secondsSinceLastRestart}s since last watchdog restart (min ${MinServeUptimeSecondsBeforeRestart}s) - skipping restart, resetting fail count"
    "0" | Set-Content -Path $FailCountFile -Encoding UTF8
    exit 0
}

Write-Log "delegating to restart-opencode-serve.ps1"

# Delegate the actual kill+relaunch to the shared restart script (also used
# by app/services/opencode_agent.py's self-heal path) instead of duplicating
# that logic here - it already handles the G: transient-unavailable retry.
& "C:\Users\user\group-bot-scripts\restart-opencode-serve.ps1"

Write-Log "restart delegated"
"0" | Set-Content -Path $FailCountFile -Encoding UTF8
"$nowEpoch" | Set-Content -Path $LastRestartFile -Encoding UTF8
