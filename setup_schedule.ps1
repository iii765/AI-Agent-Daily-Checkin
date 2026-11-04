$ErrorActionPreference = "Stop"
$TaskName = "AgentAppsDailyCheckin"
$ScriptPath = Join-Path $PSScriptRoot "auto_checkin.py"
$PythonW = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $PythonW) {
    $PythonW = (Get-Command python.exe).Source
}

Write-Host "Configuring Daily Auto Check-in..." -ForegroundColor Cyan
Write-Host "Executable : $PythonW"
Write-Host "Script Path: $ScriptPath"

# 1. Register Daily 00:05 AM Task via schtasks (no Admin required)
$TaskCmd = "`"$PythonW`" `"$ScriptPath`""
schtasks /Create /SC DAILY /TN $TaskName /TR $TaskCmd /ST 00:05 /F | Out-Null

# 2. Create User Startup Shortcut (runs silently on Windows Logon without Admin required)
$StartupDir = [Environment]::GetFolderPath("Startup")
$ShortcutPath = Join-Path $StartupDir "AgentAppsDailyCheckin.lnk"
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $PythonW
$Shortcut.Arguments = "`"$ScriptPath`""
$Shortcut.WorkingDirectory = $PSScriptRoot
$Shortcut.WindowStyle = 7
$Shortcut.Description = "Daily auto check-in for MiniMax Code, WorkBuddy, and TRAE CN"
$Shortcut.Save()

# 3. Patch Desktop/StartMenu Shortcuts with --remote-debugging-port so running apps never need restart at 00:05!
$PortMap = @{
    "MiniMax Code.lnk" = "--remote-debugging-port=19221"
    "WorkBuddy.lnk"    = "--remote-debugging-port=19222"
    "TRAE CN.lnk"      = "--remote-debugging-port=19223"
}
$SearchDirs = @(
    [Environment]::GetFolderPath("Desktop"),
    [Environment]::GetFolderPath("Programs")
)
foreach ($dir in $SearchDirs) {
    if (Test-Path $dir) {
        $lnks = Get-ChildItem -Path $dir -Filter *.lnk -Recurse -ErrorAction SilentlyContinue
        foreach ($lnk in $lnks) {
            if ($PortMap.ContainsKey($lnk.Name)) {
                $flag = $PortMap[$lnk.Name]
                $sc = $WshShell.CreateShortcut($lnk.FullName)
                if ($sc.Arguments -notmatch "remote-debugging-port") {
                    $sc.Arguments = ($sc.Arguments + " " + $flag).Trim()
                    $sc.Save()
                    Write-Host "[OK] Enabled non-disruptive CDP port on shortcut: $($lnk.FullName)" -ForegroundColor Cyan
                }
            }
        }
    }
}

Write-Host ""
Write-Host "[OK] Scheduled Task [$TaskName] (Daily 00:05) registered!" -ForegroundColor Green
Write-Host "[OK] Logon Startup Shortcut created at: $ShortcutPath" -ForegroundColor Green
