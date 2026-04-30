#!/usr/bin/env python3
"""PR Code Review Agent.

Business scenario: a local PR risk reviewer for engineering teams.

The implementation keeps the learn-claude-code harness shape:
tool handlers are explicit, domain knowledge is loaded through skills,
specialist reviewers run with clean task context, and every step is written
to an append-only transcript for observability.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


REPO_ROOT = Path.cwd()
OUTPUT_DIR_NAME = ".review-agent"
TRANSCRIPT_NAME = "transcript.jsonl"
REPORT_NAME = "report.md"
FINDINGS_NAME = "findings.json"
MAX_DIFF_CHARS = 120000
MAX_CMD_OUTPUT = 50000

SEVERITIES = {"P0", "P1", "P2", "P3"}
CATEGORIES = {"security", "correctness", "performance", "testing", "maintainability"}
VERDICTS = {"approve", "comment", "request_changes"}

SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?(?:key|token)|secret|token|password)\b\s*[:=]\s*['\"]?([A-Za-z0-9_\-./+=]{8,})"
)
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")


TOOLS = [
    {
        "name": "git_diff",
        "description": "Get the diff between base and target revisions.",
        "input_schema": {
            "type": "object",
            "properties": {"base": {"type": "string"}, "target": {"type": "string"}},
            "required": ["base", "target"],
        },
    },
    {
        "name": "changed_files",
        "description": "List changed files with added/deleted line counts.",
        "input_schema": {
            "type": "object",
            "properties": {"base": {"type": "string"}, "target": {"type": "string"}},
            "required": ["base", "target"],
        },
    },
    {
        "name": "read_file_context",
        "description": "Read a small window of source context around a line.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "line": {"type": "integer"},
                "radius": {"type": "integer"},
            },
            "required": ["path", "line"],
        },
    },
    {
        "name": "run_tests",
        "description": "Run a read-only test command and return truncated output.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "secret_scan",
        "description": "Scan changed files and added diff lines for likely secrets.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "emit_finding",
        "description": "Record one structured review finding.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file": {"type": "string"},
                "line": {"type": "integer"},
                "severity": {"type": "string", "enum": sorted(SEVERITIES)},
                "category": {"type": "string", "enum": sorted(CATEGORIES)},
                "title": {"type": "string"},
                "evidence": {"type": "string"},
                "impact": {"type": "string"},
                "suggestion": {"type": "string"},
            },
            "required": [
                "file",
                "line",
                "severity",
                "category",
                "title",
                "evidence",
                "impact",
                "suggestion",
            ],
        },
    },
    {
        "name": "write_report",
        "description": "Write Markdown and JSON review outputs.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
]


def now() -> float:
    return time.time()


def truncate(text: str, limit: int = MAX_CMD_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... truncated {len(text) - limit} chars"


def safe_repo_path(repo: Path, path: str) -> Path:
    resolved = (repo / path).resolve()
    repo_resolved = repo.resolve()
    if resolved != repo_resolved and repo_resolved not in resolved.parents:
        raise ValueError(f"Path escapes repository: {path}")
    return resolved


def run_git(repo: Path, args: list[str], timeout: int = 60) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(output or f"git {' '.join(args)} failed")
    return output


def is_git_repo(repo: Path) -> bool:
    try:
        run_git(repo, ["rev-parse", "--is-inside-work-tree"], timeout=10)
        return True
    except Exception:
        return False


def git_range(repo: Path, base: str, target: str) -> str:
    candidate = f"{base}...{target}"
    try:
        run_git(repo, ["diff", "--quiet", "--exit-code", candidate], timeout=20)
    except RuntimeError as exc:
        msg = str(exc)
        if "no merge base" in msg or "unknown revision" in msg or "bad revision" in msg:
            return f"{base}..{target}"
    return candidate


def iter_review_files(repo: Path, limit: int = 80) -> list[Path]:
    demo_fixture = repo / "demo" / "pr-fixture"
    if demo_fixture.exists():
        return [
            path
            for path in sorted(demo_fixture.rglob("*"))
            if path.is_file() and is_source_file(path.relative_to(repo).as_posix())
        ][:limit]

    excluded = {
        ".git",
        ".conda",
        ".next",
        ".pytest_cache",
        ".review-agent",
        ".tmp",
        "node_modules",
        "__pycache__",
    }
    files: list[Path] = []
    for path in repo.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(repo)
        if any(part in excluded for part in rel.parts):
            continue
        if is_source_file(str(rel)) or str(rel).replace("\\", "/").startswith("skills/"):
            files.append(path)
        if len(files) >= limit:
            break
    return files


def synthetic_changed_files(repo: Path) -> list[dict[str, Any]]:
    files = []
    for path in iter_review_files(repo):
        rel = path.relative_to(repo).as_posix()
        try:
            added = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        except Exception:
            added = 0
        files.append({"file": rel, "added": added, "deleted": 0})
    return files


def synthetic_diff(repo: Path) -> str:
    chunks: list[str] = []
    for path in iter_review_files(repo, limit=40):
        rel = path.relative_to(repo).as_posix()
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        chunks.extend(
            [
                f"diff --git a/{rel} b/{rel}",
                "--- /dev/null",
                f"+++ b/{rel}",
                f"@@ -0,0 +1,{len(lines)} @@",
            ]
        )
        chunks.extend("+" + line for line in lines[:300])
    return truncate("\n".join(chunks), MAX_DIFF_CHARS)


def parse_numstat(output: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for raw in output.splitlines():
        parts = raw.split("\t")
        if len(parts) < 3:
            continue
        added, deleted, file_path = parts[0], parts[1], parts[2]
        files.append(
            {
                "file": file_path,
                "added": None if added == "-" else int(added),
                "deleted": None if deleted == "-" else int(deleted),
            }
        )
    return files


def iter_added_lines(diff: str):
    current_file = ""
    new_line = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ b/"):
            current_file = raw[6:]
            continue
        if raw.startswith("+++ /dev/null"):
            current_file = ""
            continue
        if raw.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", raw)
            new_line = int(match.group(1)) if match else 0
            continue
        if not current_file or not raw:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            yield {"file": current_file, "line": new_line, "text": raw[1:]}
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            continue
        else:
            new_line += 1


def is_test_file(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    name = normalized.rsplit("/", 1)[-1]
    return (
        "/test/" in normalized
        or "/tests/" in normalized
        or normalized.startswith("test/")
        or normalized.startswith("tests/")
        or name.startswith("test_")
        or name.endswith(".test.ts")
        or name.endswith(".spec.ts")
        or name.endswith(".test.tsx")
        or name.endswith(".spec.tsx")
        or name.endswith(".test.js")
        or name.endswith(".spec.js")
    )


def is_source_file(path: str) -> bool:
    return Path(path).suffix.lower() in {
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".cs",
        ".rb",
        ".php",
    }


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    severity: str
    category: str
    title: str
    evidence: str
    impact: str
    suggestion: str

    def validate(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"Invalid severity: {self.severity}")
        if self.category not in CATEGORIES:
            raise ValueError(f"Invalid category: {self.category}")
        if not self.file:
            raise ValueError("Finding file is required")
        if self.line < 1:
            raise ValueError("Finding line must be >= 1")
        for field in ("title", "evidence", "impact", "suggestion"):
            if not getattr(self, field).strip():
                raise ValueError(f"Finding {field} is required")


class Transcript:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")

    def emit(self, event: str, **payload: Any) -> None:
        row = {"ts": now(), "event": event, **payload}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


class FindingCollector:
    def __init__(self):
        self.findings: list[Finding] = []
        self._keys: set[tuple[str, int, str, str]] = set()

    def emit(self, finding: Finding | dict[str, Any]) -> str:
        item = finding if isinstance(finding, Finding) else Finding(**finding)
        item.validate()
        key = (item.file, item.line, item.category, item.title)
        if key in self._keys:
            return "Duplicate finding ignored"
        self._keys.add(key)
        self.findings.append(item)
        return f"Recorded {item.severity} {item.category}: {item.title}"

    def sorted(self) -> list[Finding]:
        rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        return sorted(self.findings, key=lambda f: (rank[f.severity], f.file, f.line))


VALID_MESSAGE_TYPES = {
    "task_assignment",
    "evidence_request",
    "candidate_finding",
    "challenge",
    "defense",
    "resolution",
}


@dataclass(frozen=True)
class CouncilMessage:
    sender: str
    to: str
    msg_type: str
    content: str
    finding_id: str = ""
    timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": self.sender,
            "to": self.to,
            "type": self.msg_type,
            "content": self.content,
            "finding_id": self.finding_id,
            "timestamp": self.timestamp,
        }


class MessageBus:
    def __init__(self, transcript: Transcript):
        self.transcript = transcript
        self.messages: list[CouncilMessage] = []

    def send(self, sender: str, to: str, msg_type: str, content: str, finding_id: str = "") -> CouncilMessage:
        if msg_type not in VALID_MESSAGE_TYPES:
            raise ValueError(f"Invalid message type: {msg_type}")
        message = CouncilMessage(
            sender=sender,
            to=to,
            msg_type=msg_type,
            content=content,
            finding_id=finding_id,
            timestamp=now(),
        )
        self.messages.append(message)
        self.transcript.emit("council.message", **message.to_dict())
        return message

    def read(self, name: str) -> list[CouncilMessage]:
        return [message for message in self.messages if message.to == name]

    def all(self) -> list[dict[str, Any]]:
        return [message.to_dict() for message in self.messages]


@dataclass(frozen=True)
class EvidenceItem:
    source: str
    content: str
    added_by: str
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EvidenceStore:
    def __init__(self, transcript: Transcript):
        self.transcript = transcript
        self._items: dict[str, list[EvidenceItem]] = {}

    def add(self, finding_id: str, source: str, content: str, added_by: str) -> None:
        item = EvidenceItem(
            source=source,
            content=truncate(str(content), 2000),
            added_by=added_by,
            timestamp=now(),
        )
        self._items.setdefault(finding_id, []).append(item)
        self.transcript.emit("council.evidence", finding_id=finding_id, **item.to_dict())

    def list(self, finding_id: str) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self._items.get(finding_id, [])]


@dataclass
class CouncilFinding:
    finding_id: str
    finding: Finding
    status: str
    proposed_by: str
    challenged_by: str = ""
    resolution: str = "candidate"
    resolution_reason: str = ""

    def to_dict(self, evidence_chain: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            **asdict(self.finding),
            "finding_id": self.finding_id,
            "status": self.status,
            "proposed_by": self.proposed_by,
            "challenged_by": self.challenged_by,
            "resolution": self.resolution,
            "resolution_reason": self.resolution_reason,
            "evidence_chain": evidence_chain,
        }


class FindingLifecycle:
    def __init__(self, transcript: Transcript):
        self.transcript = transcript
        self._items: dict[str, CouncilFinding] = {}
        self._keys: dict[tuple[str, int, str, str], str] = {}
        self._next_id = 1

    def candidate(self, finding: Finding, proposed_by: str) -> CouncilFinding:
        finding.validate()
        key = (finding.file, finding.line, finding.category, finding.title)
        existing_id = self._keys.get(key)
        if existing_id:
            existing = self._items[existing_id]
            self.transcript.emit(
                "council.duplicate_candidate",
                existing_id=existing_id,
                proposed_by=proposed_by,
            )
            return existing
        finding_id = f"F-{self._next_id:03d}"
        self._next_id += 1
        item = CouncilFinding(
            finding_id=finding_id,
            finding=finding,
            status="candidate",
            proposed_by=proposed_by,
        )
        self._items[finding_id] = item
        self._keys[key] = finding_id
        self.transcript.emit("council.finding.candidate", finding_id=finding_id, proposed_by=proposed_by)
        return item

    def challenge(self, finding_id: str, challenged_by: str, reason: str) -> CouncilFinding:
        item = self._items[finding_id]
        item.status = "challenged"
        item.challenged_by = challenged_by
        item.resolution = "challenged"
        item.resolution_reason = reason
        self.transcript.emit("council.finding.challenge", finding_id=finding_id, challenged_by=challenged_by)
        return item

    def accept(self, finding_id: str, reason: str) -> CouncilFinding:
        item = self._items[finding_id]
        item.status = "accepted"
        item.resolution = "accepted"
        item.resolution_reason = reason
        self.transcript.emit("council.finding.accept", finding_id=finding_id, reason=reason)
        return item

    def reject(self, finding_id: str, reason: str) -> CouncilFinding:
        item = self._items[finding_id]
        item.status = "rejected"
        item.resolution = "rejected"
        item.resolution_reason = reason
        self.transcript.emit("council.finding.reject", finding_id=finding_id, reason=reason)
        return item

    def downgrade(self, finding_id: str, severity: str, reason: str) -> CouncilFinding:
        if severity not in SEVERITIES:
            raise ValueError(f"Invalid severity: {severity}")
        item = self._items[finding_id]
        item.finding = Finding(
            file=item.finding.file,
            line=item.finding.line,
            severity=severity,
            category=item.finding.category,
            title=item.finding.title,
            evidence=item.finding.evidence,
            impact=item.finding.impact,
            suggestion=item.finding.suggestion,
        )
        item.status = "downgraded"
        item.resolution = "downgraded"
        item.resolution_reason = reason
        self.transcript.emit("council.finding.downgrade", finding_id=finding_id, severity=severity)
        return item

    def accepted(self) -> list[CouncilFinding]:
        return [item for item in self._items.values() if item.status in {"accepted", "downgraded"}]

    def all(self) -> list[CouncilFinding]:
        return list(self._items.values())


class TodoManager:
    def __init__(self):
        self.items: list[dict[str, str]] = []

    def update(self, items: list[dict[str, str]]) -> str:
        in_progress = 0
        normalized = []
        for item in items:
            content = str(item.get("content", "")).strip()
            status = str(item.get("status", "pending")).strip()
            if not content:
                raise ValueError("Todo content is required")
            if status not in {"pending", "in_progress", "completed"}:
                raise ValueError(f"Invalid todo status: {status}")
            if status == "in_progress":
                in_progress += 1
            normalized.append({"content": content, "status": status})
        if in_progress > 1:
            raise ValueError("Only one todo can be in_progress")
        self.items = normalized
        return self.render()

    def render(self) -> str:
        if not self.items:
            return "No todos."
        marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
        return "\n".join(f"{marker[i['status']]} {i['content']}" for i in self.items)


class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills: dict[str, dict[str, str]] = {}
        if not skills_dir.exists():
            return
        for path in sorted(skills_dir.rglob("SKILL.md")):
            text = path.read_text(encoding="utf-8")
            meta, body = self._parse(text)
            name = meta.get("name", path.parent.name)
            self.skills[name] = {"description": meta.get("description", ""), "body": body}

    def _parse(self, text: str) -> tuple[dict[str, str], str]:
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text
        meta: dict[str, str] = {}
        for line in match.group(1).splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                meta[key.strip()] = value.strip()
        return meta, match.group(2).strip()

    def descriptions(self) -> str:
        if not self.skills:
            return "(no skills)"
        return "\n".join(f"- {name}: {data['description']}" for name, data in self.skills.items())

    def load(self, name: str) -> str:
        data = self.skills.get(name)
        if not data:
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills)}"
        return f"<skill name=\"{name}\">\n{data['body']}\n</skill>"


class ReviewTools:
    def __init__(self, repo: Path, base: str, target: str, transcript: Transcript):
        self.repo = repo.resolve()
        self.base = base
        self.target = target
        self.transcript = transcript
        self._diff_cache: str | None = None
        self._files_cache: list[dict[str, Any]] | None = None

    def git_diff(self, base: str | None = None, target: str | None = None) -> str:
        base = base or self.base
        target = target or self.target
        if not is_git_repo(self.repo):
            diff = synthetic_diff(self.repo)
            self._diff_cache = diff
            self.transcript.emit("tool.git_diff.fallback", reason="not a git repository", chars=len(diff))
            return diff
        revision_range = git_range(self.repo, base, target)
        diff = run_git(self.repo, ["diff", "--find-renames", revision_range], timeout=60)
        diff = truncate(diff, MAX_DIFF_CHARS)
        self._diff_cache = diff
        self.transcript.emit("tool.git_diff", base=base, target=target, chars=len(diff))
        return diff

    def changed_files(self, base: str | None = None, target: str | None = None) -> list[dict[str, Any]]:
        base = base or self.base
        target = target or self.target
        if not is_git_repo(self.repo):
            files = synthetic_changed_files(self.repo)
            self._files_cache = files
            self.transcript.emit("tool.changed_files.fallback", reason="not a git repository", count=len(files))
            return files
        revision_range = git_range(self.repo, base, target)
        output = run_git(self.repo, ["diff", "--numstat", "--find-renames", revision_range], timeout=60)
        files = parse_numstat(output)
        self._files_cache = files
        self.transcript.emit("tool.changed_files", count=len(files), files=files)
        return files

    def read_file_context(self, path: str, line: int, radius: int = 4) -> str:
        fp = safe_repo_path(self.repo, path)
        lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, line - max(0, radius))
        end = min(len(lines), line + max(0, radius))
        rendered = []
        for number in range(start, end + 1):
            rendered.append(f"{number:>5}: {lines[number - 1]}")
        result = "\n".join(rendered)
        self.transcript.emit("tool.read_file_context", path=path, line=line, radius=radius)
        return result

    def run_tests(self, command: str, timeout: int = 180) -> dict[str, Any]:
        dangerous = ["rm -rf", "git reset", "git checkout", "shutdown", "reboot", "sudo "]
        if any(token in command for token in dangerous):
            raise ValueError("Refusing to run a destructive test command")
        result = subprocess.run(
            command,
            shell=True,
            cwd=self.repo,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = truncate((result.stdout + result.stderr).strip())
        payload = {"command": command, "returncode": result.returncode, "output": output}
        self.transcript.emit("tool.run_tests", command=command, returncode=result.returncode)
        return payload

    def secret_scan(self) -> list[dict[str, Any]]:
        diff = self._diff_cache or self.git_diff()
        findings: list[dict[str, Any]] = []
        for added in iter_added_lines(diff):
            text = added["text"]
            if PRIVATE_KEY_RE.search(text) or SECRET_RE.search(text):
                findings.append(
                    {
                        "file": added["file"],
                        "line": added["line"],
                        "evidence": text.strip(),
                    }
                )
        self.transcript.emit("tool.secret_scan", count=len(findings))
        return findings


class SpecialtyReviewers:
    def __init__(self, tools: ReviewTools, collector: FindingCollector, transcript: Transcript):
        self.tools = tools
        self.collector = collector
        self.transcript = transcript

    def run(self, name: str, diff: str, files: list[dict[str, Any]], test_result: dict[str, Any] | None) -> str:
        self.transcript.emit("subagent.spawn", name=name)
        if name == "security-reviewer":
            count = self.security(diff)
        elif name == "correctness-reviewer":
            count = self.correctness(diff)
        elif name == "test-reviewer":
            count = self.testing(files, test_result)
        elif name == "maintainability-reviewer":
            count = self.maintainability(diff, files)
        else:
            raise ValueError(f"Unknown reviewer: {name}")
        self.transcript.emit("subagent.complete", name=name, findings=count)
        return f"{name} completed with {count} finding(s)"

    def security(self, diff: str) -> int:
        before = len(self.collector.findings)
        for added in iter_added_lines(diff):
            text = added["text"].strip()
            secret = PRIVATE_KEY_RE.search(text) or SECRET_RE.search(text)
            if secret:
                self.collector.emit(
                    Finding(
                        file=added["file"],
                        line=added["line"],
                        severity="P1",
                        category="security",
                        title="Likely secret committed in code",
                        evidence=text[:200],
                        impact="Credentials in source control can be copied from every clone and CI log.",
                        suggestion="Move the value to a secret manager or environment variable and rotate the leaked credential.",
                    )
                )
            if re.search(r"\bos\.system\(|subprocess\.[^(]+\(.*shell\s*=\s*True", text):
                self.collector.emit(
                    Finding(
                        file=added["file"],
                        line=added["line"],
                        severity="P1",
                        category="security",
                        title="Shell command is built on an unsafe execution surface",
                        evidence=text[:200],
                        impact="User-controlled input can become command execution if it reaches this call.",
                        suggestion="Use subprocess with an argument list, validate inputs, and keep shell=False.",
                    )
                )
            if re.search(r"\.execute\(f[\"']", text) or re.search(r"\.execute\(.*\+.*\)", text):
                self.collector.emit(
                    Finding(
                        file=added["file"],
                        line=added["line"],
                        severity="P1",
                        category="security",
                        title="SQL query appears to interpolate values directly",
                        evidence=text[:200],
                        impact="Interpolated SQL can allow injection and data exposure.",
                        suggestion="Use parameterized queries provided by the database driver.",
                    )
                )
        return len(self.collector.findings) - before

    def maintainability(self, diff: str, files: list[dict[str, Any]]) -> int:
        before = len(self.collector.findings)
        for item in files:
            added = item.get("added") or 0
            deleted = item.get("deleted") or 0
            path = item.get("file", "")
            if is_source_file(path) and added + deleted >= 250:
                self.collector.emit(
                    Finding(
                        file=path,
                        line=1,
                        severity="P3",
                        category="maintainability",
                        title="Large source change should be split or explained",
                        evidence=f"{added} added lines, {deleted} deleted lines",
                        impact="Large mixed changes are harder to review and increase regression risk.",
                        suggestion="Split unrelated changes or add a short design note explaining the review strategy.",
                    )
                )
        for added in iter_added_lines(diff):
            stripped = added["text"].strip()
            if "TODO" in stripped or "FIXME" in stripped or "HACK" in stripped:
                self.collector.emit(
                    Finding(
                        file=added["file"],
                        line=added["line"],
                        severity="P3",
                        category="maintainability",
                        title="New unresolved marker added to production code",
                        evidence=stripped[:200],
                        impact="Markers like TODO/FIXME can hide incomplete behavior after merge.",
                        suggestion="Resolve the marker before merge or link it to a tracked follow-up task.",
                    )
                )
        return len(self.collector.findings) - before

    def correctness(self, diff: str) -> int:
        before = len(self.collector.findings)
        previous_except: dict[str, int] = {}
        for added in iter_added_lines(diff):
            text = added["text"].rstrip()
            stripped = text.strip()
            if re.match(r"def .*=\s*(\[\]|\{\})", stripped):
                self.collector.emit(
                    Finding(
                        file=added["file"],
                        line=added["line"],
                        severity="P2",
                        category="correctness",
                        title="Mutable default argument can leak state between calls",
                        evidence=stripped[:200],
                        impact="The same list or dict instance is reused across calls, causing surprising cross-request state.",
                        suggestion="Default to None and create the mutable object inside the function.",
                    )
                )
            if re.match(r"except(?:\s+Exception)?\s*:$", stripped):
                previous_except[added["file"]] = added["line"]
            if re.match(r"except(?:\s+Exception)?\s*:\s*pass$", stripped) or (
                stripped == "pass" and previous_except.get(added["file"]) == added["line"] - 1
            ):
                self.collector.emit(
                    Finding(
                        file=added["file"],
                        line=previous_except.get(added["file"], added["line"]),
                        severity="P2",
                        category="correctness",
                        title="Exception is swallowed without handling",
                        evidence=stripped,
                        impact="Failures disappear, making data loss and partial writes hard to detect.",
                        suggestion="Handle the specific exception, log enough context, or re-raise after cleanup.",
                    )
                )
            if " == None" in stripped or " != None" in stripped:
                self.collector.emit(
                    Finding(
                        file=added["file"],
                        line=added["line"],
                        severity="P3",
                        category="maintainability",
                        title="None comparison should use identity checks",
                        evidence=stripped,
                        impact="Equality operators can be overloaded and make null checks less predictable.",
                        suggestion="Use 'is None' or 'is not None'.",
                    )
                )
        return len(self.collector.findings) - before

    def testing(self, files: list[dict[str, Any]], test_result: dict[str, Any] | None) -> int:
        before = len(self.collector.findings)
        changed = [f["file"] for f in files]
        source_changed = [f for f in changed if is_source_file(f) and not is_test_file(f)]
        tests_changed = [f for f in changed if is_test_file(f)]
        if source_changed and not tests_changed:
            first = source_changed[0]
            self.collector.emit(
                Finding(
                    file=first,
                    line=1,
                    severity="P2",
                    category="testing",
                    title="Production code changed without nearby test changes",
                    evidence=", ".join(source_changed[:5]),
                    impact="The PR can regress behavior without an automated signal catching it.",
                    suggestion="Add or update tests that cover the changed behavior, especially edge cases and failure paths.",
                )
            )
        if test_result and test_result.get("returncode", 0) != 0:
            self.collector.emit(
                Finding(
                    file=source_changed[0] if source_changed else ".",
                    line=1,
                    severity="P1",
                    category="testing",
                    title="Configured test command failed",
                    evidence=truncate(str(test_result.get("output", "")), 1000),
                    impact="A failing test suite means the branch is not safe to merge.",
                    suggestion="Fix the failing tests or update the implementation if the failures expose a regression.",
                )
            )
        return len(self.collector.findings) - before


class ReviewAgentMember:
    def __init__(self, name: str, role: str, tools: ReviewTools, transcript: Transcript):
        self.name = name
        self.role = role
        self.tools = tools
        self.transcript = transcript

    def review(
        self,
        diff: str,
        files: list[dict[str, Any]],
        test_result: dict[str, Any] | None,
    ) -> list[Finding]:
        collector = FindingCollector()
        reviewers = SpecialtyReviewers(self.tools, collector, self.transcript)
        reviewers.run(self.name, diff, files, test_result)
        return collector.sorted()


class CriticReviewer:
    def __init__(self, bus: MessageBus, evidence: EvidenceStore, lifecycle: FindingLifecycle):
        self.name = "critic-reviewer"
        self.bus = bus
        self.evidence = evidence
        self.lifecycle = lifecycle
        self._challenged_once = False

    def review(self, items: list[CouncilFinding]) -> None:
        for item in items:
            chain = self.evidence.list(item.finding_id)
            should_challenge = False
            reason = ""
            if item.finding.severity in {"P0", "P1"} and not self._challenged_once:
                should_challenge = True
                reason = "Merge-blocking severity must be backed by concrete diff evidence and source context."
                self._challenged_once = True
            elif len(chain) < 2:
                should_challenge = True
                reason = "Finding has too little evidence for a final report."

            if should_challenge:
                self.lifecycle.challenge(item.finding_id, self.name, reason)
                self.evidence.add(item.finding_id, "critic_challenge", reason, self.name)
                self.bus.send(
                    self.name,
                    item.proposed_by,
                    "challenge",
                    reason,
                    item.finding_id,
                )
            else:
                self.bus.send(
                    self.name,
                    "lead-reviewer",
                    "resolution",
                    "No challenge; evidence is sufficient.",
                    item.finding_id,
                )


class ReviewCouncil:
    def __init__(
        self,
        tools: ReviewTools,
        transcript: Transcript,
        collector: FindingCollector,
        critic_pass: bool = True,
    ):
        self.tools = tools
        self.transcript = transcript
        self.collector = collector
        self.critic_pass = critic_pass
        self.bus = MessageBus(transcript)
        self.evidence = EvidenceStore(transcript)
        self.lifecycle = FindingLifecycle(transcript)
        self.members = [
            ReviewAgentMember("security-reviewer", "Security risk reviewer", tools, transcript),
            ReviewAgentMember("correctness-reviewer", "Correctness and edge-case reviewer", tools, transcript),
            ReviewAgentMember("test-reviewer", "Test coverage reviewer", tools, transcript),
            ReviewAgentMember("maintainability-reviewer", "Maintainability reviewer", tools, transcript),
        ]
        self.critic = CriticReviewer(self.bus, self.evidence, self.lifecycle)

    def run(
        self,
        diff: str,
        files: list[dict[str, Any]],
        test_result: dict[str, Any] | None,
    ) -> dict[str, Any]:
        self.transcript.emit("council.start", members=[member.name for member in self.members])
        self.bus.send(
            "lead-reviewer",
            "all",
            "task_assignment",
            "Review the PR diff and submit candidate findings with evidence.",
        )

        for member in self.members:
            self.bus.send(
                "lead-reviewer",
                member.name,
                "task_assignment",
                f"Scope: {member.role}. Submit only actionable findings.",
            )
            findings = member.review(diff, files, test_result)
            print(f"[council] {member.name} proposed {len(findings)} candidate finding(s)")
            for finding in findings:
                item = self.lifecycle.candidate(finding, member.name)
                self.evidence.add(item.finding_id, "reviewer_explanation", finding.impact, member.name)
                self.evidence.add(item.finding_id, "diff_line", finding.evidence, member.name)
                try:
                    context = self.tools.read_file_context(finding.file, finding.line, radius=3)
                    self.evidence.add(item.finding_id, "file_context", context, "evidence-store")
                except Exception as exc:
                    self.evidence.add(item.finding_id, "file_context_error", str(exc), "evidence-store")
                self.bus.send(
                    member.name,
                    "lead-reviewer",
                    "candidate_finding",
                    f"{finding.severity} {finding.category}: {finding.title}",
                    item.finding_id,
                )

        if self.critic_pass:
            self.bus.send(
                "lead-reviewer",
                "critic-reviewer",
                "task_assignment",
                "Challenge weak or merge-blocking findings before final resolution.",
            )
            self.critic.review(self.lifecycle.all())

        self._resolve_findings()
        for item in self.lifecycle.accepted():
            self.collector.emit(item.finding)

        records = [
            item.to_dict(self.evidence.list(item.finding_id))
            for item in self.lifecycle.all()
        ]
        self.transcript.emit("council.complete", candidates=len(records), accepted=len(self.lifecycle.accepted()))
        return {"findings": records, "messages": self.bus.all()}

    def _resolve_findings(self) -> None:
        for item in self.lifecycle.all():
            evidence_count = len(self.evidence.list(item.finding_id))
            if item.status == "challenged":
                defense = (
                    f"Defense: {evidence_count} evidence item(s) include diff evidence and reviewer rationale."
                )
                self.evidence.add(item.finding_id, "reviewer_defense", defense, item.proposed_by)
                self.bus.send(item.proposed_by, "critic-reviewer", "defense", defense, item.finding_id)
                reason = "Accepted after challenge because evidence chain is sufficient for the report."
                self.lifecycle.accept(item.finding_id, reason)
                self.bus.send("lead-reviewer", item.proposed_by, "resolution", reason, item.finding_id)
            elif evidence_count < 1:
                reason = "Rejected because no evidence was attached."
                self.lifecycle.reject(item.finding_id, reason)
                self.bus.send("lead-reviewer", item.proposed_by, "resolution", reason, item.finding_id)
            else:
                reason = "Accepted by lead reviewer after evidence review."
                self.lifecycle.accept(item.finding_id, reason)
                self.bus.send("lead-reviewer", item.proposed_by, "resolution", reason, item.finding_id)


ZH_CATEGORY = {
    "security": "安全",
    "correctness": "正确性",
    "performance": "性能",
    "testing": "测试",
    "maintainability": "可维护性",
}

ZH_TITLE = {
    "Likely secret committed in code": "疑似密钥被提交到代码中",
    "Shell command is built on an unsafe execution surface": "Shell 命令执行面存在风险",
    "SQL query appears to interpolate values directly": "SQL 查询疑似直接拼接参数",
    "Mutable default argument can leak state between calls": "可变默认参数可能导致跨调用状态泄漏",
    "Exception is swallowed without handling": "异常被吞掉且没有处理",
    "None comparison should use identity checks": "None 比较应使用身份判断",
    "Production code changed without nearby test changes": "生产代码变更缺少对应测试",
    "Configured test command failed": "配置的测试命令执行失败",
}


ZH_TEXT = {
    "Credentials in source control can be copied from every clone and CI log.": "\u51ed\u8bc1\u8fdb\u5165\u4ee3\u7801\u4ed3\u5e93\u540e\uff0c\u4f1a\u88ab\u6bcf\u4e2a clone\u3001\u526f\u672c\u548c CI \u65e5\u5fd7\u7ee7\u7eed\u4f20\u64ad\u3002",
    "Move the value to a secret manager or environment variable and rotate the leaked credential.": "\u5c06\u8be5\u503c\u79fb\u5230\u5bc6\u94a5\u7ba1\u7406\u7cfb\u7edf\u6216\u73af\u5883\u53d8\u91cf\uff0c\u5e76\u8f6e\u6362\u5df2\u7ecf\u6cc4\u9732\u7684\u51ed\u8bc1\u3002",
    "User-controlled input can become command execution if it reaches this call.": "\u5982\u679c\u7528\u6237\u53ef\u63a7\u8f93\u5165\u6d41\u5165\u8fd9\u91cc\uff0c\u53ef\u80fd\u6f14\u53d8\u4e3a\u547d\u4ee4\u6267\u884c\u98ce\u9669\u3002",
    "Use subprocess with an argument list, validate inputs, and keep shell=False.": "\u4f7f\u7528\u53c2\u6570\u5217\u8868\u5f62\u5f0f\u8c03\u7528 subprocess\uff0c\u6821\u9a8c\u8f93\u5165\uff0c\u5e76\u4fdd\u6301 shell=False\u3002",
    "Interpolated SQL can allow injection and data exposure.": "\u76f4\u63a5\u62fc\u63a5 SQL \u53ef\u80fd\u5bfc\u81f4\u6ce8\u5165\u548c\u6570\u636e\u6cc4\u9732\u3002",
    "Use parameterized queries provided by the database driver.": "\u4f7f\u7528\u6570\u636e\u5e93\u9a71\u52a8\u63d0\u4f9b\u7684\u53c2\u6570\u5316\u67e5\u8be2\u3002",
    "Large mixed changes are harder to review and increase regression risk.": "\u8fc7\u5927\u7684\u6df7\u5408\u53d8\u66f4\u4f1a\u63d0\u9ad8\u5ba1\u67e5\u96be\u5ea6\u548c\u56de\u5f52\u98ce\u9669\u3002",
    "Split unrelated changes or add a short design note explaining the review strategy.": "\u62c6\u5206\u65e0\u5173\u53d8\u66f4\uff0c\u6216\u8865\u5145\u7b80\u77ed\u8bbe\u8ba1\u8bf4\u660e\u89e3\u91ca\u5ba1\u67e5\u7b56\u7565\u3002",
    "Markers like TODO/FIXME can hide incomplete behavior after merge.": "TODO/FIXME \u7b49\u6807\u8bb0\u53ef\u80fd\u8ba9\u672a\u5b8c\u6210\u903b\u8f91\u968f PR \u5408\u5165\u3002",
    "Resolve the marker before merge or link it to a tracked follow-up task.": "\u5408\u5165\u524d\u89e3\u51b3\u8be5\u6807\u8bb0\uff0c\u6216\u5173\u8054\u5230\u53ef\u8ffd\u8e2a\u7684\u540e\u7eed\u4efb\u52a1\u3002",
    "Failures disappear, making data loss and partial writes hard to detect.": "\u5931\u8d25\u4f1a\u88ab\u9759\u9ed8\u541e\u6389\uff0c\u6570\u636e\u4e22\u5931\u6216\u90e8\u5206\u5199\u5165\u5c06\u66f4\u96be\u53d1\u73b0\u3002",
    "Handle the specific exception, log enough context, or re-raise after cleanup.": "\u5904\u7406\u5177\u4f53\u5f02\u5e38\uff0c\u8bb0\u5f55\u8db3\u591f\u4e0a\u4e0b\u6587\uff0c\u6216\u5728\u6e05\u7406\u540e\u91cd\u65b0\u629b\u51fa\u3002",
    "The same list or dict instance is reused across calls, causing surprising cross-request state.": "\u540c\u4e00\u4e2a list \u6216 dict \u5b9e\u4f8b\u4f1a\u5728\u591a\u6b21\u8c03\u7528\u95f4\u590d\u7528\uff0c\u5bb9\u6613\u9020\u6210\u8de8\u8bf7\u6c42\u72b6\u6001\u6c61\u67d3\u3002",
    "Default to None and create the mutable object inside the function.": "\u9ed8\u8ba4\u503c\u6539\u4e3a None\uff0c\u5e76\u5728\u51fd\u6570\u5185\u90e8\u521b\u5efa\u53ef\u53d8\u5bf9\u8c61\u3002",
    "Equality operators can be overloaded and make null checks less predictable.": "\u7b49\u53f7\u8fd0\u7b97\u7b26\u53ef\u80fd\u88ab\u91cd\u8f7d\uff0c\u8ba9\u7a7a\u503c\u5224\u65ad\u53d8\u5f97\u4e0d\u53ef\u9884\u6d4b\u3002",
    "Use 'is None' or 'is not None'.": "\u4f7f\u7528 `is None` \u6216 `is not None`\u3002",
    "The PR can regress behavior without an automated signal catching it.": "\u8be5 PR \u53ef\u80fd\u5f15\u5165\u884c\u4e3a\u56de\u5f52\uff0c\u4f46\u6ca1\u6709\u81ea\u52a8\u5316\u6d4b\u8bd5\u4fe1\u53f7\u53ca\u65f6\u53d1\u73b0\u3002",
    "Add or update tests that cover the changed behavior, especially edge cases and failure paths.": "\u65b0\u589e\u6216\u66f4\u65b0\u8986\u76d6\u672c\u6b21\u53d8\u66f4\u884c\u4e3a\u7684\u6d4b\u8bd5\uff0c\u5c24\u5176\u662f\u8fb9\u754c\u6761\u4ef6\u548c\u5931\u8d25\u8def\u5f84\u3002",
    "A failing test suite means the branch is not safe to merge.": "\u6d4b\u8bd5\u5957\u4ef6\u5931\u8d25\u610f\u5473\u7740\u8be5\u5206\u652f\u5f53\u524d\u4e0d\u9002\u5408\u5408\u5165\u3002",
    "Fix the failing tests or update the implementation if the failures expose a regression.": "\u4fee\u590d\u5931\u8d25\u6d4b\u8bd5\uff1b\u5982\u679c\u5931\u8d25\u66b4\u9732\u4e86\u56de\u5f52\uff0c\u5219\u540c\u6b65\u4fee\u6b63\u5b9e\u73b0\u3002",
    "Review the PR diff and submit candidate findings with evidence.": "\u5ba1\u67e5 PR diff\uff0c\u5e76\u63d0\u4ea4\u5e26\u8bc1\u636e\u7684\u5019\u9009\u95ee\u9898\u3002",
    "Scope: Security risk reviewer. Submit only actionable findings.": "\u8303\u56f4\uff1a\u5b89\u5168\u98ce\u9669\u5ba1\u67e5\uff0c\u53ea\u63d0\u4ea4\u53ef\u6267\u884c\u7684\u95ee\u9898\u3002",
    "Scope: Correctness and edge-case reviewer. Submit only actionable findings.": "\u8303\u56f4\uff1a\u6b63\u786e\u6027\u548c\u8fb9\u754c\u6761\u4ef6\u5ba1\u67e5\uff0c\u53ea\u63d0\u4ea4\u53ef\u6267\u884c\u7684\u95ee\u9898\u3002",
    "Scope: Test coverage reviewer. Submit only actionable findings.": "\u8303\u56f4\uff1a\u6d4b\u8bd5\u8986\u76d6\u5ba1\u67e5\uff0c\u53ea\u63d0\u4ea4\u53ef\u6267\u884c\u7684\u95ee\u9898\u3002",
    "Scope: Maintainability reviewer. Submit only actionable findings.": "\u8303\u56f4\uff1a\u53ef\u7ef4\u62a4\u6027\u5ba1\u67e5\uff0c\u53ea\u63d0\u4ea4\u53ef\u6267\u884c\u7684\u95ee\u9898\u3002",
    "Challenge weak or merge-blocking findings before final resolution.": "\u5728\u6700\u7ec8\u88c1\u51b3\u524d\u8d28\u7591\u8bc1\u636e\u4e0d\u8db3\u6216\u963b\u585e\u5408\u5e76\u7684\u95ee\u9898\u3002",
    "Merge-blocking severity must be backed by concrete diff evidence and source context.": "\u963b\u585e\u5408\u5e76\u7ea7\u522b\u7684\u95ee\u9898\u5fc5\u987b\u6709\u660e\u786e diff \u8bc1\u636e\u548c\u6e90\u7801\u4e0a\u4e0b\u6587\u652f\u6491\u3002",
    "No challenge; evidence is sufficient.": "\u4e0d\u8d28\u7591\uff1b\u5f53\u524d\u8bc1\u636e\u5145\u5206\u3002",
    "Accepted after challenge because evidence chain is sufficient for the report.": "\u7ecf\u8d28\u7591\u540e\u63a5\u53d7\uff0c\u56e0\u4e3a\u8bc1\u636e\u94fe\u8db3\u4ee5\u8fdb\u5165\u6700\u7ec8\u62a5\u544a\u3002",
    "Accepted by lead reviewer after evidence review.": "Lead reviewer \u590d\u6838\u8bc1\u636e\u540e\u63a5\u53d7\u3002",
}


def zh_text(text: str) -> str:
    if text.startswith("Defense: "):
        return text.replace("Defense:", "\u7b54\u8fa9\uff1a").replace(
            "evidence item(s) include diff evidence and reviewer rationale.",
            "\u6761\u8bc1\u636e\u5305\u542b diff \u8bc1\u636e\u548c reviewer \u7406\u7531\u3002",
        )
    return ZH_TEXT.get(text, text)


class ReportWriter:
    def __init__(self, output_dir: Path, collector: FindingCollector, language: str = "zh"):
        self.output_dir = output_dir
        self.collector = collector
        self.language = language
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def verdict(self) -> str:
        severities = {finding.severity for finding in self.collector.findings}
        if "P0" in severities or "P1" in severities:
            return "request_changes"
        if severities:
            return "comment"
        return "approve"

    def summary(self) -> str:
        findings = self.collector.sorted()
        if not findings:
            if self.language == "zh":
                return "未在本次审查范围内发现阻塞合并的问题。"
            return "No blocking issues found in the reviewed diff."
        counts: dict[str, int] = {}
        for item in findings:
            counts[item.severity] = counts.get(item.severity, 0) + 1
        parts = [f"{severity}: {counts[severity]}" for severity in sorted(counts)]
        if self.language == "zh":
            return f"共发现 {len(findings)} 个问题：" + "，".join(parts) + "。"
        return f"Found {len(findings)} issue(s): " + ", ".join(parts) + "."

    def payload(self) -> dict[str, Any]:
        verdict = self.verdict()
        if verdict not in VERDICTS:
            raise ValueError(f"Invalid verdict: {verdict}")
        return {
            "summary": self.summary(),
            "verdict": verdict,
            "findings": [asdict(finding) for finding in self.collector.sorted()],
        }

    def markdown(self) -> str:
        payload = self.payload()
        if self.language == "zh":
            return self._markdown_zh(payload)
        lines = [
            "# PR Code Review Agent Report",
            "",
            f"**Verdict:** `{payload['verdict']}`",
            "",
            f"**Summary:** {payload['summary']}",
            "",
            "## Findings",
            "",
        ]
        if not payload["findings"]:
            lines.append("No findings.")
        for idx, finding in enumerate(payload["findings"], start=1):
            lines.extend(
                [
                    f"### {idx}. [{finding['severity']}] {finding['title']}",
                    "",
                    f"- File: `{finding['file']}:{finding['line']}`",
                    f"- Category: `{finding['category']}`",
                    f"- Evidence: {finding['evidence']}",
                    f"- Impact: {finding['impact']}",
                    f"- Suggestion: {finding['suggestion']}",
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"

    def _markdown_zh(self, payload: dict[str, Any]) -> str:
        verdict_label = {
            "approve": "通过",
            "comment": "需要关注",
            "request_changes": "请求修改",
        }.get(payload["verdict"], payload["verdict"])
        lines = [
            "# PR 代码审查 Agent 报告",
            "",
            f"**结论：** `{payload['verdict']}`（{verdict_label}）",
            "",
            f"**摘要：** {payload['summary']}",
            "",
            "## 问题列表",
            "",
        ]
        if not payload["findings"]:
            lines.append("未发现问题。")
        for idx, finding in enumerate(payload["findings"], start=1):
            title = ZH_TITLE.get(finding["title"], finding["title"])
            category = ZH_CATEGORY.get(finding["category"], finding["category"])
            lines.extend(
                [
                    f"### {idx}. [{finding['severity']}] {title}",
                    "",
                    f"- 文件：`{finding['file']}:{finding['line']}`",
                    f"- 分类：`{category}`",
                    f"- 证据：{finding['evidence']}",
                    f"- 影响：{finding['impact']}",
                    f"- 建议：{finding['suggestion']}",
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"

    def write(self) -> dict[str, str]:
        report_path = self.output_dir / REPORT_NAME
        findings_path = self.output_dir / FINDINGS_NAME
        report_path.write_text(self.markdown(), encoding="utf-8")
        findings_path.write_text(json.dumps(self.payload(), indent=2, ensure_ascii=False), encoding="utf-8")
        return {"report": str(report_path), "findings": str(findings_path)}


class CouncilReportWriter:
    def __init__(
        self,
        output_dir: Path,
        collector: FindingCollector,
        language: str = "zh",
        council_records: list[dict[str, Any]] | None = None,
        council_messages: list[dict[str, Any]] | None = None,
    ):
        self.output_dir = output_dir
        self.collector = collector
        self.language = language
        self.council_records = council_records or []
        self.council_messages = council_messages or []
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def verdict(self) -> str:
        severities = {finding.severity for finding in self.collector.findings}
        if "P0" in severities or "P1" in severities:
            return "request_changes"
        if severities:
            return "comment"
        return "approve"

    def summary(self) -> str:
        findings = self.collector.sorted()
        if not findings:
            if self.language == "zh":
                return "\u672a\u5728\u672c\u6b21\u5ba1\u67e5\u8303\u56f4\u5185\u53d1\u73b0\u963b\u585e\u5408\u5e76\u7684\u95ee\u9898\u3002"
            return "No blocking issues found in the reviewed diff."
        counts: dict[str, int] = {}
        for item in findings:
            counts[item.severity] = counts.get(item.severity, 0) + 1
        parts = [f"{severity}: {counts[severity]}" for severity in sorted(counts)]
        if self.language == "zh":
            return f"\u5171\u53d1\u73b0 {len(findings)} \u4e2a\u95ee\u9898\uff1a" + "\uff0c".join(parts) + "\u3002"
        return f"Found {len(findings)} issue(s): " + ", ".join(parts) + "."

    def _payload_findings(self) -> list[dict[str, Any]]:
        if not self.council_records:
            return [asdict(finding) for finding in self.collector.sorted()]
        accepted = [
            record
            for record in self.council_records
            if record.get("status") in {"accepted", "downgraded"}
        ]
        rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        return sorted(
            accepted,
            key=lambda f: (
                rank.get(str(f.get("severity")), 99),
                str(f.get("file")),
                int(f.get("line", 0)),
            ),
        )

    def payload(self) -> dict[str, Any]:
        verdict = self.verdict()
        if verdict not in VERDICTS:
            raise ValueError(f"Invalid verdict: {verdict}")
        return {
            "summary": self.summary(),
            "verdict": verdict,
            "findings": self._payload_findings(),
            "council": {
                "messages": self.council_messages,
                "candidates": self.council_records,
            },
        }

    def markdown(self) -> str:
        payload = self.payload()
        if self.language == "zh":
            return self._markdown_zh(payload)
        lines = [
            "# PR Code Review Agent Report",
            "",
            f"**Verdict:** `{payload['verdict']}`",
            "",
            f"**Summary:** {payload['summary']}",
            "",
        ]
        self._append_council_process(lines, zh=False)
        self._append_findings(lines, payload, zh=False)
        return "\n".join(lines).rstrip() + "\n"

    def _markdown_zh(self, payload: dict[str, Any]) -> str:
        verdict_label = {
            "approve": "\u901a\u8fc7",
            "comment": "\u9700\u8981\u5173\u6ce8",
            "request_changes": "\u8bf7\u6c42\u4fee\u6539",
        }.get(payload["verdict"], payload["verdict"])
        lines = [
            "# PR \u4ee3\u7801\u5ba1\u67e5 Agent \u62a5\u544a",
            "",
            f"**\u7ed3\u8bba\uff1a** `{payload['verdict']}`\uff08{verdict_label}\uff09",
            "",
            f"**\u6458\u8981\uff1a** {payload['summary']}",
            "",
        ]
        self._append_council_process(lines, zh=True)
        self._append_findings(lines, payload, zh=True)
        return "\n".join(lines).rstrip() + "\n"

    def _append_council_process(self, lines: list[str], zh: bool) -> None:
        if not self.council_messages:
            return
        lines.extend(["## " + ("\u5ba1\u67e5\u59d4\u5458\u4f1a\u8fc7\u7a0b" if zh else "Review Council Process"), ""])
        for message in self.council_messages:
            finding_suffix = f" ({message['finding_id']})" if message.get("finding_id") else ""
            content = zh_text(message["content"]) if zh else message["content"]
            lines.append(
                f"- `{message['type']}` {message['from']} -> {message['to']}{finding_suffix}: {content}"
            )
        lines.append("")

    def _append_findings(self, lines: list[str], payload: dict[str, Any], zh: bool) -> None:
        lines.extend(["## " + ("\u95ee\u9898\u5217\u8868" if zh else "Findings"), ""])
        if not payload["findings"]:
            lines.append("\u672a\u53d1\u73b0\u95ee\u9898\u3002" if zh else "No findings.")
            return
        for idx, finding in enumerate(payload["findings"], start=1):
            title = ZH_TITLE.get(finding["title"], finding["title"]) if zh else finding["title"]
            category = ZH_CATEGORY.get(finding["category"], finding["category"]) if zh else finding["category"]
            labels = {
                "file": "\u6587\u4ef6" if zh else "File",
                "category": "\u5206\u7c7b" if zh else "Category",
                "proposed": "\u63d0\u51fa\u8005" if zh else "Proposed by",
                "challenged": "\u8d28\u7591\u8005" if zh else "Challenged by",
                "resolution": "\u6700\u7ec8\u88c1\u51b3" if zh else "Resolution",
                "evidence": "\u8bc1\u636e" if zh else "Evidence",
                "impact": "\u5f71\u54cd" if zh else "Impact",
                "suggestion": "\u5efa\u8bae" if zh else "Suggestion",
                "chain": "\u8bc1\u636e\u94fe" if zh else "Evidence chain",
            }
            lines.extend(
                [
                    f"### {idx}. [{finding['severity']}] {title}",
                    "",
                    f"- {labels['file']}: `{finding['file']}:{finding['line']}`",
                    f"- {labels['category']}: `{category}`",
                    f"- Finding ID: `{finding.get('finding_id', '-')}`",
                    f"- {labels['proposed']}: `{finding.get('proposed_by', '-')}`",
                    f"- {labels['challenged']}: `{finding.get('challenged_by') or 'none'}`",
                    f"- {labels['resolution']}: `{finding.get('resolution', 'accepted')}`",
                    f"- {labels['evidence']}: {finding['evidence']}",
                    f"- {labels['impact']}: {zh_text(finding['impact']) if zh else finding['impact']}",
                    f"- {labels['suggestion']}: {zh_text(finding['suggestion']) if zh else finding['suggestion']}",
                    "",
                ]
            )
            evidence_chain = finding.get("evidence_chain") or []
            if evidence_chain:
                lines.append(f"  {labels['chain']}:")
                for evidence in evidence_chain:
                    evidence_content = zh_text(evidence.get("content", "")) if zh else evidence.get("content")
                    lines.append(
                        f"  - `{evidence.get('source')}` by `{evidence.get('added_by')}`: {evidence_content}"
                    )
                lines.append("")

    def write(self) -> dict[str, str]:
        report_path = self.output_dir / REPORT_NAME
        findings_path = self.output_dir / FINDINGS_NAME
        report_path.write_text(self.markdown(), encoding="utf-8")
        findings_path.write_text(json.dumps(self.payload(), indent=2, ensure_ascii=False), encoding="utf-8")
        return {"report": str(report_path), "findings": str(findings_path)}


ReportWriter = CouncilReportWriter


class ReviewAgent:
    def __init__(
        self,
        repo: Path,
        base: str,
        target: str,
        pr_description: Path | None = None,
        test_command: str | None = None,
        language: str = "zh",
        mode: str = "council",
        critic_pass: bool = True,
    ):
        self.repo = repo.resolve()
        self.base = base
        self.target = target
        self.pr_description = pr_description
        self.test_command = test_command
        self.language = language
        self.mode = mode
        self.critic_pass = critic_pass
        self.output_dir = self.repo / OUTPUT_DIR_NAME
        self.transcript = Transcript(self.output_dir / TRANSCRIPT_NAME)
        self.collector = FindingCollector()
        self.todos = TodoManager()
        self.skills = SkillLoader(REPO_ROOT / "skills")
        self.tools = ReviewTools(self.repo, self.base, self.target, self.transcript)
        self.reviewers = SpecialtyReviewers(self.tools, self.collector, self.transcript)
        self.council_result: dict[str, Any] = {"findings": [], "messages": []}
        self.tool_handlers: dict[str, Callable[..., Any]] = {
            "git_diff": self.tools.git_diff,
            "changed_files": self.tools.changed_files,
            "read_file_context": self.tools.read_file_context,
            "run_tests": self.tools.run_tests,
            "secret_scan": self.tools.secret_scan,
            "emit_finding": self.collector.emit,
            "write_report": lambda path=None: ReportWriter(
                self.output_dir,
                self.collector,
                self.language,
                self.council_result.get("findings", []),
                self.council_result.get("messages", []),
            ).write(),
        }

    def _description_text(self) -> str:
        if not self.pr_description:
            return ""
        path = self.pr_description
        if not path.is_absolute():
            path = safe_repo_path(self.repo, str(path))
        return path.read_text(encoding="utf-8", errors="replace")

    def run(self) -> dict[str, Any]:
        self.transcript.emit(
            "review.start",
            repo=str(self.repo),
            base=self.base,
            target=self.target,
            mode=self.mode,
            tools=[tool["name"] for tool in TOOLS],
            skills=self.skills.descriptions(),
        )
        self.todos.update(
            [
                {"content": "Load code-review skill and understand PR context", "status": "completed"},
                {"content": "Inspect changed files and diff", "status": "in_progress"},
                {"content": "Run specialist review agents", "status": "pending"},
                {"content": "Write Markdown and JSON report", "status": "pending"},
            ]
        )
        self.transcript.emit("todo.update", todos=self.todos.items)

        skill = self.skills.load("code-review")
        pr_description = self._description_text()
        self.transcript.emit("skill.load", name="code-review", chars=len(skill))
        if pr_description:
            self.transcript.emit("pr.description", chars=len(pr_description))

        files = self.tools.changed_files()
        diff = self.tools.git_diff()
        test_result = self.tools.run_tests(self.test_command) if self.test_command else None

        self.todos.update(
            [
                {"content": "Load code-review skill and understand PR context", "status": "completed"},
                {"content": "Inspect changed files and diff", "status": "completed"},
                {"content": "Run specialist review agents", "status": "in_progress"},
                {"content": "Write Markdown and JSON report", "status": "pending"},
            ]
        )
        self.transcript.emit("todo.update", todos=self.todos.items)

        if self.mode == "simple":
            for reviewer in ("security-reviewer", "correctness-reviewer", "test-reviewer"):
                print(f"[agent] spawning {reviewer}")
                print("[agent] " + self.reviewers.run(reviewer, diff, files, test_result))
        else:
            council = ReviewCouncil(
                tools=self.tools,
                transcript=self.transcript,
                collector=self.collector,
                critic_pass=self.critic_pass,
            )
            print("[agent] convening review council")
            self.council_result = council.run(diff, files, test_result)
            print(
                f"[agent] council accepted {len(self.collector.findings)} finding(s) "
                f"from {len(self.council_result['findings'])} candidate(s)"
            )

        self.todos.update(
            [
                {"content": "Load code-review skill and understand PR context", "status": "completed"},
                {"content": "Inspect changed files and diff", "status": "completed"},
                {"content": "Run specialist review agents", "status": "completed"},
                {"content": "Write Markdown and JSON report", "status": "in_progress"},
            ]
        )
        self.transcript.emit("todo.update", todos=self.todos.items)

        writer = ReportWriter(
            self.output_dir,
            self.collector,
            self.language,
            self.council_result.get("findings", []),
            self.council_result.get("messages", []),
        )
        paths = writer.write()
        payload = writer.payload()
        self.transcript.emit("review.complete", verdict=payload["verdict"], findings=len(payload["findings"]))

        self.todos.update(
            [
                {"content": "Load code-review skill and understand PR context", "status": "completed"},
                {"content": "Inspect changed files and diff", "status": "completed"},
                {"content": "Run specialist review agents", "status": "completed"},
                {"content": "Write Markdown and JSON report", "status": "completed"},
            ]
        )
        self.transcript.emit("todo.update", todos=self.todos.items)
        return {**payload, "paths": paths}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a local PR risk review agent.")
    parser.add_argument("--repo", default=".", help="Repository path to review.")
    parser.add_argument("--base", default="main", help="Base branch or revision.")
    parser.add_argument("--target", default="HEAD", help="Target branch or revision.")
    parser.add_argument("--pr-description", help="Optional PR description markdown file.")
    parser.add_argument("--test-command", help="Optional test command to run, e.g. 'python -m pytest'.")
    parser.add_argument("--language", choices=["zh", "en"], default="zh", help="Report language.")
    parser.add_argument("--mode", choices=["council", "simple"], default="council", help="Review execution mode.")
    parser.add_argument(
        "--critic-pass",
        choices=["true", "false"],
        default="true",
        help="Whether critic-reviewer challenges candidate findings in council mode.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = Path(args.repo).resolve()
    pr_description = Path(args.pr_description) if args.pr_description else None
    agent = ReviewAgent(
        repo=repo,
        base=args.base,
        target=args.target,
        pr_description=pr_description,
        test_command=args.test_command,
        language=args.language,
        mode=args.mode,
        critic_pass=args.critic_pass == "true",
    )
    result = agent.run()
    print(f"[agent] verdict: {result['verdict']}")
    print(f"[agent] summary: {result['summary']}")
    print(f"[agent] report: {result['paths']['report']}")
    print(f"[agent] findings: {result['paths']['findings']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
