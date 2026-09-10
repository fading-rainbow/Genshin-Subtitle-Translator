$ErrorActionPreference = 'Stop'

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdministrator)) {
    $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    Start-Process -FilePath powershell.exe -ArgumentList $arguments -Verb RunAs
    exit 0
}

$translatorScript = Join-Path $PSScriptRoot 'genshin_translator.py'
$targets = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -in @('python.exe', 'pythonw.exe') -and $_.CommandLine -like "*$translatorScript*"
}

if (-not $targets) {
    Write-Host 'No running Genshin Subtitle Translator process was found.'
    exit 0
}

foreach ($target in $targets) {
    Stop-Process -Id $target.ProcessId -Force
    Write-Host "Stopped Genshin Subtitle Translator process $($target.ProcessId)."
}
