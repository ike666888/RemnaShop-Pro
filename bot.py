import logging
import httpx
import time
import datetime
import json
import os
import sqlite3
import asyncio
import qrcode
import uuid
from io import BytesIO
from collections import defaultdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
DB_FILE = os.path.join(BASE_DIR, 'starlight.db')

ANOMALY_IP_THRESHOLD = 50

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
    except: return iso_str

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
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS plans (key TEXT PRIMARY KEY, name TEXT, price TEXT, days INTEGER, gb INTEGER, reset_strategy TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, uuid TEXT, created_at TIMESTAMP)''')
    try: c.execute("ALTER TABLE subscriptions ADD COLUMN plan_key TEXT")
    except: pass
    
    c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    try: c.execute("ALTER TABLE plans ADD COLUMN reset_strategy TEXT DEFAULT 'NO_RESET'")
    except: pass
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('notify_days', '3')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('cleanup_days', '7')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('anomaly_interval', '1')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('anomaly_threshold', '50')")
    c.execute("SELECT count(*) FROM plans")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO plans VALUES (?, ?, ?, ?, ?, ?)", ('p1', '1个月', '200元', 30, 100, 'NO_RESET'))
        c.execute("INSERT INTO plans VALUES (?, ?, ?, ?, ?, ?)", ('p2', '3个月', '580元', 90, 500, 'NO_RESET'))
    conn.commit()
    conn.close()

def db_query(query, args=(), one=False):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(query, args)
    rv = cur.fetchall()
    conn.commit()
    conn.close()
    return (rv[0] if rv else None) if one else rv

def db_execute(query, args=()):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(query, args)
    conn.commit()
    conn.close()

init_db()
temp_orders = {}

def get_headers():
    return {"Authorization": f"Bearer {PANEL_TOKEN}", "Content-Type": "application/json"}

async def safe_api_request(method, endpoint, json_data=None):
    url = f"{PANEL_URL}{endpoint}"
    try:
        async with httpx.AsyncClient(timeout=20.0, verify=False) as client:
            if method == 'GET':
                resp = await client.get(url, headers=get_headers())
            elif method == 'POST':
                resp = await client.post(url, json=json_data, headers=get_headers())
            elif method == 'PATCH':
                resp = await client.patch(url, json=json_data, headers=get_headers())
            elif method == 'DELETE':
                resp = await client.delete(url, headers=get_headers())
            return resp
    except Exception as e:
        logger.error(f"API Error [{method} {endpoint}]: {e}")
        return None

async def get_panel_user(uuid):
    resp = await safe_api_request('GET', f"/users/{uuid}")
    if resp and resp.status_code == 200:
        return resp.json().get('response', resp.json())
    return None

async def get_nodes_status():
    resp = await safe_api_request('GET', '/nodes')
    if resp and resp.status_code == 200:
        data = resp.json()
        return data.get('response', data.get('data', []))
    return []

async def send_or_edit_menu(update, context, text, reply_markup):
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode='Markdown')
        except Exception:
            try: await update.callback_query.delete_message()
            except: pass
            await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup, parse_mode='Markdown')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.effective_user.id in temp_orders: del temp_orders[update.effective_user.id]
    user_id = update.effective_user.id
    if user_id == ADMIN_ID:
        try:
            val_notify = db_query("SELECT value FROM settings WHERE key='notify_days'", one=True)
            notify_days = int(val_notify['value']) if val_notify else 3
            val_cleanup = db_query("SELECT value FROM settings WHERE key='cleanup_days'", one=True)
            cleanup_days = int(val_cleanup['value']) if val_cleanup else 7
        except: notify_days = 3; cleanup_days = 7
        msg_text = (f"👮‍♂️ **管理员控制台**\n🔔 提醒设置：提前 {notify_days} 天\n🗑 清理设置：过期 {cleanup_days} 天")
        keyboard = [
            [InlineKeyboardButton("📦 套餐管理", callback_data="admin_plans_list")],
            [InlineKeyboardButton("👥 用户列表", callback_data="admin_users_list")],
            [InlineKeyboardButton("🔔 提醒设置", callback_data="admin_notify"), InlineKeyboardButton("🗑 清理设置", callback_data="admin_cleanup")],
            [InlineKeyboardButton("🛡️ 异常设置", callback_data="admin_anomaly_menu")]
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
        except: pass
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
        except: pass
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
        except: pass
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
        if user_id in temp_orders: del temp_orders[user_id]
        await start(update, context)

async def handle_order_confirmation(update, context, plan_key, order_type, short_id):
    user_id = update.effective_user.id
    target_uuid = get_real_uuid(short_id) if short_id != "0" else "0"
    
    plan = db_query("SELECT * FROM plans WHERE key = ?", (plan_key,), one=True)
    if not plan: return
    
    plan_dict = dict(plan)
    strategy = plan_dict.get('reset_strategy', 'NO_RESET')
    strategy_label = get_strategy_label(strategy)
    
    msg_id = None
    if update.callback_query and update.callback_query.message:
        msg_id = update.callback_query.message.message_id
    
    temp_orders[user_id] = {
        "plan": plan_key, 
        "type": order_type, 
        "target_uuid": target_uuid,
        "menu_msg_id": msg_id
    }
    
    type_str = "续费" if order_type == 'renew' else "新购"
    back_data = f"view_sub_{short_id}" if order_type == 'renew' else "client_buy_new"
    
    keyboard = [[InlineKeyboardButton("❌ 取消订单", callback_data="cancel_order")], [InlineKeyboardButton("🔙 返回", callback_data=back_data)]]
    msg = (f"📝 **订单确认 ({type_str})**\n📦 套餐：{plan_dict['name']}\n💰 金额：**{plan_dict['price']}**\n📡 流量：**{plan_dict['gb']} GB ({strategy_label})**\n\n💳 **下一步：**\n请在此直接发送 **支付宝口令红包** (文字) 给机器人。\n👇 👇 👇")
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
        except: s_text = '总流量'
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
        keyboard = [[InlineKeyboardButton("🔄 重置流量", callback_data=f"reset_traffic_{target_uuid}")], [InlineKeyboardButton("🗑 确认删除用户", callback_data=f"confirm_del_user_{target_uuid}")], [InlineKeyboardButton("🔙 返回列表", callback_data=f"list_user_subs_{dict(sub)['tg_id']}")]]
        await send_or_edit_menu(update, context, msg, InlineKeyboardMarkup(keyboard))
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
        except: day = 3
        kb = [[InlineKeyboardButton("🔙 取消", callback_data="cancel_op")]]
        await send_or_edit_menu(update, context, f"🔔 **提醒设置**\n当前：到期前 {day} 天发送提醒\n\n**⬇️ 请回复新的天数（纯数字）：**", InlineKeyboardMarkup(kb))
        context.user_data['setting_notify'] = True
    elif data == "admin_cleanup":
        try:
            val = db_query("SELECT value FROM settings WHERE key='cleanup_days'", one=True)
            day = val['value'] if val else 7
        except: day = 7
        kb = [[InlineKeyboardButton("🔙 取消", callback_data="cancel_op")]]
        await send_or_edit_menu(update, context, f"🗑 **清理设置**\n当前：过期后 {day} 天自动删除\n(过期1天将只禁用)\n\n**⬇️ 请回复新的天数（纯数字）：**", InlineKeyboardMarkup(kb))
        context.user_data['setting_cleanup'] = True
    elif data == "admin_anomaly_menu":
        try:
            val_int = db_query("SELECT value FROM settings WHERE key='anomaly_interval'", one=True)
            interval = val_int['value'] if val_int else 1
            val_thr = db_query("SELECT value FROM settings WHERE key='anomaly_threshold'", one=True)
            threshold = val_thr['value'] if val_thr else 50
        except: interval=1; threshold=50
        msg = (f"🛡️ **异常检测设置**\n\n⏱️ 检测周期：每 {interval} 小时\n🔢 封禁阈值：单周期 > {threshold} 个IP\n\n检测到异常会自动禁用账号并通知您。")
        kb = [[InlineKeyboardButton("⏱️ 设置周期", callback_data="set_anomaly_interval"), InlineKeyboardButton("🔢 设置阈值", callback_data="set_anomaly_threshold")],[InlineKeyboardButton("🔙 返回", callback_data="back_home")]]
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
        except: await update.message.reply_text("❌ 请输入有效的数字 (例如 0.5 或 1)", reply_markup=cancel_kb)
        return
    if user_id == ADMIN_ID and context.user_data.get('setting_anomaly_threshold') and text:
        if text.isdigit():
            db_execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('anomaly_threshold', ?)", (text,))
            context.user_data['setting_anomaly_threshold'] = False
            await update.message.reply_text(f"✅ 阈值已更新：> {text} IP 封禁。", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_anomaly_menu")]]))
        else: await update.message.reply_text("❌ 请输入整数", reply_markup=cancel_kb)
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
    if user_id in temp_orders and text:
        order = temp_orders[user_id]
        plan = db_query("SELECT * FROM plans WHERE key = ?", (order['plan'],), one=True)
        t_str = "续费" if order['type'] == 'renew' else "新购"
        admin_msg = f"💰 **审核 {t_str}**\n👤 {update.effective_user.mention_html()} (`{user_id}`)\n📦 {dict(plan)['name']}\n📝 口令：<code>{text}</code>"
        safe_uuid = order['target_uuid'] if order['target_uuid'] else "0"
        sid = get_short_id(safe_uuid) if safe_uuid != "0" else "0"
        kb = [[InlineKeyboardButton("✅ 通过", callback_data=f"ap_{user_id}_{order['plan']}_{order['type']}_{sid}")], [InlineKeyboardButton("❌ 拒绝", callback_data=f"rj_{user_id}")]]
        await context.bot.send_message(ADMIN_ID, admin_msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
        msg_obj = await update.message.reply_text("✅ 已提交，等待管理员确认。", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回主菜单", callback_data="back_home")]]))
        temp_orders[user_id]['waiting_msg_id'] = msg_obj.message_id

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
    async def clean_user_waiting_msg(uid):
        if uid in temp_orders:
            if 'waiting_msg_id' in temp_orders[uid]:
                try: await context.bot.delete_message(chat_id=uid, message_id=temp_orders[uid]['waiting_msg_id'])
                except: pass
            if 'menu_msg_id' in temp_orders[uid]:
                try: await context.bot.delete_message(chat_id=uid, message_id=temp_orders[uid]['menu_msg_id'])
                except: pass
            del temp_orders[uid]
    if data.startswith("rj_"):
        uid = int(data.split("_")[1])
        await query.edit_message_text("❌ 已拒绝", reply_markup=admin_return_btn)
        await clean_user_waiting_msg(uid)
        try: await context.bot.send_message(uid, "❌ 您的订单已被管理员拒绝。", reply_markup=client_return_btn)
        except: pass
        return
    if data.startswith("ap_"):
        parts = data.split("_")
        uid = int(parts[1])
        plan_key = parts[2]
        order_type = parts[3]
        short_id = parts[4]
        target_uuid = get_real_uuid(short_id) if short_id != "0" else "0"
        plan = db_query("SELECT * FROM plans WHERE key = ?", (plan_key,), one=True)
        if not plan:
            await query.edit_message_text("❌ 套餐已删除", reply_markup=admin_return_btn)
            return
        await query.edit_message_text("🔄 处理中...")
        headers = get_headers()
        plan_dict = dict(plan)
        add_traffic = plan_dict['gb'] * 1024 * 1024 * 1024
        add_days = plan_dict['days']
        
        # 获取策略
        try: reset_strategy = plan_dict.get('reset_strategy', 'NO_RESET')
        except: reset_strategy = 'NO_RESET'
        strategy_label = get_strategy_label(reset_strategy) # 获取显示标签
        
        try:
            if order_type == 'renew':
                if not target_uuid:
                    await query.edit_message_text("⚠️ 订单数据已过期", reply_markup=admin_return_btn)
                    return
                user_info = await get_panel_user(target_uuid)
                if not user_info:
                    await query.edit_message_text("⚠️ 用户不存在", reply_markup=admin_return_btn)
                    return
                current_expire_str = user_info.get('expireAt', '').split('.')[0].replace('Z', '')
                now = datetime.datetime.utcnow()
                try: current_expire = datetime.datetime.strptime(current_expire_str, "%Y-%m-%dT%H:%M:%S")
                except: current_expire = now
                if current_expire > now: new_expire = current_expire + datetime.timedelta(days=add_days)
                else: new_expire = now + datetime.timedelta(days=add_days)
                expire_iso = new_expire.strftime("%Y-%m-%dT%H:%M:%SZ")
                new_limit = user_info.get('trafficLimitBytes', 0)
                if reset_strategy == 'NO_RESET': new_limit += add_traffic
                update_payload = {
                    "uuid": target_uuid, "trafficLimitBytes": new_limit, 
                    "expireAt": expire_iso, "status": "ACTIVE", "activeInternalSquads": [TARGET_GROUP_UUID],
                    "trafficLimitStrategy": reset_strategy
                }
                await safe_api_request('POST', f"/users/{target_uuid}/actions/enable")
                r = await safe_api_request('PATCH', "/users", json_data=update_payload)
                if r and r.status_code in [200, 204]:
                    await query.edit_message_text(f"✅ 续费成功\n用户: {uid}", reply_markup=admin_return_btn)
                    sub_url = user_info.get('subscriptionUrl', '')
                    display_expire = format_time(expire_iso)
                    display_traffic = round(new_limit/1024**3, 2)
                    # 🟢 修复：追加策略标记
                    msg = (f"🎉 **续费成功！**\n\n⏳ 新到期时间：`{display_expire}`\n📡 当前总流量：`{display_traffic} GB ({strategy_label})`\n\n🔗 订阅链接：\n`{sub_url}`")
                    await clean_user_waiting_msg(uid)
                    if sub_url and sub_url.startswith('http'):
                        qr = generate_qr(sub_url)
                        await context.bot.send_photo(uid, photo=qr, caption=msg, parse_mode='Markdown', reply_markup=client_return_btn)
                    else:
                        await context.bot.send_message(uid, msg, parse_mode='Markdown', reply_markup=client_return_btn)
                else:
                    await query.edit_message_text(f"❌ API报错", reply_markup=admin_return_btn)
            else:
                new_expire = datetime.datetime.utcnow() + datetime.timedelta(days=add_days)
                expire_iso = new_expire.strftime("%Y-%m-%dT%H:%M:%SZ")
                payload = {
                    "username": f"tg_{uid}_{int(time.time())}", 
                    "status": "ACTIVE", "trafficLimitBytes": add_traffic, "trafficLimitStrategy": reset_strategy,
                    "expireAt": expire_iso, "proxies": {}, "activeInternalSquads": [TARGET_GROUP_UUID]
                }
                r = await safe_api_request('POST', "/users", json_data=payload)
                if r and r.status_code in [200, 201]:
                    resp_data = r.json().get('response', r.json())
                    user_uuid = resp_data.get('uuid')
                    db_execute("INSERT INTO subscriptions (tg_id, uuid, created_at, plan_key) VALUES (?, ?, ?, ?)", 
                               (uid, user_uuid, int(time.time()), plan_key))
                    await query.edit_message_text(f"✅ 开通成功\n用户: {uid}", reply_markup=admin_return_btn)
                    sub_url = resp_data.get('subscriptionUrl', '')
                    display_expire = format_time(expire_iso)
                    # 🟢 修复：追加策略标记
                    msg = (f"🎉 **订阅开通成功！**\n\n📦 套餐：{plan_dict['name']}\n⏳ 到期时间：`{display_expire}`\n📡 包含流量：`{plan_dict['gb']} GB ({strategy_label})`\n\n🔗 订阅链接：\n`{sub_url}`")
                    await clean_user_waiting_msg(uid)
                    if sub_url and sub_url.startswith('http'):
                        qr = generate_qr(sub_url)
                        await context.bot.send_photo(uid, photo=qr, caption=msg, parse_mode='Markdown', reply_markup=client_return_btn)
                    else:
                        await context.bot.send_message(uid, msg, parse_mode='Markdown', reply_markup=client_return_btn)
                else:
                    await query.edit_message_text(f"❌ 失败", reply_markup=admin_return_btn)
        except Exception as e:
            await query.edit_message_text(f"❌ 错误: {e}", reply_markup=admin_return_btn)

async def check_expiry_job(context: ContextTypes.DEFAULT_TYPE):
    try: 
        val = db_query("SELECT value FROM settings WHERE key='notify_days'", one=True)
        notify_days = int(val['value']) if val else 3
        val_clean = db_query("SELECT value FROM settings WHERE key='cleanup_days'", one=True)
        cleanup_days = int(val_clean['value']) if val_clean else 7
    except: 
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
                    sid = get_short_id(u_dict['uuid'])
                    kb = [[InlineKeyboardButton("💳 立即续费", callback_data=f"selrenew_{sid}")]]
                    msg = f"⚠️ **续费提醒**\n\n您的订阅 (UUID: `{u_dict['uuid'][:8]}...`) \n将在 **{days_left}** 天后到期。\n请及时续费以免服务中断。"
                    try: await context.bot.send_message(u_dict['tg_id'], msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))
                    except: pass
                if days_left == -1 and info.get('status') == 'active':
                    await safe_api_request('POST', f"/users/{u_dict['uuid']}/actions/disable")
                if days_left < -cleanup_days:
                    to_delete_uuids.append(u_dict['uuid'])
                    db_execute("DELETE FROM subscriptions WHERE uuid = ?", (u_dict['uuid'],))
                    try: await context.bot.send_message(u_dict['tg_id'], f"🗑 您的订阅因过期超过 {cleanup_days} 天已被系统回收。")
                    except: pass
            except Exception as e: pass
    tasks = [check_single_sub(sub) for sub in subs]
    await asyncio.gather(*tasks)
    if to_delete_uuids:
        await safe_api_request('POST', '/users/bulk/delete', json_data={"uuids": to_delete_uuids})

async def check_anomalies_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        val_thr = db_query("SELECT value FROM settings WHERE key='anomaly_threshold'", one=True)
        limit = int(val_thr['value']) if val_thr else 50
        resp = await safe_api_request('GET', '/subscription-request-history')
        if not resp or resp.status_code != 200: return
        logs = resp.json().get('response', [])
        if not logs: return
        user_ip_map = defaultdict(set)
        for log in logs:
            uid = log.get('userUuid')
            ip = log.get('ip')
            if uid and ip: user_ip_map[uid].add(ip)
        for uid, ips in user_ip_map.items():
            if len(ips) > limit:
                await safe_api_request('POST', f"/users/{uid}/actions/disable")
                try: await context.bot.send_message(ADMIN_ID, f"🚨 **异常检测**\n\n用户 `{uid}` 使用了 {len(ips)} 个IP。\n已自动禁用。")
                except: pass
    except: pass

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
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^list_user_subs_"))
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^confirm_del_user_")) 
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^reset_traffic_"))
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^set_strategy_"))
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^reply_user_")) 
    app.add_handler(CallbackQueryHandler(admin_menu_handler, pattern="^set_anomaly_"))
    app.add_handler(CallbackQueryHandler(add_plan_start, pattern="^add_plan_start$"))
    app.add_handler(CallbackQueryHandler(client_menu_handler, pattern="^client_"))
    app.add_handler(CallbackQueryHandler(client_menu_handler, pattern="^selrenew_"))
    app.add_handler(CallbackQueryHandler(client_menu_handler, pattern="^order_"))
    app.add_handler(CallbackQueryHandler(client_menu_handler, pattern="^cancel_order"))
    app.add_handler(CallbackQueryHandler(client_menu_handler, pattern="^back_home$"))
    app.add_handler(CallbackQueryHandler(client_menu_handler, pattern="^contact_support$"))
    app.add_handler(CallbackQueryHandler(client_menu_handler, pattern="^client_nodes$"))
    app.add_handler(CallbackQueryHandler(client_menu_handler, pattern="^view_sub_"))
    app.add_handler(CallbackQueryHandler(process_order, pattern="^(ap|rj)_"))
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
    except: pass

    print(f"🚀 RemnaShop-Pro V3.5 已启动 | 监听中...")
    app.run_polling()
