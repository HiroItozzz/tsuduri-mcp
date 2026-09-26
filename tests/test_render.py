"""render.py の純粋な関数のテスト（DB やファイルは使わない）"""

from tsuduri_mcp.models import Message
from tsuduri_mcp.render import Scope, branch_note, excerpt, note_lines
from tsuduri_mcp.store import MessageNote, PositionedMessage

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


def _pm(position, parent_position):
    when = "2026-01-01T00:00:00.000000Z"
    return PositionedMessage(
        position=position,
        parent_position=parent_position,
        message=Message(uuid=f"u{position}", sender="human", text="t", created_at=when, updated_at=when),
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
