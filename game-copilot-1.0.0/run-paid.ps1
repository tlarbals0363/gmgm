param([string]$PythonExe = '')
& "$PSScriptRoot\scripts\launch.ps1" -Edition paid -PythonExe $PythonExe
exit $LASTEXITCODE
