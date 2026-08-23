$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

python -m pip install --require-hashes -r .\requirements.lock
python -m pip install -e . --no-deps
python -m pip install -c .\constraints.txt bandit cyclonedx-bom pip-audit pyinstaller pytest ruff
python -m pip check
python -m pytest -q
python -m compileall -q src
ruff check src tests tools
bandit -q -ll -r src -c pyproject.toml
python -m pip_audit -r requirements.lock --strict
