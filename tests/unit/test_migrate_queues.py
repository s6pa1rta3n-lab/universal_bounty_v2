"""
Unit Tests for Queue & State Migration Utility (Milestone 1).
"""

from pathlib import Path

from src.core.config import COLLECTION_BOUNTY_LEADS
from src.core.firestore_client import OfflineFirestoreClient
from src.core.safe_io import SafeIO
from src.migrate import (
    MigrationResult,
    deduplicate_leads,
    generate_canonical_doc_id,
    migrate_queues,
    normalize_lead_schema,
)


class TestLeadSchemaNormalization:
    """Verifies schema parsing, fallback inference, and canonical ID generation."""

    def test_canonical_doc_id_generation(self):
        assert (
            generate_canonical_doc_id("stellar/soroban-example", 42)
            == "stellar_soroban_example_42"
        )
        assert (
            generate_canonical_doc_id("base.org/contracts-v2", 100)
            == "base_org_contracts_v2_100"
        )

    def test_normalize_valid_lead(self):
        raw = {
            "id": "I_kwDO123",
            "number": 55,
            "title": "GrantFox Bounty ($500)",
            "url": "https://github.com/stellar/soroban-sdk/issues/55",
            "body": "Implement new auth macro",
            "repository": "stellar/soroban-sdk",
            "labels": [{"name": "grantfox"}, {"name": "bounty"}],
            "priority": "high",
            "projected_payout_usd": 500.0,
            "ecosystem": "stellar",
            "escrow_verified": True,
        }

        norm = normalize_lead_schema(raw)
        assert norm is not None
        assert norm["id"] == "stellar_soroban_sdk_55"
        assert norm["number"] == 55
        assert norm["repository"] == "stellar/soroban-sdk"
        assert norm["labels"] == ["grantfox", "bounty"]
        assert norm["priority"] == "high"
        assert norm["projected_payout_usd"] == 500.0
        assert norm["escrow_verified"] is True

    def test_normalize_legacy_nested_repo_dict(self):
        raw = {
            "num": "77",
            "title": "Fix Base Bridge",
            "html_url": "https://github.com/base-org/bridge/issues/77",
            "repository": {"nameWithOwner": "base-org/bridge"},
            "labels": ["evm", "high-priority"],
            "payout_usd": "250.5",
        }

        norm = normalize_lead_schema(raw)
        assert norm is not None
        assert norm["id"] == "base_org_bridge_77"
        assert norm["number"] == 77
        assert norm["repository"] == "base-org/bridge"
        assert norm["projected_payout_usd"] == 250.5
        assert norm["labels"] == ["evm", "high-priority"]

    def test_normalize_infer_repo_from_url(self):
        raw = {
            "url": "https://github.com/ethereum/web3.py/issues/999",
            "title": "Async provider timeout",
        }

        norm = normalize_lead_schema(raw)
        assert norm is not None
        assert norm["repository"] == "ethereum/web3.py"
        assert norm["number"] == 999
        assert norm["id"] == "ethereum_web3_py_999"

    def test_normalize_invalid_records_rejected(self):
        assert normalize_lead_schema(None) is None
        assert normalize_lead_schema("not a dict") is None
        assert normalize_lead_schema({}) is None
        assert normalize_lead_schema({"title": "No repo and no number"}) is None


class TestDeduplicationAndSorting:
    """Verifies duplicate merging and priority sorting."""

    def test_deduplicate_leads_highest_payout(self):
        lead_v1 = {
            "id": "stellar_bridge_10",
            "number": 10,
            "repository": "stellar/bridge",
            "title": "Initial Lead",
            "priority": "standard",
            "projected_payout_usd": 100.0,
            "body": "Short body",
        }
        lead_v2 = {
            "id": "stellar_bridge_10",
            "number": 10,
            "repository": "stellar/bridge",
            "title": "Updated Lead with Higher Bounty",
            "priority": "high",
            "projected_payout_usd": 500.0,
            "body": "Long detailed requirements",
        }
        lead_other = {
            "id": "base_token_20",
            "number": 20,
            "repository": "base/token",
            "title": "Other Lead",
            "priority": "high",
            "projected_payout_usd": 1000.0,
            "body": "Token info",
        }

        deduped = deduplicate_leads([lead_v1, lead_v2, lead_other])
        assert len(deduped) == 2

        # Sorted by priority (both high) then descending payout: base_token_20 ($1000), then stellar_bridge_10 ($500)
        assert deduped[0]["id"] == "base_token_20"
        assert deduped[1]["id"] == "stellar_bridge_10"
        assert deduped[1]["projected_payout_usd"] == 500.0


class TestMigrateQueuesPipeline:
    """Verifies end-to-end migrate_queues execution, backups, and Firestore syncing."""

    def test_migrate_queues_lossless_flow(
        self, temp_workspace: Path, sample_legacy_leads: list[dict]
    ):
        src_jsonl = temp_workspace / "source_intake.jsonl"
        dest_jsonl = temp_workspace / "target_intake_v2.jsonl"

        # Write sample input
        SafeIO.write_jsonl(src_jsonl, sample_legacy_leads, atomic=True)

        res = migrate_queues(
            source_jsonl=src_jsonl,
            target_jsonl=dest_jsonl,
            backup=False,
            dry_run=False,
        )

        assert isinstance(res, MigrationResult)
        assert res.total_read == 3
        assert res.valid_records == 3
        assert res.migrated_records == 3
        assert res.skipped_duplicates == 0
        assert dest_jsonl.exists()

        migrated_data = SafeIO.read_jsonl(dest_jsonl)
        assert len(migrated_data) == 3
        # First item is high priority with $1500 payout
        assert migrated_data[0]["id"] == "stellar_soroban_bridge_101"
        assert migrated_data[0]["projected_payout_usd"] == 1500.0
        # Second item is high priority with $300 payout
        assert migrated_data[1]["id"] == "base_org_web3_auth_42"
        assert migrated_data[1]["projected_payout_usd"] == 300.0

    def test_migrate_queues_backup_creation(
        self, temp_workspace: Path, sample_legacy_leads: list[dict]
    ):
        src_jsonl = temp_workspace / "queue.jsonl"
        SafeIO.write_jsonl(src_jsonl, sample_legacy_leads[:1], atomic=True)

        # Target already exists with old data
        dest_jsonl = temp_workspace / "output_queue.jsonl"
        SafeIO.write_text(dest_jsonl, '{"id": "old_data"}\n')

        res = migrate_queues(
            source_jsonl=src_jsonl,
            target_jsonl=dest_jsonl,
            backup=True,
        )

        assert res.backup_path is not None
        assert Path(res.backup_path).exists()
        assert "old_data" in Path(res.backup_path).read_text()

    def test_migrate_queues_dry_run_mode(
        self, temp_workspace: Path, sample_legacy_leads: list[dict]
    ):
        src_jsonl = temp_workspace / "source_dry.jsonl"
        dest_jsonl = temp_workspace / "dest_dry.jsonl"
        SafeIO.write_jsonl(src_jsonl, sample_legacy_leads, atomic=True)

        res = migrate_queues(
            source_jsonl=src_jsonl,
            target_jsonl=dest_jsonl,
            dry_run=True,
        )

        assert res.dry_run is True
        assert res.migrated_records == 3
        # In dry-run, target file must NOT be created
        assert dest_jsonl.exists() is False

    def test_migrate_queues_with_firestore_sync(
        self,
        temp_workspace: Path,
        sample_legacy_leads: list[dict],
        offline_db: OfflineFirestoreClient,
    ):
        src_jsonl = temp_workspace / "source_fs.jsonl"
        dest_jsonl = temp_workspace / "dest_fs.jsonl"
        SafeIO.write_jsonl(src_jsonl, sample_legacy_leads, atomic=True)

        res = migrate_queues(
            source_jsonl=src_jsonl,
            target_jsonl=dest_jsonl,
            firestore_sync=True,
            db=offline_db,
        )

        assert res.firestore_synced == 3

        # Verify Firestore collection contents
        col = offline_db.collection(COLLECTION_BOUNTY_LEADS)
        doc1 = col.document("stellar_soroban_bridge_101").get()
        assert doc1.exists is True
        assert doc1.get("projected_payout_usd") == 1500.0

        doc2 = col.document("base_org_web3_auth_42").get()
        assert doc2.exists is True
        assert doc2.get("projected_payout_usd") == 300.0
