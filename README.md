# 🚀 RemnaShop-Pro

RemnaShop-Pro 是面向 **Remnawave Panel** 的 Telegram 销售与运维机器人。

当前版本已支持「下单审核 + 自动发货 + 到期运维 + 异常检测 + 客服转发」一体化流程，并引入持久化订单状态机，避免重启丢单与重复发货。

---

## ✨ 功能总览

### 👤 用户侧
- **购买新订阅**：选择套餐后提交支付口令，进入管理员审核流程。
- **我的订阅**：查看剩余流量、到期时间、订阅链接与二维码。
- **续费订阅**：支持选择续费套餐，按策略叠加时长/流量。
- **节点状态**：查看节点在线/离线状态。
- **联系客服**：支持文字/图片/文件转发到管理员。

### 👮 管理员侧
- **套餐管理**：新增、删除套餐，支持流量重置策略（NO_RESET / DAY / WEEK / MONTH）。
- **用户管理**：查看用户订阅、重置流量、删除用户。
- **提醒与清理设置**：配置到期提醒天数与过期清理天数。
- **异常检测设置**：配置周期与 IP 阈值，异常自动禁用并告警。
- **客服回复**：在机器人内直接回复用户消息。

### 🧾 订单状态机（已实现）
订单存储在 SQLite `orders` 表，核心状态：
- `pending`：待审核
- `approved`：审核通过，处理中
- `rejected`：审核拒绝
- `delivered`：已发货
- `failed`：发货失败

> 具备幂等保护：管理员重复点击“通过”不会重复发货。

---

## 🛠️ 环境要求

请确保具备以下条件：
1. Debian/Ubuntu VPS（可访问公网）
2. 已部署 Remnawave Panel
3. Panel API Token
4. Telegram Bot Token（@BotFather）
5. Telegram 管理员 ID（@userinfobot）

---

## 🚀 安装 / 更新

执行一键脚本：

```bash
bash <(curl -sL https://raw.githubusercontent.com/ike666888/RemnaShop-Pro/main/install.sh)
```

### 安装步骤
1. 选择 `1. 🛠 安装 / 更新`。
2. 脚本自动安装 Python 与依赖。
3. 脚本会同步以下代码文件：
   - `bot.py`
   - `services/orders.py`
   - `services/panel_api.py`
   - `storage/db.py`
   - `utils/formatting.py`
4. 首次安装会提示填写：
   - 管理员 TG ID
   - 机器人 Token
   - 面板地址
   - 面板 API Token
   - 订阅域名
   - 默认用户组 UUID
   - 是否校验面板 HTTPS 证书（`panel_verify_tls`，默认推荐开启）
5. 安装完成后自动配置 systemd 并启动。

---

## ⚙️ 运行目录

- 程序目录：`/opt/RemnaShop`
- 配置文件：`/opt/RemnaShop/config.json`
- 数据库：`/opt/RemnaShop/starlight.db`
- 服务名：`remnashop.service`

---

## 🔧 常用运维命令

```bash
# 查看日志
journalctl -u remnashop -f

# 重启
systemctl restart remnashop

# 停止
systemctl stop remnashop

# 启动
systemctl start remnashop
```

---

## 📞 联系与支持
* **作者**：ike
* **交流群组**：[点击加入 Remnawave 中文交流群](https://t.me/Remnawarecn)

---

*本项目仅供学习交流使用，请遵守当地法律法规。*
