import time
from datetime import timedelta, timezone

import pytest

from tsuduri_mcp.dates import db_to_local, since_to_db, until_to_db

JST = timezone(timedelta(hours=9))


def test_date_is_local_midnight():
    assert since_to_db("2026-09-20", JST) == "2026-09-19T15:00:00.000000Z"


def test_until_date_includes_the_whole_day():
    assert until_to_db("2026-09-20", JST) == "2026-09-20T15:00:00.000000Z"


def test_datetime_with_offset_is_respected():
    assert until_to_db("2026-09-20T12:00:00+00:00", JST) == "2026-09-20T12:00:00.000000Z"


def test_db_time_is_shown_in_local_time():
    assert db_to_local("2026-09-20T15:30:00.000000Z", JST) == "2026-09-21 00:30"


def test_invalid_date_is_error():
    with pytest.raises(ValueError):
        since_to_db("先週", JST)


@pytest.fixture
def new_york(monkeypatch):
    """システムのタイムゾーンを一時的にニューヨーク（夏時間あり）にする。"""
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()  # 元の TZ（なければ未設定）に戻してから、C ライブラリにも読み直させる
    time.tzset()


def test_no_tz_parses_using_the_datetime_own_offset(new_york):
    # tz を渡さないときは「今」の時差を固定で使うのではなく、渡された日時ごとの実際の時差（夏時間込み）を使う
    assert since_to_db("2026-01-15") == "2026-01-15T05:00:00.000000Z"  # 1月は EST（-05:00）
    assert since_to_db("2026-07-15") == "2026-07-15T04:00:00.000000Z"  # 7月は EDT（-04:00）


def test_no_tz_displays_using_the_datetime_own_offset(new_york):
    assert db_to_local("2026-01-15T05:00:00.000000Z") == "2026-01-15 00:00"
    assert db_to_local("2026-07-15T04:00:00.000000Z") == "2026-07-15 00:00"
