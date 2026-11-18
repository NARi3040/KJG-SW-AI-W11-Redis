# Mini Redis

C로 직접 구현한 해시 테이블을 Python `ctypes`로 연결한 학습용 캐시 서버다.
Flask HTTP API로 문자열을 저장·조회·삭제하며, TTL 만료와 스냅샷 복구를 지원한다.

```mermaid
flowchart LR
    Client[클라이언트] -- "POST /set /get /delete" --> HTTP[Flask<br/>입력 검증]
    HTTP --> Queue[큐<br/>최대 64개]
    Queue --> Worker[단일 작업자]
    Worker --> Store[Store<br/>TTL 판정]
    Store -- ctypes --> C[C 해시 테이블]
    Worker -. 60초마다·종료 시 .-> Snapshot[(snapshot.json)]
```

`demo/`에서는 원본 API 응답을 캐시에 재사용하고, 캐시 사용 전후의 응답 시간과 원본 호출 횟수를 비교한다.

## 핵심 설계

| 영역 | 구현 | 이유 |
| --- | --- | --- |
| 해시 테이블 | djb2 해시, 16버킷 시작, 체이닝, 3/4 차면 2배 확장 | 충돌·확장을 직접 구현, 확장 때 노드만 재연결 |
| 실패 안전 | 필요한 메모리를 모두 할당한 뒤에 반영 | 할당 실패에도 기존 데이터 유지(`500 memory_error`) |
| C↔Python | 길이 있는 바이트로 저장, C 복사본은 C 함수로 해제 | 빈 문자열·NUL 저장 가능, 할당한 쪽이 해제 |
| 동시성 | 모든 요청을 큐에 넣고 작업자 1개가 처리 | C 테이블에 락 불필요, 큐 포화 시 `503 busy` |
| TTL | 조회 때 만료 판정, 1초마다 일괄 청소 | 아무도 읽지 않는 만료 항목도 정리 |
| 스냅샷(선택) | 60초마다·종료 시 저장, 시작 시 복구 | 재시작해도 캐시 유지, 임시 파일 교체로 손상 방지 |

## 빠른 시작

CPython 3.12(64비트, 일반 GIL 빌드)와 C 컴파일러가 필요하다.
지원 대상은 Windows x64·Linux x86-64다.

### 1. 설치

모든 명령은 저장소 루트에서 실행하며, 새 터미널을 열 때마다 가상환경을 활성화한다.

```bash
python -m venv .venv
# Linux: source .venv/bin/activate
# PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[test]"
```

### 2. C 라이브러리 빌드

패키지 설치와 C 빌드는 별개다. 사용하는 환경의 명령 하나를 실행한다.

```bash
# Linux · GCC
gcc -std=c11 -O2 -Wall -Wextra -fPIC -shared -o mini_redis/_hashtable.so src/hashtable.c

# Windows · MinGW-w64 x64 GCC
gcc -std=c11 -O2 -Wall -Wextra -DHT_BUILD_DLL -shared -o mini_redis/_hashtable.dll src/hashtable.c

# Windows · MSVC (x64 개발자 PowerShell)
cl /nologo /std:c11 /O2 /W4 /DHT_BUILD_DLL /LD src\hashtable.c /Femini_redis\_hashtable.dll
```

### 3. 서버 실행

```bash
python -m mini_redis --data-dir ./data
```

`127.0.0.1:5000`에서 실행된다. Ctrl+C로 종료하면 `snapshot.json`에 저장하고, 같은 디렉터리로 재시작하면 복구한다. 강제 종료 시 최종 저장은 보장하지 않는다.

## 캐시 시연·성능 비교

캐시 서버를 실행한 상태에서 별도 터미널에 원본 API를 띄운다.

```bash
python demo/origin.py --delay 0.05
```

다른 터미널에서 같은 키를 두 번 조회하면 원본 대신 캐시를 재사용한다. `invalidate`로 캐시를 명시적으로 지울 수 있다.

```bash
python demo/client.py --cache http://127.0.0.1:5000 --origin http://127.0.0.1:8001 get weather
python demo/client.py --cache http://127.0.0.1:5000 --origin http://127.0.0.1:8001 get weather
python demo/client.py --cache http://127.0.0.1:5000 --origin http://127.0.0.1:8001 invalidate weather
```

성능 비교는 캐시 미사용·첫 미적중 포함·전부 적중을 같은 결과로 측정하고 JSON으로 출력한다.

```bash
python demo/benchmark.py --cache http://127.0.0.1:5000 --origin http://127.0.0.1:8001 --requests 10 --repeats 2
```

`--delay 0.05`는 원본 비용을 흉내 낸 50ms 지연이다. 측정 중에는 다른 요청을 보내지 않는다. 자세한 옵션은 각 스크립트의 `--help`에서 확인할 수 있다.

다음은 2026-11-18 Windows 11 x64, CPython 3.12.10에서 `--delay 0.05`로 띄운 원본에 `--requests 50 --repeats 3`으로 측정한 결과다. 평균 응답은 반복 3회의 범위다.

| 단계 | 평균 응답 | 원본 호출(50회 요청당) |
| --- | --- | --- |
| 캐시 미사용(원본 직접) | 59~66ms | 50 |
| 캐시 첫 미적중 포함 | 9~12ms | 1 |
| 캐시 전부 적중 | 9~11ms | 0 |

## 테스트

서버를 종료한 뒤 위 빌드 명령에 `-DMINI_REDIS_TESTING=1`(MSVC: `/DMINI_REDIS_TESTING=1`)을 추가해 시험 빌드한다.

```bash
python -m pytest -q
python demo/benchmark.py --self-check
```

서버·시연·측정 전에는 일반 빌드로 되돌린다. Windows 종료 프로세스 시험에는 공유 콘솔이 필요하다.
ABI 확인은 `tests/abi_probe.c`를 컴파일해 출력한 크기·오프셋을 `mini_redis/hashtable.py`의 ctypes 선언과 대조한다.

2026-11-18 Windows 11 x64(CPython 3.12.10, MinGW-w64 GCC 16.1.0)에서 위 두 명령이 `369 passed`, `{"self_check": "ok", "checks": 11}`로 통과했다. 같은 날 Linux에서는 실행하지 않았다.

## 문서

- [설계 요약과 호출 흐름](docs/overview.md)
- [C 해시 테이블·ctypes·측정](docs/c-hashtable-and-bindings.md)
- [Store·TTL·스냅샷·소비자](docs/store-and-snapshots.md)
- [HTTP·작업자·서버 수명](docs/http-and-runtime.md)
- [작업 목록](docs/issues.md) · [협업 규칙](CONTRIBUTING.md)
