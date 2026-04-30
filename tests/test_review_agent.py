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
