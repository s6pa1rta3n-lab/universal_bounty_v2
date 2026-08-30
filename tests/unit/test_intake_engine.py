"""
Unit Tests for IntakeEngine & Sniper Filter.
Validates platform ban enforcement (algora, polar, opire, twentyhq), subjective/KYC
disqualification, cancelled escrow rejection, currency parsing, anti-spam load balancing,
and queue sorting.
"""


import pytest

from src.core.firestore_client import OfflineFirestoreClient
from src.engines.intake_engine import (
    IntakeEngine,
    clean_text_for_financials,
    extract_financials,
    generate_canonical_doc_id,
    verify_escrow,
)


class TestFinancialExtractionAndCleanText:
    def test_clean_text_removes_promotional_noise(self):
        text = "Reward $500 for PR.\nMore funded OSS work available on platform.\nCheck out gitcoin.co/explorer with $10,000 pool."
        cleaned = clean_text_for_financials(text)
        assert "Reward $500 for PR." in cleaned
        assert "More funded OSS work available" not in cleaned
        assert "gitcoin.co/explorer" not in cleaned

    def test_extract_dollar_amounts(self):
        p_str, val = extract_financials("Complete this issue for $1,250.50 reward")
        assert val == 1250.50
        assert p_str == "$1250.50"

    def test_extract_crypto_token_amounts(self):
        p_str, val = extract_financials("Grant pool of 500 USDC allocated")
        assert val == 500.0
        assert "500.0 USDC" in p_str

        p_str_xlm, val_xlm = extract_financials("Bounty: 2,500 XLM for Soroban contract")
        assert val_xlm == 2500.0
        assert "2500.0 XLM" in p_str_xlm

    def test_multiple_amounts_returns_highest(self):
        p_str, val = extract_financials("Tier 1: $100, Tier 2: $500, Tier 3: $1,500")
        assert val == 1500.0
        assert p_str == "$1500.00"

    def test_empty_or_no_amounts(self):
        p_str, val = extract_financials("No financial reward mentioned")
        assert val == 0.0
        assert p_str == "PENDING DISCOVERY"


class TestSniperFilterAndEscrowVerification:
    def test_reject_archived_repository(self):
        node = {
            "title": "Fix bug $500",
            "body": "GrantFox bounty",
            "repository": {"nameWithOwner": "owner/repo", "isArchived": True},
        }
        valid, reason, _, _, _, _ = verify_escrow(node)
        assert valid is False
        assert reason == "REJECT_ARCHIVED_REPO"

    def test_reject_banned_platforms(self):
        for banned in ["algora", "polar", "twentyhq/twenty", "twentyhq", "opire"]:
            node = {
                "title": f"Bounty on {banned} for $500",
                "body": f"Funded via {banned}.io",
                "repository": {"nameWithOwner": f"{banned}/core", "isArchived": False},
                "labels": [{"name": banned}],
            }
            valid, reason, _, _, _, _ = verify_escrow(node)
            assert valid is False
            assert "REJECT_BANNED_PLATFORM" in reason

    def test_reject_subjective_and_kyc_keywords(self):
        for kw in ["record a video", "loom", "zoom interview", "pitch deck", "manual kyc", "figma only"]:
            node = {
                "title": f"Feature design ({kw}) $500",
                "body": f"Please {kw} as part of submission",
                "repository": {"nameWithOwner": "owner/repo", "isArchived": False},
            }
            valid, reason, _, _, _, _ = verify_escrow(node)
            assert valid is False
            assert "REJECT_SUBJECTIVE" in reason

    def test_reject_cancelled_or_refunded_escrow(self):
        node = {
            "title": "Add feature $500",
            "body": "GrantFox bounty",
            "repository": {"nameWithOwner": "owner/repo", "isArchived": False},
            "comments": [{"body": "This bounty has been cancelled by the submitter"}],
        }
        valid, reason, _, _, _, _ = verify_escrow(node)
        assert valid is False
        assert reason == "REJECT_ESCROW_CANCELLED"

    def test_qualify_valid_grantfox_stellar_bounty(self):
        node = {
            "title": "Build Soroban oracle contract ($1,000 reward)",
            "body": "GrantFox OSS funded bounty for Stellar ecosystem",
            "repository": {"nameWithOwner": "stellar-org/soroban-oracle", "isArchived": False},
            "labels": [{"name": "grantfox"}, {"name": "soroban"}],
            "comments": [],
        }
        valid, reason, p_str, val, is_high, eco = verify_escrow(node)
        assert valid is True
        assert reason == "VERIFIED_GRANTFOX_ESCROW"
        assert val == 1000.0
        assert "$1000.00" in p_str
        assert is_high is True
        assert eco == "grantfox"


class TestIntakeEngineQueueAndLoadBalancer:
    @pytest.fixture
    def intake_engine(self, tmp_path):
        db = OfflineFirestoreClient(state_dir=tmp_path / "firestore")
        seen_path = tmp_path / "seen.json"
        queue_path = tmp_path / "queue.jsonl"
        return IntakeEngine(
            db=db,
            seen_cache_path=seen_path,
            queue_file_path=queue_path,
        )

    def test_canonical_doc_id(self):
        doc_id = generate_canonical_doc_id("stellar/soroban-example", 42)
        assert doc_id == "stellar_soroban_example_42"

    def test_ingest_bounties_offline(self, intake_engine):
        issues = [
            {
                "id": "node_1",
                "number": 1,
                "title": "High value Stellar bounty ($2,000)",
                "body": "GrantFox OSS funded",
                "repository": {"nameWithOwner": "stellar/repo1", "isArchived": False},
                "labels": [{"name": "grantfox"}],
            },
            {
                "id": "node_2",
                "number": 2,
                "title": "Standard bug ($200)",
                "body": "Funded reward",
                "repository": {"nameWithOwner": "other/repo2", "isArchived": False},
                "labels": [{"name": "bounty"}],
            },
        ]

        new_leads = intake_engine.ingest_bounties(issues=issues)
        assert len(new_leads) == 2
        assert intake_engine.queue_file_path.exists()

    def test_anti_spam_load_balancer_max_concurrent_and_per_repo(self, intake_engine):
        col = intake_engine.db.collection(intake_engine.collection_name)

        # Populate 3 queued leads on same repo, 2 on different repos
        leads = [
            {"id": "r1_1", "repo": "org/repo1", "status": "queued", "priority": "high", "projected_payout_usd": 1000.0},
            {"id": "r1_2", "repo": "org/repo1", "status": "queued", "priority": "high", "projected_payout_usd": 800.0},
            {"id": "r2_1", "repo": "org/repo2", "status": "queued", "priority": "high", "projected_payout_usd": 500.0},
            {"id": "r3_1", "repo": "org/repo3", "status": "queued", "priority": "standard", "projected_payout_usd": 300.0},
            {"id": "r4_1", "repo": "org/repo4", "status": "queued", "priority": "standard", "projected_payout_usd": 200.0},
            {"id": "r5_1", "repo": "org/repo5", "status": "queued", "priority": "standard", "projected_payout_usd": 100.0},
        ]
        for lead_item in leads:
            col.document(lead_item["id"]).set(lead_item)

        # Run load balancer (max_concurrent=4, max_per_repo=1)
        promoted = intake_engine.balance_queue(max_concurrent=4, max_per_repo=1)

        # Should promote up to 4 leads across 4 distinct repos
        assert len(promoted) == 4
        promoted_repos = [p["repo"] for p in promoted]
        # Only 1 from org/repo1
        assert promoted_repos.count("org/repo1") == 1
        assert "org/repo2" in promoted_repos
        assert "org/repo3" in promoted_repos
        assert "org/repo4" in promoted_repos

        # Run again when capacity is 4 -> should promote 0
        second_run = intake_engine.balance_queue(max_concurrent=4, max_per_repo=1)
        assert len(second_run) == 0
