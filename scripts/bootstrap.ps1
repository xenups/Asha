<#
.SYNOPSIS
    Asha-Harness one-shot bootstrap (Windows / PowerShell).
    Idempotent, fail-closed (any failed step throws).
.PARAMETER SkipMcp
    Set to $true to skip MCP server registration.
#>
[CmdletBinding()]
param(
    [switch]$SkipMcp
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot
$Venv = ".venv"
$Py = Join-Path $RepoRoot ".venv\Scripts\python.exe"

Write-Host "[asha] root: $RepoRoot"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "FATAL: python not found on PATH"
}

# --- 1. Isolated virtual environment -------------------------------------
if (Test-Path $Py) {
    Write-Host "[asha] venv exists; keeping it"
} else {
    Write-Host "[asha] creating venv"
    python -m venv $Venv
    if (-not (Test-Path $Py)) { throw "FATAL: venv creation failed" }
}

# --- 2. Pinned ABI deps + toolchain --------------------------------------
Write-Host "[asha] installing pinned toolchain"
& $Py -m pip install --quiet --upgrade pip
& $Py -m pip install --quiet `
    "tree-sitter==0.21.3" `
    "tree-sitter-languages==1.10.2" `
    "ast-grep-py==0.45.3" `
    "ruff" `
    "mypy" `
    "pytest" `
    "chromadb" `
    "mem0ai"

# --- 3. Fail-closed pin verification --------------------------------------
Write-Host "[asha] verifying pinned ABI matrix"
& $Py asha\code_search.py --verify-env
if ($LASTEXITCODE -ne 0) { throw "FATAL: --verify-env failed; toolchain is not the pinned matrix" }
& $Py asha\code_search.py --self-test
if ($LASTEXITCODE -ne 0) { throw "FATAL: code_search self-test failed" }
& $Py asha\diff_engine.py --self-test
if ($LASTEXITCODE -ne 0) { throw "FATAL: diff_engine self-test failed" }

# --- 4. MCP servers over stdio (optional) ---------------------------------
if ($SkipMcp) {
    Write-Host "[asha] skipping MCP registration (-SkipMcp)"
} else {
    if (Get-Command hermes -ErrorAction SilentlyContinue) {
        Write-Host "[asha] registering MCP servers over stdio via Hermes CLI"
        & hermes mcp add sequential_thinking --command npx `
            --args "-y" --args "@modelcontextprotocol/server-sequential-thinking" `
            --connect-timeout 30 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Warning "sequential_thinking add failed (pre-registered?) — verify with hermes mcp list" }
        & hermes mcp add remote_linux --command npx `
            --args "-y" --args "mcp-server-ssh" `
            --connect-timeout 30 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Warning "remote_linux add failed (pre-registered?) — verify with hermes mcp list" }
        Write-Host "[asha] MCP servers registered; verify with: hermes mcp list && hermes mcp test <name>"
    } else {
        Write-Warning "hermes CLI not on PATH; MCP registration skipped (re-run after installing Hermes)"
    }
}

# --- 5. Gate assertion -----------------------------------------------------
Write-Host "[asha] running gates"
& $Py -m ruff check .;  if ($LASTEXITCODE -ne 0) { throw "GATE FAIL: ruff" }
& $Py -m mypy asha\ .jspace\control.py tests\; if ($LASTEXITCODE -ne 0) { throw "GATE FAIL: mypy" }
& $Py -m pytest tests\ -q; if ($LASTEXITCODE -ne 0) { throw "GATE FAIL: pytest" }

Write-Host "[asha] BOOTSTRAP OK: venv=$Venv, pins verified, gates green"
Write-Host "[asha] next: python .jspace\control.py --transport <ssh|local> init --goal '<G>' --next '<N>'"