"""render.py の純粋な関数のテスト（DB やファイルは使わない）"""

from tsuduri_mcp.models import Message
from tsuduri_mcp.render import Scope, branch_note, excerpt, format_cost, note_lines, skip_note
from tsuduri_mcp.store import MessageNote, StoredMessage

# --- excerpt ---


def test_excerpt_returns_as_is_when_body_is_exactly_max_chars():
    text = "あ" * 30

    assert excerpt(text, 30) == text


def test_excerpt_keyword_not_found_starts_from_the_beginning():
    text = "あ" * 50 + "い" * 50

    result = excerpt(text, 20, keywords=["どこにもない言葉"])

    assert result == text[:20] + "…" + "\n（全 100 文字のうち 1〜20 文字目）"


def test_excerpt_keyword_near_the_end_has_no_trailing_ellipsis():
    text = "あ" * 90 + "キーワード" + "い" * 5  # 全 100 文字。キーワードは 90〜94 文字目

    result = excerpt(text, 20, keywords=["キーワード"])

    assert result == "…" + text[80:100] + "\n（全 100 文字のうち 81〜100 文字目）"


# --- branch_note ---


def _pm(seq, parent_seq):
    when = "2026-01-01T00:00:00.000000Z"
    return StoredMessage(
        seq=seq,
        parent_seq=parent_seq,
        message=Message(uuid=f"u{seq}", sender="human", text="t", created_at=when, updated_at=when),
    )


def test_branch_note_marks_new_root_when_showing_all_branches():
    pm = _pm(2, None)
    scope = Scope(messages=[pm], all_branches=True, leaf_count=2)

    assert branch_note(pm, scope) == "新しい根（最初の発言の編集）"


def test_branch_note_is_silent_for_the_first_root():
    pm = _pm(0, None)
    scope = Scope(messages=[pm], all_branches=True, leaf_count=1)

    assert branch_note(pm, scope) == ""


def test_branch_note_is_silent_when_not_showing_all_branches():
    pm = _pm(2, None)
    scope = Scope(messages=[pm], all_branches=False, leaf_count=1)

    assert branch_note(pm, scope) == ""


# --- note_lines ---


def test_note_lines_describes_content_missing():
    pm = _pm(0, None)
    pm.notes = [MessageNote("content_missing", "2026-03-01T12:00:00.000000Z")]

    assert note_lines(pm) == ["（最新のエクスポートでは本文が消えている。2026-03-01 に確認）"]


def test_note_lines_describes_message_missing():
    pm = _pm(0, None)
    pm.notes = [MessageNote("message_missing", "2026-03-01T12:00:00.000000Z")]

    assert note_lines(pm) == ["（最新のエクスポートにはこのメッセージがない。2026-03-01 に確認）"]


def test_note_lines_is_empty_without_notes():
    pm = _pm(0, None)

    assert note_lines(pm) == []


# --- skip_note ---


def test_skip_note_is_none_when_nothing_was_skipped():
    assert skip_note(first_seq=0, start=0) is None


def test_skip_note_describes_the_skipped_range():
    result = skip_note(first_seq=0, start=3)

    assert result == "index 0〜2 は投稿済みなので index 3 から下書きにした（全部使うなら start=0）"


# --- format_cost ---


def test_format_cost_shows_cached_note_regardless_of_cost():
    assert format_cost(0.0015, cached=True) == "保存済み（今回の料金なし）"
    assert format_cost(None, cached=True) == "保存済み（今回の料金なし）"


def test_format_cost_shows_unknown_when_cost_is_none():
    assert format_cost(None, cached=False) == "料金は不明"


def test_format_cost_formats_known_cost_to_four_decimals():
    assert format_cost(0.015, cached=False) == "約 $0.0150"
