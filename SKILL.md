---
name: durable-loop
description: Scaffold and run long-running autonomous loops that survive crashes and converge verifiably — supplying the state persistence, crash recovery, and machine-verifiable convergence that Claude Code's built-in /loop lacks (budget + thrashing guards optional, off by default — pure quality-convergence mode). Use when the user wants to make, design, or start a long-running loop that runs for hours unattended; build an autonomous or self-improving loop; prevent a loop from losing state on crash; needs persistence and resume that /loop alone cannot do; or is running long-horizon research, continuous monitoring, or self-improving iteration tasks.
---

## 何时触发

用户想做一个长时间自动运转的 loop（跑几小时、跨 crash、能验收是否真完成），或抱怨 /loop 重启就失忆 / 不收敛。`/loop` 只是调度层；本 skill 补它缺失的状态持久化、恢复、收敛校验（预算护栏 2026-06-19 起默认关闭，只要质量）。

## Windows / WSL 注意

> 本 skill 原生为 macOS/Linux 设计。Windows 用户请注意：
>
> 1. **home 目录分叉**：只有在 **WSL bash.exe**（不是 Git Bash）里跑脚本时，`~/.claude` 才指向 WSL 的 `/root/.claude` 或 `/home/<user>/.claude`，与 Windows 版 Claude Code 实际读取的 `C:/Users/<YOU>/.claude` **完全分离**——在 WSL 里改 settings.json 会被 Windows 版忽略。**Git Bash 不受影响**（它的 `~` 就是 `/c/Users/<YOU>`，与 Windows 安装一致）。
> 2. **脚本调用**：优先用 Python 入口 `python scripts/init_loop.py` / `python scripts/verify_done.py`（纯 stdlib，跨平台，不依赖 GNU `timeout`/bash 数组）。要用 `.sh` 版则在 Git Bash 里 `bash scripts/init_loop.sh`。
> 3. **hook 命令**：Windows 用绝对正斜杠路径 + `python`（如 `python "C:/Users/<YOU>/.claude/skills/durable-loop/scripts/durable_loop_observe.py"`），**不要**用 `python3`/`$HOME`/`~`——它们在 PowerShell/cmd 下不解析。
> 4. **feature 名**：只允许字母数字、`-`、`_`（见 init_loop 校验），避免 `&`/`\` 损坏占位符替换。

## 核心理念

长时间自动运转需要四件套：**调度 + 状态持久化 + 恢复 + 边界控制**。`/loop` 只解决第一件。本 skill 用 8 个维度补足其余：每轮写 checkpoint.json、done.criteria.md 双维度机器验收、~~三道预算护栏~~（2026-06-19 移除，见 step 4）、定期 full context reset、副作用幂等+重试、HITL 危险操作拦截、append-only 可观测日志、按时间尺度选调度模式。裸 `while` + 内存 state 是反模式——crash 即失忆。

## 工作流

1. **选调度模式**。按时间尺度决策：

   | 时间尺度 | 调度方式 | 适用任务类型 | 备注 |
   |---|---|---|---|
   | <5min（min 级） | `/loop <interval>` (session 内) | 迭代优化/代码重构/调研 | z.ai/GLM 环境固定 cron 一律用 **240s**，**不要 300s** |
   | <5min（min 级） | 事件驱动（watch/MCP stream） | **监控/告警/CI 轮询**（优先于 cron，省 token） | z.ai 下绕过 polling 成本劣势 |
   | hours、本地依赖 | Desktop Scheduled Tasks (`~/.claude/scheduled_tasks/`) | 跨 session 的长程任务 | 跨 session 持久 |
   | days、无本地依赖 | Cloud Routines | 跨机器/无本地文件依赖 | Anthropic 托管，1h 最小间隔 |
   | >7 天 | GitHub Actions 触发 CLI | 长期无人值守 / CI 集成 | 每次启动新 session |

   z.ai/GLM 硬约束：第三方 provider cache TTL=5min，**300s 落在 cache 边界上每次 iteration 都 miss，成本翻倍**。监控类任务优先事件驱动（watch/MCP stream），绕过 polling。

2. **初始化状态目录**。运行 `python <skill>/scripts/init_loop.py <feature> [project_dir] [--force]`（跨平台，Windows 首选）或 `bash <skill>/scripts/init_loop.sh <feature> [project_dir] [--force]`（Unix），生成 `.scratch/<feature>/` 下完整 scaffold（checkpoint.json / done.criteria.md / handoff.md / tasks.jsonl / decisions.log / session.log / intermediate/ / dead_letter/ / traces/）。该脚本幂等。

3. **设计收敛条件**。改写 `.scratch/<feature>/done.criteria.md`：必须是 **QUANTITY + QUALITY 双维度同时为真**（如"测试全绿 AND coverage>=80% AND mypy clean AND 每个测试类测不同 module 防止 `assert True` gaming 单一条件"）。**generator 与 evaluator 强制分离**——actor 写代码、`scripts/verify_done.sh` 判定，绝不 let model 自评 done（Huang 判据：纯内在 self-correction 让 model 在 ~30% 完成度就声明 done）。loop 运行中途不得改收敛条件；要改先停 loop、写 ADR 到 `decisions.log`、再 resume。

4. **~~设三道预算护栏~~（2026-06-19 已移除，只要质量）**。预算护栏（token/$/轮/小时上限 + 超 100% 阻断）按用户要求**已移除**——`max_budget` 模板全 0、`check_budget.py` 不再因预算阻断、driver prompt 不再因预算停。`budget_used` 仍记录供观测成本，但**不 gate loop**。保留的硬约束只有**verify_done 收敛判定**（质量唯一 gate）。thrashing 防卡死护栏也于 2026-06-20 按需移除（纯质量收敛）——PreToolUse hook 已从 settings.json 摘除，`check_budget.py` 保留作参考但不再触发。若日后想恢复预算上限：在 checkpoint.json 填 `max_budget` 非 0 值，并把预算判定加回 `check_budget.py`。

5. **用 loop-driver-prompt 驱动**。把 `assets/loop-driver-prompt.md` 的占位符（`<FEATURE>` `<TASK_DESCRIPTION>` `<MAX_ITERATIONS>` `<BUDGET_DOLLARS>` `<MAX_HOURS>` `<RESET_EVERY_N>`，通常 N=5）替换后，整段喂给 `/loop <interval> "<prompt>"`。这份 prompt 内建了 8 要素的逐步约束，loop 单元每轮被唤醒时严格按它执行。

6. **每轮例行**。loop 单元每一轮：(a) 开头读 checkpoint 判断 resume vs fresh start；(b) 预算/thrashing 都**不再 gate**（2026-06-19/20 移除，纯质量收敛）——没有任何 hook 会因预算或原地打转阻断你；(c) 执行本轮动作（每个副作用带 idempotency key + 退避 1/2/4/8s 最多 5 次 + 永久错误不重试走 `dead_letter/` DLQ）；(d) **跑 `python scripts/verify_done.py <feature>` 评估收敛**（actor 不得自评——这是质量唯一 gate）；(e) 末尾**原子写入 checkpoint.json**（iteration+1、phase、budget_used 仅供观测、status、resume_from）；(f) append 一行到 `session.log`。要求能从 log 重建过去 24h 所有 agent 行为，否则只是 call LLM 的 shell script。

7. **每 N 轮 full context reset**。每 `RESET_EVERY_N`（通常 5）轮，把**约束类事实**（当前目标、已完成步骤、不变量、已否决方案、下一轮必须遵守的事）写到 `.scratch/<feature>/handoff.md`，归档旧版本到 `.scratch/handoff_archive/iter_<N>.md`，然后触发 `/context-restore` 或 strategic-compact 做 full reset，新 /loop 从 handoff 重建 minimal context。**不要用 semantic summary 压缩**——它会丢"为什么否决方案X"这类约束，导致 reset 后重复踩坑。Ralph Loop 实测 90 分钟退化成 tunnel vision。大输出 offload 到 `.scratch/intermediate/` 防爆 context。

8. **危险操作走 git-guardrails-claude-code**。push --force / reset --hard / clean -fd / branch -D 等危险 git 操作交给已有的 `git-guardrails-claude-code` skill 拦截，**本 skill 不重写**。关键节点（合并、发布、删除数据）写 `.scratch/<feature>/pending_approval.json` 触发 HITL breakpoint（checkpoint.status=`paused_for_approval`，**不是** `paused`——resume 门只认 `paused_for_approval`，写 `paused` 会被当成 fresh start 丢全部 state）。

9. **装 v2 自动化 hook 套装（强烈推荐，治本）**。durable-loop v2 把实例诊断发现最易失约的 4 个软约束升级成 hook 强制——agent 想偷懒都偷不了。装 3 个 hook（全部 fail-open，无 active loop 时不干预其他 session）：

   | hook 脚本 | 事件 | 强制什么 | 替代的软约束 |
   |---|---|---|---|
   | `scripts/durable_loop_observe.py` | PostToolUse | 每个工具调用自动 append 命令级 session.log | "agent 每轮记得写 session.log"（实测最易失约） |
   | `scripts/durable_loop_checkpoint.py` | Stop | 读 transcript 统计 token/**dollars** 写 budget_used（**纯观测**，2026-06-19 起不再 gate）+ 检测 Write/Edit/git 副作用更新 idempotency_keys + 算 hours | "agent 每轮累加 budget + 记 idempotency key" |
   | ~~`scripts/check_budget.py`~~（2026-06-20 移除） | ~~PreToolUse~~ | thrashing 防卡死——**已按需移除**（纯质量收敛）；PreToolUse hook 从 settings.json 摘除，脚本保留作参考 | 防卡死护栏（已移除） |

   现在只装 **2 个 hook**（observe PostToolUse + checkpoint Stop）——都**非阻断**（只观测/写 state，不会拦任何工具调用）。PreToolUse/check_budget 已 2026-06-20 按需移除（纯质量收敛）。两者共用同一 discover 逻辑（cwd 下单一 `.scratch/<feature>/` 自动识别），互不冲突：observe 只 append session.log，checkpoint 是**唯一**写 checkpoint.json 的（原子 tmp+mv）。

   **合并步骤**：优先用 harness 内置 `/update-config` skill；否则手工 **append** 到 settings.json（**Windows**：`C:/Users/<YOU>/.claude/settings.json`；**macOS/Linux**：`~/.claude/settings.json`）。⚠️ 若在 WSL bash.exe 里编辑会落到 `/root/.claude/` 或 `/home/<user>/.claude/`——那个文件 Windows 版 Claude Code **不读**。保留现有条目，不覆盖整个数组。**只 append 2 条**（PostToolUse + Stop；纯质量收敛，无 PreToolUse 阻断 hook）：

   ```
   # macOS/Linux（bash 下 $HOME 展开；用 python 不是 python3）:
   PostToolUse: { "hooks": [{"type":"command","command":"python \"$HOME/.claude/skills/durable-loop/scripts/durable_loop_observe.py\""}]}
   Stop:        { "hooks": [{"type":"command","command":"python \"$HOME/.claude/skills/durable-loop/scripts/durable_loop_checkpoint.py\""}]}

   # Windows（PowerShell 主力：用绝对正斜杠路径 + python；不要用 python3/$HOME/~，它们在 PowerShell/cmd 下不解析）:
   PostToolUse: { "hooks": [{"type":"command","command":"python \"C:/Users/<YOU>/.claude/skills/durable-loop/scripts/durable_loop_observe.py\""}]}
   Stop:        { "hooks": [{"type":"command","command":"python \"C:/Users/<YOU>/.claude/skills/durable-loop/scripts/durable_loop_checkpoint.py\""}]}
   ```

   （PostToolUse/Stop 不加 matcher = 匹配所有工具；两者都**非阻断**，只观测/写 state。Windows 下 `<YOU>` 换成你的用户名。两个脚本都 fail-open：无 `.scratch/<feature>/` 时静默 no-op，全局安装安全。原来还有个 PreToolUse/check_budget 阻断 hook，2026-06-20 按需移除——纯质量收敛，只有 verify_done 决定 done。）

   **多 feature 并存**：两个 hook 在 cwd 检测到多个 `.scratch/<feature>/` 时静默 fail-open（避免误伤）。多 loop 并行需指定 feature 时设 `DURABLE_LOOP_FEATURE=<feature>`。

## 8 要素速查表

| 要素 | 为什么 | 实现指向 |
|---|---|---|
| 外部状态持久化 | crash 即失忆是裸 while 的反模式，checkpoint 是恢复唯一事实来源 | `assets/checkpoint.json` + 每轮末尾原子写 |
| 机器可验收敛 | model 自评 done 在 ~30% 就声明完成，必须 generator/evaluator 分离 | `assets/done.criteria.md` + `scripts/verify_done.sh` |
| ~~三道预算护栏~~（已移除） | 预算上限 2026-06-19 移除；thrashing 2026-06-20 也移除（纯质量收敛）；`budget_used` 仅观测 | `assets/checkpoint.json` 的 `budget_used`（观测）；`scripts/check_budget.py` 保留作参考但不挂 hook |
| 上下文管理 | 不 reset 90 分钟退化 tunnel vision，semantic summary 丢决策约束 | `assets/handoff.md` + 每 5 轮 reset + `.scratch/intermediate/` |
| 重试+幂等 | 95% 单步可靠性端到端只剩 60%，无幂等 key 会重复扣款/发邮件 | `assets/loop-driver-prompt.md` 的退避段 + `.scratch/dead_letter/` |
| HITL breakpoint | 关键节点必须人工把关危险操作 | 复用 `git-guardrails-claude-code` skill + `.scratch/pending_approval.json` |
| 可观测 | 无 append-only 日志就无法复盘 agent 行为 | `.scratch/session.log`（每轮 append）+ `.scratch/traces/` |
| 三层调度选型 | min/hours/days/>7天需不同调度机制，错选会丢 session 或烧钱 | 上方决策表 + `assets/loop-driver-prompt.md` 的间隔说明 |

> **v2 自动化已 hook 化**：状态持久化（checkpoint.py Stop）、重试幂等（checkpoint.py 检测副作用）、可观测（observe.py PostToolUse）这 **3 个要素已 hook 强制**（都非阻断）。预算护栏 2026-06-19、thrashing 2026-06-20 相继按需移除——**纯质量收敛**，verify_done 是唯一 gate。收敛校验、上下文管理、HITL、调度选型仍靠 driver_prompt 软约束（前两者有 verify_done.py / handoff.md 脚本辅助）。

## z.ai/GLM 硬约束

> ⚠️ **第三方 provider 始终 5min cache TTL，无法启用 1 小时 cache。**
>
> - **调度间隔务必 <5min，用 240s 不用 300s**。300s 落在 5min cache 边界上，每次 iteration 都 cache miss，成本翻倍。
> - **监控类 loop 优先事件驱动**（watch/MCP stream），绕过 polling——polling 在短 TTL 下成本劣势最大。
> - `/loop` 只做 <5min 级；hours/days 用 Desktop Scheduled Tasks 或 Cloud Routines。

## 反模式 TOP 5

1. **裸 while + 内存 state**——crash 即失忆，恢复只能从头跑。后果：几小时工作丢失。
2. **done 靠 model 自评**——actor 在 ~30% 完成就声明 done。后果：任务假装完成，实际未收敛。
3. **（已按需移除）无预算上限**——历史上 loop 失控会持续烧 token（Revenium 式 11 天 $47K）。2026-06-19 起预算护栏按用户要求移除（只要质量）；若担心成本，观测 `budget_used` 自行判断，或在 checkpoint.json 恢复 `max_budget` 上限。
4. **单一收敛条件**（只看"测试通过"）——被 `assert True` gaming。后果：质量维度塌陷，测试假绿。
5. **持续 session 不 reset**——不归档 context，90 分钟退化 tunnel vision。后果：后期迭代质量断崖。

## 参考资料

> 本 skill 的方法论深度在 `references/`、模板在 `assets/`、执行器在 `scripts/`，三层 progressive disclosure。

按任务读哪个：

- **启动 loop 前** → `assets/loop-driver-prompt.md`（替换占位符喂给 `/loop`）
- **设计收敛条件** → `assets/done.criteria.md`（按任务改写阈值与命令）
- **理解状态 schema** → `assets/checkpoint.json`（空模板，字段语义见 `assets/checkpoint.schema.md`；填好的示例见 `assets/checkpoint.example.json`）
- **规划 context reset** → `assets/handoff.md`（约束类事实清单）
- **理解方法论深度**（9 gap / 8 要素深解 / 反模式完整清单 / z.ai 工程推论）→ `references/methodology.md`
- **选配方骨架**（长程研究 / 持续监控 / 自改进迭代三配方各自的调度+收敛+状态设计）→ `references/recipes.md`
- **初始化目录** → `python scripts/init_loop.py <feature>`（Windows/跨平台）/ `bash scripts/init_loop.sh <feature>`（Unix）
- **机械验收 done** → `python scripts/verify_done.py <feature>`（Windows/跨平台）/ `bash scripts/verify_done.sh <feature>`（Unix）
- **~~thrashing 防卡死护栏~~（2026-06-20 移除）** → 原 `scripts/check_budget.py` PreToolUse hook，已按需移除（纯质量收敛）；脚本保留作参考
- **session.log 自动记录** → `scripts/durable_loop_observe.py`（PostToolUse hook，命令级自动 append）
- **token/幂等/hours 自动同步** → `scripts/durable_loop_checkpoint.py`（Stop hook，读 transcript 写 checkpoint）
- **危险 git 操作拦截** → 复用 `git-guardrails-claude-code` skill（不重写）
- **context reset 执行** → 复用 `strategic-compact` / `context-save` / `context-restore` skill
- **质量门** → 复用 `verification-loop` skill
