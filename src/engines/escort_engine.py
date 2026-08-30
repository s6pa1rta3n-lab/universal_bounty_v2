"""
Escort Engine — Autonomous PR Lifecycle Monitor & CI Escort.

Monitors active PRs, CI rollup statuses, check suites, reviews, and inactivity
across Firestore `bounty_memory` and GitHub GraphQL. Filters out maintainer-required
external preview deployment gates (Vercel, Netlify, Cloudflare Pages), detects
actionable CI failures, identifies 14-day inactivity staleness to trigger bumps,
and maintains real-time PR health state in Firestore.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from src.core.config import COLLECTION_BOUNTY_MEMORY
from src.core.firestore_client import get_firestore_client
from src.core.github_client import GitHubClient, get_github_client

logger = logging.getLogger("UniversalBountyV2.EscortEngine")

# Known bots and external preview deployment gates that should not trigger false CI failure flags
IGNORE_BOTS: set[str] = {
    "s6pa1rta3n-lab",
    "github-advanced-security",
    "coderabbitai",
    "gitar-bot",
    "vercel",
    "github-actions",
    "codecov",
    "coveralls",
    "dependabot",
    "stale",
    "github-actions[bot]",
    "vercel[bot]",
    "coderabbitai[bot]",
    "codecov[bot]",
    "dependabot[bot]",
}

EXTERNAL_DEPLOYMENT_GATES: list[str] = [
    "vercel",
    "netlify",
    "cloudflare-pages",
    "cloudflare pages",
    "preview-deployment",
    "deploy/preview",
    "deployment gate",
    "external-deployment",
    "preview / deploy",
]


def parse_iso_datetime(
    dt_val: str | datetime | float | None
) -> datetime | None:
    """Safely parses various datetime representations into UTC datetime."""
    if dt_val is None:
        return None
    if isinstance(dt_val, datetime):
        return dt_val if dt_val.tzinfo else dt_val.replace(tzinfo=timezone.utc)
    if isinstance(dt_val, (int, float)):
        return datetime.fromtimestamp(dt_val, tz=timezone.utc)
    if isinstance(dt_val, str):
        try:
            return datetime.fromisoformat(dt_val.replace("Z", "+00:00"))
        except Exception:
            return None
    return None


def is_external_deployment_gate(
    check_name: str,
    app_name: str | None = None,
) -> bool:
    """
    Checks if a CI check run or check suite represents an external preview deployment gate
    (e.g., Vercel, Netlify, Cloudflare Pages) which requires maintainer authorization for forks.
    """
    target = f"{check_name} {app_name or ''}".lower()
    for gate in EXTERNAL_DEPLOYMENT_GATES:
        if gate in target:
            return True
    return False


class EscortEngine:
    """
    Modular PR Escort & CI Monitor Engine for Milestone 4.
    Tracks open PR health, identifies real CI failures, and manages 14-day inactivity flags.
    """

    def __init__(
        self,
        db: Any | None = None,
        github_client: GitHubClient | None = None,
        memory_collection: str = COLLECTION_BOUNTY_MEMORY,
        stale_days_threshold: int = 14,
    ):
        self.db = db if db is not None else get_firestore_client()
        self.github_client = github_client or get_github_client()
        self.memory_collection = memory_collection
        self.stale_days_threshold = stale_days_threshold

    def inspect_pr_health(self, pr_data: dict[str, Any]) -> dict[str, Any]:
        """
        Inspects PR attributes (CI status, review state, staleness) and returns an evaluation.
        Filters out external preview deployment gates from failing status.
        """
        now = datetime.now(timezone.utc)

        # 1. CI Status Evaluation
        ci_status = "UNKNOWN"
        raw_ci_failures: list[str] = []
        filtered_ci_failures: list[str] = []
        ignored_gate_failures: list[str] = []

        if pr_data.get("commits"):
            commits_nodes = (
                pr_data["commits"].get("nodes", [])
                if isinstance(pr_data["commits"], dict)
                else pr_data["commits"]
            )
            if commits_nodes and isinstance(commits_nodes, list):
                last_commit = (
                    commits_nodes[0].get("commit", {})
                    if isinstance(commits_nodes[0], dict)
                    else {}
                )
                rollup = last_commit.get("statusCheckRollup", {})
                if rollup and isinstance(rollup, dict):
                    ci_status = rollup.get("state", "UNKNOWN").upper()

                # Inspect checkSuites & checkRuns
                check_suites = (
                    last_commit.get("checkSuites", {}).get("nodes", [])
                    if isinstance(last_commit.get("checkSuites"), dict)
                    else []
                )
                for suite in check_suites:
                    if isinstance(suite, dict):
                        app_name = (suite.get("app") or {}).get("name", "")
                        runs = (
                            suite.get("checkRuns", {}).get("nodes", [])
                            if isinstance(suite.get("checkRuns"), dict)
                            else []
                        )
                        for r in runs:
                            if isinstance(r, dict):
                                r_name = r.get("name", "unnamed_check")
                                r_conc = r.get("conclusion")
                                if r_conc in ("FAILURE", "TIMED_OUT", "ACTION_REQUIRED"):
                                    raw_ci_failures.append(r_name)
                                    if is_external_deployment_gate(r_name, app_name):
                                        ignored_gate_failures.append(r_name)
                                    else:
                                        filtered_ci_failures.append(r_name)

        elif "ci_status" in pr_data:
            ci_status = str(pr_data["ci_status"]).upper()

        # If all failures were external preview gates, classified as CI passing
        if ci_status in ("FAILURE", "ERROR") and not filtered_ci_failures and ignored_gate_failures:
            ci_status = "SUCCESS_GATES_IGNORED"

        needs_ci_fix = (
            ci_status in ("FAILURE", "ERROR") or bool(filtered_ci_failures)
        )

        # 2. Inactivity / Staleness Evaluation
        created_at = parse_iso_datetime(
            pr_data.get("created_at") or pr_data.get("createdAt")
        )
        updated_at = parse_iso_datetime(
            pr_data.get("updated_at") or pr_data.get("updatedAt")
        )

        last_activity = updated_at or created_at or now
        inactivity_delta = now - last_activity
        inactivity_days = max(0.0, inactivity_delta.total_seconds() / 86400.0)

        is_stalled = inactivity_days >= self.stale_days_threshold
        is_draft = bool(pr_data.get("is_draft") or pr_data.get("isDraft", False))
        needs_maintainer_bump = is_stalled and not is_draft

        # 3. Review Status Evaluation
        review_status = "PENDING_REVIEW"
        if pr_data.get("audit_status") == "PASS":
            review_status = "VICTORY_AUDIT_PASSED"
        if pr_data.get("state") == "MERGED":
            review_status = "MERGED"
        elif pr_data.get("state") == "CLOSED":
            review_status = "CLOSED"

        return {
            "pr_url": pr_data.get("pr_url") or pr_data.get("url"),
            "pr_number": pr_data.get("pr_number") or pr_data.get("number"),
            "repo": pr_data.get("repo")
            or (
                pr_data.get("repository", {}).get("nameWithOwner")
                if isinstance(pr_data.get("repository"), dict)
                else None
            ),
            "ci_status": ci_status,
            "needs_ci_fix": needs_ci_fix,
            "actionable_ci_failures": filtered_ci_failures,
            "ignored_gate_failures": ignored_gate_failures,
            "inactivity_days": round(inactivity_days, 1),
            "is_stalled": is_stalled,
            "needs_maintainer_bump": needs_maintainer_bump,
            "review_status": review_status,
            "audited_at_iso": now.isoformat(),
        }

    def audit_and_update_pr(
        self,
        doc_id: str,
        doc_data: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Audits a single PR and updates its Firestore memory document with latest telemetry.
        """
        eval_result = self.inspect_pr_health(doc_data)
        doc_ref = self.db.collection(self.memory_collection).document(doc_id)

        update_payload: dict[str, Any] = {
            "escort_telemetry": {
                "ci_status": eval_result["ci_status"],
                "needs_ci_fix": eval_result["needs_ci_fix"],
                "actionable_ci_failures": eval_result["actionable_ci_failures"],
                "ignored_gate_failures": eval_result["ignored_gate_failures"],
                "is_stalled": eval_result["is_stalled"],
                "inactivity_days": eval_result["inactivity_days"],
                "needs_maintainer_bump": eval_result["needs_maintainer_bump"],
                "audited_at_iso": eval_result["audited_at_iso"],
            },
            "updated_at_iso": datetime.now(timezone.utc).isoformat(),
        }

        try:
            doc_ref.update(update_payload)
            logger.info(
                f"[Escort] Updated doc {doc_id}: CI={eval_result['ci_status']}, Stalled={eval_result['is_stalled']} ({eval_result['inactivity_days']}d)"
            )
        except Exception as e:
            logger.error(f"[!] Failed to update escort telemetry for {doc_id}: {e}")

        return eval_result

    def check_prs(self) -> list[dict[str, Any]]:
        """
        Scans all documents in `bounty_memory` collection and evaluates PR health.
        """
        results: list[dict[str, Any]] = []
        col_ref = self.db.collection(self.memory_collection)

        try:
            snaps = list(col_ref.stream())
            for snap in snaps:
                data = snap.to_dict() or {}
                res = self.audit_and_update_pr(snap.id, data)
                results.append(res)
        except Exception as e:
            logger.error(f"Error checking PRs in {self.memory_collection}: {e}")

        return results

    def run_sweep(self) -> dict[str, Any]:
        """Runs single escort sweep pass."""
        logger.info("Executing EscortEngine sweep...")
        results = self.check_prs()
        ci_failures_count = sum(1 for r in results if r.get("needs_ci_fix"))
        stalled_count = sum(1 for r in results if r.get("is_stalled"))
        return {
            "total_monitored": len(results),
            "ci_failures_count": ci_failures_count,
            "stalled_count": stalled_count,
            "results": results,
        }
