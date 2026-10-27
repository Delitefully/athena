"""What sits in a Claude session's input box: the text someone typed there and has not sent yet.

Claude Code draws its input box between two horizontal rules, the first line starting with `❯`. With nothing typed
it shows a placeholder in dim (SGR 2), which a plain-text read cannot tell from typed text, so the screen is read
with its ANSI styling (`herdr agent read --format ansi`) and dim spans are dropped. athena never types into a pane
whose box holds text: an `/exit` or a prompt sent then joins the human's half-typed message.
"""
import os
import re
import subprocess
import time

from athena_lib import herdr

_SGR = re.compile(r"\x1b\[[0-9;:]*m")
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_PROMPT = ("❯", ">")


def _plain(line) -> str:
    return _ANSI.sub("", line)


def _undimmed(line) -> str:
    """`line` without the text drawn dim. SGR 2 starts dim, 0, 22 or an empty reset end it; the arguments of an
    extended colour (38/48/58;5;n and ;2;r;g;b) are skipped, so a truecolour 2 is not read as dim."""
    out, dim, at = [], False, 0
    for m in _SGR.finditer(line):
        if not dim:
            out.append(line[at:m.start()])
        at = m.end()
        params = re.split("[;:]", m.group()[2:-1])
        i = 0
        while i < len(params):
            p = params[i]
            if p in ("38", "48", "58"):
                i += 5 if params[i + 1:i + 2] == ["2"] else 3
                continue
            if p in ("", "0", "22"):
                dim = False
            elif p == "2":
                dim = True
            i += 1
    if not dim:
        out.append(line[at:])
    return "".join(out)


def _rule(line) -> bool:
    text = _plain(line).strip()
    return len(text) >= 10 and set(text) == {"─"}


def typed(screen):
    """The text in the input box of `screen` (an ANSI screen read), "" when the box is empty (a dim placeholder
    counts as empty), or None when no input box shows, as under a permission dialog."""
    lines = (screen or "").splitlines()
    rules = [i for i, line in enumerate(lines) if _rule(line)]
    for top, bottom in reversed(list(zip(rules, rules[1:]))):
        box = lines[top + 1:bottom]
        if not box or not _plain(box[0]).lstrip().startswith(_PROMPT):
            continue
        parts = []
        for n, line in enumerate(box):
            text = _plain(_undimmed(line)).replace("\xa0", " ").strip()
            if n == 0:
                text = text[1:].strip() if text.startswith(_PROMPT) else text
            if text:
                parts.append(text)
        return " ".join(parts)
    return None


def screen(name) -> str:
    """The agent's visible screen with its styling, or "" when herdr cannot read it."""
    try:
        return herdr.text("agent", "read", name, "--source", "visible", "--format", "ansi", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""


def typing(name) -> bool:
    """Whether someone has typed into the agent's input box and not sent it."""
    return bool(typed(screen(name)))


def wait_clear(name, wait=None) -> bool:
    """Wait up to `wait` seconds (env ATHENA_TYPING_WAIT, 120 by default) for the input box to be empty.
    True when nothing is typed there."""
    wait = float(os.environ.get("ATHENA_TYPING_WAIT") or 120) if wait is None else wait
    deadline = time.monotonic() + wait
    while typing(name):
        if time.monotonic() >= deadline:
            return False
        time.sleep(1.0)
    return True
