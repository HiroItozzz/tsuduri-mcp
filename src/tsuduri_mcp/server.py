from mcp.server.mcpserver import MCPServer

mcp = MCPServer("tsuduri")


@mcp.tool()
def hello(name: str) -> str:
    """動作確認用。名前を受け取って挨拶を返す。"""
    return f"こんにちは、{name}さん。tsuduri-mcp は動いています。"


def main() -> None:
    mcp.run()  # 既定は stdio。stdout は通信に使われるので print してはいけない
