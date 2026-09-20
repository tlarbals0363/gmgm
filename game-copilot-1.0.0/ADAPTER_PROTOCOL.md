# Game Copilot Bridge v1

1.0.3의 장기 작업 취소·유지 신호 규격은 `TASK_CANCEL_PROTOCOL.md`를 보세요.
아래 기존 상태/단발 행동 계약은 그대로 유지합니다. `/v1/action`의 완료 응답을 장기 작업 접수 ACK로 사용하지 마세요.

이 규격은 게임 지원 그 자체가 아닙니다. 게임 모드/공식 API/외부 프로그램이 이를 구현해야 합니다.
어댑터는 실제로 볼 수 있는 정보만 반환하고 숨겨진 게임 정보/온라인 안티치트를 우회하지 마세요.

## 1.0.1 선택 확장: capabilities

HTTP 경로와 최상위 schema_version:1은 그대로 유지합니다.
별도의 기능 조회 요청이나 v2 서버는 필요하지 않습니다.
GET /v1/state 응답 최상위에 아래 필드를 선택적으로 추가합니다.

```json
"capabilities": {
  "version": 1,
  "execution_mode": "turn_based",
  "read_only": false,
  "supports_tasks": false,
  "supports_cancel": false
}
```

- execution_mode: turn_based(턴제), realtime(실시간), unknown(미지정).
- read_only: 연결 자체가 관측 전용인지 여부. true이면 can_act:false, actions:[]가 필수입니다.
- can_act는 현재 시점의 행동 가능 여부입니다. 일시적으로 false라고 관측 전용 연결은 아닙니다.
- supports_tasks: 브리지의 장기 작업 처리 능력 선언입니다.
- supports_cancel: 장기 작업 취소 능력 선언이며 true이면 supports_tasks도 true여야 합니다.
- capabilities가 있으면 위 5개 필드가 모두 필요합니다. 부울 대신 문자열이나 숫자는 거절합니다.
- capabilities가 완전히 없으면 unknown / read_only:false / supports_tasks:false / supports_cancel:false로 정규화합니다.
  이때 read_only:false는 실행 권한을 새로 부여하지 않습니다. 기존 can_act, 허용 행동, revision 검사를 그대로 적용합니다.
- 잘못되거나 알 수 없는 버전의 capabilities는 레거시로 조용히 처리하지 않고 연결을 거절합니다.
- 미지의 추가 필드는 현재 본체에서 무시합니다. 필수 필드 의미를 바꾸려면 기능 규격 버전을 올려야 합니다.
- 연결 로그와 상태 JSON에 표시하며, 매 관측에서 다시 검증합니다. 능력이 바뀌면 revision도 바꾸세요.

**이번 본체의 실행 범위:**

- turn_based / unknown: 기존 v1의 단발 행동 경로 유지.
- read_only:true 또는 realtime: 관측/조언만 제공. 자동 시작은 조언 후 중단하고 수동 실행도 허용하지 않습니다.
- supports_tasks / supports_cancel은 표시만 합니다. 작업 실행/취소 요청을 보내지 않습니다.
- supports_tasks:true여도 POST /v1/action의 의미는 바뀌지 않습니다. 장기 작업을 이 경로에 넣지 마세요.
- 1.0.2는 본체 내부의 작업 ID와 상태 관리를 추가합니다(TASK_MANAGER.md).
  1.0.3에서는 별도 `/v1/tasks/*` 경로로 취소 가능한 턴제 장기 작업과 유지 신호를 연결합니다. `TASK_CANCEL_PROTOCOL.md`를 참고하세요.

예: 실시간 게임 관측 전용 연결은 execution_mode:"realtime", read_only:true,
supports_tasks:false, supports_cancel:false, can_act:false, actions:[]를 사용합니다.
기존 v1 브리지의 정보가 없으면 실시간인지 자동으로 알아낼 수 없습니다.
새 실시간 브리지는 반드시 실행 방식을 명시해야 합니다.

## 접속

- `http://127.0.0.1:8766`처럼 루프백 IPv4에만 바인딩.
- 매 요청 `Authorization: Bearer <무작위 브리지 토큰>` 검사. API 제공자의 키를 브리지 토큰으로 쓰지 마세요.
- 브라우저 Origin 요청은 거절, CORS 허용 금지. 인증 실패 401.
- JSON UTF-8, 최대 응답 1MB. 어댑터에서 모델 입력용 상태를 180KB 미만으로 요약.
- 토큰·파일 경로·사용자 개인정보는 모델에 보낼 state에 포함하지 마세요.
- 대기 중에도 본체를 멈출 수 있지만 이미 보낸 행동의 취소는 보장하지 않습니다. 장기 큐 실행을 만들지 마세요.

## GET /v1/state

```json
{
  "schema_version": 1,
  "game_id": "my-game",
  "revision": "session-unique:turn-100:decision-4",
  "can_act": true,
  "terminal": false,
  "state": {"turn": 100, "resources": {"food": 5}, "rules": "게임 규칙 요약"},
  "actions": [
    {"id": "city-3:build:farm", "label": "도시 3에 농장 건설", "risk": "low"},
    {"id": "declare-war:country-2", "label": "국가 2에 선전포고", "risk": "high"}
  ]
}
```

필수 필드 형식은 위와 같습니다. 게임 고유의 state 필드는 자유롭게 중첩할 수 있습니다.
행동은 현재 합법적인 구체적 후보를 최대 200개 제공하세요. 모델은 후보의 id만 선택합니다.
좌표·유닛·대상·명령 인자는 어댑터가 id에 연결해 보관합니다. 모델이 임의 인자나 코드를 실행하지 못하게 합니다.
관측 전용은 `can_act:false`; 종료 시 `terminal:true`, `actions:[]`.
revision은 의사결정에 영향을 주는 상태가 바뀔 때 변경하고 세션 재시작/로드 시에도 반드시 바뀌어야 합니다.
state·actions·revision은 원자적으로 같은 시점의 스냅샷이어야 합니다.
기지/도시 이름이나 NPC 대사는 비신뢰 입력입니다. 상태에 프로그램 권한 지시를 포함하지 마세요.

## POST /v1/action

```json
{
  "schema_version": 1,
  "game_id": "my-game",
  "action_id": "city-3:build:farm",
  "expected_revision": "session-unique:turn-100:decision-4",
  "request_id": "고유 UUID",
  "expires_at": 1790000000.5
}
```

**어댑터 구현의 필수 책임:**

1. 인증·game_id·버전·명령 형식 검증.
2. request_id 중복이면 재실행하지 말고 기존 receipt 반환. 결과를 충분히 오래 유지.
3. 실행 시점에 expires_at(Unix 초)이 지나지 않았는지 확인. 본체는 4초 유효기간을 설정.
4. 게임 스레드에서 revision 비교와 행동 실행을 원자적으로 수행.
5. 현재 후보의 합법성/대상/자원/위험도를 재확인. 변했으면 409로 거절.
6. 성공 응답은 접수만이 아니라 실제 게임 적용을 확인한 뒤 반환. 장기 예약/백그라운드 행동 금지.
7. 타임아웃 이후 동일 요청이 재실행되지 않도록 멱등성 유지. 파일 저장/게임 종료/결제/외부 메시지는 후보에 넣지 말 것.

응답:
```json
{"request_id":"동일 UUID", "accepted":true, "revision":"새 revision", "message":"농장 건설 적용 완료"}
```

실패/거절: 409 등의 비-2xx와 `{"accepted":false,"error":"stale state"}`.
본체는 매 행동 전 상태를 재조회하지만 최종 경합 방지는 어댑터가 반드시 구현해야 합니다.
타임아웃/알 수 없는 실행 결과에는 본체가 자동 재시도하지 않고 중지합니다.
연결 모듈 자체가 위험도를 잘못 표시하면 본체가 모든 위험을 알아낼 수 없습니다. 실제 게임에서 수동 확인으로 먼저 검증하세요.

## 포함된 샘플 시험

VS Code의 별도 터미널에서:
```powershell
python .\examples\demo_bridge.py
```
콘솔에 나온 브리지 토큰을 복사합니다. 본체에서 새 프로필:

- 연결 종류: `bridge`
- 게임 ID: `demo-colony`
- 브리지 주소: `http://127.0.0.1:8766`
- 브리지 토큰: 방금 콘솔에 표시된 값
- 목표: `연구 10 달성`

프로필 저장 → 연결 → 상태 읽기로 외부 통신을 확인합니다.
bridge 프로필에서는 내장 규칙 엔진이 지원되지 않으므로 AI 분석에는 API가 필요합니다.
서버 재시작 때 토큰이 바뀝니다. 서버 종료는 서버 터미널의 Ctrl+C.
게임 2, 3을 연결할 때는 별도 포트와 고유 game_id로 브리지를 실행하고 프로필을 추가하세요.
본체는 선택한 프로필 한 개만 제어합니다. 여러 게임 동시 자동 제어 기능은 없습니다.
