"""C 해시 테이블 ctypes 연결. 명세: docs/c-hashtable-and-bindings.md"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

HT_OK = 0
HT_NOT_FOUND = 1
HT_NOMEM = 2
HT_INVALID = 4

_LIBRARY_PATH = Path(__file__).resolve().with_name(
    "_hashtable.dll" if os.name == "nt" else "_hashtable.so"
)


class HtValue(ctypes.Structure):
    _fields_ = [
        ("data", ctypes.c_void_p),
        ("length", ctypes.c_size_t),
        ("has_expiry", ctypes.c_int),
        ("expires_at", ctypes.c_double),
    ]


class HtEntry(ctypes.Structure):
    _fields_ = [
        ("key", ctypes.c_void_p),
        ("key_length", ctypes.c_size_t),
        ("value", HtValue),
    ]


class HtSnapshot(ctypes.Structure):
    _fields_ = [
        ("entries", ctypes.POINTER(HtEntry)),
        ("count", ctypes.c_size_t),
    ]


_c_size_p = ctypes.POINTER(ctypes.c_size_t)
_c_int_p = ctypes.POINTER(ctypes.c_int)

# 이름: (restype, argtypes)
_SIGNATURES = {
    "ht_create": (ctypes.c_int, [ctypes.POINTER(ctypes.c_void_p)]),
    "ht_destroy": (None, [ctypes.c_void_p]),
    "ht_put": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                              ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_double]),
    "ht_is_live": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                  ctypes.c_double, _c_int_p]),
    "ht_get_copy": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                   ctypes.POINTER(HtValue)]),
    "ht_remove": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, _c_int_p]),
    "ht_remove_expired": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_double]),
    "ht_copy_live": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_double, ctypes.POINTER(HtSnapshot)]),
    "ht_free_value": (None, [ctypes.POINTER(HtValue)]),
    "ht_free_snapshot": (None, [ctypes.POINTER(HtSnapshot)]),
}

# 시험 빌드(MINI_REDIS_TESTING=1)에만 있는 함수
_TEST_SIGNATURES = {
    "ht_test_fail_alloc_after": (None, [ctypes.c_int64]),
    "ht_test_dump": (ctypes.c_int, [ctypes.c_void_p, ctypes.POINTER(HtSnapshot), _c_size_p, _c_size_p]),
}

_lib: ctypes.CDLL | None = None


def _load_library() -> ctypes.CDLL:
    """첫 HashTable 생성 때 호출한다. 로딩 실패는 OSError 등 원래 예외로 전달한다."""
    global _lib
    if _lib is None:
        lib = ctypes.CDLL(str(_LIBRARY_PATH))
        for name, (restype, argtypes) in _SIGNATURES.items():
            func = getattr(lib, name)
            func.restype, func.argtypes = restype, argtypes
        for name, (restype, argtypes) in _TEST_SIGNATURES.items():
            func = getattr(lib, name, None)  # 일반 빌드에는 없음. 없음을 정상 결과로 대체하지 않는다.
            if func is not None:
                func.restype, func.argtypes = restype, argtypes
        _lib = lib
    return _lib


def _check(status: int) -> None:
    """C 상태 코드를 Python 예외로 바꾼다. HT_NOT_FOUND 처리는 ht_get_copy 호출부가 맡는다."""
    if status == HT_OK:
        return
    if status == HT_NOMEM:
        raise MemoryError("hashtable allocation failed")
    if status == HT_INVALID:
        raise RuntimeError("invalid hashtable call")
    raise RuntimeError(f"unexpected hashtable status {status}")


class HashTable:
    def __init__(self) -> None:
        lib = _load_library()
        self._lib = lib
        handle = ctypes.c_void_p()
        _check(lib.ht_create(ctypes.byref(handle)))
        self._handle: ctypes.c_void_p | None = handle

    def put(self, key: bytes, value: bytes, expires_at: float | None) -> None:
        _check(self._lib.ht_put(self._open(), key, len(key), value, len(value),
                               int(expires_at is not None),
                               0 if expires_at is None else expires_at))

    def is_live(self, key: bytes, cutoff: float) -> bool:
        result = ctypes.c_int()
        _check(self._lib.ht_is_live(self._open(), key, len(key), cutoff, ctypes.byref(result)))
        return bool(result.value)

    def get(self, key: bytes) -> tuple[bytes, float | None] | None:
        result = HtValue()
        try:
            status = self._lib.ht_get_copy(self._open(), key, len(key), ctypes.byref(result))
            if status == HT_NOT_FOUND:
                return None
            _check(status)
            return self._value(result)
        finally:
            self._lib.ht_free_value(ctypes.byref(result))

    def remove(self, key: bytes) -> bool:
        result = ctypes.c_int()
        _check(self._lib.ht_remove(self._open(), key, len(key), ctypes.byref(result)))
        return bool(result.value)

    def remove_expired(self, cutoff: float) -> None:
        _check(self._lib.ht_remove_expired(self._open(), cutoff))

    def copy_live(self, cutoff: float) -> list[tuple[bytes, bytes, float | None]]:
        result = HtSnapshot()
        try:
            _check(self._lib.ht_copy_live(self._open(), cutoff, ctypes.byref(result)))
            return self._entries(result)
        finally:
            self._lib.ht_free_snapshot(ctypes.byref(result))

    def close(self) -> None:
        if self._handle is not None:
            self._lib.ht_destroy(self._handle)
            self._handle = None

    def _test_fail_alloc_after(self, successful_allocations: int) -> None:
        self._open()
        self._lib.ht_test_fail_alloc_after(successful_allocations)

    def _test_dump(self) -> tuple[list[tuple[bytes, bytes, float | None]], int, int]:
        result = HtSnapshot()
        count, buckets = ctypes.c_size_t(), ctypes.c_size_t()
        try:
            _check(self._lib.ht_test_dump(self._open(), ctypes.byref(result),
                                         ctypes.byref(count), ctypes.byref(buckets)))
            return self._entries(result), count.value, buckets.value
        finally:
            self._lib.ht_free_snapshot(ctypes.byref(result))

    def _open(self) -> ctypes.c_void_p:
        if self._handle is None:
            raise RuntimeError("hashtable is closed")
        return self._handle

    @staticmethod
    def _value(value: HtValue) -> tuple[bytes, float | None]:
        data = ctypes.string_at(value.data, value.length) if value.length else b""
        return data, value.expires_at if value.has_expiry else None

    @classmethod
    def _entries(cls, snapshot: HtSnapshot) -> list[tuple[bytes, bytes, float | None]]:
        result = []
        for i in range(snapshot.count):
            entry = snapshot.entries[i]
            key = ctypes.string_at(entry.key, entry.key_length) if entry.key_length else b""
            value, expiry = cls._value(entry.value)
            result.append((key, value, expiry))
        return result
