"""テスト用に ChatGPT エクスポート形式の dict を組み立てるヘルパー

形は 2026-10 に実物で確かめたものに合わせている（ノードに children がない、message に update_time がない）。
"""

T0 = 1767225600.0  # 2026-01-01T00:00:00Z


def gpt_message(mid, role="user", text="テストの発言", create_time=T0, content=None, hidden=False):
    metadata = {"is_visually_hidden_from_conversation": True} if hidden else {}
    return {
        "id": mid,
        "author": {"role": role},
        "create_time": create_time,
        "content": content if content is not None else {"content_type": "text", "parts": [text]},
        "metadata": metadata,
    }


def gpt_conversation(cid, nodes, title="テストの会話", update_time=T0):
    """nodes は (ノード id, 親の id, message または None) の並び。"""
    return {
        "id": cid,
        "conversation_id": cid,
        "title": title,
        "create_time": T0,
        "update_time": update_time,
        "current_node": nodes[-1][0] if nodes else None,
        "mapping": {nid: {"id": nid, "message": msg, "parent": parent} for nid, parent, msg in nodes},
    }
