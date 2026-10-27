<!-- A brief the checks pass: outcomes the worker can produce and show, local VERIFY, human steps kept apart. -->
# ABC-505: the deployed-tags job keeps each image under the tag limit
GOAL: The deployed-tags reconcile keeps each image's tag count bounded and never removes a tag a deploy still pins.
SCOPE:
In: scripts/deployed_tags.py, its workflow and its tests.
Out: running the script against the real registry; any cloud write; other repos.
CONTEXT:
- Ticket ABC-505 (read it). The failing run hit the 1000-tags-per-image cap.
ACCEPTANCE:
- Root cause explained in the PR body, with code references (file:line).
- Older deployed tags for the same namespace are superseded, so an unchanged image cannot accumulate unbounded tags.
- Unit tests cover an image already at 1000 tags and repeated deploys of the same digest.
- The deletion budget (--max-deletes) is respected, with a test.
VERIFY: `python3 -m pytest tests/test_deployed_tags.py` and a dry run against the recorded fixture: `python3 scripts/deployed_tags.py --dry-run --fixture tests/fixtures/registry.json`.
HUMAN-CHECKS:
- The human signs in to the registry console and confirms the tag count dropped after the next deploy.
TIMEBOX: 100 turns.
FORBIDDEN:
- any cloud call that writes or deletes;
- running the reconcile live;
- merging; posting to chat.
NOTES:
- An autonomous hotfix: the PR is opened as a draft.
