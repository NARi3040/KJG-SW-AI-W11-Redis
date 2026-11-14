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

Windows에서 MSVC는 x64 개발자 PowerShell을, MinGW-w64 GCC는 `bin`을 `PATH`에 추가한 PowerShell을 사용한다. MinGW의 `gcc -dumpmachine` 출력은 `x86_64-w64-mingw32`여야 한다.

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

전체 검사는 위 **시험 빌드** 후 실행한다. 현재는 다른 기능의 미구현 테스트가 남아 있어 전체 통과를 기대하지 않는다.

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
| 2026-11-14 | Windows, x64 | CPython 3.12.10, 64비트 | Flask 3.1.3, Werkzeug 3.1.9, pytest 8.3.5 | 이번 HTTP 검증에 C 빌드 불필요 | 기존 `.venv`에서 위 HTTP·공통 계약 명령: 종료 코드 0, 169 passed. H1~H12·T13·T14·T21의 HTTP 변환·미제출 경계 확인. T21의 실제 만료 항목 상태, C·작업자·서버 통합, 전체 pytest와 깨끗한 설치는 미실행 |

