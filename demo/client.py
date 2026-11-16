"""시연용 캐시 소비자. HTTP로만 캐시 서버를 쓰고 자동 재시도하지 않는다. 명세: docs/store-and-snapshots.md

원본 응답을 캐시에 저장하고 TTL 안에서 재사용하며, 원본 결과가 바뀌면 캐시 키를 명시적으로 삭제한다.
표준 라이브러리만 쓰고 mini_redis를 import하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any, Sequence

TIMEOUT = 5.0
CACHE_KEY_PREFIX = "origin:"
DEFAULT_TTL = 60


class _ClientError(Exception):
    """요청 실패나 응답 형식 오류를 종료 코드로 바꾸기 위한 내부 예외."""


def _post(url: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    """JSON POST 요청을 한 번 보내고 JSON 응답을 돌려준다. 재시도하지 않는다."""
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url.rstrip("/") + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise _ClientError(f"POST {path} failed with HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise _ClientError(f"POST {path} failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise _ClientError(f"POST {path} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise _ClientError(f"POST {path} returned a non-object response")
    return payload


def _cache_key(key: str) -> str:
    """캐시에 쓸 키 이름을 만든다."""
    return CACHE_KEY_PREFIX + key


def _lookup(cache: str, key: str) -> str | None:
    """캐시에서 값을 조회한다. 없으면 None이다."""
    payload = _post(cache, "/get", {"key": _cache_key(key)})
    if "result" not in payload:
        raise _ClientError("cache /get response has no result field")
    result = payload["result"]
    if result is not None and not isinstance(result, str):
        raise _ClientError("cache /get result is not a string")
    return result


def _fetch_origin(origin: str, key: str) -> str:
    """원본을 한 번 호출해 값을 가져온다."""
    payload = _post(origin, "/fetch", {"key": key})
    value = payload.get("value")
    if not isinstance(value, str):
        raise _ClientError("origin /fetch response has no string value")
    return value


def _store(cache: str, key: str, value: str, ttl: int) -> None:
    """원본 값을 캐시에 저장한다. 실패는 숨기지 않는다."""
    payload = _post(cache, "/set", {"key": _cache_key(key), "value": value, "ttl": ttl})
    if payload.get("result") is not True:
        raise _ClientError("cache /set did not confirm the store")


def _emit(payload: dict[str, Any]) -> None:
    """결과를 한 줄 JSON으로 출력한다."""
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _get(cache: str, origin: str, key: str, ttl: int) -> None:
    """캐시를 먼저 보고 없으면 원본을 불러 캐시에 저장한다."""
    cached = _lookup(cache, key)
    if cached is not None:
        _emit({"key": key, "value": cached, "source": "cache"})
        return
    value = _fetch_origin(origin, key)
    _store(cache, key, value, ttl)
    _emit({"key": key, "value": value, "source": "origin"})


def _invalidate(cache: str, key: str) -> None:
    """캐시 키를 명시적으로 삭제한다."""
    payload = _post(cache, "/delete", {"key": _cache_key(key)})
    deleted = payload.get("result")
    if not isinstance(deleted, bool):
        raise _ClientError("cache /delete response has no boolean result")
    _emit({"key": key, "deleted": deleted})


def _parser() -> argparse.ArgumentParser:
    """하위 명령과 공통 인자를 정의한다."""
    parser = argparse.ArgumentParser(prog="client", description="시연용 캐시 소비자")
    parser.add_argument("--cache", required=True, help="캐시 서버 URL")
    parser.add_argument("--origin", required=True, help="원본 API URL")
    subparsers = parser.add_subparsers(dest="command", required=True)

    get_parser = subparsers.add_parser("get", help="캐시에서 읽고 없으면 원본에서 채운다")
    get_parser.add_argument("key")
    get_parser.add_argument("--ttl", type=int, default=DEFAULT_TTL)

    invalidate_parser = subparsers.add_parser("invalidate", help="캐시 키를 삭제한다")
    invalidate_parser.add_argument("key")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """원본 응답 저장·재사용과 캐시 키 명시적 삭제 흐름을 실행한다."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "get":
            _get(args.cache, args.origin, args.key, args.ttl)
        else:
            _invalidate(args.cache, args.key)
    except _ClientError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
