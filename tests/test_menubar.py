"""The menu bar app: Quit athena, Start athena and Restart services through the services capability against a fake launchd,
its install as a login job, and the Swift app itself, built with menubar/build.sh and driven headless against a fake
`athena-rex` CLI, and end to end against the real one with launchctl stubbed."""
import json
import os
import plistlib
import shutil
import subprocess
import types
import zlib
from unittest import mock
from pathlib import Path

from harness import BIN, Case, ROOT

from athena_rex.caps import dash, doctor, menubar, services, watch
from athena_rex.cli import UserError
from athena_rex.kernel import caps, store

UID = os.getuid()
CORE = ("dash", "doctor", "watch")

# A launchd that remembers: bootstrap loads, bootout unloads, disable is kept, and a disabled job won't load, as launchd's own.
FAKE_LAUNCHCTL = r"""#!/bin/sh
echo "launchctl $*" >> "$ATHENA_WORLD_CALLS"
db="$ATHENA_WORLD_CALLS.launchd"; off="$ATHENA_WORLD_CALLS.disabled"
sticky="$ATHENA_WORLD_CALLS.sticky"; broken="$ATHENA_WORLD_CALLS.broken"  # a bootout that doesn't unload; a bootstrap that fails
touch "$db" "$off" "$sticky" "$broken"
label() { l=${1#*/}; echo "${l#*/}"; }
case "$1" in
bootstrap)
	l=$(basename "$3" .plist)
	grep -qx "$l" "$off" && { echo "Bootstrap failed: 119: Service is disabled" >&2; exit 119; }
	grep -qx "$l" "$broken" && { echo "Bootstrap failed: 122: Path had bad ownership/permissions" >&2; exit 122; }
	grep -qx "$l" "$db" && { echo "Bootstrap failed: 5: Input/output error" >&2; exit 5; }
	echo "$l" >> "$db" ;;
bootout)
	l=$(label "$2"); grep -qx "$l" "$db" || exit 113
	grep -qx "$l" "$sticky" && exit 0
	grep -vx "$l" "$db" > "$db.new"; mv "$db.new" "$db" ;;
print) l=$(label "$2"); grep -qx "$l" "$db" || exit 113 ;;
disable) l=$(label "$2"); grep -qx "$l" "$off" || echo "$l" >> "$off" ;;
enable) l=$(label "$2"); grep -vx "$l" "$off" > "$off.new"; mv "$off.new" "$off" ;;
kickstart) l=$(label "$3"); grep -qx "$l" "$db" || exit 113 ;;
list)
	printf 'PID\tStatus\tLabel\n'
	n=4000; while read -r l; do n=$((n + 1)); printf '%s\t0\t%s\n' "$n" "$l"; done < "$db" ;;
esac
exit 0
"""


def beacon_module():
    """A loaded module as the modules loader leaves it: a capability marked MODULE, with a launchd job of its own."""
    return types.SimpleNamespace(__name__="athena_rex_modules.beacon", MODULE="beacon",
                                 services=lambda ctx: [caps.Service("beacon", ["beacon", "run"])])


class FakeLaunchd(Case):
    def setUp(self):
        super().setUp()
        script = self.stubs / "launchctl"
        script.write_text(FAKE_LAUNCHCTL)
        script.chmod(0o755)

    def loaded(self) -> set:
        path = Path(f"{self.calls}.launchd")
        return set(path.read_text().split()) if path.exists() else set()

    def disabled(self) -> set:
        path = Path(f"{self.calls}.disabled")
        return set(path.read_text().split()) if path.exists() else set()

    def launchctl_calls(self) -> list:
        return [c[len("launchctl "):] for c in self.world_calls() if c.startswith("launchctl ")]

    def with_beacon(self):
        real = caps.loaded
        caps.loaded = lambda: [*real(), beacon_module()]
        self.addCleanup(setattr, caps, "loaded", real)

    def install(self):
        self.with_beacon()
        services.install(caps.CTX)
        self.assertEqual(self.loaded(), {f"athena-rex.{n}" for n in (*CORE, "menubar", "beacon")})
        self.calls.write_text("")


class InstallAndRestart(FakeLaunchd):
    """Found live: a bootstrap of a job still loaded aborted install before the menu bar's plist; restart kickstarted a job that
    wasn't loaded; and each plist write shows the human a Background Activity notice."""

    def _plists(self) -> dict:
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(os.environ["ATHENA_LAUNCH_AGENTS"]).glob("*.plist")}

    def test_a_job_still_loaded_is_fine_and_one_failing_stops_no_other(self):
        self.install()
        Path(f"{self.calls}.sticky").write_text("athena-rex.doctor\n")
        Path(f"{self.calls}.broken").write_text("athena-rex.dash\n")
        os.environ["ATHENA_HQ"] = "someone-else"  # every plist changes, so each is written and loaded again
        self.addCleanup(os.environ.pop, "ATHENA_HQ", None)
        with mock.patch.object(services.time, "sleep"):
            actions = services.install(caps.CTX)
        text = "\n".join(actions)
        self.assertIn("athena-rex.doctor was still loaded", text)
        self.assertIn("failed athena-rex.dash", text)
        for name in ("watch", "menubar", "beacon"):
            self.assertIn(f"wrote {Path(os.environ['ATHENA_LAUNCH_AGENTS']) / f'athena-rex.{name}.plist'}", text, name)
        self.assertIn("athena-rex.menubar", self.loaded())
        done = self.cli("services", "install")
        self.assertEqual(done.returncode, 0, "the CLI says a failure, by its exit code too, only when there is one")

    def test_a_failure_is_said_and_the_command_exits_nonzero(self):
        self.install()
        Path(f"{self.calls}.broken").write_text("athena-rex.dash\n")
        os.environ["ATHENA_HQ"] = "someone-else"
        self.addCleanup(os.environ.pop, "ATHENA_HQ", None)
        done = self.cli("services", "install")
        self.assertEqual(done.returncode, 1)
        self.assertIn("failed athena-rex.dash", done.stdout)
        self.assertIn("athena-rex.watch.plist", done.stdout, "the others still went in")

    def test_restart_loads_a_job_that_was_not_loaded(self):
        self.install()
        self.assertEqual(self.cli("services", "start").returncode, 0)
        Path(f"{self.calls}.launchd").write_text("athena-rex.dash\nathena-rex.watch\n")
        actions = services.restart(caps.CTX)
        self.assertIn("started athena-rex.doctor, which was not loaded", actions)
        self.assertIn("athena-rex.doctor", self.loaded())
        self.assertIn("restarted athena-rex.watch", actions)

    def test_a_second_install_writes_nothing_whoever_runs_it(self):
        self.install()
        before = self._plists()
        os.environ["PATH"] = "/somewhere/else:" + os.environ["PATH"]
        os.environ["ATHENA_HQ"] = "athena"
        self.addCleanup(os.environ.pop, "ATHENA_HQ", None)
        self.calls.write_text("")
        actions = services.install(caps.CTX)
        self.assertEqual(self._plists(), before, "not a byte, not a touch")
        self.assertTrue(all(a.startswith("unchanged ") for a in actions if "plist" in a), actions)
        self.assertEqual([c for c in self.launchctl_calls() if c.split()[0] in ("bootstrap", "bootout")], [])


class QuitAndStart(FakeLaunchd):
    def test_quit_stops_the_watchdog_first_then_the_rest_and_leaves_modules_itself_and_rex(self):
        self.install()
        self.assertTrue(caps.CTX.backend.up())
        actions = services.stop(caps.CTX)
        calls = self.launchctl_calls()
        self.assertEqual(calls[:2], [f"disable gui/{UID}/athena-rex.doctor", f"bootout gui/{UID}/athena-rex.doctor"])
        self.assertEqual(calls[2:], [f"{verb} gui/{UID}/athena-rex.{name}" for name in ("dash", "watch") for verb in ("disable", "bootout")])
        self.assertTrue(caps.CTX.backend.up(), "Rex runs the sessions, HQ's and the workers': Quit never stops it")
        self.assertEqual(self.loaded(), {"athena-rex.beacon", "athena-rex.menubar"}, "modules and the app itself are left")
        self.assertEqual(self.disabled(), {f"athena-rex.{n}" for n in CORE}, "launchd won't load them at login either")
        self.assertIn("left running: beacon", "\n".join(actions))
        self.assertTrue(services.is_stopped())

    def test_quit_also_stops_a_watch_doctor_started_outside_launchd_before_any_on_quit(self):
        self.install()
        order = []
        real = caps.loaded
        caps.loaded = lambda: [*real(), types.SimpleNamespace(__name__="athena_rex_modules.beacon", on_quit=lambda ctx: order.append("on_quit"))]
        with mock.patch.object(watch, "stop_running", side_effect=lambda: order.append("watch") or True) as stopped:
            actions = services.stop(caps.CTX)
        stopped.assert_called_once_with()
        self.assertEqual(order[:2], ["watch", "on_quit"], "no tick of a detached watch writes after on_quit")
        self.assertIn("stopped the watch doctor started outside launchd", actions)

    def test_a_watch_it_cannot_stop_is_said_and_quit_goes_on(self):
        self.install()
        with mock.patch.object(watch, "stop_running", side_effect=UserError("a watch holds its lock, and pid 7 isn't it")):
            actions = services.stop(caps.CTX)
        self.assertIn("watch: a watch holds its lock, and pid 7 isn't it", actions)
        self.assertTrue(services.is_stopped())
        with mock.patch.object(watch, "stop_running", return_value=False):
            self.assertFalse(any("outside launchd" in a for a in services.stop(caps.CTX)), "none running: nothing said")

    def test_nothing_comes_back_after_quit(self):
        self.install()
        services.stop(caps.CTX)
        after = len(self.launchctl_calls())
        self.assertFalse(any(c.split()[0] in ("bootstrap", "kickstart", "enable") for c in self.launchctl_calls()))
        services.install(caps.CTX)  # even a reinstall keeps them stopped until Start
        later = self.launchctl_calls()[after:]
        self.assertFalse(any(c.split()[0] in ("bootstrap", "enable") and "menubar" not in c and "beacon" not in c for c in later), later)
        self.assertEqual(self.loaded() & {f"athena-rex.{n}" for n in CORE}, set())
        out = self.cli("services", "restart")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("stopped", out.stderr)
        self.assertEqual(self.loaded() & {f"athena-rex.{n}" for n in CORE}, set())

    def test_start_loads_them_again_with_the_watchdog_last(self):
        self.install()
        services.stop(caps.CTX)
        self.calls.write_text("")
        services.start(caps.CTX)
        calls = self.launchctl_calls()
        bootstraps = [c.split()[-1] for c in calls if c.startswith("bootstrap")]
        self.assertEqual([Path(p).stem for p in bootstraps], ["athena-rex.dash", "athena-rex.watch", "athena-rex.doctor"])
        for name in CORE:
            self.assertLess(calls.index(f"enable gui/{UID}/athena-rex.{name}"),
                            calls.index(f"bootstrap gui/{UID} {services.agents_dir() / f'athena-rex.{name}.plist'}"))
        self.assertFalse(any("beacon" in c or "menubar" in c for c in calls))
        self.assertEqual(self.loaded(), {f"athena-rex.{n}" for n in (*CORE, "menubar", "beacon")})
        self.assertEqual(self.disabled(), set())
        self.assertFalse(services.is_stopped())

    def test_restart_kickstarts_athenas_own_jobs_only(self):
        self.install()
        self.assertEqual(self.cli("services", "restart").returncode, 0)
        self.assertEqual(sorted(self.launchctl_calls()), sorted(f"kickstart -k gui/{UID}/athena-rex.{n}" for n in CORE))

    def test_the_cli_says_each_step_and_status_reads_launchd(self):
        services.install(caps.CTX)
        out = self.cli("services", "stop")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines()[0], "stopped athena-rex.doctor")
        self.assertNotIn("Rex server", out.stdout)
        shown = json.loads(self.cli("services", "status", "--json").stdout)
        self.assertTrue(shown["stopped"])
        rows = {r["name"]: r for r in shown["services"]}
        self.assertEqual(rows["doctor"]["state"], "stopped")
        self.assertEqual(rows["menubar"]["state"], "running")
        self.assertTrue(rows["menubar"]["pid"])
        self.assertEqual(rows["doctor"]["every"], 60)
        self.assertEqual(self.cli("services", "start").returncode, 0)
        shown = json.loads(self.cli("services", "status", "--json").stdout)
        self.assertFalse(shown["stopped"])
        self.assertEqual({r["state"] for r in shown["services"]}, {"running"})

    def test_quit_never_stops_rex_or_a_session_whatever_config_names(self):
        services.install(caps.CTX)
        caps.CTX.backend.create("hq", str(self.tmp), ["true"])
        before = caps.CTX.backend.spaces()
        for server in ("", f"unix:///private/tmp/claude-{UID}/ar-menubar/s.sock"):
            out = self.cli("services", "stop", env={"ATHENA_REX_SERVER": server})
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertTrue(caps.CTX.backend.up())
            self.assertEqual(caps.CTX.backend.spaces(), before)
        self.assertNotIn("athena-rex.doctor", self.loaded(), "athena's own jobs still stop")
        self.assertFalse(any(" rex " in f" {c} " for c in self.world_calls()))

    def test_each_on_quit_runs_once_the_watch_is_down_and_one_that_fails_stops_nothing(self):
        services.install(caps.CTX)
        seen = []

        def card_off(ctx):
            seen.append(set(self.loaded()))
            return "beacon: ExampleCard set to off until Start"

        def broken(ctx):
            raise OSError("card gone")
        real = caps.loaded
        caps.loaded = lambda: [*real(), types.SimpleNamespace(__name__="athena_rex_modules.beacon", on_quit=card_off),
                               types.SimpleNamespace(__name__="athena_rex_modules.widget", on_quit=broken)]
        self.addCleanup(setattr, caps, "loaded", real)
        actions = services.stop(caps.CTX)
        self.assertEqual(len(seen), 1)
        self.assertNotIn("athena-rex.watch", seen[0], "after the watch, so its tick can't write the colour back")
        self.assertIn("beacon: ExampleCard set to off until Start", actions)
        self.assertIn("widget: on_quit failed: OSError: card gone", actions)
        self.assertEqual(self.loaded() & {f"athena-rex.{n}" for n in CORE}, set())

    def test_doctor_last_json_is_the_menus_contract_and_runs_nothing(self):
        out = json.loads(self.cli("doctor", "--last", "--json").stdout)
        self.assertEqual(out, {"ran": False, "ts": None, "age": None, "ok": None, "checks": []})
        self.assertEqual(self.cli("doctor", "--fix", "--quiet").returncode in (0, 1), True)
        written = json.loads(store.path("health.json").read_text())
        out = json.loads(self.cli("doctor", "--last", "--json").stdout)
        self.assertEqual(set(out), {"ran", "ts", "age", "ok", "checks"})
        self.assertTrue(out["ran"])
        self.assertEqual(out["ts"], written["ts"])
        self.assertEqual([c["name"] for c in out["checks"]], sorted(written["checks"]))
        self.assertEqual(out["ok"], written["ok"])
        for row in out["checks"]:
            self.assertEqual(set(row), {"name", "ok", "detail"})
        self.assertEqual(json.loads(store.path("health.json").read_text()), written, "--last reads it and runs nothing")

    def test_doctor_revives_neither_dash_nor_watch_while_athena_is_stopped(self):
        revived = []
        for module, name in ((dash, "on"), (watch, "_start_detached")):
            real = getattr(module, name)
            setattr(module, name, lambda *a, n=name: revived.append(n))
            self.addCleanup(setattr, module, name, real)
        store.write_json(store.path("service-rex.json"), {"jobs": ["doctor", "watch", "dash"]})
        services.install(caps.CTX)
        services.stop(caps.CTX)
        os.environ["ATHENA_DOCTOR_GRACE"] = "0"
        for now in (1000, 2000, 3000):
            doctor.run(caps.CTX, fix=True, now=now)
        self.assertEqual(revived, [], "a repair now would undo Quit")
        found = {c.name: c for c in [*dash.checks(caps.CTX), *watch.checks(caps.CTX)]}
        self.assertTrue(found["dashboard"].ok and found["watch"].ok)
        self.assertIsNone(found["watch"].repair)
        services.start(caps.CTX)
        self.assertFalse(dash.checks(caps.CTX)[0].ok, "once started, a dashboard that isn't there is doctor's again")
        self.assertIsNotNone(watch.checks(caps.CTX)[0].repair)

class LoginItem(FakeLaunchd):
    def test_services_installs_and_lists_the_menu_bar_app(self):
        out = self.cli("services", "install")
        self.assertEqual(out.returncode, 0, out.stderr)
        plist = plistlib.loads((self.tmp / "agents" / "athena-rex.menubar.plist").read_bytes())
        self.assertEqual(plist["ProgramArguments"][1:], ["menubar", "run"])
        self.assertEqual(plist["ProcessType"], "Interactive")
        self.assertEqual(plist["KeepAlive"], {"SuccessfulExit": False}, "Quit's own exit is not restarted")
        self.assertIn("athena-rex.menubar", self.loaded())
        listed = self.cli("services", "status").stdout
        self.assertRegex(listed, r"menubar\s+installed")
        self.assertIn("menubar", {r["name"] for r in json.loads(self.cli("services", "status", "--json").stdout)["services"]})

    def test_install_builds_the_app_when_it_is_missing_or_its_sources_changed(self):
        source = self.tmp / "checkout" / "menubar"
        shutil.copytree(ROOT / "menubar", source, ignore=shutil.ignore_patterns("build"))
        self.addCleanup(setattr, menubar, "SOURCE", menubar.SOURCE)
        menubar.SOURCE = source
        del os.environ["ATHENA_MENUBAR_APP"]
        self.assertTrue(menubar.stale())
        self.assertIn(f"build {menubar.app()}", "\n".join(services.install(caps.CTX, dry_run=True)))
        self.assertFalse(menubar.binary().exists(), "a dry run builds nothing")
        actions = services.install(caps.CTX)
        self.assertEqual(actions[0], f"built {source / 'build' / 'AthenaRexMenu.app'}")
        self.assertTrue(menubar.binary().is_file())
        self.assertFalse(menubar.stale())
        self.assertFalse(any(a.startswith("built") for a in services.install(caps.CTX)), "built once, not on every install")
        with open(source / "Sources" / "Model.swift", "a") as f:
            f.write("// changed\n")
        self.assertTrue(menubar.stale(), "a changed source means a new build")

    def test_no_swiftc_fails_loudly_and_installs_nothing(self):
        source = self.tmp / "checkout" / "menubar"
        shutil.copytree(ROOT / "menubar", source, ignore=shutil.ignore_patterns("build"))
        self.addCleanup(setattr, menubar, "SOURCE", menubar.SOURCE)
        menubar.SOURCE = source
        del os.environ["ATHENA_MENUBAR_APP"]
        os.environ["SWIFTC"] = str(self.tmp / "no-swiftc")
        with self.assertRaises(UserError) as caught:
            services.install(caps.CTX)
        self.assertIn("swiftc not found", str(caught.exception))
        self.assertFalse((source / "build").exists(), "swiftc is looked for before anything is made")
        self.assertFalse((self.tmp / "agents").exists() and list((self.tmp / "agents").iterdir()))
        self.assertEqual(self.launchctl_calls(), [])
        out = self.cli("services", "install", env={"ATHENA_MENUBAR_APP": str(self.tmp / "gone.app")})
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("gone.app", out.stderr)

    def test_a_failed_build_leaves_the_last_good_app_in_place(self):
        source = self.tmp / "checkout" / "menubar"
        shutil.copytree(ROOT / "menubar", source, ignore=shutil.ignore_patterns("build"))
        good = source / "build" / "AthenaRexMenu.app" / "Contents" / "MacOS" / "AthenaRexMenu"
        good.parent.mkdir(parents=True)
        good.write_text("the last good app")
        broken = self.tmp / "swiftc"
        broken.write_text('#!/bin/sh\n[ "$1" = --version ] && exit 0\necho "error: it broke" >&2\nexit 1\n')
        broken.chmod(0o755)
        done = subprocess.run([str(source / "build.sh")], capture_output=True, text=True, env={**os.environ, "SWIFTC": str(broken)})
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(good.read_text(), "the last good app")
        self.assertEqual(sorted(p.name for p in (source / "build").iterdir()), ["AthenaRexMenu.app"], "no half-built app left")

    def test_run_without_a_build_says_so_and_exits_clean(self):
        out = self.cli("menubar", "run", env={"ATHENA_MENUBAR_APP": str(self.tmp / "none.app")})
        self.assertEqual(out.returncode, 0)
        self.assertIn("menubar/build.sh", out.stderr)


def built_app() -> Path:
    """menubar/build.sh run once for this module, into the test run's own folder."""
    if not hasattr(built_app, "path"):
        out = Path(os.environ["TMPDIR"]) / "menubar-build"
        done = subprocess.run([str(ROOT / "menubar" / "build.sh"), str(out)], capture_output=True, text=True, timeout=600)
        built_app.log = done.stdout + done.stderr
        built_app.path = out / "AthenaRexMenu.app" if done.returncode == 0 else None
    return built_app.path


MODULES = [
    {"name": "clamshell", "title": "Clamshell", "public": True, "loaded": True, "state": "on",
     "detail": "lid closed keeps it awake on power", "actions": [{"id": "toggle", "label": "Turn off"}]},
    {"name": "beacon", "title": "Beacon", "public": False, "loaded": True, "state": "on", "detail": "card ExampleCard: green",
     "actions": [{"id": "toggle", "label": "Turn off"}, {"id": "test", "label": "Flash the card"}]},
    {"name": "widget", "title": "Widget", "public": False, "loaded": False, "state": "error",
     "detail": "won't import: ImportError: no widget", "actions": []},
]
CLAMSHELL_OFF = {**MODULES[0], "state": "off", "detail": "turned off", "actions": [{"id": "toggle", "label": "Turn on"}]}
SERVICES = {"stopped": False, "services": [
    {"name": "dash", "state": "running", "pid": 4101, "every": None, "installed": True, "module": False},
    {"name": "doctor", "state": "loaded", "pid": None, "every": 60, "installed": True, "module": False},
    {"name": "menubar", "state": "running", "pid": 4102, "every": None, "installed": True, "module": False},
    {"name": "watch", "state": "running", "pid": 4103, "every": None, "installed": True, "module": False}]}
DOCTOR = {"ran": True, "ts": 1000.0, "age": 20, "ok": True, "checks": [
    {"name": "dashboard", "ok": True, "detail": "http://127.0.0.1:2850/"},
    {"name": "server", "ok": True, "detail": "rex running"},
    {"name": "watch", "ok": True, "detail": "loop last came round 4s ago"}]}
DOCTOR_FAILING = {**DOCTOR, "ok": False, "checks": [
    DOCTOR["checks"][0], {"name": "disk", "ok": False, "detail": "3 GB free: `athena-rex disk` lists the caches that can go"},
    {"name": "watch", "ok": False, "detail": "loop last came round 400s ago; waiting out a 120s grace"}]}
WORKERS = [{"name": "rex-a", "state": "working"}, {"name": "rex-b", "state": "blocked"}, {"name": "rex-c", "state": "working"}]

FAKE_CLI = r"""#!/bin/sh
echo "$*" >> "$FAKE/calls.log"
case "$*" in
"modules --json") cat "$FAKE/modules.json" ;;
"modules action clamshell toggle")
	[ -f "$FAKE/fail" ] && { echo "athena-rex: clamshell: pmset needs an administrator: run it from Terminal" >&2; exit 2; }
	cat "$FAKE/clamshell-off.json" ;;
"modules action "*) cat "$FAKE/clamshell-off.json" ;;
"services status --json") cat "$FAKE/services.json" ;;
"status --json") cat "$FAKE/status.json" ;;
"doctor --last --json") cat "$FAKE/doctor.json" ;;
"doctor --fix --quiet") cp "$FAKE/doctor-after.json" "$FAKE/doctor.json"; exit 1 ;;
esac
exit 0
"""


class App(Case):
    def setUp(self):
        super().setUp()
        source = built_app()
        self.assertIsNotNone(source, getattr(built_app, "log", ""))
        shutil.copytree(source, self.tmp / "AthenaRexMenu.app", symlinks=True)
        self.app = self.tmp / "AthenaRexMenu.app" / "Contents" / "MacOS" / "AthenaRexMenu"
        self.fake = self.tmp / "fake-cli"
        self.fake.mkdir()
        for name, data in (("modules", MODULES), ("clamshell-off", CLAMSHELL_OFF), ("services", SERVICES), ("status", WORKERS),
                           ("doctor", DOCTOR), ("doctor-after", DOCTOR_FAILING)):
            (self.fake / f"{name}.json").write_text(json.dumps(data))
        cli = self.fake / "athena-rex"
        cli.write_text(FAKE_CLI)
        cli.chmod(0o755)

    def run_app(self, *args, cli=None, env=None):
        full = {**os.environ, "ATHENA_REX_CLI": str(cli or self.fake / "athena-rex"), "FAKE": str(self.fake), **(env or {})}
        return subprocess.run([str(self.app), *args], capture_output=True, text=True, env=full, timeout=120)

    def cli_calls(self) -> list:
        path = self.fake / "calls.log"
        return path.read_text().splitlines() if path.exists() else []


class Menu(App):
    def test_the_menu_shows_the_fleet_each_service_and_the_fixed_items(self):
        out = self.run_app("--dump-menu")
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.splitlines()
        self.assertEqual(lines[0], "(3 workers: 2 working, 1 needs you)")
        for line in ("✓ dash", "(✓ doctor)", "(✓ menubar)", "(✓ watch)",
                     "Open dashboard", "Open HQ", "Restart services", "Quit athena"):
            self.assertIn(line, lines)
        top = [line for line in lines if not line.startswith(" ")]  # the Doctor submenu keeps its own wording
        self.assertFalse(any("pid" in line or "running" in line or "loaded" in line for line in top), top)

    def test_each_part_is_a_mark_and_its_name_and_only_the_dash_row_is_clickable(self):
        """Green checks, no running or loaded or pids, and the dash row opens the dash."""
        broken = {**SERVICES, "services": [{**SERVICES["services"][0], "state": "exited 1"},
                                           {**SERVICES["services"][1], "state": "unknown"}, *SERVICES["services"][2:]]}
        (self.fake / "services.json").write_text(json.dumps(broken))
        lines = self.run_app("--dump-menu").stdout.splitlines()
        self.assertIn("✗ dash", lines, "a part that is not ok is red, and the dash row still opens the dash")
        self.assertIn("(! doctor)", lines, "one nobody can vouch for is amber")
        self.assertEqual([l for l in lines if l.lstrip("(").startswith(("✓", "✗", "!"))], ["✗ dash", "(! doctor)", "(✓ menubar)", "(✓ watch)"])
        (self.fake / "services.json").write_text(json.dumps(SERVICES))
        out = self.run_app("--click", "✓ dash")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("dash open", self.cli_calls())
        self.assertNotEqual(self.run_app("--click", "✓ watch").returncode, 0, "no other row is clickable")
        self.assertNotIn("Start athena", lines)
        self.assertIn("icon: needs-you", lines)

    def test_each_module_is_a_submenu_built_from_the_contract(self):
        lines = self.run_app("--dump-menu").stdout.splitlines()
        at = lines.index("Clamshell: on >")
        self.assertEqual(lines[at + 1:at + 5], ["  (On: lid closed keeps it awake on power)", "  ---", "  Turn off", "Beacon: on >"])
        at = lines.index("Beacon: on >")
        self.assertEqual(lines[at + 1:at + 5], ["  (On: card ExampleCard: green)", "  ---", "  Turn off", "  Flash the card"])
        at = lines.index("Widget: error >")
        self.assertEqual(lines[at + 1], "  (Error: won't import: ImportError: no widget)")

    def test_an_action_runs_modules_action_name_id_and_shows_the_new_object(self):
        out = self.run_app("--click", "Clamshell: on > Turn off")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("modules action clamshell toggle", self.cli_calls())
        lines = out.stdout.splitlines()
        at = lines.index("Clamshell: off >")
        self.assertEqual(lines[at + 1:at + 4], ["  (Off: turned off)", "  ---", "  Turn on"])
        self.run_app("--click", "Beacon: on > Flash the card")
        self.assertIn("modules action beacon test", self.cli_calls())

    def test_a_failed_action_shows_its_error_and_asks_for_nothing(self):
        (self.fake / "fail").write_text("")
        out = self.run_app("--click", "Clamshell: on > Turn off")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("error: Clamshell: athena-rex: clamshell: pmset needs an administrator: run it from Terminal", out.stdout)
        lines = out.stdout.splitlines()
        at = lines.index("Clamshell: on >")
        self.assertIn("  (athena-rex: clamshell: pmset needs an administrator: run it from Terminal)", lines[at:at + 4])
        self.assertNotIn("password", out.stdout.lower())

    def test_the_fixed_items_run_their_commands(self):
        for item, command in (("Open dashboard", "dash open"), ("Open HQ", "hq open"), ("Restart services", "services restart")):
            self.assertEqual(self.run_app("--click", item).returncode, 0)
            self.assertIn(command, self.cli_calls())

    def test_polls_never_stack_up_and_one_asked_meanwhile_still_runs(self):
        cli = self.fake / "athena-rex"
        cli.write_text(FAKE_CLI.replace('echo "$*" >> "$FAKE/calls.log"', 'echo "$*" >> "$FAKE/calls.log"\nsleep 0.3'))
        out = self.run_app("--refresh-burst", "20")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "polls: 2", "the first, then one for all asked while it ran")
        self.assertEqual(self.cli_calls().count("status --json"), 2)

    def test_doctor_is_a_submenu_of_its_last_pass_and_a_failure_turns_it_to_a_warning(self):
        lines = self.run_app("--dump-menu").stdout.splitlines()
        at = lines.index("Doctor: all ok >")
        self.assertEqual(lines[at + 1:at + 6], ["  (✓ dashboard: http://127.0.0.1:2850/)", "  (✓ server: rex running)",
                                                "  (✓ watch: loop last came round 4s ago)", "  ---", "  Run doctor --fix"])
        self.assertNotIn("doctor --fix --quiet", self.cli_calls(), "the poll reads its last pass and never runs it")
        (self.fake / "doctor.json").write_text(json.dumps(DOCTOR_FAILING))
        lines = self.run_app("--dump-menu").stdout.splitlines()
        at = lines.index("⚠ Doctor: 2 problems >")
        self.assertEqual(lines[at + 2:at + 4], ["  (✗ disk: 3 GB free: `athena-rex disk` lists the caches that can go)",
                                                "  (✗ watch: loop last came round 400s ago; waiting out a 120s grace)"])

    def test_run_doctor_fix_runs_it_through_the_cli_and_shows_what_it_found(self):
        out = self.run_app("--click", "Doctor: all ok > Run doctor --fix")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("doctor --fix --quiet", self.cli_calls())
        self.assertNotIn("error:", out.stdout, "its exit 1 means problems remain, which the rows show")
        self.assertIn("⚠ Doctor: 2 problems >", out.stdout.splitlines(), "read again right after, not 30 s later")

    def test_doctor_that_never_ran_or_stopped_reporting_is_a_warning(self):
        (self.fake / "doctor.json").write_text(json.dumps({**DOCTOR, "age": 900}))
        self.assertIn("⚠ Doctor: last ran 15 min ago >", self.run_app("--dump-menu").stdout.splitlines())
        (self.fake / "doctor.json").write_text(json.dumps({"ran": False, "ts": None, "age": None, "ok": None, "checks": []}))
        self.assertIn("⚠ Doctor: not run yet >", self.run_app("--dump-menu").stdout.splitlines())

    def test_stopped_shows_start_and_the_stopped_knot(self):
        (self.fake / "services.json").write_text(json.dumps({**SERVICES, "stopped": True}))
        lines = self.run_app("--dump-menu").stdout.splitlines()
        self.assertIn("Start athena", lines)
        self.assertNotIn("Quit athena", lines)
        for item in ("Restart services", "Open dashboard", "Open HQ", "✓ dash"):
            self.assertIn(f"({item})", lines, "stopped stays stopped: nothing here starts part of athena")
        at = lines.index("Doctor: stopped >")
        self.assertEqual(lines[at + 3], "  (Run doctor --fix)")
        self.assertIn("(athena is stopped)", lines)
        self.assertIn("icon: stopped", lines)

    def test_no_modules_command_yet_is_said_and_the_rest_still_works(self):
        (self.fake / "modules.json").write_text("usage: athena-rex <command>")
        out = self.run_app("--dump-menu")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("(Modules: unavailable)", out.stdout.splitlines())
        self.assertIn("Quit athena", out.stdout.splitlines())

    def test_the_icon_follows_the_fleet(self):
        (self.fake / "status.json").write_text(json.dumps([{"name": "rex-a", "state": "working"}]))
        self.assertIn("icon: working", self.run_app("--dump-menu").stdout.splitlines())
        (self.fake / "status.json").write_text("[]")
        lines = self.run_app("--dump-menu").stdout.splitlines()
        self.assertIn("icon: idle", lines)
        self.assertEqual(lines[0], "(no workers)")


class Knot(App):
    def test_the_four_glyphs_ship_and_render_and_working_moves(self):
        self.assertEqual(sorted(p.name for p in (ROOT / "menubar" / "glyphs").iterdir()),
                         ["knot-idle.svg", "knot-needs-you.svg", "knot-stopped.svg", "knot-working.svg"])
        pngs = {}
        for state, t in (("idle", 0), ("needs-you", 0), ("stopped", 0), ("working", 0), ("working", 0.25), ("working", 0.5)):
            target = self.tmp / f"{state}-{t}.png"
            out = self.run_app("--render", state, str(t), str(target))
            self.assertEqual(out.returncode, 0, out.stderr)
            pngs[(state, t)] = target.read_bytes()
            self.assertTrue(pngs[(state, t)].startswith(b"\x89PNG"))
        self.assertEqual(len({pngs[k] for k in pngs}), len(pngs), "every state looks different, and working moves")
        out = self.run_app("--dash", "0.25")
        self.assertEqual(len(out.stdout.splitlines()), 3, "one gap per strand")

    def test_every_glyph_sits_inside_the_bar_with_a_system_icons_padding(self):
        """The knot was a little too big, with less padding than the other menu bar icons. Every state's ink,
        badge and all, fits 16 pt of the 22 pt bar, with at least 2.5 pt clear on every side (the needs-you badge's corner)."""
        for state, t in (("idle", 0), ("needs-you", 0), ("stopped", 0), ("working", 0), ("working", 0.4), ("working", 0.8)):
            target = self.tmp / f"{state}-{t}.png"
            self.assertEqual(self.run_app("--render", state, str(t), str(target)).returncode, 0)
            left, top, right, bottom = ink_box(target.read_bytes())
            with self.subTest(state=state, t=t):
                self.assertLessEqual(max(right - left, bottom - top), 16, (left, top, right, bottom))
                self.assertGreaterEqual(min(left, top, 22 - right, 22 - bottom), 2.5, (left, top, right, bottom))


def ink_box(png):
    """The box, in points of the 22 pt glyph, of the pixels that differ from the bar's: --render's 8-bit RGBA PNG, read with zlib."""
    at, chunks, width, height = 8, b"", 0, 0
    while at < len(png):
        size, kind = int.from_bytes(png[at:at + 4], "big"), png[at + 4:at + 8]
        body = png[at + 8:at + 8 + size]
        if kind == b"IHDR":
            width, height = int.from_bytes(body[:4], "big"), int.from_bytes(body[4:8], "big")
            assert body[8:10] == bytes([8, 6]) and body[12] == 0, "8-bit RGBA, not interlaced"
        elif kind == b"IDAT":
            chunks += body
        at += 12 + size
    raw, stride, rows, previous = zlib.decompress(chunks), width * 4, [], bytes(width * 4)
    for y in range(height):
        kind, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - 4] if i >= 4 else 0
            b, c = previous[i], previous[i - 4] if i >= 4 else 0
            if kind == 1:
                line[i] = (line[i] + a) & 255
            elif kind == 2:
                line[i] = (line[i] + b) & 255
            elif kind == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif kind == 4:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(bytes(line))
        previous = rows[-1]
    bar = rows[0][:3]
    inked = [(x, y) for y, row in enumerate(rows) for x in range(width) if max(abs(row[x * 4 + k] - bar[k]) for k in range(3)) > 40]
    zoom = width / 22
    return (min(x for x, _ in inked) / zoom, min(y for _, y in inked) / zoom,
            (max(x for x, _ in inked) + 1) / zoom, (max(y for _, y in inked) + 1) / zoom)


class EndToEnd(App, FakeLaunchd):
    """The app's Quit and Start against the real CLI, with launchd faked."""

    def test_quit_athena_stops_everything_in_order_then_the_app_and_start_brings_it_back(self):
        self.install_with_cli()
        out = self.run_app("--click", "Quit athena", cli=BIN)
        self.assertEqual(out.returncode, 0, out.stderr)
        after = out.stdout.splitlines()
        self.assertIn("Start athena", after, "the app stays, with athena stopped and Start a click away")
        self.assertEqual(after[-1], "icon: stopped")
        calls = self.launchctl_calls()
        stops = [c for c in calls if c.split()[0] in ("disable", "bootout")]
        self.assertEqual(stops[:2], [f"disable gui/{UID}/athena-rex.doctor", f"bootout gui/{UID}/athena-rex.doctor"])
        self.assertFalse(any(c.split()[0] in ("bootstrap", "kickstart", "enable") for c in calls))
        self.assertEqual(self.loaded(), {"athena-rex.menubar", "athena-rex.beacon"})
        self.assertTrue(caps.CTX.backend.up(), "Rex and its sessions run on")
        self.calls.write_text("")
        out = self.run_app("--click", "Start athena", cli=BIN)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Quit athena", out.stdout.splitlines())
        self.assertEqual(self.loaded(), {f"athena-rex.{n}" for n in (*CORE, "menubar")} | {"athena-rex.beacon"})

    def install_with_cli(self):
        self.assertEqual(self.cli("services", "install").returncode, 0)
        with open(f"{self.calls}.launchd", "a") as db:
            db.write("athena-rex.beacon\n")  # a module's job, which no core capability names
        self.calls.write_text("")
        store.ensure()
