"""Store 구현 뼈대. 명세: docs/store-and-snapshots.md, docs/planning.md 3.2·5.4절

MR-00 뼈대: 생성자는 입력만 보관하고, 다섯 메서드는 NotImplementedError로 중단한다.
True, None, 빈 리스트 같은 가짜 성공값을 반환하지 않는다.
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
        raise NotImplementedError

    def get(self, key: str) -> str | None:
        """검증된 키의 값을 반환한다. 없거나 만료됐으면 None이다."""
        raise NotImplementedError

    def delete(self, key: str) -> bool:
        """검증된 키를 삭제하고 유효한 항목을 제거했으면 True를 반환한다."""
        raise NotImplementedError

    def cleanup(self) -> None:
        """판정 시각 기준으로 만료된 물리 항목 전체를 제거한다."""
        raise NotImplementedError

    def snapshot_entries(self) -> list[Entry]:
        """해당 연산 시각에 유효한 문자열 항목의 독립 복사본 목록을 반환한다."""
        raise NotImplementedError
