"""テスト用に ChatGPT エクスポート形式の dict を組み立てるヘルパー（形式は実物で未確認）"""

T0 = 1767225600.0  # 2026-01-01T00:00:00Z


def gpt_message(mid, role="user", text="テストの発言", create_time=T0, content=None, hidden=False):
    metadata = {"is_visually_hidden_from_conversation": True} if hidden else {}
    return {
        "id": mid,
        "author": {"role": role, "name": None, "metadata": {}},
        "create_time": create_time,
        "update_time": None,
        "content": content if content is not None else {"content_type": "text", "parts": [text]},
        "status": "finished_successfully",
        "metadata": metadata,
        "recipient": "all",
    }


def gpt_conversation(cid, nodes, title="テストの会話", update_time=T0):
    """nodes は (ノード id, 親の id, message または None) の並び。children は親子から組み立てる。"""
    mapping = {nid: {"id": nid, "message": msg, "parent": parent, "children": []} for nid, parent, msg in nodes}
    for nid, parent, _ in nodes:
        if parent is not None:
            mapping[parent]["children"].append(nid)
    return {
        "id": cid,
        "conversation_id": cid,
        "title": title,
        "create_time": T0,
        "update_time": update_time,
        "current_node": nodes[-1][0] if nodes else None,
        "mapping": mapping,
    }
