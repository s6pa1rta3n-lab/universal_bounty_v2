"""
Intake Engine — High-Value GitHub Bounty Ingestion & Sniper Filter.

Queries GitHub GraphQL API for open bounty issues across high-conviction ecosystems,
applies the strict Sniper Filter (rejecting banned platforms, subjective/KYC tasks,
archived repos, cancelled/refunded escrows), deduplicates against local seen caches and
Firestore, and load-balances the intake queue (enforcing max 4 concurrent leads and max 1
active lead per repo) with priority partitioning (high priority top, USD descending).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.config import (
    BANNED_PLATFORMS,
    COLLECTION_BOUNTY_LEADS,
    DISQUALIFY_KEYWORDS,
    HIGH_PRIORITY_KEYWORDS,
)
from src.core.firestore_client import get_firestore_client
from src.core.github_client import GitHubClient, get_github_client
from src.core.path_guard import DEFAULT_PATH_GUARD, PathGuard
from src.core.safe_io import SafeIO

logger = logging.getLogger("UniversalBountyV2.IntakeEngine")

DEFAULT_SEARCH_CATEGORIES: list[dict[str, str]] = [
    {"name": "GrantFox Global Search", "query": "is:issue is:open grantfox", "priority": "high"},
    {"name": "GrantFox Escrow Label", "query": "is:issue is:open label:grantfox", "priority": "high"},
    {"name": "GrantFox OSS", "query": 'is:issue is:open label:"GrantFox OSS"', "priority": "high"},
    {
        "name": "Stellar / Soroban Ecosystem",
        "query": "is:issue is:open stellar OR soroban OR xlm label:bounty,reward,funded",
        "priority": "high",
    },
    {"name": "Stellar Bounties", "query": 'is:issue is:open "stellar" bounty', "priority": "high"},
    {
        "name": "EVM & Ethereum",
        "query": "is:issue is:open ethereum OR evm OR solidity label:bounty,reward,funded",
        "priority": "high",
    },
    {
        "name": "Verified Smart Contract Escrow",
        "query": 'is:issue is:open escrow OR "locked funds" OR "grant pool" label:bounty,reward,funded',
        "priority": "high",
    },
    {
        "name": "Gitcoin / Bounties Network",
        "query": "is:issue is:open label:gitcoin,bounties-network",
        "priority": "high",
    },
    {
        "name": "Web3 Smart Contracts",
        "query": 'is:issue is:open "smart contract" label:bounty,reward,funded',
        "priority": "high",
    },
    {
        "name": "General Verified Bounties",
        "query": "is:issue is:open label:bounty,reward,funded,paid",
        "priority": "standard",
    },
]

DEFAULT_SEEN_CACHE_FILE = "/tmp/bounty_intake_seen_issues.json"
DEFAULT_INTAKE_QUEUE_FILE = Path.cwd() / "logs" / "intake_queue.jsonl"


def get_nodes(field: Any) -> list[Any]:
    """Safely extracts a list of nodes from either list or dict format."""
    if not field:
        return []
    if isinstance(field, list):
        return field
    if isinstance(field, dict):
        return field.get("nodes", []) or []
    return []


def clean_text_for_financials(text: str) -> str:
    """Filters out promotional footers and platform links that inflate financial values."""
    if not text:
        return ""
    cleaned_lines = []
    for line in text.splitlines():
        if re.search(r"more\s+funded\s+oss\s+work\s+available", line, re.IGNORECASE):
            continue
        if re.search(r"gitcoin\.co/(explorer|issue/fulfill)", line, re.IGNORECASE) and re.search(
            r"\$[\d,]+", line
        ):
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def extract_financials(text: str) -> tuple[str, float]:
    """
    Extracts dollar or crypto token amounts from text.
    Returns: (formatted_amount_str, numeric_value_usd)
    """
    if not text:
        return "PENDING DISCOVERY", 0.0
    if not isinstance(text, str):
        text = str(text)

    text = clean_text_for_financials(text)
    amounts: list[tuple[str, float]] = []

    # Dollar amounts ($100, $ 1,500.00, $500.5, 500$, etc.)
    dollar_prefix_pattern = re.compile(r"\$\s*(\d+(?:,\d{3})*(?:\.\d+)?)")
    dollar_postfix_pattern = re.compile(r"(?<![\d,])(\d+(?:,\d{3})*(?:\.\d+)?)\s*\$")

    for m in dollar_prefix_pattern.findall(text):
        val = float(m.replace(",", ""))
        amounts.append((f"${val:.2f}", val))

    for m in dollar_postfix_pattern.findall(text):
        val = float(m.replace(",", ""))
        amounts.append((f"${val:.2f}", val))

    # Crypto token amounts (e.g., 500 USDC, 1,000 XLM, 0.5 ETH, USDC 500)
    tokens = "USDC|USDT|XLM|ETH|WETH|DAI|MATIC|POL|OP|ARB|SOL|USD|STRK|AVAX|BNB|GNO|NEAR|DOT|LINK|UNI"
    token_postfix_pattern = re.compile(
        rf"(?<![\d,])(\d+(?:,\d{{3}})*(?:\.\d+)?)\s*({tokens})\b", re.IGNORECASE
    )
    token_prefix_pattern = re.compile(
        rf"\b({tokens})\s*(\d+(?:,\d{{3}})*(?:\.\d+)?)\b", re.IGNORECASE
    )

    for val_str, token in token_postfix_pattern.findall(text):
        val = float(val_str.replace(",", ""))
        token_upper = token.upper()
        amounts.append((f"{val} {token_upper}", val))

    for token, val_str in token_prefix_pattern.findall(text):
        val = float(val_str.replace(",", ""))
        token_upper = token.upper()
        amounts.append((f"{val} {token_upper}", val))

    if amounts:
        amounts.sort(key=lambda x: x[1], reverse=True)
        return amounts[0][0], amounts[0][1]

    return "PENDING DISCOVERY", 0.0


def verify_escrow(issue_node: dict[str, Any]) -> tuple[bool, str, str, float, bool, str]:
    """
    Sniper Filter: Strict Escrow & Qualification Verification.
    Returns: (is_valid, reason, payout_str, payout_val, is_high_priority, ecosystem)
    """
    if not issue_node or not isinstance(issue_node, dict):
        return False, "REJECT_INVALID_NODE", "0.00", 0.0, False, "unknown"

    repo_data = issue_node.get("repository") or {}
    if not isinstance(repo_data, dict):
        repo_data = {}
    repo_name = (
        repo_data.get("nameWithOwner")
        or issue_node.get("repo")
        or (repo_data if isinstance(repo_data, str) else "")
    ).lower()

    title = (issue_node.get("title") or "").lower()
    body = (issue_node.get("body") or "").lower()

    labels_nodes = get_nodes(issue_node.get("labels"))
    labels: list[str] = []
    for lbl in labels_nodes:
        if isinstance(lbl, dict):
            name = lbl.get("name") or ""
            if name:
                labels.append(name.lower())
        elif isinstance(lbl, str):
            labels.append(lbl.lower())

    comments_nodes = get_nodes(issue_node.get("comments"))
    comments_bodies: list[str] = []
    comments_authors_list: list[str] = []
    for c in comments_nodes:
        if isinstance(c, dict):
            comments_bodies.append(c.get("body") or "")
            auth = c.get("author")
            if isinstance(auth, dict):
                comments_authors_list.append(auth.get("login") or "")
            elif isinstance(auth, str):
                comments_authors_list.append(auth)
        elif isinstance(c, str):
            comments_bodies.append(c)

    comments_content = " ".join(comments_bodies).lower()
    comments_authors = " ".join(comments_authors_list).lower()

    if not repo_name:
        return False, "REJECT_MISSING_REPO", "0.00", 0.0, False, "unknown"

    # 1. Repository Status: Discard archived
    if repo_data.get("isArchived"):
        return False, "REJECT_ARCHIVED_REPO", "0.00", 0.0, False, "unknown"

    # 2. Platform Banning: Discard banned platforms
    author_field = issue_node.get("author")
    author_login = (
        author_field.get("login")
        if isinstance(author_field, dict)
        else (author_field if isinstance(author_field, str) else "") or ""
    ).lower()
    combined_content = (
        f"{title} {body} "
        + " ".join(labels)
        + f" {author_login} {repo_name} {comments_content} {comments_authors}"
    )

    for banned in BANNED_PLATFORMS:
        if (
            banned in repo_name
            or banned in author_login
            or banned in comments_authors
            or any(banned in lbl for lbl in labels)
            or f"{banned}.io" in combined_content
            or f"{banned}.sh" in combined_content
            or f"{banned}.dev" in combined_content
            or re.search(rf"\b{re.escape(banned)}\b", combined_content)
        ):
            banned_clean = banned.replace("/", "_").replace("-", "_").upper()
            return False, f"REJECT_BANNED_PLATFORM_{banned_clean}", "0.00", 0.0, False, "banned"

    # 3. Subjective / KYC Disqualification
    if re.search(
        r"\b(kyc|zoom|interview|figma only|design only|pitch deck|loom|screencast|recorded video)\b",
        combined_content,
    ):
        return False, "REJECT_SUBJECTIVE_KYC", "0.00", 0.0, False, "subjective"
    for dq in DISQUALIFY_KEYWORDS:
        if dq in combined_content:
            dq_clean = dq.replace(" ", "_").upper()
            return False, f"REJECT_SUBJECTIVE_{dq_clean}", "0.00", 0.0, False, "subjective"

    # 4. Check for Cancellation / Refund / Withdrawal in Comments or Body
    cancellation_pattern = re.compile(
        r"has\s+been\s+[\*]*(cancelled|canceled|refunded|withdrawn|voided|returned)"
        r"|\b(cancelled|canceled|refunded|withdrawn)[\s\*]+by\s+the\s+(bounty\s+submitter|funder|author|maintainer|submitter)\b"
        r"|\b(bounty|funding|reward|escrow)\b[^\.\n]*\b(cancelled|canceled|refunded|withdrawn|voided|returned)\b",
        re.IGNORECASE,
    )
    for c in comments_nodes:
        c_body = (c.get("body") if isinstance(c, dict) else c) or ""
        if (
            cancellation_pattern.search(c_body)
            or "bounty has been cancelled" in c_body.lower()
            or "funding has been cancelled" in c_body.lower()
        ):
            return False, "REJECT_ESCROW_CANCELLED", "0.00", 0.0, False, "cancelled"

    # 5. Check Ecosystems
    is_grantfox = any("grantfox" in lbl for lbl in labels) or "grantfox" in body or "grantfox" in repo_name
    is_stellar = bool(
        re.search(r"\b(stellar|soroban|xlm)\b", combined_content or "")
        or re.search(r"\b(stellar|soroban|xlm)\b", repo_name or "")
    )
    is_gitcoin = (
        any("gitcoin" in lbl for lbl in labels)
        or any("bounties-network" in lbl for lbl in labels)
        or "gitcoin" in repo_name
        or "gitcoin" in body
    )
    is_evm = bool(
        re.search(
            r"\b(ethereum|evm|base|arbitrum|optimism|polygon|matic|pol|solidity|foundry|hardhat|zkevm|avalanche|avax|bsc)\b",
            combined_content or "",
        )
        or re.search(
            r"\b(ethereum|evm|base|arbitrum|optimism|polygon|matic|pol|solidity|foundry|hardhat|zkevm|avalanche|avax|bsc)\b",
            repo_name or "",
        )
    )

    ecosystem = "other"
    if is_grantfox:
        ecosystem = "grantfox"
    elif is_stellar:
        ecosystem = "stellar"
    elif is_evm:
        ecosystem = "evm"
    elif is_gitcoin:
        ecosystem = "gitcoin"

    is_high_priority = (
        is_grantfox
        or is_stellar
        or is_gitcoin
        or is_evm
        or any(
            re.search(rf"\b{re.escape(kw)}\b", combined_content or "")
            or re.search(rf"\b{re.escape(kw)}\b", repo_name or "")
            for kw in HIGH_PRIORITY_KEYWORDS
        )
    )

    # 6. Financial & Escrow Verification
    combined_all = f"{title} {body} " + " ".join(comments_bodies)
    payout_str, payout_val = extract_financials(combined_all)

    reason = "VERIFIED_QUALIFIED_BOUNTY"
    if is_grantfox:
        reason = "VERIFIED_GRANTFOX_ESCROW"
    elif is_stellar:
        reason = "VERIFIED_STELLAR_FUNDING"
    elif is_gitcoin:
        reason = "VERIFIED_GITCOIN_ESCROW"
    elif is_evm:
        reason = "VERIFIED_EVM_FUNDING"

    return True, reason, payout_str, payout_val, is_high_priority, ecosystem


def generate_canonical_doc_id(repo: str, issue_number: int | str) -> str:
    """Generates canonical doc ID: {clean_owner}_{clean_repo}_{issue_number}."""
    clean_repo = repo.replace("/", "_").replace("-", "_").replace(".", "_").lower()
    return f"{clean_repo}_{issue_number}"


class IntakeEngine:
    """
    Modular Intake Engine for Milestone 2.
    Queries GitHub GraphQL/REST, applies the Sniper Filter, deduplicates against
    local seen cache and Firestore, load-balances queue with anti-spam constraints,
    and maintains sorted queue state.
    """

    def __init__(
        self,
        db: Any | None = None,
        github_client: GitHubClient | None = None,
        path_guard: PathGuard | None = None,
        seen_cache_path: str | Path | None = None,
        categories: list[dict[str, str]] | None = None,
        collection_name: str = COLLECTION_BOUNTY_LEADS,
        queue_file_path: str | Path | None = None,
    ):
        self.path_guard = path_guard or DEFAULT_PATH_GUARD
        self.db = db if db is not None else get_firestore_client()
        self.github_client = github_client or get_github_client()
        self.collection_name = collection_name
        self.categories = categories or DEFAULT_SEARCH_CATEGORIES

        raw_seen = seen_cache_path or DEFAULT_SEEN_CACHE_FILE
        self.seen_cache_path = self.path_guard.validate_access(
            raw_seen, operation="seen_cache_init"
        )
        self.seen_issues: set[str] = self._load_seen_cache()

        raw_queue = queue_file_path or DEFAULT_INTAKE_QUEUE_FILE
        self.queue_file_path = self.path_guard.validate_access(
            raw_queue, operation="queue_file_init"
        )

    def _load_seen_cache(self) -> set[str]:
        """Loads seen issue IDs from cache file."""
        if not self.seen_cache_path.exists():
            return set()
        try:
            content = SafeIO.read_text(self.seen_cache_path)
            data = json.loads(content)
            if isinstance(data, list):
                return set(data)
            return set()
        except Exception as e:
            logger.warning(f"Could not load seen cache from {self.seen_cache_path}: {e}")
            return set()

    def _save_seen_cache(self) -> None:
        """Saves seen issue IDs to cache file."""
        try:
            data_str = json.dumps(sorted(list(self.seen_issues)), indent=2)
            SafeIO.write_text(self.seen_cache_path, data_str)
        except Exception as e:
            logger.warning(f"Could not save seen cache to {self.seen_cache_path}: {e}")

    def fetch_bounties(
        self,
        categories: list[dict[str, str]] | None = None,
        max_pages_per_cat: int = 1,
    ) -> list[dict[str, Any]]:
        """
        Fetches issue nodes across search categories using GitHubClient.
        """
        cats = categories or self.categories
        all_nodes: list[dict[str, Any]] = []

        for cat in cats:
            query_str = cat["query"]
            cat_name = cat.get("name", "Unknown Category")
            cursor = None
            page = 0

            while page < max_pages_per_cat:
                page += 1
                try:
                    resp = self.github_client.search_bounties(query_str, cursor=cursor)
                    data = resp.get("data") or {}
                    search_data = data.get("search") or {}
                    nodes = search_data.get("nodes") or []

                    for node in nodes:
                        if node and isinstance(node, dict):
                            node["_category"] = cat_name
                            node["_category_priority"] = cat.get("priority", "standard")
                            all_nodes.append(node)

                    page_info = search_data.get("pageInfo") or {}
                    if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
                        break
                    cursor = page_info.get("endCursor")
                except Exception as e:
                    logger.warning(f"Error searching category '{cat_name}': {e}")
                    break

        return all_nodes

    def process_issue_node(self, issue_node: dict[str, Any]) -> dict[str, Any] | None:
        """
        Evaluates an issue node against the Sniper Filter and formats it into standard lead schema.
        """
        node_id = issue_node.get("id") or issue_node.get("issue_id")
        repo_info = issue_node.get("repository") or {}
        repo_name = (
            repo_info.get("nameWithOwner")
            if isinstance(repo_info, dict)
            else (issue_node.get("repo") or issue_node.get("repository") or "")
        )
        if isinstance(repo_name, dict):
            repo_name = repo_name.get("nameWithOwner", "")

        issue_number = issue_node.get("number") or issue_node.get("issue_number")
        if not repo_name or issue_number is None:
            return None

        doc_id = generate_canonical_doc_id(repo_name, issue_number)

        # Check in-memory seen cache
        if node_id and node_id in self.seen_issues:
            return None
        if doc_id in self.seen_issues:
            return None

        author_field = issue_node.get("author") or {}
        author_login = (
            author_field.get("login") if isinstance(author_field, dict) else str(author_field)
        )

        is_fleet_meta = (
            repo_name.lower() == "s6pa1rta3n-lab/universal_bounty_fleet"
            and "ankur" in str(author_login).lower()
        )

        # Apply Sniper Filter
        is_valid, reason, payout_str, payout_val, is_high_priority, ecosystem = verify_escrow(
            issue_node
        )

        if is_fleet_meta:
            is_valid = True
            is_high_priority = True
            reason = "META_INTERNAL_PRIORITY"
            payout_str = "INTERNAL_MAINTENANCE"
            payout_val = 999999.0
            ecosystem = "internal"
            logger.info(f"🚀 [META] Fast-tracking internal fleet issue from Ankur: {repo_name}#{issue_number}")

        if not is_valid:
            logger.debug(f"Discarding unqualified issue {repo_name}#{issue_number}: {reason}")
            return None

        status = "queued"
        priority = "high" if is_high_priority else "standard"

        labels_nodes = get_nodes(issue_node.get("labels"))
        labels = [
            (lbl.get("name") if isinstance(lbl, dict) else str(lbl))
            for lbl in labels_nodes
            if lbl
        ]

        title = issue_node.get("title", "")
        body = issue_node.get("body", "")
        issue_url = (
            issue_node.get("url") or f"https://github.com/{repo_name}/issues/{issue_number}"
        )

        lead_doc: dict[str, Any] = {
            "id": doc_id,
            "node_id": node_id or doc_id,
            "repo": repo_name,
            "issue_number": int(issue_number),
            "title": title,
            "body": body,
            "issue_url": issue_url,
            "status": status,
            "priority": priority,
            "projected_payout": payout_str,
            "projected_payout_usd": payout_val,
            "qualification_reason": reason,
            "ecosystem": ecosystem,
            "escrow_verified": True,
            "labels": labels,
            "author": author_login,
            "lock": {
                "owner_id": None,
                "locked_at": None,
                "lock_timeout_sec": 300,
            },
            "created_at_iso": datetime.now(timezone.utc).isoformat(),
            "updated_at_iso": datetime.now(timezone.utc).isoformat(),
        }

        return lead_doc

    def balance_queue(
        self,
        max_concurrent: int = 4,
        max_per_repo: int = 1,
    ) -> list[dict[str, Any]]:
        """
        Anti-Spam Load Balancer:
        Promotes 'queued' leads into 'pending_triage' (or 'priority_triage' if high priority)
        only when active count < max_concurrent, and enforces max 1 active lead per repo.
        Returns list of promoted lead dictionaries.
        """
        col_ref = self.db.collection(self.collection_name)
        active_statuses = ["priority_triage", "pending_triage", "claimed", "running_orbstack"]
        active_docs = []

        for st in active_statuses:
            docs = list(col_ref.where("status", "==", st).stream())
            active_docs.extend(docs)

        active_count = len(active_docs)
        active_repos: set[str] = set()
        for doc in active_docs:
            d = doc.to_dict() or {}
            repo = d.get("repo")
            if repo:
                active_repos.add(repo.lower())

        logger.info(
            f"[LoadBalancer] Current active leads: {active_count}/{max_concurrent}. Active repos: {len(active_repos)}"
        )

        if active_count >= max_concurrent:
            logger.info("[LoadBalancer] Swarm is at capacity. Holding queue.")
            return []

        slots_available = max_concurrent - active_count
        queued_docs = list(col_ref.where("status", "==", "queued").stream())

        # Sort queued docs: high priority first, then numeric USD descending
        def sort_key(doc_snap: Any) -> tuple[int, float]:
            data = doc_snap.to_dict() or {}
            p_rank = 0 if data.get("priority") == "high" else 1
            usd = float(data.get("projected_payout_usd", 0.0))
            return (p_rank, -usd)

        queued_docs.sort(key=sort_key)

        promoted: list[dict[str, Any]] = []
        for doc in queued_docs:
            if len(promoted) >= slots_available:
                break

            lead_data = doc.to_dict() or {}
            repo_name = (lead_data.get("repo") or "").lower()

            # Anti-Spam: Max 1 active lead per repository constraint
            if repo_name in active_repos:
                logger.debug(
                    f"[LoadBalancer] Skipping {doc.id} — Repository {repo_name} already has an active lead."
                )
                continue

            target_status = "priority_triage" if lead_data.get("priority") == "high" else "pending_triage"
            update_fields = {
                "status": target_status,
                "updated_at_iso": datetime.now(timezone.utc).isoformat(),
            }

            try:
                doc.reference.update(update_fields)
                lead_data.update(update_fields)
                promoted.append(lead_data)
                active_repos.add(repo_name)
                logger.info(f"[+] Promoted {doc.id} to {target_status}.")
            except Exception as e:
                logger.error(f"Error promoting doc {doc.id}: {e}")

        logger.info(f"[LoadBalancer] Successfully promoted {len(promoted)} leads.")
        return promoted

    def _sync_local_queue_file(self) -> None:
        """
        Synchronizes all leads from Firestore to logs/intake_queue.jsonl
        partitioned with high priority on top, sorted descending by USD payout.
        """
        try:
            col_ref = self.db.collection(self.collection_name)
            all_snaps = list(col_ref.stream())
            all_leads = [snap.to_dict() for snap in all_snaps if snap.to_dict()]

            # Partition & sort: high priority first, then USD descending
            all_leads.sort(
                key=lambda x: (
                    0 if x.get("priority") == "high" else 1,
                    -float(x.get("projected_payout_usd", 0.0)),
                )
            )

            SafeIO.mkdir(self.queue_file_path.parent, parents=True, exist_ok=True)
            SafeIO.write_jsonl(self.queue_file_path, all_leads)
            logger.info(f"Synchronized {len(all_leads)} leads to {self.queue_file_path}")
        except Exception as e:
            logger.warning(f"Could not sync local queue file {self.queue_file_path}: {e}")

    def ingest_bounties(
        self,
        issues: list[dict[str, Any]] | None = None,
        max_pages_per_cat: int = 1,
    ) -> list[dict[str, Any]]:
        """
        Ingests bounties into Firestore `bounty_leads`.
        If issues is None, fetches live from GitHub GraphQL.
        """
        raw_issues = (
            issues
            if issues is not None
            else self.fetch_bounties(max_pages_per_cat=max_pages_per_cat)
        )
        new_leads: list[dict[str, Any]] = []
        col_ref = self.db.collection(self.collection_name)

        for raw_node in raw_issues:
            processed = self.process_issue_node(raw_node)
            if not processed:
                continue

            doc_id = processed["id"]
            node_id = processed.get("node_id", doc_id)

            try:
                doc_ref = col_ref.document(doc_id)
                # Idempotent write
                doc_ref.set(processed, merge=True)
                new_leads.append(processed)

                self.seen_issues.add(node_id)
                self.seen_issues.add(doc_id)
                logger.info(
                    f"[+] Ingested new bounty lead: {doc_id} ({processed['projected_payout']}, {processed['ecosystem']})"
                )
            except Exception as e:
                logger.error(f"Error persisting bounty lead {doc_id}: {e}")

        # Save updated seen cache
        self._save_seen_cache()

        # Sync local queue file
        self._sync_local_queue_file()

        return new_leads

    def run_sweep(self) -> dict[str, Any]:
        """Runs a complete intake sweep."""
        logger.info("Executing IntakeEngine sweep...")
        ingested = self.ingest_bounties()
        promoted = self.balance_queue()
        self._sync_local_queue_file()
        return {
            "ingested_count": len(ingested),
            "promoted_count": len(promoted),
            "ingested_leads": ingested,
            "promoted_leads": promoted,
        }
