"""transport: ssh. A fake `sbx` (lifecycle, shared with test_sbx.py) and a fake `ssh` that behaves like sbx's SSH endpoint:
it runs the remote command locally, answers `ssh -G`, and rejects commands for a removed sandbox or a stale connection
with the same text the real endpoint uses. Run with Hermes' interpreter and source on the path, like test_sbx.py.
"""
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_sbx  # noqa: E402  (the plugin module and the fake sbx)

import importlib  # noqa: E402

sbx = test_sbx.sbx
ssh_env = importlib.import_module("hermes_sbx_plugin.ssh_env")
log = test_sbx.log

FAKE_SSH = textwrap.dedent("""\
    #!/bin/sh
    D="$(dirname "$0")"
    printf 'ssh %s\\n' "$(echo "$*" | tr '\\n' ' ')" >> "$D/sshlog"
    while [ $# -gt 0 ]; do
      case "$1" in
        -G) [ -f "$D/noproxy" ] || echo "proxycommand /usr/bin/sbx ssh proxy $2"; echo "user x"; exit 0 ;;
        -O) rm -f "$D/stale"; exit 0 ;;
        -o|-p|-i|-F) shift 2 ;;
        -*) shift ;;
        *) break ;;
      esac
    done
    target="$1"; shift
    host="${target#*@}"; name="${host%.sbx}"
    if [ -f "$D/stale" ]; then echo "sandbox identity changed: sandbox \\"$name\\" is unavailable"; exit 1; fi
    if ! grep -qx "$name" "$D/names" 2>/dev/null; then
      echo "sandbox \\"$name\\" does not exist and auto-create is off (enable with: sbx settings set ssh.autoCreate true)"; exit 1
    fi
    exec sh -c "$*"
""")


def fake_env():
    d, b = test_sbx.fake_sbx()
    for name, body in (("ssh", FAKE_SSH), ("scp", "#!/bin/sh\nexit 0\n")):
        p = d / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return d, b


def sshlog(d):
    p = d / "sshlog"
    return p.read_text().strip().splitlines() if p.exists() else []


class SshBase(unittest.TestCase):
    def setUp(self):
        self.d, self.b = fake_env()
        self._saved = {k: os.environ.get(k) for k in ("PATH", "HOME", "HERMES_HOME")}
        self.home = tempfile.mkdtemp(prefix="remote-home-")  # what `echo $HOME` answers: the fake sandbox's home
        os.environ["HOME"] = self.home
        os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="hermes-home-")
        os.environ["PATH"] = f"{self.d}:{os.environ['PATH']}"
        self.ws = tempfile.mkdtemp(prefix="ws-")

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def make(self, persistent=True, **settings):
        base = {"command": self.b, "workspace": self.ws, "sync_files": False}
        base.update(settings)
        return ssh_env.SbxSshEnvironment(cwd="/root", timeout=30, task_id="t1", persistent=persistent, settings=base)


class Integration(SshBase):
    def test_detects_missing_sbx_setup_ssh(self):
        self.assertIsNone(ssh_env.ssh_integration_error())
        (self.d / "noproxy").write_text("")
        self.assertIn("sbx setup ssh", ssh_env.ssh_integration_error())
        with self.assertRaisesRegex(RuntimeError, "sbx setup ssh"):
            self.make()
        self.assertFalse([l for l in log(self.d) if l.startswith("create ")], "must not create a sandbox it cannot reach")


class Lifecycle(SshBase):
    def test_create_once_connect_to_name_dot_sbx(self):
        env = self.make()
        r = env.execute("echo hi && pwd")
        self.assertEqual(r["returncode"], 0, r)
        self.assertIn("hi", r["output"])
        self.assertIn(os.path.realpath(self.ws), r["output"])  # cwd /root (Hermes' container default) means the workspace
        env.execute("true")
        self.assertEqual(len([l for l in log(self.d) if l.startswith("create ")]), 1)
        self.assertTrue(any(f"create shell {self.ws} --name {env.name}" in l for l in log(self.d)))
        cmds = [l for l in sshlog(self.d) if " -G " not in l and "-O exit" not in l]
        self.assertTrue(cmds)
        self.assertTrue(all(f"_default_user_@{env.name}.sbx" in l for l in cmds), cmds[:2])

    def test_ssh_options_are_compatible_with_the_generated_block(self):
        env = self.make()
        env.execute("true")
        line = [l for l in sshlog(self.d) if "bash" in l or "true" in l][-1]
        self.assertIn("BatchMode=yes", line)          # the generated block needs no prompt either
        self.assertIn("StrictHostKeyChecking=yes", line)   # not Hermes' accept-new: the integration says yes
        self.assertNotIn("accept-new", line)
        self.assertNotIn(" -i ", line)                 # no key
        self.assertNotIn(" -p ", line)                 # no port

    def test_ssh_user_setting(self):
        env = self.make(ssh_user="agent")
        env.execute("true")
        self.assertTrue(any(f"agent@{env.name}.sbx" in l for l in sshlog(self.d)))

    def test_cwd_and_env_persist(self):
        os.makedirs(os.path.join(self.ws, "sub"))
        env = self.make()
        env.execute("cd sub && export FOO=bar")
        r = env.execute("pwd; echo $FOO")
        self.assertIn("sub", r["output"])
        self.assertIn("bar", r["output"])

    def test_without_workspace_cwd_is_the_sandbox_home(self):
        env = self.make(workspace=None)
        self.assertIsNone(env._workspace)
        self.assertTrue(any(l.startswith(f"create shell --name {env.name}") for l in log(self.d)))
        r = env.execute("pwd")
        self.assertIn(os.path.realpath(self.home), r["output"])

    def test_removed_sandbox_inside_the_verify_window_is_healed_and_the_command_still_runs_once(self):
        env = self.make()
        env.execute("true")
        (self.d / "names").write_text("")  # sbx rm behind our back, verify TTL has NOT elapsed
        r = env.execute(f"echo ran >> {self.ws}/count.txt; echo again")
        self.assertEqual(r["returncode"], 0, r)
        self.assertIn("again", r["output"])
        self.assertTrue(r.get("environment_recreated"), r)
        self.assertEqual(len([l for l in log(self.d) if l.startswith("create ")]), 2)
        self.assertEqual(Path(self.ws, "count.txt").read_text().count("ran"), 1, "the command must not run twice")

    def test_removed_sandbox_after_the_verify_window(self):
        env = self.make()
        env.execute("true")
        (self.d / "names").write_text("")
        env._verified_at = 0.0
        r = env.execute("echo again")
        self.assertEqual(r["returncode"], 0, r)
        self.assertTrue(r.get("environment_recreated"), r)
        self.assertEqual(len([l for l in log(self.d) if l.startswith("create ")]), 2)

    def test_stale_connection_to_an_older_instance_reconnects_without_creating(self):
        env = self.make()
        env.execute("true")
        env.control_socket.touch()  # a live ControlMaster would have left its socket here
        (self.d / "stale").write_text("")  # sandbox exists, but the multiplexed connection is bound to an old instance
        r = env.execute("echo ok")
        self.assertEqual(r["returncode"], 0, r)
        self.assertIn("ok", r["output"])
        self.assertEqual(len([l for l in log(self.d) if l.startswith("create ")]), 1)
        self.assertFalse(r.get("environment_recreated"))
        self.assertTrue(any("-O exit" in l for l in sshlog(self.d)), "the stale ControlMaster must be closed")

    def test_only_short_sbx_refusals_count_as_rejection(self):
        rej = ssh_env.SbxSshEnvironment._rejected_by_sbx
        self.assertTrue(rej({"returncode": 1, "output": 'sandbox identity changed: sandbox "x" is unavailable'}))
        self.assertFalse(rej({"returncode": 0, "output": "sandbox identity changed"}))
        self.assertFalse(rej({"returncode": 1, "output": "x" * 500 + "is unavailable"}))
        self.assertFalse(rej({"returncode": 1, "output": "make: *** failed"}))

    def test_cleanup_persistent_ephemeral_force(self):
        env = self.make(persistent=True)
        env.cleanup()
        self.assertFalse(any(l.startswith("rm ") for l in log(self.d)))
        env.cleanup(force_remove=True)
        self.assertTrue(any(l == f"rm --force {env.name}" for l in log(self.d)))
        env2 = self.make(persistent=False)
        env2.cleanup()
        self.assertTrue(any(l == f"rm --force {env2.name}" for l in log(self.d)))


class FileSync(SshBase):
    def seed_skill(self):
        skill = Path(os.environ["HERMES_HOME"], "skills", "demo")
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("host skill\n")

    def test_skills_are_pushed_into_the_sandbox_home(self):
        self.seed_skill()
        env = self.make(sync_files=True)
        pushed = Path(self.home, ".hermes", "skills", "demo", "SKILL.md")
        self.assertTrue(pushed.exists(), sshlog(self.d)[-3:])
        self.assertEqual(pushed.read_text(), "host skill\n")
        env.cleanup()

    def test_sandbox_side_changes_are_not_pulled_back_by_default(self):
        self.seed_skill()
        env = self.make(sync_files=True)
        Path(self.home, ".hermes", "skills", "demo", "SKILL.md").write_text("changed in the sandbox\n")
        Path(self.home, ".hermes", "skills", "evil").mkdir()
        Path(self.home, ".hermes", "skills", "evil", "SKILL.md").write_text("planted\n")
        env.cleanup()
        host = Path(os.environ["HERMES_HOME"], "skills")
        self.assertEqual((host / "demo" / "SKILL.md").read_text(), "host skill\n")
        self.assertFalse((host / "evil").exists())
        self.assertFalse(any("tar cf -" in l for l in sshlog(self.d)), "no download at all when sync_back is off")

    def test_sync_back_opt_in_pulls_changes(self):
        self.seed_skill()
        env = self.make(sync_files=True, sync_back=True)
        Path(self.home, ".hermes", "skills", "demo", "SKILL.md").write_text("changed in the sandbox\n")
        env.cleanup()
        self.assertEqual(Path(os.environ["HERMES_HOME"], "skills", "demo", "SKILL.md").read_text(),
                         "changed in the sandbox\n")


class Provider(SshBase):
    def setUp(self):
        super().setUp()
        self._env_keys = ("HERMES_SBX_TRANSPORT", "HERMES_SBX_COMMAND", "HERMES_SBX_WORKSPACE")
        for k in self._env_keys:
            os.environ.pop(k, None)

    def tearDown(self):
        for k in self._env_keys:
            os.environ.pop(k, None)
        super().tearDown()

    def test_transport_selects_the_environment_class(self):
        os.environ["HERMES_SBX_COMMAND"] = self.b
        os.environ["HERMES_SBX_WORKSPACE"] = self.ws
        p = sbx.SbxProvider()
        self.assertIsInstance(p.create_environment(cwd="/root", timeout=5, task_id="a"), sbx.SbxEnvironment)
        os.environ["HERMES_SBX_TRANSPORT"] = "ssh"
        self.assertIsInstance(p.create_environment(cwd="/root", timeout=5, task_id="b"), ssh_env.SbxSshEnvironment)
        os.environ["HERMES_SBX_TRANSPORT"] = "carrier-pigeon"
        with self.assertRaisesRegex(ValueError, "transport"):
            p.create_environment(cwd="/root", timeout=5, task_id="c")

    def test_cache_path_base_only_when_files_are_synced(self):
        p = sbx.SbxProvider()
        self.assertIsNone(p.cache_path_base)
        os.environ["HERMES_SBX_TRANSPORT"] = "ssh"
        self.assertEqual(p.cache_path_base, "~/.hermes")

    def test_probe_reports_missing_ssh_integration(self):
        os.environ["HERMES_SBX_COMMAND"] = self.b
        p = sbx.SbxProvider()
        self.assertEqual(p.probe()[0], "ready")
        os.environ["HERMES_SBX_TRANSPORT"] = "ssh"
        self.assertEqual(p.probe()[0], "ready")
        self.assertTrue(p.check_requirements({}))
        (self.d / "noproxy").write_text("")
        status, detail = p.probe()
        self.assertEqual(status, "needs_setup")
        self.assertIn("sbx setup ssh", detail)
        self.assertFalse(p.check_requirements({}))
        rows = p.doctor_checks()
        self.assertTrue(any(not ok and "SSH" in label for ok, label, _ in rows), rows)

    def test_probe_environment_still_a_stub_with_ssh_transport(self):
        os.environ["HERMES_SBX_COMMAND"] = self.b
        os.environ["HERMES_SBX_TRANSPORT"] = "ssh"
        env = sbx.SbxProvider().create_environment(cwd="/root", timeout=5, task_id="prompt-backend-probe")
        self.assertIn("os=Linux", env.execute("uname")["output"])
        self.assertEqual(log(self.d), [])
        self.assertEqual(sshlog(self.d), [])


@unittest.skipUnless(os.environ.get("SBX_E2E") == "1" and os.environ.get("SBX_E2E_SSH") == "1",
                     "set SBX_E2E=1 SBX_E2E_SSH=1 (needs `sbx login` and `sbx setup ssh`)")
class RealSbxSsh(unittest.TestCase):
    def test_end_to_end(self):
        import time
        root = tempfile.mkdtemp(prefix="hsbx-e2e-ssh-")
        ws = os.path.join(root, "workspace")
        os.makedirs(ws)
        Path(ws, "seed.txt").write_text("seed-from-host\n")
        t0 = time.time()
        env = ssh_env.SbxSshEnvironment(
            cwd="/root", timeout=120, task_id=f"e2e-ssh-{os.getpid()}-{int(t0)}", persistent=False,
            settings={"workspace": ws, "sync_files": False})
        try:
            print(f"\ncreate+connect: {time.time() - t0:.1f}s")
            r = env.execute("pwd && cat seed.txt && uname -r")
            self.assertEqual(r["returncode"], 0, r)
            self.assertIn("seed-from-host", r["output"])
            self.assertIn(os.path.realpath(ws), r["output"])
            env.execute("cd /tmp && export PERSIST=yes")
            r = env.execute("pwd; echo $PERSIST")
            self.assertIn("/tmp", r["output"])
            self.assertIn("yes", r["output"])
            env.execute(f"echo from-guest > {ws}/guest.txt")
            self.assertEqual(Path(ws, "guest.txt").read_text().strip(), "from-guest")
            t1 = time.time(); env.execute("true"); print(f"warm execute: {time.time() - t1:.2f}s")
            env._sbx_run(["rm", "--force", env.name], timeout=60)  # removed behind our back, inside the verify window
            r = env.execute(f"cat {ws}/seed.txt")
            self.assertIn("seed-from-host", r["output"], r)
            self.assertTrue(r.get("environment_recreated"), r)
        finally:
            env.cleanup()
            env._sbx_run(["rm", "--force", env.name], timeout=60)


if __name__ == "__main__":
    unittest.main()
