"""枝の計算（lines.py）のテスト。DB を使わず、Node の並びだけで確かめる。"""

import pytest

from tsuduri_mcp.lines import Node, check_range, count_leaves, first_unposted, select_line


def at(minute: int) -> str:
    return f"2026-01-01T00:{minute:02d}:00.000000Z"


def nodes(*specs: tuple[str, str | None]) -> list[Node]:
    """(uuid, 親の uuid) を並べた順に、position と created_at（後ほど新しい）を振る。"""
    return [Node(uuid, parent, at(i), i) for i, (uuid, parent) in enumerate(specs)]


#  m0 ─ m1 ─ m2 ─ m3           （m2 が再生成されて m2b に分岐）
#            └ m2b ─ m3b ─ m4b （こちらが新しい）
BRANCHY = nodes(
    ("m0", None),
    ("m1", "m0"),
    ("m2", "m1"),
    ("m3", "m2"),
    ("m2b", "m1"),
    ("m3b", "m2b"),
    ("m4b", "m3b"),
)

#  a0 ─ a1   （元の発言）
#  b0 ─ b1   （最初の発言を編集して作った、別の根。こちらが新しい）
TWO_ROOTS = nodes(("a0", None), ("a1", "a0"), ("b0", None), ("b1", "b0"))


# --- select_line ---


def test_line_defaults_to_newest_branch():
    assert select_line(BRANCHY) == ["m0", "m1", "m2b", "m3b", "m4b"]


def test_line_through_old_branch_follows_it_to_the_end():
    assert select_line(BRANCHY, through_index=2) == ["m0", "m1", "m2", "m3"]


def test_line_through_common_part_takes_newest_continuation():
    assert select_line(BRANCHY, through_index=1) == ["m0", "m1", "m2b", "m3b", "m4b"]


def test_line_unknown_index_is_error():
    with pytest.raises(ValueError, match="index=99"):
        select_line(BRANCHY, through_index=99)


def test_line_of_no_messages_is_empty():
    assert select_line([]) == []


def test_line_with_two_roots_takes_the_newest_root():
    assert select_line(TWO_ROOTS) == ["b0", "b1"]


def test_line_through_old_root_stays_on_that_root():
    assert select_line(TWO_ROOTS, through_index=0) == ["a0", "a1"]


def test_newest_is_decided_by_created_at_not_position():
    # 並び順（position）では後ろでも、created_at が古い枝は本線にならない
    ns = [Node("m0", None, at(0), 0), Node("new", "m0", at(5), 1), Node("old", "m0", at(1), 2)]

    assert select_line(ns) == ["m0", "new"]


def test_same_created_at_is_decided_by_position():
    ns = [Node("m0", None, at(0), 0), Node("x", "m0", at(1), 1), Node("y", "m0", at(1), 2)]

    assert select_line(ns) == ["m0", "y"]


# --- count_leaves ---


def test_leaves_without_branch_is_one():
    assert count_leaves(nodes(("m0", None), ("m1", "m0"))) == 1


def test_leaves_of_branchy_conversation():
    assert count_leaves(BRANCHY) == 2


def test_leaves_of_two_roots():
    assert count_leaves(TWO_ROOTS) == 2


# --- first_unposted ---


def test_first_unposted_without_post_is_the_first():
    assert first_unposted(["a", "b", "c"], set()) == 0


def test_first_unposted_is_right_after_the_last_posted():
    assert first_unposted(["a", "b", "c"], {"a"}) == 1


def test_first_unposted_does_not_go_back_to_a_gap():
    # b は投稿していないが、それより後ろの c が投稿済みなので、c の次から
    assert first_unposted(["a", "b", "c", "d"], {"a", "c"}) == 3


def test_first_unposted_when_all_posted_is_none():
    assert first_unposted(["a", "b"], {"a", "b"}) is None


def test_first_unposted_ignores_posts_on_other_lines():
    assert first_unposted(["b0", "b1"], {"a0", "a1"}) == 0


# --- check_range ---


def test_range_end_defaults_to_the_last():
    assert check_range([2, 3], 2, None) == (2, 3)


def test_range_on_a_line_with_skipped_index():
    # 別の枝のメッセージで index が飛んでいる線（0, 1, 4, 5）
    assert check_range([0, 1, 4, 5], 1, 4) == (1, 4)


def test_range_start_not_on_the_line_is_error():
    with pytest.raises(ValueError, match=r"index=0 はこの枝にありません（2〜3）"):
        check_range([2, 3], 0, None)


def test_range_end_not_on_the_line_is_error():
    with pytest.raises(ValueError, match=r"index=2 はこの枝にありません（0〜5）"):
        check_range([0, 1, 4, 5], 0, 2)


def test_range_start_after_end_is_error():
    with pytest.raises(ValueError, match="より後ろです"):
        check_range([0, 1, 2], 2, 0)
