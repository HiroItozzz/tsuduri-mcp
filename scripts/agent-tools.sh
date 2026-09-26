# Claude Code のサブエージェントの様子を見るための関数。
# ~/.bashrc に次の1行を足すと使えるようになる:
#   source ~/dev/projects/tsuduri-mcp/scripts/agent-tools.sh
#
# 使い方:
#   agents          サブエージェントの一覧（新しい順に 10 件。件数は引数で変えられる）
#   watch-agent     いちばん新しいサブエージェントの動きを流して見る（Ctrl+C で止める）
#   watch-agent <ファイル>   一覧に出たファイルを指定して見る

# サブエージェントの記録ファイル（1行が1つの JSON）
_agent_files() {
  ls -t ~/.claude/projects/*/*/subagents/agent-*.jsonl 2>/dev/null
}

# 記録ファイルから、エージェントの名前を探す。
# 設定ファイル（.claude/agents/*.md）の「あなたの名前は「○○」です」を読んでいれば、その名前。
# なければ、読んだ設定ファイルの名前（implementer など）。
_agent_name() {
  local file=$1
  local name
  name=$(grep -o -m1 'あなたの名前は「[^」]*」' "$file" | sed 's/あなたの名前は「//; s/」//')
  if [ -z "$name" ]; then
    name=$(grep -o -m1 'agents/[a-z-]*\.md' "$file" | sed 's|agents/||; s|\.md||')
  fi
  echo "${name:-（不明）}"
}

agents() {
  local file
  for file in $(_agent_files | head -"${1:-10}"); do
    printf '%-12s  %s  %s\n' "$(_agent_name "$file")" "$(date -r "$file" '+%m/%d %H:%M')" "$file"
  done
}

# jq で、エージェントの発言（💬）と、使ったツール（🔧）だけを取り出す
_AGENT_JQ='
  select(.type == "assistant")
  | .message.content[]?
  | if .type == "text" then
      "💬 " + .text
    elif .type == "tool_use" then
      "🔧 " + .name + " " + ((.input.command // .input.file_path // .input.pattern // "") | tostring | .[0:100])
    else
      empty
    end
'

watch-agent() {
  local file=${1:-$(_agent_files | head -1)}
  if [ -z "$file" ]; then
    echo "サブエージェントの記録が見つかりません"
    return 1
  fi
  echo "== $(_agent_name "$file") =="
  tail -n +1 -f "$file" | jq --unbuffered -r "$_AGENT_JQ"
}
