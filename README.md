# hermes-sbx-backend

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) terminal-environment plugin that runs the agent's
`terminal`, `execute_code` and file tools inside [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) (`sbx`)
microVMs: a separate kernel and Docker engine per sandbox, so the agent can run `docker` itself with no host socket.

It is a pure plugin (`kind: backend`, one Python file, no dependencies, no Hermes core changes) built on Hermes' documented
`TerminalEnvironmentProvider` contract.

## Install

```sh
sbx login                                   # once
hermes plugins install <owner>/hermes-sbx-backend --enable
hermes config set terminal.backend sbx
hermes config set terminal.sbx.workspace /path/to/project   # optional host dir, mounted read-write at the same path
```

## Behaviour

- One sandbox per Hermes task id, named `hermes-<task>-<hash>`, created on first use (`sbx create shell ...`, ~4 s cold,
  ~0.25 s per command warm), resumed by sbx if it idled out, re-created (with Hermes' `environment_recreated` notice) if removed.
- `terminal.container_persistent: false` removes the sandbox on cleanup; `true` leaves it for the next session.
- Shell state (cwd, exported variables) persists between calls through Hermes' normal session snapshot.
- No credential is forwarded into the sandbox. Provider credentials stay on the host and are injected by sbx's proxy.
- Dangerous-command approval stays ON by default because a mounted workspace is real host data. Opt out with
  `terminal.sbx.skip_approvals: true` or `HERMES_SBX_SKIP_APPROVALS=1`.

## Settings (`terminal.sbx.*` in config.yaml; env override in brackets)

| key | default | meaning |
|---|---|---|
| `workspace` [`HERMES_SBX_WORKSPACE`] | none | host directory mounted at the same path. Explicit only; never inferred from the cwd |
| `agent` [`HERMES_SBX_AGENT`] | `shell` | agent or kit passed to `sbx create` |
| `template` [`HERMES_SBX_TEMPLATE`] | sbx default | image for `--template` |
| `cpus`, `memory` | sbx default | `--cpus`, `--memory` |
| `pull` [`HERMES_SBX_PULL`] | `missing` | `always`, `missing` or `never` |
| `workspace_mode` | `mount` | `clone`: the agent works on a private git clone inside the VM, the host repo is read-only, fetch results with `git fetch sandbox-<name>` |
| `kits`, `static_mcp` | none | lists passed to `--kit` and `--static-mcp` |
| `command` [`HERMES_SBX_COMMAND`] | `sbx` | path of the sbx binary |

## Limits

- Covers Hermes' own tools. Harnesses Hermes launches over ACP (Claude Code, Codex, ...) run wherever that harness runs.
- The passwordless-sudo probe is not enabled (the backend does not declare `_sudo_nopasswd_probe_supported`).
- `sbx` is Docker's proprietary CLI and needs `sbx login`; the plugin shells out to it and does not bundle it.
- Tested against Hermes v0.21.6 and sbx v0.48.0.

## Tests

```sh
PYTHONPATH=<hermes-agent> <hermes-python> -m unittest discover -s tests -v   # fake sbx, no Docker needed
SBX_E2E=1 PYTHONPATH=<hermes-agent> <hermes-python> -m unittest discover -s tests -v   # real sbx
```
