param(
    [ValidateSet('free','paid')][string]$Edition = 'free',
    [string]$PythonExe = '',
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$dataDir = Join-Path $env:APPDATA 'GameCopilot1'
$runtimePath = Join-Path $dataDir 'runtime.txt'
$candidates = @()
if ($PythonExe) {
    $candidates += $PythonExe
} else {
    if (Test-Path $runtimePath) { $candidates += (Get-Content $runtimePath -Raw).Trim() }
    $candidates += (Join-Path $projectRoot '.venv\Scripts\python.exe')
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd -and $pythonCmd.Source -notlike '*WindowsApps*') { $candidates += $pythonCmd.Source }
    $pyCmd = Get-Command py -ErrorAction SilentlyContinue
    if ($pyCmd) {
        $foundPython = & $pyCmd.Source -3 -c 'import sys; print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0) { $candidates += $foundPython }
    }
}
$chosen = $null
foreach ($candidate in ($candidates | Select-Object -Unique)) {
    if (-not $candidate -or -not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
    Write-Host "Checking Python: $candidate"
    & $candidate -c "import sys, tkinter, ssl; assert sys.version_info >= (3,10), 'Python 3.10+ required'; r=tkinter.Tk(); r.withdraw(); r.destroy(); print('Python / Tk / TLS: OK')"
    if ($LASTEXITCODE -eq 0) { $chosen = $candidate; break }
}
if (-not $chosen) {
    Write-Host 'No working Python 3.10+ with Tk was found.' -ForegroundColor Yellow
    Write-Host 'Use the Python from your previous working folder. Do NOT copy its .venv.'
    Write-Host 'Example: powershell -ExecutionPolicy Bypass -File .\run-free.ps1 -PythonExe "C:\path\old-folder\.venv\Scripts\python.exe"'
    Write-Host 'Alternatively install Python from https://www.python.org/downloads/windows/ with Tcl/Tk enabled.'
    exit 1
}
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
Set-Content -LiteralPath $runtimePath -Value $chosen -Encoding UTF8
if ($CheckOnly) {
    Write-Host 'Ready. No pip packages or Qt required. Run run-free.ps1 or run-paid.ps1.'
    exit 0
}
Push-Location $projectRoot
try {
    & $chosen -m copilot.app --edition $Edition
    $appExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $appExitCode
