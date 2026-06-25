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
| `idempotency_keys` | array | ✓ | 每次副作用前查重+执行后追加 | 已执行的副作用 key 列表，命中则 skip 不重放（`durable_loop_guard.py` PreToolUse 守卫在执行前命中即 deny，与 Stop hook 的追加构成 record→replay-block 闭环） |
| `thrashing_counter` | int |  | （已弃用） | thrashing 检测 2026-06-20 已移除（纯质量收敛）；字段保留兼容，不再更新/使用 |
| `run_id` | string |  | fresh start 注入（resume 不变） | uuid4 hex 运行标识，由 `init_loop.py` 在 fresh/`--force` 时注入，resume 保留旧值。`session.log` 每行带此字段，`replay_trace.py` 按它分组成独立 run。缺失（旧 checkpoint）按 `""` 处理（向后兼容） |
| `verify_history` | array |  | `verify_done.py` 每次运行 append | 每条 `{iteration:int\|null, result:"PASS"\|"FAIL", timestamp:ISO8601}`；尾部连续 PASS 数 ≥ `converge_k` 才判收敛（抗 flip-flop）。运行时不存在按 `[]` 处理；模板可不预置 |
| `evolution_trend` | array |  | Stop hook（`durable_loop_checkpoint.py`）每轮 append | **派生缓存（Gap1 进化度 metrics），非权威**。每条 `{iteration, window_size, pass_count, fail_count, pass_rate, converged, prev_pass_rate, improving}`，由 `verify_history` 最近 `EVOLUTION_WINDOW`(=10) 条聚合而来。记录最近 10 轮收敛趋势供 handoff 渲染"我在变好吗"。`converged`/`improving` 仅观察值——**权威收敛判定永远只看 `verify_done`**（K-连续-PASS）。窗口 <4 条时 `improving`/`prev_pass_rate` 为 null（防小窗口假阳性）。运行时不存在按 `[]` 处理；超 10 条自动裁剪最旧 |
| `converge_k` | int |  | fresh start（可选） | 收敛所需的连续 PASS 轮数 K，默认 2；优先级 env `DURABLE_LOOP_CONVERGE_K` > 此字段 > 默认 2。仅在 checkpoint 存在且可解析时启用门控；无 checkpoint 退化为原单次判定 |
| `reset_every_n` | int |  | fresh start（可选） | context reset 节律：每 N 轮由 Stop hook（`durable_loop_checkpoint.py`）从 cumulative_state 刷新 handoff.md、归档旧版到 `.scratch/<FEATURE>/handoff_archive/iter_<N>.md` 并置 `reset_due`。默认 5，0=禁用，iteration=0 永不触发。缺失按默认 5 处理 |
| `reset_due` | bool |  | Stop hook 触发 reset 时写 true | 标记下一轮需 full context reset；仅在 reset 触发时写入（只设不清），driver resume 时若为 true 则执行 reset 并把该键清回 false/删除。未触发时不出现该键 |
| `no_progress_limit` | int |  | fresh start（可选，默认 0=关闭） | no-progress 探测器（`check_progress.py`，可选 Stop hook）的相邻无进展轮数阈值；env `DURABLE_LOOP_NOPROGRESS_N` 优先于此字段，两者 ≤0/缺失/garbage 一律视为 0=关闭（默认 OFF）。触发时写 `pending_approval.json`、置 status=`paused_for_approval`，并记录 `paused_reason` / `status_before_pause` |
| `strict_guard` | bool |  | fresh start（可选，默认 false） | 开启 `durable_loop_guard.py` 的 strict 危险操作硬拦截（force push / reset --hard / rm -rf / DROP TABLE 等）；env `DURABLE_LOOP_STRICT`（1/true/yes/on）也可开启。默认关闭（纯质量收敛理念不变）。命中时 deny 并 best-effort 写 `pending_approval.json` |

### 关键设计约束

- **原子写入**：写 `checkpoint.json.tmp` 再 `mv` 覆盖，防写一半 crash 损坏
- **resume vs fresh 判断**：文件不存在 或 status == "fresh" → fresh start；status in {running, paused_for_approval, failed, over_budget, thrashing} → resume；status == completed → 跳过（注意：必须用 `paused_for_approval`，不是 `paused`——prompt 和 schema 全程统一此枚举值，否则会落入 fresh-start 丢 state）
- **预算不设护栏**（2026-06-19）+ **thrashing 已移除**（2026-06-20）：token/$/轮/小时、原地打转都不再阻断 loop——**纯质量收敛**。`budget_used` 仅观测，`max_budget` 不强制（全 0）。唯一 gate 是 `verify_done`
- **~~thrashing 阈值~~（已移除 2026-06-20）**：原 cosine/token-overlap >0.9 连续 3 轮即停；纯质量收敛后已废，PreToolUse hook 从 settings.json 摘除
- **新增运行时字段全部向后兼容（2026-06-22）**：`run_id` / `verify_history` / `converge_k` / `reset_every_n` / `reset_due` / `no_progress_limit` / `strict_guard` 在旧 checkpoint 缺失时各自取默认值，绝不硬失败。模板 `checkpoint.json` 已预置默认（`converge_k:2` / `reset_every_n:5` / `no_progress_limit:0` / `strict_guard:false`），但脚本不依赖模板预置。**默认理念不变**：收敛门控（K-连续-PASS）与 context-reset 是质量收敛增强（不阻断其他 session）；strict 守卫与 no-progress 暂停是**刹车类，默认关闭**，需显式 opt-in
- **session.log 行格式（2026-06-22）**：每行 JSON 新增 `run_id` 键（位于 `ts` 与 `iter` 之间），由 `durable_loop_observe.py` 从 checkpoint 读取写入；旧日志无该键时 `replay_trace.py` 归入 `(no run_id)` 桶
- **HITL 产物 `pending_approval.json`**：由 `durable_loop_guard.py`（strict 拦截）与 `check_progress.py`（no-progress 暂停）写入 `.scratch/<FEATURE>/`，结构 `{"requests":[{ts,tool,command,reason,status:"pending"}]}`。消费方人工清除该文件并把 status 从 `paused_for_approval` 改回 `running`（`status_before_pause` 记录原状态）即可恢复

---

## 经验沉淀层 `learnings.jsonl`（2026-06-22 新增，#3）

与 `checkpoint.json` **同级**的另一份持久化产物：`.scratch/<FEATURE>/learnings.jsonl`——跨轮、跨 run、（可选）跨 feature 累积"可复用经验"。由 `init_loop.py` 在 init 时 scaffold（空 0 字节，且 `--force` **也保留**已有内容，durability 等同 checkpoint.json）；由 `durable_loop_learn.py` 读写；handoff 刷新（`durable_loop_checkpoint.py`）只读它注入"已验证经验"段。

**JSONL，每行一个 JSON 对象**（11 字段）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 8 位短 hash / uuid hex 前 8 位，用于引用/去重 |
| `type` | string | `pattern`（成功模式）或 `pitfall`（失败教训） |
| `key` | string | kebab-case 去重主键（同 `type+key` 视为同一条） |
| `insight` | string | 一两句可复用经验 |
| `confidence` | int | 0–10，钳制 |
| `source` | string | 文件路径 / commit / `observed`（prune 据此判 stale） |
| `iteration` | int \| null | 记录时的 iteration |
| `run_id` | string | 从 checkpoint 读，缺失为 `""` |
| `timestamp` | ISO 8601 | 最近一次记录时刻 |
| `seen` | int | 同 key 再次 log 时 +1（默认 1） |
| `stale` | bool | `prune` 检测 source 失效时置 true（默认 false） |

**CLI**：`python scripts/durable_loop_learn.py <log|search|prune|compile> <FEATURE> [project_dir] [options]`
- `log`：去重合并（同 `(type,key)` → confidence 取 max、insight 用新值、seen+1、timestamp 更新、id 保留；否则 append 新行），原子 tmp+replace 写。
- `search`：在 key+insight+source 上关键词打分，排序=（非 stale 优先, 匹配分 desc, confidence desc）；`--limit`（默认 5）、`--type`、`--cross-feature`（默认关，扫同级 `.scratch/*/learnings.jsonl`）。
- `prune`：source 为已失踪文件路径→标 stale；默认 dry-run 仅报告，`--apply` 才删 stale 行回写（`observed`/URL/commit/无路径特征永不 stale）。
- `compile`：从非 stale 的 `pattern`（confidence ≥ `--min-confidence`，默认 6）按 confidence 降序生成 `## 已验证经验 (verified learnings)` markdown 到 stdout（供 handoff 注入），`--limit` 默认 10。

**与 handoff 的契约**：`durable_loop_checkpoint.py` 在每 `reset_every_n` 轮刷新 handoff.md 时，直接读 `learnings.jsonl`（不 import/shell learn 脚本），按相同阈值（pattern-only、非 stale、confidence ≥ 6、top-10、confidence 降序）注入 `## 已验证经验 (verified learnings)` 段；空则输出 `(暂无)`。阈值常量 `LEARNINGS_MIN_CONFIDENCE=6` / `LEARNINGS_TOP_N=10` 与 `compile` 默认对齐。

**理念**：learnings 是**质量增强、默认开启**，但**不是刹车/拦截**——不阻断任何工具调用。全程纯标准库、跨平台、fail-open（无目录/无文件/坏行 → 友好 no-op/空结果，绝不抛错影响其他 session）；非法 feature 名 / project_dir 不存在 → exit 2（对齐 verify_done.py）；文件级原子写。
