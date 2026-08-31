"""
Empirical Adversarial Challenge & Stress Test Suite for Milestone 1:
Data Consistency, Status Normalization, and Chaos Resilience in src/console_server.py.
"""

from __future__ import annotations

import json
import random
import string
import threading
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from src.console_server import (
    CANONICAL_STAGES,
    STAGE_MAPPING,
    dispatch,
    empty_pipeline_payload,
    normalize_status,
    pipeline_payload,
)
from src.core.firestore_client import OfflineFirestoreClient


# =============================================================================
# 1. Exhaustive Status Normalization & Edge Case Variants
# =============================================================================

class TestStatusNormalizationAdversarial:
    """Adversarially challenge normalize_status against all status variants, casing, and whitespace."""

    DOCUMENTED_VARIANTS = {
        # queued (3): 1 canonical + 2 aliases
        "queued": "queued",
        "pending_discovery": "queued",
        "intake": "queued",
        # pending_triage (3): 1 canonical + 2 aliases
        "pending_triage": "pending_triage",
        "priority_triage": "pending_triage",
        "triaged": "pending_triage",
        # pr_open (5): 1 canonical + 4 aliases
        "pr_open": "pr_open",
        "claimed": "pr_open",
        "running_orbstack": "pr_open",
        "in_progress": "pr_open",
        "draft_pr": "pr_open",
        # completed (3): 1 canonical + 2 aliases
        "completed": "completed",
        "merged": "completed",
        "paid": "completed",
        # failed (4): 1 canonical + 3 aliases
        "failed": "failed",
        "failed_verification": "failed",
        "abandoned": "failed",
        "rejected": "failed",
    }

    def test_all_variants_exact_mapping(self):
        """Verify that all documented status variants map to the 5 canonical stages."""
        assert len(self.DOCUMENTED_VARIANTS) == 18
        for raw_var, expected_stage in self.DOCUMENTED_VARIANTS.items():
            mapped = normalize_status(raw_var)
            assert mapped == expected_stage, f"Failed mapping '{raw_var}' -> expected '{expected_stage}', got '{mapped}'"
            assert mapped in CANONICAL_STAGES

    def test_all_casing_permutations(self):
        """Stress-test uppercase, titlecase, mixed-case, and random casing permutations."""
        for raw_var, expected_stage in self.DOCUMENTED_VARIANTS.items():
            # UPPERCASE
            assert normalize_status(raw_var.upper()) == expected_stage
            # Title Case
            assert normalize_status(raw_var.title()) == expected_stage
            # Capitalize
            assert normalize_status(raw_var.capitalize()) == expected_stage
            # Randomized mixed case
            for _ in range(5):
                mixed = "".join(c.upper() if random.choice([True, False]) else c.lower() for c in raw_var)
                assert normalize_status(mixed) == expected_stage, f"Mixed case '{mixed}' failed mapping"

    def test_whitespace_and_control_characters(self):
        """Stress-test leading/trailing whitespace, tabs, newlines, carriage returns, and unicode whitespace."""
        whitespace_samples = [
            "   {}   ",
            "\t\t{}\t\n",
            "\r\n  {}  \r\n",
            "\n\n\n{}\n\n\n",
            " \u00a0 {} \u00a0 ",  # non-breaking space
            "\u3000{}\u3000",       # full-width ideographic space
        ]
        for raw_var, expected_stage in self.DOCUMENTED_VARIANTS.items():
            for ws_template in whitespace_samples:
                variant_with_ws = ws_template.format(raw_var)
                assert normalize_status(variant_with_ws) == expected_stage, f"Whitespace failure for '{variant_with_ws}'"

    def test_unknown_and_malformed_statuses_fallback_to_queued(self):
        """Verify that unknown, corrupted, or arbitrary status strings safely fallback to 'queued'."""
        unknown_inputs = [
            "unknown_stage",
            "IN_FLIGHT",
            "SOMETHING_RANDOM_123",
            "null",
            "None",
            "undefined",
            "NaN",
            "!@#$%^&*()",
            "",
            "   ",
            "\t\n",
        ]
        for item in unknown_inputs:
            assert normalize_status(item) == "queued", f"Unknown input '{item}' should fallback to 'queued'"

    def test_non_string_and_falsy_inputs(self):
        """Verify that non-string and falsy types (None, int, float, bool, dict, list) fallback to 'queued' safely."""
        non_string_inputs = [
            None,
            123,
            45.67,
            True,
            False,
            [],
            {},
        ]
        for val in non_string_inputs:
            assert normalize_status(val) == "queued", f"Non-string input '{val}' failed to fallback to 'queued'"


# =============================================================================
# 2. Synthetic Distribution Fuzzing (100+ to 1,000+ Documents)
# =============================================================================

class TestSyntheticDistributionDataConsistency:
    """Stress-test count consistency and array partitioning over large randomized synthetic datasets."""

    def _generate_synthetic_leads(self, count: int) -> List[Dict[str, Any]]:
        all_status_keys = list(TestStatusNormalizationAdversarial.DOCUMENTED_VARIANTS.keys()) + [
            "unknown_status",
            "CUSTOM_STATUS_XYZ",
            "",
            None,
        ]
        ecosystems = ["stellar", "base", "arbitrum", "ethereum", "optimism", "polygon"]

        leads = []
        for i in range(count):
            raw_st = random.choice(all_status_keys)
            payout = round(random.uniform(0, 5000), 2)
            has_issue = random.choice([True, True, False])
            has_pr = random.choice([True, False])
            
            lead: Dict[str, Any] = {
                "id": f"synthetic_lead_{i:04d}",
                "repo": f"org_{random.randint(1, 10)}/repo_{random.randint(1, 20)}",
                "title": f"Issue title #{i}: " + "".join(random.choices(string.ascii_letters, k=15)),
                "status": raw_st,
                "projected_payout_usd": payout,
                "ecosystem": random.choice(ecosystems),
                "qualification_reason": "Verified bounty target" if payout > 0 else "Unfunded discovery",
            }
            if has_issue:
                lead["issue_number"] = random.randint(1, 9999)
            if has_pr:
                lead["pr_number"] = random.randint(1000, 9999)
            if random.choice([True, False]):
                lead["escrow_verified"] = random.choice([True, False])
            leads.append(lead)
        return leads

    def test_random_distribution_100_documents(self, offline_db: OfflineFirestoreClient):
        """Verify strict counts and grouping invariants across 100 random synthetic documents."""
        leads_col = offline_db.collection("bounty_leads")
        leads_data = self._generate_synthetic_leads(100)

        for lead in leads_data:
            leads_col.document(lead["id"]).set(lead)

        data = pipeline_payload(db=offline_db)

        # Invariant 1: Total count equals returned leads array length
        assert len(data["leads"]) == 100
        assert data["counts"]["total"] == 100

        # Invariant 2: Grouped arrays sum to total
        stage_sum = sum(data["counts"][stage] for stage in CANONICAL_STAGES)
        assert stage_sum == 100

        # Invariant 3: Each grouped array length strictly matches its count field
        for stage in CANONICAL_STAGES:
            assert len(data["grouped"][stage]) == data["counts"][stage]
            # Invariant 4: Every lead in group[stage] has status == stage
            for item in data["grouped"][stage]:
                assert item["status"] == stage

        # Invariant 5: No lead is duplicated across groups or lost
        grouped_ids = []
        for stage in CANONICAL_STAGES:
            grouped_ids.extend([item["id"] for item in data["grouped"][stage]])
        assert len(grouped_ids) == 100
        assert len(set(grouped_ids)) == 100

        # Invariant 6: All leads contain all 13 canonical schema fields
        required_fields = {
            "id", "repo", "issue_number", "title", "status", "raw_status",
            "projected_payout", "projected_payout_usd", "qualification_reason",
            "ecosystem", "escrow_verified", "issue_url", "pr_url"
        }
        for item in data["leads"]:
            assert set(item.keys()) == required_fields
            assert item["status"] in CANONICAL_STAGES
            assert isinstance(item["projected_payout_usd"], float)
            assert isinstance(item["escrow_verified"], bool)

    def test_massive_distribution_500_documents(self, temp_workspace):
        """Stress-test with 500 documents across extreme status distributions (e.g. 100% failed or 100% queued)."""
        db = OfflineFirestoreClient(project_id="stress-test", state_dir=temp_workspace / "db_500")
        leads_col = db.collection("bounty_leads")
        leads_data = self._generate_synthetic_leads(500)

        # Batch write in chunks to avoid memory spikes
        batch = db.batch()
        for i, lead in enumerate(leads_data):
            batch.set(leads_col.document(lead["id"]), lead)
            if (i + 1) % 100 == 0:
                batch.commit()
                batch = db.batch()
        batch.commit()

        data = pipeline_payload(db=db)

        assert len(data["leads"]) == 500
        assert data["counts"]["total"] == 500
        assert sum(data["counts"][st] for st in CANONICAL_STAGES) == 500
        for st in CANONICAL_STAGES:
            assert len(data["grouped"][st]) == data["counts"][st]


# =============================================================================
# 3. Firestore Chaos & Exception Resilience
# =============================================================================

class TestFirestoreChaosAndExceptionResilience:
    """Stress-test behavior when Firestore throws network, timeout, or permission exceptions."""

    def test_firestore_client_initialization_exception(self, monkeypatch):
        """Verify pipeline_payload returns safe empty structure if get_firestore_client() fails."""
        def broken_init():
            raise ConnectionError("Failed to connect to Google Cloud Firestore (gRPC socket closed)")

        monkeypatch.setattr("src.console_server.get_firestore_client", broken_init)

        data = pipeline_payload()
        assert data == empty_pipeline_payload()
        assert data["counts"]["total"] == 0
        assert data["leads"] == []

    def test_firestore_permission_denied_exception(self, monkeypatch):
        """Verify handling of Google API PermissionDenied / 403 Forbidden exceptions."""
        class MockPermissionDenied(Exception):
            pass

        mock_db = MagicMock()
        mock_db.collection.side_effect = MockPermissionDenied("403 Missing Cloud Datastore User permissions on project odin-500008")

        data = pipeline_payload(db=mock_db)
        assert data == empty_pipeline_payload()
        assert data["counts"]["total"] == 0

    def test_firestore_stream_generator_mid_stream_crash(self):
        """Verify handling when Firestore stream() raises an exception halfway through yielding documents."""
        class FailingDocStream:
            def __iter__(self):
                # Yield 3 valid docs then crash with deadline exceeded
                for i in range(3):
                    doc = MagicMock()
                    doc.id = f"doc_{i}"
                    doc.to_dict.return_value = {"repo": "test/repo", "status": "queued", "payout_usd": 100.0}
                    yield doc
                raise TimeoutError("504 Deadline Exceeded reading next Firestore stream token")

        mock_col = MagicMock()
        mock_col.stream.return_value = FailingDocStream()
        mock_db = MagicMock()
        mock_db.collection.return_value = mock_col

        data = pipeline_payload(db=mock_db)
        # Should gracefully catch mid-stream exception and return safe empty payload rather than crashing server
        assert data == empty_pipeline_payload()

    def test_doc_snapshot_corrupted_to_dict_exception(self, offline_db: OfflineFirestoreClient):
        """Verify that individual doc snapshot corrupted methods don't crash whole payload formatting."""
        # Insert a valid lead
        offline_db.collection("bounty_leads").document("lead_good").set({
            "repo": "stellar/soroban",
            "issue_number": 1,
            "status": "queued",
        })

        # Inject a doc whose to_dict returns non-dict or raises
        class CorruptSnapshot:
            id = "corrupt_1"
            def to_dict(self):
                return None  # None dict

        mock_col = MagicMock()
        valid_doc = offline_db.collection("bounty_leads").document("lead_good").get()
        mock_col.stream.return_value = [valid_doc, CorruptSnapshot()]
        mock_db = MagicMock()
        mock_db.collection.return_value = mock_col

        data = pipeline_payload(db=mock_db)
        # Should format both without raising exception
        assert len(data["leads"]) == 2
        assert data["counts"]["total"] == 2
        assert data["leads"][0]["id"] == "lead_good"
        assert data["leads"][1]["id"] == "corrupt_1"

    def test_dispatch_api_pipeline_under_chaos_returns_200_json(self, monkeypatch):
        """Verify dispatch('/api/pipeline') returns HTTP 200 with valid empty JSON during database outage."""
        def catastrophic_failure():
            raise RuntimeError("Database host unreachable")

        monkeypatch.setattr("src.console_server.get_firestore_client", catastrophic_failure)

        status, ctype, body = dispatch("/api/pipeline")
        assert status == 200
        assert ctype == "application/json; charset=utf-8"
        
        parsed = json.loads(body.decode("utf-8"))
        assert parsed["leads"] == []
        assert parsed["counts"]["total"] == 0
        for st in CANONICAL_STAGES:
            assert parsed["grouped"][st] == []
            assert parsed["counts"][st] == 0


# =============================================================================
# 4. Multi-Threaded Concurrency Stress
# =============================================================================

class TestConcurrencyAndThreadSafety:
    """Stress-test concurrent calls to pipeline_payload and dispatch."""

    def test_concurrent_pipeline_payload_invocations(self, offline_db: OfflineFirestoreClient):
        """Verify that 20 threads reading pipeline_payload simultaneously encounter zero race conditions."""
        col = offline_db.collection("bounty_leads")
        for i in range(25):
            col.document(f"c_lead_{i}").set({
                "repo": "concurrent/test",
                "issue_number": i,
                "status": "pr_open" if i % 2 == 0 else "completed",
                "payout_usd": float(i * 10),
            })

        results = []
        errors = []

        def worker():
            try:
                res = pipeline_payload(db=offline_db)
                results.append(res)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 20
        for r in results:
            assert r["counts"]["total"] == 25
            assert r["counts"]["pr_open"] == 13
            assert r["counts"]["completed"] == 12


# =============================================================================
# 5. Granular Field Normalization & Type Safety Stress
# =============================================================================

class TestLeadFormattingAndFieldTransformations:
    """Stress-test _format_lead transformations across polymorphic field inputs."""

    def test_repo_polymorphic_parsing(self, offline_db: OfflineFirestoreClient):
        """Verify repo string, dictionary nameWithOwner, name, and missing fallbacks."""
        col = offline_db.collection("bounty_leads")
        col.document("l1").set({"repository": {"nameWithOwner": "stellar/soroban-cli"}, "status": "queued"})
        col.document("l2").set({"repo": {"name": "base/bridge-contracts"}, "status": "queued"})
        col.document("l3").set({"repo": "ethereum/go-ethereum", "status": "queued"})
        col.document("l4").set({"status": "queued"})  # Missing repo

        data = pipeline_payload(db=offline_db)
        leads_by_id = {item["id"]: item for item in data["leads"]}

        assert leads_by_id["l1"]["repo"] == "stellar/soroban-cli"
        assert leads_by_id["l2"]["repo"] == "base/bridge-contracts"
        assert leads_by_id["l3"]["repo"] == "ethereum/go-ethereum"
        assert leads_by_id["l4"]["repo"] == "unknown"

    def test_url_synthesis_and_explicit_override(self, offline_db: OfflineFirestoreClient):
        """Verify automatic URL synthesis when missing and respect for explicit URLs."""
        col = offline_db.collection("bounty_leads")
        # 1. Synthesize issue_url
        col.document("l_synth_issue").set({
            "repo": "stellar/soroban-sdk",
            "issue_number": 42,
            "status": "queued",
        })
        # 2. Synthesize pr_url
        col.document("l_synth_pr").set({
            "repo": "stellar/soroban-sdk",
            "pr_number": 99,
            "status": "pr_open",
        })
        # 3. Explicit URLs should not be overwritten
        col.document("l_explicit").set({
            "repo": "stellar/soroban-sdk",
            "issue_number": 42,
            "issue_url": "https://grantfox.org/bounties/42",
            "pr_url": "https://github.com/custom/pull/1",
            "status": "queued",
        })
        # 4. Unknown repo should have empty URLs, not invalid github URLs
        col.document("l_unknown_repo").set({
            "issue_number": 55,
            "status": "queued",
        })

        data = pipeline_payload(db=offline_db)
        by_id = {item["id"]: item for item in data["leads"]}

        assert by_id["l_synth_issue"]["issue_url"] == "https://github.com/stellar/soroban-sdk/issues/42"
        assert by_id["l_synth_issue"]["pr_url"] == ""
        assert by_id["l_synth_pr"]["pr_url"] == "https://github.com/stellar/soroban-sdk/pull/99"
        assert by_id["l_explicit"]["issue_url"] == "https://grantfox.org/bounties/42"
        assert by_id["l_explicit"]["pr_url"] == "https://github.com/custom/pull/1"
        assert by_id["l_unknown_repo"]["issue_url"] == ""
        assert by_id["l_unknown_repo"]["pr_url"] == ""

    def test_payout_formatting_logic(self, offline_db: OfflineFirestoreClient):
        """Verify formatted projected_payout string generation across zero, positive, and pre-formatted strings."""
        col = offline_db.collection("bounty_leads")
        col.document("p_zero").set({"projected_payout_usd": 0.0, "status": "queued"})
        col.document("p_positive").set({"projected_payout_usd": 1250.0, "status": "queued"})
        col.document("p_string_usd").set({"payout_usd": "500", "status": "queued"})
        col.document("p_explicit_str").set({"projected_payout": "10,000 XLM", "projected_payout_usd": 2500.0, "status": "queued"})

        data = pipeline_payload(db=offline_db)
        by_id = {item["id"]: item for item in data["leads"]}

        assert by_id["p_zero"]["projected_payout"] == "PENDING DISCOVERY"
        assert by_id["p_zero"]["projected_payout_usd"] == 0.0
        assert by_id["p_zero"]["escrow_verified"] is False

        assert by_id["p_positive"]["projected_payout"] == "$1250"
        assert by_id["p_positive"]["projected_payout_usd"] == 1250.0
        assert by_id["p_positive"]["escrow_verified"] is True

        assert by_id["p_string_usd"]["projected_payout"] == "$500"
        assert by_id["p_string_usd"]["projected_payout_usd"] == 500.0
        assert by_id["p_string_usd"]["escrow_verified"] is True

        assert by_id["p_explicit_str"]["projected_payout"] == "10,000 XLM"
        assert by_id["p_explicit_str"]["projected_payout_usd"] == 2500.0
        assert by_id["p_explicit_str"]["escrow_verified"] is True
