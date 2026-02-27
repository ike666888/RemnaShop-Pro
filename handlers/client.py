import datetime


def build_nodes_status_message(nodes: list[dict]) -> str:
    msg_list = ["🌍 **节点状态**\n"]
    if not nodes:
        msg_list.append("⚠️ 暂无节点信息")
    else:
        for node in nodes:
            name = node.get('name', '未知节点')
            status_raw = str(node.get('status', '')).lower()
            is_online = status_raw in ['connected', 'healthy', 'online', 'active', 'true'] or node.get('isConnected') is True
            icon = "🟢" if is_online else "🔴"
            stat_text = "在线" if is_online else "离线"
            msg_list.append(f"{icon} **{name}** | {stat_text}")
    msg_list.append(f"\n_更新时间: {datetime.datetime.now().strftime('%H:%M:%S')}_")
    return "\n".join(msg_list)
