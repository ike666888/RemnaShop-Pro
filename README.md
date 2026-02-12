# 🚀 RemnaShop-Pro



**RemnaShop-Pro** 是专为 **Remnawave** 面板打造的专业级 Telegram 自动售卖与管理机器人。

它采用全异步架构设计，支持高并发处理，集成了流量可视化、二维码订阅、节点监控及自动化运维功能，旨在为您提供无需人工值守的商业级运营体验。



---



## ✨ 核心功能 (Features)



* **⚡️ 极致性能**：基于 `httpx` 全异步重构，消除阻塞，秒级响应。

* **📊 可视化交互**：

* 流量使用情况显示为进度条 `[████░░] 60%`。

* 订阅链接自动生成 **二维码图片**，手机扫码即连。

* **🤖 自动化运维**：

* **到期提醒**：自定义天数，自动发送续费通知。

* **自动清理**：自动检测并删除过期超过指定天数的僵尸用户。

* **🛡️ 安全机制**：内置防抖动（Anti-Flood）限流，保护面板 API 不被滥用。

* **🌍 节点监控**：用户端可实时查询所有节点的在线/离线状态。

* **👮‍♂️ 完善的管理端**：通过 TG 机器人即可完成套餐管理、用户查询、删除、策略配置等操作。



---



## 🛠️ 环境要求 (Prerequisites)



在部署之前，请确保您拥有：

1. 一台连接互联网的 **VPS** (Debian/Ubuntu 推荐)。

2. 已部署好的 **Remnawave 面板**。

3. **Remnawave API Token** (在面板设置中获取)。

4. **Telegram Bot Token** (通过 @BotFather 获取)。

5. **Telegram Admin ID** (您的 TG 用户 ID，通过 @userinfobot 获取)。



---



## 🚀 一键安装 / 更新 (Installation)



我们提供了一键全自动化脚本，自动处理环境依赖（Python3, pip, Systemd守护进程）。

**支持 安装、无损升级、卸载。**



请在 VPS 终端执行以下命令：



```bash

bash <(curl -sL https://raw.githubusercontent.com/ike666888/RemnaShop-Pro/main/install.sh)

```

### 安装步骤说明：

1. 运行脚本后，选择 `1. 🛠 安装 / 更新`。

2. 脚本会自动安装 `python3`、`pip` 及所需依赖库。

> 更新说明：新版会同步 `bot.py` 以及 `services/`、`storage/`、`utils/` 模块文件，避免只更新单文件导致启动失败。

3. **首次安装**会依次询问以下配置信息，请按提示输入：

* 管理员 TG ID

* 机器人 Token

* 面板地址 (例如 `https://panel.example.com`)

* 面板 API Token

* 订阅域名

* 默认用户组 UUID
* （可选）`panel_verify_tls`：是否校验面板 HTTPS 证书（默认开启，建议保持 `true`）

4. 安装完成后，机器人会自动启动并设置为开机自启。



---



## 📖 使用指南 (Usage)



### 👮‍♂️ 管理员指令

* `/start` - 唤出管理控制台。

* **📦 套餐管理**：添加、删除售卖套餐，设置流量重置策略。

* **👥 用户列表**：查看最近订阅用户，支持一键删除。

* **🔔 提醒设置**：配置到期前第几天发送提醒。

* **🗑 清理设置**：配置过期后第几天自动删除用户。



### 👤 用户端功能

* **🛒 购买订阅**：选择套餐 -> 发送口令红包 -> 等待审核 -> 自动发货。

* **🔍 我的订阅**：查看流量进度条、到期时间，获取订阅链接及二维码。

* **🌍 节点状态**：查看节点存活情况。

* **🆘 联系客服**：向管理员发送消息（支持图文），管理员可直接回复。



---




### 🧾 订单状态机（新增）

当前版本已引入持久化订单状态机（SQLite `orders` 表），核心状态包括：

- `pending`（待审核）
- `approved`（审核通过，发货处理中）
- `rejected`（审核拒绝）
- `delivered`（已发货）
- `failed`（发货失败）

管理员重复点击“通过”不会重复发货（幂等保护）。

## ⚙️ 目录结构



* **程序目录**：`/opt/RemnaShop`

* **配置文件**：`/opt/RemnaShop/config.json` (自动生成)

* **数据库**：`/opt/RemnaShop/starlight.db` (SQLite)

* **服务名称**：`remnashop.service`



### 常用维护命令



```bash

# 查看运行日志

journalctl -u remnashop -f



# 重启机器人

systemctl restart remnashop



# 停止机器人

systemctl stop remnashop

```
## 📞 联系与支持
* **作者**：ike

* **交流群组**：[点击加入 Remnawave 中文交流群](https://t.me/Remnawarecn)



---

*本项目仅供学习交流使用，请遵守当地法律法规。*
