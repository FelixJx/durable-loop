# checkpoint.json 字段说明

| 字段 | 类型 | 必填 | 更新时机 | 说明 |
|---|---|---|---|---|
| `feature` | string | ✓ | fresh start | 对应 .scratch/<feature>/ 目录名，跨 loop 全局唯一 |
| `iteration` | int | ✓ | 每轮 +1 | 已完成轮次（0=尚未跑过任何完整轮） |
| `phase` | string | ✓ | 每轮开头 | planning / executing / verifying / paused / completed / failed |
| `started_at` | ISO 8601 | ✓ | fresh start | 首次启动时间，resume 时不变，用于算 hours 预算 |
| `last_updated` | ISO 8601 | ✓ | 每轮末尾 | 本次写 checkpoint 的时刻 |
| `last_action` | string | ✓ | 每轮末尾 | 最近一次具体动作的可读描述（thrashing 相似度比较 + session.log 追溯用） |
| `last_result` | string | ✓ | 每轮末尾 | 上次动作结果摘要+证据路径（失败也要记） |
| `cumulative_state` | object | ✓ | 每轮末尾 | 嵌套 object，包含 decisions_made / artifacts_produced / metrics_snapshot / facts_discovered |
| `budget_used` | object |  | Stop hook 自动填 | {tokens, dollars, iterations, hours}；**纯观测**（2026-06-19 起不再用作护栏判定）。dollars 由模型价目表估算，tokens 从 transcript 统计，hours 由 started_at 算 |
| `max_budget` | object |  | fresh start | {tokens, dollars, iterations, hours}；⚠️ **2026-06-19 起不再强制**——预算护栏按用户要求移除（只要质量）。字段保留作参考，全 0 = 无上限；改了也不阻断任何调用 |
| `status` | string | ✓ | 每轮末尾 | fresh / running / paused_for_approval / over_budget / thrashing / completed / failed |
| `resume_from` | string | ✓ | 每轮末尾 | 下一轮第一步要做什么的具体指令。crash 恢复后 agent 读完即知道干啥 |
| `idempotency_keys` | array | ✓ | 每次副作用前查重+执行后追加 | 已执行的副作用 key 列表，命中则 skip 不重放 |
| `thrashing_counter` | int |  | （已弃用） | thrashing 检测 2026-06-20 已移除（纯质量收敛）；字段保留兼容，不再更新/使用 |

### 关键设计约束

- **原子写入**：写 `checkpoint.json.tmp` 再 `mv` 覆盖，防写一半 crash 损坏
- **resume vs fresh 判断**：文件不存在 或 status == "fresh" → fresh start；status in {running, paused_for_approval, failed, over_budget, thrashing} → resume；status == completed → 跳过（注意：必须用 `paused_for_approval`，不是 `paused`——prompt 和 schema 全程统一此枚举值，否则会落入 fresh-start 丢 state）
- **预算不设护栏**（2026-06-19）+ **thrashing 已移除**（2026-06-20）：token/$/轮/小时、原地打转都不再阻断 loop——**纯质量收敛**。`budget_used` 仅观测，`max_budget` 不强制（全 0）。唯一 gate 是 `verify_done`
- **~~thrashing 阈值~~（已移除 2026-06-20）**：原 cosine/token-overlap >0.9 连续 3 轮即停；纯质量收敛后已废，PreToolUse hook 从 settings.json 摘除
