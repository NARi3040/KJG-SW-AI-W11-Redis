"""실제 HTTP·작업자 통합 사례: docs/http-and-runtime.md.

threaded Werkzeug 서버와 실제 C HashTable을 조립한다. C 원본 메서드를 감싸 생성·호출·close의
스레드 ID와 호출 순서를 기록하며, 대체 저장 엔진은 쓰지 않는다. controller/CLI 조립과 프로세스
신호 종료(R4·R5의 남은 범위)는 #13에서 붙인다.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from werkzeug.serving import make_server

from mini_redis import runtime as runtime_module
from mini_redis import snapshot as snapshot_module
from mini_redis.hashtable import HashTable
from mini_redis.http import create_app
from mini_redis.runtime import Runtime


def _join(instance: Runtime, timeout: float = 5.0) -> int:
    worker = instance._worker
    assert worker is not None
    worker.join(timeout)
    assert not worker.is_alive(), "worker did not stop"
    return instance._exit_code


def _wait_until(predicate, timeout: float = 5.0) -> None:
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


@contextmanager
def _server(data_dir, *, clock=time.time, monotonic=time.monotonic):
    instance = Runtime(data_dir, clock=clock, monotonic=monotonic)
    instance.start()
    instance.wait_ready()
    server = None
    server_thread = None
    try:
        # 복구 완료 → 소켓 바인드 → 활성화 → 수신 루프 순서를 지킨다.
        server = make_server("127.0.0.1", 0, create_app(instance), threaded=True)
        instance.activate()
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        yield instance, server.server_port
    finally:
        instance.request_stop(final_snapshot=False)
        if server_thread is not None:
            server.shutdown()
            server_thread.join(5)
        if server is not None:
            server.server_close()
        _join(instance)


class _Recorder:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.threads: set[int] = set()
        self.calls: list[str] = []
        self.table_cls = None

    def thread(self) -> None:
        with self.lock:
            self.threads.add(threading.get_ident())

    def call(self, name: str) -> None:
        with self.lock:
            self.calls.append(name)


@pytest.fixture
def instrumentation(monkeypatch) -> _Recorder:
    recorder = _Recorder()

    class Instrumented(HashTable):
        gate: threading.Event | None = None
        gated_started: threading.Event | None = None
        put_gate: threading.Event | None = None
        put_started: threading.Event | None = None

        def __init__(self):
            recorder.thread()
            super().__init__()

        def put(self, key, value, expires_at):
            recorder.thread()
            recorder.call("put")
            gate = type(self).put_gate
            if gate is not None:
                type(self).put_started.set()
                assert gate.wait(10), "test put gate timeout"
            return super().put(key, value, expires_at)

        def is_live(self, key, cutoff):
            recorder.thread()
            recorder.call("is_live")
            gate = type(self).gate
            if gate is not None:
                type(self).gated_started.set()
                assert gate.wait(10), "test gate timeout"
            return super().is_live(key, cutoff)

        def get(self, key):
            recorder.thread()
            recorder.call("get")
            return super().get(key)

        def remove(self, key):
            recorder.thread()
            recorder.call("remove")
            return super().remove(key)

        def remove_expired(self, cutoff):
            recorder.thread()
            recorder.call("cleanup")
            return super().remove_expired(cutoff)

        def copy_live(self, cutoff):
            recorder.thread()
            recorder.call("copy")
            return super().copy_live(cutoff)

        def close(self):
            recorder.thread()
            recorder.call("close")
            return super().close()

    monkeypatch.setattr(runtime_module, "HashTable", Instrumented)
    recorder.table_cls = Instrumented
    return recorder


def test_s_t1_shared_state_across_connections(data_dir, instrumentation):
    with _server(data_dir) as (instance, port):
        assert _post(port, "/set", {"key": "k", "value": "v", "ttl": 10}) == (200, {"result": True})
        assert _post(port, "/get", {"key": "k"}) == (200, {"result": "v"})
        assert _post(port, "/delete", {"key": "k"}) == (200, {"result": True})
        assert _post(port, "/get", {"key": "k"}) == (200, {"result": None})
        worker_ident = instance._worker.ident
    assert instrumentation.threads == {worker_ident}


def test_s_t11_serial_periodic_work(data_dir, epoch_clock, monotonic_clock, instrumentation):
    with _server(data_dir, clock=epoch_clock, monotonic=monotonic_clock) as (instance, port):
        table_cls = instrumentation.table_cls
        gate = threading.Event()
        table_cls.gated_started = threading.Event()
        table_cls.gate = gate

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(_post, port, "/get", {"key": "first"})
            assert table_cls.gated_started.wait(5), "worker never reached the gated get"
            monotonic_clock.advance(61)
            second = pool.submit(_post, port, "/set", {"key": "second", "value": "v"})
            _wait_until(lambda: instance._queue.qsize() >= 1)
            instrumentation.calls.clear()
            table_cls.gate = None
            gate.set()
            assert first.result(5) == (200, {"result": None})
            assert second.result(5) == (200, {"result": True})
            calls = list(instrumentation.calls)

        # 청소 → 스냅샷 → 대기 요청 하나 순서이며 주기 작업이 요청을 중첩하지 않는다.
        assert calls.index("cleanup") < calls.index("copy") < calls.index("put")
        assert snapshot_module.read_snapshot(data_dir / "snapshot.json") == []


def test_s_t12_independent_clocks_and_save_failures(
    data_dir, epoch_clock, monotonic_clock, instrumentation, monkeypatch
):
    real_write = snapshot_module.write_snapshot
    attempts: list[float] = []
    condition = threading.Condition()

    def flaky_write(path, entries):
        with condition:
            attempts.append(monotonic_clock())
            condition.notify_all()
        if len(attempts) <= 2:
            raise OSError("injected snapshot failure")
        real_write(path, entries)

    monkeypatch.setattr(runtime_module, "write_snapshot", flaky_write)

    def wait_attempts(count):
        with condition:
            assert condition.wait_for(lambda: len(attempts) >= count, timeout=5), "no attempt"

    with _server(data_dir, clock=epoch_clock, monotonic=monotonic_clock) as (instance, port):
        assert _post(port, "/set", {"key": "ttl", "value": "v", "ttl": 5}) == (200, {"result": True})
        epoch_clock.advance(6)
        # epoch 전진은 조기 만료만 만들고 monotonic 기한과 무관하다.
        assert _post(port, "/get", {"key": "ttl"}) == (200, {"result": None})

        monotonic_clock.advance(60)
        wait_attempts(1)
        _wait_until(lambda: instance._snapshot_deadline == 1120.0)
        assert _post(port, "/get", {"key": "missing"}) == (200, {"result": None})
        monotonic_clock.advance(60)
        wait_attempts(2)
        _wait_until(lambda: instance._snapshot_deadline == 1180.0)
        monotonic_clock.advance(60)
        wait_attempts(3)
        assert attempts == pytest.approx([1060.0, 1120.0, 1180.0])
        _wait_until(lambda: (data_dir / "snapshot.json").exists())
        assert snapshot_module.read_snapshot(data_dir / "snapshot.json") == []


def test_s_t14_native_worker_ownership(data_dir, instrumentation):
    with _server(data_dir) as (instance, port):
        worker_ident = instance._worker.ident
        keys = [f"key-{i}" for i in range(16)]

        def store(index):
            return _post(port, "/set", {"key": keys[index], "value": f"value-{index}", "ttl": 30})

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(store, range(16)))
        assert results == [(200, {"result": True})] * 16
        for index, key in enumerate(keys):
            assert _post(port, "/get", {"key": key}) == (200, {"result": f"value-{index}"})

        # 실행 하나를 멈추고 대기 64개를 채운 뒤 다음 제출은 미실행 503이다.
        table_cls = instrumentation.table_cls
        gate = threading.Event()
        table_cls.gated_started = threading.Event()
        table_cls.gate = gate
        with ThreadPoolExecutor(max_workers=66) as pool:
            blocked = pool.submit(_post, port, "/get", {"key": "blocked"})
            assert table_cls.gated_started.wait(5), "worker never reached the gated get"
            table_cls.gate = None
            waiting = [
                pool.submit(_post, port, "/set", {"key": f"queued-{i}", "value": "v"})
                for i in range(64)
            ]
            _wait_until(lambda: instance._queue.qsize() == 64)
            assert _post(port, "/set", {"key": "overflow", "value": "v"}) == (
                503,
                {"error": {"code": "busy"}},
            )
            gate.set()
            assert blocked.result(10) == (200, {"result": None})
            assert [future.result(10) for future in waiting] == [(200, {"result": True})] * 64

        assert _post(port, "/get", {"key": "overflow"}) == (200, {"result": None})
        for index in range(64):
            assert _post(port, "/get", {"key": f"queued-{index}"}) == (200, {"result": "v"})
    assert instrumentation.threads == {worker_ident}


def _open_unread_request(port: int, path: str, payload: dict) -> socket.socket:
    body = json.dumps(payload).encode()
    head = (
        f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(head + body)
    return sock


def test_r3_disconnected_request(data_dir, instrumentation):
    """수락된 저장은 클라이언트 연결 단절 뒤에도 정확히 한 번 실행된다."""
    with _server(data_dir) as (instance, port):
        table_cls = instrumentation.table_cls
        gate = threading.Event()
        table_cls.put_started = threading.Event()
        table_cls.put_gate = gate
        client = _open_unread_request(port, "/set", {"key": "disconnect", "value": "v"})
        assert table_cls.put_started.wait(5), "worker never reached put"
        client.close()
        table_cls.put_gate = None
        gate.set()

        _wait_until(
            lambda: _post(port, "/get", {"key": "disconnect"}) == (200, {"result": "v"})
        )
        assert instrumentation.calls.count("put") == 1
        assert _post(port, "/get", {"key": "disconnect"}) == (200, {"result": "v"})


def test_r4_controller_start_bind_activate():
    """#13: controller의 지연 바인드·바인드 실패·수신 전 종료와 원본 파일 보존."""
    raise NotImplementedError


def test_r5_process_shutdown_and_exit_codes():
    """#13: OS 신호·프로세스 종료 코드와 최종 저장·해제."""
    raise NotImplementedError
