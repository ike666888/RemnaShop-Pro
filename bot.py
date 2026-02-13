import logging
import time
import datetime
import json
import os
import asyncio
import qrcode
from io import BytesIO
from collections import defaultdict
from services.panel_api import safe_api_request as api_safe_request, get_panel_user as api_get_panel_user, get_nodes_status as api_get_nodes_status, get_subscription_history_stats as api_get_subscription_history_stats, get_user_subscription_history as api_get_user_subscription_history, close_all_clients, extract_payload
from services.orders import (
    create_order,
    get_order,
    update_order_status,
    attach_payment_text,
    attach_admin_message,
    get_pending_order_for_user,
    STATUS_PENDING,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_DELIVERED,
    STATUS_FAILED,
)
from storage.db import init_db as storage_init_db, db_query as storage_db_query, db_execute as storage_db_execute
from utils.formatting import escape_markdown_v2
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
PANEL_URL = config['panel_url'].rstrip('/') + '/api'
PANEL_TOKEN = config['panel_token']
SUB_DOMAIN = config['sub_domain'].rstrip('/')
TARGET_GROUP_UUID = config['group_uuid']
PANEL_VERIFY_TLS = parse_bool(config.get('panel_verify_tls', True), default=True)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

user_cooldowns = {}
COOLDOWN_SECONDS = 1.0
uuid_map = {}

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


init_db()


def get_headers():
    return {"Authorization": f"Bearer {PANEL_TOKEN}", "Content-Type": "application/json"}


async def safe_api_request(method, endpoint, json_data=None):
    return await api_safe_request(method, endpoint, PANEL_URL, get_headers(), PANEL_VERIFY_TLS, json_data=json_data)


async def get_panel_user(uuid):
    return await api_get_panel_user(uuid, PANEL_URL, get_headers(), PANEL_VERIFY_TLS)


async def get_nodes_status():
    return await api_get_nodes_status(PANEL_URL, get_headers(), PANEL_VERIFY_TLS)

async def get_subscription_history_stats():
    return await api_get_subscription_history_stats(PANEL_URL, get_headers(), PANEL_VERIFY_TLS)


async def get_user_subscription_history(uuid):
    return await api_get_user_subscription_history(uuid, PANEL_URL, get_headers(), PANEL_VERIFY_TLS)


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
        msg_text = (f"👮‍♂️ **管理员控制台**\n🔔 提醒设置：提前 {notify_days} 天\n🗑 清理设置：过期 {cleanup_days} 天")
        keyboard = [
            [InlineKeyboardButton("📦 套餐管理", callback_data="admin_plans_list")],
            [InlineKeyboardButton("👥 用户列表", callback_data="admin_users_list")],
            [InlineKeyboardButton("🔔 提醒设置", callback_data="admin_notify"), InlineKeyboardButton("🗑 清理设置", callback_data="admin_cleanup")],
            [InlineKeyboardButton("🛡️ 异常设置", callback_data="admin_anomaly_menu")],
            [InlineKeyboardButton("🧾 订单审计", callback_data="admin_orders_menu")]
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
                await handle_order_confirmation(update, context, original_plan_key, 'renew', short_id)
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
        
        await handle_order_confirmation(update, context, plan_key, order_type, short_id)

    elif data == "cancel_order":
        pending = get_pending_order_for_user(db_query, user_id)
        if pending:
            update_order_status(db_execute, pending['order_id'], [STATUS_PENDING], STATUS_REJECTED, error_message='cancelled_by_user')
        await start(update, context)

async def handle_order_confirmation(update, context, plan_key, order_type, short_id):
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



async def show_orders_menu(update, context, status_filter=None):
    if status_filter:
        rows = db_query("SELECT * FROM orders WHERE status = ? ORDER BY created_at DESC LIMIT 20", (status_filter,))
        title = f"🧾 **订单审计 - {status_filter}**"
    else:
        rows = db_query("SELECT * FROM orders ORDER BY created_at DESC LIMIT 20")
        title = "🧾 **订单审计 - 最近20条**"

    keyboard = []
    for row in rows:
        item = dict(row)
        ts = datetime.datetime.fromtimestamp(int(item['created_at'])).strftime('%m-%d %H:%M')
        keyboard.append([
            InlineKeyboardButton(
                f"{item['status']} | {item['order_id']} | {item['tg_id']} | {ts}",
                callback_data=f"admin_order_{item['order_id']}",
            )
        ])

    keyboard.append([
        InlineKeyboardButton("pending", callback_data="admin_orders_status_pending"),
        InlineKeyboardButton("delivered", callback_data="admin_orders_status_delivered"),
    ])
    keyboard.append([
        InlineKeyboardButton("failed", callback_data="admin_orders_status_failed"),
        InlineKeyboardButton("all", callback_data="admin_orders_menu"),
    ])
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
    if data == "admin_orders_menu":
        await show_orders_menu(update, context)
        return
    if data.startswith("admin_orders_status_"):
        status_filter = data.replace("admin_orders_status_", "")
        await show_orders_menu(update, context, status_filter=status_filter)
        return
    if data.startswith("admin_order_"):
        order_id = data.replace("admin_order_", "")
        order = db_query("SELECT * FROM orders WHERE order_id = ?", (order_id,), one=True)
        if not order:
            await send_or_edit_menu(update, context, "⚠️ 订单不存在", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_orders_menu")]]))
            return
        item = dict(order)
        created = datetime.datetime.fromtimestamp(int(item['created_at'])).strftime('%Y-%m-%d %H:%M')
        txt = (
            f"🧾 **订单详情**\n\n"
            f"ID: `{item['order_id']}`\n"
            f"用户: `{item['tg_id']}`\n"
            f"状态: `{item['status']}`\n"
            f"类型: `{item['order_type']}`\n"
            f"套餐: `{item['plan_key']}`\n"
            f"创建: `{created}`"
        )
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
            "检测到异常会自动禁用账号并通知您。"
        )
        kb = [[InlineKeyboardButton("⏱️ 设置周期", callback_data="set_anomaly_interval"), InlineKeyboardButton("🔢 设置阈值", callback_data="set_anomaly_threshold")],[InlineKeyboardButton("📋 白名单", callback_data="anomaly_whitelist_menu")],[InlineKeyboardButton("🔙 返回", callback_data="back_home")]]
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
    if pending_order and text:
        plan = db_query("SELECT * FROM plans WHERE key = ?", (pending_order['plan_key'],), one=True)
        if not plan:
            await update.message.reply_text("❌ 当前订单关联套餐已删除，请重新下单。")
            update_order_status(db_execute, pending_order['order_id'], [STATUS_PENDING], STATUS_FAILED, error_message='plan_deleted')
            return
        t_str = "续费" if pending_order['order_type'] == 'renew' else "新购"
        escaped_text = escape_markdown_v2(text)
        admin_msg = (
            f"*💰 审核 {escape_markdown_v2(t_str)}*\n"
            f"👤 用户ID: `{user_id}`\n"
            f"📦 套餐: `{escape_markdown_v2(dict(plan)['name'])}`\n"
            f"📝 口令: `{escaped_text}`"
        )
        target_uuid = pending_order['target_uuid'] if pending_order['target_uuid'] else "0"
        sid = get_short_id(target_uuid) if target_uuid != "0" else "0"
        kb = [[InlineKeyboardButton("✅ 通过", callback_data=f"ap_{pending_order['order_id']}_{sid}")], [InlineKeyboardButton("❌ 拒绝", callback_data=f"rj_{pending_order['order_id']}")]]
        admin_message = await context.bot.send_message(
            ADMIN_ID,
            admin_msg,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode='MarkdownV2',
        )
        msg_obj = await update.message.reply_text(
            "✅ 已提交，等待管理员确认。",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回主菜单", callback_data="back_home")]]),
        )
        attach_admin_message(db_execute, pending_order['order_id'], admin_message.message_id)
        attach_payment_text(db_execute, pending_order['order_id'], text, waiting_message_id=msg_obj.message_id)

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
        update_order_status(db_execute, order_id, [STATUS_APPROVED], STATUS_FAILED, error_message='plan_deleted')
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
                update_order_status(db_execute, order_id, [STATUS_APPROVED], STATUS_FAILED, error_message='missing_target_uuid')
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
                update_order_status(db_execute, order_id, [STATUS_APPROVED], STATUS_FAILED, error_message='panel_api_error_renew')
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
                update_order_status(db_execute, order_id, [STATUS_APPROVED], STATUS_FAILED, error_message='panel_api_error_new')
                await query.edit_message_text("❌ 失败", reply_markup=admin_return_btn)
    except Exception as exc:
        logger.exception("Order processing failed for %s", order_id)
        update_order_status(db_execute, order_id, [STATUS_APPROVED], STATUS_FAILED, error_message=str(exc)[:400])
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
                    can_send_by_daily_limit = (now_ts - last_notify_at) >= 20 * 3600
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

        user_ip_map = defaultdict(set)
        max_seen_ts = last_scan_ts
        for log in logs:
            row_ts = _extract_log_ts(log)
            if row_ts and row_ts <= last_scan_ts:
                continue
            if row_ts > max_seen_ts:
                max_seen_ts = row_ts
            uid = log.get('userUuid')
            ip = log.get('ip')
            if uid in whitelist:
                continue
            if uid and ip:
                user_ip_map[uid].add(ip)

        for uid, ips in user_ip_map.items():
            if len(ips) > limit:
                await safe_api_request('POST', f"/users/{uid}/actions/disable")
                try:
                    await context.bot.send_message(ADMIN_ID, f"🚨 **异常检测**\n\n用户 `{uid}` 使用了 {len(ips)} 个IP。\n已自动禁用。")
                except Exception as exc:
                    logger.warning("Failed to notify anomaly admin: %s", exc)

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
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^reply_user_")) 
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^set_anomaly_"))
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^admin_orders_"))
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^admin_order_"))
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^anomaly_whitelist_"))
    app.add_handler(CallbackQueryHandler(add_plan_start, pattern="^add_plan_start$"))
    app.add_handler(CallbackQueryHandler(client_menu_handler, pattern="^client_"))
    app.add_handler(CallbackQueryHandler(client_menu_handler, pattern="^selrenew_"))
    app.add_handler(CallbackQueryHandler(client_menu_handler, pattern="^order_"))
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

    print(f"🚀 RemnaShop-Pro V2.4 已启动 | 监听中...")
    try:
        app.run_polling()
    finally:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(close_all_clients())
        finally:
            loop.close()
