param(
    [string]$PythonPath = "",
    [string]$ChromePath = "C:\Program Files\Google\Chrome\Application\chrome.exe"
)

$ErrorActionPreference = "Stop"
$resultPath = Join-Path $PSScriptRoot "logs\network_repair_result.txt"
New-Item -ItemType Directory -Path (Split-Path $resultPath) -Force | Out-Null
if (-not $PythonPath) {
    $PythonPath = (Get-Command python.exe -ErrorAction Stop).Source
}
$resolvedPython = (Resolve-Path -LiteralPath $PythonPath -ErrorAction Stop).Path
$PythonPath = $resolvedPython
$PythonRuntimePath = (& $PythonPath -c "import sys; print(sys.executable)").Trim()

function Set-FundMonitorRule {
    param([string]$Name, [string]$Program)
    if (-not (Test-Path -LiteralPath $Program)) {
        throw "Program not found: $Program"
    }
    Get-NetFirewallRule -DisplayName $Name -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule -ErrorAction SilentlyContinue
    New-NetFirewallRule `
        -DisplayName $Name `
        -Description "Allow Fund Monitor HTTPS data acquisition" `
        -Direction Outbound `
        -Program $Program `
        -Action Allow `
        -Protocol TCP `
        -RemotePort 443 `
        -Profile Any | Out-Null
}

try {
    Set-FundMonitorRule -Name "FundMonitor-Python-HTTPS" -Program $PythonPath
    if ($PythonRuntimePath -and $PythonRuntimePath -ne $PythonPath) {
        Set-FundMonitorRule -Name "FundMonitor-PythonRuntime-HTTPS" -Program $PythonRuntimePath
    }
    Set-FundMonitorRule -Name "FundMonitor-Chrome-HTTPS" -Program $ChromePath

    $testOutput = & $PythonPath -c "import socket; s=socket.create_connection(('fund.eastmoney.com',443),8); print('TCP_443_OK'); s.close(); import urllib.request; r=urllib.request.urlopen('https://fund.eastmoney.com/016702.html',timeout=15); print('HTTPS_OK',r.status,len(r.read(2000)))" 2>&1
    @(
        "status=completed"
        "time=$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
        "python=$PythonPath"
        "python_runtime=$PythonRuntimePath"
        "chrome=$ChromePath"
        "test=$testOutput"
    ) | Set-Content -LiteralPath $resultPath -Encoding UTF8
} catch {
    @(
        "status=failed"
        "time=$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
        "error=$($_.Exception.Message)"
    ) | Set-Content -LiteralPath $resultPath -Encoding UTF8
    throw
}

Write-Host "Repair finished. Result: $resultPath"
Start-Sleep -Seconds 3
