# 검증 보고서

이 문서는 Aruba MM Session Cleanup의 자동 검증이 무엇을 증명하고, 무엇을 증명하지 않는지 구분합니다.

## 자동 검증 경로

PR Validation은 Windows runner에서 다음 순서로 수행합니다.

```text
pip install -e ".[dev]" + constraints
        ↓
pip check
        ↓
pytest
        ↓
compileall src
        ↓
Windows GUI/Web 통합 패키지 빌드
        ↓
GUI smoke
        ↓
Web smoke
        ↓
release package verifier
```

기본 validation 스크립트는 `tools/validate.ps1`이며 실제 MM 접속 없이 fixture와 fake connection을 사용합니다.

## 자동 검증이 확인하는 영역

### Parser

- Aruba global user table 형식별 사용자 MAC 추출
- MAC normalize / dedupe
- Role filtering
- Type=N/A 등 parser decision
- MAC-like 값 중 사용자 대상과 비대상 구분

### 변경 안전성

- target snapshot에 포함된 MAC만 삭제 명령으로 변환
- 삭제 명령 응답 실패 시 blind retry 금지
- 삭제 결과 상태 분류
- 삭제 후 검증 조회
- remaining / verified absent / reappeared 상태 처리
- 재등장 MAC 자동 재삭제 금지
- 취소 요청 경계 처리

### 세션/실행 안정성

- 세션 재사용과 명시적 disconnect
- timeout/error 처리
- GUI 종료 시 네트워크 작업과 UI 종료의 분리
- 장시간 UI 이력/로그 상한

### 감사 자료

- 실행별 summary JSON
- 누적 deletion history JSONL
- 저장 실패가 네트워크 변경 재시도로 이어지지 않는지
- raw 장비 출력이 감사 파일에 그대로 저장되지 않는지

### Windows 배포

- GUI 실행 파일 생성
- portable Web 실행 경로 생성
- 통합 ZIP 구조
- GUI smoke
- Web smoke

## 자동 검증이 증명하지 않는 영역

CI가 green이어도 다음을 자동으로 보장하지 않습니다.

- 실제 운영 MM/Managed Device의 펌웨어별 CLI 호환성
- 현장 AAA/ClearPass 정책에 따른 삭제 후 재인증 동작
- 실제 운영망 latency/packet loss 상황의 모든 timeout 조합
- 특정 Role을 삭제 대상으로 사용해도 된다는 운영 승인
- 삭제 대상 MAC이 업무적으로 제거 가능한 세션인지 여부

이 영역은 허가된 실제 환경에서 별도로 판단해야 합니다.

## 실패 판정 원칙

네트워크 변경 자동화에서는 테스트가 통과했다는 이유로 불확실한 상태를 성공으로 승격하지 않습니다.

| 관측 | 판정 |
|---|---|
| 삭제 응답 성공 + 검증 조회에서 MAC 없음 | 성공 |
| 삭제 응답 실패/timeout | 확인 필요 |
| 검증 조회에서 MAC 잔존 | 미완료/확인 필요 |
| 성공 MAC이 다시 발견됨 | reappeared |
| 감사 파일 저장 실패 | 네트워크 결과 유지 + 로컬 warning |

## 실제 운영 검증 기록 시 주의

실제 장비 검증 결과를 공개 문서에 추가할 경우 다음 정보는 비식별화해야 합니다.

- 실제 MM/MD IP와 hostname
- 계정명과 인증 정보
- 실제 사용자 MAC
- 내부 Role/SSID/AAA 정책명
- 운영망 전체 CLI 원문

공개 저장소에는 검증 조건과 결과의 의미만 남기고 실제 운영 데이터는 포함하지 않습니다.
