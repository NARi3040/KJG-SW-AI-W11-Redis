# KJG-SW-AI-W11-Redis

직접 구현한 C 해시 테이블에 문자열과 TTL을 저장하고, 로컬 Flask HTTP API로 저장·조회·삭제를 받는 학습용 캐시 서버다. 설계 요약은 [docs/overview.md](docs/overview.md), 작업 목록은 [docs/issues.md](docs/issues.md)에 있다.

## 요구 환경

[docs/planning.md](docs/planning.md) 3.9절을 따른다.

- CPython 3.12, 일반 GIL 빌드, 64비트
- Linux: x86-64, GCC
- Windows: x64 Python, Visual Studio 2022 Build Tools(C++ 빌드 도구와 Windows SDK), x64 개발자 PowerShell
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

| 용도 | Linux (GCC) | Windows (x64 개발자 PowerShell, MSVC) |
| --- | --- | --- |
| 일반 빌드 (서버·시연·측정) | `gcc -std=c11 -O2 -Wall -Wextra -fPIC -shared -o mini_redis/_hashtable.so src/hashtable.c` | `cl /nologo /std:c11 /O2 /W4 /DHT_BUILD_DLL /LD src\hashtable.c /Femini_redis\_hashtable.dll` |
| 시험 빌드 (pytest) | 일반 빌드 명령에 `-DMINI_REDIS_TESTING=1` 추가 | 일반 빌드 명령에 `/DMINI_REDIS_TESTING=1` 추가 |

시험 빌드만 실패 주입·dump 시험 훅을 내보낸다. pytest는 시험 빌드로, 서버·시연·측정은 일반 빌드로 다시 컴파일한 뒤 실행한다.

### ABI 확인 프로그램

```bash
gcc -std=c11 -Isrc -o tests/abi_probe tests/abi_probe.c && ./tests/abi_probe
```

```powershell
cl /nologo /std:c11 /Isrc tests\abi_probe.c /Fetests\abi_probe.exe; .\tests\abi_probe.exe
```

출력한 `sizeof`·`offsetof`를 ctypes 선언과 대조한다(B2).

## 테스트

```bash
pytest
```

## 실행과 종료

```bash
python -m mini_redis --data-dir ./data
```

`127.0.0.1:5000`에서 수신한다. `--data-dir`은 필수이며 서버마다 다른 디렉터리를 쓴다. Linux는 Ctrl+C 또는 SIGTERM, Windows 콘솔은 Ctrl+C 또는 Ctrl+Break로 종료한다.

시연(MR-09)과 측정(MR-10) 명령은 해당 작업에서 추가한다.

## 검증한 환경

| 날짜 | OS | Python | 패키지 | 컴파일러 | 확인한 범위 |
| --- | --- | --- | --- | --- | --- |
| 2026-11-14 | Ubuntu 26.04.1 LTS, x86-64 | CPython 3.12.15, 64비트, GIL 빌드 | Flask 3.1.3, Werkzeug 3.1.9, pytest 8.3.5 | GCC 15.2.0 | 깨끗한 venv에서 `pip install -e ".[test]"` 성공. 뼈대 C의 일반·시험 빌드와 ABI 확인 프로그램 실행, ctypes 크기·오프셋 일치 확인. 기능 동작은 미구현이라 미확인 |

### 이슈 #8 로컬 검증

2026-11-14, Windows x64(빌드 26200), CPython 3.12.10, Flask 3.1.3,
Werkzeug 3.1.9, pytest 8.3.5, MinGW GCC 16.2.0에서 확인했다.
MinGW는 보조 검증이며, 정식 Windows MSVC 환경의 지원 검증을 대신하지 않는다.

PowerShell에서 실행한 명령과 결과:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[test]"
gcc -std=c11 -O2 -Wall -Wextra -Werror -DHT_BUILD_DLL -shared -o mini_redis/_hashtable.dll src/hashtable.c
.\.venv\Scripts\python -c "from mini_redis.hashtable import HashTable; t=HashTable(); t.put(b'k',b'v',None); assert t.get(b'k') == (b'v',None); assert not hasattr(t._lib,'ht_test_dump'); t.close()"
gcc -std=c11 -O2 -Wall -Wextra -Werror -DHT_BUILD_DLL -DMINI_REDIS_TESTING=1 -shared -o mini_redis/_hashtable.dll src/hashtable.c
gcc -std=c11 -Isrc -o tests/abi_probe.exe tests/abi_probe.c
.\tests\abi_probe.exe
.\.venv\Scripts\python -m pytest tests/test_hashtable.py -q
.\.venv\Scripts\python -m pytest -q --tb=line
git diff --check
```

- 설치·일반/시험 빌드·일반 빌드 저장/조회/해제·ABI 프로그램·diff 검사는 종료 코드 0.
- ABI: 구조체 크기 32/48/16, `HtValue.expires_at` 오프셋 24,
  `HtEntry.value` 오프셋 16. 모든 필드의 C/ctypes 대응은 B2 테스트에서도 대조했다.
- C 연결 테스트: **38 passed**, 종료 코드 0.
- 전체: **93 passed, 72 failed**, 종료 코드 1. 실패는 미구현 영역의
  `NotImplementedError`이며, 실제 Store 연결 테스트 9개도 `Store.set()` 미구현으로 실패한다.
- 문자열의 C 바이트 왕복은 확인했다. S-T4·S-T13의 파일 왕복은 MR-07/08,
  Store 연결은 MR-04에서 재검증해야 한다.

이번 구현의 MSVC·Linux 실행, 서버·시연·측정은 미검증이다. 위 명령 이후 로컬 DLL은
시험 빌드 상태이므로 서버·시연·측정 전에는 일반 빌드로 바꾼다.
