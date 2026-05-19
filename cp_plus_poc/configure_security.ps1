
# Parse command line arguments
param (
    [switch]$Verbose = $false
)

# Debug function that only prints in verbose mode
function Debug-Print {
    param (
        [string]$Message
    )
    if ($Verbose) {
        Write-Host "DEBUG: $Message" -ForegroundColor Blue
    }
}

# Output directory (created on demand by Test-OutputDirectory)
$OUTPUT_DIR = "wxo_security_config"

# Display welcome message
Write-Host "Welcome to the IBM watsonx Orchestrate Embedded Chat Security Configuration Tool" -ForegroundColor White
Write-Host ""
Write-Host "This tool will guide you through configuring security for your embedded chat integration."
Write-Host ""
Write-Host "IMPORTANT: By default, security is enabled but not configured, which means Embed Chat will not function until properly configured." -ForegroundColor Yellow
Write-Host ""

# Function to check and create output directory
function Test-OutputDirectory {
    # Check if directory exists
    if (-not (Test-Path -Path $OUTPUT_DIR -PathType Container)) {
        Write-Host "Output directory '$OUTPUT_DIR' does not exist. Creating it now..." -ForegroundColor Yellow
        # Try to create the directory
        try {
            New-Item -Path $OUTPUT_DIR -ItemType Directory -ErrorAction Stop | Out-Null
        }
        catch {
            Write-Host "ERROR: Failed to create output directory '$OUTPUT_DIR'." -ForegroundColor Red
            Write-Host "Please check permissions or create the directory manually:" -ForegroundColor Yellow
            Write-Host "    New-Item -Path $OUTPUT_DIR -ItemType Directory"
            return $false
        }
    }
    
    # Verify directory is writable
    try {
        $testFile = Join-Path -Path $OUTPUT_DIR -ChildPath "test_write.tmp"
        [System.IO.File]::WriteAllText($testFile, "test")
        Remove-Item -Path $testFile -Force
    }
    catch {
        Write-Host "ERROR: Output directory '$OUTPUT_DIR' is not writable." -ForegroundColor Red
        Write-Host "Please check permissions." -ForegroundColor Yellow
        return $false
    }
    
    return $true
}

# Function to get user input with validation
function Get-UserInput {
    param (
        [string]$Prompt,
        [string]$VarName,
        [bool]$IsSecret = $false
    )
    
    $value = ""
    while ([string]::IsNullOrEmpty($value)) {
        if ($IsSecret) {
            $secureString = Read-Host -Prompt $Prompt -AsSecureString
            $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureString)
            $value = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
            [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
        else {
            $value = Read-Host -Prompt $Prompt
        }
        
        if ([string]::IsNullOrEmpty($value)) {
            Write-Host "This field cannot be empty. Please try again." -ForegroundColor Yellow
        }
    }
    
    # Return the value
    return $value
}

# Function to display help for finding instance ID and API URL
function Show-InstanceIdHelp {
    Write-Host ""
    Write-Host "How to Find Your Instance ID and API URL:" -ForegroundColor White
    Write-Host "1. Log in to your watsonx Orchestrate instance"
    Write-Host "2. Click on the profile icon in the top right corner"
    Write-Host "3. Select 'Settings' from the dropdown menu"
    Write-Host "4. Navigate to the 'API Details' tab"
    Write-Host "5. Find the 'Service instance URL' field, which looks like:"
    Write-Host "   https://api.us-south.watson-orchestrate.ibm.com/instances/20250807-1007-4445-5049-459a42144389" -ForegroundColor Blue
    Write-Host "6. Your API URL is the base URL: https://api.us-south.watson-orchestrate.ibm.com" -ForegroundColor Blue
    Write-Host "7. Your Instance ID is the UUID after '/instances/': 20250807-1007-4445-5049-459a42144389" -ForegroundColor Blue
    Write-Host ""
    Write-Host "Your API Key can also be found in the same API Details tab."
    Write-Host "Press Enter to continue..."
    Read-Host | Out-Null
}

# Function to select environment
function Select-Environment {
    # Default to Production environment
    $script:ENVIRONMENT = "PROD"
    $script:IAMURL = "https://iam.platform.saas.ibm.com"
    
    Write-Host ""
    Write-Host "Using Production environment by default for initial setup." -ForegroundColor White
    Write-Host "The tool will automatically try other environments if needed." -ForegroundColor Blue
    Write-Host "IAM URL: $script:IAMURL"
}

# Function to parse Service instance URL and extract API URL and instance ID
function ConvertFrom-ServiceInstanceUrl {
    param (
        [string]$ServiceUrl
    )
    
    # Check if the URL matches the expected pattern
    if ($ServiceUrl -match "^(https?://[^/]+)/instances/([a-zA-Z0-9-]+)$") {
        $script:API_URL = $matches[1]
        $script:WXO_INSTANCE_ID = $matches[2]
        
        # Check if this is an IBM Cloud instance
        if ($script:API_URL -like "*.cloud.ibm.com*") {
            $script:IS_IBM_CLOUD = $true
            Write-Host "Detected IBM Cloud instance. Will use API key directly for authentication." -ForegroundColor Blue
        }
        else {
            $script:IS_IBM_CLOUD = $false
        }
        
        return $true
    }
    else {
        return $false
    }
}

# Function to get API URL and instance ID
function Get-ServiceDetails {
    Write-Host ""
    Write-Host "Enter your Service instance URL:" -ForegroundColor White
    Write-Host "You can find this URL in the Settings page under API Details tab." -ForegroundColor Blue
    Write-Host "Example: https://api.us-south.watson-orchestrate.ibm.com/instances/12345-67890-abcde" -ForegroundColor Blue
    Write-Host "Common API regions include:" -ForegroundColor Blue
    Write-Host "- api.us-south.watson-orchestrate.ibm.com (US South/Dallas)" -ForegroundColor Blue
    Write-Host "- api.eu-de.watson-orchestrate.ibm.com (EU DE/Frankfurt)" -ForegroundColor Blue
    Write-Host "- api.dl.watson-orchestrate.ibm.com (Dallas)" -ForegroundColor Blue
    
    while ($true) {
        $serviceUrl = Read-Host -Prompt "Enter your Service instance URL"
        
        if ([string]::IsNullOrEmpty($serviceUrl)) {
            Write-Host "This field cannot be empty. Please try again." -ForegroundColor Yellow
            continue
        }
        
        if (ConvertFrom-ServiceInstanceUrl -ServiceUrl $serviceUrl) {
            Write-Host "Successfully parsed Service instance URL." -ForegroundColor Green
            Write-Host "API URL: $script:API_URL" -ForegroundColor White
            Write-Host "Instance ID: $script:WXO_INSTANCE_ID" -ForegroundColor White
            return
        }
        else {
            Write-Host "Invalid Service instance URL format. It should be like:" -ForegroundColor Yellow
            Write-Host "https://api.us-south.watson-orchestrate.ibm.com/instances/12345-67890-abcde" -ForegroundColor Yellow
            
            $separateInput = Read-Host -Prompt "Would you like to enter the API URL and Instance ID separately? (yes/no)"
            if ($separateInput -eq "yes") {
                Get-ApiUrlSeparately
                Get-InstanceIdSeparately
                return
            }
        }
    }
}

# Function to get API URL separately
function Get-ApiUrlSeparately {
    Write-Host ""
    Write-Host "Enter your API URL:" -ForegroundColor White
    Write-Host "It's the base part of your Service instance URL (before /instances/)." -ForegroundColor Blue
    Write-Host "Example: https://api.us-south.watson-orchestrate.ibm.com" -ForegroundColor Blue
    
    $script:API_URL = Get-UserInput -Prompt "Enter your API URL" -VarName "API_URL" -IsSecret $false
    Write-Host "API URL: $script:API_URL" -ForegroundColor Green
}

# Function to get instance ID separately
function Get-InstanceIdSeparately {
    Write-Host ""
    Write-Host "Enter your Orchestrate instance ID:" -ForegroundColor White
    Write-Host "This is the UUID after /instances/ in your Service instance URL." -ForegroundColor Blue
    Write-Host "Example: 12345-67890-abcde" -ForegroundColor Blue
    
    $script:WXO_INSTANCE_ID = Get-UserInput -Prompt "Enter your Orchestrate instance ID" -VarName "WXO_INSTANCE_ID" -IsSecret $false
}

# Function to obtain IAM token
function Get-IamToken {
    # Check output directory before saving token
    if (-not (Test-OutputDirectory)) {
        exit 1
    }
    
    Write-Host ""
    Write-Host "Step 1: Obtaining IAM Token" -ForegroundColor White
    
    $script:WXO_API_KEY = Get-UserInput -Prompt "Enter your IBM watsonx Orchestrate API Key" -VarName "WXO_API_KEY" -IsSecret $true
    
    # Keep track of which environments have been tried
    $triedProd = $false
    $triedDev = $false
    $triedTest = $false
    $tokenObtained = $false
    
    # Try with the default environment first
    Write-Host ""
    Write-Host "Trying with $script:ENVIRONMENT environment..." -ForegroundColor White
    Write-Host "IAM URL: $script:IAMURL"
    
    try {
        $body = @{
            apikey = $script:WXO_API_KEY
        } | ConvertTo-Json
        
        $tokenResponse = Invoke-RestMethod -Uri "$script:IAMURL/siusermgr/api/1.0/apikeys/token" `
            -Method Post `
            -Headers @{ "accept" = "application/json"; "content-type" = "application/json" } `
            -Body $body `
            -ErrorAction Stop
            
        if ($tokenResponse.token) {
            $script:WXO_TOKEN = $tokenResponse.token
            $tokenObtained = $true
            Write-Host "Successfully obtained IAM token with $script:ENVIRONMENT environment." -ForegroundColor Green
            Write-Host "Successfully obtained IAM token." -ForegroundColor Green
        }
    }
    catch {
        # Error handling below
    }
    
    # Mark the current environment as tried
    if ($script:ENVIRONMENT -eq "PROD") { $triedProd = $true }
    elseif ($script:ENVIRONMENT -eq "DEV") { $triedDev = $true }
    elseif ($script:ENVIRONMENT -eq "TEST") { $triedTest = $true }
    
    # If token was not obtained, try other environments
    while (-not $tokenObtained) {
        Write-Host "Failed to obtain token with $script:ENVIRONMENT environment." -ForegroundColor Yellow
        Write-Host ""
        Write-Host "This could be due to:" -ForegroundColor Yellow
        Write-Host "1. Incorrect API key"
        Write-Host "2. Using an API key from a different watsonx Orchestrate environment"
        
        # Check if all environments have been tried
        if ($triedProd -and $triedDev -and $triedTest) {
            Write-Host ""
            Write-Host "Failed to obtain IAM token after trying all environments (PROD, DEV, TEST)." -ForegroundColor Red
            Write-Host "This likely indicates an incorrect API key. Please verify your API key and try again." -ForegroundColor Red
            exit 1
        }
        
        Write-Host ""
        Write-Host "Would you like to try a different environment?" -ForegroundColor White
        Write-Host "1) Development $(if ($triedDev) { "[Already tried]" })"
        Write-Host "2) Test $(if ($triedTest) { "[Already tried]" })"
        Write-Host "3) Production $(if ($triedProd) { "[Already tried]" })"
        Write-Host "4) Exit"
        
        $selection = Read-Host -Prompt "Enter your choice (1-4)"
        
        switch ($selection) {
            "1" {
                if ($triedDev) { Write-Host "You've already tried the Development environment." -ForegroundColor Yellow; continue }
                $script:ENVIRONMENT = "DEV"
                $script:IAMURL = "https://iam.platform.dev.saas.ibm.com"
                $triedDev = $true
            }
            "2" {
                if ($triedTest) { Write-Host "You've already tried the Test environment." -ForegroundColor Yellow; continue }
                $script:ENVIRONMENT = "TEST"
                $script:IAMURL = "https://iam.platform.test.saas.ibm.com"
                $triedTest = $true
            }
            "3" {
                if ($triedProd) { Write-Host "You've already tried the Production environment." -ForegroundColor Yellow; continue }
                $script:ENVIRONMENT = "PROD"
                $script:IAMURL = "https://iam.platform.saas.ibm.com"
                $triedProd = $true
            }
            "4" {
                Write-Host "Exiting the configuration tool." -ForegroundColor Blue
                exit 0
            }
            default {
                Write-Host "Invalid selection. Please enter 1, 2, 3, or 4." -ForegroundColor Yellow
                continue
            }
        }
        
        Write-Host ""
        Write-Host "Trying with $script:ENVIRONMENT environment..." -ForegroundColor White
        Write-Host "IAM URL: $script:IAMURL"
        
        try {
            $body = @{
                apikey = $script:WXO_API_KEY
            } | ConvertTo-Json
            
            $tokenResponse = Invoke-RestMethod -Uri "$script:IAMURL/siusermgr/api/1.0/apikeys/token" `
                -Method Post `
                -Headers @{ "accept" = "application/json"; "content-type" = "application/json" } `
                -Body $body `
                -ErrorAction Stop
                
            if ($tokenResponse.token) {
                $script:WXO_TOKEN = $tokenResponse.token
                $tokenObtained = $true
                Write-Host "Successfully obtained IAM token with $script:ENVIRONMENT environment." -ForegroundColor Green
                Write-Host "Successfully obtained IAM token." -ForegroundColor Green
            }
        }
        catch {
            # Loop continues
        }
    }
}

# Function to get current configuration
function Get-CurrentConfig {
    # Check output directory before saving configuration
    if (-not (Test-OutputDirectory)) {
        exit 1
    }
    
    Write-Host ""
    Write-Host "Getting current embed security configuration..." -ForegroundColor White
    
    try {
        $headers = @{
            "accept" = "application/json"
        }
        
        # Use different authentication header based on instance type
        if ($script:IS_IBM_CLOUD) {
            $headers["IAM-API_KEY"] = $script:WXO_API_KEY
        }
        else {
            $headers["Authorization"] = "Bearer $script:WXO_TOKEN"
        }
        
        $configResponse = Invoke-RestMethod -Uri "$script:API_URL/instances/$script:WXO_INSTANCE_ID/v1/embed/secure/config" `
            -Method Get `
            -Headers $headers `
            -ErrorAction Stop
            
        $script:IS_SECURITY_ENABLED = $configResponse.is_security_enabled
        
        if ($script:IS_SECURITY_ENABLED) {
            Write-Host "Current security status: ENABLED" -ForegroundColor White
            
            $hasPublicKey = -not [string]::IsNullOrEmpty($configResponse.public_key)
            $hasClientPublicKey = -not [string]::IsNullOrEmpty($configResponse.client_public_key)
            
            if (-not $hasPublicKey -or -not $hasClientPublicKey) {
                Write-Host "WARNING: Security is enabled but configuration is incomplete. Embed Chat will not function properly." -ForegroundColor Yellow
            }
            else {
                Write-Host "Security is properly configured with both IBM and client public keys." -ForegroundColor Green
            }
        }
        else {
            Write-Host "Current security status: DISABLED" -ForegroundColor White
        }
    }
    catch {
        Write-Host "Could not retrieve current configuration." -ForegroundColor Yellow
        Write-Host "This may be normal if security has not been configured yet." -ForegroundColor Yellow
        Write-Host "In this state, security is enabled by default but Embed Chat will not function until properly configured." -ForegroundColor Yellow
        $script:IS_SECURITY_ENABLED = "unknown"
    }
}

# Function to generate IBM public key
function New-IbmKey {
    # Check output directory before saving keys
    if (-not (Test-OutputDirectory)) {
        exit 1
    }
    
    Write-Host ""
    Write-Host "Step 2: Generating IBM Public Key" -ForegroundColor White
    Write-Host "Requesting new IBM key pair..."
    
    try {
        $headers = @{
            "accept" = "application/json"
        }
        
        # Use different authentication header based on instance type
        if ($script:IS_IBM_CLOUD) {
            $headers["IAM-API_KEY"] = $script:WXO_API_KEY
        }
        else {
            $headers["Authorization"] = "Bearer $script:WXO_TOKEN"
        }
        
        $ibmKeyResponse = Invoke-RestMethod -Uri "$script:API_URL/instances/$script:WXO_INSTANCE_ID/v1/embed/secure/generate-key-pair" `
            -Method Post `
            -Headers $headers `
            -ErrorAction Stop
            
        # Extract the public key from the response
        Write-Host "Extracting and saving IBM public key..." -ForegroundColor Blue
        
        $script:IBM_PUBLIC_KEY = $ibmKeyResponse.public_key
        
        if (-not [string]::IsNullOrEmpty($script:IBM_PUBLIC_KEY)) {
            # Save the key to files
            Set-Content -Path "$OUTPUT_DIR/ibm_public_key.pem" -Value $script:IBM_PUBLIC_KEY
            
            # Format for text file (single line with \n)
            $textFormat = $script:IBM_PUBLIC_KEY -replace "`n", "\\n"
            Set-Content -Path "$OUTPUT_DIR/ibm_public_key.txt" -Value $textFormat
            
            Write-Host "Successfully generated and saved IBM public key." -ForegroundColor Green
        }
        else {
            Write-Host "Failed to extract public key from response." -ForegroundColor Red
            exit 1
        }
    }
    catch {
        Write-Host "Failed to generate IBM key pair: $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
}

# Function to generate client key pair using PowerShell's cryptography capabilities
function New-ClientKeys {
    Write-Host ""
    Write-Host "Step 3: Generating Client Key Pair" -ForegroundColor White
    Write-Host "Generating RSA 4096-bit key pair..."
    
    # Check if output directory exists and is writable
    if (-not (Test-Path -Path $OUTPUT_DIR -PathType Container) -or -not (Test-Path -Path $OUTPUT_DIR -PathType Container -IsValid)) {
        Write-Host "ERROR: Output directory '$OUTPUT_DIR' does not exist or is not writable." -ForegroundColor Red
        Write-Host "Please check if the directory exists and has proper permissions." -ForegroundColor Yellow
        exit 1
    }
    
    try {
        # Load required .NET classes
        Add-Type -AssemblyName System.Security
        
        # Create RSA provider with 4096 bit key
        $rsa = New-Object System.Security.Cryptography.RSACryptoServiceProvider(4096)
        
        # Get the private key in PKCS#1 format
        $privateKeyBytes = $rsa.ExportRSAPrivateKey()
        $privateKeyPem = @()
        $privateKeyPem += "-----BEGIN RSA PRIVATE KEY-----"
        $privateKeyPem += [Convert]::ToBase64String($privateKeyBytes, [System.Base64FormattingOptions]::InsertLineBreaks)
        $privateKeyPem += "-----END RSA PRIVATE KEY-----"
        $privateKeyText = $privateKeyPem -join "`n"
        
        # Get the public key in X.509 format
        $publicKeyBytes = $rsa.ExportRSAPublicKey()
        $publicKeyPem = @()
        $publicKeyPem += "-----BEGIN PUBLIC KEY-----"
        $publicKeyPem += [Convert]::ToBase64String($publicKeyBytes, [System.Base64FormattingOptions]::InsertLineBreaks)
        $publicKeyPem += "-----END PUBLIC KEY-----"
        $publicKeyText = $publicKeyPem -join "`n"
        
        # Save the keys to files
        Set-Content -Path "$OUTPUT_DIR/client_private_key.pem" -Value $privateKeyText
        Set-Content -Path "$OUTPUT_DIR/client_public_key.pem" -Value $publicKeyText
        
        # Format the public key for API consumption
        Write-Host "Converting client public key to format needed for API..." -ForegroundColor Blue
        $script:CLIENT_PUBLIC_KEY = $publicKeyText -replace "`n", "\\n"
        
        # Save the processed key
        Set-Content -Path "$OUTPUT_DIR/client_public_key.txt" -Value $script:CLIENT_PUBLIC_KEY
        
        # Debug information
        $keyLength = $script:CLIENT_PUBLIC_KEY.Length
        $txtSize = (Get-Item -Path "$OUTPUT_DIR/client_public_key.txt").Length
        
        Write-Host "Debug: Client public key length is $keyLength bytes" -ForegroundColor Blue
        Write-Host "Debug: client_public_key.txt size is $txtSize bytes" -ForegroundColor Blue
        
        if ($txtSize -lt 100) {
            Write-Host "Warning: Client public key text file seems too small ($txtSize bytes)." -ForegroundColor Yellow
            Write-Host "This might cause issues when configuring security." -ForegroundColor Yellow
        }
        else {
            Write-Host "Successfully generated client key pair." -ForegroundColor Green
            Write-Host "Client keys saved to $OUTPUT_DIR/client_private_key.pem and $OUTPUT_DIR/client_public_key.pem" -ForegroundColor White
            Write-Host "Client public key (text format) saved to $OUTPUT_DIR/client_public_key.txt" -ForegroundColor White
        }
    }
    catch {
        Write-Host "ERROR: Failed to generate client key pair: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "This might be due to missing .NET Framework features or insufficient permissions." -ForegroundColor Yellow
        exit 1
    }
}

# Function to enable security
function Enable-Security {
    Write-Host ""
    Write-Host "Step 4: Enabling Security with Custom Keys" -ForegroundColor White
    Write-Host "Configuring security with IBM and client public keys..."
    
    # Create the JSON payload
    $payload = @{
        "public_key"          = $script:IBM_PUBLIC_KEY
        "client_public_key"   = $script:CLIENT_PUBLIC_KEY
        "is_security_enabled" = $true
    } | ConvertTo-Json
    
    try {
        $headers = @{
            "Content-Type" = "application/json"
        }
        
        # Use different authentication header based on instance type
        if ($script:IS_IBM_CLOUD) {
            $headers["IAM-API_KEY"] = $script:WXO_API_KEY
        }
        else {
            $headers["Authorization"] = "Bearer $script:WXO_TOKEN"
        }
        
        Invoke-RestMethod -Uri "$script:API_URL/instances/$script:WXO_INSTANCE_ID/v1/embed/secure/config" `
            -Method Post `
            -Headers $headers `
            -Body $payload `
            -ErrorAction Stop | Out-Null
            
        Write-Host "Successfully enabled security with custom keys." -ForegroundColor Green
        Write-Host "Your Embed Chat will now function properly with security enabled." -ForegroundColor Green
    }
    catch {
        Write-Host "Failed to enable security:" -ForegroundColor Red
        
        # Check for specific error codes
        if ($_.Exception.Response.StatusCode -eq 422) {
            Write-Host "Received 422 error - This typically indicates an issue with the key format:" -ForegroundColor Yellow
            Write-Host "1. The public keys may not be properly formatted"
            Write-Host "2. The keys may be too short or corrupted"
            Write-Host "3. There might be special characters causing issues"
            
            # Show key diagnostics
            Write-Host ""
            Write-Host "Key diagnostics:" -ForegroundColor Blue
            Write-Host "IBM public key length: $($script:IBM_PUBLIC_KEY.Length) bytes"
            Write-Host "Client public key length: $($script:CLIENT_PUBLIC_KEY.Length) bytes"
            
            if ($Verbose) {
                Write-Host ""
                Write-Host "Try running with -v option and check the generated files in $OUTPUT_DIR" -ForegroundColor Yellow
                Write-Host "Specifically, examine ibm_public_key.pem and client_public_key.pem" -ForegroundColor Yellow
            }
            else {
                Write-Host ""
                Write-Host "Run with -v option for more debugging information." -ForegroundColor Yellow
            }
        }
        else {
            Write-Host $_.Exception.Message
        }
        exit 1
    }
}

# Function to disable security
function Disable-Security {
    Write-Host ""
    Write-Host "Disabling Security and Allowing Anonymous Access" -ForegroundColor White
    Write-Host "WARNING: This will allow anonymous access to your embedded chat." -ForegroundColor Red
    Write-Host "Only do this if your use case specifically requires anonymous access" -ForegroundColor Yellow
    Write-Host "and the data and team tools in your instance are appropriate for anonymous access." -ForegroundColor Yellow
    
    $confirmation = Read-Host -Prompt "Are you sure you want to disable security and allow anonymous access? (yes/no)"
    
    if ($confirmation -eq "yes") {
        # Continue with disabling security
    }
    elseif ($confirmation -eq "no") {
        Write-Host "Operation cancelled."
        return $false
    }
    else {
        Write-Host "Unexpected input received. Operation cancelled." -ForegroundColor Yellow
        return $false
    }
    
    Write-Host "Disabling security and clearing key pairs..."
    
    # Create the JSON payload
    $payload = @{
        "public_key"          = ""
        "client_public_key"   = ""
        "is_security_enabled" = $false
    } | ConvertTo-Json
    
    try {
        $headers = @{
            "Content-Type" = "application/json"
        }
        
        # Use different authentication header based on instance type
        if ($script:IS_IBM_CLOUD) {
            $headers["IAM-API_KEY"] = $script:WXO_API_KEY
        }
        else {
            $headers["Authorization"] = "Bearer $script:WXO_TOKEN"
        }
        
        Invoke-RestMethod -Uri "$script:API_URL/instances/$script:WXO_INSTANCE_ID/v1/embed/secure/config" `
            -Method Post `
            -Headers $headers `
            -Body $payload `
            -ErrorAction Stop | Out-Null
            
        Write-Host "Security has been disabled and key pairs cleared. Your embedded chat now allows anonymous access." -ForegroundColor Yellow
        return $true
    }
    catch {
        Write-Host "Failed to disable security:" -ForegroundColor Red
        Write-Host $_.Exception.Message
        if (-not $Verbose) {
            Write-Host "Run with -v option for more debugging information." -ForegroundColor Yellow
        }
        return $false
    }
}

# Function to verify configuration
function Test-Configuration {
    # Check output directory before saving configuration
    if (-not (Test-OutputDirectory)) {
        exit 1
    }
    
    Write-Host ""
    Write-Host "Verifying Configuration" -ForegroundColor White
    Write-Host "Checking current security settings..."
    
    try {
        $headers = @{
            "accept" = "application/json"
        }
        
        # Use different authentication header based on instance type
        if ($script:IS_IBM_CLOUD) {
            $headers["IAM-API_KEY"] = $script:WXO_API_KEY
        }
        else {
            $headers["Authorization"] = "Bearer $script:WXO_TOKEN"
        }
        
        $verifyResponse = Invoke-RestMethod -Uri "$script:API_URL/instances/$script:WXO_INSTANCE_ID/v1/embed/secure/config" `
            -Method Get `
            -Headers $headers `
            -ErrorAction Stop
            
        $finalStatus = $verifyResponse.is_security_enabled
        
        if ($finalStatus) {
            Write-Host "Security is now: ENABLED" -ForegroundColor Green
        }
        else {
            Write-Host "Security is now: DISABLED (Anonymous Access)" -ForegroundColor Yellow
        }
        
        if ($finalStatus) {
            $hasPublicKey = -not [string]::IsNullOrEmpty($verifyResponse.public_key)
            $hasClientPublicKey = -not [string]::IsNullOrEmpty($verifyResponse.client_public_key)
            
            if (-not $hasPublicKey -or -not $hasClientPublicKey) {
                Write-Host "WARNING: Security is enabled but configuration is incomplete. Embed Chat will not function properly." -ForegroundColor Yellow
            }
            else {
                Write-Host "Security is properly configured with both IBM and client public keys." -ForegroundColor Green
                Write-Host "Your Embed Chat will function properly with security enabled." -ForegroundColor Green
            }
        }
        else {
            Write-Host "Your Embed Chat is configured for anonymous access." -ForegroundColor Yellow
        }
        
        Write-Host "Configuration completed successfully."
        return $true
    }
    catch {
        Write-Host "Failed to verify configuration:" -ForegroundColor Red
        Write-Host $_.Exception.Message
        if (-not $Verbose) {
            Write-Host "Run with -v option for more debugging information." -ForegroundColor Yellow
        }
        return $false
    }
}

# Function to display the main menu and handle user actions
function Show-MainMenu {
    $action = ""
    while ($true) {
        # Always display the menu options at the start of each loop iteration
        Write-Host ""
        Write-Host "Select an action:" -ForegroundColor White
        Write-Host "1) Configure security with custom keys (Recommended)"
        Write-Host "2) Disable security and allow anonymous access (Only for specific use cases)"
        Write-Host "3) View current configuration only"
        Write-Host "4) Exit"
        
        $action = Read-Host -Prompt "Enter your choice (1-4)"
        
        switch ($action) {
            "1" {
                New-IbmKey
                New-ClientKeys
                Enable-Security
                Test-Configuration
                Show-ConfigurationSummary -Action "1"
                return
            }
            "2" {
                $result = Disable-Security
                if (-not $result) {
                    # If disable_security returned false (cancelled), continue the loop
                    # The menu will be displayed again at the start of the next iteration
                    continue
                }
                Test-Configuration
                Show-ConfigurationSummary -Action "2"
                return
            }
            "3" {
                Write-Host "Viewing current configuration only. No changes made." -ForegroundColor Blue
                Test-Configuration
                Show-ConfigurationSummary -Action "3"
                return
            }
            "4" {
                Write-Host "Exiting the configuration tool." -ForegroundColor Blue
                exit 0
            }
            default {
                Write-Host "Invalid selection. Please enter 1, 2, 3, or 4." -ForegroundColor Yellow
            }
        }
    }
}

# Function to show configuration summary
function Show-ConfigurationSummary {
    param (
        [string]$Action
    )
    
    # Check if output directory exists before referencing files
    if (-not (Test-OutputDirectory)) {
        Write-Host "Warning: Output directory not found. Configuration files may not be accessible." -ForegroundColor Yellow
    }
    
    Write-Host ""
    Write-Host "Configuration Summary" -ForegroundColor White
    Write-Host "Key files are saved in the $OUTPUT_DIR directory:"
    
    if ($Action -eq "1") {
        Write-Host "- IBM public key: ibm_public_key.pem and ibm_public_key.txt" -ForegroundColor White
        Write-Host "- Client private key: client_private_key.pem" -ForegroundColor White
        Write-Host "- Client public key: client_public_key.pem and client_public_key.txt" -ForegroundColor White
    }
    
    Write-Host ""
    Write-Host "Configuration process completed." -ForegroundColor Green
    
    # Ask if user wants to return to action menu or exit
    Write-Host ""
    Write-Host "Would you like to:" -ForegroundColor White
    Write-Host "1) Return to action menu"
    Write-Host "2) Exit"
    
    while ($true) {
        $nextAction = Read-Host -Prompt "Enter your choice (1-2)"
        
        switch ($nextAction) {
            "1" { return }
            "2" {
                Write-Host "Exiting the configuration tool." -ForegroundColor Blue
                exit 0
            }
            default {
                Write-Host "Invalid selection. Please enter 1 or 2." -ForegroundColor Yellow
            }
        }
    }
}

# Main execution flow for PowerShell
Write-Host "Do you need help finding your Service instance URL? (y/n): " -ForegroundColor White -NoNewline
$needHelp = Read-Host
if ($needHelp -eq "y" -or $needHelp -eq "Y") {
    Show-InstanceIdHelp
}

Select-Environment
Get-ServiceDetails

# For IBM Cloud instances, we don't need to obtain an IAM token
if ($script:IS_IBM_CLOUD) {
    Write-Host ""
    Write-Host "Step 1: Getting API Key" -ForegroundColor White
    $script:WXO_API_KEY = Get-UserInput -Prompt "Enter your IBM watsonx Orchestrate API Key" -VarName "WXO_API_KEY" -IsSecret $true
    Write-Host "API Key received. Will use it directly for authentication." -ForegroundColor Green
}
else {
    Get-IamToken
}

Get-CurrentConfig

# Main menu loop
while ($true) {
    Show-MainMenu
}
