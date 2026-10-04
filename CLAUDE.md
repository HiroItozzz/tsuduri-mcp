# tsuduri-mcp

claude.ai の会話履歴を SQLite に取り込み、検索・要約・ブログ投稿を MCP ツールとして出すサーバー。
設計と決めたことは `docs/design.md` にある。仕様を変えたら、そこも直す。


## 作業の任され方

- リポジトリ内の作業は任されている。決めたことと理由は `docs/design.md` に残して報告する
- コミットも任されている。テストが通った意味のまとまりごとにコミットする（`feat:` / `fix:` / `refactor:` / `test:` / `docs:` + 日本語）
- このリポジトリでは main に直接コミットしてよい（ユーザー共通の設定に「main にはコミットしない」があっても、ここではこちらを優先する）
- push はユーザーが頼んだときだけ。push 済みのコミットは amend しない


## コードの注意

- MCP サーバーは stdio で通信するので、サーバーから呼ばれるコードで `print` など stdout に出力しない（通信が壊れるため）。ログは `logging`
- HTTP クライアントは httpx2 を使う（mcp も Authlib も httpx2 を使うため。旧 httpx と混ぜると認証が壊れる）
- はてなの認証情報は `<>` がついたままの値で動く。値を加工したり、形式チェックで弾いたりしない
- テストで LLM は pydantic-ai の `FunctionModel` に、HTTP は `httpx2.MockTransport` に差し替える
- `data/` と `assets/`（ユーザーの会話データ）は、表示も変更もしない


## チェック

```bash
uv run ruff check . && uv run ruff format . && uv run ty check && uv run pytest -q
```


## レビューで見るところ

- SQL: プレースホルダを使っているか。FTS5 と LIKE のエスケープ、トランザクション、外部キー
- 枝分かれ: メッセージの親子関係（`parent_uuid`）と「1本の線」の扱い（`lines.py`）
- MCP: ツールの説明と戻り値が、AI にとって誤解のない書き方か
