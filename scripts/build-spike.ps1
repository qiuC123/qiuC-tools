$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --name wxcli `
    --paths (Join-Path $projectRoot 'src') `
    --collect-submodules playwright `
    --distpath (Join-Path $projectRoot 'dist\spike') `
    --workpath (Join-Path $projectRoot 'build\spike') `
    --specpath (Join-Path $projectRoot 'build\spike') `
    (Join-Path $projectRoot 'src\wxcli\__main__.py')
