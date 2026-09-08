$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

python -m pip install --require-hashes -r .\requirements.lock
if ($LASTEXITCODE -ne 0) { throw "Validation command failed with exit code $LASTEXITCODE" }
python -m pip install -e . --no-deps
if ($LASTEXITCODE -ne 0) { throw "Validation command failed with exit code $LASTEXITCODE" }
python -m pip install -c .\constraints.txt bandit cyclonedx-bom pip-audit pyinstaller pytest ruff
if ($LASTEXITCODE -ne 0) { throw "Validation command failed with exit code $LASTEXITCODE" }
python -m pip check
if ($LASTEXITCODE -ne 0) { throw "Validation command failed with exit code $LASTEXITCODE" }
python -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "Validation command failed with exit code $LASTEXITCODE" }
python -m compileall -q src
if ($LASTEXITCODE -ne 0) { throw "Validation command failed with exit code $LASTEXITCODE" }
ruff check src tests tools
if ($LASTEXITCODE -ne 0) { throw "Validation command failed with exit code $LASTEXITCODE" }
bandit -q -ll -r src -c pyproject.toml
if ($LASTEXITCODE -ne 0) { throw "Validation command failed with exit code $LASTEXITCODE" }
python -m pip_audit -r requirements.lock --strict
if ($LASTEXITCODE -ne 0) { throw "Validation command failed with exit code $LASTEXITCODE" }
