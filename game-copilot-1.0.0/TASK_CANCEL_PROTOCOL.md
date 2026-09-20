# 3단계 작업 통신·취소·연결 유지 규격 (1.0.3)

2단계 TaskManager를 그대로 사용합니다. 전송/감시는 `copilot/task_control.py`가 담당합니다.
v1 상태 JSON과 기존 `/v1/action` 계약은 유지합니다. 장기 작업은 별도 `/v1/tasks/*`를 사용합니다.
실시간 게임 실행기는 이번 단계에 없습니다. `execution_mode:realtime`은 계속 조언 전용입니다.

## 기능 선언

기존 capabilities에 아래 선택 필드를 추가합니다:
```json
{
  "version":1,
  "execution_mode":"turn_based",
  "read_only":false,
  "supports_tasks":true,
  "supports_cancel":true,
  "task_protocol":1,
  "lease_seconds":3
}
```
task_protocol이 있으면 supports_tasks/supports_cancel이 모두 true여야 하며 lease_seconds는 2~5초입니다.
task_protocol이 없는 기존 v1 단발 연결은 그대로 작동합니다. supports_tasks만 있는 선언은 조언용이며 장기 작업을 단발 실행으로 보내지 않습니다.
관측 전용은 기존처럼 `can_act:false, actions:[]`를 요구하고 실행을 막습니다.

## 전송

127.0.0.1 루프백 HTTP만 허용. 모든 요청은 별도 무작위 브리지 토큰으로 Bearer 인증.
Origin 요청을 거절하고 CORS를 열지 마세요. NVIDIA/OpenAI 키를 브리지 토큰으로 사용하지 마세요.
상태/오류에 인증 비밀을 넣지 마세요. 본체의 개별 task 요청 소켓 타임아웃은 1초입니다.

POST `/v1/tasks/start`:
```json
{
  "schema_version":1,
  "game_id":"cancel-test",
  "task_id":"본체가 발급한 고유 ID",
  "controller_id":"본체 실행 세션 ID",
  "action_id":"count",
  "expected_revision":"현재 게임 세션과 상태 버전",
  "expires_at":1790000000.5,
  "max_seconds":30
}
```

expires_at은 Unix 초 기준 시작 요청 유효기간이며 본체가 전송 시 2초 후로 설정합니다.
실제 실행 제한과 lease는 모듈의 단조 시계로 관리합니다. 현재 본체 작업 제한은 30초입니다.
모듈은 인증·버전·게임·ID·소유자·만료·revision·허용 후보·자원·안전 조건을 확인하고 원자적으로 작업을 예약합니다.
접수 후 즉시 `accepted`를 반환하며 HTTP 처리 안에서 전체 작업 완료를 기다리지 마세요.
같은 task_id로 다시 start가 와도 재실행하지 않고 기존 결과를 반환해야 합니다. 다른 행동/소유자 재사용은 거절하세요.

POST `/v1/tasks/status`, `/v1/tasks/heartbeat`, `/v1/tasks/cancel` 공통 본문:
```json
{"schema_version":1,"game_id":"cancel-test","task_id":"동일 작업 ID","controller_id":"동일 소유자 ID"}
```

- status: 조회만 수행. lease를 연장하지 않음.
- heartbeat: 현재 소유자의 미종료 작업만 유지 기간 갱신. 만료 검사 후 갱신해야 함.
- cancel: 실제 조작/입력 해제를 확인한 뒤 cancelled. 아직 종료 전이면 accepted/running 보고도 가능하지만 본체는 중단 확인 전 결과 불명/차단을 유지함.
- 알 수 없는 ID의 cancel은 취소 기록을 만들어, 순서가 바뀌어 뒤늦게 도착하는 start를 차단해야 함.
- 본체는 start를 자동 재전송하지 않음. 취소는 멱등성을 요구하며 중지 버튼과 감시기의 정리 요청이 겹칠 수 있음.

## 모든 작업 엔드포인트의 응답

```json
{
  "game_id":"cancel-test",
  "task_id":"동일 작업 ID",
  "controller_id":"동일 소유자 ID",
  "status":"running",
  "sequence":3,
  "progress":0.25,
  "message":"실행기가 확인한 진행 상태"
}
```

status는 accepted/running/succeeded/failed/cancelled 중 하나. starting/cancel_requested/unknown은 본체 내부 상태입니다.
sequence는 작업별 단조 증가 정수입니다. 같은 상태를 단순 재조회할 때는 같은 sequence를 반환해도 됩니다.
progress는 null 또는 0~1의 유한 숫자이며 감소시키지 않습니다. 성공 시 본체가 1로 기록합니다.
본체는 task_id/game_id/controller_id를 모두 검사하며 오래되거나 중복인 응답은 2단계 규칙대로 무시합니다.
취소보다 실제 완료가 먼저였다면 succeeded를 유지하세요. 이미 끝난 작업을 cancelled로 바꾸지 마세요.
실패 HTTP 응답만으로 미실행/중단을 단정하지 않습니다. 확인되지 않은 결과는 unknown으로 처리합니다.

## 독립 중단의 필수 조건

1. 감시기는 HTTP 요청과 본체 실행 루프와 별도로 돌아야 합니다. 상태 조회가 끊겨도 동작해야 합니다.
2. 마지막 유지 신호부터 lease_seconds가 지나면 작업을 중단하고 모든 지속 입력을 해제해야 합니다.
3. 작업 최대시간, 명시적 cancel, 서버 정상 종료에서도 같은 해제 경로를 사용해야 합니다.
4. 만료·취소·완료된 작업은 늦은 heartbeat/start로 다시 살아나지 않아야 합니다.
5. 유지 신호는 최대 실행시간을 늘리지 못해야 합니다.
6. 취소 기록과 완료 기록을 안전하게 유지하세요. 참조 모듈은 세션 내 최대 10,000개 기록 후 새 예약을 거절합니다.
7. 게임 주 스레드가 실제 명령을 적용한다면 revision 검사·행동 적용·취소의 최종 원자성도 게임 측에서 보장해야 합니다.
8. 통신 복구 뒤 결과 조회는 새 실행이 아닙니다. 불명 결과를 성공/취소로 임의 확정하지 마세요.

참조 카운터 모듈은 50ms 간격 독립 스레드로 3초 lease를 검사합니다. 하드 실시간 보장이 아니며 OS 스케줄링 지연이 있을 수 있습니다.
이 모듈은 실제 게임용 이동/공격 구현이 아닙니다. 장애물 대응과 이벤트 기반 재판단은 이후 단계입니다.

## 예제

- `python -m examples.cancel_disconnect_demo`: API 없는 중지/연결 단절 자동 시험.
- `python -m examples.cancel_bridge`: 독립 HTTP 카운터 브리지. 서버를 켜둔 채 본체 종료를 시험할 수 있음.
- GUI `시험 작업 시작`: 내부 카운터를 같은 TaskControl 경로로 시작. 프로필/키 파일에 쓰지 않음.
- GUI `시험 통신 끊기`: 내부 시험의 유지 신호와 주기적 상태 조회 차단. 실제 네트워크 설정을 변경하지 않음.
- GUI `작업 결과 확인`: 동일 작업 ID의 결과만 조회. 유지 신호 없음.

작업 기록과 controller_id는 현재 본체 실행 세션에만 유지됩니다. 강제 종료 뒤 결과 조회 자동 복구는 이번 범위가 아닙니다.
외부 모듈은 본체 재시작 여부와 무관하게 이전 작업의 lease를 만료시켜야 합니다.
