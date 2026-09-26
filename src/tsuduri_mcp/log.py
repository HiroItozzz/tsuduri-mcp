"""ファイルへのログ。

MCP サーバーは stdio で通信するので、ログを stdout（や root ロガー経由でどこかに漏れる形）に
出してはいけない。`RotatingFileHandler` でファイルにだけ書く。

設定するのは `server.main()` の中だけにする。このモジュールを import しただけではファイルを作らない
（テストや `tsuduri-import` は import するだけなので、ログファイルができない）。
"""

import logging
import logging.handlers
import os
from pathlib import Path

from .paths import data_dir

LOGGER_NAME = "tsuduri_mcp"
MAX_BYTES = 1_000_000  # 1MB
BACKUP_COUNT = 5

logger = logging.getLogger(LOGGER_NAME)


def setup_logging() -> None:
    """`tsuduri_mcp` ロガーに RotatingFileHandler を1つだけ付ける。

    ファイルは既定で data_dir() の下の `logs/tsuduri.log`。環境変数 `TSUDURI_LOG` で変えられる。
    Claude Code などクライアントごとにプロセスが立つので、書式に %(process)d を入れて見分けられるようにする。
    """
    path = Path(os.environ.get("TSUDURI_LOG") or data_dir() / "logs" / "tsuduri.log")
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(process)d] %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # root ロガー（や stdout への設定）に流さない
