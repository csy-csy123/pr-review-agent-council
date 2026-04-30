# PR Review Agent Council

一个本地可运行的多 Agent PR 风险审查系统。它面向企业研发代码审查场景，输入 Git diff / base-target 范围后，自动调度多个 reviewer，生成中文 Markdown 报告、结构化 JSON findings 和 JSONL transcript。

## 核心能力

- Lead Reviewer 负责任务拆解、调度和最终仲裁
- Security / Correctness / Test / Maintainability Reviewer 做专项审查
- Critic Reviewer 对高风险或证据不足的 finding 发起质疑
- MessageBus 记录 Agent 间 `task_assignment`、`candidate_finding`、`challenge`、`defense`、`resolution`
- FindingLifecycle 管理 `candidate -> challenged -> accepted / rejected / downgraded`
- EvidenceStore 绑定 diff 行、源码上下文、测试输出、reviewer 解释和 critic 质疑
- 默认中文输出 Markdown 报告和 JSON findings

## 快速运行

```powershell
cd D:\pr-review-agent-council
D:\envs\mind\python.exe -m pytest
D:\envs\mind\python.exe agents\review_agent.py --repo . --base HEAD~1 --target HEAD --pr-description docs\demo-pr.md
```

输出文件：

```text
.review-agent/report.md
.review-agent/findings.json
.review-agent/transcript.jsonl
```

## CLI

```powershell
D:\envs\mind\python.exe agents\review_agent.py --repo . --base HEAD~1 --target HEAD --pr-description docs\demo-pr.md
```

常用参数：

- `--repo`：要审查的本地仓库路径
- `--base`：基线提交或分支
- `--target`：目标提交或分支
- `--pr-description`：PR 描述文件
- `--mode council`：默认，多 Agent 审查委员会模式
- `--mode simple`：快速审查模式
- `--critic-pass true/false`：是否启用 Critic Reviewer 质疑流程
- `--language zh/en`：报告语言，默认中文

## 项目结构

```text
agents/review_agent.py              # 主 Agent、工具、Review Council、报告生成
demo/pr-fixture/payment_risk.py     # demo PR 风险代码
docs/demo-pr.md                     # demo PR 描述
skills/code-review/SKILL.md         # 审查标准和 finding schema
tests/test_review_agent.py          # 核心单元测试和集成测试
```

## 简历关键词

Python、Multi-Agent Collaboration、Agent Workflow、Tool Calling、Git Diff Analysis、Message Bus、Evidence Store、Finding Lifecycle、JSONL Observability、pytest
