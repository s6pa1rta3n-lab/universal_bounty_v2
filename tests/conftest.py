"""
Pytest configuration and shared fixtures for Universal Bounty Engine V2 tests.
"""

import shutil
import sys
import tempfile
from pathlib import Path
from typing import Generator, List

import pytest

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.firestore_client import OfflineFirestoreClient  # noqa: E402
from src.core.path_guard import PathGuard  # noqa: E402
from src.core.safe_io import SafeIO  # noqa: E402


@pytest.fixture
def temp_workspace() -> Generator[Path, None, None]:
    """Provides a temporary, isolated workspace directory."""
    temp_dir = tempfile.mkdtemp(prefix="bounty_v2_test_ws_")
    ws_path = Path(temp_dir).resolve()
    yield ws_path
    if ws_path.exists():
        shutil.rmtree(ws_path, ignore_errors=True)


@pytest.fixture
def offline_db(temp_workspace: Path) -> OfflineFirestoreClient:
    """Provides an isolated OfflineFirestoreClient storing data in temp_workspace."""
    state_dir = temp_workspace / "offline_firestore"
    return OfflineFirestoreClient(project_id="test-project-v2", state_dir=state_dir)


@pytest.fixture
def custom_guard(temp_workspace: Path) -> Generator[PathGuard, None, None]:
    """Provides a PathGuard instance with an additional custom protected directory in temp_workspace."""
    custom_protected = temp_workspace / "custom_protected_trading"
    custom_protected.mkdir(parents=True, exist_ok=True)
    guard = PathGuard(ignore_list=[str(custom_protected)])
    original_guard = SafeIO.get_guard()
    SafeIO.set_guard(guard)
    yield guard
    SafeIO.set_guard(original_guard)


@pytest.fixture
def sample_legacy_leads() -> List[dict]:
    """Provides a realistic sample of legacy intake_queue leads."""
    return [
        {
            "id": "I_kwDO123456",
            "number": 101,
            "title": "Build Soroban smart contract verification bridge ($1,500 reward)",
            "url": "https://github.com/stellar/soroban-bridge/issues/101",
            "body": "GrantFox OSS escrow verified bounty. Implement Soroban smart contract test suite.",
            "repository": "stellar/soroban-bridge",
            "labels": [{"name": "grantfox"}, {"name": "bounty"}, {"name": "soroban"}],
            "priority": "high",
            "status": "queued",
            "projected_payout": "$1500",
            "projected_payout_usd": 1500.0,
            "qualification_reason": "GrantFox verified escrow",
            "ecosystem": "stellar",
            "escrow_verified": True,
        },
        {
            "id": "I_kwDO789012",
            "number": 42,
            "title": "Fix Base L2 ERC4361 Auth validation ($300)",
            "url": "https://github.com/base-org/web3-auth/issues/42",
            "body": "Fix SIWE signature recovery on Base network.",
            "repository": {"nameWithOwner": "base-org/web3-auth"},
            "labels": ["evm", "base", "security"],
            "priority": "high",
            "status": "pending_triage",
            "projected_payout_usd": 300.0,
            "ecosystem": "evm",
            "escrow_verified": True,
        },
        {
            "id": "I_kwDO333333",
            "number": 15,
            "title": "Improve documentation for plugin setup",
            "url": "https://github.com/example/docs/issues/15",
            "body": "Fix markdown typo in tutorial.",
            "repository": "example/docs",
            "labels": ["documentation"],
            "priority": "low",
            "status": "queued",
            "projected_payout_usd": 25.0,
            "ecosystem": "general",
            "escrow_verified": False,
        },
    ]
