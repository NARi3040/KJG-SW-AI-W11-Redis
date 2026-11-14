"""공통 테스트 준비. 명세: docs/planning.md 4.1·5.3절, docs/store-and-snapshots.md, docs/http-and-runtime.md

FakeClock과 epoch·monotonic 시계, 임시 데이터 경로 fixture를 제공한다.
작업자 종료 fixture와 C 실패 주입 정리 fixture는 각 구현 담당이 추가한다(planning 6.2절, http-and-runtime.md 대상 파일 절).
대체 저장 엔진을 구현하지 않는다.
"""

from pathlib import Path

import pytest


class FakeClock:
    """테스트가 직접 진행시키는 epoch·monotonic 시계. 소수 초를 그대로 유지한다."""

    def __init__(self, start: float) -> None:
        self._value = start

    def __call__(self) -> float:
        """현재 값을 반환한다. Callable[[], float] 자리에 그대로 넣을 수 있다."""
        return self._value

    def advance(self, seconds: float) -> None:
        """현재 값에 초를 더한다. 음수는 시각을 뒤로 돌린다."""
        self._value += seconds

    def set(self, value: float) -> None:
        """현재 값을 주어진 값으로 바꾼다."""
        self._value = value


@pytest.fixture
def epoch_clock() -> FakeClock:
    """항목 만료 시각용 epoch 시계를 1_700_000_000.0에서 시작한다."""
    return FakeClock(1_700_000_000.0)


@pytest.fixture
def monotonic_clock() -> FakeClock:
    """반복 기한용 monotonic 시계를 1_000.0에서 시작한다."""
    return FakeClock(1_000.0)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Runtime과 스냅샷 시험이 함께 쓰는 임시 데이터 경로를 만들고 절대 경로로 반환한다."""
    path = tmp_path / "data"
    path.mkdir()
    return path
