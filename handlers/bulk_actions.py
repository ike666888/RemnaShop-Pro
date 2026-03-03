import datetime
import re
from typing import Iterable

UUID_RE = re.compile(r"^[0-9a-fA-F-]{32,36}$")


def parse_uuids(text: str) -> list[str]:
    raw = re.split(r"[\s,;]+", (text or "").strip())
    uuids = []
    seen = set()
    for item in raw:
        if not item:
            continue
        if not UUID_RE.match(item):
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        uuids.append(item)
    return uuids


def chunked(items: Iterable[str], size: int = 500):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


def parse_expire_days_and_uuids(text: str):
    parts = (text or "").strip().splitlines()
    if len(parts) < 2:
        raise ValueError("格式错误，应为：第一行天数，后续为UUID列表")
    days = int(parts[0].strip())
    if days <= 0:
        raise ValueError("天数必须 > 0")
    uuids = parse_uuids("\n".join(parts[1:]))
    expire_at = (datetime.datetime.utcnow() + datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return expire_at, uuids


def parse_traffic_and_uuids(text: str):
    parts = (text or "").strip().splitlines()
    if len(parts) < 2:
        raise ValueError("格式错误，应为：第一行GB，后续为UUID列表")
    gb = float(parts[0].strip())
    if gb <= 0:
        raise ValueError("流量必须 > 0")
    uuids = parse_uuids("\n".join(parts[1:]))
    traffic_bytes = int(gb * 1024 * 1024 * 1024)
    return traffic_bytes, uuids


async def run_bulk_action(safe_api_request, action: str, uuids: list[str], extra_fields: dict | None = None):
    ok = 0
    fail = 0
    for batch in chunked(uuids, size=500):
        if action == "reset":
            resp = await safe_api_request('POST', '/users/bulk/reset-traffic', json_data={"uuids": batch})
        elif action == "delete":
            resp = await safe_api_request('POST', '/users/bulk/delete', json_data={"uuids": batch})
        elif action == "disable":
            resp = await safe_api_request('POST', '/users/bulk/update', json_data={"uuids": batch, "fields": {"status": "DISABLED"}})
        elif action == "expire":
            resp = await safe_api_request('POST', '/users/bulk/update', json_data={"uuids": batch, "fields": {"expireAt": extra_fields["expireAt"]}})
        elif action == "traffic":
            resp = await safe_api_request('POST', '/users/bulk/update', json_data={"uuids": batch, "fields": {"trafficLimitBytes": extra_fields["trafficLimitBytes"]}})
        else:
            raise ValueError("unknown action")

        if resp and resp.status_code in (200, 201, 204):
            ok += len(batch)
        else:
            fail += len(batch)
    return ok, fail
