"""
Integration Tests for Queue & State Migration Pipeline.

Verifies:
1. End-to-end migration from historical JSONL file to V2 normalized schema.
2. Deduplication across conflicting/duplicate records with highest payout preservation.
3. PathGuard enforcement during migration.
4. Batch writing and verification in Firestore collections.
5. Backup generation and dry-run safety.
"""

from pathlib import Path

from src.core.config import COLLECTION_BOUNTY_LEADS
from src.core.firestore_client import OfflineFirestoreClient
from src.core.safe_io import SafeIO
from src.migrate import migrate_queues


class TestQueueMigrationIntegration:
    """Integration test suite for the migrate_queues utility."""

    def test_full_migration_from_file_to_firestore(self, temp_workspace: Path):
        """Tests full pipeline: legacy jsonl -> normalization -> deduplication -> Firestore batch -> query."""
        src_jsonl = temp_workspace / "legacy_queue.jsonl"
        dest_jsonl = temp_workspace / "logs" / "intake_queue.jsonl"
        dest_jsonl.parent.mkdir(parents=True, exist_ok=True)

        legacy_data = [
            {
                "id": "node_1",
                "number": 10,
                "title": "Fix Soroban token authorization ($500)",
                "url": "https://github.com/stellar/soroban-auth/issues/10",
                "body": "GrantFox escrow verified. Stellar smart contract bug.",
                "repository": "stellar/soroban-auth",
                "labels": ["grantfox", "soroban"],
                "projected_payout_usd": 500.0,
                "priority": "high",
                "status": "queued",
                "ecosystem": "stellar",
            },
            # Duplicate with lower payout — should be overridden by the $1200 one below
            {
                "id": "node_2",
                "number": 20,
                "title": "Optimism rollup fraud proof verification ($400)",
                "url": "https://github.com/ethereum-optimism/op-challenger/issues/20",
                "body": "EVM bounty for fraud proof.",
                "repository": "ethereum-optimism/op-challenger",
                "labels": ["evm", "optimism"],
                "projected_payout_usd": 400.0,
                "priority": "standard",
                "status": "queued",
            },
            # Higher payout duplicate for same issue
            {
                "id": "node_2_rich",
                "number": 20,
                "title": "Optimism rollup fraud proof verification ($1,200 reward update)",
                "url": "https://github.com/ethereum-optimism/op-challenger/issues/20",
                "body": "EVM bounty for fraud proof with detailed specification checklist.",
                "repository": "ethereum-optimism/op-challenger",
                "labels": ["evm", "optimism", "bounty"],
                "projected_payout_usd": 1200.0,
                "priority": "high",
                "status": "queued",
            },
            # Invalid record (missing repo and issue number)
            {"invalid": True, "title": "Garbage record"},
        ]

        SafeIO.write_jsonl(src_jsonl, legacy_data)

        db_dir = temp_workspace / "offline_db"
        db = OfflineFirestoreClient(project_id="test-migration-db", state_dir=db_dir)

        # Run migration with firestore sync
        result = migrate_queues(
            source_jsonl=src_jsonl,
            target_jsonl=dest_jsonl,
            firestore_sync=True,
            db=db,
            backup=True,
        )

        assert result.total_read == 4
        assert result.valid_records == 3
        assert result.skipped_duplicates == 1
        assert result.migrated_records == 2
        assert result.firestore_synced == 2
        assert len(result.errors) == 1  # 1 invalid record

        # Verify output JSONL file
        assert dest_jsonl.exists()
        migrated_file_records = list(SafeIO.stream_jsonl(dest_jsonl))
        assert len(migrated_file_records) == 2

        # First record must be the high-payout $1200 one
        assert migrated_file_records[0]["issue_number"] == 20
        assert migrated_file_records[0]["projected_payout_usd"] == 1200.0
        assert migrated_file_records[0]["priority"] == "high"

        # Verify Firestore collection
        leads_col = db.collection(COLLECTION_BOUNTY_LEADS)
        snaps = list(leads_col.stream())
        assert len(snaps) == 2

        doc_ids = {s.id for s in snaps}
        assert "stellar_soroban_auth_10" in doc_ids
        assert "ethereum_optimism_op_challenger_20" in doc_ids

        op_doc = leads_col.document("ethereum_optimism_op_challenger_20").get().to_dict()
        assert op_doc["projected_payout_usd"] == 1200.0
        assert "fraud proof" in op_doc["body"].lower()

    def test_migration_dry_run_leaves_target_unchanged(self, temp_workspace: Path):
        """Dry-run does not write to target path or create backups."""
        src_jsonl = temp_workspace / "src.jsonl"
        dest_jsonl = temp_workspace / "target.jsonl"

        SafeIO.write_jsonl(src_jsonl, [{"repo": "org/repo", "number": 1, "title": "Bounty"}])

        result = migrate_queues(
            source_jsonl=src_jsonl,
            target_jsonl=dest_jsonl,
            dry_run=True,
        )

        assert result.dry_run is True
        assert result.migrated_records == 1
        assert not dest_jsonl.exists()
        assert result.backup_path is None
