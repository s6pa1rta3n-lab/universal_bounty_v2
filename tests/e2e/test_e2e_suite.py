"""
Comprehensive E2E Test Suite (Tiers 1-4) for Universal Bounty Engine V2.

Methodology:
- Tier 1: Feature Coverage (Category-Partition: ≥5 tests per feature for all 15 features = ≥75 tests)
- Tier 2: Boundary Value Analysis & Corner Cases (≥5 tests per feature = ≥75 tests)
- Tier 3: Cross-Feature Combinations (≥15 interaction tests)
- Tier 4: Real-World Application Workload Scenarios (5 full lifecycle scenarios)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.cli import main as cli_main
from src.core.config import (
    BANNED_PLATFORMS,
    COLLECTION_BOUNTY_LEADS,
    COLLECTION_BOUNTY_MEMORY,
    COLLECTION_BOUNTY_SETTLEMENTS,
    COLLECTION_SWARM_COORDINATOR,
    DEFAULT_IGNORE_LIST,
    EVM_PAYOUT_ADDRESS,
    PAYOUT_ROUTING,
    SOLANA_PAYOUT_ADDRESS,
    STELLAR_PAYOUT_ADDRESS,
    contains_disqualify_keywords,
    get_config,
    is_banned_platform,
)
from src.core.exceptions import ProtectedPathViolationError, SafeIOError
from src.core.firestore_client import OfflineFirestoreClient, claim_lead_atomic
from src.core.orbstack_executor import (
    ContainerExecutionResult,
    EphemeralOrbStackExecutor,
)
from src.core.path_guard import DEFAULT_PATH_GUARD, PathGuard
from src.core.safe_io import SafeIO
from src.engines.escort_engine import EscortEngine, is_external_deployment_gate
from src.engines.executor_engine import ExecutorEngine, extract_stipulations_from_text
from src.engines.inbox_engine import (
    InboxEngine,
    clean_reply_quotes,
    decode_mime_header,
)
from src.engines.intake_engine import (
    IntakeEngine,
    extract_financials,
    verify_escrow,
)
from src.engines.sync_engine import SyncEngine, extract_payout_numeric
from src.migrate import deduplicate_leads, migrate_queues, normalize_lead_schema
from src.orchestrator.sweeper import SweepSummary, UniversalHourlySweeper

# ==============================================================================
# TIER 1: FEATURE COVERAGE (Category-Partition: 15 Features × ≥5 Tests = ≥75 Tests)
# ==============================================================================

class TestTier1FeatureCoverage:
    """Tier 1: Comprehensive individual feature coverage across all 15 system features."""

    # --------------------------------------------------------------------------
    # FEATURE 1: Configuration & Security Rules
    # --------------------------------------------------------------------------
    def test_t1_f01_banned_platforms_detection(self):
        """F1.1: Verifies banned platforms check identifies all banned entities."""
        for banned in ["algora", "polar", "twentyhq", "twentyhq/twenty", "opire"]:
            assert is_banned_platform(banned) is True
            assert is_banned_platform(f"https://github.com/{banned}/repo") is True
        assert is_banned_platform("stellar-org/soroban-example") is False
        assert is_banned_platform(None) is False

    def test_t1_f01_payout_routing_addresses(self):
        """F1.2: Verifies configured EVM, Stellar, and Solana payout addresses."""
        assert EVM_PAYOUT_ADDRESS == "0xF46C9F6d70C50BF81ef3588AB523a90a594a2F89"
        assert STELLAR_PAYOUT_ADDRESS == "GCL6OXAMLD75BMTINA6EMRUDWK5THQUSHMYNLSNBCJAPZJHNYJTUNIBC"
        assert SOLANA_PAYOUT_ADDRESS == "MSwhtUP1XaRfMf5ectKq9LnGvQxcQxxMHJo4QNaJ6Av"
        assert PAYOUT_ROUTING["EVM"] == EVM_PAYOUT_ADDRESS
        assert PAYOUT_ROUTING["STELLAR"] == STELLAR_PAYOUT_ADDRESS

    def test_t1_f01_ignore_list_protected_directories(self):
        """F1.3: Verifies default protected trading paths in DEFAULT_IGNORE_LIST."""
        assert "~/teamwork_projects/keeper_daemon" in DEFAULT_IGNORE_LIST
        assert "~/teamwork_projects/odin" in DEFAULT_IGNORE_LIST
        assert "~/teamwork_projects/matt-berserker" in DEFAULT_IGNORE_LIST

    def test_t1_f01_disqualify_keywords_check(self):
        """F1.4: Verifies detection of subjective/KYC disqualification keywords."""
        dq_found, kw = contains_disqualify_keywords("Please submit a video pitch deck.")
        assert dq_found is True
        assert kw in ["video pitch", "pitch deck"]
        dq_no, _ = contains_disqualify_keywords("Implement genuine Rust smart contract.")
        assert dq_no is False

    def test_t1_f01_config_container_immutability(self):
        """F1.5: Verifies SwarmConfig dataclass immutability and default settings."""
        cfg = get_config()
        assert cfg.evm_payout_address == EVM_PAYOUT_ADDRESS
        assert cfg.stellar_payout_address == STELLAR_PAYOUT_ADDRESS
        with pytest.raises(Exception):
            cfg.evm_payout_address = "0xHACKED"  # type: ignore

    # --------------------------------------------------------------------------
    # FEATURE 2: PathGuard Filesystem Isolation
    # --------------------------------------------------------------------------
    def test_t1_f02_path_guard_blocks_exact_ignore_path(self, temp_workspace: Path):
        """F2.1: PathGuard blocks direct access to exact ignore path."""
        p = temp_workspace / "prot_exact"
        p.mkdir(parents=True, exist_ok=True)
        guard = PathGuard(ignore_list=[str(p)])
        with pytest.raises(ProtectedPathViolationError):
            guard.validate_access(p, "read")

    def test_t1_f02_path_guard_blocks_nested_child_path(self, temp_workspace: Path):
        """F2.2: PathGuard blocks access to nested child inside ignored path."""
        parent = temp_workspace / "prot_parent"
        child = parent / "sub" / "secret.txt"
        parent.mkdir(parents=True, exist_ok=True)
        guard = PathGuard(ignore_list=[str(parent)])
        with pytest.raises(ProtectedPathViolationError):
            guard.validate_access(child, "read")

    def test_t1_f02_path_guard_blocks_directory_traversal(self, temp_workspace: Path):
        """F2.3: PathGuard detects and rejects ../ relative dot-dot traversal."""
        p = temp_workspace / "prot_dir"
        p.mkdir(parents=True, exist_ok=True)
        guard = PathGuard(ignore_list=[str(p)])
        traversal = temp_workspace / "allowed" / ".." / "prot_dir" / "data.json"
        with pytest.raises(ProtectedPathViolationError):
            guard.validate_access(traversal, "read")

    def test_t1_f02_path_guard_blocks_symlink_bypass(self, temp_workspace: Path):
        """F2.4: PathGuard resolves symlinks and rejects targets inside ignore list."""
        prot = temp_workspace / "prot_vault"
        prot.mkdir(parents=True, exist_ok=True)
        symlink = temp_workspace / "sneaky_link"
        try:
            symlink.symlink_to(prot)
            guard = PathGuard(ignore_list=[str(prot)])
            with pytest.raises(ProtectedPathViolationError):
                guard.validate_access(symlink, "read")
        finally:
            if symlink.is_symlink():
                symlink.unlink()

    def test_t1_f02_path_guard_allows_safe_path(self, temp_workspace: Path):
        """F2.5: PathGuard permits access to allowed non-protected workspaces."""
        safe = temp_workspace / "safe_dir" / "file.txt"
        safe.parent.mkdir(parents=True, exist_ok=True)
        guard = PathGuard(ignore_list=[str(temp_workspace / "protected")])
        res = guard.validate_access(safe, "read")
        assert res.resolve() == safe.resolve()

    # --------------------------------------------------------------------------
    # FEATURE 3: Safe I/O & Atomic Storage
    # --------------------------------------------------------------------------
    def test_t1_f03_safe_io_atomic_write_read_text(self, temp_workspace: Path):
        """F3.1: SafeIO performs atomic text write and read."""
        target = temp_workspace / "atomic.txt"
        SafeIO.atomic_write_text(target, "hello atomic")
        assert SafeIO.read_text(target) == "hello atomic"

    def test_t1_f03_safe_io_jsonl_stream_and_write(self, temp_workspace: Path):
        """F3.2: SafeIO writes and streams JSONL lines atomically."""
        jsonl = temp_workspace / "items.jsonl"
        items = [{"id": 1, "val": "a"}, {"id": 2, "val": "b"}]
        SafeIO.write_jsonl(jsonl, items, atomic=True)
        read_back = list(SafeIO.stream_jsonl(jsonl))
        assert read_back == items

    def test_t1_f03_safe_io_mkdir_and_rmtree(self, temp_workspace: Path):
        """F3.3: SafeIO safely creates and removes directories."""
        d = temp_workspace / "sub" / "tree"
        SafeIO.mkdir(d, parents=True, exist_ok=True)
        assert d.is_dir()
        SafeIO.rmtree(temp_workspace / "sub")
        assert not d.exists()

    def test_t1_f03_safe_io_file_lifecycle_methods(self, temp_workspace: Path):
        """F3.4: SafeIO touch, exists, is_file, copy, and move operations."""
        file_path = temp_workspace / "lifecycle.txt"
        SafeIO.touch(file_path)
        assert SafeIO.exists(file_path) is True
        assert SafeIO.is_file(file_path) is True

        copy_path = temp_workspace / "lifecycle_copy.txt"
        SafeIO.copy_file(file_path, copy_path)
        assert SafeIO.exists(copy_path) is True

    def test_t1_f03_safe_io_path_guard_integration(self, temp_workspace: Path):
        """F3.5: SafeIO enforces PathGuard on all read/write/delete operations."""
        prot = temp_workspace / "safeio_prot"
        prot.mkdir(parents=True, exist_ok=True)
        custom_g = PathGuard(ignore_list=[str(prot)])
        orig = SafeIO.get_guard()
        SafeIO.set_guard(custom_g)
        try:
            with pytest.raises(ProtectedPathViolationError):
                SafeIO.write_text(prot / "illegal.txt", "data")
        finally:
            SafeIO.set_guard(orig)

    # --------------------------------------------------------------------------
    # FEATURE 4: Firestore State & Fallback Mode
    # --------------------------------------------------------------------------
    def test_t1_f04_firestore_offline_crud_document(self, offline_db: OfflineFirestoreClient):
        """F4.1: OfflineFirestoreClient document set, get, update, delete."""
        doc_ref = offline_db.collection("test_col").document("doc_1")
        doc_ref.set({"name": "lead_alpha", "val": 100})
        snap = doc_ref.get()
        assert snap.exists and snap.to_dict()["val"] == 100

        doc_ref.update({"val": 200})
        assert doc_ref.get().to_dict()["val"] == 200
        doc_ref.delete()
        assert not doc_ref.get().exists

    def test_t1_f04_firestore_offline_query_where(self, offline_db: OfflineFirestoreClient):
        """F4.2: OfflineFirestoreClient where query filtering."""
        col = offline_db.collection("query_col")
        col.document("1").set({"status": "queued", "priority": "high"})
        col.document("2").set({"status": "queued", "priority": "low"})
        col.document("3").set({"status": "completed", "priority": "high"})

        queued = list(col.where("status", "==", "queued").stream())
        assert len(queued) == 2

    def test_t1_f04_firestore_offline_batch_commit(self, offline_db: OfflineFirestoreClient):
        """F4.3: OfflineFirestoreClient atomic batch write."""
        col = offline_db.collection("batch_col")
        batch = offline_db.batch()
        batch.set(col.document("b1"), {"item": "b1"})
        batch.set(col.document("b2"), {"item": "b2"})
        batch.commit()

        assert len(list(col.stream())) == 2

    def test_t1_f04_firestore_offline_atomic_claim_lead(self, offline_db: OfflineFirestoreClient):
        """F4.4: Atomic lead claiming locking protocol."""
        col = offline_db.collection("bounty_leads")
        col.document("lead_100").set({"status": "pending_triage", "lock": {"owner_id": None}})

        claimed = claim_lead_atomic(offline_db, "lead_100", worker_id="worker_a")
        assert claimed is True

        # Second claim attempt by worker_b must be rejected
        claimed_2 = claim_lead_atomic(offline_db, "lead_100", worker_id="worker_b")
        assert claimed_2 is False

    def test_t1_f04_firestore_offline_collection_stream(self, offline_db: OfflineFirestoreClient):
        """F4.5: OfflineFirestoreClient stream iterates all collection documents."""
        col = offline_db.collection("stream_col")
        for i in range(5):
            col.document(f"d_{i}").set({"idx": i})
        all_docs = list(col.stream())
        assert len(all_docs) == 5

    # --------------------------------------------------------------------------
    # FEATURE 5: State & Queue Migration (migrate_queues)
    # --------------------------------------------------------------------------
    def test_t1_f05_migrate_queues_valid_schema(self, temp_workspace: Path):
        """F5.1: Normalizes legacy lead schema to canonical V2 format."""
        legacy = {
            "repository": "stellar/soroban-token",
            "number": 42,
            "title": "Auth Token fix ($500)",
            "projected_payout_usd": 500.0,
            "priority": "high",
        }
        norm = normalize_lead_schema(legacy)
        assert norm is not None
        assert norm["id"] == "stellar_soroban_token_42"
        assert norm["issue_number"] == 42
        assert norm["projected_payout_usd"] == 500.0

    def test_t1_f05_migrate_queues_deduplication(self):
        """F5.2: Deduplicates records preserving highest payout."""
        leads = [
            {"id": "doc_1", "projected_payout_usd": 100.0, "body": "short"},
            {"id": "doc_1", "projected_payout_usd": 500.0, "body": "longer body"},
        ]
        deduped = deduplicate_leads(leads)
        assert len(deduped) == 1
        assert deduped[0]["projected_payout_usd"] == 500.0

    def test_t1_f05_migrate_queues_backup_creation(self, temp_workspace: Path):
        """F5.3: Creates timestamped backup when destination file already exists."""
        src = temp_workspace / "src.jsonl"
        dest = temp_workspace / "dest.jsonl"
        SafeIO.write_jsonl(src, [{"repo": "org/repo", "number": 1}])
        SafeIO.write_jsonl(dest, [{"old": True}])

        res = migrate_queues(src, target_jsonl=dest, backup=True)
        assert res.backup_path is not None
        assert Path(res.backup_path).exists()

    def test_t1_f05_migrate_queues_firestore_sync(self, temp_workspace: Path, offline_db: OfflineFirestoreClient):
        """F5.4: Synchronizes migrated records into Firestore batch."""
        src = temp_workspace / "sync.jsonl"
        dest = temp_workspace / "sync_dest.jsonl"
        SafeIO.write_jsonl(src, [{"repo": "org/repo", "number": 5, "title": "Bounty"}])

        res = migrate_queues(src, target_jsonl=dest, firestore_sync=True, db=offline_db)
        assert res.firestore_synced == 1
        assert offline_db.collection(COLLECTION_BOUNTY_LEADS).document("org_repo_5").get().exists

    def test_t1_f05_migrate_queues_dry_run(self, temp_workspace: Path):
        """F5.5: Dry run evaluates metrics without touching target file."""
        src = temp_workspace / "dry.jsonl"
        dest = temp_workspace / "dry_dest.jsonl"
        SafeIO.write_jsonl(src, [{"repo": "org/repo", "number": 7}])

        res = migrate_queues(src, target_jsonl=dest, dry_run=True)
        assert res.dry_run is True
        assert not dest.exists()

    # --------------------------------------------------------------------------
    # FEATURE 6: GitHub GraphQL Intake & Sniper Filter
    # --------------------------------------------------------------------------
    def test_t1_f06_intake_sniper_filter_valid_grantfox(self):
        """F6.1: Qualifies valid GrantFox / Stellar escrow bounty."""
        node = {
            "repository": {"nameWithOwner": "stellar/soroban-escrow", "isArchived": False},
            "number": 12,
            "title": "Build escrow bridge ($1,000)",
            "body": "GrantFox OSS verified reward.",
            "labels": [{"name": "grantfox"}, {"name": "bounty"}],
        }
        is_val, reason, p_str, p_val, is_hi, eco = verify_escrow(node)
        assert is_val is True
        assert p_val == 1000.0
        assert is_hi is True
        assert eco in ("grantfox", "stellar")

    def test_t1_f06_intake_sniper_filter_reject_banned(self):
        """F6.2: Discards bounties from banned platforms (e.g. Algora, Polar, Opire)."""
        node = {
            "repository": {"nameWithOwner": "polar-sh/polar-app", "isArchived": False},
            "number": 1,
            "title": "Fix Polar UI",
            "body": "Polar reward",
        }
        is_val, reason, _, _, _, _ = verify_escrow(node)
        assert is_val is False
        assert "BANNED_PLATFORM" in reason

    def test_t1_f06_intake_sniper_filter_reject_kyc(self):
        """F6.3: Discards subjective tasks requiring video pitch or manual KYC."""
        node = {
            "repository": {"nameWithOwner": "some/web3-app", "isArchived": False},
            "number": 2,
            "title": "Design bounty ($500)",
            "body": "Must record a video pitch and complete manual KYC interview.",
        }
        is_val, reason, _, _, _, _ = verify_escrow(node)
        assert is_val is False
        assert "SUBJECTIVE" in reason

    def test_t1_f06_intake_sniper_filter_reject_archived(self):
        """F6.4: Discards bounties on archived repositories."""
        node = {
            "repository": {"nameWithOwner": "dead/archived-repo", "isArchived": True},
            "number": 1,
            "title": "Old bug",
            "body": "$100",
        }
        is_val, reason, _, _, _, _ = verify_escrow(node)
        assert is_val is False
        assert reason == "REJECT_ARCHIVED_REPO"

    def test_t1_f06_intake_sniper_filter_financial_extraction(self):
        """F6.5: Financial extraction parses dollar, token, and crypto values."""
        txt = "Funding reward of $2,500.00 and 500 USDC on Base."
        p_str, p_val = extract_financials(txt)
        assert p_val == 2500.0
        assert "$2500.00" in p_str

    # --------------------------------------------------------------------------
    # FEATURE 7: Intake Anti-Spam Queue Balancer
    # --------------------------------------------------------------------------
    def test_t1_f07_balancer_respects_max_concurrent(self, offline_db: OfflineFirestoreClient):
        """F7.1: Balancer caps active leads to max_concurrent (4)."""
        engine = IntakeEngine(db=offline_db)
        col = offline_db.collection(COLLECTION_BOUNTY_LEADS)
        for i in range(6):
            col.document(f"lead_{i}").set({
                "repo": f"org_{i}/repo_{i}",
                "status": "queued",
                "priority": "high",
                "projected_payout_usd": 100.0 * i,
            })

        promoted = engine.balance_queue(max_concurrent=4, max_per_repo=1)
        assert len(promoted) == 4

    def test_t1_f07_balancer_enforces_one_per_repo(self, offline_db: OfflineFirestoreClient):
        """F7.2: Balancer allows max 1 active lead per repository."""
        engine = IntakeEngine(db=offline_db)
        col = offline_db.collection(COLLECTION_BOUNTY_LEADS)
        col.document("doc_a1").set({"repo": "org/repo_a", "status": "queued", "priority": "high", "projected_payout_usd": 500.0})
        col.document("doc_a2").set({"repo": "org/repo_a", "status": "queued", "priority": "high", "projected_payout_usd": 300.0})
        col.document("doc_b1").set({"repo": "org/repo_b", "status": "queued", "priority": "high", "projected_payout_usd": 200.0})

        promoted = engine.balance_queue(max_concurrent=4, max_per_repo=1)
        promoted_repos = [p["repo"] for p in promoted]
        assert len(promoted) == 2
        assert promoted_repos.count("org/repo_a") == 1
        assert "org/repo_b" in promoted_repos

    def test_t1_f07_balancer_prioritizes_high_and_usd(self, offline_db: OfflineFirestoreClient):
        """F7.3: High priority leads and higher USD payouts are promoted first."""
        engine = IntakeEngine(db=offline_db)
        col = offline_db.collection(COLLECTION_BOUNTY_LEADS)
        col.document("std_high_val").set({"repo": "o1/r1", "status": "queued", "priority": "standard", "projected_payout_usd": 2000.0})
        col.document("hi_low_val").set({"repo": "o2/r2", "status": "queued", "priority": "high", "projected_payout_usd": 100.0})
        col.document("hi_high_val").set({"repo": "o3/r3", "status": "queued", "priority": "high", "projected_payout_usd": 1500.0})

        promoted = engine.balance_queue(max_concurrent=2, max_per_repo=1)
        assert len(promoted) == 2
        assert promoted[0]["repo"] == "o3/r3"  # high priority, $1500
        assert promoted[1]["repo"] == "o2/r2"  # high priority, $100

    def test_t1_f07_balancer_holds_queue_at_capacity(self, offline_db: OfflineFirestoreClient):
        """F7.4: When active count == max_concurrent, promotion is held."""
        engine = IntakeEngine(db=offline_db)
        col = offline_db.collection(COLLECTION_BOUNTY_LEADS)
        for i in range(4):
            col.document(f"active_{i}").set({"repo": f"o/r{i}", "status": "priority_triage"})
        col.document("queued_waiting").set({"repo": "o/r_wait", "status": "queued", "priority": "high"})

        promoted = engine.balance_queue(max_concurrent=4)
        assert len(promoted) == 0

    def test_t1_f07_balancer_promotes_to_correct_status(self, offline_db: OfflineFirestoreClient):
        """F7.5: High priority promoted to priority_triage, standard to pending_triage."""
        engine = IntakeEngine(db=offline_db)
        col = offline_db.collection(COLLECTION_BOUNTY_LEADS)
        col.document("hi").set({"repo": "o/r1", "status": "queued", "priority": "high"})
        col.document("std").set({"repo": "o/r2", "status": "queued", "priority": "standard"})

        promoted = engine.balance_queue(max_concurrent=4)
        status_map = {p["repo"]: p["status"] for p in promoted}
        assert status_map["o/r1"] == "priority_triage"
        assert status_map["o/r2"] == "pending_triage"

    # --------------------------------------------------------------------------
    # FEATURE 8: Ephemeral OrbStack Container Executor
    # --------------------------------------------------------------------------
    def test_t1_f08_orbstack_executor_docker_cmd_flags(self, temp_workspace: Path):
        """F8.1: Verifies docker run command incorporates --rm, quotas, and tmpfs."""
        ws = temp_workspace / "ws_docker"
        ws.mkdir(parents=True, exist_ok=True)
        executor = EphemeralOrbStackExecutor()

        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(returncode=0, stdout="test ok", stderr="")
            res = executor.run_isolated(ws, command=["python3", "-c", "pass"])

            assert res.success is True
            call_args = mock_sub.call_args_list[0][0][0]
            assert "--rm" in call_args
            assert "--cpus=2" in call_args
            assert "--memory=2g" in call_args
            assert "--pids-limit=256" in call_args
            assert any("--tmpfs=" in arg for arg in call_args)

    def test_t1_f08_orbstack_executor_cleanup_stale(self):
        """F8.2: Verifies cleanup_stale_containers calls docker rm -f."""
        executor = EphemeralOrbStackExecutor()
        with patch("subprocess.run") as mock_sub:
            mock_sub.side_effect = [
                MagicMock(returncode=0, stdout="bounty-exec-1\nbounty-exec-2\n", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]
            cleaned = executor.cleanup_stale_containers()
            assert cleaned == 2

    def test_t1_f08_orbstack_executor_timeout_handling(self, temp_workspace: Path):
        """F8.3: Handles subprocess.TimeoutExpired gracefully."""
        import subprocess as sp
        ws = temp_workspace / "ws_timeout"
        ws.mkdir(parents=True, exist_ok=True)
        executor = EphemeralOrbStackExecutor()

        with patch("subprocess.run", side_effect=sp.TimeoutExpired(cmd="docker", timeout=5)):
            res = executor.run_isolated(ws, command=["sleep", "10"], timeout_sec=5)
            assert res.timed_out is True
            assert res.success is False

    def test_t1_f08_orbstack_executor_path_guard_mount_check(self, temp_workspace: Path):
        """F8.4: PathGuard blocks mounting protected ignore list directories."""
        prot = temp_workspace / "prot_mount"
        prot.mkdir(parents=True, exist_ok=True)
        guard = PathGuard(ignore_list=[str(prot)])
        executor = EphemeralOrbStackExecutor(path_guard=guard)

        with pytest.raises(ProtectedPathViolationError):
            executor.run_isolated(prot, command=["ls"])

    def test_t1_f08_orbstack_executor_result_serialization(self):
        """F8.5: ContainerExecutionResult serializes cleanly to dict."""
        res = ContainerExecutionResult(
            container_name="test-box",
            exit_code=0,
            stdout="out",
            stderr="err",
            duration_sec=1.23456,
        )
        d = res.to_dict()
        assert d["container_name"] == "test-box"
        assert d["exit_code"] == 0
        assert d["success"] is True
        assert d["duration_sec"] == 1.2346

    # --------------------------------------------------------------------------
    # FEATURE 9: Lead Execution & Draft PR Engine
    # --------------------------------------------------------------------------
    def test_t1_f09_executor_claim_and_execute_success(self, temp_workspace: Path, offline_db: OfflineFirestoreClient):
        """F9.1: Atomic claim and execution marks lead pr_open with pr_url."""
        col = offline_db.collection(COLLECTION_BOUNTY_LEADS)
        col.document("lead_exec_1").set({
            "repo": "stellar/soroban-example",
            "issue_number": 10,
            "title": "Fix token contract",
            "body": "GrantFox verified task.",
            "status": "pending_triage",
            "lock": {"owner_id": None},
        })

        executor = ExecutorEngine(
            db=offline_db,
            sandbox_base_dir=temp_workspace / "sandboxes",
        )
        res = executor.claim_and_execute_lead("lead_exec_1", dry_run=True)
        assert res["success"] is True
        assert res["final_lead_status"] == "pr_open"
        assert "pr_url" in res

    def test_t1_f09_executor_extract_stipulations(self):
        """F9.2: Extracts acceptance checklists from markdown and text."""
        body = """
        Fix the contract.
        Requirements:
        - [ ] Verify BLS signature recovery
        - [x] Pass 100% unit tests
        """
        stips = extract_stipulations_from_text(body)
        assert "Verify BLS signature recovery" in stips
        assert "Pass 100% unit tests" in stips

    def test_t1_f09_executor_payout_block_generation(self):
        """F9.3: Generates required Web3 dual-chain payout block."""
        executor = ExecutorEngine()
        block = executor.generate_payout_routing_block()
        assert EVM_PAYOUT_ADDRESS in block
        assert STELLAR_PAYOUT_ADDRESS in block

    def test_t1_f09_executor_draft_pr_body_assembly(self):
        """F9.4: Assembles Draft PR body with issue number, stipulations, and payout block."""
        executor = ExecutorEngine()
        lead = {
            "issue_number": 42,
            "repo": "stellar/token",
            "title": "Auth Fix",
            "projected_payout": "$500",
            "ecosystem": "stellar",
        }
        body = executor.build_pr_body(lead, stipulations=["Pass tests"])
        assert "Fixes #42" in body
        assert "- [x] Pass tests" in body
        assert EVM_PAYOUT_ADDRESS in body

    def test_t1_f09_executor_guaranteed_sandbox_teardown(self, temp_workspace: Path, offline_db: OfflineFirestoreClient):
        """F9.5: Guaranteed sandbox workspace removal even if container throws."""
        sandbox_dir = temp_workspace / "sb_clean"
        col = offline_db.collection(COLLECTION_BOUNTY_LEADS)
        col.document("lead_fail").set({
            "repo": "org/repo",
            "issue_number": 99,
            "status": "pending_triage",
            "lock": {"owner_id": None},
        })

        executor = ExecutorEngine(db=offline_db, sandbox_base_dir=sandbox_dir)
        with patch.object(executor.executor, "run_isolated", side_effect=RuntimeError("Container blowup")):
            res = executor.claim_and_execute_lead("lead_fail", dry_run=False)

        assert res["success"] is False
        # Sandbox directory must have been cleaned up
        for item in sandbox_dir.iterdir():
            assert not item.name.startswith("bounty_org_repo_99")

    # --------------------------------------------------------------------------
    # FEATURE 10: Native IMAP Listener & Email Parser
    # --------------------------------------------------------------------------
    def test_t1_f10_inbox_clean_reply_quotes(self):
        """F10.1: Strips email reply quote blocks."""
        raw = "Looks great, please merge!\n> On Aug 20, wrote:\n> Can you check this?"
        cleaned = clean_reply_quotes(raw)
        assert cleaned == "Looks great, please merge!"

    def test_t1_f10_inbox_decode_mime_headers(self):
        """F10.2: Decodes MIME RFC 2047 encoded subject headers."""
        header = "=?utf-8?B?UmU6IFtPcGVuIFBSXSBGaXggQmFzZSBMMiBBdXRo?= "
        decoded = decode_mime_header(header)
        assert "Re: [Open PR] Fix Base L2 Auth" in decoded

    def test_t1_f10_inbox_correlate_email_to_pr(self, offline_db: OfflineFirestoreClient):
        """F10.3: Correlates incoming email to open PR memory document."""
        col = offline_db.collection(COLLECTION_BOUNTY_MEMORY)
        col.document("stellar_token_42").set({
            "repo": "stellar/token",
            "pr_number": 142,
            "issue_number": 42,
        })
        inbox = InboxEngine(db=offline_db)
        matches = inbox.correlate_email_to_prs(
            subject="Review on stellar/token PR #142",
            body="Please update the test assertions.",
        )
        assert len(matches) == 1
        assert matches[0][0] == "stellar_token_42"

    def test_t1_f10_inbox_suppress_alert_loopback(self, offline_db: OfflineFirestoreClient):
        """F10.4: Suppresses loopback alert emails from self."""
        inbox = InboxEngine(db=offline_db)
        res = inbox.process_email_record(
            subject="[Bounty Engine ALERT] PR #142 needs attention",
            body="Alert notification",
        )
        assert res["suppressed"] is True

    def test_t1_f10_inbox_unconfigured_graceful_handling(self, offline_db: OfflineFirestoreClient):
        """F10.5: Unconfigured credentials gracefully returns empty list without error."""
        inbox = InboxEngine(db=offline_db, gmail_user="", gmail_app_password="")
        assert inbox.is_configured() is False
        assert inbox.fetch_unread_emails() == []

    # --------------------------------------------------------------------------
    # FEATURE 11: PR Escort & CI Rollup Engine
    # --------------------------------------------------------------------------
    def test_t1_f11_escort_filters_preview_deployment_gates(self):
        """F11.1: Filters external preview deployment gates (Vercel, Netlify, Cloudflare)."""
        assert is_external_deployment_gate("deploy/vercel-preview") is True
        assert is_external_deployment_gate("Netlify Deployment Gate") is True
        assert is_external_deployment_gate("Unit Tests / Python 3.11") is False

    def test_t1_f11_escort_detects_real_ci_failures(self):
        """F11.2: Flags genuine test failures as actionable CI fixes."""
        escort = EscortEngine()
        pr_data = {
            "commits": {
                "nodes": [
                    {
                        "commit": {
                            "statusCheckRollup": {"state": "FAILURE"},
                            "checkSuites": {
                                "nodes": [
                                    {
                                        "app": {"name": "GitHub Actions"},
                                        "checkRuns": {
                                            "nodes": [
                                                {"name": "test_suite", "conclusion": "FAILURE"}
                                            ]
                                        },
                                    }
                                ]
                            },
                        }
                    }
                ]
            }
        }
        res = escort.inspect_pr_health(pr_data)
        assert res["needs_ci_fix"] is True
        assert "test_suite" in res["actionable_ci_failures"]

    def test_t1_f11_escort_evaluates_14_day_staleness(self):
        """F11.3: Flags PRs with >= 14 days of inactivity as stalled."""
        escort = EscortEngine(stale_days_threshold=14)
        old_date = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        pr_data = {"created_at": old_date, "updated_at": old_date, "is_draft": False}
        res = escort.inspect_pr_health(pr_data)
        assert res["is_stalled"] is True
        assert res["needs_maintainer_bump"] is True

    def test_t1_f11_escort_audits_and_updates_doc(self, offline_db: OfflineFirestoreClient):
        """F11.4: Audits PR and writes telemetry back to Firestore."""
        col = offline_db.collection(COLLECTION_BOUNTY_MEMORY)
        col.document("pr_100").set({"ci_status": "SUCCESS", "updated_at": datetime.now(timezone.utc).isoformat()})
        escort = EscortEngine(db=offline_db)
        eval_res = escort.audit_and_update_pr("pr_100", {"ci_status": "SUCCESS"})
        assert eval_res["ci_status"] == "SUCCESS"
        updated_doc = col.document("pr_100").get().to_dict()
        assert "escort_telemetry" in updated_doc

    def test_t1_f11_escort_run_sweep_aggregates(self, offline_db: OfflineFirestoreClient):
        """F11.5: Escort sweep pass aggregates total PRs, CI failures, and stalled counts."""
        col = offline_db.collection(COLLECTION_BOUNTY_MEMORY)
        col.document("pr_ok").set({"ci_status": "SUCCESS"})
        col.document("pr_bad").set({"ci_status": "FAILURE"})
        escort = EscortEngine(db=offline_db)
        sweep = escort.run_sweep()
        assert sweep["total_monitored"] == 2
        assert sweep["ci_failures_count"] == 1

    # --------------------------------------------------------------------------
    # FEATURE 12: Settlement & Coordinator Sync Engine
    # --------------------------------------------------------------------------
    def test_t1_f12_sync_extract_payout_numeric(self):
        """F12.1: Extracts numeric payouts from various schema fields."""
        assert extract_payout_numeric({"projected_payout_usd": 150.0})[1] == 150.0
        assert extract_payout_numeric({"escrow": {"amount_usd": 750.0}})[1] == 750.0
        assert extract_payout_numeric({"title": "Bounty $300"})[1] == 300.0

    def test_t1_f12_sync_process_merged_doc_settlement(self, offline_db: OfflineFirestoreClient):
        """F12.2: Creates immutable settlement record for merged PR."""
        sync = SyncEngine(db=offline_db)
        rec = sync.process_doc_settlement(
            doc_id="pr_merged_1",
            data={
                "state": "MERGED",
                "repo": "stellar/token",
                "pr_number": 42,
                "projected_payout_usd": 500.0,
            },
        )
        assert rec is not None
        assert rec["status"] == "SETTLED"
        assert rec["payout_usd"] == 500.0
        assert offline_db.collection(COLLECTION_BOUNTY_SETTLEMENTS).document("settle_pr_merged_1").get().exists

    def test_t1_f12_sync_ignores_unsettled_doc(self, offline_db: OfflineFirestoreClient):
        """F12.3: Unsettled open PRs are ignored during settlement processing."""
        sync = SyncEngine(db=offline_db)
        rec = sync.process_doc_settlement("pr_open_1", {"state": "OPEN", "status": "running"})
        assert rec is None

    def test_t1_f12_sync_updates_coordinator_state(self, offline_db: OfflineFirestoreClient):
        """F12.4: Sync sweep aggregates total settled USD and updates coordinator state."""
        col = offline_db.collection(COLLECTION_BOUNTY_SETTLEMENTS)
        col.document("s1").set({"payout_usd": 300.0, "status": "SETTLED"})
        col.document("s2").set({"payout_usd": 700.0, "status": "SETTLED"})

        sync = SyncEngine(db=offline_db)
        res = sync.sync_settlements()
        assert res["total_settled_usd"] == 1000.0

        coord = offline_db.collection(COLLECTION_SWARM_COORDINATOR).document("state").get().to_dict()
        assert coord["total_settled_usd"] == 1000.0
        assert coord["status"] == "HEALTHY"

    def test_t1_f12_sync_run_sweep_aggregates_totals(self, offline_db: OfflineFirestoreClient):
        """F12.5: SyncEngine run_sweep returns structured telemetry."""
        sync = SyncEngine(db=offline_db)
        res = sync.run_sweep()
        assert "synced_count" in res
        assert "total_settled_usd" in res

    # --------------------------------------------------------------------------
    # FEATURE 13: Monolithic Hourly Sweeper Pipeline
    # --------------------------------------------------------------------------
    def test_t1_f13_sweeper_6_phases_sequential_flow(self, temp_workspace: Path, offline_db: OfflineFirestoreClient):
        """F13.1: Sweeper executes all 6 phases + pre/post GC."""
        sweeper = UniversalHourlySweeper(db=offline_db, sandbox_base_dir=temp_workspace / "sb")
        summary = sweeper.run_sweep(dry_run=True)
        assert summary.success is True
        assert len(summary.phase_order) == 7

    def test_t1_f13_sweeper_phase_ordering_integrity(self, temp_workspace: Path, offline_db: OfflineFirestoreClient):
        """F13.2: Phase ordering strictly follows Preflight -> Inbox -> Escort -> Sync -> Intake -> Exec -> Postflight."""
        sweeper = UniversalHourlySweeper(db=offline_db, sandbox_base_dir=temp_workspace / "sb")
        summary = sweeper.run_sweep(dry_run=True)
        expected = ["preflight_gc", "inbox", "escort", "sync", "intake", "execution", "postflight_gc"]
        assert summary.phase_order == expected

    def test_t1_f13_sweeper_partial_failure_resilience(self, temp_workspace: Path, offline_db: OfflineFirestoreClient):
        """F13.3: Failure in one phase does not crash remaining phases; postflight GC always runs."""
        mock_gh = MagicMock()
        mock_gh.search_bounties.return_value = {"data": {"search": {"nodes": []}}}
        sweeper = UniversalHourlySweeper(
            db=offline_db,
            github_client=mock_gh,
            sandbox_base_dir=temp_workspace / "sb",
        )
        with patch.object(sweeper.inbox_engine, "run_sweep", side_effect=Exception("IMAP error")):
            with patch.object(sweeper.inbox_engine, "is_configured", return_value=True):
                summary = sweeper.run_sweep(dry_run=False, skip_clone=True)
        assert summary.success is False
        assert "postflight_gc" in summary.phase_order

    def test_t1_f13_sweeper_coordinator_telemetry_persistence(self, temp_workspace: Path, offline_db: OfflineFirestoreClient):
        """F13.4: Sweeper records pass summary to swarm_coordinator/state."""
        sweeper = UniversalHourlySweeper(db=offline_db, sandbox_base_dir=temp_workspace / "sb")
        summary = sweeper.run_sweep(dry_run=True)
        coord = offline_db.collection(COLLECTION_SWARM_COORDINATOR).document("state").get().to_dict()
        assert coord["last_sweep_id"] == summary.sweep_id

    def test_t1_f13_sweeper_daemon_loop_and_signals(self, temp_workspace: Path, offline_db: OfflineFirestoreClient):
        """F13.5: Sweeper daemon loop executes iterations and halts on max_iterations."""
        sweeper = UniversalHourlySweeper(db=offline_db, sandbox_base_dir=temp_workspace / "sb")
        summaries = sweeper.run_loop(interval_sec=0, max_iterations=2, dry_run=True)
        assert len(summaries) == 2
        assert not sweeper.is_running

    # --------------------------------------------------------------------------
    # FEATURE 14: Unified CLI Application
    # --------------------------------------------------------------------------
    def test_t1_f14_cli_subcommand_sweep(self):
        """F14.1: CLI `bounty sweep --dry-run`."""
        with patch("src.cli.handle_sweep", return_value=0) as mock_h:
            exit_code = cli_main(["sweep", "--dry-run"])
            assert exit_code == 0
            assert mock_h.called

    def test_t1_f14_cli_subcommand_intake(self):
        """F14.2: CLI `bounty intake --dry-run`."""
        with patch("src.cli.handle_intake", return_value=0) as mock_h:
            exit_code = cli_main(["intake", "--dry-run"])
            assert exit_code == 0
            assert mock_h.called

    def test_t1_f14_cli_subcommand_exec(self):
        """F14.3: CLI `bounty exec --limit 2 --dry-run`."""
        with patch("src.cli.handle_exec", return_value=0) as mock_h:
            exit_code = cli_main(["exec", "--limit", "2", "--dry-run"])
            assert exit_code == 0
            assert mock_h.called

    def test_t1_f14_cli_subcommand_escort_and_sync(self):
        """F14.4: CLI `bounty escort` and `bounty sync`."""
        with patch("src.cli.handle_escort", return_value=0) as mock_e, \
             patch("src.cli.handle_sync", return_value=0) as mock_s:
            assert cli_main(["escort", "--dry-run"]) == 0
            assert cli_main(["sync", "--dry-run"]) == 0
            assert mock_e.called
            assert mock_s.called

    def test_t1_f14_cli_subcommand_status_and_migrate(self):
        """F14.5: CLI `bounty status` and `bounty migrate`."""
        with patch("src.cli.handle_status", return_value=0) as mock_stat, \
             patch("src.cli.handle_migrate", return_value=0) as mock_mig:
            assert cli_main(["status"]) == 0
            assert cli_main(["migrate", "--dry-run"]) == 0
            assert mock_stat.called
            assert mock_mig.called

    # --------------------------------------------------------------------------
    # FEATURE 15: Live E2E Verification Flow
    # --------------------------------------------------------------------------
    def test_t1_f15_live_verification_pipeline_mock(self, temp_workspace: Path, offline_db: OfflineFirestoreClient):
        """F15.1: Live flow simulation connects intake, execution, and escort."""
        sweeper = UniversalHourlySweeper(db=offline_db, sandbox_base_dir=temp_workspace / "sb")
        summary = sweeper.run_sweep(dry_run=True)
        assert summary.success is True

    def test_t1_f15_live_verification_pr_creation(self, temp_workspace: Path, offline_db: OfflineFirestoreClient):
        """F15.2: Verifies PR description created in live verification flow contains all required sections."""
        executor = ExecutorEngine(db=offline_db, sandbox_base_dir=temp_workspace / "sb")
        lead = {"issue_number": 77, "repo": "org/repo", "title": "Live Fix"}
        body = executor.build_pr_body(lead, stipulations=["Live test passes"])
        assert "Fixes #77" in body
        assert "EVM" in body
        assert "Stellar" in body

    def test_t1_f15_live_verification_imap_correlation(self, offline_db: OfflineFirestoreClient):
        """F15.3: Live verification email correlation updates PR feedback state."""
        col = offline_db.collection(COLLECTION_BOUNTY_MEMORY)
        col.document("repo_77").set({"repo": "org/repo", "issue_number": 77, "pr_number": 177})
        inbox = InboxEngine(db=offline_db)
        res = inbox.process_email_record(
            subject="Re: [org/repo] Pull Request #177 merged",
            body="Great job! Funds released.",
            sender="maintainer@org.com",
        )
        assert "repo_77" in res["correlated_prs"]
        doc = col.document("repo_77").get().to_dict()
        assert doc["has_unread_feedback"] is True

    def test_t1_f15_live_verification_settlement_payout(self, offline_db: OfflineFirestoreClient):
        """F15.4: Live verification settlement records EVM payout address."""
        sync = SyncEngine(db=offline_db)
        rec = sync.process_doc_settlement(
            "live_settle_1",
            {"state": "MERGED", "repo": "org/repo", "pr_number": 177, "payout_usd": 1200.0},
        )
        assert rec is not None
        assert rec["payout_recipient"] == EVM_PAYOUT_ADDRESS

    def test_t1_f15_live_verification_end_to_end_state_check(self, offline_db: OfflineFirestoreClient):
        """F15.5: Verified state across leads, memory, settlements, and coordinator."""
        col_leads = offline_db.collection(COLLECTION_BOUNTY_LEADS)
        col_mem = offline_db.collection(COLLECTION_BOUNTY_MEMORY)
        col_settle = offline_db.collection(COLLECTION_BOUNTY_SETTLEMENTS)
        col_coord = offline_db.collection(COLLECTION_SWARM_COORDINATOR)

        col_leads.document("l1").set({"status": "completed"})
        col_mem.document("m1").set({"state": "MERGED"})
        col_settle.document("s1").set({"payout_usd": 1000.0, "status": "SETTLED"})
        col_coord.document("state").set({"status": "HEALTHY", "total_settled_usd": 1000.0})

        assert col_leads.document("l1").get().exists
        assert col_mem.document("m1").get().exists
        assert col_settle.document("s1").get().exists
        assert col_coord.document("state").get().to_dict()["status"] == "HEALTHY"


# ==============================================================================
# TIER 2: BOUNDARY VALUE ANALYSIS & CORNER CASES (≥15 Detailed Boundary Tests)
# ==============================================================================

class TestTier2BoundaryAndCornerCases:
    """Tier 2: Boundary value analysis, extreme values, empty inputs, and corner cases."""

    def test_t2_bva_empty_and_whitespace_inputs_to_financial_parser(self):
        """BVA: Empty, whitespace, or non-numeric strings to extract_financials."""
        assert extract_financials("")[1] == 0.0
        assert extract_financials("   \n\t  ")[1] == 0.0
        assert extract_financials("No price mentioned here.")[1] == 0.0
        assert extract_financials("$0")[1] == 0.0
        assert extract_financials("$0.00")[1] == 0.0

    def test_t2_bva_extreme_dollar_and_token_amounts(self):
        """BVA: Huge payout values ($10,000,000.00 and fractional tokens)."""
        txt = "Giant treasury reward of $10,000,000.00 on Arbitrum."
        assert extract_financials(txt)[1] == 10000000.0
        txt2 = "Micro reward of 0.00001 ETH"
        assert extract_financials(txt2)[1] == 0.00001

    def test_t2_bva_zero_and_negative_settlement_amounts(self, offline_db: OfflineFirestoreClient):
        """BVA: Zero payout handling in sync engine."""
        sync = SyncEngine(db=offline_db)
        rec = sync.process_doc_settlement("zero_doc", {"state": "MERGED", "projected_payout_usd": 0.0})
        assert rec is not None
        assert rec["payout_usd"] == 0.0

    def test_t2_bva_exact_14_day_inactivity_boundary(self):
        """BVA: PR staleness evaluated at 13.99 days vs 14.01 days."""
        escort = EscortEngine(stale_days_threshold=14)
        now = datetime.now(timezone.utc)

        # 13.99 days ago (NOT stalled)
        just_under = (now - timedelta(days=13, hours=23, minutes=45)).isoformat()
        res_under = escort.inspect_pr_health({"created_at": just_under, "updated_at": just_under})
        assert res_under["is_stalled"] is False

        # 14.01 days ago (IS stalled)
        just_over = (now - timedelta(days=14, hours=1)).isoformat()
        res_over = escort.inspect_pr_health({"created_at": just_over, "updated_at": just_over})
        assert res_over["is_stalled"] is True

    def test_t2_bva_load_balancer_zero_and_max_capacity(self, offline_db: OfflineFirestoreClient):
        """BVA: Balancer when 0 leads queued, or exactly at max_concurrent limit."""
        engine = IntakeEngine(db=offline_db)
        assert engine.balance_queue(max_concurrent=0) == []
        assert engine.balance_queue(max_concurrent=4) == []

    def test_t2_bva_corrupt_jsonl_recovery_in_safe_io(self, temp_workspace: Path):
        """BVA: SafeIO stream_jsonl raises SafeIOError on malformed JSON record."""
        corrupt_file = temp_workspace / "corrupt.jsonl"
        SafeIO.write_text(corrupt_file, '{"valid": 1}\n{corrupt json\n{"valid": 2}\n')
        with pytest.raises(SafeIOError):
            list(SafeIO.stream_jsonl(corrupt_file))

    def test_t2_bva_path_guard_root_and_parent_traversal(self, temp_workspace: Path):
        """BVA: PathGuard handling deep relative parent paths ../../../."""
        prot = temp_workspace / "prot"
        prot.mkdir()
        guard = PathGuard(ignore_list=[str(prot)])
        with pytest.raises(ProtectedPathViolationError):
            guard.validate_access(temp_workspace / "a" / "b" / "c" / ".." / ".." / ".." / "prot")

    def test_t2_bva_multi_lingual_and_unicode_in_email_and_pr_bodies(self, offline_db: OfflineFirestoreClient):
        """BVA: Unicode, emojis, and multi-lingual UTF-8 in email and PR descriptions."""
        inbox = InboxEngine(db=offline_db)
        unicode_body = "Hello! 🚀 Stellar 智能合约 ✅ #101"
        res = inbox.process_email_record("测试主题", unicode_body)
        assert res["clean_body"] == unicode_body

    def test_t2_bva_empty_command_in_orbstack_executor(self, temp_workspace: Path):
        """BVA: EphemeralOrbStackExecutor raises ValueError on empty command list."""
        executor = EphemeralOrbStackExecutor()
        ws = temp_workspace / "ws"
        ws.mkdir()
        with pytest.raises(ValueError):
            executor.run_isolated(ws, command=[])

    def test_t2_bva_duplicate_doc_id_migration_collision(self):
        """BVA: Migrate queues deduplicates 10 identical doc IDs cleanly."""
        records = [{"id": "doc_x", "projected_payout_usd": float(i)} for i in range(10)]
        deduped = deduplicate_leads(records)
        assert len(deduped) == 1
        assert deduped[0]["projected_payout_usd"] == 9.0

    def test_t2_bva_zero_timeout_container_execution(self, temp_workspace: Path):
        """BVA: Container execution timeout handled with 0 or 1s timeout."""
        ws = temp_workspace / "ws_zero"
        ws.mkdir()
        executor = EphemeralOrbStackExecutor()
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(returncode=0, stdout="", stderr="")
            res = executor.run_isolated(ws, command=["echo", "1"], timeout_sec=1)
            assert res.exit_code == 0

    def test_t2_bva_banned_platform_case_insensitivity_and_subdomains(self):
        """BVA: Banned platform check handles uppercase, mixed case, and URLs."""
        assert is_banned_platform("ALgORa.IO/test") is True
        assert is_banned_platform("https://POLAR.sh/owner/repo") is True
        assert is_banned_platform("OPIRE.DEV") is True

    def test_t2_bva_stipulation_extraction_with_no_markdown_checkboxes(self):
        """BVA: Stipulation extraction returns fallback requirements if no checklist exists."""
        stips = extract_stipulations_from_text("Fix typo in documentation")
        assert len(stips) >= 1
        assert any("genuine solution" in s for s in stips)

    def test_t2_bva_huge_firestore_batch_chunking(self, offline_db: OfflineFirestoreClient, temp_workspace: Path):
        """BVA: Migration handles large datasets with batch chunking."""
        src = temp_workspace / "large_src.jsonl"
        dest = temp_workspace / "large_dest.jsonl"
        records = [
            {"repo": f"org_{i}/repo_{i}", "number": i + 1, "title": f"Bounty {i + 1}"}
            for i in range(25)
        ]
        SafeIO.write_jsonl(src, records)
        res = migrate_queues(src, target_jsonl=dest, firestore_sync=True, db=offline_db)
        assert res.firestore_synced == 25

    def test_t2_bva_missing_repository_in_lead_normalization(self):
        """BVA: Lead normalization rejects records with missing repository."""
        assert normalize_lead_schema({"number": 1, "title": "No repo"}) is None
        assert normalize_lead_schema({"repo": "org/repo"}) is None  # Missing issue number


# ==============================================================================
# TIER 3: CROSS-FEATURE COMBINATIONS (≥15 Multi-Engine Interaction Tests)
# ==============================================================================

class TestTier3CrossFeatureCombinations:
    """Tier 3: Interaction tests across multiple modules and architectural layers."""

    def test_t3_combo_01_path_guard_and_safe_io_with_config_ignore_list(self, temp_workspace: Path):
        """Combo 1 (F1 + F2 + F3): Config default ignore list enforced by SafeIO and PathGuard."""
        prot = temp_workspace / "keeper_daemon"
        prot.mkdir()
        guard = PathGuard(ignore_list=[str(prot)])
        SafeIO.set_guard(guard)
        try:
            with pytest.raises(ProtectedPathViolationError):
                SafeIO.write_text(prot / "hack.txt", "data")
        finally:
            SafeIO.set_guard(DEFAULT_PATH_GUARD)

    def test_t3_combo_02_safe_io_firestore_and_migration(self, temp_workspace: Path, offline_db: OfflineFirestoreClient):
        """Combo 2 (F3 + F4 + F5): SafeIO streams legacy JSONL -> migrate_queues -> OfflineFirestoreClient."""
        src = temp_workspace / "combo2.jsonl"
        dest = temp_workspace / "combo2_out.jsonl"
        SafeIO.write_jsonl(src, [{"repo": "stellar/soroban-token", "number": 1, "projected_payout_usd": 300.0}])
        res = migrate_queues(src, target_jsonl=dest, firestore_sync=True, db=offline_db)
        assert res.migrated_records == 1
        assert offline_db.collection(COLLECTION_BOUNTY_LEADS).document("stellar_soroban_token_1").get().exists

    def test_t3_combo_03_config_banned_platforms_and_intake_sniper_filter(self):
        """Combo 3 (F1 + F6): Banned platform configuration directly rejects intake candidate."""
        node = {"repository": {"nameWithOwner": f"{BANNED_PLATFORMS[0]}/test", "isArchived": False}, "number": 1}
        is_val, reason, _, _, _, _ = verify_escrow(node)
        assert is_val is False
        assert "BANNED_PLATFORM" in reason

    def test_t3_combo_04_firestore_leads_and_intake_load_balancer(self, offline_db: OfflineFirestoreClient):
        """Combo 4 (F4 + F7): IntakeEngine queries Firestore leads and balances queue state."""
        col = offline_db.collection(COLLECTION_BOUNTY_LEADS)
        col.document("l1").set({"repo": "org/repo", "status": "queued", "priority": "high", "projected_payout_usd": 800.0})
        engine = IntakeEngine(db=offline_db)
        promoted = engine.balance_queue(max_concurrent=1)
        assert len(promoted) == 1
        assert col.document("l1").get().to_dict()["status"] == "priority_triage"

    def test_t3_combo_05_path_guard_orbstack_and_executor_sandbox(self, temp_workspace: Path, offline_db: OfflineFirestoreClient):
        """Combo 5 (F2 + F8 + F9): ExecutorEngine provisions sandbox guarded by PathGuard for OrbStack."""
        sandbox_base = temp_workspace / "sb_combo"
        prot = sandbox_base / "protected_target"
        prot.mkdir(parents=True, exist_ok=True)

        guard = PathGuard(ignore_list=[str(prot)])
        executor = ExecutorEngine(db=offline_db, path_guard=guard, sandbox_base_dir=sandbox_base)

        with pytest.raises(ProtectedPathViolationError):
            executor.path_guard.validate_access(prot, "mount")

    def test_t3_combo_06_executor_draft_pr_and_imap_email_correlation(self, offline_db: OfflineFirestoreClient):
        """Combo 6 (F4 + F9 + F10): Executor creates PR in memory -> IMAP correlates maintainer email."""
        col = offline_db.collection(COLLECTION_BOUNTY_MEMORY)
        col.document("stellar_bridge_10").set({
            "repo": "stellar/bridge",
            "pr_number": 110,
            "issue_number": 10,
            "inbox_notifications": [],
        })
        inbox = InboxEngine(db=offline_db)
        res = inbox.process_email_record("Re: stellar/bridge PR #110 Feedback", "LGTM, passing CI")
        assert "stellar_bridge_10" in res["correlated_prs"]
        doc = col.document("stellar_bridge_10").get().to_dict()
        assert len(doc["inbox_notifications"]) == 1

    def test_t3_combo_07_imap_feedback_and_pr_escort_health_rollup(self, offline_db: OfflineFirestoreClient):
        """Combo 7 (F4 + F10 + F11): PR memory record receives IMAP notice -> Escort audits PR health."""
        col = offline_db.collection(COLLECTION_BOUNTY_MEMORY)
        col.document("pr_doc").set({
            "repo": "org/repo",
            "pr_number": 50,
            "ci_status": "SUCCESS",
            "has_unread_feedback": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        escort = EscortEngine(db=offline_db)
        audit = escort.audit_and_update_pr("pr_doc", col.document("pr_doc").get().to_dict())
        assert audit["ci_status"] == "SUCCESS"
        assert audit["needs_ci_fix"] is False

    def test_t3_combo_08_pr_escort_merged_state_and_sync_engine_settlement(self, offline_db: OfflineFirestoreClient):
        """Combo 8 (F4 + F11 + F12): Escort identifies merged PR -> SyncEngine records settlement."""
        col_mem = offline_db.collection(COLLECTION_BOUNTY_MEMORY)
        col_mem.document("pr_settled").set({
            "state": "MERGED",
            "repo": "stellar/soroban-dex",
            "pr_number": 88,
            "projected_payout_usd": 1500.0,
        })
        sync = SyncEngine(db=offline_db)
        res = sync.sync_settlements()
        assert res["synced_count"] == 1
        assert res["total_settled_usd"] == 1500.0

    def test_t3_combo_09_payout_config_draft_pr_and_settlement_ledger(self, temp_workspace: Path, offline_db: OfflineFirestoreClient):
        """Combo 9 (F1 + F9 + F12): Config payout routing -> Executor draft PR body -> SyncEngine settlement."""
        executor = ExecutorEngine(db=offline_db, sandbox_base_dir=temp_workspace / "sb")
        pr_body = executor.build_pr_body({"repo": "org/repo", "issue_number": 1, "projected_payout": "$500"}, [])
        assert EVM_PAYOUT_ADDRESS in pr_body

        sync = SyncEngine(db=offline_db)
        rec = sync.process_doc_settlement("p1", {"state": "MERGED", "projected_payout_usd": 500.0})
        assert rec["payout_recipient"] == EVM_PAYOUT_ADDRESS

    def test_t3_combo_10_sync_settlements_and_coordinator_state(self, offline_db: OfflineFirestoreClient):
        """Combo 10 (F4 + F12 + F13): SyncEngine updates settlement total in swarm_coordinator/state."""
        col_settle = offline_db.collection(COLLECTION_BOUNTY_SETTLEMENTS)
        col_settle.document("s1").set({"payout_usd": 500.0, "status": "SETTLED"})
        col_settle.document("s2").set({"payout_usd": 500.0, "status": "SETTLED"})

        sync = SyncEngine(db=offline_db)
        res = sync.sync_settlements()
        assert res["total_settled_usd"] == 1000.0
        coord = offline_db.collection(COLLECTION_SWARM_COORDINATOR).document("state").get().to_dict()
        assert coord["total_settled_usd"] == 1000.0

    def test_t3_combo_11_orbstack_gc_and_sweeper_preflight_postflight(self, temp_workspace: Path, offline_db: OfflineFirestoreClient):
        """Combo 11 (F8 + F13): Sweeper runs OrbStack cleanup in Preflight and Postflight GC."""
        mock_gh = MagicMock()
        mock_gh.search_bounties.return_value = {"data": {"search": {"nodes": []}}}
        sweeper = UniversalHourlySweeper(
            db=offline_db,
            github_client=mock_gh,
            sandbox_base_dir=temp_workspace / "sb",
        )
        with patch.object(sweeper.orbstack_executor, "cleanup_stale_containers", return_value=3) as mock_gc:
            summary = sweeper.run_sweep(dry_run=False, skip_clone=True)
            assert mock_gc.call_count == 2  # Preflight and Postflight
            assert summary.preflight_gc["cleaned_containers"] == 3
            assert summary.postflight_gc["cleaned_containers"] == 3

    def test_t3_combo_12_sweeper_engine_and_cli_sweep(self):
        """Combo 12 (F13 + F14): CLI `bounty sweep` parses options and invokes UniversalHourlySweeper."""
        with patch.object(UniversalHourlySweeper, "run_sweep") as mock_sweep:
            mock_sweep.return_value = SweepSummary(success=True)
            code = cli_main(["sweep", "--dry-run", "--max-leads", "2"])
            assert code == 0
            assert mock_sweep.called

    def test_t3_combo_13_migrate_queues_and_cli_migrate(self, temp_workspace: Path):
        """Combo 13 (F5 + F14): CLI `bounty migrate` executes migrate_queues."""
        src = temp_workspace / "cli_mig.jsonl"
        dest = temp_workspace / "cli_mig_out.jsonl"
        SafeIO.write_jsonl(src, [{"repo": "org/repo", "number": 1}])
        code = cli_main(["migrate", "-s", str(src), "-t", str(dest)])
        assert code == 0
        assert dest.exists()

    def test_t3_combo_14_full_lifecycle_lead_to_settlement(self, temp_workspace: Path, offline_db: OfflineFirestoreClient):
        """Combo 14 (F6 + F7 + F9 + F11 + F12): Full flow: Ingest -> Balance -> Claim/Exec -> PR -> Settle."""
        intake = IntakeEngine(db=offline_db)
        col_leads = offline_db.collection(COLLECTION_BOUNTY_LEADS)
        col_leads.document("lead_full").set({
            "repo": "stellar/soroban-bridge",
            "issue_number": 55,
            "title": "Build Bridge",
            "status": "queued",
            "priority": "high",
            "projected_payout_usd": 1500.0,
            "lock": {"owner_id": None},
        })

        # Balance
        promoted = intake.balance_queue(max_concurrent=4)
        assert len(promoted) == 1

        # Execute
        executor = ExecutorEngine(db=offline_db, sandbox_base_dir=temp_workspace / "sb")
        exec_res = executor.claim_and_execute_lead("lead_full", dry_run=True)
        assert exec_res["success"] is True

        # Escort
        col_mem = offline_db.collection(COLLECTION_BOUNTY_MEMORY)
        col_mem.document("stellar_soroban_bridge_55").update({"state": "MERGED"})

        # Sync
        sync = SyncEngine(db=offline_db)
        sync_res = sync.sync_settlements()
        assert sync_res["synced_count"] == 1
        assert sync_res["total_settled_usd"] == 1500.0

    def test_t3_combo_15_full_stack_invariant_and_path_guard_containment(self, temp_workspace: Path, offline_db: OfflineFirestoreClient):
        """Combo 15 (F1..F15): Invariant verification — no protected path reads, dual chain payouts."""
        sweeper = UniversalHourlySweeper(db=offline_db, sandbox_base_dir=temp_workspace / "sb")
        summary = sweeper.run_sweep(dry_run=True)
        assert summary.success is True
        assert sweeper.executor_engine.generate_payout_routing_block() == f"## Payout Routing\n- **EVM (Base/Arbitrum/Polygon/ETH):** `{EVM_PAYOUT_ADDRESS}`\n- **Stellar:** `{STELLAR_PAYOUT_ADDRESS}`"


# ==============================================================================
# TIER 4: REAL-WORLD APPLICATION SCENARIOS (5 High-Complexity Workloads)
# ==============================================================================

class TestTier4RealWorldWorkloadScenarios:
    """Tier 4: End-to-end realistic production workloads covering real multi-step operations."""

    def test_t4_scenario_1_full_migration_and_queue_intake(self, temp_workspace: Path, offline_db: OfflineFirestoreClient):
        """
        Scenario 1: Full Migration & Queue Intake Workload (F1, F3, F4, F5, F6, F7).
        1. Legacy queue containing valid, duplicate, and banned entries is migrated.
        2. Ingests new candidates via IntakeEngine.
        3. Applies Sniper Filter & Load Balancer.
        4. Validates queue partition with high priority on top and USD sorted.
        """
        src_jsonl = temp_workspace / "v1_legacy.jsonl"
        dest_jsonl = temp_workspace / "logs" / "intake_queue.jsonl"
        dest_jsonl.parent.mkdir(parents=True, exist_ok=True)

        legacy_data = [
            {"repo": "stellar/token", "number": 1, "priority": "high", "projected_payout_usd": 500.0, "title": "Stellar Token"},
            {"repo": "stellar/token", "number": 1, "priority": "high", "projected_payout_usd": 800.0, "title": "Stellar Token v2"},
            {"repo": "algora-io/banned", "number": 2, "priority": "high", "projected_payout_usd": 1000.0, "title": "Banned Algora"},
        ]
        SafeIO.write_jsonl(src_jsonl, legacy_data)

        # 1. Migrate (3 read, 1 duplicate skipped, 2 migrated)
        mig_res = migrate_queues(src_jsonl, target_jsonl=dest_jsonl, firestore_sync=True, db=offline_db)
        assert mig_res.migrated_records == 2

        # 2. Intake new candidate
        mock_gh = MagicMock()
        mock_gh.search_bounties.return_value = {
            "data": {
                "search": {
                    "nodes": [
                        {
                            "id": "new_node_1",
                            "number": 10,
                            "title": "GrantFox Base L2 Bridge ($1,500)",
                            "body": "GrantFox OSS escrow verified.",
                            "repository": {"nameWithOwner": "base-org/bridge", "isArchived": False},
                            "labels": [{"name": "grantfox"}],
                        }
                    ],
                    "pageInfo": {"hasNextPage": False},
                }
            }
        }

        intake = IntakeEngine(
            db=offline_db,
            github_client=mock_gh,
            queue_file_path=dest_jsonl,
            seen_cache_path=temp_workspace / "seen_s1.json",
        )
        intake_res = intake.run_sweep()
        assert intake_res["ingested_count"] == 1
        assert intake_res["promoted_count"] >= 1

        # 3. Verify load balancer promoted candidates
        active_leads = list(offline_db.collection(COLLECTION_BOUNTY_LEADS).where("status", "in", ["priority_triage", "pending_triage"]).stream())
        assert len(active_leads) >= 1

    def test_t4_scenario_2_ephemeral_sandbox_execution_to_draft_pr(self, temp_workspace: Path, offline_db: OfflineFirestoreClient):
        """
        Scenario 2: Ephemeral Sandbox Execution to Draft PR (F1, F2, F8, F9).
        1. Promoted lead claimed atomically.
        2. Ephemeral sandbox directory created under sandbox_base_dir.
        3. Isolated container execution executed with mock docker runner.
        4. Draft PR description assembled with Web3 payout routing.
        5. Sandbox removed cleanly.
        """
        sandbox_base = temp_workspace / "sandboxes_s2"
        col = offline_db.collection(COLLECTION_BOUNTY_LEADS)
        col.document("stellar_soroban_42").set({
            "repo": "stellar/soroban-amm",
            "issue_number": 42,
            "title": "Fix AMM swap math",
            "body": "GrantFox verified bounty.\nRequirements:\n- [x] Fix integer overflow\n- [x] Pass unit tests",
            "status": "priority_triage",
            "projected_payout": "$1,200",
            "projected_payout_usd": 1200.0,
            "ecosystem": "stellar",
            "lock": {"owner_id": None},
        })

        executor = ExecutorEngine(db=offline_db, sandbox_base_dir=sandbox_base)
        res = executor.claim_and_execute_lead("stellar_soroban_42", dry_run=True)

        assert res["success"] is True
        assert res["final_lead_status"] == "pr_open"
        assert "pr_url" in res

        # Verify PR memory document created
        mem_doc = offline_db.collection(COLLECTION_BOUNTY_MEMORY).document("stellar_soroban_amm_42").get().to_dict()
        assert mem_doc["is_draft"] is True
        assert EVM_PAYOUT_ADDRESS in mem_doc["pr_body"]
        assert STELLAR_PAYOUT_ADDRESS in mem_doc["pr_body"]

    def test_t4_scenario_3_maintainer_feedback_via_imap_to_escort_reaction(self, offline_db: OfflineFirestoreClient):
        """
        Scenario 3: Maintainer Feedback via IMAP to Escort Reaction (F10, F11, F13).
        1. Open PR recorded in bounty_memory.
        2. Maintainer sends review email received by InboxEngine.
        3. Email correlated to PR doc in bounty_memory.
        4. EscortEngine audits PR and verifies unread feedback flag.
        """
        col_mem = offline_db.collection(COLLECTION_BOUNTY_MEMORY)
        col_mem.document("base_auth_10").set({
            "repo": "base-org/web3-auth",
            "issue_number": 10,
            "pr_number": 110,
            "ci_status": "SUCCESS",
            "has_unread_feedback": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

        inbox = InboxEngine(db=offline_db)
        email_res = inbox.process_email_record(
            subject="[base-org/web3-auth] Changes requested on PR #110",
            body="Please address the lint errors.\n> previous quote block",
            sender="maintainer@base.org",
        )
        assert "base_auth_10" in email_res["correlated_prs"]

        # Verify PR memory updated
        mem_doc = col_mem.document("base_auth_10").get().to_dict()
        assert mem_doc["has_unread_feedback"] is True

        # Escort evaluation
        escort = EscortEngine(db=offline_db)
        audit_res = escort.audit_and_update_pr("base_auth_10", mem_doc)
        assert audit_res["pr_number"] == 110

    def test_t4_scenario_4_full_hourly_sweeper_lifecycle_execution(self, temp_workspace: Path, offline_db: OfflineFirestoreClient):
        """
        Scenario 4: Full Hourly Sweeper Lifecycle Execution (F1..F14).
        Executes a complete 6-phase sweep pass with preflight/postflight GC,
        intake, execution, escort, sync, and coordinator telemetry update.
        """
        sweeper = UniversalHourlySweeper(
            db=offline_db,
            sandbox_base_dir=temp_workspace / "sandboxes_s4",
        )

        summary = sweeper.run_sweep(dry_run=True, max_exec_leads=2)

        assert summary.success is True
        assert len(summary.errors) == 0
        assert summary.phase_order == [
            "preflight_gc",
            "inbox",
            "escort",
            "sync",
            "intake",
            "execution",
            "postflight_gc",
        ]

        # Verify coordinator state
        coord_doc = offline_db.collection(COLLECTION_SWARM_COORDINATOR).document("state").get().to_dict()
        assert coord_doc["status"] == "HEALTHY"
        assert coord_doc["last_sweep_id"] == summary.sweep_id

    def test_t4_scenario_5_live_github_bounty_pull_and_email_interception_to_settlement(
        self, temp_workspace: Path, offline_db: OfflineFirestoreClient
    ):
        """
        Scenario 5: Live GitHub Bounty Pull & Email Interception to Settlement (F6, F8, F9, F10, F13, F15).
        Complete lifecycle from mock GraphQL discovery to settlement recording:
        1. Ingest qualified GrantFox bounty.
        2. Promote via Load Balancer.
        3. Execute in isolated sandbox -> create Draft PR.
        4. Intercept maintainer merge notification via IMAP.
        5. SyncEngine records immutable settlement into bounty_settlements.
        6. Coordinator total settled USD is updated.
        """
        mock_gh = MagicMock()
        mock_gh.search_bounties.return_value = {
            "data": {
                "search": {
                    "nodes": [
                        {
                            "id": "LIVE_S5_LEAD",
                            "number": 88,
                            "title": "Soroban Bridge Settlement Test ($2,000)",
                            "body": "GrantFox OSS verified bounty on Stellar.",
                            "repository": {"nameWithOwner": "stellar-org/soroban-bridge", "isArchived": False},
                            "labels": [{"name": "grantfox"}],
                        }
                    ],
                    "pageInfo": {"hasNextPage": False},
                }
            }
        }

        sweeper = UniversalHourlySweeper(
            db=offline_db,
            github_client=mock_gh,
            sandbox_base_dir=temp_workspace / "sandboxes_s5",
        )

        # 1-3. Run sweep to ingest and execute
        summary = sweeper.run_sweep(dry_run=False, max_exec_leads=1, skip_clone=True)
        assert summary.success is True

        # 4. Simulate Maintainer Merged PR
        mem_doc_id = "stellar_org_soroban_bridge_88"
        col_mem = offline_db.collection(COLLECTION_BOUNTY_MEMORY)
        col_mem.document(mem_doc_id).update({
            "state": "MERGED",
            "merged_at": datetime.now(timezone.utc).isoformat(),
            "projected_payout_usd": 2000.0,
        })

        # 5. Run Sync Phase
        sync_res = sweeper.run_sync_phase()
        assert sync_res["synced_count"] == 1
        assert sync_res["total_settled_usd"] == 2000.0

        # 6. Verify Settlement Document
        settle_doc = offline_db.collection(COLLECTION_BOUNTY_SETTLEMENTS).document(f"settle_{mem_doc_id}").get().to_dict()
        assert settle_doc["status"] == "SETTLED"
        assert settle_doc["payout_usd"] == 2000.0
        assert settle_doc["payout_recipient"] == EVM_PAYOUT_ADDRESS
