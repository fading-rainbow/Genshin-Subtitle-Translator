param(
    [switch]$Preview
)

$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot 'genshin_translator.py'
$configPath = Join-Path $PSScriptRoot 'config.json'
$venvPython = Join-Path $PSScriptRoot '.venv-cpython\Scripts\python.exe'
$python = if (Test-Path -LiteralPath $venvPython) {
    $venvPython
} else {
    (Get-Command python -ErrorAction Stop).Source
}
if (-not $Preview) {
    $config = Get-Content -LiteralPath $configPath -Raw -Encoding utf8 | ConvertFrom-Json
    $keyEnvironmentName = [string]$config.api.api_key_env
    if ([string]::IsNullOrWhiteSpace($keyEnvironmentName)) {
        throw 'config.json is missing api.api_key_env.'
    }
    $keyValue = [Environment]::GetEnvironmentVariable($keyEnvironmentName, 'Process')
    if ([string]::IsNullOrWhiteSpace($keyValue)) {
        $keyValue = [Environment]::GetEnvironmentVariable($keyEnvironmentName, 'User')
    }
    if ([string]::IsNullOrWhiteSpace($keyValue)) {
        throw "Environment variable $keyEnvironmentName is not set."
    }
    # A UAC-elevated child does not inherit this shell's temporary environment.
    # Store it for the current Windows user; the elevated launcher below reads
    # it back without including the key in its command line.
    [Environment]::SetEnvironmentVariable($keyEnvironmentName, $keyValue, 'User')
}

# A UAC child does not preserve the parent process environment. The elevated
# PowerShell restores the key from the current user's environment, then starts
# Python with a hidden console. The encoded command contains paths and the
# variable name only, never the API key.
$safePython = $python.Replace("'", "''")
$safeScript = $scriptPath.Replace("'", "''")
$safeKeyName = if ($Preview) { '' } else { $keyEnvironmentName.Replace("'", "''") }
$pythonInvocation = if ($Preview) {
    "& '$safePython' '$safeScript' --overlay-preview"
} else {
    "& '$safePython' '$safeScript'"
}
$elevatedCommand = @"
`$ErrorActionPreference = 'Stop'
`$keyEnvironmentName = '$safeKeyName'
if (`$keyEnvironmentName) {
    `$keyValue = [Environment]::GetEnvironmentVariable(`$keyEnvironmentName, 'User')
    if ([string]::IsNullOrWhiteSpace(`$keyValue)) { throw "Environment variable `$keyEnvironmentName is not set." }
    [Environment]::SetEnvironmentVariable(`$keyEnvironmentName, `$keyValue, 'Process')
}
$pythonInvocation
exit `$LASTEXITCODE
"@
$encodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($elevatedCommand))

# A process at the same integrity level as the game is required for a reliable
# topmost, click-through overlay above an elevated game window.
Write-Host 'Starting elevated Genshin Subtitle Translator...'
Start-Process -FilePath powershell.exe -ArgumentList "-NoProfile -ExecutionPolicy Bypass -EncodedCommand $encodedCommand" -WorkingDirectory $PSScriptRoot -Verb RunAs -WindowStyle Hidden
