#!/bin/bash
# The Pi worker's live proof: one worker spawned by athena with --agent pi, on the real Pi with its own default model, on a private Rex
# server of its own, a sanitized scratch state dir and a scratch repo with a local bare origin. Never the live server or state.
#
#   scripts/pi-proof.sh [--keep]
#
# The state dir is a fresh, empty one, made here; nothing is read from live state.
#
# What passes: spawn launches Pi in the worker's window with its brief and goal; status shows it working, marked [pi]; it adds a
# file, commits, runs its VERIFY and files DONE with `athena report`, which status and the watch show; `athena send` reaches it
# through its inbox and it files again; retire stops it (its Pi is gone) and removes its worktree.
set -u
here=$(cd "$(dirname "$0")/.." && pwd -P)
ar="$here/bin/athena"
rex_bin=${ATHENA_BINARY:-$(cd "$here" && /usr/bin/python3 -B -c 'from athena.kernel import config; print(config.rex_binary())')}
keep=0
[ "${1:-}" = --keep ] && keep=1
command -v pi >/dev/null || { echo "pi-proof: pi is not on PATH" >&2; exit 3; }

root=/private/tmp/claude-$(id -u)/arpi$$
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
		sleep 2
	done
	return 1
}
cleanup() {
	code=$?
	pid=$(cat "$state/run/pi-a.pid" 2>/dev/null)
	if [ "${pid:-0}" -gt 1 ] && [ "$(ps -o command= -p "$pid" 2>/dev/null | awk '{print $1}')" = pi ]; then
		kill -KILL "$pid" 2>/dev/null
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

# A fresh, empty scratch state, marked as scratch: nothing is copied from live state, not even a snapshot of it.
mkdir -p "$state"
: >"$state/.athena-scratch"
printf '{"projects_dir": "%s", "worktree_root": "%s", "rex": {"dash_port": 2899}}\n' "$root" "$root/wt" >"$state/config.json"
export ATHENA_STATE_DIR="$state"
export ATHENA_SERVER="$endpoint"
export ATHENA_BINARY="$rex_bin"
export ATHENA_HQ=proof-hq
export ATHENA_TRUST=off
unset ATHENA_CONFIG ATHENA_ROLE ATHENA_WORKER CLAUDE_CONFIG_DIR

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
git -C "$root/proj" config user.name proof
git -C "$root/proj" config user.email proof@localhost

cat >"$root/brief.md" <<'BRIEF'
# pi-proof: one file

GOAL: the repository has a file `hello.txt` whose only line is `hello from pi`, committed on your branch.

SCOPE:
- In: that one file, one commit.
- Out: everything else. This repo has no GitHub remote, so no PR and no push.

CONTEXT: a scratch repository made for a proof; nothing else is in it.

ACCEPTANCE:
- `hello.txt` exists with the one line `hello from pi`, and it is committed (git status is clean).

VERIFY: `test "$(cat hello.txt)" = "hello from pi" && git diff --quiet HEAD` exits 0.

TIMEBOX: 10 minutes.

FORBIDDEN: anything outside this worktree; any push.

NOTES: no PR here. Skip anything in your rules about PRs, review threads and CI.
BRIEF

shown() { sed -e "s#$here#<checkout>#g" -e "s#$HOME#~#g" -E -e "s#'PATH=[^']*'#PATH=<checkout>/bin:...#"; } # no home paths in what it prints
field() { # python expression over r, the worker's status row
	"$ar" status pi-a --json --live | /usr/bin/python3 -c "import json,sys; r=json.load(sys.stdin)[0]; print($1)"
}
report_is() { [ "$(field "(r.get('report') or {}).get('status')")" = "$1" ]; }
concern_has() { [ "$(field "'$1' in ((r.get('report') or {}).get('concerns') or [])")" = True ]; }
pi_gone() { pid=$(cat "$state/run/pi-a.pid" 2>/dev/null); ! ps -o command= -p "${pid:-1}" 2>/dev/null | grep -q '^pi'; }

"$ar" watch --once --tagged >/dev/null 2>&1 # the watch's first pass: what it knows before the worker
say "\$ athena spawn --agent pi --repo proj --branch t/pi-proof --name pi-a --brief brief.md --goal ..."
"$ar" spawn --agent pi --repo "$root/proj" --branch t/pi-proof --name pi-a --brief "$root/brief.md" --no-fetch --force \
	--goal "hello.txt holds the line 'hello from pi', is committed, VERIFY passes, and a DONE report is filed." >"$root/spawn.out" 2>&1
shown <"$root/spawn.out"
say "its launcher: $(grep '^exec ' "$state/run/pi-a.sh" | shown)"
check "Pi was launched in the worker's window, with athena's extension and the Pi worker prompt" \
	grep -q -- " pi --name pi-a --session-id .* --approve -e .*/pi-extension/athena.ts --append-system-prompt .*/prompts/worker-pi.md$" "$state/run/pi-a.sh"
check "no --model: Pi's own configured default runs" sh -c "! grep -q -- '--model' '$state/run/pi-a.sh'"
check "the worker runs in its own worktree" test "$(field "r['path']")" = "$(cd "$root/wt/proj/pi-proof" 2>/dev/null && pwd -P)"
say "\$ athena status pi-a"
"$ar" status pi-a | head -1
"$ar" watch --once --tagged >/dev/null 2>&1 # the watch sees it working, so its report is a change it tells
check "status marks it [pi]" sh -c "'$ar' status | grep -q 'pi-a .*\\[pi\\]'"
check "pi-a filed DONE with athena report" wait_for 420 report_is DONE
say "\$ athena status pi-a"
"$ar" status pi-a
check "its report names its commit and a passing verify" \
	test "$(field "(r.get('report') or {}).get('head','')[:7] == __import__('subprocess').run(['git','-C',r['path'],'rev-parse','HEAD'],capture_output=True,text=True).stdout[:7] and '-> pass' in (r.get('report') or {}).get('verify','')")" = True
check "hello.txt was committed on t/pi-proof" sh -c "[ \"\$(git -C '$root/wt/proj/pi-proof' show HEAD:hello.txt)\" = 'hello from pi' ]"
say "\$ athena watch --once --tagged"
"$ar" watch --once --tagged 2>/dev/null | grep 'pi-a' | tee "$root/watch.out"
check "the watch told its report" grep -q '"pi-a report DONE"' "$root/watch.out"

say "\$ athena send pi-a \"...\""
"$ar" send pi-a "HQ here, checking that messages reach you: file your report again, with the same status, verify and lines as before plus one more: --concern proof-message. Then end your turn."
check "pi-a got HQ's message and filed again" wait_for 180 concern_has proof-message
"$ar" status pi-a | head -1

gated() { grep '"topic": "worker.gate"' "$state/events.jsonl" 2>/dev/null | grep -q '"name": "pi-a"'; }
say "\$ athena send pi-a \"... end this turn without a report ...\""
"$ar" send pi-a "HQ here, testing athena's report gate: reply with just the word ok and end this turn without running athena report. When athena sends the turn back, file your report again as before (same status, verify and lines), then end your turn."
check "the gate sent back a Pi turn that ended with no report" wait_for 120 gated
check "and the worker filed its report again" wait_for 180 report_is DONE
say "the gate's events: $(grep '"worker.gate"' "$state/events.jsonl" | cut -c1-160)"
"$ar" status pi-a | head -1

inbox_empty() { [ -z "$(ls -A "$state/run/inbox/pi-a" 2>/dev/null)" ]; }
check "Pi took every message: its inbox holds nothing claimed, failed or waiting" wait_for 20 inbox_empty
say "\$ athena retire pi-a --force"
"$ar" retire pi-a --force
check "pi-a retired" test $? = 0
check "its Pi is gone" wait_for 20 pi_gone
check "its worktree is gone" test ! -e "$root/wt/proj/pi-proof"

if [ "$failures" = 0 ]; then say "pi-proof: all passed"; else say "pi-proof: $failures failed"; fi
[ "$failures" = 0 ]
