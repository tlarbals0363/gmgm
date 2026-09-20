param([string]$PythonExe = '')
& "$PSScriptRoot\scripts\launch.ps1" -Edition free -PythonExe $PythonExe -CheckOnly
exit $LASTEXITCODE
