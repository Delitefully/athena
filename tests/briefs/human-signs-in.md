<!-- Acceptance needed the human to sign in: 112 hours parked in NEEDS_CONTEXT, and "not met" verdicts throughout. -->
# ABC-503: work-account support for the chat adapter
GOAL: Work-account chats are captured by the extension's chat adapter, with tests, as one draft PR.
SCOPE: In: the chat adapter for work accounts, its fixtures and tests. Out: schema changes.
CONTEXT: Base: build on #683.
ACCEPTANCE:
- A work-account turn (prompt plus answer, and title and model where available) decodes from recorded and scrubbed fixtures, with tests for: new chat, follow-up, stop, resume on a new socket, attachment name only, and malformed frames.
- No turn is emitted twice when both tenants' code paths are loaded.
- PR env: a real work-account turn shows as a session on the Chats page with the right app and the account email. The human signs in to your throwaway browser; ask athena for that with a BLOCKED/NEED report when you're ready.
VERIFY:
- In packages/extension: `pnpm test && pnpm typecheck && pnpm lint`.
- CI green.
TIMEBOX: 250 turns.
FORBIDDEN:
- signing in to anything yourself (the human signs in);
- merging; touching prod; schema migrations without asking.
NOTES:
- Follow the provider layout.
