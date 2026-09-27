"""会話の枝（1本の線）の計算。DB を使わず、メッセージのつながりと並びだけから決める。

store.py は SQL で読み書きしたものをここに渡す。枝分かれのテストはここだけで DB なしに書ける。
"""

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass


@dataclass
class Node:
    """線を選ぶのに必要な列だけを持つ、軽いメッセージの表現。"""

    uuid: str
    parent_uuid: str | None
    created_at: str
    seq: int


def select_line(nodes: Sequence[Node], through_index: int | None = None) -> list[str]:
    """through_index のメッセージを通る線を選び、seq 順（古い→新しい）の uuid を返す。

    そのメッセージより前は親をたどり、後はいちばん新しい続きをたどる。
    through_index を省くと、会話でいちばん新しいメッセージを通る線（本線）になる。
    nodes が空なら空のリストを返す。
    """
    if not nodes:
        return []
    by_uuid = {n.uuid: n for n in nodes}
    children: dict[str, list[Node]] = {}
    for n in nodes:
        if n.parent_uuid in by_uuid:
            children.setdefault(n.parent_uuid, []).append(n)

    def newest(ns: Iterable[Node]) -> Node:
        return max(ns, key=lambda n: (n.created_at, n.seq))

    if through_index is None:
        through = newest(nodes)
    else:
        found = [n for n in nodes if n.seq == through_index]
        if not found:
            raise ValueError(f"index={through_index} のメッセージはありません（0〜{len(nodes) - 1}）")
        through = found[0]

    # 後ろ: through の子孫のうち、いちばん新しいものを終点にする
    descendants, stack = [through], [through]
    while stack:
        kids = children.get(stack.pop().uuid, [])
        descendants += kids
        stack += kids
    leaf: Node | None = newest(descendants)

    # 前: 終点から親をたどる
    line = []
    while leaf is not None:
        line.append(leaf)
        parent = leaf.parent_uuid
        leaf = by_uuid.get(parent) if parent is not None else None
    return [n.uuid for n in line[::-1]]


def count_leaves(nodes: Sequence[Node]) -> int:
    """会話全体の枝の数（子のないメッセージの数）。1 なら枝分かれなし。"""
    uuids = {n.uuid for n in nodes}
    parents_with_children = {n.parent_uuid for n in nodes if n.parent_uuid in uuids}
    return sum(1 for n in nodes if n.uuid not in parents_with_children)


def first_unposted(line_uuids: Sequence[str], posted: Collection[str]) -> int | None:
    """1本の線の上で、まだ投稿していない部分の始まりが、線の何番目か（0 始まり。index ではない）。

    最後に投稿したメッセージのすぐ次にする。途中に投稿していない部分があっても、それより前には戻らない。
    投稿がなければ 0。全部投稿済みなら None。
    """
    posted_i = [i for i, uuid in enumerate(line_uuids) if uuid in posted]
    if not posted_i:
        return 0
    if posted_i[-1] == len(line_uuids) - 1:
        return None
    return posted_i[-1] + 1


def check_range(seqs: Sequence[int], start: int, end: int | None) -> tuple[int, int]:
    """線の上の範囲 start〜end（end を含む）を確かめ、決まった (start, end) を返す。

    seqs は線のメッセージの index を線の順に並べたもの（空でないこと）。
    end を省くと線の最後の index にする。start・end が線の上にないか、start が end より後ろならエラー。
    """
    first, last = seqs[0], seqs[-1]
    if end is None:
        end = last
    for index in (start, end):
        if index not in seqs:
            raise ValueError(f"index={index} はこの枝にありません（{first}〜{last}）")
    if start > end:
        raise ValueError(f"start（{start}）が end（{end}）より後ろです")
    return start, end
