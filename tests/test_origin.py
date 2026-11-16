"""원본 API 단독 시험. C 라이브러리 없이 Flask 앱과 CLI만 확인한다.

명세: docs/c-hashtable-and-bindings.md의 시연과 측정 호출 명세. D1/D2 전체 통합은 test_integration_store.py가 맡는다.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
import types
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_origin():
    """demo는 패키지가 아니므로 파일 경로로 불러온다."""
    spec = importlib.util.spec_from_file_location("demo_origin", ROOT / "demo" / "origin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


origin = _load_origin()


def _app():
    return origin.create_app().test_client()


def test_per_key_versions_fetch_counts_and_update_before_fetch():
    client = _app()

    first = client.post("/fetch", json={"key": "k"})
    assert first.get_json() == {"key": "k", "value": "k:v1", "version": 1}
    # 조회는 버전을 올리지 않고 호출 횟수만 늘린다.
    assert client.post("/fetch", json={"key": "k"}).get_json() == {
        "key": "k", "value": "k:v1", "version": 1,
    }
    assert client.post("/update", json={"key": "k"}).get_json() == {"key": "k", "version": 2}
    assert client.post("/fetch", json={"key": "k"}).get_json() == {
        "key": "k", "value": "k:v2", "version": 2,
    }

    # 한 번도 조회하지 않은 키도 버전 1에서 올라가 2가 되고, 조회 때 1회로 센다.
    assert client.post("/update", json={"key": "m"}).get_json() == {"key": "m", "version": 2}
    assert client.post("/fetch", json={"key": "m"}).get_json() == {
        "key": "m", "value": "m:v2", "version": 2,
    }
    assert client.get("/stats").get_json() == {"calls": 4}


def test_apps_keep_separate_state():
    one, two = _app(), _app()

    one.post("/fetch", json={"key": "k"})
    one.post("/update", json={"key": "k"})

    assert one.get("/stats").get_json() == {"calls": 1}
    assert two.get("/stats").get_json() == {"calls": 0}
    assert two.post("/fetch", json={"key": "k"}).get_json() == {
        "key": "k", "value": "k:v1", "version": 1,
    }


@pytest.mark.parametrize("key", ["", "한글", "a b"], ids=["empty", "unicode", "space"])
def test_empty_and_unicode_keys_are_valid(key):
    client = _app()

    fetched = client.post("/fetch", json={"key": key})

    assert fetched.status_code == 200
    assert fetched.get_json() == {"key": key, "value": f"{key}:v1", "version": 1}
    assert client.post("/update", json={"key": key}).get_json() == {"key": key, "version": 2}


@pytest.mark.parametrize("path", ["/fetch", "/update"])
@pytest.mark.parametrize(
    "body, content_type",
    [
        ("not json", "application/json"),
        ("[1, 2]", "application/json"),
        ('{"other": "k"}', "application/json"),
        ('{"key": 5}', "application/json"),
        ('{"key": "k"}', "text/plain"),
        ('{"key": "k"}', None),
    ],
    ids=["malformed", "not-object", "missing-key", "non-string-key", "text-plain", "no-content-type"],
)
def test_invalid_requests_are_rejected_without_state_change(path, body, content_type):
    client = _app()
    headers = {} if content_type is None else {"Content-Type": content_type}

    response = client.post(path, data=body, headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "invalid_input"}
    assert client.get("/stats").get_json() == {"calls": 0}
    # 거부된 /update가 버전을 올리지 않았는지 유효 조회로 확인한다.
    assert client.post("/fetch", json={"key": "k"}).get_json() == {
        "key": "k", "value": "k:v1", "version": 1,
    }


def test_fetch_delay_comes_from_app_config(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(origin, "time", types.SimpleNamespace(sleep=slept.append))
    app = origin.create_app()
    app.config["ORIGIN_DELAY"] = 0.25
    client = app.test_client()

    response = client.post("/fetch", json={"key": "k"})

    assert response.get_json() == {"key": "k", "value": "k:v1", "version": 1}
    assert slept == [0.25]


def test_default_app_does_not_sleep(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(origin, "time", types.SimpleNamespace(sleep=slept.append))

    _app().post("/fetch", json={"key": "k"})

    assert slept == []


def test_concurrent_updates_are_serialized_by_lock():
    app = origin.create_app()
    versions: list[int] = []
    lock = threading.Lock()

    def update() -> None:
        version = app.test_client().post("/update", json={"key": "k"}).get_json()["version"]
        with lock:
            versions.append(version)

    threads = [threading.Thread(target=update) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert sorted(versions) == list(range(2, 10))
    assert app.test_client().post("/fetch", json={"key": "k"}).get_json()["version"] == 9


def test_negative_delay_is_a_cli_argument_error():
    with pytest.raises(SystemExit) as excinfo:
        origin.main(["--delay", "-0.1"])

    assert excinfo.value.code == 2


def test_cli_serves_on_ephemeral_port():
    """ready 줄의 실제 OS 포트로 HTTP 요청이 되고, 종료는 finally에서 정리한다."""
    process = subprocess.Popen(
        [sys.executable, "demo/origin.py", "--port", "0"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert process.stdout is not None
    ready = threading.Event()
    line_holder: dict[str, str] = {}

    def read_ready_line() -> None:
        line_holder["line"] = process.stdout.readline() if process.stdout is not None else ""
        ready.set()

    threading.Thread(target=read_ready_line, daemon=True).start()
    try:
        assert ready.wait(10), "origin did not print its listening line"
        line = line_holder["line"].strip()
        assert line.startswith("origin listening on http://127.0.0.1:"), line
        url = line.split()[-1]
        assert int(url.rsplit(":", 1)[1]) > 0

        assert _http_post(url, "/fetch", {"key": "k"}) == {"key": "k", "value": "k:v1", "version": 1}
        assert _http_post(url, "/update", {"key": "k"}) == {"key": "k", "version": 2}
        assert _http_post(url, "/fetch", {"key": "k"}) == {"key": "k", "value": "k:v2", "version": 2}
        assert _http_get(url, "/stats") == {"calls": 2}
    finally:
        process.terminate()
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(5)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def _http_post(base: str, path: str, body: dict) -> dict:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _http_get(base: str, path: str) -> dict:
    with urllib.request.urlopen(urllib.request.Request(base + path), timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))
