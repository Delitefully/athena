<!-- VERIFY waited on CI and on tests that already failed on main; the GOAL asked for "merge". BLOCKED, then a loop. -->
# ABC-502: get #640 merge-ready (host lookup index and single statement, connection cache)
GOAL: PR #640 is rebased on current main, conflict-free, CI green, its review comments handled, and its description current, so it is ready for review and merge.
SCOPE:
- In: rebase onto origin/main keeping only this PR's commits; resolve the conflicts; re-check the original goals against the rebased code; update the PR body.
- Out: the partition default, Kafka topics, other PRs, new features.
CONTEXT:
- ABC-502: read it.
ACCEPTANCE:
- `gh pr view 640 --json mergeable,commits` shows MERGEABLE with only this PR's commits.
- The PR's tests (one query serves both lookups; title-only skip; cache hit, expiry, negative not cached, concurrency) pass on the rebased code; show the output.
- The report lists the conflict resolutions, review threads fixed and declined, and any adaptation.
VERIFY: go test for the touched packages including integration, lint and gofmt, and `gh pr checks 640`. Show the output.
TIMEBOX: 150 turns or 3 hours.
FORBIDDEN:
- Applying schema to any environment; staging, prod or cloud.
- Merging, auto-merge, labelling.
NOTES:
- Push early (force-with-lease on your branch only).
