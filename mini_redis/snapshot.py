"""스냅샷 파일 함수 뼈대. 명세: docs/store-and-snapshots.md

MR-00 뼈대: 두 함수 본문을 NotImplementedError로 중단한다. 살아 있는 C 테이블을 받지 않는다.
"""

from __future__ import annotations

from pathlib import Path

from .contracts import Entry


def read_snapshot(path: Path) -> list[Entry] | None:
    """정식 스냅샷 파일 전체를 검증하고 독립 문자열 항목 목록을 반환한다.

    None은 정식 파일이 없어 빈 시작을 뜻한다. 빈 리스트는 유효한 빈 스냅샷 파일을 뜻한다.
    만료 항목도 검증하며 시간 기준 필터링은 하지 않는다.
    """
    raise NotImplementedError


def write_snapshot(path: Path, entries: list[Entry]) -> None:
    """임시 파일 기록, 동기화, 교체를 마친 뒤 반환한다.

    스냅샷 시각을 다시 읽거나 항목을 다시 거르지 않는다.
    """
    raise NotImplementedError
