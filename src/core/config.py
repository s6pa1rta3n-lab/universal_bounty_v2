"""
Configuration Management for Universal Bounty Engine V2.
Enforces hardcoded security policies, platform bans, dual-chain payout routing,
and container quotas.
"""

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# Strictly banned platforms and organizations (Never hunt, search, or interact)
BANNED_PLATFORMS: List[str] = [
    "algora",
    "polar",
    "twentyhq/twenty",
    "twentyhq",
    "opire",
]

# Absolute hardcoded IGNORE_LIST preventing any operations on protected trading paths
DEFAULT_IGNORE_LIST: List[str] = [
    "~/teamwork_projects/keeper_daemon",
    "~/teamwork_projects/odin",
    "~/teamwork_projects/matt-berserker",
]

# Mandatory Web3 / Gitcoin Payout Routing
EVM_PAYOUT_ADDRESS: str = "0xF46C9F6d70C50BF81ef3588AB523a90a594a2F89"
STELLAR_PAYOUT_ADDRESS: str = "GCL6OXAMLD75BMTINA6EMRUDWK5THQUSHMYNLSNBCJAPZJHNYJTUNIBC"
SOLANA_PAYOUT_ADDRESS: str = "MSwhtUP1XaRfMf5ectKq9LnGvQxcQxxMHJo4QNaJ6Av"

PAYOUT_ROUTING: Dict[str, str] = {
    "EVM": EVM_PAYOUT_ADDRESS,
    "STELLAR": STELLAR_PAYOUT_ADDRESS,
    "SOLANA": SOLANA_PAYOUT_ADDRESS,
}

# GCP / Firebase Configuration Defaults
DEFAULT_GCP_PROJECT_ID: str = "odin-500008"
DEFAULT_FIRESTORE_DATABASE: str = "(default)"

# Firestore Collection Names
COLLECTION_BOUNTY_LEADS: str = "bounty_leads"
COLLECTION_SWARM_OPERATIONS: str = "swarm_operations"
COLLECTION_SWARM_COORDINATOR: str = "swarm_coordinator"
COLLECTION_BOUNTY_TASKS: str = "bounty_tasks"
COLLECTION_BOUNTY_MEMORY: str = "bounty_memory"
COLLECTION_BOUNTY_SETTLEMENTS: str = "bounty_settlements"

# Container Isolation Quotas
DEFAULT_DOCKER_IMAGE: str = "python:3.11-slim"
DEFAULT_CONTAINER_CPUS: str = "2"
DEFAULT_CONTAINER_MEMORY: str = "2g"
DEFAULT_CONTAINER_PIDS_LIMIT: int = 256
DEFAULT_CONTAINER_TIMEOUT_SEC: int = 300
DEFAULT_SANDBOX_BASE_DIR: str = "/tmp/bounty_sandboxes"

# Subjective / Non-technical disqualification keywords (Sniper Filter)
DISQUALIFY_KEYWORDS: List[str] = [
    "video pitch",
    "record a video",
    "record video",
    "recorded video",
    "recording a video",
    "submit a video",
    "video presentation",
    "video demo required",
    "demo video",
    "video walkthrough",
    "loom",
    "loom.com",
    "screencast",
    "interview",
    "zoom interview",
    "zoom call",
    "pitch deck",
    "manual kyc",
    "kyc required",
    "figma only",
    "design only",
]

# High-priority ecosystems and keywords
HIGH_PRIORITY_KEYWORDS: List[str] = [
    "grantfox",
    "grantfox oss",
    "stellar",
    "soroban",
    "xlm",
    "stellar wave",
    "evm",
    "ethereum",
    "base",
    "arbitrum",
    "optimism",
    "polygon",
    "matic",
    "pol",
    "zkevm",
    "avalanche",
    "avax",
    "bsc",
    "binance",
    "smart contract",
    "solidity",
    "foundry",
    "hardhat",
    "rust",
    "web3",
    "gitcoin",
    "bounties-network",
]


def is_banned_platform(target: Optional[str]) -> bool:
    """
    Checks if a target string, URL, repository, or platform name matches any banned platform.
    Case-insensitive check against BANNED_PLATFORMS.
    """
    if not target:
        return False
    target_lower = target.lower().strip()
    for banned in BANNED_PLATFORMS:
        banned_lower = banned.lower()
        if (
            banned_lower == target_lower
            or banned_lower in target_lower
            or f"{banned_lower}.io" in target_lower
            or f"{banned_lower}.sh" in target_lower
            or f"{banned_lower}.dev" in target_lower
        ):
            return True
    return False


def contains_disqualify_keywords(text: Optional[str]) -> Tuple[bool, Optional[str]]:
    """
    Checks if the provided text contains any subjective/non-technical disqualifiers.
    Returns (True, matched_keyword) if disqualified, else (False, None).
    """
    if not text:
        return False, None
    text_lower = text.lower()
    for kw in DISQUALIFY_KEYWORDS:
        if kw in text_lower:
            return True, kw
    return False, None


@dataclass(frozen=True)
class SwarmConfig:
    """Immutable runtime configuration container."""

    gcp_project_id: str = field(
        default_factory=lambda: os.getenv("GCP_PROJECT_ID", DEFAULT_GCP_PROJECT_ID)
    )
    firestore_database: str = field(
        default_factory=lambda: os.getenv(
            "FIRESTORE_DATABASE_ID", DEFAULT_FIRESTORE_DATABASE
        )
    )
    ignore_list: List[str] = field(default_factory=lambda: list(DEFAULT_IGNORE_LIST))
    banned_platforms: List[str] = field(
        default_factory=lambda: list(BANNED_PLATFORMS)
    )
    docker_image: str = field(
        default_factory=lambda: os.getenv(
            "SWARM_DOCKER_IMAGE", DEFAULT_DOCKER_IMAGE
        )
    )
    container_cpus: str = field(
        default_factory=lambda: os.getenv(
            "SWARM_CONTAINER_CPUS", DEFAULT_CONTAINER_CPUS
        )
    )
    container_memory: str = field(
        default_factory=lambda: os.getenv(
            "SWARM_CONTAINER_MEMORY", DEFAULT_CONTAINER_MEMORY
        )
    )
    container_pids_limit: int = field(
        default_factory=lambda: int(
            os.getenv(
                "SWARM_CONTAINER_PIDS_LIMIT", str(DEFAULT_CONTAINER_PIDS_LIMIT)
            )
        )
    )
    container_timeout_sec: int = field(
        default_factory=lambda: int(
            os.getenv(
                "SWARM_CONTAINER_TIMEOUT_SEC", str(DEFAULT_CONTAINER_TIMEOUT_SEC)
            )
        )
    )
    sandbox_base_dir: str = field(
        default_factory=lambda: os.getenv(
            "SWARM_SANDBOX_DIR", DEFAULT_SANDBOX_BASE_DIR
        )
    )
    evm_payout_address: str = EVM_PAYOUT_ADDRESS
    stellar_payout_address: str = STELLAR_PAYOUT_ADDRESS
    solana_payout_address: str = SOLANA_PAYOUT_ADDRESS
    payout_routing: Dict[str, str] = field(
        default_factory=lambda: dict(PAYOUT_ROUTING)
    )


_CONFIG_INSTANCE: Optional[SwarmConfig] = None


def get_config(force_reload: bool = False) -> SwarmConfig:
    """Retrieves or initializes the global singleton configuration."""
    global _CONFIG_INSTANCE
    if _CONFIG_INSTANCE is None or force_reload:
        _CONFIG_INSTANCE = SwarmConfig()
    return _CONFIG_INSTANCE
