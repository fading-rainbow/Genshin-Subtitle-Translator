$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv-cpython\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Project virtual environment not found. Run the installation steps in README.md first.'
}

& $python -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin `
    --name GenshinSubtitleTranslator `
    --add-data "config.example.json;." `
    genshin_translator.py

Write-Host "Created dist\GenshinSubtitleTranslator.exe"
