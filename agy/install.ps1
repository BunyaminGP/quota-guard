# install.ps1 — standalone Windows installer for quota-guard's Antigravity part
# (hooks + the agy-run launcher). Mirrors the repo layout, so the root
# hooks.json paths (./agy/scripts/...) and ../common/resume_core.py work as-is.
$ErrorActionPreference = "Stop"

$SrcDir = Split-Path -Parent $MyInvocation.MyCommand.Path   # <repo>\agy
$RepoDir = Split-Path -Parent $SrcDir                       # <repo>
$TargetDir = if ($env:AGY_PLUGIN_INSTALL_DIR) {
    [System.IO.Path]::GetFullPath($env:AGY_PLUGIN_INSTALL_DIR)
} else {
    Join-Path $HOME ".agents\plugins\quota-guard"
}
$BinDir = if ($env:AGY_BIN_INSTALL_DIR) {
    [System.IO.Path]::GetFullPath($env:AGY_BIN_INSTALL_DIR)
} else {
    Join-Path $HOME ".local\bin"
}
$AgyDir = Join-Path $TargetDir "agy"

foreach ($sub in @(".antigravity-plugin", "common", "agy\scripts", "agy\bin", "agy\locales", "agy\templates", "agy\shell")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $TargetDir $sub) | Out-Null
}
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

Copy-Item (Join-Path $RepoDir ".antigravity-plugin\plugin.json") (Join-Path $TargetDir ".antigravity-plugin\plugin.json") -Force
Copy-Item (Join-Path $RepoDir "hooks.json") (Join-Path $TargetDir "hooks.json") -Force
Copy-Item (Join-Path $RepoDir "common\resume_core.py") (Join-Path $TargetDir "common\resume_core.py") -Force
Copy-Item (Join-Path $SrcDir "scripts\*.py") (Join-Path $AgyDir "scripts\") -Force
Copy-Item (Join-Path $SrcDir "bin\*") (Join-Path $AgyDir "bin\") -Force
Copy-Item (Join-Path $SrcDir "locales\*.json") (Join-Path $AgyDir "locales\") -Force
Copy-Item (Join-Path $SrcDir "templates\progress.md") (Join-Path $AgyDir "templates\progress.md") -Force
Copy-Item (Join-Path $SrcDir "shell\*") (Join-Path $AgyDir "shell\") -Force

$CmdShim = @"
@echo off
setlocal
if not defined AGY_QUOTA_GUARD_HOME set "AGY_QUOTA_GUARD_HOME=$AgyDir"
call "%AGY_QUOTA_GUARD_HOME%\bin\agy-run.cmd" %*
exit /b %ERRORLEVEL%
"@
Set-Content -Path (Join-Path $BinDir "agy-run.cmd") -Value $CmdShim -Encoding Ascii

$ShTarget = $AgyDir.Replace('\', '/')
$ShShim = @"
#!/usr/bin/env bash
export AGY_QUOTA_GUARD_HOME="`$:AGY_QUOTA_GUARD_HOME:-$ShTarget}"
exec "`$AGY_QUOTA_GUARD_HOME/bin/agy-run" "`$@"
"@
$ShShim = $ShShim.Replace('`$:AGY_QUOTA_GUARD_HOME', '${AGY_QUOTA_GUARD_HOME')
Set-Content -Path (Join-Path $BinDir "agy-run") -Value $ShShim -Encoding Utf8

Write-Host "Installed quota-guard (Antigravity) to: $TargetDir"
Write-Host "Installed launcher to:                  $BinDir\agy-run.cmd"
