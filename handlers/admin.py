import datetime

STATUS_CN = {
    'pending': '待审核',
    'approved': '已通过(处理中)',
    'rejected': '已拒绝',
    'delivered': '已发货',
    'failed': '失败',
}

TYPE_CN = {
    'new': '新购',
    'renew': '续费',
}

ACTION_CN = {
    'create': '创建订单',
    'reject': '管理员拒绝',
    'retry': '管理员重试',
    'deliver_success': '发货成功',
    'deliver_failed': '发货失败',
}

REASON_CN = {
    'network': '网络/接口异常',
    'business_validation': '业务校验失败',
    'database': '数据库异常',
    'telegram': 'Telegram发送异常',
    'unknown': '未知原因',
}


def order_status_label(status: str) -> str:
    return STATUS_CN.get(status, status)


def order_type_label(order_type: str) -> str:
    return TYPE_CN.get(order_type, order_type)


def _translate_reason_detail(detail: str) -> str:
    text = str(detail or '')
    if not text.startswith('reason:'):
        return text
    # format: reason:<category>|<raw>
    category = text.split('|', 1)[0].replace('reason:', '')
    raw = text.split('|', 1)[1] if '|' in text else ''
    cn = REASON_CN.get(category, category)
    if raw:
        return f"{cn}（{raw[:40]}）"
    return cn


def action_label(action: str) -> str:
    return ACTION_CN.get(action, action)


def format_order_row(item: dict) -> str:
    ts = datetime.datetime.fromtimestamp(int(item['created_at'])).strftime('%m-%d %H:%M')
    status = order_status_label(item.get('status', ''))
    icon = {
        '待审核': '🟡',
        '已发货': '✅',
        '失败': '❌',
        '已拒绝': '⛔',
    }.get(status, '📄')
    return f"{icon} {status} | {item['order_id']} | {item['tg_id']} | {ts}"


def format_order_detail(item: dict, logs: list[dict]) -> str:
    created = datetime.datetime.fromtimestamp(int(item['created_at'])).strftime('%Y-%m-%d %H:%M')
    log_lines = []
    for it in logs:
        ts = datetime.datetime.fromtimestamp(int(it['created_at'])).strftime('%m-%d %H:%M')
        action = action_label(it.get('action', ''))
        detail = _translate_reason_detail(str(it.get('detail', '')))
        log_lines.append(f"- {ts} | {action} | {detail[:40]}")

    err = _translate_reason_detail(item.get('error_message') or '无')
    return (
        f"🧾 **订单详情**\n\n"
        f"ID: `{item['order_id']}`\n"
        f"用户: `{item['tg_id']}`\n"
        f"状态: `{order_status_label(item['status'])}`\n"
        f"类型: `{order_type_label(item['order_type'])}`\n"
        f"套餐: `{item['plan_key']}`\n"
        f"失败原因: `{err}`\n"
        f"创建时间: `{created}`\n\n"
        f"最近审计:\n" + ("\n".join(log_lines) if log_lines else "- 无")
    )
