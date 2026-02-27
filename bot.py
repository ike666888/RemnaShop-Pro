import logging
import time
import datetime
import json
import os
import asyncio
import qrcode
from io import BytesIO
from collections import defaultdict
from services.panel_api import safe_api_request as api_safe_request, get_panel_user as api_get_panel_user, get_nodes_status as api_get_nodes_status, get_subscription_history_stats as api_get_subscription_history_stats, get_user_subscription_history as api_get_user_subscription_history, get_subscription_settings as api_get_subscription_settings, patch_subscription_settings as api_patch_subscription_settings, get_internal_squads as api_get_internal_squads, get_internal_squad_accessible_nodes as api_get_internal_squad_accessible_nodes, get_bandwidth_nodes_realtime as api_get_bandwidth_nodes_realtime, bulk_move_users_to_squad as api_bulk_move_users_to_squad, close_all_clients, extract_payload
from services.orders import (
    create_order,
    get_order,
    update_order_status,
    attach_payment_text,
    attach_admin_message,
    append_order_audit_log,
    classify_order_failure,
    get_pending_order_for_user,
    STATUS_PENDING,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_DELIVERED,
    STATUS_FAILED,
)
from storage.db import init_db as storage_init_db, db_query as storage_db_query, db_execute as storage_db_execute
from utils.formatting import escape_markdown_v2
from handlers.bulk_actions import parse_uuids, parse_expire_days_and_uuids, parse_traffic_and_uuids, run_bulk_action
from handlers.admin import format_order_detail, format_order_row, order_status_label
from handlers.client import build_nodes_status_message
from jobs.anomaly import build_anomaly_incidents
from jobs.expiry import should_send_expire_notice
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
DB_FILE = os.path.join(BASE_DIR, 'starlight.db')

ANOMALY_IP_THRESHOLD = 50


def parse_bool(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}

def load_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"配置文件缺失: {CONFIG_FILE}")
        exit(1)
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

config = load_config()

ADMIN_ID = int(config['admin_id'])
BOT_TOKEN = config['bot_token']
PANEL_URL = (config.get('panel_url') or '').rstrip('/') + '/api' if (config.get('panel_url') or '').strip() else ''
PANEL_TOKEN = config.get('panel_token', '')
SUB_DOMAIN = (config.get('sub_domain') or '').rstrip('/')
TARGET_GROUP_UUID = config.get('group_uuid', '')
PANEL_VERIFY_TLS = parse_bool(config.get('panel_verify_tls', True), default=True)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

user_cooldowns = {}
COOLDOWN_SECONDS = 1.0
uuid_map = {}
order_payment_method_cache = {}

def get_short_id(real_uuid):
    for sid, uid in uuid_map.items():
        if uid == real_uuid: return sid
    short_id = str(len(uuid_map) + 1)
    uuid_map[short_id] = real_uuid
    return short_id

def get_real_uuid(short_id):
    return uuid_map.get(short_id)

def check_cooldown(user_id):
    if user_id == ADMIN_ID: return True
    now = time.time()
    last_time = user_cooldowns.get(user_id, 0)
    if now - last_time < COOLDOWN_SECONDS: return False
    user_cooldowns[user_id] = now
    return True

def get_strategy_label(strategy):
    mapping = {'NO_RESET': '总流量', 'DAY': '每日重置', 'WEEK': '每周重置', 'MONTH': '每月重置'}
    return mapping.get(strategy, '总流量')

def draw_progress_bar(used, total, length=10):
    if total == 0: return "♾️ 无限制"
    percent = used / total
    if percent > 1: percent = 1
    filled_length = int(length * percent)
    bar = "█" * filled_length + "░" * (length - filled_length)
    return f"{bar} {round(percent * 100)}%"

def format_time(iso_str):
    if not iso_str: return "未知"
    try:
        clean_str = iso_str.split('.')[0].replace('Z', '')
        dt = datetime.datetime.strptime(clean_str, "%Y-%m-%dT%H:%M:%S")
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception as exc:
        logger.debug("failed to parse time %s: %s", iso_str, exc)
        return iso_str

def generate_qr(text):
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = BytesIO()
    img.save(bio)
    bio.seek(0)
    return bio

def init_db():
    storage_init_db(DB_FILE)


def db_query(query, args=(), one=False):
    return storage_db_query(DB_FILE, query, args=args, one=one)


def db_execute(query, args=()):
    return storage_db_execute(DB_FILE, query, args=args)


def get_setting_value(key, default=None):
    row = db_query("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return row['value'] if row else default


def set_setting_value(key, value):
    db_execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))


def get_json_setting(key, default):
    raw = get_setting_value(key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def set_json_setting(key, value):
    set_setting_value(key, json.dumps(value, ensure_ascii=False))


def append_ops_timeline(event_type, title, detail, actor='系统', target='-'):
    rows = get_json_setting('ops_timeline', [])
    if not isinstance(rows, list):
        rows = []
    rows.append({
        'ts': int(time.time()),
        'type': event_type,
        'title': title,
        'detail': detail[:240],
        'actor': str(actor),
        'target': str(target),
    })
    set_json_setting('ops_timeline', rows[-120:])


def push_subscription_settings_snapshot(payload, source='手动变更前快照'):
    hist = get_json_setting('subscription_settings_history', [])
    if not isinstance(hist, list):
        hist = []
    hist.append({
        'ts': int(time.time()),
        'source': source,
        'payload': payload,
    })
    set_json_setting('subscription_settings_history', hist[-10:])


def pop_subscription_settings_snapshot():
    hist = get_json_setting('subscription_settings_history', [])
    if not isinstance(hist, list) or not hist:
        return None
    item = hist.pop()
    set_json_setting('subscription_settings_history', hist)
    return item


def get_risk_watchlist():
    items = get_json_setting('risk_watchlist', [])
    if not isinstance(items, list):
        return set()
    return {str(x) for x in items if x}


def set_risk_watchlist(items):
    set_json_setting('risk_watchlist', sorted({str(x) for x in items if x}))


def panel_config_ready():
    return bool(PANEL_URL and PANEL_TOKEN and SUB_DOMAIN and TARGET_GROUP_UUID)


def save_runtime_config(**kwargs):
    global PANEL_URL, PANEL_TOKEN, SUB_DOMAIN, TARGET_GROUP_UUID, PANEL_VERIFY_TLS, config
    for k, v in kwargs.items():
        config[k] = v
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=4)
    if 'panel_url' in kwargs:
        PANEL_URL = kwargs.get('panel_url', '').rstrip('/') + '/api' if kwargs.get('panel_url') else ''
    if 'panel_token' in kwargs:
        PANEL_TOKEN = kwargs.get('panel_token', '')
    if 'sub_domain' in kwargs:
        SUB_DOMAIN = kwargs.get('sub_domain', '').rstrip('/')
    if 'group_uuid' in kwargs:
        TARGET_GROUP_UUID = kwargs.get('group_uuid', '')
    if 'panel_verify_tls' in kwargs:
        PANEL_VERIFY_TLS = parse_bool(kwargs.get('panel_verify_tls'), default=True)


init_db()


def get_headers():
    return {"Authorization": f"Bearer {PANEL_TOKEN}", "Content-Type": "application/json"}


async def safe_api_request(method, endpoint, json_data=None):
    if not PANEL_URL or not PANEL_TOKEN:
        logger.warning('panel config missing, skip request %s %s', method, endpoint)
        return None
    return await api_safe_request(method, endpoint, PANEL_URL, get_headers(), PANEL_VERIFY_TLS, json_data=json_data)


async def get_panel_user(uuid):
    return await api_get_panel_user(uuid, PANEL_URL, get_headers(), PANEL_VERIFY_TLS)


async def get_nodes_status():
    return await api_get_nodes_status(PANEL_URL, get_headers(), PANEL_VERIFY_TLS)

async def get_subscription_history_stats():
    return await api_get_subscription_history_stats(PANEL_URL, get_headers(), PANEL_VERIFY_TLS)


async def get_user_subscription_history(uuid):
    return await api_get_user_subscription_history(uuid, PANEL_URL, get_headers(), PANEL_VERIFY_TLS)


async def get_subscription_settings():
    return await api_get_subscription_settings(PANEL_URL, get_headers(), PANEL_VERIFY_TLS)


async def patch_subscription_settings(payload):
    return await api_patch_subscription_settings(PANEL_URL, get_headers(), payload, PANEL_VERIFY_TLS)


async def get_internal_squads():
    return await api_get_internal_squads(PANEL_URL, get_headers(), PANEL_VERIFY_TLS)


async def get_internal_squad_accessible_nodes(uuid):
    return await api_get_internal_squad_accessible_nodes(uuid, PANEL_URL, get_headers(), PANEL_VERIFY_TLS)


async def get_bandwidth_nodes_realtime():
    return await api_get_bandwidth_nodes_realtime(PANEL_URL, get_headers(), PANEL_VERIFY_TLS)


async def bulk_move_users_to_squad(uuids, squad_uuid):
    return await api_bulk_move_users_to_squad(uuids, squad_uuid, PANEL_URL, get_headers(), PANEL_VERIFY_TLS)


async def build_squad_capacity_summary(max_users=60):
    rows = db_query("SELECT DISTINCT uuid FROM subscriptions ORDER BY id DESC LIMIT ?", (max_users,))
    uuids = [dict(r)['uuid'] for r in rows]
    if not uuids:
        return "暂无订阅样本", None
    infos = await asyncio.gather(*[get_panel_user(u) for u in uuids])
    counts = defaultdict(int)
    for info in infos:
        if not isinstance(info, dict):
            continue
        squad = info.get('externalSquadUuid')
        if not squad:
            squads = info.get('activeInternalSquads') or []
            if isinstance(squads, list) and squads:
                first = squads[0]
                if isinstance(first, dict):
                    squad = first.get('uuid') or first.get('externalSquadUuid')
                else:
                    squad = str(first)
        counts[squad or '未分组'] += 1
    top = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    lines = [f"样本用户数: {len(uuids)}"]
    for sid, cnt in top[:5]:
        lines.append(f"- `{sid}`：{cnt}")
    suggestion = None
    if len(top) >= 2 and top[0][1] - top[-1][1] >= max(5, len(uuids) // 5):
        suggestion = {'from': top[0][0], 'to': top[-1][0], 'count': min(10, (top[0][1]-top[-1][1])//2)}
        lines.append(f"\n建议迁移：从 `{suggestion['from']}` 向 `{suggestion['to']}` 迁移约 {suggestion['count']} 人")
    return "\n".join(lines), suggestion


async def build_top_users_traffic(max_users=50):
    rows = db_query("SELECT tg_id, uuid FROM subscriptions ORDER BY id DESC LIMIT ?", (max_users,))
    if not rows:
        return []
    pairs = [(dict(r)['tg_id'], dict(r)['uuid']) for r in rows]
    infos = await asyncio.gather(*[get_panel_user(u) for _, u in pairs])
    data = []
    for (tg_id, uid), info in zip(pairs, infos):
        if not isinstance(info, dict):
            continue
        used = int((info.get('userTraffic') or {}).get('usedTrafficBytes', 0) or 0)
        data.append((tg_id, uid, used))
    return sorted(data, key=lambda x: x[2], reverse=True)[:5]


def detect_bandwidth_volatility(nodes_rt):
    prev = get_json_setting('bandwidth_last_nodes', {})
    if not isinstance(prev, dict):
        prev = {}
    alerts = []
    curr = {}
    for it in nodes_rt:
        name = it.get('name') or it.get('nodeName') or '未知节点'
        val = int(it.get('totalTrafficBytes') or it.get('trafficBytes') or 0)
        curr[name] = val
        old = int(prev.get(name, 0) or 0)
        if old > 0:
            delta = val - old
            ratio = abs(delta) / old
            if abs(delta) >= 1024**3 and ratio >= 0.5:
                alerts.append((name, delta, ratio))
    set_json_setting('bandwidth_last_nodes', curr)
    return alerts


async def send_or_edit_menu(update, context, text, reply_markup):
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode='Markdown')
        except Exception:
            try: await update.callback_query.delete_message()
            except Exception as exc:
                logger.debug("delete callback message failed: %s", exc)
            await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup, parse_mode='Markdown')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    user_id = update.effective_user.id
    if user_id == ADMIN_ID:
        try:
            val_notify = db_query("SELECT value FROM settings WHERE key='notify_days'", one=True)
            notify_days = int(val_notify['value']) if val_notify else 3
            val_cleanup = db_query("SELECT value FROM settings WHERE key='cleanup_days'", one=True)
            cleanup_days = int(val_cleanup['value']) if val_cleanup else 7
        except Exception as exc:
            logger.warning("failed to load admin settings, using defaults: %s", exc)
            notify_days = 3
            cleanup_days = 7
        try:
            pending_cnt = db_query("SELECT COUNT(*) AS c FROM orders WHERE status='pending'", one=True)['c']
            failed_cnt = db_query("SELECT COUNT(*) AS c FROM orders WHERE status='failed'", one=True)['c']
            today_ts = int(datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            today_cnt = db_query("SELECT COUNT(*) AS c FROM orders WHERE created_at>=?", (today_ts,), one=True)['c']
        except Exception:
            pending_cnt = failed_cnt = today_cnt = 0
        msg_text = (
            f"👮‍♂️ **管理员控制台**\n"
            f"🔔 提醒设置：提前 {notify_days} 天\n"
            f"🗑 清理设置：过期 {cleanup_days} 天\n"
            f"📊 今日订单：{today_cnt} | 待审核：{pending_cnt} | 失败：{failed_cnt}"
        )
        keyboard = [
            [InlineKeyboardButton("📦 套餐管理", callback_data="admin_plans_list")],
            [InlineKeyboardButton("👥 用户列表", callback_data="admin_users_list")],
            [InlineKeyboardButton("🔔 提醒设置", callback_data="admin_notify"), InlineKeyboardButton("🗑 清理设置", callback_data="admin_cleanup")],
            [InlineKeyboardButton("🛡️ 异常设置", callback_data="admin_anomaly_menu")],
            [InlineKeyboardButton("📚 批量操作", callback_data="admin_bulk_menu")],
            [InlineKeyboardButton("🧾 订单审计", callback_data="admin_orders_menu"), InlineKeyboardButton("🧾 风控回溯", callback_data="admin_risk_audit")],
            [InlineKeyboardButton("⚙️ 订阅设置", callback_data="admin_subscription_settings"), InlineKeyboardButton("🧩 用户分组", callback_data="admin_squads_menu")],
            [InlineKeyboardButton("📈 带宽看板", callback_data="admin_bandwidth_dashboard"), InlineKeyboardButton("🛡️ 风控策略", callback_data="admin_risk_policy")],
            [InlineKeyboardButton("🕒 操作时间线", callback_data="admin_ops_timeline"), InlineKeyboardButton("📢 群发通知", callback_data="admin_broadcast_start")],
            [InlineKeyboardButton("💳 收款设置", callback_data="admin_pay_settings"), InlineKeyboardButton("🔌 面板配置", callback_data="admin_panel_config")]
        ]
    else:
        msg_text = "👋 **欢迎使用自助服务！**\n请选择操作："
        keyboard = [
            [InlineKeyboardButton("🛒 购买新订阅", callback_data="client_buy_new")],
            [InlineKeyboardButton("🔍 我的订阅 / 续费", callback_data="client_status")],
            [InlineKeyboardButton("🌍 节点状态", callback_data="client_nodes"), InlineKeyboardButton("🆘 联系客服", callback_data="contact_support")]
        ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await send_or_edit_menu(update, context, msg_text, reply_markup)

async def client_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not check_cooldown(query.from_user.id):
        await query.answer("⏳ 操作太快了...", show_alert=False)
        return
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "back_home":
        await start(update, context)
        return

    if data == "client_nodes":
        try: await query.edit_message_text("🔄 正在获取节点状态...")
        except Exception as exc:
            logger.debug("node status loading hint message failed: %s", exc)
        nodes = await get_nodes_status()
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
        kb = [[InlineKeyboardButton("🔄 刷新", callback_data="client_nodes")], [InlineKeyboardButton("🔙 返回", callback_data="back_home")]]
        await send_or_edit_menu(update, context, "\n".join(msg_list), InlineKeyboardMarkup(kb))
        return

    if data == "contact_support":
        context.user_data['chat_mode'] = 'support'
        msg = "📞 **客服模式已开启**\n请直接发送文字、图片或文件。\n🚪 结束咨询请点击下方按钮。"
        keyboard = [[InlineKeyboardButton("🚪 结束咨询", callback_data="back_home")]]
        await send_or_edit_menu(update, context, msg, InlineKeyboardMarkup(keyboard))
        return

    if data == "client_buy_new":
        keyboard = []
        plans = db_query("SELECT * FROM plans")
        for p in plans:
            p_dict = dict(p) 
            strategy = p_dict.get('reset_strategy', 'NO_RESET')
            strategy_label = get_strategy_label(strategy)
            btn_text = f"{p_dict['name']} | {p_dict['price']} | {p_dict['gb']}G ({strategy_label})"
            action = f"order_{p_dict['key']}_new_0"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=action)])
        keyboard.append([InlineKeyboardButton("🔙 返回主菜单", callback_data="back_home")])
        await send_or_edit_menu(update, context, "🛒 **请选择新购套餐：**", InlineKeyboardMarkup(keyboard))

    elif data == "client_status":
        subs = db_query("SELECT * FROM subscriptions WHERE tg_id = ?", (user_id,))
        if not subs:
            await send_or_edit_menu(update, context, "❌ 您名下没有订阅。\n请点击“购买新订阅”。", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="back_home")]]))
            return
        try: await query.edit_message_text("🔄 正在加载订阅列表...")
        except Exception as exc:
            logger.debug("failed to delete view_sub message: %s", exc)
        tasks = [get_panel_user(sub['uuid']) for sub in subs]
        results = await asyncio.gather(*tasks)
        keyboard = []
        valid_count = 0
        for i, info in enumerate(results):
            sub_db = subs[i]
            if not info: continue
            valid_count += 1
            limit = info.get('trafficLimitBytes', 0)
            used = info.get('userTraffic', {}).get('usedTrafficBytes', 0)
            remain_gb = round((limit - used) / (1024**3), 1)
            sid = get_short_id(sub_db['uuid'])
            btn_text = f"📦 订阅 #{valid_count} | 剩余 {remain_gb} GB"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"view_sub_{sid}")])
        if valid_count == 0:
             await send_or_edit_menu(update, context, "⚠️ 您的所有订阅似乎都已失效。", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="back_home")]]))
             return
        keyboard.append([InlineKeyboardButton("🔙 返回主菜单", callback_data="back_home")])
        await send_or_edit_menu(update, context, "👤 **我的订阅列表**\n请点击下方按钮查看详情：", InlineKeyboardMarkup(keyboard))

    elif data.startswith("view_sub_"):
        short_id = data.split("_")[2]
        target_uuid = get_real_uuid(short_id)
        if not target_uuid:
            await query.answer("❌ 按钮已过期")
            return
        await query.answer("🔄 加载详情中...")
        try: await query.delete_message()
        except Exception as exc:
            logger.debug("delete stale sub detail message failed: %s", exc)
        info = await get_panel_user(target_uuid)
        if not info:
            await context.bot.send_message(user_id, "⚠️ 此订阅已被删除。", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回列表", callback_data="client_status")]]))
            return
        expire_show = format_time(info.get('expireAt'))
        limit = info.get('trafficLimitBytes', 0)
        used = info.get('userTraffic', {}).get('usedTrafficBytes', 0)
        limit_gb = round(limit / (1024**3), 2)
        remain_gb = round((limit - used) / (1024**3), 2)
        sub_url = info.get('subscriptionUrl', '无链接')
        progress = draw_progress_bar(used, limit)
        strategy = info.get('trafficLimitStrategy', 'NO_RESET')
        strategy_label = get_strategy_label(strategy)
        caption = (f"📃 **订阅详情**\n\n📊 流量：`{progress}`\n🔋 剩余：`{remain_gb} GB` / `{limit_gb} GB ({strategy_label})`\n⏳ 到期：`{expire_show}`\n🔗 订阅链接：\n`{sub_url}`")
        sid = get_short_id(target_uuid)
        keyboard = [[InlineKeyboardButton(f"💳 续费此订阅", callback_data=f"selrenew_{sid}")], [InlineKeyboardButton("🔙 返回列表", callback_data="client_status")]]
        if sub_url and sub_url.startswith('http'):
            qr_bio = generate_qr(sub_url)
            await context.bot.send_photo(chat_id=user_id, photo=qr_bio, caption=caption, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await context.bot.send_message(chat_id=user_id, text=caption, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("selrenew_"):
        short_id = data.split("_")[1]
        target_uuid = get_real_uuid(short_id)
        if not target_uuid:
            await query.answer("❌ 信息过期")
            return
        
        sub_record = db_query("SELECT * FROM subscriptions WHERE uuid = ?", (target_uuid,), one=True)
        original_plan_key = None
        if sub_record:
            sub_dict = dict(sub_record)
            original_plan_key = sub_dict.get('plan_key')
        
        if original_plan_key:
            plan = db_query("SELECT * FROM plans WHERE key = ?", (original_plan_key,), one=True)
            if plan:
                await show_payment_method_menu(update, context, original_plan_key, 'renew', short_id)
                return

        keyboard = []
        plans = db_query("SELECT * FROM plans")
        for p in plans:
            p_dict = dict(p)
            strategy = p_dict.get('reset_strategy', 'NO_RESET')
            strategy_label = get_strategy_label(strategy)
            btn_text = f"{p_dict['name']} | {p_dict['price']} | {p_dict['gb']}G ({strategy_label})"
            action = f"order_{p_dict['key']}_renew_{short_id}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=action)])
        keyboard.append([InlineKeyboardButton("🔙 返回列表", callback_data="client_status")])
        await send_or_edit_menu(update, context, "🔄 **请选择要续费的时长：**\n(流量和时间将自动叠加)", InlineKeyboardMarkup(keyboard))

    elif data.startswith("order_"):
        parts = data.split("_")
        plan_key = parts[1]
        order_type = parts[2]
        if order_type == 'renew':
            short_id = parts[3]
        else:
            short_id = "0"
        
        await show_payment_method_menu(update, context, plan_key, order_type, short_id)

    elif data.startswith("paymethod_"):
        # paymethod_{alipay|wechat}_{plan_key}_{order_type}_{short_id}
        parts = data.split("_", 4)
        if len(parts) < 5:
            await query.answer("参数错误")
            return
        _, pay_method, plan_key, order_type, short_id = parts
        await handle_order_confirmation(update, context, plan_key, order_type, short_id, payment_method=pay_method)

    elif data == "cancel_order":
        pending = get_pending_order_for_user(db_query, user_id)
        if pending:
            update_order_status(db_execute, pending['order_id'], [STATUS_PENDING], STATUS_REJECTED, error_message='cancelled_by_user')
        await start(update, context)

async def show_payment_method_menu(update, context, plan_key, order_type, short_id):
    type_str = "续费" if order_type == 'renew' else "新购"
    msg = f"💳 **选择支付方式（{type_str}）**\n请选择收款方式："
    kb = [
        [InlineKeyboardButton("🟦 支付宝", callback_data=f"paymethod_alipay_{plan_key}_{order_type}_{short_id}")],
        [InlineKeyboardButton("🟩 微信支付", callback_data=f"paymethod_wechat_{plan_key}_{order_type}_{short_id}")],
        [InlineKeyboardButton("🔙 返回", callback_data="back_home")],
    ]
    await send_or_edit_menu(update, context, msg, InlineKeyboardMarkup(kb))


async def handle_order_confirmation(update, context, plan_key, order_type, short_id, payment_method='alipay'):
    user_id = update.effective_user.id
    target_uuid = get_real_uuid(short_id) if short_id != "0" else "0"

    plan = db_query("SELECT * FROM plans WHERE key = ?", (plan_key,), one=True)
    if not plan:
        return

    plan_dict = dict(plan)
    strategy = plan_dict.get('reset_strategy', 'NO_RESET')
    strategy_label = get_strategy_label(strategy)

    msg_id = None
    if update.callback_query and update.callback_query.message:
        msg_id = update.callback_query.message.message_id

    order, created = create_order(db_query, db_execute, user_id, plan_key, order_type, target_uuid, menu_message_id=msg_id)
    if created:
        append_order_audit_log(db_execute, order['order_id'], 'create', user_id, f'type={order_type};plan={plan_key}')

    type_str = "续费" if order_type == 'renew' else "新购"
    back_data = f"view_sub_{short_id}" if order_type == 'renew' else "client_buy_new"

    keyboard = [[InlineKeyboardButton("❌ 取消订单", callback_data="cancel_order")], [InlineKeyboardButton("🔙 返回", callback_data=back_data)]]
    msg = (
        f"📝 **订单确认 ({type_str})**\n"
        f"📦 套餐：{plan_dict['name']}\n"
        f"💰 金额：**{plan_dict['price']}**\n"
        f"📡 流量：**{plan_dict['gb']} GB ({strategy_label})**\n\n"
        "💳 **下一步：**\n请在此直接发送 **支付宝口令红包** (文字) 给机器人。\n👇 👇 👇"
    )
    if not created:
        msg = "⚠️ 你已有一个待审核订单，请先等待管理员处理，或取消后重新下单。"
    await send_or_edit_menu(update, context, msg, InlineKeyboardMarkup(keyboard))
    if created and qr_file_id:
        try:
            await context.bot.send_photo(chat_id=user_id, photo=qr_file_id, caption=f"📌 当前收款码：{method_label}")
        except Exception as exc:
            logger.warning("发送收款码失败: %s", exc)

async def show_plans_menu(update, context):
    plans = db_query("SELECT * FROM plans")
    keyboard = []
    for p in plans:
        p_dict = dict(p)
        btn_text = f"{p_dict['name']} | {p_dict['price']} | {p_dict['gb']}G"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"plan_detail_{p_dict['key']}")])
    keyboard.append([InlineKeyboardButton("➕ 添加新套餐", callback_data="add_plan_start")])
    keyboard.append([InlineKeyboardButton("🔙 返回", callback_data="back_home")])
    await send_or_edit_menu(update, context, "📦 **套餐管理**\n点击套餐查看详情或删除。", InlineKeyboardMarkup(keyboard))

async def reschedule_anomaly_job(application, interval_hours):
    try:
        current_jobs = application.job_queue.get_jobs_by_name('check_anomalies_job')
        for job in current_jobs:
            job.schedule_removal()
        interval_seconds = float(interval_hours) * 3600
        application.job_queue.run_repeating(check_anomalies_job, interval=interval_seconds, first=10, name='check_anomalies_job')
    except Exception as e:
        logger.error(f"Reschedule failed: {e}")



async def show_orders_menu(update, context, status_filter=None, page=0):
    page = max(int(page or 0), 0)
    page_size = 20
    offset = page * page_size

    if status_filter:
        rows = db_query("SELECT * FROM orders WHERE status = ? ORDER BY created_at DESC LIMIT ? OFFSET ?", (status_filter, page_size, offset))
        total_row = db_query("SELECT COUNT(*) AS c FROM orders WHERE status=?", (status_filter,), one=True)
        total = int(total_row['c']) if total_row else 0
        title = f"🧾 **订单审计 - {order_status_label(status_filter)}**"
    else:
        rows = db_query("SELECT * FROM orders ORDER BY created_at DESC LIMIT ? OFFSET ?", (page_size, offset))
        total_row = db_query("SELECT COUNT(*) AS c FROM orders", one=True)
        total = int(total_row['c']) if total_row else 0
        title = "🧾 **订单审计 - 最近订单**"

    total_pages = max((total + page_size - 1) // page_size, 1)
    current_page = min(page + 1, total_pages)
    title += f"\n📄 第 {current_page}/{total_pages} 页"

    keyboard = []
    for row in rows:
        item = dict(row)
        keyboard.append([
            InlineKeyboardButton(
                format_order_row(item),
                callback_data=f"admin_order_{item['order_id']}",
            )
        ])

    keyboard.append([
        InlineKeyboardButton("🟡 待审核", callback_data="admin_orders_status_pending"),
        InlineKeyboardButton("🟠 处理中", callback_data="admin_orders_status_approved"),
    ])
    keyboard.append([
        InlineKeyboardButton("✅ 已发货", callback_data="admin_orders_status_delivered"),
        InlineKeyboardButton("⛔ 已拒绝", callback_data="admin_orders_status_rejected"),
    ])
    keyboard.append([
        InlineKeyboardButton("❌ 失败", callback_data="admin_orders_status_failed"),
        InlineKeyboardButton("📋 全部", callback_data="admin_orders_menu"),
    ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"admin_orders_page_{status_filter or 'all'}_{page-1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("➡️ 下一页", callback_data=f"admin_orders_page_{status_filter or 'all'}_{page+1}"))
    if nav:
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton("🔙 返回", callback_data="back_home")])
    await send_or_edit_menu(update, context, title, InlineKeyboardMarkup(keyboard))


async def show_anomaly_whitelist_menu(update, context):
    rows = db_query("SELECT * FROM anomaly_whitelist ORDER BY created_at DESC LIMIT 20")
    keyboard = [[InlineKeyboardButton("➕ 添加UUID", callback_data="anomaly_whitelist_add")]]
    for row in rows:
        item = dict(row)
        short = item['user_uuid'][:10]
        keyboard.append([InlineKeyboardButton(f"❌ 删除 {short}...", callback_data=f"anomaly_whitelist_del_{item['user_uuid']}")])
    keyboard.append([InlineKeyboardButton("🔙 返回", callback_data="admin_anomaly_menu")])
    await send_or_edit_menu(update, context, "📋 **异常检测白名单**", InlineKeyboardMarkup(keyboard))

async def admin_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "back_home":
        await start(update, context)
        return

    if data.startswith("reply_user_"):
        target_uid = int(data.split("_")[2])
        context.user_data['reply_to_uid'] = target_uid
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ 取消回复", callback_data="cancel_op")]])
        await query.message.reply_text(f"✍️ 请输入回复给用户 `{target_uid}` 的内容 (文字/图片)：", parse_mode='Markdown', reply_markup=cancel_kb)
        return
    if data == "cancel_op":
        context.user_data.clear()
        await start(update, context)
        return
    if data == "admin_panel_config":
        masked = PANEL_TOKEN[:6] + "***" if PANEL_TOKEN else "未配置"
        msg = (
            "🔌 **面板对接配置**\n"
            f"面板地址: `{PANEL_URL or '未配置'}`\n"
            f"面板Token: `{masked}`\n"
            f"订阅域名: `{SUB_DOMAIN or '未配置'}`\n"
            f"默认组UUID: `{TARGET_GROUP_UUID or '未配置'}`\n"
            f"TLS校验: `{PANEL_VERIFY_TLS}`\n\n"
            "首次安装只需机器人信息，面板参数可在这里随时修改。"
        )
        kb = [
            [InlineKeyboardButton("🌐 设置面板地址", callback_data="panelcfg_set_url")],
            [InlineKeyboardButton("🔑 设置面板Token", callback_data="panelcfg_set_token")],
            [InlineKeyboardButton("🔗 设置订阅域名", callback_data="panelcfg_set_subdomain")],
            [InlineKeyboardButton("🧩 设置默认组UUID", callback_data="panelcfg_set_group")],
            [InlineKeyboardButton("🔒 切换TLS校验", callback_data="panelcfg_toggle_tls")],
            [InlineKeyboardButton("🔙 返回", callback_data="back_home")],
        ]
        await send_or_edit_menu(update, context, msg, InlineKeyboardMarkup(kb))
        return
    if data in {"panelcfg_set_url", "panelcfg_set_token", "panelcfg_set_subdomain", "panelcfg_set_group"}:
        mode_map = {
            "panelcfg_set_url": ("panelcfg_input_url", "请输入面板地址（例如 https://panel.com ）"),
            "panelcfg_set_token": ("panelcfg_input_token", "请输入面板 API Token"),
            "panelcfg_set_subdomain": ("panelcfg_input_subdomain", "请输入订阅域名（例如 https://sub.com ）"),
            "panelcfg_set_group": ("panelcfg_input_group", "请输入默认用户组 UUID"),
        }
        key, tip = mode_map[data]
        context.user_data[key] = True
        await send_or_edit_menu(update, context, f"✍️ {tip}", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 取消", callback_data="admin_panel_config")]]))
        return
    if data == "panelcfg_toggle_tls":
        new_val = not PANEL_VERIFY_TLS
        save_runtime_config(panel_verify_tls=new_val)
        append_ops_timeline('配置', '切换TLS校验', f'panel_verify_tls={new_val}', actor=query.from_user.id)
        await query.answer(f"已切换为 {new_val}", show_alert=True)
        await send_or_edit_menu(update, context, "✅ TLS 配置已更新。", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_panel_config")]]))
        return
    if data == "admin_pay_settings":
        ali = '已配置' if get_setting_value('alipay_qr_file_id') else '未配置'
        wx = '已配置' if get_setting_value('wechat_qr_file_id') else '未配置'
        msg = (
            "💳 **收款设置**\n"
            f"🟦 支付宝收款码：{ali}\n"
            f"🟩 微信收款码：{wx}\n\n"
            "点击按钮后发送一张收款图片即可更新。"
        )
        kb = [
            [InlineKeyboardButton("上传支付宝收款码", callback_data="set_payimg_alipay")],
            [InlineKeyboardButton("上传微信收款码", callback_data="set_payimg_wechat")],
            [InlineKeyboardButton("🔙 返回", callback_data="back_home")],
        ]
        await send_or_edit_menu(update, context, msg, InlineKeyboardMarkup(kb))
        return
    if data in {"set_payimg_alipay", "set_payimg_wechat"}:
        context.user_data['set_payimg'] = 'alipay' if data.endswith('alipay') else 'wechat'
        await send_or_edit_menu(update, context, "📷 请发送收款二维码图片（可发送照片或图片文件）", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 取消", callback_data="admin_pay_settings")]]))
        return
    if data == "admin_broadcast_start":
        context.user_data['broadcast_mode'] = True
        await send_or_edit_menu(update, context, "📢 **群发通知模式**\n请发送要广播的内容（文字/图片/文件）。\n发送后将自动群发给所有用户。", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 取消", callback_data="cancel_op")]]))
        return
    if data == "admin_subscription_settings":
        settings_payload = await get_subscription_settings()
        preview = json.dumps(settings_payload, ensure_ascii=False, indent=2)[:1200] if settings_payload else '{}'
        history = get_json_setting('subscription_settings_history', [])
        latest_ts = history[-1]['ts'] if isinstance(history, list) and history else None
        latest_text = datetime.datetime.fromtimestamp(latest_ts).strftime('%m-%d %H:%M') if latest_ts else '暂无'
        msg = (
            "⚙️ **订阅设置（可视化）**\n"
            "当前配置（截断显示）：\n"
            "```json\n"
            f"{preview}\n"
            "```\n\n"
            f"最近回滚点：`{latest_text}`\n"
            "可使用模板快速应用，或直接发送 JSON 更新。"
        )
        kb = [
            [InlineKeyboardButton("✍️ 修改订阅设置(JSON)", callback_data="admin_subscription_settings_edit")],
            [InlineKeyboardButton("🧩 应用安全模板", callback_data="admin_subsettings_tpl_safe"), InlineKeyboardButton("🧩 应用兼容模板", callback_data="admin_subsettings_tpl_compat")],
            [InlineKeyboardButton("💾 保存回滚点", callback_data="admin_subsettings_snapshot"), InlineKeyboardButton("↩️ 回滚最近一次", callback_data="admin_subsettings_rollback")],
            [InlineKeyboardButton("🔙 返回", callback_data="back_home")],
        ]
        await send_or_edit_menu(update, context, msg, InlineKeyboardMarkup(kb))
        return
    if data == "admin_subsettings_snapshot":
        payload = await get_subscription_settings()
        push_subscription_settings_snapshot(payload, source='手动保存')
        append_ops_timeline('配置', '订阅设置保存回滚点', '管理员保存当前订阅设置快照', actor=query.from_user.id)
        await query.answer("✅ 已保存回滚点", show_alert=True)
        await send_or_edit_menu(update, context, "✅ 已保存当前订阅设置为回滚点。", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_subscription_settings")]]))
        return
    if data in {"admin_subsettings_tpl_safe", "admin_subsettings_tpl_compat"}:
        current = await get_subscription_settings()
        push_subscription_settings_snapshot(current, source='模板应用前自动备份')
        payload = {'allowInsecure': False} if data.endswith('safe') else {'allowInsecure': True}
        resp = await patch_subscription_settings(payload)
        if resp and resp.status_code in (200, 204):
            tpl = '安全模板' if data.endswith('safe') else '兼容模板'
            append_ops_timeline('配置', f'应用{tpl}', f'payload={json.dumps(payload, ensure_ascii=False)}', actor=query.from_user.id)
            await query.answer("✅ 模板应用成功", show_alert=True)
            await send_or_edit_menu(update, context, f"✅ 已应用{tpl}。", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_subscription_settings")]]))
        else:
            await query.answer("❌ 模板应用失败", show_alert=True)
        return
    if data == "admin_subsettings_rollback":
        snap = pop_subscription_settings_snapshot()
        if not snap:
            await query.answer("⚠️ 暂无可回滚快照", show_alert=True)
            return
        payload = snap.get('payload') or {}
        resp = await patch_subscription_settings(payload)
        if resp and resp.status_code in (200, 204):
            append_ops_timeline('配置', '订阅设置回滚', f"来源={snap.get('source', '-')}", actor=query.from_user.id)
            await query.answer("✅ 回滚成功", show_alert=True)
            await send_or_edit_menu(update, context, "✅ 已按最近回滚点恢复设置。", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_subscription_settings")]]))
        else:
            await query.answer("❌ 回滚失败", show_alert=True)
        return
    if data == "admin_subscription_settings_edit":
        context.user_data['edit_subscription_settings'] = True
        await send_or_edit_menu(update, context, "✍️ 请发送要 PATCH 的 JSON 内容（例如 {\"allowInsecure\":false}）", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 取消", callback_data="cancel_op")]]))
        return
    if data == "admin_squads_menu":
        squads = await get_internal_squads()
        summary, suggestion = await build_squad_capacity_summary()
        kb = []
        for s in squads[:20]:
            suuid = s.get('uuid') or ''
            sname = s.get('name') or suuid[:8]
            kb.append([InlineKeyboardButton(f"🧩 {sname}", callback_data=f"admin_squad_{suuid}")])
        if suggestion and suggestion['from'] != '未分组' and suggestion['to'] != '未分组':
            kb.append([InlineKeyboardButton("🚚 一键迁移建议", callback_data=f"admin_squad_suggest_{suggestion['from']}__{suggestion['to']}__{suggestion['count']}")])
        kb.append([InlineKeyboardButton("🚚 批量迁移到分组", callback_data="admin_squad_bulk_move")])
        kb.append([InlineKeyboardButton("🔙 返回", callback_data="back_home")])
        await send_or_edit_menu(update, context, f"🧩 **用户分组（内部组）**\n{summary}", InlineKeyboardMarkup(kb))
        return
    if data.startswith("admin_squad_suggest_"):
        parts = data.replace("admin_squad_suggest_", "").split("__")
        if len(parts) != 3:
            await query.answer("建议参数错误", show_alert=True)
            return
        from_squad, to_squad, cnt_text = parts
        try:
            move_n = max(1, min(int(cnt_text), 20))
        except ValueError:
            move_n = 5
        rows = db_query("SELECT uuid FROM subscriptions ORDER BY id DESC LIMIT 120")
        pool = [dict(r)['uuid'] for r in rows]
        infos = await asyncio.gather(*[get_panel_user(u) for u in pool])
        candidates = []
        for uid, info in zip(pool, infos):
            if not isinstance(info, dict):
                continue
            squad = info.get('externalSquadUuid')
            if squad == from_squad:
                candidates.append(uid)
            if len(candidates) >= move_n:
                break
        if not candidates:
            await query.answer("暂无可迁移候选用户", show_alert=True)
            return
        resp = await bulk_move_users_to_squad(candidates, to_squad)
        if resp and resp.status_code in (200, 201, 204):
            append_ops_timeline('分组', '执行迁移建议', f'from={from_squad},to={to_squad},count={len(candidates)}', actor=query.from_user.id)
            await query.answer(f"✅ 已迁移 {len(candidates)} 人", show_alert=True)
        else:
            await query.answer("❌ 迁移失败", show_alert=True)
        return
    if data == "admin_squad_bulk_move":
        context.user_data['squad_bulk_move'] = True
        await send_or_edit_menu(update, context, "✍️ 请按以下格式发送：\n第一行：目标分组UUID\n后续行：用户UUID列表", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 取消", callback_data="admin_squads_menu")]]))
        return
    if data.startswith("admin_squad_"):
        squad_uuid = data.replace("admin_squad_", "")
        nodes = await get_internal_squad_accessible_nodes(squad_uuid)
        lines = ["🧩 **分组详情**", f"UUID: `{squad_uuid}`", "", "可访问节点："]
        if not nodes:
            lines.append("- 暂无")
        else:
            for n in nodes[:20]:
                lines.append(f"- {n.get('name', '未知节点')}")
        kb = [[InlineKeyboardButton("🔙 返回分组", callback_data="admin_squads_menu")]]
        await send_or_edit_menu(update, context, "\n".join(lines), InlineKeyboardMarkup(kb))
        return
    if data == "admin_bandwidth_dashboard":
        nodes_rt = await get_bandwidth_nodes_realtime()
        top = []
        for it in nodes_rt[:5]:
            name = it.get('name') or it.get('nodeName') or '未知节点'
            val = it.get('totalTrafficBytes') or it.get('trafficBytes') or 0
            top.append((name, int(val) if isinstance(val, (int, float)) else 0))
        top.sort(key=lambda x: x[1], reverse=True)
        lines = ["📈 **带宽看板（实时）**", "TOP节点："]
        if not top:
            lines.append("- 暂无数据")
        for name, val in top:
            lines.append(f"- {name}: {round(val / 1024**3, 2)} GB")
        top_users = await build_top_users_traffic()
        lines.append("\nTOP用户流量：")
        if not top_users:
            lines.append("- 暂无")
        for tg_id, uid, used in top_users:
            lines.append(f"- 用户`{tg_id}` / `{uid[:8]}`: {round(used / 1024**3, 2)} GB")
        alerts = detect_bandwidth_volatility(nodes_rt)
        lines.append("\n节点波动提醒：")
        if not alerts:
            lines.append("- 暂无明显波动")
        else:
            for name, delta, ratio in alerts[:5]:
                symbol = '⬆️' if delta > 0 else '⬇️'
                lines.append(f"- {symbol} {name}: {round(delta / 1024**3, 2)} GB ({round(ratio*100, 1)}%)")
        stats = await get_subscription_history_stats()
        hourly = stats.get('hourlyRequestStats') if isinstance(stats, dict) else []
        recent = int(hourly[-1].get('requestCount', 0)) if hourly else 0
        lines.append(f"\n最近1小时请求数：`{recent}`")
        await send_or_edit_menu(update, context, "\n".join(lines), InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="back_home")]]))
        return
    if data == "admin_risk_policy":
        low = get_setting_value('risk_low_score', '80')
        high = get_setting_value('risk_high_score', '130')
        unfreeze_hours = get_setting_value('risk_auto_unfreeze_hours', '12')
        watchlist = sorted(list(get_risk_watchlist()))[:8]
        watch_preview = '、'.join(x[:8] for x in watchlist) if watchlist else '暂无'
        msg = (
            "🛡️ **风控策略（多级）**\n"
            f"低风险阈值: {low}\n"
            f"高风险阈值: {high}\n"
            f"自动解封时长(小时): {unfreeze_hours}\n"
            f"观察名单(预览): {watch_preview}\n\n"
            "请通过下方按钮进入修改流程。"
        )
        kb = [
            [InlineKeyboardButton("✍️ 修改阈值", callback_data="admin_risk_policy_edit")],
            [InlineKeyboardButton("⏱ 设置自动解封时长", callback_data="admin_risk_unfreeze_edit")],
            [InlineKeyboardButton("👀 查看观察名单", callback_data="admin_risk_watchlist")],
            [InlineKeyboardButton("🔙 返回", callback_data="back_home")],
        ]
        await send_or_edit_menu(update, context, msg, InlineKeyboardMarkup(kb))
        return
    if data == "admin_risk_policy_edit":
        context.user_data['edit_risk_policy'] = True
        await send_or_edit_menu(update, context, "✍️ 请发送：低阈值,高阈值（例如 80,130）", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 取消", callback_data="admin_risk_policy")]]))
        return
    if data == "admin_risk_unfreeze_edit":
        context.user_data['edit_risk_unfreeze_hours'] = True
        await send_or_edit_menu(update, context, "⏱ 请输入自动解封时长（小时，整数，例如 12）", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 取消", callback_data="admin_risk_policy")]]))
        return
    if data == "admin_risk_watchlist":
        watchlist = sorted(list(get_risk_watchlist()))
        lines = ["👀 **观察名单**"]
        if not watchlist:
            lines.append("暂无记录")
        else:
            for uid in watchlist[:30]:
                lines.append(f"- `{uid}`")
        kb = [[InlineKeyboardButton("🧹 清空观察名单", callback_data="admin_risk_watchlist_clear")], [InlineKeyboardButton("🔙 返回", callback_data="admin_risk_policy")]]
        await send_or_edit_menu(update, context, "\n".join(lines), InlineKeyboardMarkup(kb))
        return
    if data == "admin_risk_watchlist_clear":
        set_risk_watchlist(set())
        append_ops_timeline('风控', '清空观察名单', '管理员手动清空', actor=query.from_user.id)
        await query.answer("✅ 已清空", show_alert=True)
        await send_or_edit_menu(update, context, "✅ 观察名单已清空。", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_risk_policy")]]))
        return
    if data == "admin_risk_audit":
        rows = db_query("SELECT * FROM anomaly_events ORDER BY created_at DESC LIMIT 20")
        lines = ["🧾 **风控回溯（最近20条）**"]
        if not rows:
            lines.append("暂无记录")
        for r in rows:
            it = dict(r)
            ts = datetime.datetime.fromtimestamp(int(it['created_at'])).strftime('%m-%d %H:%M')
            lines.append(f"- {ts} | {it['risk_level']} | {it['user_uuid'][:8]} | 分数{it['risk_score']} | 动作:{it['action_taken']}")
        await send_or_edit_menu(update, context, "\n".join(lines), InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="back_home")]]))
        return
    if data == "admin_ops_timeline":
        lines = ["🕒 **操作时间线（订单+风控+配置）**"]
        events = []
        order_logs = db_query("SELECT order_id, action, actor_id, detail, created_at FROM order_audit_logs ORDER BY created_at DESC LIMIT 15")
        for r in order_logs:
            it = dict(r)
            events.append((int(it['created_at']), f"订单 | {it['action']} | {it['order_id']} | {it.get('detail') or '-'}"))
        risk_logs = db_query("SELECT user_uuid, risk_level, risk_score, action_taken, created_at FROM anomaly_events ORDER BY created_at DESC LIMIT 15")
        for r in risk_logs:
            it = dict(r)
            events.append((int(it['created_at']), f"风控 | {it['risk_level']} | {it['user_uuid'][:8]} | {it['action_taken']}"))
        for item in get_json_setting('ops_timeline', [])[-20:]:
            events.append((int(item.get('ts', 0)), f"{item.get('type','系统')} | {item.get('title','-')} | {item.get('detail','-')}"))
        events.sort(key=lambda x: x[0], reverse=True)
        if not events:
            lines.append('暂无记录')
        for ts, text_line in events[:25]:
            ts_text = datetime.datetime.fromtimestamp(ts).strftime('%m-%d %H:%M') if ts else '--'
            lines.append(f"- {ts_text} | {text_line[:120]}")
        await send_or_edit_menu(update, context, "\n".join(lines), InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="back_home")]]))
        return
    if data == "admin_bulk_menu":
        msg = """📚 **批量用户操作**

请选择操作类型：
- 批量重置流量
- 批量禁用
- 批量删除
- 批量改到期日
- 批量改流量包"""
        kb = [
            [InlineKeyboardButton("🔄 批量重置流量", callback_data="bulk_reset")],
            [InlineKeyboardButton("⛔ 批量禁用", callback_data="bulk_disable")],
            [InlineKeyboardButton("🗑 批量删除", callback_data="bulk_delete")],
            [InlineKeyboardButton("📅 批量改到期日", callback_data="bulk_expire")],
            [InlineKeyboardButton("📡 批量改流量包", callback_data="bulk_traffic")],
            [InlineKeyboardButton("🔙 返回", callback_data="back_home")],
        ]
        await send_or_edit_menu(update, context, msg, InlineKeyboardMarkup(kb))
        return
    if data in {"bulk_reset", "bulk_disable", "bulk_delete"}:
        context.user_data['bulk_action'] = data.replace('bulk_', '')
        tip = "每行一个UUID，或使用空格/逗号分隔。"
        await send_or_edit_menu(update, context, f"✍️ 请输入用户UUID列表\n{tip}", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 取消", callback_data="admin_bulk_menu")]]))
        return
    if data == "bulk_expire":
        context.user_data['bulk_action'] = 'expire'
        tip = "第一行输入天数（例如 30），从第二行开始输入UUID列表。"
        await send_or_edit_menu(update, context, f"✍️ 批量改到期日\n{tip}", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 取消", callback_data="admin_bulk_menu")]]))
        return
    if data == "bulk_traffic":
        context.user_data['bulk_action'] = 'traffic'
        tip = "第一行输入流量GB（例如 200），从第二行开始输入UUID列表。"
        await send_or_edit_menu(update, context, f"✍️ 批量改流量包\n{tip}", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 取消", callback_data="admin_bulk_menu")]]))
        return
    if data == "admin_orders_menu":
        await show_orders_menu(update, context)
        return
    if data.startswith("admin_orders_status_"):
        status_filter = data.replace("admin_orders_status_", "")
        await show_orders_menu(update, context, status_filter=status_filter)
        return
    if data.startswith("admin_orders_page_"):
        _, _, _, status_raw, page_raw = data.split("_", 4)
        status_filter = None if status_raw == 'all' else status_raw
        try:
            page = int(page_raw)
        except ValueError:
            page = 0
        await show_orders_menu(update, context, status_filter=status_filter, page=page)
        return
    if data.startswith("admin_order_"):
        order_id = data.replace("admin_order_", "")
        order = db_query("SELECT * FROM orders WHERE order_id = ?", (order_id,), one=True)
        if not order:
            await send_or_edit_menu(update, context, "⚠️ 订单不存在", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_orders_menu")]]))
            return
        item = dict(order)
        logs = db_query("SELECT * FROM order_audit_logs WHERE order_id=? ORDER BY created_at DESC LIMIT 5", (item['order_id'],))
        txt = format_order_detail(item, [dict(x) for x in logs])
        kb = [[InlineKeyboardButton("🔙 返回", callback_data="admin_orders_menu")]]
        if item.get('status') == STATUS_FAILED:
            kb.insert(0, [InlineKeyboardButton("♻️ 重试发货", callback_data=f"rt_{item['order_id']}")])
        await send_or_edit_menu(update, context, txt, InlineKeyboardMarkup(kb))
        return
    if data == "anomaly_whitelist_menu":
        await show_anomaly_whitelist_menu(update, context)
        return
    if data == "anomaly_whitelist_add":
        context.user_data['add_anomaly_whitelist'] = True
        await send_or_edit_menu(update, context, "✍️ 请输入要加入白名单的用户 UUID", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 取消", callback_data="anomaly_whitelist_menu")]]))
        return
    if data.startswith("anomaly_whitelist_del_"):
        uuid_val = data.replace("anomaly_whitelist_del_", "")
        db_execute("DELETE FROM anomaly_whitelist WHERE user_uuid = ?", (uuid_val,))
        await show_anomaly_whitelist_menu(update, context)
        return
    if data.startswith("anomaly_quick_whitelist_"):
        uid = data.replace("anomaly_quick_whitelist_", "")
        db_execute("INSERT OR IGNORE INTO anomaly_whitelist (user_uuid, created_at) VALUES (?, ?)", (uid, int(time.time())))
        await query.answer("✅ 已加入白名单", show_alert=False)
        return
    if data.startswith("anomaly_quick_enable_"):
        uid = data.replace("anomaly_quick_enable_", "")
        await safe_api_request('POST', f"/users/{uid}/actions/enable")
        await query.answer("✅ 已尝试解封该用户", show_alert=False)
        return
    if data == "admin_plans_list":
        await show_plans_menu(update, context)
    elif data.startswith("plan_detail_"):
        key = data.split("_")[2]
        p = db_query("SELECT * FROM plans WHERE key = ?", (key,), one=True)
        if not p: return
        try:
            p_dict = dict(p)
            strategy = p_dict.get('reset_strategy', 'NO_RESET')
            s_text = get_strategy_label(strategy)
        except Exception as exc:
            logger.warning("failed to read plan strategy for %s: %s", key, exc)
            s_text = '总流量'
        msg = f"📦 **套餐详情**\n\n🏷 名称：`{p_dict['name']}`\n💰 价格：`{p_dict['price']}`\n⏳ 时长：`{p_dict['days']} 天`\n📡 流量：`{p_dict['gb']} GB`\n🔄 策略：`{s_text}`"
        keyboard = [[InlineKeyboardButton("🗑 删除此套餐", callback_data=f"del_plan_{key}")], [InlineKeyboardButton("🔙 返回列表", callback_data="admin_plans_list")]]
        await send_or_edit_menu(update, context, msg, InlineKeyboardMarkup(keyboard))
    elif data.startswith("del_plan_"):
        key = data.split("_")[2]
        db_execute("DELETE FROM plans WHERE key = ?", (key,))
        await query.answer("✅ 套餐已删除", show_alert=True)
        await show_plans_menu(update, context)
    elif data == "admin_users_list":
        users = db_query("SELECT DISTINCT tg_id, MAX(created_at) as created_at FROM subscriptions GROUP BY tg_id ORDER BY created_at DESC LIMIT 20")
        keyboard = []
        for u in users:
            u_dict = dict(u)
            ts = u_dict['created_at']
            date_str = datetime.datetime.fromtimestamp(int(ts)).strftime('%m-%d')
            btn_text = f"🆔 {u_dict['tg_id']} | {date_str}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"list_user_subs_{u_dict['tg_id']}")])
        keyboard.append([InlineKeyboardButton("🔙 返回", callback_data="back_home")])
        await send_or_edit_menu(update, context, "👥 **用户管理 (最近20名)**\n点击ID查看其名下订阅：", InlineKeyboardMarkup(keyboard))
        
    elif data.startswith("list_user_subs_"):
        target_uid = int(data.split("_")[3])
        subs = db_query("SELECT * FROM subscriptions WHERE tg_id = ?", (target_uid,))
        keyboard = []
        for s in subs:
            s_dict = dict(s)
            short_uuid = s_dict['uuid'][:8]
            keyboard.append([InlineKeyboardButton(f"UUID: {short_uuid}...", callback_data=f"manage_user_{s_dict['uuid']}")])
        keyboard.append([InlineKeyboardButton("🔙 返回列表", callback_data="admin_users_list")])
        await send_or_edit_menu(update, context, f"👤 用户 `{target_uid}` 的订阅列表：", InlineKeyboardMarkup(keyboard))

    elif data.startswith("manage_user_"):
        target_uuid = data.replace("manage_user_", "")
        sub = db_query("SELECT * FROM subscriptions WHERE uuid = ?", (target_uuid,), one=True)
        if not sub:
            await send_or_edit_menu(update, context, "⚠️ 记录不存在", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_users_list")]]))
            return
        panel_info = await get_panel_user(target_uuid)
        status = "🟢 面板正常" if panel_info else "🔴 面板已删"
        msg = (f"👤 **用户详情**\nTG ID: `{dict(sub)['tg_id']}`\n状态: {status}\nUUID: `{target_uuid}`")
        keyboard = [
            [InlineKeyboardButton("🔄 重置流量", callback_data=f"reset_traffic_{target_uuid}")],
            [InlineKeyboardButton("📜 最近请求记录", callback_data=f"user_reqhist_{target_uuid}")],
            [InlineKeyboardButton("🗑 确认删除用户", callback_data=f"confirm_del_user_{target_uuid}")],
            [InlineKeyboardButton("🔙 返回列表", callback_data=f"list_user_subs_{dict(sub)['tg_id']}")],
        ]
        await send_or_edit_menu(update, context, msg, InlineKeyboardMarkup(keyboard))
    elif data.startswith("user_reqhist_"):
        target_uuid = data.replace("user_reqhist_", "")
        sub = db_query("SELECT * FROM subscriptions WHERE uuid = ?", (target_uuid,), one=True)
        history = await get_user_subscription_history(target_uuid)
        records = history.get('records') if isinstance(history, dict) else None
        total = history.get('total') if isinstance(history, dict) else None
        if not isinstance(records, list):
            records = []
        lines = [f"📜 **请求记录（最近{len(records)}条）**", f"UUID: `{target_uuid}`"]
        if isinstance(total, int):
            lines.append(f"总记录数: `{total}`")
        lines.append("")
        if not records:
            lines.append("暂无请求记录")
        else:
            for rec in records[:10]:
                req_at = format_time(rec.get('requestAt'))
                req_ip = rec.get('requestIp') or '未知IP'
                ua = (rec.get('userAgent') or '未知UA')[:40]
                lines.append(f"• `{req_at}` | `{req_ip}` | `{ua}`")
        back_tg = dict(sub)['tg_id'] if sub else ADMIN_ID
        kb = [[InlineKeyboardButton("🔙 返回用户", callback_data=f"manage_user_{target_uuid}")], [InlineKeyboardButton("🔙 返回列表", callback_data=f"list_user_subs_{back_tg}")]]
        await send_or_edit_menu(update, context, "\n".join(lines), InlineKeyboardMarkup(kb))
    elif data.startswith("reset_traffic_"):
        target_uuid = data.replace("reset_traffic_", "")
        resp = await safe_api_request('POST', f"/users/{target_uuid}/actions/reset-traffic")
        if resp and resp.status_code == 204: await query.answer("✅ 流量已重置", show_alert=True)
        else: await query.answer("❌ 操作失败", show_alert=True)
    elif data.startswith("confirm_del_user_"):
        target_uuid = data.replace("confirm_del_user_", "")
        await safe_api_request('DELETE', f"/users/{target_uuid}")
        db_execute("DELETE FROM subscriptions WHERE uuid = ?", (target_uuid,))
        await query.answer("✅ 用户已删除", show_alert=True)
        await show_users_list(update, context)
    elif data == "admin_notify":
        try:
            val = db_query("SELECT value FROM settings WHERE key='notify_days'", one=True)
            day = val['value'] if val else 3
        except Exception as exc:
            logger.warning("failed to load notify_days setting: %s", exc)
            day = 3
        kb = [[InlineKeyboardButton("🔙 取消", callback_data="cancel_op")]]
        await send_or_edit_menu(update, context, f"🔔 **提醒设置**\n当前：到期前 {day} 天发送提醒\n\n**⬇️ 请回复新的天数（纯数字）：**", InlineKeyboardMarkup(kb))
        context.user_data['setting_notify'] = True
    elif data == "admin_cleanup":
        try:
            val = db_query("SELECT value FROM settings WHERE key='cleanup_days'", one=True)
            day = val['value'] if val else 7
        except Exception as exc:
            logger.warning("failed to load cleanup_days setting: %s", exc)
            day = 7
        kb = [[InlineKeyboardButton("🔙 取消", callback_data="cancel_op")]]
        await send_or_edit_menu(update, context, f"🗑 **清理设置**\n当前：过期后 {day} 天自动删除\n(过期1天将只禁用)\n\n**⬇️ 请回复新的天数（纯数字）：**", InlineKeyboardMarkup(kb))
        context.user_data['setting_cleanup'] = True
    elif data == "admin_anomaly_menu":
        try:
            val_int = db_query("SELECT value FROM settings WHERE key='anomaly_interval'", one=True)
            interval = val_int['value'] if val_int else 1
            val_thr = db_query("SELECT value FROM settings WHERE key='anomaly_threshold'", one=True)
            threshold = val_thr['value'] if val_thr else 50
        except Exception as exc:
            logger.warning("failed to load anomaly settings: %s", exc)
            interval=1; threshold=50
        stats = await get_subscription_history_stats()
        by_app = stats.get('byParsedApp') if isinstance(stats, dict) else None
        app_top = "暂无"
        if isinstance(by_app, list) and by_app:
            top = sorted(by_app, key=lambda x: x.get('count', 0), reverse=True)[:3]
            app_top = ", ".join(f"{(x.get('app') or 'unknown')}:{int(x.get('count', 0))}" for x in top)
        hourly = stats.get('hourlyRequestStats') if isinstance(stats, dict) else None
        hourly_last = int(hourly[-1].get('requestCount', 0)) if isinstance(hourly, list) and hourly else 0
        msg = (
            f"🛡️ **异常检测设置**\n\n"
            f"⏱️ 检测周期：每 {interval} 小时\n"
            f"🔢 封禁阈值：单周期 > {threshold} 个IP\n"
            f"📊 最近1小时请求量：`{hourly_last}`\n"
            f"📱 TOP客户端：`{app_top}`\n\n"
            "检测支持多级处置：低风险告警入观察名单，中风险限速，高风险禁用。"
        )
        kb = [[InlineKeyboardButton("⏱️ 设置周期", callback_data="set_anomaly_interval"), InlineKeyboardButton("🔢 设置阈值", callback_data="set_anomaly_threshold")],[InlineKeyboardButton("📋 白名单", callback_data="anomaly_whitelist_menu"), InlineKeyboardButton("🛡️ 风控策略", callback_data="admin_risk_policy")],[InlineKeyboardButton("🧾 风控回溯", callback_data="admin_risk_audit")],[InlineKeyboardButton("🔙 返回", callback_data="back_home")]]
        await send_or_edit_menu(update, context, msg, InlineKeyboardMarkup(kb))
    elif data == "set_anomaly_interval":
        kb = [[InlineKeyboardButton("🔙 取消", callback_data="admin_anomaly_menu")]]
        await send_or_edit_menu(update, context, "⏱️ **请输入检测周期 (小时)**\n例如：0.5 (半小时) 或 1 (一小时)", InlineKeyboardMarkup(kb))
        context.user_data['setting_anomaly_interval'] = True
    elif data == "set_anomaly_threshold":
        kb = [[InlineKeyboardButton("🔙 取消", callback_data="admin_anomaly_menu")]]
        await send_or_edit_menu(update, context, "🔢 **请输入封禁阈值 (IP数量)**\n例如：50", InlineKeyboardMarkup(kb))
        context.user_data['setting_anomaly_threshold'] = True
    elif data.startswith("set_strategy_"):
        strategy = data.replace("set_strategy_", "")
        new_plan = context.user_data['new_plan']
        key = f"p{int(time.time())}"
        db_execute("INSERT INTO plans VALUES (?, ?, ?, ?, ?, ?)", (key, new_plan['name'], new_plan['price'], new_plan['days'], new_plan['gb'], strategy))
        del context.user_data['add_plan_step']
        await send_or_edit_menu(update, context, f"✅ **套餐添加成功！**\n{new_plan['name']} - {strategy}", None)
        await asyncio.sleep(1)
        await show_plans_menu(update, context)

async def show_users_list(update, context):
    users = db_query("SELECT DISTINCT tg_id, MAX(created_at) as created_at FROM subscriptions GROUP BY tg_id ORDER BY created_at DESC LIMIT 20")
    keyboard = []
    for u in users:
        u_dict = dict(u)
        ts = u_dict['created_at']
        date_str = datetime.datetime.fromtimestamp(int(ts)).strftime('%m-%d')
        btn_text = f"🆔 {u_dict['tg_id']} | {date_str}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"list_user_subs_{u_dict['tg_id']}")])
    keyboard.append([InlineKeyboardButton("🔙 返回", callback_data="back_home")])
    await send_or_edit_menu(update, context, "👥 **用户管理 (最近20名)**\n点击ID查看其名下订阅：", InlineKeyboardMarkup(keyboard))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ 取消", callback_data="cancel_op")]])

    if user_id == ADMIN_ID and context.user_data.get('set_payimg'):
        pay_type = context.user_data.get('set_payimg')
        file_id = None
        if update.message.photo:
            file_id = update.message.photo[-1].file_id
        elif update.message.document and (update.message.document.mime_type or '').startswith('image/'):
            file_id = update.message.document.file_id
        if not file_id:
            await update.message.reply_text("❌ 请发送图片文件", reply_markup=cancel_kb)
            return
        key = 'alipay_qr_file_id' if pay_type == 'alipay' else 'wechat_qr_file_id'
        set_setting_value(key, file_id)
        context.user_data.pop('set_payimg', None)
        label = '支付宝' if pay_type == 'alipay' else '微信支付'
        await update.message.reply_text(f"✅ 已更新{label}收款码。", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_pay_settings")]]))
        return

    if user_id == ADMIN_ID and context.user_data.get('broadcast_mode'):
        user_rows = db_query("SELECT DISTINCT tg_id FROM subscriptions")
        order_rows = db_query("SELECT DISTINCT tg_id FROM orders")
        targets = {int(dict(r)['tg_id']) for r in user_rows} | {int(dict(r)['tg_id']) for r in order_rows}
        ok = 0
        fail = 0
        for uid in targets:
            try:
                await context.bot.copy_message(chat_id=uid, from_chat_id=user_id, message_id=update.message.message_id)
                ok += 1
            except Exception:
                fail += 1
        context.user_data.pop('broadcast_mode', None)
        await update.message.reply_text(f"📢 群发完成\n成功: {ok}\n失败: {fail}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回主菜单", callback_data="back_home")]]))
        return
    if user_id == ADMIN_ID and context.user_data.get('panelcfg_input_url') and text:
        save_runtime_config(panel_url=text.strip())
        context.user_data.pop('panelcfg_input_url', None)
        await update.message.reply_text("✅ 面板地址已更新", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_panel_config")]]))
        return
    if user_id == ADMIN_ID and context.user_data.get('panelcfg_input_token') and text:
        save_runtime_config(panel_token=text.strip())
        context.user_data.pop('panelcfg_input_token', None)
        await update.message.reply_text("✅ 面板 Token 已更新", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_panel_config")]]))
        return
    if user_id == ADMIN_ID and context.user_data.get('panelcfg_input_subdomain') and text:
        save_runtime_config(sub_domain=text.strip())
        context.user_data.pop('panelcfg_input_subdomain', None)
        await update.message.reply_text("✅ 订阅域名已更新", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_panel_config")]]))
        return
    if user_id == ADMIN_ID and context.user_data.get('panelcfg_input_group') and text:
        save_runtime_config(group_uuid=text.strip())
        context.user_data.pop('panelcfg_input_group', None)
        await update.message.reply_text("✅ 默认组 UUID 已更新", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_panel_config")]]))
        return

    if user_id == ADMIN_ID and context.user_data.get('edit_subscription_settings') and text:
        try:
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise ValueError('必须是JSON对象')
            current = await get_subscription_settings()
            push_subscription_settings_snapshot(current, source='手工JSON变更前自动备份')
            resp = await patch_subscription_settings(payload)
            context.user_data.pop('edit_subscription_settings', None)
            if resp and resp.status_code in (200, 204):
                append_ops_timeline('配置', '手动更新订阅设置', json.dumps(payload, ensure_ascii=False)[:180], actor=user_id)
                await update.message.reply_text("✅ 订阅设置已更新", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_subscription_settings")]]))
            else:
                await update.message.reply_text("❌ 更新失败，请检查字段", reply_markup=cancel_kb)
        except Exception as exc:
            await update.message.reply_text(f"❌ JSON解析或更新失败: {exc}", reply_markup=cancel_kb)
        return

    if user_id == ADMIN_ID and context.user_data.get('squad_bulk_move') and text:
        try:
            lines = [x.strip() for x in text.splitlines() if x.strip()]
            if len(lines) < 2:
                raise ValueError('格式不正确，至少需要分组UUID和1个用户UUID')
            squad_uuid = lines[0]
            uuids = parse_uuids("\n".join(lines[1:]))
            if not uuids:
                raise ValueError('未解析到有效用户UUID')
            resp = await bulk_move_users_to_squad(uuids, squad_uuid)
            context.user_data.pop('squad_bulk_move', None)
            if resp and resp.status_code in (200, 201, 204):
                await update.message.reply_text(f"✅ 已提交批量迁移，目标{len(uuids)}个用户", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_squads_menu")]]))
            else:
                await update.message.reply_text("❌ 迁移失败，请检查分组UUID与用户UUID", reply_markup=cancel_kb)
        except Exception as exc:
            await update.message.reply_text(f"❌ 迁移失败: {exc}", reply_markup=cancel_kb)
        return

    if user_id == ADMIN_ID and context.user_data.get('edit_risk_policy') and text:
        try:
            low_text, high_text = [x.strip() for x in text.split(',', 1)]
            low = int(low_text)
            high = int(high_text)
            if low <= 0 or high <= low:
                raise ValueError('要求 低阈值>0 且 高阈值>低阈值')
            set_setting_value('risk_low_score', low)
            set_setting_value('risk_high_score', high)
            context.user_data.pop('edit_risk_policy', None)
            await update.message.reply_text(f"✅ 风控策略已更新：低={low} 高={high}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_anomaly_menu")]]))
        except Exception as exc:
            await update.message.reply_text(f"❌ 参数错误: {exc}", reply_markup=cancel_kb)
        return
    if user_id == ADMIN_ID and context.user_data.get('edit_risk_unfreeze_hours') and text:
        try:
            val = int(text.strip())
            if val <= 0:
                raise ValueError('必须大于0')
            set_setting_value('risk_auto_unfreeze_hours', val)
            context.user_data.pop('edit_risk_unfreeze_hours', None)
            append_ops_timeline('风控', '修改自动解封时长', f'hours={val}', actor=user_id)
            await update.message.reply_text(f"✅ 自动解封时长已更新为 {val} 小时", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_risk_policy")]]))
        except Exception as exc:
            await update.message.reply_text(f"❌ 参数错误: {exc}", reply_markup=cancel_kb)
        return
    if user_id == ADMIN_ID and 'reply_to_uid' in context.user_data:
        target_uid = context.user_data['reply_to_uid']
        try:
            await context.bot.copy_message(chat_id=target_uid, from_chat_id=user_id, message_id=update.message.message_id)
            await context.bot.send_message(target_uid, "👆 **(来自客服的回复)**", parse_mode='Markdown')
            admin_done_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回主菜单", callback_data="back_home")]])
            await update.message.reply_text("✅ 回复已送达！", reply_markup=admin_done_kb)
        except Exception as e: await update.message.reply_text(f"❌ 发送失败：{e}")
        del context.user_data['reply_to_uid']
        return
    if context.user_data.get('chat_mode') == 'support':
        admin_header = f"📨 **新客服消息**\n来自：{update.effective_user.mention_html()} (`{user_id}`)"
        reply_kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ 回复此用户", callback_data=f"reply_user_{user_id}")]])
        await context.bot.send_message(ADMIN_ID, admin_header, reply_markup=reply_kb, parse_mode='HTML')
        await context.bot.copy_message(chat_id=ADMIN_ID, from_chat_id=user_id, message_id=update.message.message_id)
        await update.message.reply_text("✅ 已转发")
        return
    if user_id == ADMIN_ID and context.user_data.get('setting_notify') and text:
        if text.isdigit():
            db_execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('notify_days', ?)", (text,))
            context.user_data['setting_notify'] = False
            await update.message.reply_text(f"✅ 已设置：到期前 {text} 天提醒。", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="back_home")]]))
        else: await update.message.reply_text("❌ 请输入数字", reply_markup=cancel_kb)
        return
    if user_id == ADMIN_ID and context.user_data.get('setting_cleanup') and text:
        if text.isdigit():
            db_execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('cleanup_days', ?)", (text,))
            context.user_data['setting_cleanup'] = False
            await update.message.reply_text(f"✅ 已设置：过期后 {text} 天自动删除。", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="back_home")]]))
        else: await update.message.reply_text("❌ 请输入数字", reply_markup=cancel_kb)
        return
    if user_id == ADMIN_ID and context.user_data.get('setting_anomaly_interval') and text:
        try:
            val = float(text)
            if val <= 0: raise ValueError
            db_execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('anomaly_interval', ?)", (text,))
            context.user_data['setting_anomaly_interval'] = False
            await reschedule_anomaly_job(context.application, val)
            await update.message.reply_text(f"✅ 周期已更新：每 {val} 小时检测一次。", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_anomaly_menu")]]))
        except (ValueError, TypeError):
            await update.message.reply_text("❌ 请输入有效的数字 (例如 0.5 或 1)", reply_markup=cancel_kb)
        return
    if user_id == ADMIN_ID and context.user_data.get('setting_anomaly_threshold') and text:
        if text.isdigit():
            db_execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('anomaly_threshold', ?)", (text,))
            context.user_data['setting_anomaly_threshold'] = False
            await update.message.reply_text(f"✅ 阈值已更新：> {text} IP 封禁。", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_anomaly_menu")]]))
        else: await update.message.reply_text("❌ 请输入整数", reply_markup=cancel_kb)
        return

    if user_id == ADMIN_ID and context.user_data.get('add_anomaly_whitelist') and text:
        value = text.strip()
        if len(value) < 8:
            await update.message.reply_text("❌ 请输入有效 UUID")
            return
        db_execute("INSERT OR IGNORE INTO anomaly_whitelist (user_uuid, created_at) VALUES (?, ?)", (value, int(time.time())))
        context.user_data['add_anomaly_whitelist'] = False
        await update.message.reply_text("✅ 白名单已添加。", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="anomaly_whitelist_menu")]]))
        return
    if user_id == ADMIN_ID and context.user_data.get('bulk_action') and text:
        action = context.user_data.get('bulk_action')
        try:
            pending = context.user_data.get('bulk_pending')
            if pending:
                if text.strip() != '确认执行':
                    context.user_data.pop('bulk_pending', None)
                    context.user_data.pop('bulk_action', None)
                    await update.message.reply_text(
                        '已取消批量执行。',
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_bulk_menu")]]),
                    )
                    return
                uuids = pending['uuids']
                extra = pending.get('extra')
                ok, fail = await run_bulk_action(safe_api_request, action, uuids, extra_fields=extra)
                context.user_data.pop('bulk_action', None)
                context.user_data.pop('bulk_pending', None)
                await update.message.reply_text(
                    f"✅ 批量操作完成\n成功: {ok}\n失败: {fail}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_bulk_menu")]]),
                )
                return

            if action in {'reset', 'disable', 'delete'}:
                uuids = parse_uuids(text)
                extra = None
                preview = {'reset': '批量重置流量', 'disable': '批量禁用', 'delete': '批量删除'}[action]
            elif action == 'expire':
                expire_at, uuids = parse_expire_days_and_uuids(text)
                extra = {'expireAt': expire_at}
                preview = f"批量改到期时间 -> {expire_at}"
            elif action == 'traffic':
                traffic_bytes, uuids = parse_traffic_and_uuids(text)
                extra = {'trafficLimitBytes': traffic_bytes}
                preview = f"批量改流量包 -> {traffic_bytes // (1024**3)}GB"
            else:
                await update.message.reply_text("❌ 未知操作类型", reply_markup=cancel_kb)
                return

            if not uuids:
                await update.message.reply_text("❌ 未解析到有效UUID，请检查输入格式", reply_markup=cancel_kb)
                return

            context.user_data['bulk_pending'] = {'uuids': uuids, 'extra': extra}
            await update.message.reply_text(
                f"🧪 预检查完成\n操作: {preview}\n目标数量: {len(uuids)}\n\n如确认执行，请回复：确认执行\n回复其他任意内容将取消。",
                reply_markup=cancel_kb,
            )
        except Exception as exc:
            context.user_data.pop('bulk_pending', None)
            await update.message.reply_text(f"❌ 批量操作失败: {exc}", reply_markup=cancel_kb)
        return
    if user_id == ADMIN_ID and 'add_plan_step' in context.user_data and text:
        step = context.user_data['add_plan_step']
        if step == 'name':
            context.user_data['new_plan'] = {'name': text}
            context.user_data['add_plan_step'] = 'price'
            await update.message.reply_text("📝 **步骤 2/5：请输入价格**\n(例如: 200元)", reply_markup=cancel_kb, parse_mode='Markdown')
        elif step == 'price':
            context.user_data['new_plan']['price'] = text
            context.user_data['add_plan_step'] = 'days'
            await update.message.reply_text("📅 **步骤 3/5：请输入有效期天数**\n(请输入纯数字，例如: 30)", reply_markup=cancel_kb, parse_mode='Markdown')
        elif step == 'days':
            if not text.isdigit(): return await update.message.reply_text("❌ 请输入数字", reply_markup=cancel_kb)
            context.user_data['new_plan']['days'] = int(text)
            context.user_data['add_plan_step'] = 'gb'
            await update.message.reply_text("📡 **步骤 4/5：请输入流量限制 GB**\n(请输入纯数字，例如: 100)", reply_markup=cancel_kb, parse_mode='Markdown')
        elif step == 'gb':
            if not text.isdigit(): return await update.message.reply_text("❌ 请输入数字", reply_markup=cancel_kb)
            context.user_data['new_plan']['gb'] = int(text)
            keyboard = [[InlineKeyboardButton("🚫 永不重置", callback_data="set_strategy_NO_RESET")], [InlineKeyboardButton("📅 每日重置", callback_data="set_strategy_DAY")], [InlineKeyboardButton("🗓 每周重置", callback_data="set_strategy_WEEK")], [InlineKeyboardButton("🌝 每月重置", callback_data="set_strategy_MONTH")], [InlineKeyboardButton("❌ 取消", callback_data="cancel_op")]]
            await update.message.reply_text("🔄 **步骤 5/5：请选择流量重置策略**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return
    pending_order = get_pending_order_for_user(db_query, user_id)
    if pending_order and (text or update.message.photo or update.message.document):
        plan = db_query("SELECT * FROM plans WHERE key = ?", (pending_order['plan_key'],), one=True)
        if not plan:
            await update.message.reply_text("❌ 当前订单关联套餐已删除，请重新下单。")
            update_order_status(db_execute, pending_order['order_id'], [STATUS_PENDING], STATUS_FAILED, error_message='plan_deleted')
            return

        t_str = "续费" if pending_order['order_type'] == 'renew' else "新购"
        pay_method = order_payment_method_cache.get(pending_order['order_id'], 'alipay')
        pay_label = "支付宝" if pay_method == 'alipay' else "微信支付"
        target_uuid = pending_order['target_uuid'] if pending_order['target_uuid'] else "0"
        sid = get_short_id(target_uuid) if target_uuid != "0" else "0"
        kb = [
            [InlineKeyboardButton("✅ 通过", callback_data=f"ap_{pending_order['order_id']}_{sid}")],
            [InlineKeyboardButton("❌ 拒绝", callback_data=f"rj_{pending_order['order_id']}")],
            [InlineKeyboardButton("📨 给用户发消息", callback_data=f"reply_user_{user_id}")],
        ]

        if text:
            escaped_text = escape_markdown_v2(text)
            admin_msg = (
                f"*💰 审核 {escape_markdown_v2(t_str)}*\n"
                f"👤 用户ID: `{user_id}`\n"
                f"📦 套餐: `{escape_markdown_v2(dict(plan)['name'])}`\n"
                f"💳 支付方式: `{escape_markdown_v2(pay_label)}`\n"
                f"📝 口令/说明: `{escaped_text}`"
            )
            admin_message = await context.bot.send_message(
                ADMIN_ID,
                admin_msg,
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode='MarkdownV2',
            )
        else:
            admin_msg = (
                f"💰 审核 {t_str}\n"
                f"👤 用户ID: {user_id}\n"
                f"📦 套餐: {dict(plan)['name']}\n"
                f"💳 支付方式: {pay_label}\n"
                f"📎 用户已提交支付凭证图片/文件"
            )
            admin_message = await context.bot.send_message(ADMIN_ID, admin_msg, reply_markup=InlineKeyboardMarkup(kb))
            await context.bot.copy_message(chat_id=ADMIN_ID, from_chat_id=user_id, message_id=update.message.message_id)

        msg_obj = await update.message.reply_text(
            "✅ 已提交，等待管理员审核。",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回主菜单", callback_data="back_home")]]),
        )
        attach_admin_message(db_execute, pending_order['order_id'], admin_message.message_id)
        attach_payment_text(db_execute, pending_order['order_id'], f"方式:{pay_label}|{text or '[图片/文件]'}", waiting_message_id=msg_obj.message_id)

async def add_plan_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['add_plan_step'] = 'name'
    await query.edit_message_text("📝 **步骤 1/5：开始添加套餐**\n\n请输入套餐名称:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ 取消", callback_data="cancel_op")]]), parse_mode='Markdown')

async def process_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    client_return_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回主菜单", callback_data="back_home")]])
    admin_return_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回主菜单", callback_data="back_home")]])

    async def clean_user_waiting_msg(order):
        waiting_message_id = order.get('waiting_message_id')
        menu_message_id = order.get('menu_message_id')
        uid = order.get('tg_id')
        if waiting_message_id:
            try:
                await context.bot.delete_message(chat_id=uid, message_id=waiting_message_id)
            except Exception as exc:
                logger.warning("Failed to delete waiting message for %s: %s", uid, exc)
        if menu_message_id:
            try:
                await context.bot.delete_message(chat_id=uid, message_id=menu_message_id)
            except Exception as exc:
                logger.warning("Failed to delete menu message for %s: %s", uid, exc)

    if data.startswith("rj_"):
        order_id = data.split("_")[1]
        order = get_order(db_query, order_id)
        if not order:
            await query.edit_message_text("⚠️ 订单不存在或已过期", reply_markup=admin_return_btn)
            return
        changed = update_order_status(db_execute, order_id, [STATUS_PENDING, STATUS_APPROVED], STATUS_REJECTED, error_message='rejected_by_admin')
        append_order_audit_log(db_execute, order_id, 'reject', query.from_user.id, 'rejected_by_admin')
        if not changed and order.get('status') == STATUS_REJECTED:
            await query.edit_message_text("ℹ️ 该订单已拒绝，无需重复操作", reply_markup=admin_return_btn)
            return
        await query.edit_message_text("❌ 已拒绝", reply_markup=admin_return_btn)
        await clean_user_waiting_msg(order)
        try:
            await context.bot.send_message(order['tg_id'], "❌ 您的订单已被管理员拒绝。", reply_markup=client_return_btn)
        except Exception as exc:
            logger.warning("Failed to notify rejected order user %s: %s", order['tg_id'], exc)
        return

    if data.startswith("rt_"):
        order_id = data.split("_", 1)[1]
        order = get_order(db_query, order_id)
        if not order:
            await query.edit_message_text("⚠️ 订单不存在", reply_markup=admin_return_btn)
            return
        if order.get('status') != STATUS_FAILED:
            await query.edit_message_text("⚠️ 仅允许重试失败订单", reply_markup=admin_return_btn)
            return
        switched = update_order_status(db_execute, order_id, [STATUS_FAILED], STATUS_APPROVED, error_message='retry_by_admin')
        append_order_audit_log(db_execute, order_id, 'retry', query.from_user.id, 'retry_by_admin')
        if not switched:
            await query.edit_message_text("⚠️ 订单状态更新失败，请重试", reply_markup=admin_return_btn)
            return
        sid = "0"
        if order.get('target_uuid') and order.get('target_uuid') != '0':
            sid = get_short_id(order['target_uuid'])
        data = f"ap_{order_id}_{sid}"

    if not data.startswith("ap_"):
        return

    _, order_id, short_id = data.split("_", 2)
    order = get_order(db_query, order_id)
    if not order:
        await query.edit_message_text("⚠️ 订单不存在或已过期", reply_markup=admin_return_btn)
        return

    if order.get('status') == STATUS_DELIVERED:
        await query.edit_message_text("ℹ️ 该订单已发货（幂等保护）", reply_markup=admin_return_btn)
        return

    if order.get('status') not in [STATUS_PENDING, STATUS_APPROVED]:
        await query.edit_message_text(f"⚠️ 当前订单状态不可处理: {order.get('status')}", reply_markup=admin_return_btn)
        return

    claimed = update_order_status(db_execute, order_id, [STATUS_PENDING], STATUS_APPROVED)
    if not claimed and order.get('status') != STATUS_APPROVED:
        await query.edit_message_text("⚠️ 订单正在被其他操作处理，请稍后重试", reply_markup=admin_return_btn)
        return

    uid = order['tg_id']
    plan_key = order['plan_key']
    order_type = order['order_type']
    target_uuid = order['target_uuid'] if order['target_uuid'] != '0' else get_real_uuid(short_id)

    plan = db_query("SELECT * FROM plans WHERE key = ?", (plan_key,), one=True)
    if not plan:
        update_order_status(db_execute, order_id, [STATUS_APPROVED], STATUS_FAILED, error_message='reason:business_validation|plan_deleted')
        await query.edit_message_text("❌ 套餐已删除", reply_markup=admin_return_btn)
        return

    await query.edit_message_text("🔄 处理中...")
    plan_dict = dict(plan)
    add_traffic = plan_dict['gb'] * 1024 * 1024 * 1024
    add_days = plan_dict['days']
    reset_strategy = plan_dict.get('reset_strategy', 'NO_RESET')
    strategy_label = get_strategy_label(reset_strategy)

    try:
        if order_type == 'renew':
            if not target_uuid:
                update_order_status(db_execute, order_id, [STATUS_APPROVED], STATUS_FAILED, error_message='reason:business_validation|missing_target_uuid')
                await query.edit_message_text("⚠️ 订单数据已过期", reply_markup=admin_return_btn)
                return
            user_info = await get_panel_user(target_uuid)
            if not user_info:
                update_order_status(db_execute, order_id, [STATUS_APPROVED], STATUS_FAILED, error_message='user_not_found')
                await query.edit_message_text("⚠️ 用户不存在", reply_markup=admin_return_btn)
                return
            current_expire_str = user_info.get('expireAt', '').split('.')[0].replace('Z', '')
            now = datetime.datetime.utcnow()
            try:
                current_expire = datetime.datetime.strptime(current_expire_str, "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                current_expire = now
            new_expire = (current_expire + datetime.timedelta(days=add_days)) if current_expire > now else (now + datetime.timedelta(days=add_days))
            expire_iso = new_expire.strftime("%Y-%m-%dT%H:%M:%SZ")
            new_limit = user_info.get('trafficLimitBytes', 0)
            if reset_strategy == 'NO_RESET':
                new_limit += add_traffic
            update_payload = {
                "uuid": target_uuid,
                "trafficLimitBytes": new_limit,
                "expireAt": expire_iso,
                "status": "ACTIVE",
                "activeInternalSquads": [TARGET_GROUP_UUID],
                "trafficLimitStrategy": reset_strategy,
            }
            await safe_api_request('POST', f"/users/{target_uuid}/actions/enable")
            r = await safe_api_request('PATCH', "/users", json_data=update_payload)
            if r and r.status_code in [200, 204]:
                update_order_status(db_execute, order_id, [STATUS_APPROVED], STATUS_DELIVERED, delivered_uuid=target_uuid)
                append_order_audit_log(db_execute, order_id, 'deliver_success', query.from_user.id, 'renew')
                await query.edit_message_text(f"✅ 续费成功\n用户: {uid}", reply_markup=admin_return_btn)
                sub_url = user_info.get('subscriptionUrl', '')
                display_expire = format_time(expire_iso)
                display_traffic = round(new_limit / 1024**3, 2)
                msg = (
                    f"🎉 *续费成功\!*\n\n"
                    f"⏳ 新到期时间: `{escape_markdown_v2(display_expire)}`\n"
                    f"📡 当前总流量: `{escape_markdown_v2(str(display_traffic))} GB \({escape_markdown_v2(strategy_label)}\)`\n\n"
                    f"🔗 订阅链接:\n`{escape_markdown_v2(sub_url)}`"
                )
                await clean_user_waiting_msg(order)
                if sub_url and sub_url.startswith('http'):
                    qr = generate_qr(sub_url)
                    await context.bot.send_photo(uid, photo=qr, caption=msg, parse_mode='MarkdownV2', reply_markup=client_return_btn)
                else:
                    await context.bot.send_message(uid, msg, parse_mode='MarkdownV2', reply_markup=client_return_btn)
            else:
                update_order_status(db_execute, order_id, [STATUS_APPROVED], STATUS_FAILED, error_message='reason:network|panel_api_error_renew')
                await query.edit_message_text("❌ API报错", reply_markup=admin_return_btn)
        else:
            new_expire = datetime.datetime.utcnow() + datetime.timedelta(days=add_days)
            expire_iso = new_expire.strftime("%Y-%m-%dT%H:%M:%SZ")
            payload = {
                "username": f"tg_{uid}_{int(time.time())}",
                "status": "ACTIVE",
                "trafficLimitBytes": add_traffic,
                "trafficLimitStrategy": reset_strategy,
                "expireAt": expire_iso,
                "proxies": {},
                "activeInternalSquads": [TARGET_GROUP_UUID],
            }
            r = await safe_api_request('POST', "/users", json_data=payload)
            if r and r.status_code in [200, 201]:
                resp_data = extract_payload(r)
                user_uuid = resp_data.get('uuid')
                db_execute(
                    "INSERT INTO subscriptions (tg_id, uuid, created_at, plan_key) VALUES (?, ?, ?, ?)",
                    (uid, user_uuid, int(time.time()), plan_key),
                )
                update_order_status(db_execute, order_id, [STATUS_APPROVED], STATUS_DELIVERED, delivered_uuid=user_uuid)
                append_order_audit_log(db_execute, order_id, 'deliver_success', query.from_user.id, 'new')
                await query.edit_message_text(f"✅ 开通成功\n用户: {uid}", reply_markup=admin_return_btn)
                sub_url = resp_data.get('subscriptionUrl', '')
                display_expire = format_time(expire_iso)
                msg = (
                    f"🎉 *订阅开通成功\!*\n\n"
                    f"📦 套餐: {escape_markdown_v2(plan_dict['name'])}\n"
                    f"⏳ 到期时间: `{escape_markdown_v2(display_expire)}`\n"
                    f"📡 包含流量: `{escape_markdown_v2(str(plan_dict['gb']))} GB \({escape_markdown_v2(strategy_label)}\)`\n\n"
                    f"🔗 订阅链接:\n`{escape_markdown_v2(sub_url)}`"
                )
                await clean_user_waiting_msg(order)
                if sub_url and sub_url.startswith('http'):
                    qr = generate_qr(sub_url)
                    await context.bot.send_photo(uid, photo=qr, caption=msg, parse_mode='MarkdownV2', reply_markup=client_return_btn)
                else:
                    await context.bot.send_message(uid, msg, parse_mode='MarkdownV2', reply_markup=client_return_btn)
            else:
                update_order_status(db_execute, order_id, [STATUS_APPROVED], STATUS_FAILED, error_message='reason:network|panel_api_error_new')
                await query.edit_message_text("❌ 失败", reply_markup=admin_return_btn)
    except Exception as exc:
        logger.exception("Order processing failed for %s", order_id)
        reason = classify_order_failure(str(exc))
        detail = f"reason:{reason}|{str(exc)[:320]}"
        update_order_status(db_execute, order_id, [STATUS_APPROVED], STATUS_FAILED, error_message=detail)
        append_order_audit_log(db_execute, order_id, 'deliver_failed', query.from_user.id, detail)
        await query.edit_message_text(f"❌ 错误: {exc}", reply_markup=admin_return_btn)

async def check_expiry_job(context: ContextTypes.DEFAULT_TYPE):
    try: 
        val = db_query("SELECT value FROM settings WHERE key='notify_days'", one=True)
        notify_days = int(val['value']) if val else 3
        val_clean = db_query("SELECT value FROM settings WHERE key='cleanup_days'", one=True)
        cleanup_days = int(val_clean['value']) if val_clean else 7
    except Exception as exc:
        logger.warning("failed to load expiry job settings: %s", exc)
        notify_days = 3
        cleanup_days = 7
    subs = db_query("SELECT * FROM subscriptions")
    if not subs: return
    now = datetime.datetime.utcnow()
    to_delete_uuids = []
    sem = asyncio.Semaphore(10)
    async def check_single_sub(sub):
        async with sem:
            u_dict = dict(sub)
            info = await get_panel_user(u_dict['uuid'])
            if not info: return
            try:
                ex_str = info.get('expireAt', '').split('.')[0].replace('Z','')
                ex_dt = datetime.datetime.strptime(ex_str, "%Y-%m-%dT%H:%M:%S")
                days_left = (ex_dt - now).days
                if 0 <= days_left <= notify_days:
                    last_notify_expire = u_dict.get('last_notify_expire_at')
                    last_notify_days_left = u_dict.get('last_notify_days_left')
                    last_notify_at = int(u_dict.get('last_notify_at') or 0)
                    now_ts = int(time.time())
                    can_send_by_daily_limit = should_send_expire_notice(last_notify_at, now_ts)
                    if (str(last_notify_expire or '') != ex_str or int(last_notify_days_left or -999) != days_left) and can_send_by_daily_limit:
                        sid = get_short_id(u_dict['uuid'])
                        kb = [[InlineKeyboardButton("💳 立即续费", callback_data=f"selrenew_{sid}")]]
                        msg = f"⚠️ **续费提醒**\n\n您的订阅 (UUID: `{u_dict['uuid'][:8]}...`) \n将在 **{days_left}** 天后到期。\n请及时续费以免服务中断。"
                        try:
                            await context.bot.send_message(u_dict['tg_id'], msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))
                            db_execute(
                                "UPDATE subscriptions SET last_notify_expire_at = ?, last_notify_days_left = ?, last_notify_at = ? WHERE uuid = ?",
                                (ex_str, days_left, int(time.time()), u_dict['uuid']),
                            )
                        except Exception as exc:
                            logger.warning("Failed to send expiry notice to %s: %s", u_dict['tg_id'], exc)
                if days_left == -1 and info.get('status') == 'active':
                    await safe_api_request('POST', f"/users/{u_dict['uuid']}/actions/disable")
                if days_left < -cleanup_days:
                    to_delete_uuids.append(u_dict['uuid'])
                    db_execute("DELETE FROM subscriptions WHERE uuid = ?", (u_dict['uuid'],))
                    try:
                        await context.bot.send_message(u_dict['tg_id'], f"🗑 您的订阅因过期超过 {cleanup_days} 天已被系统回收。")
                    except Exception as exc:
                        logger.warning("Failed to notify cleanup to %s: %s", u_dict['tg_id'], exc)
            except Exception as e:
                logger.warning("check_single_sub failed for %s: %s", u_dict.get('uuid'), e)
    tasks = [check_single_sub(sub) for sub in subs]
    await asyncio.gather(*tasks)
    if to_delete_uuids:
        await safe_api_request('POST', '/users/bulk/delete', json_data={"uuids": to_delete_uuids})

async def check_anomalies_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        # 自动解封（中风险限速后，低风险持续一段时间自动恢复）
        auto_hours = int(get_setting_value('risk_auto_unfreeze_hours', '12') or '12')
        candidates = get_json_setting('risk_unfreeze_candidates', {})
        if isinstance(candidates, dict) and candidates:
            now_ts = int(time.time())
            changed = False
            for uid, ts in list(candidates.items()):
                try:
                    added_ts = int(ts)
                except Exception:
                    added_ts = now_ts
                if now_ts - added_ts >= auto_hours * 3600:
                    resp = await safe_api_request('PATCH', '/users', json_data={"uuid": uid, "status": "ACTIVE"})
                    if resp and resp.status_code in (200, 201, 204):
                        changed = True
                        candidates.pop(uid, None)
                        append_ops_timeline('风控', '自动解封', f'uid={uid},after={auto_hours}h', actor='系统', target=uid)
            if changed:
                set_json_setting('risk_unfreeze_candidates', candidates)

        val_thr = db_query("SELECT value FROM settings WHERE key='anomaly_threshold'", one=True)
        limit = int(val_thr['value']) if val_thr else 50
        resp = await safe_api_request('GET', '/subscription-request-history')
        if not resp or resp.status_code != 200:
            return
        logs = extract_payload(resp)
        if not isinstance(logs, list) or not logs:
            return

        val_scan = db_query("SELECT value FROM settings WHERE key='anomaly_last_scan_ts'", one=True)
        last_scan_ts = int(val_scan['value']) if val_scan else 0
        whitelist_rows = db_query("SELECT user_uuid FROM anomaly_whitelist")
        whitelist = {dict(r)['user_uuid'] for r in whitelist_rows}

        def _extract_log_ts(log):
            for key in ('createdAt', 'requestAt', 'timestamp', 'time'):
                value = log.get(key)
                if value is None:
                    continue
                if isinstance(value, (int, float)):
                    return int(value)
                if isinstance(value, str):
                    try:
                        if value.isdigit():
                            return int(value)
                        dt = datetime.datetime.strptime(value.split('.')[0].replace('Z', ''), "%Y-%m-%dT%H:%M:%S")
                        return int(dt.timestamp())
                    except Exception:
                        continue
            return 0

        prepared = []
        for row in logs:
            rec = dict(row)
            ts = _extract_log_ts(rec)
            rec['_ts'] = ts
            rec['_fmt_time'] = datetime.datetime.utcfromtimestamp(ts).strftime('%m-%d %H:%M') if ts else '-'
            prepared.append(rec)

        incidents, max_seen_ts = build_anomaly_incidents(prepared, last_scan_ts, whitelist, limit)

        low_score = int(get_setting_value('risk_low_score', '80'))
        high_score = int(get_setting_value('risk_high_score', '130'))
        watchlist = get_risk_watchlist()
        unfreeze_candidates = get_json_setting('risk_unfreeze_candidates', {})
        if not isinstance(unfreeze_candidates, dict):
            unfreeze_candidates = {}

        for item in incidents:
            uid = item['uid']
            score = int(item.get('score', 0))
            if score >= high_score:
                risk_level = '高'
                action_taken = '禁用'
                await safe_api_request('POST', f"/users/{uid}/actions/disable")
                unfreeze_candidates.pop(uid, None)
            elif score >= low_score:
                risk_level = '中'
                action_taken = '限速'
                await safe_api_request('PATCH', '/users', json_data={"uuid": uid, "status": "LIMITED"})
                unfreeze_candidates[uid] = int(time.time())
            else:
                risk_level = '低'
                action_taken = '告警'
                watchlist.add(uid)

            evidence_summary = '; '.join(f"{e['ip']}@{e['ts']}" for e in item['evidence'][:3])
            db_execute(
                "INSERT INTO anomaly_events (user_uuid, risk_level, risk_score, ip_count, ua_diversity, density, action_taken, evidence_summary, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (uid, risk_level, score, int(item['ip_count']), int(item['ua_diversity']), int(item['density']), action_taken, evidence_summary[:400], int(time.time())),
            )
            append_ops_timeline('风控', '异常处置', f'uid={uid},level={risk_level},action={action_taken},score={score}', actor='系统', target=uid)

            try:
                lines = [
                    "🚨 *异常检测（可解释）*",
                    f"风险等级: `{risk_level}` \| 处置: `{action_taken}`",
                    f"用户: `{escape_markdown_v2(uid)}`",
                    f"风险评分: `{score}`",
                    f"IP数量: `{item['ip_count']}` \| UA分散: `{item['ua_diversity']}` \| 请求密度: `{item['density']}`",
                    "证据（最近10条）:",
                ]
                for ev in item['evidence'][:10]:
                    lines.append(
                        f"- `{escape_markdown_v2(str(ev['ts']))}` \| `{escape_markdown_v2(str(ev['ip']))}` \| `{escape_markdown_v2(str(ev['ua']))}`"
                    )
                quick_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ 加入白名单", callback_data=f"anomaly_quick_whitelist_{uid}")],
                    [InlineKeyboardButton("✅ 尝试解封", callback_data=f"anomaly_quick_enable_{uid}")],
                ])
                await context.bot.send_message(ADMIN_ID, "\n".join(lines), parse_mode='MarkdownV2', reply_markup=quick_kb)
            except Exception as exc:
                logger.warning("Failed to notify anomaly admin: %s", exc)

        set_risk_watchlist(watchlist)
        set_json_setting('risk_unfreeze_candidates', unfreeze_candidates)

        if max_seen_ts > last_scan_ts:
            db_execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('anomaly_last_scan_ts', ?)", (str(max_seen_ts),))
    except Exception as exc:
        logger.exception("check_anomalies_job failed: %s", exc)

if __name__ == '__main__':
    import urllib3
    urllib3.disable_warnings()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^admin_"))
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^del_plan_"))
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^plan_detail_"))
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^cancel_op$"))
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^manage_user_")) 
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^user_reqhist_"))
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^list_user_subs_"))
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^confirm_del_user_")) 
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^reset_traffic_"))
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^set_strategy_"))
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^set_payimg_"))
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^reply_user_")) 
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^set_anomaly_"))
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^admin_orders_"))
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^admin_order_"))
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^panelcfg_"))
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^anomaly_whitelist_"))
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^anomaly_quick_"))
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^bulk_"))
    app.add_handler(CallbackQueryHandler(add_plan_start, pattern="^add_plan_start$"))
    app.add_handler(CallbackQueryHandler(client_menu_handler, pattern="^client_"))
    app.add_handler(CallbackQueryHandler(client_menu_handler, pattern="^selrenew_"))
    app.add_handler(CallbackQueryHandler(client_menu_handler, pattern="^order_"))
    app.add_handler(CallbackQueryHandler(client_menu_handler, pattern="^paymethod_"))
    app.add_handler(CallbackQueryHandler(client_menu_handler, pattern="^cancel_order"))
    app.add_handler(CallbackQueryHandler(client_menu_handler, pattern="^back_home$"))
    app.add_handler(CallbackQueryHandler(client_menu_handler, pattern="^contact_support$"))
    app.add_handler(CallbackQueryHandler(client_menu_handler, pattern="^client_nodes$"))
    app.add_handler(CallbackQueryHandler(client_menu_handler, pattern="^view_sub_"))
    app.add_handler(CallbackQueryHandler(process_order, pattern="^(ap|rj|rt)_"))
    app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_message))
    
    app.job_queue.run_daily(check_expiry_job, time=datetime.time(hour=12, minute=0, second=0))
    app.job_queue.run_repeating(check_anomalies_job, interval=3600, first=60, name='check_anomalies_job')
    
    try:
        val_int = db_query("SELECT value FROM settings WHERE key='anomaly_interval'", one=True)
        if val_int:
            interval_sec = float(val_int['value']) * 3600
            if interval_sec > 0:
                loop = asyncio.get_event_loop()
                loop.create_task(reschedule_anomaly_job(app, val_int['value']))
    except Exception as exc:
        logger.warning("Failed to reschedule anomaly job at startup: %s", exc)

    print(f"🚀 RemnaShop-Pro V2.8 已启动 | 监听中...")
    try:
        app.run_polling()
    finally:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(close_all_clients())
        finally:
            loop.close()
