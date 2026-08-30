"""
Unit Tests for EphemeralOrbStackExecutor & Container Lifecycle Management.
Validates isolated container execution, resource quotas, mandatory --rm flag,
PathGuard IGNORE_LIST volume mount protection, timeout handling, and cleanup.
"""

import subprocess
from unittest.mock import patch

import pytest

from src.core.exceptions import ProtectedPathViolationError
from src.core.orbstack_executor import (
    ContainerExecutionResult,
    EphemeralOrbStackExecutor,
)
from src.core.path_guard import PathGuard


class TestContainerExecutionResult:
    def test_result_properties_and_serialization(self):
        res = ContainerExecutionResult(
            container_name="bounty-exec-123456",
            exit_code=0,
            stdout="Success output",
            stderr="",
            duration_sec=1.23456,
            timed_out=False,
        )
        assert res.success is True
        assert res.timed_out is False
        assert "bounty-exec-123456" in repr(res)

        data = res.to_dict()
        assert data["container_name"] == "bounty-exec-123456"
        assert data["exit_code"] == 0
        assert data["success"] is True
        assert data["duration_sec"] == 1.2346

    def test_failed_and_timed_out_results(self):
        res_fail = ContainerExecutionResult(
            container_name="bounty-exec-fail",
            exit_code=1,
            stdout="",
            stderr="Error occurred",
            duration_sec=0.5,
        )
        assert res_fail.success is False

        res_timeout = ContainerExecutionResult(
            container_name="bounty-exec-timeout",
            exit_code=-1,
            stdout="",
            stderr="Timed out",
            duration_sec=300.0,
            timed_out=True,
        )
        assert res_timeout.success is False
        assert res_timeout.timed_out is True


class TestEphemeralOrbStackExecutor:
    @pytest.fixture
    def custom_path_guard(self, tmp_path):
        protected = tmp_path / "protected_trading"
        protected.mkdir()
        return PathGuard(ignore_list=[str(protected)])

    @pytest.fixture
    def executor(self, custom_path_guard):
        return EphemeralOrbStackExecutor(path_guard=custom_path_guard)

    def test_initialization_defaults_and_env(self, executor):
        assert executor.cpus == "2"
        assert executor.memory == "2g"
        assert executor.pids_limit == 256
        assert executor.tmpfs_size == "256m"
        assert executor.security_opt == "no-new-privileges"

    def test_path_guard_blocks_protected_workspace_mount(self, executor, tmp_path):
        protected_dir = tmp_path / "protected_trading" / "subfolder"
        protected_dir.mkdir(parents=True)

        with pytest.raises(ProtectedPathViolationError):
            executor.run_isolated(
                workspace_path=protected_dir,
                command=["python3", "--version"],
            )

    def test_path_guard_blocks_protected_extra_mount(self, executor, tmp_path):
        valid_dir = tmp_path / "valid_workspace"
        valid_dir.mkdir()
        protected_file = tmp_path / "protected_trading" / "keys.json"
        protected_file.touch()

        with pytest.raises(ProtectedPathViolationError):
            executor.run_isolated(
                workspace_path=valid_dir,
                command=["python3", "--version"],
                extra_mounts={str(protected_file): "/keys.json"},
            )

    def test_nonexistent_workspace_raises_file_not_found(self, executor, tmp_path):
        nonexistent = tmp_path / "does_not_exist_12345"
        with pytest.raises(FileNotFoundError):
            executor.run_isolated(
                workspace_path=nonexistent,
                command=["ls"],
            )

    def test_empty_command_raises_value_error(self, executor, tmp_path):
        valid_dir = tmp_path / "valid_workspace"
        valid_dir.mkdir()
        with pytest.raises(ValueError):
            executor.run_isolated(workspace_path=valid_dir, command=[])

    def test_run_isolated_constructs_docker_cmd_with_rm_and_quotas(self, executor, tmp_path):
        valid_dir = tmp_path / "valid_workspace"
        valid_dir.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="Python 3.11.9\n", stderr=""
            )

            res = executor.run_isolated(
                workspace_path=valid_dir,
                command=["python3", "-c", "print(1)"],
                env_vars={"FOO": "BAR"},
            )

            assert mock_run.call_count >= 1
            # First call is docker run, second in finally is docker rm -f
            docker_run_call = mock_run.call_args_list[0][0][0]
            assert "run" in docker_run_call
            assert "--rm" in docker_run_call
            assert "--cpus=2" in docker_run_call
            assert "--memory=2g" in docker_run_call
            assert "--pids-limit=256" in docker_run_call
            assert "--security-opt=no-new-privileges" in docker_run_call
            assert "--tmpfs=/tmp:rw,size=256m" in docker_run_call
            assert "-e" in docker_run_call
            assert "FOO=BAR" in docker_run_call
            assert res.success is True
            assert res.stdout == "Python 3.11.9\n"

    def test_run_isolated_timeout_handling(self, executor, tmp_path):
        valid_dir = tmp_path / "valid_workspace"
        valid_dir.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.TimeoutExpired(cmd="docker", timeout=5),
                subprocess.CompletedProcess(args=[], returncode=0),  # cleanup call
            ]

            res = executor.run_isolated(
                workspace_path=valid_dir,
                command=["sleep", "10"],
                timeout_sec=5,
            )

            assert res.timed_out is True
            assert res.success is False
            assert res.exit_code == -1
            assert "timed out after 5 seconds" in res.stderr

    def test_cleanup_stale_containers(self, executor):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="bounty-exec-1\nbounty-exec-2\n", stderr=""
                ),
                subprocess.CompletedProcess(args=[], returncode=0),  # rm -f bounty-exec-1
                subprocess.CompletedProcess(args=[], returncode=0),  # rm -f bounty-exec-2
            ]

            cleaned = executor.cleanup_stale_containers("bounty-exec-")
            assert cleaned == 2
