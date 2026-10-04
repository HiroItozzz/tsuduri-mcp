import os
from pathlib import Path

APP_NAME = "tsuduri-mcp"


def data_dir() -> Path:
    """DB とログの既定の置き場所。起動したときの作業フォルダ（cwd）に左右されないようにする。"""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    else:
        base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / APP_NAME


def export_dir() -> Path:
    """export_conversation の書き出し先。既定はホームの `Documents/tsuduri-mcp`。

    Windows のドキュメントは OneDrive の下に移されていることがあるが、会話の全文をクラウドに
    同期させないよう、OS には聞かずにホームの Documents にする。環境変数 `TSUDURI_EXPORT_DIR` で変えられる。
    """
    return Path(os.environ.get("TSUDURI_EXPORT_DIR") or Path.home() / "Documents" / APP_NAME)
