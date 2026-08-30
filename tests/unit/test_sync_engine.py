"""
Unit Tests for SyncEngine.
Validates financial settlement extraction from merged PRs, writing of immutable
settlement records to `bounty_settlements`, aggregate calculation of total USD earnings,
and updating `swarm_coordinator/state`.
"""

import pytest

from src.core.config import EVM_PAYOUT_ADDRESS
from src.core.firestore_client import OfflineFirestoreClient
from src.engines.sync_engine import SyncEngine, extract_payout_numeric


class TestFinancialPayoutExtraction:
    def test_extract_from_numeric_fields(self):
        p_str, val = extract_payout_numeric({"projected_payout_usd": 1500.0})
        assert val == 1500.0
        assert "$1500.00" in p_str

    def test_extract_from_escrow_dict(self):
        p_str, val = extract_payout_numeric({"escrow": {"amount_usd": 750.0}})
        assert val == 750.0
        assert "$750.00" in p_str

    def test_extract_from_text_descriptions(self):
        p_str, val = extract_payout_numeric({"title": "Fix issue ($2,500.00 reward)"})
        assert val == 2500.0
        assert "$2500.00" in p_str

        p_str_tok, val_tok = extract_payout_numeric({"body": "Bounty allocated: 500 USDC on Base"})
        assert val_tok == 500.0
        assert "500.0 USDC" in p_str_tok


class TestSyncEngineSettlementFlow:
    @pytest.fixture
    def sync_engine(self, tmp_path):
        db = OfflineFirestoreClient(state_dir=tmp_path / "firestore")
        return SyncEngine(db=db)

    def test_process_merged_doc_settlement(self, sync_engine):
        doc_id = "stellar_repo_42"
        doc_data = {
            "repo": "stellar/repo",
            "pr_number": 42,
            "title": "Add Soroban Smart Contract",
            "state": "MERGED",
            "projected_payout": "$1,000.00",
            "projected_payout_usd": 1000.0,
        }

        rec = sync_engine.process_doc_settlement(doc_id, doc_data)
        assert rec is not None
        assert rec["settlement_id"] == "settle_stellar_repo_42"
        assert rec["payout_usd"] == 1000.0
        assert rec["payout_recipient"] == EVM_PAYOUT_ADDRESS

        # Verify settlement stored in Firestore
        settle_doc = (
            sync_engine.db.collection(sync_engine.settlements_collection)
            .document("settle_stellar_repo_42")
            .get()
            .to_dict()
        )
        assert settle_doc["status"] == "SETTLED"
        assert settle_doc["payout_usd"] == 1000.0

    def test_unmerged_open_doc_ignored(self, sync_engine):
        doc_id = "stellar_repo_50"
        doc_data = {
            "repo": "stellar/repo",
            "pr_number": 50,
            "state": "OPEN",
            "status": "in_progress",
        }
        rec = sync_engine.process_doc_settlement(doc_id, doc_data)
        assert rec is None

    def test_sync_settlements_sweep_aggregates_and_updates_coordinator(self, sync_engine):
        # Add 2 merged PRs in memory
        sync_engine.db.collection(sync_engine.memory_collection).document("pr_1").set({
            "repo": "org/repo1",
            "pr_number": 1,
            "state": "MERGED",
            "projected_payout_usd": 500.0,
        })
        sync_engine.db.collection(sync_engine.memory_collection).document("pr_2").set({
            "repo": "org/repo2",
            "pr_number": 2,
            "state": "MERGED",
            "projected_payout_usd": 1500.0,
        })

        # Add 1 completed lead in leads
        sync_engine.db.collection(sync_engine.leads_collection).document("lead_1").set({
            "repo": "org/repo3",
            "issue_number": 3,
            "status": "completed",
            "projected_payout_usd": 250.0,
        })

        res = sync_engine.sync_settlements()
        assert res["synced_count"] == 3
        assert res["total_settled_usd"] == 2250.0

        # Verify coordinator state document
        state_doc = (
            sync_engine.db.collection(sync_engine.coordinator_collection)
            .document("state")
            .get()
            .to_dict()
        )
        assert state_doc["status"] == "HEALTHY"
        assert state_doc["total_settled_usd"] == 2250.0
        assert state_doc["total_settled_count"] == 3
        assert len(state_doc["active_engines"]) == 5
