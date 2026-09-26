import logging

from tsuduri_mcp import log
from tsuduri_mcp.store import default_db_path


def test_db_is_in_user_data_dir_regardless_of_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("TSUDURI_DB", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.chdir(tmp_path)

    assert default_db_path() == tmp_path / "share" / "tsuduri-mcp" / "tsuduri.db"


def test_tsuduri_db_overrides_default(tmp_path, monkeypatch):
    monkeypatch.setenv("TSUDURI_DB", str(tmp_path / "other.db"))

    assert default_db_path() == tmp_path / "other.db"


def test_log_is_in_user_data_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("TSUDURI_LOG", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    logger = logging.getLogger(log.LOGGER_NAME)
    before = list(logger.handlers)
    try:
        log.setup_logging()
        logger.info("テストのログ")
    finally:
        for handler in logger.handlers:
            if handler not in before:
                handler.close()
                logger.removeHandler(handler)

    assert "テストのログ" in (tmp_path / "share" / "tsuduri-mcp" / "logs" / "tsuduri.log").read_text(encoding="utf-8")
