#!/bin/bash
# Real Claudes through the whole lifecycle on a private Rex server of its own, on a sanitized scratch copy.
# Part 1: folder trust, in an isolated HOME: a real Claude on a brand-new repo's first worktree must not stop at the prompt.
# Part 2: spawn, status, send and retire of 3 Haiku workers at once, on the real HOME (they need its login). There the
# trust write is off, by the lab rule that only isolated HOMEs get it, so each prompt is answered with keys instead.
#
#   scripts/smoke.sh [--state-dir DIR] [--server ENDPOINT] [--keep]
#
# An inherited ATHENA_STATE_DIR is ignored: the worker launcher sets it to live state. --state-dir and --server exist to be
# refused: the live state, the backup or any endpoint but its own stop it before anything runs.
set -u
here=$(cd "$(dirname "$0")/.." && pwd -P)
ar="$here/bin/athena-rex"
rex_bin=${ATHENA_REX_BIN:-"/Applications/Rex Beta.app/Contents/Helpers/Rex Server.rex/Contents/MacOS/rex"}
keep=0
given_state=""
given_server=""
while [ $# -gt 0 ]; do
	case "$1" in
	--state-dir) given_state=$2; shift 2 ;;
	--server) given_server=$2; shift 2 ;;
	--keep) keep=1; shift ;;
	*) echo "usage: scripts/smoke.sh [--state-dir DIR] [--server ENDPOINT] [--keep]" >&2; exit 2 ;;
	esac
done

refuse_live() { # the product's own guard: it knows the real home from the account, not $HOME. No clear answer is a refusal.
	answer=$(/usr/bin/python3 -I -B -c 'import sys; sys.path.insert(0, sys.argv[1])
from athena_rex.kernel import lab
print("live" if lab.off_limits(sys.argv[2]) else "clear")' "$here" "$1" 2>/dev/null)
	if ! [ "$answer" = clear ]; then
		echo "smoke: refusing state dir $1: live state, its backup and the real home are never used" >&2
		exit 2
	fi
}
if [ -n "$given_state" ]; then
	refuse_live "$given_state"
fi

root=/private/tmp/claude-$(id -u)/ars$$
endpoint="unix://$root/s.sock"
if [ -n "$given_server" ] && [ "$given_server" != "$endpoint" ]; then
	echo "smoke: refusing server $given_server: only this run's own private endpoint is used" >&2
	exit 2
fi
free=$(memory_pressure 2>/dev/null | awk '/free percentage/ {print $NF+0}')
if [ "${free:-100}" -lt 40 ]; then
	echo "smoke: only ${free}% memory free, needs 40%" >&2
	exit 3
fi

started=$SECONDS
failures=0
deadline=${SMOKE_DEADLINE:-480}
# Bash runs a TERM trap only once its foreground child returns, so a hung child has to go too, or the deadline means nothing.
descendants() { # pid, leaving out one pid and what is under it
	for child in $(pgrep -P "$1" 2>/dev/null); do
		[ "$child" = "$2" ] && continue
		descendants "$child" "$2"
		echo "$child"
	done
}
(
	me=$(exec sh -c 'echo $PPID')
	sleep "$deadline" &
	nap=$!
	trap 'kill "$nap" 2>/dev/null; exit 0' TERM
	wait "$nap"
	if pgrep -a -f 'smoke\.sh' 2>/dev/null | grep -qx "$$"; then
		echo "smoke: over its ${deadline}s deadline" >&2
		kill -TERM $$ 2>/dev/null
		kill -TERM $(descendants $$ "$me") 2>/dev/null
		sleep 15
		kill -KILL $(descendants $$ "$me") 2>/dev/null
	fi
) &
watchdog=$!
say() { printf '%4ds  %s\n' "$((SECONDS - started))" "$*"; }
pass() { say "PASS  $*"; }
fail() { say "FAIL  $*"; failures=$((failures + 1)); }
check() { # what, then a command
	what=$1; shift
	if "$@"; then pass "$what"; else fail "$what"; fi
}

rx() { "$rex_bin" -S "$endpoint" --autostart=false "$@"; }

kill_own() { # worker: only a pid over 1 still running the Claude with that --name, as a pid can be reused by another Claude
	pid=$(cat "$state/run/$1.pid" 2>/dev/null)
	if [ "${pid:-0}" -gt 1 ] && ps -o command= -p "$pid" 2>/dev/null | grep -qE -- "--name $1( |\$)"; then
		kill -KILL "$pid" 2>/dev/null
	fi
}

cleanup() {
	code=$?
	for f in "$state"/run/*.pid; do
		[ -f "$f" ] || continue
		kill_own "$(basename "$f" .pid)"
	done
	"$ar" dash off >/dev/null 2>&1
	sh "$here/scripts/stop-dashes.sh" "$root" # one that never answered has nothing for dash off to find but its pid file
	rx server stop >/dev/null 2>&1
	if [ "$keep" = 1 ]; then
		echo "kept $root"
	else
		rm -rf "$root"
	fi
	kill "$watchdog" 2>/dev/null
	exit "$code"
}

mkdir -p "$root"
chmod 700 "$root"
state=${given_state:-$root/state}
# A signal must not exit with the last command's status: that made a killed smoke exit 0, a false pass.
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [ -z "$given_state" ]; then
	/usr/bin/python3 "$here/scripts/scratch-state.py" "$state" --port 2897 >/dev/null || exit 1
fi
export ATHENA_STATE_DIR="$state"
/usr/bin/python3 - "$state/config.json" <<'E2'
import json, sys
cfg = json.load(open(sys.argv[1]))
cfg.setdefault("rex", {})["signal_sandbox"] = True  # opt-in; smoke proves it on its own workers
json.dump(cfg, open(sys.argv[1], "w"))
E2
export ATHENA_REX_SERVER="$endpoint"
export ATHENA_REX_BIN="$rex_bin"
export ATHENA_HQ=smoke-hq
unset ATHENA_CONFIG ATHENA_ROLE ATHENA_REX_TRUST CLAUDE_CONFIG_DIR

echo '-- empty' >"$root/empty.lua"
printf '%s' "$state" >"$root/.athena-server-owner"
env -i HOME="$HOME" USER="$USER" LOGNAME="$LOGNAME" SHELL="$SHELL" LANG="${LANG:-en_US.UTF-8}" PATH="$PATH" TMPDIR="$TMPDIR" \
	REX_CONFIG="$root/empty.lua" "$rex_bin" -S "$endpoint" server start --state-dir "$root/rex" >/dev/null || exit 1
say "private server up at $endpoint"

make_repo() { # dir
	git init -q --bare -b main "$1/origin.git"
	git init -q -b main "$1/proj"
	git -C "$1/proj" -c user.name=smoke -c user.email=smoke@localhost commit -q --allow-empty -m one
	git -C "$1/proj" remote add origin "$1/origin.git"
	git -C "$1/proj" push -q -u origin main
}

field() { # worker, python expression over r
	"$ar" status "$1" --json --live | /usr/bin/python3 -c "import json,sys; r=json.load(sys.stdin)[0]; print($2)"
}

wait_for() { # seconds, then a command that succeeds when ready
	deadline=$((SECONDS + $1)); shift
	while [ "$SECONDS" -lt "$deadline" ]; do
		if "$@"; then return 0; fi
		sleep 1
	done
	return 1
}

# -- part 1: folder trust in an isolated HOME; no login there, so it costs nothing

iso="$root/iso"
mkdir -p "$iso" "$root/t1"
echo '{"hasCompletedOnboarding":true,"numStartups":3,"theme":"dark","keep":"me"}' >"$iso/.claude.json"
make_repo "$root/t1"
claude_bin=$(command -v claude)
mkdir -p "$root/t0"
make_repo "$root/t0"
out=$(HOME="$iso" ATHENA_REX_TRUST=off ATHENA_CLAUDE="/usr/bin/env HOME=$iso $claude_bin" \
	"$ar" spawn --repo "$root/t0/proj" --branch t/control --name smoke-control --model claude-haiku-5-5 --effort low --no-gate --timeout 40 --json)
echo "$out" | /usr/bin/python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('pending') else 1)"
check "control: with the trust write off, the same launch does stop at the prompt" test $? = 0
"$ar" retire smoke-control --force >/dev/null
out=$(HOME="$iso" ATHENA_CLAUDE="/usr/bin/env HOME=$iso $claude_bin" \
	"$ar" spawn --repo "$root/t1/proj" --branch t/trust --name smoke-trust --model claude-haiku-5-5 --effort low --no-gate --timeout 40 --json)
echo "$out" | /usr/bin/python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(1 if d.get('pending') else 0)"
check "spawn did not stop at a startup prompt on a brand-new repo's first worktree" test $? = 0
screen_iso=$("$ar" screen smoke-trust)
if echo "$screen_iso" | grep -q 'Quick safety check'; then fail "the trust prompt is on screen"; else pass "no trust prompt on screen"; fi
if echo "$screen_iso" | grep -q '❯'; then pass "Claude's input box is on screen"; else fail "no input box on screen"; fi
/usr/bin/python3 - "$iso/.claude.json" "$root/t1/proj" <<'E'
import json, os, sys
d = json.load(open(sys.argv[1]))
key = os.path.realpath(sys.argv[2])
sys.exit(0 if d.get("keep") == "me" and d["projects"][key]["hasTrustDialogAccepted"] is True else 1)
E
check "trust key on the main checkout; other keys kept; isolated file only" test $? = 0
if grep -q "$root/t1" "$HOME/.claude.json"; then fail "the real ~/.claude.json was touched"; else pass "the real ~/.claude.json has no key for it"; fi
"$ar" retire smoke-trust --force >/dev/null
check "trust worker retired" test $? = 0

# -- part 2: 3 tiny real Claudes at once

mkdir -p "$root/t2"
make_repo "$root/t2"
export ATHENA_REX_TRUST=off
report() { printf 'Reply with exactly this one line and nothing else: ATHENA-REPORT {"status":"BLOCKED","pr":"","head":"","verify":"none","decisions":[],"concerns":["%s"]}' "$1"; }

say "spawning 3 workers at once"
for w in smoke-a smoke-b smoke-c; do
	"$ar" spawn --repo "$root/t2/proj" --branch "t/$w" --name "$w" --model claude-haiku-5-5 --effort low --timeout 60 \
		--no-fetch --force --prompt "$(report "first-$w")" >"$root/$w.spawn" 2>&1 &
	spawning="${spawning:-} $!"
done
wait $spawning
for w in smoke-a smoke-b smoke-c; do
	check "$w spawned" grep -q -E 'startup prompt|is running' "$root/$w.spawn"
	cat "$root/$w.spawn" | grep -v -E 'startup prompt|is running'
done
"$ar" status

for w in smoke-a smoke-b smoke-c; do
	if grep -q 'startup prompt' "$root/$w.spawn"; then
		"$ar" keys "$w" down
		sleep 0.3
		"$ar" keys "$w" enter
	fi
done

ready() { case "$(field "$1" "(r.get('live') or {}).get('state')")" in idle|done) return 0 ;; *) return 1 ;; esac; }
for w in smoke-a smoke-b smoke-c; do
	check "$w ready after its startup prompt" wait_for 60 ready "$w"
	if grep -q 'startup prompt' "$root/$w.spawn"; then "$ar" resume "$w" >/dev/null; fi
done

concern_is() { [ "$(field "$1" "((r.get('report') or {}).get('concerns') or [''])[0]")" = "$2" ]; }
for w in smoke-a smoke-b smoke-c; do
	check "$w answered: its report reached status" wait_for 90 concern_is "$w" "first-$w"
done
"$ar" status --time

check "status --json lists all 3" test "$("$ar" status --json | /usr/bin/python3 -c "import json,sys; print(sum(r['name'].startswith('smoke-') for r in json.load(sys.stdin)))")" = 3

"$ar" watch --once >/dev/null
"$ar" send smoke-a "$(report second-a)" >/dev/null
check "send reached smoke-a" wait_for 60 concern_is smoke-a second-a
"$ar" send smoke-a "$(report third-a)" >/dev/null
check "and again" wait_for 60 concern_is smoke-a third-a
padding=$(/usr/bin/python3 -c 'print(" ".join("word%d" % n for n in range(400)))')
"$ar" send smoke-a "$(report long-a) These numbered words only make this message long: $padding" >/dev/null
check "a long prompt arrives as typed, not as pasted data" wait_for 90 concern_is smoke-a long-a
"$ar" watch --once >"$root/watch.out"
check "the watch told what changed since its last run" grep -q 'smoke-a' "$root/watch.out"

# The signal sandbox: a script a worker's Claude runs can't signal a process outside it. The outsider is a sleep this script started,
# alone in its own process group. Inside, a direct SIGCONT to it and a SIGTERM to its whole group must both be refused, and it must
# live. No signal ever goes to -1, and the full proof is scripts/sandbox-proof.sh.
check "smoke-a's Claude starts under the signal sandbox" grep -q '/usr/bin/sandbox-exec' "$state/run/smoke-a.sh"
outsider=$(/usr/bin/python3 -c 'import subprocess as s; print(s.Popen(["/bin/sleep", "600"], start_new_session=True, stdin=s.DEVNULL, stdout=s.DEVNULL, stderr=s.DEVNULL).pid)')
cat >"$root/killall.py" <<'PY'
import os, signal, sys
pid, out = int(sys.argv[1]), sys.argv[2]
said = []
for what, target, sig in (("direct", pid, signal.SIGCONT), ("group", -os.getpgid(pid), signal.SIGTERM)):
    try:
        os.kill(target, sig)
        said.append(f"{what}=delivered")
    except PermissionError:
        said.append(f"{what}=EPERM")
open(out, "w").write(" ".join(said) + f" pid={os.getpid()}\n")
PY
"$ar" send smoke-a "Use the Bash tool to run exactly this command: /usr/bin/python3 $root/killall.py $outsider $root/killall.out -- then $(report killall-done)" >/dev/null
check "smoke-a ran the signal script" wait_for 90 concern_is smoke-a killall-done
say "        killall.py said: $(cat "$root/killall.out" 2>/dev/null)"
check "inside the sandbox, a signal to the outsider is refused" grep -q 'direct=EPERM' "$root/killall.out"
check "and so is a SIGTERM to its whole process group" grep -q 'group=EPERM' "$root/killall.out"
check "and the outsider, outside the sandbox, still lives" kill -0 "$outsider"
kill -KILL "$outsider" 2>/dev/null
other=$(rx new someone-elses --json | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')
"$ar" sessions | tee "$root/sessions.out"
check "sessions names the repo's Rex session, which holds its workers" grep -q -E '^proj +worker +smoke-' "$root/sessions.out"
repo_windows() { # the labels of the windows in the one session labelled proj, sorted
	space=$(rx ls --json | /usr/bin/python3 -c 'import json,sys; print(next(s["session_id"] for s in json.load(sys.stdin)["sessions"] if s.get("label") == "proj"))')
	rx session inspect "$space" --json | /usr/bin/python3 -c 'import json,sys; print(" ".join(sorted(w.get("label") or "" for w in json.load(sys.stdin)["windows"])))'
}
check "each worker is a window named for it in its repo's session" test "$(repo_windows)" = "smoke-a smoke-b smoke-c"
check "and one that is not athena's" grep -q -E '^someone-elses +other ' "$root/sessions.out"
"$ar" status | tee "$root/status.out"
check "status shows each worker's Rex session" grep -q 'session "smoke a"' "$root/status.out"
"$ar" dash on >/dev/null
sessions_shown() {
	page=$(curl -s -o "$root/page.html" -w '%{http_code}' "http://127.0.0.1:2897/")
	test "$page" = 200 && grep -q 'Rex sessions' "$root/page.html" && grep -q 'someone-elses' "$root/page.html"
}
check "the dashboard answers 200 and lists the Rex sessions" wait_for 20 sessions_shown
check "and shows the workers, each with its session" grep -q 'session “smoke b”' "$root/page.html"
"$ar" dash off >/dev/null
rx kill "$other" >/dev/null

conversation=$(field smoke-a "r['session_id']")
"$ar" send smoke-a 'Use the Bash tool to run: sleep 120. Then reply.' >/dev/null
working() { [ "$(field smoke-a "r['state']")" = working ]; }
check "smoke-a is mid-turn" wait_for 30 working
kill_own smoke-a
sleep 1
check "a Claude killed mid-turn reads as exited, not working" test "$("$ar" status smoke-a --json | /usr/bin/python3 -c "import json,sys; print(json.load(sys.stdin)[0]['state'])")" = exited
"$ar" doctor --fix --json >"$root/doctor.json"
check "doctor leaves the gone Claude to Rex: no new session or window for it" test "$(rx ls --json | /usr/bin/python3 -c "import json,sys; print(len(json.load(sys.stdin)['sessions']))") $(repo_windows)" = "1 smoke-a smoke-b smoke-c"
check "doctor wrote health.json" /usr/bin/python3 -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if 'problems' in d and 'hq_alive' in d else 1)" "$state/health.json"

"$ar" send smoke-b 'Reply with exactly the word: hello' >/dev/null
gate_fired() { grep -q '"topic": "worker.gate"' "$state/events.jsonl" && grep '"topic": "worker.gate"' "$state/events.jsonl" | grep -q smoke-b; }
check "report gate sent smoke-b's turn back" wait_for 60 gate_fired
settled() { case "$(field smoke-b "r['state']")" in blocked|done|idle-unreported) return 0 ;; *) return 1 ;; esac; }
check "smoke-b's turn ended after the gate" wait_for 90 settled

"$ar" send smoke-c 'half typed by a person' --no-submit >/dev/null
sleep 1
"$ar" send smoke-c 'must not be sent' >"$root/refused" 2>&1
check "send over typed text is refused" test $? = 3
"$ar" retire smoke-c >"$root/refused2" 2>&1
check "retire over typed text is refused" test $? != 0
"$ar" keys smoke-c ctrl+u
sleep 1

path_b=$(field smoke-b "r['path']")
echo unsaved >"$path_b/unsaved.txt"
"$ar" retire smoke-b >"$root/refused3" 2>&1
check "retire over unsaved work is refused" test $? != 0
check "the unsaved file is still there" test -f "$path_b/unsaved.txt"
"$ar" retire smoke-b --force --json >"$root/forced" 2>&1
check "retire --force went through" test $? = 0
wip=$(/usr/bin/python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['wip_backup'])" "$root/forced")
check "the unsaved file is kept under $wip" test "$(git -C "$root/t2/proj" show "$wip:unsaved.txt" 2>/dev/null)" = unsaved

pids=""
for w in smoke-a smoke-c; do
	pids="$pids $(cat "$state/run/$w.pid")"
	"$ar" retire "$w"
	check "$w retired" test $? = 0
done

gone() { for p in $pids; do if ps -p "$p" >/dev/null 2>&1; then return 1; fi; done; return 0; }
check "their Claudes are gone" wait_for 10 gone
check "no sessions left on the private server" test "$(rx ls --json | /usr/bin/python3 -c "import json,sys; print(len(json.load(sys.stdin)['sessions']))")" = 0
check "no worktrees left" test "$(git -C "$root/t2/proj" worktree list | wc -l | tr -d ' ')" = 1
check "ledger says retired" test "$("$ar" status smoke-a smoke-b smoke-c --json | /usr/bin/python3 -c "import json,sys; print(sum(r['phase']=='retired' for r in json.load(sys.stdin)))")" = 3

# -- part 3: HQ on a real Claude and its rotation (Rex runs the sessions: nothing restarts or revives HQ)

hq_cwd="$root/hq"
mkdir -p "$hq_cwd"
/usr/bin/python3 - "$state/config.json" "$hq_cwd" <<'E'
import json, sys
cfg = json.load(open(sys.argv[1]))
cfg["projects_dir"] = sys.argv[2]
json.dump(cfg, open(sys.argv[1], "w"))
E
hq_value() { /usr/bin/python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2]) or '')" "$state/hq.json" "$1"; }
hq_session() { /usr/bin/python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('session_id') or '')" "$state/hq-start.json" 2>/dev/null; }
"$ar" hq --no-routine --model claude-haiku-5-5 >/dev/null
check "HQ opened" test -n "$(hq_value block)"
rx send-key -s "$(hq_value space)" -b "$(hq_value block)" down
sleep 0.3
rx send-key -s "$(hq_value space)" -b "$(hq_value block)" enter
have_session() { [ -n "$(hq_session)" ]; }
check "HQ's SessionStart hook recorded its conversation" wait_for 60 have_session
first=$(hq_session)
old_watch() { ! pgrep -P "$(cat "$state/run/smoke-hq.pid")" -f 'watch --tagged' >/dev/null; }
check "the old athena's watch mod did not load in HQ, with the plugin enabled for this user" old_watch
check "HQ is first in the session list" test "$(rx ls --json | /usr/bin/python3 -c "import json,sys; print(json.load(sys.stdin)['sessions'][0]['label'])")" = smoke-hq
hq_labels() { rx session inspect "$(hq_value space)" --json | /usr/bin/python3 -c 'import json,sys; w=json.load(sys.stdin)["windows"]; print(" ".join([x.get("label") or "" for x in w] + [b.get("label") or "" for x in w for l in x.get("layers") or [] for b in l.get("blocks") or []]))'; }
check "HQ's window and block carry its name too" test "$(hq_labels)" = "smoke-hq smoke-hq"
hq_idle() { rx block call -s "$(hq_value space)" -b "$(hq_value block)" program_status | grep -q -E '"state":"(idle|done)"'; }
check "HQ is idle" wait_for 60 hq_idle
requests_before=$(wc -l <"$state/requests.jsonl")
"$ar" watch --once >/dev/null
printf '{"seq": 999999, "ts": %s, "item": "a999999", "status": "queued", "option": "x"}\n' "$(($(date +%s) - 60))" >>"$state/actions.jsonl"
"$ar" watch --once --wake >"$root/wake.out"
check "the watch rang" grep -q 'dash-queue 999999' "$root/wake.out"
woken() { rx capture -s "$(hq_value space)" -b "$(hq_value block)" --trim | grep -q 'athena watch [0-9:]*: dash-queue 999999'; }
check "the wake reached HQ's Claude with its line, as athena's mod sent it" wait_for 30 woken
check "HQ answered the wake" wait_for 90 hq_idle
check "a wake is not logged as the human's request" test "$(wc -l <"$state/requests.jsonl")" = "$requests_before"
"$ar" hq rotate --now >"$root/rotate" 2>&1
check "rotation went through" grep -q rotated "$root/rotate"
cat "$root/rotate" | grep -v rotated
rotated() { [ -n "$(hq_session)" ] && [ "$(hq_session)" != "$first" ]; }
check "rotation started a new conversation" wait_for 60 rotated
told_skill() { rx capture -s "$(hq_value space)" -b "$(hq_value block)" --trim | grep -q 'load the athena-rex skill'; }
check "the fresh HQ was told to load the athena-rex skill" wait_for 30 told_skill
check "HQ was launched with the skill's plugin" grep -q -- "--plugin-dir $here/plugin" "$state/run/smoke-hq.sh"
loaded_skill() { rx capture -s "$(hq_value space)" -b "$(hq_value block)" --trim | grep -q -E 'Skill\(athena-rex|athena-rex:athena-rex|Successfully loaded skill'; }
if wait_for 90 loaded_skill; then pass "and its Claude loaded the skill"; else fail "its Claude did not load the skill"; fi
rx capture -s "$(hq_value space)" -b "$(hq_value block)" --trim | grep -E 'athena-rex skill|Skill|athena-rex:athena-rex' | head -5 | sed 's/^/        hq: /'
check "and kept the old one on record" test "$(hq_value previous_session)" = "$first"
check "it wrote the handoff" test -s "$state/handoff.md"
check "HQ finished its first turn" wait_for 120 hq_idle
"$ar" hq stop >/dev/null
check "HQ stopped and its session closed" test "$(rx ls --json | /usr/bin/python3 -c "import json,sys; print(len(json.load(sys.stdin)['sessions']))")" = 0

say "$failures failures"
[ "$failures" = 0 ] || exit 1
say "smoke passed"
