import logging
import httpx

logger = logging.getLogger(__name__)


async def safe_api_request(method, endpoint, panel_url, headers, verify_tls=True, json_data=None):
    url = f"{panel_url}{endpoint}"
    try:
        async with httpx.AsyncClient(timeout=20.0, verify=verify_tls) as client:
            if method == 'GET':
                return await client.get(url, headers=headers)
            if method == 'POST':
                return await client.post(url, json=json_data, headers=headers)
            if method == 'PATCH':
                return await client.patch(url, json=json_data, headers=headers)
            if method == 'DELETE':
                return await client.delete(url, headers=headers)
            raise ValueError(f"Unsupported method: {method}")
    except Exception as exc:
        logger.error("API Error [%s %s]: %s", method, endpoint, exc)
        return None


async def get_panel_user(uuid, panel_url, headers, verify_tls=True):
    resp = await safe_api_request('GET', f"/users/{uuid}", panel_url, headers, verify_tls)
    if resp and resp.status_code == 200:
        return resp.json().get('response', resp.json())
    return None


async def get_nodes_status(panel_url, headers, verify_tls=True):
    resp = await safe_api_request('GET', '/nodes', panel_url, headers, verify_tls)
    if resp and resp.status_code == 200:
        data = resp.json()
        return data.get('response', data.get('data', []))
    return []
