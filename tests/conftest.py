"""공통 테스트 준비. 명세: docs/planning.md 4.1·5.3절, docs/store-and-snapshots.md, docs/http-and-runtime.md

FakeClock과 epoch·monotonic 시계, 임시 데이터 경로 fixture를 제공한다.
작업자 종료 fixture와 C 실패 주입 정리 fixture는 각 구현 담당이 추가한다(planning 6.2절, http-and-runtime.md 대상 파일 절).
대체 저장 엔진을 구현하지 않는다.
"""

from pathlib import Path

import pytest


class FakeClock:
    """테스트가 직접 진행시키는 시계. Store·Runtime의 시계 인자에 그대로 넣고 소수 초를 유지한다."""

    def __init__(self, start: float) -> None:
        self._value = start

    def __call__(self) -> float:
        return self._value

    def advance(self, seconds: float) -> None:
        """음수를 주면 시각을 뒤로 돌린다."""
        self._value += seconds

    def set(self, value: float) -> None:
        self._value = value


@pytest.fixture
def epoch_clock() -> FakeClock:
    """항목 만료 시각을 판정하는 epoch 시계."""
    return FakeClock(1_700_000_000.0)


@pytest.fixture
def monotonic_clock() -> FakeClock:
    """청소·스냅샷 반복 기한을 판정하는 monotonic 시계."""
    return FakeClock(1_000.0)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Runtime과 스냅샷 시험이 함께 쓰는 임시 데이터 경로."""
    path = tmp_path / "data"
    path.mkdir()
    return path
