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

# ---------------------------------------------------------------------------
# 第二層探測:正式模型鏈(GROUPBOT_MODEL_CHAIN)本身能不能回應使用者
# ---------------------------------------------------------------------------
# 2026-07-23:發現上面第一層探測(刻意用不受免費池限流影響的 nvidia 模型
# 當探針)只測得到「opencode serve 這個 process 有沒有卡死」,完全測不到
# 「正式機器人實際在用的模型鏈(GROUPBOT_MODEL_CHAIN,目前是 opencode-zen
# 的免費模型)本身被上游限流/帳號配額用盡」——這是兩條不相干的路徑。
# 2026-07-23 晚上使用者訊息真的完全沒回應超過一小時,但看門狗全程顯示
# 「recovered on its own」,因為第一層測的 nvidia 探針一直是通的。查
# opencode/uvicorn log 證實同一時段幾乎全是明確的限流/逾時,而且已經驗證
# 重啟對這種問題無效(手動重啟後這類錯誤計數器不降反升,代表是上游/帳號
# 層級的狀態,本機重啟清不掉)。
#
# 因此新增獨立的第二層:直接用正式鏈路第一顆模型送一則輕量探針,只在乎
# 「有沒有拿到乾淨的回覆內容」——逾時、錯誤回應、或收到 200 但內容裡含限流
# 關鍵字,都算退化。這一層失敗時**絕對不重啟**(重啟對上游限流無效,還會
# 腰斬正在處理中的真實請求),只累加自己獨立的計數器、寫/刷新獨立的告警
# 旗標檔、記 log,完全不影響第一層的計數器跟重啟判斷。
#
# 已知限制:.env 的 GROUPBOT_ADMIN_MODEL_CHAIN 第一顆目前也是免費模型
# (opencode/deepseek-v4-flash-free),代表免費池被限流時,機器人連透過
# LINE 私訊主動通知管理員本人都做不到——這一層能做的只有寫旗標檔 + 記
# log,無法「主動私訊通知使用者本人」,要人工去看旗標檔或 log 才會發現。
#
# 探測模型優先從 .env 的 GROUPBOT_MODEL_CHAIN 第一顆讀(用第一個 "/" 拆
# provider/model,跟 app/services/opencode_agent.py 的 _split_provider_model
# 拆法一致),讀取/解析失敗就退回下面的寫死預設值,並記一筆 log 說明用了
# fallback。
# 2026-07-24:使用者反映每 2 分鐘一次的第二層探測(真的送一則 ping 給正式
# 模型鏈第一顆,目前是 opencode-zen 免費帳號)會計入該帳號的呼叫次數/token
# 額度,要求避免這個行為。改成預設關閉,程式碼保留以便之後想重新啟用時
# 只要把這個開關改回 $true 即可,不用重寫邏輯。第一層(nvidia 探針)不受
# 影響,繼續每 2 分鐘跑,因為那不算 opencode-zen 額度。
$EnableProdChainProbe = $false

$GroupBotEnvFile = Join-Path $RepoPath ".env"
$ProdProbeModelProvider = "opencode"
$ProdProbeModelId = "deepseek-v4-flash-free"
$prodProbeModelSource = "hard-coded fallback"
try {
    if (Test-Path $GroupBotEnvFile) {
        $envLine = Get-Content $GroupBotEnvFile -ErrorAction Stop | Where-Object { $_ -match '^\s*GROUPBOT_MODEL_CHAIN\s*=' } | Select-Object -Last 1
        if ($envLine) {
            $chainValue = ($envLine -split '=', 2)[1].Trim()
            $firstModel = ($chainValue -split ',')[0].Trim()
            if ($firstModel -and $firstModel.Contains('/')) {
                $slashIndex = $firstModel.IndexOf('/')
                $ProdProbeModelProvider = $firstModel.Substring(0, $slashIndex)
                $ProdProbeModelId = $firstModel.Substring($slashIndex + 1)
                $prodProbeModelSource = ".env GROUPBOT_MODEL_CHAIN rung 0"
            }
        }
    }
} catch {
    # 解析失敗就靜靜留在上面的寫死預設值,下面會記一筆 log 交代原因。
}

# 逾時秒數對齊 .env 的 GROUPBOT_MODEL_TIMEOUT_SECONDS(目前 45 秒),讀不到
# 就用 45 當預設——跟正式機器人自己等多久才判定這顆模型逾時保持一致。
$ProdProbeTimeoutSeconds = 45
try {
    if (Test-Path $GroupBotEnvFile) {
        $timeoutLine = Get-Content $GroupBotEnvFile -ErrorAction Stop | Where-Object { $_ -match '^\s*GROUPBOT_MODEL_TIMEOUT_SECONDS\s*=' } | Select-Object -Last 1
        if ($timeoutLine) {
            $timeoutValue = ($timeoutLine -split '=', 2)[1].Trim()
            $parsedTimeout = 0
            if ([int]::TryParse($timeoutValue, [ref]$parsedTimeout) -and $parsedTimeout -gt 0) {
                $ProdProbeTimeoutSeconds = $parsedTimeout
            }
        }
    }
} catch {}

# 限流關鍵字跟 app/services/opencode_agent.py 的 _RATE_LIMIT_MARKERS 保持
# 一致(2026-07-23)——就算 HTTP 拿到 200,只要回應內容含這些字樣,也算
# 退化,不算健康。改動 app 那邊的清單時記得回來同步這裡。
$ProdProbeRateLimitMarkers = @(
    "429",
    "rate limit",
    "rate_limit",
    "quota",
    "too many requests",
    "worker local total request limit",
    "resourceexhausted",
    "resource_exhausted"
)

$ProdProbeMaxConsecutiveFailures = 3
$ProdProbeFailCountFile = "C:\Users\user\group-bot-prodprobe-failcount.txt"
$DegradedSinceFile = "C:\Users\user\group-bot-degraded-since.txt"
$DegradedFlagFile = "C:\Users\user\group-bot-DEGRADED-RATE-LIMITED.txt"
$DegradedFlagRefreshSeconds = 1800

function Invoke-ProdChainProbe($Base) {
    <#
        測「正式模型鏈第一顆模型」本身能不能回應使用者(第二層探測)。
        跟第一層極性相反:第一層只要 serve 有回 HTTP 就算通過(只在乎
        process 沒卡死);這裡要真的拿到乾淨的回覆內容才算健康——逾時、
        錯誤回應、或收到 200 但內容含限流關鍵字,都算退化。
        失敗時只回傳結果供呼叫端記錄/告警,這個函式本身絕對不會呼叫
        restart-opencode-serve.ps1,也不會動到第一層的 $FailCountFile /
        $LastRestartFile。
    #>
    $degraded = $false
    $reason = ""
    $sid2 = $null
    try {
        $sid2 = (Invoke-RestMethod -Uri "$Base/session" -Method Post -Body '{}' -ContentType 'application/json' -TimeoutSec 10 -ErrorAction Stop).id
        try {
            $body2 = @{
                parts = @(@{ type = 'text'; text = 'ping' })
                model = @{ providerID = $ProdProbeModelProvider; modelID = $ProdProbeModelId }
                agent = 'groupbot'
            } | ConvertTo-Json -Depth 6
            try {
                $msg2 = Invoke-RestMethod -Uri "$Base/session/$sid2/message" -Method Post -Body $body2 -ContentType 'application/json' -TimeoutSec $ProdProbeTimeoutSeconds -ErrorAction Stop
                $answerText = ""
                if ($msg2 -and $msg2.parts) {
                    $answerText = (($msg2.parts | Where-Object { $_.type -eq 'text' } | ForEach-Object { $_.text }) -join "")
                }
                $lowerAnswer = $answerText.ToLower()
                $hitMarker = $ProdProbeRateLimitMarkers | Where-Object { $lowerAnswer.Contains($_) } | Select-Object -First 1
                if ($hitMarker) {
                    $degraded = $true
                    $reason = "got HTTP 200 but response content looks rate-limited (matched '$hitMarker')"
                } elseif (-not $answerText) {
                    # 沒有任何 text part 也當退化(拿不到乾淨回覆)。
                    $degraded = $true
                    $reason = "got HTTP 200 but no text part in response"
                }
            } catch {
                # 跟第一層不同:這裡不區分「有 HTTP 回應」跟「完全沒回應」,
                # 逾時、4xx/5xx 錯誤回應,通通算退化——因為第二層在乎的是
                # 「使用者真的收不收得到回覆」,不是「process 有沒有卡死」。
                $degraded = $true
                $reason = "generation request failed: $($_.Exception.Message)"
            }
        } finally {
            try { Invoke-RestMethod -Uri "$Base/session/$sid2" -Method Delete -TimeoutSec 5 -ErrorAction SilentlyContinue | Out-Null } catch {}
        }
    } catch {
        $degraded = $true
        $reason = "couldn't even create a session for prod-chain probe: $($_.Exception.Message)"
    }

    return [PSCustomObject]@{ Degraded = $degraded; Reason = $reason }
}

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

# 第二層:正式模型鏈本身能不能回應使用者(獨立於上面第一層,只要 /doc 通過、
# serve process 確定還活著才有跑的意義)。失敗絕對不會觸發重啟,只寫旗標檔
# + log,不影響上面第一層的 $failCount / $healthy / 之後的重啟判斷。
if ($docOk -and $EnableProdChainProbe) {
    $prodProbeResult = Invoke-ProdChainProbe -Base $base

    $prodFailCount = 0
    if (Test-Path $ProdProbeFailCountFile) {
        $rawProd = Get-Content $ProdProbeFailCountFile -Raw -ErrorAction SilentlyContinue
        if ($rawProd) {
            $parsedProd = 0
            if ([int]::TryParse($rawProd.Trim(), [ref]$parsedProd)) { $prodFailCount = $parsedProd }
        }
    }

    if ($prodProbeResult.Degraded) {
        $prodFailCount = $prodFailCount + 1
        # 一旦超過門檻就不再無限往上累加(只是拿來判斷「有沒有連續失敗到
        # 門檻」跟驅動下面的防洗版刷新判斷,封頂在門檻值,log 才不會出現
        # 像 17/3 這種洗版式的數字,狀態仍然由 $DegradedSinceFile 準確追蹤)。
        if ($prodFailCount -gt $ProdProbeMaxConsecutiveFailures) { $prodFailCount = $ProdProbeMaxConsecutiveFailures }
        "$prodFailCount" | Set-Content -Path $ProdProbeFailCountFile -Encoding UTF8
        Write-Log "second-tier (prod chain $ProdProbeModelProvider/$ProdProbeModelId) probe failed: $($prodProbeResult.Reason) (consecutive=$prodFailCount/$ProdProbeMaxConsecutiveFailures)"

        if ($prodFailCount -ge $ProdProbeMaxConsecutiveFailures) {
            $nowEpoch2 = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
            $degradedSinceEpoch = 0
            if (Test-Path $DegradedSinceFile) {
                $rawSince = Get-Content $DegradedSinceFile -Raw -ErrorAction SilentlyContinue
                if ($rawSince) {
                    $parsedSince = 0.0
                    if ([double]::TryParse($rawSince.Trim(), [ref]$parsedSince)) { $degradedSinceEpoch = $parsedSince }
                }
            }
            $isNewlyDegraded = $false
            if ($degradedSinceEpoch -le 0) {
                $degradedSinceEpoch = $nowEpoch2
                "$degradedSinceEpoch" | Set-Content -Path $DegradedSinceFile -Encoding UTF8
                $isNewlyDegraded = $true
            }

            $lastFlagWriteEpoch = 0
            if (Test-Path $DegradedFlagFile) {
                $flagInfo = Get-Item $DegradedFlagFile -ErrorAction SilentlyContinue
                if ($flagInfo) { $lastFlagWriteEpoch = ([DateTimeOffset]$flagInfo.LastWriteTimeUtc).ToUnixTimeSeconds() }
            }
            $secondsSinceFlagWrite = $nowEpoch2 - $lastFlagWriteEpoch

            # 防洗版:旗標檔不是每 2 分鐘重寫,新進退化狀態立刻寫一次,之後
            # 每隔 $DegradedFlagRefreshSeconds(預設 30 分鐘)才刷新內容跟 log。
            if ($isNewlyDegraded -or -not (Test-Path $DegradedFlagFile) -or $secondsSinceFlagWrite -ge $DegradedFlagRefreshSeconds) {
                $durationMinutes = [Math]::Round(($nowEpoch2 - $degradedSinceEpoch) / 60, 1)
                $flagBody = @"
GroupBot 正式模型鏈退化告警(第二層探測)
================================================
偵測時間: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
已持續: 約 $durationMinutes 分鐘(自 $([DateTimeOffset]::FromUnixTimeSeconds($degradedSinceEpoch).ToLocalTime().ToString('yyyy-MM-dd HH:mm:ss')) 起)
探測用模型: $ProdProbeModelProvider/$ProdProbeModelId(來源: $prodProbeModelSource)
連續失敗次數: $prodFailCount(門檻 $ProdProbeMaxConsecutiveFailures)
最近一次失敗原因: $($prodProbeResult.Reason)

已採取動作: 僅記錄告警,未重啟 opencode serve(重啟對上游限流/帳號配額
用盡無效——已實測驗證,重啟後這類錯誤計數器不降反升,代表是上游/帳號
層級的狀態,本機重啟清不掉)。

已知限制: GROUPBOT_ADMIN_MODEL_CHAIN 第一顆目前同樣是免費模型,免費池
被限流時機器人連透過 LINE 私訊主動通知使用者本人都做不到,只能靠這個
旗標檔跟 $WatchdogLog 被人工發現。

第一層(opencode serve process 存活)探測結果: 正常(/doc 通過)。
"@
                $flagBody | Set-Content -Path $DegradedFlagFile -Encoding UTF8
                Write-Log "second-tier ALERT: prod model chain looks rate-limited/degraded for ~${durationMinutes}min, flag file refreshed at $DegradedFlagFile (not restarting - restart is known ineffective for upstream rate limits)"
            }
        }
    } else {
        if ($prodFailCount -gt 0) {
            Write-Log "second-tier (prod chain) recovered on its own (was $prodFailCount consecutive failed probes)"
        }
        "0" | Set-Content -Path $ProdProbeFailCountFile -Encoding UTF8
        if (Test-Path $DegradedSinceFile) { Remove-Item $DegradedSinceFile -Force -ErrorAction SilentlyContinue }
        if (Test-Path $DegradedFlagFile) {
            Remove-Item $DegradedFlagFile -Force -ErrorAction SilentlyContinue
            Write-Log "second-tier: prod model chain flag cleared - recovered"
        }
    }
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
