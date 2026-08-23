# 개발 가이드

## 변경 원칙

Aruba MM Session Cleanup은 실제 사용자 세션을 삭제하는 명령을 포함합니다. 기능 변경보다 **변경 안전성 보존**을 우선합니다.

- 기존 조회/삭제/검증 상태 전이를 임의로 합치지 않습니다.
- `aaa user delete mac <mac>`의 blind retry를 추가하지 않습니다.
- 최초 조회 snapshot 밖의 MAC을 현재 삭제 batch에 추가하지 않습니다.
- `reappeared` MAC을 자동 재삭제하지 않습니다.
- parser가 확신하지 못한 값을 성공 또는 삭제 대상으로 승격하지 않습니다.
- 실제 장비 주소·계정·MAC·raw output을 fixture에 넣지 않습니다.
- known_hosts, 장비 신원, 페이징, target 승인, 동시 실행 gate를 우회하지 않습니다.

## 로컬 검증

```powershell
python -m pip install --require-hashes -r .\requirements.lock
python -m pip install -e ".[dev]" -c .\constraints.txt
python -m pip check
python -m pytest
python -m compileall src
ruff check src tests tools
bandit -q -ll -r src -c pyproject.toml
python -m pip_audit -r requirements.lock --strict
```

또는:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\validate.ps1
```

## Windows 패키지 검증

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_windows_gui_exe.ps1
python .\tools\verify_release_package.py --dist .\dist --smoke-gui --smoke-web --require-gui-smoke --require-web-smoke
```

## 변경 작업 순서

1. 재현 가능한 테스트/fixture로 현재 동작을 확인합니다.
2. 안전 계약에 영향을 주는지 먼저 판단합니다.
3. 가장 작은 범위로 수정합니다.
4. 관련 unit test를 실행합니다.
5. 전체 `tools/validate.ps1`을 실행합니다.
6. Windows 배포 관련 변경이면 통합 패키지와 GUI/Web smoke까지 확인합니다.

## 테스트 데이터

테스트는 RFC 5737 문서용 주소, 로컬 관리 MAC, 합성 CLI 출력만 사용합니다. 실장비에서 복사한 원문을 그대로 커밋하지 않습니다.

## Release

문서-only 변경은 새 사용자 Release를 만들 이유가 없습니다. Release는 `pyproject.toml`과 `__version__`을 일치시킨 semver tag(`vX.Y.Z`)를 검증된 `main` commit에 push할 때 시작됩니다. 기존 tag와 Release는 수정·삭제하지 않습니다.

Release notes에는 기능 영향, 안전성 영향, 검증 방법, Windows ZIP·SHA-256·CycloneDX SBOM을 사용자 관점에서 기록합니다.
