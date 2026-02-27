import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_CLIENTS: dict[bool, httpx.AsyncClient] = {}


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


async def safe_api_request(method, endpoint, panel_url, headers, verify_tls=True, json_data=None):
    url = f"{panel_url}{endpoint}"
    client = _get_client(verify_tls)
    max_attempts = 3
    backoff_seconds = 0.6

    for attempt in range(1, max_attempts + 1):
        try:
            if method == 'GET':
                resp = await client.get(url, headers=headers)
            elif method == 'POST':
                resp = await client.post(url, json=json_data, headers=headers)
            elif method == 'PATCH':
                resp = await client.patch(url, json=json_data, headers=headers)
            elif method == 'DELETE':
                resp = await client.delete(url, headers=headers)
            else:
                raise ValueError(f"Unsupported method: {method}")

            if resp.status_code in _RETRYABLE_STATUS_CODES and attempt < max_attempts:
                await asyncio.sleep(backoff_seconds * attempt)
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
                await asyncio.sleep(backoff_seconds * attempt)
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
        return payload if isinstance(payload, list) else []
    return []


async def get_internal_squad_accessible_nodes(uuid, panel_url, headers, verify_tls=True):
    resp = await safe_api_request('GET', f'/internal-squads/{uuid}/accessible-nodes', panel_url, headers, verify_tls)
    if resp and resp.status_code == 200:
        payload = extract_payload(resp)
        return payload if isinstance(payload, list) else []
    return []


async def get_bandwidth_nodes_realtime(panel_url, headers, verify_tls=True):
    resp = await safe_api_request('GET', '/bandwidth-stats/nodes/realtime', panel_url, headers, verify_tls)
    if resp and resp.status_code == 200:
        payload = extract_payload(resp)
        return payload if isinstance(payload, list) else []
    return []


async def bulk_move_users_to_squad(uuids, squad_uuid, panel_url, headers, verify_tls=True):
    payload = {'uuids': uuids, 'fields': {'externalSquadUuid': squad_uuid}}
    return await safe_api_request('POST', '/users/bulk/update', panel_url, headers, verify_tls, json_data=payload)
