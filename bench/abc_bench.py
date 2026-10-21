#!/usr/bin/env python
"""Option A / B / C comparison harness. Drives Hermes' own terminal and file tool functions in-process, so no model
and no model credential is involved.

  A  our plugin, exec transport            terminal.backend: sbx   (terminal.sbx.transport: exec)
  B  Hermes' stock ssh backend -> <name>.sbx   terminal.backend: ssh
  C  our plugin, ssh transport             terminal.backend: sbx   (terminal.sbx.transport: ssh)

Run it with Hermes' interpreter, from any directory:
  python bench/abc_bench.py A|B|C --workspace /abs/dir [--home /tmp/hh-X] [--sandbox NAME]   (B needs --sandbox)

It writes a throwaway HERMES_HOME (connectors, nous guest, web and telemetry all OFF), enables this plugin there, and
prints one JSON report on stdout (progress on stderr). Needs `sbx` signed in and, for B and C, `sbx setup ssh`.
It only creates and removes sandboxes whose name it chose (hb-*) or was given with --sandbox.
"""
import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ap = argparse.ArgumentParser()
ap.add_argument("scenario", choices=["A", "B", "C"])
ap.add_argument("--workspace", required=True)
ap.add_argument("--home")
ap.add_argument("--sandbox", help="B: existing sandbox name. A/C: ignored (the plugin names it)")
ap.add_argument("--ssh-user", default="agent", help="B: user for <name>.sbx (sbx accepts agent, root, _default_user_)")
ap.add_argument("--terminal-cwd-ws", action="store_true", help="A/C: set terminal.cwd to the workspace instead of /root")
ap.add_argument("--n", type=int, default=15, help="commands for the latency sample")
ap.add_argument("--only", default="", help="comma list of step-number prefixes to run, e.g. 0_,1_,2_")
ap.add_argument("--skip", default="", help="comma list of steps to skip: approvals,recreate,stopped,sync,failures")
args = ap.parse_args()

SC = args.scenario
WS = os.path.abspath(args.workspace)
HOME = Path(args.home or tempfile.mkdtemp(prefix=f"hh-{SC}-"))
TASK = f"bench-{SC}-{os.getpid()}"
SKIP = set(filter(None, args.skip.split(",")))
ONLY = tuple(filter(None, args.only.split(",")))


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---- throwaway Hermes home -------------------------------------------------------------------------------------
shutil.rmtree(HOME, ignore_errors=True)
(HOME / "plugins").mkdir(parents=True)
(HOME / "plugins" / "sbx").symlink_to(ROOT)
(HOME / "skills" / "bench-skill").mkdir(parents=True)
(HOME / "skills" / "bench-skill" / "SKILL.md").write_text("---\nname: bench-skill\ndescription: x\n---\nhello from host skills\n")
TCWD = WS if args.terminal_cwd_ws else "/root"
terminal = {
    "A": f"  backend: sbx\n  cwd: {TCWD}\n  sbx:\n    workspace: {WS}\n    transport: exec\n",
    "C": f"  backend: sbx\n  cwd: {TCWD}\n  sbx:\n    workspace: {WS}\n    transport: ssh\n",
    "B": f"  backend: ssh\n  ssh_host: {args.sandbox}.sbx\n  ssh_user: {args.ssh_user}\n  cwd: {WS}\n",
}[SC]
(HOME / "config.yaml").write_text(
    "tools:\n  connectors:\n    enabled: false\nnous:\n  guest: false\n"
    "agent:\n  disabled_toolsets: [connections, web]\n"
    "web:\n  keyless_fallback: false\n  keyless_rescue: false\n"
    "security:\n  allow_lazy_installs: false\nupdates:\n  check: false\n  pre_update_backup: 'off'\n"
    "telemetry:\n  shared_metrics:\n    enabled: false\n    send: false\n"
    f"terminal:\n  env_passthrough: [BENCH_PASSTHROUGH]\n{terminal}"
    "plugins:\n  enabled:\n    - sbx\n  disabled: []\n")
os.environ["HERMES_HOME"] = str(HOME)
os.environ["OTEL_SDK_DISABLED"] = "true"
for k in [k for k in os.environ if k.startswith(("TERMINAL_", "HERMES_SBX_"))]:
    del os.environ[k]
# dummy values, not real secrets: they must never show up inside the sandbox
os.environ["OPENAI_API_KEY"] = "sk-DUMMY-bench-not-a-real-key"
os.environ["BENCH_HOST_ONLY_SECRET"] = "DUMMY-host-only-value"
os.environ["BENCH_PASSTHROUGH"] = "dummy-passthrough-value"   # listed in terminal.env_passthrough: should be forwarded

sys.path.insert(0, str(Path(os.environ.get("HERMES_SRC", Path.home() / "src/hermes-agent"))))
from hermes_cli.plugins import discover_plugins  # noqa: E402

discover_plugins()
import tools.terminal_tool as tt  # noqa: E402
from tools import file_tools as ft  # noqa: E402
from tools.terminal_tool_lifecycle import cleanup_vm  # noqa: E402

REPORT = {"scenario": SC, "workspace": WS, "home": str(HOME), "steps": {}}


def T(cmd, **kw):
    t0 = time.time()
    raw = tt.terminal_tool(cmd, task_id=TASK, **kw)
    dt = time.time() - t0
    try:
        r = json.loads(raw)
    except ValueError:
        r = {"output": raw, "exit_code": None, "unparsed": True}
    r["_s"] = round(dt, 2)
    return r


def short(r, n=700):
    o = (r.get("output") or r.get("error") or "")
    return {"exit": r.get("exit_code", r.get("returncode")), "s": r["_s"], "out": o[-n:],
            **{k: r[k] for k in ("status", "environment_recreated", "approval") if k in r}}


def step(name):
    def deco(fn):
        if name in SKIP or (ONLY and not name.startswith(ONLY)):
            return fn
        log(f"== {name}")
        t0 = time.time()
        try:
            REPORT["steps"][name] = fn()
        except Exception as exc:  # a failing step is a result, not a crash
            REPORT["steps"][name] = {"EXCEPTION": f"{type(exc).__name__}: {exc}", "tb": traceback.format_exc()[-1500:]}
            log("   EXCEPTION", exc)
        try:
            REPORT["steps"][name]["_env_cwd_after"] = getattr(env_obj(), "cwd", None)
        except Exception:
            pass
        REPORT["steps"][name]["_wall_s"] = round(time.time() - t0, 2) if isinstance(REPORT["steps"][name], dict) else None
        return fn
    return deco


def sbx(*a, timeout=120):
    p = subprocess.run(["sbx", *a], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    return p.returncode, (p.stdout + p.stderr).strip()


def env_obj():
    return tt._active_environments.get(tt._resolve_container_task_id(TASK))


def teardown():
    """What Hermes does at session end: cleanup_vm on the key the environment is cached under."""
    cleanup_vm(tt._resolve_container_task_id(TASK))


def sandbox_name():
    if SC == "B":
        return args.sandbox
    e = env_obj()
    return getattr(e, "name", None)


def sandbox_names():
    rc, out = sbx("ls", "--json")
    try:
        data = json.loads(out)
        data = data if isinstance(data, list) else (data or {}).get("sandboxes", [])
        return {e["name"] for e in data}
    except ValueError:
        return set()


@step("0_prompt_probe")
def _():
    """Hermes builds a one-off env (task id prompt-backend-probe) to describe a remote backend in the system prompt."""
    from hermes_cli.config import apply_terminal_config_to_env
    apply_terminal_config_to_env()
    import agent.prompt_builder as pb
    before = sandbox_names()
    t0 = time.time()
    hint = pb.build_environment_hints()
    dt = time.time() - t0
    new = sorted(sandbox_names() - before)
    for n in new:  # tidy up what the probe left behind (only sandboxes created inside this step)
        sbx("rm", "--force", n)
    return {"s": round(dt, 2), "hint": hint[-260:], "new_sandboxes_left_by_probe": new}


@step("1_cold_first_command")
def _():
    r = T("pwd; id -un; echo HOME=$HOME; hostname; uname -r")
    e = env_obj()
    return {**short(r), "env_class": type(e).__name__ if e else None, "sandbox": sandbox_name(),
            "sbx_ls": sbx("ls")[1]}


@step("2_workspace_visibility")
def _():
    Path(WS).mkdir(parents=True, exist_ok=True)
    Path(WS, "seed.txt").write_text("seed-from-host\n")
    r1 = T("cat seed.txt")                                  # default cwd
    r2 = T(f"echo from-guest > {WS}/guest.txt && ls -la {WS}")
    host_sees = Path(WS, "guest.txt").read_text().strip() if Path(WS, "guest.txt").exists() else None
    return {"guest_reads_host_file": short(r1), "guest_writes": short(r2, 300), "host_sees_guest_file": host_sees}


@step("3_cwd_and_env_persistence")
def _():
    T("cd /tmp && export BENCH_PERSIST=yes")
    r = T("pwd; echo PERSIST=$BENCH_PERSIST")
    return short(r)


@step("4_env_seen_by_commands")
def _():
    r = T("env | sort")
    out = r.get("output") or ""
    names = sorted({l.split("=", 1)[0] for l in out.splitlines() if "=" in l})
    # Hermes redacts secret-looking values in tool output, so count matches inside the guest instead of reading them
    probe = T("printenv OPENAI_API_KEY | grep -c DUMMY; printenv BENCH_HOST_ONLY_SECRET | grep -c DUMMY; "
              "printenv | grep -c -e DUMMY -e HERMES_HOME -e TERMINAL_ ; true")
    pt = T("echo passthrough=[$BENCH_PASSTHROUGH]")
    return {"n_vars": len(names), "host_dummy_values_in_guest(openai,hostonly,any-hermes)": (probe.get("output") or "").split(),
            "OPENAI_API_KEY_name_present": "OPENAI_API_KEY" in names,
            "env_passthrough_BENCH_PASSTHROUGH": (pt.get("output") or "").strip(),
            "proxy_or_sbx_vars": [n for n in names if "PROXY" in n.upper() or n.startswith("SBX")][:12],
            "key_names_with_KEY_or_TOKEN": [n for n in names if "KEY" in n or "TOKEN" in n],
            "sample_values": {n: [l for l in out.splitlines() if l.startswith(n + "=")][0][:60]
                              for n in names if n in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GH_TOKEN", "HTTPS_PROXY")}}


@step("5_file_tools")
def _():
    w = json.loads(ft.write_file_tool(f"{WS}/via_write_file.txt", "line1\nline2 é ü\n", task_id=TASK))
    rd = json.loads(ft.read_file_tool(f"{WS}/via_write_file.txt", task_id=TASK))
    rel = json.loads(ft.write_file_tool("via_relative.txt", "relative\n", task_id=TASK))
    host = {p: (Path(WS, p).read_text() if Path(WS, p).exists() else None) for p in ("via_write_file.txt", "via_relative.txt")}
    # host edits file, guest must see it
    Path(WS, "host_edit.txt").write_text("edited-on-host\n")
    rd2 = json.loads(ft.read_file_tool(f"{WS}/host_edit.txt", task_id=TASK))
    p = json.loads(ft.patch_tool(mode="replace", path=f"{WS}/via_write_file.txt", old_string="line1", new_string="LINE-ONE", task_id=TASK))
    return {"write_ok": "error" not in w, "write": str(w)[:200], "read_content": (rd.get("content") or str(rd))[:120],
            "relative_write": str(rel)[:200], "host_files": host,
            "read_host_edit": (rd2.get("content") or str(rd2))[:80],
            "patch": str(p)[:200], "host_after_patch": Path(WS, "via_write_file.txt").read_text()[:40]}


@step("6_fetch_file_download")
def _():
    e = env_obj()
    T("echo remote-only-content > /tmp/remote_only.txt")
    dest = Path(tempfile.mkdtemp()) / "dl.txt"
    t0 = time.time()
    e.fetch_file("/tmp/remote_only.txt", dest, max_bytes=1 << 20)
    return {"content": dest.read_text().strip(), "s": round(time.time() - t0, 2)}


@step("7_hermes_home_file_sync")
def _():
    e = env_obj()
    r = T("ls ~/.hermes/skills/bench-skill/ 2>&1; cat ~/.hermes/skills/bench-skill/SKILL.md 2>&1 | tail -1")
    return {"skill_visible_in_guest": short(r), "has_sync_manager": getattr(e, "_sync_manager", None) is not None}


@step("8_latency")
def _():
    xs = []
    for _ in range(args.n):
        xs.append(T("true")["_s"])
    ys = []
    for _ in range(5):
        ys.append(T("echo hello && pwd && ls | head -3")["_s"])
    return {"true_n": len(xs), "true_median_s": round(statistics.median(xs), 3), "true_min_s": min(xs), "true_max_s": max(xs),
            "mixed_median_s": round(statistics.median(ys), 3), "all_true": xs}


@step("9_approvals")
def _():
    out = {}
    calls = []
    os.environ["HERMES_INTERACTIVE"] = "1"

    def cb(*a, **k):
        calls.append((a, {kk: str(v)[:60] for kk, v in k.items()}))
        return cb.answer

    tt.set_approval_callback(cb)
    for label, answer, d in (("deny", "deny", "appr_d"), ("once", "once", "appr_o")):
        cb.answer = answer
        T(f"mkdir -p {WS}/{d} && touch {WS}/{d}/f")
        n0 = len(calls)
        r = T(f"rm -rf {WS}/{d}")
        out[label] = {**short(r, 300), "prompted": len(calls) - n0, "dir_still_on_host": Path(WS, d).exists()}
    # no callback, not interactive
    tt.set_approval_callback(None)
    os.environ.pop("HERMES_INTERACTIVE", None)
    T(f"mkdir -p {WS}/appr_n && touch {WS}/appr_n/f")
    r = T(f"rm -rf {WS}/appr_n")
    out["non_interactive_no_callback"] = {**short(r, 300), "dir_still_on_host": Path(WS, "appr_n").exists()}
    if SC != "B":  # the plugin's opt-out: no prompt at all
        os.environ["HERMES_SBX_SKIP_APPROVALS"] = "1"
        os.environ["HERMES_INTERACTIVE"] = "1"
        tt.set_approval_callback(cb)
        cb.answer = "deny"
        T(f"mkdir -p {WS}/appr_s && touch {WS}/appr_s/f")
        n0 = len(calls)
        r = T(f"rm -rf {WS}/appr_s")
        out["skip_approvals_opt_out"] = {**short(r, 200), "prompted": len(calls) - n0, "dir_still_on_host": Path(WS, "appr_s").exists()}
        os.environ.pop("HERMES_SBX_SKIP_APPROVALS")
        os.environ.pop("HERMES_INTERACTIVE")
        tt.set_approval_callback(None)
    out["provider_skip_container_guards"] = None
    try:
        from agent.terminal_env_registry import provider_flag
        out["provider_skip_container_guards"] = provider_flag("sbx", "skip_container_guards", "n/a") if SC != "B" else "n/a (ssh builtin: guards on)"
    except Exception as exc:
        out["provider_skip_container_guards"] = f"err {exc}"
    return out


@step("10_failures")
def _():
    out = {}
    e = env_obj()
    # a command that exits non-zero and one that times out
    out["exit_code_7"] = short(T("exit 7"), 200)
    out["timeout_3s"] = short(T("sleep 30", timeout=3), 300)
    out["stdin_pipe"] = short(T("echo piped | cat"), 100)
    out["after_timeout_still_alive"] = short(T("echo alive"), 100)
    out["binary_ok"] = short(T("head -c 300000 /dev/urandom | base64 | wc -c"), 100)
    out["big_output_s"] = T("seq 1 200000 | tail -1")["_s"]
    return out


@step("11_cleanup_then_reattach")
def _():
    name = sandbox_name()
    e = env_obj()
    sock = getattr(e, "control_socket", None)
    socks_before = sorted(str(p) for p in Path(tempfile.gettempdir(), "hermes-ssh").glob("*.sock")) if SC != "A" else []
    # remote edit of a synced file, to see whether sync_back pulls it onto the host at cleanup
    marker_host = HOME / "skills" / "bench-skill" / "SKILL.md"
    before = marker_host.read_text()
    T("echo REMOTE-EDIT >> ~/.hermes/skills/bench-skill/SKILL.md; mkdir -p ~/.hermes/skills/from-guest; echo evil > ~/.hermes/skills/from-guest/SKILL.md") if SC != "A" else None
    t0 = time.time()
    teardown()
    dt = time.time() - t0
    after = marker_host.read_text()
    return {"cleanup_s": round(dt, 2), "sockets_before": socks_before,
            "sockets_after": sorted(str(p) for p in Path(tempfile.gettempdir(), "hermes-ssh").glob("*.sock")) if SC != "A" else [],
            "host_skill_file_changed_by_guest": before != after,
            "host_got_guest_created_skill": (HOME / "skills" / "from-guest" / "SKILL.md").exists(),
            "sbx_ls_after_cleanup": sbx("ls")[1][-300:],
            "reattach_first_call": short(T("echo reattached; pwd"), 200),
            "env_cwd_after_reattach": env_obj().cwd}


@step("12_recreate_after_sbx_rm")
def _():
    name = sandbox_name()
    out = {"sandbox": name}
    r0 = T("echo warm")  # refresh the plugin's 10 s verify window
    out["warm_call"] = short(r0, 200)
    out["env_cwd_before_rm"] = env_obj().cwd
    rc, msg = sbx("rm", "--force", name)
    out["sbx_rm"] = [rc, msg[:120]]
    r = T(f"echo immediately-after-rm; cat {WS}/seed.txt")          # inside the verify TTL
    out["inside_ttl"] = short(r, 400)
    out["env_cwd_after_inside_ttl"] = env_obj().cwd
    time.sleep(11)
    r = T(f"echo after-ttl; cat {WS}/seed.txt")
    out["after_ttl"] = short(r, 400)
    r = T("echo again; pwd")
    out["next_call"] = short(r, 300)
    out["sbx_ls_after"] = sbx("ls")[1][-300:]
    if SC == "B":  # stock ssh has no lifecycle: an operator step (worker-up) must create the sandbox again
        out["external_recreate"] = sbx("create", "--name", name, "shell", WS, "--no-progress", timeout=300)[1][-80:]
        out["call_after_external_recreate(stale mux master)"] = short(T("echo hi; hostname"), 300)
        e = env_obj()
        socks = sorted(Path(tempfile.gettempdir(), "hermes-ssh").glob("*.sock"))  # Hermes keeps more than one master
        res = []
        for sock in socks:
            ex = subprocess.run(["ssh", "-o", f"ControlPath={sock}", "-O", "exit", f"{e.user}@{e.host}"],
                                capture_output=True, text=True)
            res.append((ex.stdout + ex.stderr).strip()[:60])
        out["ssh_O_exit_all_masters"] = res
        out["call_after_mux_exit"] = short(T("echo hi; hostname; cat seed.txt"), 300)
    return out


@step("13_sandbox_stopped")
def _():
    name = sandbox_name()
    out = {}
    out["stop"] = sbx("stop", name)
    time.sleep(2)
    out["ls_after_stop"] = sbx("ls")[1][-200:]
    r = T("echo after-stop; hostname")
    out["first_call_after_stop"] = short(r, 400)
    r = T("echo second; hostname")
    out["second_call"] = short(r, 300)
    return out


@step("14_final_cleanup")
def _():
    name = sandbox_name()
    t0 = time.time()
    teardown()
    dt = time.time() - t0
    left = sbx("ls")[1][-300:]
    if name:  # tidy up the sandbox this run used (A/C: the plugin's persistent sandbox; B: the --sandbox one)
        sbx("rm", "--force", name)
    return {"cleanup_s": round(dt, 2), "sbx_ls_before_rm": left}


print(json.dumps(REPORT, indent=1, default=str))
