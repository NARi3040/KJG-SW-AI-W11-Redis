# HTTP와 실행 제어 구현

## 책임

Gateway, Runtime, HTTP 변환, 서버 시작과 종료 조립을 맡는다. 외부 HTTP 요청을 검증된 작업으로 바꿔 단일 저장소 작업자에게 전달하고, 그 결과를 다시 HTTP 응답으로 변환한다. 복구 완료와 서비스 활성화를 구분해 수신 시점을 통제하고, 주기 작업과 정상, 실패 종료를 조정한다.

- Gateway는 공통 입력 검증, 연산 제출, Future 결과 대기를 맡는다. Store나 C 테이블에 직접 접근하지 않는다.
- Runtime만 큐, 실행 상태, 상태 잠금, 준비 Future, 종료 Event, 활성화 알림, 주기 기한을 소유한다. 작업자 하나가 테이블을 생성, 복구, 사용, 해제한다.
- HTTP 처리는 JSON 해석, 허용 필드 검사, 결과와 오류의 HTTP 변환을 맡는다. 저장소 상태를 보유하지 않는다.
- `__main__.py`는 데이터 경로 고정, Runtime과 Gateway와 Flask 앱과 Werkzeug 서버의 조립, 신호 전달, 수신 중단과 종료 코드를 맡는다.
- Store의 TTL, 만료 판정과 스냅샷 파일 형식은 [store-and-snapshots](store-and-snapshots.md)을, C 테이블과 연결 경계는 [c-hashtable-and-bindings](c-hashtable-and-bindings.md)을 따른다. 공통 타입, 예외, 입력 검증과 구현 뼈대는 [공통 계획](planning.md)을 따른다.

한 저장소 연산이 끝나기 전에 다른 요청이나 주기 작업을 끼워 넣지 않는다. Flask 요청 스레드는 여러 개일 수 있지만 Store와 C 테이블의 접근자는 작업자 하나다. GIL은 이 보장의 근거가 아니다.

## 대상 파일

| 파일 | 구현할 내용 | 주 담당과 함께 확인할 담당 |
| --- | --- | --- |
| `mini_redis/gateway.py` | Gateway 공개 메서드, 공통 입력 검증, runtime 제출과 Future 결과 대기. 큐, 상태, 잠금은 보유하지 않음 | 담당 3, 담당 2와 입력 검증, 내부 결과 전달 확인 |
| `mini_redis/runtime.py` | 내부 작업 제출, 큐, 상태, 작업자, 복구 데이터의 준비용 테이블 삽입과 게시, 활성화, 주기 기한, Future, 실패, 종료 처리 | 담당 3, 담당 1, 2와 접근 소유권, 복구, 실패 상태 확인 |
| `mini_redis/http.py` | Flask 앱과 세 경로, JSON 입력과 HTTP 오류, 결과 변환. Gateway를 주입받아 실제 작업자 없이도 응답 매핑 시험 가능 | 담당 3, 담당 2와 입력, HTTP 변환 확인 |
| `mini_redis/__main__.py` | `--data-dir` 해석, 경로 고정, 서버와 작업자 준비, 수신, 종료 조정, 신호 Event와 종료 코드 | 담당 3, 담당 1, 2와 준비, 수신, 신호, 해제 확인 |
| `tests/test_gateway.py` | 공통 검증 오류의 미제출, 입력 전달, Future 결과, 예외 전달, 수락 전 거부 전달 | 담당 3, 담당 2와 확인 |
| `tests/test_http.py` | H1~H12의 요청, 응답, 오류 변환과 미제출 확인 | 담당 3, 담당 2와 확인 |
| `tests/test_runtime.py` | S-T11, S-T12, S-T14, R1~R6의 기한, FIFO, 포화, Future, 소유권, 수명 | 담당 3, 담당 1, 2와 확인 |
| `tests/test_integration.py` | S-T1, S-T6~S-T14, R4, R5, D1, D2 중 실제 C, HTTP, 작업자 연결과 프로세스 수명 부분 | 담당 3이 실행 수명 시험을 조정, 담당 1은 C 실패, ABI, 담당 2는 파일, 복구, 무효화 확인 |

`contracts.py`는 공통 소유 파일이며 이 문서는 그 정의를 복제하지 않는다. `tests/conftest.py`의 가짜 epoch, monotonic 시계와 작업자 정리 fixture는 공통 준비를 따르고, Runtime 시험에 필요한 종료 fixture는 이 구현 담당이 제공한다.

## 상세 계약

아래는 구현 코드가 아니라 확정된 호출과 상태 명세다. 별도 설명이 없으면 정상 시작, 정상 실행, 정상 종료의 기존 동작을 유지한다. 공통 입력 검증, `MISSING`, `Operation`, `Entry`, 공유 예외와 Store, Gateway 공개 시그니처는 [공통 계획](planning.md)을 따른다.

### 시작과 활성화 의존성

시작과 수신 사이에 복구 완료와 서비스 활성화를 구분한다. `Runtime.start()`는 작업자 하나를 시작해 테이블 생성, 스냅샷 검증, 복구만 수행하게 한다. `Runtime.wait_ready()`가 반환해도 상태는 `STARTING`이며 `RUNNING`이 아니다. 준비 Future의 완료는 복구 성공을 뜻하고, 요청 처리 가능 상태를 뜻하지 않는다.

`Runtime.activate() -> None`은 복구가 끝난 뒤 서비스 실행을 시작하는 유일한 전환이다.

- 준비 Future가 성공했고 상태가 `STARTING`이며 종료가 요청되지 않았을 때만 활성화한다.
- 상태 잠금 안에서 활성화 순간의 monotonic 시각을 한 번 읽고 첫 청소 기한을 그 시각 +1초, 첫 스냅샷 기한을 그 시각 +60초로 정한다. 그 뒤 `RUNNING`으로 전환하고 작업자를 깨운다.
- 준비가 끝나지 않았으면 `RuntimeError`, 이미 종료 중이거나 종료됐거나 `FAILED`이면 `UnavailableError`를 전달한다. `STOPPING`이 활성화 경쟁에서 이기면 활성화는 실패하며 다시 `RUNNING`으로 열 수 없다.
- 이미 `RUNNING`인 중복 활성화는 아무 일도 하지 않고 기존 기한을 유지한다. 중복 호출로 기한을 다시 계산하거나 주기 작업을 앞당기지 않는다.
- 복구만 끝난 작업자는 요청을 실행하거나 주기 작업을 수행하지 않는다. 활성화 알림 또는 종료 Event를 표준 Event와 유한 대기로 기다리며 새 바쁜 루프나 새 큐를 만들지 않는다.
- 활성화 전에 걸린 지연은 따라잡지 않는다. 바인드에 60초 이상 걸려도 활성화 시점에 새 +1초와 +60초를 잡고, 밀린 청소나 스냅샷을 즉시 반복하지 않는다.
- 활성화 뒤에는 첫 요청보다 주기 저장이 먼저 실행될 수 있다. 이는 의도한 동작이다.
- 첫 성공 활성화 전에는 `request_stop(final_snapshot=True)`의 기본값이어도 최종 스냅샷을 쓰지 않는다. 활성화된 적이 없으면 서비스가 변경한 상태도 없다.
- 시작 중 종료가 요청되어도 준비 Future는 반드시 완료된다. 대기자를 영원히 붙잡지 않는다.
- 활성화 자체가 실패하면 컨트롤러는 열어 둔 수신 소켓과 작업자를 정리하고 수신 없이 종료한다.

컨트롤러 조립 순서는 다음과 같다.

```text
Runtime 생성 → Runtime.start → Runtime.wait_ready
→ Gateway 생성 → create_app → make_server로 소켓 바인드(serve_forever 아님)
→ Runtime.activate() → 수신 시작
```

소켓 바인드에 실패하면 `request_stop(final_snapshot=False)`와 `join`으로 작업자를 정리하고, 열린 소켓이 있으면 닫은 뒤 종료 코드 1을 반환한다. 이 경로는 복구 후 원본 스냅샷을 보존하며 스냅샷 정리나 서비스 상태 변경을 하지 않는다. 데이터 디렉터리 준비와 복구된 메모리 구성은 여기서 말하는 데이터 변경이 아니다.

활성화 성공 뒤 수신 시작 전에 종료 신호나 STOPPING, FAILED를 확인하면 수신을 시작하지 않고 해당 종료 경로로 합류한다. 수신 시작과 종료가 겹쳐 HTTP 처리가 진입하더라도 `runtime.submit`이 STOPPING, FAILED의 새 작업을 거부하며 컨트롤러는 수신을 중단한다.

정상 중단은 `request_stop()` 뒤 수신 중단, 수락 작업 완료, 최종 스냅샷 한 번, C 자원 해제, `join` 순서다. `FAILED`는 최종 스냅샷을 생략한다. 종료 코드 1은 컨트롤러가 결정하며 작업자 정리 성공 코드 0으로 덮지 않는다.

### 상태, 큐와 오류 전달

상태는 `STARTING → RUNNING → STOPPING → STOPPED`, 실패 경로는 `FAILED`다. 시작, 종료의 중복 실행으로 테이블이나 서버가 두 개 생기지 않게 한다.

대기 큐는 표준 `queue.Queue(maxsize=64)`이며 실행 중 작업 하나는 별도다. 제출은 짧은 상태 잠금 안에서 `RUNNING` 확인과 `put_nowait`를 함께 수행한다. 가득 차면 수락하지 않고 `BusyError`를 전달한다. `RUNNING`이 아니면 `UnavailableError`를 즉시 던지고 큐에 넣지 않는다. 같은 잠금으로 C 연산을 실행하지 않는다.

작업에는 `set/get/delete` 연산 이름, 검증된 불변 입력, `concurrent.futures.Future` 하나를 넣는다. 작업자는 `set_result`나 `set_exception`으로 완료하며 Gateway는 `Future.result()`로 결과를 받는다. 수락 기준은 큐에 들어간 시점이고 FIFO는 수락된 작업에 적용한다. 네트워크 도착 순서와 응답 수신 순서는 보장하지 않는다.

서버 측 실행, 결과 대기 상한은 없다. 클라이언트 단절과 자체 타임아웃으로 큐 작업을 취소하지 않는다. 포화, 시작 전, 종료 중 거부는 해당 연산 미실행이다. 수락 후 응답을 잃으면 실행 여부가 불확실하며, 서버와 소비자 예시는 자동 재시도하지 않는다. TTL 저장 재시도는 만료 시각도 바꾸므로 멱등성을 가정하지 않는다.

| 실패 | 처리 |
| --- | --- |
| 요청의 `ValueError`, `MemoryError` | 해당 Future에 전달하고 작업자 계속 실행 |
| 스냅샷의 파일, 직렬화, 할당 실패 | 기존 파일과 메모리 유지, 기록 후 다음 정기 시도에서 재시도 |
| 복구 가능한 청소 오류 | 기록하고 다음 청소 주기에 재시도 |
| 예상하지 못한 요청 실행 예외, `TableCorruptionError`, 작업자 비정상 이탈 | FAILED 전환, 현재, 대기 Future를 `WorkerFailedError`로 완료, 새 제출 차단, 서버 중단 |

작업자 최외곽에서 실패 전환과 대기 Future 해제를 보장한다. `FAILED`에서는 이미 반영된 변경을 롤백하지 않고 최종 스냅샷으로 기존 파일을 덮어쓰지 않는다. 로그에 키, 값, 요청 본문 전체를 남기지 않는다. 네이티브 프로세스 종료나 오류 보고조차 불가능한 메모리 고갈에서는 응답을 보장할 수 없다.

`Runtime.submit(operation, *, key=MISSING, value=MISSING, ttl=MISSING) -> Future`는 Gateway만 사용하는 내부 제출 경계다. operation이 세 연산 밖이거나 get/delete에 value, ttl을 보내는 내부 호출 버그는 `RuntimeError`로 거부한다. 인자 형태를 검사하는 동안 저장소를 읽거나 바꾸지 않는다. 반환 Future를 호출자가 cancel하는 인터페이스는 제공하지 않는다.

### Gateway 계약

| 생성자, 메서드 | 입력, 반환과 호출자 |
| --- | --- |
| `Gateway(runtime: Runtime)` | 컨트롤러가 만든 Runtime 참조를 받음. 다른 작업자를 생성하지 않음 |
| `Gateway.set` | validate_set → runtime.submit("set", ...) → Future.result |
| `Gateway.get` | validate_key → runtime.submit("get", key=...) → Future.result |
| `Gateway.delete` | validate_key → runtime.submit("delete", key=...) → Future.result |

Runtime의 객체 참조를 보유하는 것과 실행 상태, 큐를 보유하는 것은 다르다. Gateway는 실행 상태를 복사하거나 별도 잠금을 만들지 않는다. Gateway는 runtime의 내부 제출 메서드가 반환한 Future에 `result()`를 호출한다. 수락 전 거부 예외와 수락 후 실행 예외를 정상 결과로 바꾸지 않고 전달한다.

입력 오류, 수락 전 `BusyError`, `UnavailableError`, 수락 후 Future 예외를 그대로 전달한다. `WorkerFailedError`는 `UnavailableError`의 하위 예외로 HTTP 503/unavailable에 연결한다. Gateway는 `set/get/delete`만 제출하고 임의 callable을 받지 않는다. Gateway는 Store나 C 테이블을 import하거나 직접 호출하지 않는다.

### HTTP 계약

세 연산은 모두 POST와 JSON 객체 본문을 사용한다. 빈 키, NUL, 슬래시를 URL 경로 인코딩으로 전달하지 않는다.

| 요청 | 성공 상태와 본문 |
| --- | --- |
| `POST /set`, `{"key":"k","value":"v","ttl":10}` | `200`, `{"result":true}` |
| `POST /set`, `{"key":"k","value":"new"}` | `200`, `{"result":true}`. 기존 TTL 제거 |
| `POST /get`, `{"key":"k"}` | `200`, `{"result":"new"}` |
| `POST /get`, `{"key":"missing"}` | `200`, `{"result":null}` |
| 빈 키에 빈 값 저장 후 조회 | 각각 `200`과 `{"result":true}`, `200`과 `{"result":""}` |
| `POST /delete`, `{"key":"k"}` | `200`, `{"result":true}`. 없거나 만료됐으면 `{"result":false}` |

| 오류 조건 | 상태 | 본문 |
| --- | --- | --- |
| 잘못된 JSON, 객체 아닌 본문, 누락, 타입, TTL 오류, 알 수 없는 필드, 중복 필드 | 400 | `{"error":{"code":"invalid_input"}}` |
| JSON이 아닌 Content-Type | 415 | `{"error":{"code":"unsupported_media_type"}}` |
| 모르는 경로 | 404 | `{"error":{"code":"not_found"}}` |
| 허용하지 않는 메서드 | 405 | `{"error":{"code":"method_not_allowed"}}` |
| 큐 포화 | 503 | `{"error":{"code":"busy"}}` |
| 시작 미완료, 종료 중, FAILED | 503 | `{"error":{"code":"unavailable"}}` |
| 저장소 할당 실패 | 500 | `{"error":{"code":"memory_error"}}` |
| 그 밖의 내부 실패 | 500 | `{"error":{"code":"internal_error"}}` |

`Content-Type: application/json`과 UTF-8 JSON을 요구한다. set은 `key/value/ttl`, get/delete는 `key`만 허용한다. 중복 필드와 `NaN`, `Infinity`는 파싱 단계에서 거부한다. JSON 파싱 예외와 알려진 입력 검증 오류를 구분해 처리하고, 내부 `TypeError`나 `ValueError` 전체를 일괄 400으로 바꾸지 않는다.

응답은 `application/json`과 ASCII 이스케이프 출력을 사용해 단독 surrogate도 전송 가능하게 한다. 다만 HTTP JSON 파서는 surrogate pair를 한 보충 문자로 합칠 수 있다. 내부 Python 문자열과 스냅샷의 완전한 왕복 보장과 달리, HTTP에서 두 표현을 구별하는 별도 Base64 모드는 제공하지 않는다.

`MAX_CONTENT_LENGTH=None`을 명시하고 별도 고정 본문 상한이나 413 계약은 두지 않는다. 오류 응답에 예외 원문, 경로, 키와 값을 노출하지 않는다. 503은 자동 재시도 지시가 아니며 FAILED 전에 수락된 변경은 일부 실행됐을 수 있다.

`create_app(gateway: Gateway) -> Flask`는 컨트롤러가 Gateway를 주입해 Flask 앱을 만든다. 소켓, 작업자, C 테이블은 만들지 않는다. `handle_set() -> Response`, `handle_get() -> Response`, `handle_delete() -> Response`는 Flask가 호출하는 세 요청 함수다. request를 읽고 전달받은 Gateway를 호출해 JSON 응답을 반환하며 직접 호출용 저장소 API가 아니다.

http.py는 입력, 라우팅, 서버 오류를 위 계약대로 변환한다. JSON 파싱, 알려진 검증 오류와 내부 예외를 구별하며, 준비 전이나 종료 후의 요청 수락 판단은 `runtime.submit`에서 수행한다. HTTP 오류 응답과 JSON 해석의 내부 보조 함수 이름은 파일 안에서 정할 수 있다.

### 시작, 주기 작업과 종료

1. 필수 인자 `--data-dir`을 받아 시작 시 절대 경로로 고정하고 디렉터리를 준비한다. 작업자 안에서 테이블 생성과 스냅샷 검증, 복구를 마친다. 준비 Future가 완료되기 전에는 HTTP 요청을 받지 않는다.
2. 성공한 복구는 수신 시작이 아니다. 컨트롤러는 준비 완료 후 수신 소켓을 바인드하고, `Runtime.activate()`가 성공한 뒤에만 수신을 시작한다. 첫 청소 기한은 활성화 순간 +1초, 첫 스냅샷 기한은 활성화 순간 +60초다. 바인드 지연이나 복구 완료 시각을 기준으로 기한을 잡지 않는다.
3. 작업자는 활성화 뒤 매 요청 사이 기한을 확인한다. 둘 다 도래했으면 청소 한 번, 스냅샷 한 번을 실행한 뒤 대기 요청 하나를 처리한다. 밀린 횟수만큼 반복 실행하지 않는다.
4. 청소는 시작 시 epoch 시각 하나로 전체 `O(m+n)` 순회를 한다. 다음 청소 기한은 완료 후 1초다. 긴 작업 때문에 늦어질 수 있으며 1초 내 메모리 회수는 보장하지 않는다. 접근 시 만료 검사는 계속 유지한다.
5. 스냅샷의 다음 기한은 각 저장 시도 완료 후 60초다. 실패한 시도도 완료 시점에서 다음 기한을 계산한다. 두 반복 기한 모두 `time.monotonic()`을 사용한다.
6. 큐가 비면 가장 가까운 기한까지 `Queue.get(timeout=...)`로 대기하되, 유휴 종료 감지를 위해 최대 0.1초로 제한한다. 요청이 없거나 계속 들어와도 주기 기한을 확인한다.
7. 첫 Ctrl+C나 SIGTERM의 신호 처리기는 컨트롤러용 Event만 알린다. 컨트롤러가 `request_stop()`을 호출해 STOPPING 전환과 Runtime 종료 Event를 요청하고 새 제출을 막는다. 신호 처리기에서 큐 비우기, 디스크 기록, 동일 서버 스레드의 `shutdown()`을 실행하지 않는다.
8. 컨트롤러는 수신 중단, 수락 작업 완료 대기, 최종 스냅샷 한 번, C 자원 해제, 작업자 join을 조정한다. STOPPING에서는 정기 작업을 새로 시작하지 않는다. 큐가 비면 최종 저장을 진행한다. 첫 활성화 전 종료는 최종 저장을 생략한다.
9. 작업자는 daemon이 아니며, 종료 통지는 가득 찰 수 있는 큐가 아니라 별도 Event를 사용한다. 수락과 종료 전환을 원자적으로 처리해 종료 뒤 작업이 늦게 들어오지 않게 한다.
10. 정상 저장, 종료 성공은 종료 코드 0이다. 복구, 바인드, 활성화, 작업자 실패와 최종 저장 실패는 종료 코드 1이다. 최종 저장 실패도 이전 정식 파일을 보존하고 자원을 정리한다. FAILED에서는 최종 저장을 생략한다.

수락 작업과 파일 기록의 완료를 기다리므로 정상 종료 대기 상한은 없다. 두 번째 중단과 강제 종료에는 최종 저장을 보장하지 않는다.

### Runtime 계약과 작업자 소유권

| 생성자, 메서드 | 입력, 반환과 호출자 |
| --- | --- |
| `Runtime(data_dir: Path, *, clock: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic)` | 컨트롤러가 준비한 절대 데이터 경로를 받음. 생성만으로 스레드, 테이블, 파일 기록을 시작하지 않음 |
| `Runtime.start() -> None` | 컨트롤러가 작업자 하나를 시작. 작업자는 테이블 생성, 파일 검증, 복구만 수행. 중복 호출로 두 작업자를 만들지 않음 |
| `Runtime.wait_ready() -> None` | 준비 Future를 기다림. 복구 성공이면 반환하되 상태는 STARTING, 실패면 해당 시작 예외를 전달. 완료 전 서버 소켓을 열지 않음 |
| `Runtime.activate() -> None` | 복구 성공과 STARTING을 확인하고 활성화 시각 기준 기한을 잡은 뒤 RUNNING 전환, 작업자 깨우기. 준비 전 RuntimeError, 종료 또는 실패 뒤 UnavailableError, 중복 RUNNING은 기한을 유지하는 no-op |
| `Runtime.submit(operation: Operation, *, key=MISSING, value=MISSING, ttl=MISSING) -> Future` | Gateway만 사용하는 내부 제출 경계. 검증된 입력을 수락하고 그 작업의 Future 반환. Future에는 Store 반환값 또는 예외를 채움 |
| `Runtime.request_stop(*, final_snapshot: bool = True) -> None` | STOPPING 전환과 별도 종료 Event를 요청. 새 제출 차단 후 수락 작업 완료. False는 바인드 실패 정리처럼 최종 저장을 하지 않아야 하는 경로. 첫 활성화 전에는 True여도 최종 저장 생략 |
| `Runtime.join() -> int` | 작업자 종료, 자원 해제까지 대기. 작업자 정상 종료 0, 작업자 또는 최종 저장 실패 1 반환 |
| `Runtime.state` | 읽기 전용 상태 이름 `STARTING/RUNNING/STOPPING/STOPPED/FAILED`. Gateway가 이를 읽고 따로 수락 여부를 결정하지 않음 |

start는 작업자 하나를 생성해 시작하며 종료 뒤 재시작은 허용하지 않는다. start 전에 request_stop을 호출하면 작업자를 만들지 않고 STOPPED로 마친다. 이때 준비 Future는 `UnavailableError`로 완료하고 join은 0을 반환하며, 뒤늦은 start도 `UnavailableError`로 거부한다. 중복 종료 요청은 같은 종료 흐름을 유지한다. `final_snapshot=False`는 중복 요청으로 다시 True가 되지 않는다. FAILED는 이 인자와 무관하게 최종 저장 금지다. join은 종료 전에는 대기하며 정상 종료 시간을 제한하지 않는다. 바인드 실패 자체의 프로세스 종료 코드 1은 컨트롤러가 결정하며, 작업자 정리 성공 코드 0으로 덮지 않는다.

종료 신호 처리기는 Runtime의 잠금을 잡거나 join하지 않고 컨트롤러용 Event만 알린다. 컨트롤러가 request_stop, 서버 수신 중단과 join을 조정한다. 큐에서 꺼내는 반복 함수, 준비 Future와 작업 Future의 실제 완료 처리는 Runtime 내부 보조 메서드이며 다른 파일에서 직접 호출하지 않는다.

복구는 작업자가 `read_snapshot` 전체 검증 → clock 한 번 읽기 → 준비용 HashTable.put → Store 생성, 게시 순서로 수행한다. 외부 Store.set을 사용하지 않고 원래 E로 삽입한다. 실패 시 준비 테이블을 닫고 준비 Future를 실패시킨다. 최종 테이블 해제도 작업자가 수행한다. C 테이블 접근은 작업자 밖에서 하지 않는다. 활성화 전 대기와 종료 조정도 테이블을 읽거나 바꾸지 않는다.

`__main__.py`의 `main(argv: Sequence[str] | None = None) -> int`는 필수 데이터 경로 해석, 준비, Runtime, Gateway, Flask, Werkzeug 서버 조립, 신호, 수명 제어와 종료 코드를 맡는다. 모듈 실행 시 이 결과로 프로세스 종료한다. 조립 순서와 바인드 실패 정리는 [시작과 활성화 의존성](#시작과-활성화-의존성)을 따른다.

## 구현 순서

1. 공통 함수 뼈대와 import 경계를 먼저 맞춘다. `contracts.py`, Runtime, Gateway, http, `__main__`의 선언과 예외 상속을 확인하고, 미구현 본문은 `NotImplementedError`로 중단한다. 이 단계는 [공통 계획](planning.md)의 뼈대 절차를 따른다.
2. Gateway 대체 객체로 HTTP 요청, 응답, 오류 매핑을 먼저 완성한다. 검증 오류가 제출되지 않는지, 400, 404, 405, 415, 500, 503 변환이 계약과 맞는지 확인한다. 실제 작업자 통과로 표시하지 않는다.
3. Runtime의 상태 잠금, `submit`, 큐, Future 완료, `BusyError`, `UnavailableError`를 연결하고 Gateway를 그 경계에 붙인다. set/get/delete 입력 전달과 Future의 문자열, None, 불리언, 예외 전달을 확인한다.
4. 작업자 반복, monotonic 기한, 청소, 스냅샷 순서, 유휴 대기, 포화와 종료 경쟁을 연결한다. 기한은 활성화 뒤에만 작동하게 하고, STOPPING에서는 새 주기 작업을 시작하지 않는다.
5. 복구 게시, 수신 전 바인드, `activate`, 신호, 수락 작업 완료, 최종 저장과 종료 코드를 연결한다. 시작과 활성화 의존성의 순서를 그대로 지키고, 바인드 실패는 최종 저장 없이 정리한다.
6. `R4`의 지연 바인드, 긴 지연 뒤 활성화, 바인드 실패, 중복 활성화, 종료와 활성화 경쟁을 결정적 시계로 먼저 고정한 뒤 정상 시작, 종료와 실패 경로를 확인한다.

## 테스트와 기대 결과

이 문서는 HTTP와 실행 제어의 대표 사례만 상세히 둔다. 전체 테스트 색인과 공통 준비는 [공통 계획](planning.md)을 따른다. Store와 파일 중심 사례 T1~T16, T18, T21, S-T5~S-T10, S-T13, D1, D2는 [store-and-snapshots](store-and-snapshots.md#테스트와-기대-결과)에, C와 연결 코드 사례 T17, T19, T20, S-T2~S-T4, B1~B6, D3는 [c-hashtable-and-bindings](c-hashtable-and-bindings.md#테스트와-기대-결과)에 있다. 같은 사례를 여기서 다시 상세히 풀지 않는다.

Gateway 경계는 공통 검증 오류의 미제출, set/get/delete의 입력 전달, Future의 문자열, None, 불리언, 예외 전달, 수락 전 포화, 이용 불가 예외 전달을 확인한다. 별도 큐나 실행 상태 없이 runtime 제출 경계와 연결되는지도 확인한다.

### 준비와 공통 규칙

큐 포화는 작업 하나를 Event로 멈추고 대기 64개를 채운 뒤 다음 제출의 미실행 503을 확인한다. 테스트의 교착 감지 제한은 서버 API의 결과 대기 상한이 아니다. 정상 종료에서는 수락 작업 완료 뒤 최종 저장, FAILED에서는 모든 Future 해제와 최종 저장 생략을 확인한다.

HTTP용 대체 객체는 고정 결과와 예외를 반환하고 입력을 기록하는 Gateway 대체물이다. 별도 저장 엔진을 구현하지 않는다. 대체 객체 통과를 실제 C, 작업자 통합 통과로 표시하지 않는다.

Runtime 시험은 실제 1초, 60초를 기다리지 않고 주입한 monotonic 시계로 기한을 앞당긴다. 실제 작업자와 여러 HTTP 연결, 프로세스 수명은 통합 시험에서 확인한다.

### 실행과 수명

| 번호 | 입력과 실행 순서 | 기대 결과와 최종 상태 |
| --- | --- | --- |
| S-T1. 요청 간 상태 공유 | 서로 다른 HTTP 연결에서 저장 → 조회 → 삭제 → 조회 | 조회는 저장한 값, 삭제 결과 `True`, 이후 조회 `None`에 해당하는 JSON `null`. 요청마다 별도 저장소를 생성하지 않음 |
| S-T11. 긴 주기 작업 | 청소, 스냅샷 실행을 지연시키고 요청과 다음 기한을 겹치게 함 | 저장소 작업 중첩 없음. 대기 후 순차 실행, 밀린 주기 횟수만큼 폭주하지 않음. 두 기한 도래 시 청소 → 스냅샷 → 대기 요청 하나. 조회는 처리 시각으로 만료 판정 |
| S-T12. 시계 변경, 연속 저장 실패 | epoch 시각 전진, 후퇴와 monotonic 진행을 따로 제어. 스냅샷을 연속 실패시킴 | 전진 시 조기 만료, 후퇴 시 수명 연장 가능. 반복 기한은 epoch 변경과 독립. 마지막 성공 이후 변경의 유실 범위에 60초 상한을 가정하지 않음 |
| S-T14. 작업자 소유권과 수명 | 여러 HTTP 요청, 유휴 상태, 계속 들어오는 요청, 큐 포화, 작업자 실패, 연결 단절, 정상 종료를 각각 시험 | C 테이블 접근 스레드 하나. 유휴, 폭주 모두 기한 확인, 포화는 미실행 거부, 실패는 Future 해제, 단절은 수락 작업 취소 안 함, 정상 종료는 수락 작업 완료 후 최종 저장 |

### HTTP 입력과 오류 변환

각 오류 시험은 유효한 기존 값과 TTL을 먼저 저장하고 수행한다. 입력 단계에서 거부한 요청은 Gateway에 제출되지 않고 상태도 바뀌지 않아야 한다. JSON 본문의 필드 생략과 명시적 `null`은 별도 사례로 시험한다.

| 번호 | 요청 또는 주입 조건 | 기대 결과 |
| --- | --- | --- |
| H1. 정상 왕복 | `POST /set {"key":"k","value":"v","ttl":10}` → `/get {"key":"k"}` → `/delete {"key":"k"}` → 같은 삭제 반복 | 각각 `200`과 `{"result":true}`, `{"result":"v"}`, `{"result":true}`, `{"result":false}` |
| H2. 빈 값과 미적중 | `POST /set {"key":"","value":""}` → 빈 키 조회 → 없는 키 조회 | 저장 `200/true`, 조회 본문은 각각 `{"result":""}`, `{"result":null}`. 모두 200 |
| H3. TTL 생략과 null | TTL 있는 키에 TTL 생략 덮어쓰기. 별도로 `ttl:null`, 불리언, 실수, 문자열, 범위 밖 정수를 전송 | 생략은 200과 기존 TTL 제거. 잘못된 TTL은 `400/invalid_input`, 미제출, 상태 불변 |
| H4. 누락, 타입, 필드 | set의 key/value 누락, get/delete의 key 누락, 숫자, null 키나 값, 알 수 없는 필드, 중복 JSON 필드를 각각 전송 | `400`, `{"error":{"code":"invalid_input"}}`, 미제출 |
| H5. JSON 형식 | 잘린 JSON, 배열, 문자열, null 본문, `NaN`, `Infinity`와 `-Infinity`를 각각 전송 | `400/invalid_input`, 미제출 |
| H6. 미디어 타입 | JSON이 아닌 Content-Type으로 요청 | `415`, `{"error":{"code":"unsupported_media_type"}}`, 미제출 |
| H7. 라우팅 | 모르는 경로, 지원 경로에 허용하지 않는 메서드 | 각각 `404/not_found`, `405/method_not_allowed`. HTML 기본 오류 페이지가 아닌 JSON |
| H8. 포화, 이용 불가 | 큐 포화, STARTING, STOPPING, FAILED에서 요청 | 포화는 `503/busy`, 나머지는 `503/unavailable`. 새 연산 미실행, 자동 재시도 지시 없음 |
| H9. 메모리 실패 | Gateway 결과에 `MemoryError` 주입 | `500`, `{"error":{"code":"memory_error"}}`. 정상 없음이나 입력 오류로 숨기지 않음 |
| H10. 내부 실패 | HTTP 변환 경계에 내부 `TypeError`를 주입. 별도 시험에서 실제 작업자 요청 실행 중 예상하지 못한 예외 주입 | HTTP 내부 실패는 `500/internal_error`. 작업자 실패는 FAILED와 `WorkerFailedError` 전달에 따른 `503/unavailable`. 어느 경우도 400으로 숨기지 않고 원문, 경로, 키, 값 미노출 |
| H11. 문자열 표현 | NUL과 슬래시 키를 JSON 본문으로 왕복. 단독 surrogate 응답도 확인 | 경로 해석 없이 키 유지. 응답 Content-Type은 `application/json`, ASCII 이스케이프로 유효하게 출력 |
| H12. 본문 제한 설정 | Flask 설정 검사와 테스트 환경에서 감당 가능한 크기의 유효 본문 요청 | `MAX_CONTENT_LENGTH=None`. 임의 고정 크기 상한이나 별도 413 처리가 추가되지 않음. 자원 고갈을 실제로 일으키지 않음 |

표의 `400/invalid_input` 같은 축약은 상태 코드와 `{"error":{"code":"invalid_input"}}` 형태의 본문을 뜻한다. 결과, 예외를 고정 반환하는 Gateway 대체 객체를 사용하는 시험과 실제 작업자 통합 시험을 구분한다.

### 실행 제어와 종료

| 번호 | 입력과 실행 순서 | 기대 결과 |
| --- | --- | --- |
| R1. 수락과 종료 경쟁 | 제출과 STOPPING 전환을 경쟁시킴. 큐가 가득 찬 상태에서도 종료 Event 전달 | 전환 전 수락한 작업은 완료, 전환 후 제출은 미실행 거부. 큐 포화가 종료 통지를 막지 않음 |
| R2. 실패 Future 해제 | 작업 하나 실행 중 예상하지 못한 예외, 테이블 손상, 작업자 이탈을 각각 주입하고 대기 작업도 둠 | FAILED, 현재, 대기 Future 모두 `WorkerFailedError`, 새 제출 차단, 서버 중단, 최종 스냅샷 생략. 기존 파일 보존 |
| R3. 요청 단절 | 수락된 저장을 멈춘 동안 클라이언트 연결을 끊은 뒤 작업 재개 | 작업 취소나 자동 재시도 없이 한 번 실행. 연결 단절이 미실행, 롤백 보장으로 해석되지 않음 |
| R4. 시작, 바인드와 활성화 | 복구 지연, 복구 실패, 수신 전 바인드 지연, 소켓 바인드 실패, 시작, 종료, 활성화 중복 호출, 종료와 활성화 경쟁을 각각 시험 | 준비 전 수신 없음. `wait_ready` 반환 뒤에도 STARTING. 복구 실패는 시작 실패. 바인드, 활성화 실패는 데이터 변경 없이 자원 정리. 테이블, 서버 중복 생성 없음. 아래 세부 조건을 함께 만족 |
| R5. 종료 코드 | 정상 종료, 최종 저장 실패, 복구, 바인드, 작업자 실패를 각각 실행 | 정상 저장, 종료는 0, 실패는 1. 최종 저장 실패도 이전 파일 보존과 C 해제, join. 정상 종료 시간 상한을 가정하지 않음 |
| R6. 복구 가능한 오류 | 요청 `ValueError`, `MemoryError`, 복구 가능한 청소 실패, 스냅샷 기록 실패 주입 후 정상 요청 | 요청 오류는 해당 Future만 실패하고 작업자 계속 실행. 청소, 저장은 완료 후 다음 기한에 재시도. 상태 신뢰 불가 오류는 계속 실행하지 않음 |

`R4`는 다음을 포함한다.

- 복구를 지연시키고 준비 Future가 끝나기 전에 수신하지 않는지, 복구 실패가 시작 실패와 종료 코드 1로 이어지는지 확인한다.
- 준비 완료 뒤 바인드를 60초 넘게 지연시키고 monotonic 시계를 진행시킨다. 상태가 STARTING인 동안 제출이 `UnavailableError`로 거부되고, 청소, 스냅샷이 실행되지 않으며, 정리나 최종 저장도 없는지 확인한다.
- 그 뒤 `activate`하면 기한이 새 활성화 시각 +1초와 +60초로 잡히고, 밀린 청소, 스냅샷을 따라잡아 즉시 실행하지 않는지 확인한다.
- 바인드 실패에서 원본 스냅샷 파일 바이트가 그대로 유지되고, 작업자가 C 자원을 해제하고 join되며, 컨트롤러가 종료 코드 1을 반환하는지 확인한다.
- 중복 `activate`가 기한을 다시 계산하지 않고, 종료와 활성화가 경쟁할 때 STOPPING이 이기면 활성화가 실패하고 다시 RUNNING으로 열리지 않는지 확인한다.
- 활성화 성공 직후 수신 전에 종료 신호가 관찰되면 수신 없이 종료하고, 수신 시작과 종료가 겹치더라도 STOPPING 이후 새 저장소 작업은 수락하지 않는지 확인한다.
- start 전 request_stop은 준비 Future를 `UnavailableError`로 완료하고 join은 0을 반환하며, 이후 start가 작업자를 만들지 않는지 확인한다.
- 시작, 종료의 중복 호출에도 테이블과 서버가 하나만 생성되는지 확인한다.

## 연결 기준

- 공통 타입, `MISSING`, `Operation`, `Entry`, `BusyError`, `UnavailableError`, `WorkerFailedError`, `TableCorruptionError`, 공통 검증은 [공통 계획](planning.md)을 단일 출처로 삼는다. HTTP와 Gateway는 Store 구현을 import해 검증을 재사용하지 않는다.
- Gateway는 Runtime 객체 참조만 받고 실행 상태, 큐, 잠금을 복제하지 않는다. HTTP 앱은 Gateway만 받고 소켓, 작업자, C 테이블을 만들지 않는다. `__main__.py`만 Runtime, Gateway, Flask 앱, Werkzeug 서버를 조립한다.
- Runtime은 gateway와 http를 import하지 않는다. Store의 TTL, 만료 계약은 [store-and-snapshots](store-and-snapshots.md), HashTable과 C 오류 변환은 [c-hashtable-and-bindings](c-hashtable-and-bindings.md)을 따른다.
- 복구와 최종 저장의 파일 경계는 저장소, 스냅샷 구현이 소유한다. 이 문서는 작업자 안에서의 호출 순서, 준비용 테이블 게시, 활성화 전후의 수명만 정한다.
- 시작과 수신 시점을 다루는 다른 문서와 시험은 [시작과 활성화 의존성](#시작과-활성화-의존성)을 기준으로 링크한다. `wait_ready` 완료를 RUNNING으로 해석하거나 복구 완료 시각으로 주기 기한을 잡지 않는다.
- 입력, 반환, 예외, HTTP 상태, 본문, 큐, 수명 규칙을 바꾸면 이 문서와 영향을 받는 호출부, 테스트, README를 같은 변경 단위에 포함한다. 공유 계약 변경은 공통 문서와 저장소, C 문서의 확인 담당까지 함께 맞춘다.
