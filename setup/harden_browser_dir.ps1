<#
Creates the Chromium install directory with a protected DACL.

Patchright validates the ACL of every ancestor of its browser path and refuses to
launch if any of them grants an untrusted SID the right to remove or re-permission
the directory below it. On this machine two candidate locations fail that check:

  C:\Users\rajiv\AppData   grants an AppContainer package SID (S-1-15-3-...)
  D:\                      grants Authenticated Users (S-1-5-11)

Ancestry passes for anything directly under C:\, but the directory itself must also
have inheritance DISABLED ("protected") and carry only trusted SIDs. That is what
/inheritance:r plus three explicit grants achieves.

Run once:
    powershell -ExecutionPolicy Bypass -File setup\harden_browser_dir.ps1
#>
$ErrorActionPreference = "Stop"
$dir = "C:\pw-browsers"

if (-not (Test-Path $dir)) {
    New-Item -ItemType Directory -Path $dir | Out-Null
    Write-Host "created $dir"
} else {
    Write-Host "$dir already exists"
}

# /inheritance:r removes all inherited ACEs, which is what makes the DACL
# "protected"; the grants then restore only the three trusted identities.
icacls $dir /inheritance:r `
    /grant:r "$($env:USERNAME):(OI)(CI)F" `
    /grant:r "SYSTEM:(OI)(CI)F" `
    /grant:r "Administrators:(OI)(CI)F" | Out-Null

Write-Host "resulting ACL:"
icacls $dir

Write-Host ""
Write-Host "Now install Chromium into it:"
Write-Host '  $env:PLAYWRIGHT_BROWSERS_PATH="C:\pw-browsers"; .venv\Scripts\patchright.exe install chromium'
