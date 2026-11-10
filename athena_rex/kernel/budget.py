"""The machine budget: nothing launches into a Mac that is out of memory, disk or CPU. A fleet that only counts
itself can take the whole machine down. Anything unreadable counts as room: this holds launches, it is not a safety interlock.
"""
import os
import shutil
import subprocess

MEMORY_FLOOR = 25
DISK_FLOOR_GB = 10
LOAD_PER_CPU = 3.0


def memory_free():
    try:
        out = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if "free percentage" in line:
            try:
                return int(line.rsplit(" ", 1)[-1].rstrip("%"))
            except ValueError:
                return None
    return None


def readings() -> dict:
    fake = os.environ.get("ATHENA_BUDGET_FAKE")
    if fake:
        memory, disk, load = fake.split(",")
        return {"memory": int(memory), "disk_gb": int(disk), "load": float(load), "cpus": 8}
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = 0.0
    return {"memory": memory_free(), "disk_gb": shutil.disk_usage(os.path.expanduser("~")).free // (1 << 30),
            "load": load, "cpus": os.cpu_count() or 1}


def strained(now=None) -> str:
    """Why repairs should wait. Under this load a slow answer looks like a dead one, and a repair only adds to it."""
    r = now or readings()
    return f"load {r['load']:.0f} on {r['cpus']} cores" if r["load"] > LOAD_PER_CPU * r["cpus"] else ""


def over(cost: int = 1, now=None) -> list:
    """Why a launch of this cost should wait; an empty list means go. An ultracode worker costs more than one."""
    r = now or readings()
    reasons = []
    floor = MEMORY_FLOOR + 10 * (max(cost, 1) - 1)
    if r["memory"] is not None and r["memory"] < floor:
        reasons.append(f"{r['memory']}% memory free, under {floor}%")
    if r["disk_gb"] < DISK_FLOOR_GB:
        reasons.append(f"{r['disk_gb']} GB disk free, under {DISK_FLOOR_GB} GB")
    if r["load"] > LOAD_PER_CPU * r["cpus"]:
        reasons.append(f"load {r['load']:.0f} on {r['cpus']} cores")
    return reasons
