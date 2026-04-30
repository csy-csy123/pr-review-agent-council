# PR Review Agent Council

一个基于 **Aliyun DashScope / Qwen** 的 LLM-first 多 Agent PR 代码审查系统。项目参考并改造了 `learn-claude-code` 的 Agent 工程范式，将 Tool Calling、Skill Loading、Todo Tracking、JSONL Transcript、结构化输出和多 Agent 协作机制应用到 PR Review 场景。

它不是简单的“把代码丢给大模型问有没有 bug”，而是模拟一个代码审查委员会：

```text
Lead Reviewer 规划任务
    -> Security / Correctness / Test / Maintainability Reviewers 分工审查
    -> 本地规则兜底
    -> EvidenceStore 绑定证据链
    -> Critic Reviewer 质疑 finding
    -> Lead Reviewer 最终裁决
    -> 输出 Markdown / JSON / JSONL
```

## 项目来源与定位

本项目基于 `learn-claude-code` 的编码 Agent 练习思路扩展而来，但没有直接依赖 Claude Code SDK。项目重点是复刻并工程化 Claude Code/Codex 类 Agent 系统中的关键设计：

- Tool Calling：将 Git diff、文件上下文、测试执行、secret scan 封装为受控工具。
- Skill Loading：通过 `skills/code-review/SKILL.md` 加载代码审查规范，并注入全链路 LLM prompt。
- Todo Tracking：记录审查阶段状态。
- Multi-Agent System：Lead、Specialist、Critic 多角色协作。
- MessageBus：记录 Agent 之间的任务、候选问题、质疑、答辩和裁决。
- EvidenceStore：为每个 finding 绑定 diff 行、源码上下文、critic review 和 lead resolution。
- FindingLifecycle：管理 `candidate -> challenged -> accepted/rejected/downgraded`。
- JSONL Transcript：记录工具调用、LLM 调用、Agent 消息和状态流转。
- Structured Output：要求 Qwen 返回 JSON，并进行 schema validation。

底层 LLM 使用阿里云 DashScope 的 OpenAI-compatible Chat Completions API，默认模型为 `qwen-turbo-latest`。

## 核心能力

- LLM Lead Reviewer：调用 Qwen 生成 review plan，并在最后做 accepted/rejected/downgraded 裁决。
- LLM Specialist Reviewers：Security、Correctness、Test、Maintainability 四个 reviewer 分角色审查 Git diff。
- LLM Critic Reviewer：对每个 candidate finding 做反向审查，判断证据是否充分、严重等级是否合理、是否和本次 diff 相关。
- 本地规则兜底：稳定识别硬编码密钥、SQL 拼接、`shell=True`、可变默认参数、吞异常、缺测试等高确定性问题。
- Evidence-based Review：每个 finding 都绑定证据链，而不是只有自然语言结论。
- CI-friendly Output：生成 `report.md`、`findings.json` 和 `transcript.jsonl`。

## 项目结构

```text
agents/review_agent.py              # 核心实现：LLM client、工具、Agent Council、报告生成
skills/code-review/SKILL.md         # 代码审查 skill：checklist、severity/category、finding schema
demo/pr-fixture/payment_risk.py     # 支付风控 demo PR，包含多类风险场景
docs/demo-pr.md                     # demo PR 描述
docs/中文教程-Agent简历面试.md      # 中文教程、简历写法和面试问答
tests/test_review_agent.py          # 单元测试和 council 集成测试
```

## 快速开始

### 1. 安装依赖

本项目主要依赖 Python 标准库，测试依赖 pytest。

```powershell
cd D:\pr-review-agent-council
D:\envs\mind\python.exe -m pytest -p no:cacheprovider
```

### 2. 配置 DashScope API Key

在项目根目录创建 `.env`：

```text
DASHSCOPE_API_KEY=your_dashscope_api_key
```

`.env` 已加入 `.gitignore`，不要提交到 GitHub。

### 3. 运行完整 LLM Agent Council

```powershell
cd D:\pr-review-agent-council
D:\envs\mind\python.exe agents\review_agent.py --repo . --base HEAD~1 --target HEAD --pr-description docs\demo-pr.md --language zh --llm-provider aliyun
```

如果只想跑本地规则、不调用 Qwen：

```powershell
D:\envs\mind\python.exe agents\review_agent.py --repo . --base HEAD~1 --target HEAD --pr-description docs\demo-pr.md --language zh --llm-provider none
```

## CLI 参数

```text
--repo              要审查的本地仓库路径
--base              基线提交或分支，例如 HEAD~1 / main
--target            目标提交或分支，例如 HEAD / feature-branch
--pr-description    PR 描述 Markdown 文件
--test-command      可选测试命令，例如 "python -m pytest"
--language          报告语言：zh / en
--mode              council / simple，默认 council
--critic-pass       是否启用 critic 流程：true / false
--llm-provider      aliyun / none，默认 aliyun
--llm-model         DashScope 模型名，默认 qwen-turbo-latest
--llm-base-url      DashScope OpenAI-compatible base URL
```

## 输出文件

运行后会生成：

```text
.review-agent/report.md        # 给人看的 Markdown 审查报告
.review-agent/findings.json    # 给 CI 或平台消费的结构化结果
.review-agent/transcript.jsonl # Agent trace：工具调用、LLM 调用、消息、证据和状态流转
```

`findings.json` 中的 verdict 可用于 CI 门禁：

```text
approve          无需阻塞
comment          有 P2/P3 问题
request_changes  有 P0/P1 问题，建议阻塞合并
```

## 如何确认真的调用了 Qwen

查看 transcript：

```powershell
Select-String -Path .review-agent\transcript.jsonl -Pattern "llm.request","llm.response","llm.error","llm.skipped"
```

完整 LLM Agent Council 应该能看到：

```text
lead-reviewer.plan
security-reviewer
correctness-reviewer
test-reviewer
maintainability-reviewer
critic-reviewer
lead-reviewer.resolve
```

如果看到 `llm.skipped`，说明没有读取到 `DASHSCOPE_API_KEY`。如果看到 `llm.error`，说明网络或 API 调用失败。

## Demo 场景

`demo/pr-fixture/payment_risk.py` 模拟一个支付风控 PR，包含：

- 硬编码 token / webhook secret。
- SQL f-string 拼接。
- `shell=True` 命令执行面。
- 商户白名单绕过风控。
- 高风险国家小额支付直接放行。
- webhook 签名失败仍继续接受事件。
- 进程内列表实现幂等，重启或多实例失效。
- `float` 处理金额精度风险。
- 敏感信息日志泄露。
- 可变默认参数和吞异常。

这些问题中，一部分适合规则兜底识别，另一部分需要 Qwen 做业务语义判断。

## Agent 工作流

```text
ReviewAgent
  ├─ load .env
  ├─ load code-review skill
  ├─ ReviewTools: changed_files / git_diff / file_context / tests
  ├─ Lead Reviewer: Qwen plan_review()
  ├─ Specialist Reviewers: Qwen review() + local rules
  ├─ EvidenceStore: bind reviewer explanation / diff line / file context
  ├─ Critic Reviewer: Qwen critique_finding()
  ├─ Lead Reviewer: Qwen resolve_finding()
  └─ ReportWriter: report.md / findings.json / transcript.jsonl
```

## GitHub 上传注意事项

可以上传 GitHub，但请务必确认：

- 不要提交 `.env`，里面有真实 API Key。
- 不要提交 `.review-agent/`，里面可能包含 LLM 输出和审查上下文。
- 不要提交 `.tmp/`、`__pycache__/`、`.pytest_cache/`。
- 如使用 JetBrains/PyCharm，建议不要提交 `.idea/`。
- 如果曾经误提交过 API Key，需要立刻在 DashScope 控制台吊销并重新生成。

当前 `.gitignore` 已包含这些路径。

## 技术关键词

Python, Aliyun DashScope, Qwen, OpenAI-compatible Chat Completions, LLM Agent, Multi-Agent System, Tool Calling, Skill Loading, Todo Tracking, MessageBus, Agent Communication, EvidenceStore, FindingLifecycle, Critic Agent, Lead Agent Planning, Structured Output, JSON Schema Validation, JSONL Trace, Git Diff Analysis, pytest

## 简历一句话

参考 Claude Code/Codex Agent 工程范式，基于 Python + Aliyun DashScope/Qwen 实现 LLM-first Multi-Agent PR Review Council，通过 Tool Calling、Skill Loading、MessageBus、EvidenceStore、FindingLifecycle、Critic Agent 和 JSONL Trace 完成可解释、可审计、可接入 CI 的 PR 风险审查。
