<!-- "Docs updated" and "a non-draft PR": the evaluator could not tell what counts, and undrafting was refused. -->
# ABC-504: workers survive a terminal restart
GOAL: A restart of the terminal server leaves every worker running in its own column, and the board finds live terminals.
SCOPE: In: the resume registration, the board's terminal lookup, tests. Out: the dashboard.
CONTEXT: The board orphaned two columns after the last restart.
ACCEPTANCE:
- A restarted worker resumes with its own name, model and settings; a test proves the registered command.
- Docs updated.
- A non-draft PR on the athena repo.
VERIFY: `make test`.
TIMEBOX: 150 turns.
FORBIDDEN: restarting the live terminal server; merging.
NOTES: opus/high.
