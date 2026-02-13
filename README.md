# 🚀 RemnaShop-Pro

RemnaShop-Pro 是一个面向 **Remnawave Panel** 的 Telegram 销售与运维机器人，支持支付审核、自动发货、异常风控和批量运营。

---

## ✨ 主要功能

### 👤 用户端
- 套餐购买 / 续费下单
- 订阅信息查看（流量、到期、订阅链接、二维码）
- 节点状态查看
- 客服消息转发

### 👮 管理端
- 套餐管理（增删 + 流量策略）
- 用户管理（单用户重置流量、删除、请求历史）
- 订单审计（pending/approved/rejected/delivered/failed）
- 失败订单重试发货
- 异常检测（可解释告警：风险评分 + 证据）
- 异常白名单管理
- **批量用户操作（Bulk）**
  - 批量重置流量
  - 批量禁用
  - 批量删除
  - 批量修改到期日
  - 批量修改流量包

### 🧱 架构（已拆分）
- `bot.py`：入口和主路由
- `services/panel_api.py`：Panel API 请求、重试、连接复用
- `services/orders.py`：订单状态机、失败原因分类、审计写入
- `storage/db.py`：SQLite 初始化与访问
- `handlers/bulk_actions.py`：批量操作解析与执行
- `handlers/admin.py` / `handlers/client.py`：管理端、用户端可复用渲染逻辑
- `jobs/anomaly.py` / `jobs/expiry.py`：异常检测与到期策略辅助逻辑
- `utils/formatting.py`：MarkdownV2 转义

---

## 🛠 环境要求
- Debian / Ubuntu VPS
- Python 3.9+
- 可访问 Remnawave Panel API
- Telegram Bot Token

---

## 🚀 一键安装 / 更新

```bash
bash <(curl -sL https://raw.githubusercontent.com/ike666888/RemnaShop-Pro/main/install.sh)
```

安装脚本会：
1. 安装 Python / pip 依赖
2. 同步 `bot.py` 与 `services/ storage/ handlers/ jobs/ utils/` 代码
3. 首次生成 `config.json`
4. 创建并启动 `systemd` 服务 `remnashop`

---

## ⚙️ 配置文件
默认路径：`/opt/RemnaShop/config.json`

关键字段：
- `admin_id`
- `bot_token`
- `panel_url`
- `panel_token`
- `sub_domain`
- `group_uuid`
- `panel_verify_tls`（默认 true）

---

## 🔧 运维命令
```bash
journalctl -u remnashop -f
systemctl restart remnashop
systemctl status remnashop
```

---

## 📞 联系与支持
* **作者**：ike
* **交流群组**：[点击加入 Remnawave 中文交流群](https://t.me/Remnawarecn)

---

*本项目仅供学习交流使用，请遵守当地法律法规。*
