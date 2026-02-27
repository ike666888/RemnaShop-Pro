import datetime


def should_send_expire_notice(last_notify_at: int | None, now_ts: int, cool_down_seconds: int = 20 * 3600) -> bool:
    if not last_notify_at:
        return True
    return (now_ts - int(last_notify_at)) >= cool_down_seconds


def parse_expire_datetime(iso_str: str):
    if not iso_str:
        return None
    try:
        return datetime.datetime.strptime(iso_str.split('.')[0].replace('Z', ''), "%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None
