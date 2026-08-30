"""
Unit Tests for Firestore Client, ACID Transactions & Offline JSONL Fallback (Milestone 1).
"""

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.config import (
    COLLECTION_BOUNTY_LEADS,
    COLLECTION_BOUNTY_MEMORY,
    COLLECTION_BOUNTY_SETTLEMENTS,
    COLLECTION_SWARM_COORDINATOR,
    COLLECTION_SWARM_OPERATIONS,
)
from src.core.firestore_client import (
    OfflineFirestoreClient,
    claim_lead_atomic,
    get_coordinator_collection,
    get_leads_collection,
    get_memory_collection,
    get_operations_collection,
    get_settlements_collection,
    resolve_credentials_path,
    resolve_project_id,
)


class TestFirestoreClientResolution:
    """Verifies credential and project resolution."""

    def test_resolve_project_id_defaults(self, monkeypatch):
        monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
        monkeypatch.delenv("FIRESTORE_PROJECT_ID", raising=False)
        monkeypatch.delenv("GCP_PROJECT", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

        pid = resolve_project_id()
        assert pid == "odin-500008"

        explicit = resolve_project_id(explicit_project="custom-pid-123")
        assert explicit == "custom-pid-123"

    def test_resolve_credentials_path(self, temp_workspace: Path):
        dummy_cred = temp_workspace / "fake_creds.json"
        dummy_cred.write_text('{"project_id": "test-pid"}')

        resolved = resolve_credentials_path(str(dummy_cred))
        assert resolved is not None
        assert Path(resolved).resolve() == dummy_cred.resolve()


class TestOfflineFirestoreClientCRUD:
    """Verifies document operations, queries, and batches in OfflineFirestoreClient."""

    def test_document_set_get_update_delete(self, offline_db: OfflineFirestoreClient):
        leads_col = offline_db.collection(COLLECTION_BOUNTY_LEADS)
        doc_ref = leads_col.document("stellar_bridge_101")

        # Initial get (does not exist)
        snap = doc_ref.get()
        assert snap.exists is False
        assert snap.to_dict() is None

        # Set
        initial_data = {
            "title": "Build Soroban Bridge",
            "status": "queued",
            "payout_usd": 1500.0,
            "ecosystem": "stellar",
        }
        doc_ref.set(initial_data)

        snap = doc_ref.get()
        assert snap.exists is True
        assert snap.to_dict()["title"] == "Build Soroban Bridge"
        assert snap.get("payout_usd") == 1500.0
        assert snap.get("non_existent") is None

        # Update
        doc_ref.update({"status": "running_orbstack", "worker": "worker_1"})
        snap = doc_ref.get()
        assert snap.get("status") == "running_orbstack"
        assert snap.get("worker") == "worker_1"
        assert snap.get("payout_usd") == 1500.0  # Preserved

        # Delete
        doc_ref.delete()
        snap = doc_ref.get()
        assert snap.exists is False

    def test_collection_query_filtering_ordering_limiting(
        self, offline_db: OfflineFirestoreClient
    ):
        col = offline_db.collection("test_items")
        col.document("item_1").set({"category": "evm", "value": 100, "active": True})
        col.document("item_2").set({"category": "stellar", "value": 500, "active": True})
        col.document("item_3").set({"category": "evm", "value": 300, "active": False})
        col.document("item_4").set({"category": "evm", "value": 200, "active": True})

        # Where filter
        evm_active = col.where("category", "==", "evm").where("active", "==", True).get()
        assert len(evm_active) == 2
        ids = {s.id for s in evm_active}
        assert ids == {"item_1", "item_4"}

        # Order by and limit
        ordered = (
            col.where("active", "==", True)
            .order_by("value", direction="DESCENDING")
            .limit(2)
            .get()
        )
        assert len(ordered) == 2
        assert ordered[0].id == "item_2"  # value 500
        assert ordered[1].id == "item_4"  # value 200

    def test_batch_writes(self, offline_db: OfflineFirestoreClient):
        col = offline_db.collection("batch_test")
        batch = offline_db.batch()

        batch.set(col.document("b1"), {"count": 1})
        batch.set(col.document("b2"), {"count": 2})
        batch.set(col.document("b3"), {"count": 3})
        batch.commit()

        assert col.document("b1").get().get("count") == 1
        assert col.document("b2").get().get("count") == 2
        assert col.document("b3").get().get("count") == 3


class TestOfflineFirestoreACIDTransactions:
    """Verifies ACID transaction isolation, atomic commits, and rollbacks."""

    def test_transaction_atomic_commit(self, offline_db: OfflineFirestoreClient):
        col = offline_db.collection("accounts")
        col.document("alice").set({"balance": 1000})
        col.document("bob").set({"balance": 500})

        def transfer_funds(txn):
            alice_ref = col.document("alice")
            bob_ref = col.document("bob")

            alice_snap = txn.get(alice_ref)
            bob_snap = txn.get(bob_ref)

            alice_bal = alice_snap.get("balance")
            bob_bal = bob_snap.get("balance")

            txn.update(alice_ref, {"balance": alice_bal - 200})
            txn.update(bob_ref, {"balance": bob_bal + 200})
            return True

        res = offline_db.run_transaction(transfer_funds)
        assert res is True
        assert col.document("alice").get().get("balance") == 800
        assert col.document("bob").get().get("balance") == 700

    def test_transaction_rollback_on_error(self, offline_db: OfflineFirestoreClient):
        col = offline_db.collection("accounts")
        col.document("charlie").set({"balance": 100})
        col.document("dave").set({"balance": 50})

        def failing_transfer(txn):
            charlie_ref = col.document("charlie")
            charlie_snap = txn.get(charlie_ref)
            txn.update(charlie_ref, {"balance": charlie_snap.get("balance") - 50})
            # Simulate exception before commit
            raise ValueError("Network timeout during bank settlement")

        with pytest.raises(ValueError):
            offline_db.run_transaction(failing_transfer)

        # Confirm charlie balance was NOT mutated (rollback succeeded)
        assert col.document("charlie").get().get("balance") == 100
        assert col.document("dave").get().get("balance") == 50

    def test_concurrent_transactions_thread_safety(self, offline_db: OfflineFirestoreClient):
        import concurrent.futures

        col = offline_db.collection("counters")
        col.document("global_counter").set({"count": 0})

        def inc_counter():
            def _txn(txn):
                ref = col.document("global_counter")
                snap = txn.get(ref)
                current = snap.get("count") or 0
                txn.update(ref, {"count": current + 1})
                return current + 1

            offline_db.run_transaction(_txn)

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(inc_counter) for _ in range(100)]
            concurrent.futures.wait(futures)

        final_snap = col.document("global_counter").get()
        assert final_snap.get("count") == 100


class TestAtomicLeadClaiming:
    """Verifies claim_lead_atomic locking logic."""

    def test_claim_unclaimed_lead_success(self, offline_db: OfflineFirestoreClient):
        leads_col = offline_db.collection(COLLECTION_BOUNTY_LEADS)
        lead_id = "test_repo_issue_1"
        leads_col.document(lead_id).set({"status": "queued", "title": "Add test"})

        success = claim_lead_atomic(offline_db, lead_id, worker_id="worker_alpha")
        assert success is True

        snap = leads_col.document(lead_id).get()
        assert snap.get("status") == "claimed"
        assert snap.get("claimed_by") == "worker_alpha"
        assert snap.get("lock_acquired_at") is not None

    def test_claim_already_claimed_lead_rejected(self, offline_db: OfflineFirestoreClient):
        leads_col = offline_db.collection(COLLECTION_BOUNTY_LEADS)
        lead_id = "test_repo_issue_2"
        now_iso = datetime.now(timezone.utc).isoformat()
        leads_col.document(lead_id).set({
            "status": "claimed",
            "claimed_by": "worker_alpha",
            "lock_acquired_at": now_iso,
        })

        # Worker beta attempts to claim while lock is fresh
        success = claim_lead_atomic(offline_db, lead_id, worker_id="worker_beta", lock_timeout_sec=300)
        assert success is False

        # Status unchanged
        snap = leads_col.document(lead_id).get()
        assert snap.get("claimed_by") == "worker_alpha"

    def test_claim_expired_lock_lead_success(self, offline_db: OfflineFirestoreClient):
        leads_col = offline_db.collection(COLLECTION_BOUNTY_LEADS)
        lead_id = "test_repo_issue_3"
        # Lock acquired 600 seconds ago
        past_iso = datetime.fromtimestamp(time.time() - 600, tz=timezone.utc).isoformat()
        leads_col.document(lead_id).set({
            "status": "claimed",
            "claimed_by": "stale_worker",
            "lock_acquired_at": past_iso,
        })

        # Worker beta claims expired lead
        success = claim_lead_atomic(offline_db, lead_id, worker_id="worker_beta", lock_timeout_sec=300)
        assert success is True

        snap = leads_col.document(lead_id).get()
        assert snap.get("claimed_by") == "worker_beta"

    def test_claim_completed_or_nonexistent_lead_rejected(
        self, offline_db: OfflineFirestoreClient
    ):
        leads_col = offline_db.collection(COLLECTION_BOUNTY_LEADS)
        leads_col.document("completed_lead").set({"status": "completed"})

        assert claim_lead_atomic(offline_db, "completed_lead", worker_id="w1") is False
        assert claim_lead_atomic(offline_db, "non_existent_lead", worker_id="w1") is False


class TestCollectionAccessors:
    """Verifies collection accessor helper functions."""

    def test_accessors_with_offline_client(self, offline_db: OfflineFirestoreClient):
        assert get_leads_collection(offline_db).id == COLLECTION_BOUNTY_LEADS
        assert get_operations_collection(offline_db).id == COLLECTION_SWARM_OPERATIONS
        assert get_memory_collection(offline_db).id == COLLECTION_BOUNTY_MEMORY
        assert get_settlements_collection(offline_db).id == COLLECTION_BOUNTY_SETTLEMENTS
        assert get_coordinator_collection(offline_db).id == COLLECTION_SWARM_COORDINATOR
