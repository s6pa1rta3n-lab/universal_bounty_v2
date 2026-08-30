"""
Empirical Adversarial Stress & Chaos Test Harness for Universal Bounty V2 Milestone 1.

Tested Modules:
- src.migrate: migrate_queues, deduplicate_leads, normalize_lead_schema
- src.core.firestore_client: OfflineFirestoreClient, OfflineBatch, OfflineTransaction, claim_lead_atomic
"""

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.config import (
    COLLECTION_BOUNTY_LEADS,
    COLLECTION_BOUNTY_SETTLEMENTS,
    COLLECTION_SWARM_OPERATIONS,
)
from src.core.exceptions import MigrationError
from src.core.firestore_client import (
    OfflineFirestoreClient,
    claim_lead_atomic,
)
from src.core.safe_io import SafeIO
from src.migrate import (
    deduplicate_leads,
    migrate_queues,
    normalize_lead_schema,
)

# =============================================================================
# 1. Corrupted JSONL & Malformed Data Stress Tests
# =============================================================================


class TestMigrateQueuesCorruptJSONL:
    """Stress tests migrate_queues against invalid, malformed, and corrupted files."""

    def test_corrupt_json_syntax_raises_migration_error(self, temp_workspace: Path):
        """Verify that truncated or malformed JSON syntax raises MigrationError without partial target writes."""
        src_path = temp_workspace / "corrupt_syntax.jsonl"
        dest_path = temp_workspace / "target_never_created.jsonl"

        corrupted_content = (
            '{"repository": "stellar/soroban", "number": 1, "payout_usd": 100}\n'
            '{"repository": "base/bridge", "number": 2, "unclosed_string": "error\n'
            '{"repository": "ethereum/web3", "number": 3, "payout_usd": 50}\n'
        )
        SafeIO.write_text(src_path, corrupted_content)

        with pytest.raises(MigrationError) as exc_info:
            migrate_queues(source_jsonl=src_path, target_jsonl=dest_path)

        assert "Error reading source JSONL" in str(exc_info.value)
        # Ensure target file was NEVER created on disk
        assert not dest_path.exists()

    def test_corrupt_binary_and_null_bytes(self, temp_workspace: Path):
        """Verify handling of null bytes and binary data."""
        src_path = temp_workspace / "binary_data.jsonl"
        dest_path = temp_workspace / "dest_binary.jsonl"

        # Write binary garbage
        with open(src_path, "wb") as f:
            f.write(b'{"repository": "stellar/soroban", "number": 1}\n\x00\xff\xfe\x00\n')

        with pytest.raises(MigrationError):
            migrate_queues(source_jsonl=src_path, target_jsonl=dest_path)

        assert not dest_path.exists()

    def test_empty_and_whitespace_lines_handling(self, temp_workspace: Path):
        """Verify that blank lines, whitespace, and clean records are handled gracefully."""
        src_path = temp_workspace / "mixed_whitespace.jsonl"
        dest_path = temp_workspace / "dest_clean.jsonl"

        content = (
            "\n"
            "   \n"
            '{"repository": "stellar/soroban", "number": 1, "payout_usd": 100}\n'
            "\t\t\n"
            '{"repository": "base/bridge", "number": 2, "payout_usd": 200}\n'
            "\n\n"
        )
        SafeIO.write_text(src_path, content)

        res = migrate_queues(source_jsonl=src_path, target_jsonl=dest_path)
        assert res.total_read == 2
        assert res.valid_records == 2
        assert res.migrated_records == 2
        assert dest_path.exists()

        records = SafeIO.read_jsonl(dest_path)
        assert len(records) == 2

    def test_non_dict_json_types_discarded(self, temp_workspace: Path):
        """Verify non-dict JSON lines (arrays, ints, strings) are discarded without crashing."""
        src_path = temp_workspace / "non_dict.jsonl"
        dest_path = temp_workspace / "dest_non_dict.jsonl"

        content = (
            '[1, 2, 3]\n'
            '"a raw string"\n'
            '12345\n'
            'true\n'
            '{"repository": "stellar/soroban", "number": 5, "payout_usd": 300}\n'
        )
        SafeIO.write_text(src_path, content)

        res = migrate_queues(source_jsonl=src_path, target_jsonl=dest_path)
        assert res.total_read == 1  # SafeIO.stream_jsonl only yields dicts
        assert res.valid_records == 1
        assert res.migrated_records == 1

    def test_invalid_schema_missing_fields_discarded(self, temp_workspace: Path):
        """Verify that valid JSON dicts without required lead identifiers are discarded."""
        src_path = temp_workspace / "invalid_schema.jsonl"
        dest_path = temp_workspace / "dest_invalid_schema.jsonl"

        content = (
            '{"foo": "bar"}\n'
            '{"title": "Just a title without repo or issue number"}\n'
            '{"repository": "stellar/soroban", "number": "invalid_not_number"}\n'
            '{"repository": "stellar/soroban", "number": 10, "payout_usd": 150}\n'
        )
        SafeIO.write_text(src_path, content)

        res = migrate_queues(source_jsonl=src_path, target_jsonl=dest_path)
        assert res.total_read == 4
        assert res.valid_records == 1
        assert res.migrated_records == 1
        assert len(res.errors) >= 3

    def test_malformed_fields_fallback_resilience(self, temp_workspace: Path):
        """Verify resilient normalization when fields contain unexpected types (e.g. non-numeric payouts, dict labels)."""
        raw = {
            "url": "https://github.com/org/repo/issues/42",
            "payout_usd": "not-a-number",
            "priority": "INVALID_PRIORITY",
            "labels": [{"name": "bounty"}, "plain_label", 12345, None],
            "repository": {"invalid": "dict_without_name"},
        }
        norm = normalize_lead_schema(raw)
        assert norm is not None
        assert norm["number"] == 42
        assert norm["repository"] == "org/repo"
        assert norm["projected_payout_usd"] == 0.0
        assert norm["priority"] == "standard"
        assert norm["labels"] == ["bounty", "plain_label"]


# =============================================================================
# 2. Duplicate Leads & Conflicting Payouts Stress Tests
# =============================================================================


class TestMigrateQueuesDeduplicationAndPayouts:
    """Stress tests deduplication logic, conflicting payouts, and priority sorting."""

    def test_conflicting_payout_highest_preserved_regardless_of_order(self):
        """Verify higher payout is always preserved regardless of occurrence order."""
        # Low first, then High
        low_first = [
            {"id": "doc_1", "projected_payout_usd": 100.0, "title": "Low", "priority": "standard"},
            {"id": "doc_1", "projected_payout_usd": 500.0, "title": "High", "priority": "high"},
            {"id": "doc_1", "projected_payout_usd": 250.0, "title": "Mid", "priority": "high"},
        ]
        deduped1 = deduplicate_leads(low_first)
        assert len(deduped1) == 1
        assert deduped1[0]["projected_payout_usd"] == 500.0
        assert deduped1[0]["title"] == "High"

        # High first, then Low
        high_first = [
            {"id": "doc_1", "projected_payout_usd": 500.0, "title": "High", "priority": "high"},
            {"id": "doc_1", "projected_payout_usd": 100.0, "title": "Low", "priority": "standard"},
            {"id": "doc_1", "projected_payout_usd": 250.0, "title": "Mid", "priority": "high"},
        ]
        deduped2 = deduplicate_leads(high_first)
        assert len(deduped2) == 1
        assert deduped2[0]["projected_payout_usd"] == 500.0

    def test_identical_payout_richer_body_merge(self):
        """Verify that when payouts are identical, the record with the richer/longer body is merged."""
        records = [
            {
                "id": "doc_merge",
                "projected_payout_usd": 200.0,
                "body": "Short",
                "priority": "standard",
                "custom_field_1": "old",
            },
            {
                "id": "doc_merge",
                "projected_payout_usd": 200.0,
                "body": "Long and detailed specification with acceptance criteria.",
                "priority": "standard",
                "custom_field_2": "new",
            },
        ]
        deduped = deduplicate_leads(records)
        assert len(deduped) == 1
        assert len(deduped[0]["body"]) > 10
        assert deduped[0]["custom_field_1"] == "old"
        assert deduped[0]["custom_field_2"] == "new"

    def test_deterministic_priority_and_payout_sorting(self):
        """Verify strict sorting: High > Standard > Low, with descending payouts within each priority class."""
        import random

        raw_leads = []
        priorities = ["high", "standard", "low"]

        for i in range(100):
            prio = priorities[i % 3]
            payout = float(random.randint(10, 5000))
            raw_leads.append({
                "id": f"lead_{i}",
                "priority": prio,
                "projected_payout_usd": payout,
            })

        deduped = deduplicate_leads(raw_leads)
        assert len(deduped) == 100

        # Check priority partition
        prio_order = {"high": 0, "standard": 1, "low": 2}
        for i in range(len(deduped) - 1):
            curr_prio = prio_order[deduped[i]["priority"]]
            next_prio = prio_order[deduped[i + 1]["priority"]]
            assert curr_prio <= next_prio

            # If same priority, payout must be descending
            if curr_prio == next_prio:
                assert deduped[i]["projected_payout_usd"] >= deduped[i + 1]["projected_payout_usd"]

    def test_canonical_id_dedup_across_different_representations(self, temp_workspace: Path):
        """Verify that varied representation of the same issue resolve to identical doc_id and dedup."""
        src_path = temp_workspace / "varied_representations.jsonl"
        dest_path = temp_workspace / "dest_varied.jsonl"

        records = [
            {"repository": "stellar/soroban-example", "number": 42, "payout_usd": 100.0},
            {"url": "https://github.com/stellar/soroban-example/issues/42", "payout_usd": 800.0},
            {"repo": "stellar/soroban-example", "issue_number": "42", "payout_usd": 300.0},
        ]
        SafeIO.write_jsonl(src_path, records)

        res = migrate_queues(source_jsonl=src_path, target_jsonl=dest_path)
        assert res.total_read == 3
        assert res.valid_records == 3
        assert res.migrated_records == 1
        assert res.skipped_duplicates == 2

        migrated = SafeIO.read_jsonl(dest_path)
        assert len(migrated) == 1
        assert migrated[0]["id"] == "stellar_soroban_example_42"
        assert migrated[0]["projected_payout_usd"] == 800.0


# =============================================================================
# 3. Extreme Payload Boundaries Stress Tests
# =============================================================================


class TestMigrateQueuesExtremeBoundaries:
    """Stress tests large scale, massive payload, unicode, and boundary values."""

    def test_massive_volume_10000_records(self, temp_workspace: Path):
        """Empirically test processing 10,000 distinct lead records."""
        src_path = temp_workspace / "massive_10k.jsonl"
        dest_path = temp_workspace / "dest_10k.jsonl"

        records = [
            {
                "repository": f"org_{i % 50}/repo_{i % 200}",
                "number": i + 1,
                "title": f"Bounty Lead #{i+1}",
                "payout_usd": float(i % 1000),
                "priority": "high" if (i % 10 == 0) else "standard",
            }
            for i in range(10000)
        ]

        SafeIO.write_jsonl(src_path, records)

        t1 = time.time()
        res = migrate_queues(source_jsonl=src_path, target_jsonl=dest_path)
        t_migrate = time.time() - t1

        assert res.total_read == 10000
        assert res.valid_records == 10000
        assert res.migrated_records == 10000
        assert res.skipped_duplicates == 0
        assert dest_path.exists()
        assert t_migrate < 5.0  # Migration of 10k items must complete under 5 seconds

    def test_massive_body_payload_megabyte(self, temp_workspace: Path):
        """Empirically test a lead record containing 2 MB of body markdown."""
        src_path = temp_workspace / "large_body.jsonl"
        dest_path = temp_workspace / "dest_large_body.jsonl"

        large_body = "A" * (2 * 1024 * 1024)  # 2MB
        record = {
            "repository": "stellar/soroban-sdk",
            "number": 99,
            "title": "Large Payload Test",
            "body": large_body,
            "payout_usd": 2000.0,
        }
        SafeIO.write_jsonl(src_path, [record])

        res = migrate_queues(source_jsonl=src_path, target_jsonl=dest_path)
        assert res.migrated_records == 1

        migrated = SafeIO.read_jsonl(dest_path)
        assert len(migrated[0]["body"]) == 2 * 1024 * 1024

    def test_deeply_nested_json_structures(self, temp_workspace: Path):
        """Verify deeply nested dict structures within labels or extra metadata fields."""
        src_path = temp_workspace / "deep_nested.jsonl"
        dest_path = temp_workspace / "dest_deep_nested.jsonl"

        nested_labels = [{"name": "level1", "meta": {"level2": {"level3": [1, 2, {"level4": True}]}}}]
        record = {
            "repository": "base/contracts",
            "number": 777,
            "labels": nested_labels,
            "payout_usd": 150.0,
        }
        SafeIO.write_jsonl(src_path, [record])

        res = migrate_queues(source_jsonl=src_path, target_jsonl=dest_path)
        assert res.migrated_records == 1
        migrated = SafeIO.read_jsonl(dest_path)
        assert "level1" in migrated[0]["labels"]

    def test_unicode_emojis_rtl_and_special_characters(self, temp_workspace: Path):
        """Verify full preservation of multi-language unicode, emojis, and symbols."""
        src_path = temp_workspace / "unicode.jsonl"
        dest_path = temp_workspace / "dest_unicode.jsonl"

        record = {
            "repository": "unicode/test-repo",
            "number": 888,
            "title": "Soroban 智能合约 🚀 [GrantFox] المكافأة $500 💰",
            "body": "Testing unicode characters: 🦀 Rust, ⭐ Stellar, 日本語, 한국어, العربية, \u202eRTL_OVERRIDE\u202c",
            "payout_usd": 500.0,
        }
        SafeIO.write_jsonl(src_path, [record])

        res = migrate_queues(source_jsonl=src_path, target_jsonl=dest_path)
        assert res.migrated_records == 1

        migrated = SafeIO.read_jsonl(dest_path)
        assert "智能合约" in migrated[0]["title"]
        assert "🚀" in migrated[0]["title"]
        assert "🦀" in migrated[0]["body"]

    def test_boundary_payout_values(self, temp_workspace: Path):
        """Verify zero, negative, huge, and micro float payouts."""
        src_path = temp_workspace / "boundary_payouts.jsonl"
        dest_path = temp_workspace / "dest_boundary_payouts.jsonl"

        records = [
            {"repository": "org/zero", "number": 1, "payout_usd": 0.0},
            {"repository": "org/negative", "number": 2, "payout_usd": -100.0},
            {"repository": "org/billion", "number": 3, "payout_usd": 1000000000.0},
            {"repository": "org/fractional", "number": 4, "payout_usd": 0.00001},
        ]
        SafeIO.write_jsonl(src_path, records)

        res = migrate_queues(source_jsonl=src_path, target_jsonl=dest_path)
        assert res.migrated_records == 4

        migrated = SafeIO.read_jsonl(dest_path)
        payouts = {r["repository"]: r["projected_payout_usd"] for r in migrated}
        assert payouts["org/zero"] == 0.0
        assert payouts["org/negative"] == -100.0
        assert payouts["org/billion"] == 1000000000.0
        assert payouts["org/fractional"] == 0.00001


# =============================================================================
# 4. Dry-Run Safety Stress Tests
# =============================================================================


class TestMigrateQueuesDryRunSafety:
    """Stress tests dry-run execution safety: strictly zero mutations."""

    def test_dry_run_zero_filesystem_mutations(self, temp_workspace: Path):
        """Verify that dry_run=True creates zero files, does not touch existing files, and makes no backups."""
        src_path = temp_workspace / "source_dry.jsonl"
        dest_path = temp_workspace / "target_dry.jsonl"

        records = [{"repository": "org/repo", "number": 1, "payout_usd": 100.0}]
        SafeIO.write_jsonl(src_path, records)

        res = migrate_queues(source_jsonl=src_path, target_jsonl=dest_path, dry_run=True, backup=True)
        assert res.dry_run is True
        assert res.migrated_records == 1
        assert not dest_path.exists()

        # Check that no backup files were created
        backups = list(temp_workspace.glob("*.bak*"))
        assert len(backups) == 0

    def test_dry_run_zero_firestore_mutations(
        self, temp_workspace: Path, offline_db: OfflineFirestoreClient
    ):
        """Verify that dry_run=True with firestore_sync=True mutates zero Firestore documents."""
        src_path = temp_workspace / "source_fs_dry.jsonl"
        dest_path = temp_workspace / "target_fs_dry.jsonl"

        records = [{"repository": "org/repo", "number": 10, "payout_usd": 500.0}]
        SafeIO.write_jsonl(src_path, records)

        res = migrate_queues(
            source_jsonl=src_path,
            target_jsonl=dest_path,
            firestore_sync=True,
            db=offline_db,
            dry_run=True,
        )
        assert res.dry_run is True
        assert res.firestore_synced == 0

        col = offline_db.collection(COLLECTION_BOUNTY_LEADS)
        doc = col.document("org_repo_10").get()
        assert not doc.exists


# =============================================================================
# 5. OfflineFirestoreClient ACID Rollback Stress Tests
# =============================================================================


class TestOfflineFirestoreACIDRollback:
    """Stress tests multi-collection atomic rollback on mid-transaction exceptions."""

    def test_acid_rollback_multi_collection_on_exception(
        self, offline_db: OfflineFirestoreClient
    ):
        """Verify that when a transaction mutates multiple collections and fails mid-way, zero changes are persisted."""
        leads_col = offline_db.collection(COLLECTION_BOUNTY_LEADS)
        ops_col = offline_db.collection(COLLECTION_SWARM_OPERATIONS)
        settle_col = offline_db.collection(COLLECTION_BOUNTY_SETTLEMENTS)

        # Pre-seed existing state
        leads_col.document("lead_1").set({"status": "queued", "payout": 100})
        ops_col.document("op_1").set({"status": "idle"})
        settle_col.document("settle_1").set({"amount": 0})

        def failing_transaction(txn):
            # Mutate collection 1
            txn.update(leads_col.document("lead_1"), {"status": "claimed"})
            # Mutate collection 2
            txn.update(ops_col.document("op_1"), {"status": "running"})
            # Mutate collection 3
            txn.update(settle_col.document("settle_1"), {"amount": 500})
            # Add new document to collection 1
            txn.set(leads_col.document("lead_2"), {"status": "new_lead"})

            # Mid-transaction simulated catastrophic crash / network partition
            raise RuntimeError("CRITICAL: Network partition or contract revert during settlement")

        with pytest.raises(RuntimeError):
            offline_db.run_transaction(failing_transaction)

        # Verify in-memory state: EVERYTHING remains in pre-transaction state
        assert leads_col.document("lead_1").get().get("status") == "queued"
        assert ops_col.document("op_1").get().get("status") == "idle"
        assert settle_col.document("settle_1").get().get("amount") == 0
        assert not leads_col.document("lead_2").get().exists

        # Verify on-disk JSONL state: reload from disk in a fresh client instance
        fresh_client = OfflineFirestoreClient(
            project_id=offline_db.project, state_dir=offline_db.state_dir
        )
        assert fresh_client.collection(COLLECTION_BOUNTY_LEADS).document("lead_1").get().get("status") == "queued"
        assert fresh_client.collection(COLLECTION_SWARM_OPERATIONS).document("op_1").get().get("status") == "idle"
        assert fresh_client.collection(COLLECTION_BOUNTY_SETTLEMENTS).document("settle_1").get().get("amount") == 0
        assert not fresh_client.collection(COLLECTION_BOUNTY_LEADS).document("lead_2").get().exists

    def test_subsequent_transaction_succeeds_after_rollback(
        self, offline_db: OfflineFirestoreClient
    ):
        """Verify transaction engine is fully clean and re-entrant after a failed transaction."""
        col = offline_db.collection("retry_test")
        col.document("counter").set({"val": 0})

        def fail_first(txn):
            txn.update(col.document("counter"), {"val": 999})
            raise ValueError("Intentional failure")

        with pytest.raises(ValueError):
            offline_db.run_transaction(fail_first)

        def succeed_second(txn):
            snap = txn.get(col.document("counter"))
            txn.update(col.document("counter"), {"val": snap.get("val") + 1})
            return True

        res = offline_db.run_transaction(succeed_second)
        assert res is True
        assert col.document("counter").get().get("val") == 1


# =============================================================================
# 6. Batch Write Limits & 400-Item Chunking Tests
# =============================================================================


class TestOfflineFirestoreBatchLimits:
    """Stress tests batch write chunking and high volume batch commitments."""

    def test_batch_write_400_chunk_boundary_in_migration(
        self, temp_workspace: Path, offline_db: OfflineFirestoreClient
    ):
        """Verify migrate_queues correctly chunks 1,000 items into 400-item Firestore batches."""
        src_path = temp_workspace / "source_1000.jsonl"
        dest_path = temp_workspace / "dest_1000.jsonl"

        records = [
            {
                "repository": "stellar/batch-test",
                "number": i + 1,
                "title": f"Batch Item #{i+1}",
                "payout_usd": float(i + 1),
            }
            for i in range(1000)
        ]
        SafeIO.write_jsonl(src_path, records)

        res = migrate_queues(
            source_jsonl=src_path,
            target_jsonl=dest_path,
            firestore_sync=True,
            db=offline_db,
        )

        assert res.migrated_records == 1000
        assert res.firestore_synced == 1000
        assert len(res.errors) == 0

        col = offline_db.collection(COLLECTION_BOUNTY_LEADS)
        # Verify first, middle, and last documents
        assert col.document("stellar_batch_test_1").get().exists
        assert col.document("stellar_batch_test_400").get().exists
        assert col.document("stellar_batch_test_401").get().exists
        assert col.document("stellar_batch_test_800").get().exists
        assert col.document("stellar_batch_test_1000").get().exists

    def test_direct_offline_batch_large_operations(
        self, offline_db: OfflineFirestoreClient
    ):
        """Verify direct OfflineBatch handling 1,500 operations atomically."""
        col = offline_db.collection("large_batch_col")
        batch = offline_db.batch()

        for i in range(1500):
            batch.set(col.document(f"doc_{i}"), {"idx": i, "active": True})

        batch.commit()

        # Check random samples
        assert col.document("doc_0").get().get("idx") == 0
        assert col.document("doc_749").get().get("idx") == 749
        assert col.document("doc_1499").get().get("idx") == 1499


# =============================================================================
# 7. Atomic Lead Claiming Concurrency & Race Condition Tests
# =============================================================================


class TestAtomicLeadClaimingConcurrency:
    """Stress tests atomic lead claiming under concurrent multi-threaded execution."""

    def test_concurrent_claim_single_winner_baseline(
        self, offline_db: OfflineFirestoreClient
    ):
        """Verify 50 concurrent threads attempting to claim an un-contested lead."""
        col = offline_db.collection(COLLECTION_BOUNTY_LEADS)
        col.document("uncontested_lead").set({"status": "queued", "title": "Free Bounty"})

        winners = []
        num_threads = 50

        def claim_worker(w_id: str):
            if claim_lead_atomic(offline_db, "uncontested_lead", worker_id=w_id):
                winners.append(w_id)

        threads = [
            threading.Thread(target=claim_worker, args=(f"worker_{i}",))
            for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # In baseline execution, exactly 1 worker wins
        assert len(winners) == 1
        assert col.document("uncontested_lead").get().get("status") == "claimed"
        assert col.document("uncontested_lead").get().get("claimed_by") == winners[0]

    def test_concurrent_claim_expired_lock_stealing(
        self, offline_db: OfflineFirestoreClient
    ):
        """Verify that when a lock is expired (e.g. >300s old), concurrent workers can steal it, and only 1 succeeds."""
        col = offline_db.collection(COLLECTION_BOUNTY_LEADS)
        past_iso = datetime.fromtimestamp(time.time() - 600, tz=timezone.utc).isoformat()
        col.document("expired_lead").set({
            "status": "claimed",
            "claimed_by": "dead_worker",
            "lock_acquired_at": past_iso,
        })

        winners = []
        threads = [
            threading.Thread(
                target=lambda i=i: winners.append(f"w_{i}")
                if claim_lead_atomic(offline_db, "expired_lead", worker_id=f"w_{i}", lock_timeout_sec=300)
                else None
            )
            for i in range(30)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(winners) == 1
        assert col.document("expired_lead").get().get("claimed_by") == winners[0]

    def test_claim_lead_malformed_lock_timestamp_recovery(
        self, offline_db: OfflineFirestoreClient
    ):
        """Verify that malformed or non-ISO lock timestamps don't crash claim_lead_atomic."""
        col = offline_db.collection(COLLECTION_BOUNTY_LEADS)
        col.document("malformed_time_lead").set({
            "status": "claimed",
            "claimed_by": "worker_corrupt",
            "lock_acquired_at": "not-a-valid-iso-date",
        })

        # Claim attempt should proceed without crashing
        res = claim_lead_atomic(offline_db, "malformed_time_lead", worker_id="worker_new")
        # Since status is claimed and parsing timestamp failed, it updates safely or returns True
        assert isinstance(res, bool)


# =============================================================================
# 8. PathGuard & Advanced Query Stress Tests
# =============================================================================


class TestMigrateQueuesPathGuardAndQueries:
    """Stress tests PathGuard containment during migration and advanced OfflineFirestore queries."""

    def test_migrate_queues_blocks_protected_source_and_target(self, temp_workspace: Path):
        """Verify migrate_queues refuses to read from or write to protected directories."""
        from src.core.exceptions import ProtectedPathViolationError

        protected_src = Path("~/teamwork_projects/odin/intake_queue.jsonl")
        safe_target = temp_workspace / "target.jsonl"

        with pytest.raises(ProtectedPathViolationError):
            migrate_queues(source_jsonl=protected_src, target_jsonl=safe_target)

        safe_src = temp_workspace / "source.jsonl"
        SafeIO.write_jsonl(safe_src, [{"repository": "org/repo", "number": 1}])
        protected_target = Path("~/teamwork_projects/keeper_daemon/out.jsonl")

        with pytest.raises(ProtectedPathViolationError):
            migrate_queues(source_jsonl=safe_src, target_jsonl=protected_target)

    def test_offline_firestore_advanced_query_operators(
        self, offline_db: OfflineFirestoreClient
    ):
        """Verify where filter operators: array_contains, in, !=, <, <=, >, >=."""
        col = offline_db.collection("advanced_queries")
        col.document("d1").set({"tags": ["defi", "rust"], "status": "open", "val": 10})
        col.document("d2").set({"tags": ["frontend", "ts"], "status": "closed", "val": 20})
        col.document("d3").set({"tags": ["defi", "python"], "status": "open", "val": 30})
        col.document("d4").set({"tags": ["solana"], "status": "pending", "val": 40})

        # array_contains
        res_tag = col.where("tags", "array_contains", "defi").get()
        assert len(res_tag) == 2
        assert {d.id for d in res_tag} == {"d1", "d3"}

        # in operator
        res_in = col.where("status", "in", ["closed", "pending"]).get()
        assert len(res_in) == 2
        assert {d.id for d in res_in} == {"d2", "d4"}

        # != operator
        res_neq = col.where("status", "!=", "open").get()
        assert len(res_neq) == 2

        # inequality
        res_gt = col.where("val", ">=", 20).order_by("val", "ASCENDING").get()
        assert len(res_gt) == 3
        assert [d.id for d in res_gt] == ["d2", "d3", "d4"]

    def test_offline_snapshot_nested_field_path(
        self, offline_db: OfflineFirestoreClient
    ):
        """Verify snapshot.get('a.b.c') deep traversal."""
        col = offline_db.collection("nested_col")
        col.document("doc_deep").set({
            "meta": {
                "financials": {
                    "payout_usd": 1500.0,
                    "currency": "USDC",
                }
            }
        })

        snap = col.document("doc_deep").get()
        assert snap.get("meta.financials.payout_usd") == 1500.0
        assert snap.get("meta.financials.currency") == "USDC"
        assert snap.get("meta.non_existent.key") is None

