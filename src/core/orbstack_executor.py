"""
Ephemeral OrbStack Docker Container Runner & Ephemeral Lifecycle Manager.

Provides isolated, single-use container execution using Docker CLI / OrbStack socket.
Enforces strict resource quotas (--cpus, --memory, --pids-limit, --security-opt, --tmpfs),
mandatory --rm flag, PathGuard IGNORE_LIST volume mount protection, and guaranteed instant
container destruction in finally blocks.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from src.core.config import (
    DEFAULT_CONTAINER_CPUS,
    DEFAULT_CONTAINER_MEMORY,
    DEFAULT_CONTAINER_PIDS_LIMIT,
    DEFAULT_CONTAINER_TIMEOUT_SEC,
    DEFAULT_DOCKER_IMAGE,
)
from src.core.path_guard import DEFAULT_PATH_GUARD, PathGuard

logger = logging.getLogger("UniversalBountyV2.OrbStackExecutor")


class ContainerExecutionResult:
    """
    Structured result of an ephemeral container execution.
    """

    def __init__(
        self,
        container_name: str,
        exit_code: int,
        stdout: str,
        stderr: str,
        duration_sec: float,
        timed_out: bool = False,
    ) -> None:
        self.container_name = container_name
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.duration_sec = duration_sec
        self.timed_out = timed_out
        self.success = (exit_code == 0) and not timed_out

    def to_dict(self) -> dict[str, Any]:
        """Converts execution result to a serializable dictionary."""
        return {
            "container_name": self.container_name,
            "exit_code": self.exit_code,
            "success": self.success,
            "timed_out": self.timed_out,
            "duration_sec": round(self.duration_sec, 4),
            "stdout": self.stdout,
            "stderr": self.stderr,
        }

    def __repr__(self) -> str:
        return (
            f"<ContainerExecutionResult name={self.container_name} "
            f"exit_code={self.exit_code} success={self.success} "
            f"timed_out={self.timed_out} duration={self.duration_sec:.2f}s>"
        )


class EphemeralOrbStackExecutor:
    """
    Manages isolated, single-use container execution via Docker / OrbStack.
    Enforces resource bounds (--cpus=2, --memory=2g, --pids-limit=256, --tmpfs=/tmp:rw,size=256m,
    --security-opt=no-new-privileges, --rm), validates mounted paths against IGNORE_LIST
    via PathGuard, and guarantees container destruction in a finally block (docker rm -f).
    """

    DEFAULT_ORBSTACK_SOCKET = Path.home() / ".orbstack" / "run" / "docker.sock"

    def __init__(
        self,
        default_image: str = DEFAULT_DOCKER_IMAGE,
        default_timeout_sec: int = DEFAULT_CONTAINER_TIMEOUT_SEC,
        cpus: str = DEFAULT_CONTAINER_CPUS,
        memory: str = DEFAULT_CONTAINER_MEMORY,
        pids_limit: int = DEFAULT_CONTAINER_PIDS_LIMIT,
        tmpfs_size: str = "256m",
        security_opt: str = "no-new-privileges",
        path_guard: PathGuard | None = None,
        docker_bin: str | None = None,
        docker_host: str | None = None,
    ) -> None:
        self.default_image = default_image
        self.default_timeout_sec = default_timeout_sec
        self.cpus = str(cpus)
        self.memory = str(memory)
        self.pids_limit = int(pids_limit)
        self.tmpfs_size = str(tmpfs_size)
        self.security_opt = str(security_opt)
        self.path_guard = path_guard or DEFAULT_PATH_GUARD

        # Locate docker CLI binary
        self.docker_bin = docker_bin or shutil.which("docker") or "docker"

        # Resolve docker host / socket
        if docker_host:
            self.docker_host = docker_host
        elif "DOCKER_HOST" in os.environ:
            self.docker_host = os.environ["DOCKER_HOST"]
        elif self.DEFAULT_ORBSTACK_SOCKET.exists():
            self.docker_host = f"unix://{self.DEFAULT_ORBSTACK_SOCKET}"
        else:
            self.docker_host = None

    def _get_subprocess_env(self) -> dict[str, str]:
        """Builds environment dictionary with DOCKER_HOST if configured."""
        env = dict(os.environ)
        if self.docker_host and "DOCKER_HOST" not in env:
            env["DOCKER_HOST"] = self.docker_host
        return env

    def is_docker_available(self) -> bool:
        """
        Verifies whether Docker daemon / OrbStack is accessible and responsive.
        """
        try:
            res = subprocess.run(
                [self.docker_bin, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                timeout=5,
                env=self._get_subprocess_env(),
            )
            return res.returncode == 0
        except Exception:
            return False

    def cleanup_container(self, container_name: str) -> bool:
        """
        Force-removes a container by name. Guarantees 0 lingering containers.
        """
        if not container_name:
            return True
        try:
            res = subprocess.run(
                [self.docker_bin, "rm", "-f", container_name],
                capture_output=True,
                text=True,
                timeout=10,
                env=self._get_subprocess_env(),
            )
            return res.returncode == 0
        except Exception:
            return False

    def cleanup_stale_containers(self, prefix: str = "bounty-exec-") -> int:
        """
        Scans for and removes all lingering containers matching prefix.
        Returns the count of cleaned containers.
        """
        try:
            res = subprocess.run(
                [
                    self.docker_bin,
                    "ps",
                    "-a",
                    "--filter",
                    f"name={prefix}",
                    "--format",
                    "{{.Names}}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                env=self._get_subprocess_env(),
            )
            if res.returncode != 0 or not res.stdout.strip():
                return 0

            names = [n.strip() for n in res.stdout.strip().splitlines() if n.strip()]
            cleaned_count = 0
            for name in names:
                if self.cleanup_container(name):
                    cleaned_count += 1
            return cleaned_count
        except Exception:
            return 0

    def run_isolated(
        self,
        workspace_path: str | Path,
        command: list[str],
        image: str | None = None,
        env_vars: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        read_only: bool = False,
        network_mode: str = "bridge",
        extra_mounts: dict[str, str] | None = None,
        working_dir: str = "/workspace",
        extra_flags: list[str] | None = None,
    ) -> ContainerExecutionResult:
        """
        Executes a command inside an ephemeral OrbStack Docker container.

        1. Validates workspace_path (and any extra_mounts) against IGNORE_LIST using PathGuard.
        2. Spins up single-use container with mandatory --rm and strict quotas.
        3. Enforces timeout.
        4. Guarantees container destruction in a finally block (docker rm -f).
        """
        if not command:
            raise ValueError("Command list must not be empty.")

        # 1. Structural PathGuard Validation Gate
        validated_workspace = self.path_guard.validate_access(
            workspace_path, operation="docker_volume_mount"
        )
        if not validated_workspace.is_dir():
            raise FileNotFoundError(
                f"Workspace directory does not exist or is not a directory: {validated_workspace}"
            )

        # Validate any extra mounts against PathGuard
        validated_extra_mounts: list[str] = []
        if extra_mounts:
            for host_p, cont_p in extra_mounts.items():
                val_host = self.path_guard.validate_access(
                    host_p, operation="docker_volume_mount_extra"
                )
                validated_extra_mounts.extend(["-v", f"{val_host!s}:{cont_p}"])

        image_to_use = image or self.default_image
        timeout = timeout_sec or self.default_timeout_sec
        container_name = f"bounty-exec-{uuid.uuid4().hex[:12]}"

        # 2. Build Docker CLI Command
        mount_mode = "ro" if read_only else "rw"
        env_args: list[str] = []
        if env_vars:
            for k, v in env_vars.items():
                env_args.extend(["-e", f"{k}={v}"])

        docker_cmd = [
            self.docker_bin,
            "run",
            "--rm",  # MANDATORY --rm flag for ephemeral container destruction
            "--name",
            container_name,
            f"--cpus={self.cpus}",
            f"--memory={self.memory}",
            f"--pids-limit={self.pids_limit}",
            f"--security-opt={self.security_opt}",
            f"--tmpfs=/tmp:rw,size={self.tmpfs_size}",
            "--network",
            network_mode,
            "-v",
            f"{validated_workspace!s}:{working_dir}:{mount_mode}",
            "-w",
            working_dir,
            *validated_extra_mounts,
            *env_args,
        ]

        if extra_flags:
            docker_cmd.extend(extra_flags)

        docker_cmd.append(image_to_use)
        docker_cmd.extend(command)

        # 3. Execution & Guaranteed Cleanup
        start_time = time.perf_counter()
        stdout, stderr = "", ""
        exit_code = -1
        timed_out = False

        try:
            res = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._get_subprocess_env(),
            )
            stdout = res.stdout
            stderr = res.stderr
            exit_code = res.returncode
        except subprocess.TimeoutExpired as e:
            timed_out = True
            exit_code = -1
            stdout = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            if not stderr:
                stderr = f"Container execution timed out after {timeout} seconds."
            logger.warning(f"Container {container_name} timed out after {timeout}s")
        except Exception as e:
            exit_code = -1
            stderr = f"Container execution error: {e!s}"
            logger.error(f"Error executing container {container_name}: {e}")
        finally:
            # 4. INSTANT DESTRUCTION GUARANTEE (0 Lingering Containers)
            self.cleanup_container(container_name)
            duration_sec = time.perf_counter() - start_time

        return ContainerExecutionResult(
            container_name=container_name,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_sec=duration_sec,
            timed_out=timed_out,
        )
