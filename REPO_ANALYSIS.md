# RemnaShop-Pro × Remnawave API 联合改造分析（2026-04-11）

> 目标：基于 `docs.rw/api` 与 Remnawave backend contract（`libs/contract/api`）暴露的控制器集合，结合当前仓库实现，给出“可落地”的升级/改造清单。

---

## 1. API 盘点结论（高层）

从 Remnawave backend 的 contract 信息看，API 已覆盖以下大类能力：

- 用户与批量用户动作（`USERS` / `USERS_BULK_ACTIONS`）
- 订阅相关（公开订阅 + 受保护订阅 + 订阅设置 + 订阅请求历史）
- 节点与带宽统计（`NODES` / `BANDWIDTH_STATS` / `NODE_PLUGINS`）
- 分组管理（`INTERNAL_SQUADS` / `EXTERNAL_SQUADS`）
- 安全与接入（`API_TOKENS` / `PASSKEYS` / `IP_CONTROL`）
- 扩展能力（`METADATA` / `SNIPPETS` / `SUBSCRIPTION_PAGE_CONFIGS` / `SYSTEM` / `REMNAAWAVE_SETTINGS` 等）

你当前仓库只用了其中的一小部分（主要集中在 users、nodes、subscription-settings、history、internal-squads、bandwidth-stats）。整体属于“能跑但覆盖偏窄”。

---

## 2. 当前代码对 API 的使用覆盖（现状）

### 2.1 已接入能力（较成熟）

`services/panel_api.py` 已封装并调用了这些核心接口：

- `GET /users/{uuid}`
- `GET /users/by-telegram-id/{telegram_id}`
- `GET /nodes`
- `GET /subscription-request-history/stats`
- `GET /users/{uuid}/subscription-request-history`
- `GET/PATCH /subscription-settings`
- `GET /internal-squads`
- `GET /internal-squads/{uuid}/accessible-nodes`
- `GET /bandwidth-stats/nodes/realtime`
- `POST /users/bulk/update-squads`（含 `/users/bulk/update` fallback）

另外在 `bot.py` 内仍有大量直调 API 的逻辑（而不是统一走 `services/panel_api.py`），例如：

- `POST /users/{uuid}/actions/enable|disable|reset-traffic`
- `PATCH /users`
- `POST /users`
- `DELETE /users/{uuid}`
- `POST /users/bulk/delete`
- `GET /subscription-request-history`

这说明当前 API 调用层“部分集中、部分散落”，后续维护成本偏高。

---

## 3. 结合 API 能力的优先改造清单（和你原先技术债合并）

下面是按优先级排序的建议（P0 > P1 > P2）：

### P0（建议先做，直接提升稳定性和维护性）

1. **统一 API 访问层（收敛 `bot.py` 直调）**
   - 把 `bot.py` 里所有 `/users/*`、`/subscription-request-history` 等直调迁移到 `services/panel_api.py`。
   - 统一错误处理、重试、日志字段、响应解析，避免双实现偏差。
   - 这一步能同时解决你之前报告里的“`bot.py` 过大 + 可观测性弱 + 规则散落”。

2. **批量动作优先化（充分利用 `USERS_BULK_ACTIONS`）**
   - 现在代码对大量用户动作仍有循环单发请求（启用/禁用/限速/删除）。
   - 优先替换为 bulk 端点（你已接了 `update-squads`，思路正确），可显著减少 API 压力和 Telegram 回调耗时。

3. **接口版本兼容探测机制**
   - 当前只有 `update-squads -> update` 这一处 fallback。
   - 建议启动时做“能力探测缓存”（例如记录某些 endpoint 是否可用），避免每次失败后再回退。

### P1（中期，提升风控与运营能力）

4. **接入 `IP_CONTROL`，闭环异常检测**
   - 你已有 `jobs/anomaly.py` 风险识别，但处置动作仍偏基础（禁用/限流）。
   - 结合 IP 管理 API，可实现：高风险 IP 拉黑、临时封禁、解封自动化，形成“检测 -> 处置 -> 回滚”闭环。

5. **接入 `METADATA`，减少本地状态耦合**
   - 把 `tg_id`、订单号、风控标签、运营标签等映射信息逐步沉淀到远端 metadata。
   - 好处：跨实例迁移更轻松，减少仅靠本地 sqlite 维护映射的风险。

6. **接入 `SYSTEM/REMNAAWAVE_SETTINGS` 做健康检查**
   - 启动时拉取版本/关键配置，输出兼容警告（比如某端点不存在、字段变更风险）。
   - 这能提前暴露“升级后才炸”的问题。

### P2（体验与生态增强）

7. **接入 `SUBSCRIPTION_PAGE_CONFIGS` / `SNIPPETS` 做动态文案配置**
   - 当前支付提示、客服文案、说明文本大多硬编码在 bot 内。
   - 改为后端配置化后，运营调整无需发版。

8. **评估 `EXTERNAL_SQUADS` + `CONFIG_PROFILES` 的套餐映射能力**
   - 现在套餐与策略主要靠本地 plan 字段驱动。
   - 若改成“套餐 -> 配置模板/分组策略”映射，可降低人工维护复杂度。

---

## 4. 与前版分析合并后的“落地路线图”

### Phase A（1~2 天）

- 完成 API 调用收敛：`bot.py` 直调迁移到 `services/panel_api.py`。
- 提取常量（状态、支付方式、动作名、端点名）。
- 给关键 API 增加结构化日志（endpoint、status_code、latency_ms、order_id/tg_id）。

### Phase B（3~5 天）

- 拆分 `bot.py`（路由层 / 用例层 / 基础设施层）。
- 引入“API capability cache”（版本/端点兼容矩阵）。
- 补单测：`services/orders.py` + `jobs/*.py` + `panel_api` fallback 行为。

### Phase C（5~10 天）

- 接入 `IP_CONTROL` 与 `METADATA`，完成风控自动化与状态外置。
- 评估 sqlite -> PostgreSQL（取决于并发与运维要求）。
- 增加监控与告警（成功率、失败分类、重试次数、耗时分位）。

---

## 5. 你这个仓库“最值得先改的三件事”

1. **先把 API 调用统一收敛**（立即降低维护成本）。
2. **把批量动作做全**（立即降低接口与任务压力）。
3. **补 API 兼容与可观测性**（为后续 Remnawave 升级兜底）。

---

## 6. 边界说明

- 本文严格不改动 `README.md`。
- 本文为改造分析文档，不引入运行时行为变化。
