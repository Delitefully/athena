"""What sits in a Claude session's input box: the text someone typed there and has not sent yet.

Claude Code draws its input box between two horizontal rules, the first line starting with `❯`. With nothing typed
it shows a placeholder in dim (SGR 2), which a plain-text read cannot tell from typed text, so the screen is read
with its ANSI styling (`herdr agent read --format ansi`) and dim spans are dropped, as are inverse ones (a drawn
cursor). A one-letter message under the cursor reads as empty: the price of never reading a cursor as typing. athena never types into a pane
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
    """`line` without the text drawn dim or inverse. Dim (SGR 2) is the empty box's placeholder; inverse (SGR 7) is
    a drawn cursor, which may sit on the placeholder's first letter or after the text. 0 or an empty reset ends
    both, 22 dim, 27 inverse; the arguments of an extended colour (38/48/58;5;n and ;2;r;g;b) are skipped, so a
    truecolour 2 or 7 is not read as either."""
    out, dim, inverse, at = [], False, False, 0
    for m in _SGR.finditer(line):
        if not (dim or inverse):
            out.append(line[at:m.start()])
        at = m.end()
        params = re.split("[;:]", m.group()[2:-1])
        i = 0
        while i < len(params):
            p = params[i]
            if p in ("38", "48", "58"):
                i += 5 if params[i + 1:i + 2] == ["2"] else 3
                continue
            if p in ("", "0"):
                dim = inverse = False
            elif p == "22":
                dim = False
            elif p == "27":
                inverse = False
            elif p == "2":
                dim = True
            elif p == "7":
                inverse = True
            i += 1
    if not (dim or inverse):
        out.append(line[at:])
    return "".join(out)


def _rule(line) -> bool:
    text = _plain(line).strip()
    return len(text) >= 10 and set(text) == {"─"}


def typed(screen):
    """The text in the input box of `screen` (an ANSI screen read): "" when the box is empty (a dim placeholder or a
    drawn cursor counts as empty), or None when the screen shows no box athena recognises. Only the bottom box counts:
    the last two rules, with a `❯` (or `>`) prompt on the first line between them. Anything else, such as a
    permission dialog, bash mode's `!` prompt or a screen that could not be read, is None."""
    lines = (screen or "").splitlines()
    rules = [i for i, line in enumerate(lines) if _rule(line)]
    if len(rules) < 2:
        return None
    box = lines[rules[-2] + 1:rules[-1]]
    if not box or not _plain(box[0]).lstrip().startswith(_PROMPT):
        return None
    parts = []
    for n, line in enumerate(box):
        text = _plain(_undimmed(line)).replace("\xa0", " ").strip()
        if n == 0:
            text = text[1:].strip() if text.startswith(_PROMPT) else text
        if text:
            parts.append(text)
    return " ".join(parts)


def screen(name) -> str:
    """The agent's visible screen with its styling, or "" when herdr cannot read it."""
    try:
        return herdr.text("agent", "read", name, "--source", "visible", "--format", "ansi", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""


def typing(name) -> bool:
    """Whether athena must not type into the agent's pane: someone typed into its input box and has not sent it, or
    the box cannot be read or is not one athena recognises (fail closed: a screen that could not be read, a dialog,
    bash mode). Only an input box seen empty is safe."""
    return typed(screen(name)) != ""


def wait_clear(name, wait=None) -> bool:
    """Wait up to `wait` seconds (env ATHENA_TYPING_WAIT, 120 by default) for the input box to be empty.
    True once the box is seen empty."""
    wait = float(os.environ.get("ATHENA_TYPING_WAIT") or 120) if wait is None else wait
    deadline = time.monotonic() + wait
    while typing(name):
        if time.monotonic() >= deadline:
            return False
        time.sleep(1.0)
    return True
