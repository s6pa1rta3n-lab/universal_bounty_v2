"""
Unit Tests for PathGuard Filesystem Containment & Security Rules (Milestone 1).
"""

import os
from pathlib import Path

import pytest

from src.core.config import DEFAULT_IGNORE_LIST
from src.core.exceptions import ProtectedPathViolationError
from src.core.path_guard import (
    PathGuard,
)


class TestPathGuardBaseline:
    """Verifies standard detection of all baseline protected trading directories."""

    def test_default_ignore_list_paths(self):
        guard = PathGuard()
        for raw_path in DEFAULT_IGNORE_LIST:
            assert guard.is_protected(raw_path) is True
            expanded = os.path.expanduser(raw_path)
            assert guard.is_protected(expanded) is True
            assert guard.is_protected(Path(expanded)) is True

    def test_keeper_daemon_exact_and_nested(self):
        guard = PathGuard()
        assert guard.is_protected("~/teamwork_projects/keeper_daemon") is True
        assert guard.is_protected("~/teamwork_projects/keeper_daemon/") is True
        assert guard.is_protected("~/teamwork_projects/keeper_daemon/config.json") is True
        assert (
            guard.is_protected(
                "~/teamwork_projects/keeper_daemon/src/deep/nested/worker.py"
            )
            is True
        )

    def test_odin_exact_and_nested(self):
        guard = PathGuard()
        assert guard.is_protected("~/teamwork_projects/odin") is True
        assert guard.is_protected("~/teamwork_projects/odin/") is True
        assert guard.is_protected("~/teamwork_projects/odin/.env") is True
        assert guard.is_protected("~/teamwork_projects/odin/keys/private.pem") is True

    def test_matt_berserker_exact_and_nested(self):
        guard = PathGuard()
        assert guard.is_protected("~/teamwork_projects/matt-berserker") is True
        assert guard.is_protected("~/teamwork_projects/matt-berserker/") is True
        assert (
            guard.is_protected("~/teamwork_projects/matt-berserker/strategies/main.py")
            is True
        )

    def test_allowed_workspaces(self):
        guard = PathGuard()
        assert guard.is_protected("/tmp/bounty_sandboxes/issue_42") is False
        assert (
            guard.is_protected("~/Desktop/activeProjects/universal_bounty_swarm")
            is False
        )
        assert guard.is_protected("~/Desktop/activeProjects/bounty_operations") is False
        assert guard.is_protected("/private/tmp/safe_run") is False

    def test_prefix_collision_prevention(self):
        """Ensures that paths sharing a prefix name are NOT falsely blocked."""
        guard = PathGuard()
        # Similar names to 'odin'
        assert guard.is_protected("~/teamwork_projects/odin_backup") is False
        assert guard.is_protected("~/teamwork_projects/odin2") is False
        assert guard.is_protected("~/teamwork_projects/odin-production") is False
        assert guard.is_protected("~/teamwork_projects/odin_test_dir/file.py") is False

        # Similar names to 'keeper_daemon'
        assert guard.is_protected("~/teamwork_projects/keeper_daemon_v2") is False
        assert guard.is_protected("~/teamwork_projects/keeper_daemon_archive") is False

        # Similar names to 'matt-berserker'
        assert guard.is_protected("~/teamwork_projects/matt-berserker-v2") is False
        assert guard.is_protected("~/teamwork_projects/matt-berserker_copy") is False
        assert guard.is_protected("~/teamwork_projects/matt_berserker") is False

        # Sibling directories under teamwork_projects
        assert guard.is_protected("~/teamwork_projects/tea_octant_registration") is False
        assert guard.is_protected("~/teamwork_projects/universal_bounty_v2") is False


class TestPathGuardAdversarialContainment:
    """Verifies resilience against symlink attacks, traversals, and firmlinks."""

    def test_relative_dot_traversal_detection(self):
        guard = PathGuard()
        # Traversal into odin via sibling directory
        traversal = "~/teamwork_projects/tea_octant_registration/../odin"
        assert guard.is_protected(traversal) is True

        traversal_file = "~/teamwork_projects/tea_octant_registration/../odin/.env"
        assert guard.is_protected(traversal_file) is True

        traversal_keeper = "~/teamwork_projects/tea_octant_registration/../keeper_daemon/secrets.json"
        assert guard.is_protected(traversal_keeper) is True

    def test_symlink_bypass_prevention(self, temp_workspace: Path):
        # Create a protected target directory inside test workspace
        protected_target = temp_workspace / "secret_trading_vault"
        protected_target.mkdir(parents=True, exist_ok=True)
        secret_file = protected_target / "keys.pem"
        secret_file.write_text("SUPER_SECRET_KEY")

        guard = PathGuard(ignore_list=[str(protected_target)])

        # Create a symlink pointing to the protected directory
        symlink_dir = temp_workspace / "innocent_symlink"
        symlink_dir.symlink_to(protected_target)

        assert guard.is_protected(symlink_dir) is True
        assert guard.is_protected(symlink_dir / "keys.pem") is True

        with pytest.raises(ProtectedPathViolationError) as exc_info:
            guard.validate_access(symlink_dir / "keys.pem", operation="read")
        assert exc_info.value.operation == "read"

    def test_multi_hop_symlink_bypass_prevention(self, temp_workspace: Path):
        # Multi-hop symlink chain: link3 -> link2 -> link1 -> protected_dir
        protected_dir = temp_workspace / "isolated_trading_repo"
        protected_dir.mkdir(parents=True, exist_ok=True)
        guard = PathGuard(ignore_list=[str(protected_dir)])

        link1 = temp_workspace / "link_hop_1"
        link2 = temp_workspace / "link_hop_2"
        link3 = temp_workspace / "link_hop_3"

        link1.symlink_to(protected_dir)
        link2.symlink_to(link1)
        link3.symlink_to(link2)

        assert guard.is_protected(link1) is True
        assert guard.is_protected(link2) is True
        assert guard.is_protected(link3) is True
        assert guard.is_protected(link3 / "subfile.txt") is True

    def test_validate_access_allowed_vs_protected(self, temp_workspace: Path):
        protected_dir = temp_workspace / "vault"
        protected_dir.mkdir(parents=True, exist_ok=True)
        allowed_dir = temp_workspace / "workspace"
        allowed_dir.mkdir(parents=True, exist_ok=True)

        guard = PathGuard(ignore_list=[str(protected_dir)])

        # Allowed access returns resolved Path
        res = guard.validate_access(allowed_dir, operation="read")
        assert isinstance(res, Path)
        assert res.resolve() == allowed_dir.resolve()

        # Protected access raises ProtectedPathViolationError
        with pytest.raises(ProtectedPathViolationError) as exc:
            guard.validate_access(protected_dir, operation="write")
        assert exc.value.operation == "write"

        # None or empty raises ValueError
        with pytest.raises(ValueError):
            guard.validate_access(None)
        with pytest.raises(ValueError):
            guard.validate_access("")

    def test_validate_mount(self, temp_workspace: Path):
        protected_dir = temp_workspace / "trading_storage"
        protected_dir.mkdir(parents=True, exist_ok=True)
        guard = PathGuard(ignore_list=[str(protected_dir)])

        with pytest.raises(ProtectedPathViolationError) as exc:
            guard.validate_mount(protected_dir)
        assert exc.value.operation == "docker_volume_mount"
