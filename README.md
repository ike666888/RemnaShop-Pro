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
1.  一台连接互联网的 **VPS** (Debian/Ubuntu 推荐)。
2.  已部署好的 **Remnawave 面板**。
3.  **Remnawave API Token** (在面板设置中获取)。
4.  **Telegram Bot Token** (通过 @BotFather 获取)。
5.  **Telegram Admin ID** (您的 TG 用户 ID，通过 @userinfobot 获取)。

---

## 🚀 一键安装 / 更新 (Installation)

我们提供了一键全自动化脚本，自动处理环境依赖（Python3, pip, Systemd守护进程）。
**支持 安装、无损升级、卸载。**

请在 VPS 终端执行以下命令：

```bash
bash <(curl -sL [https://raw.githubusercontent.com/ike666888/RemnaShop-Pro/main/install.sh](https://raw.githubusercontent.com/ike666888/RemnaShop-Pro/main/install.sh))
