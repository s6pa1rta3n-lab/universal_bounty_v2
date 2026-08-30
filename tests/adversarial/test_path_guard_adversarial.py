"""
Adversarial Verification Suite for PathGuard (Milestone 1).
Stress-tests symlinks, APFS firmlinks, macOS /private aliases, relative traversals,
case-folding, circular links, and boundary conditions.
"""

import os
from pathlib import Path

import pytest

from src.core.exceptions import ProtectedPathViolationError
from src.core.path_guard import PathGuard


class TestPathGuardSymlinkAttacks:
    """Empirical adversarial testing of symlink bypass techniques."""

    def test_direct_symlink_to_protected_dir(self, temp_workspace: Path):
        protected = temp_workspace / "secret_vault"
        protected.mkdir(parents=True, exist_ok=True)
        guard = PathGuard(ignore_list=[str(protected)])

        # Attacker creates symlink pointing to protected dir
        attacker_link = temp_workspace / "innocent_link"
        attacker_link.symlink_to(protected)

        assert guard.is_protected(attacker_link) is True
        with pytest.raises(ProtectedPathViolationError):
            guard.validate_access(attacker_link, operation="symlink_read")

    def test_relative_symlink_escape(self, temp_workspace: Path):
        nested_dir = temp_workspace / "a" / "b" / "c"
        nested_dir.mkdir(parents=True, exist_ok=True)
        protected = temp_workspace / "vault"
        protected.mkdir(parents=True, exist_ok=True)
        guard = PathGuard(ignore_list=[str(protected)])

        # Create relative symlink ../../../vault
        rel_link = nested_dir / "escape_link"
        os.symlink("../../../vault", str(rel_link))

        assert guard.is_protected(rel_link) is True
        assert guard.is_protected(rel_link / "data.json") is True
        with pytest.raises(ProtectedPathViolationError):
            guard.validate_access(rel_link / "data.json")

    def test_symlink_to_parent_traversing_down(self, temp_workspace: Path):
        parent_dir = temp_workspace / "parent_box"
        protected = parent_dir / "target_trading"
        protected.mkdir(parents=True, exist_ok=True)
        guard = PathGuard(ignore_list=[str(protected)])

        # Attacker links to parent_box, then traverses down into target_trading
        symlink_parent = temp_workspace / "link_to_parent"
        symlink_parent.symlink_to(parent_dir)

        attack_path = symlink_parent / "target_trading"
        assert guard.is_protected(attack_path) is True
        assert guard.is_protected(attack_path / "keys.json") is True

        with pytest.raises(ProtectedPathViolationError):
            guard.validate_access(attack_path)

    def test_multi_hop_deep_symlink_chain(self, temp_workspace: Path):
        protected = temp_workspace / "deep_vault"
        protected.mkdir(parents=True, exist_ok=True)
        guard = PathGuard(ignore_list=[str(protected)])

        current = protected
        links = []
        for i in range(10):
            link = temp_workspace / f"hop_{i}"
            link.symlink_to(current)
            current = link
            links.append(link)

        # The 10th link resolves back through all 10 hops to protected
        assert guard.is_protected(links[-1]) is True
        assert guard.is_protected(links[-1] / "nested" / "file.txt") is True

    def test_dangling_symlink_targeting_nonexistent_protected_subpath(self, temp_workspace: Path):
        protected = temp_workspace / "nonexistent_vault"
        # Directory does not even exist on disk yet
        guard = PathGuard(ignore_list=[str(protected)])

        dangling_link = temp_workspace / "dangling_link"
        dangling_link.symlink_to(protected / "future_sub" / "secret.env")

        assert guard.is_protected(dangling_link) is True
        with pytest.raises(ProtectedPathViolationError):
            guard.validate_access(dangling_link)

    def test_symlink_inside_protected_pointing_out_vs_pointing_in(self, temp_workspace: Path):
        protected = temp_workspace / "vault"
        protected.mkdir(parents=True, exist_ok=True)
        safe_dir = temp_workspace / "safe_sandbox"
        safe_dir.mkdir(parents=True, exist_ok=True)
        safe_file = safe_dir / "safe.txt"
        safe_file.write_text("safe content")

        guard = PathGuard(ignore_list=[str(protected)])

        # A symlink located INSIDE protected directory pointing to safe_file
        link_inside_protected = protected / "link_to_safe"
        link_inside_protected.symlink_to(safe_file)

        # Path itself is inside protected directory hierarchy, so access via protected path is blocked
        assert guard.is_protected(link_inside_protected) is True


class TestPathGuardMacOSAliasesAndFirmlinks:
    """Empirical testing of macOS /private and APFS firmlinks."""

    def test_macos_private_prefix_resolution(self):
        guard = PathGuard()
        home = str(Path.home())

        # Test /private/Users/... vs /Users/...
        if home.startswith("/Users/"):
            private_home = "/private" + home
            assert guard.is_protected(f"{private_home}/teamwork_projects/odin") is True
            assert guard.is_protected(f"{private_home}/teamwork_projects/keeper_daemon") is True
            assert guard.is_protected(f"{private_home}/teamwork_projects/matt-berserker") is True

    def test_macos_apfs_firmlink_volume_data_prefix(self):
        guard = PathGuard()
        home = str(Path.home())

        # Test /System/Volumes/Data/Users/... vs /Users/...
        firmlink_home = f"/System/Volumes/Data{home}"
        assert guard.is_protected(f"{firmlink_home}/teamwork_projects/odin") is True
        assert guard.is_protected(f"{firmlink_home}/teamwork_projects/keeper_daemon") is True
        assert guard.is_protected(f"{firmlink_home}/teamwork_projects/matt-berserker") is True

    def test_combined_apfs_firmlink_and_private_prefix(self):
        guard = PathGuard()
        home = str(Path.home())

        if home.startswith("/Users/"):
            combined = f"/System/Volumes/Data/private{home}/teamwork_projects/odin"
            # Canonical resolution will normalize and check containment
            assert guard.is_protected(combined) is True
            assert guard.is_protected(f"/System/Volumes/Data/private{home}/teamwork_projects/keeper_daemon") is True
            assert guard.is_protected(f"/System/Volumes/Data/private{home}/teamwork_projects/matt-berserker") is True

    def test_cross_product_expansion_variants(self):
        # Explicitly verify variant generation
        variants = PathGuard._get_canonical_variants("/System/Volumes/Data/private/Users/foo/teamwork_projects/odin")
        variant_strs = {str(v) for v in variants}
        assert "/Users/foo/teamwork_projects/odin" in variant_strs
        assert "/private/Users/foo/teamwork_projects/odin" in variant_strs
        assert "/System/Volumes/Data/Users/foo/teamwork_projects/odin" in variant_strs
        assert "/System/Volumes/Data/private/Users/foo/teamwork_projects/odin" in variant_strs


class TestPathGuardRelativeTraversalsAndNormalization:
    """Empirical testing of complex relative path navigations and redundant slashes."""

    def test_extreme_relative_dot_escapes(self):
        guard = PathGuard()
        home = str(Path.home())

        # Escape from deeply nested innocent directories (3 levels up reaches home directory)
        attack1 = f"{home}/Desktop/activeProjects/universal_bounty_swarm/../../../teamwork_projects/odin"
        assert guard.is_protected(attack1) is True

        # 2 levels up reaches Desktop/teamwork_projects/odin which is NOT a protected directory
        non_escape = f"{home}/Desktop/activeProjects/universal_bounty_swarm/../../teamwork_projects/odin"
        assert guard.is_protected(non_escape) is False

        attack2 = f"/tmp/bounty_sandboxes/issue_123/../../..{home}/teamwork_projects/keeper_daemon"
        assert guard.is_protected(attack2) is True

        attack3 = "~/teamwork_projects/tea_octant_registration/../matt-berserker/strategies"
        assert guard.is_protected(attack3) is True

    def test_redundant_slashes_and_current_dir_dots(self):
        guard = PathGuard()
        home = str(Path.home())

        # Path with redundant slashes and dots
        messy_path = f"{home}///teamwork_projects/././odin//keys/../config.json"
        assert guard.is_protected(messy_path) is True

        trailing_slash = f"{home}/teamwork_projects/odin/"
        assert guard.is_protected(trailing_slash) is True

        double_dot_internal = f"{home}/teamwork_projects/odin/sub/../deep/file.txt"
        assert guard.is_protected(double_dot_internal) is True


class TestPathGuardCaseSensitivity:
    """Empirical testing of case folding (APFS default case-insensitivity)."""

    def test_case_variations_on_protected_paths(self):
        guard = PathGuard()
        home = str(Path.home())

        assert guard.is_protected(f"{home}/teamwork_projects/ODIN") is True
        assert guard.is_protected(f"{home}/teamwork_projects/Odin") is True
        assert guard.is_protected(f"{home}/TEAMWORK_PROJECTS/odin") is True
        assert guard.is_protected(f"{home}/teamwork_projects/KEEPER_DAEMON") is True
        assert guard.is_protected(f"{home}/teamwork_projects/Keeper_Daemon/sub") is True
        assert guard.is_protected(f"{home}/teamwork_projects/MATT-BERSERKER") is True
        assert guard.is_protected(f"{home}/teamwork_projects/Matt-Berserker/strat.py") is True


class TestPathGuardFalsePositiveResistance:
    """Ensures legitimate workspace paths are NEVER falsely blocked."""

    def test_prefix_and_sibling_directories_allowed(self):
        guard = PathGuard()
        home = str(Path.home())

        # Allowed siblings that share substring names
        allowed_paths = [
            f"{home}/teamwork_projects/odin_v2",
            f"{home}/teamwork_projects/odin-backup",
            f"{home}/teamwork_projects/odin_archive",
            f"{home}/teamwork_projects/odin.config",
            f"{home}/teamwork_projects/keeper_daemon_v2",
            f"{home}/teamwork_projects/keeper_daemon_backup",
            f"{home}/teamwork_projects/matt-berserker-v2",
            f"{home}/teamwork_projects/matt_berserker",
            f"{home}/teamwork_projects/universal_bounty_v2",
            f"{home}/teamwork_projects/tea_octant_registration",
            f"{home}/Desktop/activeProjects/universal_bounty_swarm",
            f"{home}/Desktop/activeProjects/bounty_operations",
            "/tmp/bounty_sandboxes/issue_999",
            "/private/tmp/safe_worker",
        ]

        for p in allowed_paths:
            assert guard.is_protected(p) is False, f"Path falsely marked protected: {p}"
            # Validate access must succeed
            resolved = guard.validate_access(p)
            assert isinstance(resolved, Path)


class TestPathGuardEdgeAndBoundaryInputs:
    """Empirical testing of invalid, empty, or special parameter types."""

    def test_none_and_empty_inputs(self):
        guard = PathGuard()
        assert guard.is_protected(None) is False
        assert guard.is_protected("") is False
        assert guard.is_protected("   ") is False

        with pytest.raises(ValueError):
            guard.validate_access(None)

        with pytest.raises(ValueError):
            guard.validate_access("")

        with pytest.raises(ValueError):
            guard.validate_access("   ")

    def test_get_ignore_list_immutability(self):
        guard = PathGuard()
        protected_list = guard.get_ignore_list()
        original_len = len(protected_list)

        # Mutating the returned list should not affect the guard instance
        protected_list.append(Path("/tmp/fake_addition"))
        assert len(guard.get_ignore_list()) == original_len
        assert guard.is_protected("/tmp/fake_addition") is False
