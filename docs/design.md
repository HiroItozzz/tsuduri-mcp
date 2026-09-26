# 設計メモ

決めたことと、その理由を残す。あとで見直すときに「なぜこうしたか」がわかるようにする。

## 会話データの取り込み（SQLite）

### 元データ

claude.ai の公式エクスポート（設定 → データのエクスポート）。manifest の JSON に zip の URL が4つ並んでおり、各 URL は1回しか使えない。

| zip | 中身 | 扱い |
|---|---|---|
| conversations | `conversations.json`（全会話） | 取り込む |
| projects | プロジェクトごとの JSON（名前・指示・ナレッジ） | 後回し（下記） |
| memories | Claude のメモリ | 取り込まない |
| light_metadata | ログイン履歴（IP・位置）とメールアドレス | 取り込まない（個人情報で、使い道がない） |

2026-09-26 のエクスポート: 1,157 会話・41,152 メッセージ、最古は 2025-05-17。展開したものは `assets/export-2026-09-26/` に置いている（`assets/` は git 管理外）。

### 取り込みの規則

- **会話**: uuid が主キー。`updated_at` が保存済みより新しいときだけ、名前・要約・`updated_at` を上書きする。古いエクスポートを後から取り込んでも巻き戻らない
- **メッセージ**: uuid が主キー。`INSERT OR IGNORE` で、すでにあるものは上書きしない。会話が更新されていなくても毎回実行するので、古いエクスポートをあとから取り込んでも、古いほうにしかないメッセージが入る（取り込みの順番に依存しない）
- **消えた本文の印**: 新しいエクスポートで本文が空になっていたメッセージ（`content_missing`）や、メッセージ自体がなくなっていたもの（`message_missing`）は、`message_notes` に最初に気づいた日時と一緒に記録し、表示と要約の入力で注記する。本文つきで戻ってきたら印を消す。2026-09-26 のエクスポートでは `content_missing` が 4 件
  - 旧エクスポート（2026-09-20 まで）と新エクスポートを比べると、メッセージの `updated_at` は1件も変わっていないのに、122 件で content が変わっていた。ほとんどは tool_result の中身の違いだったが、4 件は新しいほうで content が空になっていた
  - 上書きしない方針なら、エクスポートから消えた本文も DB に残る。実際に旧→新の順で取り込んだ DB で、この 4 件の本文が残っていることを確認した
- 日時はすべて `2026-01-01T00:00:00.000000Z` の形にそろっているので、文字列のまま比べている
- 1回の取り込みは1つのトランザクション。途中で失敗したら何も書き込まれない
- DB は WAL モードで、ロック待ちは 30 秒。取り込み（約 42 秒の1トランザクション）の最中でも、ツールが読めて、投稿の記録も待てるようにするため
- 取り込みはユーザーが手で行う（エクスポートの zip の URL は1回しか使えず、自動化が難しいため。2026-09-27 に決定）。上の規則で重複しないので、いつ・どの順で取り込んでもよい

### 本文（`messages.text`）

- content の **text ブロックだけ**を `\n\n` でつないで作る。検索と要約はこの列を使う
- message の `text` 欄は使わない。画面表示用の定型文（`This block is not supported on your current device yet.` など）が混ざっているため
- thinking / tool_use / tool_result / injected_prompt_block / token_budget は本文に入れない。ただし `raw_content` 列に元のブロックを JSON のまま全部残しているので、あとで方針を変えても再取り込みなしで作り直せる
- text ブロックがないメッセージ（添付だけ、ツール呼び出しだけなど）は本文が空になる。新エクスポートでは 4,448 件

### テーブル

- `conversations(uuid, name, summary, created_at, updated_at)`
- `messages(id, uuid, conversation_uuid, position, parent_uuid, sender, text, created_at, updated_at, raw_content, attachments, files)`
  - `id` は整数の主キーで、全文検索の索引（`messages_fts`）が参照する。uuid を主キーにした表の暗黙の rowid は VACUUM で変わることがあるため、明示した
  - `position` はエクスポート内での並び順（0 始まり）。ツールでは「index」と呼ぶ。配列の順は時刻順と一致していた
  - `parent_uuid` は親メッセージ。先頭メッセージの親として入っている `00000000-0000-4000-8000-000000000000` は NULL にしている。407 会話で枝分かれ（編集や再生成）があり、エクスポートには全部の枝が入っている。いま表示されている枝がどれかは、エクスポートからはわからない
  - `attachments`（`extracted_content` に本文あり）と `files`（名前と uuid だけ）は JSON のまま保存している
- 会話とプロジェクトの対応はエクスポートに含まれていない。`projects` テーブルは、会話とは独立したものとして後で足す

### DB の場所

- 既定は、ユーザーのデータ置き場の `tsuduri.db`（Linux は `$XDG_DATA_HOME` か `~/.local/share` の下の `tsuduri-mcp/`、Windows は `%LOCALAPPDATA%\tsuduri-mcp\`）。環境変数 `TSUDURI_DB` で変えられる
- 以前はリポジトリの `data/tsuduri.db`（cwd 基準の相対パス）だった。`uv run --directory` 以外（uvx、Claude デスクトップが別の作業フォルダで起動する場合など）でも DB が見つかるように、作業フォルダに左右されない場所に変えた（2026-09-27）。移すときは SQLite のバックアップ機能でコピーし、表ごとの件数・整合性チェック・全文検索を比べて確かめた
- 手元のリポジトリの `data` と `logs` は、新しい置き場所へのシンボリックリンクにしている（git 管理外）
- 起動は、開発中なので `uv run --directory <リポジトリ> --env-file <.env> tsuduri-mcp`（コードを直せばすぐ反映される）。uvx はインストールした時点のコピーを動かすので、配布するときに考える
- `*.db` などの SQLite ファイルは git 管理外

### 構成

- `models.py`: `Conversation` / `Message`（dataclass）。形式に依存しない
- `sources.py`: `ConversationSource`（読み込みのインターフェース）と、その実装の `ClaudeExportSource`。claude.ai 形式の知識（キー名、ブロックの種類、ルートの親 uuid）はここに閉じ込める。`conversations.json` と、それを含む zip のどちらも読める
- `store.py`: `ConversationStore`。SQLite への書き込みと読み出し
- `dates.py`: ツールの引数（ローカル時刻）と DB（UTC）の日時の変換
- `render.py`: ツールの戻り値のテキストを組み立てる
- `server.py`: MCP ツールの定義
- `importer.py`: `tsuduri-import` コマンド
- `llm.py`: pydantic-ai 経由で Gemini を呼ぶ。`prompts/` の指示文（要約・ブログ）を読む
- `blog.py`: ブログへの投稿（`BlogPoster` と `HatenaPoster`）
- `log.py`: ファイルへのログ
- `paths.py`: DB とログの既定の置き場所

SQLAlchemy は使わない。中心の FTS5（仮想テーブル、`MATCH`、トリガー）は SQLAlchemy でも生の SQL になり、表が2つで書き込みも取り込みの1か所だけなので、ORM の利点が小さいため。動的な WHERE は小さな `_Where` で組み立てている。

## 検索

- FTS5 の `trigram` トークナイザで、本文の部分一致を索引で引く。日本語でも分かち書きなしで使え、大文字・小文字は区別しない
- trigram は3文字以上でしか引けないので、2文字以下のキーワードは `LIKE` で探す（本物の DB で 0.6 秒ほど）
- キーワードはフレーズとして渡すので、`AND` や `*` などの FTS の演算子は文字として扱われる。LIKE の `%` `_` もエスケープする
- 取り込みは索引づくりのぶん遅くなった（新エクスポート全件で約 7 秒 → 約 42 秒）。たまにしか実行しないので許容している
- メッセージは追加だけなので、索引の更新は INSERT のトリガーだけにしている

## MCP ツール

AI のコンテキストを節約することを優先している。

| ツール | 用途 |
|---|---|
| `search_messages` | キーワード検索。all/any、除外語、発言者、期間、会話で絞り込み、ページ分け |
| `list_conversations` | 期間・タイトルで会話の一覧（例: 先週の会話） |
| `get_messages` | 会話の index の範囲だけを読む |
| `export_conversation` | 全文を Markdown ファイルに書き出して、パスだけを返す |
| `summarize_conversation` | Gemini で1本の枝を要約する。要約は保存して使い回す |
| `draft_blog_post` | Gemini で1本の枝からブログの下書き（タイトル・本文・カテゴリー）を作る。投稿はしない |
| `post_blog_article` | はてなブログへ投稿し（既定は下書き）、投稿した範囲を記録する |
| `record_blog_post` | 投稿した範囲（1本の枝の index a〜b）と URL を記録する。`post_blog_article` を使わずに投稿したとき用 |

MCP の prompt（クライアント自身に読ませて書かせるための指示書）も2つある。

| prompt | 用途 |
|---|---|
| `summarize_with_claude` | クライアント自身が会話を読んで要約する。形式は `prompts/summary.md` |
| `draft_blog_with_claude` | クライアント自身がブログの下書きを書く。指示は `prompts/blog.md`（cha2hatena の config.yaml から移した）。投稿はしない |

- **戻り値はテキスト**（`structured_output=False`）。構造化出力にすると、同じ内容がインデントつき JSON の文字列と構造化データの両方で送られるため。キー名のくり返しや改行のエスケープもなくなる
- 検索結果は1件ごとに `conversation=<uuid> index=<n>`、発言者、日時、タイトル、本文。長い本文は、最初に当たったキーワードのまわりを `max_chars` 文字だけ切り出し、何文字目かを添える。続きは `get_messages` で読む
- 件数の多い結果は「N 件中 a〜b 件目（続きは offset=…）」と書き、AI が続きを取りに行けるようにする
- 一覧の期間は「期間中に作られたメッセージがある会話」。会話の `updated_at` はタイトルの変更などメッセージのない操作でも新しくなるので、期間の判定にも並び順にも使わない。並び順と表示は「最後の発言の日時」
- タイトルが空の会話（新エクスポートで 219 件）は、最初の発言の冒頭を添える。エクスポートに本文がまったく入っていない会話（195 件。2026-05 に多い）は「本文なし」と表示し、AI が空振りで読みに行かないようにする
- キーワードは2文字以上。1文字はほとんどのメッセージに当たるのでエラーにする。キーワードなしで呼ぶと、条件に合うすべてのメッセージを返す（期間で眺める用）
- 検索結果のうち本線に入っていないメッセージには「本線外」と、その枝を読むための `through_index` を添える。`main_line_only=true` で本線だけに絞れる（再生成した似た答えが並ぶのを避ける）
- 日付の引数はローカル時刻の `YYYY-MM-DD` か ISO 8601。`until` に日付だけを渡すと、その日を含む。表示もローカル時刻。DB の時刻はエクスポートのまま UTC（`…Z`）で、ローカル時刻との変換は日時ごとの実際の時差（夏時間を含む）で行う
- 枝分かれした会話は、既定で1本の枝だけを扱う（下記）。`all_branches=true` なら全部の枝を index 順に並べ、直前以外のメッセージへの返信に「分岐」と表示する
- `export_conversation` は、AI がファイルを読めるクライアント（Claude Code など）向け。書き出し先は一時ディレクトリの `tsuduri-mcp/<uuid>-to<最後の index>.md`（すべての枝なら `<uuid>-all.md`）。枝ごとにファイルを分け、別の枝の書き出しで上書きしない。uuid は DB に存在することを確かめてからファイル名に使う
- DB がないときは、取り込みのコマンドを案内するエラーにする
- 想定内のエラー（ValueError・FileNotFoundError・RuntimeError）は、ツールの入口（`mcp_tool`）で `ToolError` に変える。mcp 2.x の MCPServer は `ToolError` 以外の例外を「Error executing tool <name>」だけにして文言を消すため。実地確認で、次の一手のヒント（「start=0 で…」など）が AI に届いていないことがわかった
- サーバーの `instructions` に、ツールの使い分けを書いている

## 枝分かれ

枝は消さずに全部保存し、見せ方で扱う（消すと取り戻せないため。DB は約 180MB で容量は問題になっていない）。

407 会話で、編集や再生成による枝分かれがある。枝をすべて並べると同じような内容が重なる（本線に入るのは平均 86%）ので、ツールは既定で1本の枝（線）だけを扱う。

- `through_index=k` で「index k を通る線」を選ぶ。k より前は親をたどり、k より後は子孫のうちいちばん新しいメッセージまでたどる。検索で当たった index をそのまま渡せば、その枝が読める
- 省くと、会話でいちばん新しいメッセージを通る線（本線）。本線に入るメッセージは取り込みのたびに計算して `main_line_messages` に保存する（検索の「本線外」の印と絞り込みに使う）
- 最初の発言を編集した会話では根が複数ある。線は終点から親をたどるので、そのまま扱える
- ブログに投稿した範囲の記録も、線の上のメッセージ単位で持つ（下記）。こうすると、枝分かれと「投稿後に続いた会話」を同じ仕組みで扱える

## ブログ投稿の記録

- `posts(id, conversation_uuid, service, url, title, posted_at)` と `post_messages(post_id, message_uuid)`。範囲の両端ではなく、範囲に入るメッセージを全部持つ。枝の違う線でも、共通の親の部分だけが投稿済みになる
- 記録は `record_blog_post` で入れる。投稿の仕組み（P3）より先に、手で投稿したものも記録できるようにした。P3 のポスターも同じ `record_post` を呼ぶ。このアプリより前の投稿（cha2hatena）は会話の uuid が残っていないので取り込まない
- `posted_at` は記録した日時。service は `hatena` / `qiita` / `devto` / `other`
- 未投稿の始まり: 線の上で最後に投稿済みのメッセージの、線の上での次のメッセージの index。投稿がなければ線の最初の index（最初の発言を編集した会話では 0 とは限らない）
- `draft_blog_post` の `start` を省くと、未投稿の始まりから下書きにし、飛ばした範囲を戻り値の先頭に書く。全部投稿済みならエラー（`start=0` で全部を材料にできる）。`record_blog_post` の start・end の既定も同じ（未投稿の始まり〜線の最後）なので、既定どうしで呼べば同じ範囲になる
- 表示: 一覧に「投稿 N 件・本線の未投稿 M 件」、検索結果に「投稿済み」。一覧の SQL は実データで変わらず約 0.03 秒
- `draft_blog_post` の戻り値の先頭で、この会話の投稿の記録（タイトル・URL・範囲・この枝か別の枝か）と、枝分かれしていること（枝の数、いまの枝、through_index で選べること）を知らせる。前の記事と話題が重なるのを避けるのは、Gemini への指示ではなく、読む側（AI とユーザー）に気づかせる形にした
- 戻り値の最後に、同じ範囲で `record_blog_post` を呼ぶための具体的な引数を書く。件数は「この枝の N 件」と書く（index の範囲には別の枝の index も含まれるため）
- 材料が薄いと、Gemini は会話のタイトルに引っぱられて材料にない一般論を書いた。`prompts/blog.md` に「材料にない話題や一般論を足さない」を足し、本文の合計が 1,000 文字未満なら戻り値で注意する

## ブログへの投稿

- `blog.py` に投稿先のインターフェース `BlogPoster`（`post(article, draft=…)`）を置き、サービスごとに実装する（`sources.py` と同じ考え方）。いまは `HatenaPoster` だけ。Qiita / Dev.to は実装クラスを足し、`server.default_poster` に1行足せば入る
- `post_blog_article` は、title・content を省くと、その範囲（最初と最後のメッセージ）の保存済みの Gemini の下書き（いちばん新しいもの）をそのまま投稿する。渡した項目だけ差し替わるので、手で直した下書きや Claude が書いた下書きも投稿できる
  - 最初は title・content を必ず引数で受け取っていた。実地確認で、AI が `draft_blog_post` の戻り値（タイトル行・カテゴリー行・案内の行と本文がひと続きの文章）から本文を自分で切り出していた。Gemini の出力は pydantic で分かれているのに、テキストにまとめて AI に切り直させると、案内の行などが本文に混ざっても気づけない。DB 経由で受け渡し、AI には範囲だけを持たせる
  - 下書きがなければ、投稿せずにエラーにする。下書きのあとで会話が続いて範囲の終わりが変わったときも同じ（`draft_blog_post` の戻り値に書いた start・end を渡せば当たる）
- 既定は下書き投稿（`publish=true` で公開）。はてなの下書きも記事として扱い、投稿した範囲は `posts` に記録する
- 範囲（start・end）は `record_blog_post` と同じ規則で、**投稿する前に**検証する。投稿に失敗したら記録しない
- 投稿できたあとで記録に失敗したら（取り込み中の DB のロックなど）、投稿した URL と、`record_blog_post` で記録するための引数を含むエラーにする。やり直すと二重投稿になるため
- はてなとの通信エラー・応答の XML が壊れているときは、「投稿されたかわからないので下書き一覧を確かめてから」のエラーにする。タイムアウトは 30 秒（httpx2 の既定は 5 秒で、はてなの応答が遅いと投稿されたのにエラーになりうる）
- 同じ範囲の二重記録は止めずに知らせる（同じ範囲を別のサービスに投稿することがあるため。2026-09-27 に決定）
- HTTP は httpx2。Authlib は httpx2 があるとそちらを選ぶので、旧 httpx のクライアントと組み合わせると `Invalid "auth" argument` で壊れる（cha2hatena で起きた）
- 認証情報は環境変数 `HATENA_ENTRY_URL`・`HATENA_CONSUMER_KEY`・`HATENA_CONSUMER_SECRET`・`HATENA_ACCESS_TOKEN`・`HATENA_ACCESS_TOKEN_SECRET`（cha2hatena と同じ名前）。値は `<>` つきのままで動くので加工しない。足りないときは変数名だけを出すエラーにする
- cha2hatena の pydantic のスキーマ・固定カテゴリー・author・公開時刻の指定は持ち込んでいない。必要になったら足す
- はてなの応答には URL が2つある。`alternate`（記事の URL。下書きのあいだは外から見えない）と `edit`（管理画面の編集 URL）。下書きのときは、記事の URL がまだ見えないことを戻り値に書く
- テストでは `server.make_poster` を偽のポスターに差し替え、`HatenaPoster` は `httpx2.MockTransport` で確かめる。2026-09-27 に本物のはてなで下書き投稿を確認した（下書き一覧に入る・Markdown が崩れない・投稿者の表示も問題なし。`<author>` を送らなくてよい）

### はてなで確かめた挙動（2026-09-27、下書きの記事で実験）

- 記事の目印はメンバー URI（`…/atom/entry/{entry_id}`、応答の `link rel="edit"`）。**下書きのあいだは、編集するたびに記事の URL（alternate）が変わる**（末尾が最後に編集した時刻になる）。公開したあとの URL は変わらない（ユーザー談）
- PUT は記事を丸ごと置き換える。カテゴリーを省くと消える。`updated` を省いても `updated`・`published` は変わらず、`app:edited` だけが新しくなる。`author` は省いても通る
- 仕様書によると、PUT で `app:draft` を省くと「下書きでない」とみなされて公開される。PUT を作るときは必ず `app:draft` を送る

### 記録した記事の確認

- `posts.member_uri` にメンバー URI を持つ（`post_blog_article` で記録したものだけ。`record_blog_post` の記録は NULL）
- `check_blog_posts` は、記録した記事をはてなから GET して、下書きか公開か・今の URL・カテゴリー・本文の文字数を出す。URL が記録と違えば記録を今の URL に直す。見つからなければ「削除されたようです」と出すが、記録は消さない
- 下書きを公開に変える PUT は、まだ作っていない（公開は取り消しにくいので、はてなの画面で人が押す流れにしている）

## DB の版

- `PRAGMA user_version` で版を持ち、`MIGRATIONS`（版番号と SQL の並び）のうち今の版より新しいものだけを順に実行する。1回の移行は1トランザクション。SCHEMA は変えず、新しい DB も古い DB も同じ道筋で最新になる
- 版1: `posts.member_uri` を足した

## 要約（LLM）

- pydantic-ai を通して Gemini（`gemini-3-flash-preview`）を呼ぶ。LiteLLM は `openai<3`・httpx（旧）を要求し、mcp の httpx2 と食い違うので使わない
- API キーは `GEMINI_API_KEY` を明示して渡す。pydantic-ai は `GOOGLE_API_KEY` を優先して読むため、別のキーが黙って使われないようにした。見えないときはキー名を出すエラーにする
- 要約は `summaries` テーブルに保存する。キーは「範囲の最初と最後のメッセージ・種類（要約 / ブログ下書き）・モデル・プロンプトのハッシュ」。ハッシュには会話ログの形の版（`TRANSCRIPT_VERSION`）も含めるので、プロンプトや会話ログの形を変えると作り直しになる。`refresh=true` で強制的に作り直す
- 要約の形式は `prompts/summary.md`。話題ごとに `[index=12]` を付けさせ、原文を `get_messages` で読みに行けるようにしている
- 入力は本線の本文を index つきで並べたもの。いちばん長い会話でも約 36 万文字で、Gemini Flash の入力上限に収まる。60 万文字を超えたら `start` でしぼるようにエラーにする
- 実測: 70 件の会話で入力 1.8 万トークン・約 12 秒。保存済みなら 0.02 秒
- テストでは `server.make_model` を pydantic-ai の `FunctionModel` に差し替え、通信しない
- stdout を汚さないことを確認済み: pydantic-ai の初回バナー（stderr）は `BANNER_ENABLED = False` で止め、google-genai の AFC の警告は logging 経由で stderr に1回だけ出る

## ログ

- DB と同じ置き場所の `logs/tsuduri.log` に RotatingFileHandler（1MB × 5 世代）で書く。環境変数 `TSUDURI_LOG` で変えられる
- 設定するのは `server.main()` の中だけ。テストや `tsuduri-import` では import するだけなので、ファイルはできない
- `tsuduri_mcp` ロガーだけを INFO にし、root には流さない（stdout を汚さないため）。セッションごとにプロセスが立つので、書式にプロセス番号を入れる
- 依存ライブラリのどれかが、import したときに root ロガーへ stderr 向けの INFO のハンドラを付ける（httpx2 の「HTTP Request」などが stderr に出る）。stdout には出ないことを確かめた
- 記録するもの: ツールの呼び出し（名前・uuid や範囲などの一部の引数・かかった時間・成否。失敗はトレースバックごと）、Gemini の呼び出し（種類・モデル・範囲・トークン数・料金・保存済みを使ったか）、投稿（service・URL・編集 URL・下書きか・範囲。DB に記録する前に書く）
- 記録しないもの: 会話や記事の本文、検索キーワード、環境変数。ただしエラーの文言はそのまま残すので、「キーワードは2文字以上に」のエラーでは、その1文字のキーワードが入る

## 料金

- pydantic-ai の `RunUsage.cost`（中で genai-prices を使う）で出す。thinking のトークンも込みで、知らないモデルなら例外ではなく None になる
- Gemini を実際に呼んだときだけ、新しいテーブル `llm_calls(kind, conversation_uuid, model, input_tokens, output_tokens, cost_usd, created_at)` に1行記録する。`summaries` は作り直すと上書きされるので、累計には使わない
- 要約・下書きの戻り値に「約 $0.0150」と出す。わからなければ「料金は不明」、保存済みを使ったときは今回の料金がかからないことを出す

## 開発ツール

ruff（リント・整形）と ty（型チェック）。`uv run ruff check . && uv run ruff format . && uv run ty check && uv run pytest`

## 未決定

- レビュー（2026-09-27）の残り
  - DB を使わない枝の計算（`_Node`・`_select_line`・枝の数・未投稿の始まりの後半・範囲の検証）を `lines.py` に出す。store.py は SQL だけにし、枝分かれのテストを DB なしで書けるようにする
  - 根が複数ある会話で、draft・record・post の既定の範囲を確かめるテスト（実データに 39 件ある）
  - 飛ばした範囲の注記（`skip_note`）が、途中だけ投稿したときに範囲を言い切ってしまう。「index k より前に投稿済みの部分があるので k から」のように直す
  - SQL: 期間で絞った一覧を `c.uuid IN (SELECT conversation_uuid FROM messages WHERE created_at …)` にする（初回が最大 4 秒）、`posts(conversation_uuid)` の索引、取り込みの最後に `PRAGMA optimize`
- 下書きを公開に変える PUT（`app:draft` を必ず送る。カテゴリーも送り直す）。ほしくなったら
- Windows の Claude デスクトップで試す（README に設定例あり）
- 投稿の一覧を出すツールや、記録の取り消し。使ってみて必要なら
- 枝の一覧を出すツール（各枝の最後の発言の冒頭と日時）。使ってみて必要なら
- P3 の残り: Qiita / Dev.to
- DeepSeek / OpenAI（pydantic-ai ならモデル名を足すだけ）
- 料金の累計を見るツール（`llm_calls` を集計する）。使ってみて必要なら
- エクスポート JSON の読み込みを pydantic のモデルで検証する（形式が増えたときに、どの項目がおかしいかをわかるようにする）
- projects テーブル
- thinking やツールの入出力も検索対象にするか（`raw_content` に残っている）
