"""Store·파일·복구·소비자 통합 사례. 사례 정의: docs/store-and-snapshots.md

실제 Runtime 작업자, 실제 C 시험 빌드 테이블, 실제 snapshot.py로 복구와 스냅샷 저장을 확인한다.
S-T7, S-T8, S-T10, S-T13은 이 파일에서 Runtime 수명으로 확인한다. S-T6, S-T9는 실제 자식
프로세스의 강제 중단과 파이프 단계 동기화로 확인한다. D1, D2는 시연 소비자 범위라 남긴다.
"""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time
from pathlib import Path

import pytest

from _runtime_process import Child
from mini_redis import runtime as runtime_module
from mini_redis import snapshot as snapshot_module
from mini_redis.contracts import UnavailableError
from mini_redis.hashtable import HashTable
from mini_redis.runtime import Runtime
from mini_redis.snapshot import read_snapshot, write_snapshot


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


def test_d1_cache_reuse():
    """TTL 내 재요청은 저장 결과를 재사용하고 원본 호출 횟수가 줄어든다."""
    raise NotImplementedError


def test_d2_explicit_invalidation():
    """원본 결과 변경 뒤 명시적 삭제 후 재요청은 이전 캐시 값을 쓰지 않고 새 원본 결과를 저장한다."""
    raise NotImplementedError
