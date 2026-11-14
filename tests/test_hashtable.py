"""Actual C/ctypes tests; requires a MINI_REDIS_TESTING=1 build."""

import ctypes as C
from pathlib import Path
import shutil
import subprocess

import pytest

from mini_redis import hashtable as h


@pytest.fixture
def table():
    table = h.HashTable()
    table._test_fail_alloc_after(-1)
    try:
        yield table
    finally:
        table._lib.ht_test_fail_alloc_after(-1)
        table.close()


def state(table):
    entries, n, m = table._test_dump()
    return sorted(entries), n, m


def fill(table):
    entries = [(b"a", b"A", 110.5), (b"q", b"Q", 120.5)]
    entries += [(f"k{i}".encode(), f"v{i}".encode(), None) for i in range(10)]
    for entry in entries:
        table.put(*entry)
    return entries


def test_t17_colliding_keys(table):
    table.put(b"a", b"A", 110.5)
    table.put(b"q", b"Q", 120.5)
    table.put(b"a", b"new", None)
    assert table.get(b"a") == (b"new", None)
    assert table.remove(b"a") is True
    assert table.get(b"q") == (b"Q", 120.5)
    assert state(table) == ([(b"q", b"Q", 120.5)], 1, 16)


def test_t19_resize_and_rehash(table):
    entries = fill(table)
    assert state(table) == (sorted(entries), 12, 16)
    table.put(b"13", b"new", 130.5)
    assert state(table) == (sorted(entries + [(b"13", b"new", 130.5)]), 13, 32)
    for key, value, expiry in entries:
        assert table.get(key) == (value, expiry)
    table.put(b"a", b"changed", None)
    assert table.remove(b"q")
    assert table.get(b"a") == (b"changed", None)
    assert state(table)[1:] == (12, 32)


@pytest.mark.parametrize("site", range(4))
def test_t20_resize_allocation_failure(table, site):
    fill(table)
    before = state(table)
    table._test_fail_alloc_after(site)
    with pytest.raises(MemoryError):
        table.put(b"13", b"value", 150.5)
    table._test_fail_alloc_after(-1)
    assert state(table) == before
    assert table.get(b"13") is None
    assert table.get(b"a") == (b"A", 110.5)
    assert table.remove(b"q")


def test_s_t2_resize_and_overwrite(table):
    fill(table)
    table.put(b"a", b"replacement", None)
    assert state(table)[1:] == (12, 16)
    table.put(b"13", b"new", None)
    assert state(table)[1:] == (13, 32)
    assert table.get(b"a") == (b"replacement", None)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("site", range(3))
def test_s_t3_allocation_failure_per_site(table, existing, site):
    table.put(b"a", b"expired", 90.5)
    table.put(b"q", b"live", 110.5)
    before = state(table)
    table._test_fail_alloc_after(site)
    with pytest.raises(MemoryError):
        table.put(b"a" if existing else b"new", b"replacement", 200.5)
    table._test_fail_alloc_after(-1)
    assert state(table) == before
    assert table.get(b"a") == (b"expired", 90.5)
    assert table.remove(b"a")
    assert table.get(b"q") == (b"live", 110.5)


STRINGS = ["a", "q", "", "한글", "a\0b", "a\0c", "\U0001f600", "\ud83d\ude00", "\ud800"]


def test_s_t4_string_roundtrip(table):
    expected = []
    for i, text in enumerate(STRINGS):
        key = text.encode("utf-8", "surrogatepass")
        value = (text + "\0끝").encode("utf-8", "surrogatepass")
        expiry = 200.5 + i
        table.put(key, value, expiry)
        expected.append((key, value, expiry))
    for key, value, expiry in expected:
        assert table.get(key) == (value, expiry)
        assert value.decode("utf-8", "surrogatepass").endswith("\0끝")
    copied = table.copy_live(100)
    assert sorted(copied) == sorted(expected)
    assert state(table)[1:] == (len(STRINGS), 16)
    # File persistence is deferred to MR-07/MR-08, not simulated here.


def test_s_t13_surrogate_keys(table):
    keys = ["\U0001f600", "\ud83d\ude00"]
    assert keys[0] != keys[1]
    for key in keys:
        encoded = key.encode("utf-8", "surrogatepass")
        table.put(encoded, encoded, None)
    for key in keys:
        encoded = key.encode("utf-8", "surrogatepass")
        value, expiry = table.get(encoded)
        assert value.decode("utf-8", "surrogatepass") == key
        assert expiry is None
    assert state(table)[1] == 2


def test_b1_minimal_c_connection(table):
    assert state(table) == ([], 0, 16)
    assert table.put(b"k", b"v", None) is None
    assert table.get(b"k") == (b"v", None)
    assert table.is_live(b"k", 100)
    assert table.remove(b"k")
    assert table.get(b"k") is None
    assert table.is_live(b"k", 100) is False
    assert table.remove(b"k") is False
    value, flag = h.HtValue(), C.c_int(99)
    lib = table._lib
    assert lib.ht_get_copy(table._handle, b"k", 1, C.byref(value)) == h.HT_NOT_FOUND
    assert lib.ht_is_live(table._handle, b"k", 1, 100, C.byref(flag)) == h.HT_OK
    assert flag.value == 0
    assert lib.ht_remove(table._handle, b"k", 1, C.byref(flag)) == h.HT_OK
    assert flag.value == 0


def test_b2_abi_and_declarations(table, tmp_path):
    root = Path(__file__).resolve().parents[1]
    exe = tmp_path / "abi_probe.exe"
    if shutil.which("cl"):
        command = ["cl", "/nologo", "/std:c11", f"/I{root / 'src'}",
                   str(root / "tests/abi_probe.c"), f"/Fe{exe}",
                   f"/Fo{tmp_path / 'abi_probe.obj'}"]
    else:
        compiler = shutil.which("gcc")
        assert compiler, "B2 requires the documented C compiler to run abi_probe"
        command = [compiler, "-std=c11", f"-I{root / 'src'}", "-o", str(exe),
                   str(root / "tests/abi_probe.c")]
    subprocess.run(command, check=True, capture_output=True, text=True)
    output = subprocess.check_output([str(exe)], text=True)
    actual = {name: int(number) for name, number in
              (line.split() for line in output.splitlines())}
    expected = {}
    for struct in (h.HtValue, h.HtEntry, h.HtSnapshot):
        expected[f"sizeof({struct.__name__})"] = C.sizeof(struct)
        for name, _ in struct._fields_:
            expected[f"offsetof({struct.__name__}.{name})"] = getattr(struct, name).offset
    assert actual == expected
    assert [C.sizeof(s) for s in (h.HtValue, h.HtEntry, h.HtSnapshot)] == [32, 48, 16]
    assert h.HtValue.expires_at.offset == 24
    assert h.HtEntry.value.offset == 16
    for name, (restype, argtypes) in (h._SIGNATURES | h._TEST_SIGNATURES).items():
        function = getattr(table._lib, name)
        assert function.restype is restype
        assert function.argtypes == argtypes


def test_b3_copy_and_free(table):
    table.put(b"k", b"value\0end", 120.5)
    value, snapshot = h.HtValue(), h.HtSnapshot()
    lib = table._lib
    assert lib.ht_get_copy(table._handle, b"k", 1, C.byref(value)) == h.HT_OK
    assert lib.ht_copy_live(table._handle, 100, C.byref(snapshot)) == h.HT_OK
    python_value = table.get(b"k")
    python_snapshot = table.copy_live(100)
    table.put(b"k", b"changed", None)
    table.close()
    try:
        assert C.string_at(value.data, value.length) == b"value\0end"
        assert C.string_at(snapshot.entries[0].key, 1) == b"k"
        assert h.HashTable._entries(snapshot) == [(b"k", b"value\0end", 120.5)]
        assert python_value == (b"value\0end", 120.5)
        assert python_snapshot == [(b"k", b"value\0end", 120.5)]
    finally:
        lib.ht_free_value(C.byref(value))
        lib.ht_free_snapshot(C.byref(snapshot))
    assert not value.data and value.length == value.has_expiry == value.expires_at == 0
    assert not snapshot.entries and snapshot.count == 0
    lib.ht_free_value(C.byref(value))
    lib.ht_free_snapshot(C.byref(snapshot))
    lib.ht_free_value(None)
    lib.ht_free_snapshot(None)
    lib.ht_destroy(None)


@pytest.mark.parametrize(("operation", "copy_site"),
                         [("get", 0), ("copy_live", 0), ("copy_live", 1),
                          ("_test_dump", 0), ("_test_dump", 1)])
def test_b3_python_copy_failure_frees_c_output(table, monkeypatch, operation, copy_site):
    table.put(b"k", b"value", 120.5)
    original_copy = C.string_at
    original_free_value = table._lib.ht_free_value
    original_free_snapshot = table._lib.ht_free_snapshot
    freed = []
    calls = 0

    def fail_copy(*args):
        nonlocal calls
        calls += 1
        if calls > copy_site:
            raise MemoryError("Python copy")
        return original_copy(*args)

    def free_value(pointer):
        original_free_value(pointer)
        result = C.cast(pointer, C.POINTER(h.HtValue)).contents
        freed.append(not result.data and result.length == 0)

    def free_snapshot(pointer):
        original_free_snapshot(pointer)
        result = C.cast(pointer, C.POINTER(h.HtSnapshot)).contents
        freed.append(not result.entries and result.count == 0)

    with monkeypatch.context() as patch:
        patch.setattr(C, "string_at", fail_copy)
        patch.setattr(table._lib, "ht_free_value", free_value)
        patch.setattr(table._lib, "ht_free_snapshot", free_snapshot)
        with pytest.raises(MemoryError, match="Python copy"):
            if operation == "get":
                table.get(b"k")
            elif operation == "copy_live":
                table.copy_live(100)
            else:
                table._test_dump()
    assert freed == [True]
    assert table.get(b"k") == (b"value", 120.5)


def test_b4_empty_buffers_and_output_failure(table):
    lib = table._lib
    snapshot = h.HtSnapshot()
    assert lib.ht_copy_live(table._handle, 100, C.byref(snapshot)) == h.HT_OK
    assert not snapshot.entries and snapshot.count == 0
    assert lib.ht_put(table._handle, None, 0, None, 0, 0, 0) == h.HT_OK
    assert table.get(b"") == (b"", None)
    assert table.copy_live(100) == [(b"", b"", None)]
    table.put(b"k", b"value", 120.5)
    before = state(table)
    for site in range(2):
        handle = C.c_void_p(123)
        table._test_fail_alloc_after(site)
        assert lib.ht_create(C.byref(handle)) == h.HT_NOMEM
        assert handle.value is None
    value = h.HtValue()
    table._test_fail_alloc_after(0)
    assert lib.ht_get_copy(table._handle, b"k", 1, C.byref(value)) == h.HT_NOMEM
    assert bytes(value) == bytes(C.sizeof(value))
    # Array, key and value copying, including a later partially populated entry.
    table.put(b"q", b"other", None)
    for site in range(5):
        table._test_fail_alloc_after(site)
        assert lib.ht_copy_live(table._handle, 100, C.byref(snapshot)) == h.HT_NOMEM
        assert not snapshot.entries and snapshot.count == 0
    table._test_fail_alloc_after(-1)
    assert state(table) == (sorted(before[0] + [(b"q", b"other", None)]), 3, 16)


@pytest.mark.parametrize("site", range(2))
def test_b4_python_create_failure(table, site):
    table._test_fail_alloc_after(site)
    with pytest.raises(MemoryError):
        h.HashTable()
    other = h.HashTable()
    other.close()


@pytest.mark.parametrize("site", range(5))
def test_b4_dump_partial_failure_clears_all_outputs(table, site):
    table.put(b"a", b"A", 90)
    table.put(b"q", b"Q", None)
    before = state(table)
    snapshot = h.HtSnapshot()
    count, buckets = C.c_size_t(99), C.c_size_t(99)
    table._test_fail_alloc_after(site)
    assert table._lib.ht_test_dump(table._handle, C.byref(snapshot),
                                   C.byref(count), C.byref(buckets)) == h.HT_NOMEM
    assert not snapshot.entries and snapshot.count == 0
    assert count.value == buckets.value == 0
    table._test_fail_alloc_after(-1)
    assert state(table) == before


def test_b5_lifetime(table):
    table.close()
    table.close()
    for call in (lambda: table.put(b"k", b"v", None), lambda: table.get(b"k"),
                 lambda: table.is_live(b"k", 100), lambda: table.remove(b"k"),
                 lambda: table.remove_expired(100), lambda: table.copy_live(100),
                 table._test_dump, lambda: table._test_fail_alloc_after(-1)):
        with pytest.raises(RuntimeError, match="closed"):
            call()


def test_b6_test_hooks(table):
    table._test_fail_alloc_after(0)
    with pytest.raises(MemoryError):
        table.put(b"k", b"v", None)
    table.put(b"k", b"v", None)  # one-shot reset
    table._test_fail_alloc_after(3)
    table.put(b"a", b"A", 90)
    with pytest.raises(MemoryError):
        table.put(b"q", b"Q", None)
    table.put(b"q", b"Q", None)
    table._test_fail_alloc_after(0)
    table._test_fail_alloc_after(-1)
    assert state(table) == ([(b"a", b"A", 90), (b"k", b"v", None), (b"q", b"Q", None)], 3, 16)


def test_cutoff_is_nonmutating_until_cleanup(table):
    for key, expiry in [(b"a", 99.5), (b"q", 100), (b"future", 100.5), (b"forever", None)]:
        table.put(key, key, expiry)
    before = state(table)
    table._test_fail_alloc_after(0)
    assert not table.is_live(b"a", 100)
    assert not table.is_live(b"q", 100)
    assert table.is_live(b"future", 100)
    assert table.is_live(b"forever", 100)
    table._test_fail_alloc_after(-1)
    assert sorted(table.copy_live(100)) == [(b"forever", b"forever", None), (b"future", b"future", 100.5)]
    assert state(table) == before
    table._test_fail_alloc_after(0)
    assert table.remove_expired(100) is None
    assert table.remove(b"future")
    table._test_fail_alloc_after(-1)
    assert state(table) == ([(b"forever", b"forever", None)], 1, 16)


@pytest.mark.parametrize("status,exception", [(h.HT_NOMEM, MemoryError),
                                            (h.HT_INVALID, RuntimeError),
                                            (99, RuntimeError),
                                            (h.HT_NOT_FOUND, RuntimeError)])
def test_error_mapping(status, exception):
    with pytest.raises(exception):
        h._check(status)
