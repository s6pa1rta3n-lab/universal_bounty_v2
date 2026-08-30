"""
Unit Tests for EscortEngine.
Validates PR status check rollup inspection, filtering of external preview deployment
gates (Vercel, Netlify, Cloudflare Pages), actionable CI failure detection, 14-day
inactivity staleness identification, and Firestore memory synchronization.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.core.firestore_client import OfflineFirestoreClient
from src.engines.escort_engine import (
    EscortEngine,
    is_external_deployment_gate,
)


class TestDeploymentGateFiltering:
    def test_deployment_gate_detection(self):
        assert is_external_deployment_gate("Deploy Preview - Vercel", "Vercel") is True
        assert is_external_deployment_gate("netlify/example/deploy-preview", "Netlify") is True
        assert is_external_deployment_gate("Cloudflare Pages Deployment", "Cloudflare") is True
        assert is_external_deployment_gate("preview-deployment-gate") is True

    def test_native_ci_not_treated_as_deployment_gate(self):
        assert is_external_deployment_gate("build-and-test", "GitHub Actions") is False
        assert is_external_deployment_gate("cargo-test", "GitHub Actions") is False
        assert is_external_deployment_gate("lint-and-typecheck", "CircleCI") is False


class TestEscortEnginePRHealth:
    @pytest.fixture
    def escort_engine(self, tmp_path):
        db = OfflineFirestoreClient(state_dir=tmp_path / "firestore")
        return EscortEngine(db=db, stale_days_threshold=14)

    def test_filter_vercel_preview_gate_failure(self, escort_engine):
        pr_data = {
            "pr_url": "https://github.com/stellar/repo/pull/1",
            "pr_number": 1,
            "repo": "stellar/repo",
            "is_draft": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "commits": [{
                "commit": {
                    "statusCheckRollup": {"state": "FAILURE"},
                    "checkSuites": {
                        "nodes": [{
                            "app": {"name": "Vercel"},
                            "conclusion": "FAILURE",
                            "checkRuns": {
                                "nodes": [{"name": "Deploy Preview - Vercel", "conclusion": "FAILURE"}]
                            },
                        }]
                    },
                }
            }],
        }

        health = escort_engine.inspect_pr_health(pr_data)
        assert health["ci_status"] == "SUCCESS_GATES_IGNORED"
        assert health["needs_ci_fix"] is False
        assert len(health["ignored_gate_failures"]) == 1
        assert len(health["actionable_ci_failures"]) == 0

    def test_detect_actionable_ci_failure(self, escort_engine):
        pr_data = {
            "pr_url": "https://github.com/stellar/repo/pull/2",
            "pr_number": 2,
            "repo": "stellar/repo",
            "is_draft": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "commits": [{
                "commit": {
                    "statusCheckRollup": {"state": "FAILURE"},
                    "checkSuites": {
                        "nodes": [{
                            "app": {"name": "GitHub Actions"},
                            "conclusion": "FAILURE",
                            "checkRuns": {
                                "nodes": [{"name": "cargo-test", "conclusion": "FAILURE"}]
                            },
                        }]
                    },
                }
            }],
        }

        health = escort_engine.inspect_pr_health(pr_data)
        assert health["needs_ci_fix"] is True
        assert "cargo-test" in health["actionable_ci_failures"]

    def test_detect_14_day_inactivity_staleness(self, escort_engine):
        stale_date = (datetime.now(timezone.utc) - timedelta(days=16)).isoformat()
        pr_data = {
            "pr_url": "https://github.com/stellar/repo/pull/3",
            "pr_number": 3,
            "repo": "stellar/repo",
            "is_draft": False,
            "updated_at": stale_date,
            "ci_status": "SUCCESS",
        }

        health = escort_engine.inspect_pr_health(pr_data)
        assert health["is_stalled"] is True
        assert health["needs_maintainer_bump"] is True
        assert health["inactivity_days"] >= 15.0

    def test_draft_pr_staleness_does_not_trigger_maintainer_bump(self, escort_engine):
        stale_date = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        pr_data = {
            "pr_url": "https://github.com/stellar/repo/pull/4",
            "pr_number": 4,
            "repo": "stellar/repo",
            "is_draft": True,
            "updated_at": stale_date,
            "ci_status": "SUCCESS",
        }

        health = escort_engine.inspect_pr_health(pr_data)
        assert health["is_stalled"] is True
        assert health["needs_maintainer_bump"] is False  # Draft PRs should not ping maintainers!

    def test_audit_and_update_firestore_memory(self, escort_engine):
        doc_id = "stellar_repo_5"
        pr_data = {
            "pr_url": "https://github.com/stellar/repo/pull/5",
            "pr_number": 5,
            "repo": "stellar/repo",
            "is_draft": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "ci_status": "SUCCESS",
        }
        escort_engine.db.collection(escort_engine.memory_collection).document(doc_id).set(pr_data)

        eval_res = escort_engine.audit_and_update_pr(doc_id, pr_data)
        assert eval_res["ci_status"] == "SUCCESS"

        updated = escort_engine.db.collection(escort_engine.memory_collection).document(doc_id).get().to_dict()
        assert "escort_telemetry" in updated
        assert updated["escort_telemetry"]["ci_status"] == "SUCCESS"
