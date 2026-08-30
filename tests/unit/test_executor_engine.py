"""
Unit Tests for ExecutorEngine.
Validates atomic lead claiming, stipulation extraction, Draft PR creation,
payout routing formatting, PathGuard sandbox validation, and teardown.
"""

from datetime import datetime, timezone

import pytest

from src.core.config import EVM_PAYOUT_ADDRESS, STELLAR_PAYOUT_ADDRESS
from src.core.firestore_client import OfflineFirestoreClient
from src.engines.executor_engine import (
    ExecutorEngine,
    extract_stipulations_from_text,
)


class TestStipulationsAndPayoutBlock:
    def test_extract_checklist_stipulations(self):
        body = """
        Please fix the bug.
        - [ ] Add unit test for error case
        - [x] Update documentation
        - [ ] Verify Soroban auth
        """
        stips = extract_stipulations_from_text(body)
        assert len(stips) == 3
        assert "Add unit test for error case" in stips
        assert "Update documentation" in stips
        assert "Verify Soroban auth" in stips

    def test_fallback_default_stipulations_when_empty(self):
        stips = extract_stipulations_from_text("Short body with no checklist")
        assert len(stips) >= 3
        assert any("genuine" in s.lower() for s in stips)

    def test_mandatory_payout_routing_addresses(self):
        engine = ExecutorEngine()
        block = engine.generate_payout_routing_block()
        assert EVM_PAYOUT_ADDRESS in block
        assert STELLAR_PAYOUT_ADDRESS in block
        assert "0xF46C9F6d70C50BF81ef3588AB523a90a594a2F89" in block
        assert "GCL6OXAMLD75BMTINA6EMRUDWK5THQUSHMYNLSNBCJAPZJHNYJTUNIBC" in block


class TestExecutorEngineExecutionFlow:
    @pytest.fixture
    def test_env(self, tmp_path):
        db = OfflineFirestoreClient(state_dir=tmp_path / "firestore")
        sandbox_dir = tmp_path / "sandboxes"
        sandbox_dir.mkdir()
        engine = ExecutorEngine(
            db=db,
            sandbox_base_dir=sandbox_dir,
            worker_id="test_worker_1",
        )
        return db, engine, sandbox_dir

    def test_claim_and_execute_unclaimed_lead_success(self, test_env):
        db, engine, _ = test_env

        # Create a lead in Firestore
        lead_id = "stellar_example_10"
        lead_data = {
            "id": lead_id,
            "repo": "stellar/example",
            "issue_number": 10,
            "title": "Add Soroban Token Example",
            "body": "- [ ] Implement token\n- [ ] Add tests",
            "status": "pending_triage",
            "projected_payout": "$500.00",
            "projected_payout_usd": 500.0,
            "ecosystem": "stellar",
        }
        db.collection(engine.leads_collection).document(lead_id).set(lead_data)

        # Run claim and execute (dry_run=True to skip docker daemon calls)
        res = engine.claim_and_execute_lead(
            lead_id=lead_id,
            lead_data=lead_data,
            skip_clone=True,
            dry_run=True,
        )

        assert res["success"] is True
        assert res["final_lead_status"] == "pr_open"
        assert "pr_url" in res

        # Verify Firestore lead update
        updated_lead = db.collection(engine.leads_collection).document(lead_id).get().to_dict()
        assert updated_lead["status"] == "pr_open"
        assert updated_lead["execution_success"] is True

        # Verify operations record
        ops = list(db.collection(engine.operations_collection).stream())
        assert len(ops) == 1
        assert ops[0].to_dict()["status"] == "DESTROYED"

    def test_claim_and_execute_already_claimed_lead_rejected(self, test_env):
        db, engine, _ = test_env

        lead_id = "stellar_example_20"
        lead_data = {
            "id": lead_id,
            "repo": "stellar/example",
            "issue_number": 20,
            "status": "claimed",
            "lock_acquired_at": datetime.now(timezone.utc).isoformat(),
        }
        db.collection(engine.leads_collection).document(lead_id).set(lead_data)

        res = engine.claim_and_execute_lead(
            lead_id=lead_id,
            lead_data=lead_data,
            skip_clone=True,
            dry_run=True,
        )

        assert res["success"] is False
        assert res["reason"] == "CLAIM_REJECTED"

    def test_execute_pending_leads_batch(self, test_env):
        db, engine, _ = test_env

        for i in range(1, 4):
            lead_id = f"stellar_example_{i}"
            db.collection(engine.leads_collection).document(lead_id).set({
                "id": lead_id,
                "repo": "stellar/example",
                "issue_number": i,
                "title": f"Bounty {i}",
                "status": "priority_triage" if i == 1 else "pending_triage",
                "projected_payout": "$500.00",
                "projected_payout_usd": 500.0,
            })

        results = engine.execute_pending_leads(limit=2)
        assert len(results) == 2
        assert all(r["success"] is True for r in results)

    def test_execute_lead_alias_and_banned_platform_rejection(self, test_env):
        db, engine, _ = test_env

        # 1. Test execute_lead alias with valid lead
        valid_id = "valid_lead_1"
        valid_data = {
            "id": valid_id,
            "repo": "stellar/example",
            "issue_number": 42,
            "title": "Valid task",
            "status": "pending_triage",
            "platform": "github",
        }
        db.collection(engine.leads_collection).document(valid_id).set(valid_data)
        res_valid = engine.execute_lead(
            lead_id=valid_id,
            lead_data=valid_data,
            skip_clone=True,
            dry_run=True,
        )
        assert res_valid["success"] is True
        assert res_valid["final_lead_status"] == "pr_open"

        # 2. Test execute_lead alias with banned repository even with platform='github'
        banned_id = "banned_twenty_1"
        banned_data = {
            "id": banned_id,
            "repo": "twentyhq/twenty",
            "issue_number": 99,
            "title": "Banned task",
            "status": "pending_triage",
            "platform": "github",
        }
        db.collection(engine.leads_collection).document(banned_id).set(banned_data)
        res_banned = engine.execute_lead(
            lead_id=banned_id,
            lead_data=banned_data,
            skip_clone=True,
            dry_run=True,
        )
        assert res_banned["success"] is False
        assert res_banned["final_lead_status"] == "failed_verification"

