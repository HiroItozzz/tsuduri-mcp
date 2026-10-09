import json
import sqlite3

import pytest

from tsuduri_mcp import store as store_module
from tsuduri_mcp.sources import ClaudeExportSource
from tsuduri_mcp.store import MIGRATIONS, SCHEMA, ConversationStore, connect

from claude_export import raw_conversation, raw_message

OLD = "2026-01-01T00:00:00.000000Z"
NEW = "2026-02-01T00:00:00.000000Z"


@pytest.fixture
def store():
    conn = connect(":memory:")
    yield ConversationStore(conn)
    conn.close()


def import_raw(store, tmp_path, conversations):
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(conversations, ensure_ascii=False), encoding="utf-8")
    return store.import_conversations(ClaudeExportSource(path).load())


def two_turns():
    return raw_conversation(
        "c1",
        [
            raw_message("m1", text="テストの質問"),
            raw_message("m2", sender="assistant", text="テストの答え", parent="m1"),
        ],
        updated_at=OLD,
    )


def test_imported_conversation_can_be_read_back(store, tmp_path):
    result = import_raw(store, tmp_path, [two_turns()])

    assert (result.added, result.messages_added) == (1, 2)
    conv = store.get_conversation("c1")
    assert conv.name == "テストの会話"
    assert [(m.sender, m.text) for m in conv.messages] == [("human", "テストの質問"), ("assistant", "テストの答え")]


def test_importing_same_export_twice_changes_nothing(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])

    result = import_raw(store, tmp_path, [two_turns()])

    assert (result.added, result.updated, result.unchanged, result.messages_added) == (0, 0, 1, 0)
    assert len(store.get_conversation("c1").messages) == 2


def test_continued_conversation_is_updated(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])
    continued = two_turns()
    continued["name"] = "テストの会話（続き）"
    continued["updated_at"] = NEW
    continued["chat_messages"].append(raw_message("m3", text="テストの追加の質問", parent="m2"))

    result = import_raw(store, tmp_path, [continued])

    assert (result.updated, result.messages_added) == (1, 1)
    conv = store.get_conversation("c1")
    assert conv.name == "テストの会話（続き）"
    assert [m.uuid for m in conv.messages] == ["m1", "m2", "m3"]


def test_older_export_does_not_overwrite(store, tmp_path):
    newer = two_turns()
    newer["updated_at"] = NEW
    newer["name"] = "テストの新しい名前"
    import_raw(store, tmp_path, [newer])
    older = two_turns()

    result = import_raw(store, tmp_path, [older])

    assert result.unchanged == 1
    assert store.get_conversation("c1").name == "テストの新しい名前"


def test_older_export_still_adds_its_own_messages(store, tmp_path):
    # 新しいエクスポート → 古いエクスポートの順で取り込んでも、古いほうにしかないメッセージは入る
    newer = two_turns()
    newer["updated_at"] = NEW
    newer["name"] = "テストの新しい名前"
    import_raw(store, tmp_path, [newer])
    older = two_turns()
    older["chat_messages"].append(raw_message("m3", text="古いエクスポートだけのメッセージ", parent="m2"))

    result = import_raw(store, tmp_path, [older])

    assert result.unchanged == 1
    assert result.messages_added == 1
    conv = store.get_conversation("c1")
    assert conv.name == "テストの新しい名前"  # 会話の名前は新しいほうのまま
    assert [m.uuid for m in conv.messages] == ["m1", "m2", "m3"]


def seqs(store, conversation_uuid="c1"):
    return [(pm.seq, pm.message.uuid) for pm in store.get_messages(conversation_uuid)]


def test_new_messages_get_seqs_after_existing_ones_when_a_message_disappears(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])
    changed = two_turns()
    # m1 が消え、m3 が増えたエクスポート。配列の番号のままだと m3 が m2 と同じ 1 になる
    changed["chat_messages"] = [
        changed["chat_messages"][1],
        raw_message("m3", text="テストの追加の質問", parent="m2"),
    ]

    result = import_raw(store, tmp_path, [changed])

    assert result.messages_added == 1
    assert seqs(store) == [(0, "m1"), (1, "m2"), (2, "m3")]


def test_older_export_messages_get_seqs_after_existing_ones(store, tmp_path):
    newer = two_turns()
    newer["chat_messages"] = newer["chat_messages"][:1]  # 新しいほうでは m2 が消えている
    newer["chat_messages"].append(raw_message("m3", text="新しいエクスポートだけのメッセージ", parent="m1"))
    import_raw(store, tmp_path, [newer])

    import_raw(store, tmp_path, [two_turns()])

    assert seqs(store) == [(0, "m1"), (1, "m3"), (2, "m2")]  # m2 は時間では古いが、番号は後ろ


def test_same_uuid_twice_in_one_export_is_imported_once(store, tmp_path):
    conv = two_turns()
    conv["chat_messages"].append(raw_message("m2", sender="assistant", text="重なった uuid", parent="m1"))

    result = import_raw(store, tmp_path, [conv])

    assert result.messages_added == 2
    assert seqs(store) == [(0, "m1"), (1, "m2")]


def test_unknown_conversation_is_none(store):
    assert store.get_conversation("存在しないテストの会話") is None


# --- 消えた本文・メッセージの印 ---


def test_content_missing_note_is_added_when_content_disappears(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])
    dropped = two_turns()
    dropped["chat_messages"][0]["content"] = []

    result = import_raw(store, tmp_path, [dropped])

    assert result.notes_added == 1
    m1 = store.get_messages("c1")[0]
    assert m1.message.text == "テストの質問"  # 本文は残っている
    assert [n.kind for n in m1.notes] == ["content_missing"]


def test_message_missing_note_is_added_when_message_disappears(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])
    dropped = two_turns()
    dropped["chat_messages"] = dropped["chat_messages"][:1]  # m2 が消えたエクスポート

    result = import_raw(store, tmp_path, [dropped])

    assert result.notes_added == 1
    messages = store.get_messages("c1")
    m2 = next(pm for pm in messages if pm.message.uuid == "m2")
    assert [n.kind for n in m2.notes] == ["message_missing"]


def test_notes_are_not_duplicated_and_noticed_at_does_not_change(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])
    dropped = two_turns()
    dropped["chat_messages"][0]["content"] = []
    import_raw(store, tmp_path, [dropped])
    first_noticed_at = store.get_messages("c1")[0].notes[0].noticed_at

    result = import_raw(store, tmp_path, [dropped])

    assert result.notes_added == 0
    assert store.get_messages("c1")[0].notes[0].noticed_at == first_noticed_at


def test_content_missing_note_is_removed_when_content_comes_back(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])
    dropped = two_turns()
    dropped["chat_messages"][0]["content"] = []
    import_raw(store, tmp_path, [dropped])

    import_raw(store, tmp_path, [two_turns()])  # 本文つきで戻ってきた

    assert store.get_messages("c1")[0].notes == []


def test_message_missing_note_is_removed_when_message_comes_back(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])
    dropped = two_turns()
    dropped["chat_messages"] = dropped["chat_messages"][:1]
    import_raw(store, tmp_path, [dropped])

    import_raw(store, tmp_path, [two_turns()])  # 存在して戻ってきた

    messages = store.get_messages("c1")
    m2 = next(pm for pm in messages if pm.message.uuid == "m2")
    assert m2.notes == []


# --- ブログ投稿の記録 ---


def test_unposted_start_without_any_post_is_zero(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])

    assert store.unposted_start(store.get_line("c1").messages) == 0


def test_unposted_start_after_partial_post(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])
    store.record_post("c1", "hatena", "https://example.com/1", "タイトル", ["m1"])

    assert store.unposted_start(store.get_line("c1").messages) == 1


def test_unposted_start_when_fully_posted_is_none(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])
    store.record_post("c1", "hatena", "https://example.com/1", "タイトル", ["m1", "m2"])

    assert store.unposted_start(store.get_line("c1").messages) is None


def test_record_post_saves_service_url_title(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])

    post_id = store.record_post("c1", "hatena", "https://example.com/1", "タイトル", ["m1", "m2"])

    row = store.conn.execute("SELECT service, url, title FROM posts WHERE id = ?", (post_id,)).fetchone()
    assert (row["service"], row["url"], row["title"]) == ("hatena", "https://example.com/1", "タイトル")
    rows = store.conn.execute("SELECT message_uuid FROM post_messages WHERE post_id = ?", (post_id,))
    assert {r["message_uuid"] for r in rows} == {"m1", "m2"}


def test_find_posted_overlap_is_empty_when_nothing_recorded(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])

    assert store.find_posted_overlap(["m1", "m2"]) == []


def test_find_posted_overlap_reports_count_per_post(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])
    store.record_post("c1", "hatena", "https://example.com/1", "はてなの記事", ["m1", "m2"])

    overlaps = store.find_posted_overlap(["m1", "m2"])

    assert len(overlaps) == 1
    assert (overlaps[0].service, overlaps[0].title, overlaps[0].count) == ("hatena", "はてなの記事", 2)


# --- LLM の呼び出し記録 ---


def test_record_llm_call_and_list_llm_calls_round_trip(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])

    store.record_llm_call("summary", "c1", "gemini-3-flash-preview", 100, 20, 0.0015, "2026-01-01T00:00:00Z")

    calls = store.list_llm_calls("c1")
    assert len(calls) == 1
    call = calls[0]
    assert (call.kind, call.model, call.input_tokens, call.output_tokens) == (
        "summary",
        "gemini-3-flash-preview",
        100,
        20,
    )
    assert call.cost_usd == 0.0015


def test_record_llm_call_allows_unknown_cost(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])

    store.record_llm_call("summary", "c1", "unknown-model", 10, 5, None, "2026-01-01T00:00:00Z")

    assert store.list_llm_calls("c1")[0].cost_usd is None


def test_list_llm_calls_filters_by_conversation(store, tmp_path):
    import_raw(store, tmp_path, [two_turns(), raw_conversation("c2", [raw_message("m3", text="別会話")])])

    store.record_llm_call("summary", "c1", "m", 1, 1, None, "2026-01-01T00:00:00Z")
    store.record_llm_call("summary", "c2", "m", 1, 1, None, "2026-01-01T00:00:00Z")

    assert [c.conversation_uuid for c in store.list_llm_calls("c1")] == ["c1"]


# --- DB 接続 ---


def test_connect_sets_wal_and_busy_timeout_for_file_db(tmp_path):
    conn = connect(tmp_path / "tsuduri.db")
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert journal_mode == "wal"
        assert busy_timeout == 30000
    finally:
        conn.close()


def test_connect_does_not_set_wal_for_in_memory_db():
    conn = connect(":memory:")
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert journal_mode != "wal"  # :memory: は WAL にならない
    finally:
        conn.close()


# --- DB の版（PRAGMA user_version） ---


def test_new_db_is_migrated_to_the_latest_version(tmp_path):
    conn = connect(tmp_path / "new.db")
    try:
        ConversationStore(conn)

        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    finally:
        conn.close()


def test_migration_adds_member_uri_column_and_keeps_existing_rows(tmp_path):
    path = tmp_path / "old.db"
    conn = connect(path)
    conn.executescript(SCHEMA)  # 版0の形（posts に member_uri 列がない）
    conn.execute(
        "INSERT INTO conversations (uuid, name, summary, created_at, updated_at) VALUES ('c1', '名前', '', 't', 't')"
    )
    conn.execute(
        """INSERT INTO posts (conversation_uuid, service, url, title, posted_at)
           VALUES ('c1', 'hatena', 'https://example.com/1', 'タイトル', 't')"""
    )
    conn.commit()
    conn.close()

    conn = connect(path)
    try:
        store = ConversationStore(conn)

        row = store.conn.execute("SELECT url, member_uri FROM posts").fetchone()
        assert row["url"] == "https://example.com/1"  # 既存の行は残っている
        assert row["member_uri"] is None  # 新しい列は NULL
        assert store.conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    finally:
        conn.close()


def test_migration_marks_existing_conversations_as_claude(tmp_path):
    path = tmp_path / "old.db"
    conn = connect(path)
    conn.executescript(SCHEMA)  # 版0の形（conversations に source 列がない）
    conn.execute(
        "INSERT INTO conversations (uuid, name, summary, created_at, updated_at) VALUES ('c1', '名前', '', 't', 't')"
    )
    conn.commit()
    conn.close()

    conn = connect(path)
    try:
        store = ConversationStore(conn)

        assert store.conn.execute("SELECT source FROM conversations").fetchone()[0] == "claude"
    finally:
        conn.close()


def test_duplicate_seq_in_a_conversation_is_rejected(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])

    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            """INSERT INTO messages (uuid, conversation_uuid, seq, parent_uuid, sender, text, created_at,
                                     updated_at, raw_content, attachments, files)
               VALUES ('m9', 'c1', 1, NULL, 'human', '', 't', 't', '[]', '[]', '[]')"""
        )


def test_reopening_a_migrated_db_does_not_fail(tmp_path):
    path = tmp_path / "old.db"
    conn = connect(path)
    ConversationStore(conn)  # 1回目で最新の版になる
    conn.close()

    conn = connect(path)
    try:
        ConversationStore(conn)  # 2回目に開いても、移行をやり直さず壊れない
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    finally:
        conn.close()


def test_migration_failure_rolls_back_everything(tmp_path, monkeypatch):
    """途中の SQL が失敗したら、その回に流した分は全部巻き戻る。"""
    broken = [*MIGRATIONS, (len(MIGRATIONS) + 1, ["CREATE TABLE dummy_migration_check (id INTEGER)", "NOT VALID SQL"])]
    monkeypatch.setattr(store_module, "MIGRATIONS", broken)
    conn = connect(tmp_path / "broken.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            ConversationStore(conn)

        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0  # それまでの版もまとめて巻き戻る
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "dummy_migration_check" not in tables  # 正しい方の SQL の結果も残っていない
    finally:
        conn.close()


def test_backup_before_migration_includes_wal_contents(tmp_path, monkeypatch):
    """コピーには WAL に残ったままの分（まだ本体に書き戻していない分）も含まれる。"""
    path = tmp_path / "wal.db"
    conn = connect(path)
    ConversationStore(conn)  # 先に最新の版にしておく
    conn.close()

    # checkpoint を止めて、別の接続で行を入れる。WAL に残ったままになる
    writer_conn = connect(path)
    writer_conn.execute("PRAGMA wal_autocheckpoint = 0")
    writer_conn.execute(
        "INSERT INTO conversations (uuid, name, summary, created_at, updated_at) VALUES ('c1', '名前', '', 't', 't')"
    )
    writer_conn.commit()

    latest_version = len(MIGRATIONS)
    monkeypatch.setattr(
        store_module, "MIGRATIONS", [*MIGRATIONS, (latest_version + 1, ["CREATE TABLE extra_check (id INTEGER)"])]
    )

    conn = connect(path)
    try:
        ConversationStore(conn)  # writer_conn を開いたまま移行する

        backups = list(tmp_path.glob(f"wal.db.v{latest_version}-*.bak"))
        assert len(backups) == 1
        backup_conn = connect(backups[0])
        try:
            row = backup_conn.execute("SELECT uuid FROM conversations").fetchone()
            assert row["uuid"] == "c1"  # WAL のままだった行もコピーに入っている
        finally:
            backup_conn.close()
    finally:
        conn.close()
        writer_conn.close()


def test_backup_skipped_if_already_exists_for_this_version(tmp_path, monkeypatch):
    path = tmp_path / "existing.db"
    conn = connect(path)
    ConversationStore(conn)
    conn.close()

    latest_version = len(MIGRATIONS)
    dummy_backup = path.with_name(f"{path.name}.v{latest_version}-20260101T000000Z.bak")
    dummy_backup.write_bytes(b"")
    monkeypatch.setattr(
        store_module, "MIGRATIONS", [*MIGRATIONS, (latest_version + 1, ["CREATE TABLE extra_check (id INTEGER)"])]
    )

    conn = connect(path)
    try:
        ConversationStore(conn)

        backups = list(tmp_path.glob(f"existing.db.v{latest_version}-*.bak"))
        assert backups == [dummy_backup]  # 同じ版のコピーがすでにあるので増えない
    finally:
        conn.close()


class _BrokenBackupConnection(sqlite3.Connection):
    """sqlite3.Connection は不変の型で backup を直接差し替えられないので、サブクラスで上書きする。"""

    def backup(self, *args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("disk full（テスト用）")


def test_backup_before_migration_cleans_up_partial_file_on_failure(tmp_path, monkeypatch, caplog):
    path = tmp_path / "fail.db"
    setup_conn = connect(path)
    ConversationStore(setup_conn)
    setup_conn.close()

    latest_version = len(MIGRATIONS)
    monkeypatch.setattr(
        store_module, "MIGRATIONS", [*MIGRATIONS, (latest_version + 1, ["CREATE TABLE extra_check (id INTEGER)"])]
    )

    conn = sqlite3.connect(path, factory=_BrokenBackupConnection)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ConversationStore(conn)
    finally:
        conn.close()

    assert list(tmp_path.glob(f"fail.db.v{latest_version}-*.bak")) == []  # コピー先の途中までのファイルは消える
    assert "移行の前のコピーに失敗しました" in caplog.text


def _prepare_pending_migration(path, monkeypatch) -> int:
    """最新の DB を path に作り、無害な版を1つ足して、移行が残っている状態にする。今の版を返す。"""
    conn = connect(path)
    ConversationStore(conn)
    conn.close()
    latest_version = len(MIGRATIONS)
    monkeypatch.setattr(
        store_module, "MIGRATIONS", [*MIGRATIONS, (latest_version + 1, ["CREATE TABLE extra_check (id INTEGER)"])]
    )
    return latest_version


def test_backup_failure_keeps_the_original_error_when_cleanup_also_fails(tmp_path, monkeypatch):
    path = tmp_path / "fail.db"
    _prepare_pending_migration(path, monkeypatch)

    def broken_unlink(self, missing_ok=False):
        raise PermissionError("消せない（テスト用）")

    monkeypatch.setattr(store_module.Path, "unlink", broken_unlink)
    conn = sqlite3.connect(path, factory=_BrokenBackupConnection)
    try:
        with pytest.raises(sqlite3.OperationalError, match="disk full"):
            ConversationStore(conn)
    finally:
        conn.close()


def test_backup_check_treats_db_file_name_literally(tmp_path, monkeypatch):
    """DB のファイル名の [ ] を glob の記号として扱わない（a[1].db のコピーを a1.db のコピーと取り違えない）。"""
    path = tmp_path / "a[1].db"
    latest_version = _prepare_pending_migration(path, monkeypatch)
    decoy = tmp_path / f"a1.db.v{latest_version}-20260101T000000Z.bak"
    decoy.write_bytes(b"")

    conn = connect(path)
    try:
        ConversationStore(conn)
    finally:
        conn.close()

    own = [p for p in tmp_path.iterdir() if p.name.startswith(f"a[1].db.v{latest_version}-")]
    assert len(own) == 1


def test_backup_is_left_to_the_process_that_created_the_file_first(tmp_path, monkeypatch):
    """同じ秒に別のプロセスが同じ名前のコピーを作り始めていたら、上書きせずに任せる。"""
    path = tmp_path / "race.db"
    latest_version = _prepare_pending_migration(path, monkeypatch)

    real_datetime = store_module.datetime

    class FixedDatetime:
        @staticmethod
        def now(tz=None):
            return real_datetime(2026, 1, 1, tzinfo=tz)

    monkeypatch.setattr(store_module, "datetime", FixedDatetime)
    other = tmp_path / f"race.db.v{latest_version}-20260101T000000Z.bak"
    original_iterdir = store_module.Path.iterdir

    def iterdir_then_race(self):
        entries = list(original_iterdir(self))  # 確かめた時点では、まだ同じ版のコピーはない
        other.write_bytes(b"other process")  # その直後に別のプロセスが同じ名前で作った
        return iter(entries)

    monkeypatch.setattr(store_module.Path, "iterdir", iterdir_then_race)

    conn = connect(path)
    try:
        ConversationStore(conn)

        assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_version + 1
    finally:
        conn.close()
    assert other.read_bytes() == b"other process"  # 別のプロセスのファイルを上書きしていない


def test_migration_rereads_version_inside_transaction(tmp_path, monkeypatch):
    """コピーを取っているあいだに別の接続が移行を終えていたら、トランザクションの中で読み直して何もしない。"""
    path = tmp_path / "race.db"
    conn = connect(path)
    ConversationStore(conn)  # 先に最新の版にしておく
    conn.close()

    latest_version = len(MIGRATIONS)
    # IF NOT EXISTS がない SQL なので、同じ版を2回流すとエラーになる（冪等でない）
    monkeypatch.setattr(
        store_module, "MIGRATIONS", [*MIGRATIONS, (latest_version + 1, ["CREATE TABLE extra_check (id INTEGER)"])]
    )

    original_backup = store_module._backup_before_migration
    already_raced = False

    def racing_backup(conn: sqlite3.Connection, current_version: int) -> None:
        nonlocal already_raced
        if already_raced:  # 別の接続の中で呼ばれたときは、いつも通りに振る舞う
            original_backup(conn, current_version)
            return
        already_raced = True
        other_conn = connect(path)
        try:
            store_module._migrate(other_conn)  # 別の接続で先に移行を最後まで済ませてしまう
        finally:
            other_conn.close()

    monkeypatch.setattr(store_module, "_backup_before_migration", racing_backup)

    conn = connect(path)
    try:
        ConversationStore(conn)  # ここでエラーにならない

        assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_version + 1
    finally:
        conn.close()


def test_new_db_has_both_message_indexes(tmp_path):
    """idx_messages_conversation は idx_messages_position と中身が同じだが、古い SCHEMA が困るので消さない。"""
    conn = connect(tmp_path / "new.db")
    try:
        ConversationStore(conn)

        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert "idx_messages_conversation" in indexes
        assert "idx_messages_position" in indexes
    finally:
        conn.close()


def test_no_backup_when_already_latest_or_new_or_in_memory(tmp_path):
    path = tmp_path / "latest.db"
    conn = connect(path)
    ConversationStore(conn)
    conn.close()
    conn = connect(path)
    try:
        ConversationStore(conn)  # 最新の DB を開き直してもコピーはできない
    finally:
        conn.close()
    assert list(tmp_path.glob("*.bak")) == []

    new_conn = connect(tmp_path / "new.db")
    try:
        ConversationStore(new_conn)  # 新しい DB（版0）でもコピーはできない
    finally:
        new_conn.close()
    assert list(tmp_path.glob("*.bak")) == []

    memory_conn = connect(":memory:")
    try:
        ConversationStore(memory_conn)  # :memory: でもエラーにならない
    finally:
        memory_conn.close()
