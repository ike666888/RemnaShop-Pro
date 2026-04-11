# 🚀 RemnaShop-Pro

当前版本：`V3.6`

RemnaShop-Pro 是一个面向 **Remnawave 面板** 的 Telegram 机器人，提供订阅售卖、续费、状态查询与基础运维能力。

---

## 功能

### 用户端
- 购买新订阅（选择套餐并提交付款信息）
- 我的订阅 / 续费
- 订阅详情查看（到期时间、状态、流量使用）
- 订阅链接二维码生成
- 节点状态查询
- 联系客服

### 管理端
- 套餐管理（新增、查看、删除）
- 用户列表与订阅管理（查看、删除、重置流量、重置策略）
- 订单审核（通过 / 拒绝）
- 到期提醒天数设置
- 过期清理天数设置
- 异常检测阈值与检测周期设置

---

## 部署要求

> 本仓库 **仅支持 Docker Compose 部署**，不再支持 systemd / 纯 Python / 服务器裸装方式。

- Linux 服务器（推荐 Debian / Ubuntu）
- 网络可访问 GitHub 与 Docker 镜像仓库

---

## 一键安装（唯一推荐方式）

```bash
curl -fsSL https://raw.githubusercontent.com/ike666888/RemnaShop-Pro/main/bootstrap.sh | bash
```

该命令会执行仓库内 `bootstrap.sh`，自动完成：

1. 检查并安装 Docker（缺失时）
2. 检查并安装 Docker Compose（缺失时）
3. 克隆/更新仓库到 `/opt/remnashop-pro`
4. 基于 `.env.example` 准备 `.env`
5. 启动 Docker Compose 栈
6. 打印启动验证与健康检查结果

> `bootstrap.sh` 支持两种模式：
> - 交互菜单（直接执行 `bash bootstrap.sh`）
> - 非交互参数：`bash bootstrap.sh install` / `bash bootstrap.sh uninstall`

---

## 卸载（仅移除 RemnaShop-Pro 资源）

### 非交互卸载（服务器本地脚本）

```bash
cd /opt/remnashop-pro
bash bootstrap.sh uninstall
```

### 远程一行卸载（不经过菜单）

```bash
curl -fsSL https://raw.githubusercontent.com/ike666888/RemnaShop-Pro/main/bootstrap.sh | bash -s -- uninstall
```

卸载会**仅**清理以下 RemnaShop-Pro 资源：
- Compose 项目 `remnashop`
- 该项目创建的容器
- 该项目创建的本地镜像（`--rmi local`）
- 该项目创建的卷（`-v`）
- 项目目录 `/opt/remnashop-pro`

不会触碰其他 Compose 项目或无关 Docker 资源。

---

## 安装后验证（手动复核）

进入部署目录：

```bash
cd /opt/remnashop-pro
```

执行：

```bash
docker --version
docker compose version
test -f .env && echo ".env exists"
docker compose ps
docker compose logs --tail=100 remnashop
```

预期结果：
- `docker --version` 能输出版本号
- `docker compose version` 能输出版本号
- `.env exists` 输出成功
- `docker compose ps` 显示 `remnashop` 服务在运行中（初始化阶段可能显示 `starting`）
- 日志中无持续崩溃重启

---

## 日常运维（Docker Compose）

```bash
cd /opt/remnashop-pro
./docker-manage.sh ps
./docker-manage.sh logs
./docker-manage.sh restart
./docker-manage.sh down
```

---

## 环境变量说明

首次安装会自动从 `.env.example` 生成 `.env`（若不存在）。请至少配置：

- `ADMIN_ID`
- `BOT_TOKEN`

建议一并配置：

- `PANEL_URL`
- `PANEL_TOKEN`
- `SUB_DOMAIN`
- `GROUP_UUID`
- `PANEL_VERIFY_TLS`

---

## 迁移说明（旧版 standalone / server-install 用户）

旧版 `install.sh + systemd(remnashop.service)` 部署流已移除。

迁移步骤：

1. 备份旧机器中的 `config.json` 与 `starlight.db`
2. 执行新的一键安装命令（见上）
3. 将备份数据恢复到 Docker 数据卷（或容器 `/data`）
4. 使用 `docker compose ps` 与日志确认服务正常

> 若旧机器仍在运行 `remnashop.service`，请先停用旧服务再切换，避免重复实例同时运行。

---

## 项目结构

- `bot.py`：主程序入口
- `docker-compose.yml`：唯一部署编排入口
- `bootstrap.sh`：一键安装引导脚本（用于 curl | bash）
- `docker-manage.sh`：Docker Compose 运维助手
- `handlers/`：消息与回调处理辅助代码
- `services/`：面板 API、订单相关服务代码
- `storage/`：数据库初始化与访问辅助代码
- `jobs/`：定时任务辅助代码
- `utils/`：通用工具函数

---

## 联系

- 作者：ike
- 群组：https://t.me/Remnawarecn

---

本项目仅供学习交流使用，请遵守当地法律法规。
