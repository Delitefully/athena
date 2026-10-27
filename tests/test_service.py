"""athena service: the launchd agents and systemd user units, install and uninstall run twice, and the dry run."""
import contextlib
import io
import json
import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path

from tests import helpers
from athena_lib import cli, paths, service

FAKE = f"{sys.executable} {helpers.ROOT / 'tests' / 'fake_service.py'}"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.env = helpers.isolated_env(self.tmp.name)
        for key, value in {"ATHENA_LAUNCH_AGENTS": str(tmp / "LaunchAgents"),
                           "ATHENA_SYSTEMD_USER_DIR": str(tmp / "systemd"),
                           "ATHENA_LAUNCHCTL": FAKE, "ATHENA_SYSTEMCTL": FAKE,
                           "FAKE_SERVICE_LOG": str(tmp / "service.log"),
                           "FAKE_SERVICE_STATE": str(tmp / "loaded.json"),
                           "ATHENA_HERDR": "/opt/tools/herdr --session lt"}.items():
            self.env.set(key, value)
        self.out = []

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def calls(self):
        log = Path(os.environ["FAKE_SERVICE_LOG"])
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def changes(self, calls):
        """The calls that change something: not the read-only print and is-enabled."""
        return [c for c in calls if c[0] not in ("print",) and c[1:2] != ["is-enabled"]]

    def loaded(self):
        try:
            return json.loads(Path(os.environ["FAKE_SERVICE_STATE"]).read_text())
        except OSError:
            return []

    def install(self, kind="launchd", **kw):
        return service.install(kind, out=self.out.append, **kw)

    def plists(self, instance=None):
        return {job: plistlib.loads((service.launch_agents_dir() / f"{service.label(job, instance)}.plist").read_bytes())
                for job in service.JOBS}


class PlistTest(Fixture):
    def test_four_agents_with_what_each_needs(self):
        self.install()
        jobs = self.plists()
        self.assertEqual({j: p["Label"] for j, p in jobs.items()},
                         {j: f"athena.{j}" for j in service.JOBS})
        herdr = jobs["herdr"]
        # The athena herdr command, session included, as `herdr server`.
        self.assertEqual(herdr["ProgramArguments"], ["/opt/tools/herdr", "--session", "lt", "server"])
        # Restarted after a crash only, never fighting `herdr server stop`, and throttled.
        self.assertEqual(herdr["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(herdr["ThrottleInterval"], 30)
        self.assertTrue(herdr["RunAtLoad"])
        self.assertEqual(jobs["dash"]["ProgramArguments"][-2:], ["dash", "run"])
        self.assertEqual(jobs["board"]["ProgramArguments"][-4:], ["-m", "athena_lib.cli", "board", "watch"])
        self.assertEqual(jobs["board"]["WorkingDirectory"], str(paths.PLUGIN_ROOT))
        doctor = jobs["doctor"]
        self.assertEqual(doctor["ProgramArguments"][-3:], ["doctor", "--fix", "--quiet"])
        self.assertEqual(doctor["StartInterval"], 60)
        self.assertNotIn("KeepAlive", doctor)
        for job in ("dash", "board"):
            self.assertEqual(jobs[job]["KeepAlive"], {"SuccessfulExit": False})
        for job, p in jobs.items():
            self.assertEqual(p["StandardOutPath"], str(paths.state_dir() / "logs" / f"service-{job}.log"))

    def test_jobs_carry_this_shells_athena_environment_but_herdrs_panes_get_none(self):
        self.env.set("ATHENA_DASH_PORT", "3999")
        self.install()
        jobs = self.plists()
        for job in ("dash", "board", "doctor"):
            env = jobs[job]["EnvironmentVariables"]
            self.assertEqual(env["ATHENA_STATE_DIR"], str(paths.state_dir()))
            self.assertEqual(env["ATHENA_HERDR"], "/opt/tools/herdr --session lt")
            self.assertEqual(env["ATHENA_DASH_PORT"], "3999")
            self.assertEqual(env["PYTHONPATH"], str(paths.PLUGIN_ROOT))
        herdr_env = jobs["herdr"]["EnvironmentVariables"]
        self.assertFalse([k for k in herdr_env if k.startswith("ATHENA_") or k == "PYTHONPATH"], herdr_env)
        for p in jobs.values():
            self.assertIn("/usr/bin", p["EnvironmentVariables"]["PATH"].split(":"))

    def test_doctors_agent_abandons_its_process_group(self):
        self.install()
        jobs = self.plists()
        self.assertIs(jobs["doctor"]["AbandonProcessGroup"], True)  # a detached HQ restart outlives doctor
        self.assertNotIn("AbandonProcessGroup", jobs["herdr"])

    def test_an_instance_runs_beside_the_default(self):
        self.install(instance="lt")
        self.assertEqual(self.plists("lt")["herdr"]["Label"], "athena.lt.herdr")
        with self.assertRaises(service.ServiceError):
            self.install(instance="Bad Name")


class InstallTest(Fixture):
    def test_install_loads_every_job_and_records_it(self):
        result = self.install()
        self.assertEqual(set(result["files"].values()), {"new"})
        self.assertEqual(sorted(self.loaded()), sorted(f"athena.{j}" for j in service.JOBS))
        boots = [c for c in self.calls() if c[0] == "bootstrap"]
        self.assertEqual([os.path.basename(c[2]) for c in boots], [f"athena.{j}.plist" for j in service.JOBS])
        self.assertTrue(all(c[1] == f"gui/{os.getuid()}" for c in boots))
        manifest = service.installed()
        self.assertEqual(manifest["platform"], "launchd")
        self.assertEqual(manifest["jobs"]["dash"]["label"], "athena.dash")

    def test_install_twice_changes_nothing_the_second_time(self):
        self.install()
        before = {p: p.read_bytes() for p in service.launch_agents_dir().iterdir()}
        Path(os.environ["FAKE_SERVICE_LOG"]).unlink()
        result = self.install()
        self.assertEqual(set(result["files"].values()), {"unchanged"})
        self.assertEqual(result["ran"], [])
        self.assertEqual(self.changes(self.calls()), [])
        self.assertEqual({p: p.read_bytes() for p in service.launch_agents_dir().iterdir()}, before)

    def test_a_changed_job_is_reloaded_alone(self):
        self.install()
        Path(os.environ["FAKE_SERVICE_LOG"]).unlink()
        self.env.set("ATHENA_DASH_PORT", "3999")  # dash, board and doctor carry it; herdr does not
        result = self.install()
        self.assertEqual(result["files"][str(service.launch_agents_dir() / "athena.herdr.plist")], "unchanged")
        reloaded = [c[1].rsplit("/", 1)[-1] for c in self.changes(self.calls()) if c[0] == "bootout"]
        self.assertEqual(reloaded, ["athena.dash", "athena.board", "athena.doctor"])

    def test_a_loaded_herdr_job_is_never_reloaded(self):
        self.install()
        Path(os.environ["FAKE_SERVICE_LOG"]).unlink()
        self.env.set("ATHENA_HERDR", "/opt/tools/herdr --session other")
        result = self.install()
        self.assertEqual(result["files"][str(service.launch_agents_dir() / "athena.herdr.plist")], "changed")
        self.assertNotIn(["bootout", f"gui/{os.getuid()}/athena.herdr"], self.calls())
        self.assertTrue(any("herdr" in n for n in result["notes"]))

    def test_a_job_unloaded_by_hand_is_loaded_again(self):
        self.install()
        Path(os.environ["FAKE_SERVICE_STATE"]).write_text(json.dumps(["athena.herdr", "athena.dash", "athena.doctor"]))
        Path(os.environ["FAKE_SERVICE_LOG"]).unlink()
        self.install()
        self.assertEqual([c for c in self.changes(self.calls())],
                         [["bootstrap", f"gui/{os.getuid()}", str(service.launch_agents_dir() / "athena.board.plist")]])

    def test_uninstall_twice(self):
        self.install()
        result = service.uninstall("launchd", out=self.out.append)
        self.assertEqual(len(result["removed"]), 4)
        self.assertEqual(self.loaded(), [])
        self.assertEqual(list(service.launch_agents_dir().iterdir()), [])
        self.assertEqual(service.installed(), {})
        Path(os.environ["FAKE_SERVICE_LOG"]).unlink()
        again = service.uninstall("launchd", out=self.out.append)
        self.assertEqual((again["removed"], again["ran"]), ([], []))
        self.assertEqual(self.changes(self.calls()), [])

    def test_start_kickstarts_an_installed_job(self):
        self.assertFalse(service.start("dash"))  # not installed: doctor repairs it by hand
        self.install()
        self.assertTrue(service.start("dash"))
        self.assertIn(["kickstart", f"gui/{os.getuid()}/athena.dash"], self.calls())


class DryRunTest(Fixture):
    def test_dry_run_prints_exactly_what_install_writes_and_runs_nothing(self):
        result = self.install(dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(self.calls(), [])
        self.assertFalse(service.launch_agents_dir().exists())
        self.assertFalse(service.manifest_file().exists())
        printed = "\n".join(self.out)
        self.install()
        for path in service.launch_agents_dir().iterdir():
            self.assertIn(f"--- {path} (new: would write)\n{path.read_text()}", printed)
        for job in service.JOBS:
            self.assertIn(f"bootstrap gui/{os.getuid()} {service.launch_agents_dir() / f'athena.{job}.plist'}", printed)

    def test_dry_run_after_install_says_unchanged(self):
        self.install()
        self.out.clear()
        before = len(self.calls())
        self.install(dry_run=True)
        self.assertEqual(len(self.calls()), before)
        self.assertEqual(sum("(unchanged)" in line for line in self.out), 4)

    def test_uninstall_dry_run_runs_and_removes_nothing(self):
        self.install()
        before = len(self.calls())
        service.uninstall("launchd", dry_run=True, out=self.out.append)
        self.assertEqual(len(self.calls()), before)
        self.assertEqual(len(list(service.launch_agents_dir().iterdir())), 4)
        self.assertIn("bootout", "\n".join(self.out))

    def test_cli_dry_run(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["service", "install", "--dry-run", "--platform", "systemd"])
        self.assertEqual(code, 0)
        self.assertIn("athena-doctor.timer", out.getvalue())
        self.assertEqual(self.calls(), [])


class SystemdTest(Fixture):
    def unit_text(self, name):
        return (service.systemd_dir() / name).read_text()

    def test_units(self):
        self.install("systemd")
        names = sorted(p.name for p in service.systemd_dir().iterdir())
        self.assertEqual(names, ["athena-board.service", "athena-dash.service", "athena-doctor.service",
                                 "athena-doctor.timer", "athena-herdr.service"])
        herdr = self.unit_text("athena-herdr.service")
        self.assertIn("ExecStart=/opt/tools/herdr --session lt server\n", herdr)
        self.assertIn("Restart=on-failure\n", herdr)
        self.assertIn("RestartSec=30\n", herdr)
        self.assertIn("WantedBy=default.target\n", herdr)
        self.assertNotIn("ATHENA_", herdr)
        doctor = self.unit_text("athena-doctor.service")
        self.assertIn("Type=oneshot\n", doctor)
        self.assertNotIn("Restart=", doctor)
        timer = self.unit_text("athena-doctor.timer")
        self.assertIn("OnUnitActiveSec=60\n", timer)
        self.assertIn("Unit=athena-doctor.service\n", timer)
        self.assertIn(f"Environment=ATHENA_STATE_DIR={paths.state_dir()}\n", self.unit_text("athena-dash.service"))
        calls = self.changes(self.calls())
        self.assertEqual(calls[0], ["--user", "daemon-reload"])
        self.assertEqual([c[-1] for c in calls[1:]], ["athena-herdr.service", "athena-dash.service",
                                                       "athena-board.service", "athena-doctor.timer"])

    def test_doctor_leaves_what_it_started_running_when_it_exits(self):
        self.install("systemd")
        doctor = self.unit_text("athena-doctor.service")
        self.assertIn("KillMode=process\n", doctor)
        for job in ("herdr", "dash", "board"):
            self.assertNotIn("KillMode", self.unit_text(f"athena-{job}.service"))

    def test_quoting(self):
        self.assertEqual(service._systemd_quote("/a b/c"), '"/a b/c"')
        self.assertEqual(service._systemd_quote("50%"), "50%%")
        self.assertEqual(service._systemd_quote("$HOME"), "$$HOME")
        self.assertEqual(service._systemd_quote('say "hi"'), '"say \\"hi\\""')

    def test_install_and_uninstall_twice(self):
        self.install("systemd")
        Path(os.environ["FAKE_SERVICE_LOG"]).unlink()
        self.install("systemd")
        self.assertEqual(self.changes(self.calls()), [])
        service.uninstall("systemd", out=self.out.append)
        self.assertEqual(self.loaded(), [])
        self.assertEqual(list(service.systemd_dir().iterdir()), [])
        Path(os.environ["FAKE_SERVICE_LOG"]).unlink()
        service.uninstall("systemd", out=self.out.append)
        self.assertEqual(self.changes(self.calls()), [])

    def test_a_changed_dash_unit_is_restarted_not_herdr(self):
        self.install("systemd")
        Path(os.environ["FAKE_SERVICE_LOG"]).unlink()
        self.env.set("ATHENA_DASH_PORT", "3999")
        self.install("systemd")
        restarted = [c[-1] for c in self.calls() if c[1:2] == ["restart"]]
        self.assertEqual(restarted, ["athena-dash.service", "athena-board.service"])


if __name__ == "__main__":
    unittest.main()
