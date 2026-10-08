"""The herdr CLI, called as a subprocess. Every call returns the parsed `result` object."""
import json
import shlex
import subprocess

from cos_lib import paths


class HerdrError(Exception):
    def __init__(self, code, message, argv=None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.argv = argv


def _parse_error(text):
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        return data["error"]
    return None


def argv(*args) -> list:
    return shlex.split(paths.herdr_bin()) + [str(a) for a in args]


def call(*args, timeout=180) -> dict:
    cmd = argv(*args)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HerdrError("cli_timeout", f"herdr did not answer in {timeout}s", cmd)
    out, err = proc.stdout.strip(), proc.stderr.strip()
    for text in (err, out):
        error = _parse_error(text) if text else None
        if error:
            raise HerdrError(error.get("code", "error"), error.get("message", ""), cmd)
    if proc.returncode != 0:
        raise HerdrError("exit_%d" % proc.returncode, err or out, cmd)
    if not out:
        return {}
    try:
        data = json.loads(out)
    except ValueError:
        return {"text": out}
    return data.get("result", data) if isinstance(data, dict) else {"value": data}


def text(*args, timeout=60) -> str:
    """For commands that print plain text (pane read)."""
    proc = subprocess.run(argv(*args), capture_output=True, text=True, timeout=timeout)
    return proc.stdout
