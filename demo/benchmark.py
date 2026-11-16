"""캐시 사용·미사용 성능 비교(D3). 실제 HTTP로 측정한다. 명세: docs/c-hashtable-and-bindings.md

같은 원본 키·작업·결과를 direct(원본만), cache cold(첫 미적중), cache warm(전부 적중)으로
순차 실행하고 원본 /stats 델타로 실제 원본 호출 횟수를 센다. 재시도는 하지 않는다.
"""

from __future__ import annotations

import argparse
import http.client
import importlib.util
import json
import platform
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Sequence

# 같은 디렉터리의 시연 소비자 헬퍼를 그대로 쓴다. demo는 패키지가 아니므로 경로로 불러온다.
_spec = importlib.util.spec_from_file_location("_demo_client", Path(__file__).with_name("client.py"))
if _spec is None or _spec.loader is None:  # pragma: no cover - 파일이 함께 배포되는 전제
    raise ImportError("demo/client.py is required")
_client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_client)

TOOL = "demo/benchmark.py"
TTL_SECONDS = 3600
CONCURRENCY = 1
_Error = _client._ClientError


def _latency_summary(samples: list[float]) -> dict[str, float]:
    """초 단위 표본을 밀리초 요약으로 바꾼다. 빈 표본은 집계 오류다."""
    if not samples:
        raise ValueError("no latency samples")
    ms = [sample * 1000 for sample in samples]
    return {
        "total_ms": round(sum(ms), 3),
        "mean_ms": round(sum(ms) / len(ms), 3),
        "min_ms": round(min(ms), 3),
        "max_ms": round(max(ms), 3),
    }


def _check_phase(
    mode: str,
    *,
    values: list[str],
    hits: int,
    misses: int,
    origin_calls: int,
    requests: int,
) -> None:
    """한 단계의 결과·적중·원본 호출 집계가 실행 조건과 맞는지 확인한다."""
    if requests < 1 or len(values) != requests or hits + misses > requests:
        raise ValueError(f"{mode}: invalid aggregation")
    if len(set(values)) != 1:
        raise ValueError(f"{mode}: results differ across requests")
    if mode == "direct":
        ok = hits == 0 and misses == 0 and origin_calls == requests
    elif mode == "cache_cold":
        ok = hits == requests - 1 and misses == 1 and origin_calls == 1
    elif mode == "cache_warm":
        ok = hits == requests and misses == 0 and origin_calls == 0
    else:
        raise ValueError(f"unknown mode: {mode}")
    if not ok:
        raise ValueError(
            f"{mode}: expected hits/misses/origin_calls do not match requests={requests}"
        )


def _origin_calls(origin: str) -> int:
    """원본 /stats의 누적 호출 횟수를 읽는다."""
    url = origin.rstrip("/") + "/stats"
    try:
        with urllib.request.urlopen(url, timeout=_client.TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        raise _Error(f"GET /stats failed: {exc}") from exc
    calls = payload.get("calls") if isinstance(payload, dict) else None
    if not isinstance(calls, int):
        raise _Error("origin /stats returned no integer calls")
    return calls


def _direct(origin: str, key: str) -> tuple[str, None]:
    """캐시 없이 원본만 호출한다. 적중·미적중 집계 대상이 아니다."""
    return _client._fetch_origin(origin, key), None


def _cache_aside(cache: str, origin: str, key: str) -> tuple[str, bool]:
    """캐시를 먼저 보고, 없으면 원본을 불러 TTL로 저장한다."""
    value = _client._lookup(cache, key)
    if value is not None:
        return value, True
    value = _client._fetch_origin(origin, key)
    _client._store(cache, key, value, TTL_SECONDS)
    return value, False


def _measure(
    requests: int, call: Callable[[], tuple[str, bool | None]]
) -> dict[str, Any]:
    """순차 요청을 실행하고 결과 표본·적중·지연을 모은다. hit이 None이면 캐시 미사용 단계다."""
    values: list[str] = []
    latencies: list[float] = []
    hits = 0
    misses = 0
    for _ in range(requests):
        start = time.perf_counter()
        value, hit = call()
        latencies.append(time.perf_counter() - start)
        values.append(value)
        if hit is not None:
            hits += hit
            misses += not hit
    return {"values": values, "hits": hits, "misses": misses, "latency_ms": _latency_summary(latencies)}


def _phase(mode: str, requests: int, cache: str, origin: str, key: str) -> dict[str, Any]:
    """한 단계를 원본 호출 델타와 함께 측정하고 집계를 검증한다."""
    call = (lambda: _direct(origin, key)) if mode == "direct" else (lambda: _cache_aside(cache, origin, key))
    before = _origin_calls(origin)
    measured = _measure(requests, call)
    origin_calls = _origin_calls(origin) - before
    _check_phase(
        mode,
        values=measured["values"],
        hits=measured["hits"],
        misses=measured["misses"],
        origin_calls=origin_calls,
        requests=requests,
    )
    return {
        "requests": requests,
        "http_requests": requests + measured["misses"] * 2,
        "hits": measured["hits"],
        "misses": measured["misses"],
        "origin_calls": origin_calls,
        "latency_ms": measured["latency_ms"],
        "value": measured["values"][0],
    }


def _cleanup(cache: str, keys: list[str]) -> None:
    """이번 실행이 만든 캐시 키만 지운다. 실패는 stderr 경고로 남긴다."""
    for key in keys:
        try:
            payload = _client._post(cache, "/delete", {"key": _client._cache_key(key)})
            if not isinstance(payload.get("result"), bool):
                raise _Error("cache /delete response has no boolean result")
        except _Error as exc:
            print(f"cleanup failed for {key!r}: {exc}", file=sys.stderr, flush=True)


def _self_check() -> int:
    """서버 없이 집계·분기 검증을 확인한다."""
    checks = 0
    for mode, hits, misses, calls in (
        ("direct", 0, 0, 3),
        ("cache_cold", 2, 1, 1),
        ("cache_warm", 3, 0, 0),
    ):
        _check_phase(mode, values=["v"] * 3, hits=hits, misses=misses, origin_calls=calls, requests=3)
        checks += 1
    for mode, hits, misses, calls in (
        ("direct", 0, 0, 2),
        ("cache_cold", 3, 0, 1),
        ("cache_warm", 2, 1, 0),
    ):
        try:
            _check_phase(mode, values=["v"] * 3, hits=hits, misses=misses, origin_calls=calls, requests=3)
        except ValueError:
            checks += 1
        else:
            raise AssertionError(f"self-check accepted invalid {mode} aggregation")
    try:
        _check_phase("cache_warm", values=["a", "b", "c"], hits=3, misses=0, origin_calls=0, requests=3)
    except ValueError:
        checks += 1
    else:
        raise AssertionError("self-check accepted unequal results")
    try:
        _latency_summary([])
    except ValueError:
        checks += 1
    else:
        raise AssertionError("self-check accepted an empty latency sample")
    if _latency_summary([0.001, 0.003]) != {"total_ms": 4.0, "mean_ms": 2.0, "min_ms": 1.0, "max_ms": 3.0}:
        raise AssertionError("self-check latency summary changed")
    checks += 1
    measured = _measure(3, lambda: ("v", None))
    _check_phase(
        "direct", values=measured["values"], hits=measured["hits"],
        misses=measured["misses"], origin_calls=3, requests=3,
    )
    measured = _measure(3, lambda: ("v", True))
    _check_phase(
        "cache_warm", values=measured["values"], hits=measured["hits"],
        misses=measured["misses"], origin_calls=0, requests=3,
    )
    checks += 2
    print(json.dumps({"self_check": "ok", "checks": checks}), flush=True)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="benchmark", description="캐시 사용·미사용 실제 HTTP 비교(D3)")
    parser.add_argument("--cache", help="캐시 서버 URL")
    parser.add_argument("--origin", help="원본 API URL")
    parser.add_argument("--requests", type=int, default=50, help="단계별 측정 요청 수")
    parser.add_argument("--repeats", type=int, default=1, help="원본 키를 바꿔 반복할 횟수")
    parser.add_argument("--self-check", action="store_true", help="서버 없이 집계 검증만 확인")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """direct·cache cold·cache warm을 같은 키로 비교하고 JSON 한 줄을 출력한다."""
    args = _parser().parse_args(argv)
    if args.self_check:
        try:
            return _self_check()
        except (AssertionError, ValueError) as exc:
            print(f"self-check failed: {exc}", file=sys.stderr, flush=True)
            return 1
    if not args.cache or not args.origin:
        _parser().error("--cache and --origin are required")
    if args.requests < 1:
        _parser().error("--requests must be at least 1")
    if args.repeats < 1:
        _parser().error("--repeats must be at least 1")

    run_id = uuid.uuid4().hex[:12]
    created: list[str] = []
    repeats: list[dict[str, Any]] = []
    try:
        for index in range(args.repeats):
            key = f"bench-{run_id}-{index}"
            created.append(key)
            phases = {
                mode: _phase(mode, args.requests, args.cache, args.origin, key)
                for mode in ("direct", "cache_cold", "cache_warm")
            }
            for mode in ("direct", "cache_cold", "cache_warm"):
                if phases[mode]["value"] != phases["direct"]["value"]:
                    raise ValueError(f"{mode}: result differs from direct for key {key!r}")
            value = phases["direct"]["value"]
            repeats.append(
                {
                    "key": key,
                    "payload_bytes": len(value.encode("utf-8")),
                    "value": value,
                    "phases": phases,
                }
            )
    except (ValueError, _Error) as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1
    finally:
        _cleanup(args.cache, created)

    origin_calls = {
        "direct": sum(item["phases"]["direct"]["origin_calls"] for item in repeats),
        "cache": sum(
            item["phases"]["cache_cold"]["origin_calls"] + item["phases"]["cache_warm"]["origin_calls"]
            for item in repeats
        ),
    }
    result = {
        "tool": TOOL,
        "environment": {"python": sys.version.split()[0], "platform": platform.platform()},
        "conditions": {
            "origin": args.origin,
            "cache": args.cache,
            "requests_per_phase": args.requests,
            "repeats": args.repeats,
            "concurrency": CONCURRENCY,
            "cache_ttl_seconds": TTL_SECONDS,
            "payload_bytes": [item["payload_bytes"] for item in repeats],
            "work": "same origin key per repeat; direct=/fetch, cache=/get[, /fetch + /set]",
            "timer": "time.perf_counter",
            "client": "urllib.request (stdlib)",
            "origin_calls": "origin /stats delta per phase",
            "mock_origin_cost": "origin --delay (not set by this tool)",
        },
        "warmup": {
            "requests": 0,
            "note": "cache cold phase primes the cache for cache warm; no request is excluded",
        },
        "repeats": repeats,
        "summary": {"requests": args.requests, "repeats": args.repeats, "origin_calls": origin_calls},
    }
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
