# KJG-SW-AI-W11-Redis

직접 구현한 C 해시 테이블에 문자열과 TTL을 저장하고, 로컬 Flask HTTP API로 저장·조회·삭제를 받는 학습용 캐시 서버다. 설계 요약은 [docs/overview.md](docs/overview.md), 작업 목록은 [docs/issues.md](docs/issues.md)에 있다.

## 요구 환경

[docs/planning.md](docs/planning.md) 3.9절을 따른다.

- CPython 3.12, 일반 GIL 빌드, 64비트
- Linux: x86-64, GCC
- Windows: x64 Python, MSVC(Visual Studio 2022 Build Tools의 C++ 빌드 도구·Windows SDK) 또는 MinGW-w64 x64 GCC
- macOS는 검증 대상이 아니다.

## 설치

Linux:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
```

Windows (PowerShell):

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[test]"
```

`pip install`은 C 라이브러리를 빌드하지 않는다. 아래 C 빌드를 따로 실행한다.

## C 빌드

`src/hashtable.c`를 `mini_redis/` 안의 공유 라이브러리로 빌드한다. 바이너리는 저장소에 올리지 않고 각자 소스에서 빌드한다. 라이브러리를 사용 중인 서버·테스트 프로세스를 종료한 뒤 다시 빌드한다.

| 용도 | Linux (GCC) | Windows (MSVC) | Windows (MinGW-w64 GCC) |
| --- | --- | --- | --- |
| 일반 빌드 (서버·시연·측정) | `gcc -std=c11 -O2 -Wall -Wextra -fPIC -shared -o mini_redis/_hashtable.so src/hashtable.c` | `cl /nologo /std:c11 /O2 /W4 /DHT_BUILD_DLL /LD src\hashtable.c /Femini_redis\_hashtable.dll` | `gcc -std=c11 -O2 -Wall -Wextra -DHT_BUILD_DLL -shared -o mini_redis/_hashtable.dll src/hashtable.c` |
| 시험 빌드 (pytest) | 일반 빌드 명령에 `-DMINI_REDIS_TESTING=1` 추가 | 일반 빌드 명령에 `/DMINI_REDIS_TESTING=1` 추가 | 일반 빌드 명령에 `-DMINI_REDIS_TESTING=1` 추가 |

Windows에서 MSVC는 x64 개발자 PowerShell을, MinGW-w64 GCC는 설치 디렉터리의 `bin`(`gcc.exe`가 있는 폴더)을 `PATH`에 추가한 PowerShell을 사용한다. `gcc -dumpmachine` 출력이 `x86_64-w64-mingw32`가 아니면 GCC의 설치 대상과 `PATH` 순서를 확인한다.

시험 빌드만 실패 주입·dump 시험 훅을 내보낸다. pytest는 시험 빌드로, 서버·시연·측정은 일반 빌드로 다시 컴파일한 뒤 실행한다.

### ABI 확인 프로그램

```bash
gcc -std=c11 -Isrc -o tests/abi_probe tests/abi_probe.c && ./tests/abi_probe
```

```powershell
cl /nologo /std:c11 /Isrc tests\abi_probe.c /Fetests\abi_probe.exe; .\tests\abi_probe.exe
```

MinGW-w64 GCC (PowerShell):

```powershell
gcc -std=c11 -Isrc -o tests/abi_probe.exe tests/abi_probe.c
if ($LASTEXITCODE -eq 0) { .\tests\abi_probe.exe }
```

출력한 `sizeof`·`offsetof`를 ctypes 선언과 대조한다(B2).

## 테스트

가상환경 활성화 후 HTTP·공통 계약만 확인할 때는 C 빌드 없이 실행할 수 있다.

```bash
python -m pytest tests/test_http.py tests/test_contracts.py -q
```

전체 검사는 위 **시험 빌드** 후 실행한다.

```bash
pytest
```

## 실행과 종료

```bash
python -m mini_redis --data-dir ./data
```

`127.0.0.1:5000`에서 수신한다. `--data-dir`은 필수이며 서버마다 다른 디렉터리를 쓴다. Linux는 Ctrl+C 또는 SIGTERM, Windows 콘솔은 Ctrl+C 또는 Ctrl+Break로 종료한다.

정상 종료는 수락한 작업을 완료한 뒤 `snapshot.json`을 저장한다. 종료 코드 0은 성공, 1은 시작·실행·최종 저장 실패다. 강제 종료에는 최종 저장을 보장하지 않는다.

컨트롤러·프로세스·시연 통합 시험은 **시험 빌드** 후 실행한다. Windows 정상 종료 시험에는 공유 콘솔이 필요하다.

```bash
python -m pytest tests/test_runtime.py tests/test_integration_runtime.py tests/test_integration_store.py -q
```

## 시연: API 응답 캐시

원본 API, 캐시 서버, 소비자를 서로 다른 프로세스로 실행해 캐시 재사용(D1)과 명시적 무효화(D2)를 보여 준다. 세 역할은 HTTP로만 통신한다. 먼저 **일반 빌드**로 C 라이브러리를 다시 컴파일하고, 터미널 세 개에서 저장소 루트를 기준으로 실행한다.

터미널 1, 원본 API(`127.0.0.1:8001`):

```bash
python demo/origin.py
```

`origin listening on http://127.0.0.1:8001` 한 줄을 출력하고 요청을 받는다. `--delay 0.2`처럼 지연을 주면 `/fetch` 응답마다 원본 비용을 흉내 낸다.

터미널 2, 캐시 서버(`127.0.0.1:5000`):

```bash
python -m mini_redis --data-dir ./data
```

터미널 3, 소비자. 같은 키를 두 번 조회하면 두 번째는 캐시에서 가져온다(D1).

```bash
python demo/client.py --cache http://127.0.0.1:5000 --origin http://127.0.0.1:8001 get weather
python demo/client.py --cache http://127.0.0.1:5000 --origin http://127.0.0.1:8001 get weather
```

```text
{"key": "weather", "value": "weather:v1", "source": "origin"}
{"key": "weather", "value": "weather:v1", "source": "cache"}
```

원본 결과를 바꿔도 TTL(기본 60초) 안에서는 캐시의 옛 값을 쓴다. 캐시 키를 명시적으로 지우면 새 원본 결과를 가져와 다시 저장한다(D2).

```bash
curl -X POST -H "Content-Type: application/json" -d '{"key": "weather"}' http://127.0.0.1:8001/update
python demo/client.py --cache http://127.0.0.1:5000 --origin http://127.0.0.1:8001 get weather
python demo/client.py --cache http://127.0.0.1:5000 --origin http://127.0.0.1:8001 invalidate weather
python demo/client.py --cache http://127.0.0.1:5000 --origin http://127.0.0.1:8001 get weather
```

```text
{"key":"weather","version":2}
{"key": "weather", "value": "weather:v1", "source": "cache"}
{"key": "weather", "deleted": true}
{"key": "weather", "value": "weather:v2", "source": "origin"}
```

원본 호출 횟수는 `curl http://127.0.0.1:8001/stats`로 확인한다. 위 순서를 모두 실행하면 `{"calls":2}`다. Windows PowerShell에서는 `curl` 대신 `curl.exe`를 쓰고 JSON 본문의 따옴표를 `'{\"key\": \"weather\"}'`처럼 이스케이프한다.

소비자는 실패하면 원인 한 줄을 stderr에 쓰고 종료 코드 1로 끝나며 자동 재시도하지 않는다. 시연을 마치면 캐시 서버를 Ctrl+C(Windows는 Ctrl+C 또는 Ctrl+Break)로 정상 종료하고 원본 API도 같은 방법(Ctrl+C, Windows는 Ctrl+Break도 가능)으로 정상 종료한다. 명령과 응답 형식의 상세는 [store-and-snapshots](docs/store-and-snapshots.md)의 시연 소비자 절과 [c-hashtable-and-bindings](docs/c-hashtable-and-bindings.md)의 시연과 측정 호출 명세를 따른다.

측정 명령 안내는 MR-10에서 추가한다.

## 검증한 환경

| 날짜 | OS | Python | 패키지 | 컴파일러 | 확인한 범위 |
| --- | --- | --- | --- | --- | --- |
| 2026-11-14 | Ubuntu 26.04.1 LTS, x86-64 | CPython 3.12.15, 64비트, GIL 빌드 | Flask 3.1.3, Werkzeug 3.1.9, pytest 8.3.5 | GCC 15.2.0 | 깨끗한 venv에서 `pip install -e ".[test]"` 성공. 뼈대 C의 일반·시험 빌드와 ABI 확인 프로그램 실행, ctypes 크기·오프셋 일치 확인. 기능 동작은 미구현이라 미확인 |
| 2026-11-16 | Windows x64, 빌드 26200 | CPython 3.12.10, 64비트 | Flask 3.1.3, Werkzeug 3.1.9, pytest 8.3.5 | MinGW-w64 GCC 16.2.0 | main 병합 후 시험 빌드에서 `.venv/Scripts/python.exe -m pytest -q`: 367 passed, 종료 코드 0. R4·R5·S-T6·S-T9 프로세스 시험 포함. 병합 전 일반 빌드·실제 CLI·5000 포트에서 저장 → Ctrl+Break 정상 종료(코드 0) → 재시작·복구 → 정상 종료 확인. 병합 후 수동 확인·Linux·수동 Ctrl+C·깨끗한 설치는 미실행 |
| 2026-11-16 | Windows x64, 빌드 26200 | CPython 3.12.10, 64비트 | Flask 3.1.3, Werkzeug 3.1.9, pytest 8.3.5 | MinGW-w64 GCC 16.1.0(MSYS2 UCRT64) | 일반 빌드로 원본 API·캐시 서버(`python -m mini_redis`, 5000 포트)·소비자를 별도 프로세스로 실행해 위 시연 순서를 그대로 확인. 출력과 원본 호출 횟수(1 → 2)가 README와 일치. 캐시 서버는 Ctrl+Break 정상 종료(코드 0) 후 `snapshot.json` 생성 확인. curl의 `/update`·`/stats` 호출은 Git Bash curl과 PowerShell `curl.exe` 두 형태로 따로 실행해 확인. 같은 흐름을 실제 컨트롤러 프로세스로 띄운 캐시 서버로 자동 시험(`test_d2_demo_through_controller_process`)하고, 원본 API의 Ctrl+Break 정상 종료(코드 0)도 자동 시험으로 확인. 원본·캐시 서버의 수동 Ctrl+C와 Linux는 미실행 |

