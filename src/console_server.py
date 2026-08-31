"""Serve the Fleet Console SPA against V2 Memory Bank / offline JSONL."""

from __future__ import annotations

import json
import mimetypes
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from src.core.firestore_client import get_firestore_client

REPO_ROOT = Path(__file__).resolve().parents[1]
CONSOLE_DIR = REPO_ROOT / "static" / "console"
HISTORY_PATH = REPO_ROOT / "data" / "overseer.json"
MEMORY_JSONL = REPO_ROOT / "logs" / "offline_firestore" / "bounty_memory.jsonl"

CANONICAL_STAGES = ["queued", "pending_triage", "pr_open", "completed", "failed"]

STAGE_MAPPING: Dict[str, str] = {
    # queued: 'queued', 'pending_discovery', 'intake'
    "queued": "queued",
    "pending_discovery": "queued",
    "intake": "queued",
    # pending_triage: 'pending_triage', 'priority_triage', 'triaged'
    "pending_triage": "pending_triage",
    "priority_triage": "pending_triage",
    "triaged": "pending_triage",
    # pr_open: 'pr_open', 'claimed', 'running_orbstack', 'in_progress', 'draft_pr'
    "pr_open": "pr_open",
    "claimed": "pr_open",
    "running_orbstack": "pr_open",
    "in_progress": "pr_open",
    "draft_pr": "pr_open",
    # completed: 'completed', 'merged', 'paid'
    "completed": "completed",
    "merged": "completed",
    "paid": "completed",
    # failed: 'failed', 'failed_verification', 'abandoned', 'rejected'
    "failed": "failed",
    "failed_verification": "failed",
    "abandoned": "failed",
    "rejected": "failed",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def overseer_payload() -> Dict[str, Any]:
    if not HISTORY_PATH.exists():
        return {
            "meta": {"rule": "Overseer history is not packaged."},
            "sprint": {"opened": 0, "waiting": 0, "merged": 0, "closed": 0, "days": [], "repos": [], "prs": []},
            "claims": [],
            "archive": [],
        }
    return json.loads(HISTORY_PATH.read_text())


def latest_memory_doc() -> Optional[Dict[str, Any]]:
    if not MEMORY_JSONL.exists():
        return None
    last: Optional[Dict[str, Any]] = None
    for line in MEMORY_JSONL.read_text().splitlines():
        if not line.strip():
            continue
        last = json.loads(line)
    return last


def normalize_bounty(doc: Dict[str, Any]) -> Dict[str, Any]:
    bounty_id = doc.get("_id") or f"{doc.get('repo', 'unknown')}#{doc.get('issue_number') or doc.get('pr_number')}"
    status = (doc.get("audit_status") or "PENDING").upper()
    repo = doc.get("repo")
    issue_number = doc.get("issue_number")
    issue_url = None
    if repo and issue_number:
        issue_url = f"https://github.com/{repo}/issues/{issue_number}"
    return {
        "bounty_id": bounty_id,
        "title": doc.get("title"),
        "repo": repo,
        "issue_number": issue_number,
        "pr_number": doc.get("pr_number"),
        "issue_url": issue_url,
        "pr_url": doc.get("pr_url"),
        "audit_status": status,
        "merge_allowed": status == "PASS",
        "cheat_detected": doc.get("cheat_detected"),
        "source": "live",
        "escrow": {
            "verified": bool(doc.get("payout_usd")),
            "amount_usd": float(doc.get("payout_usd") or 0),
            "source": doc.get("payout") or doc.get("strategy"),
        },
        "events": doc.get("events") or [],
        "agents": doc.get("agents")
        or {"intake": "idle", "executor": "idle", "auditor": "idle"},
        "gcp": {
            "project": "odin-500008",
            "region": "us-central1",
            "firestore_doc": f"bounty_memory/{bounty_id}",
        },
        "updated_at": doc.get("updated_at") or _now(),
    }


def normalize_status(raw: Optional[str]) -> str:
    """Standardize raw status into one of 5 canonical stages."""
    if not raw:
        return "queued"
    clean = str(raw).strip().lower()
    return STAGE_MAPPING.get(clean, "queued")


def _format_lead(doc: Any) -> Dict[str, Any]:
    """Format a Firestore doc snapshot or dict into a standardized lead item."""
    if hasattr(doc, "to_dict"):
        d = doc.to_dict() or {}
        doc_id = str(doc.id)
    elif isinstance(doc, dict):
        d = doc
        doc_id = str(doc.get("id") or doc.get("_id") or "unknown")
    else:
        d = {}
        doc_id = "unknown"

    raw_status = str(d.get("status") or "queued")
    status = normalize_status(raw_status)

    repo_val = d.get("repo") or d.get("repository") or "unknown"
    if isinstance(repo_val, dict):
        repo = str(repo_val.get("nameWithOwner") or repo_val.get("name") or "unknown")
    else:
        repo = str(repo_val)

    issue_num_raw = d.get("issue_number")
    if issue_num_raw is None:
        issue_num_raw = d.get("number")
    if issue_num_raw is not None:
        try:
            issue_number: Optional[int] = int(issue_num_raw)
        except (ValueError, TypeError):
            issue_number = None
    else:
        issue_number = None

    pr_num_raw = d.get("pr_number")
    if pr_num_raw is not None:
        try:
            pr_number: Optional[int] = int(pr_num_raw)
        except (ValueError, TypeError):
            pr_number = None
    else:
        pr_number = None

    # Projected payout USD parsing
    payout_usd_raw = d.get("projected_payout_usd")
    if payout_usd_raw is None:
        payout_usd_raw = d.get("payout_usd")
    try:
        projected_payout_usd = float(payout_usd_raw or 0.0)
    except (ValueError, TypeError):
        projected_payout_usd = 0.0

    # Projected payout formatted string
    projected_payout = str(d.get("projected_payout") or "")
    if not projected_payout:
        if projected_payout_usd > 0:
            projected_payout = f"${projected_payout_usd:g}"
        else:
            projected_payout = "PENDING DISCOVERY"

    # Escrow verification
    if d.get("escrow_verified") is not None:
        escrow_verified = bool(d.get("escrow_verified"))
    else:
        escrow_verified = bool(projected_payout_usd > 0)

    # Issue and PR URLs
    issue_url = d.get("issue_url") or d.get("url")
    if not issue_url and repo != "unknown" and issue_number is not None:
        issue_url = f"https://github.com/{repo}/issues/{issue_number}"
    elif not issue_url:
        issue_url = ""

    pr_url = d.get("pr_url")
    if not pr_url and repo != "unknown" and pr_number is not None:
        pr_url = f"https://github.com/{repo}/pull/{pr_number}"
    elif not pr_url:
        pr_url = ""

    return {
        "id": doc_id,
        "repo": repo,
        "issue_number": issue_number,
        "title": str(d.get("title") or ""),
        "status": status,
        "raw_status": raw_status,
        "projected_payout": projected_payout,
        "projected_payout_usd": projected_payout_usd,
        "qualification_reason": str(d.get("qualification_reason") or ""),
        "ecosystem": str(d.get("ecosystem") or "unknown"),
        "escrow_verified": escrow_verified,
        "issue_url": str(issue_url),
        "pr_url": str(pr_url),
    }


def empty_pipeline_payload() -> Dict[str, Any]:
    return {
        "leads": [],
        "grouped": {
            "queued": [],
            "pending_triage": [],
            "pr_open": [],
            "completed": [],
            "failed": [],
        },
        "counts": {
            "queued": 0,
            "pending_triage": 0,
            "pr_open": 0,
            "completed": 0,
            "failed": 0,
            "total": 0,
        },
    }


def pipeline_payload(db: Optional[Any] = None) -> Dict[str, Any]:
    try:
        client = db if db is not None else get_firestore_client()
        docs = client.collection("bounty_leads").stream()
        leads: List[Dict[str, Any]] = []
        grouped: Dict[str, List[Dict[str, Any]]] = {
            "queued": [],
            "pending_triage": [],
            "pr_open": [],
            "completed": [],
            "failed": [],
        }

        for doc in docs:
            item = _format_lead(doc)
            leads.append(item)
            st = item["status"]
            if st in grouped:
                grouped[st].append(item)
            else:
                grouped["queued"].append(item)

        counts = {
            "queued": len(grouped["queued"]),
            "pending_triage": len(grouped["pending_triage"]),
            "pr_open": len(grouped["pr_open"]),
            "completed": len(grouped["completed"]),
            "failed": len(grouped["failed"]),
            "total": len(leads),
        }

        return {
            "leads": leads,
            "grouped": grouped,
            "counts": counts,
        }
    except Exception as e:
        print(f"[ConsoleServer] Error fetching pipeline: {e}")
        return empty_pipeline_payload()


def registry_payload() -> Dict[str, Any]:
    return {
        "track": "Fortified Enterprise Fleet",
        "policy": {"god_token": False},
        "agents": [
            {
                "id": "intake",
                "name": "Intake Taskmaster",
                "status": "idle",
                "tool_scope": ["issues:read", "issues:comment"],
            },
            {
                "id": "executor",
                "name": "Execution Engineer",
                "status": "idle",
                "tool_scope": ["contents:write", "pull_requests:write"],
            },
            {
                "id": "auditor",
                "name": "Victory Auditor",
                "status": "idle",
                "tool_scope": ["pull_requests:review"],
            },
        ],
    }


def dispatch(path: str, db: Optional[Any] = None) -> Tuple[int, str, bytes]:
    parsed = urlparse(path)
    route = parsed.path or "/"

    if route in {"/health", "/healthz"}:
        return 200, "application/json; charset=utf-8", json.dumps({"status": "healthy", "service": "universal-bounty-v2"}).encode()

    if route == "/api/pipeline":
        return 200, "application/json; charset=utf-8", json.dumps(pipeline_payload(db=db)).encode()

    if route == "/api/registry":
        return 200, "application/json; charset=utf-8", json.dumps(registry_payload()).encode()

    if route == "/api/bounties/latest":
        doc = latest_memory_doc()
        bounty = normalize_bounty(doc) if doc else None
        return 200, "application/json; charset=utf-8", json.dumps({"bounty": bounty}).encode()

    if route == "/api/history":
        return 200, "application/json; charset=utf-8", json.dumps(overseer_payload()).encode()

    if route == "/console" or route.startswith("/console/"):
        rel = route[len("/console") :].lstrip("/")
        base_dir = CONSOLE_DIR.resolve()

        if rel:
            try:
                target = (CONSOLE_DIR / rel).resolve()
            except Exception:
                return 404, "text/plain; charset=utf-8", b"not found"

            # Path traversal security check
            try:
                target.relative_to(base_dir)
            except ValueError:
                return 404, "text/plain; charset=utf-8", b"not found"

            if target.is_file():
                mime, _ = mimetypes.guess_type(str(target))
                if not mime:
                    mime = "application/octet-stream"
                elif mime.startswith("text/") or mime in ("application/javascript", "application/json"):
                    if "charset" not in mime:
                        mime = f"{mime}; charset=utf-8"
                return 200, mime, target.read_bytes()

        # SPA fallback
        if not rel or rel == "/":
            pipeline = CONSOLE_DIR / "pipeline.html"
            if pipeline.exists():
                return 200, "text/html; charset=utf-8", pipeline.read_bytes()
        
        index = CONSOLE_DIR / "index.html"
        if index.exists():
            return 200, "text/html; charset=utf-8", index.read_bytes()

        hint = (
            "<!doctype html><title>Fleet Console</title>"
            "<p>Build the console first: <code>cd console-ui && npm install && npm run build</code></p>"
        )
        return 503, "text/html; charset=utf-8", hint.encode()

    return 404, "text/plain; charset=utf-8", b"not found"


class ConsoleHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        status, content_type, body = dispatch(self.path)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def serve_console(host: str = "127.0.0.1", port: int = 8080) -> None:
    server = ThreadingHTTPServer((host, port), ConsoleHandler)
    server.serve_forever()


if __name__ == "__main__":
    print("Starting Console Server at http://127.0.0.1:8000")
    serve_console("127.0.0.1", 8000)
