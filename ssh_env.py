"""``terminal.sbx.transport: ssh``: sbx lifecycle through the CLI, command execution over SSH.

Modelled on how OpenClaw's OpenShell plugin works (vendor CLI for ``create``/``get``/``delete``, a stock SSH transport
for everything else) and built on Hermes' own ``SSHEnvironment``, so command execution, the session snapshot,
ControlMaster reuse, the ``terminal.env_passthrough`` forwarding and the ``~/.hermes`` file sync are Hermes' code, not ours.

What this class adds to ``SSHEnvironment``:
  * the lifecycle: ``sbx create`` on first use, again if the sandbox was removed, ``sbx rm`` on non-persistent cleanup;
  * the target: ``<sandbox-name>.sbx``, resolved by the ``Host *.sbx`` block that ``sbx setup ssh`` writes (ProxyCommand
    ``sbx ssh proxy %n``, no key, no sshd), so there is no host, port or key to configure;
  * recovery when sbx's SSH endpoint refuses a command because the sandbox behind a multiplexed connection is gone;
  * a safer default for the sandbox-to-host direction of the file sync (see ``sync_back``).
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any, Dict, Optional

from tools.environments.remote_common import run_capture
from tools.environments.ssh import SSHEnvironment

from ._common import SbxSandbox, truthy

logger = logging.getLogger(__name__)

# `sbx setup ssh` writes `User _default_user_`: the placeholder sbx maps to the sandbox's own default user.
# Hermes always sends `<user>@<host>`, and that overrides the config's User, so the placeholder has to be passed too.
DEFAULT_SSH_USER = "_default_user_"
SSH_HOST_SUFFIX = ".sbx"
# What sbx's SSH endpoint answers (exit 1, before any command is started) when the sandbox behind the connection was
# removed, or was re-created under the same name while a multiplexed connection to the old one was still open.
GONE_MARKERS = (
    "sandbox identity changed",
    "does not exist and auto-create is off",
    "is unavailable",
)
_REJECT_OUTPUT_MAX = 400


def ssh_integration_error(ssh: str = "ssh") -> Optional[str]:
    """None when ``ssh <name>.sbx`` is routed through ``sbx ssh proxy`` (``sbx setup ssh`` was run), else why not.
    ``ssh -G`` only prints the effective client config: no connection, no sandbox needed."""
    try:
        r = run_capture([ssh, "-G", f"probe{SSH_HOST_SUFFIX}"], timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"cannot run {ssh}: {exc}"
    if r.returncode != 0:
        return f"ssh -G failed: {(r.stderr or r.stdout).strip()[:200]}"
    if "ssh proxy" not in r.stdout.lower():
        return ("`ssh <name>.sbx` is not routed through sbx: run `sbx setup ssh` once "
                "(it adds an Include to ~/.ssh/config)")
    return None


class SbxSshEnvironment(SbxSandbox, SSHEnvironment):
    """One sbx sandbox per Hermes task, driven over the stock sbx SSH integration."""

    def __init__(self, cwd: str, timeout: int, task_id: str = "default", persistent: bool = True,
                 settings: Optional[Dict[str, Any]] = None):
        self._init_sandbox(cwd=cwd, task_id=task_id, persistent=persistent, settings=settings)
        problem = ssh_integration_error()
        if problem:
            raise RuntimeError(problem)
        # sync_files: push skills, credential files and cache files into the sandbox's ~/.hermes, as Hermes' ssh
        # backend does. sync_back (default off): pull sandbox-side changes to those files back to the host on cleanup.
        # Hermes' ssh backend always does; for a sandbox that is the one place sandbox content is written into the
        # host's skills directory, so it is an explicit opt-in here.
        self._sync_back = truthy(self._settings.get("sync_back", False))
        self._ssh_kwargs = {
            "host": f"{self.name}{SSH_HOST_SUFFIX}",
            "user": str(self._settings.get("ssh_user") or DEFAULT_SSH_USER),
            "cwd": self._workspace or "~",  # the mounted workspace is at the same path inside; else the sandbox home
            "timeout": timeout,
            "sync_files": truthy(self._settings.get("sync_files", True)),
        }
        self._ensure_sandbox(force=True)
        SSHEnvironment.__init__(self, **self._ssh_kwargs)

    # -- ssh command line ------------------------------------------------------------
    def _build_ssh_command(self, extra_args: list | None = None, send_env=()) -> list:
        cmd = super()._build_ssh_command(extra_args, send_env)
        # Hermes asks for accept-new; the generated Host *.sbx block says yes, with host keys supplied by
        # `sbx ssh known-hosts`. Keep the integration's stricter choice.
        return ["StrictHostKeyChecking=yes" if a == "StrictHostKeyChecking=accept-new" else a for a in cmd]

    # -- (re)connecting --------------------------------------------------------------
    def _close_masters(self) -> None:
        """Close every ControlMaster for this target. A master stays bound to the sandbox instance it was opened to."""
        for sock in self._control_sockets():
            if not sock.exists():
                continue
            try:
                run_capture(["ssh", "-o", f"ControlPath={sock}", "-O", "exit", f"{self.user}@{self.host}"], timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                sock.unlink()
            except OSError:
                pass

    def _reattach(self) -> bool:
        """Drop stale connections, make sure the sandbox exists, and run the SSH environment's setup again
        (connection, remote ~/.hermes, file sync, session snapshot). True when the sandbox had to be created."""
        self._close_masters()
        created = self._ensure_sandbox(force=True)
        SSHEnvironment.__init__(self, **self._ssh_kwargs)
        if created:
            self._mark_recreated()
        return created

    # -- BaseEnvironment -------------------------------------------------------------
    def _before_execute(self) -> None:
        if self._ensure_sandbox():  # not verified within VERIFY_TTL_S: is it still there?
            self._close_masters()
            SSHEnvironment.__init__(self, **self._ssh_kwargs)
            self._mark_recreated()
            return
        super()._before_execute()  # Hermes' rate-limited ~/.hermes file sync

    @staticmethod
    def _rejected_by_sbx(result: dict) -> bool:
        out = (result.get("output") or "").strip()
        return (result.get("returncode") not in (0, None) and len(out) <= _REJECT_OUTPUT_MAX
                and any(m in out for m in GONE_MARKERS))

    def execute(self, command: str, cwd: str = "", **kwargs: Any) -> dict:
        cwd = self._map_cwd(cwd)
        result = super().execute(command, cwd, **kwargs)
        if self._rejected_by_sbx(result):
            # sbx refused before starting anything, so running it once more cannot repeat a side effect.
            logger.info("sbx ssh endpoint refused the command (%s); re-attaching to %s",
                        (result.get("output") or "").strip()[:80], self.name)
            self._reattach()
            result = super().execute(command, cwd, **kwargs)
        return result

    def cleanup(self, force_remove: bool = False) -> None:
        try:
            # sync_back runs three attempts with backoff against a dead target, so only ask it when it can work.
            if not self._sync_back or not self._exists():
                self._sync_manager = None
            super().cleanup()  # sync_back if enabled, then close the ControlMasters
        finally:
            self._sync_manager = None  # Hermes calls cleanup() again from __del__; do not sync back twice
            if not self._persistent or force_remove:
                self._remove_sandbox()
