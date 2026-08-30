"""
Queue & State Migration Utility for Universal Bounty Engine V2.

Provides the `migrate_queues` function and CLI interface to securely port historical
`intake_queue.jsonl` files and Firestore records to the V2 schema with automatic
schema validation, deduplication, timestamped backups, priority partitioning,
and atomic persistence.
"""

import argparse
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from src.core.config import COLLECTION_BOUNTY_LEADS
from src.core.exceptions import MigrationError
from src.core.firestore_client import (
    get_firestore_client,
)
from src.core.safe_io import SafeIO

logger = logging.getLogger("UniversalBountyV2.Migrate")


@dataclass
class MigrationResult:
    """Telemetry report produced by migrate_queues."""

    total_read: int = 0
    valid_records: int = 0
    skipped_duplicates: int = 0
    migrated_records: int = 0
    firestore_synced: int = 0
    backup_path: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    dry_run: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def generate_canonical_doc_id(repo: str, issue_number: Union[int, str]) -> str:
    """
    Generates canonical document ID in the format: {clean_repo}_{issue_number}
    Example: 'stellar/soroban-example', 42 -> 'stellar_soroban_example_42'
    """
    clean_repo = str(repo).replace("/", "_").replace("-", "_").replace(".", "_").lower()
    return f"{clean_repo}_{issue_number}"


def normalize_lead_schema(raw_record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Validates and transforms a legacy or partial lead record into the canonical V2 schema.
    Returns normalized dict if valid, or None if irrecoverable/invalid.
    """
    if not isinstance(raw_record, dict):
        return None

    # 1. Resolve repository
    repo = ""
    repo_val = raw_record.get("repository") or raw_record.get("repo") or raw_record.get("repo_name")
    if isinstance(repo_val, dict):
        repo = repo_val.get("nameWithOwner") or repo_val.get("name") or ""
    elif isinstance(repo_val, str):
        repo = repo_val.strip()

    # 2. Resolve issue number
    raw_num = (
        raw_record.get("number")
        or raw_record.get("issue_number")
        or raw_record.get("num")
    )
    if raw_num is None:
        # Attempt to parse from URL if available
        url_str = raw_record.get("url") or raw_record.get("html_url") or ""
        if "/issues/" in url_str:
            try:
                raw_num = int(url_str.rstrip("/").split("/issues/")[-1])
            except Exception:
                pass

    if raw_num is None:
        return None

    try:
        issue_number = int(raw_num)
    except (ValueError, TypeError):
        return None

    if not repo:
        # Attempt to infer repo from url
        url_str = raw_record.get("url") or raw_record.get("html_url") or ""
        if "github.com/" in url_str:
            parts = url_str.split("github.com/")[-1].split("/")
            if len(parts) >= 2:
                repo = f"{parts[0]}/{parts[1]}"

    if not repo:
        return None

    # 3. Canonical doc ID
    # Ensure doc_id matches standard format
    standard_doc_id = generate_canonical_doc_id(repo, issue_number)

    # 4. Resolve labels
    raw_labels = raw_record.get("labels") or []
    labels_list: List[str] = []
    if isinstance(raw_labels, list):
        for item in raw_labels:
            if isinstance(item, dict):
                name = item.get("name")
                if name:
                    labels_list.append(str(name))
            elif isinstance(item, str):
                labels_list.append(item.strip())

    # 5. Financial payouts
    raw_usd = raw_record.get("projected_payout_usd") or raw_record.get("payout_usd") or raw_record.get("usd_value")
    try:
        projected_payout_usd = float(raw_usd) if raw_usd is not None else 0.0
    except (ValueError, TypeError):
        projected_payout_usd = 0.0

    projected_payout_str = str(
        raw_record.get("projected_payout")
        or raw_record.get("payout")
        or f"${projected_payout_usd:.0f}"
    )

    # 6. Priority & Status
    priority = str(raw_record.get("priority", "standard")).lower()
    if priority not in ("high", "standard", "low"):
        priority = "high" if projected_payout_usd >= 100.0 else "standard"

    status = str(raw_record.get("status", "queued")).lower()

    # 7. Ecosystem & Escrow
    ecosystem = str(raw_record.get("ecosystem", "general")).lower()
    escrow_verified = bool(raw_record.get("escrow_verified", False))

    # 8. Timestamps
    now_iso = datetime.now(timezone.utc).isoformat()
    created_at = raw_record.get("created_at") or raw_record.get("createdAt") or now_iso
    updated_at = raw_record.get("updated_at") or raw_record.get("updatedAt") or now_iso

    return {
        "id": standard_doc_id,
        "doc_id": standard_doc_id,
        "node_id": str(raw_record.get("node_id") or raw_record.get("id") or ""),
        "number": issue_number,
        "issue_number": issue_number,
        "title": str(raw_record.get("title", "")),
        "url": str(raw_record.get("url") or raw_record.get("html_url") or f"https://github.com/{repo}/issues/{issue_number}"),
        "body": str(raw_record.get("body", "")),
        "repository": repo,
        "labels": labels_list,
        "priority": priority,
        "status": status,
        "projected_payout": projected_payout_str,
        "projected_payout_usd": projected_payout_usd,
        "qualification_reason": str(raw_record.get("qualification_reason", "Migrated lead record")),
        "ecosystem": ecosystem,
        "escrow_verified": escrow_verified,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def deduplicate_leads(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Deduplicates lead records by canonical doc ID and node ID.
    When duplicates exist, preserves the record with the highest payout,
    or richer fields.
    """
    seen: Dict[str, Dict[str, Any]] = {}

    for record in records:
        doc_id = record.get("id") or record.get("doc_id")
        if not doc_id:
            continue

        if doc_id not in seen:
            seen[doc_id] = record
        else:
            existing = seen[doc_id]
            # Replace if new record has higher USD payout or is in a more advanced status
            existing_usd = existing.get("projected_payout_usd", 0.0)
            new_usd = record.get("projected_payout_usd", 0.0)

            if new_usd > existing_usd:
                seen[doc_id] = record
            elif len(record.get("body", "")) > len(existing.get("body", "")):
                # If higher body length / richer context, merge
                merged = dict(existing)
                merged.update(record)
                seen[doc_id] = merged

    # Sort records: high priority first, then descending by projected_payout_usd
    def sort_key(rec: Dict[str, Any]) -> Tuple[int, float]:
        prio_val = 0 if rec.get("priority") == "high" else (1 if rec.get("priority") == "standard" else 2)
        payout = rec.get("projected_payout_usd", 0.0)
        return (prio_val, -payout)

    sorted_records = list(seen.values())
    sorted_records.sort(key=sort_key)
    return sorted_records


def migrate_queues(
    source_jsonl: Union[str, Path],
    target_dir: Optional[Union[str, Path]] = None,
    target_jsonl: Optional[Union[str, Path]] = None,
    firestore_sync: bool = False,
    db: Optional[Any] = None,
    dry_run: bool = False,
    backup: bool = True,
) -> MigrationResult:
    """
    Migrates leads from historical source JSONL and/or Firestore into V2 normalized state.

    Args:
        source_jsonl: Path to input intake_queue.jsonl
        target_dir: Directory where v2 output files should be written (defaults to source parent or logs/)
        target_jsonl: Explicit path to output v2 JSONL file
        firestore_sync: Whether to write migrated records to Firestore collection
        db: Firestore client instance (defaults to get_firestore_client())
        dry_run: If True, calculates migration metrics without writing to disk/Firestore
        backup: If True, creates timestamped backup of existing target files

    Returns:
        MigrationResult dataclass with telemetry.
    """
    result = MigrationResult(dry_run=dry_run)
    src_path = Path(source_jsonl).expanduser().resolve()

    # Validate source path against PathGuard
    SafeIO.get_guard().validate_access(src_path, operation="migrate_read_source")

    raw_records: List[Dict[str, Any]] = []

    # 1. Read source JSONL if present
    if src_path.exists():
        try:
            for rec in SafeIO.stream_jsonl(src_path):
                result.total_read += 1
                raw_records.append(rec)
        except Exception as e:
            err_msg = f"Error reading source JSONL {src_path}: {e}"
            logger.error(err_msg)
            result.errors.append(err_msg)
            raise MigrationError(err_msg) from e
    else:
        logger.warning(f"Source JSONL file does not exist: {src_path}")

    # 2. Normalize records
    valid_normalized: List[Dict[str, Any]] = []
    for raw in raw_records:
        norm = normalize_lead_schema(raw)
        if norm:
            valid_normalized.append(norm)
            result.valid_records += 1
        else:
            result.errors.append(f"Discarded invalid/unparseable record: {str(raw)[:100]}")

    # 3. Deduplicate
    deduped = deduplicate_leads(valid_normalized)
    result.skipped_duplicates = len(valid_normalized) - len(deduped)
    result.migrated_records = len(deduped)

    # Resolve target paths
    if target_jsonl:
        dest_path = Path(target_jsonl).expanduser().resolve()
    elif target_dir:
        dest_dir = Path(target_dir).expanduser().resolve()
        dest_path = dest_dir / "intake_queue.jsonl"
    else:
        dest_path = src_path

    # Validate target path against PathGuard
    SafeIO.get_guard().validate_access(dest_path, operation="migrate_write_target")

    if dry_run:
        logger.info(
            f"[DRY-RUN] Migration evaluated: {result.total_read} read, "
            f"{result.valid_records} valid, {result.skipped_duplicates} duplicate(s) skipped, "
            f"{result.migrated_records} candidate record(s) ready."
        )
        return result

    # 4. Create backup of existing target if requested
    if backup and dest_path.exists():
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        bak_path = dest_path.parent / f"{dest_path.name}.bak_{timestamp}"
        try:
            SafeIO.copy_file(dest_path, bak_path)
            result.backup_path = str(bak_path)
            logger.info(f"Created pre-migration backup at {bak_path}")
        except Exception as e:
            logger.warning(f"Could not create backup of {dest_path}: {e}")

    # 5. Atomically persist migrated JSONL to target
    try:
        SafeIO.write_jsonl(dest_path, deduped, atomic=True)
        logger.info(f"Successfully wrote {len(deduped)} migrated records to {dest_path}")
    except Exception as e:
        err_msg = f"Failed to persist migrated JSONL to {dest_path}: {e}"
        logger.error(err_msg)
        result.errors.append(err_msg)
        raise MigrationError(err_msg) from e

    # 6. Synchronize to Firestore if requested
    if firestore_sync and deduped:
        firestore_db = db or get_firestore_client()
        leads_col = firestore_db.collection(COLLECTION_BOUNTY_LEADS)

        # Batch writes in chunks of 400 (under 500 Firestore limit)
        chunk_size = 400
        synced_count = 0

        for i in range(0, len(deduped), chunk_size):
            chunk = deduped[i : i + chunk_size]
            batch = firestore_db.batch()

            for item in chunk:
                doc_id = item["id"]
                doc_ref = leads_col.document(doc_id)
                batch.set(doc_ref, item, merge=True)

            try:
                batch.commit()
                synced_count += len(chunk)
            except Exception as e:
                err_msg = f"Firestore batch write failed on chunk {i // chunk_size}: {e}"
                logger.error(err_msg)
                result.errors.append(err_msg)

        result.firestore_synced = synced_count
        logger.info(f"Successfully synced {synced_count} records to Firestore collection '{COLLECTION_BOUNTY_LEADS}'")

    return result


def main() -> None:
    """CLI entry point for migrate_queues."""
    parser = argparse.ArgumentParser(
        description="Universal Bounty V2 Queue & State Migration Utility"
    )
    parser.add_argument(
        "--source",
        "-s",
        type=str,
        default="logs/intake_queue.jsonl",
        help="Path to source intake_queue.jsonl",
    )
    parser.add_argument(
        "--target",
        "-t",
        type=str,
        default=None,
        help="Path to output intake_queue.jsonl",
    )
    parser.add_argument(
        "--target-dir",
        "-d",
        type=str,
        default=None,
        help="Target directory for output files",
    )
    parser.add_argument(
        "--firestore",
        "-f",
        action="store_true",
        help="Synchronize migrated records to Firestore collection",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate migration without writing changes",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Disable automatic backup creation",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    )

    try:
        res = migrate_queues(
            source_jsonl=args.source,
            target_dir=args.target_dir,
            target_jsonl=args.target,
            firestore_sync=args.firestore,
            dry_run=args.dry_run,
            backup=not args.no_backup,
        )
        print("Migration completed successfully:")
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
    except Exception as e:
        print(f"Migration failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
