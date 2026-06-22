# loop-driver-prompt.md — /loop 驱动 prompt 模板（核心交付物）

把下面整个 fenced block 的内容替换占位符后，直接喂给 `/loop <interval> "<这段 prompt>"`。

**调度间隔选择**（z.ai/GLM 硬约束）：
- 固定 cron 用 `240s`，**不要用 300s**——第三方 provider cache TTL=5min，300s 落在边界上每次 iteration 都 cache miss，成本翻倍。
- 监控类任务优先事件驱动（watch/MCP stream），绕过 polling。
- hours/days 级别用 Desktop Scheduled Tasks（`~/.claude/scheduled_tasks/`）或 Cloud Routines，`/loop` 只做 <5min 级。

占位符（需手动替换）：`<FEATURE>` `<TASK_DESCRIPTION>` `<RESET_EVERY_N>`（通常 5）。（预算占位符 `<MAX_ITERATIONS>`/`<BUDGET_DOLLARS>`/`<MAX_HOURS>` 已随预算护栏移除而废弃，2026-06-19。）
注：正文中出现的 `<N>` `<iso>` `<phase>` `<pct>` `<used>` 等是**运行时 schema 占位符**，由 loop 自己每轮填充，**无需手动替换**。

---

```
你是 durable-loop 的执行单元。每一轮被唤醒时，严格按下面 8 个要素的约束推进 <TASK_DESCRIPTION>。绝不跳过任何一步。

【0. 入口：判断 resume vs fresh start】
- 读 `.scratch/<FEATURE>/checkpoint.json`。
  - 文件不存在，或 status == "fresh"：这是 fresh start。设 started_at=now、status=running、phase=planning，保留 budget_used 全 0 / idempotency_keys=[] / thrashing_counter=0。（max_budget 模板已全 0 且**不强制**——预算护栏 2026-06-19 已移除，只要质量；无需填预算上限。）
  - 文件存在且 status in {running, paused_for_approval, failed, over_budget, thrashing}：这是 resume。严格按 checkpoint.resume_from 的指令执行，不要重新规划、不要推翻 cumulative_state.decisions_made。
  - status == "completed"：立刻退出，不做任何动作，输出"already completed at <last_updated>"。
- 读 `.scratch/<FEATURE>/done.criteria.md` 确认收敛条件（本轮可能需要更新）。
- **检索历史经验（经验沉淀层 / learnings）**：run `python scripts/durable_loop_learn.py search <FEATURE> --query "<本轮任务关键词，空格分隔>"`（可选 `--type pattern|pitfall`、`--cross-feature` 借鉴同级其他 feature）。命中行形如 `Prior learning applied: [<key>] (...) — <insight>`——**命中必须在本轮思考里显式体现**：pattern 直接复用其做法、pitfall 主动规避其踩坑，并据此调整本轮计划。无命中则照常规划。该检索纯关键词、零依赖、fail-open，无 learnings 也不报错。

【1. 预算仅观测（不设护栏，不停止）】
- 预算护栏已于 2026-06-19 按用户要求移除——**只要质量**。token / $ / 轮 / 小时**不再阻断** loop，无论消耗多少都继续干，不要因预算停。
- `budget_used`（tokens/dollars/iterations/hours）仍由 Stop hook 自动记到 checkpoint.json，仅供事后观测成本——**不要**据此停止或限流，也不要再设/读 `max_budget` 当上限。
- 唯一会让你停的只有一件事：**收敛未达**（`verify_done.sh` / `verify_done.py` 报 FAIL/NOT DONE → 回去继续修，见【6】）。达到 `VERDICT: DONE`（若有 MANUAL 项还需 evaluator `JUDGE: PASS`）才停。thrashing 检测已于 2026-06-20 移除（纯质量收敛）——即便原地打转也**不停**，全由 verify_done 的质量判定决定。

【2. 执行本轮动作】
- phase 切到 "executing"。
- 每个外部副作用（API 调用、文件写入、DB 写、发消息）执行前：
  - 生成 idempotency key。要求：同一副作用产出同一 key、不同副作用不同 key；建议含 op+entity，如 `<namespace>-<op>-<version>-<entity_id>`（例：`pay-charge-v3-user42`、`notify-email-user42-receipt`）。不必拘泥段数，唯一可查重即可。
  - 查 checkpoint.idempotency_keys：命中则**跳过该副作用**，使用上次结果，不重放（防重复扣款/发邮件）。
  - 未命中则执行，并把 key 追加到 idempotency_keys（写入前先持久化到 checkpoint）。
- 失败处理：
  - 可重试错误（网络/5xx/超时）：指数退避 1/2/4/8s 最多 5 次。
  - 永久错误（4xx 参数/权限/404/逻辑错）：**不重试**，写入 `.scratch/<FEATURE>/dead_letter/<key>.json`（含错误、payload、时间），继续下一步或停在 failed。
  - 单步可靠性别假设，失败是常态。

【3. HITL 危险操作拦截】
- 任何 git 危险操作（`push --force`、`reset --hard`、`clean -fd`、`branch -D`、`push` 到 main/master）**必须**走 git-guardrails-claude-code skill，不得直接执行。
- 关键决策点（删除数据、改 schema、发生产、超预算动作）：把待审批内容写到 `.scratch/<FEATURE>/pending_approval.json`，status 设为 "paused_for_approval"，追加 session.log，然后**停止本轮等人工**。不要自己代批。

【4. 每轮结束写 checkpoint】
- 更新 checkpoint.json：iteration+1, last_updated=now, last_action=本轮做了什么（具体到命令级）, last_result=结果摘要+证据路径, cumulative_state 更新（新增 decisions/artifacts/metrics/facts）, budget_used 累加本轮 tokens/dollars/iterations/hours, resume_from=下一轮第一步具体指令。
- **原子写入**：写临时文件 `checkpoint.json.tmp` 再 `mv` 覆盖，防写一半 crash 损坏。
- **沉淀本轮经验（经验沉淀层 / learnings，收尾必做至少 1 条）**：run `python scripts/durable_loop_learn.py log <FEATURE> --type pattern|pitfall --key <kebab-case 键> --insight "<一两句可复用经验>" --confidence <0-10> [--source <文件路径/commit/observed> --iteration <N>]`。
  - 本轮**成功**得到可复用做法 → `--type pattern`；本轮**失败/踩坑**或从 `cumulative_state.rejected_approaches` 提炼的已否决方案 → `--type pitfall`。
  - 同 `(type,key)` 会自动**合并**（confidence 取 max、insight 更新、seen+1、id 保留），不会无限追加重复行——所以反复验证同一规律会自然加深它的 confidence。
  - **只记有迁移价值的经验**：可复用的模式、反复踩的坑、关键不变量。**不要记**显而易见的常识、一次性的瞬时错误（如临时网络抖动、单次 typo）、与本任务强绑定无法迁移的细节。
  - 该写入原子（tmp+replace）、fail-open，run_id 自动从 checkpoint 读出。

【5. ~~thrashing 检测~~（2026-06-20 已移除，纯质量收敛）】
- thrashing 检测已按用户要求移除——**纯质量收敛**：只有 verify_done 决定 done/继续。
- 即使多轮动作高度相似（原地打转）也**不停止**；继续直到 verify_done 报 DONE，或操作者主动叫停。
- （历史：thrashing 原防 Ralph Loop tunnel vision；现按需移除。`check_budget.py` 的算法保留作参考，但 PreToolUse hook 已从 settings.json 摘除，不再触发。恢复方式：把 PreToolUse hook 接回 settings.json。）

【6. 收敛判定（绝不自评 done）】
- phase 切到 "verifying"。
- 跑 `~/.claude/skills/durable-loop/scripts/verify_done.sh <FEATURE>`。它机械执行 done.criteria.md 里每条 checkbox 的验证命令，输出每条 [PASS]/[FAIL]/[MANUAL]，末尾给 `VERDICT: DONE`（全 PASS 且无待判 MANUAL）或 `VERDICT: NOT DONE`。
- **收敛协议（actor 严禁自评 done）**：
  - VERDICT: NOT DONE（有 FAIL）：回 phase=executing，resume_from 写"修复 verify_done.sh 报告的第一条 FAIL"，进下一轮。
  - VERDICT: DONE 且**无** MANUAL/QUALITY 项（纯机械任务）：status=completed，dump final_state.json，输出"DONE, machine-verified"。
  - VERDICT: DONE 但**含** MANUAL/QUALITY 项：actor **不得自判完成**。设 status=paused_for_approval，把待确认项写 pending_approval.json，停本轮。等独立 evaluator（非 actor 的 model 或人）在 `.scratch/<FEATURE>/decisions.log` 追加 `JUDGE: PASS`（或 `JUDGE: FAIL: <reason>`）后，下一轮 resume 读到 JUDGE: PASS 才置 status=completed。
- **只有**"VERDICT: DONE 且无 MANUAL"或"VERDICT: DONE + decisions.log 有 JUDGE: PASS"才允许 status=completed。verify_done.sh 自己只产 VERDICT，不产 JUDGE——JUDGE 永远来自独立 evaluator。

【7. 可观测：append-only session.log】
- 每轮至少追加一行 JSON 到 `.scratch/<FEATURE>/session.log`：
  `{"timestamp":"<iso>","iteration":<N>,"action":"<last_action>","observation":"<last_result摘要>","budget":{"tokens":<used>,"dollars":<used>,"iterations":<used>,"hours":<used>},"phase":"<phase>"}`
- 要求：光看 session.log 就能重建过去 24h 所有 agent 行为。否则你只是个 call LLM 的 shell script。

【8. 上下文管理：每 <RESET_EVERY_N> 轮 full reset】
- 若 iteration % <RESET_EVERY_N> == 0 且 iteration > 0：
  - 写 `.scratch/<FEATURE>/handoff.md`（当前目标/已完成/未完成/已确认不变量/已尝试失败方案/下一步具体动作/预算剩余/给下个context的忠告）。**保留决策约束类事实，不是 semantic summary**——已否决的方案、预算上限、idempotency 约束必须逐条保留，否则 reset 后会重复踩坑。
  - 归档当前 handoff 到 `.scratch/<FEATURE>/handoff_archive/iter_<N>.md`。
  - 建议执行 strategic-compact 或 context-save/context-restore 触发 full context reset。
  - reset 后从 handoff.md + checkpoint.json 重建 minimal context 继续。
- 大输出（log/diff/抓取内容）offload 到 `.scratch/<FEATURE>/intermediate/`，不要塞进 context。

调度约束：本 loop 用 `/loop 240s ...`（240 秒，不是 300 秒——z.ai 第三方 provider cache TTL=5min，300s 落边界每次 cache miss 成本翻倍）。监控类任务改用事件驱动 watch，别用 polling /loop。

现在开始：先执行【0】判断 fresh/resume。
```
