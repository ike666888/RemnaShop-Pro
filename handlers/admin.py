import datetime


def format_order_detail(item: dict, logs: list[dict]) -> str:
    created = datetime.datetime.fromtimestamp(int(item['created_at'])).strftime('%Y-%m-%d %H:%M')
    log_lines = []
    for it in logs:
        ts = datetime.datetime.fromtimestamp(int(it['created_at'])).strftime('%m-%d %H:%M')
        log_lines.append(f"- {ts} | {it['action']} | {str(it.get('detail', ''))[:40]}")
    return (
        f"🧾 **订单详情**\n\n"
        f"ID: `{item['order_id']}`\n"
        f"用户: `{item['tg_id']}`\n"
        f"状态: `{item['status']}`\n"
        f"类型: `{item['order_type']}`\n"
        f"套餐: `{item['plan_key']}`\n"
        f"创建: `{created}`\n\n"
        f"最近审计:\n" + ("\n".join(log_lines) if log_lines else "- 无")
    )
