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
