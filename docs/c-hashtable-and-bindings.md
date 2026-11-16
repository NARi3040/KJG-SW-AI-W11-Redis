# C 테이블과 Python 연결 구현

[핵심 요약과 호출 흐름](overview.md)

[공통 계획](planning.md)에서 나눈 구현 문서다. 공통 입력 계약, 전체 파일 소유권 표, 실행 환경, 테스트 준비는 [공통 계획](planning.md)에 두고, 이 문서는 C 해시 테이블과 ctypes 연결, 시험 훅, C 빌드, 원본 API와 성능 비교 도구를 다룬다. 저장소 규칙과 파일 처리는 [store-and-snapshots](store-and-snapshots.md), HTTP와 실행 제어는 [http-and-runtime](http-and-runtime.md)을 따른다.

## 책임

- C 헤더와 해시 테이블: djb2, 노드와 버킷, 탐색, 삽입, 삭제, 확장, 독립 복사와 해제, 시험 전용 할당 실패와 dump.
- ctypes 연결과 메모리 수명: 라이브러리 로딩, 타입 선언, `HashTable` 메서드, C 오류 변환, 포인터 수명과 닫힘 검사.
- C 빌드: Windows와 Linux에서 로컬 공유 라이브러리 산출과 시험 빌드 전환.
- 원본 API와 성능 비교 도구: `demo/origin.py`, `demo/benchmark.py`와 측정 조건 기록.
- Store가 사용할 `HashTable`, 시험 훅과 ABI 확인 프로그램, 로컬 빌드 방법, 원본 API와 측정 결과를 다른 담당에게 제공한다.

C 해시 테이블은 노드, 문자열 복사본, 버킷 배열, 물리 항목 수를 보유한다. 탐색, 삽입, 제거와 cutoff 보조 연산을 맡고 현재 시각 읽기, TTL 계산, HTTP 처리, 스케줄링은 하지 않는다. TTL 정책과 만료 판정 시각은 Store가 정하고 C는 전달받은 값만 사용한다.

## 대상 파일

아래는 이 담당이 주로 수정하는 파일이다. 전체 파일 목록과 다른 담당의 파일은 [공통 계획](planning.md)의 5.2절에 있다.

| 파일 | 구현할 내용 | 함께 확인할 담당 |
| --- | --- | --- |
| `src/hashtable.h` | C 함수 원형, 출력 구조체, 오류 코드와 함수 내보내기. C 구현과 ctypes가 맞춰야 할 경계를 한곳에 고정 | 2. 값, 만료, 오류, 출력 계약 확인 |
| `src/hashtable.c` | djb2, 노드, 버킷, 탐색, 삽입, 삭제, 확장, 독립 복사와 해제, 시험 전용 할당 실패, dump. TTL 시계나 HTTP 정책은 넣지 않음 | 2. 실패 시 값, TTL, 물리 상태 보존 확인 |
| `mini_redis/hashtable.py` | DLL/so 로딩, ctypes 선언, `HashTable` 메서드, C 오류 변환과 포인터 수명, 닫힘 검사. C 주소를 이 파일 밖으로 노출하지 않음 | 2. Store 호출 순서와 독립 복사 확인 |
| `tests/test_hashtable.py` | T17, T19, T20, S-T2~S-T4, S-T13, B1~B6. 충돌, 확장, 할당 실패, 복사 수명, 닫힘 이후 접근, ABI | 2 |
| `tests/test_integration_c.py` | S-T4와 C 실패·복사 수명의 실제 Store 연결 | 2 |
| `tests/abi_probe.c` | C 헤더의 `sizeof`와 `offsetof`를 출력하는 시험용 프로그램. B2에서 ctypes 결과와 대조하며 운영 라이브러리 API는 늘리지 않음 | 2 |
| `pyproject.toml` | 패키지 설치 메타데이터, 의존성 범위와 `test` extra. pip 설치가 C 컴파일도 수행한다고 가정하지 않음 | 2, 3. 의존성과 설치, 실행 확인 |
| `.gitignore` | 가상환경, Python 캐시, 로컬 DLL/so, C 빌드 산출물과 실행 데이터 제외. 바이너리와 실제 스냅샷을 소스로 공유하지 않음 | 2, 3. 데이터, 빌드 산출물 제외 확인 |
| `mini_redis/__init__.py` | Python 패키지 인식만 담당. `__init__.py` 명세는 [공통 계획](planning.md)의 5.4절을 따름 | 3. import 시 실행 부작용 없음 확인 |
| `demo/origin.py` | 결과를 바꾸고 호출 횟수를 확인할 작은 로컬 원본 API. 원본 서비스 역할을 캐시 서버와 구분 | 2. 결과 변경과 호출 횟수 관찰 확인 |
| `demo/benchmark.py` | 같은 원본 작업의 캐시 사용, 미사용 비교와 측정 조건, 응답 시간, 원본 호출 횟수 기록 | 2, 3. 동일 원본 작업, 결과와 측정 조건 확인 |
| `README.md` | 설치, C 빌드, 시험 빌드 전환, 실행, 종료, 테스트, 시연, 측정 명령과 실제 검증한 환경 버전 기록 | 2는 데이터, 파일, 소비자, 3은 서버, 종료, 실행 시험 명령 작성, 확인 |

`_hashtable.dll` 또는 `_hashtable.so`는 `mini_redis/`에 생성하는 로컬 빌드 산출물이며 직접 작성할 소스 파일이 아니다. `snapshot.json`과 임시 파일은 지정한 데이터 디렉터리에 생성하고 소스 관리에서 제외한다. 바이너리를 저장소에 공유하지 않고 각자 소스에서 빌드한다.

`demo/client.py`는 저장소와 스냅샷 담당이 작성한다. 원본 호출 횟수와 결과 변경은 이 담당이 제공하는 `demo/origin.py`로 확인한다.

## 상세 계약

### 테이블 구조와 확장 규칙

C 노드는 키, 값 각각의 바이트 포인터와 `size_t` 길이, `has_expiry`, `double expires_at`, 다음 노드 포인터를 함께 보유한다. 테이블은 버킷 배열, 버킷 수 `m`, 물리 항목 수 `n`을 보유한다. 문자열은 독립 복사하며 `strlen`이나 `strcmp`에 의존하지 않고 길이와 전체 바이트를 비교한다.

| 항목 | 확정 규칙 |
| --- | --- |
| 해시 | 64비트 부호 없는 djb2. `h=5381`에서 각 부호 없는 바이트 `b`에 `h=h*33+b` 적용. `2^64` 모듈러 연산 |
| 버킷과 충돌 | 초기 16버킷, `h % m`, 단일 연결 리스트 체이닝. 같은 해시나 버킷이어도 전체 키를 비교 |
| 확장 경계 | 신규 물리 노드 삽입 전 `n >= m - m/4`이면 `2m`으로 확장. m은 16부터 두 배씩 늘므로 `4*(n+1) > 3*m`과 동치이며 C 정수 나눗셈과 곱셈 오버플로를 피함. 16버킷에서 12개까지 유지, 13번째 신규 키 전에 32버킷 |
| 덮어쓰기 | 기존 물리 노드면 만료 여부와 무관하게 개수 증가와 확장 없음 |
| 만료 항목 | 실제 제거 전까지 `n`에 포함. 확장 중 전체 청소를 끼워 넣지 않음 |
| 용량 | 키, 값, 항목 수에 고정 상한 없음. 자동 축출과 자동 축소 없음 |

저장에 필요한 노드, 문자열, 새 버킷 배열을 먼저 준비한다. 버킷 배열과 출력 배열은 `calloc(count, sizeof(*array))`로 할당해 바이트 수 곱셈을 직접 하지 않는다. 두 배 확장 전 `m > SIZE_MAX/2`만 검사해 `2*m`의 래핑을 막는다. 부하율은 위의 덧셈, 곱셈 없는 식을 쓴다. 표현 불가능과 할당 불가능은 `MemoryError`이며, 이 단계까지 실패하면 준비 자원만 해제하고 기존 키, 값, TTL, 노드, 항목 수, 버킷 수, 테이블을 유지한다.

준비가 끝난 뒤에는 추가 할당, Python 호출, 실패 가능한 콜백 없이 커밋한다. 재해싱은 기존 노드를 새 버킷으로 재연결하고 새 노드를 연결한 뒤 배열과 개수를 게시한다. 옛 배열이나 교체된 노드는 이후 해제한다. 커밋 도중 다른 저장소 작업에 제어를 넘기지 않는다.

순간 메모리는 기존 노드와 문자열, 기존 버킷 배열과 새 버킷 배열, 이번 저장용 노드와 문자열을 포함한다. 재해싱을 위해 기존 노드와 값을 모두 두 벌 복사하지 않는다. 메모리 훼손과 커밋 불변식 위반까지 복구한다는 보장은 하지 않는다.

### ctypes 연결 클래스

얇은 Python `HashTable` 연결 클래스가 C 공유 라이브러리를 감싼다. Windows에서는 `_hashtable.dll`, Linux에서는 `_hashtable.so`를 연결 코드 파일 위치 기준 절대 경로로 `ctypes.CDLL`에 전달한다. 별도 프로세스나 CPython 확장 타입을 만들지 않는다.

`cutoff`는 Python이 한 번 읽어 전달한 판정 시각이다. C는 시계를 읽거나 TTL을 계산하지 않는다. 연결 클래스는 닫힘 상태를 검사하며, 정상 종료를 소멸자에 맡기지 않는다.

| 메서드 | 입력과 반환 | 역할과 경계 |
| --- | --- | --- |
| `HashTable()` | 없음 | 생성한 스레드에서 초기 16버킷 C 테이블 생성. 라이브러리 로딩도 첫 생성 시 수행하며 모듈 import 때 DLL을 요구하지 않음 |
| `put(key: bytes, value: bytes, expires_at: float \| None) -> None` | 바이트 키, 값과 절대 만료 시각 또는 무기한 | 키, 값, 만료 정보를 독립 복사하고 함께 교체. 반환 `None` |
| `is_live(key: bytes, cutoff: float) -> bool` | 바이트 키와 판정 시각 | 변경, 할당 없이 물리 항목의 유효 여부 반환 |
| `get(key: bytes) -> tuple[bytes, float \| None] \| None` | 바이트 키 | 물리 항목의 값, 절대 시각 독립 복사본 또는 `None`. 만료 정리 없음 |
| `remove(key: bytes) -> bool` | 바이트 키 | 물리 항목 제거 여부 반환. 추가 할당 없음. Store의 논리 삭제 결과와 구별 |
| `remove_expired(cutoff: float) -> None` | 판정 시각 | `E <= cutoff`인 만료 노드 전체 제거. 반환 `None` |
| `copy_live(cutoff: float) -> list[tuple[bytes, bytes, float \| None]]` | 판정 시각 | 유효 항목의 독립적인 목록. 원본 변경 없음 |
| `close() -> None` | 없음 | 전체 자원을 명시적으로 해제. 중복 호출은 안전하고, 이후 데이터 메서드는 `RuntimeError` |
| `_test_fail_alloc_after(successful_allocations: int) -> None` | 시험 빌드 전용 | -1은 해제, 0은 다음 한 번 실패, N은 N번 성공 뒤 한 번 실패 |
| `_test_dump() -> tuple[list[tuple[bytes, bytes, float \| None]], int, int]` | 시험 빌드 전용 | 모든 물리 항목의 독립 복사본, 물리 개수 n, 버킷 수 m 순서 |

할당, 표현 실패는 `MemoryError`, 닫힘과 잘못된 C 호출은 `RuntimeError`다. 작업자 단독 접근 전제는 필요하면 생성 스레드 ID를 비교하는 assert 하나로 확인하며 별도 거부 API와 다른 스레드 접근 시험은 두지 않는다. DLL 없음, 로딩 실패는 `OSError` 등 원래 로딩 예외로 시작을 실패시킨다. 시험 함수가 없으면 시험 실패로 알리고 정상 결과로 대체하지 않는다.

### C 함수 원형과 출력 구조체

`ht_*` 함수는 Windows에서 명시적으로 내보내는 cdecl 함수를 사용한다. `HT_BUILD_DLL`을 DLL 빌드에 정의하며, 구조체는 기본 정렬을 사용한다. 별도 packing을 적용하지 않는다.

```c
typedef struct HtTable HtTable;
typedef struct {
  unsigned char *data;
  size_t length;
  int has_expiry;
  double expires_at;
} HtValue;
typedef struct {
  unsigned char *key;
  size_t key_length;
  HtValue value;
} HtEntry;
typedef struct {
  HtEntry *entries;
  size_t count;
} HtSnapshot;

int ht_create(HtTable **out_table);
void ht_destroy(HtTable *table);
int ht_put(HtTable *table, const unsigned char *key, size_t key_length,
           const unsigned char *value, size_t value_length,
           int has_expiry, double expires_at);
int ht_is_live(HtTable *table, const unsigned char *key, size_t key_length,
               double cutoff, int *out_live);
int ht_get_copy(HtTable *table, const unsigned char *key, size_t key_length,
                HtValue *out_value);
int ht_remove(HtTable *table, const unsigned char *key, size_t key_length,
              int *out_removed);
int ht_remove_expired(HtTable *table, double cutoff);
int ht_copy_live(HtTable *table, double cutoff, HtSnapshot *out_snapshot);
void ht_free_value(HtValue *value);
void ht_free_snapshot(HtSnapshot *snapshot);

/* MINI_REDIS_TESTING=1인 시험 빌드에서만 제공 */
void ht_test_fail_alloc_after(int64_t successful_allocations);
int ht_test_dump(HtTable *table, HtSnapshot *out_snapshot,
                 size_t *out_count, size_t *out_bucket_count);
```

C 두 파일에는 같은 선언과 구현 자리를 만들고, Python 연결 클래스의 공개 메서드를 별도 C API로 다시 정의하지 않는다.

| C 표현 | ctypes 대응 |
| --- | --- |
| 불투명 테이블 핸들과 바이트 주소 | `c_void_p`. 테이블 생성 출력은 그 포인터 |
| `size_t`, `size_t*` | `c_size_t`, 그 포인터 |
| 상태 코드와 플래그, `int*` | `c_int`, 그 포인터 |
| `double` | `c_double` |
| `int64_t` | `c_int64` |
| `HtValue`, `HtEntry`, `HtSnapshot` | 같은 필드 순서의 `ctypes.Structure`. 배열 주소는 `POINTER(HtEntry)` |
| `void` 반환 | `restype=None` |

모든 함수에 `argtypes`와 `restype`을 명시한다. 64비트 환경의 예상 크기는 `HtValue=32`, `HtEntry=48`, `HtSnapshot=16`바이트다. 예상 오프셋은 `HtValue.expires_at=24`, `HtEntry.value=16`이다. C의 `sizeof`와 `offsetof`를 ctypes 선언과 대조해 크기와 오프셋 일치를 확인한다.

### 출력, 실패와 해제 규칙

- 모든 출력 인자는 0으로 초기화하고, 버퍼가 남아 있는 출력 구조체에 덮어쓰지 않는다. 실패하면 C가 이번 부분 할당을 정리하고 출력을 빈 상태로 둔다.
- `ht_create` 실패는 출력 핸들을 NULL로 둔다. `ht_destroy(NULL)`은 아무 일도 하지 않지만 같은 실제 핸들의 이중 해제는 금지한다. Python `close()`는 해제 후 핸들을 비운다.
- 길이가 0이면 바이트 주소가 NULL이어도 된다. 양수면 유효 주소가 필요하다. 입력 bytes는 C 호출이 끝날 때까지 Python이 보유한다.
- `has_expiry`는 0 또는 1이다. 1일 때 시각은 유한해야 한다. 무기한 출력은 플래그 0, 시각 0이며 해당 시각으로 만료를 판정하지 않는다.
- `HT_OK=0`, `HT_NOT_FOUND=1`, `HT_NOMEM=2`, `HT_INVALID=4`다. 각각 성공, 정상적인 없음, `MemoryError`, `RuntimeError`로 대응한다. `HT_INVALID`는 NULL 핸들, 필수 출력 주소 누락, 양수 길이의 NULL 바이트 주소, 잘못된 has_expiry와 비유한 시각을 변경 전에 거부하는 C 경계 검사로만 유지한다. 내부 구조로 막힌 입력 조합을 만드는 전용 실패 주입 시험은 두지 않는다. 탐지 조건을 정의하지 않은 `HT_CORRUPT`와 `TableCorruptionError`는 제거한다. 알 수 없는 상태 코드나 내부 assert 실패는 예상 밖 실행 오류로 FAILED에 연결하며 메모리 손상을 검출, 복구한다고 보장하지 않는다.
- 물리 항목 없음에 `HT_NOT_FOUND`를 쓰는 함수는 `ht_get_copy`뿐이다. `ht_is_live`와 `ht_remove`는 없음도 `HT_OK`와 출력 0으로 표현한다.
- 출력 bytes는 길이가 양수일 때 `ctypes.string_at(pointer, length)`로 Python에 복사한다. 길이 0은 주소를 읽지 않고 `b""`로 처리한다. `c_char_p` 자동 문자열 변환을 쓰지 않는다.
- C 출력 버퍼와 배열은 Python 복사 성공 여부와 무관하게 `finally`에서 같은 라이브러리의 해제 함수로 해제한다. 노드, 버킷, 핸들과 임시 버퍼 포인터는 연결 코드 밖으로 넘기지 않는다.
- `ht_copy_live`가 비었으면 `entries=NULL`, `count=0`이다. 복사본은 테이블과 독립적이며, 테이블을 닫은 뒤에도 따로 해제할 수 있다. Python에 복사한 값도 테이블 변경, 해제 후 유효하다.
- `ht_free_value`와 `ht_free_snapshot`은 내부 버퍼만 해제하고 구조체를 0으로 비운다. 구조체 자체는 Python 소유다. NULL이나 이미 비운 구조체의 해제는 아무 일도 하지 않는다.
- `ctypes.CDLL` 호출 중 GIL이 해제될 수 있다. 접근 소유자 하나라는 규칙으로 보호하며, 네이티브 메모리 훼손까지 Python 예외로 안전하게 복구한다고 약속하지 않는다.

### 시험 훅과 시험 빌드

C 할당은 내부 함수 한 경로에 모으고 시험 빌드에서만 실패 주입과 dump를 제공한다. `ht_test_fail_alloc_after(0)`은 다음 할당 한 번 실패 후 자동 해제, N은 N번 성공 뒤 한 번 실패, -1은 해제다. 다른 음수는 쓰지 않는다. 실패 주입은 라이브러리 전역 시험 경로이므로 관련 시험을 병렬 실행하지 않는다.

연결 코드는 시험 함수를 `_test_fail_alloc_after`, `_test_dump`로 제공한다. dump는 만료 항목을 포함한 독립 복사본과 물리 개수, 버킷 수를 반환한다. 실패 주입을 끈 뒤 상태를 관찰하고 C 복사본을 해제한다. 시험 API는 HTTP에 노출하지 않는다.

`MINI_REDIS_TESTING=1`로 시험 함수를 내보낸다. 서버와 테스트 프로세스 종료 후 시험용, 시연용 라이브러리를 다시 컴파일하며, 시험 훅이 없다고 관련 시험을 조용히 건너뛰지 않는다. 파일 실패 주입은 표준 라이브러리 호출 지점의 monkeypatch로 수행하며 [store-and-snapshots](store-and-snapshots.md)의 시험 준비를 따른다.

### 시연과 측정 호출 명세

- `demo/origin.py`: `create_app() -> Flask`, `main(argv: Sequence[str] | None = None) -> int`를 둔다. 원본 앱과 프로세스 실행을 구분한다. `create_app()`은 인자를 받지 않고 지연 시간은 앱 설정 `ORIGIN_DELAY`로 받는다. 키별 버전과 호출 횟수는 앱마다 두고 스레드 잠금으로 보호한다.

  | 경로 | 요청 | 성공 응답 | 의미 |
  | --- | --- | --- | --- |
  | `POST /fetch` | `{"key": "k"}` | `{"key": "k", "value": "k:v1", "version": 1}` | 키의 현재 버전 값 `<key>:v<version>`을 준다. 버전은 키마다 1부터 시작한다. 호출 횟수를 1 늘리고, 응답 전에 `ORIGIN_DELAY`초 대기해 원본 비용을 흉내 낸다 |
  | `POST /update` | `{"key": "k"}` | `{"key": "k", "version": 2}` | 키의 버전을 1 올려 원본 결과를 바꾼다. 한 번도 조회하지 않은 키도 버전 1에서 올라가 2가 된다 |
  | `GET /stats` | 없음 | `{"calls": 3}` | 지금까지의 `/fetch` 호출 횟수 |

  - 본문이 JSON 객체가 아니거나, `key`가 없거나 문자열이 아니면 400 `{"error": "invalid_input"}`이다. `Content-Type`이 `application/json`이 아니어도 같다. 빈 문자열 `""`은 유효한 키다.
  - `main`은 `--host`(기본 `127.0.0.1`), `--port`(기본 `8001`, 0이면 OS가 빈 포트를 고름), `--delay`(초, 기본 0.0)를 받는다. 음수 `--delay`는 인자 오류로 종료 코드 2다. 바인드 뒤 `origin listening on http://HOST:PORT` 한 줄을 출력하고 Ctrl+C, Windows의 Ctrl+Break, SIGTERM 중 하나를 받으면 소켓을 닫고 종료 코드 0으로 끝난다.
- `demo/benchmark.py`: `main(argv: Sequence[str] | None = None) -> int`를 둔다. D3은 실제 HTTP로 확인하며 원본과 캐시 프로세스 사이에 Python 객체를 공유하지 않는다.

## 구현 순서

1. 공통 함수 뼈대와 C 선언, ctypes 구조체를 맞춘다. Python은 NotImplementedError, int 반환 C 함수는 뼈대 전용 HT_UNIMPLEMENTED=5와 빈 출력, void 반환 C 함수는 abort()로 중단한다. 구현 완료 뒤 임시 상태 코드를 제거한다.
2. 담당 1이 노드·테이블 구조, 공용 탐색, 할당 래퍼, djb2, 오류 변환을 먼저 고정한다. 생성·해제와 ABI 프로그램, 실패 주입·dump 훅을 함께 구현한다. dump가 사용할 복사·해제 경계도 이 단계에 둔다.
3. put → get → is_live → remove와 ctypes 최소 연결을 구현하고 B1, B2를 실행한다. 빈 문자열, NUL, 한글도 확인한다. 2단계의 훅으로 n, m과 할당 실패를 관찰할 수 있어야 한다. 초기 용량 안의 부분 연결을 전체 구현 완료로 보고하지 않는다.
4. 확장·재해싱의 준비와 커밋, remove_expired, copy_live를 붙인다. T17, T19, S-T2와 각 할당 실패의 T20, S-T3을 확인한다. 덮어쓰기 실패에서 기존 만료 노드도 보존한다.
5. 복사·해제, Python 복사 MemoryError, 닫힘 수명의 B3~B5, 시험 훅 B6, 문자열 S-T4를 확인한다. 인위적 디코딩 실패·다른 스레드 접근 시험은 두지 않는다.
6. 첫 메모리·TTL·HTTP 연결 뒤 demo/origin.py와 demo/benchmark.py를 붙인다. 담당 2의 소비자와 D1, D2, D3을 확인하며 스냅샷 완성을 선행 조건으로 두지 않는다.
7. README에 C 빌드, 시험 빌드 전환, 측정 조건·결과와 실제 검증 환경을 남긴다. 파일 왕복 S-T4·S-T13은 선택 스냅샷 연결 뒤 담당 2와 확인한다.

C 연결과 실제 통합을 통과하기 전에는 HTTP 변환과 파일 형식 같은 독립 단위만 작성한 상태를 완료로 보고하지 않는다. 이 담당은 C 연결을 먼저 제공하고 원본, 측정 도구를 뒤에 붙인다.

## 테스트와 기대 결과

아래는 이 담당이 직접 작성하는 사례다. 나머지 사례는 [store-and-snapshots](store-and-snapshots.md)의 테스트와 기대 결과에 있는 T1~T12, T15~T16, T18, S-T5~S-T10, S-T13, D1, D2와 [http-and-runtime](http-and-runtime.md)의 테스트와 기대 결과에 있는 T13, T14, T21, S-T1, S-T11, S-T12, S-T14, H1~H12, R1~R6을 따른다. 각 사례는 별도 설명이 없으면 빈 저장소와 독립된 임시 데이터 디렉터리에서 시작한다. `t`는 테스트가 제어하는 epoch 시각, `E`는 절대 만료 시각이다.

T17, T19, T20은 시각을 고정해 TTL 항목이 유효한 상태로 시험한다. 실패 직후 상태 비교는 조회, 삭제로 상태를 바꾸기 전에 dump로 수행한다. 이후 삭제한 항목까지 계속 보존돼야 한다는 의미는 아니다.

| 번호 | 입력과 실행 순서 | 기대 결과와 최종 상태 |
| --- | --- | --- |
| T17. 충돌한 키 | 초기 16버킷에서 같은 버킷인 서로 다른 키 `"a"`, `"q"`를 값, TTL과 저장 → `"a"`만 덮어쓰기, 삭제 → `"q"` 조회 | `"q"`의 값, TTL 유지. 키 길이와 전체 바이트로 구별 |
| T19. 확장, 재해싱 | 초기 16버킷에 충돌 키와 TTL 키를 포함해 서로 다른 키 12개 저장 → 13번째 신규 키 저장 → 즉시 상태 확인 → 조회, 덮어쓰기, 삭제 | 12개까지 16버킷, 13번째 삽입 전에 32버킷. 기존 값, TTL 유지, 성공한 신규 키만 물리 개수 증가. 이후 연산 정상 |
| T20. 확장 할당 실패 | 13번째 신규 키 저장으로 확장을 유발하고 새 배열 등 준비 할당을 실패시킴 | `MemoryError`. 새 키 미저장, 기존 값, TTL, 물리 개수, 버킷 수, 테이블 보존. 실패 뒤 기존 키 조회, 삭제 가능. 실제 메모리 고갈 금지 |
| S-T2. 확장과 덮어쓰기 | 초기 16버킷에 서로 다른 키 12개 → 기존 키 덮어쓰기 → 13번째 신규 키 저장 | 덮어쓰기는 16버킷 유지, 13번째는 32버킷. 기존 값, TTL 보존 |
| S-T3. 할당 지점별 실패 | 노드, 키, 값, 새 배열 준비의 각 할당을 실패시킴. 만료된 기존 키 덮어쓰기 실패도 포함 | `MemoryError`, 변경 전 키, 값, TTL, 물리 개수, 버킷 수, 테이블 보존. 기존 만료 노드도 먼저 지우지 않음. 실패 뒤 조회, 삭제 가능 |
| S-T4. 문자열 왕복 | 충돌 키, 빈 키, 한글, `"a\u0000b"`, 보충 문자와 단독 surrogate를 키, 값으로 저장 → 조회 → 스냅샷 → 복구 | 문자열과 만료 정보 보존, 내부 NUL 뒤 바이트도 유지, 서로 다른 키 구별. UTF-8/surrogatepass 문자열 왕복을 Python 직접 호출과 파일 경계에서 검증 |
| B1. 최소 C 연결 | Flask와 파일 없이 생성 → `k=v` 삽입 → 복사 조회 → 삭제 → 없음 확인 → 해제 | 초기 16버킷, 값 `v`, 삭제 성공, 이후 get만 `HT_NOT_FOUND`. `is_live`, `remove`의 없음은 `HT_OK`와 출력 0 |
| B2. ABI와 선언 | C `sizeof`, `offsetof`와 ctypes 구조체 크기, 오프셋 대조. 모든 함수 선언 검사 | 64비트에서 `HtValue=32`, `HtEntry=48`, `HtSnapshot=16`, 오프셋 24와 16. 각 `argtypes/restype`, 포인터 출력과 void 반환 일치 |
| B3. 복사와 해제 | get, snapshot 복사 후 테이블 변경, 닫기. Python 복사의 MemoryError도 주입 | 독립 복사본은 원본 변경과 무관. 실패 시에도 같은 C 라이브러리에서 임시 버퍼 해제. 해제 함수는 구조체를 비우고 빈 구조체 재해제 허용 |
| B4. 빈 버퍼와 출력 실패 | 빈 키, 값, 빈 snapshot, 생성, 복사 할당 실패 시험 | 0길이는 NULL 주소 허용, 빈 snapshot은 NULL/0. 생성 실패 출력 핸들 NULL, 부분 실패 출력은 빈 상태. 정상 입력의 할당 실패와 해제를 확인 |
| B5. 수명 | `close()` 두 번 → 데이터 메서드 | 중복 close는 안전, 닫힘 이후 데이터 메서드는 `RuntimeError`. 실제 핸들 이중 해제 없음 |
| B6. 시험 훅 | 실패 주입 0, 양수 N, -1을 각각 설정하고 할당, dump 실행 | 다음 한 번 또는 N번 성공 뒤 한 번만 실패, 이후 자동 해제. -1은 해제. dump 전 주입 해제, 모든 물리 항목과 n, m 확인. 시험 훅 누락 시 관련 시험을 조용히 통과, 생략하지 않음 |
| D3. 성능 비교 | 동일한 원본 작업과 결과로 캐시 사용, 미사용 비교. 요청 수, 동시성, 데이터 크기, 예열, 적중, 미적중, 반복 횟수와 측정 도구 기록 | 응답 시간과 원본 호출 횟수를 함께 보고. 필요 시 메모리도 기록. 모의 원본 비용은 명시하고 캐시가 항상 빠르다고 가정하지 않음. 해시 테이블 단독 측정을 전체 캐시 효과로 대체하지 않음 |

`tests/abi_probe.c`는 `int main(void)`로 C 크기, 오프셋만 출력하는 시험용 프로그램이며 운영 라이브러리에 ABI 조회용 함수를 추가하지 않는다. B2의 대조는 이 출력과 ctypes 선언을 비교한다.

## 연결 기준

- Store는 `HashTable`의 `put`, `is_live`, `get`, `remove`, `remove_expired`, `copy_live`를 호출하고 바이트와 절대 만료 시각만 주고받는다. Store의 호출 순서와 TTL 판정은 [store-and-snapshots](store-and-snapshots.md)이 정한다.
- 테이블 생성, 복구, 해제는 실행 관리자의 작업자 스레드가 수행한다. 연결 클래스는 닫힘 상태를 검사하고 C 주소를 밖으로 내보내지 않는다. 실제 통합 경로의 단일 접근 스레드는 S-T14에서 확인한다. 작업자 수명 규칙은 [http-and-runtime](http-and-runtime.md)을 따른다.
- 외부 입력 오류는 HTTP에서 큐 제출 전에 걸러지고, 파일 입력은 복구 전에 전체 검증된다. C 연결은 할당, 표현 실패를 `MemoryError`, 닫힘, 호출 오류를 `RuntimeError`로 전달한다. 어느 경우도 `None`이나 `False`로 숨기지 않는다.
- C 헤더나 수명을 바꾸면 C 구현, ctypes, Store와 ABI 시험을 담당 1과 2가 함께 확인하고, 작업자 접근이나 종료 동작에도 영향이 있으면 담당 3도 확인한다.
- 공개 계약을 임의로 늘리지 않는다. Store가 필요로 하는 연산만 `HashTable`에 두고 별도 C API나 다른 저장 엔진을 만들지 않는다.
- 첫 통합 완료 기준은 B1, B2와 빈 문자열, NUL, 한글의 저장, 조회, 삭제, 해제다. 시험 훅을 사용할 수 있고 C 오류가 Python 예외로 전달되어야 한다.
- C 연결 코드는 HTTP나 실행 제어를 import하지 않는다. 이 담당의 파일은 `hashtable.h`, `hashtable.c`, `hashtable.py` 안에서 닫힌 경계를 유지한다.
- 바이너리와 실제 스냅샷은 소스로 공유하지 않고 각자 빌드한다. pip 설치가 C 컴파일을 수행한다고 가정하지 않는다.
