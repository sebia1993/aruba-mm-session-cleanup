# Aruba MM Session Cleanup v0.2.0

## 변경 내용

- 앱 전용 known_hosts와 최초 SSH SHA-256 지문 승인, 저장된 키 변경 차단을 추가했습니다.
- `no paging` 실패·잔존 paging marker·MM/Mobility Conductor/WLC 신원 불확실 시 삭제를 fail-closed로 차단합니다.
- Role, Host, 계정, 포트, timeout을 공통 규칙으로 검증하고 secret dataclass 필드를 `repr()`에서 제외했습니다.
- CLI의 평문 `--password`·`--enable-password` 옵션을 제거하고 안전 입력으로 전환했습니다.
- GUI의 1회·주기 실행은 매 회 대상 MAC을 보여 주고 `DELETE N`을 정확히 입력해야 삭제합니다.
- Web UI는 `127.0.0.1`에만 bind하고 loopback Host, CSRF, 요청 크기, SSH `TRUST`, 대상 `DELETE N`, fresh snapshot 일치를 확인합니다.
- Runner 비차단 잠금으로 동시에 두 조회·삭제가 실행되지 않도록 했습니다.
- PR과 `main` push에 pytest, ruff, Bandit, pip-audit, locked runtime, SBOM 및 Windows package smoke 검증을 적용했습니다.
- Release에는 Windows ZIP, SHA-256 sidecar, CycloneDX SBOM을 함께 제공합니다.
- README·보안 정책·안전 모델·검증 한계를 한국어 중심으로 갱신하고 MIT License를 명시했습니다.
- 공개 fixture의 IP는 RFC 5737 문서용 대역으로 정리했습니다.

## 보존된 동작

- 즉시 1회 실행과 주기 실행을 모두 유지합니다. 단, 주기 실행도 각 회차마다 명시 승인이 필요합니다.
- 삭제 대상은 최초 승인 snapshot으로 고정하며 중복 MAC은 한 번만 처리합니다.
- 삭제 명령은 blind retry하지 않습니다.
- 삭제 후 같은 Role을 다시 조회하고 재등장 MAC은 기록만 하며 자동 재삭제하지 않습니다.

## Release 자산

- `aruba-mm-session-cleanup_v0.2.0_windows.zip`
- `aruba-mm-session-cleanup_v0.2.0_windows.zip.sha256`
- `aruba-mm-session-cleanup_v0.2.0_sbom.cdx.json`

GitHub가 자동으로 표시하는 `Source code (zip)`과 `Source code (tar.gz)`는 Windows 실행 패키지가 아닙니다.

## 사용 시작

1. Windows ZIP과 `.sha256`을 내려받아 ZIP의 SHA-256을 확인합니다.
2. 압축을 풀고 `gui\ArubaMMCleanupGUI.exe` 또는 `web\start_webapp.cmd`(`ArubaMMCleanupWeb.exe` 실행)를 사용합니다.
3. 최초 SSH 지문은 장비 관리자에게 별도 경로로 확인한 뒤 승인합니다.
4. 조회된 대상 snapshot을 확인하고 `DELETE N`을 정확히 입력합니다.

## 검증과 한계

자동 검증은 합성 fixture와 fake connection만 사용합니다. 실제 MM/WLC, 펌웨어별 CLI 차이, 현장 AAA/ClearPass 재인증, 운영망 장애, 변경 승인까지 검증했다는 뜻은 아닙니다. 실제 장비 성과 수치나 현장 검증 결과는 이 Release에서 주장하지 않습니다.
