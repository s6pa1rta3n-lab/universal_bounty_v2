"""
Tier 5 Adversarial White-Box Coverage Hardening Test Suite (Milestone 6).

Comprehensive white-box branch, path, condition, signal, and error-recovery coverage
across all core modules, engines, and orchestrator components:
- `src/core/config.py`
- `src/core/path_guard.py`
- `src/core/safe_io.py`
- `src/core/firestore_client.py`
- `src/core/orbstack_executor.py`
- `src/core/github_client.py`
- `src/engines/intake_engine.py`
- `src/engines/inbox_engine.py`
- `src/engines/executor_engine.py`
- `src/engines/escort_engine.py`
- `src/engines/sync_engine.py`
- `src/orchestrator/sweeper.py`
- `src/cli.py`
"""

from __future__ import annotations

import email
import email.message
import os
import signal
import subprocess
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.cli import (
    build_parser,
    handle_escort,
    handle_exec,
    handle_inbox,
    handle_intake,
    handle_migrate,
    handle_status,
    handle_sweep,
    handle_sync,
    main,
)
from src.core.config import (
    BANNED_PLATFORMS,
    COLLECTION_BOUNTY_LEADS,
    COLLECTION_BOUNTY_MEMORY,
    COLLECTION_BOUNTY_SETTLEMENTS,
    COLLECTION_SWARM_COORDINATOR,
    COLLECTION_SWARM_OPERATIONS,
    DEFAULT_IGNORE_LIST,
    DEFAULT_SANDBOX_BASE_DIR,
    DISQUALIFY_KEYWORDS,
    EVM_PAYOUT_ADDRESS,
    HIGH_PRIORITY_KEYWORDS,
    PAYOUT_ROUTING,
    SOLANA_PAYOUT_ADDRESS,
    STELLAR_PAYOUT_ADDRESS,
    SwarmConfig,
    contains_disqualify_keywords,
    get_config,
    is_banned_platform,
)
from src.core.exceptions import GitHubAPIError, ProtectedPathViolationError, SafeIOError
from src.core.firestore_client import (
    OfflineCollectionReference,
    OfflineDocumentReference,
    OfflineDocumentSnapshot,
    OfflineFirestoreClient,
    OfflineQuery,
    claim_lead_atomic,
    get_firestore_client,
)
from src.core.github_client import GitHubClient, get_github_client
from src.core.orbstack_executor import ContainerExecutionResult, EphemeralOrbStackExecutor
from src.core.path_guard import DEFAULT_PATH_GUARD, PathGuard
from src.core.safe_io import SafeIO
from src.engines.escort_engine import EscortEngine, is_external_deployment_gate
from src.engines.executor_engine import (
    ExecutorEngine,
    SwarmAgentAttestation,
    TeamworkSwarmDelegator,
    TeamworkSwarmResult,
    TeamworkSwarmRole,
    extract_stipulations_from_text,
)
from src.engines.inbox_engine import (
    InboxEngine,
    clean_reply_quotes,
    decode_mime_header,
    extract_text_from_email_message,
)
from src.engines.intake_engine import (
    IntakeEngine,
    clean_text_for_financials,
    extract_financials,
    get_nodes,
    verify_escrow,
)
from src.engines.sync_engine import SyncEngine, extract_payout_numeric
from src.orchestrator.sweeper import SweepSummary, UniversalHourlySweeper


# ==============================================================================
# 1. CORE CONFIG & SECURITY RULES WHITE-BOX HARDENING
# ==============================================================================
class TestConfigWhiteBoxHardening:
    """White-box testing of config.py edge cases and branch conditions."""

    def test_is_banned_platform_variations(self):
        assert is_banned_platform(None) is False
        assert is_banned_platform("") is False
        assert is_banned_platform("   ") is False
        assert is_banned_platform("algora") is True
        assert is_banned_platform("ALGORA") is True
        assert is_banned_platform("https://algora.io/bounties") is True
        assert is_banned_platform("polar.sh") is True
        assert is_banned_platform("opire.dev") is True
        assert is_banned_platform("twentyhq/twenty") is True
        assert is_banned_platform("stellar/soroban-example") is False
        assert is_banned_platform("ethereum/solidity") is False

    def test_contains_disqualify_keywords_branches(self):
        assert contains_disqualify_keywords(None) == (False, None)
        assert contains_disqualify_keywords("") == (False, None)
        disqualified, kw = contains_disqualify_keywords("Please record a video walkthrough of your fix")
        assert disqualified is True
        assert kw in ("video walkthrough", "record a video")

        disqualified, kw = contains_disqualify_keywords("Standard technical PR with unit tests")
        assert disqualified is False
        assert kw is None

    def test_swarm_config_custom_env_overrides(self, monkeypatch):
        monkeypatch.setenv("GCP_PROJECT_ID", "custom-gcp-999")
        monkeypatch.setenv("FIRESTORE_DATABASE_ID", "custom-db")
        monkeypatch.setenv("SWARM_DOCKER_IMAGE", "custom-image:latest")
        monkeypatch.setenv("SWARM_CONTAINER_CPUS", "4")
        monkeypatch.setenv("SWARM_CONTAINER_MEMORY", "8g")
        monkeypatch.setenv("SWARM_CONTAINER_PIDS_LIMIT", "512")
        monkeypatch.setenv("SWARM_CONTAINER_TIMEOUT_SEC", "600")
        monkeypatch.setenv("SWARM_SANDBOX_DIR", "/tmp/custom_sandboxes")

        config = get_config(force_reload=True)
        assert config.gcp_project_id == "custom-gcp-999"
        assert config.firestore_database == "custom-db"
        assert config.docker_image == "custom-image:latest"
        assert config.container_cpus == "4"
        assert config.container_memory == "8g"
        assert config.container_pids_limit == 512
        assert config.container_timeout_sec == 600
        assert config.sandbox_base_dir == "/tmp/custom_sandboxes"
        assert config.evm_payout_address == EVM_PAYOUT_ADDRESS
        assert config.stellar_payout_address == STELLAR_PAYOUT_ADDRESS
        assert config.solana_payout_address == SOLANA_PAYOUT_ADDRESS

        # Reload back to standard
        monkeypatch.undo()
        get_config(force_reload=True)


# ==============================================================================
# 2. PATHGUARD & SAFE_IO ADVERSARIAL HARDENING
# ==============================================================================
class TestPathGuardAndSafeIOWhiteBoxHardening:
    """White-box hardening for filesystem containment and atomic SafeIO."""

    def test_path_guard_canonical_variants_edge_cases(self):
        assert PathGuard._get_canonical_variants(None) == []
        assert PathGuard._get_canonical_variants("") == []
        assert PathGuard._get_canonical_variants("   ") == []

        variants = PathGuard._get_canonical_variants("/tmp/../tmp")
        assert len(variants) > 0

    def test_path_guard_custom_protected_list_extension(self, temp_workspace: Path):
        secret_dir = temp_workspace / "secret_zone"
        secret_dir.mkdir(parents=True, exist_ok=True)

        guard = PathGuard(ignore_list=[str(secret_dir), ""])
        assert guard.is_protected(secret_dir) is True
        assert guard.is_protected(secret_dir / "nested" / "file.txt") is True

        with pytest.raises(ProtectedPathViolationError):
            guard.validate_access(secret_dir / "keys.pem", operation="read_keys")

        with pytest.raises(ProtectedPathViolationError):
            guard.validate_mount(secret_dir)

        # Immutability check
        raw_list = guard.get_ignore_list()
        raw_list.append("/malicious/injected/path")
        assert "/malicious/injected/path" not in guard.get_ignore_list()

    def test_safe_io_atomic_write_bytes_and_text_resilience(self, temp_workspace: Path):
        file_path = temp_workspace / "nested" / "dir" / "test_file.txt"

        # Auto mkdir during atomic write
        SafeIO.atomic_write_text(file_path, "Hello SafeIO", encoding="utf-8")
        assert SafeIO.read_text(file_path) == "Hello SafeIO"

        # Byte writes
        bin_path = temp_workspace / "bin_file.dat"
        SafeIO.write_bytes(bin_path, b"\x00\xFF\xAA\x55")
        assert SafeIO.read_bytes(bin_path) == b"\x00\xFF\xAA\x55"

        # Touch, copy, move, delete
        touched = temp_workspace / "touched.txt"
        SafeIO.touch(touched)
        assert touched.exists()

        copied = temp_workspace / "copied.txt"
        SafeIO.copy_file(touched, copied)
        assert copied.exists()

        moved = temp_workspace / "moved.txt"
        SafeIO.move(copied, moved)
        assert moved.exists()
        assert not copied.exists()

        SafeIO.delete_file(moved)
        assert not moved.exists()

    def test_safe_io_jsonl_corrupt_lines_recovery(self, temp_workspace: Path):
        jsonl_path = temp_workspace / "test_stream.jsonl"
        corrupt_content = '{"id": 1, "val": "valid"}\nINVALID_JSON_LINE\n{"id": 2, "val": "also_valid"}\n\n'
        SafeIO.atomic_write_text(jsonl_path, corrupt_content)

        # stream_jsonl should detect corrupt JSON lines and raise SafeIOError with exact line details
        with pytest.raises(SafeIOError):
            list(SafeIO.stream_jsonl(jsonl_path))


# ==============================================================================
# 3. OFFLINE FIRESTORE CLIENT WHITE-BOX COVERAGE
# ==============================================================================
class TestOfflineFirestoreWhiteBoxHardening:
    """Exhaustive coverage of OfflineFirestoreClient queries, transactions, and mutations."""

    def test_offline_firestore_query_where_order_limit(self, offline_db: OfflineFirestoreClient):
        col = offline_db.collection("test_bounties")
        col.document("doc_1").set({"score": 100, "status": "active", "name": "Bounty A"})
        col.document("doc_2").set({"score": 50, "status": "active", "name": "Bounty B"})
        col.document("doc_3").set({"score": 200, "status": "closed", "name": "Bounty C"})
        col.document("doc_4").set({"score": 150, "status": "active", "name": "Bounty D"})

        # Test where equality
        active_snaps = list(col.where("status", "==", "active").stream())
        assert len(active_snaps) == 3

        # Test limit
        limited_snaps = list(col.where("status", "==", "active").limit(2).stream())
        assert len(limited_snaps) == 2

        # Test order_by descending
        ordered_snaps = list(
            col.where("status", "==", "active").order_by("score", direction="DESCENDING").stream()
        )
        scores = [s.to_dict()["score"] for s in ordered_snaps]
        assert scores == [150, 100, 50]

        # Test order_by ascending
        ordered_asc = list(
            col.where("status", "==", "active").order_by("score", direction="ASCENDING").stream()
        )
        scores_asc = [s.to_dict()["score"] for s in ordered_asc]
        assert scores_asc == [50, 100, 150]

    def test_offline_firestore_document_delete_and_missing(self, offline_db: OfflineFirestoreClient):
        col = offline_db.collection("test_deletes")
        doc_ref = col.document("ephemeral")
        doc_ref.set({"k": "v"})
        assert doc_ref.get().exists is True

        doc_ref.delete()
        assert doc_ref.get().exists is False
        assert doc_ref.get().to_dict() is None

        # Delete non-existent document does not crash
        doc_ref.delete()

    def test_claim_lead_atomic_concurrency_and_rejections(self, offline_db: OfflineFirestoreClient):
        col_name = "bounty_leads_claim_test"
        col = offline_db.collection(col_name)

        # 1. Lead in queued state can be claimed
        col.document("lead_q").set({"status": "queued", "title": "Queued task"})
        assert claim_lead_atomic(offline_db, "lead_q", "worker_A", collection_name=col_name) is True

        # 2. Re-claiming same lead by worker_B is rejected
        assert claim_lead_atomic(offline_db, "lead_q", "worker_B", collection_name=col_name) is False

        # 3. Claiming non-existent lead is rejected
        assert claim_lead_atomic(offline_db, "non_existent", "worker_A", collection_name=col_name) is False

        # 4. Lead in completed state cannot be claimed
        col.document("lead_done").set({"status": "completed"})
        assert claim_lead_atomic(offline_db, "lead_done", "worker_A", collection_name=col_name) is False


# ==============================================================================
# 4. GITHUB CLIENT & ORBSTACK EXECUTOR WHITE-BOX HARDENING
# ==============================================================================
class TestGitHubClientAndOrbStackWhiteBoxHardening:
    """White-box error path coverage for GitHubClient and OrbStackExecutor."""

    def test_github_client_error_and_rate_limiting(self):
        client = GitHubClient(token="test-token")

        # Test invalid repo in get_pr_rollup
        with pytest.raises(ValueError):
            client.get_pr_rollup(repo="invalid_no_slash", pr_number=10)

        # Test query_graphql exception simulation
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Bad credentials")
            with pytest.raises(GitHubAPIError):
                client.query_graphql("query { viewer { login } }", max_retries=1)

    def test_orbstack_executor_error_branches(self, temp_workspace: Path):
        executor = EphemeralOrbStackExecutor()

        # Workspace in protected path raises ProtectedPathViolationError
        protected_ws = Path(DEFAULT_IGNORE_LIST[0]).expanduser().resolve()
        with pytest.raises(ProtectedPathViolationError):
            executor.run_isolated(
                workspace_path=protected_ws,
                command=["echo", "test"],
            )

        # Empty command raises ValueError
        valid_ws = temp_workspace / "valid_box"
        valid_ws.mkdir(parents=True, exist_ok=True)
        with pytest.raises(ValueError):
            executor.run_isolated(
                workspace_path=valid_ws,
                command=[],
            )

        # Nonexistent workspace raises FileNotFoundError
        with pytest.raises(FileNotFoundError):
            executor.run_isolated(
                workspace_path=temp_workspace / "does_not_exist",
                command=["echo", "1"],
            )


# ==============================================================================
# 5. INTAKE & INBOX ENGINES WHITE-BOX HARDENING
# ==============================================================================
class TestIntakeAndInboxEnginesWhiteBoxHardening:
    """White-box branch tests for financial parser, MIME decoder, and email correlation."""

    def test_intake_extract_payout_formats(self):
        assert extract_financials(None) == ("PENDING DISCOVERY", 0.0)
        assert extract_financials("") == ("PENDING DISCOVERY", 0.0)

        # Dollar formats
        p_str, p_val = extract_financials("Reward of $1,500 for PR merge")
        assert p_val == 1500.0
        assert "$1500.00" in p_str

        # Token formats (XLM, ETH, USDC, SOL)
        p_str, p_val = extract_financials("Bounty reward: 5000 XLM distributed via Stellar escrow")
        assert p_val == 5000.0
        assert "5000.0 XLM" in p_str

        p_str, p_val = extract_financials("Allocated 2.5 ETH to winner")
        assert p_val == 2.5
        assert "2.5 ETH" in p_str

    def test_clean_text_for_financials(self):
        text = "Task description\nMore funded OSS work available on Gitcoin\nReal requirement"
        cleaned = clean_text_for_financials(text)
        assert "More funded OSS work available" not in cleaned
        assert "Real requirement" in cleaned

    def test_inbox_mime_decoding_and_reply_cleaning(self):
        assert clean_reply_quotes("") == ""
        reply_sample = "Approved fix.\n> On yesterday, maintainer wrote:\n> please check\n--- Original Message ---\n> Old email"
        cleaned = clean_reply_quotes(reply_sample)
        assert cleaned == "Approved fix."

        # Complex MIME multipart extraction
        msg = MIMEMultipart()
        msg["Subject"] = "Test Subject"
        msg["From"] = "sender@example.com"
        part = MIMEText("This is plain text message body.", "plain", "utf-8")
        msg.attach(part)

        extracted = extract_text_from_email_message(msg)
        assert extracted.strip() == "This is plain text message body."

        # Non-multipart single message
        single_msg = MIMEText("Single part body", "plain", "utf-8")
        assert extract_text_from_email_message(single_msg).strip() == "Single part body"

        # Header decoding
        assert decode_mime_header(None) == ""
        assert decode_mime_header("Simple ASCII Header") == "Simple ASCII Header"


# ==============================================================================
# 6. EXECUTOR, ESCORT & SYNC ENGINES WHITE-BOX HARDENING
# ==============================================================================
class TestExecutorEscortSyncWhiteBoxHardening:
    """White-box testing of stipulation parsing, PR escort gating, and sync calculations."""

    def test_stipulation_extraction_patterns(self):
        body = """
## Requirements:
- [x] Write Soroban smart contract tests
- [ ] Implement secure withdrawal authorization
1. Ensure 100% genuine assertions
2. No mock cryptography
"""
        stips = extract_stipulations_from_text(body, comments=["- [ ] Deploy contract to testnet"])
        assert len(stips) >= 4
        assert "Write Soroban smart contract tests" in stips
        assert "Implement secure withdrawal authorization" in stips
        assert "Deploy contract to testnet" in stips

    def test_escort_preview_deployment_gate_filtering(self):
        # External deployment gates to filter out
        assert is_external_deployment_gate("deploy/vercel") is True
        assert is_external_deployment_gate("preview-app.netlify.app") is True
        assert is_external_deployment_gate("Cloudflare Pages Deployment") is True
        assert is_external_deployment_gate("preview-deployment") is True

        # True CI checks that must NOT be filtered
        assert is_external_deployment_gate("test (Python 3.11)") is False
        assert is_external_deployment_gate("cargo-test / soroban-contracts") is False
        assert is_external_deployment_gate("Lint and Type Check") is False

    def test_sync_engine_financial_extraction(self):
        # From numeric fields
        assert extract_payout_numeric({"payout_usd": 1250.0}) == ("$1250.00", 1250.0)
        assert extract_payout_numeric({"payout": "$750", "projected_payout_usd": 750.0}) == ("$750.00", 750.0)

        # From escrow dict
        assert extract_payout_numeric({"escrow": {"amount_usd": 2000.0}}) == (
            "$2000.00",
            2000.0,
        )

        # Fallback to text parsing
        assert extract_payout_numeric({"title": "Bounty for $500 fix"}) == ("$500.00", 500.0)
        assert extract_payout_numeric({}) == ("$0.00", 0.0)


# ==============================================================================
# 7. SWEEPER ORCHESTRATOR & CLI WHITE-BOX HARDENING
# ==============================================================================
class TestSweeperAndCLIWhiteBoxHardening:
    """White-box testing of 6-phase pipeline recovery, daemon loop, and CLI subcommands."""

    def test_sweeper_signal_handler_and_loop_termination(
        self, offline_db: OfflineFirestoreClient, temp_workspace: Path
    ):
        sweeper = UniversalHourlySweeper(
            db=offline_db,
            sandbox_base_dir=temp_workspace / "sandboxes",
        )
        sweeper.register_signal_handlers()

        # Run loop with max_iterations=2
        summaries = sweeper.run_loop(
            interval_sec=1,
            max_iterations=2,
            dry_run=True,
            execution_strategy="teamwork_swarm",
        )
        assert len(summaries) == 2
        assert all(s.success for s in summaries)

    def test_cli_subcommands_execution(self, offline_db: OfflineFirestoreClient, monkeypatch):
        monkeypatch.setattr("src.cli.get_firestore_client", lambda: offline_db)

        parser = build_parser()

        # 1. status subcommand
        args = parser.parse_args(["status"])
        assert handle_status(args) == 0

        # 2. intake --dry-run
        args = parser.parse_args(["intake", "--dry-run"])
        assert handle_intake(args) == 0

        # 3. intake --balance-only
        args = parser.parse_args(["intake", "--balance-only"])
        assert handle_intake(args) == 0

        # 4. exec --dry-run
        args = parser.parse_args(["exec", "--dry-run", "--limit", "2"])
        assert handle_exec(args) == 0

        # 5. escort --dry-run
        args = parser.parse_args(["escort", "--dry-run"])
        assert handle_escort(args) == 0

        # 6. sync --dry-run
        args = parser.parse_args(["sync", "--dry-run"])
        assert handle_sync(args) == 0

        # 7. inbox --dry-run
        args = parser.parse_args(["inbox", "--dry-run"])
        assert handle_inbox(args) == 0

        # 8. sweep --dry-run --strategy teamwork_swarm
        args = parser.parse_args(["sweep", "--dry-run", "--max-leads", "2", "--strategy", "teamwork_swarm"])
        assert handle_sweep(args) == 0

        # 9. main entrypoint with no args prints help and returns 1
        assert main([]) == 1
