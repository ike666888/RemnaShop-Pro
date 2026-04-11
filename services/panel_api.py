import asyncio
import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_CLIENTS: dict[bool, httpx.AsyncClient] = {}

IP_CONTROL_ENDPOINT_SPECS: tuple[tuple[str, str], ...] = (
    ('POST', '/ip-control/drop-connections'),
    ('POST', '/ip-control/fetch-ips/{uuid}'),
    ('GET', '/ip-control/fetch-ips/result/{jobId}'),
)


def _get_client(verify_tls: bool) -> httpx.AsyncClient:
    client = _CLIENTS.get(verify_tls)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=20.0, verify=verify_tls)
        _CLIENTS[verify_tls] = client
    return client


def extract_payload(resp: httpx.Response):
    data = resp.json()
    if isinstance(data, dict):
        if 'response' in data:
            return data['response']
        if 'data' in data:
            return data['data']
    return data


async def close_all_clients() -> None:
    for client in list(_CLIENTS.values()):
        if not client.is_closed:
            await client.aclose()
    _CLIENTS.clear()




def _calc_retry_delay(resp: Optional[httpx.Response], attempt: int, base: float = 0.6) -> float:
    if resp is not None and resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(float(retry_after), 0.0)
            except ValueError:
                pass
    return base * attempt


def _build_request_kwargs(json_data: Optional[dict[str, Any]] = None, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if json_data is not None:
        kwargs["json"] = json_data
    if params is not None:
        kwargs["params"] = params
    return kwargs

async def safe_api_request(method, endpoint, panel_url, headers, verify_tls=True, json_data=None, params=None):
    url = f"{panel_url}{endpoint}"
    client = _get_client(verify_tls)
    max_attempts = 3

    for attempt in range(1, max_attempts + 1):
        resp = None
        try:
            req_kwargs = _build_request_kwargs(json_data=json_data, params=params)
            resp = await client.request(method.upper(), url, headers=headers, **req_kwargs)

            if resp.status_code in _RETRYABLE_STATUS_CODES and attempt < max_attempts:
                await asyncio.sleep(_calc_retry_delay(resp, attempt))
                continue

            if resp.status_code >= 400:
                logger.warning(
                    "Panel API returned %s [%s %s]: %s",
                    resp.status_code,
                    method,
                    endpoint,
                    resp.text[:300],
                )
            return resp
        except httpx.HTTPError as exc:
            if attempt < max_attempts:
                await asyncio.sleep(_calc_retry_delay(resp, attempt))
                continue
            logger.error("API HTTP Error [%s %s]: %s", method, endpoint, exc)
            return None
        except Exception as exc:
            logger.error("API Error [%s %s]: %s", method, endpoint, exc)
            return None


async def get_panel_user(uuid, panel_url, headers, verify_tls=True):
    resp = await safe_api_request('GET', f"/users/{uuid}", panel_url, headers, verify_tls)
    if resp and resp.status_code == 200:
        return extract_payload(resp)
    return None


async def get_user_by_telegram_id(telegram_id, panel_url, headers, verify_tls=True):
    resp = await safe_api_request('GET', f"/users/by-telegram-id/{telegram_id}", panel_url, headers, verify_tls)
    if resp and resp.status_code == 200:
        payload = extract_payload(resp)
        return payload if isinstance(payload, dict) else None
    return None


async def get_nodes_status(panel_url, headers, verify_tls=True):
    resp = await safe_api_request('GET', '/nodes', panel_url, headers, verify_tls)
    if resp and resp.status_code == 200:
        payload = extract_payload(resp)
        return payload if isinstance(payload, list) else []
    return []


async def get_subscription_history_stats(panel_url, headers, verify_tls=True):
    resp = await safe_api_request('GET', '/subscription-request-history/stats', panel_url, headers, verify_tls)
    if resp and resp.status_code == 200:
        payload = extract_payload(resp)
        return payload if isinstance(payload, dict) else {}
    return {}


async def get_user_subscription_history(uuid, panel_url, headers, verify_tls=True):
    resp = await safe_api_request('GET', f"/users/{uuid}/subscription-request-history", panel_url, headers, verify_tls)
    if resp and resp.status_code == 200:
        payload = extract_payload(resp)
        return payload if isinstance(payload, dict) else {}
    return {}


async def get_subscription_settings(panel_url, headers, verify_tls=True):
    resp = await safe_api_request('GET', '/subscription-settings', panel_url, headers, verify_tls)
    if resp and resp.status_code == 200:
        payload = extract_payload(resp)
        return payload if isinstance(payload, dict) else {}
    return {}


async def patch_subscription_settings(panel_url, headers, payload, verify_tls=True):
    return await safe_api_request('PATCH', '/subscription-settings', panel_url, headers, verify_tls, json_data=payload)


async def get_internal_squads(panel_url, headers, verify_tls=True):
    resp = await safe_api_request('GET', '/internal-squads', panel_url, headers, verify_tls)
    if resp and resp.status_code == 200:
        payload = extract_payload(resp)
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            squads = payload.get('internalSquads')
            return squads if isinstance(squads, list) else []
    return []


async def get_internal_squad_accessible_nodes(uuid, panel_url, headers, verify_tls=True):
    resp = await safe_api_request('GET', f'/internal-squads/{uuid}/accessible-nodes', panel_url, headers, verify_tls)
    if resp and resp.status_code == 200:
        payload = extract_payload(resp)
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            nodes = payload.get('accessibleNodes')
            return nodes if isinstance(nodes, list) else []
    return []


async def get_bandwidth_nodes_realtime(panel_url, headers, verify_tls=True):
    resp = await safe_api_request('GET', '/bandwidth-stats/nodes/realtime', panel_url, headers, verify_tls)
    if resp and resp.status_code == 200:
        payload = extract_payload(resp)
        return payload if isinstance(payload, list) else []
    return []


async def bulk_move_users_to_squad(uuids, squad_uuid, panel_url, headers, verify_tls=True):
    # Remnawave API v2.6.x 推荐 /users/bulk/update-squads
    payload = {'uuids': uuids, 'activeInternalSquads': [squad_uuid] if squad_uuid else []}
    resp = await safe_api_request('POST', '/users/bulk/update-squads', panel_url, headers, verify_tls, json_data=payload)
    if resp and resp.status_code in (200, 201, 204):
        return resp

    # 兼容旧版 API：退回到 legacy bulk update 字段
    fallback_payload = {'uuids': uuids, 'fields': {'externalSquadUuid': squad_uuid}}
    return await safe_api_request('POST', '/users/bulk/update', panel_url, headers, verify_tls, json_data=fallback_payload)


async def create_user(payload, panel_url, headers, verify_tls=True):
    return await safe_api_request('POST', '/users', panel_url, headers, verify_tls, json_data=payload)


async def patch_user(payload, panel_url, headers, verify_tls=True):
    return await safe_api_request('PATCH', '/users', panel_url, headers, verify_tls, json_data=payload)


async def delete_user(uuid, panel_url, headers, verify_tls=True):
    return await safe_api_request('DELETE', f"/users/{uuid}", panel_url, headers, verify_tls)


async def enable_user(uuid, panel_url, headers, verify_tls=True):
    return await safe_api_request('POST', f"/users/{uuid}/actions/enable", panel_url, headers, verify_tls)


async def disable_user(uuid, panel_url, headers, verify_tls=True):
    return await safe_api_request('POST', f"/users/{uuid}/actions/disable", panel_url, headers, verify_tls)


async def reset_user_traffic(uuid, panel_url, headers, verify_tls=True):
    return await safe_api_request('POST', f"/users/{uuid}/actions/reset-traffic", panel_url, headers, verify_tls)


async def get_subscription_request_history(panel_url, headers, verify_tls=True):
    resp = await safe_api_request('GET', '/subscription-request-history', panel_url, headers, verify_tls)
    if resp and resp.status_code == 200:
        payload = extract_payload(resp)
        return payload if isinstance(payload, list) else []
    return []


async def bulk_delete_users(uuids, panel_url, headers, verify_tls=True):
    return await safe_api_request('POST', '/users/bulk/delete', panel_url, headers, verify_tls, json_data={"uuids": uuids})


async def bulk_reset_traffic_users(uuids, panel_url, headers, verify_tls=True):
    return await safe_api_request('POST', '/users/bulk/reset-traffic', panel_url, headers, verify_tls, json_data={"uuids": uuids})


async def bulk_update_users(uuids, fields, panel_url, headers, verify_tls=True):
    return await safe_api_request('POST', '/users/bulk/update', panel_url, headers, verify_tls, json_data={"uuids": uuids, "fields": fields})


async def probe_api_capabilities(panel_url, headers, verify_tls=True):
    checks = {
        "users_bulk_update_squads": ('POST', '/users/bulk/update-squads', {"uuids": [], "activeInternalSquads": []}),
        "users_bulk_update": ('POST', '/users/bulk/update', {"uuids": [], "fields": {}}),
        "users_bulk_delete": ('POST', '/users/bulk/delete', {"uuids": []}),
        "subscription_history": ('GET', '/subscription-request-history', None),
        "ip_control_drop_connections": (
            'POST',
            '/ip-control/drop-connections',
            {
                "dropBy": {"by": "ipAddresses", "ipAddresses": ["127.0.0.1"]},
                "targetNodes": {"target": "allNodes"},
            },
        ),
        "ip_control_fetch_ips": ('POST', '/ip-control/fetch-ips/00000000-0000-0000-0000-000000000000', None),
        "ip_control_fetch_ips_result": ('GET', '/ip-control/fetch-ips/result/00000000-0000-0000-0000-000000000000', None),
        "metadata": ('GET', '/metadata', None),
        "system_health": ('GET', '/system/health', None),
        "snippets": ('GET', '/snippets', None),
        "subscription_page_configs": ('GET', '/subscription-page-configs', None),
        "external_squads": ('GET', '/external-squads', None),
        "config_profiles": ('GET', '/config-profiles', None),
    }
    result = {}
    for key, (method, endpoint, payload) in checks.items():
        resp = await safe_api_request(method, endpoint, panel_url, headers, verify_tls, json_data=payload)
        result[key] = bool(resp and resp.status_code not in (404, 405))
    result["ip_control"] = bool(
        result.get("ip_control_drop_connections")
        or result.get("ip_control_fetch_ips")
        or result.get("ip_control_fetch_ips_result")
    )
    return result


async def _request_first_success(candidates, panel_url, headers, verify_tls=True, json_data=None, params=None):
    for method, endpoint in candidates:
        resp = await safe_api_request(method, endpoint, panel_url, headers, verify_tls, json_data=json_data, params=params)
        if resp and resp.status_code < 400:
            return resp
    return None


async def set_user_metadata(user_uuid, metadata: dict[str, Any], panel_url, headers, verify_tls=True):
    payload = {"userUuid": user_uuid, "metadata": metadata}
    candidates = [
        ('POST', '/metadata/user'),
        ('PUT', '/metadata/user'),
        ('PATCH', '/metadata/user'),
    ]
    return await _request_first_success(candidates, panel_url, headers, verify_tls, json_data=payload)


async def block_ip_address(ip: str, reason: str, panel_url, headers, verify_tls=True):
    payload = {
        "dropBy": {"by": "ipAddresses", "ipAddresses": [ip]},
        "targetNodes": {"target": "allNodes"},
    }
    resp = await safe_api_request('POST', '/ip-control/drop-connections', panel_url, headers, verify_tls, json_data=payload)
    if resp and resp.status_code < 400:
        logger.info("Dropped connections for ip=%s (reason=%s)", ip, reason)
        return resp
    return None


async def get_system_health(panel_url, headers, verify_tls=True):
    candidates = [
        ('GET', '/system/health'),
        ('GET', '/system/info'),
        ('GET', '/remnawave-settings'),
    ]
    resp = await _request_first_success(candidates, panel_url, headers, verify_tls)
    if resp and resp.status_code == 200:
        payload = extract_payload(resp)
        return payload if isinstance(payload, dict) else {}
    return {}


async def get_snippet_by_key(key: str, panel_url, headers, verify_tls=True):
    candidates = [
        ('GET', f'/snippets/{key}'),
        ('GET', '/snippets'),
    ]
    for method, endpoint in candidates:
        params = {"key": key} if endpoint == '/snippets' else None
        resp = await safe_api_request(method, endpoint, panel_url, headers, verify_tls, params=params)
        if resp and resp.status_code == 200:
            payload = extract_payload(resp)
            if isinstance(payload, dict):
                return payload
            if isinstance(payload, list):
                for row in payload:
                    if isinstance(row, dict) and (row.get('key') == key or row.get('name') == key):
                        return row
    return {}


async def get_subscription_page_configs(panel_url, headers, verify_tls=True):
    resp = await safe_api_request('GET', '/subscription-page-configs', panel_url, headers, verify_tls)
    if resp and resp.status_code == 200:
        payload = extract_payload(resp)
        return payload if isinstance(payload, (list, dict)) else {}
    return {}


async def get_external_squads(panel_url, headers, verify_tls=True):
    resp = await safe_api_request('GET', '/external-squads', panel_url, headers, verify_tls)
    if resp and resp.status_code == 200:
        payload = extract_payload(resp)
        return payload if isinstance(payload, list) else []
    return []


async def get_config_profiles(panel_url, headers, verify_tls=True):
    resp = await safe_api_request('GET', '/config-profiles', panel_url, headers, verify_tls)
    if resp and resp.status_code == 200:
        payload = extract_payload(resp)
        return payload if isinstance(payload, list) else []
    return []
