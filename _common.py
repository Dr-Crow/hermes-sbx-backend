"""Settings, naming and sbx CLI lifecycle shared by both transports (``exec`` and ``ssh``).

Everything that talks to the ``sbx`` CLI about a sandbox's existence lives here: ``sbx create``, ``sbx ls`` and
``sbx rm``. How a command then reaches the sandbox (``sbx exec`` or SSH) is the transport's business.
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
from typing import Any, Dict, List, Optional

from tools.environments.remote_common import run_capture

logger = logging.getLogger(__name__)

VERIFY_TTL_S = 10.0
CREATE_TIMEOUT_S = 300
# The task id Hermes' system-prompt builder uses for its one-off "what OS is this backend" environment.
PROMPT_PROBE_TASK_ID = "prompt-backend-probe"
TRANSPORTS = ("exec", "ssh")

_SETTING_ENV = {
    "command": "HERMES_SBX_COMMAND",
    "agent": "HERMES_SBX_AGENT",
    "workspace": "HERMES_SBX_WORKSPACE",
    "workspace_mode": "HERMES_SBX_WORKSPACE_MODE",
    "template": "HERMES_SBX_TEMPLATE",
    "pull": "HERMES_SBX_PULL",
    "cpus": "HERMES_SBX_CPUS",
    "memory": "HERMES_SBX_MEMORY",
    "transport": "HERMES_SBX_TRANSPORT",
    "ssh_user": "HERMES_SBX_SSH_USER",
}
_PULL = {"always", "missing", "never"}


def truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


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


def transport_of(settings: Dict[str, Any]) -> str:
    transport = str(settings.get("transport") or "exec").strip().lower()
    if transport not in TRANSPORTS:
        raise ValueError(f"sbx transport must be one of {list(TRANSPORTS)}, got {transport!r}")
    return transport


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


class SbxSandbox:
    """Mixin: one sbx sandbox per Hermes task, created on first use, re-created if removed, removed on request.

    Hosts must call :meth:`_init_sandbox` before anything else. It makes no sbx call; the first one is
    :meth:`_ensure_sandbox`.
    """

    def _init_sandbox(self, *, cwd: str, task_id: str, persistent: bool, settings: Optional[Dict[str, Any]]) -> None:
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

    def _map_cwd(self, cwd: str) -> str:
        return "" if cwd and cwd == self._requested_cwd else cwd

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

    def _remove_sandbox(self) -> None:
        try:
            self._sbx_run(["rm", "--force", self.name], timeout=60)
        except Exception as exc:
            logger.warning("sbx rm %s failed: %s", self.name, exc)
