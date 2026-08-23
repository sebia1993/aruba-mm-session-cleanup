# Changelog

## 0.2.0

- SSH 서버 키 최초 승인과 앱 known_hosts pinning을 추가하고 변경된 키를 차단합니다.
- MM/Mobility Conductor/WLC 신원 또는 페이징 해제 상태를 확인할 수 없으면 삭제하지 않습니다.
- 1회·주기 실행 모두 target snapshot 미리보기와 매 회 명시 승인을 요구합니다.
- Role·접속 입력 검증, password repr 차단, CLI 평문 password option 제거를 적용합니다.
- Web UI를 loopback 전용 조회→미리보기→확인 흐름으로 바꾸고 Host/CSRF/요청 크기/fresh snapshot 검사를 추가합니다.
- Runner 동시 실행 잠금과 fail-closed 합성 테스트를 추가합니다.
- runtime hash lock, ruff, Bandit, pip-audit, CycloneDX SBOM 및 `main` push CI를 추가합니다.
- Release 자산을 semver tag 기반 Windows ZIP, SHA-256 sidecar, CycloneDX SBOM으로 표준화합니다.
- MIT License, 보안 정책, 한국어 README와 자동 검증 한계를 정리합니다.

## 0.1.0

- Windows 11 통합 ZIP에 GUI 실행 경로와 웹앱 실행 경로를 함께 제공합니다.
- 웹앱은 `web\start_webapp.cmd`로 실행하며, PyInstaller 빌드 실행 파일을 사용해 Python/런타임 별도 설치 없이 실행할 수 있습니다.
- GitHub Release 직접 업로드 asset은 `aruba-mm-cleanup_<tag>_windows.zip` 1개로 고정하고, SHA256 checksum은 Release notes 본문에 기록합니다.
- 최종 사용자용 Release ZIP은 `README_START_HERE_KO.txt`, `gui/`, `web/` 구조를 사용합니다.
- CLI 코드는 저장소에 유지하지만 최종 사용자용 Release ZIP에는 포함하지 않습니다.
- Windows 11 GUI 대시보드에서 Aruba MM profiling Role MAC 조회와 삭제를 실행할 수 있습니다.
- `show global-user-table list role <role>` 조회 후 `aaa user delete mac <mac>` 삭제 명령을 실행합니다.
- 같은 MAC이 여러 줄에서 발견되어도 정규화된 MAC 기준으로 삭제 명령은 한 번만 실행합니다.
- GUI는 프로그램 실행 중 같은 장비 세션을 유지하고, 종료 또는 수동 연결 해제 시 세션을 닫습니다.
- 수동 1회 실행과 주기 실행에서 상단 카드는 `누적 조회 MAC`, `누적 삭제 완료`, `작업 상태`로 단순화했습니다.
- `누적 조회 MAC`과 `누적 삭제 완료`는 앱 실행 이후 작업 건수 누계로 표시하고, 실패/남은 MAC/재조회 상세는 결과 테이블에서 확인합니다.
- `삭제 대상 및 결과`와 `최근 삭제 이력`의 `MAC` 셀을 클릭하면 해당 MAC을 클립보드에 복사하고 중앙 팝업을 1초 표시합니다.
- GUI는 조회 완료 후 즉시 삭제를 시작하고, 주기 실행은 `주기(초)` 입력값을 최소 1초부터 그대로 적용합니다.
- 기존 `Timeout` 입력은 삭제 전 대기 시간이 아니라 SSH 접속/명령 응답 대기 시간이므로 `장비 응답 대기(초)`로 표시합니다.
- 실행 중에는 장비/장비 응답 대기/주기 설정 영역을 접고, 결과와 최근 삭제 이력 영역을 넓게 표시합니다.
- 최근 삭제 이력을 화면에 유지해 이전 실행에서 어떤 MAC을 삭제했는지 확인할 수 있고, `이력 전체 지우기` 버튼으로 정리할 수 있습니다.
- 삭제 명령은 네트워크 응답 실패 시 재시도하지 않고 `확인 필요` 상태로 기록해 같은 MAC 삭제 명령 재전송을 방지합니다.
- 삭제 응답이 성공이고 삭제 후 검증 조회에서 사라진 MAC만 최종 삭제 성공으로 확정합니다.
- 조회 결과의 `Type` 값이 `N/A`인 MAC은 자동 삭제를 유지하면서 결과 테이블과 로그에 관리자 직접 장비 지정 안내를 표시합니다.
- 삭제 성공 응답을 받은 MAC이 삭제 후 검증 조회에서 다시 발견되면 `재조회됨`으로 별도 강조합니다.
- 실행 중 창 닫기, 주기 실행 정지, 삭제 취소가 네트워크 timeout 또는 runner lock 때문에 Tk 메인 스레드를 멈추지 않도록 처리합니다.
- 주기 실행 대기 중에는 수동 1회 실행을 차단하고, 정지 요청은 다음 안전 지점에서 현재 삭제 루프와 검증 조회를 중단합니다.
- 최근 삭제 이력은 최대 500개 행만 유지하고 `deletion_history.jsonl`에서 재시작 후 복원합니다.
- 로그창은 최대 1000줄만 유지해 장시간 주기 실행 중 UI가 무거워지지 않도록 했습니다.
- audit JSON 저장 실패는 warning으로 남기고 실행 결과는 UI에 계속 표시합니다.
- 민감정보 제거된 Aruba 출력 fixture를 추가해 BSSID/AP MAC 오탐을 방지하는 parser 검증을 보강했습니다.
- parser는 삭제 대상으로 선택하거나 제외한 row의 reason을 audit JSON에 남깁니다.
- GitHub Actions에서 Windows ZIP 패키지를 빌드하고 GUI/웹앱 smoke, pip check, ZIP 필수 파일 검증 후 공개 Release로 자동 배포합니다.
