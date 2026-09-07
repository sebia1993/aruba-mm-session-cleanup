# 포트폴리오 검토 안내: 상태 변경 자동화의 승인과 불확실성

이 프로젝트에서 검토할 역량은 **실제 사용자 세션을 변경하는 자동화에서 승인 대상과 실행 대상을 일치시키고, 응답 유실을 성공으로 오인하지 않는 설계**입니다. 아래 재현은 테스트의 fake connection만 사용합니다.

## 코드로 확인하는 설계 판단

| 검토 질문 | 구현 근거 | 재현 근거 |
|---|---|---|
| 운영자가 승인하지 않은 대상을 삭제할 수 있는가? | [cleanup.py](../src/aruba_mm_cleanup/cleanup.py)의 `MmCleanupRunner.run_once`, [models.py](../src/aruba_mm_cleanup/models.py)의 대상 계획 | [test_safety_hardening.py](../tests/test_safety_hardening.py)의 `test_runner_without_target_approval_never_calls_delete` |
| 명령 응답이 유실되면 자동으로 재전송하는가? | [cleanup.py](../src/aruba_mm_cleanup/cleanup.py)의 `retry_once=False`, [session.py](../src/aruba_mm_cleanup/session.py) | [test_cleanup.py](../tests/test_cleanup.py)의 `test_delete_command_exception_is_unknown_without_retry` |
| 삭제 응답과 재조회 결과를 함께 보는가? | [cleanup.py](../src/aruba_mm_cleanup/cleanup.py)의 사후 검증 | 같은 테스트 파일의 `test_run_once_deletes_snapshot_and_verifies_remaining` |
| 성공 응답 뒤 MAC이 재등장하면 어떻게 처리하는가? | 같은 구현의 `reappeared_macs`와 결과 상태 | `test_run_once_flags_successfully_deleted_mac_that_reappears` |
| GUI와 Web도 같은 승인 계약을 유지하는가? | [gui_app.py](../src/aruba_mm_cleanup/gui_app.py), [web_app.py](../src/aruba_mm_cleanup/web_app.py) | [test_safety_hardening.py](../tests/test_safety_hardening.py)의 매 주기 승인 및 fresh snapshot 검사 |
| 사용자 MAC과 BSSID 등 다른 MAC 값을 구분하는가? | [parser.py](../src/aruba_mm_cleanup/parser.py) | [test_parser.py](../tests/test_parser.py), [합성 fixture](../tests/fixtures/) |

정규화·중복 제거는 한 실행 대상의 중복을 줄이는 장치입니다. 네트워크 장애를 포함한 **exactly-once 실행 보장**은 아닙니다. 이미 전송한 삭제를 되돌릴 수 없으므로, timeout은 `unknown`으로 남기고 재조회·운영자 판단으로 이어지는 이유를 설명할 수 있어야 합니다.

## 장비 없이 안전 계약 재현하기

Windows PowerShell과 Python 3.11에서 저장소 루트 기준으로 실행합니다. 의존성 설치 이후의 테스트에는 실제 MM 주소나 계정이 필요하지 않습니다.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m pip install -c constraints.txt pytest
.\.venv\Scripts\python.exe -m pytest -q tests/test_cleanup.py tests/test_safety_hardening.py tests/test_parser.py
```

위 테스트의 `FakeConnection`은 전송할 명령을 메모리의 목록에 기록하고 합성 응답을 반환합니다. 실제 삭제 명령을 실행하기 위한 CLI/GUI 데모가 아닙니다. 일부 단위 테스트는 runner 내부 로직을 격리하려고 연결 안전 검사를 대체하므로, 그 결과만으로 SSH 지문·장비 신원·페이징 게이트가 검증됐다고 보지 않습니다. `test_safety_hardening.py`의 별도 연결·승인 검사를 함께 읽습니다.

## 결과 상태를 설명하는 순서

1. 최초 조회 결과에서 어떤 사용자 MAC을 선택·제외했는지 확인합니다.
2. 승인된 snapshot과 실제 전송된 명령 목록이 일치하는지 확인합니다.
3. 삭제 응답이 명확했는지, 사후 조회가 완료됐는지 각각 확인합니다.
4. `verified_deleted`, `unknown`, 잔존·재등장 결과를 구분합니다. 빈 응답이나 예외를 정상 처리로 승격하지 않습니다.
5. 요약 JSON·MAC별 JSONL을 통해 판단 과정을 확인하되, 이 자료도 운영 대상 정보이므로 실제 생성본을 공개하지 않습니다.

상태별 의미와 취소 경계는 [안전 모델](SAFETY_MODEL.md), UI·runner·session의 책임은 [아키텍처](ARCHITECTURE.md)에 정리되어 있습니다.

## 검증 근거와 후속 과제

- [PR Validation 실행](https://github.com/sebia1993/aruba-mm-session-cleanup/actions/workflows/pr-validation.yml)의 해당 SHA에서 Linux 품질·보안 job과 Windows 패키지 job이 모두 완료됐는지 확인합니다. [정의](../.github/workflows/pr-validation.yml)는 GUI/Web smoke와 release verifier를 포함합니다.
- [Releases](https://github.com/sebia1993/aruba-mm-session-cleanup/releases)의 ZIP·SHA-256·SBOM은 태그에 연결된 별도 배포 근거입니다. 소스의 테스트 통과만으로 다른 바이너리의 호환성을 주장하지 않습니다.
- 남은 실증은 펌웨어별 global-user-table 출력, 실제 장비 명령 응답 유실, AAA/ClearPass 재인증과 단말 재접속, 조직의 변경 승인·복구 절차입니다. 현재 공개 근거의 한계는 [검증 보고서](VALIDATION_REPORT.md)를 참고합니다.

현장 적용 전에는 허가된 테스트 환경에서 영향과 복구 절차를 확인해야 합니다. 이 문서의 자동 테스트는 운영 사용자 세션 삭제나 현장 성과를 입증하는 기록이 아닙니다.
