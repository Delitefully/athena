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
ar="$here/bin/athena"
rex_bin=${ATHENA_BINARY:-"/Applications/Rex Beta.app/Contents/Helpers/Rex Server.rex/Contents/MacOS/rex"}
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
from athena.kernel import lab
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
export ATHENA_SERVER="$endpoint"
export ATHENA_BINARY="$rex_bin"
export ATHENA_HQ=smoke-hq
unset ATHENA_CONFIG ATHENA_ROLE ATHENA_TRUST CLAUDE_CONFIG_DIR

# This server's own Rex config, never the human's: the palette's questions wait 5 seconds here, so one left unanswered ends fast.
cat >"$root/init.lua" <<'LUA'
for _, ask in ipairs({ "pick", "input" }) do
	local real = rex.ui[ask]
	rex.ui[ask] = function(opts) opts.timeout = 5; return real(opts) end
end
LUA
printf '%s' "$state" >"$root/.athena-server-owner"
# The athena the palette runs inside the server talks to this server alone (with the door shut it may name no other).
env -i HOME="$HOME" USER="$USER" LOGNAME="$LOGNAME" SHELL="$SHELL" LANG="${LANG:-en_US.UTF-8}" PATH="$PATH" TMPDIR="$TMPDIR" \
	REX_CONFIG="$root/init.lua" "$rex_bin" -S "$endpoint" server start --state-dir "$root/rex" \
	--env ATHENA_SERVER="$endpoint" --env ATHENA_BINARY="$rex_bin" >/dev/null || exit 1
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
out=$(HOME="$iso" ATHENA_TRUST=off ATHENA_CLAUDE="/usr/bin/env HOME=$iso $claude_bin" \
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
export ATHENA_TRUST=off
# A worker files its report with the command, this checkout's own: the installed athena on PATH may be older than it.
report() { printf 'Use the Bash tool to run exactly this command: %s report --status BLOCKED --verify none --concern %s -- then reply with one short sentence.' "$ar" "$1"; }
# The line workers started before the command typed, which must still reach status (the transition).
typed_report() { printf 'Reply with exactly this one line and nothing else: ATHENA-REPORT {"status":"BLOCKED","pr":"","head":"","verify":"none","decisions":[],"concerns":["%s"]}' "$1"; }

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
	check "$w answered: the report it filed with athena report reached status" wait_for 90 concern_is "$w" "first-$w"
done
"$ar" status --time

check "status --json lists all 3" test "$("$ar" status --json | /usr/bin/python3 -c "import json,sys; print(sum(r['name'].startswith('smoke-') for r in json.load(sys.stdin)))")" = 3

"$ar" watch --once >/dev/null
"$ar" send smoke-a "$(typed_report second-a)" >/dev/null
check "send reached smoke-a, and its typed ATHENA-REPORT line reached status" wait_for 60 concern_is smoke-a second-a
"$ar" send smoke-a "$(report third-a)" >/dev/null
check "and again" wait_for 60 concern_is smoke-a third-a
padding=$(/usr/bin/python3 -c 'print(" ".join("word%d" % n for n in range(400)))')
"$ar" send smoke-a "$(report long-a) These numbered words only make this message long: $padding" >/dev/null
check "a long prompt arrives as typed, not as pasted data" wait_for 90 concern_is smoke-a long-a
"$ar" watch --once >"$root/watch.out"
check "the watch told what changed since its last run" grep -q 'smoke-a' "$root/watch.out"
filed_by_command() { grep -q '"topic": "worker.state"' "$state/events.jsonl" && ! grep -q '"filed"' "$state/workers/smoke-c.json"; }
check "a Stop took smoke-c's filed report in" filed_by_command

# -- the palette: athena's actions in Rex's command palette, run with `rex do` on this server's own config. Questions go to
# scripts/rex-app.py, a stand-in for the Rex app that answers as told; with none connected, no app is there to ask.

app() { # answers..., then it serves for 12 seconds; its log is $root/app.log
	/usr/bin/python3 -I "$here/scripts/rex-app.py" "$endpoint" "$@" --for 12 >"$root/app.log" 2>&1 &
	app_pid=$!
	wait_for 5 grep -q '"type": "welcome"' "$root/app.log"
}
app_done() { kill "$app_pid" 2>/dev/null; wait "$app_pid" 2>/dev/null; }
do_palette() { # name, then rex do's KEY=VALUE pairs: its JSON or its error in $root/do.out, its exit status returned
	rx do "$@" --json >"$root/do.out" 2>&1
}
said() { grep -qF -- "$1" "$root/do.out"; }
answers() { wc -l <"$state/actions.jsonl" | tr -d ' '; }

"$ar" palette install --config "$root/init.lua" >"$root/install.1"
"$ar" palette install --config "$root/init.lua" >"$root/install.2"
check "palette install wrote its line into this server's config once" test "$(grep -c '(athena palette install)$' "$root/init.lua")" = 1
check "and a second install changed nothing" grep -q 'already loads them' "$root/install.2"
if ! grep -qF "state_dir = \"$state\"" "$root/init.lua"; then # every athena the actions run is this scratch state's, or none runs
	fail "the line names this run's scratch state dir"
	exit 1
fi
pass "the line names this run's scratch state dir"
rx config reload >/dev/null
check "the server loaded the three athena actions" test "$(rx actions | grep -c '^athena_')" = 3

d1=$("$ar" item add "Ship the palette's docs first?" --name "palette docs" --option Yes --option No --rec yes | cut -d' ' -f1)
d2=$("$ar" item add "Ship the dash's docs first?" --name "dash docs" --option Yes --option No --rec yes | cut -d' ' -f1)
d3=$("$ar" item add "Rename the palette?" --name "palette name" --option Keep --option Rename | cut -d' ' -f1)
d4=$("$ar" item add "Which test first?" --name "test order" --option Smoke --option Unit | cut -d' ' -f1)
d5=$("$ar" item add "Leave this one open?" --name "left open" --option Yes --option No | cut -d' ' -f1)
d6=$("$ar" item add "Docs or tests?" --name "docs or tests" --option Docs --option Tests | cut -d' ' -f1)
d7=$("$ar" item add "Ship tonight?" --name "ship tonight" --option Yes --option No | cut -d' ' -f1)
do_palette athena_answer item="$d1" option=yes
check "answer: rex do answers a decision from its arguments" said '"said": "Answered palette docs: Yes. athena takes it up'
"$ar" dash on >/dev/null
offered() { # the page, once it offers d2's answer: its token
	token=$(curl -s "http://127.0.0.1:2897/" | tee "$root/dash.html" | sed -n 's/.*name="athena-token" content="\([0-9a-f]*\)".*/\1/p')
	[ -n "$token" ] && grep -q "$d2" "$root/dash.html"
}
wait_for 20 offered
posted=$(curl -s -o "$root/posted" -w '%{http_code}' -X POST -H 'Content-Type: application/json' -H "X-Athena-Token: $token" \
	-H 'Origin: http://127.0.0.1:2897' --data "{\"item\": \"$d2\", \"option\": \"yes\"}" http://127.0.0.1:2897/api/action)
check "and the dashboard took the same answer to another decision" test "$posted" = 202
app "id:$d3" id:rename
do_palette athena_answer
app_done
check "answer: asked in the palette, a decision and then its option" said '"said": "Answered palette name: Rename.'
check "the decision question offered the decisions waiting" grep -q "\"id\": \"$d3\", \"subtitle\": \"Rename the palette?\"" "$root/app.log"
app "id:$d4" "text:unit first, then smoke"
do_palette athena_answer
app_done
check "answer: text typed instead of an option is a reply" said '"said": "Replied to test order: unit first, then smoke.'
asked() { grep -c '"client_prompt_requested"' "$root/app.log" | tr -d ' '; }
app "id:$d6"
do_palette athena_answer text="tests, given up front"
app_done
check "answer: a text given is the answer, and only the decision is asked" test "$(asked)" = 1 -a -n "$(said '"said": "Replied to docs or tests: tests, given up front.' && echo y)"
app "id:$d7"
do_palette athena_answer option=no
app_done
check "answer: an option given is the answer, and only the decision is asked" test "$(asked)" = 1 -a -n "$(said '"said": "Answered ship tonight: No.' && echo y)"
before=$(answers)
for case in "cancel|cancelled; nothing was answered" "unsupported|the Rex app can't show questions: update it; nothing was answered" \
	"silent|nobody answered in time; nothing was answered" "|no Rex app is connected to ask in; nothing was answered"; do
	answer=${case%%|*}
	[ -n "$answer" ] && app "$answer"
	do_palette athena_answer
	code=$?
	[ -n "$answer" ] && app_done
	check "answer, ${answer:-no app}: refused, and says so" test "$code" = 1 -a -n "$(said "${case#*|}" && echo y)"
done
check "and none of them queued anything" test "$(answers)" = "$before"
sleep 7 # past the dashboard's undo window, as HQ reads the queue
"$ar" ask queue >"$root/queue.json"
/usr/bin/python3 - "$root/queue.json" "$d1" "$d2" "$d3" "$d4" "$d5" "$d6" "$d7" <<'E'
import json, sys
queue = {e["item"]: e for e in json.load(open(sys.argv[1]))}
d1, d2, d3, d4, d5, d6, d7 = sys.argv[2:]
strip = lambda e: {k: v for k, v in e.items() if k not in ("seq", "at", "item", "ask")}
same = strip(queue[d1]) == strip(queue[d2]) and queue[d1]["option"] == {"id": "yes", "label": "Yes", "do": "reply"}
print(f"        queue: {d1} {strip(queue[d1])}; {d2} (the dashboard's) {strip(queue[d2])}")
given = queue[d6].get("reply") == "tests, given up front" and queue[d7]["option"]["id"] == "no"
sys.exit(0 if same and given and queue[d3]["option"]["id"] == "rename" and queue[d4]["reply"] == "unit first, then smoke" and d5 not in queue else 1)
E
check "athena ask queue holds each palette answer as it holds the dashboard's, and nothing for the one left open" test $? = 0
"$ar" dash off >/dev/null

space_b=$(field smoke-b "r['space']")
active() { rx session inspect "$space_b" --json | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin).get("active_window_id"))'; }
rx window focus -s "$space_b" "$(field smoke-a "r['window']")" >/dev/null
do_palette athena_open target=worker:smoke-b
check "open: a worker's window from plain rex do, with no app to show it, refused first" said 'showing a window needs the Rex app that asked'
check "and nothing moved" test "$(active)" = "$(field smoke-a "r['window']")"
app
client=$(sed -n 's/.*"id": "\(client:[^"]*\)".*"type": "welcome".*/\1/p' "$root/app.log")
rx -C "$client" do athena_open target=worker:smoke-b --origin palette --json >"$root/do.out" 2>&1 # as the palette runs it
app_done
check "open: a worker's window is shown in the app that asked" said '"said": "Showing smoke-b."'
check "smoke-b's window is its session's active one again" test "$(active)" = "$(field smoke-b "r['window']")"
check "and the app was asked to show that session" grep -q "\"action\": \"session.select\", \"args\": {\"session_id\": \"$space_b\"}" "$root/app.log"
do_palette athena_open target="item:$d5"
check "open: a page needs the real world's browser, and a scratch state dir has the door shut" said 'opening a browser is off here'
do_palette athena_open target=pr:https://example.com/x
check "open: only what athena offers" said 'is not something athena offers to open now'
for w in smoke-a smoke-b; do cp "$state/workers/$w.json" "$root/$w.before"; done
/usr/bin/python3 - "$state/workers" <<'E'
import json, sys
for name in ("smoke-a", "smoke-b"):  # both report the same pull request
    path = f"{sys.argv[1]}/{name}.json"
    data = json.load(open(path))
    data["report"] = {**(data.get("report") or {}), "pr": "https://github.com/acme/proj/pull/7"}
    json.dump(data, open(path, "w"))
E
app id:pr:https://github.com/acme/proj/pull/7
do_palette athena_open
app_done
check "open: two workers on one pull request offer it once, and Rex takes the list" said 'opening a browser is off here'
check "and the list held it once" test "$(grep -o '"id": "pr:https://github.com/acme/proj/pull/7"' "$root/app.log" | wc -l | tr -d ' ')" = 1
for w in smoke-a smoke-b; do cp "$root/$w.before" "$state/workers/$w.json"; done

do_palette athena_retire worker=nobody confirm=true
check "retire: only a live worker" said 'no live worker named nobody; nothing was retired.'
do_palette athena_retire worker=smoke-c
check "retire: without a confirm, and no app to confirm in, refused" test $? = 1 -a -n "$(said 'no Rex app is connected to ask in; nothing was retired' && echo y)"
app id:keep
do_palette athena_retire worker=smoke-c
app_done
check "retire: Keep keeps it" said 'smoke-c kept; nothing was retired.'
app cancel
do_palette athena_retire worker=smoke-c
app_done
check "retire: a closed question retires nothing" said 'cancelled; nothing was retired'
check "and smoke-c is still live" test "$(field smoke-c "r['phase']")" = live

# The signal sandbox: a script a worker's Claude runs can't signal a process outside it. The outsider is a sleep this script started,
# alone in its own process group. Inside, a direct SIGCONT to it and a SIGTERM to its whole group must both be refused, and it must
# live. No signal ever goes to -1; scripts/sandbox-proof.sh is the full proof.
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
"$ar" send smoke-a "This is this smoke test's own sandbox check: killall.py tries to signal a sleep process the test itself started, and records that the sandbox refuses it. Use the Bash tool to run exactly this command: /usr/bin/python3 $root/killall.py $outsider $root/killall.out -- then $(typed_report killall-done)" >/dev/null
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

pids="$(cat "$state/run/smoke-a.pid") $(cat "$state/run/smoke-c.pid")"
do_palette athena_retire worker=smoke-a confirm=true
check "smoke-a retired from rex do, confirm=true in place of the question" said '"said": "smoke-a retired'
app id:smoke-c id:retire
do_palette athena_retire
app_done
check "smoke-c retired from the palette: a worker, then Retire" said '"said": "smoke-c retired'
check "the worker question offered the live workers" grep -q '"id": "smoke-c"' "$root/app.log"

gone() { for p in $pids; do if ps -p "$p" >/dev/null 2>&1; then return 1; fi; done; return 0; }
check "their Claudes are gone" wait_for 10 gone
check "no sessions left on the private server" test "$(rx ls --json | /usr/bin/python3 -c "import json,sys; print(len(json.load(sys.stdin)['sessions']))")" = 0
check "no worktrees left" test "$(git -C "$root/t2/proj" worktree list | wc -l | tr -d ' ')" = 1
check "ledger says retired" test "$("$ar" status smoke-a smoke-b smoke-c --json | /usr/bin/python3 -c "import json,sys; print(sum(r['phase']=='retired' for r in json.load(sys.stdin)))")" = 3

# The palette's edges: no worker left to choose, and more open items than a pick may show (Rex takes at most 2,000).
do_palette athena_retire
check "retire, with no worker live: says so" said 'no worker is live; nothing was retired.'
cp "$state/needs-you.json" "$root/needs-you.before"
/usr/bin/python3 - "$here" <<'E'
import sys, time
sys.path.insert(0, sys.argv[1])
from athena.caps import items
data, now = items.load(), int(time.time())
data["items"] += [{"id": f"a{90000 + n}", "kind": "request", "text": f"filler {n}", "name": f"filler {n}", "next": "athena",
                   "options": [], "created": now, "updated": now, "log": []} for n in range(2100)]
items.save(data)
E
app id:item:a90000
do_palette athena_open
app_done
check "open, with over 2,000 open items: Rex took the first 2,000 as one pick" /usr/bin/python3 -c '
import json, sys
asked = [json.loads(l)["payload"]["payload"]["prompt"] for l in open(sys.argv[1]) if "client_prompt_requested" in l]
sys.exit(0 if len(asked) == 1 and len(asked[0]["items"]) == 2000 else 1)' "$root/app.log"
check "and the one chosen reached athena, which opens a page only through the door" said 'opening a browser is off here'
cp "$root/needs-you.before" "$state/needs-you.json"

# -- part 3: HQ on a real Claude and its rotation (Rex runs the sessions: nothing restarts or revives HQ)

hq_cwd="$root/hq"
mkdir -p "$hq_cwd"
/usr/bin/python3 - "$state/config.json" "$hq_cwd" <<'E'
import json, sys
cfg = json.load(open(sys.argv[1]))
cfg["projects_dir"] = sys.argv[2]
cfg["hq_model"] = "claude-haiku-5-5"  # every HQ smoke starts, the rotated one too: --model is for one open only
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
told_skill() { rx capture -s "$(hq_value space)" -b "$(hq_value block)" --trim | grep -q 'load the athena skill'; }
check "the fresh HQ was told to load the athena skill" wait_for 30 told_skill
check "HQ was launched with the skill's plugin" grep -q -- "--plugin-dir $here/plugin" "$state/run/smoke-hq.sh"
loaded_skill() { rx capture -s "$(hq_value space)" -b "$(hq_value block)" --trim | grep -q -E 'Skill\(athena|athena-hq:athena|Successfully loaded skill'; }
if wait_for 90 loaded_skill; then pass "and its Claude loaded the skill"; else fail "its Claude did not load the skill"; fi
rx capture -s "$(hq_value space)" -b "$(hq_value block)" --trim | grep -E 'athena skill|Skill|athena-hq:athena' | head -5 | sed 's/^/        hq: /'
check "and kept the old one on record" test "$(hq_value previous_session)" = "$first"
check "it wrote the handoff" test -s "$state/handoff.md"
if wait_for 120 hq_idle; then pass "HQ finished its first turn"; else
	fail "HQ finished its first turn"
	rx capture -s "$(hq_value space)" -b "$(hq_value block)" --trim | tail -15 | sed 's/^/        hq: /' # what it is at, to tell slow from stuck
fi
"$ar" hq stop >/dev/null
check "HQ stopped and its session closed" test "$(rx ls --json | /usr/bin/python3 -c "import json,sys; print(len(json.load(sys.stdin)['sessions']))")" = 0

say "$failures failures"
[ "$failures" = 0 ] || exit 1
say "smoke passed"
