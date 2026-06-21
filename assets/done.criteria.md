# done.criteria.md — 收敛条件模板

> ⚠️ **绝不 let model 自评 done**。收敛判定只能跑 `scripts/verify_done.sh`（机械检查 + 独立 evaluator model），actor 自己说"完成了"一律不算。
> ⚠️ **generator 和 evaluator 必须分离**。让同一个 model 既写代码又判 done，系统性降低准确率（Huang 判据：纯内在 self-correction 让 model 倾向于在 30% 完成度就声明 done，把 `assert True` 塞进测试 gaming 单一收敛条件）。
> ⚠️ 收敛条件必须 **QUANTITY + QUALITY 双维度同时为真**，单维度（只有"测试通过"）会被 gaming。

复制到 `.scratch/<FEATURE>/done.criteria.md` 后，按任务改写每条的具体阈值与命令。改完不得在 loop 运行中途修改——若要改，先停 loop、写 ADR 到 `decisions.log`、再 resume。

---

## QUANTITY（可机械验证的数量维度）

全部为真才算通过。命令列在每条后面，由 `verify_done.sh` 执行。

- [ ] **测试全绿**：`pytest -x --tb=short` 退出码 0（或对应语言的 `npm test` / `go test ./...`）。`--tb=short` 是为了把 traceback 写进 log 而非堆在终端。 <!-- cmd: pytest -x --tb=short -->
- [ ] **覆盖率达标**：`pytest --cov=src --cov-fail-under=80`，阈值 `>= 80%`。低于则 FAIL。 <!-- cmd: pytest --cov=src --cov-fail-under=80 -->
- [ ] **lint clean**：`ruff check .`（Python）/ `eslint .`（JS）/ `golangci-lint run`（Go）退出码 0。 <!-- cmd: ruff check . -->
- [ ] **typecheck clean**：`mypy src/`（Python）/ `tsc --noEmit`（TS）退出码 0。禁止用 `# type: ignore` 压平告警。 <!-- cmd: mypy src/ -->
- [ ] **新增/修改文件数在预期内**：`git diff --stat` 的文件数 `<= <MAX_FILES_CHANGED>`（防 scope 蔓延）。 <!-- cmd: [ "$(git diff --name-only | wc -l)" -le <MAX_FILES_CHANGED> ] -->

---

## QUALITY（防 gaming 的质量维度）

这些条件防止"测试通过但啥也没测"的反模式。需独立 evaluator model（非 actor）或人工抽检核对。

- [ ] **每测试类测不同 module**：用 `grep -rE "^class Test" tests/ | wc -l` 与 `ls src/*.py | wc -l` 比对，禁止多个 test class 全测同一个 module。防 `assert True` gaming。 <!-- cmd: [ "$(grep -rE '^class Test' tests/ 2>/dev/null | wc -l)" -ge "$(ls src/*.py 2>/dev/null | wc -l)" ] -->
- [ ] **无 unreferenced dead code**：`vulture src/ --min-confidence 80`（Python）/ `ts-prune`（TS）无新增报告项。 <!-- cmd: vulture src/ --min-confidence 80 -->
- [ ] **无 debug 残留**：`grep -rnE "(console\.log|print\(|debugger|pdb\.set_trace)" src/` 无命中（除显式 logger 调用）。 <!-- cmd: ! grep -rnEq '(console\.log|print\(|debugger|pdb\.set_trace)' src/ -->
- [ ] **关键决策已记录**：`.scratch/<FEATURE>/decisions.log` 至少含 `<MIN_DECISIONS>` 条带时间戳、决策内容、替代方案、否决理由的条目。 <!-- cmd: test -f .scratch/<FEATURE>/decisions.log && [ "$(grep -c '' .scratch/<FEATURE>/decisions.log)" -ge <MIN_DECISIONS> ] -->
- [ ] **每条 public API 有对应测试**：手动跑 `coverage report --show-missing --include='src/**/*.py'`，核对 src 下每个 `def` / `class` 至少有一行被测到。（MANUAL — 逐 def 覆盖无法用单条 shell 命令表达，由独立 evaluator 按 --show-missing 抽检）
- [ ] **无新增的 `# TODO` / `# FIXME`**（除非显式记到 issue tracker）：`git diff main...HEAD` 无新增命中。 <!-- cmd: ! git diff main...HEAD | grep -qE '^\+.*(TODO|FIXME)' -->
- [ ] **副作用幂等性已验证**（MANUAL — 需人工/evaluator 抽检）：对每个外部调用（付款/发邮件/写 DB），checkpoint.json 的 idempotency_keys 列表覆盖到，且重放同一 key 确认返回缓存结果而非重复执行。

---

## evaluator 独立判定（非 actor 自评）

由独立 model（建议用与 actor 不同 family 的 model，如 actor=GLM，evaluator=本地 Llama 或人）执行：

- [ ] **evaluator 确认 actor 产出的代码确实实现了 TASK_DESCRIPTION**（不是占位 stub、不是 mock 假装通过）。
- [ ] **evaluator 确认测试断言有效**：随机抽 3 个测试，逐个核对断言语义是否真的约束了被测行为。
- [ ] **evaluator 独立确认 done 并在 `.scratch/<FEATURE>/decisions.log` 追加一行 `JUDGE: PASS` 或 `JUDGE: FAIL: <reason>`**（MANUAL — 由人工或独立模型执行，不跑 shell 命令）。

只有 QUANTITY 全绿 **且** QUALITY 全绿 **且** evaluator 写 `JUDGE: PASS`，status 才置 `completed`。任何一环 FAIL，回 executing 继续。
