"""ツールの引数（ローカル時刻）と DB の日時（UTC の ISO 文字列）の変換"""

from datetime import UTC, date, datetime, timedelta, tzinfo

DB_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"  # エクスポートの日時と同じ形。文字列のまま大小を比べられる


def _to_db(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(DB_FORMAT)


def now_db() -> str:
    return _to_db(datetime.now(UTC))


def since_to_db(value: str, tz: tzinfo | None = None) -> str:
    """`2026-09-20` はその日の 0 時（ローカル時刻）から。"""
    return _to_db(_parse(value, tz))


def until_to_db(value: str, tz: tzinfo | None = None) -> str:
    """`2026-09-20` はその日を含む（翌日 0 時の手前まで）。日時で渡したときはその時刻の手前まで。"""
    dt = _parse(value, tz)
    if _is_date_only(value):
        dt += timedelta(days=1)
    return _to_db(dt)


def db_to_local(value: str, tz: tzinfo | None = None) -> str:
    """表示用。`2026-09-20 21:03` の形にする。

    tz を渡さないときは、固定の時差ではなく、その日時ぶんの実際の時差（サマータイムを含む）を使う。
    """
    dt = datetime.strptime(value, DB_FORMAT).replace(tzinfo=UTC)
    local = dt.astimezone(tz) if tz else dt.astimezone()
    return local.strftime("%Y-%m-%d %H:%M")


def _is_date_only(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _parse(value: str, tz: tzinfo | None) -> datetime:
    """value を aware な datetime にする。

    tz を渡さないときは、固定の時差ではなく、その日時ぶんの実際の時差を使う
    （naive な datetime はシステムのローカル時刻とみなす）。
    """
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as e:
        raise ValueError(f"日付は YYYY-MM-DD か ISO 8601 の日時で指定してください: {value!r}") from e
    if dt.tzinfo:
        return dt
    if tz:
        return dt.replace(tzinfo=tz)
    return dt.astimezone()
