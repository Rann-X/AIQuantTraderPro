param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Run install.ps1 first.' }
$startArguments = @('-m','quant.desktop','start')
if ($NoBrowser) { $startArguments += '--no-browser' }
New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot 'logs') | Out-Null
Start-Process -FilePath $pythonPath -ArgumentList $startArguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $PSScriptRoot 'logs\supervisor-output.log') -RedirectStandardError (Join-Path $PSScriptRoot 'logs\supervisor-error.log')
