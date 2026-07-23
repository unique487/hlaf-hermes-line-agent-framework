# Pure restart action for the persistent `opencode serve` process (port 4097).
# No polling/threshold logic here - that lives in watchdog-opencode-serve.ps1
# (a periodic outside-in health check) and, more importantly, directly in
# app/services/opencode_agent.py, which now calls this script the moment
# every rung in a fallback chain fails (that "all rungs failed" event is a
# much more direct signal of "opencode serve is actually stuck" than any
# external /doc probe can see - /doc keeps responding fine even while a
# specific in-flight tool call has wedged message processing, so the
# polling watchdog alone can miss this failure mode for a long time).
#
# Only touches opencode serve. Does not touch uvicorn/ngrok/LINE webhook.
#
# IMPORTANT (2026-07-21 incident #2): this used to launch opencode serve
# with its working directory AND its log files on G: (the cloud-drive
# mount, same path as tools/group-bot). That created a circular failure:
# G: stalling is exactly what wedges opencode serve's message processing
# in the first place, so when G: also stalled during the restart itself,
# this script's own Test-Path check on the G: path timed out and aborted
# the restart - leaving the service down until G: recovered on its own.
# Fixed by launching opencode serve with an all-local (C:) working
# directory and log path, so restarting it no longer depends on G: being
# responsive at all. This is safe: the "groupbot" agent has every file/bash
# tool disabled in its definition (see groupbot.md frontmatter), so the
# server's own launch cwd is irrelevant to it; the "admin" agent always
# passes its own explicit `directory` (G:\...\LINE-admin) per request
# regardless of what the server was launched in. Neither depends on this
# script's working directory - only the restart mechanics do, and now they
# don't touch G: either.

$OpenCodeServeHost = "127.0.0.1"
$OpenCodeServePort = 4097
$OpenCodeExe = "C:\Users\user\AppData\Roaming\npm\node_modules\opencode-ai\bin\opencode.exe"
$LocalWorkDir = "C:\Users\user\group-bot-scripts\opencode-serve-workdir"
$LocalLogDir = "C:\Users\user\group-bot-scripts\logs"
$RestartLog = "C:\Users\user\group-bot-restart.log"

function Write-Log($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" | Out-File -FilePath $RestartLog -Append -Encoding UTF8
}

if (Test-Path $RestartLog) {
    $existingLines = Get-Content $RestartLog -ErrorAction SilentlyContinue
    if ($existingLines -and $existingLines.Count -gt 500) {
        $existingLines[-500..-1] | Set-Content -Path $RestartLog -Encoding UTF8
    }
}

Write-Log "restart requested"

$stuckProcesses = Get-CimInstance Win32_Process -Filter "Name='opencode.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'serve' -and $_.CommandLine -match "$OpenCodeServePort" }
foreach ($proc in $stuckProcesses) {
    Write-Log "killing opencode serve PID $($proc.ProcessId)"
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 2

New-Item -ItemType Directory -Force -Path $LocalWorkDir | Out-Null
New-Item -ItemType Directory -Force -Path $LocalLogDir | Out-Null

Start-Process -WindowStyle Hidden -FilePath $OpenCodeExe -ArgumentList "serve","--hostname",$OpenCodeServeHost,"--port","$OpenCodeServePort" -WorkingDirectory $LocalWorkDir -RedirectStandardOutput "$LocalLogDir\opencode-serve.out.log" -RedirectStandardError "$LocalLogDir\opencode-serve.err.log"

$ready = $false
$waited = 0
while (-not $ready -and $waited -lt 30) {
    Start-Sleep -Seconds 2
    $waited += 2
    try {
        Invoke-RestMethod -Uri "http://${OpenCodeServeHost}:${OpenCodeServePort}/doc" -TimeoutSec 5 -ErrorAction Stop | Out-Null
        $ready = $true
    } catch {}
}

if ($ready) {
    Write-Log "restart complete, ready after ${waited}s"
} else {
    Write-Log "restart issued but not responding after ${waited}s"
}
