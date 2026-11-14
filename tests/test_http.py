"""HTTP-only contract checks with fixed results, not a storage engine."""

import importlib
import json
import socket
import threading
from concurrent.futures import Future

import pytest

from mini_redis import http
from mini_redis.contracts import BusyError, UnavailableError, WorkerFailedError


class RuntimeDouble:
    def __init__(self, result=True, *, error=None, phase="result"):
        self.result = result
        self.error = error
        self.phase = phase
        self.calls = []

    def submit(self, operation, **fields):
        self.calls.append((operation, fields))
        if self.error is not None and self.phase == "submit":
            raise self.error
        future = Future()
        if self.error is not None:
            future.set_exception(self.error)
        else:
            future.set_result(self.result)
        return future


@pytest.fixture
def api():
    runtime = RuntimeDouble()
    app = http.create_app(runtime)
    app.testing = True
    return app.test_client(), runtime


def assert_error(response, status, code):
    assert response.status_code == status
    assert response.mimetype == "application/json"
    assert response.get_json() == {"error": {"code": code}}
    assert response.data.isascii()
    assert "Retry-After" not in response.headers


def reject(api, path, *, payload=None, data=None, content_type="application/json", status=400, code="invalid_input"):
    client, runtime = api
    assert client.post("/set", json={"key": "k", "value": "old", "ttl": 10}).status_code == 200
    before = runtime.calls.copy()
    response = client.post(
        path,
        data=json.dumps(payload) if data is None else data,
        content_type=content_type,
    )
    assert_error(response, status, code)
    assert runtime.calls == before


@pytest.mark.parametrize("path,payload", [
    ("/set", {"key": 1, "value": "v"}),
    ("/set", {"key": "k", "value": 1}),
    ("/set", {"key": "k"}),
    ("/set", {"value": "v"}),
    ("/get", {"key": 1}),
    ("/delete", {"key": 1}),
    ("/get", {}),
    ("/delete", {}),
])
def test_t13_invalid_input_no_submission(api, path, payload):
    reject(api, path, payload=payload)


@pytest.mark.parametrize("ttl", [1, 2_147_483_647])
def test_t14_ttl_bounds(api, ttl):
    client, runtime = api
    response = client.post("/set", json={"key": "k", "value": "v", "ttl": ttl})
    assert response.status_code == 200
    assert runtime.calls == [("set", {"key": "k", "value": "v", "ttl": ttl})]


@pytest.mark.parametrize("ttl", [0, -1, 2_147_483_648, 1.5, "10", True, False, None])
def test_t14_h3_invalid_ttl(api, ttl):
    reject(api, "/set", payload={"key": "k", "value": "v", "ttl": ttl})


def test_t21_invalid_request_for_expired_entry_no_submission(api):
    # HTTP-only: no clock or real C state is modeled or asserted here.
    reject(api, "/set", payload={"key": "k", "value": "new", "ttl": 0})


def test_h1_roundtrip(api):
    client, runtime = api
    for path, payload, result, fields in [
        ("/set", {"key": "k", "value": "v", "ttl": 10}, True, {"key": "k", "value": "v", "ttl": 10}),
        ("/get", {"key": "k"}, "v", {"key": "k"}),
        ("/delete", {"key": "k"}, True, {"key": "k"}),
        ("/delete", {"key": "k"}, False, {"key": "k"}),
    ]:
        runtime.result = result
        response = client.post(path, json=payload)
        assert response.status_code == 200
        assert response.get_json() == {"result": result}
        assert runtime.calls[-1] == (path[1:], fields)
    assert len(runtime.calls) == 4


def test_h2_empty_value_and_miss(api):
    client, runtime = api
    assert client.post("/set", json={"key": "", "value": ""}).get_json() == {"result": True}
    assert runtime.calls == [("set", {"key": "", "value": "", "ttl": None})]
    for key, result in [("", ""), ("missing", None)]:
        runtime.result = result
        response = client.post("/get", json={"key": key})
        assert response.status_code == 200
        assert response.get_json() == {"result": result}
        assert runtime.calls[-1] == ("get", {"key": key})


def test_h3_omitted_ttl(api):
    client, runtime = api
    for payload in [{"key": "k", "value": "old", "ttl": 10}, {"key": "k", "value": "new"}]:
        response = client.post("/set", json=payload)
        assert response.status_code == 200
        assert response.get_json() == {"result": True}
    assert runtime.calls[-1] == ("set", {"key": "k", "value": "new", "ttl": None})


@pytest.mark.parametrize("path,payload", [
    (path, payload)
    for path in ["/set", "/get", "/delete"]
    for payload in [
        {}, {"key": None}, {"key": True}, {"key": []}, {"key": {}},
        {"key": "k", "extra": 1},
        {"key": "k", "value": None}, {"key": "k", "value": False},
    ]
] + [
    ("/get", {"key": "k", "ttl": 1}),
    ("/delete", {"key": "k", "ttl": 1}),
    ("/get", {"key": "k", "value": "v"}),
    ("/delete", {"key": "k", "value": "v"}),
    ("/set", {"key": "k", "value": "v", "extra": 1}),
])
def test_h4_missing_types_and_fields(api, path, payload):
    reject(api, path, payload=payload)


@pytest.mark.parametrize("path,data", [
    ("/set", '{"key":"k","key":"other","value":"v"}'),
    ("/set", '{"key":"k","value":"v","value":"other"}'),
    ("/set", '{"key":"k","value":"v","ttl":1,"ttl":2}'),
    ("/get", '{"key":"k","\\u006bey":"other"}'),
    ("/delete", '{"key":"k","key":"k"}'),
])
def test_h4_duplicate_fields(api, path, data):
    reject(api, path, data=data)


@pytest.mark.parametrize("data", [
    "", '{"key":', "[]", '"text"', "null", "1", "true",
    '{"key":"k"} trailing',
    '{"key":"k","ttl":NaN}', '{"key":"k","ttl":Infinity}',
    '{"key":"k","ttl":-Infinity}', '{"key":NaN}',
    '{"key":{"nested":Infinity}}',
    b'{"key":"\xff"}',
    '{"key":"k"}'.encode("utf-16"),
    b'{"key":"\xed\xa0\x80"}',
])
def test_h5_json_format(api, data):
    reject(api, "/get", data=data)


def test_h5_deeply_nested_json_no_submission(api):
    reject(api, "/get", data="[" * 10_000 + "0" + "]" * 10_000)


@pytest.mark.parametrize("content_type", [None, "text/plain", "text/json", "application/octet-stream", "application/problem+json"])
def test_h6_media_type(api, content_type):
    reject(api, "/get", data='{"key":"k"}', content_type=content_type,
           status=415, code="unsupported_media_type")


def test_h6_utf8_json_with_parameters(api):
    client, runtime = api
    response = client.post("/get", data='{"key":"한글"}'.encode("utf-8"),
                           content_type="application/json; charset=utf-8")
    assert response.status_code == 200
    assert runtime.calls == [("get", {"key": "한글"})]


@pytest.mark.parametrize("path,method,status,code", [
    ("/unknown", "POST", 404, "not_found"),
    ("/static/secret", "GET", 404, "not_found"),
    ("/set", "GET", 405, "method_not_allowed"),
    ("/get", "PUT", 405, "method_not_allowed"),
    ("/delete", "DELETE", 405, "method_not_allowed"),
    ("/get", "OPTIONS", 405, "method_not_allowed"),
])
def test_h7_routing(api, path, method, status, code):
    client, runtime = api
    assert_error(client.open(path, method=method), status, code)
    assert runtime.calls == []


@pytest.mark.parametrize("error,code", [
    (BusyError("secret"), "busy"),
    (UnavailableError("STARTING secret"), "unavailable"),
    (UnavailableError("STOPPING secret"), "unavailable"),
    (UnavailableError("FAILED secret"), "unavailable"),
    (WorkerFailedError("secret"), "unavailable"),
])
@pytest.mark.parametrize("phase", ["submit", "result"])
def test_h8_busy_and_unavailable(error, code, phase):
    runtime = RuntimeDouble(error=error, phase=phase)
    response = http.create_app(runtime).test_client().post("/get", json={"key": "secret"})
    assert_error(response, 503, code)
    assert len(runtime.calls) == 1


@pytest.mark.parametrize("phase", ["submit", "result"])
def test_h9_memory_failure(phase):
    runtime = RuntimeDouble(error=MemoryError("secret"), phase=phase)
    response = http.create_app(runtime).test_client().post("/get", json={"key": "secret"})
    assert_error(response, 500, "memory_error")
    assert len(runtime.calls) == 1


@pytest.mark.parametrize("error", [
    TypeError("secret key=value C:/private/path"),
    ValueError("secret key=value C:/private/path"),
    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "secret"),
    RuntimeError("secret key=value C:/private/path"),
    RecursionError("secret key=value C:/private/path"),
    Exception("secret key=value C:/private/path"),
])
@pytest.mark.parametrize("phase", ["submit", "result"])
def test_h10_internal_failure(error, phase):
    runtime = RuntimeDouble(error=error, phase=phase)
    app = http.create_app(runtime)
    app.testing = True
    response = app.test_client().post("/set", json={"key": "secret", "value": "secret"})
    assert_error(response, 500, "internal_error")
    assert len(runtime.calls) == 1
    assert b"secret" not in response.data
    assert b"private" not in response.data


@pytest.mark.parametrize("text", ["", " a/A\x00 ", "한글", "😀", "\ud800", "\udfff"])
def test_h11_string_representation(api, text):
    client, runtime = api
    runtime.result = True
    assert client.post("/set", json={"key": text, "value": text}).status_code == 200
    assert runtime.calls[-1] == ("set", {"key": text, "value": text, "ttl": None})
    runtime.result = text
    response = client.post("/get", json={"key": text})
    assert response.status_code == 200
    assert response.mimetype == "application/json"
    assert response.data.isascii()
    assert response.get_json() == {"result": text}
    assert runtime.calls[-1] == ("get", {"key": text})
    runtime.result = True
    assert client.post("/delete", json={"key": text}).get_json() == {"result": True}
    assert runtime.calls[-1] == ("delete", {"key": text})


def test_h12_body_limit(api):
    client, runtime = api
    assert client.application.config["MAX_CONTENT_LENGTH"] is None
    value = "v" * (1024 * 1024)
    response = client.post("/set", json={"key": "large", "value": value})
    assert response.status_code == 200
    assert runtime.calls == [("set", {"key": "large", "value": value, "ttl": None})]


def test_apps_are_isolated():
    first, second = RuntimeDouble("first"), RuntimeDouble("second")
    first_app = http.create_app(first)
    second_app = http.create_app(second)
    for app, expected in [(first_app, "first"), (second_app, "second"), (first_app, "first")]:
        assert app.test_client().post("/get", json={"key": "k"}).get_json() == {"result": expected}
    assert len(first.calls) == 2
    assert len(second.calls) == 1


def test_import_and_app_creation_have_no_external_effects(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("HTTP import/app creation must not start resources")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(threading.Thread, "start", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr("ctypes.CDLL", forbidden)
    importlib.reload(http)
    runtime = RuntimeDouble()
    http.create_app(runtime)
    assert runtime.calls == []
    assert list(tmp_path.iterdir()) == []
