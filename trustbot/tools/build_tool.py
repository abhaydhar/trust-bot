"""
Build Tool -- wraps subprocess calls for npm and dotnet CLI.

Used by the Code Build Agent and Code Test Agent to compile
generated projects and run test suites.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass

from trustbot.config import settings
from trustbot.tools.base import BaseTool

logger = logging.getLogger("trustbot.tools.build")


@dataclass
class CommandResult:
    """Result of a shell command execution."""

    command: str
    return_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def success(self) -> bool:
        return self.return_code == 0 and not self.timed_out


class BuildTool(BaseTool):
    """
    Tool for running build commands (npm, dotnet) with timeout and output capture.
    """

    name = "build"
    description = (
        "Run build and test commands for frontend (npm/node) and backend (dotnet) projects. "
        "Captures stdout/stderr and supports configurable timeouts."
    )

    def __init__(self) -> None:
        super().__init__()
        self._timeout = 300
        self._npm_path: str | None = None
        self._dotnet_path: str | None = None

    async def initialize(self) -> None:
        self._timeout = settings.modernization_build_timeout_seconds
        self._npm_path = shutil.which("npm")
        self._dotnet_path = shutil.which("dotnet")
        if self._npm_path:
            logger.info("BuildTool: npm found at %s", self._npm_path)
        else:
            logger.warning("BuildTool: npm not found on PATH")
        if self._dotnet_path:
            logger.info("BuildTool: dotnet found at %s", self._dotnet_path)
        else:
            logger.warning("BuildTool: dotnet not found on PATH")

    async def shutdown(self) -> None:
        pass

    async def run_command(
        self,
        cmd: list[str],
        cwd: str,
        timeout: int | None = None,
        log_callback=None,
    ) -> CommandResult:
        """Run an arbitrary command with timeout and output capture.

        Args:
            log_callback: Optional ``(msg: str, level: str) -> None`` that
                receives live status lines (``"cmd"``, ``"success"``, ``"error"``).
        """
        effective_timeout = timeout or self._timeout
        cmd_str = " ".join(cmd)
        logger.info("BuildTool running: %s (cwd=%s, timeout=%ds)", cmd_str, cwd, effective_timeout)

        if log_callback:
            log_callback(f"$ {cmd_str}", "cmd")

        import time as _time
        t0 = _time.perf_counter()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=effective_timeout
            )
            elapsed = _time.perf_counter() - t0
            result = CommandResult(
                command=cmd_str,
                return_code=proc.returncode or 0,
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
            )
            if log_callback:
                if result.success:
                    log_callback(f"  OK (rc=0, {elapsed:.1f}s)", "success")
                else:
                    first_err = (result.stderr or result.stdout).strip().splitlines()
                    hint = first_err[0][:120] if first_err else "unknown error"
                    log_callback(f"  FAILED (rc={result.return_code}, {elapsed:.1f}s): {hint}", "error")
        except asyncio.TimeoutError:
            proc.kill()
            result = CommandResult(
                command=cmd_str,
                return_code=-1,
                stdout="",
                stderr=f"Command timed out after {effective_timeout}s",
                timed_out=True,
            )
            if log_callback:
                log_callback(f"  TIMEOUT after {effective_timeout}s", "error")
        except FileNotFoundError as e:
            result = CommandResult(
                command=cmd_str,
                return_code=-1,
                stdout="",
                stderr=f"Command not found: {e}",
            )
            if log_callback:
                log_callback(f"  NOT FOUND: {e}", "error")

        await self._record_call(
            "run_command",
            {"cmd": cmd_str, "cwd": cwd},
            result=f"rc={result.return_code}",
            error="" if result.success else result.stderr[:200],
        )
        return result

    async def npm_install(self, project_dir: str, timeout: int | None = None, log_callback=None) -> CommandResult:
        """Run npm install in the given project directory."""
        npm = self._npm_path or "npm"
        return await self.run_command([npm, "install"], cwd=project_dir, timeout=timeout, log_callback=log_callback)

    async def npm_build(self, project_dir: str, timeout: int | None = None, log_callback=None) -> CommandResult:
        """Run npm run build in the given project directory."""
        npm = self._npm_path or "npm"
        return await self.run_command([npm, "run", "build"], cwd=project_dir, timeout=timeout, log_callback=log_callback)

    async def npm_test(self, project_dir: str, timeout: int | None = None, log_callback=None) -> CommandResult:
        """Run npm test in the given project directory."""
        npm = self._npm_path or "npm"
        return await self.run_command([npm, "test", "--", "--passWithNoTests"], cwd=project_dir, timeout=timeout, log_callback=log_callback)

    async def dotnet_build(self, project_dir: str, timeout: int | None = None, log_callback=None) -> CommandResult:
        """Run dotnet build in the given project directory."""
        dotnet = self._dotnet_path or "dotnet"
        return await self.run_command([dotnet, "build", "--no-restore"], cwd=project_dir, timeout=timeout, log_callback=log_callback)

    async def dotnet_restore(self, project_dir: str, timeout: int | None = None, log_callback=None) -> CommandResult:
        """Run dotnet restore in the given project directory."""
        dotnet = self._dotnet_path or "dotnet"
        return await self.run_command([dotnet, "restore"], cwd=project_dir, timeout=timeout, log_callback=log_callback)

    async def dotnet_test(self, project_dir: str, timeout: int | None = None, log_callback=None) -> CommandResult:
        """Run dotnet test in the given project directory."""
        dotnet = self._dotnet_path or "dotnet"
        return await self.run_command([dotnet, "test", "--no-build"], cwd=project_dir, timeout=timeout, log_callback=log_callback)

    async def run_tests(
        self,
        project_dir: str,
        framework: str = "vitest",
        timeout: int | None = None,
        log_callback=None,
    ) -> CommandResult:
        """Run tests using the specified framework."""
        if framework in ("jest", "vitest"):
            return await self.npm_test(project_dir, timeout=timeout, log_callback=log_callback)
        elif framework in ("xunit", "nunit", "mstest"):
            return await self.dotnet_test(project_dir, timeout=timeout, log_callback=log_callback)
        else:
            return CommandResult(
                command=f"unknown framework: {framework}",
                return_code=-1,
                stdout="",
                stderr=f"Unsupported test framework: {framework}",
            )
