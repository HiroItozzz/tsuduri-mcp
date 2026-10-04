import logging
from pathlib import Path

from tsuduri_mcp import log
from tsuduri_mcp.paths import export_dir
from tsuduri_mcp.store import default_db_path


def use_data_home(monkeypatch, base):
    # Linux などは XDG_DATA_HOME、Windows は LOCALAPPDATA を見るので、両方を同じ場所に向ける
    monkeypatch.setenv("XDG_DATA_HOME", str(base))
    monkeypatch.setenv("LOCALAPPDATA", str(base))


def test_db_is_in_user_data_dir_regardless_of_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("TSUDURI_DB", raising=False)
    use_data_home(monkeypatch, tmp_path / "share")
    monkeypatch.chdir(tmp_path)

    assert default_db_path() == tmp_path / "share" / "tsuduri-mcp" / "tsuduri.db"


def test_tsuduri_db_overrides_default(tmp_path, monkeypatch):
    monkeypatch.setenv("TSUDURI_DB", str(tmp_path / "other.db"))

    assert default_db_path() == tmp_path / "other.db"


def test_export_dir_is_under_home_documents(tmp_path, monkeypatch):
    monkeypatch.delenv("TSUDURI_EXPORT_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert export_dir() == tmp_path / "Documents" / "tsuduri-mcp"


def test_tsuduri_export_dir_overrides_default(tmp_path, monkeypatch):
    monkeypatch.setenv("TSUDURI_EXPORT_DIR", str(tmp_path / "out"))

    assert export_dir() == tmp_path / "out"


def test_log_is_in_user_data_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("TSUDURI_LOG", raising=False)
    use_data_home(monkeypatch, tmp_path / "share")
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
