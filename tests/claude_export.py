"""テスト用に claude.ai エクスポート形式の dict を組み立てるヘルパー"""

ROOT = "00000000-0000-4000-8000-000000000000"


def raw_message(uuid, sender="human", text="テストの発言", parent=ROOT, created_at="2026-01-01T00:00:00.000000Z", content=None):
    return {
        "uuid": uuid,
        "text": text,
        "content": content if content is not None else [{"type": "text", "text": text}],
        "sender": sender,
        "created_at": created_at,
        "updated_at": created_at,
        "attachments": [],
        "files": [],
        "parent_message_uuid": parent,
    }


def raw_conversation(uuid, messages, name="テストの会話", updated_at="2026-01-01T00:00:00.000000Z"):
    return {
        "uuid": uuid,
        "name": name,
        "summary": "",
        "created_at": "2026-01-01T00:00:00.000000Z",
        "updated_at": updated_at,
        "account": {"uuid": "テストのアカウント"},
        "chat_messages": messages,
    }
