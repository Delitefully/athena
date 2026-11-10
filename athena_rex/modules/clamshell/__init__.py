"""Staying awake with the lid closed: only while HQ runs and the Mac is on good AC power, and asleep on battery with the lid
closed. Any fact it cannot read counts against staying awake.

decide() is pure. The privileged actuator stays what it is: the root daemon that reads `hq_alive` from health.json, which
doctor keeps writing. This module adds the decision anyone can inspect, and caffeinate while a worker is mid-turn on AC.

A public module, off by default: `athena-rex modules load clamshell` lists it in config's `rex.modules`.
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

from athena_rex.kernel import caps, store, world

SUMMARY = "whether this Mac stays awake with its lid closed, and why"
CHARGE_FLOOR = 20
HEARTBEAT_MAX_AGE = 180
CAFFEINATE_SECONDS = 180
DAEMON = "athena-clamshell"
DAEMON_GRACE = 180
HEARTBEAT_STALE = 240  # the daemon calls a heartbeat stale at 180 s and looks every 30 s: past this, its safety failed
SAFETY_HINT = "the daemon's safety failed; clear it with sudo pmset -a disablesleep 0, then look at /var/log/athena-clamshell.log"


def parse_power(text: str) -> tuple:
    """(good, why). Good AC is `AC Power` at or above the floor; discharging on a real charger still counts."""
    lines = text.splitlines()
    source = next((line for line in lines if "Now drawing from" in line), None)
    if source is None:
        return False, "no power source"
    if "'Battery Power'" in source:
        return False, "on battery"
    if "'AC Power'" not in source:
        return False, "unrecognised power source"
    battery = next((line for line in lines if line.strip().startswith("-InternalBattery-")), None)
    if battery is None:
        return False, "no internal battery line"
    try:
        percent = int(battery.split(";")[0].split()[-1].rstrip("%"))
    except (ValueError, IndexError):
        return False, "no battery percent"
    if percent < CHARGE_FLOOR:
        return False, f"on AC under the {CHARGE_FLOOR}% floor"
    return True, "good AC"


def parse_lid(text: str):
    for line in text.splitlines():
        if '"AppleClamshellState"' in line:
            value = line.split("=")[-1].strip()
            return {"Yes": True, "No": False}.get(value)
    return None


def parse_sleep_disabled(text):
    found = re.search(r"^\s*SleepDisabled\s+([01])\s*$", text or "", re.M)
    return found.group(1) == "1" if found else None


def safety_failure(batt_text, health, now):
    """Why SleepDisabled must not be 1 now, or None: on battery, power unreadable, or a heartbeat the daemon should have
    stopped trusting. health.json holds the last pass's: this pass's is written after the checks."""
    on_battery = True if "'Battery Power'" in batt_text else False if "'AC Power'" in batt_text else None
    if on_battery is not False:
        return "while on battery" if on_battery else "with the power state unreadable"
    ts = (health or {}).get("ts")
    fresh = isinstance(ts, (int, float)) and not isinstance(ts, bool) and 0 <= now - ts <= HEARTBEAT_STALE
    return None if fresh else "with a stale heartbeat"


def daemon_plist():
    """The root daemon's plist: a stand-in folder if named, the real /Library/LaunchDaemons only with the door open."""
    named = os.environ.get("ATHENA_LAUNCH_DAEMONS")
    if named and not world._is_real_place(named):
        return Path(named) / f"{DAEMON}.plist"
    return Path("/Library/LaunchDaemons") / f"{DAEMON}.plist" if world.enabled() else None


def daemon(now) -> tuple:
    """(ok, words). Not installed is no failure: the Mac sleeps with its lid closed as usual. Down past the grace is."""
    plist = daemon_plist()
    if plist is None:
        return True, "daemon not read here"
    if not plist.exists():
        return True, ("daemon not installed, the Mac sleeps with its lid closed as usual; to keep it awake on power, "
                      "install athena's local/clamshell (sudo install.sh)")
    out = _read("launchctl", "print", f"system/{DAEMON}")
    pid = re.search(r"^\s*pid = (\d+)$", out, re.M) if re.search(r"^\s*state = running$", out, re.M) else None
    memory = store.path("run", "clamshell-down.json")
    if pid:
        memory.unlink(missing_ok=True)
        return True, f"daemon running (pid {pid.group(1)})"
    since = float(store.read_dict(memory).get("since") or now)
    store.write_json(memory, {"since": since})
    return now - since < DAEMON_GRACE, f"daemon not running for {int(now - since)} s"


def decide(power_good, power_why, lid_closed, heartbeat, now) -> dict:
    """{awake, sleep_now, why}: `awake` is whether sleep stays disabled, `sleep_now` whether to sleep this Mac at once."""
    fresh = bool(heartbeat) and now - float(heartbeat.get("ts") or 0) < HEARTBEAT_MAX_AGE
    alive = fresh and heartbeat.get("hq_alive") is True
    if not power_good:
        return {"awake": False, "sleep_now": lid_closed is True, "why": power_why}
    if not fresh:
        return {"awake": False, "sleep_now": False, "why": "no fresh heartbeat from doctor"}
    if not alive:
        return {"awake": False, "sleep_now": False, "why": "HQ is not running"}
    return {"awake": True, "sleep_now": False, "why": "HQ is running on good AC"}


def _read(tool, *args):
    try:
        return world.run(tool, *args, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def current(now) -> dict:
    power_good, why = parse_power(_read("pmset", "-g", "batt"))
    lid = parse_lid(_read("ioreg", "-r", "-k", "AppleClamshellState", "-d", "1"))
    return {**decide(power_good, why, lid, store.read_json(store.path("health.json"), None), now), "lid_closed": lid}


def _any_working() -> bool:
    from athena_rex.caps.status import collect
    return any(r["state"] == "working" for r in collect())


def caffeinate() -> str:
    """While a worker is mid-turn on AC: renewed each pass, so nothing holds the Mac awake once this stops running."""
    if not parse_power(_read("pmset", "-g", "batt"))[0]:
        return "not on good AC"
    if not _any_working():
        return "nobody working"
    world.start("caffeinate", "-i", "-t", str(CAFFEINATE_SECONDS))
    return "caffeinated"


def checks(ctx):
    if not world.available("pmset"):
        return [caps.Check("clamshell", True, "not read here")]
    now = time.time()
    disabled = parse_sleep_disabled(_read("pmset", "-g"))
    flag = {True: "SleepDisabled 1", False: "SleepDisabled 0", None: "SleepDisabled unreadable"}[disabled]
    failure = safety_failure(_read("pmset", "-g", "batt"), store.read_json(store.path("health.json"), None), now) if disabled else None
    if failure:
        first = caps.Check("clamshell", False, f"{flag} {failure}: {SAFETY_HINT}")
    else:
        state = current(now)
        ok, words = daemon(now)
        first = caps.Check("clamshell", ok, f"{words}, {flag}; awake={state['awake']}: {state['why']}")
    return [first, caps.Check("sleep", True, caffeinate())]


def status(ctx) -> tuple:
    """For the menu, from doctor's last look in health.json: a menu poll runs no power tool."""
    found = (store.read_dict(store.path("health.json")).get("checks") or {}).get("clamshell")
    if not isinstance(found, dict):
        return True, "doctor hasn't looked yet"
    return bool(found.get("ok", True)), str(found.get("detail") or "")


def _configure(p):
    p.add_argument("action", nargs="?", choices=["status"], default="status")


def _run(args):
    print(json.dumps(current(time.time()), indent=1))
    return 0


def commands():
    return {"clamshell": caps.Command(SUMMARY, _configure, _run)}
