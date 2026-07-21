# Watchdog for the persistent `opencode serve` process (port 4097) used by
# the group-bot LINE integration. Polls /doc every run; if it fails
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

$healthy = $false
try {
    $resp = Invoke-WebRequest -Uri "http://${OpenCodeServeHost}:${OpenCodeServePort}/doc" -TimeoutSec $ProbeTimeoutSeconds -UseBasicParsing -ErrorAction Stop
    if ($resp.StatusCode -eq 200) { $healthy = $true }
} catch {
    $healthy = $false
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

Write-Log "threshold reached, delegating to restart-opencode-serve.ps1"

# Delegate the actual kill+relaunch to the shared restart script (also used
# by app/services/opencode_agent.py's self-heal path) instead of duplicating
# that logic here - it already handles the G: transient-unavailable retry.
& "C:\Users\user\group-bot-scripts\restart-opencode-serve.ps1"

Write-Log "restart delegated"
"0" | Set-Content -Path $FailCountFile -Encoding UTF8
