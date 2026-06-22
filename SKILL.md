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

   > **调度脚手架 `emit_schedule.py`（2026-06-22）**：把上表决策落地为可用配置骨架——`python <skill>/scripts/emit_schedule.py <feature> <min|hours|days|long> [project_dir] [--interval X] [--command Y] [--stdout]`。`min`→`/loop` 用法（默认 240s + z.ai cache 提示）；`hours`→Desktop Scheduled Task JSON；`days`→Cloud Routine JSON；`long`→GitHub Actions workflow YAML。默认写到 `.scratch/<feature>/schedules/<horizon>.<ext>`，`--stdout` 只打印。纯生成器，不改 checkpoint、不接 hook。

2. **初始化状态目录**。运行 `python <skill>/scripts/init_loop.py <feature> [project_dir] [--force]`（跨平台，Windows 首选）或 `bash <skill>/scripts/init_loop.sh <feature> [project_dir] [--force]`（Unix），生成 `.scratch/<feature>/` 下完整 scaffold（checkpoint.json / done.criteria.md / handoff.md / tasks.jsonl / decisions.log / session.log / **learnings.jsonl（经验沉淀层，空 0 字节，`--force` 也保留已有内容，durability 等同 checkpoint.json）** / intermediate/ / dead_letter/ / traces/）。该脚本幂等。

3. **设计收敛条件**。改写 `.scratch/<feature>/done.criteria.md`：必须是 **QUANTITY + QUALITY 双维度同时为真**（如"测试全绿 AND coverage>=80% AND mypy clean AND 每个测试类测不同 module 防止 `assert True` gaming 单一条件"）。**generator 与 evaluator 强制分离**——actor 写代码、`scripts/verify_done.sh` 判定，绝不 let model 自评 done（Huang 判据：纯内在 self-correction 让 model 在 ~30% 完成度就声明 done）。loop 运行中途不得改收敛条件；要改先停 loop、写 ADR 到 `decisions.log`、再 resume。

4. **~~设三道预算护栏~~（2026-06-19 已移除，只要质量）**。预算护栏（token/$/轮/小时上限 + 超 100% 阻断）按用户要求**已移除**——`max_budget` 模板全 0、`check_budget.py` 不再因预算阻断、driver prompt 不再因预算停。`budget_used` 仍记录供观测成本，但**不 gate loop**。保留的硬约束只有**verify_done 收敛判定**（质量唯一 gate）。thrashing 防卡死护栏也于 2026-06-20 按需移除（纯质量收敛）——PreToolUse hook 已从 settings.json 摘除，`check_budget.py` 保留作参考但不再触发。若日后想恢复预算上限：在 checkpoint.json 填 `max_budget` 非 0 值，并把预算判定加回 `check_budget.py`。

5. **用 loop-driver-prompt 驱动**。把 `assets/loop-driver-prompt.md` 的占位符（`<FEATURE>` `<TASK_DESCRIPTION>` `<MAX_ITERATIONS>` `<BUDGET_DOLLARS>` `<MAX_HOURS>` `<RESET_EVERY_N>`，通常 N=5）替换后，整段喂给 `/loop <interval> "<prompt>"`。这份 prompt 内建了 8 要素的逐步约束，loop 单元每轮被唤醒时严格按它执行。

6. **每轮例行**。loop 单元每一轮：(a) 开头读 checkpoint 判断 resume vs fresh start；**(a') reflect-in：`python scripts/durable_loop_learn.py search <feature> --query "<本轮关键词>"` 检索经验沉淀层，命中的 pattern 复用、pitfall 规避，在思考里显式体现**；(b) 预算/thrashing 都**不再 gate**（2026-06-19/20 移除，纯质量收敛）——没有任何 hook 会因预算或原地打转阻断你；(c) 执行本轮动作（每个副作用带 idempotency key + 退避 1/2/4/8s 最多 5 次 + 永久错误不重试走 `dead_letter/` DLQ）；(d) **跑 `python scripts/verify_done.py <feature>` 评估收敛**（actor 不得自评——这是质量唯一 gate）；(e) 末尾**原子写入 checkpoint.json**（iteration+1、phase、budget_used 仅供观测、status、resume_from）；**(e') reflect-out：`python scripts/durable_loop_learn.py log <feature> --type pattern|pitfall --key ... --insight ... --confidence ...` 至少沉淀 1 条（成功→pattern；失败/已否决方案→pitfall），同 (type,key) 自动合并去重，不记显而易见或一次性瞬时错误**；(f) append 一行到 `session.log`。要求能从 log 重建过去 24h 所有 agent 行为，否则只是 call LLM 的 shell script。

   > **经验沉淀层 learnings（2026-06-22，#3）**：`.scratch/<feature>/learnings.jsonl` 跨轮/跨 run/（可选）跨 feature 累积"可复用经验"，由 `python scripts/durable_loop_learn.py <log|search|prune|compile> <feature>` 读写。每轮构成 **reflect 闭环**：开头 `search` 借鉴历史（命中 pattern 复用、pitfall 规避），收尾 `log` 沉淀（成功 pattern / 踩坑 pitfall）。**(type,key) 去重合并**（confidence 取 max、seen+1、id 保留）是比 gstack 单纯追加更进一步的地方——反复验证同一规律会自然加深其 confidence，而不是堆重复行。`compile`（非 stale pattern、confidence≥6、top-10、降序）产出 `## 已验证经验 (verified learnings)` markdown，与 handoff 注入同源同阈值。**默认开启、纯关键词检索零依赖、跨 feature 默认关、全程 fail-open**（无 learnings 也不报错、不阻断任何工具调用，是质量增强不是刹车）。schema 见 `assets/checkpoint.schema.md` 末节。

   > **抗 flip-flop 收敛（2026-06-22）**：`verify_done.py` 现做 **K-连续-PASS 门控**——退出码 0 表示"已达 K 连续 PASS 收敛"，退 1 现包含**"本次 PASS 但 streak<K（尚未收敛，需再迭代）"**这一新情形。驱动循环应继续迭代直到退 0，**不要把单次 PASS 当完成**。任何一次 FAIL 清零 streak（中途 flip-flop 无法蒙混过关）。K 优先级 env `DURABLE_LOOP_CONVERGE_K` > checkpoint.`converge_k` > 默认 2；判定逐条 append 到 checkpoint.`verify_history`。该门控**仅在 checkpoint.json 存在时启用**；无 checkpoint 退化为原单次判定（向后兼容）。

7. **每 N 轮 full context reset（已脚本化，2026-06-22）**。`durable_loop_checkpoint.py` Stop hook 现会**自动**在 `iteration % reset_every_n == 0`（默认 N=5，0=禁用，iteration=0 不触发）时从 cumulative_state 渲染**约束类事实**（当前目标、已完成 artifacts+decisions、不变量、已否决方案、下一轮第一步、预算观测）刷新 `.scratch/<feature>/handoff.md`，把旧版归档到 `.scratch/<feature>/handoff_archive/iter_<N>.md`，并置 checkpoint.`reset_due=true`。driver resume 时若读到 `reset_due==true`，则触发 `/context-restore` 或 strategic-compact 做 full reset、从 handoff 重建 minimal context，**并把 `reset_due` 清回 false/删除该键**。整个 handoff 刷新 fail-open（不阻断 Stop、不污染 checkpoint）。**不要用 semantic summary 压缩**——它会丢"为什么否决方案X"这类约束，导致 reset 后重复踩坑。Ralph Loop 实测 90 分钟退化成 tunnel vision。大输出 offload 到 `.scratch/intermediate/` 防爆 context。

8. **危险操作守卫**。push --force / reset --hard / clean -fd / branch -D 等危险 git 操作交给已有的 `git-guardrails-claude-code` skill 拦截，**本 skill 不重写**。关键节点（合并、发布、删除数据）写 `.scratch/<feature>/pending_approval.json` 触发 HITL breakpoint（checkpoint.status=`paused_for_approval`，**不是** `paused`——resume 门只认 `paused_for_approval`，写 `paused` 会被当成 fresh start 丢全部 state）。

   > **可选 PreToolUse 守卫 `durable_loop_guard.py`（2026-06-22，默认 opt-in）**：本 skill 现自带一个**可选第 3 个 PreToolUse hook**，两项功能——(1) **幂等门**（发现单一 active 循环时默认开启）：即将执行的工具调用若派生出的副作用 key 已在 checkpoint.`idempotency_keys` 中则 deny，与 Stop hook 的 record 构成 replay-block 闭环；(2) **strict 危险操作硬拦截**（**默认关闭**，需 env `DURABLE_LOOP_STRICT=1/true/yes/on` 或 checkpoint.`strict_guard=true` 显式 opt-in）：命中危险模式即 deny 并 best-effort 写 `pending_approval.json`。全程 fail-open（无 checkpoint / >1 feature 歧义 / inactive status 一律放行）。这是**纯质量收敛理念不变**的前提下的可选增强——**不接线就完全没有任何拦截**；要启用见 step 9 hook 套装。

9. **装 v2 自动化 hook 套装（强烈推荐，治本）**。durable-loop v2 把实例诊断发现最易失约的 4 个软约束升级成 hook 强制——agent 想偷懒都偷不了。装 3 个 hook（全部 fail-open，无 active loop 时不干预其他 session）：

   | hook 脚本 | 事件 | 强制什么 | 替代的软约束 |
   |---|---|---|---|
   | `scripts/durable_loop_observe.py` | PostToolUse | 每个工具调用自动 append 命令级 session.log（含 run_id，供 replay_trace.py 分组复盘） | "agent 每轮记得写 session.log"（实测最易失约） |
   | `scripts/durable_loop_checkpoint.py` | Stop | 读 transcript 统计 token/**dollars** 写 budget_used（**纯观测**，2026-06-19 起不再 gate）+ 检测 Write/Edit/git 副作用更新 idempotency_keys + 算 hours **+ 每 reset_every_n 轮自动刷新 handoff/归档/置 reset_due（2026-06-22 新增）+ 刷新 handoff 时直接读 learnings.jsonl 注入「## 已验证经验 (verified learnings)」段（pattern-only、非 stale、confidence≥6、top-10，fail-open，#3）** | "agent 每轮累加 budget + 记 idempotency key + 每 N 轮写 handoff + 把已验证经验带进 reset 后的 context" |
   | `scripts/durable_loop_guard.py`（**可选第 3 个，默认 opt-in**） | PreToolUse | (1) 幂等门：副作用 key 命中 idempotency_keys 即 deny（与 Stop record 闭环）；(2) strict 危险操作硬拦截（**默认关闭**，env `DURABLE_LOOP_STRICT` 或 checkpoint.`strict_guard=true` 显式开启） | 重放副作用 / 危险不可逆操作（默认不接线则无任何拦截） |
   | `scripts/check_progress.py`（**可选，默认不接线**） | Stop | no-progress 探测：相邻 N 轮信号逐字节相同→写 pending_approval.json 暂停（**默认关闭**，env `DURABLE_LOOP_NOPROGRESS_N` 或 checkpoint.`no_progress_limit>0` 开启） | 原地打转无进展（默认关，永远 exit 0 不阻断） |
   | ~~`scripts/check_budget.py`~~（2026-06-20 移除） | ~~PreToolUse~~ | thrashing 防卡死——**已按需移除**（纯质量收敛）；PreToolUse hook 从 settings.json 摘除，脚本保留作参考 | 防卡死护栏（已移除） |

   **默认仍只装 2 个非阻断 hook**（observe PostToolUse + checkpoint Stop）——都**非阻断**（只观测/写 state，不会拦任何工具调用）。**`durable_loop_guard.py` 是可选第 3 个 PreToolUse hook，默认 opt-in**：不接线则纯质量收敛、零拦截；接线后幂等门默认生效、strict 拦截仍需 env/字段显式开启。`check_progress.py` 同样默认不接线。observe / checkpoint / guard / check_progress 共用同一 discover 逻辑（cwd 下单一 `.scratch/<feature>/` 自动识别，>1 feature 静默 no-op），互不冲突：observe 只 append session.log，checkpoint 是**唯一**写 checkpoint.json 的（原子 tmp+mv），guard 只读 checkpoint 做 allow/deny。

   **合并步骤**：优先用 harness 内置 `/update-config` skill；否则手工 **append** 到 settings.json（**Windows**：`C:/Users/<YOU>/.claude/settings.json`；**macOS/Linux**：`~/.claude/settings.json`）。⚠️ 若在 WSL bash.exe 里编辑会落到 `/root/.claude/` 或 `/home/<user>/.claude/`——那个文件 Windows 版 Claude Code **不读**。保留现有条目，不覆盖整个数组。**只 append 2 条**（PostToolUse + Stop；纯质量收敛，无 PreToolUse 阻断 hook）：

   ```
   # macOS/Linux（bash 下 $HOME 展开；用 python 不是 python3）:
   PostToolUse: { "hooks": [{"type":"command","command":"python \"$HOME/.claude/skills/durable-loop/scripts/durable_loop_observe.py\""}]}
   Stop:        { "hooks": [{"type":"command","command":"python \"$HOME/.claude/skills/durable-loop/scripts/durable_loop_checkpoint.py\""}]}

   # Windows（PowerShell 主力：用绝对正斜杠路径 + python；不要用 python3/$HOME/~，它们在 PowerShell/cmd 下不解析）:
   PostToolUse: { "hooks": [{"type":"command","command":"python \"C:/Users/<YOU>/.claude/skills/durable-loop/scripts/durable_loop_observe.py\""}]}
   Stop:        { "hooks": [{"type":"command","command":"python \"C:/Users/<YOU>/.claude/skills/durable-loop/scripts/durable_loop_checkpoint.py\""}]}

   # 可选第 3 个（默认 opt-in，要幂等门/strict 拦截才装）——PreToolUse 守卫：
   PreToolUse:  { "hooks": [{"type":"command","command":"python \"C:/Users/<YOU>/.claude/skills/durable-loop/scripts/durable_loop_guard.py\""}]}
   # 可选 no-progress 暂停（默认不接线，要 DURABLE_LOOP_NOPROGRESS_N>0 才有意义）——再挂一个 Stop hook：
   #   Stop: ... + python "C:/Users/<YOU>/.claude/skills/durable-loop/scripts/check_progress.py"
   ```

   （PostToolUse/Stop 不加 matcher = 匹配所有工具；前两者都**非阻断**，只观测/写 state。Windows 下 `<YOU>` 换成你的用户名。脚本都 fail-open：无 `.scratch/<feature>/` 时静默 no-op，全局安装安全。**纯质量收敛理念不变**：默认仍只装 observe+checkpoint 两个非阻断 hook；`durable_loop_guard.py` 是**可选**的 PreToolUse hook，装了才有幂等门（strict 拦截还要 env/字段显式开启），不装则零拦截。原 check_budget PreToolUse 阻断 hook 2026-06-20 按需移除——只有 verify_done 决定 done。）

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

> **v2 自动化已 hook 化**：状态持久化（checkpoint.py Stop）、重试幂等（checkpoint.py 检测副作用）、可观测（observe.py PostToolUse，含 run_id）这 **3 个要素已 hook 强制**（都非阻断，默认装）。**上下文管理 2026-06-22 也脚本化**：checkpoint.py Stop 现每 `reset_every_n` 轮自动刷新 handoff/归档/置 reset_due。收敛校验由 verify_done.py 升级为**抗 flip-flop K-连续-PASS**（唯一 gate）。HITL 多了**可选** `durable_loop_guard.py` PreToolUse 守卫（幂等门 + opt-in strict 拦截）与**可选** `check_progress.py` no-progress 暂停（**两者默认不接线/不开启**）。调度选型多了 `emit_schedule.py` 脚手架，trace 复盘多了 `replay_trace.py`。预算护栏 2026-06-19、thrashing 2026-06-20 相继按需移除——**纯质量收敛理念不变**：默认仍是非阻断的 verify_done 唯一 gate，所有刹车类（strict / no-progress）默认关闭、需显式 opt-in。

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
- **机械验收 done（抗 flip-flop K-连续-PASS）** → `python scripts/verify_done.py <feature>`（Windows/跨平台）/ `bash scripts/verify_done.sh <feature>`（Unix）；退 0=已收敛(K 连续 PASS)，退 1 含"单次 PASS 但 streak<K"。K 由 env `DURABLE_LOOP_CONVERGE_K` > checkpoint.`converge_k` > 默认 2
- **经验沉淀层 learnings（reflect 闭环，#3 2026-06-22）** → `python scripts/durable_loop_learn.py <log|search|prune|compile> <feature> [project_dir] [options]`。`log`=记录/合并（同 (type,key) 去重：confidence max + seen+1 + id 保留）；`search`=关键词检索历史经验（reflect-in，`--cross-feature` 默认关）；`prune`=按 source 失效标 stale（默认 dry-run，`--apply` 才写）；`compile`=产出「## 已验证经验」markdown（pattern-only、confidence≥6、top-10）。存储 `.scratch/<feature>/learnings.jsonl`（11 字段，schema 见 `assets/checkpoint.schema.md` 末节）。**默认开启、纯关键词零依赖、跨 feature 默认关、全程 fail-open、不阻断任何调用**；handoff reset 时由 checkpoint hook 同源注入。`init_loop.py` 在 init/`--force` 时 scaffold 空 learnings.jsonl 且永不清空已有内容
- **trace 复盘** → `python scripts/replay_trace.py <feature> [project_dir]`（只读 reporter：把 session.log 按 run_id/iter 分组，统计工具直方图、phase 变迁、cost/timing 汇总；不写文件、不拦截、fail-open exit 0）
- **调度脚手架** → `python scripts/emit_schedule.py <feature> <min|hours|days|long> [project_dir] [--interval X] [--command Y] [--stdout]`（把调度决策表落地为 /loop 用法 / Scheduled Task / Cloud Routine / GitHub Actions 骨架）
- **~~thrashing 防卡死护栏~~（2026-06-20 移除）** → 原 `scripts/check_budget.py` PreToolUse hook，已按需移除（纯质量收敛）；脚本保留作参考
- **session.log 自动记录** → `scripts/durable_loop_observe.py`（PostToolUse hook，命令级自动 append，含 run_id）
- **token/幂等/hours 自动同步 + 每 N 轮自动 handoff/reset** → `scripts/durable_loop_checkpoint.py`（Stop hook，读 transcript 写 checkpoint；reset_every_n 节律刷新 handoff/归档/置 reset_due）
- **可选 PreToolUse 守卫（幂等门 + strict 拦截，默认 opt-in）** → `scripts/durable_loop_guard.py`（不接线则零拦截；strict 还要 env `DURABLE_LOOP_STRICT` 或 checkpoint.`strict_guard=true`）
- **可选 no-progress 暂停（默认关闭）** → `scripts/check_progress.py`（CLI 或 Stop hook；env `DURABLE_LOOP_NOPROGRESS_N` 或 checkpoint.`no_progress_limit>0` 开启）
- **危险 git 操作拦截** → 复用 `git-guardrails-claude-code` skill（不重写）
- **context reset 执行** → 复用 `strategic-compact` / `context-save` / `context-restore` skill
- **质量门** → 复用 `verification-loop` skill
