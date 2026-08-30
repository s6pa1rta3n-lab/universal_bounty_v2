"""
Core Exceptions for Universal Bounty Engine V2.
Defines system-wide exception hierarchy and specialized security errors.
"""

from typing import Optional


class BountySwarmError(Exception):
    """Base exception for all Universal Bounty Engine V2 errors."""

    pass


class ProtectedPathViolationError(PermissionError, BountySwarmError):
    """
    Raised when any filesystem, traversal, or container mount operation
    attempts to access a path contained within the IGNORE_LIST.
    Inherits from PermissionError to satisfy system-level security constraints.
    """

    def __init__(
        self,
        message: str,
        path: Optional[str] = None,
        operation: Optional[str] = None,
    ):
        super().__init__(message)
        self.path = path
        self.operation = operation


class ConfigurationError(BountySwarmError):
    """Raised when configuration values are missing, invalid, or conflicting."""

    pass


class SafeIOError(BountySwarmError):
    """Raised when atomic I/O, file locking, or JSONL operations fail."""

    pass


class FirestoreSyncError(BountySwarmError):
    """Raised when Firestore synchronization or document transaction fails."""

    pass


class OfflineModeError(FirestoreSyncError):
    """Raised when an operation cannot be completed in offline mode."""

    pass


class MigrationError(BountySwarmError):
    """Raised when queue or state migration encounters unrecoverable data errors."""

    pass


class GitHubAPIError(BountySwarmError):
    """Raised when GitHub GraphQL or REST API requests fail or exceed rate limits."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class ExecutorError(BountySwarmError):
    """Raised when isolated container execution fails or encounters runtime errors."""

    pass


class ContainerExecutionTimeoutError(ExecutorError):
    """Raised when an ephemeral container execution exceeds its allotted timeout."""

    pass


class SniperFilterError(BountySwarmError):
    """Raised when an issue fails critical qualification or policy filters."""

    pass
