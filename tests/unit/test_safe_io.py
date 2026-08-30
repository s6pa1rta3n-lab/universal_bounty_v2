"""
Unit Tests for SafeIO Filesystem Gateway & Atomic Operations (Milestone 1).
"""

import json
from pathlib import Path

import pytest

from src.core.exceptions import ProtectedPathViolationError, SafeIOError
from src.core.path_guard import PathGuard
from src.core.safe_io import SafeIO


class TestSafeIOBasicOperations:
    """Verifies standard read, write, copy, move, and directory operations."""

    def test_text_and_bytes_read_write(self, temp_workspace: Path):
        test_file = temp_workspace / "sub" / "test.txt"
        content = "Universal Bounty Engine V2\nSafeIO Verification."

        # Write text creating parent dirs
        SafeIO.write_text(test_file, content)
        assert test_file.exists()
        assert SafeIO.read_text(test_file) == content

        # Write bytes
        bin_file = temp_workspace / "binary.dat"
        raw_bytes = b"\x00\x01\x02\x03\xff\xfe"
        SafeIO.write_bytes(bin_file, raw_bytes)
        assert SafeIO.read_bytes(bin_file) == raw_bytes

    def test_atomic_write_text(self, temp_workspace: Path):
        atomic_file = temp_workspace / "state" / "atomic_target.json"
        data = json.dumps({"status": "synced", "ops": 42})

        SafeIO.atomic_write_text(atomic_file, data)
        assert atomic_file.exists()
        assert SafeIO.read_text(atomic_file) == data

        # Overwrite atomically
        updated_data = json.dumps({"status": "synced", "ops": 43})
        SafeIO.atomic_write_text(atomic_file, updated_data)
        assert SafeIO.read_text(atomic_file) == updated_data

    def test_file_lifecycle_mkdir_touch_copy_move_delete(self, temp_workspace: Path):
        dir_path = temp_workspace / "workspace_a"
        SafeIO.mkdir(dir_path)
        assert SafeIO.is_dir(dir_path) is True

        file_path = dir_path / "data.txt"
        SafeIO.touch(file_path)
        assert SafeIO.is_file(file_path) is True

        SafeIO.write_text(file_path, "Initial text")

        # Copy
        copy_dest = temp_workspace / "workspace_b" / "data_copy.txt"
        SafeIO.copy_file(file_path, copy_dest)
        assert SafeIO.exists(copy_dest) is True
        assert SafeIO.read_text(copy_dest) == "Initial text"

        # Move
        move_dest = temp_workspace / "workspace_c" / "data_moved.txt"
        SafeIO.move(copy_dest, move_dest)
        assert SafeIO.exists(copy_dest) is False
        assert SafeIO.exists(move_dest) is True

        # Delete
        SafeIO.delete_file(file_path)
        assert SafeIO.exists(file_path) is False


class TestSafeIOJSONLProcessing:
    """Verifies stream_jsonl, read_jsonl, write_jsonl, and append_jsonl."""

    def test_write_read_and_stream_jsonl(self, temp_workspace: Path):
        jsonl_path = temp_workspace / "queue.jsonl"
        records = [
            {"id": "lead_1", "payout": 500, "status": "queued"},
            {"id": "lead_2", "payout": 1200, "status": "claimed"},
            {"id": "lead_3", "payout": 250, "status": "completed"},
        ]

        # Atomic write
        SafeIO.write_jsonl(jsonl_path, records, atomic=True)
        assert jsonl_path.exists()

        # Read back
        loaded = SafeIO.read_jsonl(jsonl_path)
        assert len(loaded) == 3
        assert loaded[0]["id"] == "lead_1"
        assert loaded[1]["payout"] == 1200

        # Stream
        streamed = list(SafeIO.stream_jsonl(jsonl_path))
        assert streamed == loaded

    def test_append_jsonl(self, temp_workspace: Path):
        jsonl_path = temp_workspace / "stream.jsonl"
        SafeIO.append_jsonl(jsonl_path, {"event": "start", "time": 100})
        SafeIO.append_jsonl(jsonl_path, {"event": "stop", "time": 200})

        loaded = SafeIO.read_jsonl(jsonl_path)
        assert len(loaded) == 2
        assert loaded[0]["event"] == "start"
        assert loaded[1]["event"] == "stop"

    def test_corrupt_jsonl_handling(self, temp_workspace: Path):
        corrupt_path = temp_workspace / "corrupt.jsonl"
        # Write valid line followed by malformed json line
        corrupt_path.write_text('{"id": 1}\n{MALFORMED_JSON\n{"id": 2}\n')

        with pytest.raises(SafeIOError) as exc_info:
            SafeIO.read_jsonl(corrupt_path)
        assert "Corrupt JSONL record" in str(exc_info.value)


class TestSafeIOPathGuardEnforcement:
    """Verifies that SafeIO operations respect PathGuard rules."""

    def test_protected_read_write_delete_blocked(self, temp_workspace: Path):
        protected_dir = temp_workspace / "protected_vault"
        protected_dir.mkdir(parents=True, exist_ok=True)
        guard = PathGuard(ignore_list=[str(protected_dir)])
        SafeIO.set_guard(guard)

        protected_file = protected_dir / "secret.txt"

        # Direct read/write/delete attempts on protected path must raise
        with pytest.raises(ProtectedPathViolationError):
            SafeIO.write_text(protected_file, "exploit")

        with pytest.raises(ProtectedPathViolationError):
            SafeIO.atomic_write_text(protected_file, "exploit")

        with pytest.raises(ProtectedPathViolationError):
            SafeIO.read_text(protected_file)

        with pytest.raises(ProtectedPathViolationError):
            SafeIO.delete_file(protected_file)

        with pytest.raises(ProtectedPathViolationError):
            SafeIO.rmtree(protected_dir)

    def test_rmtree_ancestor_protection(self, temp_workspace: Path):
        """Ensures that rmtree on a parent directory containing a protected path is strictly forbidden."""
        parent_dir = temp_workspace / "parent_container"
        protected_child = parent_dir / "secret_vault"
        protected_child.mkdir(parents=True, exist_ok=True)

        guard = PathGuard(ignore_list=[str(protected_child)])
        SafeIO.set_guard(guard)

        with pytest.raises(ProtectedPathViolationError) as exc_info:
            SafeIO.rmtree(parent_dir)
        assert "strictly forbidden on parent directory containing protected paths" in str(exc_info.value)

    def test_walk_filters_protected_paths(self, temp_workspace: Path):
        root_dir = temp_workspace / "project_root"
        root_dir.mkdir(parents=True, exist_ok=True)

        safe_sub = root_dir / "safe_module"
        safe_sub.mkdir(parents=True, exist_ok=True)
        (safe_sub / "main.py").write_text("print('safe')")

        protected_sub = root_dir / "protected_keys"
        protected_sub.mkdir(parents=True, exist_ok=True)
        (protected_sub / "id_rsa").write_text("SECRET_KEY")

        guard = PathGuard(ignore_list=[str(protected_sub)])
        SafeIO.set_guard(guard)

        all_walked_files = []
        for _, dirs, files in SafeIO.walk(root_dir):
            all_walked_files.extend(files)

        assert "main.py" in all_walked_files
        assert "id_rsa" not in all_walked_files
