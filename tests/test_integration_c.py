"""Actual Store/C integration; MR-04 blockers must fail, never silently skip."""

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


def test_s_t4_string_roundtrip_through_store(table):
    store = Store(table, clock=lambda: 100.5)
    strings = ["a", "q", "", "한글", "a\0b", "a\0c", "\U0001f600", "\ud83d\ude00", "\ud800"]
    for text in strings:
        assert store.set(text, text + "\0끝", ttl=20) is True
    for text in strings:
        assert store.get(text) == text + "\0끝"
    assert sorted(store.snapshot_entries()) == sorted((text, text + "\0끝", 120.5) for text in strings)


@pytest.mark.parametrize(("existing", "site"),
                         [(False, 0), (False, 1), (False, 2), (False, 3),
                          (True, 0), (True, 1), (True, 2)])
def test_c_allocation_failure_preserves_store_state(table, existing, site):
    now = [100.5]
    store = Store(table, clock=lambda: now[0])
    for i in range(12):
        assert store.set(f"k{i}", f"v{i}", ttl=10) is True
    now[0] = 111
    before = table._test_dump()
    table._test_fail_alloc_after(site)
    with pytest.raises(MemoryError):
        store.set("k0" if existing else "new", "new", ttl=20)
    table._test_fail_alloc_after(-1)
    assert table._test_dump() == before
    now[0] = 100.5
    assert store.get("k0") == "v0"
    assert store.delete("k1") is True


def test_copy_lifetime_through_store(table):
    store = Store(table, clock=lambda: 100.5)
    assert store.set("k", "value\0끝", ttl=20)
    value = store.get("k")
    entries = store.snapshot_entries()
    assert store.set("k", "changed")
    table.close()
    assert value == "value\0끝"
    assert entries == [("k", "value\0끝", 120.5)]
