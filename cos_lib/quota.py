"""Launch gate on the Claude 5-hour quota, read from the herdr-agent-quota plugin's state file."""
import json
import time

from cos_lib import paths

STALE_SECONDS = 30 * 60


def five_hour_used(path=None):
    """Percent of the 5-hour window used, or None when the data is missing or older than 30 minutes."""
    try:
        with open(path or paths.quota_file()) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if time.time() - float(data.get("fetched_at_unix") or 0) > STALE_SECONDS:
        return None
    for window in data.get("windows") or []:
        if window.get("kind") == "five_hour":
            return float(window.get("used_percent"))
    return None


def gate(threshold=85):
    used = five_hour_used()
    if used is None:
        return True, "quota unknown (no fresh data); not gating"
    if used >= threshold:
        return False, f"5-hour quota {used:.0f}% used, at or above {threshold}%"
    return True, f"5-hour quota {used:.0f}% used"
