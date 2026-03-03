from collections import defaultdict


def build_anomaly_incidents(logs, last_scan_ts, whitelist, ip_threshold):
    user_ip_map = defaultdict(set)
    user_ua_map = defaultdict(set)
    user_logs = defaultdict(list)
    max_seen_ts = last_scan_ts

    for item in logs:
        ts = int(item.get('_ts', 0) or 0)
        if ts and ts <= last_scan_ts:
            continue
        if ts > max_seen_ts:
            max_seen_ts = ts

        uid = item.get('userUuid')
        ip = item.get('ip') or item.get('requestIp')
        ua = item.get('userAgent') or ''
        if not uid or uid in whitelist or not ip:
            continue
        user_ip_map[uid].add(ip)
        if ua:
            user_ua_map[uid].add(ua[:120])
        user_logs[uid].append(item)

    incidents = []
    for uid, ips in user_ip_map.items():
        ip_count = len(ips)
        ua_diversity = len(user_ua_map.get(uid, set()))
        density = min(len(user_logs.get(uid, [])), 20)
        score = ip_count * 2 + ua_diversity + density // 3
        if ip_count <= ip_threshold and score < (ip_threshold * 2):
            continue
        evidence = []
        for row in user_logs.get(uid, [])[:10]:
            evidence.append({
                "ts": row.get('_fmt_time') or row.get('requestAt') or row.get('createdAt') or '-',
                "ip": row.get('ip') or row.get('requestIp') or '-',
                "ua": (row.get('userAgent') or '-')[:40],
            })
        incidents.append({
            "uid": uid,
            "ip_count": ip_count,
            "ua_diversity": ua_diversity,
            "density": density,
            "score": score,
            "evidence": evidence,
        })
    return incidents, max_seen_ts
