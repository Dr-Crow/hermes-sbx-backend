"""Hermes terminal backend for Docker Sandboxes (``sbx``).

Every Hermes ``terminal`` / ``execute_code`` / file-tool call runs inside an sbx microVM (own kernel, own Docker engine,
host-side network policy and credential injection). The sandbox is created on first use, named after the Hermes task,
restarted by sbx if it idled out, and re-created if it was removed. Select it with ``terminal.backend: sbx``.

Settings (``terminal.sbx`` in config.yaml, each overridable by the env var in brackets):
  agent [HERMES_SBX_AGENT]         sbx agent or kit for ``sbx create`` (default: shell)
  workspace_mode [HERMES_SBX_WORKSPACE_MODE] mount (default) or clone: the agent works on a private git clone, host repo read-only
  workspace [HERMES_SBX_WORKSPACE] host directory bind-mounted read-write at the same path (default: none, the agent
                                   works in the sandbox's own filesystem)
  cpus / memory / template / pull / kits / static_mcp / command   see README
No credential is forwarded into the sandbox; sbx injects provider credentials on the host side.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from agent.terminal_env_provider import TerminalEnvironmentProvider
from tools.environments.base import BaseEnvironment
from tools.environments.base_output import _popen_bash
from tools.environments.remote_common import bash_argv

from ._common import (  # noqa: F401  (re-exported: the tests and users import them from the plugin module)
    CREATE_TIMEOUT_S, PROMPT_PROBE_TASK_ID, VERIFY_TTL_S, PromptProbeEnvironment, SbxSandbox, _load_settings,
    build_create_args, find_sbx, sandbox_name, transport_of, truthy)

logger = logging.getLogger(__name__)


class SbxEnvironment(SbxSandbox, BaseEnvironment):
    """One sbx sandbox per Hermes task; spawn-per-call ``sbx exec`` (like the Docker and Singularity backends)."""

    def __init__(self, cwd: str, timeout: int, task_id: str = "default", persistent: bool = True,
                 settings: Optional[Dict[str, Any]] = None):
        self._init_sandbox(cwd=cwd, task_id=task_id, persistent=persistent, settings=settings)
        # With a bind mount the guest sees the host path; without one the agent lives in the sandbox's own filesystem.
        super().__init__(cwd=self._workspace or "~", timeout=timeout)

        self._ensure_sandbox(force=True)
        self.init_session()

    # -- BaseEnvironment ---------------------------------------------------------------
    def execute(self, command: str, cwd: str = "", **kwargs: Any) -> dict:
        # "" falls back to self.cwd: the mounted workspace, or the sandbox home, then wherever `cd` led
        return super().execute(command, self._map_cwd(cwd), **kwargs)

    def _before_execute(self) -> None:
        if self._ensure_sandbox():
            # The sandbox was removed behind our back: the session snapshot lived inside it.
            self._mark_recreated()
            self.init_session()

    def _run_bash(self, cmd_string: str, *, login: bool = False, timeout: int = 120,
                  stdin_data: str | None = None) -> subprocess.Popen:
        cmd = [self._sbx, "exec"]
        if stdin_data is not None:
            cmd.append("-i")
        cmd += [self.name, *bash_argv(cmd_string, login)]
        return _popen_bash(cmd, stdin_data)

    def cleanup(self, force_remove: bool = False) -> None:
        if self._persistent and not force_remove:
            return  # sbx stops idle sandboxes itself; the next session with this task id resumes it
        self._remove_sandbox()


def transport_of_safe() -> str:
    try:
        return transport_of(_load_settings())
    except ValueError:
        return "exec"


class SbxProvider(TerminalEnvironmentProvider):
    name = "sbx"
    display_name = "Docker Sandboxes (sbx)"
    is_remote = True

    @property
    def is_container(self) -> bool:
        # Hermes assumes a container cannot see host paths: it discards a host-looking terminal.cwd (anything under
        # /home, /Users, ...) for /root and resolves relative file-tool paths against /root, which the sandbox user
        # cannot write. With a workspace bind-mounted at its host path that assumption is false, so the backend
        # presents as a plain remote machine and terminal.cwd may be the workspace. Without a workspace the sandbox
        # is a closed container and the default applies.
        return not _load_settings().get("workspace")
    # A sandbox is resumed by name, so two ephemeral sessions must not share one.
    session_isolated_when_nonpersistent = True

    @property
    def description(self) -> str:
        return "Run commands in an sbx microVM (own kernel and Docker engine, host-side network policy)."

    @property
    def skip_container_guards(self) -> bool:
        # A bind-mounted workspace is real host data, so the dangerous-command approval layer stays ON unless the
        # operator opts out with terminal.sbx.skip_approvals: true (HERMES_SBX_SKIP_APPROVALS=1).
        env = os.environ.get("HERMES_SBX_SKIP_APPROVALS")
        if env is not None:
            return env.strip().lower() in {"1", "true", "yes"}
        return bool(_load_settings().get("skip_approvals", False))

    @property
    def cache_path_base(self) -> Optional[str]:
        # The ssh transport syncs ~/.hermes/{skills,credentials,cache} into the sandbox, like Hermes' ssh backend;
        # with the exec transport nothing is synced and host cache paths do not exist inside.
        settings = _load_settings()
        if transport_of(settings) == "ssh" and truthy(settings.get("sync_files", True)):
            return "~/.hermes"
        return None

    @property
    def env_description(self) -> str:
        return "a Docker Sandboxes (sbx) microVM (Linux)"

    def is_available(self) -> bool:
        return find_sbx(_load_settings().get("command")) is not None

    def _ssh_problem(self) -> Optional[str]:
        """Why the ssh transport cannot work right now (None: fine or not selected). One local ``ssh -G``, no network."""
        try:
            if transport_of(_load_settings()) != "ssh":
                return None
        except ValueError as exc:
            return str(exc)
        from .ssh_env import ssh_integration_error  # lazy: the exec transport must not depend on Hermes' SSH module

        return ssh_integration_error()

    def check_requirements(self, config: Dict[str, Any]) -> bool:
        if not self.is_available():
            logger.error("sbx backend selected but the sbx CLI was not found on PATH")
            return False
        problem = self._ssh_problem()
        if problem:
            logger.error("sbx backend (transport ssh): %s", problem)
        return problem is None

    def probe(self) -> Tuple[str, str]:
        if not self.is_available():
            return ("needs_setup", "Install the sbx CLI and run `sbx login`.")
        problem = self._ssh_problem()
        return ("needs_setup", problem) if problem else ("ready", "")

    def doctor_checks(self) -> List[Tuple[bool, str, str]]:
        rows = [(self.is_available(), self.display_name + " backend", "sbx CLI on PATH")]
        problem = self._ssh_problem()
        if problem or transport_of_safe() == "ssh":
            rows.append((problem is None, "sbx SSH integration", problem or "ssh <name>.sbx routed through sbx"))
        return rows

    def setup_instructions(self) -> List[str]:
        return [
            "Install sbx: https://docs.docker.com/ai/sandboxes/install/ then run `sbx login`.",
            "Optional: set terminal.sbx.workspace (host directory mounted at the same path in the sandbox).",
            "Optional: terminal.sbx.transport: ssh runs commands over SSH instead of `sbx exec`; run `sbx setup ssh` once.",
        ]

    def create_environment(self, *, cwd: str, timeout: int, task_id: str = "default", image: Optional[str] = None,
                           container_config: Optional[Dict[str, Any]] = None, **kwargs: Any):
        if task_id == PROMPT_PROBE_TASK_ID:
            return PromptProbeEnvironment()
        persistent = bool((container_config or {}).get("container_persistent", True))
        settings = _load_settings()
        if transport_of(settings) == "ssh":
            from .ssh_env import SbxSshEnvironment  # lazy, see _ssh_problem

            return SbxSshEnvironment(cwd=cwd, timeout=timeout, task_id=task_id, persistent=persistent,
                                     settings=settings)
        return SbxEnvironment(cwd=cwd, timeout=timeout, task_id=task_id, persistent=persistent, settings=settings)


def register(ctx) -> None:
    ctx.register_terminal_environment_provider(SbxProvider())
