param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,
    [string]$HostAddress = "127.0.0.1"
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDirectory = Join-Path $ProjectRoot ".venv"
$PythonPath = Join-Path $VenvDirectory "Scripts\python.exe"
$RequirementsPath = Join-Path $ProjectRoot "requirements.txt"

Set-Location -LiteralPath $ProjectRoot

function Invoke-ExternalCommand {
    param(
        [scriptblock]$Command,
        [string]$FailureMessage
    )

    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw $FailureMessage
    }
}

function Get-BootstrapPython {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $PythonCommand) {
        return @($PythonCommand.Source)
    }

    $PythonLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $PythonLauncher) {
        return @($PythonLauncher.Source, "-3")
    }

    throw "Python 3 was not found. Install Python 3.10+ and make it available as python or py."
}

function Test-PortAvailable {
    param(
        [string]$Address,
        [int]$CandidatePort
    )

    try {
        $IpAddress = [System.Net.IPAddress]::Parse($Address)
    }
    catch {
        throw "HostAddress must be an IP address, for example 127.0.0.1."
    }

    $Listener = [System.Net.Sockets.TcpListener]::new($IpAddress, $CandidatePort)
    try {
        $Listener.Start()
        return $true
    }
    catch [System.Net.Sockets.SocketException] {
        return $false
    }
    finally {
        $Listener.Stop()
    }
}

if (-not (Test-Path -LiteralPath $PythonPath)) {
    Write-Host "Creating virtual environment..."
    $BootstrapPython = @(Get-BootstrapPython)
    $BootstrapArguments = @()
    if ($BootstrapPython.Count -gt 1) {
        $BootstrapArguments = $BootstrapPython[1..($BootstrapPython.Count - 1)]
    }
    Invoke-ExternalCommand -Command {
        & $BootstrapPython[0] @BootstrapArguments -m venv $VenvDirectory
    } -FailureMessage "Could not create the virtual environment."
}

$DependenciesReady = $false
& $PythonPath -c "import importlib.util, sys; modules = ('fastapi', 'jinja2', 'openpyxl', 'pydantic', 'pytest', 'uvicorn'); sys.exit(0 if all(importlib.util.find_spec(module) for module in modules) else 1)"
if ($LASTEXITCODE -eq 0) {
    $DependenciesReady = $true
}

if (-not $DependenciesReady) {
    Write-Host "Installing dependencies..."
    Invoke-ExternalCommand -Command {
        & $PythonPath -m pip install --disable-pip-version-check --timeout 30 --retries 2 -r $RequirementsPath
    } -FailureMessage "Could not install project dependencies."
}

$SelectedPort = $Port
while (-not (Test-PortAvailable -Address $HostAddress -CandidatePort $SelectedPort)) {
    if ($SelectedPort -ge 65535) {
        throw "No free port was found."
    }
    $SelectedPort++
}

$ApplicationUrl = "http://${HostAddress}:$SelectedPort"
Write-Host "Starting Moscow Rent Search"
Write-Host "Open: $ApplicationUrl"

& $PythonPath -m uvicorn app.main:app --host $HostAddress --port $SelectedPort
exit $LASTEXITCODE
