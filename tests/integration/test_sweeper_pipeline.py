"""
Integration Tests for Universal Hourly Sweeper Pipeline.

Verifies:
1. Full 6-phase sequential execution flow (Preflight GC -> Inbox -> Escort -> Sync -> Intake -> Execution -> Postflight GC).
2. Strict phase execution ordering.
3. Resilience against partial failures (individual phase errors do not crash subsequent phases, and postflight GC always runs).
4. Telemetry persistence to `swarm_coordinator/state`.
5. Preflight and postflight GC cleanup of orphaned containers and sandboxes.
6. Daemon loop iteration and graceful shutdown mechanisms.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.core.config import (
    COLLECTION_BOUNTY_LEADS,
    COLLECTION_BOUNTY_MEMORY,
    COLLECTION_SWARM_COORDINATOR,
)
from src.core.firestore_client import OfflineFirestoreClient
from src.core.path_guard import PathGuard
from src.engines.escort_engine import EscortEngine
from src.engines.executor_engine import ExecutorEngine
from src.engines.inbox_engine import InboxEngine
from src.engines.intake_engine import IntakeEngine
from src.engines.sync_engine import SyncEngine
from src.orchestrator.sweeper import SweepSummary, UniversalHourlySweeper


@pytest.fixture
def sweeper_env(temp_workspace: Path):
    """Sets up a complete isolated environment for the Sweeper pipeline."""
    db_dir = temp_workspace / "offline_db"
    db = OfflineFirestoreClient(project_id="test-sweeper-v2", state_dir=db_dir)

    sandbox_base = temp_workspace / "sandboxes"
    sandbox_base.mkdir(parents=True, exist_ok=True)

    seen_cache = temp_workspace / "seen_cache.json"
    queue_file = temp_workspace / "logs" / "intake_queue.jsonl"
    queue_file.parent.mkdir(parents=True, exist_ok=True)

    guard = PathGuard(ignore_list=[])

    # Mock GitHubClient
    mock_gh = MagicMock()
    mock_gh.search_bounties.return_value = {
        "data": {
            "search": {
                "nodes": [
                    {
                        "id": "ISSUE_INTEG_101",
                        "number": 101,
                        "title": "Stellar Soroban escrow contract ($1,000)",
                        "body": "GrantFox verified bounty for Stellar Soroban bridge.",
                        "repository": {"nameWithOwner": "stellar-org/soroban-bridge", "isArchived": False},
                        "labels": [{"name": "grantfox"}, {"name": "bounty"}],
                        "comments": [],
                    }
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        }
    }
    mock_gh.check_repo_archived.return_value = False

    # Ephemeral container executor mock
    mock_executor = MagicMock()
    mock_executor.is_docker_available.return_value = False
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
    }


class TestSweeperPipelineSequentialExecution:
    """Validates sequential execution and phase ordering."""

    def test_full_pipeline_phase_ordering(self, sweeper_env):
        """Ensures all 6 phases + pre/post GC execute in exact deterministic order."""
        sweeper: UniversalHourlySweeper = sweeper_env["sweeper"]

        summary = sweeper.run_sweep(dry_run=False, skip_clone=True)

        assert isinstance(summary, SweepSummary)
        assert summary.success is True
        assert len(summary.errors) == 0

        expected_order = [
            "preflight_gc",
            "inbox",
            "escort",
            "sync",
            "intake",
            "execution",
            "postflight_gc",
        ]
        assert summary.phase_order == expected_order

    def test_preflight_and_postflight_gc_cleans_dangling_artifacts(self, sweeper_env):
        """Verifies preflight and postflight GC clean up leftover sandboxes and containers."""
        sweeper: UniversalHourlySweeper = sweeper_env["sweeper"]
        sandbox_base: Path = sweeper_env["sandbox_base"]

        # Create dummy leftover sandboxes
        dangling_1 = sandbox_base / "bounty_old_repo_1_deadbeef"
        dangling_2 = sandbox_base / "bounty_old_repo_2_cafebabe"
        dangling_1.mkdir(parents=True, exist_ok=True)
        dangling_2.mkdir(parents=True, exist_ok=True)
        (dangling_1 / "leftover.tmp").write_text("junk")

        res_pre = sweeper.run_preflight_gc()
        assert res_pre["status"] == "COMPLETED"
        assert res_pre["cleaned_sandboxes"] == 2
        assert not dangling_1.exists()
        assert not dangling_2.exists()

        # Create another dummy for postflight
        dangling_post = sandbox_base / "bounty_exec_leftover"
        dangling_post.mkdir(parents=True, exist_ok=True)

        res_post = sweeper.run_postflight_gc()
        assert res_post["status"] == "COMPLETED"
        assert res_post["cleaned_sandboxes"] == 1
        assert not dangling_post.exists()

    def test_coordinator_state_persistence_after_sweep(self, sweeper_env):
        """Verifies swarm_coordinator/state is updated with sweep telemetry."""
        sweeper: UniversalHourlySweeper = sweeper_env["sweeper"]
        db: OfflineFirestoreClient = sweeper_env["db"]

        summary = sweeper.run_sweep(dry_run=False, skip_clone=True)

        state_doc = db.collection(COLLECTION_SWARM_COORDINATOR).document("state").get()
        assert state_doc.exists
        state_data = state_doc.to_dict()

        assert state_data["last_sweep_id"] == summary.sweep_id
        assert state_data["last_sweep_success"] is True
        assert state_data["status"] == "HEALTHY"
        assert "last_sweep_summary" in state_data


class TestSweeperPipelinePartialFailureResilience:
    """Verifies that individual engine errors do not crash the sweeper and postflight GC always runs."""

    def test_inbox_phase_exception_does_not_halt_subsequent_phases(self, sweeper_env):
        """If InboxEngine throws, Escort, Sync, Intake, Exec, and Postflight still run."""
        sweeper: UniversalHourlySweeper = sweeper_env["sweeper"]

        with patch.object(sweeper.inbox_engine, "run_sweep", side_effect=RuntimeError("IMAP connection broken")):
            with patch.object(sweeper.inbox_engine, "is_configured", return_value=True):
                summary = sweeper.run_sweep(dry_run=False, skip_clone=True)

        # Summary captured the error
        assert summary.success is False
        assert any("IMAP connection broken" in err for err in summary.errors)
        # All subsequent phases still executed
        assert summary.phase_order == [
            "preflight_gc",
            "inbox",
            "escort",
            "sync",
            "intake",
            "execution",
            "postflight_gc",
        ]
        assert summary.intake_phase.get("ingested_count") == 1
        assert summary.postflight_gc.get("status") == "COMPLETED"

    def test_sync_phase_exception_does_not_halt_execution(self, sweeper_env):
        """If SyncEngine throws, Intake, Execution, and Postflight still run."""
        sweeper: UniversalHourlySweeper = sweeper_env["sweeper"]

        with patch.object(sweeper.sync_engine, "run_sweep", side_effect=ValueError("Corrupt settlement ledger")):
            summary = sweeper.run_sweep(dry_run=False, skip_clone=True)

        assert summary.success is False
        assert any("Corrupt settlement ledger" in err for err in summary.errors)
        assert "execution" in summary.phase_order
        assert "postflight_gc" in summary.phase_order

    def test_postflight_gc_runs_even_if_execution_phase_explodes(self, sweeper_env):
        """If ExecutorEngine raises uncaught exception, Postflight GC still executes."""
        sweeper: UniversalHourlySweeper = sweeper_env["sweeper"]

        with patch.object(sweeper.executor_engine, "run_sweep", side_effect=Exception("OrbStack daemon crashed")):
            summary = sweeper.run_sweep(dry_run=False, skip_clone=True)

        assert summary.success is False
        assert "postflight_gc" in summary.phase_order
        assert summary.postflight_gc.get("status") == "COMPLETED"


class TestSweeperPipelineEndToEndFlow:
    """Verifies end-to-end data progression across engines within a single sweep."""

    def test_bounty_intake_to_execution_in_single_sweep(self, sweeper_env):
        """
        1. Intake ingests Stellar issue and load balancer promotes it.
        2. Execution phase claims and processes the promoted lead.
        3. PR is created in bounty_memory.
        4. Coordinator state records the completed sweep.
        """
        sweeper: UniversalHourlySweeper = sweeper_env["sweeper"]
        db: OfflineFirestoreClient = sweeper_env["db"]

        summary = sweeper.run_sweep(dry_run=False, max_exec_leads=4, skip_clone=True)

        assert summary.success is True
        assert summary.intake_phase["ingested_count"] == 1
        assert summary.intake_phase["promoted_count"] == 1
        assert summary.execution_phase["executed_count"] == 1
        assert summary.execution_phase["successful_count"] == 1

        # Check lead in db
        leads = list(db.collection(COLLECTION_BOUNTY_LEADS).stream())
        assert len(leads) == 1
        lead_data = leads[0].to_dict()
        assert lead_data["status"] == "pr_open"
        assert "pr_url" in lead_data

        # Check PR memory record
        mem_snaps = list(db.collection(COLLECTION_BOUNTY_MEMORY).stream())
        assert len(mem_snaps) == 1
        mem_data = mem_snaps[0].to_dict()
        assert mem_data["repo"] == "stellar-org/soroban-bridge"
        assert mem_data["is_draft"] is True

    def test_dry_run_mode_does_not_mutate_remote_state(self, sweeper_env):
        """Dry-run mode does not claim or execute live containers or fetch live IMAP."""
        sweeper: UniversalHourlySweeper = sweeper_env["sweeper"]

        summary = sweeper.run_sweep(dry_run=True)

        assert summary.dry_run is True
        assert summary.success is True
        assert summary.inbox_phase.get("skipped") is True


class TestSweeperDaemonLoopMode:
    """Verifies multi-iteration loop mode, iteration callbacks, and graceful termination."""

    def test_run_loop_with_max_iterations(self, sweeper_env):
        """Sweeper runs requested iterations and terminates."""
        sweeper: UniversalHourlySweeper = sweeper_env["sweeper"]

        iteration_records = []

        def callback(s: SweepSummary):
            iteration_records.append(s.sweep_id)

        summaries = sweeper.run_loop(
            interval_sec=0,
            max_iterations=3,
            dry_run=True,
            on_iteration=callback,
        )

        assert len(summaries) == 3
        assert len(iteration_records) == 3
        assert not sweeper.is_running

    def test_run_loop_graceful_stop_requested(self, sweeper_env):
        """Setting stop_requested halts the loop cleanly."""
        sweeper: UniversalHourlySweeper = sweeper_env["sweeper"]

        def stop_after_first(s: SweepSummary):
            sweeper.stop_requested = True

        summaries = sweeper.run_loop(
            interval_sec=0,
            max_iterations=10,
            dry_run=True,
            on_iteration=stop_after_first,
        )

        assert len(summaries) == 1
        assert not sweeper.is_running
