"""Store 사례. 사례 정의: docs/store-and-snapshots.md, T17·T19·T20은 docs/c-hashtable-and-bindings.md

실제 C 시험 빌드 테이블과 가짜 epoch 시계로 저장, 조회, 삭제, TTL, 청소를 확인한다.
입력 검증과 잘못된 요청의 상태 보존 T13, T14, T21은 HTTP 경계(MR-05)에서 확인한다.
"""

from __future__ import annotations

import pytest

from mini_redis.hashtable import HashTable
from mini_redis.store import Store


@pytest.fixture
def table():
    table = HashTable()
    table._test_fail_alloc_after(-1)
    try:
        yield table
    finally:
        table._lib.ht_test_fail_alloc_after(-1)
        table.close()


def _dump(table) -> tuple[list, int, int]:
    entries, count, buckets = table._test_dump()
    return sorted(entries), count, buckets


class CountingClock:
    """호출 횟수를 세는 가짜 epoch 시계."""

    def __init__(self, value: float) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return self.value


def test_t1_new_set_get(table, epoch_clock):
    """저장 True, 조회 "v". TTL 생략이므로 무기한이다."""
    epoch_clock.set(100)
    store = Store(table, clock=epoch_clock)

    assert store.set("k", "v") is True
    assert store.get("k") == "v"
    assert _dump(table) == ([(b"k", b"v", None)], 1, 16)


def test_t1_each_operation_reads_clock_once(table):
    """set, get, delete, cleanup, snapshot_entries가 각각 시계를 한 번만 읽는다."""
    clock = CountingClock(100)
    store = Store(table, clock=clock)

    store.set("k", "v")
    assert clock.calls == 1
    store.get("k")
    assert clock.calls == 2
    store.delete("k")
    assert clock.calls == 3
    store.cleanup()
    assert clock.calls == 4
    store.snapshot_entries()
    assert clock.calls == 5


def test_t2_missing_key(table, epoch_clock):
    """없는 키 조회는 None이고 상태 변화가 없다."""
    epoch_clock.set(100)
    store = Store(table, clock=epoch_clock)

    assert store.get("missing") is None
    assert _dump(table) == ([], 0, 16)


def test_t3_empty_strings(table, epoch_clock):
    """빈 키에 빈 값은 True, 조회는 "", 없는 키 조회는 None. 빈 값과 키 없음을 구분한다."""
    epoch_clock.set(100)
    store = Store(table, clock=epoch_clock)

    assert store.set("", "") is True
    assert store.get("") == ""
    assert store.get("missing") is None


def test_t4_exact_string_identity(table, epoch_clock):
    """"A" 저장 뒤 "a", " A " 조회는 모두 None. 공백 제거와 대소문자 변환이 없다."""
    epoch_clock.set(100)
    store = Store(table, clock=epoch_clock)
    assert store.set("A", "v") is True

    assert store.get("a") is None
    assert store.get(" A ") is None


def test_t5_overwrite_without_ttl(table, epoch_clock):
    """두 저장 모두 True, 조회 "new", 무기한이다."""
    epoch_clock.set(100)
    store = Store(table, clock=epoch_clock)

    assert store.set("k", "old") is True
    assert store.set("k", "new") is True
    assert store.get("k") == "new"
    assert _dump(table) == ([(b"k", b"new", None)], 1, 16)


def test_t6_expiry_boundary(table, epoch_clock):
    """t=100에 TTL 10 저장 후 109.999는 "v", 110과 111은 None. 110 조회에서 값과 만료 정보를 제거한다."""
    epoch_clock.set(100)
    store = Store(table, clock=epoch_clock)
    assert store.set("k", "v", ttl=10) is True

    epoch_clock.set(109.999)
    assert store.get("k") == "v"
    epoch_clock.set(110)
    assert store.get("k") is None
    assert _dump(table)[1] == 0
    epoch_clock.set(111)
    assert store.get("k") is None


def test_t6_fractional_expiry(table, epoch_clock):
    """t=100.25에 TTL 10이면 E=110.25이고 110.249는 유효, 110.25는 만료다."""
    epoch_clock.set(100.25)
    store = Store(table, clock=epoch_clock)
    assert store.set("k", "v", ttl=10) is True
    assert _dump(table)[0] == [(b"k", b"v", 110.25)]

    epoch_clock.set(110.249)
    assert store.get("k") == "v"
    epoch_clock.set(110.25)
    assert store.get("k") is None


def test_t7_ttl_replace(table, epoch_clock):
    """t=100에 TTL 10으로 저장한 뒤 105에 TTL 20으로 덮어쓰면 True, 새 E=125. 110은 "new", 125는 None이다."""
    epoch_clock.set(100)
    store = Store(table, clock=epoch_clock)
    assert store.set("k", "old", ttl=10) is True

    epoch_clock.set(105)
    assert store.set("k", "new", ttl=20) is True
    assert _dump(table)[0] == [(b"k", b"new", 125.0)]

    epoch_clock.set(110)
    assert store.get("k") == "new"
    epoch_clock.set(125)
    assert store.get("k") is None


def test_t8_ttl_remove(table, epoch_clock):
    """TTL 없이 덮어쓰면 True, 110 청소 후에도 "new"와 무기한. 과거 TTL로 제거되지 않는다."""
    epoch_clock.set(100)
    store = Store(table, clock=epoch_clock)
    assert store.set("k", "old", ttl=10) is True

    epoch_clock.set(105)
    assert store.set("k", "new") is True
    epoch_clock.set(110)
    store.cleanup()

    assert store.get("k") == "new"
    assert _dump(table)[0] == [(b"k", b"new", None)]


def test_t9_ttl_add(table, epoch_clock):
    """무기한 키를 t=100에 TTL 1로 덮어쓰면 True, 101 조회는 None이다."""
    epoch_clock.set(100)
    store = Store(table, clock=epoch_clock)
    assert store.set("k", "v") is True

    assert store.set("k", "v", ttl=1) is True
    epoch_clock.set(101)
    assert store.get("k") is None


def test_t10_delete_and_repeat_delete(table, epoch_clock):
    """삭제 True, 조회 None, 재삭제 False. 없는 다른 키 삭제도 False이며 값과 만료 정보를 함께 제거한다."""
    epoch_clock.set(100)
    store = Store(table, clock=epoch_clock)
    assert store.set("k", "v", ttl=10) is True

    assert store.delete("k") is True
    assert store.get("k") is None
    assert store.delete("k") is False
    assert store.delete("other") is False
    assert _dump(table)[1] == 0


def test_t11_delete_expired_key(table, epoch_clock):
    """t=100에 TTL 1 저장 후 청소와 조회 없이 101에 삭제하면 False. 남아 있던 물리 값도 제거한다."""
    epoch_clock.set(100)
    store = Store(table, clock=epoch_clock)
    assert store.set("k", "v", ttl=1) is True

    epoch_clock.set(101)
    assert store.delete("k") is False
    assert _dump(table)[1] == 0


@pytest.mark.parametrize("path", ["after-expiry", "after-delete"])
def test_t12_set_again_after_expiry_or_delete(table, epoch_clock, path):
    """TTL 만료 뒤와 명시적 삭제 뒤 각각 다시 저장하면 True, 조회 "new", 과거 만료 정보가 없다."""
    epoch_clock.set(100)
    store = Store(table, clock=epoch_clock)
    assert store.set("k", "old", ttl=10) is True

    if path == "after-expiry":
        epoch_clock.set(110)
        assert store.get("k") is None
    else:
        assert store.delete("k") is True

    assert store.set("k", "new") is True
    assert store.get("k") == "new"
    assert _dump(table)[0] == [(b"k", b"new", None)]


def test_t15_cleanup_targets(table, epoch_clock):
    """t=100에 A는 TTL 1, B는 TTL 10, C는 무기한 저장 후 101 청소. A만 제거되고 B, C는 값이 남는다."""
    epoch_clock.set(100)
    store = Store(table, clock=epoch_clock)
    assert store.set("A", "a", ttl=1) is True
    assert store.set("B", "b", ttl=10) is True
    assert store.set("C", "c") is True

    epoch_clock.set(101)
    store.cleanup()

    assert _dump(table)[0] == [(b"B", b"b", 110.0), (b"C", b"c", None)]
    assert store.get("A") is None
    assert store.get("B") == "b"
    assert store.get("C") == "c"


@pytest.mark.parametrize("path", ["update-then-cleanup", "cleanup-then-update"])
def test_t16_update_cleanup_order(table, epoch_clock, path):
    """옛 E=110, 판정 시각 110에서 갱신과 청소를 두 순서로 실행해도 최종 값은 "new", E=130이다."""
    epoch_clock.set(100)
    store = Store(table, clock=epoch_clock)
    assert store.set("k", "old", ttl=10) is True

    epoch_clock.set(110)
    if path == "update-then-cleanup":
        assert store.set("k", "new", ttl=20) is True
        store.cleanup()
    else:
        store.cleanup()
        assert store.set("k", "new", ttl=20) is True

    assert store.get("k") == "new"
    assert _dump(table)[0] == [(b"k", b"new", 130.0)]


@pytest.mark.parametrize("target", ["a", "q"])
def test_t17_collision_through_store(table, epoch_clock, target):
    """16버킷에서 같은 버킷인 "a", "q" 중 하나만 덮어쓰기·삭제해도 다른 키의 값과 TTL 유지."""
    epoch_clock.set(100)
    store = Store(table, clock=epoch_clock)
    assert store.set("a", "va", ttl=10) is True
    assert store.set("q", "vq", ttl=20) is True

    assert store.set(target, "new", ttl=30) is True
    assert store.delete(target) is True

    other = "q" if target == "a" else "a"
    other_value, other_expiry = ("vq", 120.0) if other == "q" else ("va", 110.0)
    assert store.get(other) == other_value
    assert _dump(table)[0] == [(other.encode(), other_value.encode(), other_expiry)]


@pytest.mark.parametrize("method", ["set", "get", "delete"])
def test_t18_closed_table_propagates_runtime_error(table, epoch_clock, method):
    """닫힌 테이블에 set, get, delete를 하면 각각 RuntimeError를 전달한다."""
    epoch_clock.set(100)
    store = Store(table, clock=epoch_clock)
    store.set("k", "v")
    table.close()

    with pytest.raises(RuntimeError):
        if method == "set":
            store.set("k", "new")
        elif method == "get":
            store.get("k")
        else:
            store.delete("k")


@pytest.mark.parametrize("existing", [False, True], ids=["new-key", "existing-key"])
def test_t18_set_allocation_failure(table, epoch_clock, existing):
    """신규 키와 기존 키 저장의 할당 실패는 MemoryError이고 물리 상태를 보존한다."""
    epoch_clock.set(100)
    store = Store(table, clock=epoch_clock)
    assert store.set("k", "v", ttl=10) is True
    before = _dump(table)

    table._test_fail_alloc_after(0)
    try:
        with pytest.raises(MemoryError):
            store.set("k" if existing else "new", "other", ttl=20)
    finally:
        table._test_fail_alloc_after(-1)

    assert _dump(table) == before


@pytest.mark.parametrize("method", ["get", "snapshot_entries"], ids=["get-copy", "snapshot-copy"])
def test_t18_copy_failure_preserves_original(table, epoch_clock, method):
    """조회와 스냅샷 복사의 할당 실패는 MemoryError이고 원본을 보존한다."""
    epoch_clock.set(100)
    store = Store(table, clock=epoch_clock)
    assert store.set("k", "v", ttl=10) is True
    before = _dump(table)

    table._test_fail_alloc_after(0)
    try:
        with pytest.raises(MemoryError):
            store.get("k") if method == "get" else store.snapshot_entries()
    finally:
        table._test_fail_alloc_after(-1)

    assert _dump(table) == before


def test_t19_resize_through_store(table, epoch_clock):
    """12개까지 16버킷, 13번째 신규 키 전에 32버킷. 기존 값·TTL 유지."""
    epoch_clock.set(100)
    store = Store(table, clock=epoch_clock)
    for i in range(12):
        assert store.set(f"k{i}", f"v{i}", ttl=10) is True
    assert _dump(table)[1:] == (12, 16)

    assert store.set("k12", "v12", ttl=10) is True
    assert _dump(table)[1:] == (13, 32)

    for i in range(13):
        assert store.get(f"k{i}") == f"v{i}"
    assert sorted(_dump(table)[0]) == sorted(
        (f"k{i}".encode(), f"v{i}".encode(), 110.0) for i in range(13)
    )


@pytest.mark.parametrize("site", [0, 1, 2, 3])
def test_t20_resize_alloc_failure_through_store(table, epoch_clock, site):
    """확장 준비 할당 실패는 MemoryError, 새 키 미저장, 기존 상태 보존."""
    epoch_clock.set(100)
    store = Store(table, clock=epoch_clock)
    for i in range(12):
        assert store.set(f"k{i}", f"v{i}", ttl=10) is True
    before = _dump(table)
    assert before[1:] == (12, 16)

    table._test_fail_alloc_after(site)
    try:
        with pytest.raises(MemoryError):
            store.set("k12", "v12", ttl=10)
    finally:
        table._test_fail_alloc_after(-1)

    assert _dump(table) == before
    assert store.get("k0") == "v0"


@pytest.mark.parametrize("path", ["update-then-cleanup", "cleanup-then-update", "resize-then-cleanup"])
def test_s_t5_update_cleanup_resize(table, epoch_clock, path):
    """갱신과 청소를 두 순서로 실행하고 확장 후 청소해도 새 값과 E=130을 유지한다. 청소는 확장 후 현재 테이블을 순회하고 옛 버킷 포인터를 쓰지 않는다."""
    epoch_clock.set(100)
    store = Store(table, clock=epoch_clock)
    assert store.set("k", "old", ttl=10) is True  # 옛 E=110
    assert store.set("gone", "gone", ttl=5) is True  # E=105, t=110에 만료
    for i in range(10):
        assert store.set(f"f{i}", f"v{i}") is True
    assert _dump(table)[1:] == (12, 16)  # 다음 신규 키에서 확장한다

    kept = [(f"f{i}".encode(), f"v{i}".encode(), None) for i in range(10)]
    epoch_clock.set(110)
    if path == "update-then-cleanup":
        assert store.set("k", "new", ttl=20) is True
        store.cleanup()
        expected = (sorted(kept + [(b"k", b"new", 130.0)]), 11, 16)
    elif path == "cleanup-then-update":
        store.cleanup()
        assert store.set("k", "new", ttl=20) is True
        expected = (sorted(kept + [(b"k", b"new", 130.0)]), 11, 16)
    else:
        assert store.set("k", "new", ttl=20) is True
        assert _dump(table)[1:] == (12, 16)  # 갱신은 개수를 늘리지 않는다
        assert store.set("k12", "v12") is True
        assert _dump(table)[1:] == (13, 32)  # 바로 직전에 확장했다
        store.cleanup()
        expected = (sorted(kept + [(b"k", b"new", 130.0), (b"k12", b"v12", None)]), 12, 32)

    assert _dump(table) == expected
    assert store.get("k") == "new"
