"""실제 C 라이브러리와 Store 연결 시험. 사례 정의: docs/c-hashtable-and-bindings.md

MR-00 뼈대: 사례마다 자리만 두고 NotImplementedError로 실패시킨다. 통과로 표시하지 않는다.
"""


def test_s_t4_string_roundtrip_through_store():
    """S-T4 문자열을 실제 HashTable 위의 Store로 저장·조회."""
    raise NotImplementedError


def test_c_allocation_failure_preserves_store_state():
    """실제 C 할당 실패가 Store까지 MemoryError로 전달되고 기존 상태 보존."""
    raise NotImplementedError


def test_copy_lifetime_through_store():
    """Store가 받은 복사본이 테이블 변경·close 뒤에도 유효."""
    raise NotImplementedError
