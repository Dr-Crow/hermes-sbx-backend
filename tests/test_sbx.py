"""Run with Hermes' interpreter and source on the path:
   PYTHONPATH=<hermes-agent> <hermes-venv>/bin/python3 -m unittest discover -s tests -v
Real-sbx tests are opt-in: SBX_E2E=1.
"""
import importlib.util
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("hermes_sbx_plugin", ROOT / "__init__.py")
sbx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sbx)

# A fake sbx that records calls and runs `exec` commands locally, so BaseEnvironment's real execute() path is exercised.
FAKE = textwrap.dedent("""\
    #!/bin/sh
    D="$(dirname "$0")"
    echo "$*" >> "$D/log"
    case "$1" in
      ls) if [ -s "$D/names" ]; then printf '['; s=''; while read n; do printf '%s{"name":"%s"}' "$s" "$n"; s=','; done < "$D/names"; printf ']\\n'; else echo '[]'; fi ;;
      create) while [ $# -gt 0 ]; do [ "$1" = "--name" ] && echo "$2" >> "$D/names"; shift; done ;;
      rm) : > "$D/names" ;;
      exec) shift; [ "$1" = "-i" ] && shift; shift; exec "$@" ;;
    esac
""")


def fake_sbx():
    d = Path(tempfile.mkdtemp(prefix="fake-sbx-"))
    b = d / "sbx"
    b.write_text(FAKE)
    b.chmod(b.stat().st_mode | stat.S_IEXEC)
    return d, str(b)


def log(d):
    p = d / "log"
    return p.read_text().strip().splitlines() if p.exists() else []


class Naming(unittest.TestCase):
    def test_stable_safe_distinct(self):
        a = sbx.sandbox_name("agent:main/Session ONE")
        self.assertRegex(a, r"^hermes-[a-z0-9-]+-[0-9a-f]{8}$")
        self.assertEqual(a, sbx.sandbox_name("agent:main/Session ONE"))
        self.assertNotEqual(a, sbx.sandbox_name("agent:main/Session TWO"))
        self.assertRegex(sbx.sandbox_name(""), r"^hermes-default-[0-9a-f]{8}$")
        self.assertLess(len(sbx.sandbox_name("x" * 500)), 60)


class CreateArgs(unittest.TestCase):
    def test_defaults_and_extras(self):
        a = sbx.build_create_args(name="n", settings={}, workspace="/ws")
        self.assertEqual(a[:3], ["create", "shell", "/ws"])
        self.assertIn("--no-progress", a)
        b = sbx.build_create_args(name="n", workspace=None, settings={
            "agent": "claude", "cpus": 2, "memory": "4g", "template": "img:1", "kits": ["./k"], "static_mcp": ["a", "b"]})
        self.assertEqual(b[:3], ["create", "claude", "--name"])
        for k, v in [("--cpus", "2"), ("--memory", "4g"), ("--template", "img:1"), ("--kit", "./k"), ("--static-mcp", "a,b")]:
            self.assertEqual(b[b.index(k) + 1], v)
        with self.assertRaises(ValueError):
            sbx.build_create_args(name="n", settings={"pull": "sometimes"}, workspace=None)


class Environment(unittest.TestCase):
    def make(self, ws, **kw):
        d, b = fake_sbx()
        env = sbx.SbxEnvironment(cwd=ws, timeout=30, task_id=kw.pop("task_id", "t1"),
                                 settings={"command": b, **kw.pop("settings", {})}, **kw)
        return d, env

    def test_create_once_and_execute(self):
        ws = tempfile.mkdtemp(prefix="ws-")
        d, env = self.make("/root", settings={"workspace": ws})
        r = env.execute("echo hi && pwd")
        self.assertEqual(r["returncode"], 0, r)
        self.assertIn("hi", r["output"])
        self.assertIn(os.path.realpath(ws), r["output"])
        self.assertEqual(len([l for l in log(d) if l.startswith("create ")]), 1)
        env.execute("true")
        self.assertEqual(len([l for l in log(d) if l.startswith("create ")]), 1)
        self.assertTrue(any(f"create shell {ws} --name {env.name}" in l for l in log(d)))

    def test_cwd_and_env_persist_between_calls(self):
        ws = tempfile.mkdtemp(prefix="ws-")
        os.makedirs(os.path.join(ws, "sub"))
        d, env = self.make("/root", settings={"workspace": ws})
        env.execute("cd sub && export FOO=bar")
        r = env.execute("pwd; echo $FOO")
        self.assertIn("sub", r["output"])
        self.assertIn("bar", r["output"])

    def test_no_workspace_setting_is_mountless_even_if_cwd_exists(self):
        d, env = self.make("/root")
        self.assertIsNone(env._workspace)
        self.assertTrue(any(l.startswith(f"create shell --name {env.name}") for l in log(d)))

    def test_recreated_after_removal_flags_notice(self):
        ws = tempfile.mkdtemp(prefix="ws-")
        d, env = self.make("/root", settings={"workspace": ws})
        env.execute("true")
        (d / "names").write_text("")
        env._verified_at = 0.0  # TTL elapsed
        r = env.execute("echo again")
        self.assertEqual(r["returncode"], 0, r)
        self.assertEqual(len([l for l in log(d) if l.startswith("create ")]), 2)
        self.assertTrue(r.get("environment_recreated"), r)

    def test_container_default_cwd_maps_to_workspace(self):
        ws = tempfile.mkdtemp(prefix="ws-")
        d, env = self.make("/root", settings={"workspace": ws})
        r = env.execute("pwd", cwd="/root")  # what Hermes passes on the first call
        self.assertEqual(r["returncode"], 0, r)
        self.assertIn(os.path.realpath(ws), r["output"])
        r = env.execute("pwd", cwd="/tmp")  # an explicit, different cwd is honoured
        self.assertIn("/tmp", r["output"])

    def test_stdin_uses_dash_i(self):
        ws = tempfile.mkdtemp(prefix="ws-")
        d, env = self.make("/root", settings={"workspace": ws})
        r = env.execute("cat", stdin_data="piped-in")
        self.assertIn("piped-in", r["output"])
        self.assertTrue(any(l.startswith("exec -i ") for l in log(d)))

    def test_cleanup_persistent_vs_ephemeral(self):
        ws = tempfile.mkdtemp(prefix="ws-")
        d, env = self.make("/root", settings={"workspace": ws}, persistent=True)
        env.cleanup()
        self.assertFalse(any(l.startswith("rm ") for l in log(d)))
        d2, env2 = self.make("/root", settings={"workspace": ws}, persistent=False)
        env2.cleanup()
        self.assertTrue(any(l == f"rm --force {env2.name}" for l in log(d2)))

    def test_missing_binary(self):
        with self.assertRaises(RuntimeError):
            sbx.SbxEnvironment(cwd="~", timeout=5, settings={"command": "/nonexistent/sbx"})

    def test_create_failure_surfaces(self):
        d = Path(tempfile.mkdtemp())
        b = d / "sbx"
        b.write_text("#!/bin/sh\ncase \"$1\" in ls) echo '[]';; create) echo boom >&2; exit 3;; esac\n")
        b.chmod(0o755)
        with self.assertRaisesRegex(RuntimeError, "boom"):
            sbx.SbxEnvironment(cwd="~", timeout=5, settings={"command": str(b)})


class Provider(unittest.TestCase):
    def test_flags_and_registration(self):
        p = sbx.SbxProvider()
        self.assertEqual(p.name, "sbx")
        self.assertTrue(p.is_remote and p.is_container and p.session_isolated_when_nonpersistent)
        os.environ.pop("HERMES_SBX_SKIP_APPROVALS", None)
        self.assertFalse(p.skip_container_guards)
        os.environ["HERMES_SBX_SKIP_APPROVALS"] = "1"
        try:
            self.assertTrue(p.skip_container_guards)
        finally:
            del os.environ["HERMES_SBX_SKIP_APPROVALS"]
        got = []
        sbx.register(type("Ctx", (), {"register_terminal_environment_provider": lambda self, pr: got.append(pr)})())
        self.assertEqual(len(got), 1)

    def test_probe(self):
        d, b = fake_sbx()
        os.environ["HERMES_SBX_COMMAND"] = b
        try:
            self.assertEqual(sbx.SbxProvider().probe()[0], "ready")
            os.environ["HERMES_SBX_COMMAND"] = "/nonexistent/sbx"
            self.assertEqual(sbx.SbxProvider().probe()[0], "needs_setup")
        finally:
            del os.environ["HERMES_SBX_COMMAND"]


@unittest.skipUnless(os.environ.get("SBX_E2E") == "1", "set SBX_E2E=1 (needs a working `sbx login`)")
class RealSbx(unittest.TestCase):
    def test_end_to_end(self):
        import time
        root = tempfile.mkdtemp(prefix="hsbx-e2e-")
        ws = os.path.join(root, "workspace")
        os.makedirs(ws)
        Path(ws, "seed.txt").write_text("seed-from-host\n")
        t0 = time.time()
        env = sbx.SbxEnvironment(cwd="/root", timeout=120, task_id=f"e2e-{os.getpid()}-{int(t0)}", persistent=False,
                                 settings={"agent": "shell", "workspace": ws})
        try:
            print(f"\ncreate+init_session: {time.time() - t0:.1f}s")
            r = env.execute("pwd && cat seed.txt && uname -r && docker info --format 'docker {{.ServerVersion}}'")
            self.assertEqual(r["returncode"], 0, r)
            print(r["output"].strip())
            self.assertIn("seed-from-host", r["output"])
            self.assertIn("docker 2", r["output"])
            env.execute("cd /tmp && export PERSIST=yes")
            r = env.execute("pwd; echo $PERSIST")
            self.assertIn("/tmp", r["output"])
            self.assertIn("yes", r["output"])
            env.execute(f"echo from-guest > {ws}/guest.txt")
            self.assertEqual(Path(ws, "guest.txt").read_text().strip(), "from-guest")
            r = env.execute("docker run --rm busybox:latest echo container-in-microvm 2>&1 | tail -1")
            self.assertIn("container-in-microvm", r["output"])
            t1 = time.time(); env.execute("true"); print(f"warm execute: {time.time() - t1:.2f}s")
            # removed behind our back -> transparent recreate with notice
            env._sbx_run(["rm", "--force", env.name], timeout=60)
            env._verified_at = 0.0
            r = env.execute(f"cat {ws}/seed.txt")
            self.assertIn("seed-from-host", r["output"])
            self.assertTrue(r.get("environment_recreated"), r)
        finally:
            env.cleanup()
            env._sbx_run(["rm", "--force", env.name], timeout=60)


if __name__ == "__main__":
    unittest.main()
