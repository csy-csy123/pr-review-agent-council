# Agent 开发岗简历写法

本文整理 `PR Review Agent Council` 项目在 Agent 开发岗、LLM 应用开发岗、AI Engineer 实习面试中的简历表达方式。写法重点放在 Agent 架构、ReAct 式控制循环、Tool Calling、Skill Loading、Agent 状态管理、结构化输出和评估机制上。

## 推荐简历版本

**PR Review Agent Council：多 Agent 代码审查系统**

基于 Python + Qwen/DashScope 构建 Debate Council PR Review Agent，将代码审查拆解为 Reviewer、Critic、Lead Controller、ReportWriterAgent、AI Judge 等角色，实现从代码理解、问题发现、质疑补证到报告评估的多 Agent 审查链路。

- 设计多 Agent Debate 架构：Reviewer 生成候选 finding，Critic 质疑证据与严重级别，Lead Controller 负责动态裁决，ReportWriterAgent 输出标准化报告，形成可控的 PR Review Agent 流程。
- 借鉴 ReAct 思路设计 Lead Controller 驱动的动态审查 loop，根据代码证据与 finding 状态调度 Critic 质疑、Reviewer 补证、重复项合并和最终裁决，形成围绕 finding 质量控制的多 Agent 闭环。
- 实现 Agent State / Lifecycle Management，维护“候选、被质疑、已接受、已拒绝、已降级”等 finding 状态，并将 diff 行、源码上下文、质疑意见、Reviewer 补证、Lead 裁决理由绑定为证据链，支撑 Lead Controller 基于状态做动态决策。
- 实现 Tool Calling + Skill Loading 机制，将 `git_diff`、`read_file_context`、`search_code`、`secret_scan` 等只读工具和 code-review skill 注入 prompt，使 LLM 基于统一审查规范与真实证据生成结构化 finding。
- 设计结构化输出与 Trace 机制，约束 Reviewer、Critic、Lead Controller、ReportWriterAgent、AI Judge 输出固定 JSON schema，并通过 JSONL Transcript 记录工具调用、Agent 通信、Debate 决策、证据更新和裁决过程，提升系统可观测性与可复盘性。
- 在支付风控类 PR 审查场景中，相比固定 council baseline，Debate 模式 AI Judge 总分从 72 提升至 92；严重级别准确性 70 -> 90，重复/噪音控制 40 -> 98，证据质量 85 -> 95。

## 精简版

适合简历空间较紧时使用。

**PR Review Agent Council：多 Agent 代码审查系统**

- 基于 Python + Qwen/DashScope 构建 Debate Council PR Review Agent，设计 Reviewer、Critic、Lead Controller、ReportWriterAgent、AI Judge 多 Agent 协作架构。
- 借鉴 ReAct 思路实现 Lead Controller 驱动的动态审查 loop，根据代码证据与 finding 状态调度质疑、补证、合并和裁决等 action。
- 实现 Tool Calling + Skill Loading，将 Git diff、代码上下文、代码搜索、密钥扫描和 code-review skill 注入 prompt，使 LLM 基于统一规范与真实证据生成结构化 finding。
- 通过 finding 生命周期管理、结构化 JSON 输出和 JSONL Transcript Trace，提升 Agent 系统的可解释性、可观测性和可复盘性。
- 在支付风控类 PR 审查场景中，相比固定 council baseline，Debate 模式 AI Judge 总分从 72 提升至 92。

## 面试一句话介绍

我做的是一个多 Agent PR Review 系统，不是简单把 diff 丢给 LLM，而是把代码审查拆成 Reviewer、Critic、Lead Controller、ReportWriterAgent 和 AI Judge 等角色；Lead Controller 借鉴 ReAct 思路，根据 finding 状态和证据链动态调度质疑、补证、合并和裁决，并通过 Tool Calling、Skill Loading、结构化输出和 Transcript Trace 让审查结果更可控、更可解释。

## 关键词

`LLM Agent`、`Multi-Agent System`、`ReAct`、`Lead Controller`、`Tool Calling`、`Skill Loading`、`Agent State Management`、`Finding Lifecycle`、`Structured Output`、`JSON Schema`、`Transcript Trace`、`LLM-as-Judge`、`Qwen/DashScope`

## 新版本补充：公司知识 RAG 对齐

如果 GitHub 版本已经包含 `knowledge/company/`、`CompanyKnowledgeBase`、`retrieve_company_policy` 和 `company-policy-reviewer`，简历可以把项目从“多 Agent 代码审查”升级为“企业规范对齐的 PR Review Agent”。

推荐新增一句：

> 新增公司知识 RAG 模块，将安全基线、支付审核规范、测试策略和历史事故案例切分为 Markdown chunks，调用 DashScope OpenAI-compatible `text-embedding-v4` 构建向量索引，并通过 embedding similarity + keyword fallback 检索相关条款，为 finding 注入 `company_policy` 证据。

推荐新增一条项目 bullet：

- 新增 `company-policy-reviewer`，基于 `risk_scan + retrieve_company_policy` 主动发现违反公司规范的问题，并将命中的规范写入 `EvidenceStore` 与标准化报告的 `policy_references`，使 Critic、Lead Controller 和 AI Judge 能依据企业私有规范进行质疑、裁决和评估。

面试时可以这样解释 RAG 的位置：

1. review 前检索公司规范，注入 `<company_knowledge>` 作为上下文。
2. finding 生成后再次检索规范，作为 `company_policy` 证据挂到 evidence chain。
3. 新增 `company-policy-reviewer` 主动发现公司规范违规，避免只对已有 finding 做事后补证。

更完整的简历写法、面试问答和边界表述见 [resume-company-rag-update.md](resume-company-rag-update.md)。
