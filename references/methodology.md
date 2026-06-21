# 方法论深解：长时间自动运转 Loop 的工程科学

> 本文档是 durable-loop skill 的深度参考，按需加载。核心速查在 SKILL.md 的 8 要素速查表。
>
> ⚠️ **2026-06-19/20 更新（以此为准）**：预算护栏（06-19）与 thrashing 防卡死（06-20）相继按用户要求移除——**纯质量收敛**，verify_done 唯一 gate。本文中关于"预算上限 / 超 100% 停止 / thrashing 停止 / max_budget 硬护栏"的描述**已过时**，以 SKILL.md step 4 与 `checkpoint.schema.md` 为准（`budget_used` 仅观测）。

## 目录

1. [为什么裸 /loop 做长跑会出事](#1-为什么裸-loop-做长跑会出事)
2. [9 个 Gap](#2-9-个-gap)
3. [8 要素深解](#3-8-要素深解)
4. [反模式完整清单](#4-反模式完整清单)
5. [z.ai/GLM 硬约束的工程推论](#5-zaiglm-硬约束的工程推论)

---

## 1. 为什么裸 /loop 做长跑会出事

Claude Code 内置 `/loop` 提供两种调度引擎：Dynamic（ScheduleWakeup，self-paced 自适应间隔 60-3600s）和 Cron（CronCreate，标准 5-field cron，recurring 7 天过期）。**这只是一层「时间触发器」**，解决了「什么时候唤醒」的问题。

长时间自动运转还需要另外三件套：
- **状态持久化**：checkpoint 每轮落盘，crash 后能从断点恢复而非从头来
- **恢复**：读取 checkpoint 判断 resume vs fresh start，不依赖 context 记忆
- **边界控制**：预算上限、收敛校验、tharshing 检测、HITL 拦截

裸用 `/loop` 只触发不做持久化，等于一个有闹钟但没存盘的 while loop——crash 即失忆。

---

## 2. 9 个 Gap

### Gap 1: 无状态持久化 → crash 即失忆（🔴 P0）

**现象**：`/loop` 的执行 state 只活在 context window，REPL 进程一死 state 全没。

**失效模式**：跑 6 小时的优化 loop 在 5h55min crash → 从头再来。Desktop Scheduled Tasks 持久化 task 定义但**不持久化执行进度**。

**业界答案**：Temporal event sourcing（crash 后任意 worker replay 历史重建）、DBOS Postgres 事务（checkpoint 与副作用同事务）、LangGraph checkpoint per super-step。

**Claude Code 解法**：`.scratch/<feature>/checkpoint.json` 每轮原子写入（tmp+mv），含 iteration/phase/status/resume_from 等全字段。loop 开头读此文件判断 resume vs fresh start。

### Gap 2: 无收敛条件强校验 → goal drift + reward hacking（🔴 P0）

**现象**：done 条件写在 prompt 里靠 Claude 自觉判断，无 machine-verifiable gate。

**失效模式**：model 自评偏乐观（Anthropic 实证问"完成没"答案偏 yes），~30% 完成就声明 done。Huang 判据：纯内在 self-correction **系统性降低准确率**。甚至写 `assert True` 式 trivial pass gaming 单一收敛条件。

**业界答案**：test ratchet 单调锁（只增不减已通过测试）、generator-evaluator 分离（同 model 不同 prompt）、stop-hook 读外部 criteria 文件。

**Claude Code 解法**：`done.criteria.md` 外部文件（QUANTITY + QUALITY 双维度），`verify_done.sh` 机械跑命令判定，**绝不 let model 自评 done**。

### Gap 3: 无上下文管理 → context rot 线性增长（🔴 P0）

**现象**：context 随轮次线性堆积，硬上限前性能就开始衰减。

**失效模式**：持续 session 在 90 分钟退化成 tunnel vision（Ralph Loop 实测）。context 爆炸导致遗忘早期目标和已否决方案。

**业界答案**：Context-Folding（32K 压缩为摘要撑到 327K）、MemGPT OS 式分页、Deep Agents filesystem offload、Anthropic compaction + sub-agent 委派 + tool-context 剪枝。

**Claude Code 解法**：**full context reset** 每 N 轮从 `handoff.md` 重建 minimal context（非 semantic summary——必须保决策约束类事实），sub-agent worktree 隔离大文件探索，大输出 offload 到 `.scratch/intermediate/`。

### Gap 4: 无错误恢复/重试 → 单点失败即终止（🟡 P1）

**现象**：API 超时/tool 执行失败直接停，无 backoff 无重试。

**失效模式**：网络抖动一次就整个 loop 挂。95% 单步可靠性的链路端到端只有 60%（Augment Code 复利衰减数据）。

**业界答案**：指数退避四件套（InitialInterval 1s / BackoffCoefficient 2.0 / MaximumInterval 100× / MaximumAttempts 5-10）+ 幂等 key + DLQ + nonRetryableErrorTypes 分类。

**Claude Code 解法**：loop-driver-prompt 内建退避 1/2/4/8s + 永久错误不重试进 DLQ + idempotency key 查重去重。

### Gap 5: 无预算护栏 → 失控烧钱（🔴 P0）

**现象**：`/loop` 没有 token/美元/iteration 硬上限。

**失效模式**：Revenium 案例 11 天烧 $47K；失控 token 消耗可达正常 15 倍。

**业界答案**：AutoGen termination callable 组合 + Agent Contracts session 级预算 + spawn budget 继承模型。

**Claude Code 解法**：checkpoint.json max_budget 三维度（dollars/tokens/iterations/hours），三道护栏独立检查取 max frac。check_budget.py 可选 PreToolUse hook 自动阻断。

### Gap 6: 无 HITL breakpoint → 不可逆操作无刹车（🟢 P2）

**现象**：无 pause-in-place，无人工审批 gate。

**失效模式**：loop 自己执行 `git push --force` / 删数据，无法在关键节点拦截。

**业界答案**：LangGraph interrupt() 存 checkpoint 暂停 + Command(resume=) 恢复。

**Claude Code 解法**：复用 `git-guardrails-claude-code` skill 拦截危险 git 操作；关键节点写 `pending_approval.json` + status=paused_for_approval。

### Gap 7: 无可观测性 → silent failure 不可见（🟡 P1）

**现象**：无 trace 无 metrics 无 replay。

**失效模式**：loop 在转但产出空，用户无感知；context rot/goal drift 无预警信号。

**业界答案**：OpenTelemetry 全程 trace + Temporal replay 本地复现 + EvaLooop ASL 指标。

**Claude Code 解法**：append-only `session.log`（每轮 timestamp/iteration/action/observation/budget），要求能从日志重建过去 24h 所有 agent 行为。

### Gap 8: 无 catch-up → missed fires 丢失（🟢 P2）

**现象**：Claude 忙时 missed 的 fire 只补一次，长时间 busy 丢失大量调度点。

**业界答案**：Temporal timer 精确补齐；CronCreate 语义明确 no catch-up。

**Claude Code 解法**：监控类任务优先事件驱动（绕过 polling），避免依赖 catch-up。

### Gap 9: 跨 session 能力弱 → 7 天硬过期（🟢 P2）

**现象**：recurring 7 天自动过期；REPL 死即停。

**失效模式**：>7 天任务无法用 /loop；机器重启即断。

**业界答案**：Cloud Routines / GitHub Actions。

**Claude Code 解法**：三层调度选型（min→/loop, hours→Desktop Tasks, days→Cloud Routines, >7天→GitHub Actions）。

---

## 3. 8 要素深解

### 要素 1: 外部状态持久化

**Why**：context window 是易失的，长跑必须把 state 落盘。否则 REPL 死一次，6 小时成果归零。

**Claude Code 实现**：
- `assets/checkpoint.json` 每轮原子写入（tmp+mv），字段覆盖 iteration/phase/status/resume_from/cumulative_state/budget_used/idempotency_keys/thrashing_counter/max_budget
- `scripts/init_loop.sh` 初始化 `.scratch/<feature>/` 完整骨架
- loop 开头读 checkpoint 判断 resume vs fresh start
- `decisions.log` append-only 审计轨迹

**反模式**：把 state 塞在 context 里靠 Claude "记得"。

### 要素 2: 机器可验收敛条件

**Why**：Huang 判据证明纯内在 self-correction 系统性降低准确率；model 自评偏乐观。

**Claude Code 实现**：
- `assets/done.criteria.md` 外部文件，QUANTITY + QUALITY 双维度
- `scripts/verify_done.sh` 机械跑命令判定（HTML comment 或 backtick 约定）
- generator/evaluator 分离：actor 写代码、verify_done.sh 判定、evaluator 独立 JUDGE: PASS
- test ratchet：prompt 显式禁止删/改已通过测试

**反模式**：done 条件只写在 prompt 里靠 Claude 判断；单一条件只看"测试通过"会被 `assert True` gaming。

### 要素 3: 三道预算护栏

**Why**：无上限 = 定时炸弹。Revenium $47K 案例。

**Claude Code 实现**：
- checkpoint.json max_budget 三维度（dollars/tokens/iterations/hours）各自独立
- 超阈值取 max frac（check_budget.py 修正后三维度独立取最大，不因只设 iteration 没设 dollars 导致失效）
- thrashing 检测：cosine/token-overlap >0.9 连续 3 轮停（阈值已统一：代码 0.90 = 文档 0.90）
- check_budget.py 可选安装为 PreToolUse hook，自动阻断超预算工具调用

**反模式**：无任何上限；budget 按 depth 递增（指数爆炸）。

### 要素 4: 上下文管理策略

**Why**：context 随轮次线性堆积，90min 退化 tunnel vision。

**Claude Code 实现**：
- **full context reset**（非 summary 压缩）：每 N 轮从 `handoff.md` 重建 minimal context
- handoff.md 保留决策约束类事实（已否决方案、预算上限、idempotency 约束）
- sub-agent worktree 隔离大文件探索
- filesystem offload：大 tool 输出写到 `.scratch/intermediate/`
- 归档机制：每 N 轮旧 handoff 归档到 `handoff_archive/iter_<N>.md`

**反模式**：让历史在 context 堆积；纯 semantic summary（丢决策约束）；sliding window 截断（丢早期重要上下文）。

### 要素 5: 重试 + 幂等

**Why**：95% 单步可靠性端到端只剩 60%。

**Claude Code 实现**：
- idempotency key（格式 `<op>-<version>-<entity_id>`），checkpoint.idempotency_keys 查重
- 退避 1/2/4/8s 最多 5 次
- 永久错误（4xx 参数/权限）不重试，进 `.scratch/dead_letter/` DLQ
- 每步设计成可重入

**反模式**：失败即停；无幂等 key 导致重复扣款/发邮件；裸退避无上限。

### 要素 6: HITL breakpoint

**Why**：关键节点必须人工把关。

**Claude Code 实现**：
- 复用 `git-guardrails-claude-code` skill 拦截危险 git 操作
- 关键决策点写 `pending_approval.json` + status=paused_for_approval
- SKILL.md 明确引导用 `update-config` skill 做 hook merge（不直接改 settings.json）

**反模式**：全自动跑生产操作；无 approval gate。

### 要素 7: 可观测性

**Why**：无 trace = silent failure。

**Claude Code 实现**：
- `session.log` append-only JSON：{timestamp, iteration, action, observation, budget, phase}
- 要求能从日志重建过去 24h 所有 agent 行为
- `.scratch/traces/` 可选 per-N 轮 dump

**反模式**：无日志；日志不落盘；只看最终输出不看过程。

### 要素 8: 三层调度选型

**Why**：场景错配即挂。

**Claude Code 实现**：min 级→`/loop`(session 1min 最小间隔)；hours 本地→Desktop Tasks；days→Cloud Routines；>7 天→GitHub Actions CLI。

**z.ai 特殊约束**：第三方 provider 5min TTL，间隔务必 <5min 用 240s 不用 300s。监控类优先事件驱动。

---

## 4. 反模式完整清单

| # | 反模式 | 后果 | 正确做法 |
|---|---|---|---|
| 1 | 裸 while + 内存 state | crash 即失忆 | checkpoint.json 外部持久化 |
| 2 | done 靠 model 自评 | 30% 完成就声明 done | verify_done.sh + 独立 evaluator |
| 3 | 无预算上限 | $47K 炸弹 | max_budget 三维度护栏 |
| 4 | budget 按 depth 递增 | 指数爆炸（10x token 实测） | 继承/固定上限 |
| 5 | 持续 session 不 reset | 90min tunnel vision | 每 N 轮 full reset + handoff |
| 6 | 纯 semantic summary 压缩 | 丢决策约束类事实 | full reset 从 handoff 重建 |
| 7 | sliding window 截断 | 丢早期重要上下文 | 重建而非截断 |
| 8 | 失败即停无重试 | 复利衰减到 60% | 退避 + 永久错误分类 + DLQ |
| 9 | 无幂等 key | 重复扣款/发邮件 | idempotency key + checkpoint 查重 |
| 10 | 裸退避无上限 | 永久错误也重试到天荒 | 最大 5 次 + 永久错误分类 |
| 11 | 无 HITL gate | 不可逆灾难 | git-guardrails + pending_approval |
| 12 | 无 trace 无日志 | silent failure 不可见 | append-only session.log |
| 13 | 长跑任务用 session-scoped /loop | REPL 死即停 | 按时间尺度选 Desktop/Cloud/GitHub |
| 14 | 5min TTL 下用 300s 间隔 | 每次 cache miss 成本翻倍 | 用 240s（留 30s 余量） |
| 15 | 第三方 provider 期望 1h cache | 不支持 | 必须 <5min 或事件驱动 |
| 16 | :00 整点 cron | jitter 影响 | 用 7\* \* \* 不用 0 \* \* \* |
| 17 | 单一收敛条件 | 被 assert True gaming | QUANTITY+QUALITY 双维度 |
| 18 | multi-agent 编排长程任务 | 上下文割裂 | 单 agent 连续上下文更可靠 |
| 19 | evaluator 和 generator 同 prompt | self-grading 不可靠 | 不同 prompt 不同角色 |
| 20 | 长程任务无 Planner | 计划漂移 | Plan-and-Execute 模式 |

---

## 5. z.ai/GLM 硬约束的工程推论

第三方 provider（z.ai）始终 **5min prompt cache TTL**，无法启用 1 小时 cache（那是 Anthropic 直连订阅才有的功能）。

### 推论

1. **调度间隔务必 <5min**：建议用 240s（留 30s 安全余量），**绝对不要用 300s**。300s 恰好落在 5min cache window 边界上，每次 iteration 都 cache miss，成本翻倍。

2. **监控类 loop 优先事件驱动**：用 watch / MCP stream / Monitor 工具，绕过 polling。polling 在短 TTL 下成本劣势最大。

3. **Desktop Tasks / Cloud Routines 不受 TTL 约束**：这些调度层有自己独立的 API 调用，不受 loop 内的 prompt cache 影响。长间隔任务优先用这些。

4. **Dynamic self-paced 模式天然省 cache**：ScheduleWakeup 间隔是 Claude 根据观察动态选的（build 忙短 wait、PR 安静时长 wait），不一定命中 :00/:30 cache miss。

5. **Subagent 始终 5min TTL**：不管主 session 的 cache 设置如何，subagent（Workflow/Agent）始终走 5min。长 chain 的 subagent 间无缓存继承，每次从头 context 加载。
