# 보안 정책

## 지원 버전

| 버전 | 보안 업데이트 |
|---|---|
| 0.2.x | 지원 |
| 0.1.x 이하 | 지원 종료 |

## 취약점 보고

실제 장비 정보나 자격 증명을 공개 Issue에 올리지 마십시오. 가능하면 GitHub의
[비공개 보안 권고 제출](https://github.com/sebia1993/aruba-mm-session-cleanup/security/advisories/new)을 사용하십시오.
해당 기능을 사용할 수 없다면 공개 Issue에는 재현 데이터 없이 연락 요청만 남기고, 저장소 소유자와 비공개 전달 경로를 먼저 정하십시오.

보고할 때 다음 정보를 비식별 상태로 포함하면 도움이 됩니다.

- 영향을 받는 버전과 실행 경로(GUI, Web, CLI)
- 기대한 fail-closed 동작과 실제 동작의 차이
- RFC 5737 문서용 IP와 로컬 관리 MAC으로 만든 최소 재현
- 삭제 명령이 실제 전송되었는지 여부

## 공개하면 안 되는 정보

- 실제 MM/WLC IP, hostname, SSH 지문
- 사용자명, SSH/Enable 암호, 개인키, 토큰
- 실제 사용자 MAC, 내부 Role/SSID/AAA/ClearPass 정책명
- 운영망 전체 CLI 출력, 내부 경로와 조직 식별 정보

노출된 비밀은 Git 기록 수정만으로 안전해지지 않습니다. 먼저 값을 폐기·교체한 뒤 Git 기록, Release 자산, CI 로그를 함께 점검해야 합니다.

## 안전성 회귀로 보는 변경

다음은 보안 또는 운영 안전성 회귀로 취급합니다.

- 앱 known_hosts를 우회하거나 변경된 SSH 서버 키를 자동 승인
- MM/WLC 신원 확인 또는 페이징 해제 실패 뒤 삭제 진행
- Role/Host/계정 검증 없이 CLI 문자열 구성
- 조회 snapshot 미리보기와 명시 승인 없이 즉시·주기 삭제
- 삭제 명령 blind retry 또는 재등장 MAC 자동 재삭제
- Web UI의 non-loopback 바인딩, CSRF 우회, 확인 문구 우회
- 로그·예외·`repr()`에 암호 포함

## 운영 경계

이 도구는 `aaa user delete mac <mac>`을 실행할 수 있습니다. 허가된 장비와 승인된 변경 절차에서만 사용하십시오. 공개 CI는 합성 fixture와 fake connection만 사용하며 실제 장비 호환성이나 운영 승인을 증명하지 않습니다.
