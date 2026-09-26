"""ツールの引数（ローカル時刻）と DB の日時（UTC の ISO 文字列）の変換"""

from datetime import date, datetime, timedelta, timezone, tzinfo

DB_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"  # エクスポートの日時と同じ形。文字列のまま大小を比べられる


def local_tz() -> tzinfo:
    tz = datetime.now().astimezone().tzinfo
    assert tz is not None
    return tz


def _to_db(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(DB_FORMAT)


def since_to_db(value: str, tz: tzinfo | None = None) -> str:
    """`2026-09-20` はその日の 0 時（ローカル時刻）から。"""
    return _to_db(_parse(value, tz or local_tz()))


def until_to_db(value: str, tz: tzinfo | None = None) -> str:
    """`2026-09-20` はその日を含む（翌日 0 時の手前まで）。日時で渡したときはその時刻の手前まで。"""
    tz = tz or local_tz()
    dt = _parse(value, tz)
    if _is_date_only(value):
        dt += timedelta(days=1)
    return _to_db(dt)


def db_to_local(value: str, tz: tzinfo | None = None) -> str:
    """表示用。`2026-09-20 21:03` の形にする。"""
    dt = datetime.strptime(value, DB_FORMAT).replace(tzinfo=timezone.utc)
    return dt.astimezone(tz or local_tz()).strftime("%Y-%m-%d %H:%M")


def _is_date_only(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _parse(value: str, tz: tzinfo) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as e:
        raise ValueError(f"日付は YYYY-MM-DD か ISO 8601 の日時で指定してください: {value!r}") from e
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)
