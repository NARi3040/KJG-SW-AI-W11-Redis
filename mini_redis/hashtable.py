"""C 해시 테이블 ctypes 연결. 명세: docs/c-hashtable-and-bindings.md

MR-00 뼈대: 구조체와 함수 선언은 명세대로 두고, 데이터 메서드 본문은 NotImplementedError로 중단한다.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

HT_OK = 0
HT_NOT_FOUND = 1
HT_NOMEM = 2
HT_INVALID = 4
HT_UNIMPLEMENTED = 5  # 뼈대 전용. 모든 C 함수 구현 뒤 제거한다.

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
    if status == HT_UNIMPLEMENTED:
        raise NotImplementedError("hashtable C function is not implemented")
    raise RuntimeError(f"unexpected hashtable status {status}")


class HashTable:
    def __init__(self) -> None:
        lib = _load_library()
        handle = ctypes.c_void_p()
        _check(lib.ht_create(ctypes.byref(handle)))
        self._handle: ctypes.c_void_p | None = handle

    def put(self, key: bytes, value: bytes, expires_at: float | None) -> None:
        raise NotImplementedError

    def is_live(self, key: bytes, cutoff: float) -> bool:
        raise NotImplementedError

    def get(self, key: bytes) -> tuple[bytes, float | None] | None:
        raise NotImplementedError

    def remove(self, key: bytes) -> bool:
        raise NotImplementedError

    def remove_expired(self, cutoff: float) -> None:
        raise NotImplementedError

    def copy_live(self, cutoff: float) -> list[tuple[bytes, bytes, float | None]]:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def _test_fail_alloc_after(self, successful_allocations: int) -> None:
        raise NotImplementedError

    def _test_dump(self) -> tuple[list[tuple[bytes, bytes, float | None]], int, int]:
        raise NotImplementedError
