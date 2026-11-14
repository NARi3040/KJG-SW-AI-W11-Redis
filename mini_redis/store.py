"""Store 구현. 명세: docs/store-and-snapshots.md, docs/planning.md 3.2·5.4절

검증된 입력만 받아 저장, 조회, 삭제, TTL 계산과 만료 처리를 맡는다.
연산마다 epoch 시계를 한 번 읽고 그 판정 시각을 다시 쓴다.
C 테이블 참조를 받아 쓰기만 하며 테이블을 만들거나 닫지 않는다.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Callable

from .contracts import Entry

if TYPE_CHECKING:
    from .hashtable import HashTable


class Store:
    def __init__(self, table: HashTable, *, clock: Callable[[], float] = time.time) -> None:
        """작업자가 생성한 테이블 참조와 epoch 시계를 보관한다.

        시계를 호출하거나 테이블 메서드를 부르지 않는다.
        """
        self._table = table
        self._clock = clock

    def set(self, key: str, value: str, *, ttl: int | None = None) -> bool:
        """검증된 키, 값을 저장하고 값을 반영하면 True를 반환한다."""
        k = key.encode("utf-8", "surrogatepass")
        v = value.encode("utf-8", "surrogatepass")
        t = self._clock()
        expires_at = None if ttl is None else t + ttl
        self._table.put(k, v, expires_at)
        return True

    def get(self, key: str) -> str | None:
        """검증된 키의 값을 반환한다. 없거나 만료됐으면 None이다."""
        k = key.encode("utf-8", "surrogatepass")
        t = self._clock()
        if not self._table.is_live(k, t):
            self._table.remove(k)
            return None
        found = self._table.get(k)
        return found[0].decode("utf-8", "surrogatepass")

    def delete(self, key: str) -> bool:
        """검증된 키를 삭제하고 유효한 항목을 제거했으면 True를 반환한다."""
        k = key.encode("utf-8", "surrogatepass")
        t = self._clock()
        live = self._table.is_live(k, t)
        self._table.remove(k)
        return live

    def cleanup(self) -> None:
        """판정 시각 기준으로 만료된 물리 항목 전체를 제거한다."""
        t = self._clock()
        self._table.remove_expired(t)

    def snapshot_entries(self) -> list[Entry]:
        """해당 연산 시각에 유효한 문자열 항목의 독립 복사본 목록을 반환한다."""
        t = self._clock()
        return [
            (key.decode("utf-8", "surrogatepass"), value.decode("utf-8", "surrogatepass"), expires_at)
            for key, value, expires_at in self._table.copy_live(t)
        ]
