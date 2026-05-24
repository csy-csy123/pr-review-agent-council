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
import hashlib
import json
import math
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


REPO_ROOT = Path.cwd()
OUTPUT_DIR_NAME = ".review-agent"
TRANSCRIPT_NAME = "transcript.jsonl"
REPORT_NAME = "report.md"
FINDINGS_NAME = "findings.json"
JUDGE_INPUT_NAME = "judge_input.json"
JUDGE_NAME = "judge.json"
JUDGE_REPORT_NAME = "judge.md"
JUDGE_TRANSCRIPT_NAME = "judge-transcript.jsonl"
COMPANY_KNOWLEDGE_INDEX_NAME = "company_knowledge_index.json"
MAX_DIFF_CHARS = 120000
MAX_CMD_OUTPUT = 50000
MAX_SKILL_CHARS = 12000
MAX_COMPANY_CONTEXT_CHARS = 6000

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
        "name": "search_code",
        "description": "Search text files in the repository for a query or regex.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string"},
                "max_matches": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "risk_scan",
        "description": "Extract review-worthy risk signals from added diff lines for the LLM to judge.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "retrieve_company_policy",
        "description": "Retrieve company-specific coding, security, payment, and testing policies relevant to a review question.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "category": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["query"],
        },
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
        encoding="utf-8",
        errors="replace",
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


def load_env_file(path: Path) -> list[str]:
    if not path.exists():
        return []

    loaded: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if not key or key in os.environ:
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded


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

    def revise(self, finding_id: str, finding: Finding, reason: str) -> CouncilFinding:
        finding.validate()
        item = self._items[finding_id]
        old_key = (item.finding.file, item.finding.line, item.finding.category, item.finding.title)
        self._keys.pop(old_key, None)
        item.finding = finding
        item.status = "candidate"
        item.resolution = "revised"
        item.resolution_reason = reason
        self._keys[(finding.file, finding.line, finding.category, finding.title)] = finding_id
        self.transcript.emit("council.finding.revise", finding_id=finding_id, reason=reason)
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


@dataclass(frozen=True)
class CompanyKnowledgeChunk:
    policy_id: str
    doc_path: str
    heading: str
    text: str
    category: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CompanyKnowledgeBase:
    def __init__(
        self,
        knowledge_dir: Path,
        index_path: Path,
        transcript: Transcript,
        api_key: str | None = None,
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_model: str = "text-embedding-v4",
        embedding_dimensions: int = 1024,
        enabled: bool = True,
        timeout: int = 20,
    ):
        self.knowledge_dir = knowledge_dir.resolve()
        self.index_path = index_path
        self.transcript = transcript
        self.api_key = api_key or ""
        self.base_url = base_url.rstrip("/")
        self.embedding_model = embedding_model
        self.embedding_dimensions = int(embedding_dimensions or 1024)
        self.enabled = enabled
        self.timeout = timeout
        self._chunks: list[CompanyKnowledgeChunk] | None = None
        self._embeddings: list[list[float] | None] | None = None
        self._embeddings_fingerprint = ""
        self._embedding_failed = False

    def available(self) -> bool:
        return self.enabled and self.knowledge_dir.exists()

    def search(self, query: str, category: str | None = None, max_results: int = 3) -> list[dict[str, Any]]:
        query = query.strip()
        if not self.available() or not query:
            return []
        max_results = max(1, min(int(max_results or 3), 10))
        chunks = self._load_chunks()
        if category:
            category = category.strip().lower()
            filtered = [chunk for chunk in chunks if category in {chunk.category.lower(), "all"}]
            if filtered:
                chunks = filtered
        if not chunks:
            return []

        query_tokens = self._tokens(query)
        keyword_scores = [self._keyword_score(query_tokens, query, chunk) for chunk in chunks]
        query_embedding = self._embed_query(query)
        embeddings = self._load_embeddings(chunks) if query_embedding is not None else []
        results: list[dict[str, Any]] = []
        for idx, chunk in enumerate(chunks):
            method = "keyword_fallback"
            score = keyword_scores[idx]
            if query_embedding is not None and idx < len(embeddings) and embeddings[idx]:
                cosine = self._cosine(query_embedding, embeddings[idx] or [])
                score = (cosine * 0.85) + (min(keyword_scores[idx], 1.0) * 0.15)
                method = "embedding_hybrid"
            if score <= 0:
                continue
            results.append(self._result(chunk, score, method))

        results.sort(key=lambda item: item["score"], reverse=True)
        selected = results[:max_results]
        self.transcript.emit(
            "company_rag.search",
            query=truncate(query, 300),
            category=category or "",
            count=len(selected),
            method=selected[0]["retrieval_method"] if selected else "none",
        )
        return selected

    def context_block(self, query: str, max_results: int = 5) -> str:
        results = self.search(query, max_results=max_results)
        if not results:
            return ""
        lines = ["<company_knowledge>", "Use these company policies as review standards when relevant."]
        for item in results:
            lines.extend(
                [
                    f"- policy_id: {item['policy_id']}",
                    f"  source: {item['doc_path']}#{item['heading']}",
                    f"  retrieval: {item['retrieval_method']} score={item['score']}",
                    f"  excerpt: {item['excerpt']}",
                ]
            )
        lines.append("</company_knowledge>")
        return truncate("\n".join(lines), MAX_COMPANY_CONTEXT_CHARS)

    def _load_chunks(self) -> list[CompanyKnowledgeChunk]:
        if self._chunks is not None:
            return self._chunks
        self._chunks = self._read_markdown_chunks()
        self.transcript.emit("company_rag.load", dir=str(self.knowledge_dir), chunks=len(self._chunks))
        return self._chunks

    def _read_markdown_chunks(self) -> list[CompanyKnowledgeChunk]:
        if not self.knowledge_dir.exists():
            self.transcript.emit("company_rag.missing", dir=str(self.knowledge_dir))
            return []
        chunks: list[CompanyKnowledgeChunk] = []
        for path in sorted(self.knowledge_dir.rglob("*.md")):
            rel = path.relative_to(self.knowledge_dir).as_posix()
            category = path.stem.replace("_", "-")
            text = path.read_text(encoding="utf-8", errors="replace")
            current_heading = path.stem.replace("_", " ").title()
            current_lines: list[str] = []

            def flush() -> None:
                body = "\n".join(line.strip() for line in current_lines).strip()
                if not body:
                    return
                chunks.append(
                    CompanyKnowledgeChunk(
                        policy_id=self._policy_id(rel, current_heading),
                        doc_path=rel,
                        heading=current_heading,
                        text=truncate(body, 1800),
                        category=category,
                    )
                )

            for raw in text.splitlines():
                if raw.startswith("#"):
                    flush()
                    current_heading = raw.lstrip("#").strip() or current_heading
                    current_lines = []
                else:
                    current_lines.append(raw)
            flush()
        return chunks

    def _load_embeddings(self, chunks: list[CompanyKnowledgeChunk]) -> list[list[float] | None]:
        fingerprint = self._fingerprint(chunks)
        if self._embeddings is not None and self._embeddings_fingerprint == fingerprint:
            return self._embeddings
        cached = self._read_index_cache(chunks)
        if cached is not None:
            self._embeddings = cached
            self._embeddings_fingerprint = fingerprint
            return cached
        if not self.api_key or self._embedding_failed:
            self.transcript.emit("company_rag.embedding_disabled", reason="DASHSCOPE_API_KEY is not set")
            self._embeddings = [None for _ in chunks]
            self._embeddings_fingerprint = fingerprint
            return self._embeddings
        vectors = self._embed_texts([self._embedding_text(chunk) for chunk in chunks])
        if vectors is None:
            self._embedding_failed = True
            self._embeddings = [None for _ in chunks]
            self._embeddings_fingerprint = fingerprint
            return self._embeddings
        self._embeddings = vectors
        self._embeddings_fingerprint = fingerprint
        self._write_index_cache(chunks, vectors)
        return vectors

    def _embed_query(self, query: str) -> list[float] | None:
        if not self.api_key or self._embedding_failed:
            if not self.api_key:
                self.transcript.emit("company_rag.embedding_disabled", reason="DASHSCOPE_API_KEY is not set")
            return None
        vectors = self._embed_texts([query])
        if not vectors or vectors[0] is None:
            self._embedding_failed = True
            return None
        return vectors[0]

    def _embed_texts(self, texts: list[str]) -> list[list[float]] | None:
        payload: dict[str, Any] = {
            "model": self.embedding_model,
            "input": texts,
            "dimensions": self.embedding_dimensions,
        }
        endpoint = f"{self.base_url}/embeddings"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        self.transcript.emit(
            "company_rag.embedding.request",
            model=self.embedding_model,
            dimensions=self.embedding_dimensions,
            count=len(texts),
            endpoint=endpoint,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            vectors = [item["embedding"] for item in sorted(data["data"], key=lambda item: item.get("index", 0))]
            if len(vectors) != len(texts):
                raise ValueError("embedding count mismatch")
            self.transcript.emit("company_rag.embedding.response", count=len(vectors))
            return [[float(value) for value in vector] for vector in vectors]
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self.transcript.emit("company_rag.embedding.error", error=str(exc))
            return None

    def _read_index_cache(self, chunks: list[CompanyKnowledgeChunk]) -> list[list[float] | None] | None:
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if payload.get("fingerprint") != self._fingerprint(chunks):
            return None
        if payload.get("model") != self.embedding_model:
            return None
        if int(payload.get("dimensions", 0) or 0) != self.embedding_dimensions:
            return None
        vectors = payload.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(chunks):
            return None
        self.transcript.emit("company_rag.index_cache.hit", chunks=len(chunks))
        return vectors

    def _write_index_cache(self, chunks: list[CompanyKnowledgeChunk], vectors: list[list[float]]) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fingerprint": self._fingerprint(chunks),
            "model": self.embedding_model,
            "dimensions": self.embedding_dimensions,
            "chunks": [chunk.to_dict() for chunk in chunks],
            "embeddings": vectors,
        }
        self.index_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.transcript.emit("company_rag.index_cache.write", chunks=len(chunks))

    def _fingerprint(self, chunks: list[CompanyKnowledgeChunk]) -> str:
        digest = hashlib.sha256()
        for chunk in chunks:
            digest.update(json.dumps(chunk.to_dict(), sort_keys=True, ensure_ascii=False).encode("utf-8"))
        return digest.hexdigest()

    def _policy_id(self, rel: str, heading: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-") or "policy"
        return f"{Path(rel).stem}:{slug}"

    def _embedding_text(self, chunk: CompanyKnowledgeChunk) -> str:
        return f"{chunk.doc_path}\n{chunk.heading}\n{chunk.text}"

    def _result(self, chunk: CompanyKnowledgeChunk, score: float, method: str) -> dict[str, Any]:
        excerpt = " ".join(chunk.text.split())
        return {
            "policy_id": chunk.policy_id,
            "doc_path": chunk.doc_path,
            "heading": chunk.heading,
            "excerpt": truncate(excerpt, 700),
            "score": round(float(score), 4),
            "retrieval_method": method,
        }

    def _tokens(self, text: str) -> set[str]:
        tokens = {token.lower() for token in re.findall(r"[A-Za-z0-9_./-]{2,}", text)}
        aliases = {
            "webhook": {"signature", "callback", "replay", "idempotency"},
            "signature": {"webhook", "hmac", "reject"},
            "token": {"secret", "credential", "log"},
            "card": {"pan", "sensitive", "log"},
            "sql": {"parameterized", "injection", "query"},
            "shell": {"subprocess", "command", "execution"},
            "test": {"coverage", "regression"},
        }
        expanded = set(tokens)
        for token in tokens:
            expanded.update(aliases.get(token, set()))
        return expanded

    def _keyword_score(self, query_tokens: set[str], query: str, chunk: CompanyKnowledgeChunk) -> float:
        haystack = f"{chunk.doc_path} {chunk.heading} {chunk.text}".lower()
        if not query_tokens:
            return 0.0
        overlap = sum(1 for token in query_tokens if token in haystack)
        score = overlap / max(len(query_tokens), 1)
        query_lower = query.lower()
        for phrase in ("webhook signature", "sensitive log", "sql injection", "shell true", "command execution"):
            if phrase in query_lower and all(part in haystack for part in phrase.split()):
                score += 0.35
        return score

    def _cosine(self, left: list[float], right: list[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(a * a for a in left))
        right_norm = math.sqrt(sum(b * b for b in right))
        if left_norm == 0 or right_norm == 0:
            return 0.0
        return dot / (left_norm * right_norm)


class ReviewTools:
    def __init__(
        self,
        repo: Path,
        base: str,
        target: str,
        transcript: Transcript,
        company_kb: CompanyKnowledgeBase | None = None,
    ):
        self.repo = repo.resolve()
        self.base = base
        self.target = target
        self.transcript = transcript
        self.company_kb = company_kb
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

    def search_code(self, query: str, path: str | None = None, max_matches: int = 20) -> list[dict[str, Any]]:
        if not query.strip():
            raise ValueError("search_code query is required")
        root = safe_repo_path(self.repo, path or ".")
        max_matches = max(1, min(int(max_matches or 20), 100))
        try:
            pattern = re.compile(query)
        except re.error:
            pattern = re.compile(re.escape(query))
        excluded = {".git", ".review-agent", ".tmp", ".pytest_cache", "__pycache__", "node_modules", ".venv"}
        candidates = [root] if root.is_file() else sorted(root.rglob("*"))
        matches: list[dict[str, Any]] = []
        for fp in candidates:
            if len(matches) >= max_matches:
                break
            if not fp.is_file():
                continue
            rel = fp.relative_to(self.repo)
            if any(part in excluded for part in rel.parts):
                continue
            if not (is_source_file(rel.as_posix()) or rel.suffix.lower() in {".md", ".txt", ".toml", ".yaml", ".yml", ".json"}):
                continue
            try:
                lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for number, text in enumerate(lines, start=1):
                if pattern.search(text):
                    matches.append({"file": rel.as_posix(), "line": number, "text": truncate(text.strip(), 500)})
                    if len(matches) >= max_matches:
                        break
        self.transcript.emit("tool.search_code", query=query, path=path or ".", count=len(matches))
        return matches

    def retrieve_company_policy(
        self,
        query: str,
        category: str | None = None,
        max_results: int = 3,
    ) -> list[dict[str, Any]]:
        if not self.company_kb:
            self.transcript.emit("tool.retrieve_company_policy.disabled", reason="company RAG is disabled")
            return []
        results = self.company_kb.search(query, category=category, max_results=max_results)
        self.transcript.emit("tool.retrieve_company_policy", count=len(results), category=category or "")
        return results

    def risk_scan(self) -> list[dict[str, Any]]:
        diff = self.git_diff()
        signals: list[dict[str, Any]] = []
        added = list(iter_added_lines(diff))
        added_by_file: dict[str, list[dict[str, Any]]] = {}
        for line in added:
            added_by_file.setdefault(line["file"], []).append(line)

        def add(line: dict[str, Any], category: str, signal: str, rationale: str) -> None:
            signals.append(
                {
                    "file": line["file"],
                    "line": line["line"],
                    "category": category,
                    "signal": signal,
                    "evidence": truncate(line["text"].strip(), 500),
                    "rationale": rationale,
                }
            )

        pending_execute: dict[str, dict[str, Any]] = {}
        pending_except: dict[str, dict[str, Any]] = {}
        for line in added:
            text = line["text"]
            stripped = text.strip()
            file_path = line["file"]
            lowered = stripped.lower()
            if ".execute(" in stripped:
                pending_execute[file_path] = line
            elif file_path in pending_execute and ('f"' in stripped or "f'" in stripped or "{" in stripped):
                add(
                    line,
                    "security",
                    "sql_interpolation",
                    "SQL text appears to interpolate variables across a multi-line execute call.",
                )
                pending_execute.pop(file_path, None)
            if re.search(r"\.execute\(f[\"']", stripped) or re.search(r"\.execute\(.*\+.*\)", stripped):
                add(line, "security", "sql_interpolation", "SQL query appears to interpolate values directly.")
            if "shell=True" in stripped or re.search(r"\bos\.system\(", stripped):
                add(line, "security", "shell_execution", "Shell execution is introduced on a path containing formatted values.")
            if re.match(r"def .*=\s*(\[\]|\{\})", stripped):
                add(line, "correctness", "mutable_default", "Mutable default arguments can share state across requests.")
            if re.match(r"except(?:\s+Exception)?\s*:$", stripped):
                pending_except[file_path] = line
            if re.match(r"except(?:\s+Exception)?\s*:\s*pass$", stripped) or (
                stripped == "pass" and pending_except.get(file_path, {}).get("line") == line["line"] - 1
            ):
                add(
                    pending_except.get(file_path, line),
                    "correctness",
                    "swallowed_exception",
                    "The new code swallows exceptions and can fail open or hide data failures.",
                )
            if re.search(r"\b(print|logging\.\w+)\(.*(token|secret|signature|card|password)", lowered) and (
                "{" in stripped or "=" in stripped
            ):
                add(line, "security", "sensitive_logging", "Sensitive values appear in logs or print output.")
            if "signature !=" in stripped and file_path in added_by_file:
                next_lines = [
                    item["text"].strip().lower()
                    for item in added_by_file[file_path]
                    if line["line"] < item["line"] <= line["line"] + 3
                ]
                rejects = ("reject", "unauthorized", "forbidden", "invalid", "401", "403", "raise")
                if not any(text.startswith("raise") or (text.startswith("return") and any(token in text for token in rejects)) for text in next_lines):
                    add(line, "security", "signature_mismatch_not_rejected", "Signature mismatch is observed but not rejected.")
            if "startswith(\"test-\")" in stripped or "startswith('test-')" in stripped:
                add(line, "correctness", "trust_bypass_pattern", "A test-prefix shortcut can accidentally trust production input.")
            if "high_risk" in lowered and "approved" in lowered:
                add(line, "correctness", "risk_rule_approves_high_risk_case", "High-risk payment logic returns approval.")
            if "amount = -abs(" in stripped:
                add(line, "correctness", "negative_amount_refund", "Refund handling creates negative amounts that may need downstream validation.")

        changed_files = self.changed_files()
        if any(is_source_file(item.get("file", "")) for item in changed_files) and not any(
            is_test_file(item.get("file", "")) for item in changed_files
        ):
            first_source = next(item for item in changed_files if is_source_file(item.get("file", "")))
            signals.append(
                {
                    "file": first_source["file"],
                    "line": 1,
                    "category": "testing",
                    "signal": "no_tests_changed",
                    "evidence": "No test files changed with this source diff.",
                    "rationale": "Risk-sensitive source changes should normally include targeted tests.",
                }
            )
            test_signal_map = {
                "risk_rule_approves_high_risk_case": "missing_test_high_risk_country_logic",
                "signature_mismatch_not_rejected": "missing_test_webhook_signature_mismatch",
                "negative_amount_refund": "missing_test_refund_amount_handling",
                "sql_interpolation": "missing_test_sql_parameterization_or_malicious_user_id",
                "shell_execution": "missing_test_shell_execution_input_handling",
                "swallowed_exception": "missing_test_failure_path",
            }
            for signal in list(signals):
                mapped = test_signal_map.get(signal["signal"])
                if not mapped:
                    continue
                signals.append(
                    {
                        "file": signal["file"],
                        "line": signal["line"],
                        "category": "testing",
                        "signal": mapped,
                        "evidence": signal["evidence"],
                        "rationale": f"No test files changed for critical behavior: {signal['rationale']}",
                    }
                )
        self.transcript.emit("tool.risk_scan", count=len(signals))
        return signals


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
        elif name == "company-policy-reviewer":
            count = self.company_policy()
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

    def company_policy(self) -> int:
        before = len(self.collector.findings)
        policy_map = {
            "sql_interpolation": {
                "severity": "P1",
                "category": "security",
                "title": "Company policy requires parameterized SQL",
                "impact": "This violates the company SQL baseline and can expose payment data through injection.",
                "suggestion": "Use database-driver parameter binding and add a malicious identifier regression test.",
            },
            "shell_execution": {
                "severity": "P1",
                "category": "security",
                "title": "Company policy blocks shell execution with request input",
                "impact": "This violates the command execution baseline for request paths and can become command injection.",
                "suggestion": "Use an argument list with shell=False and validate every argument.",
            },
            "sensitive_logging": {
                "severity": "P1",
                "category": "security",
                "title": "Company policy forbids logging payment secrets",
                "impact": "This violates sensitive logging rules and can leak credentials or card data into log systems.",
                "suggestion": "Remove the sensitive fields or use masked logging helpers.",
            },
            "signature_mismatch_not_rejected": {
                "severity": "P1",
                "category": "security",
                "title": "Company policy requires webhook signature failures to reject",
                "impact": "This violates payment webhook policy and can accept forged or replayed payment callbacks.",
                "suggestion": "Reject invalid signatures before any state mutation and use constant-time comparison.",
            },
            "swallowed_exception": {
                "severity": "P2",
                "category": "correctness",
                "title": "Company policy requires risk controls to fail closed",
                "impact": "This violates payment risk policy because failures can disappear and default to unsafe behavior.",
                "suggestion": "Handle specific exceptions and return a conservative manual review decision.",
            },
            "no_tests_changed": {
                "severity": "P2",
                "category": "testing",
                "title": "Company policy requires tests for payment-sensitive changes",
                "impact": "This violates testing policy for payment, security, or risk-control changes.",
                "suggestion": "Add targeted tests for the changed payment behavior and failure paths.",
            },
        }
        for signal in self.tools.risk_scan():
            spec = policy_map.get(signal.get("signal"))
            if not spec:
                continue
            policies = self.tools.retrieve_company_policy(
                " ".join([str(signal.get("signal", "")), str(signal.get("evidence", "")), str(signal.get("rationale", ""))]),
                category=str(spec["category"]),
                max_results=2,
            )
            if not policies:
                continue
            policy_text = "; ".join(policy["policy_id"] for policy in policies)
            self.collector.emit(
                Finding(
                    file=str(signal["file"]),
                    line=int(signal["line"]),
                    severity=str(spec["severity"]),
                    category=str(spec["category"]),
                    title=str(spec["title"]),
                    evidence=f"{signal['evidence']} | company policy: {policy_text}",
                    impact=str(spec["impact"]),
                    suggestion=str(spec["suggestion"]),
                )
            )
        return len(self.collector.findings) - before



class LLMClient:
    provider = "none"
    enabled = False

    def plan_review(
        self,
        pr_description: str,
        skill_context: str,
        diff: str,
        files: list[dict[str, Any]],
        test_result: dict[str, Any] | None,
    ) -> dict[str, str]:
        return {}

    def review(
        self,
        reviewer_name: str,
        reviewer_role: str,
        focus: str,
        skill_context: str,
        diff: str,
        files: list[dict[str, Any]],
        test_result: dict[str, Any] | None,
    ) -> list[Finding]:
        return []

    def critique_finding(
        self,
        item: CouncilFinding,
        evidence_chain: list[dict[str, Any]],
        skill_context: str = "",
    ) -> dict[str, str]:
        return {}

    def resolve_finding(
        self,
        item: CouncilFinding,
        evidence_chain: list[dict[str, Any]],
        skill_context: str = "",
    ) -> dict[str, str]:
        return {}

    def next_action(
        self,
        agent_state: dict[str, Any],
        skill_context: str,
        tools: list[dict[str, Any]],
        observations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {}

    def coverage_review(
        self,
        pr_description: str,
        skill_context: str,
        diff: str,
        files: list[dict[str, Any]],
        existing_findings: list[dict[str, Any]],
        observations: list[dict[str, Any]],
    ) -> list[Finding]:
        return []

    def next_debate_action(
        self,
        debate_state: dict[str, Any],
        skill_context: str,
    ) -> dict[str, Any]:
        return {}

    def reviewer_defense(
        self,
        reviewer_name: str,
        item: CouncilFinding,
        challenge: str,
        evidence_chain: list[dict[str, Any]],
        skill_context: str = "",
    ) -> dict[str, Any]:
        return {}

    def report_writer_review(
        self,
        report_context: dict[str, Any],
        language: str = "zh",
    ) -> dict[str, Any]:
        return {}

    def judge_report(
        self,
        judge_input: dict[str, Any],
        diff: str,
        pr_description: str,
    ) -> dict[str, Any]:
        return {}


class AliyunDashScopeClient(LLMClient):
    provider = "aliyun"

    def __init__(
        self,
        api_key: str | None,
        model: str,
        base_url: str,
        transcript: Transcript,
        timeout: int = 60,
    ):
        self.api_key = api_key or ""
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.transcript = transcript
        self.timeout = timeout
        self.enabled = bool(self.api_key)
        self._pending_actions: list[dict[str, Any]] = []
        self._last_extra_json_payloads: list[Any] = []

    def _chat_json(self, agent_name: str, system_prompt: str, user_prompt: str) -> Any:
        self._last_extra_json_payloads = []
        if not self.enabled:
            self.transcript.emit(
                "llm.skipped",
                provider=self.provider,
                reviewer=agent_name,
                reason="DASHSCOPE_API_KEY is not set",
            )
            return None

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
        }
        endpoint = f"{self.base_url}/chat/completions"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        self.transcript.emit(
            "llm.request",
            provider=self.provider,
            reviewer=agent_name,
            model=self.model,
            endpoint=endpoint,
            prompt_chars=len(user_prompt),
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self.transcript.emit(
                "llm.error",
                provider=self.provider,
                reviewer=agent_name,
                error=str(exc),
            )
            return None

        self.transcript.emit(
            "llm.response",
            provider=self.provider,
            reviewer=agent_name,
            chars=len(raw),
        )
        try:
            data = json.loads(raw)
            content = data["choices"][0]["message"]["content"]
            return self._parse_json_content(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            self.transcript.emit(
                "llm.parse_error",
                provider=self.provider,
                reviewer=agent_name,
                error=str(exc),
                raw=truncate(raw, 2000),
            )
            return None

    def plan_review(
        self,
        pr_description: str,
        skill_context: str,
        diff: str,
        files: list[dict[str, Any]],
        test_result: dict[str, Any] | None,
    ) -> dict[str, str]:
        schema = {
            "assignments": [
                {
                    "reviewer": "security-reviewer",
                    "focus": "specific review focus for this PR",
                }
            ]
        }
        prompt = "\n".join(
            [
                "Create a concise review plan for a multi-agent PR review council.",
                "Allowed reviewers: security-reviewer, correctness-reviewer, test-reviewer, maintainability-reviewer, company-policy-reviewer.",
                "Return JSON only in this shape:",
                json.dumps(schema, ensure_ascii=False),
                "Code-review skill instructions:",
                truncate(skill_context, MAX_SKILL_CHARS),
                "PR description:",
                pr_description[:8000],
                "Changed files:",
                json.dumps(files, ensure_ascii=False),
                "Test result:",
                json.dumps(test_result or {}, ensure_ascii=False)[:4000],
                "Diff:",
                truncate(diff, 40000),
            ]
        )
        payload = self._chat_json("lead-reviewer.plan", self._system_prompt("lead-reviewer.plan"), prompt)
        if not isinstance(payload, dict):
            return {}
        assignments = payload.get("assignments", [])
        if not isinstance(assignments, list):
            return {}
        allowed = {
            "security-reviewer",
            "correctness-reviewer",
            "test-reviewer",
            "maintainability-reviewer",
            "company-policy-reviewer",
        }
        plan: dict[str, str] = {}
        for item in assignments:
            if not isinstance(item, dict):
                continue
            reviewer = str(item.get("reviewer", "")).strip()
            focus = str(item.get("focus", "")).strip()
            if reviewer in allowed and focus:
                plan[reviewer] = focus[:1000]
        return plan

    def review(
        self,
        reviewer_name: str,
        reviewer_role: str,
        focus: str,
        skill_context: str,
        diff: str,
        files: list[dict[str, Any]],
        test_result: dict[str, Any] | None,
    ) -> list[Finding]:
        prompt = self._prompt(reviewer_name, reviewer_role, focus, skill_context, diff, files, test_result)
        payload = self._chat_json(reviewer_name, self._system_prompt(reviewer_name), prompt)
        if payload is None:
            return []
        return self._parse_findings_payload(payload)

    def critique_finding(
        self,
        item: CouncilFinding,
        evidence_chain: list[dict[str, Any]],
        skill_context: str = "",
    ) -> dict[str, str]:
        schema = {
            "decision": "challenge",
            "reason": "specific reason",
        }
        prompt = "\n".join(
            [
                "Review this proposed finding. Do not find new issues.",
                "Decide whether the evidence is sufficient and the severity is justified.",
                "Use decision 'challenge' or 'no_challenge'. Return JSON only:",
                json.dumps(schema, ensure_ascii=False),
                "Code-review skill instructions:",
                truncate(skill_context, MAX_SKILL_CHARS),
                "Finding:",
                json.dumps(item.to_dict(evidence_chain), ensure_ascii=False)[:12000],
            ]
        )
        payload = self._chat_json("critic-reviewer", self._system_prompt("critic-reviewer"), prompt)
        if not isinstance(payload, dict):
            return {}
        decision = str(payload.get("decision", "")).strip()
        reason = str(payload.get("reason", "")).strip()
        if decision not in {"challenge", "no_challenge"} or not reason:
            return {}
        return {"decision": decision, "reason": reason[:1200]}

    def resolve_finding(
        self,
        item: CouncilFinding,
        evidence_chain: list[dict[str, Any]],
        skill_context: str = "",
    ) -> dict[str, str]:
        schema = {
            "resolution": "accepted",
            "reason": "specific reason",
            "severity": "P2",
        }
        prompt = "\n".join(
            [
                "Make the final lead-reviewer resolution for this finding.",
                "Use resolution accepted, rejected, or downgraded.",
                "If downgraded, include the new severity P0/P1/P2/P3.",
                "Prefer evidence-backed findings over volume. Return JSON only:",
                json.dumps(schema, ensure_ascii=False),
                "Code-review skill instructions:",
                truncate(skill_context, MAX_SKILL_CHARS),
                "Finding:",
                json.dumps(item.to_dict(evidence_chain), ensure_ascii=False)[:16000],
            ]
        )
        payload = self._chat_json("lead-reviewer.resolve", self._system_prompt("lead-reviewer.resolve"), prompt)
        if not isinstance(payload, dict):
            return {}
        resolution = str(payload.get("resolution", "")).strip()
        reason = str(payload.get("reason", "")).strip()
        severity = str(payload.get("severity", item.finding.severity)).strip()
        if resolution not in {"accepted", "rejected", "downgraded"} or not reason:
            return {}
        if severity not in SEVERITIES:
            severity = item.finding.severity
        return {"resolution": resolution, "reason": reason[:1200], "severity": severity}

    def coverage_review(
        self,
        pr_description: str,
        skill_context: str,
        diff: str,
        files: list[dict[str, Any]],
        existing_findings: list[dict[str, Any]],
        observations: list[dict[str, Any]],
    ) -> list[Finding]:
        schema = {
            "findings": [
                {
                    "file": "path/to/file.py",
                    "line": 42,
                    "severity": "P1",
                    "category": "security",
                    "title": "short actionable title",
                    "evidence": "specific diff line or code snippet",
                    "impact": "why this matters",
                    "suggestion": "concrete fix",
                }
            ]
        }
        prompt = "\n".join(
            [
                "You are the same autonomous PR review agent doing a final coverage reflection before finalize.",
                "This is not a role-based workflow. Do one holistic pass over the full diff and current evidence.",
                "Find high-confidence issues the dynamic loop may have missed.",
                "The recent observations may include a risk_scan result. Treat those risk signals as candidate evidence,",
                "and convert every high-confidence non-duplicate risk signal into a finding.",
                "Prioritize merge-blocking or reviewer-worthy findings across:",
                "- injection and unsafe command execution",
                "- secrets, sensitive logging, and trust-boundary mistakes",
                "- authentication, webhook signature, idempotency, and replay handling",
                "- mutable default arguments or shared state across requests",
                "- swallowed exceptions and fail-open behavior",
                "- suspicious business logic bypasses",
                "- missing tests for critical changed behavior",
                "If risk_scan reports testing signals, prefer concrete missing-test findings tied to the exact changed behavior.",
                "Avoid duplicates of existing findings. Do not invent issues outside the diff.",
                "Do not report documented placeholders such as your_api_key in .env.example as leaked real secrets.",
                "Return JSON only in this shape:",
                json.dumps(schema, ensure_ascii=False),
                "Code-review skill instructions:",
                truncate(skill_context, MAX_SKILL_CHARS),
                "PR description:",
                pr_description[:8000],
                "Changed files:",
                json.dumps(files, ensure_ascii=False),
                "Existing findings:",
                json.dumps(existing_findings, ensure_ascii=False)[:12000],
                "Recent observations:",
                json.dumps(observations[-12:], ensure_ascii=False)[:16000],
                "Full diff:",
                truncate(diff, MAX_DIFF_CHARS),
            ]
        )
        payload = self._chat_json(
            "agentic-reviewer.coverage_review",
            self._system_prompt("agentic-reviewer"),
            prompt,
        )
        if payload is None:
            return []
        return self._parse_findings_payload(payload)

    def next_debate_action(
        self,
        debate_state: dict[str, Any],
        skill_context: str,
    ) -> dict[str, Any]:
        schema = {
            "thought": "short reason for the next debate step",
            "action": "ask_critic",
            "finding_id": "F-001",
            "reason": "why this action is useful",
        }
        allowed = [
            "ask_critic",
            "request_reviewer_defense",
            "request_more_evidence",
            "revise_finding",
            "merge_duplicates",
            "accept_finding",
            "reject_finding",
            "ask_report_writer",
            "finalize",
        ]
        prompt = "\n".join(
            [
                "You are the lead controller of a multi-agent code review debate council.",
                "Do not follow a fixed workflow. Choose the single most useful next debate action.",
                "The goal is high-quality review, not a larger number of findings.",
                "Prefer actions that reduce duplicates, improve evidence, correct severity, or remove noise.",
                "Allowed actions:",
                ", ".join(allowed),
                "For revise_finding include finding_id, finding, and reason.",
                "For merge_duplicates include source_id, target_id, and reason.",
                "For accept_finding/reject_finding include finding_id and reason.",
                "For request_more_evidence include finding_id and reason.",
                "For ask_report_writer include reason.",
                "Return JSON only in this shape:",
                json.dumps(schema, ensure_ascii=False),
                "Code-review skill instructions:",
                truncate(skill_context, MAX_SKILL_CHARS),
                "Current debate state:",
                json.dumps(debate_state, ensure_ascii=False)[:40000],
            ]
        )
        payload = self._chat_json("lead-reviewer.debate", self._system_prompt("lead-reviewer.debate"), prompt)
        if not isinstance(payload, dict):
            return {}
        return self._normalize_debate_action(payload)

    def reviewer_defense(
        self,
        reviewer_name: str,
        item: CouncilFinding,
        challenge: str,
        evidence_chain: list[dict[str, Any]],
        skill_context: str = "",
    ) -> dict[str, Any]:
        schema = {
            "decision": "defend",
            "reason": "why the finding should stand, be revised, or be withdrawn",
            "finding": {
                "file": "path/to/file.py",
                "line": 42,
                "severity": "P2",
                "category": "security",
                "title": "revised title if decision is revise",
                "evidence": "specific evidence",
                "impact": "impact",
                "suggestion": "fix",
            },
        }
        prompt = "\n".join(
            [
                f"You are {reviewer_name} responding to a critic challenge.",
                "Choose decision defend, revise, or withdraw.",
                "If revise, include a complete revised finding. If defend or withdraw, finding may be omitted.",
                "Return JSON only in this shape:",
                json.dumps(schema, ensure_ascii=False),
                "Code-review skill instructions:",
                truncate(skill_context, MAX_SKILL_CHARS),
                "Challenge:",
                challenge[:4000],
                "Finding and evidence:",
                json.dumps(item.to_dict(evidence_chain), ensure_ascii=False)[:16000],
            ]
        )
        payload = self._chat_json(reviewer_name + ".defense", self._system_prompt(reviewer_name), prompt)
        if not isinstance(payload, dict):
            return {}
        decision = str(payload.get("decision", "")).strip()
        reason = str(payload.get("reason", "")).strip()
        if decision not in {"defend", "revise", "withdraw"} or not reason:
            return {}
        result: dict[str, Any] = {"decision": decision, "reason": reason[:1200]}
        finding = payload.get("finding")
        if decision == "revise" and isinstance(finding, dict):
            try:
                Finding(**finding).validate()
            except (TypeError, ValueError):
                return {}
            result["finding"] = finding
        return result

    def report_writer_review(
        self,
        report_context: dict[str, Any],
        language: str = "zh",
    ) -> dict[str, Any]:
        schema = {
            "verdict": "request_changes",
            "summary_points": ["short factual point"],
            "accepted_findings": [
                {
                    "issue": "short issue",
                    "severity": "P1",
                    "file": "path/to/file.py",
                    "line": 42,
                    "evidence": "specific evidence",
                    "impact": "impact",
                    "fix": "fix",
                    "why_accepted": "evidence-backed reason",
                    "critic_notes": "critic or lead notes",
                    "policy_references": ["company policy id or source"],
                }
            ],
            "rejected_findings": [],
            "downgraded_findings": [],
            "duplicate_notes": [],
            "critic_notes": [],
        }
        prompt = "\n".join(
            [
                "You are ReportWriterAgent for a PR review council.",
                "Standardize the report into fixed JSON fields. Do not write free-form Markdown.",
                "Use short factual sentences. Avoid rhetorical style, marketing language, or literary polish.",
                "The downstream AI judge should score evidence quality, not writing style.",
                f"Report language: {language}",
                "Return JSON only in this shape:",
                json.dumps(schema, ensure_ascii=False),
                "Report context:",
                json.dumps(report_context, ensure_ascii=False)[:50000],
            ]
        )
        payload = self._chat_json("report-writer", self._system_prompt("report-writer"), prompt)
        return payload if isinstance(payload, dict) else {}

    def judge_report(
        self,
        judge_input: dict[str, Any],
        diff: str,
        pr_description: str,
    ) -> dict[str, Any]:
        schema = {
            "overall_score": 85,
            "verdict": "pass",
            "dimensions": {
                "critical_issue_coverage": 0,
                "evidence_quality": 0,
                "severity_accuracy": 0,
                "duplicate_noise_control": 0,
                "actionability": 0,
                "report_clarity": 0,
            },
            "strengths": ["short point"],
            "weaknesses": ["short point"],
            "recommendations": ["short point"],
        }
        prompt = "\n".join(
            [
                "You are an independent AI judge for PR review quality.",
                "Ignore writing polish and rhetorical style. Score only review quality and evidence quality.",
                "Penalize duplicate findings, unsupported claims, severity inflation, and missed critical issues.",
                "Score each dimension from 0 to 100. Return JSON only in this shape:",
                json.dumps(schema, ensure_ascii=False),
                "PR description:",
                pr_description[:8000],
                "Standardized judge input:",
                json.dumps(judge_input, ensure_ascii=False)[:50000],
                "Diff:",
                truncate(diff, MAX_DIFF_CHARS),
            ]
        )
        payload = self._chat_json("ai-judge", self._system_prompt("ai-judge"), prompt)
        return payload if isinstance(payload, dict) else {}

    def next_action(
        self,
        agent_state: dict[str, Any],
        skill_context: str,
        tools: list[dict[str, Any]],
        observations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        tool_names = {tool["name"] for tool in tools}
        while self._pending_actions:
            queued = self._pending_actions.pop(0)
            normalized = self._normalize_action(queued, tool_names)
            if normalized:
                self.transcript.emit(
                    "llm.queued_action",
                    provider=self.provider,
                    reviewer="agentic-reviewer.next_action",
                    action=normalized["action"],
                    remaining=len(self._pending_actions),
                )
                return normalized

        schema = {
            "thought": "short private planning summary",
            "action": "call_tool",
            "tool": "changed_files",
            "args": {},
        }
        prompt = "\n".join(
            [
                "You are running an autonomous read-only PR review loop.",
                "Choose exactly one next action. Do not follow a fixed workflow if another action is more useful.",
                "Allowed actions:",
                "- update_todos: include todos=[{content,status}] with status pending/in_progress/completed",
                "- call_tool: include tool and args",
                "- emit_finding: include finding with file,line,severity,category,title,evidence,impact,suggestion",
                "- ask_critic: include finding_id for an existing candidate finding",
                "- finalize: include reason when enough investigation has been done",
                "You may return either one JSON action or a JSON array of independent next actions.",
                "When returning a batch, do not include finalize; the runtime will execute queued actions after each observation.",
                "Do not report placeholder examples such as your_api_key in .env.example as leaked real secrets.",
                "Return JSON only, using this object shape for each action:",
                json.dumps(schema, ensure_ascii=False),
                "Code-review skill instructions:",
                truncate(skill_context, MAX_SKILL_CHARS),
                "Available tools:",
                json.dumps(tools, ensure_ascii=False)[:12000],
                "Current agent state:",
                json.dumps(agent_state, ensure_ascii=False)[:16000],
                "Recent observations:",
                json.dumps(observations[-8:], ensure_ascii=False)[:16000],
            ]
        )
        payload = self._chat_json("agentic-reviewer.next_action", self._system_prompt("agentic-reviewer"), prompt)
        if isinstance(payload, list):
            actions = [item for item in payload if isinstance(item, dict)]
            if not actions:
                return {}
            payload = actions[0]
            self._last_extra_json_payloads.extend(actions[1:])
        if not isinstance(payload, dict):
            return {}
        self._enqueue_extra_actions(tool_names)
        normalized = self._normalize_action(payload, tool_names)
        if normalized:
            return normalized
        self.transcript.emit(
            "llm.invalid_action",
            provider=self.provider,
            reviewer="agentic-reviewer.next_action",
            raw=truncate(json.dumps(payload, ensure_ascii=False), 2000),
        )
        repair_prompt = "\n".join(
            [
                "The previous response was not a valid action for the PR review agent.",
                "Rewrite it as exactly one valid JSON action with no commentary.",
                "Allowed actions: update_todos, call_tool, emit_finding, ask_critic, finalize.",
                "For call_tool use fields: action, tool, args.",
                "For finalize use fields: action, reason.",
                "Available tool names:",
                json.dumps(sorted(tool_names), ensure_ascii=False),
                "Invalid response:",
                json.dumps(payload, ensure_ascii=False)[:4000],
            ]
        )
        repaired = self._chat_json(
            "agentic-reviewer.repair_action",
            self._system_prompt("agentic-reviewer"),
            repair_prompt,
        )
        if not isinstance(repaired, dict):
            return {}
        normalized = self._normalize_action(repaired, tool_names)
        if not normalized:
            self.transcript.emit(
                "llm.invalid_action",
                provider=self.provider,
                reviewer="agentic-reviewer.repair_action",
                raw=truncate(json.dumps(repaired, ensure_ascii=False), 2000),
            )
        return normalized

    def _enqueue_extra_actions(self, tool_names: set[str]) -> None:
        queued: list[dict[str, Any]] = []
        for payload in self._last_extra_json_payloads:
            if not isinstance(payload, dict):
                continue
            normalized = self._normalize_action(payload, tool_names)
            if normalized and normalized["action"] != "finalize":
                queued.append(payload)
        if not queued:
            return
        self._pending_actions.extend(queued)
        self.transcript.emit(
            "llm.action_queue.extend",
            provider=self.provider,
            reviewer="agentic-reviewer.next_action",
            count=len(queued),
            pending=len(self._pending_actions),
        )

    def _system_prompt(self, agent_name: str) -> str:
        base = "Return only valid JSON. Do not include markdown fences or commentary."
        prompts = {
            "lead-reviewer.plan": (
                "You are the lead reviewer coordinating a multi-agent PR review council. "
                "Plan focused assignments from PR context, changed files, tests, and diff. "
                "Prefer precise, risk-driven review focus over broad generic instructions. "
                + base
            ),
            "lead-reviewer.resolve": (
                "You are the lead reviewer making final resolutions after specialist review and critic review. "
                "Accept only findings supported by concrete evidence from the diff or source context. "
                "Reject weak or unrelated findings; downgrade overstated severity. "
                "Reject findings that treat documented placeholders such as your_api_key in .env.example as leaked real secrets. "
                + base
            ),
            "lead-reviewer.debate": (
                "You are a debate controller for a multi-agent PR review council. "
                "Your job is to improve review quality through targeted challenge, defense, evidence gathering, "
                "deduplication, and final resolution. Optimize for true critical coverage, low noise, and clear evidence. "
                + base
            ),
            "security-reviewer": (
                "You are a senior security code review agent. Focus only on exploitable security risks "
                "introduced by this diff: secrets, auth bypass, injection, unsafe command execution, "
                "sensitive logging, crypto, and trust-boundary mistakes. Every finding must cite concrete evidence. "
                + base
            ),
            "correctness-reviewer": (
                "You are a senior correctness reviewer. Focus on business logic regressions, edge cases, "
                "exception handling, state bugs, idempotency, data consistency, and payment correctness. "
                "Avoid security-only or style-only issues unless they cause correctness failures. "
                + base
            ),
            "test-reviewer": (
                "You are a senior test reviewer. Focus on missing tests for changed behavior, edge cases, "
                "failure paths, security-sensitive paths, and whether the configured tests are meaningful. "
                "Do not report implementation issues unless the key problem is missing test coverage. "
                + base
            ),
            "maintainability-reviewer": (
                "You are a senior maintainability reviewer. Focus on long-term code health: complexity, "
                "coupling, confusing control flow, unsafe shared state, observability, and operational clarity. "
                "Avoid duplicating security or correctness findings unless maintainability is the main issue. "
                + base
            ),
            "company-policy-reviewer": (
                "You are a company policy alignment reviewer. Focus only on violations of the provided company "
                "knowledge, security baselines, payment policies, testing standards, and incident cases. "
                "Every finding must cite concrete code evidence and a company policy or incident reference. "
                "Do not report generic issues unless the company policy context makes them actionable. "
                + base
            ),
            "critic-reviewer": (
                "You are a skeptical review critic. Your job is not to find new issues, but to verify whether "
                "a proposed finding is well-supported, scoped to the diff, correctly categorized, and assigned "
                "an appropriate severity. Challenge weak evidence, vague impact, wrong severity, or non-actionable findings. "
                "Challenge findings that confuse example placeholders with real committed credentials. "
                + base
            ),
            "agentic-reviewer": (
                "You are an autonomous read-only PR review agent inspired by Claude Code and Codex. "
                "Use an observe-think-act loop: update todos, call tools, inspect observations, gather evidence, "
                "ask the critic for risky findings, and finalize only when you have enough evidence. "
                "Never modify files. Prefer targeted tool calls over fixed reviewer pipelines. "
                + base
            ),
            "report-writer": (
                "You are a structured report writer agent. Produce only standardized JSON fields. "
                "Use plain factual language and avoid style flourishes so evaluation scores reflect review quality. "
                + base
            ),
            "ai-judge": (
                "You are an independent evaluator of PR review reports. Ignore writing polish and score evidence, "
                "coverage of critical issues, severity accuracy, duplicate/noise control, and actionability. "
                + base
            ),
        }
        return prompts.get(agent_name, "You are a senior code review agent. " + base)

    def _prompt(
        self,
        reviewer_name: str,
        reviewer_role: str,
        focus: str,
        skill_context: str,
        diff: str,
        files: list[dict[str, Any]],
        test_result: dict[str, Any] | None,
    ) -> str:
        schema = {
            "findings": [
                {
                    "file": "path/to/file.py",
                    "line": 42,
                    "severity": "P1",
                    "category": "security",
                    "title": "short actionable title",
                    "evidence": "specific diff line or code snippet",
                    "impact": "why this matters",
                    "suggestion": "concrete fix",
                }
            ]
        }
        return "\n".join(
            [
                f"Reviewer: {reviewer_name}",
                f"Role: {reviewer_role}",
                "Lead reviewer focus for this PR:",
                focus or "Use the role scope and the concrete diff evidence.",
                "Code-review skill instructions:",
                truncate(skill_context, MAX_SKILL_CHARS),
                "Review only this role's scope. Prefer high-confidence, actionable issues.",
                "Use severity P0/P1/P2/P3 and category security/correctness/performance/testing/maintainability.",
                "Return JSON exactly in this shape:",
                json.dumps(schema, ensure_ascii=False),
                "Changed files:",
                json.dumps(files, ensure_ascii=False),
                "Test result:",
                json.dumps(test_result or {}, ensure_ascii=False)[:4000],
                "Diff:",
                truncate(diff, MAX_DIFF_CHARS),
            ]
        )

    def _parse_json_content(self, content: str) -> Any:
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            objects = self._parse_sequential_json_objects(cleaned)
            if objects:
                self._last_extra_json_payloads = objects[1:]
                if self._last_extra_json_payloads:
                    self.transcript.emit(
                        "llm.extra_json_queued",
                        count=len(self._last_extra_json_payloads),
                    )
                return objects[0]

            match = re.search(r"\{.*\}", cleaned, flags=re.S)
            if not match:
                raise
            matched = match.group(0)
            try:
                return json.loads(matched)
            except json.JSONDecodeError:
                objects = self._parse_sequential_json_objects(matched)
                if not objects:
                    raise
                self._last_extra_json_payloads = objects[1:]
                if self._last_extra_json_payloads:
                    self.transcript.emit(
                        "llm.extra_json_queued",
                        count=len(self._last_extra_json_payloads),
                    )
                return objects[0]

    def _parse_sequential_json_objects(self, text: str) -> list[Any]:
        decoder = json.JSONDecoder()
        objects: list[Any] = []
        index = 0
        while index < len(text):
            while index < len(text) and text[index].isspace():
                index += 1
            if index >= len(text):
                break
            try:
                value, end_index = decoder.raw_decode(text, index)
            except json.JSONDecodeError:
                if objects:
                    self.transcript.emit(
                        "llm.trailing_text_ignored",
                        ignored_chars=len(text) - index,
                        preview=truncate(text[index:], 500),
                    )
                break
            objects.append(value)
            index = end_index
        return objects

    def _parse_findings_payload(self, payload: Any) -> list[Finding]:
        if isinstance(payload, dict):
            items = payload.get("findings", [])
        elif isinstance(payload, list):
            items = payload
        else:
            items = []
        collector = FindingCollector()
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                collector.emit(item)
            except ValueError:
                continue
        return collector.sorted()

    def _normalize_action(self, payload: dict[str, Any], tool_names: set[str]) -> dict[str, Any]:
        action = str(payload.get("action", "")).strip()
        aliases = {
            "tool_call": "call_tool",
            "use_tool": "call_tool",
            "create_finding": "emit_finding",
            "report_finding": "emit_finding",
            "critic": "ask_critic",
            "finish": "finalize",
            "done": "finalize",
            "final": "finalize",
        }
        action = aliases.get(action, action)
        if action in tool_names:
            args = payload.get("args", payload.get("parameters"))
            if not isinstance(args, dict):
                args = {
                    key: value
                    for key, value in payload.items()
                    if key not in {"thought", "action", "tool", "tool_name"}
                }
            payload = {**payload, "action": "call_tool", "tool": action, "args": args}
            action = "call_tool"
        if action not in {"update_todos", "call_tool", "emit_finding", "ask_critic", "finalize"}:
            return {}
        normalized: dict[str, Any] = {
            "thought": str(payload.get("thought", "")).strip()[:1200],
            "action": action,
        }
        if action == "update_todos":
            todos = payload.get("todos")
            if not isinstance(todos, list):
                return {}
            normalized["todos"] = todos
        elif action == "call_tool":
            tool = str(payload.get("tool") or payload.get("tool_name") or "").strip()
            if tool not in tool_names:
                return {}
            args = payload.get("args", payload.get("parameters", {}))
            normalized["tool"] = tool
            normalized["args"] = args if isinstance(args, dict) else {}
        elif action == "emit_finding":
            finding = payload.get("finding")
            if not isinstance(finding, dict):
                return {}
            try:
                Finding(**finding).validate()
            except (TypeError, ValueError):
                return {}
            normalized["finding"] = finding
        elif action == "ask_critic":
            finding_id = str(payload.get("finding_id", "")).strip()
            finding = payload.get("finding")
            if finding_id:
                normalized["finding_id"] = finding_id
            elif isinstance(finding, dict):
                normalized["finding"] = finding
            else:
                return {}
        elif action == "finalize":
            normalized["reason"] = str(payload.get("reason", "")).strip()[:1200] or "Agent decided review is complete."
        return normalized


    def _normalize_debate_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action", "")).strip()
        aliases = {
            "critic": "ask_critic",
            "challenge": "ask_critic",
            "defense": "request_reviewer_defense",
            "more_evidence": "request_more_evidence",
            "revise": "revise_finding",
            "merge": "merge_duplicates",
            "accept": "accept_finding",
            "reject": "reject_finding",
            "report_writer": "ask_report_writer",
            "finish": "finalize",
            "done": "finalize",
        }
        action = aliases.get(action, action)
        allowed = {
            "ask_critic",
            "request_reviewer_defense",
            "request_more_evidence",
            "revise_finding",
            "merge_duplicates",
            "accept_finding",
            "reject_finding",
            "ask_report_writer",
            "finalize",
        }
        if action not in allowed:
            return {}
        normalized: dict[str, Any] = {
            "thought": str(payload.get("thought", "")).strip()[:1200],
            "action": action,
            "reason": str(payload.get("reason", "")).strip()[:1200],
        }
        if action in {
            "ask_critic",
            "request_reviewer_defense",
            "request_more_evidence",
            "accept_finding",
            "reject_finding",
        }:
            finding_id = str(payload.get("finding_id", "")).strip()
            if not finding_id:
                return {}
            normalized["finding_id"] = finding_id
        elif action == "revise_finding":
            finding_id = str(payload.get("finding_id", "")).strip()
            finding = payload.get("finding")
            if not finding_id or not isinstance(finding, dict):
                return {}
            try:
                Finding(**finding).validate()
            except (TypeError, ValueError):
                return {}
            normalized["finding_id"] = finding_id
            normalized["finding"] = finding
        elif action == "merge_duplicates":
            source_id = str(payload.get("source_id") or payload.get("duplicate_id") or "").strip()
            target_id = str(payload.get("target_id") or payload.get("canonical_id") or "").strip()
            if not source_id or not target_id or source_id == target_id:
                return {}
            normalized["source_id"] = source_id
            normalized["target_id"] = target_id
        elif action == "finalize":
            normalized["reason"] = normalized["reason"] or "Debate controller decided review is complete."
        return normalized


class ReviewAgentMember:
    def __init__(
        self,
        name: str,
        role: str,
        tools: ReviewTools,
        transcript: Transcript,
        llm_client: LLMClient | None = None,
    ):
        self.name = name
        self.role = role
        self.tools = tools
        self.transcript = transcript
        self.llm_client = llm_client

    def review(
        self,
        diff: str,
        files: list[dict[str, Any]],
        test_result: dict[str, Any] | None,
        focus: str = "",
        skill_context: str = "",
    ) -> list[Finding]:
        collector = FindingCollector()
        if self.llm_client:
            for finding in self.llm_client.review(
                self.name,
                self.role,
                focus,
                skill_context,
                diff,
                files,
                test_result,
            ):
                collector.emit(finding)
        reviewers = SpecialtyReviewers(self.tools, collector, self.transcript)
        reviewers.run(self.name, diff, files, test_result)
        return collector.sorted()


class CriticReviewer:
    def __init__(
        self,
        bus: MessageBus,
        evidence: EvidenceStore,
        lifecycle: FindingLifecycle,
        llm_client: LLMClient | None = None,
        skill_context: str = "",
    ):
        self.name = "critic-reviewer"
        self.bus = bus
        self.evidence = evidence
        self.lifecycle = lifecycle
        self.llm_client = llm_client
        self.skill_context = skill_context
        self._challenged_once = False

    def review(self, items: list[CouncilFinding]) -> None:
        for item in items:
            chain = self.evidence.list(item.finding_id)
            decision = (
                self.llm_client.critique_finding(item, chain, self.skill_context)
                if self.llm_client
                else {}
            )
            if decision:
                self.evidence.add(
                    item.finding_id,
                    "critic_review",
                    f"{decision['decision']}: {decision['reason']}",
                    self.name,
                )
                should_challenge = decision["decision"] == "challenge"
                reason = decision["reason"]
            else:
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


class AgenticReviewLoop:
    def __init__(
        self,
        tools: ReviewTools,
        transcript: Transcript,
        collector: FindingCollector,
        todos: TodoManager,
        critic_pass: bool = True,
        llm_client: LLMClient | None = None,
        skill_context: str = "",
        test_command: str | None = None,
        max_steps: int = 20,
    ):
        self.tools = tools
        self.transcript = transcript
        self.collector = collector
        self.todos = todos
        self.critic_pass = critic_pass
        self.llm_client = llm_client
        self.skill_context = skill_context
        self.test_command = test_command
        self.max_steps = max_steps
        self.bus = MessageBus(transcript)
        self.evidence = EvidenceStore(transcript)
        self.lifecycle = FindingLifecycle(transcript)
        self.critic = CriticReviewer(self.bus, self.evidence, self.lifecycle, llm_client, skill_context)
        self.observations: list[dict[str, Any]] = []

    def run(self, pr_description: str = "") -> dict[str, Any]:
        self.transcript.emit("agentic.start", max_steps=self.max_steps)
        self._set_default_todos()
        if not self.llm_client or not self.llm_client.enabled:
            self.transcript.emit("agentic.fallback", reason="llm unavailable")
            return self._run_local_guardrails()

        finalized = False
        for step in range(1, self.max_steps + 1):
            action = self.llm_client.next_action(
                self._agent_state(pr_description, step),
                self.skill_context,
                self._available_tools(),
                self.observations,
            )
            if not action:
                action = self._recovery_action()
                self.transcript.emit("agent.recovery_action", step=step, **action)
            self.transcript.emit("agent.action", step=step, **action)
            observation = self._apply_action(action)
            self.observations.append(observation)
            self.transcript.emit("agent.observation", step=step, **observation)
            if action["action"] == "finalize":
                finalized = True
                break

        if not finalized:
            self.transcript.emit("agent.finalize", reason="max steps reached")
        self._coverage_review(pr_description)
        self._resolve_candidates("Agentic review finalized.")
        records = [item.to_dict(self.evidence.list(item.finding_id)) for item in self.lifecycle.all()]
        self.transcript.emit("agentic.complete", candidates=len(records), accepted=len(self.lifecycle.accepted()))
        return {"findings": records, "messages": self.bus.all()}

    def _recovery_action(self) -> dict[str, Any]:
        called_tools = [obs.get("tool") for obs in self.observations if obs.get("type") == "tool"]
        if "changed_files" not in called_tools:
            return {
                "thought": "Recover from invalid LLM action by inspecting changed files.",
                "action": "call_tool",
                "tool": "changed_files",
                "args": {},
            }
        if "git_diff" not in called_tools:
            return {
                "thought": "Recover from invalid LLM action by reading the diff.",
                "action": "call_tool",
                "tool": "git_diff",
                "args": {},
            }
        return {
            "thought": "Recover from repeated invalid LLM action by finalizing with gathered evidence.",
            "action": "finalize",
            "reason": "LLM returned invalid actions after useful observations were gathered.",
        }

    def _set_default_todos(self) -> None:
        self.todos.update(
            [
                {"content": "Inspect PR context with read-only tools", "status": "in_progress"},
                {"content": "Investigate risky changes and collect evidence", "status": "pending"},
                {"content": "Ask critic for weak or merge-blocking findings", "status": "pending"},
                {"content": "Finalize accepted findings and write report", "status": "pending"},
            ]
        )
        self.transcript.emit("todo.update", todos=self.todos.items)

    def _available_tools(self) -> list[dict[str, Any]]:
        allowed = {"git_diff", "changed_files", "read_file_context", "run_tests", "secret_scan", "search_code", "risk_scan"}
        return [tool for tool in TOOLS if tool["name"] in allowed]

    def _agent_state(self, pr_description: str, step: int) -> dict[str, Any]:
        return {
            "step": step,
            "max_steps": self.max_steps,
            "repo": str(self.tools.repo),
            "base": self.tools.base,
            "target": self.tools.target,
            "pr_description": pr_description[:6000],
            "todos": self.todos.items,
            "candidate_findings": [
                item.to_dict(self.evidence.list(item.finding_id)) for item in self.lifecycle.all()
            ],
            "test_command_available": bool(self.test_command),
        }

    def _apply_action(self, action: dict[str, Any]) -> dict[str, Any]:
        kind = action["action"]
        try:
            if kind == "update_todos":
                rendered = self.todos.update(action["todos"])
                self.transcript.emit("todo.update", todos=self.todos.items)
                return {"type": "todo_update", "ok": True, "result": rendered}
            if kind == "call_tool":
                return self._call_tool(action["tool"], action.get("args", {}))
            if kind == "emit_finding":
                item = self._emit_candidate(action["finding"], proposed_by="agentic-reviewer")
                return {"type": "finding", "ok": True, "finding_id": item.finding_id}
            if kind == "ask_critic":
                return self._ask_critic(action)
            if kind == "finalize":
                self.transcript.emit("agent.finalize", reason=action.get("reason", "Agent finalized."))
                return {"type": "finalize", "ok": True, "reason": action.get("reason", "")}
        except Exception as exc:
            return {"type": kind, "ok": False, "error": str(exc)}
        return {"type": kind, "ok": False, "error": "Unknown action"}

    def _call_tool(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool == "git_diff":
            result = self.tools.git_diff(args.get("base"), args.get("target"))
        elif tool == "changed_files":
            result = self.tools.changed_files(args.get("base"), args.get("target"))
        elif tool == "read_file_context":
            result = self.tools.read_file_context(
                str(args.get("path", "")),
                int(args.get("line", 1)),
                int(args.get("radius", 4)),
            )
        elif tool == "run_tests":
            if not self.test_command:
                return {
                    "type": "tool",
                    "ok": False,
                    "tool": tool,
                    "error": "run_tests is disabled because --test-command was not provided",
                }
            result = self.tools.run_tests(self.test_command)
        elif tool == "secret_scan":
            result = self.tools.secret_scan()
        elif tool == "search_code":
            result = self.tools.search_code(
                str(args.get("query", "")),
                args.get("path"),
                int(args.get("max_matches", 20)),
            )
        elif tool == "risk_scan":
            result = self.tools.risk_scan()
        elif tool == "retrieve_company_policy":
            result = self.tools.retrieve_company_policy(
                str(args.get("query", "")),
                args.get("category"),
                int(args.get("max_results", 3)),
            )
        else:
            return {"type": "tool", "ok": False, "tool": tool, "error": "Tool is not allowed"}
        return {"type": "tool", "ok": True, "tool": tool, "result": self._compact_result(result)}

    def _compact_result(self, result: Any) -> Any:
        if isinstance(result, str):
            return truncate(result, 12000)
        text = json.dumps(result, ensure_ascii=False)
        if len(text) <= 12000:
            return result
        return {"truncated": True, "preview": truncate(text, 12000)}

    def _emit_candidate(self, finding_payload: dict[str, Any], proposed_by: str) -> CouncilFinding:
        finding = Finding(**finding_payload)
        finding.validate()
        item = self.lifecycle.candidate(finding, proposed_by)
        self.evidence.add(item.finding_id, "reviewer_explanation", finding.impact, proposed_by)
        self.evidence.add(item.finding_id, "diff_line", finding.evidence, proposed_by)
        try:
            context = self.tools.read_file_context(finding.file, finding.line, radius=3)
            self.evidence.add(item.finding_id, "file_context", context, "evidence-store")
        except Exception as exc:
            self.evidence.add(item.finding_id, "file_context_error", str(exc), "evidence-store")
        self._attach_company_policy(item.finding_id, finding)
        self.bus.send(
            proposed_by,
            "lead-reviewer",
            "candidate_finding",
            f"{finding.severity} {finding.category}: {finding.title}",
            item.finding_id,
        )
        return item

    def _attach_company_policy(self, finding_id: str, finding: Finding) -> None:
        query = " ".join([finding.title, finding.category, finding.evidence, finding.impact, finding.suggestion])
        policies = self.tools.retrieve_company_policy(query, category=finding.category, max_results=3)
        for policy in policies:
            content = (
                f"{policy['policy_id']} ({policy['doc_path']}#{policy['heading']}): "
                f"{policy['excerpt']} [method={policy['retrieval_method']} score={policy['score']}]"
            )
            self.evidence.add(finding_id, "company_policy", content, "company-rag")

    def _coverage_review(self, pr_description: str) -> None:
        if not self.llm_client or not self.llm_client.enabled:
            return
        try:
            files = self.tools.changed_files()
            diff = self.tools.git_diff()
            risk_signals = self.tools.risk_scan()
        except Exception as exc:
            self.transcript.emit("agent.coverage_review.skipped", reason=str(exc))
            return
        existing = [item.to_dict(self.evidence.list(item.finding_id)) for item in self.lifecycle.all()]
        findings = self.llm_client.coverage_review(
            pr_description,
            self.skill_context,
            diff,
            files,
            existing,
            [*self.observations, {"type": "tool", "tool": "risk_scan", "ok": True, "result": risk_signals}],
        )
        added = 0
        skipped = 0
        for finding in findings:
            if self._is_duplicate_finding(finding):
                skipped += 1
                continue
            self._emit_candidate(asdict(finding), proposed_by="agentic-coverage-reviewer")
            added += 1
        promoted = 0
        for signal in risk_signals:
            finding = self._finding_from_risk_signal(signal)
            if not finding or self._is_duplicate_finding(finding):
                continue
            self._emit_candidate(asdict(finding), proposed_by="risk-scan-tool")
            promoted += 1
        self.transcript.emit(
            "agent.coverage_review.complete",
            proposed=len(findings),
            added=added,
            skipped_duplicates=skipped,
            promoted_risk_signals=promoted,
        )

    def _is_duplicate_finding(self, finding: Finding) -> bool:
        normalized_evidence = re.sub(r"\s+", " ", finding.evidence.strip().lower())
        normalized_title = re.sub(r"\s+", " ", finding.title.strip().lower())
        for item in self.lifecycle.all():
            existing = item.finding
            if existing.file != finding.file:
                continue
            existing_evidence = re.sub(r"\s+", " ", existing.evidence.strip().lower())
            existing_title = re.sub(r"\s+", " ", existing.title.strip().lower())
            if existing.line == finding.line and existing.category == finding.category:
                return True
            if normalized_evidence and normalized_evidence == existing_evidence:
                return True
            if existing.line == finding.line and normalized_title == existing_title:
                return True
        return False

    def _finding_from_risk_signal(self, signal: dict[str, Any]) -> Finding | None:
        file = str(signal.get("file", ""))
        line = int(signal.get("line", 1) or 1)
        evidence = str(signal.get("evidence", "")).strip()
        name = str(signal.get("signal", ""))
        templates: dict[str, dict[str, str]] = {
            "sql_interpolation": {
                "severity": "P1",
                "category": "security",
                "title": "SQL query interpolates user-controlled values",
                "impact": "Interpolating request data into SQL can allow injection or unintended data access.",
                "suggestion": "Use the parameter binding API for the actual database driver.",
            },
            "shell_execution": {
                "severity": "P1",
                "category": "security",
                "title": "Shell command uses formatted runtime values",
                "impact": "Using shell=True with formatted values can turn crafted input into command execution.",
                "suggestion": "Avoid shell=True and pass an argument list to subprocess.",
            },
            "mutable_default": {
                "severity": "P2",
                "category": "correctness",
                "title": "Mutable default argument can leak state between calls",
                "impact": "The same object is reused across calls, which can leak state between requests or events.",
                "suggestion": "Default to None and allocate the list or dict inside the function.",
            },
            "swallowed_exception": {
                "severity": "P2",
                "category": "correctness",
                "title": "Exception is swallowed without recovery",
                "impact": "Failures disappear and the function can continue with misleading fallback behavior.",
                "suggestion": "Catch specific exceptions and either handle, log, or re-raise them.",
            },
            "sensitive_logging": {
                "severity": "P1",
                "category": "security",
                "title": "Sensitive values are written to logs",
                "impact": "Tokens, signatures, or card data in logs can be exposed through monitoring or support tooling.",
                "suggestion": "Remove sensitive fields from logs or redact them before logging.",
            },
            "signature_mismatch_not_rejected": {
                "severity": "P1",
                "category": "security",
                "title": "Webhook signature mismatch is not rejected",
                "impact": "Invalidly signed webhook events can continue through the handler and be accepted.",
                "suggestion": "Return an error or raise before processing when signature verification fails.",
            },
            "trust_bypass_pattern": {
                "severity": "P2",
                "category": "correctness",
                "title": "Test-prefix shortcut can trust unintended merchants",
                "impact": "Production merchant IDs matching the test prefix can bypass normal risk checks.",
                "suggestion": "Restrict test shortcuts to non-production environments or explicit fixtures.",
            },
            "risk_rule_approves_high_risk_case": {
                "severity": "P2",
                "category": "correctness",
                "title": "High-risk country branch approves payments",
                "impact": "A risk rule can approve payments from high-risk countries instead of escalating review.",
                "suggestion": "Revisit the business rule and require manual review unless there is documented policy.",
            },
            "negative_amount_refund": {
                "severity": "P2",
                "category": "correctness",
                "title": "Refund handler returns negative amount",
                "impact": "Downstream systems may double-negate or misinterpret negative refund amounts.",
                "suggestion": "Use an explicit event type and normalized positive amount contract.",
            },
        }
        if name.startswith("missing_test_"):
            templates[name] = {
                "severity": "P2",
                "category": "testing",
                "title": "Missing test coverage for critical changed behavior",
                "impact": "Risk-sensitive behavior changed without a targeted regression test.",
                "suggestion": "Add a focused test that exercises this branch and asserts the expected outcome.",
            }
        template = templates.get(name)
        if not template or not file or not evidence:
            return None
        return Finding(
            file=file,
            line=line,
            severity=template["severity"],
            category=template["category"],
            title=template["title"],
            evidence=evidence,
            impact=template["impact"],
            suggestion=template["suggestion"],
        )

    def _ask_critic(self, action: dict[str, Any]) -> dict[str, Any]:
        finding_id = action.get("finding_id", "")
        if not finding_id and isinstance(action.get("finding"), dict):
            finding_id = self._emit_candidate(action["finding"], proposed_by="agentic-reviewer").finding_id
        item = next((candidate for candidate in self.lifecycle.all() if candidate.finding_id == finding_id), None)
        if not item:
            return {"type": "critic", "ok": False, "error": f"Unknown finding_id: {finding_id}"}
        if not self.critic_pass:
            return {"type": "critic", "ok": True, "finding_id": finding_id, "result": "critic disabled"}
        self.bus.send(
            "agentic-reviewer",
            "critic-reviewer",
            "task_assignment",
            "Challenge this candidate finding if evidence or severity is weak.",
            finding_id,
        )
        self.critic.review([item])
        return {
            "type": "critic",
            "ok": True,
            "finding_id": finding_id,
            "status": item.status,
            "evidence_chain": self.evidence.list(finding_id),
        }

    def _resolve_candidates(self, default_reason: str) -> None:
        for item in self.lifecycle.all():
            evidence_chain = self.evidence.list(item.finding_id)
            resolution = (
                self.llm_client.resolve_finding(item, evidence_chain, self.skill_context)
                if self.llm_client and self.llm_client.enabled
                else {}
            )
            if resolution:
                self.evidence.add(
                    item.finding_id,
                    "lead_resolution",
                    f"{resolution['resolution']}: {resolution['reason']}",
                    "lead-reviewer",
                )
                if resolution["resolution"] == "rejected":
                    self.lifecycle.reject(item.finding_id, resolution["reason"])
                elif resolution["resolution"] == "downgraded":
                    self.lifecycle.downgrade(item.finding_id, resolution["severity"], resolution["reason"])
                else:
                    self.lifecycle.accept(item.finding_id, resolution["reason"])
            elif item.status == "challenged":
                self.lifecycle.accept(item.finding_id, f"{default_reason} Critic challenge reviewed.")
            else:
                self.lifecycle.accept(item.finding_id, default_reason)
        for item in self.lifecycle.accepted():
            self.collector.emit(item.finding)

    def _run_local_guardrails(self) -> dict[str, Any]:
        self.transcript.emit("agent.action", step=0, action="call_tool", tool="changed_files", args={})
        files = self.tools.changed_files()
        obs = {"type": "tool", "ok": True, "tool": "changed_files", "result": files}
        self.observations.append(obs)
        self.transcript.emit("agent.observation", step=0, **obs)
        self.transcript.emit("agent.action", step=0, action="call_tool", tool="git_diff", args={})
        diff = self.tools.git_diff()
        obs = {"type": "tool", "ok": True, "tool": "git_diff", "result": truncate(diff, 12000)}
        self.observations.append(obs)
        self.transcript.emit("agent.observation", step=0, **obs)
        test_result = self.tools.run_tests(self.test_command) if self.test_command else None
        reviewers = SpecialtyReviewers(self.tools, self.collector, self.transcript)
        for reviewer in (
            "security-reviewer",
            "correctness-reviewer",
            "test-reviewer",
            "maintainability-reviewer",
            "company-policy-reviewer",
        ):
            reviewers.run(reviewer, diff, files, test_result)
        self.transcript.emit("agent.finalize", reason="LLM unavailable; used local guardrails fallback.")
        self.todos.update(
            [
                {"content": "Inspect PR context with read-only tools", "status": "completed"},
                {"content": "Run local guardrail reviewers", "status": "completed"},
                {"content": "Finalize accepted findings and write report", "status": "completed"},
            ]
        )
        self.transcript.emit("todo.update", todos=self.todos.items)
        self.transcript.emit("agentic.complete", candidates=0, accepted=len(self.collector.findings), fallback=True)
        return {"findings": [], "messages": []}


class ReviewCouncil:
    def __init__(
        self,
        tools: ReviewTools,
        transcript: Transcript,
        collector: FindingCollector,
        critic_pass: bool = True,
        llm_client: LLMClient | None = None,
        skill_context: str = "",
    ):
        self.tools = tools
        self.transcript = transcript
        self.collector = collector
        self.critic_pass = critic_pass
        self.llm_client = llm_client
        self.skill_context = skill_context
        self.bus = MessageBus(transcript)
        self.evidence = EvidenceStore(transcript)
        self.lifecycle = FindingLifecycle(transcript)
        self.members = [
            ReviewAgentMember("security-reviewer", "Security risk reviewer", tools, transcript, llm_client),
            ReviewAgentMember("correctness-reviewer", "Correctness and edge-case reviewer", tools, transcript, llm_client),
            ReviewAgentMember("test-reviewer", "Test coverage reviewer", tools, transcript, llm_client),
            ReviewAgentMember("maintainability-reviewer", "Maintainability reviewer", tools, transcript, llm_client),
            ReviewAgentMember("company-policy-reviewer", "Company policy alignment reviewer", tools, transcript, llm_client),
        ]
        self.critic = CriticReviewer(self.bus, self.evidence, self.lifecycle, llm_client, skill_context)

    def run(
        self,
        diff: str,
        files: list[dict[str, Any]],
        test_result: dict[str, Any] | None,
        pr_description: str = "",
    ) -> dict[str, Any]:
        self.transcript.emit("council.start", members=[member.name for member in self.members])
        review_plan = (
            self.llm_client.plan_review(pr_description, self.skill_context, diff, files, test_result)
            if self.llm_client
            else {}
        )
        if review_plan:
            self.transcript.emit("council.plan", assignments=review_plan)
        self.bus.send(
            "lead-reviewer",
            "all",
            "task_assignment",
            "Review the PR diff and submit candidate findings with evidence.",
        )

        for member in self.members:
            focus = review_plan.get(member.name, f"Scope: {member.role}. Submit only actionable findings.")
            self.bus.send(
                "lead-reviewer",
                member.name,
                "task_assignment",
                focus,
            )
            findings = member.review(diff, files, test_result, focus, self.skill_context)
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
                self._attach_company_policy(item.finding_id, finding)
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

    def _attach_company_policy(self, finding_id: str, finding: Finding) -> None:
        query = " ".join(
            [
                finding.title,
                finding.category,
                finding.evidence,
                finding.impact,
                finding.suggestion,
            ]
        )
        policies = self.tools.retrieve_company_policy(query, category=finding.category, max_results=3)
        for policy in policies:
            content = (
                f"{policy['policy_id']} ({policy['doc_path']}#{policy['heading']}): "
                f"{policy['excerpt']} [method={policy['retrieval_method']} score={policy['score']}]"
            )
            self.evidence.add(finding_id, "company_policy", content, "company-rag")

    def _resolve_findings(self) -> None:
        for item in self.lifecycle.all():
            evidence_count = len(self.evidence.list(item.finding_id))
            evidence_chain = self.evidence.list(item.finding_id)
            llm_resolution = (
                self.llm_client.resolve_finding(item, evidence_chain, self.skill_context)
                if self.llm_client
                else {}
            )
            if llm_resolution:
                self.evidence.add(
                    item.finding_id,
                    "lead_resolution",
                    f"{llm_resolution['resolution']}: {llm_resolution['reason']}",
                    "lead-reviewer",
                )
                resolution = llm_resolution["resolution"]
                reason = llm_resolution["reason"]
                if item.status == "challenged":
                    defense = (
                        f"Defense: {evidence_count} evidence item(s) include diff evidence and reviewer rationale."
                    )
                    self.evidence.add(item.finding_id, "reviewer_defense", defense, item.proposed_by)
                    self.bus.send(item.proposed_by, "critic-reviewer", "defense", defense, item.finding_id)
                if resolution == "rejected":
                    self.lifecycle.reject(item.finding_id, reason)
                elif resolution == "downgraded":
                    self.lifecycle.downgrade(item.finding_id, llm_resolution["severity"], reason)
                else:
                    self.lifecycle.accept(item.finding_id, reason)
                self.bus.send("lead-reviewer", item.proposed_by, "resolution", reason, item.finding_id)
            elif item.status == "challenged":
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


class DebateCouncilLoop:
    def __init__(
        self,
        tools: ReviewTools,
        transcript: Transcript,
        collector: FindingCollector,
        critic_pass: bool = True,
        llm_client: LLMClient | None = None,
        skill_context: str = "",
        max_actions: int = 12,
        language: str = "zh",
    ):
        self.tools = tools
        self.transcript = transcript
        self.collector = collector
        self.critic_pass = critic_pass
        self.llm_client = llm_client
        self.skill_context = skill_context
        self.max_actions = max_actions
        self.language = language
        self.bus = MessageBus(transcript)
        self.evidence = EvidenceStore(transcript)
        self.lifecycle = FindingLifecycle(transcript)
        self.members = [
            ReviewAgentMember("security-reviewer", "Security risk reviewer", tools, transcript, llm_client),
            ReviewAgentMember("correctness-reviewer", "Correctness and edge-case reviewer", tools, transcript, llm_client),
            ReviewAgentMember("test-reviewer", "Test coverage reviewer", tools, transcript, llm_client),
            ReviewAgentMember("maintainability-reviewer", "Maintainability reviewer", tools, transcript, llm_client),
            ReviewAgentMember("company-policy-reviewer", "Company policy alignment reviewer", tools, transcript, llm_client),
        ]
        self.critic = CriticReviewer(self.bus, self.evidence, self.lifecycle, llm_client, skill_context)
        self.observations: list[dict[str, Any]] = []
        self.report_writer_notes: dict[str, Any] = {}
        self._fallback_critic_ids: set[str] = set()

    def run(
        self,
        diff: str,
        files: list[dict[str, Any]],
        test_result: dict[str, Any] | None,
        pr_description: str = "",
    ) -> dict[str, Any]:
        if not self.llm_client or not self.llm_client.enabled:
            self.transcript.emit("debate.fallback", reason="llm unavailable")
            council = ReviewCouncil(
                self.tools,
                self.transcript,
                self.collector,
                self.critic_pass,
                self.llm_client,
                self.skill_context,
            )
            return council.run(diff, files, test_result, pr_description)

        self.transcript.emit(
            "debate.start",
            members=[member.name for member in self.members],
            max_actions=self.max_actions,
        )
        self._seed_candidates(diff, files, test_result, pr_description)
        finalized = False
        for step in range(1, self.max_actions + 1):
            action = self.llm_client.next_debate_action(
                self._debate_state(step, pr_description, files, test_result),
                self.skill_context,
            )
            if not action:
                action = self._fallback_debate_action()
            self.transcript.emit("debate.action", step=step, **action)
            observation = self._apply_debate_action(action)
            self.observations.append(observation)
            self.transcript.emit("debate.observation", step=step, **observation)
            if action["action"] == "finalize":
                finalized = True
                break
        if not finalized:
            self.transcript.emit("debate.finalize", reason="max actions reached")

        self._resolve_remaining()
        for item in self.lifecycle.accepted():
            self.collector.emit(item.finding)

        records = [item.to_dict(self.evidence.list(item.finding_id)) for item in self.lifecycle.all()]
        self.transcript.emit("debate.complete", candidates=len(records), accepted=len(self.lifecycle.accepted()))
        return {
            "findings": records,
            "messages": self.bus.all(),
            "report_writer_notes": self.report_writer_notes,
        }

    def _seed_candidates(
        self,
        diff: str,
        files: list[dict[str, Any]],
        test_result: dict[str, Any] | None,
        pr_description: str,
    ) -> None:
        review_plan = self.llm_client.plan_review(pr_description, self.skill_context, diff, files, test_result)
        if review_plan:
            self.transcript.emit("debate.plan", assignments=review_plan)
        self.bus.send(
            "lead-reviewer",
            "all",
            "task_assignment",
            "Submit candidate findings with concrete evidence. Debate will challenge weak or duplicate items.",
        )
        for member in self.members:
            focus = review_plan.get(member.name, f"Scope: {member.role}. Submit only actionable findings.")
            self.bus.send("lead-reviewer", member.name, "task_assignment", focus)
            findings = member.review(diff, files, test_result, focus, self.skill_context)
            print(f"[debate] {member.name} proposed {len(findings)} candidate finding(s)")
            for finding in findings:
                item = self.lifecycle.candidate(finding, member.name)
                self.evidence.add(item.finding_id, "reviewer_explanation", finding.impact, member.name)
                self.evidence.add(item.finding_id, "diff_line", finding.evidence, member.name)
                try:
                    context = self.tools.read_file_context(finding.file, finding.line, radius=3)
                    self.evidence.add(item.finding_id, "file_context", context, "evidence-store")
                except Exception as exc:
                    self.evidence.add(item.finding_id, "file_context_error", str(exc), "evidence-store")
                self._attach_company_policy(item.finding_id, finding)
                self.bus.send(
                    member.name,
                    "lead-reviewer",
                    "candidate_finding",
                    f"{finding.severity} {finding.category}: {finding.title}",
                    item.finding_id,
                )

    def _attach_company_policy(self, finding_id: str, finding: Finding) -> None:
        query = " ".join(
            [
                finding.title,
                finding.category,
                finding.evidence,
                finding.impact,
                finding.suggestion,
            ]
        )
        policies = self.tools.retrieve_company_policy(query, category=finding.category, max_results=3)
        for policy in policies:
            content = (
                f"{policy['policy_id']} ({policy['doc_path']}#{policy['heading']}): "
                f"{policy['excerpt']} [method={policy['retrieval_method']} score={policy['score']}]"
            )
            self.evidence.add(finding_id, "company_policy", content, "company-rag")

    def _debate_state(
        self,
        step: int,
        pr_description: str,
        files: list[dict[str, Any]],
        test_result: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "step": step,
            "max_actions": self.max_actions,
            "pr_description": pr_description[:6000],
            "changed_files": files,
            "test_result": test_result or {},
            "findings": [item.to_dict(self.evidence.list(item.finding_id)) for item in self.lifecycle.all()],
            "messages": self.bus.all()[-30:],
            "recent_observations": self.observations[-10:],
            "quality_goal": "maximize true critical coverage while minimizing duplicates, unsupported claims, and severity inflation",
        }

    def _fallback_debate_action(self) -> dict[str, Any]:
        candidate = next(
            (
                item
                for item in self.lifecycle.all()
                if item.status == "candidate" and item.finding_id not in self._fallback_critic_ids
            ),
            None,
        )
        if candidate and self.critic_pass:
            self._fallback_critic_ids.add(candidate.finding_id)
            return {
                "thought": "Fallback asks critic to verify the next unresolved candidate.",
                "action": "ask_critic",
                "finding_id": candidate.finding_id,
                "reason": "LLM debate action was unavailable.",
            }
        unresolved = next((item for item in self.lifecycle.all() if item.status in {"candidate", "challenged"}), None)
        if unresolved:
            return {
                "thought": "Fallback accepts candidate after available evidence review.",
                "action": "accept_finding",
                "finding_id": unresolved.finding_id,
                "reason": "Evidence chain is sufficient for fallback resolution.",
            }
        return {
            "thought": "No unresolved candidates remain.",
            "action": "finalize",
            "reason": "Debate has no unresolved candidates.",
        }

    def _apply_debate_action(self, action: dict[str, Any]) -> dict[str, Any]:
        kind = action["action"]
        try:
            if kind == "ask_critic":
                return self._ask_critic(action["finding_id"])
            if kind == "request_reviewer_defense":
                return self._request_reviewer_defense(action["finding_id"], action.get("reason", ""))
            if kind == "request_more_evidence":
                return self._request_more_evidence(action["finding_id"], action.get("reason", ""))
            if kind == "revise_finding":
                finding = Finding(**action["finding"])
                item = self.lifecycle.revise(action["finding_id"], finding, action.get("reason", "Revised by debate controller."))
                self.evidence.add(item.finding_id, "lead_revision", action.get("reason", ""), "lead-reviewer")
                return {"type": "revise", "ok": True, "finding_id": item.finding_id}
            if kind == "merge_duplicates":
                source_id = action["source_id"]
                target_id = action["target_id"]
                reason = action.get("reason", f"Duplicate of {target_id}.")
                self.lifecycle.reject(source_id, reason)
                self.evidence.add(source_id, "duplicate_merge", f"Merged into {target_id}: {reason}", "lead-reviewer")
                self.bus.send("lead-reviewer", "report-writer", "resolution", reason, source_id)
                return {"type": "merge", "ok": True, "source_id": source_id, "target_id": target_id}
            if kind == "accept_finding":
                item = self.lifecycle.accept(action["finding_id"], action.get("reason", "Accepted by debate controller."))
                self.bus.send("lead-reviewer", item.proposed_by, "resolution", item.resolution_reason, item.finding_id)
                return {"type": "resolution", "ok": True, "finding_id": item.finding_id, "status": item.status}
            if kind == "reject_finding":
                item = self.lifecycle.reject(action["finding_id"], action.get("reason", "Rejected by debate controller."))
                self.bus.send("lead-reviewer", item.proposed_by, "resolution", item.resolution_reason, item.finding_id)
                return {"type": "resolution", "ok": True, "finding_id": item.finding_id, "status": item.status}
            if kind == "ask_report_writer":
                context = self._report_context()
                self.report_writer_notes = self.llm_client.report_writer_review(context, self.language) if self.llm_client else {}
                self.transcript.emit("debate.report_writer", chars=len(json.dumps(self.report_writer_notes, ensure_ascii=False)))
                self.bus.send("lead-reviewer", "report-writer", "task_assignment", action.get("reason", "Review final report quality."))
                return {"type": "report_writer", "ok": True, "has_notes": bool(self.report_writer_notes)}
            if kind == "finalize":
                self.transcript.emit("debate.finalize", reason=action.get("reason", "Debate finalized."))
                return {"type": "finalize", "ok": True, "reason": action.get("reason", "")}
        except Exception as exc:
            return {"type": kind, "ok": False, "error": str(exc)}
        return {"type": kind, "ok": False, "error": "Unknown debate action"}

    def _ask_critic(self, finding_id: str) -> dict[str, Any]:
        item = self._find_item(finding_id)
        if not item:
            return {"type": "critic", "ok": False, "error": f"Unknown finding_id: {finding_id}"}
        self.bus.send(
            "lead-reviewer",
            "critic-reviewer",
            "task_assignment",
            "Challenge this candidate if evidence, severity, or duplication is weak.",
            finding_id,
        )
        self.critic.review([item])
        return {
            "type": "critic",
            "ok": True,
            "finding_id": finding_id,
            "status": item.status,
            "evidence_chain": self.evidence.list(finding_id),
        }

    def _request_reviewer_defense(self, finding_id: str, reason: str) -> dict[str, Any]:
        item = self._find_item(finding_id)
        if not item:
            return {"type": "defense", "ok": False, "error": f"Unknown finding_id: {finding_id}"}
        chain = self.evidence.list(finding_id)
        challenge = reason or item.resolution_reason or "Please defend, revise, or withdraw this finding."
        self.bus.send("lead-reviewer", item.proposed_by, "evidence_request", challenge, finding_id)
        response = self.llm_client.reviewer_defense(item.proposed_by, item, challenge, chain, self.skill_context) if self.llm_client else {}
        if not response:
            defense = f"Defense: {len(chain)} evidence item(s) support this finding."
            self.evidence.add(finding_id, "reviewer_defense", defense, item.proposed_by)
            self.bus.send(item.proposed_by, "lead-reviewer", "defense", defense, finding_id)
            return {"type": "defense", "ok": True, "finding_id": finding_id, "decision": "defend"}
        decision = response["decision"]
        self.evidence.add(finding_id, "reviewer_defense", response["reason"], item.proposed_by)
        self.bus.send(item.proposed_by, "lead-reviewer", "defense", response["reason"], finding_id)
        if decision == "withdraw":
            self.lifecycle.reject(finding_id, response["reason"])
        elif decision == "revise" and isinstance(response.get("finding"), dict):
            self.lifecycle.revise(finding_id, Finding(**response["finding"]), response["reason"])
        return {"type": "defense", "ok": True, "finding_id": finding_id, "decision": decision}

    def _request_more_evidence(self, finding_id: str, reason: str) -> dict[str, Any]:
        item = self._find_item(finding_id)
        if not item:
            return {"type": "evidence", "ok": False, "error": f"Unknown finding_id: {finding_id}"}
        try:
            context = self.tools.read_file_context(item.finding.file, item.finding.line, radius=6)
        except Exception as exc:
            self.evidence.add(finding_id, "extra_evidence_error", str(exc), "evidence-store")
            return {"type": "evidence", "ok": False, "finding_id": finding_id, "error": str(exc)}
        self.evidence.add(finding_id, "extra_file_context", context, "evidence-store")
        self.bus.send("lead-reviewer", item.proposed_by, "evidence_request", reason or "Extra evidence was collected.", finding_id)
        return {"type": "evidence", "ok": True, "finding_id": finding_id, "result": truncate(context, 1200)}

    def _resolve_remaining(self) -> None:
        for item in self.lifecycle.all():
            if item.status in {"accepted", "rejected", "downgraded"}:
                continue
            evidence_chain = self.evidence.list(item.finding_id)
            resolution = self.llm_client.resolve_finding(item, evidence_chain, self.skill_context) if self.llm_client else {}
            if resolution:
                self.evidence.add(
                    item.finding_id,
                    "lead_resolution",
                    f"{resolution['resolution']}: {resolution['reason']}",
                    "lead-reviewer",
                )
                if resolution["resolution"] == "rejected":
                    self.lifecycle.reject(item.finding_id, resolution["reason"])
                elif resolution["resolution"] == "downgraded":
                    self.lifecycle.downgrade(item.finding_id, resolution["severity"], resolution["reason"])
                else:
                    self.lifecycle.accept(item.finding_id, resolution["reason"])
            elif item.status == "challenged":
                self.lifecycle.reject(item.finding_id, item.resolution_reason or "Rejected after unresolved challenge.")
            else:
                self.lifecycle.accept(item.finding_id, "Accepted by debate fallback after evidence review.")

    def _report_context(self) -> dict[str, Any]:
        return {
            "findings": [item.to_dict(self.evidence.list(item.finding_id)) for item in self.lifecycle.all()],
            "messages": self.bus.all(),
            "observations": self.observations,
            "quality_rules": [
                "Do not reward duplicate findings.",
                "Prefer evidence-backed severe issues.",
                "Keep report style standardized for judge fairness.",
            ],
        }

    def _find_item(self, finding_id: str) -> CouncilFinding | None:
        return next((item for item in self.lifecycle.all() if item.finding_id == finding_id), None)


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


class ReportWriterAgent:
    def __init__(
        self,
        llm_client: LLMClient | None,
        transcript: Transcript,
        language: str = "zh",
    ):
        self.llm_client = llm_client
        self.transcript = transcript
        self.language = language

    def draft(self, report_context: dict[str, Any]) -> dict[str, Any]:
        payload = (
            self.llm_client.report_writer_review(report_context, self.language)
            if self.llm_client and self.llm_client.enabled
            else {}
        )
        draft = self._normalize(payload, report_context)
        self.transcript.emit(
            "report_writer.draft",
            llm_used=bool(payload),
            accepted=len(draft["accepted_findings"]),
            rejected=len(draft["rejected_findings"]),
            downgraded=len(draft["downgraded_findings"]),
        )
        return draft

    def _normalize(self, payload: dict[str, Any], report_context: dict[str, Any]) -> dict[str, Any]:
        fallback = self._fallback(report_context)
        if not isinstance(payload, dict):
            return fallback
        verdict = str(payload.get("verdict") or fallback["verdict"]).strip()
        if verdict not in VERDICTS:
            verdict = fallback["verdict"]
        accepted = self._normalize_standard_findings(payload.get("accepted_findings")) or fallback["accepted_findings"]
        rejected = self._normalize_standard_findings(payload.get("rejected_findings")) or fallback["rejected_findings"]
        downgraded = self._normalize_standard_findings(payload.get("downgraded_findings")) or fallback["downgraded_findings"]
        return {
            "report_style": "standardized",
            "verdict": verdict,
            "summary_points": self._list_of_strings(payload.get("summary_points")) or fallback["summary_points"],
            "accepted_findings": self._merge_policy_references(accepted, fallback["accepted_findings"]),
            "rejected_findings": self._merge_policy_references(rejected, fallback["rejected_findings"]),
            "downgraded_findings": self._merge_policy_references(downgraded, fallback["downgraded_findings"]),
            "duplicate_notes": self._list_of_strings(payload.get("duplicate_notes")) or fallback["duplicate_notes"],
            "critic_notes": self._list_of_strings(payload.get("critic_notes")) or fallback["critic_notes"],
        }

    def _fallback(self, report_context: dict[str, Any]) -> dict[str, Any]:
        records = report_context.get("council_records") or report_context.get("findings") or []
        accepted = [
            self._standard_finding(record, "Accepted by lead reviewer.")
            for record in records
            if record.get("status") == "accepted"
        ]
        downgraded = [
            self._standard_finding(record, "Downgraded by lead reviewer.")
            for record in records
            if record.get("status") == "downgraded"
        ]
        rejected = [
            self._standard_finding(record, "Rejected by lead reviewer.")
            for record in records
            if record.get("status") == "rejected"
        ]
        severities = {item["severity"] for item in accepted + downgraded}
        verdict = "request_changes" if severities & {"P0", "P1"} else ("comment" if severities else "approve")
        if accepted or downgraded:
            summary = [f"{len(accepted) + len(downgraded)} accepted or downgraded finding(s)."]
        else:
            summary = ["No blocking findings accepted."]
        duplicate_notes = [
            f"{record.get('finding_id')} rejected as duplicate/noise."
            for record in records
            if record.get("status") == "rejected" and "duplicate" in str(record.get("resolution_reason", "")).lower()
        ]
        critic_notes = [
            evidence.get("content", "")
            for record in records
            for evidence in record.get("evidence_chain", [])
            if evidence.get("source") in {"critic_review", "critic_challenge"}
        ][:12]
        return {
            "report_style": "standardized",
            "verdict": verdict,
            "summary_points": summary,
            "accepted_findings": accepted,
            "rejected_findings": rejected,
            "downgraded_findings": downgraded,
            "duplicate_notes": duplicate_notes,
            "critic_notes": critic_notes,
        }

    def _normalize_standard_findings(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        normalized = []
        for item in value:
            if not isinstance(item, dict):
                continue
            normalized.append(
                {
                    "issue": str(item.get("issue", "")).strip()[:300],
                    "severity": str(item.get("severity", "")).strip() if str(item.get("severity", "")).strip() in SEVERITIES else "P3",
                    "file": str(item.get("file", "")).strip(),
                    "line": int(item.get("line", 1) or 1),
                    "evidence": str(item.get("evidence", "")).strip()[:1200],
                    "impact": str(item.get("impact", "")).strip()[:1200],
                    "fix": str(item.get("fix", "")).strip()[:1200],
                    "why_accepted": str(item.get("why_accepted", "")).strip()[:1200],
                    "critic_notes": str(item.get("critic_notes", "")).strip()[:1200],
                    "policy_references": self._list_of_strings(item.get("policy_references"))[:5],
                }
            )
        return [item for item in normalized if item["issue"] and item["file"] and item["evidence"]]

    def _merge_policy_references(
        self,
        items: list[dict[str, Any]],
        fallback_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        fallback_by_location = {
            (item.get("file"), item.get("line")): item.get("policy_references", [])
            for item in fallback_items
        }
        for item in items:
            if item.get("policy_references"):
                continue
            item["policy_references"] = fallback_by_location.get((item.get("file"), item.get("line")), [])
        return items

    def _standard_finding(self, record: dict[str, Any], default_reason: str) -> dict[str, Any]:
        critic_notes = [
            evidence.get("content", "")
            for evidence in record.get("evidence_chain", [])
            if evidence.get("source") in {"critic_review", "critic_challenge"}
        ]
        policy_references = [
            evidence.get("content", "")
            for evidence in record.get("evidence_chain", [])
            if evidence.get("source") == "company_policy"
        ][:5]
        return {
            "issue": str(record.get("title", "")),
            "severity": str(record.get("severity", "P3")),
            "file": str(record.get("file", "")),
            "line": int(record.get("line", 1) or 1),
            "evidence": str(record.get("evidence", "")),
            "impact": str(record.get("impact", "")),
            "fix": str(record.get("suggestion", "")),
            "why_accepted": str(record.get("resolution_reason") or default_reason),
            "critic_notes": " ".join(critic_notes)[:1200],
            "policy_references": policy_references,
        }

    def _list_of_strings(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip()[:500] for item in value if str(item).strip()]


class CouncilReportWriter:
    def __init__(
        self,
        output_dir: Path,
        collector: FindingCollector,
        language: str = "zh",
        council_records: list[dict[str, Any]] | None = None,
        council_messages: list[dict[str, Any]] | None = None,
        standardized_report: dict[str, Any] | None = None,
    ):
        self.output_dir = output_dir
        self.collector = collector
        self.language = language
        self.council_records = council_records or []
        self.council_messages = council_messages or []
        self.standardized_report = standardized_report or {}
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
            "standard_report": self.standardized_report,
            "council": {
                "messages": self.council_messages,
                "candidates": self.council_records,
            },
        }

    def markdown(self) -> str:
        payload = self.payload()
        if self.standardized_report:
            return self._standard_markdown(payload)
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

    def _standard_markdown(self, payload: dict[str, Any]) -> str:
        report = self.standardized_report
        zh = self.language == "zh"
        title = "# PR 代码审查标准化报告" if zh else "# Standardized PR Review Report"
        labels = {
            "verdict": "结论" if zh else "Verdict",
            "summary": "摘要" if zh else "Summary",
            "accepted": "接受的问题" if zh else "Accepted Findings",
            "downgraded": "降级的问题" if zh else "Downgraded Findings",
            "rejected": "拒绝的问题" if zh else "Rejected Findings",
            "duplicates": "重复/噪音说明" if zh else "Duplicate/Noise Notes",
            "critic": "Critic 说明" if zh else "Critic Notes",
            "evidence": "证据" if zh else "Evidence",
            "impact": "影响" if zh else "Impact",
            "fix": "修复建议" if zh else "Fix",
            "why": "接受理由" if zh else "Why",
        }
        lines = [
            title,
            "",
            f"**{labels['verdict']}:** `{report.get('verdict', payload['verdict'])}`",
            "",
            f"**Report Style:** `standardized`",
            "",
            f"## {labels['summary']}",
            "",
        ]
        summary_points = report.get("summary_points") or [payload["summary"]]
        lines.extend(f"- {point}" for point in summary_points)
        lines.append("")
        for section_key, section_label in (
            ("accepted_findings", labels["accepted"]),
            ("downgraded_findings", labels["downgraded"]),
            ("rejected_findings", labels["rejected"]),
        ):
            findings = report.get(section_key) or []
            lines.extend([f"## {section_label}", ""])
            if not findings:
                lines.append("无。" if zh else "None.")
                lines.append("")
                continue
            for idx, finding in enumerate(findings, start=1):
                lines.extend(
                    [
                        f"### {idx}. [{finding.get('severity')}] {finding.get('issue')}",
                        "",
                        f"- File: `{finding.get('file')}:{finding.get('line')}`",
                        f"- {labels['evidence']}: {finding.get('evidence')}",
                        f"- {labels['impact']}: {finding.get('impact')}",
                        f"- {labels['fix']}: {finding.get('fix')}",
                        f"- {labels['why']}: {finding.get('why_accepted')}",
                        f"- {labels['critic']}: {finding.get('critic_notes') or 'none'}",
                    ]
                )
                policy_references = finding.get("policy_references") or []
                if policy_references:
                    policy_label = "\u516c\u53f8\u89c4\u8303\u4f9d\u636e" if zh else "Company policy"
                    lines.append(f"- {policy_label}:")
                    lines.extend(f"  - {reference}" for reference in policy_references)
                lines.append("")
        for key, label in (("duplicate_notes", labels["duplicates"]), ("critic_notes", labels["critic"])):
            notes = report.get(key) or []
            lines.extend([f"## {label}", ""])
            if notes:
                lines.extend(f"- {note}" for note in notes)
            else:
                lines.append("无。" if zh else "None.")
            lines.append("")
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
        judge_input_path = self.output_dir / JUDGE_INPUT_NAME
        report_path.write_text(self.markdown(), encoding="utf-8")
        payload = self.payload()
        findings_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        judge_input = {
            "report_style": "standardized" if self.standardized_report else "legacy",
            "standard_report": self.standardized_report,
            "verdict": payload["verdict"],
            "summary": payload["summary"],
            "findings": payload["findings"],
            "council": payload["council"],
        }
        judge_input_path.write_text(json.dumps(judge_input, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"report": str(report_path), "findings": str(findings_path), "judge_input": str(judge_input_path)}


ReportWriter = CouncilReportWriter


class JudgeRunner:
    def __init__(
        self,
        repo: Path,
        output_dir: Path,
        transcript: Transcript,
        llm_client: LLMClient | None,
    ):
        self.repo = repo
        self.output_dir = output_dir
        self.transcript = transcript
        self.llm_client = llm_client
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def run(
        self,
        judge_report: Path,
        base: str,
        target: str,
        pr_description: Path | None = None,
    ) -> dict[str, str]:
        path = judge_report if judge_report.is_absolute() else safe_repo_path(self.repo, str(judge_report))
        judge_input = json.loads(path.read_text(encoding="utf-8"))
        diff = self._git_diff(base, target)
        description = ""
        if pr_description:
            description_path = pr_description if pr_description.is_absolute() else safe_repo_path(self.repo, str(pr_description))
            description = description_path.read_text(encoding="utf-8", errors="replace")
        raw = self.llm_client.judge_report(judge_input, diff, description) if self.llm_client and self.llm_client.enabled else {}
        result = self._normalize(raw)
        judge_path = self.output_dir / JUDGE_NAME
        markdown_path = self.output_dir / JUDGE_REPORT_NAME
        judge_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        markdown_path.write_text(self._markdown(result), encoding="utf-8")
        self.transcript.emit("judge.complete", score=result["overall_score"], verdict=result["verdict"])
        return {"judge": str(judge_path), "judge_report": str(markdown_path)}

    def _git_diff(self, base: str, target: str) -> str:
        try:
            return run_git(self.repo, ["diff", "--no-ext-diff", "--unified=80", base, target], timeout=60)
        except Exception as exc:
            self.transcript.emit("judge.diff_error", error=str(exc))
            return ""

    def _normalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        dimensions = {
            "critical_issue_coverage": 0,
            "evidence_quality": 0,
            "severity_accuracy": 0,
            "duplicate_noise_control": 0,
            "actionability": 0,
            "report_clarity": 0,
        }
        if isinstance(payload.get("dimensions"), dict):
            for key in dimensions:
                try:
                    dimensions[key] = max(0, min(int(payload["dimensions"].get(key, 0)), 100))
                except (TypeError, ValueError):
                    dimensions[key] = 0
        try:
            overall = int(payload.get("overall_score", 0))
        except (TypeError, ValueError):
            overall = 0
        overall = max(0, min(overall, 100))
        verdict = str(payload.get("verdict", "needs_improvement")).strip()
        if verdict not in {"pass", "needs_improvement", "fail"}:
            verdict = "pass" if overall >= 80 else "needs_improvement"
        return {
            "overall_score": overall,
            "verdict": verdict,
            "dimensions": dimensions,
            "strengths": self._list_of_strings(payload.get("strengths")),
            "weaknesses": self._list_of_strings(payload.get("weaknesses")),
            "recommendations": self._list_of_strings(payload.get("recommendations")),
        }

    def _list_of_strings(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip()[:500] for item in value if str(item).strip()]

    def _markdown(self, result: dict[str, Any]) -> str:
        lines = [
            "# AI Judge Review Quality Report",
            "",
            f"**Overall Score:** {result['overall_score']}",
            f"**Verdict:** `{result['verdict']}`",
            "",
            "## Dimensions",
            "",
        ]
        for key, score in result["dimensions"].items():
            lines.append(f"- `{key}`: {score}")
        for key, title in (
            ("strengths", "Strengths"),
            ("weaknesses", "Weaknesses"),
            ("recommendations", "Recommendations"),
        ):
            lines.extend(["", f"## {title}", ""])
            items = result.get(key) or []
            lines.extend(f"- {item}" for item in items) if items else lines.append("- None.")
        return "\n".join(lines).rstrip() + "\n"


class ReviewAgent:
    def __init__(
        self,
        repo: Path,
        base: str,
        target: str,
        pr_description: Path | None = None,
        test_command: str | None = None,
        language: str = "zh",
        mode: str = "debate",
        critic_pass: bool = True,
        llm_provider: str = "aliyun",
        llm_model: str = "qwen-turbo-latest",
        llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        debate_max_actions: int = 12,
        company_knowledge_dir: Path | None = None,
        embedding_model: str = "text-embedding-v4",
        embedding_dimensions: int = 1024,
        disable_company_rag: bool = False,
    ):
        self.repo = repo.resolve()
        self.base = base
        self.target = target
        self.pr_description = pr_description
        self.test_command = test_command
        self.language = language
        self.mode = mode
        self.critic_pass = critic_pass
        self.llm_provider = llm_provider
        self.llm_model = llm_model
        self.llm_base_url = llm_base_url
        self.debate_max_actions = debate_max_actions
        self.company_knowledge_dir = company_knowledge_dir or (self.repo / "knowledge" / "company")
        self.embedding_model = embedding_model
        self.embedding_dimensions = embedding_dimensions
        self.disable_company_rag = disable_company_rag
        self.output_dir = self.repo / OUTPUT_DIR_NAME
        self.transcript = Transcript(self.output_dir / TRANSCRIPT_NAME)
        self.collector = FindingCollector()
        self.todos = TodoManager()
        self.skills = SkillLoader(REPO_ROOT / "skills")
        self.skill_context = ""
        self.loaded_env_keys = load_env_file(self.repo / ".env")
        if self.loaded_env_keys:
            self.transcript.emit("env.load", path=".env", keys=self.loaded_env_keys)
        self.company_kb = self._build_company_kb()
        self.tools = ReviewTools(self.repo, self.base, self.target, self.transcript, self.company_kb)
        self.llm_client = self._build_llm_client()
        self.reviewers = SpecialtyReviewers(self.tools, self.collector, self.transcript)
        self.council_result: dict[str, Any] = {"findings": [], "messages": []}
        self.standardized_report: dict[str, Any] = {}
        self.tool_handlers: dict[str, Callable[..., Any]] = {
            "git_diff": self.tools.git_diff,
            "changed_files": self.tools.changed_files,
            "read_file_context": self.tools.read_file_context,
            "run_tests": self.tools.run_tests,
            "secret_scan": self.tools.secret_scan,
            "search_code": self.tools.search_code,
            "risk_scan": self.tools.risk_scan,
            "retrieve_company_policy": self.tools.retrieve_company_policy,
            "emit_finding": self.collector.emit,
            "write_report": lambda path=None: ReportWriter(
                self.output_dir,
                self.collector,
                self.language,
                self.council_result.get("findings", []),
                self.council_result.get("messages", []),
                self.standardized_report,
            ).write(),
        }

    def _build_company_kb(self) -> CompanyKnowledgeBase | None:
        if self.disable_company_rag:
            self.transcript.emit("company_rag.disabled", reason="--disable-company-rag")
            return None
        return CompanyKnowledgeBase(
            knowledge_dir=self.company_knowledge_dir,
            index_path=self.output_dir / COMPANY_KNOWLEDGE_INDEX_NAME,
            transcript=self.transcript,
            api_key=os.getenv("DASHSCOPE_API_KEY"),
            base_url=self.llm_base_url,
            embedding_model=self.embedding_model,
            embedding_dimensions=self.embedding_dimensions,
            enabled=True,
        )

    def _build_llm_client(self) -> LLMClient | None:
        if self.llm_provider == "none":
            self.transcript.emit("llm.disabled", provider="none")
            return None
        if self.llm_provider == "aliyun":
            client = AliyunDashScopeClient(
                api_key=os.getenv("DASHSCOPE_API_KEY"),
                model=self.llm_model,
                base_url=self.llm_base_url,
                transcript=self.transcript,
            )
            self.transcript.emit(
                "llm.configure",
                provider=client.provider,
                model=client.model,
                base_url=client.base_url,
                enabled=client.enabled,
            )
            return client
        raise ValueError(f"Unsupported LLM provider: {self.llm_provider}")

    def _description_text(self) -> str:
        if not self.pr_description:
            return ""
        path = self.pr_description
        if not path.is_absolute():
            path = safe_repo_path(self.repo, str(path))
        return path.read_text(encoding="utf-8", errors="replace")

    def _augment_skill_context(self, skill: str, query: str) -> str:
        if not self.company_kb:
            return skill
        block = self.company_kb.context_block(query, max_results=5)
        if not block:
            return skill
        return f"{skill}\n\n{block}"

    def run(self) -> dict[str, Any]:
        self.transcript.emit(
            "review.start",
            repo=str(self.repo),
            base=self.base,
            target=self.target,
            mode=self.mode,
            llm_provider=self.llm_provider,
            llm_model=self.llm_model,
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
        self.skill_context = skill
        pr_description = self._description_text()
        self.transcript.emit("skill.load", name="code-review", chars=len(skill))
        if pr_description:
            self.transcript.emit("pr.description", chars=len(pr_description))

        if self.mode == "agentic":
            self.skill_context = self._augment_skill_context(skill, pr_description)
            loop = AgenticReviewLoop(
                tools=self.tools,
                transcript=self.transcript,
                collector=self.collector,
                todos=self.todos,
                critic_pass=self.critic_pass,
                llm_client=self.llm_client,
                skill_context=self.skill_context,
                test_command=self.test_command,
            )
            print("[agent] starting agentic review loop")
            self.council_result = loop.run(pr_description)
            print(f"[agent] agentic loop accepted {len(self.collector.findings)} finding(s)")
        else:
            files = self.tools.changed_files()
            diff = self.tools.git_diff()
            test_result = self.tools.run_tests(self.test_command) if self.test_command else None
            self.skill_context = self._augment_skill_context(
                skill,
                "\n".join([pr_description, json.dumps(files, ensure_ascii=False), truncate(diff, 12000)]),
            )

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
                for reviewer in ("security-reviewer", "correctness-reviewer", "test-reviewer", "company-policy-reviewer"):
                    print(f"[agent] spawning {reviewer}")
                    print("[agent] " + self.reviewers.run(reviewer, diff, files, test_result))
            elif self.mode == "debate":
                debate = DebateCouncilLoop(
                    tools=self.tools,
                    transcript=self.transcript,
                    collector=self.collector,
                    critic_pass=self.critic_pass,
                    llm_client=self.llm_client,
                    skill_context=self.skill_context,
                    max_actions=self.debate_max_actions,
                    language=self.language,
                )
                print("[agent] starting debate council loop")
                self.council_result = debate.run(diff, files, test_result, pr_description)
                print(
                    f"[agent] debate council accepted {len(self.collector.findings)} finding(s) "
                    f"from {len(self.council_result['findings'])} candidate(s)"
                )
            else:
                council = ReviewCouncil(
                    tools=self.tools,
                    transcript=self.transcript,
                    collector=self.collector,
                    critic_pass=self.critic_pass,
                    llm_client=self.llm_client,
                    skill_context=self.skill_context,
                )
                print("[agent] convening review council")
                self.council_result = council.run(diff, files, test_result, pr_description)
                print(
                    f"[agent] council accepted {len(self.collector.findings)} finding(s) "
                    f"from {len(self.council_result['findings'])} candidate(s)"
                )

        if self.mode == "agentic":
            final_todos = self.todos.items or [
                {"content": "Run agentic review loop", "status": "completed"},
                {"content": "Write Markdown and JSON report", "status": "in_progress"},
            ]
            if final_todos:
                final_todos = [
                    {
                        "content": item["content"],
                        "status": "completed" if item["status"] == "in_progress" else item["status"],
                    }
                    for item in final_todos
                ]
                if not any(item["content"] == "Write Markdown and JSON report" for item in final_todos):
                    final_todos.append({"content": "Write Markdown and JSON report", "status": "in_progress"})
                else:
                    final_todos = [
                        {
                            "content": item["content"],
                            "status": "in_progress" if item["content"] == "Write Markdown and JSON report" else item["status"],
                        }
                        for item in final_todos
                    ]
                self.todos.update(final_todos)
                self.transcript.emit("todo.update", todos=self.todos.items)
        else:
            self.todos.update(
                [
                    {"content": "Load code-review skill and understand PR context", "status": "completed"},
                    {"content": "Inspect changed files and diff", "status": "completed"},
                    {"content": "Run specialist review agents", "status": "completed"},
                    {"content": "Write Markdown and JSON report", "status": "in_progress"},
                ]
            )
            self.transcript.emit("todo.update", todos=self.todos.items)

        report_context = {
            "mode": self.mode,
            "council_records": self.council_result.get("findings", []),
            "council_messages": self.council_result.get("messages", []),
            "report_writer_notes": self.council_result.get("report_writer_notes", {}),
        }
        self.standardized_report = ReportWriterAgent(
            self.llm_client,
            self.transcript,
            self.language,
        ).draft(report_context)

        writer = ReportWriter(
            self.output_dir,
            self.collector,
            self.language,
            self.council_result.get("findings", []),
            self.council_result.get("messages", []),
            self.standardized_report,
        )
        paths = writer.write()
        payload = writer.payload()
        self.transcript.emit("review.complete", verdict=payload["verdict"], findings=len(payload["findings"]))

        if self.mode == "agentic":
            completed = [
                {"content": item["content"], "status": "completed"}
                for item in self.todos.items
                if item.get("content") != "Write Markdown and JSON report"
            ]
            completed.append({"content": "Write Markdown and JSON report", "status": "completed"})
            self.todos.update(completed)
        else:
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
    parser.add_argument("--mode", choices=["debate", "council", "agentic", "simple"], default="debate", help="Review execution mode.")
    parser.add_argument("--llm-provider", choices=["aliyun", "none"], default="aliyun", help="LLM provider for specialist reviewers.")
    parser.add_argument("--llm-model", default="qwen-turbo-latest", help="Aliyun DashScope model name, e.g. qwen-turbo-latest.")
    parser.add_argument("--llm-base-url", default="https://dashscope.aliyuncs.com/compatible-mode/v1", help="OpenAI-compatible DashScope base URL.")
    parser.add_argument("--company-knowledge-dir", default="knowledge/company", help="Directory of company policy Markdown files for RAG alignment.")
    parser.add_argument("--embedding-model", default="text-embedding-v4", help="DashScope embedding model for company policy RAG.")
    parser.add_argument("--embedding-dimensions", type=int, default=1024, help="Embedding dimensions for company policy RAG.")
    parser.add_argument("--disable-company-rag", action="store_true", help="Disable company policy RAG alignment.")
    parser.add_argument("--debate-max-actions", type=int, default=12, help="Maximum dynamic actions for debate council mode.")
    parser.add_argument("--judge-report", help="Run AI judge on a standardized judge_input.json instead of running review.")
    parser.add_argument("--judge-model", default="qwen-plus", help="Aliyun DashScope model for AI judge.")
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
    load_env_file(repo / ".env")
    if args.judge_report:
        output_dir = repo / OUTPUT_DIR_NAME
        transcript = Transcript(output_dir / JUDGE_TRANSCRIPT_NAME)
        llm_client: LLMClient | None
        if args.llm_provider == "none":
            llm_client = None
            transcript.emit("llm.disabled", provider="none")
        else:
            llm_client = AliyunDashScopeClient(
                api_key=os.getenv("DASHSCOPE_API_KEY"),
                model=args.judge_model,
                base_url=args.llm_base_url,
                transcript=transcript,
            )
            transcript.emit(
                "llm.configure",
                provider=llm_client.provider,
                model=llm_client.model,
                base_url=llm_client.base_url,
                enabled=llm_client.enabled,
            )
        paths = JudgeRunner(repo, output_dir, transcript, llm_client).run(
            Path(args.judge_report),
            args.base,
            args.target,
            pr_description,
        )
        print(f"[judge] output: {paths['judge']}")
        print(f"[judge] report: {paths['judge_report']}")
        return 0
    agent = ReviewAgent(
        repo=repo,
        base=args.base,
        target=args.target,
        pr_description=pr_description,
        test_command=args.test_command,
        language=args.language,
        mode=args.mode,
        critic_pass=args.critic_pass == "true",
        llm_provider=args.llm_provider,
        llm_model=args.llm_model,
        llm_base_url=args.llm_base_url,
        debate_max_actions=args.debate_max_actions,
        company_knowledge_dir=repo / args.company_knowledge_dir,
        embedding_model=args.embedding_model,
        embedding_dimensions=args.embedding_dimensions,
        disable_company_rag=args.disable_company_rag,
    )
    result = agent.run()
    print(f"[agent] verdict: {result['verdict']}")
    print(f"[agent] summary: {result['summary']}")
    print(f"[agent] report: {result['paths']['report']}")
    print(f"[agent] findings: {result['paths']['findings']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
