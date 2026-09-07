<#
Registers the agent's schedule in Windows Task Scheduler.

Task Scheduler rather than Cloud Scheduler + Pub/Sub on purpose: if the PC is off
for six hours, Pub/Sub retains six trigger messages and delivers them all at once
on reconnect, producing a burst of applications -- the exact pattern that gets
LinkedIn accounts flagged. Task Scheduler simply fires when the machine is awake
and skips what it missed. The rolling 24h Firestore cap remains the real limit.

Run from an elevated PowerShell:
    powershell -ExecutionPolicy Bypass -File setup\register_tasks.ps1

Remove with:
    Unregister-ScheduledTask -TaskName "JobAgent-Run","JobAgent-Digest","JobAgent-Slack" -Confirm:$false
#>

$ErrorActionPreference = "Stop"
$Root   = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$PyW    = Join-Path $Root ".venv\Scripts\pythonw.exe"

if (-not (Test-Path $Python)) { throw "python not found at $Python" }
Write-Host "project root: $Root"

function Register-AgentTask {
    param(
        [string]$Name,
        [string]$Exe,
        [string]$Args,
        [Microsoft.Management.Infrastructure.CimInstance[]]$Triggers,
        [string]$Description
    )
    $action = New-ScheduledTaskAction -Execute $Exe -Argument $Args -WorkingDirectory $Root
    # Run only on AC power? No -- a laptop on battery should still work. But do not
    # wake the machine: a run that never happens is strictly safer than a burst.
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable:$false `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 1)
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Host "  replaced existing task $Name"
    }
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Triggers `
        -Settings $settings -Principal $principal -Description $Description | Out-Null
    Write-Host "  registered $Name"
}

# --- Workflow A: every 2 hours between 08:00 and 22:00 IST -------------------
# Every 2h rather than hourly: Chromium is heavy, and 7 runs/day clears the
# 20-application cap comfortably while looking far less mechanical.
$runTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddHours(8) `
    -RepetitionInterval (New-TimeSpan -Hours 2) -RepetitionDuration (New-TimeSpan -Hours 14)
Register-AgentTask -Name "JobAgent-Run" -Exe $Python -Args "-m agent.main run" `
    -Triggers @($runTrigger) `
    -Description "LinkedIn Easy Apply pass. Respects agent_config.dry_run and the rolling 24h cap."

# --- Workflow C: 21:00 IST daily --------------------------------------------
$digestTrigger = New-ScheduledTaskTrigger -Daily -At "21:00"
Register-AgentTask -Name "JobAgent-Digest" -Exe $Python -Args "-m agent.main digest" `
    -Triggers @($digestTrigger) `
    -Description "Daily resume gap report + XLSX export to GCS."

# --- Slack listener: at logon, windowless -----------------------------------
# pythonw avoids a permanent console window. Socket Mode reconnects on its own.
$slackTrigger = New-ScheduledTaskTrigger -AtLogOn
Register-AgentTask -Name "JobAgent-Slack" -Exe $PyW -Args "-m agent.main slack" `
    -Triggers @($slackTrigger) `
    -Description "Slack Socket Mode listener: Approve/Reject buttons and slash commands."

Write-Host ""
Write-Host "registered tasks:"
Get-ScheduledTask -TaskName "JobAgent-*" |
    Select-Object TaskName, State |
    Format-Table -AutoSize
Write-Host "Start the Slack listener now without logging out:"
Write-Host "    Start-ScheduledTask -TaskName JobAgent-Slack"
