from __future__ import annotations

import json
import uuid
from pathlib import Path

from agents import review_agent


ROOT = Path(__file__).resolve().parents[1]


def workspace_tmp(name: str) -> Path:
    path = ROOT / ".tmp" / f"{name}-{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_parse_numstat_extracts_changed_files() -> None:
    output = "12\t3\tsrc/app.py\n-\t-\tassets/logo.png\n"

    assert review_agent.parse_numstat(output) == [
        {"file": "src/app.py", "added": 12, "deleted": 3},
        {"file": "assets/logo.png", "added": None, "deleted": None},
    ]


def test_iter_added_lines_tracks_new_file_lines() -> None:
    diff = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -10,2 +10,3 @@
 context
+token = "abc123456789"
 unchanged
"""

    lines = list(review_agent.iter_added_lines(diff))

    assert lines == [{"file": "app.py", "line": 11, "text": 'token = "abc123456789"'}]


def test_finding_collector_validates_schema() -> None:
    collector = review_agent.FindingCollector()

    message = collector.emit(
        {
            "file": "app.py",
            "line": 7,
            "severity": "P1",
            "category": "security",
            "title": "Hardcoded token",
            "evidence": "token = 'abc123456789'",
            "impact": "Credential exposure",
            "suggestion": "Move it to secrets storage",
        }
    )

    assert "Recorded P1 security" in message
    assert len(collector.findings) == 1


def test_security_reviewer_flags_fake_secret() -> None:
    tmp_path = workspace_tmp("security-reviewer")
    transcript = review_agent.Transcript(tmp_path / "transcript.jsonl")
    tools = review_agent.ReviewTools(tmp_path, "main", "HEAD", transcript)
    collector = review_agent.FindingCollector()
    reviewers = review_agent.SpecialtyReviewers(tools, collector, transcript)
    diff = """diff --git a/service.py b/service.py
--- a/service.py
+++ b/service.py
@@ -1,1 +1,2 @@
 print("ok")
+API_TOKEN = "fake_token_for_tests_12345"
"""

    count = reviewers.security(diff)

    assert count == 1
    assert collector.findings[0].severity == "P1"
    assert collector.findings[0].category == "security"


def test_report_writer_outputs_markdown_and_json() -> None:
    tmp_path = workspace_tmp("report-writer")
    collector = review_agent.FindingCollector()
    collector.emit(
        review_agent.Finding(
            file="service.py",
            line=2,
            severity="P1",
            category="security",
            title="Likely secret committed in code",
            evidence='API_TOKEN = "fake_token_for_tests_12345"',
            impact="Credential exposure",
            suggestion="Use an environment variable",
        )
    )
    writer = review_agent.ReportWriter(tmp_path, collector, language="en")

    paths = writer.write()

    report = Path(paths["report"]).read_text(encoding="utf-8")
    payload = json.loads(Path(paths["findings"]).read_text(encoding="utf-8"))
    assert "PR Code Review Agent Report" in report
    assert payload["verdict"] == "request_changes"
    assert payload["findings"][0]["file"] == "service.py"


def test_report_writer_outputs_chinese_markdown_by_default() -> None:
    tmp_path = workspace_tmp("report-writer-zh")
    collector = review_agent.FindingCollector()
    collector.emit(
        review_agent.Finding(
            file="service.py",
            line=2,
            severity="P2",
            category="testing",
            title="Production code changed without nearby test changes",
            evidence="service.py",
            impact="Regression risk",
            suggestion="Add tests",
        )
    )
    writer = review_agent.ReportWriter(tmp_path, collector)

    paths = writer.write()
    report = Path(paths["report"]).read_text(encoding="utf-8")

    assert "\u0050\u0052 \u4ee3\u7801\u5ba1\u67e5 Agent \u62a5\u544a" in report
    assert "\u751f\u4ea7\u4ee3\u7801\u53d8\u66f4\u7f3a\u5c11\u5bf9\u5e94\u6d4b\u8bd5" in report


def test_message_bus_preserves_message_order() -> None:
    tmp_path = workspace_tmp("message-bus")
    transcript = review_agent.Transcript(tmp_path / "transcript.jsonl")
    bus = review_agent.MessageBus(transcript)

    bus.send("lead-reviewer", "security-reviewer", "task_assignment", "check auth")
    bus.send("security-reviewer", "lead-reviewer", "candidate_finding", "P1 issue", "F-001")

    messages = bus.all()
    assert [m["type"] for m in messages] == ["task_assignment", "candidate_finding"]
    assert bus.read("lead-reviewer")[0].finding_id == "F-001"


def test_finding_lifecycle_supports_challenge_and_accept() -> None:
    tmp_path = workspace_tmp("finding-lifecycle")
    transcript = review_agent.Transcript(tmp_path / "transcript.jsonl")
    lifecycle = review_agent.FindingLifecycle(transcript)
    finding = review_agent.Finding(
        file="service.py",
        line=3,
        severity="P1",
        category="security",
        title="Likely secret committed in code",
        evidence="API_TOKEN = 'fake_token_for_tests_12345'",
        impact="Credential exposure",
        suggestion="Move it to secrets storage",
    )

    item = lifecycle.candidate(finding, "security-reviewer")
    lifecycle.challenge(item.finding_id, "critic-reviewer", "needs evidence")
    lifecycle.accept(item.finding_id, "evidence supplied")

    assert lifecycle.accepted()[0].finding_id == "F-001"
    assert lifecycle.accepted()[0].challenged_by == "critic-reviewer"
    assert lifecycle.accepted()[0].resolution == "accepted"


def test_evidence_store_binds_items_to_finding() -> None:
    tmp_path = workspace_tmp("evidence-store")
    transcript = review_agent.Transcript(tmp_path / "transcript.jsonl")
    store = review_agent.EvidenceStore(transcript)

    store.add("F-001", "diff_line", "API_TOKEN = 'fake_token_for_tests_12345'", "security-reviewer")
    store.add("F-001", "critic_challenge", "prove this is merge-blocking", "critic-reviewer")

    chain = store.list("F-001")
    assert [item["source"] for item in chain] == ["diff_line", "critic_challenge"]


def test_search_code_finds_matches_and_blocks_escape() -> None:
    tmp_path = workspace_tmp("search-code")
    (tmp_path / "service.py").write_text("TOKEN = 'fake_token_for_tests_12345'\n", encoding="utf-8")
    transcript = review_agent.Transcript(tmp_path / "transcript.jsonl")
    tools = review_agent.ReviewTools(tmp_path, "main", "HEAD", transcript)

    matches = tools.search_code("TOKEN")

    assert matches == [
        {"file": "service.py", "line": 1, "text": "TOKEN = 'fake_token_for_tests_12345'"}
    ]
    try:
        tools.search_code("TOKEN", path="..")
    except ValueError as exc:
        assert "escapes repository" in str(exc)
    else:
        raise AssertionError("search_code should reject paths outside the repository")


def test_agentic_run_tests_requires_user_test_command() -> None:
    tmp_path = workspace_tmp("agentic-run-tests-disabled")
    transcript = review_agent.Transcript(tmp_path / "transcript.jsonl")
    tools = review_agent.ReviewTools(tmp_path, "main", "HEAD", transcript)
    loop = review_agent.AgenticReviewLoop(
        tools=tools,
        transcript=transcript,
        collector=review_agent.FindingCollector(),
        todos=review_agent.TodoManager(),
        llm_client=None,
        test_command=None,
    )

    observation = loop._call_tool("run_tests", {})

    assert observation["ok"] is False
    assert "test-command" in observation["error"]


def test_risk_scan_extracts_payment_style_signals() -> None:
    tmp_path = workspace_tmp("risk-scan")
    transcript = review_agent.Transcript(tmp_path / "transcript.jsonl")
    tools = review_agent.ReviewTools(tmp_path, "main", "HEAD", transcript)
    tools.git_diff = lambda base=None, target=None: """diff --git a/payment.py b/payment.py
--- a/payment.py
+++ b/payment.py
@@ -1,0 +1,9 @@
+def score_payment(request, db, history=[]):
+    user_id = request.get("user_id", "")
+    cursor.execute(
+        f"SELECT count(*) FROM payments WHERE user_id = '{user_id}'"
+    )
+    print(f"risk check token={API_TOKEN}")
+    subprocess.run(f"echo checking {user_id}", shell=True)
+    except Exception:
+        pass
"""  # type: ignore[method-assign]
    tools.changed_files = lambda base=None, target=None: [{"file": "payment.py", "added": 9, "deleted": 0}]  # type: ignore[method-assign]

    signals = tools.risk_scan()
    names = {item["signal"] for item in signals}

    assert "mutable_default" in names
    assert "sql_interpolation" in names
    assert "sensitive_logging" in names
    assert "shell_execution" in names
    assert "no_tests_changed" in names



def test_aliyun_client_without_api_key_skips_network() -> None:
    tmp_path = workspace_tmp("aliyun-no-key")
    transcript = review_agent.Transcript(tmp_path / "transcript.jsonl")
    client = review_agent.AliyunDashScopeClient(
        api_key="",
        model="qwen-turbo-latest",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        transcript=transcript,
    )

    findings = client.review("security-reviewer", "Security risk reviewer", "", "", "", [], None)

    assert findings == []
    events = [json.loads(line)["event"] for line in (tmp_path / "transcript.jsonl").read_text().splitlines()]
    assert "llm.skipped" in events


def test_aliyun_next_action_normalizes_tool_action() -> None:
    class ActionClient(review_agent.AliyunDashScopeClient):
        def _chat_json(self, agent_name, system_prompt, user_prompt):
            return {
                "thought": "Need changed files.",
                "action": "call_tool",
                "tool": "changed_files",
                "args": {},
            }

    tmp_path = workspace_tmp("aliyun-next-action")
    transcript = review_agent.Transcript(tmp_path / "transcript.jsonl")
    client = ActionClient(
        api_key="test-key",
        model="qwen-turbo-latest",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        transcript=transcript,
    )

    action = client.next_action({}, "", [{"name": "changed_files"}], [])

    assert action == {
        "thought": "Need changed files.",
        "action": "call_tool",
        "tool": "changed_files",
        "args": {},
    }


def test_aliyun_next_action_accepts_tool_name_as_action() -> None:
    class ActionClient(review_agent.AliyunDashScopeClient):
        def _chat_json(self, agent_name, system_prompt, user_prompt):
            return {
                "thought": "Read the context directly.",
                "action": "read_file_context",
                "path": "service.py",
                "line": 2,
                "radius": 1,
            }

    tmp_path = workspace_tmp("aliyun-next-action-tool-name")
    transcript = review_agent.Transcript(tmp_path / "transcript.jsonl")
    client = ActionClient(
        api_key="test-key",
        model="qwen-turbo-latest",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        transcript=transcript,
    )

    action = client.next_action({}, "", [{"name": "read_file_context"}], [])

    assert action == {
        "thought": "Read the context directly.",
        "action": "call_tool",
        "tool": "read_file_context",
        "args": {"path": "service.py", "line": 2, "radius": 1},
    }


def test_aliyun_json_parser_accepts_first_of_multiple_actions() -> None:
    tmp_path = workspace_tmp("aliyun-multiple-actions")
    transcript = review_agent.Transcript(tmp_path / "transcript.jsonl")
    client = review_agent.AliyunDashScopeClient(
        api_key="test-key",
        model="qwen-turbo-latest",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        transcript=transcript,
    )

    payload = client._parse_json_content(
        '{"action":"call_tool","tool":"changed_files","args":{}}\n'
        '{"action":"call_tool","tool":"git_diff","args":{}}'
    )

    assert payload == {"action": "call_tool", "tool": "changed_files", "args": {}}
    events = [json.loads(line)["event"] for line in (tmp_path / "transcript.jsonl").read_text().splitlines()]
    assert "llm.extra_json_queued" in events


def test_aliyun_next_action_replays_queued_actions_without_new_request() -> None:
    class ActionClient(review_agent.AliyunDashScopeClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.calls = 0

        def _chat_json(self, agent_name, system_prompt, user_prompt):
            self.calls += 1
            self._last_extra_json_payloads = [
                {
                    "thought": "Read diff next.",
                    "action": "call_tool",
                    "tool": "git_diff",
                    "args": {},
                }
            ]
            return {
                "thought": "Need changed files.",
                "action": "call_tool",
                "tool": "changed_files",
                "args": {},
            }

    tmp_path = workspace_tmp("aliyun-action-queue")
    transcript = review_agent.Transcript(tmp_path / "transcript.jsonl")
    client = ActionClient(
        api_key="test-key",
        model="qwen-turbo-latest",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        transcript=transcript,
    )
    tools = [{"name": "changed_files"}, {"name": "git_diff"}]

    first = client.next_action({}, "", tools, [])
    second = client.next_action({}, "", tools, [{"type": "tool", "tool": "changed_files"}])

    assert first["tool"] == "changed_files"
    assert second["tool"] == "git_diff"
    assert client.calls == 1
    events = [json.loads(line)["event"] for line in (tmp_path / "transcript.jsonl").read_text().splitlines()]
    assert "llm.action_queue.extend" in events
    assert "llm.queued_action" in events


class FakeLLMClient(review_agent.LLMClient):
    enabled = True

    def review(self, reviewer_name, reviewer_role, focus, skill_context, diff, files, test_result):
        return [
            review_agent.Finding(
                file="service.py",
                line=2,
                severity="P2",
                category="correctness",
                title="LLM detected branch regression",
                evidence="+return False",
                impact="The changed branch can reject valid requests.",
                suggestion="Restore the original condition or add a targeted test.",
            )
        ]


def test_review_agent_member_merges_llm_findings() -> None:
    tmp_path = workspace_tmp("member-llm")
    transcript = review_agent.Transcript(tmp_path / "transcript.jsonl")
    tools = review_agent.ReviewTools(tmp_path, "main", "HEAD", transcript)
    member = review_agent.ReviewAgentMember(
        "correctness-reviewer",
        "Correctness and edge-case reviewer",
        tools,
        transcript,
        FakeLLMClient(),
    )

    findings = member.review("", [], None)

    assert any(finding.title == "LLM detected branch regression" for finding in findings)


class FakeCouncilLLMClient(review_agent.LLMClient):
    enabled = True
    skill_seen = False

    def plan_review(self, pr_description, skill_context, diff, files, test_result):
        self.skill_seen = "code-review" in skill_context
        return {"security-reviewer": "Focus on authentication and secrets."}

    def review(self, reviewer_name, reviewer_role, focus, skill_context, diff, files, test_result):
        if reviewer_name != "security-reviewer":
            return []
        return [
            review_agent.Finding(
                file="service.py",
                line=2,
                severity="P1",
                category="security",
                title="LLM detected auth bypass",
                evidence="+return {'decision': 'approved'}",
                impact="The changed branch can approve unauthorized requests.",
                suggestion="Require signature validation before approving the request.",
            )
        ]

    def critique_finding(self, item, evidence_chain, skill_context=""):
        return {"decision": "challenge", "reason": "Need source context proving this is on the approval path."}

    def resolve_finding(self, item, evidence_chain, skill_context=""):
        return {"resolution": "downgraded", "reason": "Evidence supports the issue, but P2 is more appropriate.", "severity": "P2"}


class FakeAgenticLLMClient(review_agent.LLMClient):
    enabled = True

    def __init__(self):
        self.step = 0

    def next_action(self, agent_state, skill_context, tools, observations):
        self.step += 1
        actions = [
            {"thought": "List files first.", "action": "call_tool", "tool": "changed_files", "args": {}},
            {"thought": "Read diff.", "action": "call_tool", "tool": "git_diff", "args": {}},
            {
                "thought": "Inspect changed line.",
                "action": "call_tool",
                "tool": "read_file_context",
                "args": {"path": "service.py", "line": 2, "radius": 1},
            },
            {
                "thought": "Emit evidence-backed finding.",
                "action": "emit_finding",
                "finding": {
                    "file": "service.py",
                    "line": 2,
                    "severity": "P1",
                    "category": "security",
                    "title": "Agentic detected auth bypass",
                    "evidence": "return {'decision': 'approved'}",
                    "impact": "The changed branch can approve unauthorized requests.",
                    "suggestion": "Require signature validation before approving the request.",
                },
            },
            {"thought": "Critic should review the candidate.", "action": "ask_critic", "finding_id": "F-001"},
            {"thought": "Enough evidence.", "action": "finalize", "reason": "Review complete."},
        ]
        return actions[self.step - 1]

    def critique_finding(self, item, evidence_chain, skill_context=""):
        return {"decision": "no_challenge", "reason": "Evidence and severity are sufficient."}

    def resolve_finding(self, item, evidence_chain, skill_context=""):
        return {"resolution": "accepted", "reason": "Accepted with diff and source context evidence.", "severity": item.finding.severity}


class FakeDebateLLMClient(review_agent.LLMClient):
    enabled = True

    def __init__(self):
        self.step = 0

    def plan_review(self, pr_description, skill_context, diff, files, test_result):
        return {"security-reviewer": "Focus on auth bypass."}

    def review(self, reviewer_name, reviewer_role, focus, skill_context, diff, files, test_result):
        if reviewer_name != "security-reviewer":
            return []
        return [
            review_agent.Finding(
                file="service.py",
                line=2,
                severity="P1",
                category="security",
                title="Debate detected auth bypass",
                evidence="return {'decision': 'approved'}",
                impact="The changed branch can approve unauthorized requests.",
                suggestion="Require signature validation before approving the request.",
            )
        ]

    def next_debate_action(self, debate_state, skill_context):
        self.step += 1
        actions = [
            {"action": "ask_critic", "finding_id": "F-001", "reason": "Validate evidence."},
            {"action": "request_reviewer_defense", "finding_id": "F-001", "reason": "Answer critic challenge."},
            {"action": "request_more_evidence", "finding_id": "F-001", "reason": "Bind source context."},
            {"action": "accept_finding", "finding_id": "F-001", "reason": "Evidence is now sufficient."},
            {"action": "ask_report_writer", "reason": "Check standardized report quality."},
            {"action": "finalize", "reason": "Debate complete."},
        ]
        return actions[self.step - 1]

    def critique_finding(self, item, evidence_chain, skill_context=""):
        return {"decision": "challenge", "reason": "Need proof this is on an approval path."}

    def reviewer_defense(self, reviewer_name, item, challenge, evidence_chain, skill_context=""):
        return {"decision": "defend", "reason": "The return value is an approval decision in the changed branch."}

    def report_writer_review(self, report_context, language="zh"):
        return {
            "verdict": "request_changes",
            "summary_points": ["One accepted security finding."],
            "accepted_findings": [
                {
                    "issue": "Debate detected auth bypass",
                    "severity": "P1",
                    "file": "service.py",
                    "line": 2,
                    "evidence": "return {'decision': 'approved'}",
                    "impact": "Unauthorized requests can be approved.",
                    "fix": "Require signature validation.",
                    "why_accepted": "Evidence and source context are sufficient.",
                    "critic_notes": "Critic challenge was answered.",
                }
            ],
            "rejected_findings": [],
            "downgraded_findings": [],
            "duplicate_notes": [],
            "critic_notes": ["Critic challenge was answered."],
        }

    def judge_report(self, judge_input, diff, pr_description):
        return {
            "overall_score": 88,
            "verdict": "pass",
            "dimensions": {
                "critical_issue_coverage": 90,
                "evidence_quality": 85,
                "severity_accuracy": 80,
                "duplicate_noise_control": 95,
                "actionability": 88,
                "report_clarity": 90,
            },
            "strengths": ["Critical issue is covered."],
            "weaknesses": ["One fix could be more specific."],
            "recommendations": ["Keep standardized report format."],
        }


def test_agentic_loop_uses_dynamic_actions_and_observations() -> None:
    tmp_path = workspace_tmp("agentic-loop")
    (tmp_path / "service.py").write_text("def approve():\n    return {'decision': 'approved'}\n", encoding="utf-8")
    transcript = review_agent.Transcript(tmp_path / "transcript.jsonl")
    tools = review_agent.ReviewTools(tmp_path, "main", "HEAD", transcript)
    collector = review_agent.FindingCollector()
    loop = review_agent.AgenticReviewLoop(
        tools=tools,
        transcript=transcript,
        collector=collector,
        todos=review_agent.TodoManager(),
        critic_pass=True,
        llm_client=FakeAgenticLLMClient(),
        skill_context="<skill name=\"code-review\">review rules</skill>",
    )

    result = loop.run("Auth approval change")

    assert any(finding.title == "Agentic detected auth bypass" for finding in collector.findings)
    assert result["findings"][0]["status"] == "accepted"
    events = [json.loads(line)["event"] for line in (tmp_path / "transcript.jsonl").read_text().splitlines()]
    assert "agent.action" in events
    assert "agent.observation" in events
    assert "agent.finalize" in events


def test_debate_loop_uses_dynamic_actions_and_report_writer() -> None:
    tmp_path = workspace_tmp("debate-loop")
    (tmp_path / "service.py").write_text("def approve():\n    return {'decision': 'approved'}\n", encoding="utf-8")
    transcript = review_agent.Transcript(tmp_path / "transcript.jsonl")
    tools = review_agent.ReviewTools(tmp_path, "main", "HEAD", transcript)
    collector = review_agent.FindingCollector()
    loop = review_agent.DebateCouncilLoop(
        tools=tools,
        transcript=transcript,
        collector=collector,
        critic_pass=True,
        llm_client=FakeDebateLLMClient(),
        skill_context="<skill name=\"code-review\">review rules</skill>",
        max_actions=12,
        language="en",
    )

    result = loop.run("", [{"file": "service.py", "added": 2, "deleted": 0}], None, "Auth change")

    assert any(finding.title == "Debate detected auth bypass" for finding in collector.findings)
    assert result["report_writer_notes"]["summary_points"] == ["One accepted security finding."]
    events = [json.loads(line)["event"] for line in (tmp_path / "transcript.jsonl").read_text().splitlines()]
    assert "debate.action" in events
    assert "debate.observation" in events
    assert "debate.report_writer" in events
    assert "debate.complete" in events


def test_debate_fallback_does_not_repeat_same_critic_target() -> None:
    tmp_path = workspace_tmp("debate-fallback")
    transcript = review_agent.Transcript(tmp_path / "transcript.jsonl")
    tools = review_agent.ReviewTools(tmp_path, "main", "HEAD", transcript)
    loop = review_agent.DebateCouncilLoop(
        tools=tools,
        transcript=transcript,
        collector=review_agent.FindingCollector(),
        critic_pass=True,
        llm_client=FakeDebateLLMClient(),
    )
    loop.lifecycle.candidate(
        review_agent.Finding(
            file="service.py",
            line=1,
            severity="P1",
            category="security",
            title="First issue",
            evidence="first()",
            impact="First risk.",
            suggestion="Fix first.",
        ),
        "security-reviewer",
    )
    loop.lifecycle.candidate(
        review_agent.Finding(
            file="service.py",
            line=2,
            severity="P2",
            category="correctness",
            title="Second issue",
            evidence="second()",
            impact="Second risk.",
            suggestion="Fix second.",
        ),
        "correctness-reviewer",
    )

    first = loop._fallback_debate_action()
    second = loop._fallback_debate_action()

    assert first["action"] == "ask_critic"
    assert second["action"] == "ask_critic"
    assert first["finding_id"] != second["finding_id"]


def test_report_writer_agent_standardizes_and_writes_judge_input() -> None:
    tmp_path = workspace_tmp("report-writer-agent")
    transcript = review_agent.Transcript(tmp_path / "transcript.jsonl")
    llm = FakeDebateLLMClient()
    context = {
        "council_records": [
            {
                "file": "service.py",
                "line": 2,
                "severity": "P1",
                "category": "security",
                "title": "Debate detected auth bypass",
                "evidence": "return {'decision': 'approved'}",
                "impact": "Unauthorized requests can be approved.",
                "suggestion": "Require signature validation.",
                "finding_id": "F-001",
                "status": "accepted",
                "resolution_reason": "Evidence is sufficient.",
                "evidence_chain": [],
            }
        ],
        "council_messages": [],
    }
    draft = review_agent.ReportWriterAgent(llm, transcript, language="en").draft(context)
    collector = review_agent.FindingCollector()
    collector.emit(
        review_agent.Finding(
            file="service.py",
            line=2,
            severity="P1",
            category="security",
            title="Debate detected auth bypass",
            evidence="return {'decision': 'approved'}",
            impact="Unauthorized requests can be approved.",
            suggestion="Require signature validation.",
        )
    )
    writer = review_agent.ReportWriter(
        tmp_path,
        collector,
        "en",
        context["council_records"],
        [],
        draft,
    )

    paths = writer.write()
    judge_input = json.loads(Path(paths["judge_input"]).read_text(encoding="utf-8"))
    report = Path(paths["report"]).read_text(encoding="utf-8")

    assert judge_input["report_style"] == "standardized"
    assert judge_input["standard_report"]["accepted_findings"][0]["issue"] == "Debate detected auth bypass"
    assert "# Standardized PR Review Report" in report


def test_judge_runner_writes_score_outputs() -> None:
    tmp_path = workspace_tmp("judge-runner")
    (tmp_path / "judge_input.json").write_text(
        json.dumps({"report_style": "standardized", "standard_report": {"verdict": "request_changes"}}),
        encoding="utf-8",
    )
    transcript = review_agent.Transcript(tmp_path / "judge-transcript.jsonl")
    runner = review_agent.JudgeRunner(tmp_path, tmp_path, transcript, FakeDebateLLMClient())

    paths = runner.run(tmp_path / "judge_input.json", "main", "HEAD", None)

    result = json.loads(Path(paths["judge"]).read_text(encoding="utf-8"))
    assert result["overall_score"] == 88
    assert result["dimensions"]["duplicate_noise_control"] == 95
    assert Path(paths["judge_report"]).read_text(encoding="utf-8").startswith("# AI Judge")


def test_review_council_uses_llm_lead_critic_and_resolution() -> None:
    tmp_path = workspace_tmp("council-llm")
    service = tmp_path / "service.py"
    service.write_text("def approve():\n    return {'decision': 'approved'}\n", encoding="utf-8")
    transcript = review_agent.Transcript(tmp_path / "transcript.jsonl")
    tools = review_agent.ReviewTools(tmp_path, "main", "HEAD", transcript)
    collector = review_agent.FindingCollector()
    council = review_agent.ReviewCouncil(
        tools,
        transcript,
        collector,
        critic_pass=True,
        llm_client=FakeCouncilLLMClient(),
        skill_context="<skill name=\"code-review\">review rules</skill>",
    )

    result = council.run("", [{"file": "service.py", "added": 2, "deleted": 0}], None, "Auth change")

    llm_record = next(item for item in result["findings"] if item["title"] == "LLM detected auth bypass")
    assert any(finding.title == "LLM detected auth bypass" and finding.severity == "P2" for finding in collector.findings)
    assert llm_record["resolution"] == "downgraded"
    evidence_sources = [item["source"] for item in llm_record["evidence_chain"]]
    assert "critic_review" in evidence_sources
    assert "lead_resolution" in evidence_sources


def test_review_agent_agentic_without_llm_falls_back_to_guardrails() -> None:
    demo_commit = review_agent.run_git(
        ROOT,
        ["log", "--format=%H", "--grep", "Demo PR with payment risks", "--max-count=1"],
    )
    agent = review_agent.ReviewAgent(
        repo=ROOT,
        base=f"{demo_commit}^",
        target=demo_commit,
        pr_description=ROOT / "docs" / "demo-pr.md",
        language="en",
        mode="agentic",
        critic_pass=True,
        llm_provider="none",
    )

    result = agent.run()

    assert result["verdict"] == "request_changes"
    assert result["paths"]["report"].endswith("report.md")
    events = [
        json.loads(line)["event"]
        for line in (ROOT / ".review-agent" / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert "agentic.fallback" in events
    assert "agent.action" in events


def test_review_agent_debate_without_llm_falls_back_to_council() -> None:
    demo_commit = review_agent.run_git(
        ROOT,
        ["log", "--format=%H", "--grep", "Demo PR with payment risks", "--max-count=1"],
    )
    agent = review_agent.ReviewAgent(
        repo=ROOT,
        base=f"{demo_commit}^",
        target=demo_commit,
        pr_description=ROOT / "docs" / "demo-pr.md",
        language="en",
        mode="debate",
        critic_pass=True,
        llm_provider="none",
    )

    result = agent.run()

    assert result["verdict"] == "request_changes"
    assert result["paths"]["judge_input"].endswith("judge_input.json")
    events = [
        json.loads(line)["event"]
        for line in (ROOT / ".review-agent" / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert "debate.fallback" in events
    assert "council.complete" in events


def test_review_council_demo_contains_challenged_accepted_finding() -> None:
    demo_commit = review_agent.run_git(
        ROOT,
        ["log", "--format=%H", "--grep", "Demo PR with payment risks", "--max-count=1"],
    )
    agent = review_agent.ReviewAgent(
        repo=ROOT,
        base=f"{demo_commit}^",
        target=demo_commit,
        pr_description=ROOT / "docs" / "demo-pr.md",
        language="en",
        mode="council",
        critic_pass=True,
        llm_provider="none",
    )

    result = agent.run()

    assert result["verdict"] == "request_changes"
    categories = {finding["category"] for finding in result["findings"]}
    assert {"security", "correctness", "testing"}.issubset(categories)
    assert any(finding["challenged_by"] == "critic-reviewer" for finding in result["findings"])
    assert all("finding_id" in finding for finding in result["findings"])
    assert all("evidence_chain" in finding for finding in result["findings"])
