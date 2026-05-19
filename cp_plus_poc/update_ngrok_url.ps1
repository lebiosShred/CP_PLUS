<#
.SYNOPSIS
    Automatically updates ngrok tunnel URL in both OpenAPI spec files.

.DESCRIPTION
    Reads the active ngrok tunnel URL from the local ngrok API (http://localhost:4040),
    then patches both cp_plus_tools_unified.yaml and master_openapi.json with the new URL.
    Run this script every time ngrok restarts to keep the contract chain in sync.

.EXAMPLE
    .\update_ngrok_url.ps1
    .\update_ngrok_url.ps1 -NgrokApiUrl "http://localhost:4041"
#>

param(
    [string]$NgrokApiUrl = "http://localhost:4040"
)

$ErrorActionPreference = "Stop"

# --- File paths (relative to repo root) ---
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$DeployedSpec = Join-Path $RepoRoot "cp_plus_poc\cp_plus_tools_unified.yaml"
$MasterSpec = Join-Path $RepoRoot "backend_llm\alt_arch_cp_plus_poc\master_openapi.json"

Write-Host "`n=== CP Plus ngrok URL Updater ===" -ForegroundColor Cyan

# --- Step 1: Get active tunnel URL from ngrok API ---
Write-Host "`n[1/3] Querying ngrok API at $NgrokApiUrl..." -ForegroundColor Yellow
try {
    $tunnels = Invoke-RestMethod -Uri "$NgrokApiUrl/api/tunnels" -TimeoutSec 5
} catch {
    Write-Host "ERROR: Cannot connect to ngrok API at $NgrokApiUrl" -ForegroundColor Red
    Write-Host "Make sure ngrok is running: ngrok http 8100" -ForegroundColor Yellow
    exit 1
}

# Find the HTTPS tunnel
$httpsTunnel = $tunnels.tunnels | Where-Object { $_.proto -eq "https" } | Select-Object -First 1
if (-not $httpsTunnel) {
    Write-Host "ERROR: No HTTPS tunnel found. Available tunnels:" -ForegroundColor Red
    $tunnels.tunnels | ForEach-Object { Write-Host "  - $($_.proto): $($_.public_url)" }
    exit 1
}

$NewUrl = $httpsTunnel.public_url
Write-Host "  Found tunnel: $NewUrl" -ForegroundColor Green

# --- Step 2: Update cp_plus_tools_unified.yaml ---
Write-Host "`n[2/3] Updating deployed spec: $DeployedSpec" -ForegroundColor Yellow

if (-not (Test-Path $DeployedSpec)) {
    Write-Host "  WARNING: File not found at $DeployedSpec" -ForegroundColor Red
} else {
    $yamlContent = Get-Content $DeployedSpec -Raw
    $updatedYaml = $yamlContent -replace 'https://[a-z0-9\-]+\.ngrok-free\.app', $NewUrl
    if ($updatedYaml -ne $yamlContent) {
        Set-Content -Path $DeployedSpec -Value $updatedYaml -NoNewline
        Write-Host "  Updated successfully." -ForegroundColor Green
    } else {
        Write-Host "  Already up to date." -ForegroundColor DarkGray
    }
}

# --- Step 3: Update master_openapi.json ---
Write-Host "`n[3/3] Updating master spec: $MasterSpec" -ForegroundColor Yellow

if (-not (Test-Path $MasterSpec)) {
    Write-Host "  WARNING: File not found at $MasterSpec" -ForegroundColor Red
} else {
    $jsonContent = Get-Content $MasterSpec -Raw
    $updatedJson = $jsonContent -replace 'https://[a-z0-9\-]+\.ngrok-free\.app', $NewUrl
    if ($updatedJson -ne $jsonContent) {
        Set-Content -Path $MasterSpec -Value $updatedJson -NoNewline
        Write-Host "  Updated successfully." -ForegroundColor Green
    } else {
        Write-Host "  Already up to date." -ForegroundColor DarkGray
    }
}

Write-Host "`n=== Done ===" -ForegroundColor Cyan
Write-Host "Both specs now point to: $NewUrl" -ForegroundColor Green
Write-Host "Remember to re-upload cp_plus_tools_unified.yaml to watsonx Orchestrate if deployed.`n" -ForegroundColor Yellow
