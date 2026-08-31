"""Serve the Fleet Console SPA against V2 Memory Bank / offline JSONL."""

from __future__ import annotations

import json
import mimetypes
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
CONSOLE_DIR = REPO_ROOT / "static" / "console"
HISTORY_PATH = REPO_ROOT / "data" / "overseer.json"
MEMORY_JSONL = REPO_ROOT / "logs" / "offline_firestore" / "bounty_memory.jsonl"


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


def dispatch(path: str) -> Tuple[int, str, bytes]:
    parsed = urlparse(path)
    route = parsed.path or "/"

    if route in {"/health", "/healthz"}:
        return 200, "application/json", json.dumps({"status": "healthy", "service": "universal-bounty-v2"}).encode()

    if route == "/api/registry":
        return 200, "application/json", json.dumps(registry_payload()).encode()

    if route == "/api/bounties/latest":
        doc = latest_memory_doc()
        bounty = normalize_bounty(doc) if doc else None
        return 200, "application/json", json.dumps({"bounty": bounty}).encode()

    if route == "/api/history":
        return 200, "application/json", json.dumps(overseer_payload()).encode()

    if route.startswith("/console"):
        rel = route[len("/console") :].lstrip("/")
        if rel.startswith("assets/"):
            asset = (CONSOLE_DIR / rel).resolve()
            if CONSOLE_DIR.resolve() not in asset.parents and asset != CONSOLE_DIR.resolve():
                return 404, "text/plain", b"not found"
            if asset.is_file():
                mime = mimetypes.guess_type(str(asset))[0] or "application/octet-stream"
                return 200, mime, asset.read_bytes()
            return 404, "text/plain", b"not found"
        index = CONSOLE_DIR / "index.html"
        if index.exists():
            return 200, "text/html; charset=utf-8", index.read_bytes()
        hint = (
            "<!doctype html><title>Fleet Console</title>"
            "<p>Build the console first: <code>cd console-ui && npm install && npm run build</code></p>"
        )
        return 503, "text/html; charset=utf-8", hint.encode()

    return 404, "text/plain", b"not found"


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
