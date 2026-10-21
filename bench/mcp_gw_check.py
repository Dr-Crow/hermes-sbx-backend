"""Prove a Hermes MCP tool call end to end through the shared MCP gateway, without a model.

Uses Hermes' own MCP client path: config.yaml `mcp_servers` (with `Authorization: Bearer ${GW_TOKEN}`), the HERMES_HOME/.env
loader, `discover_mcp_tools()` and the tool registry's `dispatch()`. The token is only ever read from HERMES_HOME/.env;
this script prints a masked form and never the value.

  HERMES_HOME=~/hh-mcp PYTHONPATH=<hermes-agent> <hermes-python> bench/mcp_gw_check.py
"""
import json
import os
import sys
import time
from pathlib import Path

home = Path(os.environ["HERMES_HOME"])
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

from hermes_cli.env_loader import load_hermes_dotenv  # noqa: E402

load_hermes_dotenv(hermes_home=home)
token = os.environ.get("GW_TOKEN", "")
cfg_text = (home / "config.yaml").read_text()
report = {
    "env_file_mode": oct((home / ".env").stat().st_mode & 0o777),
    "token_loaded_from_env_file": bool(token),
    "token_shape": (token[:8] + "..." + str(len(token)) + " chars") if token else None,
    "token_inline_in_config_yaml": bool(token) and token in cfg_text,
}


def mask(text):
    return text.replace(token, "<token>") if token else text


from tools import mcp_tool_discovery  # noqa: E402
from tools.registry import registry  # noqa: E402

t0 = time.time()
names = mcp_tool_discovery.discover_mcp_tools()
report["discover_s"] = round(time.time() - t0, 2)
report["registered_tools"] = sorted(names)

calls = {}
for tool, args in (("mcp__gw__everything__echo", {"message": "hello-from-hermes-0222"}), ("mcp__gw__everything__get_sum", {"a": 2, "b": 40})):
    if tool not in names:
        calls[tool] = "NOT REGISTERED"
        continue
    t0 = time.time()
    out = registry.dispatch(tool, args)
    calls[tool] = {"s": round(time.time() - t0, 2), "result": mask(str(out))[:300]}
report["calls"] = calls
print(json.dumps(report, indent=1))

from tools.mcp_tool_lifecycle import shutdown_mcp_servers  # noqa: E402

shutdown_mcp_servers()
sys.exit(0 if all(isinstance(v, dict) for v in calls.values()) else 1)
