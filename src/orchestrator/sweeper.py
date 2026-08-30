"""
Universal Hourly Sweeper Orchestrator — Monolithic 6-Phase Pipeline & Daemon Scheduler.

Consolidates all modular engines into a unified sequential execution pipeline:
  Phase 0: Preflight GC (cleanup orphaned containers & stale tmpfs/sandboxes)
  Phase 1: Inbox Phase (drain UNSEEN maintainer/CI emails via InboxEngine)
  Phase 2: Escort Phase (monitor open PR health & CI rollups via EscortEngine)
  Phase 3: Sync Phase (extract settlements & update ledger via SyncEngine)
  Phase 4: Intake Phase (GraphQL ingestion, Sniper Filter & Load Balancer via IntakeEngine)
  Phase 5: Execution Phase (atomic lead claim & isolated execution via ExecutorEngine)
  Phase 6: Postflight GC (teardown leftover sandboxes/containers & memory flush)

Supports both single-sweep invocation and recurring daemon loop mode with graceful
shutdown handling on SIGINT/SIGTERM.
"""

from __future__ import annotations

import logging
import signal
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.core.config import (
    COLLECTION_SWARM_COORDINATOR,
    DEFAULT_SANDBOX_BASE_DIR,
    get_config,
)
from src.core.firestore_client import get_firestore_client
from src.core.github_client import GitHubClient, get_github_client
from src.core.orbstack_executor import EphemeralOrbStackExecutor
from src.core.path_guard import DEFAULT_PATH_GUARD, PathGuard
from src.core.safe_io import SafeIO
from src.engines.escort_engine import EscortEngine
from src.engines.executor_engine import ExecutorEngine
from src.engines.inbox_engine import InboxEngine
from src.engines.intake_engine import IntakeEngine
from src.engines.sync_engine import SyncEngine

logger = logging.getLogger("UniversalBountyV2.Sweeper")


@dataclass
class SweepSummary:
    """Comprehensive telemetry report for a single sweeper execution pass."""

    sweep_id: str = field(default_factory=lambda: f"sweep_{uuid.uuid4().hex[:12]}")
    started_at_iso: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    completed_at_iso: str = ""
    duration_sec: float = 0.0
    success: bool = True
    dry_run: bool = False
    phase_order: list[str] = field(default_factory=list)
    preflight_gc: dict[str, Any] = field(default_factory=dict)
    inbox_phase: dict[str, Any] = field(default_factory=dict)
    escort_phase: dict[str, Any] = field(default_factory=dict)
    sync_phase: dict[str, Any] = field(default_factory=dict)
    intake_phase: dict[str, Any] = field(default_factory=dict)
    execution_phase: dict[str, Any] = field(default_factory=dict)
    postflight_gc: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Converts telemetry dataclass to standard dictionary."""
        return asdict(self)


class UniversalHourlySweeper:
    """
    Monolithic Hourly Sweeper Orchestrator for Universal Bounty Engine V2.
    Executes the 6 core phases sequentially and provides daemon lifecycle management.
    """

    def __init__(
        self,
        db: Any | None = None,
        path_guard: PathGuard | None = None,
        github_client: GitHubClient | None = None,
        orbstack_executor: EphemeralOrbStackExecutor | None = None,
        inbox_engine: InboxEngine | None = None,
        escort_engine: EscortEngine | None = None,
        sync_engine: SyncEngine | None = None,
        intake_engine: IntakeEngine | None = None,
        executor_engine: ExecutorEngine | None = None,
        sandbox_base_dir: str | Path | None = None,
        coordinator_collection: str = COLLECTION_SWARM_COORDINATOR,
    ):
        self.path_guard = path_guard or DEFAULT_PATH_GUARD
        self.db = db if db is not None else get_firestore_client()
        self.github_client = github_client or get_github_client()
        self.orbstack_executor = orbstack_executor or EphemeralOrbStackExecutor(
            path_guard=self.path_guard
        )

        raw_sandbox = (
            sandbox_base_dir
            or get_config().sandbox_base_dir
            or DEFAULT_SANDBOX_BASE_DIR
        )
        self.sandbox_base_dir = self.path_guard.validate_access(
            raw_sandbox, operation="sweeper_sandbox_init"
        )
        SafeIO.mkdir(self.sandbox_base_dir, parents=True, exist_ok=True)

        self.inbox_engine = inbox_engine or InboxEngine(
            db=self.db, path_guard=self.path_guard
        )
        self.escort_engine = escort_engine or EscortEngine(
            db=self.db, github_client=self.github_client
        )
        self.sync_engine = sync_engine or SyncEngine(
            db=self.db, github_client=self.github_client
        )
        self.intake_engine = intake_engine or IntakeEngine(
            db=self.db,
            github_client=self.github_client,
            path_guard=self.path_guard,
            seen_cache_path=self.sandbox_base_dir / "seen_issues.json",
        )
        self.executor_engine = executor_engine or ExecutorEngine(
            db=self.db,
            executor=self.orbstack_executor,
            github_client=self.github_client,
            path_guard=self.path_guard,
            sandbox_base_dir=self.sandbox_base_dir,
        )

        self.coordinator_collection = coordinator_collection
        self.stop_requested = False
        self.is_running = False
        self._installed_signals = False

    def register_signal_handlers(self) -> None:
        """Installs SIGINT and SIGTERM handlers for graceful shutdown."""
        if self._installed_signals:
            return

        def _handle_signal(signum: int, frame: Any) -> None:
            sig_name = signal.Signals(signum).name
            logger.info(f"Received {sig_name} signal. Requesting graceful sweeper shutdown...")
            self.stop_requested = True

        try:
            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)
            self._installed_signals = True
            logger.debug("Registered SIGINT/SIGTERM signal handlers for sweeper daemon.")
        except (ValueError, AttributeError) as e:
            logger.debug(f"Could not register signal handlers (likely non-main thread): {e}")

    def run_preflight_gc(self, dry_run: bool = False) -> dict[str, Any]:
        """
        Phase 0: Preflight Garbage Collection.
        Cleans orphaned Docker containers and clears stale/dangling tmpfs sandboxes.
        """
        logger.info("[Sweeper Phase 0] Executing Preflight GC...")
        cleaned_containers = 0
        cleaned_sandboxes = 0
        errors: list[str] = []

        if not dry_run:
            try:
                cleaned_containers = self.orbstack_executor.cleanup_stale_containers()
            except Exception as e:
                err = f"Preflight container cleanup error: {e}"
                logger.warning(err)
                errors.append(err)

            try:
                if self.sandbox_base_dir.exists():
                    for item in self.sandbox_base_dir.iterdir():
                        if item.is_dir() and item.name.startswith("bounty_"):
                            try:
                                SafeIO.rmtree(item, ignore_errors=True)
                                cleaned_sandboxes += 1
                            except Exception as sb_err:
                                errors.append(f"Failed to remove stale sandbox {item}: {sb_err}")
            except Exception as e:
                err = f"Preflight sandbox directory sweep error: {e}"
                logger.warning(err)
                errors.append(err)
        else:
            logger.info("[DryRun] Preflight GC simulated.")

        result = {
            "status": "COMPLETED",
            "cleaned_containers": cleaned_containers,
            "cleaned_sandboxes": cleaned_sandboxes,
            "errors": errors,
        }
        logger.info(
            f"[Sweeper Phase 0] Preflight GC done: {cleaned_containers} containers, {cleaned_sandboxes} sandboxes cleaned."
        )
        return result

    def run_inbox_phase(self, dry_run: bool = False) -> dict[str, Any]:
        """
        Phase 1: Inbox Phase.
        Drains UNSEEN maintainer/CI emails via InboxEngine.
        """
        logger.info("[Sweeper Phase 1] Executing Inbox Phase...")
        errors: list[str] = []
        res: dict[str, Any] = {}

        try:
            if not dry_run and self.inbox_engine.is_configured():
                res = self.inbox_engine.run_sweep()
            else:
                res = {
                    "processed_count": 0,
                    "processed_emails": [],
                    "skipped": True,
                    "reason": "dry_run" if dry_run else "unconfigured_imap",
                }
        except Exception as e:
            err = f"Inbox phase error: {e}"
            logger.error(err, exc_info=True)
            errors.append(err)
            res = {"processed_count": 0, "processed_emails": [], "error": err}

        res["errors"] = errors
        logger.info(
            f"[Sweeper Phase 1] Inbox Phase done: {res.get('processed_count', 0)} emails processed."
        )
        return res

    def run_escort_phase(self, dry_run: bool = False) -> dict[str, Any]:
        """
        Phase 2: Escort Phase.
        Monitors open PRs via EscortEngine, filtering preview deployment gates.
        """
        logger.info("[Sweeper Phase 2] Executing Escort Phase...")
        errors: list[str] = []
        res: dict[str, Any] = {}

        try:
            res = self.escort_engine.run_sweep()
        except Exception as e:
            err = f"Escort phase error: {e}"
            logger.error(err, exc_info=True)
            errors.append(err)
            res = {
                "total_monitored": 0,
                "ci_failures_count": 0,
                "stalled_count": 0,
                "results": [],
                "error": err,
            }

        res["errors"] = errors
        logger.info(
            f"[Sweeper Phase 2] Escort Phase done: {res.get('total_monitored', 0)} PRs monitored, "
            f"{res.get('ci_failures_count', 0)} CI fixes needed, {res.get('stalled_count', 0)} stalled."
        )
        return res

    def run_sync_phase(self, dry_run: bool = False) -> dict[str, Any]:
        """
        Phase 3: Sync Phase.
        Extracts settlements via SyncEngine, updates ledger and coordinator state.
        """
        logger.info("[Sweeper Phase 3] Executing Sync Phase...")
        errors: list[str] = []
        res: dict[str, Any] = {}

        try:
            res = self.sync_engine.run_sweep()
        except Exception as e:
            err = f"Sync phase error: {e}"
            logger.error(err, exc_info=True)
            errors.append(err)
            res = {
                "synced_count": 0,
                "total_settled_count": 0,
                "total_settled_usd": 0.0,
                "error": err,
            }

        res["errors"] = errors
        logger.info(
            f"[Sweeper Phase 3] Sync Phase done: {res.get('synced_count', 0)} newly synced, "
            f"Total Settled=${res.get('total_settled_usd', 0.0):.2f} across {res.get('total_settled_count', 0)} bounties."
        )
        return res

    def run_intake_phase(
        self,
        dry_run: bool = False,
        max_pages: int = 1,
    ) -> dict[str, Any]:
        """
        Phase 4: Intake Phase.
        Queries GraphQL via IntakeEngine, applies Sniper Filter and Anti-Spam Load Balancer.
        """
        logger.info("[Sweeper Phase 4] Executing Intake Phase...")
        errors: list[str] = []
        res: dict[str, Any] = {}

        try:
            if not dry_run:
                res = self.intake_engine.run_sweep()
            else:
                promoted = self.intake_engine.balance_queue()
                res = {
                    "ingested_count": 0,
                    "promoted_count": len(promoted),
                    "ingested_leads": [],
                    "promoted_leads": promoted,
                    "dry_run": True,
                }
        except Exception as e:
            err = f"Intake phase error: {e}"
            logger.error(err, exc_info=True)
            errors.append(err)
            res = {
                "ingested_count": 0,
                "promoted_count": 0,
                "ingested_leads": [],
                "promoted_leads": [],
                "error": err,
            }

        res["errors"] = errors
        logger.info(
            f"[Sweeper Phase 4] Intake Phase done: {res.get('ingested_count', 0)} ingested, "
            f"{res.get('promoted_count', 0)} promoted."
        )
        return res

    def run_execution_phase(
        self,
        dry_run: bool = False,
        limit: int = 4,
        skip_clone: bool = False,
        strategy: str | None = None,
    ) -> dict[str, Any]:
        """
        Phase 5: Execution Phase.
        Claims leads via ExecutorEngine, executes in ephemeral OrbStack containers
        or via Teamwork Multi-Agent Swarm, creates Draft PRs with Web3 payout routing.
        """
        logger.info(
            f"[Sweeper Phase 5] Executing Execution Phase (limit={limit}, dry_run={dry_run}, strategy={strategy or self.executor_engine.execution_strategy})..."
        )
        errors: list[str] = []
        res: dict[str, Any] = {}

        try:
            res = self.executor_engine.run_sweep(
                limit=limit, dry_run=dry_run, skip_clone=skip_clone, strategy=strategy
            )
        except Exception as e:
            err = f"Execution phase error: {e}"
            logger.error(err, exc_info=True)
            errors.append(err)
            res = {
                "executed_count": 0,
                "successful_count": 0,
                "results": [],
                "error": err,
            }

        res["errors"] = errors
        logger.info(
            f"[Sweeper Phase 5] Execution Phase done: {res.get('executed_count', 0)} executed, "
            f"{res.get('successful_count', 0)} successful."
        )
        return res

    def run_postflight_gc(self, dry_run: bool = False) -> dict[str, Any]:
        """
        Phase 6: Postflight Garbage Collection.
        Teardown, memory flush, leftover container destruction, and clean state assurance.
        """
        logger.info("[Sweeper Phase 6] Executing Postflight GC...")
        cleaned_containers = 0
        cleaned_sandboxes = 0
        errors: list[str] = []

        if not dry_run:
            try:
                cleaned_containers = self.orbstack_executor.cleanup_stale_containers()
            except Exception as e:
                err = f"Postflight container cleanup error: {e}"
                logger.warning(err)
                errors.append(err)

            try:
                if self.sandbox_base_dir.exists():
                    for item in self.sandbox_base_dir.iterdir():
                        if item.is_dir() and item.name.startswith("bounty_"):
                            try:
                                SafeIO.rmtree(item, ignore_errors=True)
                                cleaned_sandboxes += 1
                            except Exception as sb_err:
                                errors.append(f"Failed to remove sandbox {item}: {sb_err}")
            except Exception as e:
                err = f"Postflight sandbox directory sweep error: {e}"
                logger.warning(err)
                errors.append(err)
        else:
            logger.info("[DryRun] Postflight GC simulated.")

        result = {
            "status": "COMPLETED",
            "cleaned_containers": cleaned_containers,
            "cleaned_sandboxes": cleaned_sandboxes,
            "errors": errors,
        }
        logger.info(
            f"[Sweeper Phase 6] Postflight GC done: {cleaned_containers} containers, {cleaned_sandboxes} sandboxes cleaned."
        )
        return result

    def _persist_sweep_telemetry(self, summary: SweepSummary) -> None:
        """Persists sweep telemetry to swarm_coordinator collection in Firestore."""
        try:
            state_ref = self.db.collection(self.coordinator_collection).document("state")
            update_data = {
                "last_sweep_id": summary.sweep_id,
                "last_sweep_success": summary.success,
                "last_sweep_duration_sec": summary.duration_sec,
                "last_sweep_iso": summary.completed_at_iso,
                "last_sweep_summary": {
                    "sweep_id": summary.sweep_id,
                    "success": summary.success,
                    "dry_run": summary.dry_run,
                    "duration_sec": summary.duration_sec,
                    "errors_count": len(summary.errors),
                },
                "last_sweep": {
                    "sweep_id": summary.sweep_id,
                    "started_at_iso": summary.started_at_iso,
                    "completed_at_iso": summary.completed_at_iso,
                    "duration_sec": summary.duration_sec,
                    "success": summary.success,
                    "dry_run": summary.dry_run,
                    "errors_count": len(summary.errors),
                    "inbox_emails_processed": summary.inbox_phase.get("processed_count", 0),
                    "prs_monitored": summary.escort_phase.get("total_monitored", 0),
                    "leads_ingested": summary.intake_phase.get("ingested_count", 0),
                    "leads_executed": summary.execution_phase.get("executed_count", 0),
                    "leads_succeeded": summary.execution_phase.get("successful_count", 0),
                    "total_settled_usd": summary.sync_phase.get("total_settled_usd", 0.0),
                },
                "status": "HEALTHY" if summary.success else "DEGRADED",
                "updated_at_iso": datetime.now(timezone.utc).isoformat(),
            }
            state_ref.set(update_data, merge=True)
            logger.debug(f"Persisted sweep telemetry {summary.sweep_id} to coordinator state.")
        except Exception as e:
            logger.warning(f"Could not persist sweep telemetry to coordinator state: {e}")

    def run_sweep(
        self,
        dry_run: bool = False,
        max_exec_leads: int = 4,
        max_intake_pages: int = 1,
        skip_clone: bool = False,
        execution_strategy: str | None = None,
    ) -> SweepSummary:
        """
        Executes a single, complete 6-phase sweep pass sequentially:
          1. run_preflight_gc()
          2. run_inbox_phase()
          3. run_escort_phase()
          4. run_sync_phase()
          5. run_intake_phase()
          6. run_execution_phase()
          7. run_postflight_gc()
        Guarantees phase ordering and error recovery (postflight GC always runs).
        """
        start_time = time.perf_counter()
        summary = SweepSummary(dry_run=dry_run)
        logger.info(f"=== Starting Universal Hourly Sweep [{summary.sweep_id}] (dry_run={dry_run}) ===")

        # Phase 0: Preflight GC
        try:
            summary.phase_order.append("preflight_gc")
            summary.preflight_gc = self.run_preflight_gc(dry_run=dry_run)
            if summary.preflight_gc.get("errors"):
                summary.errors.extend(summary.preflight_gc["errors"])
        except Exception as e:
            err = f"Preflight GC uncaught exception: {e}"
            logger.error(err, exc_info=True)
            summary.errors.append(err)
            summary.preflight_gc = {"error": err}

        # Phase 1: Inbox Phase
        try:
            summary.phase_order.append("inbox")
            summary.inbox_phase = self.run_inbox_phase(dry_run=dry_run)
            if summary.inbox_phase.get("errors"):
                summary.errors.extend(summary.inbox_phase["errors"])
        except Exception as e:
            err = f"Inbox phase uncaught exception: {e}"
            logger.error(err, exc_info=True)
            summary.errors.append(err)
            summary.inbox_phase = {"error": err}

        # Phase 2: Escort Phase
        try:
            summary.phase_order.append("escort")
            summary.escort_phase = self.run_escort_phase(dry_run=dry_run)
            if summary.escort_phase.get("errors"):
                summary.errors.extend(summary.escort_phase["errors"])
        except Exception as e:
            err = f"Escort phase uncaught exception: {e}"
            logger.error(err, exc_info=True)
            summary.errors.append(err)
            summary.escort_phase = {"error": err}

        # Phase 3: Sync Phase
        try:
            summary.phase_order.append("sync")
            summary.sync_phase = self.run_sync_phase(dry_run=dry_run)
            if summary.sync_phase.get("errors"):
                summary.errors.extend(summary.sync_phase["errors"])
        except Exception as e:
            err = f"Sync phase uncaught exception: {e}"
            logger.error(err, exc_info=True)
            summary.errors.append(err)
            summary.sync_phase = {"error": err}

        # Phase 4: Intake Phase
        try:
            summary.phase_order.append("intake")
            summary.intake_phase = self.run_intake_phase(
                dry_run=dry_run, max_pages=max_intake_pages
            )
            if summary.intake_phase.get("errors"):
                summary.errors.extend(summary.intake_phase["errors"])
        except Exception as e:
            err = f"Intake phase uncaught exception: {e}"
            logger.error(err, exc_info=True)
            summary.errors.append(err)
            summary.intake_phase = {"error": err}

        # Phase 5: Execution Phase
        try:
            summary.phase_order.append("execution")
            summary.execution_phase = self.run_execution_phase(
                dry_run=dry_run,
                limit=max_exec_leads,
                skip_clone=skip_clone,
                strategy=execution_strategy,
            )
            if summary.execution_phase.get("errors"):
                summary.errors.extend(summary.execution_phase["errors"])
        except Exception as e:
            err = f"Execution phase uncaught exception: {e}"
            logger.error(err, exc_info=True)
            summary.errors.append(err)
            summary.execution_phase = {"error": err}

        # Phase 6: Postflight GC (Always guaranteed to run)
        try:
            summary.phase_order.append("postflight_gc")
            summary.postflight_gc = self.run_postflight_gc(dry_run=dry_run)
            if summary.postflight_gc.get("errors"):
                summary.errors.extend(summary.postflight_gc["errors"])
        except Exception as e:
            err = f"Postflight GC uncaught exception: {e}"
            logger.error(err, exc_info=True)
            summary.errors.append(err)
            summary.postflight_gc = {"error": err}

        duration = time.perf_counter() - start_time
        summary.duration_sec = round(duration, 4)
        summary.completed_at_iso = datetime.now(timezone.utc).isoformat()
        summary.success = len(summary.errors) == 0

        # Persist coordinator state
        self._persist_sweep_telemetry(summary)

        logger.info(
            f"=== Universal Hourly Sweep [{summary.sweep_id}] Complete in {summary.duration_sec}s "
            f"(success={summary.success}, errors={len(summary.errors)}) ==="
        )
        return summary

    def run_loop(
        self,
        interval_sec: int = 3600,
        max_iterations: int | None = None,
        dry_run: bool = False,
        max_exec_leads: int = 4,
        skip_clone: bool = False,
        execution_strategy: str | None = None,
        on_iteration: Callable[[SweepSummary], None] | None = None,
    ) -> list[SweepSummary]:
        """
        Runs recurring daemon loop on interval_sec schedule with graceful SIGINT/SIGTERM shutdown.
        """
        self.register_signal_handlers()
        self.is_running = True
        self.stop_requested = False
        iteration = 0
        summaries: list[SweepSummary] = []

        logger.info(
            f"Starting Universal Hourly Sweeper daemon loop: interval={interval_sec}s, "
            f"max_iterations={max_iterations}, dry_run={dry_run}, strategy={execution_strategy or self.executor_engine.execution_strategy}"
        )

        try:
            while not self.stop_requested:
                iteration += 1
                logger.info(f"--- Starting Sweeper Daemon Loop Iteration #{iteration} ---")

                summary = self.run_sweep(
                    dry_run=dry_run,
                    max_exec_leads=max_exec_leads,
                    skip_clone=skip_clone,
                    execution_strategy=execution_strategy,
                )
                summaries.append(summary)

                if on_iteration:
                    try:
                        on_iteration(summary)
                    except Exception as cb_err:
                        logger.warning(f"Error in on_iteration callback: {cb_err}")

                if max_iterations and iteration >= max_iterations:
                    logger.info(f"Reached max_iterations limit ({max_iterations}). Exiting loop.")
                    break

                if self.stop_requested:
                    logger.info("Stop requested. Exiting daemon loop.")
                    break

                # Responsive sleeping in 1-second chunks if interval > 0
                if interval_sec > 0:
                    logger.info(f"Sleeping for {interval_sec} seconds until next sweep pass...")
                    slept = 0
                    while slept < interval_sec and not self.stop_requested:
                        time.sleep(1)
                        slept += 1

        finally:
            self.is_running = False
            logger.info(f"Sweeper daemon loop terminated after {iteration} iteration(s).")

        return summaries
