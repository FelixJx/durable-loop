# 三配方详解

> 每个配方按统一 9 项结构展开。配方级详情，按需加载。
>
> ⚠️ **2026-06-19/20 更新（以此为准）**：预算护栏（06-19）与 thrashing（06-20）相继移除——**纯质量收敛**，verify_done 唯一 gate。配方中"预算上限 / 超预算停止 / thrashing"相关项**已过时**，以 SKILL.md step 4 为准。

---

## 配方 A: 长程研究 / 迭代优化

### 1. 适用场景

代码库重构、深度调研、文档体系搭建、多模块协调开发——**需要多轮迭代、中间产出需要持久化、跑几小时不散**

### 2. 调度模式

**Dynamic ScheduleWakeup**（self-paced，无固定 interval）。让 Claude 根据观察自适应选 delay：
- 探索中（有 CI 在跑 / 有子任务排队）→ 短 wait（2-4min）
- 等外部信号（等 CI green / 等人回复）→ 长 wait（8-15min）
- 完成子任务准备下一轮→ 短 wait（1-2min）

z.ai 环境：self-paced 天然省 cache（不等 :00/:30 边界）。若需 fallback 到固定 cron，用 **4m**（240s）。

### 3. 状态目录结构

```
.scratch/<FEATURE>/
├── checkpoint.json          # 每轮原子写入（schema 见 assets/checkpoint.json）
├── done.criteria.md       # QUANTITY+QUALITY 双维度收敛条件
├── handoff.md             # full context reset 交接模板
├── tasks.jsonl            # 子任务队列，append-only
├── decisions.log          # 关键决策审计轨迹（append-only）
├── session.log            # append-only 可观测日志
├── intermediate/          # 大输出 offload（log/diff/抓取内容）
├── handoff_archive/       # 每 N 轮旧 handoff 归档
│   └── iter_5.md
├── dead_letter/           # 永久错误隔离（retries exhausted）
└── traces/                # 可选 per-N 轮 dump
```

### 4. 防上下文爆炸

- 每完成一个子任务→压缩为结构化摘要写入 `decisions.log` → 替代原始 trajectory
- **每 5 轮 full context reset**：新 /loop 从 `handoff.md` 重建 minimal context，不留历史 context
- 读大文件 / 长探索→派 sub-agent 用 worktree 隔离，主 agent 只收结构化结果
- 大 tool 输出（长 log / 大 diff）→ offload 到 `.scratch/intermediate/`，context 只留文件引用

### 5. 收敛条件（done.criteria.md 实例）

**QUANTITY**（机械验证）：
- [ ] 测试全绿：`pytest -x --tb=short`
- [ ] 覆盖率达标：`pytest --cov=src --cov-fail-under=80`
- [ ] lint clean：`ruff check .`
- [ ] typecheck clean：`mypy src/`
- [ ] 修改文件数 <= 10：`git diff --stat | wc -l`

**QUALITY**（evaluator 独立判定）：
- [ ] 每测试类测不同 module（防 assert True gaming）
- [ ] 无 dead code
- [ ] 无 debug 残留
- [ ] 关键决策已记录在 decisions.log
- [ ] 副作用幂等性已验证

### 6. 恢复机制

crash 后新 session 开头：
1. 读 `.scratch/<FEATURE>/checkpoint.json`
2. 若 status in {running, paused_for_approval, failed, over_budget, thrashing} → resume，从 `resume_from` 指令继续
3. 若文件不存在或 status == fresh → fresh start
4. 读 `handoff.md`（如果存在）恢复上下文
5. 读 `decisions.log` 恢复决策约束

### 7. 预算护栏

- max_iterations: 20-30
- max_dollars: $10-50（研究类不太烧钱）
- max_hours: 6-8（单 session 上限，超了上 Desktop Tasks）
- thrashing: 连续 3 轮 >0.9 → 停

### 8. 何时 escalate 人工

- 连续 3 轮无进展（thrashing 检测自动触发）
- budget 超 80%（check_budget.py hook 或 prompt 内检查）
- 遇到 done.criteria 无法自动判断的模糊决策
- 所有验证命令都 FAIL 但不知道怎么修

### 9. 预期运转时长上限

- **单 session**: 6-8 小时（必须每 5 轮 full reset，否则 context 退化）
- **跨 session**: Desktop Tasks 可支撑跨重启，7 天上限
- **>7 天**: 用 GitHub Actions 定期触发新 session

### loop-driver-prompt 片段

```
你是 durable-loop 的执行单元。每一轮被唤醒时，严格按下面 8 个要素的约束推进 [TASK_DESCRIPTION]。
绝不跳过任何一步。

【0. 入口：判断 resume vs fresh start】
- 读 `.scratch/<FEATURE>/checkpoint.json`。...

（完整 prompt 见 assets/loop-driver-prompt.md，替换占位符后喂给 `/loop 240s "<这段 prompt>"`）
```

---

## 配方 B: 持续监控 + 告警

### 1. 适用场景

PR review 自动审查、CI 状态轮询、部署后健康检查、文件变更监控——**不需要收敛，持续运转直到目标事件出现或人工停止**

### 2. 调度模式

**优先事件驱动**（绕过 polling）：
- 用 `watch` / MCP stream / Monitor 工具监控变化源（CI webhook、file watcher、API poller）
- 变化 → 触发 agent 处理 → 无变化则 idle（不消耗 token）

**fallback fixed cron**（事件驱动不可用时）：
- `/loop 4m`（**240s**，避开 300s cache 边界）
- 每轮只处理增量事件，state 外置，context 恒定瘦

### 3. 状态目录结构

```
.scratch/<FEATURE>/
├── state.json              # {last_seen_event_id, acknowledged_alerts[], silence_until, config_hash}
├── alerts.jsonl             # append-only 告警历史
├── dedup.json              # 告警去重（同 event_id 只报一次）
├── session.log
└── dead_letter/
```

### 4. 防上下文爆炸

- 事件驱动模式天然省 token（只在有变化时处理，流式 output 不堆 context）
- Fixed cron 模式：每轮只处理增量事件，state 外置到 `state.json`，context 恒定瘦（和第一轮一样轻量）
- 不需要 full context reset（上下文不增长）

### 5. 收敛条件

**监控类 loop 通常不收敛**（持续运转直到目标事件出现或人工停止）。

终止条件（OR 任一即停）：
- 目标 CI green
- PR merged
- 用户人工 Esc 停止（`/loop` 会检测 idle 退出或 Ctrl+C）
- 7 天硬过期

### 6. 恢复机制

用 `--resume`/`--continue` 恢复未过期 cron task。读 `state.json` 的 `last_seen_event_id`，只处理增量事件，不重复处理已确认的 alert。

### 7. 预算护栏

- max_iterations: 100-500（监控类轮次多但每轮轻）
- max_dollars: $5-10（每轮只读增量）
- max_hours: 24-72（Desktop Tasks 跨重启）
- thrashing: 告警触发频率过高则自动 silence（`silence_until` 时间戳）

### 8. 何时 escalate 人工

- CI 连续失败 3 次
- 检测到 production 异常（5xx 持续）
- 告警无法自动 resolve（需要人工介入 root cause）

### 9. 预期运转时长上限

- 事件驱动：数小时（session 存活期间）
- Fixed cron Desktop Tasks：跨重启，**7 天上限**
- >7 天 或跨机器：Cloud Routines 或 GitHub Actions 触发 CLI

---

## 配方 C: 自改进迭代

### 1. 适用场景

prompt 优化、skill 库积累、测试覆盖率迭代、配置调参——**Voyager 式"做中学"循环，每轮产出可复用的 knowledge**

### 2. 调度模式

**Dynamic ScheduleWakeup + CronCreate 组合**：
- 主循环用 Dynamic self-paced：每轮评估→改进→验证→决定下一轮 delay
- CronCreate 固定节奏触发 checkpoint：`57 */2 * * *`（每 2 小时存一次 skill 库快照到外置存储）
- 保证即使 dynamic loop 因某种原因中断，skill 库至少 2 小时一备份

### 3. 状态目录结构

```
.scratch/<FEATURE>/
├── checkpoint.json
├── done.criteria.md
├── handoff.md
├── session.log
├── skill_library/
│   ├── index.json             # 可检索的 skill 索引 {name, description, tags, success_rate, last_used}
│   ├── skills/
│   │   ├── skill_001.md       # {name, description, code/commands, success_rate, last_used, learned_from}
│   │   └── ...
│   └── episodic_memory.jsonl  # 失败反思（Reflexion 式：每次 fail 后记录 what went wrong and why）
├── eval_results.jsonl         # 每轮 eval 结果 {iteration, score, evaluator, details}
└── ratchet.lock               # test ratchet 单调锁：已通过的测试只增不减
```

### 4. 防上下文爆炸

- 每轮 retrieve 相关 skill（从 index.json 检索）+ episodic memory（最近 N 条失败反思）→注入 minimal context
- 成功 trajectory 固化为 skill 存库（skill-as-code 可组合可迁移，非只在对话 memory 留 reflection）
- 主 context 只放：当前任务 + 检索到的相关 skill + 最近失败反思
- 每 10 轮 full reset + handoff

### 5. 收敛条件（done.criteria.md 实例）

**QUANTITY**（机械验证）：
- [ ] eval 分数连续 3 轮无提升（收敛）
- [ ] eval 分数超目标阈值
- [ ] skill 库覆盖所有已知失败模式

**QUALITY**（evaluator 独立判定）：
- [ ] 每轮 eval 必须用独立 judge model（LLM-as-Judge），非 actor 自评
- [ ] 新增 skill 至少在 3 个不同场景验证（cross-validation）
- [ ] 无矛盾 skill（两个 skill 给同一输入产生冲突建议）

### 6. 恢复机制

读 checkpoint + skill_library index。从最后 eval 结果继续。episodic memory 不丢失（append-only，resume 时全部可回溯）。

### 7. 预算护栏

- max_iterations: 50（自改进收敛慢）
- max_dollars: $50-100（每轮有 eval 成本）
- max_hours: 跨多天（Desktop Tasks 7 天上限，超了 GitHub Actions）
- thrashing: eval 分数连续 3 轮无变化 → 疑似 reward hacking → escalate

### 8. 何时 escalate 人工

- eval 分数退化（reward hacking 信号：改进后分数反而降）
- skill 库出现矛盾 skill（需要人工仲裁选哪个）
- 连续 5 轮 eval 无变化
- done.criteria 的 QUALITY 维度需要人工判断

### 9. 预期运转时长上限

- 单 session: 6-8h（需 full reset）
- 跨 session: Desktop Tasks 7 天
- >7 天: GitHub Actions 定期触发新 session，skill 库 + episodic memory 持久化到 repo

---

## 通用补丁清单

所有配方共享的 gap 补丁，在任何长跑 loop 启动前必须确认：

1. **状态序列化**：`.scratch/checkpoint.json` 每轮写（tmp+mv 原子写）
2. **crash 恢复**：loop 开头读 checkpoint 判断 resume vs fresh
3. **预算检查**：每轮检查 max_iterations + max_dollars + max_hours
4. **重复检测**：thrashing 检测（相似度 >0.9 连续 3 轮）
5. **HITL gate**：关键动作前用 git-guardrails 拦截
6. **trace dump**：每 N 轮 dump 到 `.scratch/traces/`
7. **full context reset**：每 N 轮用 fresh /loop 从 handoff 重建
8. **generator/evaluator 分离**：done 判定用 verify_done.sh + 独立 evaluator，不用 actor 自评
