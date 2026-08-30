"""
Universal Bounty Engine V2 — Unified CLI Application.

Provides unified command-line management for the entire bounty engine fleet:
  bounty sweep    : Executes monolithic 6-phase hourly sweeper pipeline (single or loop mode)
  bounty intake   : Queries GitHub GraphQL, applies Sniper Filter & balances intake queue
  bounty exec     : Claims pending triage leads & executes in isolated OrbStack sandboxes
  bounty escort   : Monitors open PR rollups, evaluates CI health & detects staleness
  bounty sync     : Extracts verified settlements & updates coordinator state ledger
  bounty inbox    : Drains unread maintainer/CI feedback emails from Gmail IMAP
  bounty migrate  : Ports historical intake queues & Firestore state to V2 schema
  bounty status   : Outputs comprehensive cluster metrics, queue status & settlement totals
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Sequence

from src.core.config import (
    COLLECTION_BOUNTY_LEADS,
    COLLECTION_BOUNTY_MEMORY,
    COLLECTION_BOUNTY_SETTLEMENTS,
    COLLECTION_SWARM_COORDINATOR,
)
from src.core.firestore_client import get_firestore_client
from src.core.orbstack_executor import EphemeralOrbStackExecutor
from src.engines.escort_engine import EscortEngine
from src.engines.executor_engine import ExecutorEngine
from src.engines.inbox_engine import InboxEngine
from src.engines.intake_engine import IntakeEngine
from src.engines.sync_engine import SyncEngine
from src.migrate import migrate_queues
from src.orchestrator.sweeper import UniversalHourlySweeper

logger = logging.getLogger("UniversalBountyV2.CLI")


def setup_logging(verbose: bool = False) -> None:
    """Configures root logging format and level."""
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def handle_sweep(args: argparse.Namespace) -> int:
    """Handles `bounty sweep` command."""
    sweeper = UniversalHourlySweeper()
    strategy = getattr(args, "strategy", None)
    if args.loop:
        print(f"Starting Universal Hourly Sweeper in loop mode (interval={args.interval}s, strategy={strategy or 'orbstack_container'})...")
        loop_kwargs = {
            "interval_sec": args.interval,
            "max_iterations": args.max_iterations,
            "dry_run": args.dry_run,
            "max_exec_leads": args.max_leads,
        }
        if strategy:
            loop_kwargs["execution_strategy"] = strategy
        summaries = sweeper.run_loop(**loop_kwargs)
        print(f"Loop completed: {len(summaries)} sweep iteration(s) executed.")
        return 0
    else:
        print(f"Running single sweep pass (dry_run={args.dry_run}, max_leads={args.max_leads}, strategy={strategy or 'orbstack_container'})...")
        sweep_kwargs = {
            "dry_run": args.dry_run,
            "max_exec_leads": args.max_leads,
        }
        if strategy:
            sweep_kwargs["execution_strategy"] = strategy
        summary = sweeper.run_sweep(**sweep_kwargs)
        print("\n=== Sweep Pass Summary ===")
        print(f"  Sweep ID:         {summary.sweep_id}")
        print(f"  Duration:         {summary.duration_sec:.2f}s")
        print(f"  Success:          {summary.success}")
        print(f"  Dry Run:          {summary.dry_run}")
        print(f"  Phase Order:      {' -> '.join(summary.phase_order)}")
        print(f"  Inbox Emails:     {summary.inbox_phase.get('processed_count', 0)}")
        print(f"  PRs Monitored:    {summary.escort_phase.get('total_monitored', 0)}")
        print(f"  CI Fixes Needed:  {summary.escort_phase.get('ci_failures_count', 0)}")
        print(f"  Newly Synced:     {summary.sync_phase.get('synced_count', 0)}")
        print(f"  Total Settled:    ${summary.sync_phase.get('total_settled_usd', 0.0):.2f}")
        print(f"  Leads Ingested:   {summary.intake_phase.get('ingested_count', 0)}")
        print(f"  Leads Promoted:   {summary.intake_phase.get('promoted_count', 0)}")
        print(f"  Leads Executed:   {summary.execution_phase.get('executed_count', 0)}")
        print(f"  Leads Succeeded:  {summary.execution_phase.get('successful_count', 0)}")
        if summary.errors:
            print(f"  Errors ({len(summary.errors)}):")
            for err in summary.errors[:5]:
                print(f"    - {err}")
        return 0 if summary.success else 1


def handle_intake(args: argparse.Namespace) -> int:
    """Handles `bounty intake` command."""
    engine = IntakeEngine()
    if args.balance_only:
        print("Running intake queue load balancer only...")
        promoted = engine.balance_queue()
        print(f"Promoted {len(promoted)} lead(s) into active triage.")
        return 0

    print(f"Running intake sweep (pages={args.pages}, dry_run={args.dry_run})...")
    if args.dry_run:
        bounties = engine.fetch_bounties(max_pages_per_cat=args.pages)
        print(f"Fetched {len(bounties)} raw candidate issues across categories.")
        promoted = engine.balance_queue()
        print(f"Simulated load balance: {len(promoted)} lead(s) would be active.")
    else:
        res = engine.run_sweep()
        print(f"Intake completed: {res['ingested_count']} lead(s) ingested, {res['promoted_count']} promoted.")
    return 0


def handle_exec(args: argparse.Namespace) -> int:
    """Handles `bounty exec` command."""
    strategy = getattr(args, "strategy", None)
    engine = ExecutorEngine(execution_strategy=strategy or "orbstack_container")
    if args.lead_id:
        print(f"Executing single lead: {args.lead_id} (dry_run={args.dry_run}, strategy={strategy or 'orbstack_container'})...")
        res = engine.claim_and_execute_lead(
            lead_id=args.lead_id, dry_run=args.dry_run, strategy=strategy
        )
        print(f"Execution Result for {args.lead_id}:")
        print(f"  Success:  {res.get('success')}")
        print(f"  Status:   {res.get('final_lead_status')}")
        print(f"  PR URL:   {res.get('pr_url')}")
        return 0 if res.get("success") else 1

    print(f"Executing pending triage leads (limit={args.limit}, dry_run={args.dry_run}, strategy={strategy or 'orbstack_container'})...")
    res = engine.run_sweep(limit=args.limit, strategy=strategy)
    print(f"Execution pass completed: {res['executed_count']} executed, {res['successful_count']} succeeded.")
    return 0


def handle_escort(args: argparse.Namespace) -> int:
    """Handles `bounty escort` command."""
    engine = EscortEngine(stale_days_threshold=args.threshold_days)
    print(f"Running PR Escort sweep (staleness threshold={args.threshold_days} days)...")
    res = engine.run_sweep()
    print(f"Escort completed: {res['total_monitored']} PR(s) audited, "
          f"{res['ci_failures_count']} CI failure(s), {res['stalled_count']} stalled.")
    return 0


def handle_sync(args: argparse.Namespace) -> int:
    """Handles `bounty sync` command."""
    engine = SyncEngine()
    print("Running Settlement & Coordinator Sync pass...")
    res = engine.run_sweep()
    print(f"Sync completed: {res['synced_count']} newly recorded, "
          f"Total Settled=${res['total_settled_usd']:.2f} across {res['total_settled_count']} settlements.")
    return 0


def handle_inbox(args: argparse.Namespace) -> int:
    """Handles `bounty inbox` command."""
    engine = InboxEngine()
    if not engine.is_configured():
        print("Notice: GMAIL_USER and GMAIL_APP_PASSWORD not configured in environment.")
        return 0
    print(f"Draining unread IMAP maintainer/CI feedback (mark_seen={not args.dry_run})...")
    emails = engine.fetch_unread_emails(mark_seen=not args.dry_run)
    print(f"Inbox drainage complete: {len(emails)} email(s) processed.")
    return 0


def handle_migrate(args: argparse.Namespace) -> int:
    """Handles `bounty migrate` command."""
    print(f"Running queue migration: source={args.source}, target={args.target}, dry_run={args.dry_run}...")
    res = migrate_queues(
        source_jsonl=args.source,
        target_jsonl=args.target,
        target_dir=args.target_dir,
        firestore_sync=args.firestore,
        dry_run=args.dry_run,
        backup=not args.no_backup,
    )
    print("Migration Summary:")
    print(f"  Total Read:         {res.total_read}")
    print(f"  Valid Records:      {res.valid_records}")
    print(f"  Skipped Duplicates: {res.skipped_duplicates}")
    print(f"  Migrated Records:   {res.migrated_records}")
    print(f"  Firestore Synced:   {res.firestore_synced}")
    if res.backup_path:
        print(f"  Backup Created:     {res.backup_path}")
    if res.errors:
        print(f"  Errors/Warnings:    {len(res.errors)}")
        for err in res.errors[:5]:
            print(f"    - {err}")
    return 0


def handle_status(args: argparse.Namespace) -> int:
    """Handles `bounty status` command."""
    db = get_firestore_client()
    executor = EphemeralOrbStackExecutor()
    docker_ok = executor.is_docker_available()

    print("==========================================================")
    print("         UNIVERSAL BOUNTY FLEET V2 — STATUS DASHBOARD     ")
    print("==========================================================")

    # 1. Coordinator State
    try:
        coord_doc = db.collection(COLLECTION_SWARM_COORDINATOR).document("state").get()
        coord_data = coord_doc.to_dict() or {}
        status_str = coord_data.get("status", "INITIALIZING")
        last_sync = coord_data.get("last_sync_iso") or coord_data.get("updated_at_iso") or "Never"
        print(f"Cluster Status:     {status_str.upper()}")
        print(f"Last Cluster Sync:  {last_sync}")
        print(f"Container Runtime:  {'AVAILABLE (OrbStack/Docker)' if docker_ok else 'UNAVAILABLE / SIMULATION'}")
    except Exception as e:
        print(f"Cluster Status:     ERROR ({e})")

    # 2. Leads Queue Summary
    print("\n--- Bounty Leads Queue ---")
    lead_counts: dict[str, int] = {
        "queued": 0,
        "priority_triage": 0,
        "pending_triage": 0,
        "claimed": 0,
        "running_orbstack": 0,
        "pr_open": 0,
        "completed": 0,
        "failed_verification": 0,
    }
    total_leads = 0
    try:
        leads_snaps = list(db.collection(COLLECTION_BOUNTY_LEADS).stream())
        total_leads = len(leads_snaps)
        for snap in leads_snaps:
            d = snap.to_dict() or {}
            st = d.get("status", "unknown")
            if st in lead_counts:
                lead_counts[st] += 1
            else:
                lead_counts[st] = lead_counts.get(st, 0) + 1

        print(f"  Total Tracked Leads: {total_leads}")
        print(f"  Queued:              {lead_counts.get('queued', 0)}")
        print(f"  Priority Triage:     {lead_counts.get('priority_triage', 0)}")
        print(f"  Pending Triage:      {lead_counts.get('pending_triage', 0)}")
        print(f"  Running (OrbStack):  {lead_counts.get('running_orbstack', 0)}")
        print(f"  PR Open:             {lead_counts.get('pr_open', 0)}")
        print(f"  Completed:           {lead_counts.get('completed', 0)}")
        print(f"  Failed:              {lead_counts.get('failed_verification', 0)}")
    except Exception as e:
        print(f"  Error reading leads queue: {e}")

    # 3. Active PRs Memory
    print("\n--- Open PRs & Escort Health ---")
    try:
        mem_snaps = list(db.collection(COLLECTION_BOUNTY_MEMORY).stream())
        ci_failures = 0
        stalled_prs = 0
        for snap in mem_snaps:
            d = snap.to_dict() or {}
            telemetry = d.get("escort_telemetry", {})
            if telemetry.get("needs_ci_fix"):
                ci_failures += 1
            if telemetry.get("is_stalled"):
                stalled_prs += 1
        print(f"  Active Monitored PRs: {len(mem_snaps)}")
        print(f"  Actionable CI Flags:  {ci_failures}")
        print(f"  Stalled PRs (>14d):   {stalled_prs}")
    except Exception as e:
        print(f"  Error reading PR memory: {e}")

    # 4. Settlements & Financial Totals
    print("\n--- Settlements & Financial Accounting ---")
    try:
        settle_snaps = list(db.collection(COLLECTION_BOUNTY_SETTLEMENTS).stream())
        total_usd = 0.0
        for snap in settle_snaps:
            d = snap.to_dict() or {}
            total_usd += float(d.get("payout_usd", 0.0))
        print(f"  Total Settlements:    {len(settle_snaps)}")
        print(f"  Total Settled USD:    ${total_usd:.2f}")
    except Exception as e:
        print(f"  Error reading settlements: {e}")

    print("==========================================================\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Constructs the unified CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="bounty",
        description="Universal Bounty Engine V2 — Monolithic Orchestrator & Ephemeral Fleet CLI",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose debug logging"
    )

    subparsers = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    # 1. sweep
    sweep_p = subparsers.add_parser("sweep", help="Run 6-phase hourly sweeper pipeline")
    sweep_p.add_argument("--dry-run", action="store_true", help="Simulate sweep pass")
    sweep_p.add_argument("--loop", action="store_true", help="Run in continuous daemon loop mode")
    sweep_p.add_argument(
        "--interval", type=int, default=3600, help="Loop interval in seconds (default: 3600)"
    )
    sweep_p.add_argument(
        "--max-leads", type=int, default=4, help="Max leads to execute in execution phase (default: 4)"
    )
    sweep_p.add_argument(
        "--max-iterations", type=int, default=None, help="Max iterations for daemon loop"
    )
    sweep_p.add_argument(
        "--strategy",
        type=str,
        choices=["orbstack_container", "teamwork_swarm", "hybrid"],
        default=None,
        help="Execution strategy: orbstack_container, teamwork_swarm, or hybrid (default: orbstack_container)",
    )

    # 2. intake
    intake_p = subparsers.add_parser("intake", help="Run bounty intake & sniper filter")
    intake_p.add_argument("--pages", type=int, default=1, help="Max pages per category")
    intake_p.add_argument("--dry-run", action="store_true", help="Dry-run mode")
    intake_p.add_argument("--balance-only", action="store_true", help="Only balance existing queue")

    # 3. exec
    exec_p = subparsers.add_parser("exec", help="Execute pending leads in isolated containers")
    exec_p.add_argument("--limit", type=int, default=4, help="Max leads to execute")
    exec_p.add_argument("--lead-id", type=str, default=None, help="Specific lead ID to execute")
    exec_p.add_argument("--dry-run", action="store_true", help="Dry-run simulation mode")
    exec_p.add_argument(
        "--strategy",
        type=str,
        choices=["orbstack_container", "teamwork_swarm", "hybrid"],
        default=None,
        help="Execution strategy: orbstack_container, teamwork_swarm, or hybrid (default: orbstack_container)",
    )

    # 4. escort
    escort_p = subparsers.add_parser("escort", help="Monitor PR health and CI status")
    escort_p.add_argument(
        "--threshold-days", type=int, default=14, help="Inactivity staleness threshold in days"
    )
    escort_p.add_argument("--dry-run", action="store_true", help="Dry-run audit mode")

    # 5. sync
    sync_p = subparsers.add_parser("sync", help="Extract settlements and update cluster ledger")
    sync_p.add_argument("--dry-run", action="store_true", help="Dry-run sync mode")

    # 6. inbox
    inbox_p = subparsers.add_parser("inbox", help="Drain unread maintainer/CI feedback emails")
    inbox_p.add_argument("--dry-run", action="store_true", help="Do not mark messages as \\Seen")

    # 7. migrate
    migrate_p = subparsers.add_parser("migrate", help="Port intake queue & Firestore state")
    migrate_p.add_argument(
        "--source", "-s", type=str, default="logs/intake_queue.jsonl", help="Source JSONL path"
    )
    migrate_p.add_argument("--target", "-t", type=str, default=None, help="Target JSONL path")
    migrate_p.add_argument("--target-dir", "-d", type=str, default=None, help="Target directory")
    migrate_p.add_argument("--firestore", "-f", action="store_true", help="Sync to Firestore")
    migrate_p.add_argument("--dry-run", action="store_true", help="Simulate migration")
    migrate_p.add_argument("--no-backup", action="store_true", help="Disable backup creation")

    # 8. status
    subparsers.add_parser("status", help="Display cluster queues, PRs, and settlement metrics")

    return parser


def main(args_list: Sequence[str] | None = None) -> int:
    """Main CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(args_list)
    setup_logging(verbose=args.verbose)

    if not args.subcommand:
        parser.print_help()
        return 1

    handlers = {
        "sweep": handle_sweep,
        "intake": handle_intake,
        "exec": handle_exec,
        "escort": handle_escort,
        "sync": handle_sync,
        "inbox": handle_inbox,
        "migrate": handle_migrate,
        "status": handle_status,
    }

    handler = handlers.get(args.subcommand)
    if not handler:
        print(f"Unknown subcommand: {args.subcommand}", file=sys.stderr)
        parser.print_help()
        return 1

    try:
        return handler(args)
    except Exception as e:
        logger.error(f"Error executing '{args.subcommand}': {e}", exc_info=args.verbose)
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
