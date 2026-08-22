# Security Policy

## 민감정보

이 저장소의 Issue, Pull Request, Discussion, 로그, screenshot, fixture에 다음 정보를 올리지 마십시오.

- 실제 Aruba MM/MD IP 또는 hostname
- 사용자명, 암호, enable 암호, SSH key
- 실제 사용자 MAC
- 내부 Role/SSID/AAA/ClearPass 정책명
- 운영망 전체 CLI raw output
- 내부 경로·조직명 등 환경 식별 정보

재현이 필요하면 RFC 5737 문서용 IP와 임의 MAC/Role 이름을 사용하십시오.

## 운영 안전성

이 프로그램은 `aaa user delete mac <mac>`을 실행할 수 있습니다. 따라서 취약점 재현이나 테스트 목적으로 승인되지 않은 실제 장비에서 실행하지 마십시오.

특히 다음 변경은 안전성 회귀로 간주합니다.

- 삭제 명령 자동 재시도 추가
- 최초 query snapshot 외 MAC의 자동 삭제
- parser 불확실 항목의 자동 삭제 승격
- 검증 없이 삭제 성공 확정
- `reappeared` MAC 자동 재삭제

## 보고 방법

민감한 보안 문제를 발견한 경우 공개 이슈에 실제 자격 증명이나 운영망 데이터를 첨부하지 마십시오. 공개 재현에는 비식별 데이터와 최소한의 기술 설명만 사용하십시오.

이미 비밀정보가 커밋된 경우 해당 값을 즉시 폐기/교체한 뒤 Git 기록과 공개 artifact에 남은 복사본을 별도로 점검해야 합니다.
