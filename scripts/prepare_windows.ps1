[CmdletBinding()]
param(
    [string]$Python = '',
    [string]$InstallDir = '',
    [string]$Workspace = ''
)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') {
    throw 'Run this preparation script on Windows with PowerShell 5.1 or later.'
}
$sourceRoot = Split-Path -Parent $PSScriptRoot
if (-not $InstallDir) { $InstallDir = Join-Path $env:LOCALAPPDATA 'ACH\app' }
if (-not $Workspace) { $Workspace = Join-Path $env:LOCALAPPDATA 'ACH\workspace' }
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
$Workspace = [IO.Path]::GetFullPath($Workspace)
if (Test-Path -LiteralPath $Workspace) {
    if (-not (Test-Path -LiteralPath $Workspace -PathType Container)) {
        throw 'The staging workspace must be a directory.'
    }
    if (Get-ChildItem -LiteralPath $Workspace -Force | Select-Object -First 1) {
        throw 'Choose an empty staging workspace. Existing board data and client settings are not replaced.'
    }
}

# Resolve the interpreter once so installation and generated configs use the same Python.
$pythonPrefix = @()
if (-not $Python) {
    $candidate = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($candidate) {
        $Python = $candidate.Source
        $pythonPrefix = @('-3')
    } else {
        $candidate = Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $candidate) { throw 'Install Python 3.10 or later, or pass -Python with its executable path.' }
        $Python = $candidate.Source
    }
}
$resolvedPython = & $Python @pythonPrefix -c 'import sys; sys.exit(1) if sys.version_info < (3,10) else print(sys.executable)'
if ($LASTEXITCODE -ne 0) { throw 'Python verification failed. No installation was attempted.' }
$Python = ($resolvedPython | Select-Object -Last 1).Trim()
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Python did not return a usable executable path.' }

function Invoke-CheckedPython {
    param([string[]]$Arguments)
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Python command failed (exit $LASTEXITCODE). Preparation stopped." }
}

$environmentNames = @('COLLAB_WORKSPACE', 'COLLAB_ALLOW_CONSULTS', 'COLLAB_DISABLE_EXTERNAL_DELIVERY',
                      'COLLAB_ALERTS_AUTOSTART', 'PYTHONUTF8', 'PYTHONDONTWRITEBYTECODE')
$previousEnvironment = @{}
foreach ($name in $environmentNames) {
    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$validationWorkspace = Join-Path ([IO.Path]::GetTempPath()) ('ach-validation-' + [Guid]::NewGuid().ToString('N'))
try {
    New-Item -ItemType Directory -Path $validationWorkspace | Out-Null
    $env:COLLAB_WORKSPACE = $validationWorkspace
    $env:COLLAB_ALLOW_CONSULTS = '0'
    $env:COLLAB_DISABLE_EXTERNAL_DELIVERY = '1'
    $env:COLLAB_ALERTS_AUTOSTART = '0'
    $env:PYTHONUTF8 = '1'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    Push-Location -LiteralPath $sourceRoot
    try {
        Write-Host 'Testing ACH in disposable workspaces. No model calls or external alerts are enabled.'
        Invoke-CheckedPython -Arguments @('-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py', '-v')
        Invoke-CheckedPython -Arguments @('tests/smoke_mcp.py', 'mcp_server.py', '--cases', 'tests/v2_cases.json')
        Invoke-CheckedPython -Arguments @('install.py', '--install-dir', $InstallDir, '--workspace', $Workspace, '--no-shortcut', '--dry-run')
        Invoke-CheckedPython -Arguments @('install.py', '--install-dir', $InstallDir, '--workspace', $Workspace, '--no-shortcut')
        Invoke-CheckedPython -Arguments @((Join-Path $InstallDir 'setup_clients.py'), '--workspace', $Workspace)
    } finally {
        Pop-Location
    }
    Write-Host ''
    Write-Host "ACH staged: $InstallDir"
    Write-Host "Private workspace: $Workspace"
    Write-Host "Client configuration: $(Join-Path $Workspace 'client-configs')"
    Write-Host "Launch the staged board: $(Join-Path $InstallDir 'launch.cmd')"
    Write-Host 'Next: merge the generated server entry into each client, restart its connection, and test a post/read/reply round trip.'
    Write-Host 'Desktop-client connections are not verified by these automated tests. The existing workspace has not been migrated or replaced.'
} finally {
    foreach ($name in $environmentNames) {
        [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'Process')
    }
    if (Test-Path -LiteralPath $validationWorkspace) {
        Remove-Item -LiteralPath $validationWorkspace -Recurse -Force
    }
}
