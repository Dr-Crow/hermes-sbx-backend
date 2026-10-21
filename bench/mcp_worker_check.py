"""Hermes holds the gateway token (MCP client, from HERMES_HOME/.env); the sbx worker it creates must not.

Builds a throwaway HERMES_HOME next to the real one: the MCP-gateway config (token only in a 0600 .env, never in
config.yaml), `terminal.backend: sbx` (transport exec or ssh) with a workspace, and the plugin. In ONE process it
(1) loads the .env and calls an MCP tool through the gateway, then (2) runs commands in the sandbox and looks for the
token. The token value never enters the sandbox: we search for the shared `mcp_agt_` prefix and for the variable name.
Prints no secret.

  HERMES_SRC=<hermes-agent> PYTHONPATH=$HERMES_SRC <hermes-mcp-python> bench/mcp_worker_check.py exec|ssh SRC_HOME WORKSPACE
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

transport, src_home, workspace = sys.argv[1], Path(sys.argv[2]), os.path.abspath(sys.argv[3])
ROOT = Path(__file__).resolve().parent.parent
home = Path(os.path.expanduser(f"~/hh-mcpw-{transport}"))
shutil.rmtree(home, ignore_errors=True)
(home / "plugins").mkdir(parents=True)
(home / "plugins" / "sbx").symlink_to(ROOT)
os.makedirs(workspace, exist_ok=True)
cfg = (src_home / "config.yaml").read_text().replace("  backend: local\n", "")
cfg += (f"  backend: sbx\n  cwd: {workspace}\n  sbx:\n    workspace: {workspace}\n    transport: {transport}\n"
        "plugins:\n  enabled:\n    - sbx\n  disabled: []\n")
(home / "config.yaml").write_text(cfg)
old = os.umask(0o077)
shutil.copyfile(src_home / ".env", home / ".env")
os.umask(old)
os.chmod(home / ".env", 0o600)
os.environ["HERMES_HOME"] = str(home)
os.environ["OTEL_SDK_DISABLED"] = "true"
for k in [k for k in os.environ if k.startswith(("TERMINAL_", "HERMES_SBX_"))]:
    del os.environ[k]

from hermes_cli.env_loader import load_hermes_dotenv  # noqa: E402

load_hermes_dotenv(hermes_home=home)
token = os.environ.get("GW_TOKEN", "")
rep = {"transport": transport, "hermes_process_has_token": bool(token),
       "token_in_config_yaml": bool(token) and token in (home / "config.yaml").read_text(),
       "env_file_mode": oct((home / ".env").stat().st_mode & 0o777)}

from hermes_cli.plugins import discover_plugins  # noqa: E402

discover_plugins()
from tools import mcp_tool_discovery  # noqa: E402
from tools.registry import registry  # noqa: E402

names = mcp_tool_discovery.discover_mcp_tools()
out = registry.dispatch("mcp__gw__everything__echo", {"message": f"from-hermes-with-sbx-{transport}"})
rep["mcp_echo_through_gateway"] = str(out).replace(token, "<token>")[:120] if token else str(out)[:120]
rep["mcp_tool_count"] = len(names)

import tools.terminal_tool as tt  # noqa: E402
from tools.terminal_tool_lifecycle import cleanup_vm  # noqa: E402

TASK = f"mcpw-{transport}-{os.getpid()}"


def T(cmd):
    r = json.loads(tt.terminal_tool(cmd, task_id=TASK))
    return (r.get("output") or r.get("error") or "").strip()


t0 = time.time()
rep["worker"] = T("hostname; id -un")
rep["worker_first_command_s"] = round(time.time() - t0, 1)
rep["env_has_mcp_agt_prefix"] = T("env | grep -c 'mcp_agt_' || true")
rep["env_has_GW_TOKEN_or_Authorization"] = T("env | grep -ciE '^(GW_TOKEN|AUTHORIZATION)=' || true")
rep["files_with_mcp_agt_prefix(home,tmp,etc,workspace)"] = T(
    f"grep -rIl 'mcp_agt_' /home/agent /tmp /etc '{workspace}' 2>/dev/null | wc -l")
rep["any_dotenv_in_home_or_workspace"] = T(f"find /home/agent '{workspace}' -maxdepth 3 -name '.env*' 2>/dev/null | wc -l")
rep["proc_environ_with_prefix"] = T("cat /proc/[0-9]*/environ 2>/dev/null | tr '\\0' '\\n' | grep -c 'mcp_agt_' || true")
rep["host_workspace_has_token"] = subprocess.run(
    ["grep", "-rIl", "mcp_agt_", workspace], capture_output=True, text=True).stdout.strip() or "no"
print(json.dumps(rep, indent=1))
tt_cleanup = tt._resolve_container_task_id(TASK)
cleanup_vm(tt_cleanup)
from tools.mcp_tool_lifecycle import shutdown_mcp_servers  # noqa: E402

shutdown_mcp_servers()
