"""Optional modules: the loader, `athena-rex modules` as the menu bar's contract, and clamshell as a public module."""
import json
import os
import subprocess
import time

from harness import ROOT, Case

from athena_rex.caps import doctor
from athena_rex.kernel import caps, config, modules, store

KEYS = ["name", "title", "public", "loaded", "state", "detail", "actions"]
BATT = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t80%; charging"

TOY = '''"""A private module for tests: records start and stop, and has one action of its own."""
from athena_rex.kernel import caps, store

SUMMARY = "a toy"


def _note(what):
    store.append_line(store.path("toy-calls.jsonl"), {"call": what})


def start(ctx):
    _note("start")


def stop(ctx):
    _note("stop")


def status(ctx):
    return not store.path("toy-broken").exists(), "toy is fine"


def actions(ctx):
    return [{"id": "poke", "label": "Poke it"}]


def action(ctx, action_id):
    _note(action_id)


def checks(ctx):
    return [caps.Check("toy", True, "toy line")]


def tick(ctx, now):
    _note("tick")
    return []


def _run(args):
    print("toy ran")
    return 0


def commands():
    return {"toy": caps.Command(SUMMARY, lambda p: None, _run)}
'''


class _Modules(Case):
    def setUp(self):
        super().setUp()
        self.private = self.tmp / "private-modules"
        self.private.mkdir()

    def private_module(self, name="toy", code=TOY, title="Toy"):
        folder = self.private / name
        folder.mkdir()
        (folder / "manifest.json").write_text(json.dumps({"name": name, "title": title}))
        (folder / "__init__.py").write_text(code)
        return folder

    def configure(self, listed, **rex):
        self.write_config(rex={"modules": listed, "modules_dir": str(self.private), **rex})

    def toy_calls(self):
        return [line["call"] for line in store.read_lines(store.path("toy-calls.jsonl"))]

    def run_json(self, *args):
        out = self.cli("modules", *args)
        try:
            return out.returncode, json.loads(out.stdout), out.stderr
        except ValueError:
            return out.returncode, None, out.stderr + out.stdout

    def loaded_after(self, *argv):
        code = ("import sys; sys.path.insert(0, %r)\nfrom athena_rex.cli import main\nmain(%r)\n"
                "print('MODULES ' + ' '.join(sorted(sys.modules)))" % (str(ROOT), list(argv)))
        out = subprocess.run(["/usr/bin/python3", "-I", "-B", "-c", code], capture_output=True, text=True, env=os.environ)
        line = [x for x in out.stdout.splitlines() if x.startswith("MODULES ")]
        self.assertTrue(line, out.stderr)
        return set(line[-1].split()[1:])


class NoModules(_Modules):
    def test_an_empty_list_runs_the_core_with_no_module_code_loaded(self):
        self.private_module()
        for listed in ([], None):
            if listed is None:
                (self.state / "config.json").unlink()
                config.reset()
            else:
                self.configure([])
            for argv in (["--help"], ["status"], ["doctor", "--json"], ["modules", "--json"], ["watch", "--once"]):
                names = self.loaded_after(*argv)
                strays = {n for n in names if n.startswith(modules.PACKAGE) or "clamshell" in n or "beacon" in n or "toy" in n}
                self.assertEqual(strays, set(), argv)
            self.assertEqual(self.cli("clamshell").returncode, 2)
            self.assertEqual(self.cli("toy").returncode, 2)
            self.assertEqual(json.loads(self.cli("modules", "--json").stdout), [])
        self.assertEqual(modules.active(), [])
        self.assertFalse(any(m.__name__.startswith(modules.PACKAGE) for m in caps.loaded()))

    def test_the_default_private_folder_is_never_made(self):
        self.write_config(rex={"modules": ["toy"]})
        out = self.cli("modules", "--json")
        self.assertEqual(json.loads(out.stdout)[0]["state"], "error")
        self.assertFalse((self.home / ".config" / "athena-rex").exists())

    def test_the_core_capability_filter_is_rex_caps_now(self):
        self.write_config(rex={"caps": ["status", "modules"]})
        self.assertEqual(self.cli("status").returncode, 0)
        self.assertEqual(self.cli("doctor").returncode, 2)
        self.assertEqual(self.cli("modules", "--json").returncode, 0)


class Clamshell(_Modules):
    def test_it_ships_public_and_off(self):
        self.assertIn("clamshell", modules.public_names())
        self.assertFalse((ROOT / "athena_rex" / "caps" / "clamshell.py").exists())
        self.assertIsNone(caps.get("clamshell"))
        self.assertNotIn("clamshell", doctor.run(caps.CTX, fix=False, now=time.time())["checks"])

    def test_listed_it_runs_as_before(self):
        self.stub("pmset", BATT)
        self.configure(["clamshell"])
        clamshell = caps.get("clamshell")
        self.assertTrue(clamshell.PUBLIC)
        health = doctor.run(caps.CTX, fix=False, now=time.time())
        self.assertIn("clamshell", health["checks"])
        self.assertIn("awake=", health["checks"]["clamshell"]["detail"])
        self.assertIn("sleep", health["checks"])
        self.assertTrue(any(c.startswith("pmset -g batt") for c in self.world_calls()))
        out = self.cli("clamshell")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(set(json.loads(out.stdout)), {"awake", "sleep_now", "why", "lid_closed"})
        self.assertIn("clamshell", self.cli("--help").stdout)

    def test_its_menu_line_comes_from_doctors_last_look(self):
        self.configure(["clamshell"])
        code, rows, _ = self.run_json("--json")
        self.assertEqual(rows, [{"name": "clamshell", "title": "Clamshell", "public": True, "loaded": True, "state": "on",
                                 "detail": "doctor hasn't looked yet", "actions": [{"id": "toggle", "label": "Turn off"}]}])
        store.write_json(store.path("health.json"), {"ts": 1, "ok": True, "problems": [],
                                                     "checks": {"clamshell": {"ok": True, "detail": "awake=True: HQ is running on good AC"}}})
        self.assertEqual(self.run_json("--json")[1][0]["detail"], "awake=True: HQ is running on good AC")
        self.assertEqual(self.world_calls(), [])


class ClamshellDoctor(_Modules):
    """The audit's clamshell doctor rows: the safety check, the root daemon's job, and hq_alive only when HQ is confirmed."""

    def setUp(self):
        super().setUp()
        self.configure(["clamshell"])
        self.daemons = self.tmp / "daemons"
        self.daemons.mkdir()
        os.environ["ATHENA_LAUNCH_DAEMONS"] = str(self.daemons)

    def line(self):
        return {c.name: c for c in caps.get("clamshell").checks(caps.CTX)}["clamshell"]

    def test_sleep_disabled_on_battery_or_a_stale_heartbeat_is_the_daemons_safety_failing(self):
        self.stub("pmset", "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t80%; discharging\n SleepDisabled\t\t1")
        check = self.line()
        self.assertFalse(check.ok)
        self.assertIn("SleepDisabled 1 while on battery", check.detail)
        self.assertIn("sudo pmset -a disablesleep 0", check.detail)
        self.stub("pmset", BATT + "\n SleepDisabled\t\t1")
        store.write_json(store.path("health.json"), {"ts": time.time() - 600, "ok": True, "problems": []})
        self.assertIn("with a stale heartbeat", self.line().detail)
        store.write_json(store.path("health.json"), {"ts": time.time(), "ok": True, "problems": []})
        self.assertTrue(self.line().ok)
        self.stub("pmset", "Now drawing from 'Battery Power'\n SleepDisabled\t\t0")
        self.assertTrue(self.line().ok)
        self.assertEqual([c for c in self.world_calls() if "disablesleep" in c], [])

    def test_the_daemon_not_installed_is_fine_and_down_past_its_grace_is_a_problem(self):
        self.stub("pmset", BATT + "\n SleepDisabled\t\t0")
        self.assertIn("daemon not installed", self.line().detail)
        self.assertTrue(self.line().ok)
        (self.daemons / "athena-clamshell.plist").write_text("<plist/>")
        self.stub("launchctl", "system/athena-clamshell = {\n\tstate = running\n\tpid = 42\n}")
        self.assertIn("daemon running (pid 42)", self.line().detail)
        self.assertIn("launchctl print system/athena-clamshell", self.world_calls())
        self.stub("launchctl", "")
        self.assertTrue(self.line().ok, "inside its grace")
        store.write_json(store.path("run", "clamshell-down.json"), {"since": time.time() - 200})
        check = self.line()
        self.assertFalse(check.ok)
        self.assertIn("daemon not running", check.detail)

    def test_the_daemon_is_never_looked_for_with_the_door_shut(self):
        os.environ.pop("ATHENA_LAUNCH_DAEMONS")
        self.stub("pmset", BATT + "\n SleepDisabled\t\t0")
        self.assertIn("daemon not read here", self.line().detail)
        self.assertFalse(any(c.startswith("launchctl") for c in self.world_calls()))

    def test_hq_alive_is_written_true_only_when_hq_is_confirmed_alive(self):
        from unittest import mock
        from athena_rex.kernel import agents
        hq = caps.get("hq")
        with mock.patch.object(hq, "alive", side_effect=agents.Unknown("can't tell")):
            self.assertIs(doctor.run(caps.CTX, fix=False, now=time.time())["hq_alive"], False)
        with mock.patch.object(hq, "alive", return_value=True):
            self.assertIs(doctor.run(caps.CTX, fix=False, now=time.time())["hq_alive"], True)


class Contract(_Modules):
    def test_json_is_one_object_per_listed_module_loaded_or_not(self):
        self.private_module()
        self.configure(["clamshell", "toy", "nowhere", "../x"])
        code, rows, err = self.run_json("--json")
        self.assertEqual(code, 0, err)
        self.assertEqual([r["name"] for r in rows], ["clamshell", "toy", "nowhere", "../x"])
        for row in rows:
            self.assertEqual(list(row), KEYS)
            self.assertIn(row["state"], ("on", "off", "error"))
            self.assertNotIn("\n", row["detail"])
        toy = rows[1]
        self.assertEqual(toy, {"name": "toy", "title": "Toy", "public": False, "loaded": True, "state": "on", "detail": "toy is fine",
                               "actions": [{"id": "toggle", "label": "Turn off"}, {"id": "poke", "label": "Poke it"}]})
        for missing in rows[2:]:
            self.assertEqual((missing["loaded"], missing["state"], missing["actions"]), (False, "error", []))
        self.assertIn("no module nowhere", rows[2]["detail"])

    def test_toggle_keeps_it_listed_and_turns_it_off_then_on(self):
        self.private_module()
        self.configure(["toy"])
        code, off, err = self.run_json("action", "toy", "toggle")
        self.assertEqual(code, 0, err)
        self.assertEqual((off["loaded"], off["state"], off["actions"]), (True, "off", [{"id": "toggle", "label": "Turn on"}]))
        self.assertEqual(self.toy_calls(), ["stop"])
        config.reset()
        self.assertEqual(modules.listed(), ["toy"])
        self.assertIsNone(caps.get("toy"))
        self.assertEqual(self.cli("toy").returncode, 2)
        self.assertNotIn("toy", doctor.run(caps.CTX, fix=False, now=time.time())["checks"])
        code, on, _ = self.run_json("action", "toy", "toggle")
        self.assertEqual((on["state"], on["actions"][0]["label"]), ("on", "Turn off"))
        self.assertEqual(self.toy_calls(), ["stop", "start"])
        self.assertEqual(self.cli("toy").stdout.strip(), "toy ran")

    def test_a_modules_own_action_runs_and_prints_the_new_object(self):
        self.private_module()
        self.configure(["toy"])
        code, out, err = self.run_json("action", "toy", "poke")
        self.assertEqual((code, out["state"]), (0, "on"), err)
        self.assertEqual(self.toy_calls(), ["poke"])
        self.assertEqual(self.cli("modules", "action", "toy", "nope").returncode, 1)
        self.assertEqual(self.cli("modules", "action", "clamshell", "toggle").returncode, 1)
        self.run_json("action", "toy", "toggle")
        self.assertEqual(self.cli("modules", "action", "toy", "poke").returncode, 1)

    def test_load_adds_it_to_the_config_list_and_starts_it(self):
        self.private_module()
        self.write_config(model="sonnet", rex={"modules_dir": str(self.private), "dash_port": 2999})
        code, out, err = self.run_json("load", "toy")
        self.assertEqual(code, 0, err)
        self.assertEqual((out["name"], out["loaded"], out["state"]), ("toy", True, "on"))
        self.assertEqual(self.toy_calls(), ["start"])
        saved = json.loads((self.state / "config.json").read_text())
        self.assertEqual((saved["model"], saved["rex"]["dash_port"], saved["rex"]["modules"]), ("sonnet", 2999, ["toy"]))
        self.assertEqual(self.run_json("load", "clamshell")[1]["public"], True)
        self.assertEqual(json.loads((self.state / "config.json").read_text())["rex"]["modules"], ["toy", "clamshell"])
        self.run_json("load", "toy")
        self.assertEqual(json.loads((self.state / "config.json").read_text())["rex"]["modules"], ["toy", "clamshell"])

    def test_load_refuses_what_it_cannot_find_or_import(self):
        self.private_module("bad", code="raise RuntimeError('nope')\n")
        self.configure([])
        for name in ("nowhere", "bad", "../../etc"):
            out = self.cli("modules", "load", name)
            self.assertEqual(out.returncode, 1, name)
        self.assertEqual(json.loads((self.state / "config.json").read_text())["rex"]["modules"], [])

    def test_unload_removes_it_from_the_list_and_stops_it(self):
        self.private_module()
        self.configure(["toy", "clamshell"])
        code, out, err = self.run_json("unload", "toy")
        self.assertEqual(code, 0, err)
        self.assertEqual((out["name"], out["loaded"], out["state"], out["actions"]), ("toy", False, "off", []))
        self.assertEqual(self.toy_calls(), ["stop"])
        self.assertEqual(json.loads((self.state / "config.json").read_text())["rex"]["modules"], ["clamshell"])
        self.assertEqual(self.cli("modules", "unload", "toy").returncode, 1)
        self.assertEqual(self.cli("toy").returncode, 2)

    def test_a_damaged_config_is_never_written_over(self):
        self.private_module()
        (self.state / "config.json").write_text("{not json")
        config.reset()
        self.assertEqual(self.cli("modules", "load", "clamshell").returncode, 1)
        self.assertEqual((self.state / "config.json").read_text(), "{not json")


class Loader(_Modules):
    def test_a_broken_module_is_an_error_the_core_runs_past_and_doctor_names(self):
        self.private_module("bad", code="raise RuntimeError('nope')\n")
        self.private_module()
        self.configure(["bad", "toy"])
        self.assertEqual([name for name, _ in modules.active()], ["toy"])
        health = doctor.run(caps.CTX, fix=False, now=time.time())
        self.assertFalse(health["checks"]["module:bad"]["ok"])
        self.assertIn("RuntimeError: nope", health["checks"]["module:bad"]["detail"])
        self.assertEqual(health["checks"]["toy"]["detail"], "toy line")
        self.assertEqual(self.cli("status").returncode, 0)
        row = json.loads(self.cli("modules", "--json").stdout)[0]
        self.assertEqual((row["loaded"], row["state"]), (False, "error"))

    def test_a_status_that_says_not_ok_reads_as_error(self):
        self.private_module()
        self.configure(["toy"])
        store.path("toy-broken").write_text("")
        self.assertEqual(json.loads(self.cli("modules", "--json").stdout)[0]["state"], "error")

    def test_a_name_in_both_places_is_an_error_not_a_quiet_pick(self):
        self.private_module("clamshell", title="Mine")
        self.configure(["clamshell"])
        row = json.loads(self.cli("modules", "--json").stdout)[0]
        self.assertEqual(row["state"], "error")
        self.assertIn("named twice", row["detail"])

    def test_a_module_reaches_the_watch_and_a_running_process_sees_load_and_unload(self):
        from athena_rex.caps import watch
        self.private_module()
        self.configure([])
        self.assertIsNone(caps.get("toy"))
        config.load()
        time.sleep(0.01)
        self.assertEqual(self.cli("modules", "load", "toy").returncode, 0)
        self.assertIsNotNone(caps.get("toy"))
        watch.step(caps.CTX, {}, 0, watch.Dedupe(None))
        self.assertEqual(self.toy_calls(), ["start", "tick"])
        time.sleep(0.01)
        self.assertEqual(self.cli("modules", "unload", "toy").returncode, 0)
        self.assertIsNone(caps.get("toy"))
        watch.step(caps.CTX, {}, 0, watch.Dedupe(None))
        self.assertEqual(self.toy_calls(), ["start", "tick", "stop"])

    def test_a_modules_files_import_each_other(self):
        folder = self.private_module(code="from .inner import VALUE\n")
        (folder / "inner.py").write_text("VALUE = 7\n")
        self.configure(["toy"])
        self.assertEqual(caps.get("toy").VALUE, 7)
