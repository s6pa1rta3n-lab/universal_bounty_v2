"""
Specialized Engine Tier for Universal Bounty Engine V2.
Consolidates discrete microservices into deterministic batch engines.
"""

from src.engines.escort_engine import (
    EXTERNAL_DEPLOYMENT_GATES,
    IGNORE_BOTS,
    EscortEngine,
    is_external_deployment_gate,
)
from src.engines.executor_engine import (
    MANDATORY_PAYOUT_ROUTING_BLOCK,
    ExecutorEngine,
    extract_stipulations_from_text,
)
from src.engines.inbox_engine import (
    InboxEngine,
    clean_reply_quotes,
    decode_mime_header,
    extract_text_from_email_message,
)
from src.engines.intake_engine import (
    DEFAULT_SEARCH_CATEGORIES,
    IntakeEngine,
    clean_text_for_financials,
    extract_financials,
    generate_canonical_doc_id,
    verify_escrow,
)
from src.engines.sync_engine import (
    SyncEngine,
    extract_payout_numeric,
)

__all__ = [
    # Intake Engine
    "IntakeEngine",
    "verify_escrow",
    "extract_financials",
    "clean_text_for_financials",
    "generate_canonical_doc_id",
    "DEFAULT_SEARCH_CATEGORIES",
    # Executor Engine
    "ExecutorEngine",
    "extract_stipulations_from_text",
    "MANDATORY_PAYOUT_ROUTING_BLOCK",
    # Inbox Engine
    "InboxEngine",
    "clean_reply_quotes",
    "decode_mime_header",
    "extract_text_from_email_message",
    # Escort Engine
    "EscortEngine",
    "IGNORE_BOTS",
    "EXTERNAL_DEPLOYMENT_GATES",
    "is_external_deployment_gate",
    # Sync Engine
    "SyncEngine",
    "extract_payout_numeric",
]
