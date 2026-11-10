#!/bin/bash
# Gap 9's lab proof: one Haiku worker, spawned by athena-rex on a private Rex server of its own and a sanitized scratch copy,
# runs a script inside its signal sandbox. Outside, this script started a sleep in a process group of its own.
#
#   scripts/sandbox-proof.sh [--keep]
#
# What the worker's script does, and what passes:
#   kill(-1, 0)              returns. Not a hole: macOS kill() is POSIX, so -1 counts the caller itself, which may always
#                            signal itself; delivery to anything outside is still refused (the two lines below). Signal 0
#                            only: a real signal to -1 is never sent, by this or any test.
#   kill(outsider, SIGTERM)  PermissionError, and the outsider is still alive afterwards.
#   kill(-its group, SIGTERM) PermissionError: the group path is refused too. The group holds only the outsider.
#   its own child            started inside, then SIGTERMed by the script: it dies (-15).
set -u
here=$(cd "$(dirname "$0")/.." && pwd -P)
ar="$here/bin/athena-rex"
rex_bin=${ATHENA_REX_BIN:-"/Applications/Rex Beta.app/Contents/Helpers/Rex Server.rex/Contents/MacOS/rex"}
keep=0
[ "${1:-}" = --keep ] && keep=1

free=$(memory_pressure 2>/dev/null | awk '/free percentage/ {print $NF+0}')
if [ "${free:-100}" -lt 40 ]; then
	echo "sandbox-proof: only ${free}% memory free, needs 40%" >&2
	exit 3
fi

root=/private/tmp/claude-$(id -u)/arp$$
endpoint="unix://$root/s.sock"
state=$root/state
started=$SECONDS
failures=0
say() { printf '%4ds  %s\n' "$((SECONDS - started))" "$*"; }
check() { # what, then a command
	what=$1; shift
	if "$@"; then say "PASS  $what"; else say "FAIL  $what"; failures=$((failures + 1)); fi
}
rx() { "$rex_bin" -S "$endpoint" --autostart=false "$@"; }
wait_for() { # seconds, then a command that succeeds when ready
	deadline=$((SECONDS + $1)); shift
	while [ "$SECONDS" -lt "$deadline" ]; do
		if "$@"; then return 0; fi
		sleep 1
	done
	return 1
}
outsider=""
cleanup() {
	code=$?
	pid=$(cat "$state/run/proof-a.pid" 2>/dev/null)
	if [ "${pid:-0}" -gt 1 ] && ps -o command= -p "$pid" 2>/dev/null | grep -qE -- "--name proof-a( |\$)"; then
		kill -KILL "$pid" 2>/dev/null
	fi
	if [ "${outsider:-0}" -gt 1 ] && ps -o command= -p "$outsider" 2>/dev/null | grep -q '^/bin/sleep 600'; then
		kill -KILL "$outsider" 2>/dev/null
	fi
	rx server stop >/dev/null 2>&1
	if [ "$keep" = 1 ]; then echo "kept $root"; else rm -rf "$root"; fi
	exit "$code"
}
mkdir -p "$root"
chmod 700 "$root"
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

/usr/bin/python3 "$here/scripts/scratch-state.py" "$state" --port 2898 >/dev/null || exit 1
export ATHENA_STATE_DIR="$state"
/usr/bin/python3 - "$state/config.json" <<'E2'
import json, sys
cfg = json.load(open(sys.argv[1]))
cfg.setdefault("rex", {})["signal_sandbox"] = True  # opt-in: the proof turns it on for its own scratch state
json.dump(cfg, open(sys.argv[1], "w"))
E2
export ATHENA_REX_SERVER="$endpoint"
export ATHENA_REX_BIN="$rex_bin"
export ATHENA_HQ=proof-hq
export ATHENA_REX_TRUST=off
unset ATHENA_CONFIG ATHENA_ROLE CLAUDE_CONFIG_DIR

echo '-- empty' >"$root/empty.lua"
printf '%s' "$state" >"$root/.athena-server-owner"
env -i HOME="$HOME" USER="$USER" LOGNAME="$LOGNAME" SHELL="$SHELL" LANG="${LANG:-en_US.UTF-8}" PATH="$PATH" TMPDIR="$TMPDIR" \
	REX_CONFIG="$root/empty.lua" "$rex_bin" -S "$endpoint" server start --state-dir "$root/rex" >/dev/null || exit 1
say "private server up at $endpoint"

git init -q --bare -b main "$root/origin.git"
git init -q -b main "$root/proj"
git -C "$root/proj" -c user.name=proof -c user.email=proof@localhost commit -q --allow-empty -m one
git -C "$root/proj" remote add origin "$root/origin.git"
git -C "$root/proj" push -q -u origin main

field() { # python expression over r
	"$ar" status proof-a --json --live | /usr/bin/python3 -c "import json,sys; r=json.load(sys.stdin)[0]; print($1)"
}
report() { printf 'Reply with exactly this one line and nothing else: ATHENA-REPORT {"status":"BLOCKED","pr":"","head":"","verify":"none","decisions":[],"concerns":["%s"]}' "$1"; }
ready() { case "$(field "(r.get('live') or {}).get('state')")" in idle|done) return 0 ;; *) return 1 ;; esac; }
concern_is() { [ "$(field "((r.get('report') or {}).get('concerns') or [''])[0]")" = "$1" ]; }

"$ar" spawn --repo "$root/proj" --branch t/proof --name proof-a --model claude-haiku-5-5 --effort low --timeout 60 \
	--no-fetch --force --prompt "$(report first)" >"$root/spawn.out" 2>&1
cat "$root/spawn.out"
if grep -q 'startup prompt' "$root/spawn.out"; then
	"$ar" keys proof-a down
	sleep 0.3
	"$ar" keys proof-a enter
	wait_for 60 ready
	"$ar" resume proof-a >/dev/null
fi
check "proof-a answered its first prompt" wait_for 90 concern_is first
say "its launcher: $(grep '^exec ' "$state/run/proof-a.sh" | cut -c1-150)..."
check "proof-a's Claude was launched under sandbox-exec with the signal profile" \
	grep -q "^exec /usr/bin/sandbox-exec -p '(version 1)(allow default)(deny signal)(allow signal (target same-sandbox))' env ATHENA_SIGNAL_SANDBOX=1 " "$state/run/proof-a.sh"

# The outsider: started here, outside every sandbox, as the leader of a process group of its own.
/usr/bin/python3 -I -c 'import os; os.setsid(); os.execv("/bin/sleep", ["/bin/sleep", "600"])' &
outsider=$!
wait_for 5 sh -c "[ \"\$(ps -o pgid= -p $outsider | tr -d ' ')\" = $outsider ]"
check "the outsider $outsider leads its own group, alone in it" test "$(ps -A -o pgid= | tr -d ' ' | grep -cx "$outsider")" = 1

cat >"$root/proof.py" <<'PY'
import json, os, signal, subprocess, sys
outsider, out = int(sys.argv[1]), sys.argv[2]
assert outsider > 1
said = {"sandbox_mark": os.environ.get("ATHENA_SIGNAL_SANDBOX"), "me": os.getpid()}

def attempt(key, target, sig):
    try:
        os.kill(target, sig)
        said[key] = "returned"
    except OSError as exc:
        said[key] = f"{type(exc).__name__} errno={exc.errno}"

attempt("kill(-1, 0)", -1, 0)  # signal 0: nothing is delivered whatever it returns
attempt("kill(outsider, SIGTERM)", outsider, signal.SIGTERM)
attempt("kill(-outsider_group, SIGTERM)", -outsider, signal.SIGTERM)
child = subprocess.Popen(["/bin/sleep", "600"])
attempt("kill(own child, SIGTERM)", child.pid, signal.SIGTERM)
said["own child exit"] = child.wait(timeout=10)
open(out, "w").write(json.dumps(said) + "\n")
PY
"$ar" send proof-a "Use the Bash tool to run exactly this command: /usr/bin/python3 -I $root/proof.py $outsider $root/proof.out -- then $(report proof-done)" >/dev/null
check "proof-a ran the script" wait_for 120 concern_is proof-done
say "proof.py said: $(cat "$root/proof.out" 2>/dev/null)"
said() { /usr/bin/python3 -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if str(d.get(sys.argv[2])) == sys.argv[3] else 1)" "$root/proof.out" "$1" "$2"; }
check "the script ran inside the sandbox (ATHENA_SIGNAL_SANDBOX=1 inherited)" said sandbox_mark 1
check "kill(-1, 0) returned, as POSIX kill() does when the caller counts itself" said "kill(-1, 0)" returned
check "kill(outsider, SIGTERM) was refused: PermissionError" said "kill(outsider, SIGTERM)" "PermissionError errno=1"
check "kill(-outsider's group, SIGTERM) was refused: PermissionError" said "kill(-outsider_group, SIGTERM)" "PermissionError errno=1"
check "the outsider $outsider is still alive" sh -c "ps -o command= -p $outsider | grep -q '^/bin/sleep 600'"
check "proof-a's Claude killed its own child" said "kill(own child, SIGTERM)" returned
check "and the child died of it (exit -15)" said "own child exit" -15
"$ar" retire proof-a --force >/dev/null
check "proof-a retired" test $? = 0

if [ "$failures" = 0 ]; then say "sandbox-proof: all passed"; else say "sandbox-proof: $failures failed"; fi
[ "$failures" = 0 ]
