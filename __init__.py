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

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from agent.terminal_env_provider import TerminalEnvironmentProvider
from tools.environments.base import BaseEnvironment
from tools.environments.base_output import _popen_bash
from tools.environments.remote_common import bash_argv, run_capture

logger = logging.getLogger(__name__)

VERIFY_TTL_S = 10.0
CREATE_TIMEOUT_S = 300
_SETTING_ENV = {
    "command": "HERMES_SBX_COMMAND",
    "agent": "HERMES_SBX_AGENT",
    "workspace": "HERMES_SBX_WORKSPACE",
    "workspace_mode": "HERMES_SBX_WORKSPACE_MODE",
    "template": "HERMES_SBX_TEMPLATE",
    "pull": "HERMES_SBX_PULL",
    "cpus": "HERMES_SBX_CPUS",
    "memory": "HERMES_SBX_MEMORY",
}
_PULL = {"always", "missing", "never"}


def _load_settings() -> Dict[str, Any]:
    """terminal.sbx from config.yaml (best effort), overlaid by HERMES_SBX_* env vars."""
    settings: Dict[str, Any] = {}
    try:
        from hermes_cli.config import load_config

        cfg = (load_config() or {}).get("terminal", {}).get("sbx", {}) or {}
        if isinstance(cfg, dict):
            settings.update(cfg)
    except Exception as exc:  # config is optional
        logger.debug("sbx: could not read terminal.sbx from config: %s", exc)
    for key, env in _SETTING_ENV.items():
        if os.environ.get(env):
            settings[key] = os.environ[env]
    return settings


def find_sbx(command: Optional[str] = None) -> Optional[str]:
    return shutil.which(command or "sbx")


def sandbox_name(task_id: str) -> str:
    """Stable, sbx-name-safe name for a Hermes task id."""
    raw = (task_id or "default").strip() or "default"
    safe = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")[:32].strip("-") or "task"
    return f"hermes-{safe}-{hashlib.sha256(raw.encode()).hexdigest()[:8]}"


def build_create_args(*, name: str, settings: Dict[str, Any], workspace: Optional[str]) -> List[str]:
    agent = str(settings.get("agent") or "shell")
    pull = str(settings.get("pull") or "missing")
    if pull not in _PULL:
        raise ValueError(f"sbx pull must be one of {sorted(_PULL)}, got {pull!r}")
    mode = str(settings.get("workspace_mode") or "mount")
    if mode not in ("mount", "clone"):
        raise ValueError(f"sbx workspace_mode must be 'mount' or 'clone', got {mode!r}")
    if mode == "clone" and not workspace:
        raise ValueError("sbx workspace_mode 'clone' needs terminal.sbx.workspace (a git repository)")
    args = ["create", agent]
    if workspace:
        args.append(workspace)
    args += ["--name", name, "--pull", pull, "--no-progress"]
    if mode == "clone":
        # private clone in the VM, host repo read-only; sbx itself rejects a non-git workspace (no silent fallback)
        args.append("--clone")
    if settings.get("template"):
        args += ["--template", str(settings["template"])]
    if settings.get("cpus"):
        args += ["--cpus", str(int(settings["cpus"]))]
    if settings.get("memory"):
        args += ["--memory", str(settings["memory"])]
    for kit in settings.get("kits") or []:
        args += ["--kit", str(kit)]
    mcp = settings.get("static_mcp") or []
    if mcp:
        args += ["--static-mcp", ",".join(str(m) for m in mcp)]
    return args


class SbxEnvironment(BaseEnvironment):
    """One sbx sandbox per Hermes task; spawn-per-call ``sbx exec`` (like the Docker and Singularity backends)."""

    def __init__(self, cwd: str, timeout: int, task_id: str = "default", persistent: bool = True,
                 settings: Optional[Dict[str, Any]] = None):
        self._settings = dict(settings if settings is not None else _load_settings())
        self._sbx = find_sbx(self._settings.get("command"))
        if not self._sbx:
            raise RuntimeError("the sbx CLI was not found on PATH (https://docs.docker.com/ai/sandboxes/install/)")
        self.name = sandbox_name(task_id)
        self._persistent = persistent
        self._lock = threading.Lock()
        self._verified_at = 0.0

        # Explicit opt-in only: Hermes hands plugin backends a container-style cwd (e.g. /root), never the host
        # directory, and guessing a host path to mount would be unsafe.
        workspace = self._settings.get("workspace")
        self._workspace = os.path.abspath(workspace) if workspace else None
        # Hermes replaces host-looking cwds with a container default such as /root and passes it to execute() on the
        # first call; /root does not exist for the sandbox user, so that value means "use my tracked cwd".
        self._requested_cwd = cwd
        # With a bind mount the guest sees the host path; without one the agent lives in the sandbox's own filesystem.
        super().__init__(cwd=self._workspace or "~", timeout=timeout)

        self._ensure_sandbox(force=True)
        self.init_session()

    # -- sbx helpers -----------------------------------------------------------------
    def _sbx_run(self, args: List[str], timeout: float) -> subprocess.CompletedProcess:
        return run_capture([self._sbx, *args], timeout=timeout)

    def _exists(self) -> bool:
        r = self._sbx_run(["ls", "--json"], timeout=60)
        if r.returncode != 0:
            return False
        try:
            data = json.loads(r.stdout or "[]")
        except ValueError:
            return False
        entries = data if isinstance(data, list) else (data or {}).get("sandboxes", [])
        return any(isinstance(e, dict) and e.get("name") == self.name for e in entries)

    def _ensure_sandbox(self, force: bool = False) -> bool:
        """Create the sandbox if absent. Returns True when it had to be created."""
        with self._lock:
            if not force and time.monotonic() - self._verified_at < VERIFY_TTL_S:
                return False
            if self._exists():
                self._verified_at = time.monotonic()
                return False
            if self._workspace:
                os.makedirs(self._workspace, exist_ok=True)  # sbx create refuses a missing path without a TTY
            args = build_create_args(name=self.name, settings=self._settings, workspace=self._workspace)
            r = self._sbx_run(args, timeout=CREATE_TIMEOUT_S)
            if r.returncode != 0 and not self._exists():
                raise RuntimeError(f"sbx create failed: {(r.stderr or r.stdout).strip()}")
            self._verified_at = time.monotonic()
            logger.info("sbx sandbox %s created (workspace=%s)", self.name, self._workspace)
            return True

    # -- BaseEnvironment ---------------------------------------------------------------
    def execute(self, command: str, cwd: str = "", **kwargs: Any) -> dict:
        if cwd and cwd == self._requested_cwd:
            cwd = ""  # fall back to self.cwd: the mounted workspace, or the sandbox home, then wherever `cd` led
        return super().execute(command, cwd, **kwargs)

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

    def cleanup(self) -> None:
        if self._persistent:
            return  # sbx stops idle sandboxes itself; the next session with this task id resumes it
        try:
            self._sbx_run(["rm", "--force", self.name], timeout=60)
        except Exception as exc:
            logger.warning("sbx rm %s failed: %s", self.name, exc)


class SbxProvider(TerminalEnvironmentProvider):
    name = "sbx"
    display_name = "Docker Sandboxes (sbx)"
    is_remote = True
    is_container = True
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
    def env_description(self) -> str:
        return "a Docker Sandboxes (sbx) microVM (Linux)"

    def is_available(self) -> bool:
        return find_sbx(_load_settings().get("command")) is not None

    def probe(self) -> Tuple[str, str]:
        return ("ready", "") if self.is_available() else ("needs_setup", "Install the sbx CLI and run `sbx login`.")

    def setup_instructions(self) -> List[str]:
        return [
            "Install sbx: https://docs.docker.com/ai/sandboxes/install/ then run `sbx login`.",
            "Optional: set terminal.sbx.workspace (host directory mounted at the same path in the sandbox).",
        ]

    def create_environment(self, *, cwd: str, timeout: int, task_id: str = "default", image: Optional[str] = None,
                           container_config: Optional[Dict[str, Any]] = None, **kwargs: Any):
        persistent = bool((container_config or {}).get("container_persistent", True))
        return SbxEnvironment(cwd=cwd, timeout=timeout, task_id=task_id, persistent=persistent)


def register(ctx) -> None:
    ctx.register_terminal_environment_provider(SbxProvider())
