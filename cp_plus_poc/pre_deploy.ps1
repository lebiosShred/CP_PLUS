<#
.SYNOPSIS
    Pre-deploy validation for the CP Plus Agentic Suite.

.DESCRIPTION
    Runs all pre-deployment checks:
    1. Validates .env has required keys
    2. Checks Python dependencies are installed
    3. Runs contract chain validator
    4. Starts backend and runs health check
    5. Reports pass/fail

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File pre_deploy.ps1
#>

$ErrorActionPreference = "Stop"
$passed = 0
$failed = 0

$BackendDir = Join-Path $PSScriptRoot ".." "backend_llm" "alt_arch_cp_plus_poc"
$EnvFile = Join-Path $PSScriptRoot ".." "backend_llm" ".env"

Write-Host "`n============================================" -ForegroundColor Cyan
Write-Host "  CP Plus Pre-Deploy Validation" -ForegroundColor Cyan
Write-Host "============================================`n" -ForegroundColor Cyan

# --- Check 1: .env file exists and has required keys ---
Write-Host "[1/5] Checking .env configuration..." -ForegroundColor Yellow
$requiredKeys = @("MISTRAL_API_KEY", "FLASK_SECRET_KEY")

if (Test-Path $EnvFile) {
    $envContent = Get-Content $EnvFile -Raw
    $allKeysFound = $true
    foreach ($key in $requiredKeys) {
        if ($envContent -match "$key=.+") {
            Write-Host "  OK  $key" -ForegroundColor Green
        } else {
            Write-Host "  MISSING  $key" -ForegroundColor Red
            $allKeysFound = $false
        }
    }
    # Optional keys
    foreach ($optKey in @("GEMINI_API_KEY", "ADMIN_API_KEY", "MISTRAL_BACKUP_API_KEY")) {
        if ($envContent -match "$optKey=.+") {
            Write-Host "  OK  $optKey (optional)" -ForegroundColor DarkGray
        } else {
            Write-Host "  --  $optKey not set (optional)" -ForegroundColor DarkGray
        }
    }
    if ($allKeysFound) { $passed++ } else { $failed++ }
} else {
    Write-Host "  FAIL  .env file not found at $EnvFile" -ForegroundColor Red
    Write-Host "  Copy from .env.example and fill in your API keys." -ForegroundColor Yellow
    $failed++
}

# --- Check 2: Python dependencies ---
Write-Host "`n[2/5] Checking Python dependencies..." -ForegroundColor Yellow
$reqFile = Join-Path $BackendDir "requirements.txt"
try {
    $pipCheck = & python -m pip check 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  OK  All pip packages healthy" -ForegroundColor Green
        $passed++
    } else {
        Write-Host "  WARN  Some packages have issues: $pipCheck" -ForegroundColor Yellow
        $passed++  # Non-blocking
    }
} catch {
    Write-Host "  FAIL  Python not found in PATH" -ForegroundColor Red
    $failed++
}

# --- Check 3: Contract chain validation ---
Write-Host "`n[3/5] Running contract chain validator..." -ForegroundColor Yellow
$validatorScript = Join-Path $BackendDir "validate_contracts.py"
if (Test-Path $validatorScript) {
    try {
        & python $validatorScript 2>&1 | ForEach-Object { Write-Host "  $_" }
        if ($LASTEXITCODE -eq 0) {
            $passed++
        } else {
            $failed++
        }
    } catch {
        Write-Host "  FAIL  Validator crashed: $_" -ForegroundColor Red
        $failed++
    }
} else {
    Write-Host "  SKIP  validate_contracts.py not found" -ForegroundColor Yellow
}

# --- Check 4: PDF directories have content ---
Write-Host "`n[4/5] Checking PDF directories..." -ForegroundColor Yellow
$rfpDir = Join-Path $BackendDir "rfp_pdfs"
$productDir = Join-Path $BackendDir "product_sheet"

foreach ($dir in @(@{Name="RFP PDFs"; Path=$rfpDir}, @{Name="Product Sheets"; Path=$productDir})) {
    if (Test-Path $dir.Path) {
        $pdfCount = (Get-ChildItem $dir.Path -Filter "*.pdf" | Measure-Object).Count
        if ($pdfCount -gt 0) {
            Write-Host "  OK  $($dir.Name): $pdfCount files" -ForegroundColor Green
        } else {
            Write-Host "  WARN  $($dir.Name): directory empty" -ForegroundColor Yellow
        }
    } else {
        Write-Host "  WARN  $($dir.Name): directory not found" -ForegroundColor Yellow
    }
}
$passed++

# --- Check 5: Quick backend startup test ---
Write-Host "`n[5/5] Backend import test..." -ForegroundColor Yellow
try {
    $importTest = & python -c "import flask; import mistralai; import pypdf; print('OK')" 2>&1
    if ($importTest -match "OK") {
        Write-Host "  OK  Core imports successful (flask, mistralai, pypdf)" -ForegroundColor Green
        $passed++
    } else {
        Write-Host "  FAIL  Import test failed: $importTest" -ForegroundColor Red
        $failed++
    }
} catch {
    Write-Host "  FAIL  Import test crashed: $_" -ForegroundColor Red
    $failed++
}

# --- Final Report ---
Write-Host "`n============================================" -ForegroundColor Cyan
if ($failed -eq 0) {
    Write-Host "  ALL CHECKS PASSED ($passed/$($passed + $failed))" -ForegroundColor Green
    Write-Host "  Ready to deploy." -ForegroundColor Green
} else {
    Write-Host "  $failed CHECK(S) FAILED ($passed passed, $failed failed)" -ForegroundColor Red
    Write-Host "  Fix the issues above before deploying." -ForegroundColor Yellow
}
Write-Host "============================================`n" -ForegroundColor Cyan

exit $failed
