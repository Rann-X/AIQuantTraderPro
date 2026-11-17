param([string]$EnvironmentPath = '.venv', [switch]$SkipTests)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$environmentTarget = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot $EnvironmentPath))
if (-not $environmentTarget.StartsWith($PSScriptRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The virtual environment must be a subdirectory of this project.'
}
python -c 'import sys; assert sys.version_info >= (3,11)'
if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11+ from python.org and enable PATH.' }
$pythonPath = Join-Path $environmentTarget 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    python -m venv $environmentTarget
    if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Virtual environment creation failed.' }
}
& $pythonPath -m pip --version *> $null
if ($LASTEXITCODE -ne 0) {
    # Some managed Windows Python builds omit ensurepip; bootstrap only into this venv.
    python -m pip --python $pythonPath install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw 'pip bootstrap failed. Install a Python distribution with pip/ensurepip.' }
}
$pythonMinor = & $pythonPath -c 'import sys; print(sys.version.split()[0])'
if ($LASTEXITCODE -ne 0) { throw 'Cannot read virtual-environment Python version.' }
if ($pythonMinor.Trim().StartsWith('3.14.')) {
    & $pythonPath -m pip install -e '.[dev]' -c requirements-win-py314.lock
} else {
    & $pythonPath -m pip install -e '.[dev]'
}
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $pythonPath -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency conflict detected.' }
if (-not $SkipTests) {
    & $pythonPath -m pytest -q --junitxml=reports/install-tests.xml
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
}
Write-Host 'Installation verified. Double-click start.bat to open the local terminal.'
