# 🚀 RemnaShop-Pro

当前版本：`V3.5`

RemnaShop-Pro 是一个面向 **Remnawave 面板** 的 Telegram 订阅售卖与管理机器人。
当前仓库主程序仍为单文件 `bot.py`（已可直接部署运行），并包含一组后续重构用的模块目录（`services/`、`storage/`、`handlers/`、`jobs/`、`utils/`）。

---

## 功能概览

### 用户端
- 购买新订阅（选择套餐后提交付款信息）。
- 查看我的订阅/续费。
- 展示订阅链接二维码。
- 查看节点在线状态。
- 联系客服（向管理员发送消息）。

### 管理端
- 套餐管理（增删套餐、查看详情）。
- 用户与订阅管理（查看、删除、重置流量、修改重置策略）。
- 到期提醒与自动清理设置。
- 异常检测阈值与检测周期设置。

---

## 环境要求

- Debian / Ubuntu VPS（推荐）。
- Python 3.9+。
- 可访问的 Remnawave 面板。
- Telegram Bot Token（@BotFather）。
- 管理员 Telegram ID。

---

## 一键安装 / 更新

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/ike666888/RemnaShop-Pro/main/install.sh)
```

安装脚本会：
1. 安装 Python 与依赖。
2. 拉取 `bot.py` 到 `/opt/RemnaShop`。
3. 首次生成 `config.json`。
4. 创建并启动 `remnashop.service`（systemd）。

---

## 配置文件

路径：`/opt/RemnaShop/config.json`

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

## 目录说明（按当前仓库实际）

- `bot.py`：当前生产主程序入口。
- `install.sh`：安装/更新/卸载脚本。
- `services/`、`storage/`、`handlers/`、`jobs/`、`utils/`：预留与重构相关代码目录（当前主流程主要在 `bot.py` 中）。
- `CODE_REVIEW.md`：历史代码审查记录。

---

## 常用运维命令

## 🔧 运维命令
```bash
# 查看日志
journalctl -u remnashop -f

# 重启
systemctl restart remnashop

# 停止
systemctl stop remnashop

# 开机自启状态
systemctl status remnashop
```

---

## 联系与支持

- 作者：ike
- 交流群组：https://t.me/Remnawarecn

---

本项目仅供学习交流使用，请遵守当地法律法规。
