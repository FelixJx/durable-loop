# handoff.md — Context Reset 交接文档

> **用途**：每 N 轮（`RESET_EVERY_N`）或 full context reset 时，loop 从这份文档重建 minimal context。
> **不是 semantic summary**。semantic summary 会丢决策约束类事实（"为什么不能用方案X"、"已经否决的路径"），导致 reset 后 agent 重复踩坑。
> 本文档必须 **逐条保留约束、不变量、已否决方案**——这些是"未来不能违反的事"，比"已经做了什么"更重要。
>
> 复制到 `.scratch/<FEATURE>/handoff.md`，由 loop 在每个 reset 点覆盖更新（保留历史版本到 `.scratch/handoff_archive/iter_<N>.md`）。
> 写完后立刻建议 agent 执行 `/context-restore` 或 strategic-compact 触发 full reset，然后用本文档 + checkpoint.json 重建上下文。

---

## 当前目标

<!-- 一句话。目标不变则不变;目标变更必须先停 loop 改这里再 resume -->
<一句话陈述本轮 loop 要达成的最终状态,例如"让 pay-service 的 charge 端点在重试下幂等,测试覆盖 80%+ 且 evaluator 判 PASS">

---

## 已完成步骤

<!-- 带 iteration 号。只记可复现的实质产出,不记"思考过" -->
- iter 1: <动作> → <产出,如 "src/charge.py 初版,idempotency key 接入">
- iter 2: <动作> → <产出>
- iter 3: <动作> → <产出>

---

## 未完成步骤

<!-- 从 checkpoint.resume_from 衍生。下一轮的待办,具体到命令级 -->
- [ ] <下一个具体动作,例如 "修 charge.py line 47 缺 api_key header,然后重跑 pytest">
- [ ] <再下一个>

---

## 已确认的不变量（约束,别违反）

<!-- 最重要的一节。这些是 reset 后绝不能丢的"护栏"。每条都要可执行验证 -->
- **~~预算上限~~（06-19 移除）+ ~~thrashing~~（06-20 移除）**:纯质量收敛——无预算上限、不因原地打转停。`budget_used` 仅观测成本。唯一硬约束是 verify_done（质量收敛）。
- **idempotency 约束**:每个外部副作用必须带 key,格式 `<op>-<version>-<entity_id>`,执行前查 checkpoint.idempotency_keys 去重。
- **重试策略**:可重试错误(网络/5xx/超时)退避 1/2/4/8s 最多 5 次;永久错误(4xx 参数/权限/404)立即 DLQ 不重试。
- **危险 git 操作**:`git push --force` / `reset --hard` / `clean -fd` / `branch -D` 必须走 git-guardrails-claude-code skill 拦截,不得直接执行。
- **调度间隔**(z.ai/GLM):固定 cron 用 240s 不用 300s(避开 5min cache TTL 边界,否则每次 iteration 都 cache miss 成本翻倍)。
- <任务特有的其他不变量,如 "禁止删 migration 文件"、"禁止改 schema 不写 ADR">

---

## 已尝试失败的方案（别重试）

<!-- 每条带"为什么失败"的证据链接,避免 reset 后重复尝试 -->
- **<方案A>** — 失败原因:<具体>。证据:`.scratch/intermediate/iter_<N>_<desc>.log`。结论:别再用这条路。
- **<方案B>** — 失败原因:<具体>。证据:<url/log 路径>。

---

## 下一步具体动作

<!-- 一条。reset 后 agent 读完应立刻能执行,不用重新规划 -->
<精确指令,例如:"读取 checkpoint.json 确认 phase=executing,执行 `python -m pytest tests/test_charge.py::test_idempotent_charge -x`,把输出存 .scratch/intermediate/iter4_pytest.log,根据结果决定下一步">

---

## 预算消耗（仅观测，不 gate）

<!-- 预算护栏 2026-06-19 已按用户要求移除（只要质量）；这里只记已花多少供参考，不设上限、不影响 loop -->
- 已用 tokens: <budget_used.tokens>
- 已用 dollars: $<budget_used.dollars>（模型价目表估算）
- 已用 hours: <budget_used.hours>（自 started_at）
- 已用 iterations: <iteration>
- ⚠️ 这些数字不再触发停止/限流——纯质量收敛，唯一 gate 是 verify_done（thrashing 已 2026-06-20 移除）

---

## 给下一个 context 的忠告

<!-- reset 后容易再犯的坑、本任务特有的 gotcha。free-form -->
- <例如:"mypy 在 CI 和本地版本不同,用 .scratch/intermediate/ 下 pinned 版本的输出为准,别信本地 mypy 的报错">
- <例如:"stripe 的 idempotency key 重放返回的是第一次的 200 而非真正的 charge,看到 200 别误判为新 charge 成功">
- <例如:"evaluator model 用 X,actor 用 Y,两者对'完成'的判断分歧时以 evaluator 为准,别让 actor 覆盖">
