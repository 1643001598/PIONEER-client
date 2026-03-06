$ErrorActionPreference = 'Stop'

$projectVenv = Join-Path $PSScriptRoot '.venv\Scripts\Activate.ps1'
$workspaceVenv = Join-Path $PSScriptRoot '..\..\.venv\Scripts\Activate.ps1'

if (Test-Path $projectVenv) {
    . $projectVenv
    Write-Host "Activated venv: $projectVenv" -ForegroundColor Green
    return
}

if (Test-Path $workspaceVenv) {
    . $workspaceVenv
    Write-Host "Activated venv: $workspaceVenv" -ForegroundColor Green
    return
}

Write-Host "Venv activate script not found." -ForegroundColor Red
Write-Host "Checked paths:" -ForegroundColor Yellow
Write-Host " - $projectVenv"
Write-Host " - $workspaceVenv"
Write-Host "Create one with: python -m venv .venv" -ForegroundColor Yellow
