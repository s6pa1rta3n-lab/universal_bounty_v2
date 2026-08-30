"""
Sync Engine — Settlement Verification, Accounting Ledger & Swarm Coordinator Sync.

Monitors merged PRs and completed bounties across Firestore `bounty_memory` and `bounty_leads`,
extracts verified settlement amounts, writes immutable settlement records into `bounty_settlements`,
computes aggregate financial totals, and updates cluster state in `swarm_coordinator/state`.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from src.core.config import (
    COLLECTION_BOUNTY_LEADS,
    COLLECTION_BOUNTY_MEMORY,
    COLLECTION_BOUNTY_SETTLEMENTS,
    COLLECTION_SWARM_COORDINATOR,
    EVM_PAYOUT_ADDRESS,
)
from src.core.firestore_client import (
    get_firestore_client,
)
from src.core.github_client import GitHubClient, get_github_client

logger = logging.getLogger("UniversalBountyV2.SyncEngine")


def extract_payout_numeric(data: dict[str, Any]) -> tuple[str, float]:
    """
    Extracts payout string and numeric USD value from various document schemas.
    """
    # 1. Direct numeric float/int fields
    if "projected_payout_usd" in data and isinstance(
        data["projected_payout_usd"], (int, float)
    ):
        val = float(data["projected_payout_usd"])
        p_str = data.get("projected_payout") or f"${val:.2f}"
        return p_str, val

    if "payout_usd" in data and isinstance(data["payout_usd"], (int, float)):
        val = float(data["payout_usd"])
        p_str = data.get("payout_str") or data.get("payout") or f"${val:.2f}"
        return p_str, val

    if "escrow" in data and isinstance(data["escrow"], dict):
        escrow_data = data["escrow"]
        amount_usd = escrow_data.get("amount_usd")
        if isinstance(amount_usd, (int, float)) and amount_usd > 0:
            return f"${float(amount_usd):.2f}", float(amount_usd)

    # 2. Text extraction
    raw_text = ""
    for field in ["projected_payout", "payout", "reward_tokens", "title", "body"]:
        v = data.get(field)
        if isinstance(v, str):
            raw_text += f" {v}"

    # Dollar amounts ($100, $1,500.00, etc.)
    m_dollar = re.findall(r"\$\s*(\d+(?:,\d{3})*(?:\.\d+)?)", raw_text)
    if m_dollar:
        val = float(m_dollar[0].replace(",", ""))
        return f"${val:.2f}", val

    # Token amounts (500 USDC, 1000 XLM, etc.)
    m_token = re.findall(
        r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*(USDC|USDT|XLM|ETH|WETH|DAI|MATIC|POL|OP|ARB|SOL|USD)",
        raw_text,
        re.IGNORECASE,
    )
    if m_token:
        val_str, tok = m_token[0]
        val = float(val_str.replace(",", ""))
        return f"{val} {tok.upper()}", val

    return "$0.00", 0.0


class SyncEngine:
    """
    Modular Sync & Settlement Engine for Milestone 4.
    Synchronizes settlement ledgers in `bounty_settlements` and updates `swarm_coordinator/state`.
    """

    def __init__(
        self,
        db: Any | None = None,
        github_client: GitHubClient | None = None,
        memory_collection: str = COLLECTION_BOUNTY_MEMORY,
        leads_collection: str = COLLECTION_BOUNTY_LEADS,
        coordinator_collection: str = COLLECTION_SWARM_COORDINATOR,
        settlements_collection: str = COLLECTION_BOUNTY_SETTLEMENTS,
    ):
        self.db = db if db is not None else get_firestore_client()
        self.github_client = github_client or get_github_client()
        self.memory_collection = memory_collection
        self.leads_collection = leads_collection
        self.coordinator_collection = coordinator_collection
        self.settlements_collection = settlements_collection

    def process_doc_settlement(
        self,
        doc_id: str,
        data: dict[str, Any],
    ) -> dict[str, Any] | None:
        """
        Evaluates a document to determine if it is eligible for settlement recording:
        - state == 'MERGED' or status == 'completed' or audit_status == 'PASS' (and merged)
        """
        state = str(data.get("state", "")).upper()
        status = str(data.get("status", "")).lower()
        audit_status = str(data.get("audit_status", "")).upper()

        is_settled = (
            state == "MERGED"
            or status in ("completed", "settled")
            or (audit_status == "PASS" and data.get("merge_allowed", False))
            or bool(data.get("merged_at") or data.get("mergedAt"))
        )

        if not is_settled:
            return None

        payout_str, payout_usd = extract_payout_numeric(data)
        now_iso = datetime.now(timezone.utc).isoformat()

        clean_doc_id = doc_id.replace("/", "_").replace("#", "_")
        settlement_id = f"settle_{clean_doc_id}"
        settlement_record: dict[str, Any] = {
            "settlement_id": settlement_id,
            "source_doc_id": doc_id,
            "repo": data.get("repo") or data.get("repository"),
            "pr_number": data.get("pr_number") or data.get("number"),
            "pr_url": data.get("pr_url") or data.get("url"),
            "title": data.get("title"),
            "payout_str": payout_str,
            "payout_usd": payout_usd,
            "payout_recipient": EVM_PAYOUT_ADDRESS,
            "settled_at_iso": now_iso,
            "status": "SETTLED",
        }

        # Write immutable settlement record
        try:
            settle_ref = self.db.collection(self.settlements_collection).document(
                settlement_id
            )
            settle_ref.set(settlement_record, merge=True)
            logger.info(
                f"[Sync] Recorded settlement {settlement_id} for {doc_id}: {payout_str} (${payout_usd:.2f})"
            )
        except Exception as e:
            logger.error(f"[!] Error writing settlement record {settlement_id}: {e}")

        return settlement_record

    def sync_settlements(self) -> dict[str, Any]:
        """
        Full sweep:
        1. Scans bounty_memory and bounty_leads for completed/merged items
        2. Records settlements
        3. Aggregates financial totals
        4. Updates swarm_coordinator/state
        """
        settled_records: list[dict[str, Any]] = []

        # Scan bounty_memory
        try:
            mem_snaps = list(self.db.collection(self.memory_collection).stream())
            for snap in mem_snaps:
                rec = self.process_doc_settlement(snap.id, snap.to_dict())
                if rec:
                    settled_records.append(rec)
        except Exception as e:
            logger.error(f"Error scanning {self.memory_collection}: {e}")

        # Scan bounty_leads
        try:
            leads_snaps = list(
                self.db.collection(self.leads_collection)
                .where("status", "==", "completed")
                .stream()
            )
            for snap in leads_snaps:
                rec = self.process_doc_settlement(snap.id, snap.to_dict())
                if rec:
                    settled_records.append(rec)
        except Exception as e:
            logger.error(f"Error scanning {self.leads_collection}: {e}")

        # Compute aggregate metrics
        total_settled_usd = 0.0
        all_settlements = []
        try:
            all_settlements = list(
                self.db.collection(self.settlements_collection).stream()
            )
            for s_snap in all_settlements:
                s_data = s_snap.to_dict() or {}
                total_settled_usd += float(s_data.get("payout_usd", 0.0))
        except Exception as e:
            logger.warning(f"Could not aggregate total settlements: {e}")

        # Update swarm_coordinator state singleton doc
        now_iso = datetime.now(timezone.utc).isoformat()
        coordinator_state = {
            "total_settled_usd": round(total_settled_usd, 2),
            "total_settled_count": len(all_settlements),
            "last_sync_iso": now_iso,
            "status": "HEALTHY",
            "active_engines": [
                "inbox_engine",
                "intake_engine",
                "executor_engine",
                "escort_engine",
                "sync_engine",
            ],
        }

        try:
            state_ref = self.db.collection(self.coordinator_collection).document(
                "state"
            )
            state_ref.set(coordinator_state, merge=True)
            logger.info(
                f"[Sync] Updated coordinator state: Total Settled=${total_settled_usd:.2f} ({len(all_settlements)} settlements)"
            )
        except Exception as e:
            logger.error(f"[!] Error updating coordinator state: {e}")

        return {
            "synced_count": len(settled_records),
            "total_settled_count": len(all_settlements),
            "total_settled_usd": round(total_settled_usd, 2),
            "coordinator_state": coordinator_state,
        }

    def run_sweep(self) -> dict[str, Any]:
        """Runs single sync pass."""
        logger.info("Executing SyncEngine sweep...")
        res = self.sync_settlements()
        logger.info(
            f"Sync sweep completed. Recorded {res['synced_count']} items, Total Settled=${res['total_settled_usd']:.2f}"
        )
        return res
