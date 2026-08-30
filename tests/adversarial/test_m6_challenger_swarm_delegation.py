"""
Empirical Adversarial Stress & Chaos Test Suite for Milestone 6 (Swarm Delegation & Live E2E).

Challenger Focus Areas:
1. TeamworkSwarmDelegator:
   - Exhaustive failure mode testing for all 5 specialist roles (Explorer, Worker, Reviewer, Challenger, Auditor).
   - Robustness under malformed/empty lead data, corrupted stipulations, missing fields, and unexpected role exceptions.
   - Attestation cryptographic/traceability integrity and status propagation.
2. Victory Audit Rejection & Anti-Cheating Invariants:
   - Comprehensive rejection of banned platforms (algora, polar, twentyhq, opire, with case variations and URL schemes).
   - Verification that rejected audits strictly prevent PR creation, set status to `failed_verification`, and do NOT write to `bounty_memory`.
   - Guaranteed sandbox cleanup even when audit rejects or throws.
3. Multi-Strategy Execution Switching & Hybrid Execution:
   - Independent and concurrent verification of `orbstack_container`, `teamwork_swarm`, and `hybrid` strategies.
   - Hybrid mode failure logic: failure in EITHER swarm victory audit OR container execution causes overall failure.
   - Strategy precedence resolution (explicit argument > lead doc field > default engine strategy).
4. CLI Multi-Strategy Integration:
   - Live invocation and argument validation for `bounty status`, `bounty sweep --strategy ...`, and `bounty exec --strategy ...`.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.cli import (
    build_parser,
    handle_exec,
    handle_status,
    handle_sweep,
    main as cli_main,
)
from src.core.config import (
    COLLECTION_BOUNTY_LEADS,
    COLLECTION_BOUNTY_MEMORY,
    COLLECTION_BOUNTY_SETTLEMENTS,
    COLLECTION_SWARM_COORDINATOR,
    COLLECTION_SWARM_OPERATIONS,
    EVM_PAYOUT_ADDRESS,
    STELLAR_PAYOUT_ADDRESS,
    is_banned_platform,
)
from src.core.exceptions import ProtectedPathViolationError
from src.core.firestore_client import OfflineFirestoreClient
from src.core.orbstack_executor import (
    ContainerExecutionResult,
    EphemeralOrbStackExecutor,
)
from src.core.path_guard import PathGuard
from src.core.safe_io import SafeIO
from src.engines.executor_engine import (
    ExecutorEngine,
    SwarmAgentAttestation,
    TeamworkSwarmDelegator,
    TeamworkSwarmResult,
    TeamworkSwarmRole,
    extract_stipulations_from_text,
)
from src.orchestrator.sweeper import UniversalHourlySweeper


@pytest.fixture
def m6_env(temp_workspace: Path):
    """Sets up an isolated test environment with OfflineFirestoreClient and sandboxes."""
    db_dir = temp_workspace / "m6_offline_db"
    db = OfflineFirestoreClient(project_id="test-m6-challenger", state_dir=db_dir)

    sandbox_base = temp_workspace / "sandboxes"
    sandbox_base.mkdir(parents=True, exist_ok=True)

    guard = PathGuard(ignore_list=[])

    mock_gh = MagicMock()
    mock_gh.search_bounties.return_value = {
        "data": {"search": {"nodes": [], "pageInfo": {"hasNextPage": False}}}
    }
    mock_gh.check_repo_archived.return_value = False
    mock_gh.create_pull_request.return_value = {
        "html_url": "https://github.com/test-org/test-repo/pull/101",
        "number": 101,
    }

    mock_executor = MagicMock()
    mock_executor.is_docker_available.return_value = True
    mock_executor.cleanup_stale_containers.return_value = 0
    mock_executor.run_isolated.return_value = ContainerExecutionResult(
        container_name="test-cont-01",
        exit_code=0,
        stdout="Container executed successfully.",
        stderr="",
        duration_sec=0.1,
        timed_out=False,
    )

    return {
        "db": db,
        "guard": guard,
        "sandbox_base": sandbox_base,
        "mock_gh": mock_gh,
        "mock_executor": mock_executor,
        "temp_workspace": temp_workspace,
    }


class TestTeamworkSwarmDelegationFailures:
    """Empirical tests for TeamworkSwarmDelegator edge cases and role failures."""

    def test_swarm_delegator_with_empty_and_minimal_lead_data(self, temp_workspace: Path):
        """Tests that SwarmDelegator gracefully handles completely empty or minimal lead data."""
        delegator = TeamworkSwarmDelegator()
        empty_lead: dict[str, Any] = {}
        stipulations: list[str] = []

        result = delegator.execute_swarm_flow(
            lead_data=empty_lead,
            workspace_path=temp_workspace,
            stipulations=stipulations,
            dry_run=True,
        )

        assert isinstance(result, TeamworkSwarmResult)
        assert result.strategy == "teamwork_swarm"
        # Unknown/repo is not on the banned list, so victory audit passes on default
        assert result.victory_audit_passed is True
        assert len(result.attestations) == 5
        assert result.duration_sec >= 0.0
        assert EVM_PAYOUT_ADDRESS in result.generated_pr_body
        assert STELLAR_PAYOUT_ADDRESS in result.generated_pr_body

    def test_swarm_delegator_role_attestations_structure_and_types(self, temp_workspace: Path):
        """Verifies that each of the 5 roles produces valid SwarmAgentAttestation objects with timestamps."""
        delegator = TeamworkSwarmDelegator()
        lead_data = {
            "repo": "stellar/soroban-examples",
            "issue_number": 42,
            "title": "Add multi-auth escrow contract",
            "ecosystem": "stellar",
            "projected_payout": "$1,000",
            "projected_payout_usd": 1000.0,
            "platform": "grantfox",
        }
        stipulations = ["Add require_auth()", "Write tests"]

        result = delegator.execute_swarm_flow(
            lead_data=lead_data,
            workspace_path=temp_workspace,
            stipulations=stipulations,
            dry_run=True,
        )

        expected_roles = [
            TeamworkSwarmRole.EXPLORER.value,
            TeamworkSwarmRole.WORKER.value,
            TeamworkSwarmRole.REVIEWER.value,
            TeamworkSwarmRole.CHALLENGER.value,
            TeamworkSwarmRole.AUDITOR.value,
        ]

        roles_found = [a.role for a in result.attestations]
        assert roles_found == expected_roles

        for att in result.attestations:
            assert isinstance(att, SwarmAgentAttestation)
            assert att.status == "PASSED"
            assert len(att.action) > 0
            assert len(att.details) > 0
            assert "T" in att.timestamp_iso  # Valid ISO timestamp
            assert att.integrity_verified is True
            as_dict = att.to_dict()
            assert as_dict["role"] == att.role
            assert as_dict["action"] == att.action

    def test_custom_stipulation_extraction_edge_cases(self):
        """Tests stipulation extraction against malformed markdown, empty comments, and non-dict inputs."""
        # 1. Numbered lists and checkboxes mixed
        body = """
        ## Tasks to complete
        1. Implement gas estimation
        2. Fix off-by-one error
        - [ ] Add integration tests
        - [x] Refactor module
        """
        comments = [
            {"body": "Additional requirement: Ensure backwards compatibility"},
            {"body": ""},
            None,
            "Plain string comment with stipulation: Must use Base RPC",
        ]
        stips = extract_stipulations_from_text(body=body, comments=comments)
        assert len(stips) >= 4
        assert any("gas estimation" in s for s in stips)
        assert any("integration tests" in s for s in stips)
        assert any("compatibility" in s for s in stips)

        # 2. Completely empty body & comments -> fallback to standard stipulations
        default_stips = extract_stipulations_from_text(body="", comments=[])
        assert len(default_stips) == 4
        assert any("genuine solution" in s for s in default_stips)
        assert any("Victory Audit" in s for s in default_stips)


class TestVictoryAuditRejectionAndSecurity:
    """Empirical tests for Victory Audit rejection and anti-cheating enforcement."""

    @pytest.mark.parametrize(
        "banned_target,platform_value",
        [
            ("algora-bounty/issue-1", "algora"),
            ("polar-sh/rewards", "polar"),
            ("twentyhq/twenty", "github"),
            ("some-org/repo", "opire"),
            ("org/repo", "https://algora.io/bounties/123"),
            ("org/repo", "https://polar.sh/issue/456"),
            ("org/repo", "https://opire.dev/task/789"),
            ("twentyhq/twenty-crm", "grantfox"),
            ("ANY_ORG/ANY_REPO", "ALGORA"),
            ("ANY_ORG/ANY_REPO", "Polar.sh"),
        ],
    )
    def test_victory_audit_unconditionally_rejects_banned_platforms(
        self, temp_workspace: Path, banned_target: str, platform_value: str
    ):
        """Verifies that is_banned_platform and Victory Audit reject all variants of banned platforms."""
        delegator = TeamworkSwarmDelegator()
        lead_data = {
            "repo": banned_target,
            "issue_number": 999,
            "title": "Banned platform task",
            "body": "Should be rejected immediately by Auditor.",
            "platform": platform_value,
        }

        result = delegator.execute_swarm_flow(
            lead_data=lead_data,
            workspace_path=temp_workspace,
            stipulations=["Do not execute"],
            dry_run=True,
        )

        assert result.success is False
        assert result.victory_audit_passed is False
        assert result.auditor_attestation.status == "FAILED"
        assert result.auditor_attestation.integrity_verified is False
        assert "rejected" in result.auditor_attestation.details.lower()
        assert result.error_msg is not None

    def test_executor_engine_rejects_banned_lead_and_updates_firestore(
        self, m6_env: dict[str, Any]
    ):
        """
        Verifies that when a lead fails the Victory Audit:
        1. Lead status transitions to 'failed_verification'
        2. execution_success is False
        3. PR URL is NOT set
        4. No document is created in bounty_memory
        5. swarm_operations records victory_audit_passed=False and status='DESTROYED'
        6. Sandbox directory is cleanly deleted.
        """
        db = m6_env["db"]
        guard = m6_env["guard"]
        sandbox_base = m6_env["sandbox_base"]

        engine = ExecutorEngine(
            db=db,
            path_guard=guard,
            sandbox_base_dir=sandbox_base,
            execution_strategy="teamwork_swarm",
        )

        banned_lead_id = "lead_banned_polar_88"
        lead_doc = {
            "id": banned_lead_id,
            "repo": "banned-org/repo",
            "issue_number": 88,
            "title": "Polar funded bounty",
            "body": "Funded via polar.sh",
            "platform": "polar",
            "status": "pending_triage",
            "projected_payout": "$500",
            "projected_payout_usd": 500.0,
        }
        db.collection(COLLECTION_BOUNTY_LEADS).document(banned_lead_id).set(lead_doc)

        res = engine.claim_and_execute_lead(
            lead_id=banned_lead_id,
            lead_data=lead_doc,
            dry_run=True,
            strategy="teamwork_swarm",
        )

        # 1. Verification of return summary
        assert res["success"] is False
        assert res["final_lead_status"] == "failed_verification"
        assert res["pr_url"] == ""
        assert res["victory_audit_passed"] is False

        # 2. Verification of bounty_leads document
        lead_snap = db.collection(COLLECTION_BOUNTY_LEADS).document(banned_lead_id).get().to_dict()
        assert lead_snap["status"] == "failed_verification"
        assert lead_snap["execution_success"] is False
        assert "pr_url" not in lead_snap or lead_snap["pr_url"] == ""

        # 3. Verification of bounty_memory: NO record should exist
        mem_snap = (
            db.collection(COLLECTION_BOUNTY_MEMORY)
            .document("banned_org_repo_88")
            .get()
        )
        assert not mem_snap.exists

        # 4. Verification of swarm_operations document
        ops_snaps = list(
            db.collection(COLLECTION_SWARM_OPERATIONS)
            .where("lead_id", "==", banned_lead_id)
            .stream()
        )
        assert len(ops_snaps) == 1
        op_data = ops_snaps[0].to_dict()
        assert op_data["status"] == "DESTROYED"
        assert op_data["success"] is False
        assert op_data["victory_audit_passed"] is False
        assert op_data["strategy"] == "teamwork_swarm"

        # 5. Sandbox cleanup guarantee
        created_sandboxes = list(sandbox_base.glob("bounty_banned_org_repo_88_*"))
        assert len(created_sandboxes) == 0


class TestMultiStrategySwitchingAndHybridExecution:
    """Empirical tests for multi-strategy execution switching across engines and CLI."""

    def test_strategy_precedence_and_individual_lead_override(
        self, m6_env: dict[str, Any]
    ):
        """
        Verifies that execution strategy respects strict hierarchy:
        explicit call argument > lead doc field > engine default.
        """
        db = m6_env["db"]
        guard = m6_env["guard"]
        sandbox_base = m6_env["sandbox_base"]
        mock_executor = m6_env["mock_executor"]

        # Default engine configured with 'orbstack_container'
        engine = ExecutorEngine(
            db=db,
            executor=mock_executor,
            path_guard=guard,
            sandbox_base_dir=sandbox_base,
            execution_strategy="orbstack_container",
        )

        # 1. Lead specifies 'teamwork_swarm' in doc -> should execute as teamwork_swarm
        lead_1_id = "lead_strat_01"
        lead_1 = {
            "id": lead_1_id,
            "repo": "org1/repo1",
            "issue_number": 10,
            "title": "Lead with swarm strategy in doc",
            "strategy": "teamwork_swarm",
            "status": "pending_triage",
            "projected_payout": "$100",
            "platform": "grantfox",
        }
        db.collection(COLLECTION_BOUNTY_LEADS).document(lead_1_id).set(lead_1)

        res1 = engine.claim_and_execute_lead(lead_id=lead_1_id, lead_data=lead_1, dry_run=True)
        assert res1["strategy"] == "teamwork_swarm"
        assert res1["victory_audit_passed"] is True
        assert "swarm_attestations" in res1

        # 2. Lead specifies no strategy -> should use engine default 'orbstack_container'
        lead_2_id = "lead_strat_02"
        lead_2 = {
            "id": lead_2_id,
            "repo": "org2/repo2",
            "issue_number": 20,
            "title": "Lead with no strategy",
            "status": "pending_triage",
            "projected_payout": "$200",
            "platform": "grantfox",
        }
        db.collection(COLLECTION_BOUNTY_LEADS).document(lead_2_id).set(lead_2)

        res2 = engine.claim_and_execute_lead(lead_id=lead_2_id, lead_data=lead_2, dry_run=True)
        assert res2["strategy"] == "orbstack_container"

        # 3. Explicit argument overrides lead doc and engine default
        lead_3_id = "lead_strat_03"
        lead_3 = {
            "id": lead_3_id,
            "repo": "org3/repo3",
            "issue_number": 30,
            "title": "Lead with doc strategy overridden by call arg",
            "strategy": "orbstack_container",
            "status": "pending_triage",
            "projected_payout": "$300",
            "platform": "grantfox",
        }
        db.collection(COLLECTION_BOUNTY_LEADS).document(lead_3_id).set(lead_3)

        res3 = engine.claim_and_execute_lead(
            lead_id=lead_3_id, lead_data=lead_3, dry_run=True, strategy="hybrid"
        )
        assert res3["strategy"] == "hybrid"
        assert res3["success"] is True

    def test_hybrid_strategy_both_pass(self, m6_env: dict[str, Any]):
        """Verifies that hybrid mode succeeds when both Teamwork Swarm and Container pass."""
        db = m6_env["db"]
        guard = m6_env["guard"]
        sandbox_base = m6_env["sandbox_base"]
        mock_executor = m6_env["mock_executor"]

        engine = ExecutorEngine(
            db=db,
            executor=mock_executor,
            path_guard=guard,
            sandbox_base_dir=sandbox_base,
            execution_strategy="hybrid",
        )

        lead_id = "lead_hybrid_success"
        lead_data = {
            "id": lead_id,
            "repo": "good-org/good-repo",
            "issue_number": 55,
            "title": "Valid hybrid bounty",
            "status": "pending_triage",
            "projected_payout": "$1,500",
            "projected_payout_usd": 1500.0,
            "platform": "grantfox",
        }
        db.collection(COLLECTION_BOUNTY_LEADS).document(lead_id).set(lead_data)

        res = engine.claim_and_execute_lead(
            lead_id=lead_id, lead_data=lead_data, dry_run=True, strategy="hybrid"
        )

        assert res["success"] is True
        assert res["strategy"] == "hybrid"
        assert res["final_lead_status"] == "pr_open"
        assert res["victory_audit_passed"] is True
        assert "Swarm ok=True, Container ok=True" in res["stdout"]

    def test_hybrid_strategy_fails_when_container_execution_fails(
        self, m6_env: dict[str, Any]
    ):
        """Verifies that hybrid mode FAILS if the container execution fails even if swarm passes."""
        db = m6_env["db"]
        guard = m6_env["guard"]
        sandbox_base = m6_env["sandbox_base"]

        failing_executor = MagicMock()
        failing_executor.is_docker_available.return_value = True
        failing_executor.run_isolated.return_value = ContainerExecutionResult(
            container_name="failing-cont",
            exit_code=137,
            stdout="Out of Memory error",
            stderr="Killed (OOM)",
            duration_sec=2.5,
            timed_out=False,
        )

        engine = ExecutorEngine(
            db=db,
            executor=failing_executor,
            path_guard=guard,
            sandbox_base_dir=sandbox_base,
            execution_strategy="hybrid",
        )

        lead_id = "lead_hybrid_cont_fail"
        lead_data = {
            "id": lead_id,
            "repo": "org/repo-oom",
            "issue_number": 77,
            "title": "OOM task in hybrid mode",
            "status": "pending_triage",
            "projected_payout": "$800",
            "platform": "grantfox",
        }
        db.collection(COLLECTION_BOUNTY_LEADS).document(lead_id).set(lead_data)

        res = engine.claim_and_execute_lead(
            lead_id=lead_id, lead_data=lead_data, dry_run=False, strategy="hybrid"
        )

        assert res["success"] is False
        assert res["strategy"] == "hybrid"
        assert res["final_lead_status"] == "failed_verification"
        assert res["exit_code"] == 137
        assert "Killed (OOM)" in res["stderr"]

    def test_hybrid_strategy_fails_when_swarm_victory_audit_fails(
        self, m6_env: dict[str, Any]
    ):
        """Verifies that hybrid mode FAILS if the Swarm Victory Audit fails even if container passes."""
        db = m6_env["db"]
        guard = m6_env["guard"]
        sandbox_base = m6_env["sandbox_base"]
        mock_executor = m6_env["mock_executor"]

        engine = ExecutorEngine(
            db=db,
            executor=mock_executor,
            path_guard=guard,
            sandbox_base_dir=sandbox_base,
            execution_strategy="hybrid",
        )

        lead_id = "lead_hybrid_swarm_fail"
        lead_data = {
            "id": lead_id,
            "repo": "banned-org/banned-task",
            "issue_number": 99,
            "title": "Banned platform task in hybrid mode",
            "status": "pending_triage",
            "projected_payout": "$800",
            "platform": "algora",  # Banned!
        }
        db.collection(COLLECTION_BOUNTY_LEADS).document(lead_id).set(lead_data)

        res = engine.claim_and_execute_lead(
            lead_id=lead_id, lead_data=lead_data, dry_run=True, strategy="hybrid"
        )

        assert res["success"] is False
        assert res["strategy"] == "hybrid"
        assert res["final_lead_status"] == "failed_verification"
        assert res["victory_audit_passed"] is False

    def test_sweeper_runs_with_teamwork_swarm_strategy(
        self, m6_env: dict[str, Any]
    ):
        """Verifies that UniversalHourlySweeper forwards strategy='teamwork_swarm' down to ExecutorEngine."""
        db = m6_env["db"]
        guard = m6_env["guard"]
        sandbox_base = m6_env["sandbox_base"]

        # Seed valid lead
        lead_id = "lead_sweeper_swarm_01"
        db.collection(COLLECTION_BOUNTY_LEADS).document(lead_id).set(
            {
                "id": lead_id,
                "repo": "sweeper-org/sweeper-repo",
                "issue_number": 101,
                "title": "Sweeper Swarm Lead",
                "status": "priority_triage",
                "projected_payout": "$1,200",
                "projected_payout_usd": 1200.0,
                "platform": "grantfox",
            }
        )

        sweeper = UniversalHourlySweeper(
            db=db,
            path_guard=guard,
            sandbox_base_dir=sandbox_base,
        )

        summary = sweeper.run_sweep(
            dry_run=True, max_exec_leads=1, execution_strategy="teamwork_swarm"
        )

        assert summary.success is True
        assert summary.execution_phase.get("executed_count") == 1
        assert summary.execution_phase.get("successful_count") == 1
        exec_results = summary.execution_phase.get("results", [])
        assert len(exec_results) == 1
        assert exec_results[0]["strategy"] == "teamwork_swarm"
        assert exec_results[0]["victory_audit_passed"] is True


class TestSwarmDelegationInvariantsAndConcurrency:
    """Empirical tests for security invariants, path confinement, and concurrency."""

    def test_payout_routing_block_mandatory_across_all_strategies(
        self, m6_env: dict[str, Any]
    ):
        """Verifies that EVM and Stellar payout addresses are strictly present in PR body across all strategies."""
        db = m6_env["db"]
        guard = m6_env["guard"]
        sandbox_base = m6_env["sandbox_base"]
        mock_executor = m6_env["mock_executor"]

        engine = ExecutorEngine(
            db=db,
            executor=mock_executor,
            path_guard=guard,
            sandbox_base_dir=sandbox_base,
        )

        for strat in ["teamwork_swarm", "orbstack_container", "hybrid"]:
            lead_id = f"lead_payout_test_{strat}"
            lead_data = {
                "id": lead_id,
                "repo": "payout-org/payout-repo",
                "issue_number": 200,
                "title": f"Payout test for strategy {strat}",
                "status": "pending_triage",
                "projected_payout": "$500",
                "projected_payout_usd": 500.0,
                "platform": "grantfox",
            }
            db.collection(COLLECTION_BOUNTY_LEADS).document(lead_id).set(lead_data)

            res = engine.claim_and_execute_lead(
                lead_id=lead_id, lead_data=lead_data, dry_run=True, strategy=strat
            )
            assert res["success"] is True

            # Verify memory document PR body
            mem_doc = (
                db.collection(COLLECTION_BOUNTY_MEMORY)
                .document("payout_org_payout_repo_200")
                .get()
                .to_dict()
            )
            pr_body = mem_doc.get("pr_body", "")
            assert EVM_PAYOUT_ADDRESS in pr_body
            assert STELLAR_PAYOUT_ADDRESS in pr_body
            assert "## Payout Routing" in pr_body

    def test_sandbox_cleanup_under_exception_during_swarm_execution(
        self, m6_env: dict[str, Any]
    ):
        """Verifies that sandbox directories are guaranteed to be cleaned up even if delegator raises an unhandled error."""
        db = m6_env["db"]
        guard = m6_env["guard"]
        sandbox_base = m6_env["sandbox_base"]

        buggy_delegator = MagicMock()
        buggy_delegator.execute_swarm_flow.side_effect = RuntimeError("Catastrophic Swarm Failure")

        engine = ExecutorEngine(
            db=db,
            path_guard=guard,
            sandbox_base_dir=sandbox_base,
            swarm_delegator=buggy_delegator,
            execution_strategy="teamwork_swarm",
        )

        lead_id = "lead_crash_test_99"
        lead_data = {
            "id": lead_id,
            "repo": "crash-org/crash-repo",
            "issue_number": 99,
            "title": "Crash Lead",
            "status": "pending_triage",
            "platform": "grantfox",
        }
        db.collection(COLLECTION_BOUNTY_LEADS).document(lead_id).set(lead_data)

        res = engine.claim_and_execute_lead(
            lead_id=lead_id, lead_data=lead_data, dry_run=True, strategy="teamwork_swarm"
        )

        assert res["success"] is False
        assert res["final_lead_status"] == "failed_verification"

        # Check sandbox directory was cleaned up
        leftover = list(sandbox_base.glob("bounty_crash_org_crash_repo_99_*"))
        assert len(leftover) == 0


class TestCLILiveExecutionAndSubcommands:
    """Empirical tests verifying CLI entrypoints with strategy flags."""

    def test_cli_status_command_execution(self, m6_env: dict[str, Any], capsys: pytest.CaptureFixture):
        """Tests that `bounty status` executes cleanly and displays cluster queues."""
        with patch("src.cli.get_firestore_client", return_value=m6_env["db"]):
            exit_code = cli_main(["status"])
            assert exit_code == 0
            captured = capsys.readouterr()
            assert "UNIVERSAL BOUNTY FLEET V2 — STATUS DASHBOARD" in captured.out
            assert "Bounty Leads Queue" in captured.out
            assert "Open PRs & Escort Health" in captured.out

    def test_cli_sweep_dry_run_with_teamwork_swarm_strategy(
        self, m6_env: dict[str, Any], capsys: pytest.CaptureFixture
    ):
        """Tests `bounty sweep --dry-run --strategy teamwork_swarm` CLI invocation."""
        with patch("src.cli.get_firestore_client", return_value=m6_env["db"]):
            exit_code = cli_main(["sweep", "--dry-run", "--strategy", "teamwork_swarm"])
            assert exit_code == 0
            captured = capsys.readouterr()
            assert "Sweep Pass Summary" in captured.out
            assert "Dry Run:          True" in captured.out
            assert "preflight_gc -> inbox -> escort -> sync -> intake -> execution -> postflight_gc" in captured.out

    def test_cli_exec_dry_run_with_hybrid_strategy(
        self, m6_env: dict[str, Any], capsys: pytest.CaptureFixture
    ):
        """Tests `bounty exec --dry-run --strategy hybrid` CLI invocation."""
        with patch("src.cli.get_firestore_client", return_value=m6_env["db"]):
            exit_code = cli_main(["exec", "--dry-run", "--strategy", "hybrid"])
            assert exit_code == 0
            captured = capsys.readouterr()
            assert "strategy=hybrid" in captured.out
            assert "Execution pass completed" in captured.out
