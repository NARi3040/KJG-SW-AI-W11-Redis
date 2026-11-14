"""스냅샷 파일 단위 사례. 사례 정의: docs/store-and-snapshots.md

파일 저장·교체·검증을 정지와 재시작 없이 확인한다.
정지와 재시작이 필요한 사례는 tests/test_integration_store.py에 둔다.
"""

from __future__ import annotations

import base64
import json
import logging
import os

import pytest

from mini_redis import snapshot


def _canonical(text: str) -> str:
    """테스트가 손으로 만드는 Base64를 표준형으로 만든다."""
    return base64.b64encode(text.encode("utf-8", "surrogatepass")).decode("ascii")


def _document(*entries: dict) -> bytes:
    """테스트가 손으로 만드는 스냅샷 파일 바이트를 만든다."""
    return json.dumps({"version": 1, "entries": list(entries)}).encode("utf-8")


def _entry(key: str, value: str, expires_at: float | None) -> dict:
    """항목 하나를 직렬화 형식으로 만든다."""
    return {"key_b64": _canonical(key), "value_b64": _canonical(value), "expires_at": expires_at}


def _as_set(entries) -> set:
    """순서를 보장하지 않는 항목 목록을 집합으로 비교한다."""
    return {(key, value, expires_at) for key, value, expires_at in entries}


def test_s_t6_only_last_snapshot_in_file(data_dir):
    """성공한 스냅샷 파일은 그 시각의 항목만 담는다. 이후 변경은 파일에 반영되지 않는다."""
    path = data_dir / "snapshot.json"
    original = [("a", "1", None), ("b", "2", 110.25)]
    snapshot.write_snapshot(path, original)

    original.append(("c", "3", None))
    original.remove(("a", "1", None))
    assert _as_set(snapshot.read_snapshot(path)) == {("a", "1", None), ("b", "2", 110.25)}

    snapshot.write_snapshot(path, [("d", "4", 111.5)])
    assert snapshot.read_snapshot(path) == [("d", "4", 111.5)]


def test_s_t7_reads_original_absolute_expiry(data_dir):
    """E=110.25와 무기한 항목을 원래 절대 만료 시각 그대로 읽는다. TTL을 처음부터 다시 시작하지 않는다. 복구 시각 필터링은 Runtime이 한다."""
    path = data_dir / "snapshot.json"
    entries = [
        ("k", "v", 110.25),
        ("forever", "no-expiry", None),
        ("", "", None),
        ("a\u0000b", "x\u0000y", 120.5),
        ("한글", "값", None),
    ]
    snapshot.write_snapshot(path, entries)

    assert _as_set(snapshot.read_snapshot(path)) == set(entries)


def test_s_t7_reads_original_absolute_expiry_from_int(data_dir):
    """손으로 쓴 파일의 정수 expires_at도 float로 읽는다."""
    path = data_dir / "snapshot.json"
    path.write_bytes(_document(_entry("k", "v", 110)))

    loaded = snapshot.read_snapshot(path)
    assert loaded == [("k", "v", 110.0)]
    assert type(loaded[0][2]) is float


def _failing_fdopen(monkeypatch, stage):
    """os.fdopen을 감싸 지정한 메서드에서 OSError를 내게 한다."""
    real_fdopen = os.fdopen

    def wrapper(fd, mode, *args, **kwargs):
        handle = real_fdopen(fd, mode, *args, **kwargs)
        if stage == "write":
            def failing_write(*_args, **_kwargs):
                raise OSError("injected write failure")
            handle.write = failing_write
        elif stage == "flush":
            def failing_flush(*_args, **_kwargs):
                raise OSError("injected flush failure")
            handle.flush = failing_flush
        return handle

    monkeypatch.setattr(snapshot.os, "fdopen", wrapper)


def _raise_os(message: str):
    """표준 라이브러리 호출 지점에 주입할 OSError 발생 함수를 만든다."""
    def raise_it(*_args, **_kwargs):
        raise OSError(message)
    return raise_it


@pytest.mark.parametrize("stage", ["open", "write", "flush", "fsync", "replace"], ids=[
    "open", "write", "flush", "fsync", "replace",
])
def test_s_t8_write_stage_failures(data_dir, monkeypatch, stage):
    """임시 파일 열기, JSON 기록, flush, fsync, os.replace 실패마다 저장 실패를 보고하고 이전 정식 파일을 보존한다. 이번 임시 파일만 정리한다."""
    path = data_dir / "snapshot.json"
    snapshot.write_snapshot(path, [("old", "1", None)])
    previous_bytes = path.read_bytes()

    leftover = data_dir / "snapshot.json.old.tmp"
    leftover.write_bytes(b"previous leftover")

    if stage == "open":
        monkeypatch.setattr(snapshot.os, "fdopen", _raise_os("injected fdopen failure"))
    elif stage in ("write", "flush"):
        _failing_fdopen(monkeypatch, stage)
    elif stage == "fsync":
        monkeypatch.setattr(snapshot.os, "fsync", _raise_os("injected fsync failure"))
    else:
        monkeypatch.setattr(snapshot.os, "replace", _raise_os("injected replace failure"))

    with pytest.raises(OSError):
        snapshot.write_snapshot(path, [("new", "2", 120.0)])

    assert path.read_bytes() == previous_bytes
    assert sorted(p.name for p in data_dir.iterdir()) == ["snapshot.json", "snapshot.json.old.tmp"]
    assert leftover.read_bytes() == b"previous leftover"

    monkeypatch.undo()
    snapshot.write_snapshot(path, [("new", "2", 120.0)])
    assert snapshot.read_snapshot(path) == [("new", "2", 120.0)]


def test_s_t9_interrupted_write_file_level(data_dir, caplog):
    """기록 중 중단은 이전 정식 파일을, 교체 완료 뒤 중단은 새 정식 파일을 쓰며 임시 파일을 자동 채택하지 않는다."""
    path = data_dir / "snapshot.json"
    snapshot.write_snapshot(path, [("old", "1", None)])

    partial = data_dir / "snapshot.json.x.tmp"
    partial.write_bytes(_document(_entry("partial", "9", None))[:20])

    with caplog.at_level(logging.WARNING, logger="mini_redis.snapshot"):
        assert snapshot.read_snapshot(path) == [("old", "1", None)]

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "snapshot.json.x.tmp" in message
    assert "partial" not in message
    assert "old" not in message
    assert partial.read_bytes() == _document(_entry("partial", "9", None))[:20]

    snapshot.write_snapshot(path, [("new", "2", 121.0)])
    assert snapshot.read_snapshot(path) == [("new", "2", 121.0)]


_INVALID_DOCUMENTS = {
    "truncated-json": b'{"version": 1, "entries": [',
    "nested-too-deeply": b'{"version": 1, "entries": ' + b"[" * 100_000 + b"]" * 100_000 + b"}",
    "invalid-utf8": b'{"version": 1, "entries": [], "x\xff": 1}',
    "duplicate-top-field": b'{"version": 1, "entries": [], "version": 1}',
    "duplicate-entry-field": b'{"version": 1, "entries": [{"key_b64": "aw==", "value_b64": "dg==", "expires_at": null, "expires_at": null}]}',
    "duplicate-key-after-decode": _document(_entry("k", "v", None), _entry("k", "w", None)),
    "unknown-top-field": b'{"version": 1, "entries": [], "extra": 1}',
    "unknown-entry-field": _document(
        {"key_b64": "aw==", "value_b64": "dg==", "expires_at": None, "extra": 1}
    ),
    "missing-entry-field": _document({"key_b64": "aw==", "expires_at": None}),
    "version-2": b'{"version": 2, "entries": []}',
    "version-true": b'{"version": true, "entries": []}',
    "version-float": b'{"version": 1.0, "entries": []}',
    "version-string": b'{"version": "1", "entries": []}',
    "entries-not-list": b'{"version": 1, "entries": {}}',
    "entry-not-object": b'{"version": 1, "entries": [1]}',
    "key-not-string": b'{"version": 1, "entries": [{"key_b64": 1, "value_b64": "dg==", "expires_at": null}]}',
    "expires-true": _document(_entry("k", "v", True)),
    "expires-string": _document(_entry("k", "v", "1")),
    "expires-nan": b'{"version": 1, "entries": [{"key_b64": "aw==", "value_b64": "dg==", "expires_at": NaN}]}',
    "expires-infinity": b'{"version": 1, "entries": [{"key_b64": "aw==", "value_b64": "dg==", "expires_at": Infinity}]}',
    "expires-overflow": b'{"version": 1, "entries": [{"key_b64": "aw==", "value_b64": "dg==", "expires_at": 1e400}]}',
    "base64-non-ascii": _document(
        {"key_b64": "\u00e9", "value_b64": "dg==", "expires_at": None}
    ),
    "base64-missing-padding": _document(
        {"key_b64": "aw", "value_b64": "dg==", "expires_at": None}
    ),
    "base64-non-canonical": _document(
        {"key_b64": "bR==", "value_b64": "dg==", "expires_at": None}
    ),
    "base64-invalid-utf8": _document(
        {"key_b64": base64.b64encode(b"\xff").decode("ascii"), "value_b64": "dg==", "expires_at": None}
    ),
    "expired-entry-invalid-base64": _document(
        {"key_b64": "aw", "value_b64": "dg==", "expires_at": 1.0}
    ),
}


@pytest.mark.parametrize("content", list(_INVALID_DOCUMENTS.values()), ids=list(_INVALID_DOCUMENTS))
def test_s_t10_restore_validation_failures(data_dir, content):
    """정식 파일 없음만 None이고 임시 파일만 있으면 경고 후 None이다. 잘림, 중복 필드·키, 알 수 없는 필드, 버전·타입 오류는 ValueError, 권한·읽기 오류는 OSError, 할당 실패는 MemoryError로 전달한다."""
    path = data_dir / "snapshot.json"
    previous_bytes = content
    path.write_bytes(previous_bytes)

    with pytest.raises(ValueError):
        snapshot.read_snapshot(path)

    assert path.read_bytes() == previous_bytes


def test_s_t10_missing_file_returns_none(data_dir, caplog):
    """정식 파일이 없으면 None이고 경고가 없다."""
    path = data_dir / "snapshot.json"

    with caplog.at_level(logging.WARNING, logger="mini_redis.snapshot"):
        assert snapshot.read_snapshot(path) is None

    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
    assert list(data_dir.iterdir()) == []


def test_s_t10_only_temp_file_returns_none(data_dir, caplog):
    """임시 파일만 있으면 None이고 경고 한 건을 남긴다."""
    path = data_dir / "snapshot.json"
    (data_dir / "snapshot.json.only.tmp").write_bytes(_document(_entry("k", "v", None)))

    with caplog.at_level(logging.WARNING, logger="mini_redis.snapshot"):
        assert snapshot.read_snapshot(path) is None

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "snapshot.json.only.tmp" in warnings[0].getMessage()


def test_s_t10_empty_file_returns_empty_list(data_dir):
    """유효한 빈 파일은 빈 리스트다."""
    path = data_dir / "snapshot.json"
    path.write_bytes(b'{"version": 1, "entries": []}')

    assert snapshot.read_snapshot(path) == []


def test_s_t10_permission_error_propagates(data_dir, monkeypatch):
    """권한 오류는 PermissionError로 전달되고 None으로 바뀌지 않는다."""
    path = data_dir / "snapshot.json"

    def raise_permission(self):
        raise PermissionError("injected")

    monkeypatch.setattr(type(path), "read_bytes", raise_permission)

    with pytest.raises(PermissionError):
        snapshot.read_snapshot(path)


def test_s_t10_read_error_propagates(data_dir, monkeypatch):
    """일반 OSError도 그대로 전달된다."""
    path = data_dir / "snapshot.json"

    def raise_os(self):
        raise OSError("injected read failure")

    monkeypatch.setattr(type(path), "read_bytes", raise_os)

    with pytest.raises(OSError):
        snapshot.read_snapshot(path)


def test_s_t10_allocation_failure_propagates(data_dir, monkeypatch):
    """할당 실패는 MemoryError로 전달된다."""
    path = data_dir / "snapshot.json"
    path.write_bytes(_document(_entry("k", "v", None)))

    def raise_memory(*args, **kwargs):
        raise MemoryError("injected")

    monkeypatch.setattr(snapshot.json, "loads", raise_memory)

    with pytest.raises(MemoryError):
        snapshot.read_snapshot(path)


def test_s_t13_surrogate_and_base64(data_dir):
    r"""보충 문자 1개인 "\U0001F600"과 surrogate 2개인 "\ud83d\ude00"을 키·값으로 파일 왕복해 서로 다른 문자열로 구별한다. 잘못된 Base64는 ValueError로 거부한다."""
    path = data_dir / "snapshot.json"
    lone = chr(0x1F600)
    pair = chr(0xD83D) + chr(0xDE00)
    assert lone != pair and len(lone) == 1 and len(pair) == 2

    snapshot.write_snapshot(path, [(lone, pair, None), (pair, lone, 111.5)])

    loaded = _as_set(snapshot.read_snapshot(path))
    assert loaded == {(lone, pair, None), (pair, lone, 111.5)}

    raw = json.loads(path.read_bytes().decode("utf-8"))
    encoded = {entry["key_b64"]: base64.b64decode(entry["key_b64"]) for entry in raw["entries"]}
    assert encoded[_canonical(lone)] == b"\xf0\x9f\x98\x80"
    assert encoded[_canonical(pair)] == b"\xed\xa0\xbd\xed\xb8\x80"


def test_s_t13_invalid_base64_rejected(data_dir):
    """잘못된 Base64는 ValueError로 거부한다."""
    path = data_dir / "snapshot.json"
    path.write_bytes(_document(
        {"key_b64": "bR==", "value_b64": "dg==", "expires_at": None}
    ))

    with pytest.raises(ValueError):
        snapshot.read_snapshot(path)
