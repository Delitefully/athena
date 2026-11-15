"""What a step is called, and what it would cost at list prices. Pure functions, no state.

A frame name is a few safe characters or `other`: whatever a transcript holds, only a command's first words, a subagent
type, a skill or an MCP server and tool can become a name, and only if they look like one.
"""
import os
import re

SAFE = re.compile(r"^[A-Za-z0-9._+:/@-]{1,40}$")
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
SKIP = {"cd", "export", "source", ".", "set", "unset", "pushd", "popd", "true", "local", "shopt", "trap", "umask", "ulimit"}
WRAPPERS = {"nice", "time", "timeout", "env", "sudo", "caffeinate", "nohup", "exec", "command", "builtin", "gtimeout", "stdbuf"}
TAKES_VALUE = {"nice": {"-n"}, "timeout": {"-s", "-k", "--signal", "--kill-after"}, "sudo": {"-u", "-g"}, "env": {"-u"},
               "caffeinate": {"-t", "-w"}, "stdbuf": {"-i", "-o", "-e"}}
SUBVERBS = {"git", "gh", "athena", "athena", "rex", "herdr", "docker", "npm", "pnpm", "yarn", "go", "aws", "kubectl", "make",
            "brew", "uv", "cargo", "bun", "npx", "launchctl", "gcloud", "terraform", "claude", "pip", "pip3", "python3", "python"}
SUB_SKIP = {"git": {"-C", "-c", "--git-dir", "--work-tree"}, "gh": {"-R", "--repo"}, "docker": {"--context", "-H"},
            "aws": {"--profile", "--region", "--output"}, "kubectl": {"-n", "--namespace", "--context"}, "make": {"-C", "-f", "-j"},
            "npm": {"--prefix"}, "pnpm": {"-C", "--dir", "--filter"}, "go": set(), "python3": set(), "python": set()}
SEPARATORS = re.compile(r"&&|\|\||;|\||\n")
AGENT_TOOLS = ("Agent", "Task")
WORKFLOW_TOOL = "Workflow"


def safe(name, fallback="other") -> str:
    text = str(name or "").strip()
    return text if SAFE.match(text) else fallback


def _words(segment):
    return [w.strip("'\"") for w in segment.strip().lstrip("({!").split()]


LOOPS = {"for", "while", "until", "select"}
BRANCHES = {"if", "elif"}
LEADS = {"do", "then", "else", "{", "(", "!"}
CLOSERS = {"done", "fi", "esac", "}", ")"}
TESTS = {"[", "[[", "test", "true", "false", ":"}


def bash_family(command) -> list:
    """`cd x && FOO=1 nice -n 5 git -C y push origin` is ["git", "git push"]: the first real command, seen through wrappers.
    A loop is named by what it runs: `until gh pr checks 1; do sleep 30; done` is ["loop", "loop: gh pr"] (its condition,
    when that is a command), `for f in *; do git add "$f"; done` is ["loop", "loop: git add"] (else its body's first)."""
    return _family([_words(seg) for seg in SEPARATORS.split(str(command or ""))[:24]])


def _family(segments) -> list:
    i = 0
    while i < len(segments):
        words = segments[i]
        i += 1
        while words and words[0] in LEADS:
            words = words[1:]
        if not words or words[0] in CLOSERS:
            continue
        head = words[0]
        if head in LOOPS or head in BRANCHES:
            label = "loop" if head in LOOPS else "if"
            if head not in ("for", "select") and len(words) > 1 and words[1] not in TESTS:
                inner = _family([words[1:]] + segments[i:])  # what it waits on, polls or tests
            else:
                body = next((k for k in range(i, len(segments)) if segments[k] and segments[k][0] in ("do", "then")), None)
                inner = _family(segments[body:]) if body is not None else ["other"]
            name = inner[-1].split(": ", 1)[-1]  # a loop in a loop is named by what the inner one runs
            return [label, f"{label}: {name}"] if name != "other" else [label]
        found = _command(words)
        if found:
            return found
    return ["other"]


def _command(words):
    """The family of one simple command, or None for one that only sets things up (`cd`, `export`, a bare wrapper)."""
    i = 0
    while i < len(words):
        word = words[i]
        if not word or ASSIGN.match(word):
            i += 1
            continue
        base = os.path.basename(word.rstrip("/")) or word
        if base in WRAPPERS:
            i += 1
            takes = TAKES_VALUE.get(base, set())
            while i < len(words) and (words[i].startswith("-") or ASSIGN.match(words[i]) or
                                      (base in ("timeout", "gtimeout") and re.match(r"^\d+[smhd]?$", words[i]))):
                i += 2 if words[i] in takes else 1
            continue
        break
    if i >= len(words):
        return None
    base = os.path.basename(words[i].rstrip("/")) or words[i]
    if base in SKIP:
        return None
    base = safe(base)
    if base not in SUBVERBS:
        return [base]
    rest = words[i + 1:]
    skip = SUB_SKIP.get(base, set())
    j = 0
    while j < len(rest):
        word = rest[j]
        if word in skip:
            j += 2
            continue
        if word.startswith("-") or ASSIGN.match(word):
            j += 1
            continue
        if base in ("python3", "python") and word.endswith(".py"):
            word = os.path.basename(word)
        elif base in ("python3", "python"):
            break
        sub = safe(word)
        return [base, f"{base} {sub}"] if sub != "other" else [base]
    if base in ("python3", "python") and "-c" in rest:
        return [base, f"{base} -c"]
    return [base]


def workflow_name(tool_input) -> str:
    """The Workflow tool's own name for the run: a saved one's `name`, else the script's `meta.name`."""
    if not isinstance(tool_input, dict):
        return "workflow"
    if tool_input.get("name"):
        return safe(tool_input["name"], "workflow")
    script = str(tool_input.get("script") or "")[:4000]
    found = re.search(r"\bname\s*:\s*['\"]([^'\"]{1,60})['\"]", script)
    return safe(found.group(1), "workflow") if found else "workflow"


def tool_path(name, tool_input) -> list:
    name = str(name or "")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if name == "Bash":
        return ["Bash", *bash_family(tool_input.get("command"))]
    if name in AGENT_TOOLS:
        return ["Agent", safe(tool_input.get("subagent_type") or "general-purpose")]
    if name == WORKFLOW_TOOL:
        return ["Workflow", workflow_name(tool_input)]
    if name == "Skill":
        return ["Skill", safe(tool_input.get("skill") or tool_input.get("name"))]
    if name.startswith("mcp__"):
        parts = name.split("__")
        server = parts[1] if len(parts) > 2 else "mcp"
        tool = "__".join(parts[2:]) if len(parts) > 2 else parts[-1]
        return ["MCP", safe(server), safe(tool)]
    return [safe(name, "tool")]


def credits_run(name) -> bool:
    """An Agent or Workflow call's wait is the subagents' own steps, counted there; its run is not counted again."""
    return str(name or "") not in AGENT_TOOLS + (WORKFLOW_TOOL,)


# List API prices per million tokens: input, output, cache read. Cache writes are 1.25x input (5 minutes) and 2x (1 hour).
# Fast mode is twice the price. The first pattern that matches the model wins; an unknown model is priced as Opus 5.5.
PRICES = (
    (r"opus-5-5", (4.0, 20.0, 0.20)), (r"opus-5", (5.0, 25.0, 0.50)), (r"opus-4-[5-9]", (5.0, 25.0, 0.50)),
    (r"opus", (15.0, 75.0, 1.50)),
    (r"sonnet-5-5", (2.0, 10.0, 0.10)), (r"sonnet-5", (2.0, 10.0, 0.20)), (r"sonnet", (3.0, 15.0, 0.30)),
    (r"haiku-5-5", (0.10, 0.50, 0.01)), (r"haiku", (1.0, 5.0, 0.10)),
    (r"(fable|mythos)-5-2", (8.0, 40.0, 0.20)), (r"(fable|mythos)-5-1", (10.0, 50.0, 0.25)), (r"(fable|mythos)", (10.0, 50.0, 1.0)),
)
DEFAULT_PRICE = (4.0, 20.0, 0.20)
_price_cache = {}


def price(model) -> tuple:
    model = str(model or "")
    if model not in _price_cache:
        _price_cache[model] = next((p for pattern, p in PRICES if re.search(pattern, model)), DEFAULT_PRICE)
    return _price_cache[model]


def cost_micro(model, usage: dict) -> int:
    """Estimated USD, in millionths, at list prices."""
    inp, out, read = price(model)
    write_5m, write_1h = usage.get("cw5", 0), usage.get("cw1", 0)
    rest = max(usage.get("cw", 0) - write_5m - write_1h, 0)  # a total without the split is priced as 5-minute writes
    dollars_per_m = (usage.get("in", 0) * inp + usage.get("out", 0) * out + usage.get("cr", 0) * read
                     + (write_5m + rest) * inp * 1.25 + write_1h * inp * 2.0)
    if usage.get("fast"):
        dollars_per_m *= 2
    return int(round(dollars_per_m))  # tokens × $/M = micro-dollars
