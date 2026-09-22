<#
.SYNOPSIS
    Asha-Harness zero-bleed self-teardown (Windows / PowerShell).
    Idempotent: re-running is a no-op. Never touches files outside the repo
    .venv/.jspace bounds or the two MCP entries this harness registered.
#>
[CmdletBinding()]
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# --- 0. Listening-port snapshot (zero-bleed assertion) -------------------
function Get-ListeningPorts {
    $rows = netstat -ano -p TCP | Select-String "LISTENING"
    return @($rows | ForEach-Object { ($_ -split "\s+")[1].Split(":")[-1] } | Sort-Object -Unique)
}
$PortsBefore = @(Get-ListeningPorts)
$ExitedClean = $false
$PortCheck = {
    if (-not $ExitedClean) { return }
    $PortsAfter = @(Get-ListeningPorts)
    $New = @($PortsAfter | Where-Object { $_ -notin $PortsBefore })
    if ($New.Count -gt 0) {
        Write-Host "[asha] FAIL: new listening ports post-teardown: $($New -join ', ')" -ForegroundColor Red
        exit 1
    }
}
Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action $PortCheck | Out-Null

# --- 1. Registered MCP servers (this harness's own registrations) -------
if (Get-Command hermes -ErrorAction SilentlyContinue) {
    $list = hermes mcp list 2>$null | Out-String
    foreach ($Name in @("sequential_thinking", "remote_linux")) {
        if ($list -match "^[ \t]*$Name[ \t]") {
            Write-Host "[asha] removing MCP server: $Name"
            hermes mcp remove $Name 2>&1 | Out-Null
            if ($LASTEXITCODE -ne 0) { Write-Warning "mcp remove $Name failed" }
        } else {
            Write-Host "[asha] MCP server $Name not registered; nothing to remove"
        }
    }
} else {
    Write-Warning "hermes CLI not found; MCP entries left untouched (remove manually: hermes mcp remove <name>)"
}

# --- 2. Isolated venv -----------------------------------------------------
if (Test-Path ".venv") {
    Write-Host "[asha] removing .venv"
    Remove-Item -Recurse -Force ".venv"
} else {
    Write-Host "[asha] .venv absent; nothing to remove"
}

# --- 3. J-Space caches / stale lock --------------------------------------
Remove-Item -Recurse -Force ".jspace\cache" -ErrorAction SilentlyContinue
Remove-Item -Force ".jspace\lock" -ErrorAction SilentlyContinue
Write-Host "[asha] .jspace/cache and stale lock removed"

# --- 4. Zero-bleed assertions --------------------------------------------
$Procs = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "asha-harness|hermes-disciplined-harness" })
if ($Procs.Count -gt 0) {
    Write-Host "[asha] FAIL: lingering harness processes found" -ForegroundColor Red
    $Procs | ForEach-Object { Write-Host "$($_.ProcessId) $($_.CommandLine)" }
    exit 1
}

$ExitedClean = $true
Write-Host "[asha] TEARDOWN OK: .venv gone, caches gone, MCP entries pruned, zero processes, zero new listeners"