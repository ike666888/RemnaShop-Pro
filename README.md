# 🚀 RemnaShop-Pro

当前版本：`V3.6`

RemnaShop-Pro 是一个面向 **Remnawave 面板** 的 Telegram 机器人，提供订阅售卖、续费、状态查询与基础运维能力。

---

## 功能

### 用户端
- 购买新订阅（选择套餐并提交付款信息）。
- 我的订阅 / 续费。
- 订阅详情查看（到期时间、状态、流量使用）。
- 订阅链接二维码生成。
- 节点状态查询。
- 联系客服。

### 管理端
- 套餐管理（新增、查看、删除）。
- 用户列表与订阅管理（查看、删除、重置流量、重置策略）。
- 订单审核（通过 / 拒绝）。
- 到期提醒天数设置。
- 过期清理天数设置。
- 异常检测阈值与检测周期设置。

---

## 运行环境

- Debian / Ubuntu（推荐）
- Python 3.9+
- 已部署 Remnawave 面板
- Telegram Bot Token
- Telegram 管理员 ID

---

## 安装

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/ike666888/RemnaShop-Pro/main/install.sh)
```

安装脚本功能：
1. 安装 Python 与依赖包。
2. 同步项目代码到 `/opt/RemnaShop`。
3. 首次创建 `config.json`。
4. 创建并启动 `remnashop.service`。

---

## Docker Compose 部署

### 1) 准备配置目录

```bash
mkdir -p data
```

可选两种方式：

- **方式 A（推荐）**：手动创建 `data/config.json`。
- **方式 B**：不提供 `config.json`，在 `docker-compose.yml` 里填写 `ADMIN_ID`、`BOT_TOKEN` 等环境变量，容器首次启动会自动生成配置。

示例 `data/config.json`：

```json
{
  "admin_id": "123456789",
  "bot_token": "123456:ABCDEF",
  "panel_url": "https://panel.example.com",
  "panel_token": "your_panel_api_token",
  "sub_domain": "https://sub.example.com",
  "group_uuid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "panel_verify_tls": true
}
```

### 2) 启动

```bash
docker compose up -d --build
```

### 3) 查看日志

```bash
docker compose logs -f remnashop
```

### 4) 停止

```bash
docker compose down
```

> 数据持久化说明：`./data` 会映射到容器 `/data`，其中 `config.json` 与 `starlight.db` 会持久保存。

---

## 配置文件

配置文件路径：`/opt/RemnaShop/config.json`

示例：

```json
{
  "admin_id": "123456789",
  "bot_token": "123456:ABCDEF",
  "panel_url": "https://panel.example.com",
  "panel_token": "your_panel_api_token",
  "sub_domain": "https://sub.example.com",
  "group_uuid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
}
```

---

## 项目结构

- `bot.py`：主程序入口
- `install.sh`：安装/更新/卸载脚本
- `handlers/`：消息与回调处理辅助代码
- `services/`：面板 API、订单相关服务代码
- `storage/`：数据库初始化与访问辅助代码
- `jobs/`：定时任务辅助代码
- `utils/`：通用工具函数

---

## 常用命令

## 🔧 运维命令
```bash
# 查看日志
journalctl -u remnashop -f

# 重启服务
systemctl restart remnashop

# 停止服务
systemctl stop remnashop

# 查看服务状态
systemctl status remnashop
```

---

## 联系

- 作者：ike
- 群组：https://t.me/Remnawarecn

---

本项目仅供学习交流使用，请遵守当地法律法规。
