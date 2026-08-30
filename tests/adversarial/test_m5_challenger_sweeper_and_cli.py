"""
Empirical Adversarial Stress & Chaos Test Suite for Milestone 5 (Universal Bounty V2).

Challenger Focus Areas:
1. UniversalHourlySweeper:
   - Exhaustive partial phase failure testing across all 6 phases + preflight/postflight GC.
   - Unconditional execution guarantee for postflight_gc under single, multiple, and catastrophic cascading failures.
   - Deterministic phase ordering preservation under chaos injection.
   - Coordinator telemetry persistence and health degradation under errors.
   - Daemon loop resilience across repeated error cycles and graceful shutdown signals.
2. CLI Application (`src/cli.py`):
   - Fuzz and stress-test all 8 subcommands (sweep, intake, exec, escort, sync, inbox, migrate, status).
   - Verify argument parsing, type conversion, valid executions, missing flags, and invalid/malformed parameters.
   - Verify top-level CLI entrypoint, error handling, exit code contracts, and help output.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
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
    main as cli_main,
)
from src.core.config import (
    COLLECTION_BOUNTY_LEADS,
    COLLECTION_BOUNTY_MEMORY,
    COLLECTION_BOUNTY_SETTLEMENTS,
    COLLECTION_SWARM_COORDINATOR,
)
from src.core.exceptions import GitHubAPIError, ProtectedPathViolationError
from src.core.firestore_client import OfflineFirestoreClient
from src.core.path_guard import PathGuard
from src.core.safe_io import SafeIO
from src.engines.escort_engine import EscortEngine
from src.engines.executor_engine import ExecutorEngine
from src.engines.inbox_engine import InboxEngine
from src.engines.intake_engine import IntakeEngine
from src.engines.sync_engine import SyncEngine
from src.orchestrator.sweeper import SweepSummary, UniversalHourlySweeper


@pytest.fixture
def m5_env(temp_workspace: Path):
    """Sets up an isolated testing environment with mocked external dependencies."""
    db_dir = temp_workspace / "m5_offline_db"
    db = OfflineFirestoreClient(project_id="test-m5-challenger", state_dir=db_dir)

    sandbox_base = temp_workspace / "sandboxes"
    sandbox_base.mkdir(parents=True, exist_ok=True)

    seen_cache = temp_workspace / "seen_cache.json"
    queue_file = temp_workspace / "logs" / "intake_queue.jsonl"
    queue_file.parent.mkdir(parents=True, exist_ok=True)

    guard = PathGuard(ignore_list=[])

    mock_gh = MagicMock()
    mock_gh.search_bounties.return_value = {
        "data": {
            "search": {
                "nodes": [
                    {
                        "id": "ISSUE_M5_1",
                        "number": 501,
                        "title": "Base Bridge Gas Optimizer ($800)",
                        "body": "GrantFox verified task for Optimism/Base bridge.",
                        "repository": {"nameWithOwner": "base-org/bridge", "isArchived": False},
                        "labels": [{"name": "grantfox"}, {"name": "bounty"}],
                        "comments": [],
                    }
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        }
    }
    mock_gh.check_repo_archived.return_value = False
    mock_gh.create_pull_request.return_value = {
        "html_url": "https://github.com/base-org/bridge/pull/501",
        "number": 501,
    }

    mock_executor = MagicMock()
    mock_executor.is_docker_available.return_value = True
    mock_executor.cleanup_stale_containers.return_value = 0

    inbox = InboxEngine(db=db, path_guard=guard)
    escort = EscortEngine(db=db, github_client=mock_gh)
    sync = SyncEngine(db=db, github_client=mock_gh)
    intake = IntakeEngine(
        db=db,
        github_client=mock_gh,
        path_guard=guard,
        seen_cache_path=seen_cache,
        queue_file_path=queue_file,
    )
    executor = ExecutorEngine(
        db=db,
        executor=mock_executor,
        github_client=mock_gh,
        path_guard=guard,
        sandbox_base_dir=sandbox_base,
    )

    sweeper = UniversalHourlySweeper(
        db=db,
        path_guard=guard,
        github_client=mock_gh,
        orbstack_executor=mock_executor,
        inbox_engine=inbox,
        escort_engine=escort,
        sync_engine=sync,
        intake_engine=intake,
        executor_engine=executor,
        sandbox_base_dir=sandbox_base,
    )

    return {
        "sweeper": sweeper,
        "db": db,
        "sandbox_base": sandbox_base,
        "mock_gh": mock_gh,
        "mock_executor": mock_executor,
        "inbox": inbox,
        "escort": escort,
        "sync": sync,
        "intake": intake,
        "executor": executor,
        "guard": guard,
        "temp_workspace": temp_workspace,
    }


# =============================================================================
# PART 1: UniversalHourlySweeper Partial Failures & Postflight GC Guarantees
# =============================================================================


class TestSweeperPartialPhaseFailures:
    """Stress tests every single phase failure in UniversalHourlySweeper."""

    def test_preflight_gc_failure_does_not_halt_pipeline(self, m5_env):
        """If Preflight GC throws an exception, all other phases and postflight GC still execute."""
        sweeper: UniversalHourlySweeper = m5_env["sweeper"]

        with patch.object(
            sweeper.orbstack_executor,
            "cleanup_stale_containers",
            side_effect=RuntimeError("Docker daemon socket unresponsive"),
        ):
            summary = sweeper.run_sweep(dry_run=False, skip_clone=True)

        assert summary.success is False
        assert any("Docker daemon socket unresponsive" in e for e in summary.errors)
        assert summary.phase_order == [
            "preflight_gc",
            "inbox",
            "escort",
            "sync",
            "intake",
            "execution",
            "postflight_gc",
        ]
        assert summary.postflight_gc.get("status") == "COMPLETED"

    def test_inbox_phase_imap_failure_does_not_halt_pipeline(self, m5_env):
        """If InboxEngine throws an IMAP error, subsequent phases still run."""
        sweeper: UniversalHourlySweeper = m5_env["sweeper"]

        with patch.object(
            sweeper.inbox_engine,
            "run_sweep",
            side_effect=TimeoutError("IMAP connection timed out after 30s"),
        ), patch.object(sweeper.inbox_engine, "is_configured", return_value=True):
            summary = sweeper.run_sweep(dry_run=False, skip_clone=True)

        assert summary.success is False
        assert any("IMAP connection timed out" in e for e in summary.errors)
        assert summary.intake_phase.get("ingested_count", 0) == 1
        assert summary.postflight_gc.get("status") == "COMPLETED"

    def test_escort_phase_github_api_failure_does_not_halt_pipeline(self, m5_env):
        """If EscortEngine throws a GitHub API error, Sync, Intake, Exec, and Postflight still run."""
        sweeper: UniversalHourlySweeper = m5_env["sweeper"]

        with patch.object(
            sweeper.escort_engine,
            "run_sweep",
            side_effect=GitHubAPIError("GraphQL 502 Bad Gateway from GitHub"),
        ):
            summary = sweeper.run_sweep(dry_run=False, skip_clone=True)

        assert summary.success is False
        assert any("GraphQL 502 Bad Gateway" in e for e in summary.errors)
        assert "sync" in summary.phase_order
        assert "intake" in summary.phase_order
        assert "execution" in summary.phase_order
        assert "postflight_gc" in summary.phase_order
        assert summary.postflight_gc.get("status") == "COMPLETED"

    def test_sync_phase_firestore_failure_does_not_halt_pipeline(self, m5_env):
        """If SyncEngine throws an error, Intake, Exec, and Postflight still run."""
        sweeper: UniversalHourlySweeper = m5_env["sweeper"]

        with patch.object(
            sweeper.sync_engine,
            "run_sweep",
            side_effect=RuntimeError("Firestore transaction failed"),
        ):
            summary = sweeper.run_sweep(dry_run=False, skip_clone=True)

        assert summary.success is False
        assert any("Firestore transaction failed" in e for e in summary.errors)
        assert summary.intake_phase.get("ingested_count", 0) == 1
        assert summary.postflight_gc.get("status") == "COMPLETED"

    def test_intake_phase_network_error_does_not_halt_pipeline(self, m5_env):
        """If IntakeEngine throws a network connection error, Execution and Postflight still run."""
        sweeper: UniversalHourlySweeper = m5_env["sweeper"]

        with patch.object(
            sweeper.intake_engine,
            "run_sweep",
            side_effect=ConnectionResetError("Connection reset by peer during intake"),
        ):
            summary = sweeper.run_sweep(dry_run=False, skip_clone=True)

        assert summary.success is False
        assert any("Connection reset by peer" in e for e in summary.errors)
        assert "execution" in summary.phase_order
        assert "postflight_gc" in summary.phase_order
        assert summary.postflight_gc.get("status") == "COMPLETED"

    def test_execution_phase_crash_does_not_prevent_postflight_gc(self, m5_env):
        """If ExecutorEngine encounters an unhandled exception or container crash, Postflight GC ALWAYS runs."""
        sweeper: UniversalHourlySweeper = m5_env["sweeper"]
        sandbox_base: Path = m5_env["sandbox_base"]

        def _crashing_exec(*args, **kwargs):
            # Create a dangling sandbox during execution before crashing
            dangling_sb = sandbox_base / "bounty_crash_test_123"
            dangling_sb.mkdir(parents=True, exist_ok=True)
            (dangling_sb / "test.txt").write_text("leaked")
            raise RuntimeError("Container OOMKilled / OrbStack panic")

        with patch.object(
            sweeper.executor_engine,
            "run_sweep",
            side_effect=_crashing_exec,
        ):
            summary = sweeper.run_sweep(dry_run=False, skip_clone=True)

        assert summary.success is False
        assert any("Container OOMKilled" in e for e in summary.errors)
        assert summary.phase_order[-1] == "postflight_gc"
        assert summary.postflight_gc.get("status") == "COMPLETED"
        assert summary.postflight_gc.get("cleaned_sandboxes") == 1
        assert not (sandbox_base / "bounty_crash_test_123").exists()


class TestSweeperCatastrophicFailuresAndInvariants:
    """Stress tests multi-phase cascading chaos and invariant guarantees."""

    def test_all_phases_failing_cascades_safely_and_executes_postflight_gc(self, m5_env):
        """If phases 0-5 all throw different exceptions, postflight GC still runs and state is marked DEGRADED."""
        sweeper: UniversalHourlySweeper = m5_env["sweeper"]
        db: OfflineFirestoreClient = m5_env["db"]

        with patch.object(sweeper, "run_preflight_gc", side_effect=Exception("Preflight fail")), \
             patch.object(sweeper, "run_inbox_phase", side_effect=Exception("Inbox fail")), \
             patch.object(sweeper, "run_escort_phase", side_effect=Exception("Escort fail")), \
             patch.object(sweeper, "run_sync_phase", side_effect=Exception("Sync fail")), \
             patch.object(sweeper, "run_intake_phase", side_effect=Exception("Intake fail")), \
             patch.object(sweeper, "run_execution_phase", side_effect=Exception("Exec fail")):
            summary = sweeper.run_sweep(dry_run=False)

        assert summary.success is False
        assert len(summary.errors) >= 6
        assert summary.phase_order == [
            "preflight_gc",
            "inbox",
            "escort",
            "sync",
            "intake",
            "execution",
            "postflight_gc",
        ]
        assert summary.postflight_gc.get("status") == "COMPLETED"

        # Verify coordinator state persistence recorded degraded status
        state_doc = db.collection(COLLECTION_SWARM_COORDINATOR).document("state").get()
        assert state_doc.exists
        state_data = state_doc.to_dict()
        assert state_data["status"] == "DEGRADED"
        assert state_data["last_sweep_success"] is False
        assert state_data["last_sweep_id"] == summary.sweep_id

    def test_postflight_gc_exception_does_not_crash_sweep_return(self, m5_env):
        """Even if Postflight GC itself raises an exception, run_sweep returns a valid SweepSummary."""
        sweeper: UniversalHourlySweeper = m5_env["sweeper"]

        with patch.object(sweeper, "run_postflight_gc", side_effect=Exception("GC Disk Error")):
            summary = sweeper.run_sweep(dry_run=False)

        assert isinstance(summary, SweepSummary)
        assert summary.success is False
        assert any("GC Disk Error" in e for e in summary.errors)
        assert summary.postflight_gc.get("error") == "Postflight GC uncaught exception: GC Disk Error"

    def test_daemon_loop_survives_intermittent_sweep_crashes(self, m5_env):
        """Daemon loop survives consecutive crashed sweeps and executes all iterations."""
        sweeper: UniversalHourlySweeper = m5_env["sweeper"]

        call_count = 0

        def failing_sweep(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            summary = SweepSummary(success=False, errors=[f"Sweep error #{call_count}"])
            return summary

        with patch.object(sweeper, "run_sweep", side_effect=failing_sweep):
            summaries = sweeper.run_loop(interval_sec=0, max_iterations=4, dry_run=True)

        assert len(summaries) == 4
        assert call_count == 4
        assert all(not s.success for s in summaries)
        assert not sweeper.is_running


# =============================================================================
# PART 2: CLI Subcommand Testing across all 8 Subcommands
# =============================================================================


class TestCLISubcommandsEmpirical:
    """Empirically tests all 8 subcommands with valid, missing, and invalid arguments."""

    # 1. SWEEP SUBCOMMAND
    def test_cli_subcommand_sweep_valid_dry_run(self, capsys):
        """`bounty sweep --dry-run` executes single pass and returns 0."""
        with patch("src.cli.UniversalHourlySweeper") as MockSweeper:
            instance = MockSweeper.return_value
            mock_summary = SweepSummary(success=True, dry_run=True, duration_sec=0.45)
            mock_summary.phase_order = ["preflight_gc", "inbox", "escort", "sync", "intake", "execution", "postflight_gc"]
            instance.run_sweep.return_value = mock_summary

            exit_code = cli_main(["sweep", "--dry-run", "--max-leads", "2"])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "=== Sweep Pass Summary ===" in captured.out
        assert "Duration:         0.45s" in captured.out
        assert "Success:          True" in captured.out
        instance.run_sweep.assert_called_once_with(dry_run=True, max_exec_leads=2)

    def test_cli_subcommand_sweep_loop_mode(self, capsys):
        """`bounty sweep --loop --interval 10 --max-iterations 2 --dry-run`."""
        with patch("src.cli.UniversalHourlySweeper") as MockSweeper:
            instance = MockSweeper.return_value
            mock_summary = SweepSummary(success=True)
            instance.run_loop.return_value = [mock_summary, mock_summary]

            exit_code = cli_main(["sweep", "--loop", "--interval", "10", "--max-iterations", "2", "--dry-run"])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Loop completed: 2 sweep iteration(s) executed." in captured.out
        instance.run_loop.assert_called_once_with(
            interval_sec=10, max_iterations=2, dry_run=True, max_exec_leads=4
        )

    def test_cli_subcommand_sweep_failure_returns_exit_code_1(self, capsys):
        """`bounty sweep` when sweep has errors returns exit code 1."""
        with patch("src.cli.UniversalHourlySweeper") as MockSweeper:
            instance = MockSweeper.return_value
            mock_summary = SweepSummary(success=False, errors=["Intake failure"])
            instance.run_sweep.return_value = mock_summary

            exit_code = cli_main(["sweep"])

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "Success:          False" in captured.out
        assert "Errors (1):" in captured.out
        assert "Intake failure" in captured.out

    # 2. INTAKE SUBCOMMAND
    def test_cli_subcommand_intake_valid_and_balance_only(self, capsys):
        """`bounty intake --balance-only` and `bounty intake --pages 2 --dry-run`."""
        with patch("src.cli.IntakeEngine") as MockEngine:
            instance = MockEngine.return_value
            instance.balance_queue.return_value = [{"id": "lead_1"}, {"id": "lead_2"}]
            instance.fetch_bounties.return_value = [{"id": "lead_1"}]
            instance.run_sweep.return_value = {"ingested_count": 5, "promoted_count": 2}

            # 1. balance-only
            exit_code_bal = cli_main(["intake", "--balance-only"])
            assert exit_code_bal == 0
            captured_bal = capsys.readouterr()
            assert "Promoted 2 lead(s) into active triage." in captured_bal.out

            # 2. dry-run
            exit_code_dry = cli_main(["intake", "--pages", "2", "--dry-run"])
            assert exit_code_dry == 0
            captured_dry = capsys.readouterr()
            assert "Fetched 1 raw candidate issues" in captured_dry.out

            # 3. live sweep
            exit_code_live = cli_main(["intake"])
            assert exit_code_live == 0
            captured_live = capsys.readouterr()
            assert "Intake completed: 5 lead(s) ingested, 2 promoted." in captured_live.out

    # 3. EXEC SUBCOMMAND
    def test_cli_subcommand_exec_single_lead_and_batch(self, capsys):
        """`bounty exec --lead-id ...` and `bounty exec --limit 3`."""
        with patch("src.cli.ExecutorEngine") as MockEngine:
            instance = MockEngine.return_value
            instance.claim_and_execute_lead.return_value = {
                "success": True,
                "final_lead_status": "pr_open",
                "pr_url": "https://github.com/org/repo/pull/1",
            }
            instance.run_sweep.return_value = {
                "executed_count": 3,
                "successful_count": 3,
            }

            # Single lead success
            exit_code_single = cli_main(["exec", "--lead-id", "lead_test_123", "--dry-run"])
            assert exit_code_single == 0
            captured_single = capsys.readouterr()
            assert "Execution Result for lead_test_123:" in captured_single.out
            assert "PR URL:   https://github.com/org/repo/pull/1" in captured_single.out

            # Batch execution
            exit_code_batch = cli_main(["exec", "--limit", "3"])
            assert exit_code_batch == 0
            captured_batch = capsys.readouterr()
            assert "Execution pass completed: 3 executed, 3 succeeded." in captured_batch.out

    def test_cli_subcommand_exec_single_lead_failure_returns_exit_code_1(self, capsys):
        """`bounty exec --lead-id ...` returning failure returns exit code 1."""
        with patch("src.cli.ExecutorEngine") as MockEngine:
            instance = MockEngine.return_value
            instance.claim_and_execute_lead.return_value = {
                "success": False,
                "final_lead_status": "failed_verification",
                "pr_url": None,
            }
            exit_code = cli_main(["exec", "--lead-id", "lead_failed_456"])

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "Success:  False" in captured.out

    # 4. ESCORT SUBCOMMAND
    def test_cli_subcommand_escort_valid(self, capsys):
        """`bounty escort --threshold-days 21`."""
        with patch("src.cli.EscortEngine") as MockEngine:
            instance = MockEngine.return_value
            instance.run_sweep.return_value = {
                "total_monitored": 8,
                "ci_failures_count": 1,
                "stalled_count": 2,
            }

            exit_code = cli_main(["escort", "--threshold-days", "21"])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Escort completed: 8 PR(s) audited, 1 CI failure(s), 2 stalled." in captured.out
        MockEngine.assert_called_once_with(stale_days_threshold=21)

    # 5. SYNC SUBCOMMAND
    def test_cli_subcommand_sync_valid(self, capsys):
        """`bounty sync`."""
        with patch("src.cli.SyncEngine") as MockEngine:
            instance = MockEngine.return_value
            instance.run_sweep.return_value = {
                "synced_count": 3,
                "total_settled_usd": 2500.0,
                "total_settled_count": 5,
            }

            exit_code = cli_main(["sync"])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Sync completed: 3 newly recorded, Total Settled=$2500.00 across 5 settlements." in captured.out

    # 6. INBOX SUBCOMMAND
    def test_cli_subcommand_inbox_configured_and_unconfigured(self, capsys):
        """`bounty inbox` unconfigured and configured."""
        with patch("src.cli.InboxEngine") as MockEngine:
            instance = MockEngine.return_value

            # Unconfigured
            instance.is_configured.return_value = False
            exit_code_unconf = cli_main(["inbox"])
            assert exit_code_unconf == 0
            captured_unconf = capsys.readouterr()
            assert "Notice: GMAIL_USER and GMAIL_APP_PASSWORD not configured" in captured_unconf.out

            # Configured
            instance.is_configured.return_value = True
            instance.fetch_unread_emails.return_value = [{"subject": "CI Passed"}, {"subject": "Feedback"}]
            exit_code_conf = cli_main(["inbox", "--dry-run"])
            assert exit_code_conf == 0
            captured_conf = capsys.readouterr()
            assert "Inbox drainage complete: 2 email(s) processed." in captured_conf.out
            instance.fetch_unread_emails.assert_called_once_with(mark_seen=False)

    # 7. MIGRATE SUBCOMMAND
    def test_cli_subcommand_migrate_valid(self, capsys, temp_workspace: Path):
        """`bounty migrate --source ... --target ... --dry-run`."""
        src_file = temp_workspace / "old_queue.jsonl"
        src_file.write_text('{"repository": "org/repo", "number": 1, "payout_usd": 500}\n')
        target_file = temp_workspace / "new_queue.jsonl"

        exit_code = cli_main([
            "migrate",
            "--source", str(src_file),
            "--target", str(target_file),
            "--dry-run",
            "--no-backup",
        ])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Migration Summary:" in captured.out
        assert "Total Read:         1" in captured.out
        assert "Migrated Records:   1" in captured.out

    # 8. STATUS SUBCOMMAND
    def test_cli_subcommand_status_valid(self, capsys, m5_env):
        """`bounty status` outputs comprehensive dashboard."""
        db: OfflineFirestoreClient = m5_env["db"]

        # Populate some state in db
        db.collection(COLLECTION_SWARM_COORDINATOR).document("state").set({
            "status": "HEALTHY",
            "last_sync_iso": "2026-08-29T22:00:00Z",
        })
        db.collection(COLLECTION_BOUNTY_LEADS).document("lead_1").set({
            "status": "queued",
        })
        db.collection(COLLECTION_BOUNTY_LEADS).document("lead_2").set({
            "status": "completed",
        })
        db.collection(COLLECTION_BOUNTY_MEMORY).document("pr_1").set({
            "repo": "org/repo",
            "escort_telemetry": {"needs_ci_fix": True, "is_stalled": False},
        })
        db.collection(COLLECTION_BOUNTY_SETTLEMENTS).document("settle_1").set({
            "payout_usd": 1250.0,
        })

        with patch("src.cli.get_firestore_client", return_value=db):
            exit_code = cli_main(["status"])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "UNIVERSAL BOUNTY FLEET V2 — STATUS DASHBOARD" in captured.out
        assert "Cluster Status:     HEALTHY" in captured.out
        assert "Total Tracked Leads: 2" in captured.out
        assert "Queued:              1" in captured.out
        assert "Completed:           1" in captured.out
        assert "Active Monitored PRs: 1" in captured.out
        assert "Actionable CI Flags:  1" in captured.out
        assert "Total Settlements:    1" in captured.out
        assert "Total Settled USD:    $1250.00" in captured.out


# =============================================================================
# PART 3: CLI Error Handling, Invalid Inputs, and Help Trapping
# =============================================================================


class TestCLIInvalidInputsAndErrorHandling:
    """Stress tests invalid arguments, missing subcommands, and unhandled exceptions."""

    def test_cli_no_args_returns_1_and_prints_help(self, capsys):
        """Invoking `bounty` with no arguments prints help and returns exit code 1."""
        exit_code = cli_main([])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "usage: bounty" in captured.out or "usage: bounty" in captured.err

    def test_cli_invalid_subcommand_raises_or_returns_code_2(self):
        """Invoking `bounty invalid_subcmd` causes argparse SystemExit with code 2."""
        with pytest.raises(SystemExit) as exc_info:
            cli_main(["nonexistent_command"])
        assert exc_info.value.code == 2

    def test_cli_invalid_argument_types(self):
        """Invalid types for int flags raise SystemExit with code 2."""
        invalid_cases = [
            ["sweep", "--interval", "not_a_number"],
            ["sweep", "--max-leads", "invalid_leads"],
            ["intake", "--pages", "five"],
            ["exec", "--limit", "zero_point_five"],
            ["escort", "--threshold-days", "many_days"],
        ]

        for args in invalid_cases:
            with pytest.raises(SystemExit) as exc_info:
                cli_main(args)
            assert exc_info.value.code == 2

    def test_cli_unrecognized_arguments(self):
        """Unrecognized flags raise SystemExit with code 2."""
        unrecognized_cases = [
            ["sweep", "--foo-bar"],
            ["intake", "--unknown"],
            ["exec", "--dangerous-exec"],
            ["escort", "--ignore-all"],
            ["sync", "--fake-usd"],
            ["inbox", "--delete-all"],
            ["migrate", "--bypass-schema"],
            ["status", "--all-secrets"],
        ]

        for args in unrecognized_cases:
            with pytest.raises(SystemExit) as exc_info:
                cli_main(args)
            assert exc_info.value.code == 2

    def test_cli_handler_unexpected_exception_caught_and_returns_code_1(self, capsys):
        """When a subcommand handler encounters an unhandled exception, main catches it and returns 1."""
        with patch("src.cli.handle_status", side_effect=RuntimeError("Database corruption crash")):
            exit_code = cli_main(["status"])

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "Error: Database corruption crash" in captured.err
