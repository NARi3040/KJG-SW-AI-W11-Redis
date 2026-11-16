"""Store·파일·복구·소비자 통합 사례. 사례 정의: docs/store-and-snapshots.md

실제 Runtime 작업자, 실제 C 시험 빌드 테이블, 실제 snapshot.py로 복구와 스냅샷 저장을 확인한다.
S-T7, S-T8, S-T10, S-T13은 이 파일에서 Runtime 수명으로, S-T6, S-T9는 실제 자식 프로세스의 강제 중단과
파이프 단계 동기화로 확인한다. D1, D2와 재시도 없음 사례는 실제 원본 자식 프로세스와 HTTP로 확인한다.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import logging
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from _runtime_process import Child
from mini_redis import runtime as runtime_module
from mini_redis import snapshot as snapshot_module
from mini_redis.contracts import UnavailableError
from mini_redis.hashtable import HashTable
from mini_redis.http import create_app
from mini_redis.runtime import Runtime
from mini_redis.snapshot import read_snapshot, write_snapshot

ROOT = Path(__file__).resolve().parents[1]


def _join(instance: Runtime) -> int:
    """작업자가 멈출 때까지 기다리고 종료 코드를 반환한다."""
    worker = instance._worker
    assert worker is not None
    worker.join(5)
    assert not worker.is_alive(), "worker did not stop"
    return instance._exit_code


def _wait_until(predicate, timeout: float = 5.0) -> None:
    """조건이 참이 될 때까지 제한 시간 안에서 폴링한다."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition timeout")
        time.sleep(0.01)


def _post(port: int, path: str, payload: dict) -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(
            "POST",
            path,
            body=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def _normal_stop(child: Child) -> None:
    child.normal_stop()


def _start(data_dir, *, clock, monotonic) -> Runtime:
    """Runtime을 시작해 복구를 마치고 활성화한다."""
    instance = Runtime(data_dir, clock=clock, monotonic=monotonic)
    instance.start()
    instance.wait_ready()
    instance.activate()
    return instance


def _warnings(caplog, logger_name: str) -> list[logging.LogRecord]:
    """지정한 로거의 WARNING 레코드를 모은다."""
    return [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and record.name == logger_name
    ]


class _PutRecorder:
    """복구 중 put 호출을 기록한다. super()는 그대로 호출한다."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[tuple[bytes, bytes, float | None]] = []
        self.fail = False

    def record(self, key, value, expires_at) -> None:
        with self.lock:
            self.calls.append((key, value, expires_at))

    def snapshot(self) -> list[tuple[bytes, bytes, float | None]]:
        with self.lock:
            return list(self.calls)


def _recording_table(monkeypatch) -> _PutRecorder:
    """runtime의 HashTable 이름을 put을 기록하는 하위 클래스로 바꾼다."""
    recorder = _PutRecorder()

    class Recording(HashTable):
        def put(self, key, value, expires_at):
            if recorder.fail:
                raise MemoryError("injected recovery allocation failure")
            recorder.record(key, value, expires_at)
            return super().put(key, value, expires_at)

    monkeypatch.setattr(runtime_module, "HashTable", Recording)
    return recorder


def test_s_t6_changes_lost_after_kill(data_dir):
    """강제 종료 후 마지막 성공 스냅샷 상태로 복구하고, 이후 변경과 삭제는 유실될 수 있다. 복구 시 이미 만료된 값은 제외한다."""
    path = data_dir / "snapshot.json"
    initial = [("A", "old", None), ("expired", "old", 1.0)]
    write_snapshot(path, initial)

    with Child(data_dir, "--freeze-periodic") as child:
        port = child.ready()
        assert _post(port, "/set", {"key": "A", "value": "new"}) == (200, {"result": True})
        assert _post(port, "/set", {"key": "C", "value": "new"}) == (200, {"result": True})
        assert _post(port, "/delete", {"key": "A"}) == (200, {"result": True})
        assert child.kill_wait() != 0

    assert read_snapshot(path) == initial

    with Child(data_dir, "--freeze-periodic") as restarted:
        port = restarted.ready()
        assert _post(port, "/get", {"key": "A"}) == (200, {"result": "old"})
        assert _post(port, "/get", {"key": "C"}) == (200, {"result": None})
        assert _post(port, "/get", {"key": "expired"}) == (200, {"result": None})
        assert restarted.kill_wait() != 0

    assert read_snapshot(path) == initial


@pytest.mark.parametrize(
    "flag,stage",
    [("--gate-before-replace", "before_replace "), ("--gate-after-replace", "after_replace ")],
    ids=["before-replace", "after-replace"],
)
def test_s_t9_kill_during_and_after_replace(data_dir, flag, stage):
    """기록 중 중단은 이전 정식 파일, 교체 완료 뒤 중단은 새 정식 파일로 복구한다."""
    path = data_dir / "snapshot.json"
    write_snapshot(path, [("old", "1", None)])
    previous_bytes = path.read_bytes()
    with Child(data_dir, flag, "--freeze-periodic") as child:
        port = child.ready()
        assert _post(port, "/set", {"key": "k", "value": "new"}) == (200, {"result": True})
        _normal_stop(child)
        line = child.expect(stage)
        target = Path(line.split(" ", 1)[1])
        assert child.kill_wait() != 0

    if flag == "--gate-before-replace":
        assert path.read_bytes() == previous_bytes
        assert target.exists()
    else:
        assert target == path
        assert path.read_bytes() != previous_bytes
        assert list(data_dir.glob("snapshot.json.*.tmp")) == []
        assert sorted(read_snapshot(path)) == [("k", "new", None), ("old", "1", None)]

    with Child(data_dir, "--freeze-periodic") as restarted:
        port = restarted.ready()
        if flag == "--gate-before-replace":
            assert _post(port, "/get", {"key": "old"}) == (200, {"result": "1"})
            assert _post(port, "/get", {"key": "k"}) == (200, {"result": None})
        else:
            assert _post(port, "/get", {"key": "old"}) == (200, {"result": "1"})
            assert _post(port, "/get", {"key": "k"}) == (200, {"result": "new"})
        assert restarted.kill_wait() != 0

    if flag == "--gate-before-replace":
        assert path.read_bytes() == previous_bytes
        assert target.exists()


@pytest.mark.parametrize("restore_at", [107.25, 110.25], ids=["before-expiry", "at-expiry"])
def test_s_t7_restore_keeps_absolute_expiry(data_dir, epoch_clock, monotonic_clock, monkeypatch, caplog, restore_at):
    """E=110.25를 t_restore=107.25에서는 원래 만료 시각까지 유지하고, 110.25에서는 제외한다. 무기한 항목은 유지한다."""
    path = data_dir / "snapshot.json"
    write_snapshot(path, [("keep", "v", 110.25), ("forever", "w", None)])
    recorder = _recording_table(monkeypatch)
    epoch_clock.set(restore_at)

    instance = _start(data_dir, clock=epoch_clock, monotonic=monotonic_clock)
    try:
        puts = recorder.snapshot()
        assert (b"forever", b"w", None) in puts
        if restore_at == 107.25:
            assert (b"keep", b"v", 110.25) in puts
            assert instance.submit("get", key="keep").result(5) == "v"
            epoch_clock.set(110.249)
            assert instance.submit("get", key="keep").result(5) == "v"
            epoch_clock.set(110.25)
            assert instance.submit("get", key="keep").result(5) is None
        else:
            # 복구 시각에 이미 만료라 테이블에 들어가지 않는다. 들어간 뒤 만료된 것과 구별한다.
            assert all(key != b"keep" for key, _, _ in puts)
            assert instance.submit("get", key="keep").result(5) is None
        assert instance.submit("get", key="forever").result(5) == "w"
        assert instance.state == "RUNNING"
    finally:
        instance.request_stop(final_snapshot=False)
        assert _join(instance) == 0


def _fail_first_snapshot_stage(monkeypatch, stage: str) -> None:
    """실제 write_snapshot의 첫 시도만 지정한 표준 라이브러리 호출 지점에서 OSError를 내게 한다."""
    state = {"failed": False}

    def should_fail() -> bool:
        if state["failed"]:
            return False
        state["failed"] = True
        return True

    if stage in ("write", "flush"):
        real_fdopen = snapshot_module.os.fdopen

        def fdopen(fd, *args, **kwargs):
            handle = real_fdopen(fd, *args, **kwargs)
            if should_fail():
                def fail(*_args, **_kwargs):
                    raise OSError(f"injected {stage} failure")

                setattr(handle, stage, fail)
            return handle

        monkeypatch.setattr(snapshot_module.os, "fdopen", fdopen)
    else:
        real = getattr(snapshot_module.os, stage)

        def flaky(*args, **kwargs):
            if should_fail():
                raise OSError(f"injected {stage} failure")
            return real(*args, **kwargs)

        monkeypatch.setattr(snapshot_module.os, stage, flaky)


@pytest.mark.parametrize("stage", ["write", "flush", "fsync", "replace"])
def test_s_t8_snapshot_failure_keeps_service(data_dir, epoch_clock, monotonic_clock, monkeypatch, caplog, stage):
    """JSON 기록, flush, fsync, os.replace 실패 각각 뒤에도 메모리 서비스는 계속하고 다음 정기 시도에서 다시 저장한다."""
    path = data_dir / "snapshot.json"
    write_snapshot(path, [("old", "1", None)])
    previous_bytes = path.read_bytes()

    _fail_first_snapshot_stage(monkeypatch, stage)

    real_write = runtime_module.write_snapshot
    attempts: list[int] = []
    condition = threading.Condition()

    def counting_write(snapshot_path, entries):
        try:
            return real_write(snapshot_path, entries)
        finally:
            with condition:
                attempts.append(len(attempts) + 1)
                condition.notify_all()

    monkeypatch.setattr(runtime_module, "write_snapshot", counting_write)

    def wait_attempt(count: int) -> None:
        with condition:
            assert condition.wait_for(lambda: len(attempts) >= count, timeout=5), "no snapshot attempt"

    instance = _start(data_dir, clock=epoch_clock, monotonic=monotonic_clock)
    try:
        assert instance.submit("set", key="k", value="v").result(5) is True

        # 첫 스냅샷 기한은 활성화 시각 1000 + 60이다.
        assert instance._snapshot_deadline == 1060.0
        with caplog.at_level(logging.WARNING, logger="mini_redis.runtime"):
            monotonic_clock.set(1060.0)
            wait_attempt(1)

            assert path.read_bytes() == previous_bytes
            assert list(data_dir.glob("snapshot.json.*.tmp")) == []
            assert instance.submit("get", key="k").result(5) == "v"
            assert instance.state == "RUNNING"

            failures = [
                record.getMessage()
                for record in _warnings(caplog, "mini_redis.runtime")
                if "snapshot failed" in record.getMessage()
            ]
            assert len(failures) == 1
            assert "v" not in failures[0]

            # 실패한 시도도 완료 시각 1060 기준 +60초에서 다음 기한을 잡는다.
            monotonic_clock.set(1120.0)
            wait_attempt(2)

        assert sorted(read_snapshot(path)) == [("k", "v", None), ("old", "1", None)]
        assert instance.submit("get", key="k").result(5) == "v"
        assert instance.state == "RUNNING"
    finally:
        instance.request_stop(final_snapshot=False)
        assert _join(instance) == 0


_INVALID_START_FILES = {
    "truncated-json": b'{"version": 1, "entries": [',
    "version-2": b'{"version": 2, "entries": []}',
    "duplicate-field": (
        b'{"version": 1, "entries": ['
        b'{"key_b64": "aw==", "value_b64": "dg==", "expires_at": null, "expires_at": null}]}'
    ),
    "key-not-string": (
        b'{"version": 1, "entries": ['
        b'{"key_b64": 1, "value_b64": "dg==", "expires_at": null}]}'
    ),
    "expires-at-bool": (
        b'{"version": 1, "entries": ['
        b'{"key_b64": "aw==", "value_b64": "dg==", "expires_at": true}]}'
    ),
    "duplicate-key-after-decode": (
        b'{"version": 1, "entries": ['
        b'{"key_b64": "aw==", "value_b64": "dg==", "expires_at": null},'
        b'{"key_b64": "aw==", "value_b64": "dw==", "expires_at": null}]}'
    ),
    "invalid-base64": (
        b'{"version": 1, "entries": ['
        b'{"key_b64": "aw", "value_b64": "dg==", "expires_at": null}]}'
    ),
    "expired-entry-bad-field": (
        b'{"version": 1, "entries": ['
        b'{"key_b64": "aw==", "value_b64": "dg==", "expires_at": 1.0, "extra": 1}]}'
    ),
}


@pytest.mark.parametrize("content", list(_INVALID_START_FILES.values()), ids=list(_INVALID_START_FILES))
def test_s_t10_corrupt_snapshot_fails_start(
    data_dir, epoch_clock, monotonic_clock, monkeypatch, caplog, content
):
    """손상, 형식, 권한 오류는 요청 수신 전 시작 실패이며 정식 파일을 보존하고 자동 덮어쓰기를 하지 않는다."""
    path = data_dir / "snapshot.json"
    path.write_bytes(content)
    writes: list[bytes] = []

    def counting_write(snapshot_path, entries):
        writes.append(snapshot_path.read_bytes() if snapshot_path.exists() else b"")
        raise AssertionError("write_snapshot must not run on a failed start")

    monkeypatch.setattr(runtime_module, "write_snapshot", counting_write)

    instance = Runtime(data_dir, clock=epoch_clock, monotonic=monotonic_clock)
    instance.start()
    try:
        with pytest.raises(ValueError):
            instance.wait_ready()
    finally:
        instance.request_stop(final_snapshot=False)
        assert _join(instance) == 1

    assert instance.state == "FAILED"
    with pytest.raises(UnavailableError):
        instance.submit("get", key="k")
    assert path.read_bytes() == content
    assert writes == []
    assert list(data_dir.glob("snapshot.json.*.tmp")) == []


@pytest.mark.parametrize("error", [PermissionError, OSError], ids=["permission", "read-error"])
def test_s_t10_read_error_fails_start(data_dir, epoch_clock, monotonic_clock, monkeypatch, error):
    """권한 오류와 그 밖의 읽기 오류는 원래 OSError로 시작을 실패시키고 정식 파일을 보존한다."""
    path = data_dir / "snapshot.json"
    valid = b'{"version": 1, "entries": []}'
    path.write_bytes(valid)
    writes: list = []

    def raise_error(self):
        raise error("injected")

    monkeypatch.setattr(type(path), "read_bytes", raise_error)
    monkeypatch.setattr(runtime_module, "write_snapshot", lambda *a, **k: writes.append(a))

    instance = Runtime(data_dir, clock=epoch_clock, monotonic=monotonic_clock)
    instance.start()
    try:
        with pytest.raises(error) as raised:
            instance.wait_ready()
        assert type(raised.value) is error
    finally:
        instance.request_stop(final_snapshot=False)
        assert _join(instance) == 1

    assert instance.state == "FAILED"
    with pytest.raises(UnavailableError):
        instance.submit("get", key="k")
    monkeypatch.undo()
    assert path.read_bytes() == valid
    assert writes == []
    assert list(data_dir.glob("snapshot.json.*.tmp")) == []


def test_s_t10_recovery_allocation_failure(data_dir, epoch_clock, monotonic_clock, monkeypatch):
    """복구 중 할당 실패는 MemoryError로 시작을 실패시키고 정식 파일을 보존한다."""
    path = data_dir / "snapshot.json"
    write_snapshot(path, [("k", "v", None)])
    previous_bytes = path.read_bytes()
    recorder = _recording_table(monkeypatch)
    recorder.fail = True
    writes: list = []
    monkeypatch.setattr(runtime_module, "write_snapshot", lambda *a, **k: writes.append(a))

    instance = Runtime(data_dir, clock=epoch_clock, monotonic=monotonic_clock)
    instance.start()
    try:
        with pytest.raises(MemoryError):
            instance.wait_ready()
    finally:
        instance.request_stop(final_snapshot=False)
        assert _join(instance) == 1

    assert instance.state == "FAILED"
    with pytest.raises(UnavailableError):
        instance.submit("get", key="k")
    assert path.read_bytes() == previous_bytes
    assert writes == []
    assert list(data_dir.glob("snapshot.json.*.tmp")) == []


def test_s_t10_temp_file_only_starts_empty(data_dir, epoch_clock, monotonic_clock, caplog):
    """임시 파일만 있으면 경고 후 빈 시작이며 임시 파일을 그대로 둔다."""
    path = data_dir / "snapshot.json"
    leftover = data_dir / "snapshot.json.x.tmp"
    leftover.write_bytes(b'{"version": 1, "entr')

    with caplog.at_level(logging.WARNING, logger="mini_redis.snapshot"):
        instance = _start(data_dir, clock=epoch_clock, monotonic=monotonic_clock)
        try:
            assert instance.submit("get", key="k").result(5) is None
            assert instance.state == "RUNNING"
        finally:
            instance.request_stop(final_snapshot=False)
            assert _join(instance) == 0

    warnings = _warnings(caplog, "mini_redis.snapshot")
    assert len(warnings) == 1
    assert "snapshot.json.x.tmp" in warnings[0].getMessage()
    assert leftover.read_bytes() == b'{"version": 1, "entr'
    assert not path.exists()


def test_s_t13_surrogate_roundtrip_restart(data_dir, epoch_clock, monotonic_clock):
    r"""보충 문자 1개인 "\U0001F600"과 surrogate 2개인 "\ud83d\ude00"을 재시작 왕복 후에도 서로 다른 문자열로 구별해 복구한다."""
    lone = chr(0x1F600)
    pair = chr(0xD83D) + chr(0xDE00)
    assert lone != pair and len(lone) == 1 and len(pair) == 2

    first = _start(data_dir, clock=epoch_clock, monotonic=monotonic_clock)
    try:
        assert first.submit("set", key=lone, value="one").result(5) is True
        assert first.submit("set", key=pair, value="two").result(5) is True
        assert first.submit("set", key=lone, value=pair).result(5) is True
        assert first.submit("set", key=pair, value=lone).result(5) is True
    finally:
        first.request_stop(final_snapshot=True)
        assert _join(first) == 0

    stored = read_snapshot(data_dir / "snapshot.json")
    assert sorted(stored, key=lambda entry: len(entry[0])) == [
        (lone, pair, None),
        (pair, lone, None),
    ]

    second = _start(data_dir, clock=epoch_clock, monotonic=monotonic_clock)
    try:
        assert second.submit("get", key=lone).result(5) == pair
        assert second.submit("get", key=pair).result(5) == lone
    finally:
        second.request_stop(final_snapshot=False)
        assert _join(second) == 0


def _load_demo(name: str):
    """demo는 패키지가 아니므로 파일 경로로 불러온다."""
    spec = importlib.util.spec_from_file_location(f"demo_{name}", ROOT / "demo" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def _origin_process():
    """원본 API를 자식 프로세스로 띄우고 첫 줄에서 실제 포트를 읽는다."""
    process = subprocess.Popen(
        [sys.executable, "demo/origin.py", "--port", "0"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    line_holder: dict[str, str] = {}
    ready = threading.Event()

    def read_first_line() -> None:
        line = process.stdout.readline() if process.stdout is not None else ""
        line_holder["line"] = line
        ready.set()

    reader = threading.Thread(target=read_first_line, daemon=True)
    reader.start()
    try:
        assert ready.wait(5), "origin did not print its listening line"
        line = line_holder["line"].strip()
        assert line.startswith("origin listening on http://"), line
        url = line.split(" ", 3)[3]
        yield url
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


@contextmanager
def _cache_server(data_dir, *, clock, monotonic):
    """캐시 서버를 start → wait_ready → bind → activate → serve 순서로 띄운다."""
    instance = Runtime(data_dir, clock=clock, monotonic=monotonic)
    instance.start()
    instance.wait_ready()
    server = None
    server_thread = None
    try:
        server = make_server("127.0.0.1", 0, create_app(instance), threaded=True)
        instance.activate()
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        instance.request_stop(final_snapshot=False)
        if server_thread is not None:
            server.shutdown()
            server_thread.join(5)
        if server is not None:
            server.server_close()
        _join(instance)


def _origin_calls(origin: str) -> int:
    """원본의 /fetch 호출 횟수를 읽는다."""
    with urllib.request.urlopen(urllib.request.Request(origin + "/stats"), timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))["calls"]


def _origin_post(origin: str, path: str, body: dict) -> dict:
    """원본에 JSON POST 요청을 보낸다."""
    request = urllib.request.Request(
        origin + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _closed_port() -> int:
    """곧바로 닫힌 로컬 포트 번호를 구한다."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _read_single_line(capsys) -> dict:
    """소비자가 출력한 한 줄 JSON을 읽는다."""
    out = capsys.readouterr()
    lines = out.out.splitlines()
    assert len(lines) == 1, out.out
    return json.loads(lines[0])


def test_d1_cache_reuse(tmp_path, epoch_clock, monotonic_clock, capsys):
    """TTL 내 재요청은 저장 결과를 재사용하고 원본 호출 횟수가 줄어든다."""
    client = _load_demo("client")
    with _origin_process() as origin, _cache_server(
        tmp_path / "cache", clock=epoch_clock, monotonic=monotonic_clock
    ) as cache:
        args = ["--cache", cache, "--origin", origin, "get", "k"]

        assert client.main(args) == 0
        first = _read_single_line(capsys)
        assert first == {"key": "k", "value": "k:v1", "source": "origin"}

        assert client.main(args) == 0
        second = _read_single_line(capsys)
        assert second == {"key": "k", "value": "k:v1", "source": "cache"}
        assert first["value"] == second["value"]
        assert _origin_calls(origin) == 1


def test_d2_explicit_invalidation(tmp_path, epoch_clock, monotonic_clock, capsys):
    """원본 결과 변경 뒤 명시적 삭제 후 재요청은 이전 캐시 값을 쓰지 않고 새 원본 결과를 저장한다."""
    client = _load_demo("client")
    with _origin_process() as origin, _cache_server(
        tmp_path / "cache", clock=epoch_clock, monotonic=monotonic_clock
    ) as cache:
        base = ["--cache", cache, "--origin", origin]

        assert client.main([*base, "get", "k"]) == 0
        assert _read_single_line(capsys) == {"key": "k", "value": "k:v1", "source": "origin"}

        assert _origin_post(origin, "/update", {"key": "k"}) == {"key": "k", "version": 2}

        # TTL 안이라 원본이 바뀌어도 오래된 캐시 값을 그대로 재사용한다.
        assert client.main([*base, "get", "k"]) == 0
        assert _read_single_line(capsys) == {"key": "k", "value": "k:v1", "source": "cache"}

        assert client.main([*base, "invalidate", "k"]) == 0
        assert _read_single_line(capsys) == {"key": "k", "deleted": True}

        assert client.main([*base, "get", "k"]) == 0
        assert _read_single_line(capsys) == {"key": "k", "value": "k:v2", "source": "origin"}
        assert _origin_calls(origin) == 2

        assert client.main([*base, "get", "k"]) == 0
        assert _read_single_line(capsys) == {"key": "k", "value": "k:v2", "source": "cache"}
        assert _origin_calls(origin) == 2


def test_d1_no_retry_when_cache_unavailable(capsys):
    """캐시가 닫혀 있으면 종료 코드 1이고 원본을 부르지 않으며 자동 재시도도 하지 않는다."""
    client = _load_demo("client")
    with _origin_process() as origin:
        cache = f"http://127.0.0.1:{_closed_port()}"

        assert client.main(["--cache", cache, "--origin", origin, "get", "k"]) == 1

        captured = capsys.readouterr()
        assert captured.out == ""
        assert len(captured.err.strip().splitlines()) == 1
        assert _origin_calls(origin) == 0


@contextmanager
def _raw_http_server(response: bytes):
    """요청 하나를 끝까지 읽고 미리 정한 바이트를 그대로 보낸 뒤 연결을 닫는 시험용 서버."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve() -> None:
        try:
            connection, _ = listener.accept()
        except OSError:
            return
        with connection:
            connection.settimeout(5)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = connection.recv(4096)
                if not chunk:
                    return
                data += chunk
            head, _, body = data.partition(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
            while len(body) < length:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                body += chunk
            connection.sendall(response)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        listener.close()
        thread.join(5)


_BAD_CACHE_RESPONSES = {
    # Content-Length보다 짧게 보내고 닫으면 urllib가 http.client.IncompleteRead를 낸다.
    "truncated": b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{}",
    # UTF-8이 아닌 본문은 JSONDecodeError가 아니라 UnicodeDecodeError를 낸다.
    "invalid-utf8": b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 1\r\n\r\n\xff",
}


@pytest.mark.parametrize("response", list(_BAD_CACHE_RESPONSES.values()), ids=list(_BAD_CACHE_RESPONSES))
def test_d1_client_bad_cache_response_exits_1(capsys, response):
    """잘린 응답과 UTF-8이 아닌 응답도 예외로 터지지 않고 stderr 한 줄과 종료 코드 1로 끝난다."""
    client = _load_demo("client")
    origin = f"http://127.0.0.1:{_closed_port()}"
    with _raw_http_server(response) as cache:
        assert client.main(["--cache", cache, "--origin", origin, "get", "k"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.strip().splitlines()) == 1


def test_d1_client_escapes_unpaired_surrogate(capsys):
    """캐시 값에 단독 surrogate가 있어도 ASCII 이스케이프한 한 줄 JSON으로 출력한다."""
    client = _load_demo("client")
    origin = f"http://127.0.0.1:{_closed_port()}"
    body = b'{"result": "\\ud800"}'
    response = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )
    with _raw_http_server(response) as cache:
        assert client.main(["--cache", cache, "--origin", origin, "get", "k"]) == 0

    out = capsys.readouterr().out
    assert out.isascii()
    assert _read_single_line_text(out) == {"key": "k", "value": "\ud800", "source": "cache"}


def _read_single_line_text(text: str) -> dict:
    """한 줄 JSON 출력 문자열을 읽는다."""
    lines = text.splitlines()
    assert len(lines) == 1, text
    return json.loads(lines[0])


@pytest.mark.parametrize("path", ["/fetch", "/update"])
@pytest.mark.parametrize(
    "content_type",
    ["application/problem+json", "text/plain", None],
    ids=["problem-json", "text-plain", "missing"],
)
def test_d2_origin_rejects_non_json_media_type(path, content_type):
    """원본은 Content-Type이 정확히 application/json이 아니면 본문이 JSON이어도 400이고 상태를 바꾸지 않는다."""
    origin = _load_demo("origin")
    client = origin.create_app().test_client()
    headers = {} if content_type is None else {"Content-Type": content_type}

    response = client.post(path, data=json.dumps({"key": "k"}), headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "invalid_input"}
    assert client.get("/stats").get_json() == {"calls": 0}
    # 같은 본문을 application/json으로 보내면 받아들인다. 거부된 /update는 버전을 올리지 않았다.
    accepted = client.post("/fetch", json={"key": "k"})
    assert accepted.status_code == 200
    assert accepted.get_json() == {"key": "k", "value": "k:v1", "version": 1}


def test_d2_demo_through_controller_process(data_dir, capsys):
    """실제 컨트롤러(mini_redis.__main__) 프로세스를 캐시 서버로 써도 D1·D2 흐름이 같고, 정상 종료하면 코드 0이다."""
    client = _load_demo("client")
    with _origin_process() as origin, Child(data_dir) as server:
        port = server.ready()
        base = ["--cache", f"http://127.0.0.1:{port}", "--origin", origin]

        assert client.main([*base, "get", "k"]) == 0
        assert _read_single_line(capsys) == {"key": "k", "value": "k:v1", "source": "origin"}
        assert client.main([*base, "get", "k"]) == 0
        assert _read_single_line(capsys) == {"key": "k", "value": "k:v1", "source": "cache"}
        assert _origin_calls(origin) == 1

        assert _origin_post(origin, "/update", {"key": "k"}) == {"key": "k", "version": 2}
        assert client.main([*base, "get", "k"]) == 0
        assert _read_single_line(capsys) == {"key": "k", "value": "k:v1", "source": "cache"}
        assert client.main([*base, "invalidate", "k"]) == 0
        assert _read_single_line(capsys) == {"key": "k", "deleted": True}
        assert client.main([*base, "get", "k"]) == 0
        assert _read_single_line(capsys) == {"key": "k", "value": "k:v2", "source": "origin"}
        assert _origin_calls(origin) == 2

        server.normal_stop()
        assert server.wait() == 0

    # 정상 종료의 최종 저장에 소비자가 마지막으로 넣은 값이 남는다.
    assert [(key, value) for key, value, _ in read_snapshot(data_dir / "snapshot.json")] == [("origin:k", "k:v2")]


def _first_stdout_line(process: subprocess.Popen, timeout: float = 5.0) -> str:
    """자식의 첫 stdout 줄을 제한 시간 안에서 읽는다. 늦으면 정리 블록이 처리하도록 예외를 낸다."""
    holder: dict[str, str] = {}
    ready = threading.Event()

    def read_line() -> None:
        holder["line"] = process.stdout.readline() if process.stdout is not None else ""
        ready.set()

    reader = threading.Thread(target=read_line, daemon=True)
    reader.start()
    assert ready.wait(timeout), "origin did not print its listening line"
    return holder["line"]


def test_d2_origin_normal_stop_exits_0():
    """원본 API는 Ctrl+Break(Windows)나 SIGTERM으로도 정상 종료해 종료 코드 0을 돌려준다."""
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    process = subprocess.Popen(
        [sys.executable, "demo/origin.py", "--port", "0"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        creationflags=creationflags,
    )
    try:
        assert _first_stdout_line(process).startswith("origin listening on http://")
        process.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM)
        assert process.wait(10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(5)
        if process.stdout is not None:
            process.stdout.close()
