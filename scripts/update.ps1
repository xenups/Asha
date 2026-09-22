<#
.SYNOPSIS
    Asha-Harness update wrapper (Windows / PowerShell).
    Usage: powershell -File scripts/update.ps1 [-DryRun]
#>
[CmdletBinding()]
param([switch]$DryRun)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$Py = Join-Path $RepoRoot ".hermes\venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }

& $Py (Join-Path $RepoRoot "scripts\update.py") $(if ($DryRun) { "--dry-run" }) 
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }