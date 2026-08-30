"""
Core components for Universal Bounty Engine V2.
"""

from src.core.config import (
    BANNED_PLATFORMS,
    DEFAULT_GCP_PROJECT_ID,
    DEFAULT_IGNORE_LIST,
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
from src.core.exceptions import (
    BountySwarmError,
    ConfigurationError,
    ContainerExecutionTimeoutError,
    ExecutorError,
    FirestoreSyncError,
    GitHubAPIError,
    MigrationError,
    OfflineModeError,
    ProtectedPathViolationError,
    SafeIOError,
    SniperFilterError,
)
from src.core.firestore_client import (
    COLLECTION_BOUNTY_LEADS,
    COLLECTION_BOUNTY_MEMORY,
    COLLECTION_BOUNTY_SETTLEMENTS,
    COLLECTION_BOUNTY_TASKS,
    COLLECTION_SWARM_COORDINATOR,
    COLLECTION_SWARM_OPERATIONS,
    OfflineFirestoreClient,
    claim_lead_atomic,
    claim_lead_transaction,
    get_collection,
    get_coordinator_collection,
    get_firestore_client,
    get_leads_collection,
    get_memory_collection,
    get_operations_collection,
    get_settlements_collection,
    initialize_firebase_app,
    resolve_credentials_path,
    resolve_project_id,
)
from src.core.github_client import GitHubClient, get_github_client
from src.core.orbstack_executor import (
    ContainerExecutionResult,
    EphemeralOrbStackExecutor,
)
from src.core.path_guard import DEFAULT_PATH_GUARD, PathGuard, is_protected, validate_access
from src.core.safe_io import SafeIO

__all__ = [
    # Config
    "BANNED_PLATFORMS",
    "DEFAULT_IGNORE_LIST",
    "EVM_PAYOUT_ADDRESS",
    "STELLAR_PAYOUT_ADDRESS",
    "SOLANA_PAYOUT_ADDRESS",
    "PAYOUT_ROUTING",
    "DEFAULT_GCP_PROJECT_ID",
    "DISQUALIFY_KEYWORDS",
    "HIGH_PRIORITY_KEYWORDS",
    "SwarmConfig",
    "get_config",
    "is_banned_platform",
    "contains_disqualify_keywords",
    # Exceptions
    "BountySwarmError",
    "ProtectedPathViolationError",
    "ConfigurationError",
    "SafeIOError",
    "FirestoreSyncError",
    "OfflineModeError",
    "MigrationError",
    "GitHubAPIError",
    "ExecutorError",
    "ContainerExecutionTimeoutError",
    "SniperFilterError",
    # PathGuard
    "PathGuard",
    "DEFAULT_PATH_GUARD",
    "is_protected",
    "validate_access",
    # SafeIO
    "SafeIO",
    # Firestore
    "COLLECTION_BOUNTY_LEADS",
    "COLLECTION_SWARM_OPERATIONS",
    "COLLECTION_SWARM_COORDINATOR",
    "COLLECTION_BOUNTY_TASKS",
    "COLLECTION_BOUNTY_MEMORY",
    "COLLECTION_BOUNTY_SETTLEMENTS",
    "OfflineFirestoreClient",
    "resolve_credentials_path",
    "resolve_project_id",
    "initialize_firebase_app",
    "get_firestore_client",
    "get_collection",
    "get_leads_collection",
    "get_operations_collection",
    "get_memory_collection",
    "get_settlements_collection",
    "get_coordinator_collection",
    "claim_lead_atomic",
    "claim_lead_transaction",
    # GitHub Client
    "GitHubClient",
    "get_github_client",
    # OrbStack Executor
    "EphemeralOrbStackExecutor",
    "ContainerExecutionResult",
]
