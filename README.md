# hermes-sbx-backend

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) terminal-environment plugin that runs the agent's
`terminal`, `execute_code` and file tools inside [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) (`sbx`)
microVMs: a separate kernel and Docker engine per sandbox, so the agent can run `docker` itself with no host socket.

It is a pure plugin (`kind: backend`, one Python file, no dependencies, no Hermes core changes) built on Hermes' documented
`TerminalEnvironmentProvider` contract.

## Install

```sh
sbx login                                   # once
hermes plugins install Dr-Crow/hermes-sbx-backend --enable
hermes config set terminal.backend sbx
hermes config set terminal.sbx.workspace /path/to/project   # optional host dir, mounted read-write at the same path
hermes config set terminal.cwd /path/to/project             # same path: relative file-tool paths then land in the workspace
```

## Behaviour

- One sandbox per Hermes task id, named `hermes-<task>-<hash>`, created on first use (`sbx create shell ...`, 28 to 32 s cold in a
  nested VM, 0.25 to 0.4 s per command warm), resumed by sbx if it idled out, re-created (with Hermes' `environment_recreated` notice) if removed.
- `terminal.container_persistent: false` removes the sandbox on cleanup; `true` leaves it for the next session.
- With a workspace the backend tells Hermes it is not a closed container (`is_container` is false), because Hermes would
  otherwise replace a host-looking `terminal.cwd` such as `/home/me/proj` with `/root` and resolve relative file-tool paths
  there (permission denied for the sandbox user). Set `terminal.cwd` to the workspace path. Without a workspace the Hermes
  container defaults apply unchanged.
- Shell state (cwd, exported variables) persists between calls through Hermes' normal session snapshot.
- No credential is forwarded into the sandbox. Provider credentials stay on the host and are injected by sbx's proxy.
- Dangerous-command approval stays ON by default because a mounted workspace is real host data. Opt out with
  `terminal.sbx.skip_approvals: true` or `HERMES_SBX_SKIP_APPROVALS=1`.

## Transports

`terminal.sbx.transport` [`HERMES_SBX_TRANSPORT`] picks how commands reach the sandbox. The lifecycle (`sbx create`,
`sbx ls`, `sbx rm`), the workspace mount, the naming and the approvals are identical.

| | `exec` (default) | `ssh` |
|---|---|---|
| command path | `sbx exec <name> bash -c ...` | `ssh <name>.sbx bash -c ...`, Hermes' own `SSHEnvironment` (ControlMaster, `env_passthrough`, `~/.hermes` file sync) |
| prerequisite | `sbx login` | `sbx login` and `sbx setup ssh` once (adds the generated `Host *.sbx` block to `~/.ssh/config`; no key, no sshd, no port) |
| warm command | about 0.4 s | about 0.25 to 0.4 s (Hermes' poll interval dominates both) |
| skills, credential files, cache files in the sandbox's `~/.hermes` | not synced | pushed by Hermes' file sync (`sync_files`) |
| `terminal.env_passthrough` | not forwarded | not forwarded either: the sbx SSH endpoint drops `SendEnv` and `SetEnv` (checked with plain `ssh`) |

SSH-only settings: `ssh_user` [`HERMES_SBX_SSH_USER`] (default `_default_user_`, the placeholder the generated block uses for
the sandbox's own user; `agent` and `root` also work), `sync_files` (default `true`), `sync_back` (default `false`: Hermes'
`ssh` backend pulls the sandbox's `~/.hermes` back into the host's on cleanup, which lets a sandbox plant files in the
host's skills directory; here that is opt-in).

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
- Tested against Hermes v0.21.6 and sbx v0.48.0 (exec), and Hermes v0.22.0 (`e06df9c2`) and sbx v0.49.1 (exec and ssh).
- The `ssh` transport subclasses Hermes' `tools.environments.ssh.SSHEnvironment` (`hermes plugins validate` lists it, like
  the other core imports, under "private core imports"). It overrides `_build_ssh_command` and `_before_execute` and re-runs
  `SSHEnvironment.__init__` to re-attach, so a refactor of that class can break it.
- Warm-command latency is dominated by Hermes' own poll interval, so `exec` and `ssh` are indistinguishable per command; the transports differ in file sync and failure behaviour, not speed.

## Tests

```sh
PYTHONPATH=<hermes-agent> <hermes-python> -m unittest discover -s tests -v   # fake sbx and fake ssh, no Docker needed
SBX_E2E=1 PYTHONPATH=<hermes-agent> <hermes-python> -m unittest discover -s tests -v   # real sbx (exec transport)
SBX_E2E=1 SBX_E2E_SSH=1 PYTHONPATH=<hermes-agent> <hermes-python> -m unittest discover -s tests -v   # also the ssh transport (needs `sbx setup ssh`)
PYTHONPATH=<hermes-agent> <hermes-python> bench/abc_bench.py A|B|C --workspace DIR [--sandbox NAME]   # A/B/C comparison, see bench/
```
