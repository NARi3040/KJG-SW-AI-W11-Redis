"""Store 사례 뼈대. 사례 정의: docs/store-and-snapshots.md, T17·T19·T20은 docs/c-hashtable-and-bindings.md

MR-00 뼈대: 사례마다 자리만 두고 NotImplementedError로 실패시킨다. 통과로 표시하지 않는다.
"""


def test_t1_new_set_get():
    """저장 True, 조회 "v". TTL 생략이므로 무기한이다."""
    raise NotImplementedError


def test_t2_missing_key():
    """없는 키 조회는 None이고 상태 변화가 없다."""
    raise NotImplementedError


def test_t3_empty_strings():
    """빈 키에 빈 값은 True, 조회는 "", 없는 키 조회는 None. 빈 값과 키 없음을 구분한다."""
    raise NotImplementedError


def test_t4_exact_string_identity():
    """"A" 저장 뒤 "a", " A " 조회는 모두 None. 공백 제거와 대소문자 변환이 없다."""
    raise NotImplementedError


def test_t5_overwrite_without_ttl():
    """두 저장 모두 True, 조회 "new", 무기한이다."""
    raise NotImplementedError


def test_t6_expiry_boundary():
    """t=100에 TTL 10 저장 후 109.999는 "v", 110과 111은 None. 110 조회에서 값과 만료 정보를 제거한다."""
    raise NotImplementedError


def test_t7_ttl_replace():
    """t=100에 TTL 10으로 저장한 뒤 105에 TTL 20으로 덮어쓰면 True, 새 E=125. 110은 "new", 125는 None이다."""
    raise NotImplementedError


def test_t8_ttl_remove():
    """TTL 없이 덮어쓰면 True, 110 청소 후에도 "new"와 무기한. 과거 TTL로 제거되지 않는다."""
    raise NotImplementedError


def test_t9_ttl_add():
    """무기한 키를 t=100에 TTL 1로 덮어쓰면 True, 101 조회는 None이다."""
    raise NotImplementedError


def test_t10_delete_and_repeat_delete():
    """삭제 True, 조회 None, 재삭제 False. 없는 다른 키 삭제도 False이며 값과 만료 정보를 함께 제거한다."""
    raise NotImplementedError


def test_t11_delete_expired_key():
    """t=100에 TTL 1 저장 후 청소와 조회 없이 101에 삭제하면 False. 남아 있던 물리 값도 제거한다."""
    raise NotImplementedError


def test_t12_set_again_after_expiry_or_delete():
    """TTL 만료 뒤와 명시적 삭제 뒤 각각 다시 저장하면 True, 조회 "new", 과거 만료 정보가 없다."""
    raise NotImplementedError


def test_t15_cleanup_targets():
    """t=100에 A는 TTL 1, B는 TTL 10, C는 무기한 저장 후 101 청소. A만 제거되고 B, C는 값이 남는다."""
    raise NotImplementedError


def test_t16_update_cleanup_order():
    """옛 E=110, 판정 시각 110에서 갱신과 청소를 두 순서로 실행해도 최종 값은 "new", E=130이다."""
    raise NotImplementedError


def test_t17_collision_through_store():
    """16버킷에서 같은 버킷인 "a", "q" 중 "a"만 덮어쓰기·삭제해도 "q"의 값과 TTL 유지."""
    raise NotImplementedError


def test_t18_internal_failure_propagates():
    """조회·저장·삭제의 테이블 접근 실패는 예외로 전달하고 None, False, True로 숨기지 않는다. 처리 가능한 할당·복사 실패는 변경 전 상태를 유지하고, 상태를 신뢰할 수 없는 오류는 중단한다."""
    raise NotImplementedError


def test_t19_resize_through_store():
    """12개까지 16버킷, 13번째 신규 키 전에 32버킷. 기존 값·TTL 유지."""
    raise NotImplementedError


def test_t20_resize_alloc_failure_through_store():
    """확장 준비 할당 실패는 MemoryError, 새 키 미저장, 기존 상태 보존."""
    raise NotImplementedError


def test_s_t5_update_cleanup_resize():
    """갱신과 청소를 두 순서로 실행하고 확장 후 청소해도 새 값과 E=130을 유지한다. 청소는 확장 후 현재 테이블을 순회하고 옛 버킷 포인터를 쓰지 않는다."""
    raise NotImplementedError
