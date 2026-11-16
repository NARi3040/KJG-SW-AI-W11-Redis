"""Runtime 단위 사례: docs/http-and-runtime.md S-T11·S-T12·S-T14, R1~R6.

runtime.HashTable 이름과 스냅샷 파일 함수만 대체한다. 대체 테이블은 고정 결과·Event·호출
기록만 제공하며 별도 저장 엔진이 아니다. 실제 C 소유권은 test_integration_runtime에서 확인한다.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from contextlib import contextmanager

import pytest

from mini_redis import runtime as runtime_module
from mini_redis.contracts import BusyError, UnavailableError, WorkerFailedError
from mini_redis.runtime import Runtime


class FakeTable:
    """작업자가 참조하는 HashTable의 고정 결과·대기 Event·호출 기록 대체물."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.closed = False
        self.started = threading.Event()
        self.put_event = threading.Event()
        self.get_gate: threading.Event | None = None
        self.get_result = b"v"
        self.get_errors: list[BaseException] = []
        self.get_failures: list[BaseException] = []
        self.cleanup_errors: list[BaseException] = []
        self.cleanup_gate: threading.Event | None = None
        self.cleanup_started = threading.Event()
        self.copy_live_result: list[tuple[bytes, bytes, float | None]] = []

    def put(self, key: bytes, value: bytes, expires_at: float | None) -> None:
        self.events.append(f"put:{key.decode('utf-8', 'surrogatepass')}")
        self.put_event.set()

    def is_live(self, key: bytes, cutoff: float) -> bool:
        self.events.append(f"is_live:{key.decode('utf-8', 'surrogatepass')}")
        return True

    def get(self, key: bytes) -> tuple[bytes, float | None]:
        self.events.append(f"get:{key.decode('utf-8', 'surrogatepass')}")
        if self.get_errors:
            raise self.get_errors.pop(0)
        gate = self.get_gate
        if gate is not None:
            self.started.set()
            assert gate.wait(5), "test gate timeout"
        if self.get_failures:
            raise self.get_failures.pop(0)
        return self.get_result, None

    def remove(self, key: bytes) -> bool:
        self.events.append(f"remove:{key.decode('utf-8', 'surrogatepass')}")
        return True

    def remove_expired(self, cutoff: float) -> None:
        self.events.append("cleanup")
        if self.cleanup_gate is not None:
            self.cleanup_started.set()
            assert self.cleanup_gate.wait(5), "test cleanup gate timeout"
        if self.cleanup_errors:
            raise self.cleanup_errors.pop(0)

    def copy_live(self, cutoff: float) -> list[tuple[bytes, bytes, float | None]]:
        self.events.append("copy")
        return self.copy_live_result

    def close(self) -> None:
        self.events.append("close")
        self.closed = True


@pytest.fixture
def table(monkeypatch) -> FakeTable:
    fake = FakeTable()
    monkeypatch.setattr(runtime_module, "HashTable", lambda: fake)
    monkeypatch.setattr(runtime_module, "read_snapshot", lambda path: None)
    return fake


def _join(instance: Runtime, timeout: float = 5.0) -> int:
    """교착 감지용 제한을 둔 join. Runtime API의 대기 상한이 아니다."""
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


@contextmanager
def _running(data_dir, epoch_clock, monotonic_clock):
    instance = Runtime(data_dir, clock=epoch_clock, monotonic=monotonic_clock)
    instance.start()
    try:
        instance.wait_ready()
        instance.activate()
        yield instance
    finally:
        instance.request_stop(final_snapshot=False)
        _join(instance)


def test_running_helper_stops_worker_when_activate_fails(
    data_dir, epoch_clock, monotonic_clock, table, monkeypatch
):
    workers: list[threading.Thread] = []

    def failing_activate(self):
        assert self._worker is not None
        workers.append(self._worker)
        raise RuntimeError("injected activate failure")

    monkeypatch.setattr(Runtime, "activate", failing_activate)
    with pytest.raises(RuntimeError, match="injected activate failure"):
        with _running(data_dir, epoch_clock, monotonic_clock):
            pass
    assert workers and not workers[0].is_alive()


def test_s_t11_long_periodic_work(data_dir, epoch_clock, monotonic_clock, table, monkeypatch):
    monkeypatch.setattr(
        runtime_module, "write_snapshot", lambda path, entries: table.events.append("snapshot")
    )
    instance = Runtime(data_dir, clock=epoch_clock, monotonic=monotonic_clock)
    gate = threading.Event()
    table.get_gate = gate
    instance.start()
    try:
        instance.wait_ready()
        assert instance.state == "STARTING"
        # 준비만 끝난 상태에서는 요청도 주기 작업도 없다.
        monotonic_clock.advance(125)
        assert table.events == []
        with pytest.raises(UnavailableError):
            instance.submit("get", key="rejected")
        instance.activate()

        first = instance.submit("get", key="first")
        assert table.started.wait(5)
        monotonic_clock.advance(125)
        second = instance.submit("get", key="second")
        gate.set()
        assert first.result(5) == "v"
        assert second.result(5) == "v"

        # 밀린 횟수만큼 폭주하지 않고 청소→스냅샷→대기 요청 하나 순서다.
        assert table.events.count("cleanup") == 1
        assert table.events.count("snapshot") == 1
        assert table.events.index("get:first") < table.events.index("cleanup")
        assert table.events.index("cleanup") < table.events.index("get:second")
        assert table.events.index("snapshot") < table.events.index("get:second")

        instance.request_stop(final_snapshot=False)
        assert _join(instance) == 0
        assert instance.state == "STOPPED"
        assert table.closed
    finally:
        gate.set()
        instance.request_stop(final_snapshot=False)
        _join(instance)


def test_s_t12_clock_changes_and_save_failures(data_dir, epoch_clock, monotonic_clock, table, monkeypatch):
    table.copy_live_result = []
    attempts: list[float] = []
    condition = threading.Condition()

    def fake_write(path, entries):
        with condition:
            attempts.append(monotonic_clock())
            condition.notify_all()
        if len(attempts) <= 2:
            raise OSError("injected snapshot failure")

    monkeypatch.setattr(runtime_module, "write_snapshot", fake_write)

    def wait_attempts(count):
        with condition:
            assert condition.wait_for(lambda: len(attempts) >= count, timeout=5), "no snapshot attempt"

    with _running(data_dir, epoch_clock, monotonic_clock) as instance:
        monotonic_clock.advance(60)
        wait_attempts(1)
        _wait_until(lambda: instance._snapshot_deadline == 1120.0)
        # epoch 전진은 monotonic 기한에 영향을 주지 않는다.
        epoch_clock.advance(10_000)
        monotonic_clock.advance(59)
        assert len(attempts) == 1
        monotonic_clock.advance(1)
        wait_attempts(2)
        _wait_until(lambda: instance._snapshot_deadline == 1180.0)
        monotonic_clock.advance(60)
        wait_attempts(3)
        assert attempts == pytest.approx([1060.0, 1120.0, 1180.0])
        # 연속 저장 실패 뒤에도 정상 요청을 처리한다.
        assert instance.submit("get", key="after-failures").result(5) == "v"


def test_s_t14_worker_ownership_and_lifetime(data_dir, epoch_clock, monotonic_clock, table, monkeypatch):
    monkeypatch.setattr(
        runtime_module, "write_snapshot", lambda path, entries: table.events.append("snapshot")
    )
    with _running(data_dir, epoch_clock, monotonic_clock) as instance:
        gate = threading.Event()
        table.get_gate = gate
        first = instance.submit("get", key="k0")
        assert table.started.wait(5)
        pending = [instance.submit("get", key=f"k{i}") for i in range(1, 65)]
        with pytest.raises(BusyError):
            instance.submit("get", key="overflow")
        gate.set()
        assert first.result(5) == "v"
        assert [future.result(5) for future in pending] == ["v"] * 64
        # FIFO: 수락한 순서대로 실행되고 거부한 작업은 실행되지 않는다.
        executed = [event for event in table.events if event.startswith("get:")]
        assert executed == [f"get:k{i}" for i in range(65)]

        # 유휴 상태에서도 다음 요청 전에 기한을 확인한다.
        table.events.clear()
        monotonic_clock.advance(61)
        assert instance.submit("get", key="after-idle").result(5) == "v"
        assert instance.submit("get", key="after-periodic").result(5) == "v"
        assert "cleanup" in table.events
        assert "copy" in table.events
        assert table.events.index("cleanup") < table.events.index("get:after-periodic")

    with pytest.raises(UnavailableError):
        instance.submit("get", key="late")


def test_r1_acceptance_stop_race(data_dir, epoch_clock, monotonic_clock, table):
    instance = Runtime(data_dir, clock=epoch_clock, monotonic=monotonic_clock)
    gate = threading.Event()
    table.get_gate = gate
    instance.start()
    try:
        instance.wait_ready()
        instance.activate()
        first = instance.submit("get", key="k0")
        assert table.started.wait(5)
        accepted = [instance.submit("get", key=f"k{i}") for i in range(1, 65)]
        # 가득 찬 큐에서도 종료 전환은 즉시 진행된다.
        instance.request_stop(final_snapshot=False)
        assert instance.state == "STOPPING"
        with pytest.raises(UnavailableError):
            instance.submit("get", key="after-stop")
        gate.set()
        assert first.result(5) == "v"
        assert [future.result(5) for future in accepted] == ["v"] * 64
    finally:
        gate.set()
        instance.request_stop(final_snapshot=False)
        _join(instance)
    assert _join(instance) == 0
    assert instance.state == "STOPPED"


def test_r2_failed_worker_releases_futures(data_dir, epoch_clock, monotonic_clock, table, monkeypatch):
    writes: list[object] = []
    monkeypatch.setattr(runtime_module, "write_snapshot", lambda path, entries: writes.append(entries))
    instance = Runtime(data_dir, clock=epoch_clock, monotonic=monotonic_clock)
    gate = threading.Event()
    table.get_gate = gate
    instance.start()
    try:
        instance.wait_ready()
        instance.activate()
        current = instance.submit("get", key="k0")
        assert table.started.wait(5)
        queued = [instance.submit("get", key=f"k{i}") for i in (1, 2)]
        table.get_failures = [RuntimeError("injected")]
        gate.set()

        with pytest.raises(WorkerFailedError):
            current.result(5)
        for future in queued:
            with pytest.raises(WorkerFailedError):
                future.result(5)
        assert instance.state == "FAILED"
        with pytest.raises(UnavailableError):
            instance.submit("get", key="after-failure")
        assert _join(instance) == 1
        # FAILED에서는 최종 저장으로 기존 파일을 덮어쓰지 않는다.
        instance.request_stop()
        assert writes == []
        assert table.closed
    finally:
        gate.set()
        instance.request_stop(final_snapshot=False)
        _join(instance)


class GatedQueue(queue.Queue):
    """큐에서 작업을 꺼내는 순간을 고정해 idle 대기 복귀 시점을 결정적으로 만든다."""

    def __init__(self) -> None:
        super().__init__(maxsize=64)
        self.armed = False
        self.get_started = threading.Event()
        self.release = threading.Event()

    def get(self, block: bool = True, timeout: float | None = None):
        if self.armed and block:
            self.armed = False
            self.get_started.set()
            assert self.release.wait(5), "test get gate timeout"
        return super().get(block=block, timeout=timeout)


def test_s_t11_idle_deadline_checked_before_returned_request(
    data_dir, epoch_clock, monotonic_clock, table, monkeypatch
):
    """유휴 Queue.get이 요청을 반환한 직후 도래한 기한을 그 요청 전에 처리한다."""
    monkeypatch.setattr(
        runtime_module, "write_snapshot", lambda path, entries: table.events.append("snapshot")
    )
    instance = Runtime(data_dir, clock=epoch_clock, monotonic=monotonic_clock)
    gated = GatedQueue()
    instance._queue = gated
    instance.start()
    try:
        instance.wait_ready()
        gated.armed = True
        instance.activate()
        assert gated.get_started.wait(5), "worker never waited on the queue"

        table.events.clear()
        monotonic_clock.advance(61)
        future = instance.submit("get", key="after-idle")
        gated.release.set()
        assert future.result(5) == "v"

        assert table.events.index("cleanup") < table.events.index("get:after-idle")
        assert table.events.index("snapshot") < table.events.index("get:after-idle")
        assert table.events.count("cleanup") == 1
        assert table.events.count("snapshot") == 1
    finally:
        gated.release.set()
        instance.request_stop(final_snapshot=False)
        assert _join(instance) == 0


class Escape(BaseException):
    """BaseException 이탈 주입용."""


@pytest.mark.parametrize(
    "failure",
    [TypeError("secret-snapshot"), Escape("secret-snapshot")],
    ids=["type_error", "base_exception"],
)
def test_s_t11_pending_future_released_on_periodic_failure(
    data_dir, epoch_clock, monotonic_clock, table, monkeypatch, failure
):
    """Queue.get으로 꺼낸 pending Future도 주기 예외 시 WorkerFailedError로 해제된다."""

    def bad_write(path, entries):
        raise failure

    monkeypatch.setattr(runtime_module, "write_snapshot", bad_write)
    instance = Runtime(data_dir, clock=epoch_clock, monotonic=monotonic_clock)
    gated = GatedQueue()
    instance._queue = gated
    instance.start()
    try:
        instance.wait_ready()
        gated.armed = True
        instance.activate()
        assert gated.get_started.wait(5), "worker never waited on the queue"

        pending = [instance.submit("get", key=f"k{i}") for i in (1, 2)]
        monotonic_clock.advance(60)
        gated.release.set()

        for future in pending:
            with pytest.raises(WorkerFailedError):
                future.result(5)
        assert instance.state == "FAILED"
        assert _join(instance) == 1
        assert table.closed
    finally:
        gated.release.set()
        instance.request_stop(final_snapshot=False)
        _join(instance)


def test_s_t11_due_batch_once_before_request(
    data_dir, epoch_clock, monotonic_clock, table, monkeypatch
):
    """긴 스냅샷으로 청소 기한이 재도래해도 due 배치 1회 뒤 요청 1개가 먼저 실행된다."""

    def long_write(path, entries):
        table.events.append("snapshot")
        monotonic_clock.advance(5)

    monkeypatch.setattr(runtime_module, "write_snapshot", long_write)
    table.cleanup_gate = threading.Event()
    instance = Runtime(data_dir, clock=epoch_clock, monotonic=monotonic_clock)
    instance.start()
    try:
        instance.wait_ready()
        instance.activate()
        monotonic_clock.advance(61)
        assert table.cleanup_started.wait(5), "cleanup never started"
        future = instance.submit("get", key="queued")
        table.cleanup_gate.set()
        assert future.result(5) == "v"
    finally:
        table.cleanup_gate.set()
        instance.request_stop(final_snapshot=False)
        assert _join(instance) == 0
    before_request = table.events[: table.events.index("get:queued")]
    assert before_request.count("cleanup") == 1
    assert "snapshot" in before_request


def test_s_t11_stop_during_cleanup_skips_snapshot(
    data_dir, epoch_clock, monotonic_clock, table, monkeypatch
):
    """청소 중 STOPPING이면 뒤 스냅샷을 새로 시작하지 않는다."""
    monkeypatch.setattr(
        runtime_module, "write_snapshot", lambda path, entries: table.events.append("snapshot")
    )
    table.cleanup_gate = threading.Event()
    instance = Runtime(data_dir, clock=epoch_clock, monotonic=monotonic_clock)
    instance.start()
    try:
        instance.wait_ready()
        instance.activate()
        monotonic_clock.advance(61)
        assert table.cleanup_started.wait(5), "cleanup never started"
        instance.request_stop(final_snapshot=False)
        table.cleanup_gate.set()
        assert _join(instance) == 0
    finally:
        table.cleanup_gate.set()
        instance.request_stop(final_snapshot=False)
        _join(instance)
    assert instance.state == "STOPPED"
    assert table.events.count("cleanup") == 1
    assert "snapshot" not in table.events
    assert table.closed


def test_r4_start_bind_activate(data_dir, epoch_clock, monotonic_clock, table, monkeypatch):
    monkeypatch.setattr(
        runtime_module, "write_snapshot", lambda path, entries: table.events.append("snapshot")
    )
    instance = Runtime(data_dir, clock=epoch_clock, monotonic=monotonic_clock)
    instance.start()
    try:
        instance.wait_ready()
        assert instance.state == "STARTING"
        # 활성화가 60초 넘게 늦어져도 밀린 주기 작업을 따라잡지 않는다.
        monotonic_clock.advance(100)
        assert "cleanup" not in table.events and "snapshot" not in table.events
        with pytest.raises(UnavailableError):
            instance.submit("get", key="before-activate")
        instance.activate()
        assert instance.state == "RUNNING"

        # 첫 기한은 활성화 시각(1100) +1초와 +60초다.
        monotonic_clock.advance(0.5)
        assert instance.submit("get", key="r1").result(5) == "v"
        assert "cleanup" not in table.events
        monotonic_clock.advance(0.6)
        assert instance.submit("get", key="r2").result(5) == "v"
        assert table.events.count("cleanup") == 1
        assert "snapshot" not in table.events
        monotonic_clock.advance(60)
        assert instance.submit("get", key="r3").result(5) == "v"
        assert table.events.count("snapshot") == 1
    finally:
        instance.request_stop(final_snapshot=False)
        assert _join(instance) == 0


def test_r4_stop_before_activation(data_dir, epoch_clock, monotonic_clock, table, monkeypatch):
    monkeypatch.setattr(
        runtime_module, "write_snapshot", lambda path, entries: table.events.append("snapshot")
    )
    instance = Runtime(data_dir, clock=epoch_clock, monotonic=monotonic_clock)
    instance.start()
    try:
        instance.wait_ready()
        instance.request_stop()
        assert _join(instance) == 0
        assert instance.state == "STOPPED"
        assert "snapshot" not in table.events
        assert table.closed
    finally:
        instance.request_stop(final_snapshot=False)
        _join(instance)


def test_r5_exit_codes(data_dir, epoch_clock, monotonic_clock, table, monkeypatch):
    table.copy_live_result = [(b"k", b"v", None)]
    writes: list[object] = []
    monkeypatch.setattr(runtime_module, "write_snapshot", lambda path, entries: writes.append(entries))
    with _running(data_dir, epoch_clock, monotonic_clock) as instance:
        assert instance.submit("set", key="k", value="v").result(5) is True
        instance.request_stop()
        assert _join(instance) == 0
        assert instance.state == "STOPPED"
        assert writes == [[("k", "v", None)]]
        assert table.closed


def test_r5_final_snapshot_failure(data_dir, epoch_clock, monotonic_clock, table, monkeypatch):
    def failing_write(path, entries):
        raise OSError("injected final failure")

    monkeypatch.setattr(runtime_module, "write_snapshot", failing_write)
    with _running(data_dir, epoch_clock, monotonic_clock) as instance:
        assert instance.submit("get", key="k").result(5) == "v"
        instance.request_stop()
        assert _join(instance) == 1
        assert instance.state == "STOPPED"
        assert table.closed


def test_r6_recoverable_errors(data_dir, epoch_clock, monotonic_clock, table, monkeypatch):
    monkeypatch.setattr(
        runtime_module, "write_snapshot", lambda path, entries: table.events.append("snapshot")
    )
    with _running(data_dir, epoch_clock, monotonic_clock) as instance:
        table.get_errors = [MemoryError("injected")]
        with pytest.raises(MemoryError):
            instance.submit("get", key="memory").result(5)
        assert instance.submit("get", key="ok").result(5) == "v"

        table.events.clear()
        table.cleanup_errors = [MemoryError("injected cleanup")]
        monotonic_clock.advance(1)
        assert instance.submit("get", key="after-failed-cleanup").result(5) == "v"
        _wait_until(lambda: table.events.count("cleanup") == 1)
        _wait_until(lambda: instance._cleanup_deadline == 1002.0)
        monotonic_clock.advance(1)
        assert instance.submit("get", key="after-cleanup").result(5) == "v"
        _wait_until(lambda: table.events.count("cleanup") == 2)


def test_r1_submit_stop_barrier_race(data_dir, epoch_clock, monotonic_clock, table):
    """제출과 STOPPING 전환을 실제 Barrier로 경쟁시킨다."""
    instance = Runtime(data_dir, clock=epoch_clock, monotonic=monotonic_clock)
    gate = threading.Event()
    table.get_gate = gate
    instance.start()
    threads: list[threading.Thread] = []
    try:
        instance.wait_ready()
        instance.activate()
        first = instance.submit("get", key="block")
        assert table.started.wait(5)

        barrier = threading.Barrier(2)
        first_accepted = threading.Event()
        accepted: list = []
        rejected: list[BaseException] = []

        def submitter():
            barrier.wait()
            while True:
                try:
                    accepted.append(instance.submit("get", key=f"k{len(accepted)}"))
                    first_accepted.set()
                except BusyError as exc:
                    rejected.append(exc)
                except UnavailableError as exc:
                    rejected.append(exc)
                    return

        def stopper():
            barrier.wait()
            assert first_accepted.wait(5)
            instance.request_stop(final_snapshot=False)

        threads = [threading.Thread(target=submitter), threading.Thread(target=stopper)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        gate.set()

        assert accepted
        assert any(isinstance(error, UnavailableError) for error in rejected)
        assert first.result(5) == "v"
        assert [future.result(5) for future in accepted] == ["v"] * len(accepted)
        assert _join(instance) == 0
        assert instance.state == "STOPPED"
        with pytest.raises(UnavailableError):
            instance.submit("get", key="late")
    finally:
        gate.set()
        instance.request_stop(final_snapshot=False)
        for thread in threads:
            thread.join(5)
        _join(instance)


def test_r2_base_exception_escape_drains_futures(
    data_dir, epoch_clock, monotonic_clock, table, monkeypatch
):
    class Escape(BaseException):
        pass

    writes: list[object] = []
    monkeypatch.setattr(runtime_module, "write_snapshot", lambda path, entries: writes.append(entries))
    instance = Runtime(data_dir, clock=epoch_clock, monotonic=monotonic_clock)
    gate = threading.Event()
    table.get_gate = gate
    instance.start()
    try:
        instance.wait_ready()
        instance.activate()
        current = instance.submit("get", key="k0")
        assert table.started.wait(5)
        queued = [instance.submit("get", key=f"k{i}") for i in (1, 2)]
        table.get_failures = [Escape("secret")]
        gate.set()

        with pytest.raises(WorkerFailedError):
            current.result(5)
        for future in queued:
            with pytest.raises(WorkerFailedError):
                future.result(5)
        assert instance.state == "FAILED"
        assert _join(instance) == 1
        assert writes == []
        assert table.closed
    finally:
        gate.set()
        instance.request_stop(final_snapshot=False)
        _join(instance)


def test_startup_recovery_failure_fails_ready(
    data_dir, epoch_clock, monotonic_clock, table, monkeypatch
):
    def failing_read(path):
        raise ValueError("secret-read-value")

    monkeypatch.setattr(runtime_module, "read_snapshot", failing_read)
    instance = Runtime(data_dir, clock=epoch_clock, monotonic=monotonic_clock)
    instance.start()
    try:
        with pytest.raises(ValueError):
            instance.wait_ready()
        assert instance.state == "FAILED"
        assert _join(instance) == 1
        assert table.closed
        with pytest.raises(UnavailableError):
            instance.submit("get", key="late")
    finally:
        instance.request_stop(final_snapshot=False)
        _join(instance)


def test_r6_unexpected_cleanup_fails(data_dir, epoch_clock, monotonic_clock, table, monkeypatch):
    monkeypatch.setattr(
        runtime_module, "write_snapshot", lambda path, entries: table.events.append("snapshot")
    )
    instance = Runtime(data_dir, clock=epoch_clock, monotonic=monotonic_clock)
    gate = threading.Event()
    table.get_gate = gate
    instance.start()
    try:
        instance.wait_ready()
        instance.activate()
        first = instance.submit("get", key="first")
        assert table.started.wait(5)
        table.cleanup_errors = [RuntimeError("secret-cleanup-value")]
        monotonic_clock.advance(1)
        queued = instance.submit("get", key="queued")
        gate.set()
        assert first.result(5) == "v"
        with pytest.raises(WorkerFailedError):
            queued.result(5)
        assert instance.state == "FAILED"
        assert _join(instance) == 1
        assert table.closed
    finally:
        gate.set()
        instance.request_stop(final_snapshot=False)
        _join(instance)


def test_r6_unexpected_snapshot_fails(data_dir, epoch_clock, monotonic_clock, table, monkeypatch):
    def bad_write(path, entries):
        raise TypeError("secret-snapshot-value")

    monkeypatch.setattr(runtime_module, "write_snapshot", bad_write)
    instance = Runtime(data_dir, clock=epoch_clock, monotonic=monotonic_clock)
    gate = threading.Event()
    table.get_gate = gate
    instance.start()
    try:
        instance.wait_ready()
        instance.activate()
        first = instance.submit("get", key="first")
        assert table.started.wait(5)
        monotonic_clock.advance(60)
        queued = instance.submit("get", key="queued")
        gate.set()
        assert first.result(5) == "v"
        with pytest.raises(WorkerFailedError):
            queued.result(5)
        assert instance.state == "FAILED"
        assert _join(instance) == 1
        assert table.closed
    finally:
        gate.set()
        instance.request_stop(final_snapshot=False)
        _join(instance)


def test_r6_logs_hide_exception_text(data_dir, epoch_clock, monotonic_clock, table, monkeypatch, caplog):
    def failing_write(path, entries):
        raise OSError("secret-snapshot-value")

    monkeypatch.setattr(runtime_module, "write_snapshot", failing_write)
    caplog.set_level(logging.WARNING, logger="mini_redis.runtime")
    with _running(data_dir, epoch_clock, monotonic_clock) as instance:
        monotonic_clock.advance(60)
        assert instance.submit("get", key="k").result(5) == "v"
        _wait_until(lambda: "snapshot failed" in caplog.text)
        table.cleanup_errors = [MemoryError("secret-cleanup-value")]
        monotonic_clock.advance(1)
        assert instance.submit("get", key="k2").result(5) == "v"
        _wait_until(lambda: "cleanup failed" in caplog.text)
    assert "secret-snapshot-value" not in caplog.text
    assert "secret-cleanup-value" not in caplog.text
    assert "OSError" in caplog.text
    assert "MemoryError" in caplog.text


def test_failed_log_hides_exception_text(data_dir, epoch_clock, monotonic_clock, table, caplog):
    caplog.set_level(logging.ERROR, logger="mini_redis.runtime")
    table.get_errors = [RuntimeError("secret-worker-value")]
    instance = Runtime(data_dir, clock=epoch_clock, monotonic=monotonic_clock)
    instance.start()
    try:
        instance.wait_ready()
        instance.activate()
        with pytest.raises(WorkerFailedError):
            instance.submit("get", key="k").result(5)
        assert _join(instance) == 1
        assert "worker failed" in caplog.text
        assert "secret-worker-value" not in caplog.text
    finally:
        instance.request_stop(final_snapshot=False)
        _join(instance)
