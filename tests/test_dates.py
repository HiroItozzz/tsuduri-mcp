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
